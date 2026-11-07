import asyncio
import contextvars
import base64
import json
import logging
import mimetypes
import os
import random
import re
import threading
import sqlite3
import string
import time
import uuid
from datetime import datetime, timedelta, timezone

import aiohttp
import deepseek_tokenizer
import wasmtime
try:
    from playwright.async_api import async_playwright
except ImportError:
    async_playwright = None

logger = logging.getLogger("deeperseeker.functions")

# B12 (Stage 1 audit): all relative runtime paths are anchored to the package
# directory — the old global os.chdir(BASE_DIR) at import mutated process-wide
# state (hostile to embedding, testing and PyPI packaging).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

wasm_path = os.path.join(BASE_DIR, "wasm", "deepseek_pow_solver.wasm")
_session = None
_db = os.getenv("DB_PATH") or os.path.join(BASE_DIR, "deeperseeker.db")


def data_dir():
    """Directory holding the SQLite database (and runtime files stored next to
    it, e.g. the generated API key). Honors DB_PATH; anchored to the package
    directory by default so nothing depends on the process CWD."""
    return os.path.dirname(os.path.abspath(_db))


def cookie_file_path():
    """Resolve where the DeepSeek cookie file lives.

    Order: DEEPSEEKER_COOKIE_PATH env > the target of a legacy Docker symlink
    > next to the real DB file (which honors DB_PATH) > the package directory.
    Writing goes to the RESOLVED path so os.replace() can never destroy a
    symlink that bridges the file into the persistent data volume.
    """
    p = os.getenv("DEEPSEEKER_COOKIE_PATH")
    if p:
        return p
    p = os.path.join(BASE_DIR, "aws_cookies_deepseek.json")
    try:
        if os.path.islink(p):
            target = os.path.realpath(p)
            if target:
                return target
    except Exception:
        pass
    d = os.path.dirname(os.path.abspath(_db))
    if d and os.path.abspath(d) != os.path.abspath(os.getcwd()):
        return os.path.join(d, "aws_cookies_deepseek.json")
    return p

try:
    _TZ_OFFSET = str(int(datetime.now().astimezone().utcoffset().total_seconds()))
except Exception:
    _TZ_OFFSET = "19800"


_DB_TLS = threading.local()


class _PooledConn:
    """SQLite connection handle (reused within a thread).

    get_db() used to call sqlite3.connect() and run PRAGMA journal_mode on
    every call — roughly 15ms each on slow CPUs, and a single request calls
    it 6-10 times. Connections are now reused per thread; close() just
    returns the handle to the pool without actually closing it, so the
    caller-visible semantics are unchanged.
    """
    __slots__ = ("_conn",)

    def __init__(self, conn):
        self._conn = conn

    def close(self):
        try:
            if self._conn.in_transaction:
                self._conn.rollback()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_conn"), name)


def get_db():
    conn = getattr(_DB_TLS, "conn", None)
    # [v57 fix] Connections are cached with a composite key (db path, thread).
    # The v46 original cached by thread only: if _db were ever swapped (test
    # fixtures like functions._db = tmp/xxx.db, or future multi-DB/migration
    # scenarios), the pool would hand back handles belonging to the old DB —
    # init_db would create tables in the old DB too, the new path would stay
    # empty forever, and every subsequent read/write would silently land in
    # the wrong database. In production _db never changes after startup, so
    # the extra string comparison has no measurable cost.
    if conn is None or getattr(_DB_TLS, "db_key", None) != _db:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        conn = sqlite3.connect(_db, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        _DB_TLS.conn = conn
        _DB_TLS.db_key = _db
    return _PooledConn(conn)


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alias TEXT,
            token TEXT,
            status TEXT DEFAULT 'ACTIVE'
        );
        CREATE TABLE IF NOT EXISTS sessions (
            signature TEXT PRIMARY KEY,
            token_id INTEGER,
            deepseek_session_id TEXT,
            parent_message_id INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS session_map (
            old_session TEXT PRIMARY KEY,
            new_session TEXT,
            token_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS files (
            file_id TEXT PRIMARY KEY,
            token_id INTEGER,
            created_at REAL
        );
        CREATE TABLE IF NOT EXISTS chat_memory (
            session_id TEXT PRIMARY KEY,
            baseline INTEGER DEFAULT 0,
            created_at REAL
        );
    """)
    # B3 (Stage 1 audit) migration: pool cooldown + usage tracking. Older
    # databases miss the columns; the ALTERs are idempotent behind the
    # pragma check.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(tokens)").fetchall()}
    if "rate_limited_until" not in existing_cols:
        conn.execute("ALTER TABLE tokens ADD COLUMN rate_limited_until REAL")
    if "last_used" not in existing_cols:
        conn.execute("ALTER TABLE tokens ADD COLUMN last_used REAL")
    # [v77 P0-3 port] Consecutive-403 counter: number of 403s with no
    # successful turn in between. Reset by mark_active() and restore_token().
    # [v77.3] rollover storm fix: record "the client-side total history at
    # session creation" and use it as the growth reference instead of
    # baseline (the first real token). A rollover-summary session has a tiny
    # baseline that represents all the old history; using baseline as the
    # reference counts history that never entered the session as "growth",
    # triggering a rollover every turn (confirmed in production 2026-10-27
    # 13:32-14:08: 17 consecutive rollovers, one every 1.5-2 minutes).
    existing_cm_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(chat_memory)").fetchall()
    }
    if "creation_total" not in existing_cm_cols:
        conn.execute(
            "ALTER TABLE chat_memory ADD COLUMN creation_total REAL NOT NULL DEFAULT 0"
        )
    # [v77.5/B] Rolling handoff summary: rolling_summary = accumulated
    # continuation notes; rolling_marker = the client-history token position
    # digested so far (same estimation basis as the digest slice).
    if "rolling_summary" not in existing_cm_cols:
        conn.execute(
            "ALTER TABLE chat_memory ADD COLUMN rolling_summary TEXT NOT NULL DEFAULT ''"
        )
    if "rolling_marker" not in existing_cm_cols:
        conn.execute(
            "ALTER TABLE chat_memory ADD COLUMN rolling_marker REAL NOT NULL DEFAULT 0"
        )
    if "consecutive_403" not in existing_cols:
        conn.execute(
            "ALTER TABLE tokens ADD COLUMN consecutive_403 INTEGER NOT NULL DEFAULT 0"
        )
    # [v89.6] Quarantine reason persistence: mark_quarantined() stores its
    # reason here; the dashboard separates "likely expired / likely muted /
    # other" by it and shows the concrete reason. Written only on the
    # quarantine path — zero contact with normal request handling.
    if "quarantine_reason" not in existing_cols:
        conn.execute("ALTER TABLE tokens ADD COLUMN quarantine_reason TEXT")
    # [v89.12] bridge-native login (optional module) metadata columns: native
    # tokens record their source account and password (for auto re-login).
    # Passwords live only in the local DB — never logged, never returned.
    # Missing/empty columns keep the module fully inert.
    if "account" not in existing_cols:
        conn.execute("ALTER TABLE tokens ADD COLUMN account TEXT")
    if "password" not in existing_cols:
        conn.execute("ALTER TABLE tokens ADD COLUMN password TEXT")
    if "last_login" not in existing_cols:
        conn.execute("ALTER TABLE tokens ADD COLUMN last_login REAL")
    # [v89.6] Feature toggles (read/written from the admin dashboard):
    # a single key-value table. First toggle: effort_enhance — the prompt
    # enhancement (effort instructions) master switch, defaulting to ON
    # (restoring the v77 design intent); when OFF the chat path never sets
    # the request_effort ContextVar, byte-identical to pre-toggle behavior.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    # [v77 P0-6 port] Usage stats: one row per completed turn that is
    # "client-visible success", aggregated by day x masked key at read time.
    # Raw rows are kept instead of pre-aggregated counters — failed attempts
    # have no row to record, which makes "a retry counts only once" a
    # structural rule.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            day TEXT NOT NULL,
            key_alias TEXT NOT NULL,
            surface TEXT NOT NULL,
            model TEXT,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0
        );
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_usage_day_key ON usage(day, key_alias)"
    )
    existing_session_cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    if "last_used" not in existing_session_cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN last_used REAL")
    conn.commit()
    conn.close()
    try:
        prune_sessions()
    except Exception:
        logger.exception("Startup session pruning failed (non-fatal)")


_session_lock = asyncio.Lock()


async def get_session():
    global _session
    if _session is None or _session.closed:
        async with _session_lock:
            if _session is None or _session.closed:
                logger.info("Opening shared aiohttp ClientSession")
                # Long-lived keepalive pool: the default 15s keepalive is too
                # short — gaps between user turns (typing/thinking) commonly
                # exceed it, forcing a fresh TLS handshake (1-2s) every turn.
                # 10-minute keepalive plus DNS caching keeps consecutive
                # conversation turns on warm connections (memory cost: a few
                # MB of sockets).
                connector = aiohttp.TCPConnector(
                    limit=64,
                    keepalive_timeout=600,
                    ttl_dns_cache=300,
                    use_dns_cache=True,
                )
                _session = aiohttp.ClientSession(
                    connector=connector,
                    timeout=aiohttp.ClientTimeout(total=None, connect=15, sock_read=600),
                )
    return _session


async def close_session():
    """Close the shared aiohttp ClientSession (Stage 1 minor list).

    Called from app lifespan shutdown: without it, every uvicorn reload/exit
    leaked the session's connector sockets and logged 'Unclosed client
    session' warnings."""
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


# ==============================================================================
# Stage 0.4 — Dual-endpoint failover
#
# Every upstream POST goes through post_with_failover(): if the primary host
# fails at the connection level (ClientError / timeout) or answers HTTP 5xx,
# the identical request is replayed against the fallback endpoint. 4xx
# responses are returned as-is — a bad token or permission error will not
# improve on a different endpoint, so we fail fast instead of retrying.
#
# Endpoints (env, both optional to override):
#   DEEPSEEKER_UPSTREAM_BASE     primary   (default https://chat.deepseek.com)
#   DEEPSEEKER_UPSTREAM_FALLBACK fallback  (default: none; single-endpoint mode)
# ==============================================================================

def _build_upstream_bases():
    primary = (os.getenv("DEEPSEEKER_UPSTREAM_BASE") or "https://chat.deepseek.com").strip().rstrip("/")
    fallback = (os.getenv("DEEPSEEKER_UPSTREAM_FALLBACK") or "").strip().rstrip("/")
    return [primary] + ([fallback] if fallback else [])


UPSTREAM_BASES = _build_upstream_bases()


async def post_with_failover(path, *, headers, session=None, header_timeout=None, **kwargs):
    """POST to an upstream endpoint with automatic failover across
    UPSTREAM_BASES. This is a plain coroutine that RETURNS an owned response —
    it is not an async context manager:

        resp = await post_with_failover(...)
        async with resp:
            ...

    The caller owns and closes the returned response (async with). Raises the
    last connection error if every endpoint is unreachable, or returns the
    final 4xx/5xx response when the last endpoint answers with one (callers
    keep their existing error paths). `session` is injectable for tests;
    defaults to the shared ClientSession.

    [v63] `header_timeout` (seconds, default None = disabled) adds an
    independent budget for the "waiting for response headers" phase: long
    streaming requests already have the caller's own stall protection for
    body reads, but if the upstream "accepts the POST yet never returns
    response headers", the old behavior could only sit on the session-level
    sock_read timeout (600s in production — a 2026-10-24 incident caused a
    12-minute unresponsive window). When this parameter is given, each
    endpoint's POST is wrapped in asyncio.wait_for; on expiry it is
    cancelled and falls through the existing except branch into failover.
    Without it, behavior is byte-for-byte identical to the previous
    version."""
    if session is None:
        session = await get_session()
    last_exc = None
    for attempt, base in enumerate(UPSTREAM_BASES):
        is_last = attempt == len(UPSTREAM_BASES) - 1
        try:
            if header_timeout is not None:
                # [v63] Independent header-phase budget (S8): wait_for
                # cancels the POST as soon as it fires; asyncio.TimeoutError
                # lands in the existing except branch and triggers failover.
                # Verified: after cancellation the connection is closed and
                # removed correctly, no pool accumulation; on the normal path
                # the POST returns in milliseconds and wait_for costs nothing
                # (0.000s).
                resp = await asyncio.wait_for(
                    session.post(base + path, headers=headers, **kwargs),
                    timeout=header_timeout)
            else:
                resp = await session.post(base + path, headers=headers, **kwargs)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning("Upstream POST %s failed on %s: %s", path, base, e)
            last_exc = e
            if is_last:
                raise
            continue
        if resp.status >= 500 and not is_last:
            try:
                body = await resp.text()
            except (aiohttp.ClientError, asyncio.TimeoutError):
                body = ""
            # PR #26 review fix (Medium): hand the failed connection back to
            # the pool. Without this, repeated failovers leak responses and
            # eventually exhaust the connector.
            resp.release()
            logger.warning(
                "Upstream POST %s -> HTTP %d on %s; failing over to fallback endpoint",
                path, resp.status, base,
            )
            last_exc = UpstreamError(resp.status, body[:200])
            continue
        if attempt > 0:
            logger.info("Upstream POST %s succeeded on fallback endpoint %s", path, base)
        return resp
    raise last_exc if last_exc is not None else RuntimeError("post_with_failover: no endpoints configured")


# B11 (Stage 1 audit): the Android client identity used to be hardcoded to one
# version for ALL tokens — when DeepSeek's app moves, a single stale string
# degrades every account simultaneously, and identical fingerprints correlate
# bans. The version is env-configurable now; per-token identity profiles
# (rotating UA/version/locale) remain a P1.
DEEPSEEKER_CLIENT_VERSION = os.getenv("DEEPSEEKER_CLIENT_VERSION", "2.4.5")


def get_headers(auth_token, pow=None):
    headers = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "origin": "https://chat.deepseek.com",
        "referer": "https://chat.deepseek.com/",
        "user-agent": "Dalvik/2.1.0 (Linux; U; Android 14; Pixel 7)",
        "x-client-platform": "android",
        "x-client-version": DEEPSEEKER_CLIENT_VERSION,
        "x-client-locale": "en_US",
        "x-client-bundle-id": "com.deepseek.chat",
        "x-client-timezone-offset": _TZ_OFFSET,
    }
    if auth_token:
        headers["authorization"] = f"Bearer {auth_token}"
    if pow:
        headers["x-ds-pow-response"] = pow
    return headers


# ==============================================================================
# BACKUP WAF COOKIE GENERATION (DEPRECATED IN FAVOR OF ANDROID CLIENT HEADERS)
#
# DeepSeek's backend does not enforce AWS WAF on requests using Android client
# headers. The Playwright/Chromium cookie generation below is retained as a backup
# so that if DeepSeek ever tightens WAF rules in the future, it can easily be
# re-enabled simply by uncommenting cookies=cookie in API calls.
# ==============================================================================

_cookie_lock = asyncio.Lock()

COOKIE_REGEN_ATTEMPTS = int(os.getenv("DEEPSEEKER_COOKIE_ATTEMPTS", "2"))
COOKIE_REGEN_TIMEOUT = float(os.getenv("DEEPSEEKER_COOKIE_TIMEOUT", "120"))
COOKIE_FAIL_COOLDOWN = float(os.getenv("DEEPSEEKER_COOKIE_COOLDOWN", "20"))

_cookie_fail = {"until": 0.0, "error": ""}


class CookieGenerationError(Exception):
    """Raised when the DeepSeek WAF cookie file cannot be produced."""


class UpstreamError(Exception):
    """Upstream DeepSeek failure carrying the HTTP status and body (B6).

    Replaces the old Exception(f"HTTP {status}: ...") string protocol whose
    'HTTP (\\d{3}):' regex parsing in app.py broke token rotation and error
    mapping whenever a wording changed. Callers read .status directly; str()
    keeps the familiar 'HTTP <status>: <body>' shape for logs."""

    def __init__(self, status, body=""):
        self.status = int(status)
        self.body = str(body)
        super().__init__(f"HTTP {self.status}: {self.body}")


class UpstreamBizError(UpstreamError):
    """[v88] HTTP 200 whose envelope carries a business-level error.

    DeepSeek answers several endpoints with 200 + {"code": N, "data": null}
    (or data.biz_data: null) when the account is flagged, its web session was
    killed, or the per-account "messages too frequent" wall is hit. The old
    code indexed straight into biz_data and died with TypeError, which is not
    an UpstreamError — so the token never rotated and the dead account stayed
    pinned forever. Subclassing UpstreamError lets _upstream_http_code() and
    handle_chat's 401/403/429 rotation pick it up unchanged.
    (Adapted from upstream PR #94 by Rrainich.)"""


def _format_mute_until(ts):
    """[v89.7] mute_until (upstream epoch seconds) -> human-readable date.

    Pinned to UTC+8 (the user's timezone) instead of the container's local
    timezone — the hosting container's TZ is not under our control, and a
    wrong date would corrupt the "restore after unmute" guidance."""
    try:
        return datetime.fromtimestamp(
            float(ts), timezone(timedelta(hours=8)),
        ).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "unknown"


class UpstreamMutedError(UpstreamBizError):
    """[v89.7] Upstream explicitly reports the account is muted
    (data.biz_data.is_muted == 1, nested inside an outer code=0 envelope).

    Hard evidence — a boolean field from the upstream itself, not a text
    heuristic — so unlike the v89.3 auth-shaped soft path there is no
    multi-strike grace period: a mute lasts days (observed mute_until spans
    7 days), cooldown rotation would just keep re-arming a doomed account
    every 60s and feeding failures to users. Hit = immediate quarantine
    (same tier as a hard 401); restore_token() remains the only exit."""

    def __init__(self, mute_until=None):
        self.mute_until = mute_until
        super().__init__(
            403, f"user is muted (until {_format_mute_until(mute_until)})",
        )


def _biz_data(payload, what="response"):
    """Return payload["data"]["biz_data"] or raise UpstreamBizError.

    Auth-shaped business messages map to 401 (quarantine/rotate); everything
    else maps to 429 (cool down and rotate). Both are statuses handle_chat
    already understands."""
    data = payload.get("data") if isinstance(payload, dict) else None
    biz = data.get("biz_data") if isinstance(data, dict) else None
    if biz:
        return biz
    payload = payload if isinstance(payload, dict) else {}
    code = payload.get("code")
    msg = payload.get("msg") or payload.get("message") or "empty biz_data"
    text = str(msg)
    # [v89.6] Mute/restriction keywords first: a hit maps to 403 — riding
    # mark_limited()'s 403 channel (cooldown + quarantine only after 3
    # consecutive), after which the dashboard shows "likely muted". Fixes
    # muted accounts cycling cooldown and looking healthy on the dashboard.
    # Long words only, to avoid substring collateral (banned, not ban —
    # bandwidth; muted, not mute). Note the order: if a mute message happens
    # to contain token/session words, mute still wins (the more precise
    # diagnosis).
    if re.search(r"禁言|违禁|违规|banned|muted|suspended|restricted|violation|violating", text, re.I):
        status = 403
    elif re.search(r"login|auth|token|session|expire", text, re.I):
        status = 401
    else:
        status = 429
    raise UpstreamBizError(status, f"{what}: code={code} msg={msg}")


def _read_cookie_file():
    """Return valid (unexpired) cookies, or None."""
    try:
        with open(cookie_file_path()) as f:
            c = json.load(f)
        if c.get("expiry") is not None and c["expiry"] > time.time():
            return c["cookie"]
    except Exception:
        pass
    return None


def _read_stale_cookie_file():
    """Return cookies even if expired (last-resort fallback)."""
    try:
        with open(cookie_file_path()) as f:
            c = json.load(f)
        return c.get("cookie") or None
    except Exception:
        return None


async def get_cookies():
    cookies = _read_cookie_file()
    if cookies:
        return cookies

    def _cooldown_error():
        return CookieGenerationError(
            _cookie_fail["error"] + " (cooling down; will retry automatically — try again shortly)"
        )

    if time.time() < _cookie_fail["until"]:
        stale = _read_stale_cookie_file()
        if stale:
            return stale
        raise _cooldown_error()
    async with _cookie_lock:
        cookies = _read_cookie_file()
        if cookies:
            return cookies
        if time.time() < _cookie_fail["until"]:
            stale = _read_stale_cookie_file()
            if stale:
                return stale
            raise _cooldown_error()
        last_err = None
        for attempt in range(1, COOKIE_REGEN_ATTEMPTS + 1):
            try:
                logger.info("Generating DeepSeek cookies (attempt %d/%d)...", attempt, COOKIE_REGEN_ATTEMPTS)
                await asyncio.wait_for(_generate_cookies(), timeout=COOKIE_REGEN_TIMEOUT)
                cookies = _read_cookie_file()
                if cookies:
                    _cookie_fail["until"] = 0.0
                    _cookie_fail["error"] = ""
                    return cookies
                last_err = CookieGenerationError("cookie file missing/invalid after generation")
            except Exception as e:
                last_err = e
                logger.warning("DeepSeek cookie generation attempt %d/%d failed: %s", attempt, COOKIE_REGEN_ATTEMPTS, e)
            if attempt < COOKIE_REGEN_ATTEMPTS:
                await asyncio.sleep(min(5 * attempt, 10))
        _cookie_fail["until"] = time.time() + COOKIE_FAIL_COOLDOWN
        _cookie_fail["error"] = f"Could not generate DeepSeek cookies: {last_err}"
        logger.error("%s", _cookie_fail["error"])
        stale = _read_stale_cookie_file()
        if stale:
            logger.warning("Serving STALE DeepSeek cookies after generation failure (upstream may reject them)")
            return stale
        raise CookieGenerationError(_cookie_fail["error"])


async def _generate_cookies():
    if async_playwright is None:
        raise CookieGenerationError("playwright is not installed")
    launch_kwargs = {"headless": False}
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        launch_kwargs["args"] = ["--no-sandbox"]
    async with async_playwright() as p:
        browser = await p.chromium.launch(**launch_kwargs)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto("https://chat.deepseek.com/", wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_selector("body", timeout=30000)
            try:
                await page.wait_for_url("**/sign_in*", timeout=30000)
            except Exception:
                pass
            cookies = await context.cookies()
        finally:
            await browser.close()
    final_cookies = {}
    expiry = None
    for i in cookies:
        if i.get("name") == "aws-waf-token":
            expiry = i.get("expires")
        final_cookies[i["name"]] = i["value"]
    final_cookies["ds_cookie_preference"] = "%257B%2522level%2522%253A%2522all%2522%257D"
    if not expiry or expiry < 0:
        expiry = time.time() + 1800
    target = cookie_file_path()
    target_dir = os.path.dirname(os.path.abspath(target))
    os.makedirs(target_dir, exist_ok=True)
    tmp_path = os.path.join(target_dir, os.path.basename(target) + ".tmp")
    with open(tmp_path, "w") as f:
        f.write(json.dumps({"cookie": final_cookies, "expiry": expiry}))
    os.replace(tmp_path, target)
    logger.info("DeepSeek cookies saved to %s (expires %s)", target, datetime.fromtimestamp(expiry) if expiry else "n/a")


def get_auth_token():
    conn = get_db()
    row = conn.execute("SELECT token FROM tokens LIMIT 1").fetchone()
    conn.close()
    if row:
        return row[0]
    return None


def add_token(token, alias=None):
    conn = get_db()
    if not conn.execute("SELECT 1 FROM tokens WHERE id = 1").fetchone():
        next_id = 1
    else:
        row = conn.execute("""
            SELECT min(t1.id + 1)
            FROM tokens t1
            LEFT JOIN tokens t2 ON t1.id + 1 = t2.id
            WHERE t2.id IS NULL
        """).fetchone()
        next_id = row[0] if row and row[0] else 1
    conn.execute("INSERT INTO tokens (id, alias, token, status) VALUES (?, ?, ?, 'ACTIVE')", (next_id, alias, token))
    conn.commit()
    conn.close()


def get_tokens():
    conn = get_db()
    rows = conn.execute("SELECT id, alias, token, status FROM tokens").fetchall()
    conn.close()
    return [{"id": r[0], "alias": r[1], "token": r[2], "status": r[3]} for r in rows]


def get_token(token_id):
    conn = get_db()
    row = conn.execute("SELECT id, alias, token, status FROM tokens WHERE id = ?", (token_id,)).fetchone()
    conn.close()
    if row:
        return {"id": row[0], "alias": row[1], "token": row[2], "status": row[3]}
    return None


def delete_token(token_id):
    conn = get_db()
    conn.execute("DELETE FROM tokens WHERE id = ?", (token_id,))
    conn.commit()
    conn.close()


# ==============================================================================
# [v89.12] Bridge-native login (optional module)
#
# Background: since November, upstream revokes whole sessions when a
# browser-born token is used from another environment — the token dies within
# minutes and the browser session gets logged out too. The only way out is
# sessions born inside the bridge: android-protocol login (sandbox-verified:
# the request shape is accepted, wrong passwords reach the password check).
#
# Red lines:
#   * fully inert until a native account is added — zero requests, byte-
#     identical behavior
#   * auto re-login is conservatively throttled: never attempted within
#     DEEPSEEKER_RELOGIN_MIN_INTERVAL (default 6h => max ~4/day) — better no
#     self-healing than a "re-login machine" rhythm
#   * device_id is deterministically derived per account (uuid5, zero
#     storage, restart-stable): one account, one stable "device"
#   * community docs claim device_id must be a real SMSdk fingerprint
#     (empty/random may hit RISK_DEVICE_DETECTED, biz_code=11); our
#     wrong-password probes never reached that layer — first real account
#     will tell, and risk_device results are reported honestly and stop
#     auto-retry
# ==============================================================================

_RELOGIN_MIN_INTERVAL = float(os.getenv("DEEPSEEKER_RELOGIN_MIN_INTERVAL", "21600"))
_relogin_inflight: set = set()


def _login_device_uuid(account):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "deeperseeker-native:" + account))


async def login_account(account, password):
    """[v89.12] Bridge-native login of one account; structured result
    (never contains the password).

    account is email or mobile (told apart by @). Request shape = the
    official android app login: android header set + os=android + sign_in
    referer. Upstream business rejections (bad password / banned /
    risk-device) are reported as-is; network failures return
    reason="network"."""
    if "@" in account:
        email, mobile = account, None
    else:
        email, mobile = None, account
    _fail = lambda r, d="": {"ok": False, "reason": r, "detail": d,
                             "token": None, "muted": None, "mute_until": None}
    session = await get_session()
    try:
        async with session.post(
            UPSTREAM_BASES[0] + "/api/v0/users/login",
            headers={**get_headers(None),
                     "referer": "https://chat.deepseek.com/sign_in"},
            json={
                "email": email, "mobile": mobile, "password": password,
                "area_code": "+86", "device_id": _login_device_uuid(account),
                "os": "android",
            },
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            body = await resp.json(content_type=None)
    except Exception as exc:
        return _fail("network", str(exc)[:200])
    code = body.get("code") if isinstance(body, dict) else None
    data = body.get("data") if isinstance(body, dict) else None
    if code != 0 or not isinstance(data, dict):
        return _fail("unexpected", str(body)[:200])
    biz = data.get("biz_code")
    if biz not in (None, 0):
        reason_map = {2: "bad_credentials", 10: "banned", 11: "risk_device"}
        return _fail(reason_map.get(biz, f"biz_{biz}"),
                     str(data.get("biz_msg") or "")[:200])
    user = (data.get("biz_data") or {}).get("user") or {}
    token = user.get("token")
    if not token:
        return _fail("no_token", "login accepted but no token in response")
    chat = user.get("chat") or {}
    return {"ok": True, "reason": None, "detail": None, "token": token,
            "muted": chat.get("is_muted") == 1,
            "mute_until": chat.get("mute_until")}


def add_native_token(token, account, password, alias=None):
    """[v89.12] Pool insert for bridge-logged-in tokens: same pool, same
    scheduler, plus account/password/last_login metadata (auto re-login)."""
    conn = get_db()
    if not conn.execute("SELECT 1 FROM tokens WHERE id = 1").fetchone():
        next_id = 1
    else:
        row = conn.execute("""
            SELECT min(t1.id + 1)
            FROM tokens t1
            LEFT JOIN tokens t2 ON t1.id + 1 = t2.id
            WHERE t2.id IS NULL
        """).fetchone()
        next_id = row[0] if row and row[0] else 1
    conn.execute(
        "INSERT INTO tokens (id, alias, token, status, account, password, last_login) "
        "VALUES (?, ?, ?, 'ACTIVE', ?, ?, ?)",
        (next_id, alias, token, account, password, time.time()),
    )
    conn.commit()
    conn.close()


def update_native_token(token_id, new_token):
    """[v89.12] In-place token replacement after a successful re-login;
    rotation state reset."""
    conn = get_db()
    conn.execute(
        "UPDATE tokens SET token = ?, status = 'ACTIVE', rate_limited_until = NULL, "
        "consecutive_403 = 0, quarantine_reason = NULL, last_login = ? WHERE id = ?",
        (new_token, time.time(), token_id),
    )
    conn.commit()
    conn.close()


def get_native_credentials(token_id):
    """[v89.12] Re-login credentials for native tokens; None for non-native
    rows (no account/password) — auto re-login only touches explicitly
    added accounts."""
    conn = get_db()
    row = conn.execute(
        "SELECT account, password, last_login FROM tokens WHERE id = ?", (token_id,)
    ).fetchone()
    conn.close()
    if row and row[0] and row[1]:
        return {"account": row[0], "password": row[1], "last_login": row[2] or 0.0}
    return None


async def relogin_native(token_id):
    """[v89.12] Conservative auto re-login: never inside the throttle window;
    outcomes recorded honestly.

    Success -> update_native_token in place (seamless pool self-healing);
    bad password / banned / risk-device -> logged as-is (banned quarantines
    the row, risk-device stops auto-retry); network/unexpected -> status quo,
    retried next trigger within throttle. Concurrency dedup: one re-login
    task per token at a time."""
    if token_id in _relogin_inflight:
        return
    _relogin_inflight.add(token_id)
    try:
        creds = await asyncio.to_thread(get_native_credentials, token_id)
        if not creds:
            return  # non-native: module inert
        now = time.time()
        if now - creds["last_login"] < _RELOGIN_MIN_INTERVAL:
            logger.info("[relogin] token #%s within %.0fs of last login, skipping",
                        token_id, _RELOGIN_MIN_INTERVAL)
            return
        result = await login_account(creds["account"], creds["password"])
        if result.get("ok"):
            await asyncio.to_thread(update_native_token, token_id, result["token"])
            logger.info("[relogin] token #%s self-healed via bridge login (account %s***)",
                        token_id, str(creds["account"])[:3])
            if result.get("muted"):
                logger.warning("[relogin] token #%s new session is muted (until %s)",
                               token_id, result.get("mute_until"))
        elif result.get("reason") == "banned":
            await asyncio.to_thread(
                mark_quarantined, token_id, "account banned (bridge login)")
        elif result.get("reason") in ("bad_credentials", "risk_device", "no_token"):
            logger.warning("[relogin] token #%s bridge login rejected (%s: %s) — "
                           "left as-is, please check the account manually",
                           token_id, result.get("reason"),
                           result.get("detail") or "")
        else:
            logger.warning("[relogin] token #%s bridge login failed (%s), "
                           "will retry within throttle next trigger",
                           token_id, result.get("reason"))
    finally:
        _relogin_inflight.discard(token_id)


# ==============================================================================
# B3 (Stage 1 audit) — token pool scheduler v2
#
# The old pool selected ACTIVE tokens with ORDER BY RANDOM() and, when every
# token was RATE_LIMITED, fell back to the FIRST token by id. Consequences
# (root cause of issue #32): a limited token never recovered while any other
# token stayed ACTIVE, a single-token pool had no backoff at all, and
# concurrent requests stampeded onto one account — uneven wear and a
# ban-ladder accelerator. The scheduler now provides:
#   * a rate_limited_until cooldown with automatic RATE_LIMITED -> ACTIVE
#     recovery once the cooldown expires (flipped back on the next pick),
#   * least-in-flight selection (spreads load instead of RANDOM()),
#   * an idle-oldest tie-break (the never/least-recently-used token leads),
#   * a soft per-token concurrency cap: capped tokens are only used when
#     nothing else is free,
#   * a soonest-to-recover fallback when every token is still cooling down.
# ==============================================================================

# [v77 P0-3 port] When consecutive 403s (with no successful turn in between)
# reach this count, treat it as the ban ladder: the account leaves rotation
# until an admin manually restores it. 0 = escalation disabled (keeps the old
# cooldown-only behavior); 401 quarantines unconditionally.
QUARANTINE_AFTER_403 = max(0, int(os.getenv("DEEPSEEKER_QUARANTINE_AFTER_403", "3")))

# [v89.3] Auth-shaped "business" errors (a 401 mapped from HTTP 200 + msg
# wording heuristic) are soft evidence: any English business wording that
# merely contains auth/token/session/login/expire matches. A single hit must
# never put an account into any penalty state — that is the zero-cost-on-
# false-positive guarantee. Only BIZ_AUTH_QUARANTINE_AFTER consecutive hits
# (no successful turn in between) reproduce the hard-401 conclusion and
# quarantine; a real HTTP 401 (a status code upstream actually returned)
# keeps its immediate quarantine. The counter is in-memory, per process: a
# restart just makes a genuinely dead account re-earn its quarantine; it can
# never un-quarantine a row.
BIZ_AUTH_QUARANTINE_AFTER = max(
    1, int(os.getenv("DEEPSEEKER_BIZ_AUTH_QUARANTINE_AFTER", "3"))
)
_biz_auth_streaks = {}

TOKEN_COOLDOWN_SECONDS = max(1, int(os.getenv("DEEPSEEKER_RATE_LIMIT_COOLDOWN", "60")))
TOKEN_CONCURRENCY_CAP = max(1, int(os.getenv("DEEPSEEKER_TOKEN_CONCURRENCY", "8")))

# Per-token in-flight refcounts (per process; single event-loop worker). Every
# send reserves one slot via acquire_token_slot() and releases it when the
# request ends — success, upstream failure or client abort — mirroring the
# ownership-transfer pattern of the per-chat locks.
_in_flight = {}


def _bump_in_flight(token_id, delta):
    n = _in_flight.get(token_id, 0) + delta
    if n > 0:
        _in_flight[token_id] = n
    else:
        _in_flight.pop(token_id, None)


class _TokenSlot:
    """One in-flight reservation against a token. release() is once-only, so
    transferring the slot to a streaming generator (exactly like the per-chat
    lock owner) is safe by construction."""

    __slots__ = ("token_id", "_held")

    def __init__(self, token_id):
        self.token_id = token_id
        self._held = True
        _bump_in_flight(token_id, +1)

    def release(self):
        if not self._held:
            return
        self._held = False
        _bump_in_flight(self.token_id, -1)


def acquire_token_slot(token_id):
    """Reserve one in-flight slot for token_id; pair it with slot.release()."""
    return _TokenSlot(token_id)


def token_in_flight(token_id):
    """Current in-flight request count for a token (selection signal)."""
    return _in_flight.get(token_id, 0)


def pick_token(exclude=None):
    """Select the best available token (scheduler v2 — block comment above).

    `exclude` (optional, B10) lists token ids the caller just saw fail: they
    are avoided while ANY other token is available, so an empty-SSE retry
    cannot land on the same poisoned token.
    """
    now = time.time()
    conn = get_db()
    rows = conn.execute(
        "SELECT id, status, rate_limited_until, last_used FROM tokens"
    ).fetchall()
    conn.close()
    if not rows:
        return None

    excluded = set(exclude or ())
    available = []
    recovered = []
    for tid, status, until, last_used in rows:
        if status == "QUARANTINED":
            continue  # [v77 P0-3] terminal state: only restore_token() can recover
        if status == "RATE_LIMITED" and until is not None and until > now:
            continue  # still cooling down
        if status == "RATE_LIMITED":
            recovered.append(tid)  # cooldown expired -> auto-recover below
        available.append((tid, last_used or 0.0))

    pickable = [entry for entry in available if entry[0] not in excluded] or available
    if pickable:
        if recovered:
            conn = get_db()
            conn.executemany(
                "UPDATE tokens SET status = 'ACTIVE', rate_limited_until = NULL WHERE id = ?",
                [(tid,) for tid in recovered],
            )
            conn.commit()
            conn.close()
            logger.info("Token pool: cooldown expired, auto-recovered token(s) %s", recovered)

        def _sort_key(entry):
            tid, last_used = entry
            in_flight = _in_flight.get(tid, 0)
            # Soft cap: over-cap tokens sort behind every under-cap one.
            over_cap = 1 if in_flight >= TOKEN_CONCURRENCY_CAP else 0
            return (over_cap, in_flight, last_used, tid)

        return min(pickable, key=_sort_key)[0]

    # Every token is rate-limited and still cooling: pick the one that
    # recovers first (bounded wait) instead of stampeding onto the lowest id
    # with no backoff at all.
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM tokens WHERE status != 'QUARANTINED' "
        "ORDER BY COALESCE(rate_limited_until, 0) ASC, id ASC LIMIT 1"
    ).fetchone()
    conn.close()
    return row[0] if row else None


def mark_quarantined(token_id, reason):
    """[v77 P0-3 port] Take the account out of rotation until an admin
    restores it.

    Unlike RATE_LIMITED, this state has no timer — rate_limited_until is
    cleared, so pick_token()'s auto-recovery can never mistake a quarantined
    row for one that finished cooling down.
    [v89.6] The reason is persisted to the quarantine_reason column; the
    dashboard uses it to separate "likely expired / likely muted / other"
    with per-state plain-language guidance. restore_token() clears the
    column so a stale reason never mislabels a fresh state.
    """
    logger.warning("Token #%d QUARANTINED (manual return required): %s", token_id, reason)
    conn = get_db()
    conn.execute(
        "UPDATE tokens SET status = ?, rate_limited_until = NULL, quarantine_reason = ? "
        "WHERE id = ?",
        ("QUARANTINED", str(reason or ""), token_id),
    )
    conn.commit()
    conn.close()


def restore_token(token_id):
    """[v77 P0-3 port] The only way out of quarantine: an explicit admin
    action.

    last_used is deliberately left untouched — the restored account carries
    the oldest usage stamp, so the idle-oldest tie-break eases it back into
    rotation instead of flooding it with a burst of traffic.
    [v89.6] Also clears quarantine_reason: a restored account must not keep
    displaying the stale reason of its previous state.
    """
    conn = get_db()
    conn.execute(
        "UPDATE tokens SET status = ?, rate_limited_until = NULL, "
        "consecutive_403 = 0, quarantine_reason = NULL WHERE id = ?",
        ("ACTIVE", token_id),
    )
    conn.commit()
    conn.close()
    logger.info("Token #%d restored to ACTIVE by operator", token_id)


def _bump_403_streak(token_id):
    """[v77 P0-3 port] Increment and return the account's consecutive-403 count."""
    conn = get_db()
    conn.execute(
        "UPDATE tokens SET consecutive_403 = consecutive_403 + 1 WHERE id = ?",
        (token_id,),
    )
    row = conn.execute(
        "SELECT consecutive_403 FROM tokens WHERE id = ?", (token_id,)
    ).fetchone()
    conn.commit()
    conn.close()
    return row[0] if row else 0


def _bump_biz_auth_streak(token_id):
    """[v89.3] Increment and return the account's consecutive auth-shaped
    business-error count (in-memory, per process)."""
    n = _biz_auth_streaks.get(token_id, 0) + 1
    _biz_auth_streaks[token_id] = n
    return n


def mark_limited(token_id, code=None, biz_auth=False):
    """[v77 P0-3 port] Record one upstream failure against an account — the
    single gateway into the pool.

    Without a code: exactly the existing behavior (RATE_LIMITED cooldown with
    automatic recovery).
    With an upstream status code:
      * 401 -> QUARANTINED immediately. Rejected credentials are not
        transient; a 60-second cooldown would just re-arm the same dead
        account.
        [v89.3] Exception: biz_auth=True means this 401 was mapped from an
        HTTP 200 business envelope via the wording heuristic (soft evidence).
        A single hit applies no penalty at all (zero cost on a false
        positive); it only bumps a consecutive counter, and quarantine fires
        after BIZ_AUTH_QUARANTINE_AFTER hits.
      * 403 -> cooldown as before; if consecutive 403s (with no successful
        turn in between) reach QUARANTINE_AFTER_403, QUARANTINED.
      * Anything else (429) -> cooldown, as before.
    """
    if code == 401:
        if biz_auth:
            n = _bump_biz_auth_streak(token_id)
            if n >= BIZ_AUTH_QUARANTINE_AFTER:
                mark_quarantined(
                    token_id,
                    f"{n} consecutive auth-shaped business errors "
                    f"(threshold {BIZ_AUTH_QUARANTINE_AFTER})",
                )
            else:
                logger.warning(
                    "Token #%d: auth-shaped business error %d/%d — no "
                    "penalty (soft evidence, auto-zero on next success)",
                    token_id, n, BIZ_AUTH_QUARANTINE_AFTER,
                )
            return
        mark_quarantined(token_id, "upstream 401 (auth rejected)")
        return
    if code == 403:
        streak = _bump_403_streak(token_id)
        threshold = QUARANTINE_AFTER_403
        if threshold and streak >= threshold:
            mark_quarantined(
                token_id,
                f"{streak} consecutive upstream 403s (threshold {threshold})",
            )
            return
    until = time.time() + TOKEN_COOLDOWN_SECONDS
    logger.warning(
        "Token #%d marked RATE_LIMITED for %ds (recovers automatically)",
        token_id,
        TOKEN_COOLDOWN_SECONDS,
    )
    conn = get_db()
    conn.execute(
        "UPDATE tokens SET status = ?, rate_limited_until = ? WHERE id = ?",
        ("RATE_LIMITED", until, token_id),
    )
    conn.commit()
    conn.close()


def mark_active(token_id):
    """[v77 P0-3 port] Record a successful turn: back into rotation, the
    consecutive counter reset to zero.

    QUARANTINED rows are deliberately untouched: requests overlap, and a send
    issued before quarantine may land successfully after the ban. Letting it
    through would pollute an admin-controlled state; only restore_token() can
    end a quarantine.
    """
    # [v89.3] One successful turn refutes the "auth is dead" soft evidence:
    # the consecutive counter resets, so a single wording fluke auto-clears.
    _biz_auth_streaks.pop(token_id, None)
    conn = get_db()
    conn.execute(
        "UPDATE tokens SET status = ?, rate_limited_until = NULL, last_used = ?, "
        "consecutive_403 = 0 WHERE id = ? AND status != 'QUARANTINED'",
        ("ACTIVE", time.time(), token_id),
    )
    conn.commit()
    conn.close()


def _format_span(seconds):
    """[v77 P0-3 port] Compact duration for pool views: 42s / 7m / 3h / 2d."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def get_pool_view():
    """[v77 P0-3 port] Per-account operational view.

    Status, load and timers are given as render-ready strings. No usable
    credential fields: the full token column is deliberately absent — this
    output goes straight into HTML, which browsers cache and screenshots
    capture. token_head exposes only the first 8 + last 4 characters for
    eyeball account separation (the old dashboard already showed 12).
    """
    now = time.time()
    conn = get_db()
    rows = conn.execute(
        "SELECT id, alias, status, rate_limited_until, last_used, consecutive_403, token, "
        "quarantine_reason, account FROM tokens ORDER BY id"
    ).fetchall()
    conn.close()

    view = []
    for tid, alias, status, until, last_used, streak, token, q_reason, account in rows:
        streak = streak or 0
        token_head = f"{(token or '')[:8]}…{((token or '')[-4:] if len(token or '') > 12 else '')}" if token else "—"
        # [v89.6] Quarantine classification: the dashboard separates
        # "likely expired / likely muted / other" by it. Based on the reason
        # text mark_limited() writes — three sources:
        #   * v89.3 auth-shaped biz errors: "N consecutive auth-shaped ..."
        #   * [v89.7] confirmed muted: "user is muted (until <date>)"
        #     (upstream biz_data.is_muted==1 boolean, startup/manual probe
        #     or runtime detection)
        #   * hard 401: "upstream 401 (auth rejected)"
        #   * 403 streaks: "N consecutive upstream 403s"
        # Purely read-side string matching; no new write path.
        reason_kind = None
        if status == "QUARANTINED":
            state_class, cooldown = "err", "—"
            note = "quarantined — manual return required"
            qr = str(q_reason or "")
            if "muted" in qr.lower() or "禁言" in qr:
                reason_kind = "muted"
            elif "auth-shaped" in qr or "auth rejected" in qr:
                reason_kind = "expired"
            elif "403" in qr:
                reason_kind = "restricted"
            else:
                reason_kind = "other"
        elif status == "RATE_LIMITED" and until and until > now:
            state_class, cooldown = "warn", f"{_format_span(until - now)} left"
            note = "cooling down, recovers automatically"
        elif status == "RATE_LIMITED":
            state_class, cooldown = "warn", "expired"
            note = "rejoins rotation on the next pick"
        else:
            state_class, cooldown = "ok", "—"
            note = "in rotation"
        if streak and status != "QUARANTINED":
            note = f"{note}, 403 streak {streak}"
        age = "never" if not last_used else f"{_format_span(now - last_used)} ago"
        entry = {
            "id": tid,
            "label": alias if alias else f"untagged #{tid}",
            "token_head": token_head,
            "status": status,
            "state_class": state_class,
            "state_note": note,
            "in_flight": _in_flight.get(tid, 0),
            "last_used": age,
            "cooldown": cooldown,
            "can_restore": status == "QUARANTINED",
        }
        # [v89.12] native marker for bridge-logged-in tokens (dashboard badge);
        # browser tokens carry no field — display unchanged.
        if account:
            entry["native"] = True
        if reason_kind:
            entry["reason_kind"] = reason_kind
            entry["reason"] = str(q_reason or "")
        view.append(entry)
    return view


# ============ [v77 P0-6 port] usage statistics ============


def mask_api_key(key):
    """Masked key alias (for display): keep only the last 4 characters.

    The dashboard and all aggregations render this — page screenshots must
    never carry usable key material. Very short keys (<= 8 chars) are masked
    entirely."""
    key = str(key or "").strip()
    if not key:
        return "unknown"
    if len(key) <= 8:
        return "…"
    return "…" + key[-4:]


def record_usage(key_alias, surface, model, in_tokens, out_tokens, ts=None):
    """[v77 P0-6 port] Record one completed turn. key_alias should already be
    masked — call sites pass mask_api_key(raw_key); this function never
    persists a raw key."""
    alias = (
        mask_api_key(key_alias)
        if key_alias and not str(key_alias).startswith("…")
        else str(key_alias or "unknown")
    )
    when = time.time() if ts is None else float(ts)
    day = time.strftime("%Y-%m-%d", time.gmtime(when))
    conn = get_db()
    conn.execute(
        "INSERT INTO usage (ts, day, key_alias, surface, model, input_tokens, output_tokens) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            when,
            day,
            alias,
            str(surface or "unknown"),
            str(model or ""),
            max(0, int(in_tokens or 0)),
            max(0, int(out_tokens or 0)),
        ),
    )
    conn.commit()
    conn.close()


def usage_by_day_key(since_ts=0.0):
    """[v77 P0-6 port] Aggregate usage by day x masked key.

    Returns render-ready rows. The (day, key_alias) index covers the GROUP BY
    — even a month of data is a few milliseconds of SQLite; no
    pre-aggregation table needed."""
    conn = get_db()
    rows = conn.execute(
        "SELECT day, key_alias, "
        "GROUP_CONCAT(DISTINCT surface), COUNT(*), "
        "SUM(input_tokens), SUM(output_tokens) "
        "FROM usage WHERE ts >= ? "
        "GROUP BY day, key_alias ORDER BY day DESC, key_alias ASC",
        (float(since_ts or 0.0),),
    ).fetchall()
    conn.close()

    out = []
    for day, alias, surfaces, requests, in_tok, out_tok in rows:
        in_tok = in_tok or 0
        out_tok = out_tok or 0
        out.append(
            {
                "day": day,
                "key": alias,
                "surfaces": surfaces or "unknown",
                "requests": requests,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
            }
        )
    return out


# ============ [v77 PR#67 port] context-overflow error classifier ============

_CONTEXT_LIMIT_HINT_RE = re.compile(
    r"maximum.?context"
    r"|context.?(?:length|limit|window)"
    r"|token.?limit"
    r"|(?:prompt|payload|conversation|messages?).{0,24}too.?long"
    r"|exceed(?:ed|s)?[^.\n]{0,30}context",
    re.IGNORECASE,
)


def is_context_limit_error(exc):
    """[v77 PR#67 port] Public classifier: is this failure an upstream
    session-context exhaustion?

    This is where agent clients used to die: context-type failures were
    retried as-is with a token swap (context belongs to CHAT, not TOKEN, so
    every attempt failed identically) and only surfaced to the client as a
    context error at the very end. Callers use this to trigger a summary
    rollover. Matches:
      * UpstreamError whose body carries context wording (HTTP 400 'maximum
        context length' / 'context limit' / 'too long', etc.).
    Deliberately NOT matched: generic 5xx/timeouts — 'deadline exceeded',
    '502 upstream timeout' are retry-type, not context-type. Loose words only
    count when anchored to context material: a bare 'exceed' or a 'too long'
    without a preceding prompt/message no longer classifies as context
    (an upstream 'header limit exceeded' once falsely triggered a wasted
    summary rollover)."""
    if isinstance(exc, UpstreamError):
        body = getattr(exc, "body", None) or ""
        return bool(_CONTEXT_LIMIT_HINT_RE.search(str(body)))
    text = str(exc or "")
    return bool(
        re.search(
            r"maximum.?context|context.?(?:length|limit|window)|token.?limit",
            text,
            re.IGNORECASE,
        )
    )


def find_session(sig):
    conn = get_db()
    row = conn.execute("SELECT token_id, deepseek_session_id, parent_message_id FROM sessions WHERE signature = ?", (sig,)).fetchone()
    if row:
        # B13: touch on read so pruning evicts by real recency of USE, not by
        # insertion order — long-running chats get older rows every turn, and
        # they are precisely the sessions pruning must protect.
        conn.execute("UPDATE sessions SET last_used = ? WHERE signature = ?", (time.time(), sig))
        conn.commit()
    conn.close()
    if row:
        return {"token_id": row[0], "session_id": row[1], "parent_message_id": row[2]}
    return None


# Cap on stored session signatures. Every request stores 2 rows and nothing
# ever removed them, so after many chats the SQLite file (and its WAL) grew
# unbounded — on volume-limited deployments a full disk freezes ALL requests,
# including brand-new chats. PRUNE_EVERY saves trigger a prune that keeps the
# MAX_SESSIONS MOST-RECENTLY-USED rows (B13: by last_used, not insertion
# order — the old rowid policy evicted long-running chats, the most valuable
# sessions, exactly because they hold the oldest rows).
MAX_SESSIONS = int(os.getenv("DEEPSEEKER_MAX_SESSIONS", "20000"))
PRUNE_EVERY = int(os.getenv("DEEPSEEKER_PRUNE_EVERY", "500"))
_save_counter = {"n": 0}


def prune_sessions():
    conn = get_db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        deleted = 0
        if total > MAX_SESSIONS:
            # B13: evict LEAST-RECENTLY-USED. NULL last_used (never re-hit)
            # sorts as 0, i.e. evicted first; rowid breaks ties deterministically.
            deleted = conn.execute(
                "DELETE FROM sessions WHERE rowid IN "
                "(SELECT rowid FROM sessions ORDER BY COALESCE(last_used, 0) ASC, rowid ASC LIMIT ?)",
                (total - MAX_SESSIONS,),
            ).rowcount
        deleted_map = conn.execute(
            "DELETE FROM session_map WHERE created_at < datetime('now', '-7 days')"
        ).rowcount
        conn.commit()
        if deleted or deleted_map:
            logger.info("Pruned %d session signature(s) and %d stale session_map row(s)", deleted, deleted_map)
    finally:
        conn.close()


def save_session(sig, token_id, session_id, parent_message_id=0):
    conn = get_db()
    conn.execute(
        """INSERT OR REPLACE INTO sessions (signature, token_id, deepseek_session_id, parent_message_id)
           VALUES (?, ?, ?, ?)""",
        (sig, token_id, session_id, parent_message_id),
    )
    conn.commit()
    conn.close()
    _save_counter["n"] += 1
    if _save_counter["n"] >= PRUNE_EVERY:
        _save_counter["n"] = 0
        try:
            prune_sessions()
        except Exception:
            logger.exception("Session pruning failed (non-fatal)")


def get_chat_baseline(session_id):
    """(baseline, creation_total) — the two bookkeeping values captured at
    session creation.

    baseline: token count of the first message that actually reached the
    upstream (the first-message fast path's payload).
    creation_total: token count of the client's full history at the moment
    the session was created — the growth reference point.
    Rollover compares GROWTH (= current total - creation_total) instead of
    re-measuring the entire client history (which only ever grows —
    otherwise every turn would trigger a brand-new rollover).
    Old rows (no creation_total column) return creation_total=0; callers
    fall back to baseline as the reference (matching v72-v76 behavior)."""
    if not session_id:
        return 0, 0
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT baseline, creation_total FROM chat_memory WHERE session_id = ?", (session_id,)
        ).fetchone()
        if not row or row[0] is None:
            return 0, 0
        ct = int(row[1]) if (len(row) > 1 and row[1] is not None) else 0
        return int(row[0]), ct
    finally:
        conn.close()


def set_chat_baseline(session_id, baseline, creation_total=0):
    """Record the conversation size at which this upstream chat was opened.

    baseline: tokens of the first message that actually reached the upstream;
    creation_total: tokens of the client's full history at that moment (the
    growth reference point)."""
    if not session_id:
        return
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO chat_memory (session_id, baseline, created_at, creation_total) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET baseline = excluded.baseline, "
            "creation_total = excluded.creation_total",
            (session_id, int(baseline or 0), time.time(), int(creation_total or 0)),
        )
        conn.commit()
    finally:
        conn.close()


def get_rolling_state(session_id):
    """(rolling_summary, rolling_marker) — the rolling handoff summary
    passed forward when this session was created.

    rolling_summary: accumulated continuation notes as of the previous
    generation switch ('' = none, first switch).
    rolling_marker: the token position in the client history already digested
    by summaries (the left boundary of the summary slice).
    Old/missing rows return ("", 0) — degrading to "digest everything
    (except the raw tail)", equivalent to v77.4.
    """
    if not session_id:
        return "", 0
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT rolling_summary, rolling_marker FROM chat_memory WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return "", 0
        return (row[0] or ""), float(row[1] or 0)
    finally:
        conn.close()


def set_rolling_state(session_id, summary, marker):
    """Record the rolling summary and digestion position (preserving the
    row's baseline/creation_total bookkeeping)."""
    if not session_id:
        return
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO chat_memory (session_id, baseline, created_at, creation_total,"
            " rolling_summary, rolling_marker) VALUES (?, 0, ?, 0, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET rolling_summary = excluded.rolling_summary,"
            " rolling_marker = excluded.rolling_marker",
            (session_id, time.time(), summary or "", float(marker or 0)),
        )
        conn.commit()
    finally:
        conn.close()


def delete_session(sig):
    conn = get_db()
    conn.execute("DELETE FROM sessions WHERE signature = ?", (sig,))
    conn.commit()
    conn.close()


def next_parent(parent_message_id):
    """[v86 deprecated] The old protocol's +2 guess. Under the new protocol
    the upstream broadcasts the real number at the start of the stream (ready
    event), and the parent chain only trusts real numbers — this stub is kept
    solely for backward compatibility of the import name; no code path may
    ever advance the chain with a guessed value again."""
    raise NotImplementedError("next_parent (+2 guess) removed in v85: use _LAST_REAL_PARENT / saved_parent")


# [v84] Direct integration with the new protocol: since 2026-10-31 the
# upstream broadcasts the real message numbers at the start of the stream
# (event ready -> {"request_message_id": N, "response_message_id": M}).
# We capture it in _send_message_once and store it in this table when
# _send_message_locked wraps up.
# [v85] Full native adaptation complete: saved_parent only ever returns real
# numbers, with no guessed fallback — no broadcast means the upstream stored
# nothing and the chain state is unknown, so the caller skips saving
# (keeping the original mapping) and the next turn re-sends natively with the
# same parent. The old protocol's +2 semantics have been removed from all
# paths.
_LAST_REAL_PARENT = {}


def saved_parent(chat_id, prev_parent):
    """For save_session only: trusts exclusively the real numbers broadcast
    by the upstream.

    Returning None means no broadcast happened this turn (upstream stored
    nothing, chain not advanced) and the caller MUST skip save_session —
    keep the original mapping, never write a guessed value. The prev_parent
    parameter exists only for call-site signature compatibility and no longer
    participates in the computation."""
    return _LAST_REAL_PARENT.get(chat_id)


def delete_sessions_for_chat(token_id, session_id):
    conn = get_db()
    conn.execute("DELETE FROM sessions WHERE token_id = ? AND deepseek_session_id = ?", (token_id, session_id))
    conn.commit()
    conn.close()


# ==============================================================================
# B4 (Stage 1 audit) — file ownership registry
#
# /v1/files uploads picked a RANDOM token and upstream files are
# account-scoped, so a later /v1/chat/completions referencing that file_id
# could pick a different token and get "file not found" — the OpenAI-style
# upload->reference flow (Claude Code / Cline file flows) was broken by
# design. Uploads are now pinned to their token; chat handlers prefer the
# file-owner token on the first turn and re-home foreign references onto the
# chat's own token on later turns.
# ==============================================================================


def record_file(file_id, token_id):
    """Pin an upstream file_id to the token (account) that owns it.
    First owner wins: INSERT OR IGNORE keeps the original mapping stable."""
    conn = get_db()
    conn.execute(
        "INSERT OR IGNORE INTO files (file_id, token_id, created_at) VALUES (?, ?, ?)",
        (file_id, token_id, time.time()),
    )
    conn.commit()
    conn.close()


def get_file_token(file_id):
    """Return the token id that owns an uploaded file_id, or None."""
    conn = get_db()
    row = conn.execute("SELECT token_id FROM files WHERE file_id = ?", (file_id,)).fetchone()
    conn.close()
    return row[0] if row else None


# DeepSeek now serves a single model (v4.1flash) as the website default. The
# web API uses the website default when model_type is null, so no explicit
# value is sent. If DeepSeek ever exposes an explicit model_type enum value
# for v4.1flash, this constant is the single place to set it.
DEFAULT_MODEL_TYPE = None

# Flat peak-hour rates (per 1M tokens) for the single default model,
# per https://api-docs.deepseek.com/quick_start/pricing
DEEPSEEK_TARIFFS = {
    "deepseek-v4.1-flash": {
        "cache_miss_input": 0.44,
        "output_generation": 1.32,
    },
}


def count_tokens(text, model="deepseek-v4.1-flash"):
    return len(deepseek_tokenizer.ds_token.encode(text))


_BAD_ARG_KEY_RE = re.compile(r"""[\s"'<>=]|^(?:string|type|bool|int|float|number|true|false|null)$""", re.IGNORECASE)


class _TruncatedArgs(Exception):
    """[v57 ported from v50] The args string was cut off mid-string: the
    content is untrusted and the entire call must be discarded.

    The old behavior patched in a closing quote and bracket and let it
    through, so a truncated 3KB script could write out half a file — and
    downstream had no idea the data was corrupt. This now fails explicitly;
    better to drop one call than to accept bad data.
    """


def _repair_truncated_json(s, strict=False):
    if not isinstance(s, str):
        return s
    t = s.strip()
    if not t or t[0] not in "{[":
        return s
    try:
        json.loads(t)
        return t
    except Exception:
        pass

    def _scan(u):
        in_str = False
        esc = False
        stack = []
        for ch in u:
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch in "{[":
                stack.append(ch)
            elif ch in "}]":
                if stack:
                    stack.pop()
        return in_str, stack

    in_str, stack = _scan(t)
    if in_str:
        # [v57 ported from v50] The string was cut off mid-way: the boundary
        # is unknowable, and patching in a quote would let half a script pass
        # validation and write out a file that is "syntactically valid but
        # content-incomplete" — the hardest kind of corruption to trace.
        # In strict mode refuse to repair and return None so the caller
        # reports a clear error.
        if strict:
            return None
        t += '"'
    in_str, stack = _scan(t)
    t += "".join("}" if c == "{" else "]" for c in reversed(stack))
    try:
        json.loads(t)
        return t
    except Exception:
        return s


def _sanitize_args(obj):
    """[v57 ported from v50] Strict mode.

    String arguments first go through _repair_truncated_json(strict=True):
    JSON cut off mid-way raises _TruncatedArgs and normalize_tool_call drops
    the whole call — never downgraded to {} and let through (that would
    become a "valid call with empty arguments", and when the client rejects
    it the error message points at nothing real). Key sanitization follows
    v41: drop whitespace keys and non-argument keys hallucinated by the
    model (tool_title/type and the like).
    """
    if isinstance(obj, str):
        obj = _repair_truncated_json(obj, strict=True)
        if obj is None:
            raise _TruncatedArgs("args string cut off inside a string literal")
        try:
            obj = json.loads(obj)
        except Exception:
            return {}
    if not isinstance(obj, dict):
        return {}
    out = {}
    for k, v in obj.items():
        if not isinstance(k, str):
            continue
        k2 = k.strip()
        if not k2 or _BAD_ARG_KEY_RE.search(k2):
            continue
        out[k2] = v
    return out


def _coerce_args_json(args):
    try:
        return json.dumps(_sanitize_args(args), ensure_ascii=False)
    except _TruncatedArgs as e:
        logger.warning("[tool-args] untrusted arguments, dropping the whole call: %s", e)
        raise
    except Exception:
        return json.dumps({})


_TOOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _valid_tool_call(t):
    """[v57 fix] Drop calls the client would refuse anyway.

    The v36 original checked the flat {"name","arguments"} shape, but
    normalize_tool_call returns the OpenAI nested format
    {"id","type","function":{...}} with arguments as a JSON string — the two
    shapes never matched, the gate always returned False, and streaming tool
    calls were silently dropped 100% of the time. That bug spanned 15+
    releases before anyone noticed. This function is the only dual-format fix
    across all archived versions: both nested and flat shapes are accepted,
    and string arguments are json.loads'ed first.

    [v57 fix A6] Empty arguments pass: v36 condemned len(args)==0 outright,
    wrongly killing legitimate calls to no-argument tools like list_tabs.
    The proxy has no tool-schema knowledge and is not entitled to judge
    "required" — empty arguments are let through so the client rejects them
    with an accurate error, which beats silently dropping the call at the
    proxy so the tool never runs at all.

    [v57 fix] The name regex drops the {1,63} cap: v36 added it to guard
    against "short names produced by transport truncation", but real MCP tool
    names can far exceed 64 chars (the upstream's own tests assert that a
    5000+ char name must parse), so the cap only misfired without stopping
    any fraud — a truncated long name gets an explicit "unknown tool"
    rejection from the client, better than a silent drop at the proxy.
    Truncation protection is handled by len(name) < 2 (observed stub names
    are all single characters).
    """
    if not isinstance(t, dict):
        return False
    fn = t.get("function")
    if isinstance(fn, dict):
        name = (fn.get("name") or "").strip()
        args = fn.get("arguments")
    else:
        name = (t.get("name") or "").strip()
        args = t.get("arguments")
    if len(name) < 2 or _TOOL_NAME_RE.match(name) is None:
        return False
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            return False
    if args is None:
        return True
    if not isinstance(args, dict):
        return False
    return True


def normalize_tool_call(tool_data_or_name, args_if_name=None):
    if isinstance(tool_data_or_name, str):
        name = tool_data_or_name
        args = args_if_name if args_if_name is not None else {}
    elif isinstance(tool_data_or_name, dict):
        tool_data = tool_data_or_name
        if "function" in tool_data and isinstance(tool_data["function"], dict):
            fn = tool_data["function"]
            name = fn.get("name") or tool_data.get("name")
            args = fn.get("arguments") or fn.get("parameters") or fn.get("input") or fn.get("args") or fn.get("params") or {}
        else:
            name = tool_data.get("name") or tool_data.get("tool") or tool_data.get("tool_name") or tool_data.get("function") or tool_data.get("action")
            args = tool_data.get("arguments") or tool_data.get("parameters") or tool_data.get("input") or tool_data.get("args") or tool_data.get("params") or tool_data.get("tool_input") or tool_data.get("action_input") or {}
    else:
        return None

    if not name or not isinstance(name, str):
        return None
    # [v66] "Tool names" starting with an underscore are fragments of a
    # mangled marker (production evidence "_call": model/history garbage made
    # the parser extract a stub name; client execution always fails with
    # "$ _call invalid command"). Reject at the source — no delivery path can
    # produce such a call again.
    if name.lstrip().startswith("_"):
        return None
    # [v57 ported from v50] Route through _coerce_args_json: when a string
    # arguments value is cut off mid-way it raises _TruncatedArgs and the
    # whole call is dropped, instead of re-wrapping the truncated JSON in
    # double quotes as a string and letting it through (the A4 bug in v41
    # and earlier).
    try:
        args_str = _coerce_args_json(args)
    except _TruncatedArgs:
        return None
    call_id = "call_" + "".join(random.choices(string.ascii_letters + string.digits, k=8))
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name.strip(),
            "arguments": args_str,
        },
    }


def clean_json_str(s):
    s = s.strip()
    if s.startswith("```json"):
        s = s[7:]
    elif s.startswith("```"):
        s = s[3:]
    if s.endswith("```"):
        s = s[:-3]
    return s.strip()


def _code_fence_spans(text):
    """Return the (start, end) spans of markdown fenced code blocks.

    Tool-call markup inside a fence is documentation/example text, not an
    actual tool call, so matches falling inside these spans are ignored.
    Unclosed fences extend to end-of-text.
    """
    return [(m.start(), m.end()) for m in re.finditer(r"```.*?(?:```|$)", text, re.DOTALL)]


# [FIX 6] Bare decorated DSML opener without its leading '<': the model
# occasionally emits "｜｜DSML｜｜ calls>..." (or the half-width/spaced
# variant "| | DSML | | calls>") missing the opening angle bracket. Every
# entry regex in this module requires '<', so these fragments used to leak
# into the chat as prose and the tool never executed (2026-10-20 field
# evidence: "| | DSML | | calls>" between two successful tool rounds).
# Repair: a line that is essentially a bare decorated DSML opener gets its
# '<' prepended and its bars de-spaced/normalized so the entry regex can
# match. Line-anchored on purpose — prose merely *mentioning* DSML is left
# untouched.
_BARE_OPEN_LINE_RE = re.compile(
    r"(?m)^([ \t]*[｜\|][ \t｜\|]*DSML[ \t｜\|]*(?:calls|invoke|tool_calls?|function_calls?|function_call|parameter)\b[^\n]*)$",
    re.IGNORECASE,
)

_BARE_OPEN_HEAD_RE = re.compile(
    r"^([ \t]*[｜\|][ \t｜\|]*DSML[ \t｜\|]*)([A-Za-z_][A-Za-z0-9_]*)([^\n]*)$",
    re.IGNORECASE,
)

# [v57 fix] Group 1 captures an optional ｜｜DSML｜｜ decorated prefix: a
# decorated bare opener is not the A2 field issue (which had no decoration at
# all) and is left to _BARE_OPEN_LINE_RE to restore its '<'. The v38 original
# had no such group and stripped the decoration outright — the open tag became
# <｜｜DSML｜｜<tool_call> and the close tag </｜｜DSML｜｜<tool_call>; the
# tool still parsed, but the leftover <｜｜DSML｜｜ in front of the block
# leaked into prose (caught by the upstream test json_body chunk=5).
_BARE_PLAIN_OPEN_RE = re.compile(
    r"(?m)(?<!<)([｜\| \t]{0,8}DSML[｜\| \t]{0,8})?(tool_calls?|function_calls?|function_call|invoke|calls) *> *(?=[{])",
    re.IGNORECASE,
)


def _repair_bare_dsml_lines(text):
    def _fix_bare_plain(m):
        if m.group(1):
            # Decorated: return as-is; _BARE_OPEN_LINE_RE below restores the '<'
            return m.group(0)
        return chr(60) + m.group(2) + ">"

    # [v57 fix] Cheap pre-filter: a necessary condition for _BARE_PLAIN_OPEN_RE
    # to match is the presence of one of tool_call / function_call / invoke /
    # calls (IGNORECASE). Pure prose blocks pass with a single substring scan
    # instead of paying a full-buffer regex sweep — on a 50KB buffer that is
    # 3.9ms -> 0.03ms, and for long streaming answers the accumulated scan
    # drops from O(n^2) to O(n).
    # If none of the four literals is present the regex cannot match anyway,
    # which is losslessly equivalent to the original DSML guard early-exit.
    _low = text.lower()
    if ("tool_call" in _low or "function_call" in _low
            or "invoke" in _low or "calls" in _low):
        text = _BARE_PLAIN_OPEN_RE.sub(_fix_bare_plain, text)
    if "DSML" not in text:
        return text

    def _fix(m):
        body = m.group(1)
        mm = _BARE_OPEN_HEAD_RE.match(body)
        if not mm:
            return body
        head, tag, rest = mm.groups()
        compact = re.sub(r"[ \t]", "", head).replace("|", "｜")
        return "<" + compact + tag + rest

    return _BARE_OPEN_LINE_RE.sub(_fix, text)


# Residue that ends up INSIDE a parameter value when the model emits a
# stray decorated closer before the structural close (e.g.
# ">check...content</| | DSML | |></｜DSML｜>parameter>"). Structural cleanup
# never touches it because it is not at a tag boundary — strip it from
# values directly. Bare decorated markers only; real prose is untouched.
_VALUE_RESIDUE_RE = re.compile(
    r"(?:</)?[｜\| \t]{0,8}DSML[｜\| \t]{0,8}\s*>[^<\n]{0,12}>"  # marker + trailing fragment (e.g. ">param>")
    r"|(?:</)?[｜\| \t]{0,8}DSML[｜\| \t]{0,8}\s*>"              # marker with '>' but no fragment
    r"|(?:</)?[｜\| \t]{0,8}DSML[｜\| \t]{0,8}[^>\n]{0,40}>"     # [v66] mangled closer: a tag tail stuck to the marker (production evidence 2026-10-25
                                                                # session d440af4c: model wrote "<title>｜｜DSML｜｜ parameter>" (missing '</');
                                                                # every branch of the old regex started with </? — that is "an < is required",
                                                                # not "an optional </" — so a fragment without < could only match the bare
                                                                # marker branch and the tail " parameter>" leaked into
                                                                # tool_title; the client's tool card showed "<the model's title> parameter>")
    r"|[｜\|]{2}\s*DSML\s*[｜\|]{2}",                            # [v33] bare marker, no '>' at all
    re.IGNORECASE,
)


def _clean_param_value(val):
    val = re.sub(r"</?[｜\| \t]{0,8}(?:DSML[｜\| \t]{0,8}>?)?\s*(?:tool_calls?|invoke|function_call|parameter|param)\b[^>]*>", "", val, flags=re.IGNORECASE)
    # [v33] same whitelist blind spot as _WRAPPER_TAG_RE, but here it is fatal:
    # a decorated closer left inside `command` reaches the client and the shell
    # dies with "unterminated quoted string". Marker wins, name is irrelevant.
    val = _DECOR_TAG_RE.sub("", val)
    val = _BARE_CLOSE_RE.sub("", val)
    val = _VALUE_RESIDUE_RE.sub("", val)
    # [v33] bare DSML marker anywhere inside the value ("echo a ｜｜DSML｜｜ b").
    # This construct is never legitimate content, so strip it unconditionally —
    # unlike the streaming path we need no keyword lookahead here because the
    # value is already finalized.
    # [v32] bare closer missing '</' stuck to the value tail (model writes
    # "<title>｜｜DSML｜｜>" instead of "</｜｜DSML｜｜ parameter>") — drop any
    # trailing decoration tail that no legitimate value ends with.
    val = re.sub(r"[｜\| \t]{0,8}DSML[｜\| \t]{0,8}(?:\s*(?:parameter|invoke|calls?|tool_calls?)\b[^>\n]{0,40}>)?\s*$", "", val, flags=re.IGNORECASE).strip()
    return val.strip()


# [v66] Exact open tag: when <tool_call>/<function_call> exists in the prose,
# the DSML sweeper must yield (see the comment inside parse_tools). References
# inside a think span do not count.
_EXACT_CALL_OPEN_RE = re.compile(r"<(?:tool_call|function_call)\s*>", re.IGNORECASE)
# [v66] Think-block span: the model's private reasoning area; any marker text
# quoted in there does not participate in parsing.
_THINK_SPAN_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


# [v75-a] DSML argument wrapper names: when the model imitates the OpenAI
# <tool_call> structure it wraps the entire arguments into a single parameter.
# When these names appear with an object value, merge-unwrap to eliminate the
# {"arguments": {...}} double nesting (production evidence 2026-10-25 session
# e1751fe3).
_DSML_ARGS_WRAPPERS = {"arguments", "args", "parameters", "params"}


# [v77.10-b] Lenient arguments rescue: when the model escapes inconsistently
# (production evidence 2026-10-29 19:32 session: opening quotes written as \"
# and closing quotes as bare ", breaking json.loads), the unwrap branch falls
# back to a {"arguments": "<bad JSON string>"} double structure -> the client
# cannot find the required parameter(s) and rejects the whole call ("missing
# required parameter(s): command"). This scanner extracts top-level
# "key": value pairs key by key, restoring bare quotes inside values literally
# (exactly the model's intent). It is a last resort, used only after both
# strict parsing and raw_decode have failed.
_LENIENT_KV_HEAD_RE = re.compile(r"[\x27\x22]([A-Za-z0-9_\-]+)[\x27\x22]\s*:\s*")
_LENIENT_VALUE_TAIL_WS = " \t\r\n"
_LENIENT_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f"}


def _lenient_json_object(s: str) -> dict:
    """Leniently extract top-level string/number key-value pairs from broken
    JSON text. Returns {} when no complete pair can be extracted (the caller
    then proceeds with its existing fallback, unchanged)."""
    out = {}
    n = len(s)
    for m in _LENIENT_KV_HEAD_RE.finditer(s):
        key = m.group(1)
        i = m.end()
        if i >= n:
            continue
        if s[i] in "\x27\x22":
            # String value: read until a quote followed (after optional
            # whitespace) by , or }; a bare quote in the middle (followed by
            # another character) is treated as a literal quote inside the value
            j = i + 1
            buf = []
            closed = False
            while j < n:
                ch = s[j]
                if ch == "\\" and j + 1 < n:
                    nxt = s[j + 1]
                    buf.append(_LENIENT_ESCAPES.get(nxt, "\\" + nxt))
                    j += 2
                    continue
                if ch in "\x27\x22":
                    k = j + 1
                    while k < n and s[k] in _LENIENT_VALUE_TAIL_WS:
                        k += 1
                    if k >= n or s[k] in ",}":
                        closed = True
                        break
                    buf.append(ch)
                    j += 1
                    continue
                buf.append(ch)
                j += 1
            if closed or (not closed and buf):
                out[key] = "".join(buf)
            continue
        m2 = re.match(r"-?\d+(?:\.\d+)?|true|false|null", s[i:])
        if m2:
            v = m2.group(0)
            if v[0] in "-0123456789":
                try:
                    out[key] = json.loads(v)
                except Exception:
                    pass
            elif v == "true":
                out[key] = True
            elif v == "false":
                out[key] = False
            else:
                out[key] = None
    return out


# [v77 PR#70 port] Tool list of the current request, set by handle_chat so
# parse_tools can infer which call a schema belongs to when the model drops
# the tool name.
request_tools = contextvars.ContextVar("request_tools", default=None)


def _tool_schemas():
    schemas = {}
    for t in request_tools.get() or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        name = fn.get("name")
        params = fn.get("parameters") or fn.get("input_schema") or {}
        if name and isinstance(params, dict):
            schemas[name] = params
    return schemas


def _infer_tool_name(args, schemas):
    """[v77 PR#70 port] Schema-based inference: the unique tool that accepts
    exactly these parameter names.

    First require all required fields to be present; if the model even dropped
    a required field, still name the only tool that can accept these
    parameters, so the client reports "missing field" instead of "unknown
    tool"."""
    for need_required in (True, False):
        fits = []
        for name, params in schemas.items():
            props = set(params.get("properties") or {})
            required = set(params.get("required") or [])
            if set(args) <= props and (not need_required or required <= set(args)):
                fits.append((len(props - set(args)), name))
        fits.sort()
        if fits and (len(fits) == 1 or fits[0][0] < fits[1][0]):
            return fits[0][1]
    return None


def _repair_tool_names(tools):
    schemas = _tool_schemas()
    if not schemas:
        return tools
    for t in tools:
        if not isinstance(t, dict) or "function" not in t:
            continue
        fn = t["function"]
        if fn.get("name") in schemas:
            continue
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:
            continue
        if isinstance(args, dict):
            inferred = _infer_tool_name(args, schemas)
            if inferred:
                fn["name"] = inferred
    return tools


# [v77 PR#72 port] The current request's reasoning effort (when thinking is
# on); build_prompt turns it into the thinking directives.
request_effort = contextvars.ContextVar("request_effort", default=None)


def effort_level(body):
    """[v77 PR#72 port] The client's effort string, whichever dialect field it
    lives in."""
    cands = [body.get("reasoning_effort"), body.get("effort")]
    for key in ("reasoning", "output_config", "thinking"):
        v = body.get(key)
        if isinstance(v, dict):
            cands += [v.get("effort"), v.get("reasoning_effort"), v.get("level")]
        elif key == "thinking" and isinstance(v, str):
            cands.append(v)
    for c in cands:
        if isinstance(c, str) and c.strip():
            return c.strip().lower()
    return None


# ============ [v89.6] Prompt enhancement master switch (admin toggle) ============

_effort_enhance_cache = None


def get_effort_enhance():
    """Whether prompt enhancement is enabled. Persisted in the settings
    table, cached in memory; defaults to ON.

    ON restores the v77 design intent (EFFORT_INSTRUCTIONS never took effect
    in production because the only set site referenced an undefined name).
    When OFF the chat path never sets the request_effort ContextVar —
    build_prompt's _with_effort reads the default None, no instruction is
    attached, and behavior is byte-identical to the pre-toggle release.
    """
    global _effort_enhance_cache
    if _effort_enhance_cache is None:
        conn = get_db()
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'effort_enhance'"
        ).fetchone()
        conn.close()
        _effort_enhance_cache = (row[0] != "off") if row else True
    return _effort_enhance_cache


def set_effort_enhance(enabled):
    """Write the toggle (dashboard call): settings table + immediate cache
    refresh."""
    global _effort_enhance_cache
    conn = get_db()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('effort_enhance', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        ("on" if enabled else "off",),
    )
    conn.commit()
    conn.close()
    _effort_enhance_cache = bool(enabled)
    return _effort_enhance_cache


def parse_tools(text):
    tools = []
    clean_text = text
    fence_spans = _code_fence_spans(text)
    # [v66] Think blocks are private reasoning, not a tool-call area. The
    # model discusses/quotes marker text inside think (evidence: 2026-10-25
    # session dee8cb2a — the model wrote "closing tag got mangled into
    # </｜｜DSML｜｜_call>" in think, and the DSML sweeper promptly extracted
    # the stub name "_call" from that quoted sentence, producing an invalid
    # {"name": "_call"} call while the real call was lost entirely). All
    # scans unconditionally skip think spans.
    think_spans = [m.span() for m in _THINK_SPAN_RE.finditer(text)]

    def fenced(pos):
        return any(s <= pos < e for s, e in fence_spans)

    def hidden(pos):
        if any(s <= pos < e for s, e in fence_spans):
            return True
        if any(s <= pos < e for s, e in think_spans):
            return True
        # [v78-b] Markers inside inline code (single backticks) are
        # teaching/example references, not calls. Fix: an inline <tool_call>
        # mention was paired as an opener with a real call, producing
        # duplicate tools (2026-10-30 production sample, same disease in the
        # v77.11 baseline: 1 mention + 1 real call -> 2 identical tools).
        # Real tool calls are never written inside single backticks.
        return any(s <= pos < e for s, e in inline_spans)

    # [v78-b] Inline-code masking: equal-length \x00 placeholders keep
    # "match on the mask, delete on the original" positions aligned, used to
    # prevent block cleanup from truncating prose at an inline mention. Text
    # without inline code is byte-for-byte unchanged.
    inline_spans = [m.span() for m in re.finditer(r"`[^`\n]*`", text)]

    def _sub_keep_inline(pattern, s, flags=0):
        if not inline_spans:
            return re.sub(pattern, "", s, flags=flags)
        parts, last = [], 0
        for a, b in inline_spans:
            parts.append(s[last:a])
            parts.append("\x00" * (b - a))
            last = b
        parts.append(s[last:])
        masked = "".join(parts)
        out, last, hit = [], 0, False
        for m in re.finditer(pattern, masked, flags):
            out.append(s[last:m.start()])
            last = m.end()
            hit = True
        if not hit:
            return s
        out.append(s[last:])
        return "".join(out)

    param_names = {"command", "description", "file_path", "content", "path", "prompt", "query", "subject", "old_string", "new_string", "url", "input"}
    tool_matches = list(re.finditer(r"<[｜\| \t]{0,8}(?:DSML[｜\| \t]{0,8}>?)?\s*(?:tool_calls?|invoke|function_calls?)\s+(?:name|tool)=[\x27\x22]([^\x27\x22]+)[\x27\x22][^>]*>", text, re.IGNORECASE))
    real_tool_matches = [tm for tm in tool_matches if not hidden(tm.start())]

    if real_tool_matches:
        for i, tm in enumerate(real_tool_matches):
            candidate_name = tm.group(1).strip()
            start_idx = tm.end()
            end_idx = real_tool_matches[i+1].start() if i + 1 < len(real_tool_matches) else len(text)
            body = text[start_idx:end_idx]
            args = {}
            p_matches = re.finditer(r"<[/]?[｜\| \t]{0,8}(?:DSML[｜\| \t]{0,8}>?)?\s*(?:parameter|tool_call|param|invoke)\s+name=[\x27\x22]([^\x27\x22]+)[\x27\x22][^>]*>(.*?)(?:(?=<[/]?[｜\| \t]{0,8}(?:DSML[｜\| \t]{0,8}>?)?\s*(?:parameter|tool_call|param|invoke)\b)|(?:(?<=^)|(?<=[^\n<｜\|/ \t]))[｜\| \t]{0,8}DSML[｜\| \t]{0,8}\s*>|</[｜\| \t]{0,8}(?:DSML[｜\| \t]{0,8}>?)?\s*(?:parameter|tool_call|param|invoke)\b[^>]*>|$)", body, flags=re.DOTALL | re.IGNORECASE)
            for pm in p_matches:
                p_name = pm.group(1).strip()
                p_val = _clean_param_value(pm.group(2))
                try:
                    val = json.loads(_repair_truncated_json(p_val, strict=True))
                except Exception:
                    val = p_val
                # [v75-a] Outer wrapper-name unwrap: when imitating the
                # <tool_call> structure the model writes parameter
                # name="arguments" wrapping the whole parameter set; nesting
                # it directly produces a {"arguments": {...}} double structure
                # and the executor rejects the whole call for missing required
                # parameters (production evidence 2026-10-25 18:24 session
                # e1751fe3). Merge-unwrap here.
                if p_name in _DSML_ARGS_WRAPPERS:
                    if isinstance(val, dict):
                        args.update(val)
                        continue
                    if isinstance(val, str):
                        inner = None
                        try:
                            inner = json.loads(val)
                        except Exception:
                            # Trailing garbage (extra closing brackets the
                            # model wrote): raw_decode takes only the leading
                            # complete JSON value and ignores the rest;
                            # non-JSON text fails outright
                            try:
                                inner, _end = json.JSONDecoder().raw_decode(val.lstrip())
                            except Exception:
                                inner = None
                        if isinstance(inner, dict):
                            args.update(inner)
                            continue
                        # [v77.10-b] Both strict parsing and raw_decode failed
                        # (inconsistent escaping etc., broken JSON): leniently
                        # extract top-level key-value pairs to prevent the
                        # double-nesting rejection (production evidence:
                        # 2026-10-29 19:32 shell_execute "missing required
                        # parameter(s): command" incident).
                        inner = _lenient_json_object(val)
                        if inner:
                            args.update(inner)
                            continue
                    args[p_name] = val
                    continue
                # [v75-b] Container types reverted to strings: tool parameters
                # are all string-typed (the model annotates string="true").
                # JSON text (e.g. file_write's content payload) must stay
                # textual; json.loads must never turn the string into an
                # object — the client's file_write only accepts strings and
                # would reject with "missing content" (production evidence:
                # 2026-10-25, three rejections the same day). Scalars still
                # convert normally.
                # [v77 PR#70 port] The string="true" annotation means a literal
                # string: "42"/"true" must not become JSON. Checked after the
                # wrapper-name unwrap — historical evidence (v75 Fix 1): the
                # model misused the annotation, marking a JSON object as
                # string="true" inside an arguments wrapper; if the guard ran
                # before the unwrap it would re-create the double nesting.
                if re.search(r"string\s*=\s*[\x27\x22]true", pm.group(0), re.IGNORECASE):
                    args[p_name] = p_val
                    continue
                if isinstance(val, (dict, list)):
                    val = p_val
                args[p_name] = val
            tag_param_matches = re.finditer(r"<([A-Za-z0-9_\-]+)>(.*?)(?:</\1>|$)", body, flags=re.DOTALL | re.IGNORECASE)
            for pm in tag_param_matches:
                t_name = pm.group(1).strip().lower()
                if t_name in param_names:
                    t_val = _clean_param_value(pm.group(2))
                    if re.search(r"string\s*=\s*[\x27\x22]true", tm.group(0), re.IGNORECASE):
                        t_val_parsed = t_val
                    else:
                        try:
                            t_val_parsed = json.loads(_repair_truncated_json(t_val, strict=True))
                        except Exception:
                            t_val_parsed = t_val
                    if isinstance(t_val_parsed, (dict, list)):
                        t_val_parsed = t_val
                    args[t_name] = t_val_parsed
            if candidate_name:
                norm = normalize_tool_call(candidate_name, args)
                if norm:
                    tools.append(norm)

    if tools:
        clean_text = _sub_keep_inline(r"<[｜\| \t]{0,8}(?:DSML[｜\| \t]{0,8}>?)?\s*(?:tool_calls?|calls)[^>]*>.*?(?:</[｜\| \t]{0,8}(?:DSML[｜\| \t]{0,8}>?)?\s*(?:tool_calls?|calls)>|$)", clean_text, flags=re.DOTALL | re.IGNORECASE).strip()
        clean_text = _sub_keep_inline(r"<[｜\| \t]{0,8}(?:DSML[｜\| \t]{0,8}>?)?\s*(?:invoke|function_call)[^>]*>.*?(?:</[｜\| \t]{0,8}(?:DSML[｜\| \t]{0,8}>?)?\s*(?:invoke|function_call)>|$)", clean_text, flags=re.DOTALL | re.IGNORECASE).strip()

    # [v66] Exact format takes precedence: when exact <tool_call>/<function_call>
    # open tags exist in the prose (outside think spans), never enter the DSML
    # sweeper. The sweeper's final no-'<'-prefix search treats a mangled
    # closing tag as a tool name, producing an invalid {"name": "_call"} call
    # while crowding out the intact real call next to it (production evidence:
    # session dee8cb2a, three consecutive rounds of file_write/shell_execute
    # all became "_call invalid command").
    _exact_openers = [m for m in _EXACT_CALL_OPEN_RE.finditer(text)
                      if not any(s <= m.start() < e for s, e in think_spans)]
    if not tools and "DSML" in text and not _exact_openers:
        dsml_block_pattern = re.compile(r"<[｜\|]{2}DSML[｜\|]{2}([A-Za-z0-9_]+)>(.*?)(?:</[｜\|]{2}DSML[｜\|]{2}\1>|$)", re.DOTALL | re.IGNORECASE)
        param_pattern_b = re.compile(r"<[｜\|]{2}DSML[｜\|]{2}B([A-Za-z0-9_]+)[^>]*>(.*?)(?:</[｜\|]{2}DSML[｜\|]{2}B.*?>|$)", re.DOTALL | re.IGNORECASE)
        for m in dsml_block_pattern.finditer(text):
            if hidden(m.start()):
                continue
            tool_name = m.group(1).strip()
            # [v66] "Names" starting with an underscore are fragments of a
            # mangled closing tag (e.g. _call), not tools
            if tool_name.startswith("_"):
                continue
            body = m.group(2)
            args = {}
            for pm in param_pattern_b.finditer(body):
                p_name = pm.group(1).lower().strip()
                p_val = _clean_param_value(pm.group(2))
                if re.search(r"string\s*=\s*[\x27\x22]true", pm.group(0), re.IGNORECASE):
                    args[p_name] = p_val
                    continue
                try:
                    val = json.loads(_repair_truncated_json(p_val, strict=True))
                except Exception:
                    val = p_val
                # [v75-b] Same as the main branch: container types reverted to
                # strings
                # [v77 PR#70 port] The string="true" annotation means a literal
                # string: "42"/"true" must not become JSON. Checked after the
                # wrapper-name unwrap — historical evidence (v75 Fix 1): the
                # model misused the annotation, marking a JSON object as
                # string="true" inside an arguments wrapper; if the guard ran
                # before the unwrap it would re-create the double nesting.
                if re.search(r"string\s*=\s*[\x27\x22]true", pm.group(0), re.IGNORECASE):
                    args[p_name] = p_val
                    continue
                if isinstance(val, (dict, list)):
                    val = p_val
                args[p_name] = val
            norm = normalize_tool_call(tool_name, args)
            if norm:
                tools.append(norm)
        if not tools:
            tool_match = re.search(r"[｜\|]{2}DSML[｜\|]{2}(Bash|Read|Write|Edit|Agent|TaskList|TaskCreate|WebSearch|[A-Za-z0-9_]+)", text, re.IGNORECASE)
            if tool_match and not hidden(tool_match.start()) and not tool_match.group(1).startswith("_"):
                candidate = tool_match.group(1).strip()
                tool_name = "Bash" if candidate.lower().startswith("b") and candidate.lower() not in ["bdescription", "bparam"] else candidate
                args = {}
                cmd_match = re.search(r"[｜\|]{2}B[\x22\x27]?command[\x22\x27]?[^>]*>(.*?)(?:</[｜\|]{2}B|$)", text, re.DOTALL | re.IGNORECASE)
                desc_match = re.search(r"[｜\|]{2}B[\x22\x27]?description[\x22\x27]?[^>]*>(.*?)(?:</[｜\|]{2}B|$)", text, re.DOTALL | re.IGNORECASE)
                if cmd_match:
                    clean_cmd = re.sub(r"</?[｜\|]{2}DSML[｜\|]{2}[^>]*>", "", cmd_match.group(1)).strip("\x22\x27() ")
                    args["command"] = clean_cmd
                if desc_match:
                    clean_desc = re.sub(r"</?[｜\|]{2}DSML[｜\|]{2}[^>]*>", "", desc_match.group(1)).strip("\x22\x27() ")
                    args["description"] = clean_desc
                norm = normalize_tool_call(tool_name, args)
                if norm:
                    tools.append(norm)
        # [v66] Whether or not any tool was swept out, DSML residue must never
        # reach the prose — a mangled closing tag left in the flush tail leaks
        # to the client verbatim.
        clean_text = re.sub(r"<[｜\|]{2}DSML[｜\|]{2}[^>]*>.*?(?:</[｜\|]{2}DSML[｜\|]{2}[^>]*>|$)", "", clean_text, flags=re.DOTALL | re.IGNORECASE).strip()
        clean_text = re.sub(r"</?[｜\|]{2}DSML[｜\|]{2}[^>]*>", "", clean_text, flags=re.IGNORECASE).strip()

    if not tools:
        fn_call_pattern = re.compile(r"<function_call>\s*<name>([^<]+)</name>\s*<arguments>(.*?)</arguments>\s*</function_call>", re.DOTALL | re.IGNORECASE)
        for m in fn_call_pattern.finditer(text):
            if hidden(m.start()):
                continue
            name = m.group(1).strip()
            args_raw = m.group(2).strip()
            try:
                args = json.loads(args_raw)
            except Exception:
                args = args_raw
            norm = normalize_tool_call(name, args)
            if norm:
                tools.append(norm)
        if tools:
            clean_text = _sub_keep_inline(r"<function_call>.*?</function_call>", clean_text, flags=re.DOTALL | re.IGNORECASE).strip()

    if not tools:
        # [v76-a] Tag-name completion for <call>: the model mixes in a third
        # spelling, <call tool="name">{...}</call> (production evidence
        # 2026-10-26 session 3e4457c5, an entire shell_execute call leaked
        # into prose). This path already supports the tool= attribute; only
        # the tag name was missing. The attribute branch automatically treats
        # top-level JSON keys as parameters.
        tag_regex = re.compile(r"<(?:tool_call|function_call|call)(?:\s+(?:name|tool|function)=[\x27\x22]([^\x27\x22]+)[\x27\x22])?\s*>", re.IGNORECASE)
        decoder = json.JSONDecoder()
        matches = [m for m in tag_regex.finditer(text) if not hidden(m.start())]
        if matches:
            for m in matches:
                tag_name = m.group(1)
                after_tag = text[m.end():]
                brace_pos = after_tag.find("{")
                if brace_pos != -1:
                    json_substr = after_tag[brace_pos:]
                    data = None
                    try:
                        data, _ = decoder.raw_decode(json_substr)
                    except Exception:
                        pass
                    if not data:
                        cleaned_json = re.sub(r"</?(?:tool_call|function_call|tool_calls|invoke|call(?=[\s>/]))[^>]*>.*", "", json_substr, flags=re.DOTALL).strip()
                        open_b = cleaned_json.count("{")
                        close_b = cleaned_json.count("}")
                        if open_b > close_b:
                            cleaned_json += "}" * (open_b - close_b)
                        try:
                            data = json.loads(cleaned_json)
                        except Exception:
                            pass
                    if isinstance(data, dict):
                        if tag_name:
                            name = tag_name
                            if "arguments" in data and isinstance(data["arguments"], dict):
                                args = data["arguments"]
                            elif "parameters" in data and isinstance(data["parameters"], dict):
                                args = data["parameters"]
                            elif "input" in data and isinstance(data["input"], dict):
                                args = data["input"]
                            else:
                                args = {k: v for k, v in data.items() if k not in ["name", "tool", "function"]}
                        else:
                            name = data.get("name") or data.get("tool") or data.get("tool_name") or data.get("function") or data.get("action")
                            args = data.get("arguments") or data.get("parameters") or data.get("input") or data.get("args") or data.get("params") or data.get("tool_input") or data.get("action_input")
                            if args is None:
                                args = {}
                        if name:
                            norm = normalize_tool_call(name, args)
                            if norm:
                                tools.append(norm)
            # [v76-a] <call> must be followed immediately by whitespace or >
            # (call(?=[\s>])): prevents malformed tags like <call_call> from
            # being taken as a <call> opener with DOTALL swallowing the rest
            # of the prose (replay evidence 1792866321_stream_ce93ffb8).
            clean_text = _sub_keep_inline(r"<(?:tool_call|function_call|call(?=[\s>]))[^>]*>.*?(?:</(?:tool_call|function_call|call)>|$)(?:\s*(?:参数|parameter|param)\s*>)?", text, flags=re.DOTALL).strip()

    if not tools:
        codeblock_pattern = r"```(?:tool_call|function_call)\s*(.*?)\s*```"
        cb_matches = list(re.finditer(codeblock_pattern, clean_text, flags=re.DOTALL))
        for m in cb_matches:
            cleaned = clean_json_str(m.group(1))
            try:
                data = json.loads(cleaned)
                if isinstance(data, dict):
                    name = data.get("name") or data.get("tool") or data.get("function") or data.get("action")
                    args = data.get("arguments") or data.get("parameters") or data.get("input") or data.get("args") or {}
                    norm = normalize_tool_call(name, args)
                    if norm:
                        tools.append(norm)
            except Exception:
                pass
        if tools:
            clean_text = re.sub(codeblock_pattern, "", clean_text, flags=re.DOTALL).strip()

    if not tools:
        json_pattern = r"```json\s*(\{.*?\})\s*```"
        json_matches = list(re.finditer(json_pattern, clean_text, flags=re.DOTALL))
        for m in json_matches:
            cleaned = clean_json_str(m.group(1))
            try:
                data = json.loads(cleaned)
                if isinstance(data, dict) and ("name" in data or "tool" in data or "function" in data):
                    name = data.get("name") or data.get("tool") or data.get("function") or data.get("action")
                    args = data.get("arguments") or data.get("parameters") or data.get("input") or data.get("args") or {}
                    norm = normalize_tool_call(name, args)
                    if norm:
                        tools.append(norm)
            except Exception:
                pass
        if tools:
            clean_text = re.sub(json_pattern, "", clean_text, flags=re.DOTALL).strip()
    # Keep companion text: the tool-call blocks themselves were already removed
    # from clean_text above; only leftover bare tags are stripped here.
    # [v78-b] Inline code is swapped for unique sentinels before stripping
    # tags and restored afterwards: prevents _strip_wrapper_tags from
    # stripping markers like `<tool_call>` inside inline examples out of the
    # prose (2026-10-30 verified: a teaching mention became empty backticks).
    # The sentinel contains \x00, which the tag-family regexes can never
    # match; mentions removed as whole blocks naturally vanish on restore.
    _sent = {}

    def _protect_inline(m):
        key = "\x00K%d\x00" % len(_sent)
        _sent[key] = m.group(0)
        return key

    if inline_spans:
        _protected = re.sub(r"`[^`\n]*`", _protect_inline, clean_text)
        clean_text = _strip_wrapper_tags(_protected).strip()
        for _k, _v in _sent.items():
            clean_text = clean_text.replace(_k, _v)
    else:
        clean_text = _strip_wrapper_tags(clean_text).strip()
    # [v77 PR#70 port] When the model drops the tool name, infer it from the
    # request's tool schemas
    tools = _repair_tool_names(tools)
    return tools, clean_text


# Family-wide closer pattern for the tool-call wrapper family, including
# the |/｜-decorated variants that parse_tools accepts. Used by
# StreamToolParser so a mismatched closer closes the open block instead of
# hanging until flush(). [FIX 3]
_TOOL_END_TAG_RE = re.compile(
    r"</[｜\| \t]{0,8}(?:DSML[｜\| \t]{0,8}>?)?\s*(?:tool_calls?|calls|invoke|function_calls?)\b[^>]*>",
    re.IGNORECASE,
)

# [FIX 4] Spaced-DSML dialect: deepseek-harness emits decorated tags with a
# space between the ｜｜DSML｜｜ marker and the tag name, e.g.
# "<｜｜DSML｜｜ invoke name=...>". Entry detection cannot rely on plain
# substring start tags; this regex accepts bars, the DSML marker and the
# space in any combination.
_STREAM_ENTRY_RE = re.compile(
    r"<[｜\| \t]{0,8}(?:DSML[｜\| \t]{0,8}>?)?\s*(tool_calls?|calls|function_calls?|invoke)\b[^>]*>",
    re.IGNORECASE,
)

# Orphan closers trailing a block already closed by the per-tag or family
# fallback (e.g. "</｜｜DSML｜｜ calls>" after the inner invoke was flushed).
_ORPHAN_CLOSER_RE = re.compile(
    r"</[｜\| \t]{0,8}(?:DSML[｜\| \t]{0,8}>?)?\s*(?:tool_calls?|calls|function_calls?|invoke)\b[^>]*>",
    re.IGNORECASE,
)

# [FIX 5] Bare DSML marker: a "<｜｜DSML｜｜>" / "</｜｜DSML｜｜>" whose tag-name
# slot is empty (nothing between the DSML marker and ">"). Every other cleanup
# regex in this module requires an actual tag name (tool_calls/calls/invoke/
# function_call/parameter), so these bare markers used to slip through and get
# streamed to the client as literal text. Note the tag-name slot is required to
# be EMPTY: "<｜｜DSML｜｜ invoke ...>" does not match and is left for the entry
# regex.
_BARE_DSML_MARKER_RE = re.compile(
    r"</?[｜\| \t]{0,8}DSML[｜\| \t]{0,8}\s*>(?!\s*(?:tool_calls?|calls|function_calls?|invoke|parameter)\b)",
    re.IGNORECASE,
)


# Full wrapper-tag family (open or close, with an optional tag name). Used on
# the non-streaming path and on flush() to strip wrapper noise; unlike
# _ORPHAN_CLOSER_RE it also removes stray OPEN tags.
_WRAPPER_TAG_RE = re.compile(
    r"</?[｜\| \t]{0,8}(?:DSML[｜\| \t]{0,8}>?)?\s*(?:tool_calls?|calls|function_calls?|invoke|parameter)[^>]*>",
    re.IGNORECASE,
)

# [v33] Decorated tag with an ARBITRARY tag name. The fixed keyword lists above
# only know tool_calls/calls/invoke/parameter, but the upstream harness also
# emits closing tags whose name slot holds a parameter key or attribute residue:
#     </｜｜DSML｜｜ tool_title" string="true">
# which used to survive every rule and land inside the client's Bash command
# (-> /bin/sh: unterminated quoted string). Anything carrying the DSML marker is
# protocol garbage regardless of the name that follows.
_DECOR_TAG_RE = re.compile(
    r"</?[｜\| \t]{0,8}DSML[｜\| \t]{0,8}\s*[^\s<>]{0,40}[^<\n]{0,120}>",
    re.IGNORECASE,
)

# [v33] A '<' / '</' left dangling at the end of a flush, with no tag body.
_DANGLING_LT_RE = re.compile(r"<[/｜\| \t]{0,8}$")

# [v33] Closer-only variant for the prose path: never touches an OPEN tag
# (mid-stream prose may legitimately contain one), but kills every closer that
# carries the DSML marker, whatever name sits in the tag-name slot.
_DECOR_CLOSER_RE = re.compile(
    r"</[｜\| \t]{0,8}DSML[｜\| \t]{0,8}\s*[^\s<>]{0,40}[^<\n]{0,120}>",
    re.IGNORECASE,
)

# [v33] A "</" that is not followed by an ASCII letter can never open a real
# closing tag. This is what leaked into Bash commands as a bare "</".
_BARE_CLOSE_RE = re.compile(r"</(?![A-Za-z])")


_DECOR_BLOCK_RE = re.compile(
    # [v77.6] The head tolerates an optional "/": the model emits hybrid
    # "closing-slash + open tag" forms like </｜｜DSML｜｜ parameter name=...>
    # (production evidence 2026-10-27 session 60e216a4, the echo parameter
    # leaking wholesale into prose). Once the old <[|｜...]DSML anchor was
    # broken by the "/", the cleanup rules only shredded it further and the
    # residue leaked out as anchorless text.
    r"</?[|｜ \t]{0,8}DSML[|｜ \t]{0,8}[^>\n]{0,200}>"
    r"[^\n]*?"
    r"(?:</?)?[|｜ \t]{0,8}DSML[|｜ \t]{0,8}[^>\n]{0,200}>",
    re.IGNORECASE,
)

_DECOR_OPEN_RE = re.compile(
    r"</?[|｜ \t]{0,8}DSML[|｜ \t]{0,8}[^>\n]{0,200}>",
    re.IGNORECASE,
)


def _strip_stray_markers(text: str) -> str:
    """Prose path: drop bare DSML markers + orphan closers only.

    Open tags are left untouched here so literal prose mentioning e.g.
    "" is not silently deleted mid-stream.
    """
    if not text:
        return text
    # [v57 ported from v39] whole marked block on one line (open + content + close)
    text = _DECOR_BLOCK_RE.sub("", text)
    # [v57 ported from v39] lone marked open tag with any name
    text = _DECOR_OPEN_RE.sub("", text)
    text = _sub_bare_markers(text)
    text = _ORPHAN_CLOSER_RE.sub("", text)
    # [v33] closer with arbitrary tag name, then any impossible "</"
    text = _DECOR_CLOSER_RE.sub("", text)
    text = _BARE_CLOSE_RE.sub("", text)
    return text


def _sub_bare_markers(text: str) -> str:
    """Drop bare DSML markers, but NEVER when what follows could be the head
    of a tool tag. Streaming chunks arrive mid-tag (e.g. "<｜DSML｜>invok"),
    so a plain negative lookahead on complete keywords is not enough: the
    keyword itself may still be incomplete. Keep the marker if the following
    text is a prefix of, or starts with, any tool-tag keyword."""
    def _repl(m):
        tail = text[m.end():m.end() + 24].lstrip()
        low = tail.lower()
        if not low:
            # Nothing follows the marker: no tool head can start here — drop
            # it. (k.startswith("") is vacuously True and used to keep
            # end-of-buffer bare markers alive, leaking them to clients.)
            return ""
        for k in ("tool_call", "calls", "function_call", "invoke", "parameter"):
            if low.startswith(k) or k.startswith(low):
                return m.group(0)
        # [v24] the marker itself is garbage; a short trailing fragment the
        # model emitted on the same construct (e.g. "<｜｜DSML｜｜>param>")
        # is garbage too — drop it together with the marker.
        return ""
    # [v24] marker+fragment PAIRS first (while the marker is still there to
    # anchor the fragment), then any remaining bare markers.
    _DECOR_FRAGMENT_RE = re.compile(r"</?[｜\| \t]{0,8}DSML[｜\| \t]{0,8}\s*>[^<\n]{0,12}>", re.IGNORECASE)
    bare = _DECOR_FRAGMENT_RE.sub("", text)
    return _BARE_DSML_MARKER_RE.sub(_repl, bare)


def _strip_wrapper_tags(text: str) -> str:
    """Wrapper path: drop the whole tag family (open or close) + bare markers."""
    if not text:
        return text
    text = _sub_bare_markers(text)
    text = _WRAPPER_TAG_RE.sub("", text)
    # [v33] whatever the keyword lists missed, the marker decides
    text = _DECOR_TAG_RE.sub("", text)
    text = _BARE_CLOSE_RE.sub("", text)
    return text


def _append_clean_text(results, text: str) -> None:
    """Append cleaned prose, skipping empty deltas after marker stripping."""
    cleaned = _strip_stray_markers(text)
    if cleaned:
        results.append({"text": cleaned})


# Tag names _STREAM_ENTRY_RE can open on.
_STREAM_ENTRY_TAGS = ("tool_calls", "tool_call", "function_calls", "function_call", "invoke", "calls")

def _skip_bars(text, pos):
    # Skip any mix of bars/spaces/tabs (decorated dialects interleave them,
    # e.g. "| | DSML | | >"); newlines are NOT decor — never cross lines.
    while pos < len(text) and text[pos] in "|｜ \t":
        pos += 1
    return pos

def _is_plausible_bare_opener(line):
    """True while `line` (no newline yet) could still become a bare opener
    (decorated DSML tag missing its leading '<'). Prefix-tolerant: '|',
    '| ', '| | D' all hold; '| a' (markdown table) does not."""
    s = line.lstrip()
    if not s or s.startswith("<"):
        return False
    m = re.match(r"^[｜\| \t]+", s)
    if not m:
        return False
    rest = s[m.end():]
    if not rest:
        return True  # pure decoration so far
    m2 = re.match(r"^D(?:S(?:M(?:L?)?)?)?", rest, re.IGNORECASE)
    if not m2:
        return False
    rest2 = rest[m2.end():]
    if not rest2:
        return True
    m3 = re.match(r"^[｜\| \t]+", rest2)
    if not m3:
        return False
    rest3 = rest2[m3.end():]
    if not rest3:
        return True
    low = rest3.lower()
    return any(k.startswith(low) for k in ("tool_call", "calls", "function_call", "invoke", "parameter"))


def _is_plausible_stream_entry_prefix(segment: str) -> bool:
    if not segment.startswith("<"):
        return False
    body = segment[1:]
    pos = _skip_bars(body, 0)
    length = len(body)
    if pos < length and body[pos] in "Dd":
        matched = 0
        while matched < 4 and pos < length and body[pos].upper() == "DSML"[matched]:
            pos += 1
            matched += 1
        if matched < 4:
            return ">" not in body   # partial "DSML" — only plausible while no stray '>'
        pos = _skip_bars(body, pos)
        # B-dialect decorated '>' ("<｜DSML｜>invoke ...") may arrive mid-entry
        # while the tag/attributes are still streaming in. It is part of the
        # decoration, not the entry terminator; skip it and keep holding.
        if pos < length and body[pos] == ">":
            pos += 1
    else:
        # No DSML decoration: any '>' here means the segment is not a
        # plausible incomplete entry prefix.
        if ">" in body:
            return False
    while pos < length and body[pos].isspace():
        pos += 1
    if pos == length:
        return True
    remainder = body[pos:]
    if ">" in remainder:
        return False
    remainder_lower = remainder.lower()
    for tag in _STREAM_ENTRY_TAGS:
        if tag.startswith(remainder_lower):
            return True
        if remainder_lower.startswith(tag):
            suffix = remainder[len(tag):]
            if not suffix or not (suffix[0].isalnum() or suffix[0] == "_"):
                return True
    return False

def _is_plausible_stream_closer_prefix(segment: str) -> bool:
    return segment.startswith("</") and _is_plausible_stream_entry_prefix("<" + segment[2:])


class StreamToolParser:
    def __init__(self):
        self.buffer = ""
        self.in_tool = False
        self.has_tool = False
        self.json_done = False
        self._end_re = None
        # [v59] Swallow whitespace-only prose after a tool has been emitted.
        # Newlines inside tool markup (the "\n" the model writes between
        # </…invoke> and </…calls>) make the family-wide closer [FIX 3] close
        # the block early at the invoke closer, and the trailing "\n" then
        # leaks into content as prose — every tool turn gained an extra
        # orphan newline (2026-10-24 user report "an extra newline appears
        # whenever a tool is called"; the other half of that bug was the
        # bridge's own leading "\n", already stripped in app.py's splitter).
        # Real prose (non-whitespace-only) is unaffected and passes through
        # with the flag cleared.
        # [v77.10] Initial value changed to True: leading whitespace-only
        # chunks at stream start are swallowed too. Production evidence
        # 2026-10-29 session f33a197e: the model habitually writes "\n\n"
        # between </think> and the tool block; once the block is stripped
        # that whitespace run becomes the entire turn's prose — the client
        # concatenates the prose of a dozen tool turns and the user sees
        # "a pile of spaces after the thinking" (v59 only covered after the
        # tool block, not the start or between blocks). Only whitespace-only
        # chunks are swallowed; chunks with actual text (including
        # between-paragraph newlines) pass through normally.
        self._post_tool_ws = True
        # [v77.11] Whitespace hold-back buffer: whitespace chunks are held
        # instead of emitted, until the next output (real text / tool block /
        # end of stream) settles their fate. Replaces v77.10's "lstrip after
        # a boundary", eliminating its only cost (the first paragraph after a
        # tool block would lose its leading indentation) — under the hold-back
        # scheme any text containing actual characters is never modified;
        # whitespace is only normalized at boundaries with a
        # "strip up to the last newline" rule (spaces/tabs after it are
        # deliberate indentation and are preserved).
        self._pending_ws = ""

    def _emit_text(self, results, text: str) -> None:
        """Append prose. [v77.11] Hold-back-and-settle semantics:
        - whitespace-only chunk -> held (_pending_ws), decision deferred;
        - first real text after a boundary (stream start / just emitted a tool
          block) -> merged with the held whitespace, stripped of layout noise
          up to the last newline; spaces/tabs after it are preserved verbatim
          (the model's deliberate indentation, zero loss);
        - real text not at a boundary -> emit the held whitespace first (the
          normal between-paragraph newlines of flowing prose), then emit this
          text verbatim — any text containing actual characters is never
          modified."""
        cleaned = _strip_stray_markers(text)
        if not cleaned:
            return
        if not cleaned.strip():
            self._pending_ws += cleaned
            return
        if self._post_tool_ws:
            self._post_tool_ws = False
            combined = self._pending_ws + cleaned
            self._pending_ws = ""
            i = 0
            n = len(combined)
            while i < n and combined[i] in " \t\r\n":
                i += 1
            last_nl = combined.rfind("\n", 0, i)
            out = combined[last_nl + 1:] if last_nl != -1 else combined
            if out:
                results.append({"text": out})
            return
        if self._pending_ws:
            results.append({"text": self._pending_ws})
            self._pending_ws = ""
        results.append({"text": cleaned})

    def feed(self, chunk):
        self.buffer += chunk
        results = []
        while True:
            if self.in_tool:
                # [FIX 3] Family-wide closer fallback: accept ANY wrapper closer
                # the tool-call family can emit (including |/｜-decorated
                # variants parse_tools tolerates) instead of hanging an open
                # block until flush() when the model closes with a wrong tag.
                # [FIX 4] Prefer the closer for the tag that OPENED the block
                # so nested wrappers (<｜｜DSML｜｜ calls> wrapping invokes)
                # close as one unit; fall back to the family-wide closer for
                # mismatched or decorated closers [FIX 3].
                end_match = self._end_re.search(self.buffer) if self._end_re else None
                if end_match is None:
                    end_match = _TOOL_END_TAG_RE.search(self.buffer)
                if end_match:
                    if not self.json_done:
                        tool_xml = self.buffer[: end_match.end()]
                        parsed, _ = parse_tools(tool_xml)
                        for item in parsed:
                            if _valid_tool_call(item):
                                results.append({"tool": item})
                                self._post_tool_ws = True
                    self.buffer = self.buffer[end_match.end():]
                    self.in_tool = False
                    self.json_done = False
                    self._end_re = None
                    continue
                brace_idx = self.buffer.find("{")
                if brace_idx != -1 and not self.json_done:
                    decoder = json.JSONDecoder()
                    try:
                        data, consumed = decoder.raw_decode(self.buffer[brace_idx:])
                        norm = normalize_tool_call(data)
                        if norm and _valid_tool_call(norm):
                            results.append({"tool": norm})
                            self._post_tool_ws = True
                            self.json_done = True
                            self.buffer = self.buffer[brace_idx + consumed:]
                            continue
                    except Exception:
                        pass
                break
            else:
                # [FIX 6] repair bare decorated openers (missing '<') so the
                # entry regex can catch them instead of leaking as prose.
                self.buffer = _repair_bare_dsml_lines(self.buffer)
                # [FIX 4] Regex entry detection replaces plain substring finds
                # so decorated/spaced DSML openers enter tool mode.
                m = _STREAM_ENTRY_RE.search(self.buffer)
                if m:
                    start = m.start()
                    if start > 0:
                        # [v77.10] Strip trailing whitespace from the prose
                        # chunk before a tool block (the "\n\n" the model
                        # writes between prose and the tool block sticks to
                        # the chunk tail and remains after the block is
                        # stripped)
                        self._emit_text(results, self.buffer[:start].rstrip())
                    self.buffer = self.buffer[start:]
                    tag_name = m.group(1).lower()
                    self._end_re = re.compile(
                        r"</[｜\| \t]{0,8}(?:DSML[｜\| \t]{0,8}>?)?\s*" + re.escape(tag_name) + r"[｜\| \t]{0,8}\s*>",
                        re.IGNORECASE,
                    )
                    self.in_tool = True
                    self.has_tool = True
                    continue
                # [FIX 8] A bare opener line (missing leading '<', e.g.
                # "| | DSML | | invoke ...") can only be repaired once its
                # full line is in the buffer (_repair_bare_dsml_lines is
                # line-anchored). While the LAST line is still streaming in
                # (prefix could still become decoration+DSML+tag, no newline
                # yet), hold it — but complete earlier lines are emitted.
                last_nl = self.buffer.rfind("\n")
                tail_line = self.buffer[last_nl + 1:]
                if tail_line and _is_plausible_bare_opener(tail_line):
                    if last_nl != -1:
                        self._emit_text(results, self.buffer[:last_nl + 1])
                        self.buffer = tail_line
                    break
                # [FIX 2] Hold an unclosed '<' tail only while it remains a
                # plausible partial match of _STREAM_ENTRY_RE or of a wrapper
                # closer ("</..."), so a split closer is never dumped as prose.
                # [FIX 7] The prefix predicates themselves decide whether an
                # embedded decorated '>' (B-dialect "<｜DSML｜>invoke ...") is
                # acceptable mid-entry — no blanket '" not in tail' short-circuit,
                # which used to dump the tail as prose the moment the dialect's
                # decoration '>' arrived but before the entry's own '>' did.
                last_lt = self.buffer.rfind("<")
                tail = self.buffer[last_lt:] if last_lt != -1 else ""
                hold = last_lt != -1 and (
                    _is_plausible_stream_entry_prefix(tail)
                    or _is_plausible_stream_closer_prefix(tail)
                )
                if last_lt != -1 and hold:
                    if last_lt > 0:
                        self._emit_text(results, self.buffer[:last_lt])
                    self.buffer = tail
                    break
                if self.buffer:
                    self._emit_text(results, self.buffer)
                self.buffer = ""
                break
        return results

    def flush(self):
        out = []
        if self.buffer and not self.in_tool:
            # [FIX 8] Last chance: the stream ended before the bare-opener
            # line got its newline — repair now (end-of-string closes the
            # line for the line-anchored regex), then re-dispatch.
            if True:
                repaired = _repair_bare_dsml_lines(self.buffer)
                m = _STREAM_ENTRY_RE.search(repaired)
                if m:
                    start = m.start()
                    pre = repaired[:start]
                    if pre.strip():
                        # [v77.10] Same as the feed path: strip trailing
                        # whitespace before a tool block
                        self._emit_text(out, pre.rstrip())
                    parsed, clean_tail = parse_tools(repaired[start:])
                    for item in parsed:
                        if _valid_tool_call(item):
                            out.append({"tool": item})
                            self._post_tool_ws = True
                    if clean_tail.strip():
                        self._emit_text(out, clean_tail)
                    self.buffer = ""
                    return out
            self._emit_text(out, self.buffer)
        elif self.in_tool:
            # [FIX 1] Recover the tool before stripping: if the stream was cut
            # off before the closing tag arrived but the payload itself is
            # parseable, emit the tool call instead of dumping raw parameter
            # values into the chat text. Strip only when nothing parses.
            parsed, _ = parse_tools(self.buffer)
            if parsed:
                for item in parsed:
                    if _valid_tool_call(item):
                        out.append({"tool": item})
                        self._post_tool_ws = True
            elif not self.json_done:
                stripped = _strip_wrapper_tags(self.buffer).strip()
                if stripped:
                    out.append({"text": stripped})
            # With json_done set, whatever is left after the consumed JSON
            # payload is wrapper noise (partial closers / whitespace) and is
            # dropped instead of leaking into the chat text.
        self.buffer = ""
        self.in_tool = False
        self.json_done = False
        self._end_re = None
        return out


# ── [v81] Tool-marker leak gate ─────────────────────────────────────────
# Hard evidence 2026-10-31 20:38 (chat 86983f4f, two consecutive turns): the
# model emitted malformed tool markup the parser could not recognize
# (<tool_call>{"arguments": ...}, no invoke structure), StreamToolParser
# could only pass it through as ordinary prose, and the user saw raw markup
# in the response body; the functions layer only noticed "the prose is
# nothing but unparsed markers" at end of stream (FINISHED) and triggered
# auto-continuation — too late, the markup had already streamed out live.
# This gate hangs after StreamToolParser: any prose text containing a member
# of the tool-marker family (including a "half marker" split across chunks)
# is held; flush() rules uniformly — fenced content passes normally
# (teaching examples are exempt, matching parse_tools' hidden rules), marker
# residue outside fences is dropped (repair/continuation is handled by the
# existing salvage + auto-continue chain, whose input is the raw upstream
# chunks and which does not pass through this gate — behavior unchanged).
# Normal prose is untouched.
_LEAK_MARKERS = (
    "<tool_call>", "</tool_call>", "<tool_calls>", "</tool_calls>",
    "<invoke>", "</invoke>", "<function_call>", "</function_call>",
    "<tool_call/>", "<tool_calls/>", "<invoke/>", "<function_call/>",
    "<｜｜DSML", "</｜｜DSML", "<||DSML", "</||DSML",
)


class _LeakGate:
    """The last gate before prose streams out: tool-marker family members may
    only travel through the tool_calls channel, never leak to the user as
    content."""

    def __init__(self):
        self._seen = ""    # all prose the gate has seen (for fence-span math)
        self._hold = ""    # hold-back area (from the first suspected marker)
        self.dropped = 0   # characters dropped (for caller-side logging)

    def feed(self, text):
        self._seen += text
        self._hold += text
        return self._drain()

    def _drain(self):
        out = []
        pos = -1
        for m in _LEAK_MARKERS:
            p = self._hold.find(m)
            if p != -1 and (pos == -1 or p < pos):
                pos = p
        if pos != -1:
            if pos:
                out.append(self._hold[:pos])
            self._hold = self._hold[pos:]  # isolate from the marker to flush
            return out
        # No complete marker: hold the tail if it could be a "half marker"
        # (joined across chunks)
        keep = 0
        for m in _LEAK_MARKERS:
            for k in range(min(len(m) - 1, len(self._hold)), keep, -1):
                if self._hold.endswith(m[:k]):
                    keep = k
                    break
        cut = len(self._hold) - keep
        if cut > 0:
            out.append(self._hold[:cut])
            self._hold = self._hold[cut:]
        return out

    def flush(self):
        """End-of-stream ruling: inside fences pass, outside fences drop."""
        if not self._hold:
            return []
        hold, self._hold = self._hold, ""
        start = len(self._seen) - len(hold)
        spans = _code_fence_spans(self._seen)
        # split into fenced / unfenced segments
        segs = []
        seg, seg_f = "", None
        for i, ch in enumerate(hold):
            f = any(s <= start + i < e for s, e in spans)
            if seg_f is None or f == seg_f:
                seg += ch
            else:
                segs.append((seg, seg_f))
                seg = ch
            seg_f = f
        if seg:
            segs.append((seg, seg_f))
        emit = []
        for text, fenced in segs:
            if fenced or not any(m in text for m in _LEAK_MARKERS):
                # Fenced teaching examples pass as usual; unfenced leftover
                # prose without markers passes too
                emit.append(text)
            else:
                # Tool-marker residue outside fences: dropped (repair goes to
                # salvage, retry goes to auto-continuation)
                self.dropped += len(text)
        return ["".join(emit)] if emit else []


def summarize_messages(messages, max_tokens=500):
    recent = messages[-10:] if len(messages) > 10 else messages
    parts = []
    for msg in recent:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content if c.get("type") == "text")
        if content:
            parts.append(f"{role}: {content[:200]}")
    summary = "\n".join(parts)
    tokens = count_tokens(summary)
    while tokens > max_tokens and len(parts) > 1:
        parts = parts[1:]
        summary = "\n".join(parts)
        tokens = count_tokens(summary)
    return summary


_pow_setup = None


def _get_pow_setup():
    global _pow_setup
    if _pow_setup is None:
        engine = wasmtime.Engine()
        with open(wasm_path, "rb") as f:
            module = wasmtime.Module(engine, f.read())
        _pow_setup = (engine, module, wasmtime.Linker(engine))
    return _pow_setup


def _find_pow_answer_blocking(challange_data):
    engine, module, linker = _get_pow_setup()
    store = wasmtime.Store(engine)
    instance = linker.instantiate(store, module)
    memory = instance.exports(store)["memory"]
    alloc_func = instance.exports(store)["alloc"]
    solve_func = instance.exports(store)["solve_pow"]
    ch_ptr, ch_len = write_string_pow(challange_data["challenge"], alloc_func, memory, store)
    salt_ptr, salt_len = write_string_pow(challange_data["salt"], alloc_func, memory, store)
    result = solve_func(store, ch_ptr, ch_len, salt_ptr, salt_len, challange_data["expire_at"], challange_data["difficulty"])
    if result < 0:
        result = result + 0x10000000000000000
    return result if result != 0xFFFFFFFFFFFFFFFF else None


async def create_challange_pow(target_path, auth_token):
    headers = get_headers(auth_token)
    # Backup: WAF cookies not required with Android headers. Kept as fallback:
    # cookie = await get_cookies()
    response = await post_with_failover(
        "/api/v0/chat/create_pow_challenge",
        headers=headers, json={"target_path": target_path},
        # cookies=cookie,  # Backup WAF fallback
        timeout=aiohttp.ClientTimeout(total=20),
    )
    async with response:
        data = await response.json()
    return _biz_data(data, "chat/create_pow_challenge")["challenge"]


def write_string_pow(text, alloc_func, memory, store):
    data = text.encode("utf-8")
    ptr = alloc_func(store, len(data))
    mem = memory.data_ptr(store)
    for i in range(len(data)):
        mem[ptr + i] = data[i]
    return ptr, len(data)


async def find_pow_answer(challange_data):
    return await asyncio.to_thread(_find_pow_answer_blocking, challange_data)


async def solve_create_pow(target_path, auth_token):
    pow = await create_challange_pow(target_path, auth_token)
    answer = await find_pow_answer(pow)
    if answer is None:
        raise Exception("PoW solve failed")
    json_data = {
        "algorithm": "DeepSeekHashV1",
        "challenge": pow["challenge"],
        "salt": pow["salt"],
        "answer": answer,
        "signature": pow["signature"],
        "target_path": target_path,
    }
    return base64.b64encode(json.dumps(json_data).encode()).decode()


# ── PoW prefetch pool (v26) ─────────────────────────────────────────────
# Every completion turn used to pay a full create_pow_challenge round-trip
# (~200-400ms) plus wasm solve CPU before the real request could fire. The
# challenge carries its own expire_at, so we pre-solve one in the background
# right after each turn completes and the next turn consumes it instantly.
# Request rate towards create_pow_challenge is unchanged (1 per turn), only
# its timing moves earlier — zero semantic change for the upstream.
_POW_PREFETCH: dict = {}
_POW_PREFETCH_MAX = 8


async def _prefetch_pow(auth_token, target_path="/api/v0/chat/completion"):
    try:
        p = await solve_create_pow(target_path, auth_token)
        try:
            data = json.loads(base64.b64decode(p))
            expire_at = float(data.get("expire_at") or 0)
        except Exception:
            expire_at = 0
        key = (auth_token, target_path)
        if len(_POW_PREFETCH) >= _POW_PREFETCH_MAX:
            _POW_PREFETCH.pop(next(iter(_POW_PREFETCH)), None)
        # [v87] Upstream PoW payloads no longer carry expire_at (as measured
        # 2026-11-04 the key set is only algorithm/answer/challenge/salt/
        # signature/target_path), so the expiry check never fires on 0 ->
        # stale credentials in the prefetch pool get used as usual -> 40301
        # INVALID_POW_RESPONSE. Record solved_at and drop entries past a hard
        # TTL at take time.
        _POW_PREFETCH[key] = (p, expire_at, time.time())
    except Exception:
        pass


_POW_PREFETCH_TTL = 600  # [v87] challenge lifetime unknown (no expire_at in payload), 10-minute hard cap


def _take_prefetched_pow(auth_token, target_path="/api/v0/chat/completion"):
    entry = _POW_PREFETCH.pop((auth_token, target_path), None)
    if not entry:
        return None
    if len(entry) == 2:
        entry = (entry[0], entry[1], time.time())
    p, expire_at, solved_at = entry
    if expire_at and expire_at < time.time() + 30:
        return None  # too close to expiry — solve fresh
    if time.time() - solved_at > _POW_PREFETCH_TTL:
        return None  # [v87] entry without expire_at past the hard TTL — solve fresh, never send stale PoW
    return p


# ── HIF risk-control token (v89.8) ──────────────────────────────────────
# Community packet capture (2026-10-07, ds-free-api #97/#112) proved the
# official client attaches an x-hif-leim header to /chat/completion
# requests — a short-lived token issued by hif-leim.deepseek.com (response
# header x-hif-ttl, default 600s). Every third-party proxy omitted this
# header; since the November 2026 risk-control tightening, a missing header
# lets the upstream identify a non-official client with zero behavioral
# statistics — matching the instant-mute wave. Official behavior: poll on
# startup, re-fetch per TTL, and on poll failure simply send without the
# header — this implementation mirrors that one-to-one:
#   * cached per x-hif-ttl, treated as expired 30s before the deadline
#   * a failed fetch backs off 60s and returns None: the caller degrades to
#     sending without the header — exactly the historical behavior, zero
#     new risk
#   * never sends a stale token (expired = dropped), never blocks requests
# The endpoint is unauthenticated, needs no account token, and generates
# zero account-side traffic.
_HIF_QUERY_URL = os.getenv("DEEPSEEKER_HIF_URL", "https://hif-leim.deepseek.com/query")
# [v89.11] Default OFF. Community capture (artyom08-dev) proves the android
# app never sends x-hif-leim (it is a web-frontend header); carrying it on an
# android-profile client creates a hybrid identity — an inconsistency signal
# in itself. The Nov 2 environment checks sit only on the browser/login path,
# so nothing web-flavored is needed on the android path. Re-enable with
# DEEPSEEKER_HIF_ENABLED=1 if ever needed.
_HIF_ENABLED = os.getenv("DEEPSEEKER_HIF_ENABLED", "0").strip().lower() not in ("0", "false", "off")
_HIF_DEFAULT_TTL = 600.0    # official default when x-hif-ttl is absent
_HIF_REFRESH_MARGIN = 30.0  # treat as expired 30s early — a stale token never goes on a request
_HIF_FAIL_BACKOFF = 60.0    # retry backoff after a failed fetch
_HIF_FETCH_TIMEOUT = 5.0    # network budget: degrade rather than slow the chat
_hif_cache: dict = {"value": None, "expire_at": 0.0, "retry_at": 0.0}
_hif_lock = asyncio.Lock()


async def get_hif_token():
    """[v89.8] Fetch the x-hif-leim risk-control token; returns None on any
    failure and the caller degrades to sending without the header (exactly
    what the real client does when its poll fails). Cache hits cost zero
    network and zero wait."""
    if not _HIF_ENABLED:
        return None
    now = time.time()
    if _hif_cache["value"] and now < _hif_cache["expire_at"]:
        return _hif_cache["value"]  # fast path: lock-free
    if now < _hif_cache["retry_at"]:
        return None  # inside backoff: degrade, never send a stale token
    async with _hif_lock:
        now = time.time()  # time advanced while waiting for the lock
        if _hif_cache["value"] and now < _hif_cache["expire_at"]:
            return _hif_cache["value"]  # double-check: another request refreshed it
        if now < _hif_cache["retry_at"]:
            return None
        try:
            session = await get_session()
            async with session.get(
                _HIF_QUERY_URL,
                timeout=aiohttp.ClientTimeout(total=_HIF_FETCH_TIMEOUT),
            ) as resp:
                ttl = resp.headers.get("x-hif-ttl")
                try:
                    ttl = float(ttl)
                except (TypeError, ValueError):
                    ttl = _HIF_DEFAULT_TTL
                js = await resp.json(content_type=None)
                value = ((js.get("data") or {}).get("biz_data") or {}).get("value")
            if value:
                _hif_cache["value"] = value
                _hif_cache["expire_at"] = now + max(ttl - _HIF_REFRESH_MARGIN, 1.0)
                _hif_cache["retry_at"] = 0.0
                return value
            logger.warning("[hif] risk-control endpoint replied without a value, retrying in 60s")
        except Exception as exc:
            logger.warning("[hif] failed to fetch risk-control token (degrading to no header): %s", exc)
        _hif_cache["retry_at"] = time.time() + _HIF_FAIL_BACKOFF
        return None


async def warmup_pool(max_tokens=3):
    """[v57 ported from v35] Startup warmup: moves ~30s of cold-start cost to
    the startup phase.

    Three things done in order so the first user request no longer pays:
      1. wasm PoW module compilation (CPU-bound, on a thread pool)
      2. TLS connection to the upstream chat.deepseek.com (transoceanic
         handshake is the most expensive)
      3. Pre-solve PoW with ACTIVE tokens to fill _POW_PREFETCH
    Any step failing only logs; startup of the service is never affected.
    """
    t0 = time.time()
    try:
        await asyncio.to_thread(_get_pow_setup)
    except Exception as exc:
        logger.warning("[warmup] wasm pre-compilation failed: %s", exc)
    try:
        await get_session()
    except Exception as exc:
        logger.warning("[warmup] failed to establish upstream session: %s", exc)
    try:
        toks = await asyncio.to_thread(get_tokens)
        actives = [t for t in toks if t.get("status") == "ACTIVE"][:max_tokens]
        for t in actives:
            try:
                await _prefetch_pow(t["token"], "/api/v0/chat/completion")
            except Exception:
                pass
        logger.info("[warmup] token=%d warmup complete", len(actives))
    except Exception as exc:
        logger.warning("[warmup] PoW warmup failed: %s", exc)
    # [v89.7] Silent startup scan: probe users/current once for every ACTIVE
    # token. Only a confirmed mute (is_muted==1, upstream boolean hard
    # evidence) quarantines — runtime detection waits for a real failure,
    # during which the dashboard would show the muted account as "Active"
    # (production-verified 2026-11-05). Invalidated tokens (40003) are only
    # logged, never written: expired-token quarantine is the runtime chain's
    # job and is proven correct (same day, token #1). Any scan failure is
    # logged only and never blocks startup.
    try:
        toks_all = await asyncio.to_thread(get_tokens)
        for t in [x for x in toks_all if x.get("status") == "ACTIVE"]:
            _pr = await probe_token_mute(t["token"])
            if _pr.get("verdict") == "muted":
                _until = _format_mute_until(_pr.get("mute_until"))
                await asyncio.to_thread(
                    mark_quarantined,
                    t["id"],
                    f"user is muted (until {_until}) — startup probe",
                )
                logger.warning(
                    "Token #%d: confirmed muted until %s (startup probe) — quarantined",
                    t["id"], _until,
                )
    except Exception as exc:
        logger.warning("[warmup] mute scan failed: %s", exc)
    # [v89.8] The official client polls the hif-leim risk-control token on
    # startup; prefetch once so the first chat request already carries it.
    # Failure only logs: runtime retries with backoff and degrades to
    # sending without the header (= historical behavior) — startup is never
    # affected.
    try:
        if _HIF_ENABLED:
            if await get_hif_token():
                logger.info("[warmup] hif risk-control token prefetched")
            else:
                logger.warning("[warmup] hif risk-control token not prefetched (runtime retries automatically, degrading to no header)")
        else:
            logger.info("[warmup] hif disabled (pure android profile, no x-hif-leim header)")
    except Exception as exc:
        logger.warning("[warmup] hif prefetch error: %s", exc)
    logger.info("[warmup] all done in %.1fs, PoW pool %d entries", time.time() - t0, len(_POW_PREFETCH))


async def create_new_chat(auth_token):
    headers = get_headers(auth_token)
    # Backup: WAF cookies not required with Android headers. Kept as fallback:
    # cookie = await get_cookies()
    response = await post_with_failover(
        "/api/v0/chat_session/create",
        headers=headers,
        # cookies=cookie,  # Backup WAF fallback
        timeout=aiohttp.ClientTimeout(total=20),
    )
    async with response:
        data = await response.json()
    return _biz_data(data, "chat_session/create")["chat_session"]["id"]


async def probe_token_mute(auth_token):
    """[v89.7] Actively probe one token's real upstream status (admin probe /
    startup scan).

    GET /api/v0/users/current — DeepSeek's own app profile endpoint: no PoW,
    read-only, and muted accounts still answer normally (verified 2026-11-05:
    muted accounts return biz_data.chat.is_muted=1/mute_until; invalidated
    tokens return code=40003; healthy tokens return biz_code=0). Same domain
    and same headers as the bridge's everyday upstream calls — no new
    exposure surface.

    Observation only — never writes state. The caller decides what to persist
    by evidence class (confirmed mutes only; invalidated tokens are report-
    only because expired-token quarantine is the runtime chain's job and is
    proven correct). The return carries no credential fields."""
    session = await get_session()
    headers = get_headers(auth_token)
    try:
        async with session.get(
            UPSTREAM_BASES[0] + "/api/v0/users/current",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            body = await resp.json(content_type=None)
    except Exception as exc:
        return {"verdict": "error", "detail": f"probe failed: {exc}", "mute_until": None}
    if not isinstance(body, dict):
        return {"verdict": "error", "detail": "unexpected non-JSON body", "mute_until": None}
    code = body.get("code")
    if code == 40003:
        return {
            "verdict": "invalid",
            "detail": str(body.get("msg") or "upstream 40003 (invalid token)"),
            "mute_until": None,
        }
    data = body.get("data") if isinstance(body.get("data"), dict) else None
    biz = data.get("biz_data") if isinstance(data, dict) else None
    chat = biz.get("chat") if isinstance(biz, dict) else None
    if isinstance(chat, dict) and chat.get("is_muted") == 1:
        return {
            "verdict": "muted",
            "detail": "user is muted",
            "mute_until": chat.get("mute_until"),
        }
    if code == 0 and isinstance(biz, dict):
        return {"verdict": "ok", "detail": "", "mute_until": None}
    return {
        "verdict": "error",
        "detail": f"unexpected envelope: code={code}",
        "mute_until": None,
    }


_EMPTY_SSE_DEFAULT = (
    "Empty response from DeepSeek (no parseable SSE content): "
    "stream ended with no assistant fragments"
)
_EMPTY_SSE_CONTEXT = (
    "Empty response from DeepSeek (prompt may exceed the session context limit)"
)
_CONTEXT_LIMIT_HINT_RE = re.compile(
    r"context|token.?limit|maximum.?context|too.?long|exceed",
    re.IGNORECASE,
)
_SSE_RECENT_LINES = 8


class _SseStreamFinished(Exception):
    pass


class _SseStreamInterrupted(Exception):
    """The upstream stream ended abnormally before FINISHED (stall/disconnect/
    upstream early close).

    [v62] reason/detail carriers: lets send_message's continuation loop pick
    the resend prompt by interruption cause — the malformed-tool-call case
    switches to a diagnostic prompt (telling the model what went wrong,
    attaching the original call, giving the correct format), other cases keep
    using the generic _CONTINUE_PROMPT.
    Default parameters keep the existing bare raises (stall/interruption/
    no_body) zero-changed and behavior-identical."""

    def __init__(self, reason="interrupted", detail=""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class _RetryBoundary(str):
    """[v61] Continuation-turn boundary sentinel: an empty-string str subclass,
    inserted into the client stream only by send_message's continuation loop
    to tell app.stream_response "a new attempt starts here, please reset the
    splitter".

    Why it is needed: app._StreamTagSplitter latches think_done after the
    first </think> (since v46, to tolerate literal <think> in model prose);
    afterwards a <think> in the same stream is no longer recognized — while
    auto-continuation turns start with _send_message_once's "\\n<think>"
    (two yield sites in functions.py), so the continuation turn's whole
    thinking section would be passed through by the splitter as content,
    leaking into prose (2026-10-24 21:25 production instance 1792849156:
    attempt-0 answered normally and was interrupted by an upstream stall;
    attempt-1's "The user says continue from where we left off..." leaked
    wholesale into prose).
    v39/v40 misdiagnosed this as "caused by continuation turns disabling
    thinking" (the B3 thinking leak); rolling back the thinking parameter
    cannot fix it — the root cause is the splitter's cross-turn state, which
    has nothing to do with the thinking switch.

    The empty-string subclass is transparent to other consumers:
    full_text += chunk with an empty string is harmless, non-streaming joins
    are harmless, any len/truthiness check sees an empty string; only
    stream_response intercepts it via isinstance to reset the splitter."""
    __slots__ = ()


def _redact_sse_line(line: str) -> str:
    if len(line) > 500:
        return line[:500] + "..."
    return line


def _remember_sse_line(recent_lines, line: str):
    recent_lines.append(_redact_sse_line(line))
    if len(recent_lines) > _SSE_RECENT_LINES:
        del recent_lines[0]


def _sse_context_limit_hint(recent_lines, parsed_events):
    for obj in parsed_events:
        try:
            blob = json.dumps(obj, ensure_ascii=False)
        except Exception:
            blob = str(obj)
        if _CONTEXT_LIMIT_HINT_RE.search(blob):
            return True
    for line in recent_lines:
        if _CONTEXT_LIMIT_HINT_RE.search(line):
            return True
    return False


def _scan_recent_sse_errors(parsed_events):
    """Return (http_status, message) if a recent parsed event is an upstream error."""
    for obj in reversed(parsed_events):
        if not isinstance(obj, dict) or obj.get("type") != "error":
            continue
        content = obj.get("content") or obj.get("message") or "Upstream error"
        finish = str(obj.get("finish_reason") or "")
        if finish == "rate_limit_reached" or "rate_limit" in finish.lower():
            return 429, content
        if finish in ("permission_denied", "forbidden"):
            return 403, content
        return 502, content
    return None


def _raise_empty_sse_response(recent_lines, parsed_events, resp_ct="", non_sse_tail=None):
    # [v77 P0-4 port] Embed the request correlation id inside the envelope (if
    # the middleware injected one) so failed responses can be matched to
    # access-log lines. Deferred import avoids a dependency-direction problem.
    try:
        from middleware import request_id_var
        _rid = request_id_var.get() or "-"
    except Exception:
        _rid = "-"
    err = _scan_recent_sse_errors(parsed_events)
    if err:
        code, msg = err
        logger.warning(
            "DeepSeek SSE error event (not empty stream); last events: %s",
            recent_lines[-_SSE_RECENT_LINES:],
        )
        raise UpstreamError(code, f"{msg} [rid:{_rid}]")
    # [v87] Business JSON errors in the non-SSE tail (confirmed 2026-11-04:
    # {"code":40301,"msg":"INVALID_POW_RESPONSE"} — a stale prefetched PoW
    # rejected upstream). This is NOT an empty stream: raise it as a real
    # upstream error. It used to fall into the empty-stream default branch ->
    # the v81 auto-continue fallback sent the orphan "system notice" text as
    # a brand-new chat, poisoning the session map -> memory loss everywhere.
    # Note: the error message must NOT contain "no assistant fragments"
    # (the v81b empty-stream retry matches on it).
    for _tail in reversed(non_sse_tail or []):
        _t = (_tail or "").strip()
        if not _t.startswith("{"):
            continue
        try:
            _bj = json.loads(_t)
        except Exception:
            continue
        if isinstance(_bj, dict) and _bj.get("code") is not None:
            _bcode = _bj.get("code")
            _bmsg = _bj.get("msg") or _bj.get("message") or "biz error"
            # [v89.7] Nested business envelope: outer code=0/msg="" while the
            # real error lives in data.biz_code/biz_msg (production-verified
            # 2026-11-05, muted account:
            # {"code":0,"msg":"","data":{"biz_code":5,"biz_msg":"user is
            # muted","biz_data":{"is_muted":1,"mute_until":...}}}). The old
            # code only read the outer layer -> 502 "biz error 0", the mute
            # info was swallowed and the token never entered any quarantine
            # path. This branch already always raised a 502; only WHAT it
            # raises changes: a mute -> UpstreamMutedError (hard evidence,
            # keyed on the upstream is_muted boolean only, no text
            # heuristics); other nested errors re-raise with the real
            # message. Every legacy shape (outer 40301/40003, non-JSON,
            # empty tail) stays byte-identical.
            _ndata = _bj.get("data") if isinstance(_bj.get("data"), dict) else None
            _nbiz = _ndata.get("biz_data") if isinstance(_ndata, dict) else None
            if isinstance(_nbiz, dict) and _nbiz.get("is_muted") == 1:
                logger.warning("DeepSeek biz error in non-SSE body: %s", _t[:200])
                raise UpstreamMutedError(_nbiz.get("mute_until"))
            _nbcode = _ndata.get("biz_code") if isinstance(_ndata, dict) else None
            if _bcode == 0 and _nbcode not in (None, 0):
                _nbmsg = (_ndata.get("biz_msg") or _ndata.get("msg")
                          or _ndata.get("message") or _bmsg)
                logger.warning("DeepSeek nested biz error in non-SSE body: %s", _t[:200])
                raise UpstreamError(
                    502, f"DeepSeek biz error {_nbcode}: {_nbmsg} [rid:{_rid}]",
                )
            if str(_bcode) == "40301" or "POW" in str(_bmsg).upper():
                _POW_PREFETCH.clear()  # invalidate all stale prefetched credentials; retry solves fresh
            logger.warning("DeepSeek biz error in non-SSE body: %s", _t[:200])
            raise UpstreamError(502, f"DeepSeek biz error {_bcode}: {_bmsg} [rid:{_rid}]")
    if _sse_context_limit_hint(recent_lines, parsed_events):
        msg = _EMPTY_SSE_CONTEXT
    else:
        msg = _EMPTY_SSE_DEFAULT
    # [v82 diagnostics] On an empty stream, log the response content type and
    # non-streamed raw text (distinguish WAF HTML / business JSON / a genuinely
    # empty SSE — 2026-10-31 evening protocol-change investigation)
    _tail_txt = " | ".join((non_sse_tail or [])[-3:])[:400]
    logger.warning(
        "DeepSeek SSE ended without assistant output; ct=%s non_sse=%s; last events: %s",
        resp_ct or "-", _tail_txt or "-",
        recent_lines[-_SSE_RECENT_LINES:],
    )
    raise UpstreamError(502, f"{msg} [rid:{_rid}]")


def _raise_sse_error_event(data, recent_lines):
    content = data.get("content") or data.get("message") or "Upstream error"
    finish = str(data.get("finish_reason") or "")
    if finish == "rate_limit_reached" or "rate_limit" in finish.lower():
        logger.warning(
            "DeepSeek SSE error event (not empty stream); last events: %s",
            recent_lines[-_SSE_RECENT_LINES:],
        )
        raise UpstreamError(429, content)
    if finish in ("permission_denied", "forbidden"):
        raise UpstreamError(403, content)
    raise UpstreamError(502, content)


# ============================== [v78] tool-marker salvage ==============================
# Production evidence (2026-10-29 23:15 session cd859ddc, a 298k-token long
# session): while wobbling between the two formats the model dropped the whole
# <｜｜DSML｜｜ invoke name="..."> opener line — <calls> was followed directly by
# <parameter> with only an orphan </invoke> closer left. parse_tools needs the
# name from the opener line to parse -> the whole block fails to parse -> v58
# interrupted on "pure markers, no prose" -> the continuation turn reproduced
# the same shape (the malformed original text attached by v62's diagnostic
# prompt itself became a bad example) -> retries exhausted, the client got
# only the fallback message, and the turn delivered zero prose.
#
# Salvage in two steps (deterministic string operations, DSML double-bar family
# only; think spans and code fences are never touched, matching parse_tools'
# hidden rules):
#   1. Dropped-slash closer repair: `text｜｜DSML｜｜ parameter>` (the model
#      lost </) -> `</｜｜DSML｜｜ parameter>` (2026-10-29 23:10/23:14, the
#      same family of lesion where tool_title values carried trailing
#      fragments; legitimate openers/closers are always preceded by < or /,
#      excluded by negative lookahead, so no false hits).
#   2. Missing-invoke-opener synthesis: when a calls block contains no invoke
#      opener, infer the tool name from the arguments and synthesize the
#      opener line. Name sources, in order:
#      a) the "name" key in the arguments JSON (accepted only if the value is
#         a tool name declared this turn);
#      b) the unique tool in this turn's tool list with required ⊆ argument
#         keys (no guessing among multiple candidates).
#      If inference fails, return unchanged and take the existing malformed
#      continuation path — never guess blindly.
# When no repair happened, returns byte-for-byte identical — zero change, zero
# cost for normal output.

_SALVAGE_CLOSER_FIX_RE = re.compile(
    r"(?<![</])"
    r"[｜|]{2}\s*DSML\s*[｜|]{2}\s*(?:parameter|invoke|calls|tool_call)\s*>"
)
_SALVAGE_CALLS_BLOCK_RE = re.compile(
    r"<[｜\| \t]{0,8}(?:DSML[｜\| \t]{0,8}>?)?\s*(?:tool_calls?|calls)[^>]*>"
    r"(.*?)"
    r"(?:</[｜\| \t]{0,8}(?:DSML[｜\| \t]{0,8}>?)?\s*(?:tool_calls?|calls)\s*>|$)",
    re.DOTALL | re.IGNORECASE,
)
_SALVAGE_INVOKE_OPEN_RE = re.compile(
    r"<[｜\| \t]{0,8}(?:DSML[｜\| \t]{0,8}>?)?\s*(?:invoke|function_call)\s+(?:name|tool)=[\x27\x22]",
    re.IGNORECASE,
)
_SALVAGE_PARAM_RE = re.compile(
    r"<[｜\| \t]{0,8}(?:DSML[｜\| \t]{0,8}>?)?\s*parameter\s+name=[\x27\x22]([^\x27\x22]+)[\x27\x22]([^>]*)>"
    r"(.*?)"
    r"</[｜\| \t]{0,8}(?:DSML[｜\| \t]{0,8}>?)?\s*parameter\s*>",
    re.DOTALL | re.IGNORECASE,
)
_SALVAGE_INVOKE_SYNTH = '\n<｜｜DSML｜｜ invoke name="%s">'

# [v80] Missing-parameter-closer repair kit. Field evidence (2026-10-31
# 11:37 UTC, chat d13d3e76): the upstream wrote the parameter opener but the
# closing tag was lost (<...parameter name="command"...>cd /tmp ... followed
# directly by the next parameter opener); _SALVAGE_PARAM_RE requires a paired
# closer -> the whole parameter match fails -> the tool cannot be inferred ->
# the entire salvage chain is defeated and the flow falls to a continuation
# turn (the user sees the same action execute repeatedly). v80 adds a
# deterministic repair pass before the main salvage: if a real closer exists
# before the boundary -> byte-for-byte untouched; genuinely missing -> insert
# a canonical closer before the next structural boundary.
_SALVAGE_PARAM_OPEN_RE = re.compile(
    r"<[｜\| \t]{0,8}(?:DSML[｜\| \t]{0,8}>?)?\s*parameter\s+name=[\x27\x22][^\x27\x22]+[\x27\x22][^>]*>",
    re.IGNORECASE,
)
_SALVAGE_PARAM_CLOSER_ONLY_RE = re.compile(
    r"</[｜\| \t]{0,8}(?:DSML[｜\| \t]{0,8}>?)?\s*parameter\s*>",
    re.IGNORECASE,
)
# The closer must appear before the next DSML structural boundary: any
# invoke/calls/tool_call tag (open or close) or the next parameter opener.
# Note a parameter closer itself is NOT a boundary — otherwise a healthy
# parameter carrying one would be misjudged as missing.
_SALVAGE_BOUNDARY_RE = re.compile(
    r"</?[｜\| \t]{0,8}(?:DSML[｜\| \t]{0,8}>?)?\s*(?:invoke|function_call|calls|tool_call)[\s>/]"
    r"|<[｜\| \t]{0,8}(?:DSML[｜\| \t]{0,8}>?)?\s*parameter\s+name=",
    re.IGNORECASE,
)
_SALVAGE_PARAM_CLOSER_SYNTH = "</｜｜DSML｜｜ parameter>"
# [v80] The "dropped slash" mangled parameter closer (fixable by the main
# loop's _SALVAGE_CLOSER_FIX_RE). If it exists within the window, a closer is
# considered present and none is inserted — otherwise we would produce a
# double closer.
_SALVAGE_PARAM_CLOSER_BROKEN_RE = re.compile(
    r"[｜|]{2}\s*DSML\s*[｜|]{2}\s*parameter\s*>"
)


def _insert_missing_param_closers(text, hidden_fn):
    """[v80] First repair pass: when a parameter opener lacks its closer,
    insert a canonical closer before the next structural boundary.

    Deterministic repairs only, never guesses: a closer exists before the
    boundary -> untouched; openers inside fences/inline code/think spans ->
    not touched. Healthy text is returned byte-for-byte. Returns
    (new_text, number inserted)."""
    out_parts = []
    last = 0
    inserted = 0
    for om in _SALVAGE_PARAM_OPEN_RE.finditer(text):
        if hidden_fn(om.start()):
            continue
        bm = _SALVAGE_BOUNDARY_RE.search(text, om.end())
        window_end = bm.start() if bm else len(text)
        if _SALVAGE_PARAM_CLOSER_ONLY_RE.search(text, om.end(), window_end):
            continue
        # [v80] A dropped-slash mangled closer exists within the window -> a
        # closer is present (the main loop will fix it); do not insert one,
        # avoiding a double closer.
        if _SALVAGE_PARAM_CLOSER_BROKEN_RE.search(text[om.end():window_end]):
            continue
        out_parts.append(text[last:window_end])
        out_parts.append(_SALVAGE_PARAM_CLOSER_SYNTH)
        inserted += 1
        last = window_end
    if not inserted:
        return text, 0
    out_parts.append(text[last:])
    return "".join(out_parts), inserted


def _salvage_tool_name_from_hint(args_keys, tools_hint):
    """The unique tool name with required ⊆ argument keys; accepted only when
    unique — multiple candidates or none means no guess."""
    if not tools_hint:
        return None
    cands = []
    for t in tools_hint:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") or t
        name = fn.get("name")
        if not name:
            continue
        req = ((fn.get("parameters") or {}).get("required")) or []
        if req and set(req) <= set(args_keys):
            cands.append(name)
    return cands[0] if len(cands) == 1 else None


def _salvage_tool_markup(text, tools_hint=None):
    """[v78] Repair DSML tool blocks the model wrote incorrectly (dropped-slash
    closers + missing invoke openers).

    [v80] The first pass inserts missing parameter closers (field evidence
    2026-10-31: lost parameter closers made _SALVAGE_PARAM_RE miss everything
    and defeated the whole salvage chain). Blocks that cannot be repaired are
    left as-is (left to the existing malformed handling). With no repair made,
    the return value is byte-identical to the input. tools_hint is this turn's
    OpenAI tool list (defaults to the request_tools ContextVar).

    [v89.5] Adds auto-repair for the sixth lesion: a bare-JSON tool call (the
    wrapper tags lost entirely, the v89.4 production shape). Only when all
    four gates pass (exactly one JSON object / tool name matches this turn's
    tool table / arguments is a dict / all schema-required fields present)
    does this re-wrap the lost <tool_call> tags; parsing, whitelist checks
    and delivery then flow through the existing chain — no new execution
    path is added. If any gate fails, the input is returned byte-identical
    (floor = the v89.4 v62 diagnostic-continue fallback). Text containing
    "DSML" never enters this branch (a bare call with DSML markup embedded
    in an argument value is not re-serialized, avoiding any new input shape
    for the parse_tools sub-regexes); it keeps riding the v89.4 continuation."""
    if not text:
        return text
    if "DSML" not in text:
        _bare = _parse_bare_tool_call(text, tools_hint)
        if _bare is not None and _bare_tool_call_schema_ok(_bare, tools_hint):
            logger.warning("[v89.5] bare-JSON tool call salvaged -> re-wrapped as <tool_call>")
            return "<tool_call>\n" + json.dumps(_bare, ensure_ascii=False) + "\n</tool_call>"
        return text

    def _hidden_in(f_spans, t_spans, pos):
        return any(s <= pos < e for s, e in f_spans) or any(
            s <= pos < e for s, e in t_spans
        )

    # [v80] First pass: missing parameter closers (exempt spans computed on the
    # original-text offsets)
    _f0 = _code_fence_spans(text)
    _f0 += [m.span() for m in re.finditer(r"`[^`\n]*`", text)]
    _t0 = [m.span() for m in _THINK_SPAN_RE.finditer(text)]
    text, closer_fixes = _insert_missing_param_closers(
        text, lambda p: _hidden_in(_f0, _t0, p))
    if not text:
        return text

    fence_spans = _code_fence_spans(text)
    # [v78] DSML inside inline code (single backticks) is a teaching example,
    # not a broken block — same evidence as _has_unswallowed_tool_markup's
    # inline exemption (edac56c5).
    fence_spans += [m.span() for m in re.finditer(r"`[^`\n]*`", text)]
    think_spans = [m.span() for m in _THINK_SPAN_RE.finditer(text)]

    def hidden(pos):
        return _hidden_in(fence_spans, think_spans, pos)

    if tools_hint is None:
        tools_hint = request_tools.get()

    out_parts = []
    last = 0
    repaired = closer_fixes  # [v80] pre-check insertions count toward the total
    for cm in _SALVAGE_CALLS_BLOCK_RE.finditer(text):
        if hidden(cm.start()):
            continue
        block = cm.group(1)
        fixed_block = _SALVAGE_CLOSER_FIX_RE.sub(lambda m: "</" + m.group(0), block)
        if not _SALVAGE_INVOKE_OPEN_RE.search(fixed_block):
            p_matches = list(_SALVAGE_PARAM_RE.finditer(fixed_block))
            if p_matches:
                args = {}
                wrapped_raw = None
                for pm in p_matches:
                    p_name = pm.group(1).strip()
                    p_val = _clean_param_value(pm.group(3))
                    if p_name in _DSML_ARGS_WRAPPERS:
                        wrapped_raw = p_val
                        continue
                    args[p_name] = p_val
                cand = None
                if wrapped_raw is not None:
                    inner = None
                    try:
                        inner = json.loads(_repair_truncated_json(wrapped_raw, strict=True))
                    except Exception:
                        try:
                            inner, _end = json.JSONDecoder().raw_decode(wrapped_raw.lstrip())
                        except Exception:
                            inner = None
                    if not isinstance(inner, dict):
                        inner = _lenient_json_object(wrapped_raw)
                    if isinstance(inner, dict):
                        maybe_name = inner.pop("name", None)
                        args.update(inner)
                        if isinstance(maybe_name, str) and maybe_name.strip():
                            hint_names = {
                                ((t.get("function") or t) or {}).get("name")
                                for t in (tools_hint or [])
                                if isinstance(t, dict)
                            }
                            if not hint_names or maybe_name in hint_names:
                                cand = maybe_name.strip()
                if not cand:
                    cand = _salvage_tool_name_from_hint(args.keys(), tools_hint)
                # [v80] Schema gate: the name matches but required parameters
                # are missing -> the repaired result would fail at dispatch
                # anyway; better to return to the continuation turn and let the
                # model rewrite than to produce a call destined to fail. Only
                # tightens determinism: when required fields are missing or the
                # name is not in the tool list, no opener is synthesized; the
                # behavioral floor is the existing continuation path.
                if cand and tools_hint:
                    _fn = None
                    for _t in tools_hint:
                        if not isinstance(_t, dict):
                            continue
                        _f = _t.get("function") if isinstance(_t.get("function"), dict) else _t
                        if _f.get("name") == cand:
                            _fn = _f
                            break
                    _req = ((_fn.get("parameters") or {}).get("required")) or [] if _fn else []
                    if _req and not set(_req) <= set(args):
                        cand = None
                if cand:
                    fixed_block = (_SALVAGE_INVOKE_SYNTH % cand) + fixed_block
                    repaired += 1
        if fixed_block != block:
            repaired += 1
            out_parts.append(text[last:cm.start(1)])
            out_parts.append(fixed_block)
            last = cm.end(1)
    if not repaired:
        return text
    logger.warning(
        "[v78 salvage] repaired %d malformed tool marker(s) (dropped-slash closer / missing invoke opener / missing parameter closer)",
        repaired,
    )
    out_parts.append(text[last:])
    return "".join(out_parts)


# [v60] Structural signature of tool-call markup (native
# tool_call/function_call family + DSML decorated family). Matches only
# tag/decoration structure, not the word "DSML" mentioned in prose, so
# ordinary discussion is never misjudged as a call. Used to identify turns
# where "the parser swallowed a malformed call whole": the markup is there
# but parse_tools parses out no tool.
_TOOL_MARKUP_HINT_RE = re.compile(
    r"<\s*/?\s*(?:tool_calls?|function_calls?|invoke|parameter)\b"
    r"|[<｜\|][｜\|][｜\| \t]*DSML",
    re.IGNORECASE,
)


def _has_unswallowed_tool_markup(text: str) -> bool:
    """[v60] Whether the prose contains tool-call markup that was NOT parsed
    into a tool.

    Covers two blind spots of v58's _body_has_real_content:
    1. It only recognized DSML markers — the native <tool_call> family was
       missed entirely;
    2. It only caught "prose entirely empty after stripping markers" —
       "real text + a swallowed call" was missed.
       2026-10-24 12:27:42 production instance (session bb59b5d3): the model
       emitted one line of English prose plus a native call missing its name
       field; the parser silently swallowed the call, finish_reason=stop
       ended the turn, downstream assumed a normal completion, the session
       died, and the user had to send "continue" manually.

    Markup inside code fences is examples/docs and does not count. Markup
    quoted inside a think block does not count either ([v66] the model
    discusses what markers look like during reasoning, evidence dee8cb2a).
    Normal turns that parse out tools do not count (tools travel the normal
    delivery path and are executed or schema-rejected downstream — a
    rejection carries the error back to the model, which can self-correct;
    that fault-tolerance path predates v58).

    [v78] Markup inside inline code (single backticks) does not count either.
    2026-10-30 16:51 production instance (session edac56c5): while drafting a
    request, the model wrote a ` <tool_call> ` mention in inline code inside
    teaching prose; the fence exemption did not cover inline code, the whole
    turn was judged malformed, continuation was interrupted twice in a row,
    and the final answer was downgraded and rewritten. Real tool calls are
    never written inside single backticks, so the exemption is risk-free.
    """
    if not text:
        return False
    spans = _code_fence_spans(text)
    spans += [m.span() for m in re.finditer(r"`[^`\n]*`", text)]
    think_spans = [m.span() for m in _THINK_SPAN_RE.finditer(text)]
    hit = False
    for m in _TOOL_MARKUP_HINT_RE.finditer(text):
        if any(s <= m.start() < e for s, e in spans):
            continue
        if any(s <= m.start() < e for s, e in think_spans):
            continue
        hit = True
        break
    if not hit:
        return False
    tools, _ = parse_tools(text)
    return not tools


def _parse_bare_tool_call(text: str, tools_hint=None):
    """[v89.5] Parsing form of the bare-JSON tool-call check: returns the call
    dict on hit, None otherwise.

    Production case 2026-11-05 00:23: the model emitted a tool call as a
    bare JSON object (no <tool_call> wrapper, no code fence, plus a
    client-schema tool_title field inside arguments). Every rung of the
    parse ladder requires a tag or a fence, so the JSON stayed in the body
    as plain text; the model ended its turn believing it had called, and
    the client was left with a visible leak and no executable call - a
    stalled session.

    Strictest possible shape, zero impact on normal paths:
    * after stripping think blocks, the whole body is exactly one JSON
      object (one extra character fails the check);
    * top-level "name" is a non-empty string matching a tool in the
      current request's tool table (tools_hint, defaulting to the same
      extraction as the salvage layer: function.name first, bare name
      fallback); underscore residue names rejected (v66 rule);
    * top-level "arguments" must be a dict (the taught format always has
      it; absence is ambiguous - keep the status quo, no trigger);
    * no tool table on this request -> always None; any JSON output in a
      tool-less conversation is untouched.

    v89.4 uses it as a boolean check (hit -> v62 diagnostic continue);
    from v89.5 on the salvage layer uses it as the repair precondition
    (hit + schema gate -> re-wrap <tool_call> for auto-repair; gate
    failure -> still rides the v89.4 continuation).
    """
    if not text or not text.strip():
        return None
    stripped = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None
    try:
        data = json.loads(stripped)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    name = data.get("name")
    if not isinstance(name, str) or not name.strip() or name.lstrip().startswith("_"):
        return None
    if not isinstance(data.get("arguments"), dict):
        return None
    hint = tools_hint if tools_hint is not None else request_tools.get()
    for t in hint or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") or t
        if isinstance(fn, dict) and fn.get("name") == name:
            return data
    return None


def _bare_tool_call_schema_ok(data, tools_hint=None):
    """[v89.5] Schema gate for bare-JSON auto-repair (same rule as the v80
    salvage layer).

    Every required parameter of the matched tool in this turn's tool table
    must appear in arguments — a "repair" whose name matches but whose
    required params are missing is doomed to fail at dispatch; better fall
    back to the v89.4 continuation and let the model rewrite. Missing/empty
    required lists, or the tool not found in the table (defensive: in
    theory _parse_bare_tool_call already guarantees the match) count as a
    pass — never tighten without cause. Extra keys in arguments (e.g.
    tool_title) are not rejected — same road as a normal call, backed by
    the client's schema validation."""
    name = data.get("name")
    hint = tools_hint if tools_hint is not None else request_tools.get()
    for t in hint or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") or t
        if not isinstance(fn, dict) or fn.get("name") != name:
            continue
        req = ((fn.get("parameters") or {}).get("required")) or []
        args = data.get("arguments") or {}
        return set(req) <= set(args.keys())
    return False


def _is_bare_tool_call_attempt(text: str) -> bool:
    """[v89.4] Whether the ENTIRE body is a tool call that lost its wrapper
    tags.

    From v89.5 on, the core check lives in _parse_bare_tool_call (parsing
    form, reused by the salvage layer); this function stays as a thin shell
    for the v89.4 fallback-continue branch at FINISHED."""
    return _parse_bare_tool_call(text) is not None


def _body_has_real_content(text: str) -> bool:
    """At FINISHED, whether the prose still has readable content after tool-call
    markup is stripped.

    [v58] Malformed-DSML detection: the upstream occasionally emits a tool
    block with only the invoke closer (the opener
    <｜｜DSML｜｜ invoke name=...> is lost); StreamToolParser swallows the
    whole calls>..</calls> block and parse_tools parses out no tool. If
    FINISHED then arrives normally, the turn is judged "completed
    successfully": the client sees only thinking, prose entirely empty
    (finish_reason=stop). "Pure markers that parse into no tool" is therefore
    treated as no prose, triggering the existing auto-continuation mechanism
    so the upstream produces real output.

    [v60] Blind-spot extension: v58's condition covered only the DSML family
    and only "prose entirely empty after stripping". When the model drops the
    native <tool_call>'s name field entirely (real text + a swallowed call),
    neither condition matched and the turn was still judged complete —
    switched to _has_unswallowed_tool_markup to uniformly detect "markup
    present but parses into no tool", regardless of family or surrounding
    prose.

    With readable prose, or a marker block that parses into valid tools (to be
    delivered downstream as tool_calls), returns True — behavior identical to
    before.
    """
    if not text or not text.strip():
        return False
    if _has_unswallowed_tool_markup(text):
        return False
    if "DSML" not in text:
        return True
    cleaned = re.sub(
        r"<[｜\| \t]{0,8}(?:DSML[｜\| \t]{0,8}>?)?\s*(?:tool_calls?|calls)[^>]*>"
        r".*?(?:</[｜\| \t]{0,8}(?:DSML[｜\| \t]{0,8}>?)?\s*(?:tool_calls?|calls)>|$)",
        "", text, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(
        r"<[｜\| \t]{0,8}(?:DSML[｜\| \t]{0,8}>?)?\s*(?:invoke|function_call)[^>]*>"
        r".*?(?:</[｜\| \t]{0,8}(?:DSML[｜\| \t]{0,8}>?)?\s*(?:invoke|function_call)>|$)",
        "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"</?think>", "", cleaned)
    if cleaned.strip():
        return True
    tools, _ = parse_tools(text)
    return bool(tools)


async def _send_message_once(chat_id, auth_token, message, parent_message_id, thinking=False, search=False, file_ids_=None, meta=None):
    # Backup: WAF cookies not required with Android headers. Kept as fallback:
    # cookie = await get_cookies()
    if meta is None:
        meta = {}
    if parent_message_id == 0:
        parent_message_id = None
    file_ids = file_ids_ or []

    _pow = _take_prefetched_pow(auth_token, "/api/v0/chat/completion")
    if _pow is None:
        _pow = await solve_create_pow("/api/v0/chat/completion", auth_token)
    else:
        # consumed one — refill in the background for the next turn
        asyncio.create_task(_prefetch_pow(auth_token, "/api/v0/chat/completion"))
    headers = get_headers(auth_token, _pow)
    # [v89.8] The official client sends x-hif-leim on completion requests (a
    # missing header is direct proof of a non-official client since Nov 2026).
    # On failure degrade to sending without it (= historical behavior);
    # never block, never send a stale token.
    _hif = await get_hif_token()
    if _hif:
        headers["x-hif-leim"] = _hif
    # [v82] DeepSeek protocol change on the evening of 2026-10-31:
    # parent_message_id no longer accepts 0 (biz_code 26 "invalid message
    # id"). Different access points showed inconsistent tolerance for null —
    # omitting the key entirely (first message) is the only way that is safe
    # on every node.
    json_data = {
        "chat_session_id": chat_id,
        "model_type": DEFAULT_MODEL_TYPE,
        "prompt": message,
        "ref_file_ids": file_ids,
        "thinking_enabled": thinking,
        "search_enabled": search,
        "preempt": False,
        "action": None,
    }
    if parent_message_id:
        json_data["parent_message_id"] = parent_message_id

    _resp_ct = ""
    non_sse_tail = []  # [v82 diagnostics] raw tail of non-"data:" lines
    think_open = False
    got_output = False
    got_body = False
    finished = False
    # [v77 PR#70 port] DeepSeek ends replies it truncated with status
    # INCOMPLETE (visible during long-context thinking). With no answer text
    # yet, this is a failed turn, not a completed one: an empty "stop" makes
    # agent clients believe the turn ended normally.
    incomplete = False
    # [v58] Accumulate non-thinking prose so FINISHED can judge whether only
    # unparsed tool markup remains
    body_acc = []
    # [v63] The first-byte budget is now defined before the POST (S8): the
    # budget's design intent was always "from sending the request to seeing
    # the first byte" (original comment: during prefill the upstream "does not
    # emit a single character", not even response headers), but the old
    # implementation only applied it to the first body byte, leaving the
    # header phase unprotected and stuck on the session's sock_read=600s.
    # With the budget moved earlier, the POST and subsequent reads share it:
    # the POST's header wait is bounded by header_timeout and the first body
    # byte by the same deadline — unified semantics. The env key and default
    # are unchanged (DEEPSEEKER_FIRST_BYTE_TIMEOUT=120).
    _first_byte_budget = float(os.getenv("DEEPSEEKER_FIRST_BYTE_TIMEOUT", "120"))
    _first_byte_deadline = time.time() + _first_byte_budget
    resp = await post_with_failover(
        "/api/v0/chat/completion",
        headers=headers, json=json_data,
        # [v63] S8: independent header-phase budget. If the upstream "accepts
        # the request but never returns headers", cancel and failover after at
        # most 120s (old behavior waited 600s doing nothing); on the normal
        # path the POST returns in milliseconds, zero overhead.
        header_timeout=_first_byte_budget,
        # cookies=cookie,  # Backup WAF fallback
    )
    async with resp:
        _resp_ct = resp.headers.get("content-type", "")
        if resp.status != 200:
            error_text = await resp.text()
            logger.warning("DeepSeek completion HTTP %d for chat %s: %s", resp.status, chat_id, error_text[:300])
            raise UpstreamError(resp.status, error_text)

        recent_lines = []
        parsed_events = []
        line_buf = b""

        async def _emit_from_event(data):
            nonlocal think_open, got_output, got_body, incomplete
            if isinstance(data, dict) and data.get("type") == "error":
                _raise_sse_error_event(data, recent_lines)

            if data.get("o") == "BATCH" and isinstance(data.get("v"), list):
                for op in data["v"]:
                    if not isinstance(op, dict):
                        continue
                    if op.get("p") == "quasi_status":
                        # [v77 PR#70 port] Status words never belong to content;
                        # record INCOMPLETE
                        incomplete = incomplete or op.get("v") == "INCOMPLETE"
                        continue
                    async for chunk in _emit_from_event(op):
                        yield chunk
                return

            # [v77 PR#70 port] Other status fields ("response/status",
            # ".../status") carry INCOMPLETE / WIP style status words, never
            # content.
            _p_name, _p_val = str(data.get("p") or ""), data.get("v")
            if (
                _p_name.endswith("status")
                and isinstance(_p_val, str)
                and _p_val.isupper()
                and _p_val != "FINISHED"
            ):
                incomplete = incomplete or _p_val == "INCOMPLETE"
                return

            if data.get("p") == "response/status" and data.get("v") == "FINISHED":
                if think_open:
                    yield "</think>"
                # [v77 PR#70 port adaptation] INCOMPLETE with no answer at all =
                # the upstream truncated the reply. Raise an interruption to
                # recover via the existing auto-continue (better than the
                # upstream's 502: the continuation turn asks to "continue" and
                # the model resumes from where it left off).
                if incomplete and not got_body:
                    logger.warning("[v77] FINISHED with status INCOMPLETE and no body -> continue")
                    raise _SseStreamInterrupted()
                if not got_output:
                    _raise_empty_sse_response(recent_lines, parsed_events, _resp_ct, non_sse_tail)
                # [v57 ported from v39] FINISHED with thinking only, no body: upstream
                # closed the turn without ever producing an answer. Treat as
                # interrupted so the auto-continue path asks for the real reply.
                if not got_body:
                    logger.warning("[auto-continue] FINISHED but no body (thinking only) -> continue")
                    raise _SseStreamInterrupted()
                # [v58] When the prose is nothing but unparsed tool-call markup,
                # treat it as an interruption too: the upstream occasionally
                # emits malformed DSML (a </｜｜DSML｜｜ invoke> closer with no
                # <｜｜DSML｜｜ invoke name=...> opener) — 2026-10-24 16:53:51
                # production instance (session 8e13b53e, WAL rowid 1541, 5
                # frames all the same version, 1208 chars): thinking complete,
                # FINISHED normal, but StreamToolParser swallowed the whole
                # calls block (parse_tools parsed out no tool); the client saw
                # only thinking, prose empty, and the user had to send
                # "continue" manually. Here the existing auto-continuation
                # takes over and asks the upstream again for a real reply
                # (continuation turns still carry thinking).
                body_text = "".join(body_acc)
                # [v78] Missing-opener/dropped-slash salvage: malformed blocks
                # that can be repaired deterministically are fixed directly
                # instead of interrupting into a continuation turn (which
                # would reproduce the shape and cost a full extra upstream
                # round-trip). Unrepairable blocks stay as-is and take the
                # existing malformed path.
                _salvaged_body = _salvage_tool_markup(body_text)
                if _salvaged_body != body_text:
                    body_text = _salvaged_body
                if not _body_has_real_content(body_text):
                    logger.warning("[auto-continue] FINISHED but body is unparsed tool markup only -> continue")
                    # [v62] Fork: when triggered by a malformed tool call,
                    # carry the original call text into the exception so the
                    # continuation turn uses the diagnostic prompt (the model
                    # can only rewrite correctly if it knows what went wrong),
                    # instead of the generic "continue from where we left off"
                    # (where the model can only coin-flip).
                    if _has_unswallowed_tool_markup(body_text):
                        raise _SseStreamInterrupted(
                            "malformed_tool_call", _extract_malformed_call(body_text))
                    raise _SseStreamInterrupted("no_body", "")
                elif _is_bare_tool_call_attempt(body_text):
                    # [v89.4] Bare-JSON tool call (lost its <tool_call>
                    # wrapper): the body "has content" (the JSON itself), so
                    # the markup-only branch above never fires and the turn
                    # used to end normally, leaving the client stalled. Ride
                    # the v62 diagnostic continuation and let the model
                    # rewrite it.
                    logger.warning("[v89.4] FINISHED with bare-JSON tool-call attempt -> malformed continue")
                    raise _SseStreamInterrupted(
                        "malformed_tool_call", _extract_malformed_call(body_text))
                raise _SseStreamFinished()

            if "v" in data and isinstance(data["v"], dict) and "response" in data["v"]:
                fragments = data["v"]["response"].get("fragments")
                if fragments:
                    for fragment in fragments:
                        if fragment.get("type") == "THINK":
                            if not think_open:
                                yield "\n<think>"
                                think_open = True
                            got_output = True
                            yield fragment.get("content", "")
                        else:
                            if think_open:
                                yield "</think>"
                                think_open = False
                            got_output = True
                            got_body = True
                            _last_emit[0] = time.time()
                            body_acc.append(fragment.get("content", "") or "")
                            yield fragment.get("content", "")
                return

            if data.get("p") == "response/fragments" and data.get("o") == "APPEND":
                fragments = data.get("v")
                if isinstance(fragments, list):
                    for fragment in fragments:
                        if fragment.get("type") == "RESPONSE":
                            if think_open:
                                yield "</think>"
                                think_open = False
                            got_output = True
                            got_body = True
                            _last_emit[0] = time.time()
                            body_acc.append(fragment.get("content", "") or "")
                            yield fragment.get("content", "")
                        elif fragment.get("type") == "THINK":
                            if not think_open:
                                yield "\n<think>"
                                think_open = True
                            got_output = True
                            _last_emit[0] = time.time()
                            yield fragment.get("content", "")
                        else:
                            got_output = True
                            got_body = True
                            _last_emit[0] = time.time()
                            body_acc.append(fragment.get("content", "") or "")
                            yield fragment.get("content", "")
                return

            v = data.get("v")
            if isinstance(v, str) and v:
                got_output = True
                got_body = True
                _last_emit[0] = time.time()
                body_acc.append(v)
                yield v

        async def _consume_sse_line(decoded_line: str):
            if not decoded_line.startswith("data: "):
                if decoded_line:
                    non_sse_tail.append(decoded_line[:200])
                    del non_sse_tail[:-5]
                return
            _remember_sse_line(recent_lines, decoded_line)
            try:
                data = json.loads(decoded_line[6:])
            except Exception:
                return
            if isinstance(data, dict):
                # [v84] New protocol: real message numbers broadcast at stream
                # start (request/response)
                if "response_message_id" in data:
                    meta["response_message_id"] = data["response_message_id"]
                parsed_events.append(data)
                if len(parsed_events) > _SSE_RECENT_LINES:
                    del parsed_events[0]
            try:
                async for chunk in _emit_from_event(data):
                    yield chunk
            except _SseStreamFinished:
                raise

        # [v29] Upstream stall guard: an SSE stream that neither emits bytes
        # nor closes (thinking finished, answer pending, upstream hung) used
        # to block until the CLIENT timed out first — the user saw "thinking
        # done, no reply", and send_message's auto-continue never fired
        # because the stream never *ended*. Treat N seconds of total silence
        # as an interruption so the auto-continue path takes over.
        _aiter = resp.content.iter_any()
        _idle_timeout = float(os.getenv("DEEPSEEKER_STREAM_IDLE_TIMEOUT", "25"))
        _last_emit = [time.time()]
        # [v57 ported from v45] First-byte grace: during prefill of a long
        # context the upstream emits nothing at all (measured: 120k input
        # 18.7s, 300k input 71s), while the old code applied the 25s idle
        # verdict from the moment the request was sent — long inputs were
        # misjudged as hung mid-prefill -> a continuation was sent ->
        # parent+2 -> prefill all over again, which was slower and could
        # produce "thinking done, no output". An independent budget is used
        # before the first byte; only afterwards does the idle verdict kick
        # in. (_first_byte_budget/_first_byte_deadline were already moved
        # before the POST in [v63], sharing one budget with the header phase.)
        _got_first_byte = False
        while True:
            # [v30] Silence is measured on CONTENT EMISSION, not raw bytes:
            # the upstream may dribble keep-alive bytes/blank lines forever
            # while hanging, which would defeat a byte-level guard. remaining
            # budget shrinks until _emit_from_event actually yields content.
            if _got_first_byte:
                remaining = _idle_timeout - (time.time() - _last_emit[0])
            else:
                remaining = _first_byte_deadline - time.time()
            if remaining <= 0:
                # [v57 ported from v49] Silence is always treated as an
                # interruption, not branched on got_body: v45 once changed
                # "silence after body output" to end normally without
                # continuing, and in a real incident the reply broke off
                # mid-code-block — after that ending, the client received
                # truncated output and nothing would ever continue it.
                # FINISHED is the only proof of "finished speaking properly"
                # (same conclusion as v43); without it, interrupt to trigger
                # continuation and let the continuation turn complete the
                # reply.
                logger.warning("[stream-stall] upstream stream idle for %.0fs with no new content (got_body=%s); treating as interruption to trigger continuation", _idle_timeout, got_body)
                if think_open:
                    yield "</think>"
                logger.warning("[stream-stall] upstream stream produced no prose for %.0fs (suspected hang); interrupting proactively to trigger auto-continuation", _idle_timeout)
                raise _SseStreamInterrupted()
            try:
                chunk = await asyncio.wait_for(_aiter.__anext__(), timeout=remaining)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                logger.warning("[stream-stall] upstream stream idle for %.0fs with no new content (got_body=%s); treating as interruption to trigger continuation", _idle_timeout, got_body)
                if think_open:
                    yield "</think>"
                logger.warning("[stream-stall] upstream stream produced no prose for %.0fs (suspected hang); interrupting proactively to trigger auto-continuation", _idle_timeout)
                raise _SseStreamInterrupted()
            if not chunk:
                continue
            if not _got_first_byte:
                # Only reset the idle timer once the first byte arrives:
                # otherwise the first-byte latency (long-context prefill can
                # reach 71s) would burn the idle budget outright and the
                # second loop would already misjudge a hang.
                _got_first_byte = True
                _last_emit[0] = time.time()
            line_buf += chunk
            while b"\n" in line_buf:
                raw_line, line_buf = line_buf.split(b"\n", 1)
                decoded_line = raw_line.decode("utf-8", errors="replace").strip()
                if not decoded_line:
                    continue
                try:
                    async for out in _consume_sse_line(decoded_line):
                        yield out
                except _SseStreamFinished:
                    finished = True
                    return

        if line_buf.strip():
            decoded_line = line_buf.decode("utf-8", errors="replace").strip()
            if decoded_line:
                try:
                    async for out in _consume_sse_line(decoded_line):
                        yield out
                except _SseStreamFinished:
                    finished = True
                    return

        if think_open:
            yield "</think>"
        # [v77 PR#70 port adaptation] Check INCOMPLETE at end of stream as well
        if incomplete and not got_body:
            logger.warning("[v77] stream ended with status INCOMPLETE and no body -> continue")
            raise _SseStreamInterrupted()
        if not got_output:
            _raise_empty_sse_response(recent_lines, parsed_events, _resp_ct, non_sse_tail)
        if not finished:
            # [v57 ported from v43] FINISHED is the only proof of "finished
            # speaking properly". Not receiving it means the stream was cut by
            # the upstream/network; even with prose already emitted it may
            # have broken mid-sentence — treat as an interruption to trigger
            # continuation. v34's got_body guard was right for "stream broke
            # after speaking" but silently truncated output for "cut off
            # mid-sentence" (2026-10-23 field: reply broke inside a code
            # block).
            logger.warning("[auto-continue] no FINISHED received (got_body=%s); treating as interruption to trigger continuation", got_body)
            raise _SseStreamInterrupted()


_CONTINUE_PROMPT = (
    "Continue writing from where the text above left off. Do not repeat "
    "anything already written; no greetings, explanations, or restatements. "
    "If the text above is already fully complete, output only 【END】 and "
    "nothing else."
)

# [v64] Continuation prompt for the zero-visible-content case. Division of
# labor with _CONTINUE_PROMPT: the old prompt's 【END】 escape hatch serves
# the "real prose was written, just cut off" case — the model says 【END】 to
# mean it is done, and the filter cuts it to avoid duplicate appends. But when
# the turn has not yet emitted ANY visible content (the upstream ends the turn
# right after thinking), the model facing an empty assistant turn tends to
# emit only 【END】, which the filter cuts entirely -> the client receives only
# thinking, finish_reason=stop, and the turn dies silently (2026-10-25
# 01:04:11 production instance session d4a7e123: 517 chars of thinking then
# zero output; the previous 12 turns were all tool-call turns).
# This case explicitly tells the model "the client received nothing" and
# disables the termination marker, demanding direct output.
_CONTINUE_PROMPT_EMPTY = (
    "[SYSTEM NOTICE] Your previous reply contained only thinking, with no "
    "prose or tool calls, and the client received nothing. Please output the "
    "complete reply content directly, or re-issue the tool call. "
    "Outputting 【END】 or any other termination marker is forbidden; do not "
    "restate the thinking content; no greetings, explanations, or "
    "self-evaluation."
)

# [v62] Continuation prompt for the malformed-tool-call case. Division of
# labor with _CONTINUE_PROMPT: the generic prompt serves "stream cut / hung /
# thinking with no output" (the model lacks no information, it just needs to
# be nudged to continue); this prompt serves "a tool call with a broken format
# silently swallowed" (2026-10-24 12:27 production instance session bb59b5d3:
# the model emitted one line of prose plus a native call missing its name; the
# parser swallowed the block, parse_tools parsed nothing, finish_reason=stop,
# session died).
# [v78] Revision: the malformed call's original text is no longer attached to
# the prompt — in long sessions the raw text becomes a bad example and the
# model copies its own fresh mistake (production evidence 2026-10-29 23:15
# session cd859ddc, three consecutive turns reproducing the same
# "missing invoke opener" shape). Instead it only describes the common causes
# and the correct format; the raw text is still written to server logs by the
# call site, so operator visibility is unaffected.
_MALFORMED_TOOL_PROMPT = (
    "[SYSTEM NOTICE] The tool call you issued in your previous turn was "
    "malformed and could not be parsed by the server, so it was rejected and "
    "not executed. "
    "Location of the error: the tool call in your previous message, i.e. the "
    "last block of output immediately before this notice.\n"
    "Common causes: the tool-call block is missing its "
    "<invoke name=\"tool_name\"> opener line, or the closing tag lost the "
    "slash in </, or the name / arguments fields are missing.\n"
    "The correct format for a tool call is (both the name and the arguments "
    "fields are indispensable; the example is for format reference only — "
    "never copy its content, and the tool name must be a real, existing "
    "tool):\n"
    "```\n<tool_call>{\"name\": \"tool_name\", \"arguments\": {...}}</tool_call>\n```\n\n"
    "Please re-issue the tool call in the correct format; if this task no "
    "longer needs a tool, give the final answer directly — do not restate "
    "the text above and do not explain this error."
)

# [v78-c] Escalating disclosure: only the final continuation — the one where
# another mistake means no next chance — attaches the original text, so the
# model can compare precisely to locate the error. No earlier attempt ever
# attaches it (to prevent bad-example imitation; production evidence
# 2026-10-29 cd859ddc, three consecutive repeats). The risk of attaching it on
# the final try is contained: if this attempt also fails, the forced rollover
# that immediately follows discards the pollution wholesale with the old
# session; if it succeeds, the error has been absorbed by the delivery chain
# and residual examples are still covered by the rollover threshold and the
# salvage layer.
_MALFORMED_TOOL_FINAL_SUFFIX = (
    "\n\n[FINAL RETRY] Below is the original text of the call you mangled in "
    "your previous message, provided only so you can compare and locate the "
    "error — never imitate its broken format:\n"
    "<rejected_call>\n{raw}\n</rejected_call>"
)

# [v78] Malformed-continuation counter (accounted per upstream chat):
# +1 each time send_message continues due to malformed_tool_call; reset to
# zero on a clean finish (FINISHED with visible prose). app.handle_chat forces
# a rollover (to a brand-new upstream session) when the count for the same
# session reaches the threshold — the model's self-produced bad examples
# accumulated in the upstream session's memory are beyond our reach; the only
# real cure is moving the session, rebuilding from a carried-over summary so
# the bad examples no longer flow back.
_MALFORMED_CONTINUE_COUNT = {}


def malformed_continue_count(chat_id):
    return _MALFORMED_CONTINUE_COUNT.get(chat_id, 0)


def reset_malformed_continue_count(chat_id):
    _MALFORMED_CONTINUE_COUNT.pop(chat_id, None)


# [v79] Empty-stream fallback counter: +1 when an upstream stream "ends
# normally with zero visible output" (a dangling turn); reset to zero by a
# normal turn with visible output. A dangling turn leaves a "user message
# sent, model replied with nothing" breakpoint in the upstream session's
# memory, and later context breaks (production evidence 2026-10-30 session
# 67010e7d amnesia event: the 22:26:54 turn had zero output -> the client got
# an empty reply -> the model forgot "the four things").
# Same mechanism as _MALFORMED_CONTINUE_COUNT: app.handle_chat forces a
# rollover (to a brand-new upstream session) when the count for the same
# session reaches the threshold, rebuilding from a summary so the bad state
# does not flow back.
_EMPTY_STREAM_COUNT = {}


def empty_stream_count(chat_id):
    return _EMPTY_STREAM_COUNT.get(chat_id, 0)


def bump_empty_stream_count(chat_id):
    _EMPTY_STREAM_COUNT[chat_id] = _EMPTY_STREAM_COUNT.get(chat_id, 0) + 1
    return _EMPTY_STREAM_COUNT[chat_id]


def reset_empty_stream_count(chat_id):
    _EMPTY_STREAM_COUNT.pop(chat_id, None)


def _extract_malformed_call(text: str, limit: int = 400) -> str:
    """[v62] Extract the original text of the malformed tool call from the
    prose (truncated to limit), for the diagnostic prompt to attach.

    Strategy: find the first tool marker (_TOOL_MARKUP_HINT_RE, skipping
    examples inside code fences), then within 600 chars from that point find
    the last > as the end of the call block (covers native multi-line JSON as
    well as DSML multi-line parameter blocks); if none found, fall back to
    300 chars after the marker.
    """
    if not text:
        return ""
    spans = _code_fence_spans(text)
    for m in _TOOL_MARKUP_HINT_RE.finditer(text):
        if any(s <= m.start() < e for s, e in spans):
            continue
        tail = text[m.start():m.start() + 600]
        close = tail.rfind(">")
        raw = tail[:close + 1] if close >= 0 else tail[:300]
        return raw[:limit]
    return text[:limit]

_END_MARKERS = ("【END】", "[END]", "<END>")


class _ContinuationFilter:
    """Output filter for continuation turns.

    The first turn passes through verbatim, no rewriting. Continuation turns
    buffer output until flush and do three things:
    1. Cut everything after a termination marker (the model uses 【END】 to
       mean "the text above is complete")
    2. Strip leading blank lines/whitespace
    3. Tail-deduplicate against already-sent content (the model often repeats
       its final sentence verbatim)

    Dedup requires an overlap of >= 8 characters before cutting, so single
    character coincidences cannot cause false deletions.
    """

    def __init__(self, passthrough, tail=""):
        self.passthrough = passthrough
        self.tail = tail or ""
        self.buf = []
        self.done = False

    def feed(self, chunk):
        if not chunk:
            return []
        if self.passthrough:
            return [chunk]
        self.buf.append(chunk)
        return []

    def flush(self):
        if self.passthrough or self.done:
            return []
        self.done = True
        out = "".join(self.buf)
        self.buf = []
        if not out:
            return []
        cut = len(out)
        hit = False
        for m in _END_MARKERS:
            i = out.find(m)
            if 0 <= i < cut:
                cut = i
                hit = True
        out = out[:cut]
        if hit:
            out = out.rstrip()
        out = out.lstrip("\r\n \t")
        tail = self.tail[-400:]
        if tail and out:
            k = min(len(tail), len(out), 400)
            while k >= 8:
                if out.startswith(tail[-k:]):
                    out = out[k:]
                    break
                k -= 1
        return [out] if out else []


class _VisibleTracker:
    """[v64] Tracks across chunks whether the client ever received "visible
    prose" (non-thinking content).

    What is fed in must be content that actually reached the client after
    _ContinuationFilter (a 【END】 cut from a continuation turn never enters
    here — the client never saw it).

    Semantics align with app._StreamTagSplitter's think_done latch: after the
    first thinking block closes, all subsequent content counts as visible
    (the splitter no longer routes it as reasoning either); tool-call markup
    counts as visible — it is real output to the user/downstream.

    Contract: the caller guarantees "<think>"/"</think>" tags arrive
    atomically (_emit_from_event yields them as whole strings; neither
    _ContinuationFilter's passthrough nor flush splits chunks), so no
    splitter-style tag-prefix hold-back logic is needed.
    """

    __slots__ = ("in_think", "done", "seen")

    def __init__(self):
        self.in_think = False
        self.done = False
        self.seen = False

    def feed(self, s):
        if self.seen or not s:
            return
        i = 0
        n = len(s)
        while i < n:
            if self.done or not self.in_think:
                j = -1 if self.done else s.find("<think>", i)
                if j == -1:
                    if s[i:].strip():
                        self.seen = True
                    return
                if s[i:j].strip():
                    self.seen = True
                    return
                self.in_think = True
                i = j + 7
            else:
                j = s.find("</think>", i)
                if j < 0:
                    return
                self.in_think = False
                self.done = True
                i = j + 8


# [v77 PR#71 port] The DeepSeek web backend allows only one reply being
# generated per account at a time; a second concurrent send raises
# "A message is being generated" / "Messages too frequent". Sends are now
# queued per account. All generation goes through send_message, so every
# caller is covered.
_send_locks = {}


def token_busy(auth_token):
    """[v77 PR#71 port] True while the account is generating (its send lock is
    held)."""
    lock = _send_locks.get(auth_token)
    return bool(lock and lock.locked())


async def send_message(chat_id, auth_token, message, parent_message_id, thinking=False, search=False, file_ids_=None, max_continue=2):
    # [v77 PR#71 port] The upstream stream is drained by an independent task:
    # even if the consumer abandons this generator midway (client vanished
    # without aclose), the lock is released when DeepSeek finishes — holding
    # the lock across yields would leak it and deadlock every later request
    # for that account.
    lock = _send_locks.setdefault(auth_token, asyncio.Lock())
    queue = asyncio.Queue()
    done = object()

    async def _pump():
        try:
            async with lock:
                gen = _send_message_locked(chat_id, auth_token, message, parent_message_id, thinking=thinking, search=search, file_ids_=file_ids_, max_continue=max_continue)
                try:
                    async for chunk in gen:
                        queue.put_nowait(chunk)
                finally:
                    await gen.aclose()
        except Exception as e:
            queue.put_nowait(e)
        finally:
            queue.put_nowait(done)

    task = asyncio.create_task(_pump())
    try:
        while True:
            item = await queue.get()
            if item is done:
                break
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        # still cancel the upstream when the consumer closes us (disconnect,
        # abort).
        task.cancel()


async def _send_message_locked(chat_id, auth_token, message, parent_message_id, thinking=False, search=False, file_ids_=None, max_continue=2):
    """Public entry point with automatic continuation.

    When the upstream stream ends abnormally before FINISHED (stall/
    disconnect/early close), it automatically continues within the same
    session with a "continue" message — parent advances strictly by the real
    numbers broadcast in the upstream ready event ([v85] full native
    adaptation: no broadcast means no movement, never a guess), and the
    continuation content is spliced seamlessly into the same client stream.
    At most max_continue automatic continuations.

    [v64] Two hardenings (root cause: the 2026-10-25 01:04 production
    "thinking done, no output"):
    1. Prompt branching: when this turn has not yet emitted any visible
       prose, continuation does not use _CONTINUE_PROMPT with its 【END】
       escape hatch; it switches to _CONTINUE_PROMPT_EMPTY, which explicitly
       forbids termination markers — otherwise the model facing an empty
       assistant turn emits only 【END】, the filter cuts it all, and the
       client silently receives nothing.
    2. Finish safety net: if visible prose is still empty at the end of the
       whole turn (continuation turns only emitted termination markers), retry
       with the other prompt while budget remains; when the budget is gone,
       emit a visible notice instead of returning silently.
    """
    parent = parent_message_id
    msg = message
    files = file_ids_
    _LAST_REAL_PARENT.pop(chat_id, None)  # [v84] this turn has produced no new number yet
    emitted = ""
    # [v64] Client-visible prose tracking (accumulated across attempts); what
    # is fed in is always filtered content that actually reached the client.
    visible = _VisibleTracker()
    for attempt in range(max_continue + 1):
        flt = _ContinuationFilter(passthrough=(attempt == 0), tail=emitted)
        # [v57 ported from v50] Continuation turns pass thinking through
        # verbatim instead of forcing it off: v39/v40 experimented with
        # _thinking = thinking if attempt == 0 else False, and in practice the
        # upstream emitted the second turn's new thinking as prose (the B3
        # thinking leak); the real final version reverted to this line.
        _thinking = thinking
        clean_end = False
        interrupt_reason = "interrupted"
        interrupt_detail = ""
        try:
            _meta = {}
            async for chunk in _send_message_once(chat_id, auth_token, msg, parent, _thinking, search, files, meta=_meta):
                for out in flt.feed(chunk):
                    emitted = (emitted + out)[-400:]
                    visible.feed(out)
                    yield out
            for out in flt.flush():
                emitted = (emitted + out)[-400:]
                visible.feed(out)
                yield out
            clean_end = True  # completed normally (upstream sent FINISHED)
        except _SseStreamFinished:
            for out in flt.flush():
                emitted = (emitted + out)[-400:]
                visible.feed(out)
                yield out
            clean_end = True
        except _SseStreamInterrupted as e:
            for out in flt.flush():
                emitted = (emitted + out)[-400:]
                visible.feed(out)
                yield out
            # `except as` variables are unbound at block exit (PEP 3110);
            # extract before use
            interrupt_reason = getattr(e, "reason", "interrupted")
            interrupt_detail = getattr(e, "detail", "")
            pass
        except UpstreamError as e:
            # [v81] Empty-stream retry: the upstream occasionally returns a
            # completely empty SSE (zero fragments). Evidence 2026-10-31
            # 21:02 (key:2 client): on a tool-result round the upstream sent
            # an empty stream, the 502 pierced straight through to the
            # client, and the user saw "the model stopped right after calling
            # the tool".
            # An empty stream = this turn produced nothing; handle it as
            # "interruption with no visible prose" and continue
            # (visible.seen=False automatically hits v64's END-forbidden
            # prompt branch). The context-overflow variant (prompt may
            # exceed) is not retried — asking again would just overflow
            # again; other error codes (401/403/429/400, upstream error
            # events) have clear semantics and are raised as before.
            if getattr(e, "status", None) == 502 and "no assistant fragments" in str(e):
                # [v87] Pre-condition for the auto-continue fallback: only fire
                # when the message was actually accepted upstream (this turn
                # got a broadcast id, or the chat had a valid parent to begin
                # with). No broadcast = upstream never received the message;
                # sending the orphan "system notice" text then would create a
                # one-liner chat and poison the session map (root cause of the
                # 2026-11-04 fleet-wide memory loss). Re-raise instead so the
                # normal error-retry path handles it.
                if not (_meta.get("response_message_id") or parent or _LAST_REAL_PARENT.get(chat_id)):
                    logger.warning("[v87] message not accepted upstream (no broadcast, no parent); skipping auto-continue, re-raising")
                    raise
                for out in flt.flush():
                    emitted = (emitted + out)[-400:]
                    visible.feed(out)
                    yield out
                interrupt_reason = "empty_stream"
                logger.warning("[v81] upstream empty stream (502, zero output); treating as interruption to auto-continue")
            else:
                raise
        if clean_end:
            # [v84] The upstream broadcast a real number -> wire up directly
            # (including any continuation this turn may have produced; meta
            # keeps only the last broadcast, i.e. this exchange's final
            # response id). Also written to the global table for the app
            # layer's save_session. When no real number is available, keep
            # v83 semantics: empty streams do not advance, real interruptions
            # use the +2 guess.
            if _meta.get("response_message_id"):
                parent = _meta["response_message_id"]
                _LAST_REAL_PARENT[chat_id] = parent
            asyncio.create_task(_prefetch_pow(auth_token, "/api/v0/chat/completion"))
            if visible.seen:
                # [v78] Clean finish (FINISHED with visible prose): reset the
                # malformed-continuation counter; the session is considered
                # healthy again and no rollover is triggered.
                reset_malformed_continue_count(chat_id)
                return  # completed normally (upstream sent FINISHED)
            # [v64] Finish safety net: the whole turn emitted no visible prose —
            # continuation turns only emitted 【END】 which the filter cut
            # entirely (or repeatedly only thinking). Cannot return silently:
            # the client would receive only thinking + finish_reason=stop and
            # the turn would die silently (the 01:04 failure shape). Retry
            # with the other prompt while budget remains; once the budget is
            # gone, emit a visible notice.
            if attempt >= max_continue:
                logger.warning("[auto-continue] several turns in a row with no visible prose; ending with a visible notice")
                yield chr(10) + chr(10) + (
                    "(The upstream returned only thinking for several turns in a row without producing any prose; automatic retries have stopped. "
                    "Please resend your message or try again later.)")
                return
            logger.warning("[auto-continue] still no visible prose after continuation (suspected termination-marker-only output); "
                           "retrying with the END-forbidden prompt %d/%d", attempt + 1, max_continue)
            interrupt_reason = "empty_continuation"
        if attempt >= max_continue:
            logger.warning("[auto-continue] automatic continuation limit reached (%d); finishing as-is", max_continue)
            yield chr(10) + chr(10) + "(The upstream kept interrupting after thinking; %d retries still produced no prose)" % max_continue
            return
        logger.warning("[auto-continue] upstream stream did not end normally (reason=%s); automatic continuation %d/%d",
                       interrupt_reason, attempt + 1, max_continue)
        # [v85] Full native adaptation: parent trusts only real numbers
        # broadcast in ready. No broadcast (PoW rejection / empty stream /
        # instant disconnect) = the upstream stored nothing; parent stays put
        # and the continuation turn re-sends natively with the same parent;
        # the +2 guess semantics are retired (evidence v83 23:57: guessing
        # pushed the chain onto certainly-invalid numbers, causing cascading
        # biz_code 26 failures).
        if _meta.get("response_message_id"):
            parent = _meta["response_message_id"]
            _LAST_REAL_PARENT[chat_id] = parent
        # [v62] The malformed-tool-call case switches to the diagnostic
        # prompt: tell the model what went wrong, attach the original call,
        # give the correct format, and offer two ways out (rewrite the call or
        # answer directly) — only knowing the cause lets the model rewrite a
        # correct call instead of coin-flipping against a generic "keep
        # writing". Other cases are unchanged.
        # [v64] Additional branch: when this turn has no visible prose yet
        # (visible.seen=False), the old prompt's 【END】 escape hatch would be
        # treated by the model as an exit for "nothing to continue" — emitting
        # only 【END】 which gets cut -> silently dead turn. Switch to
        # _CONTINUE_PROMPT_EMPTY, which forbids termination markers. With
        # visible prose already present, behavior is identical to v62.
        if interrupt_reason == "malformed_tool_call":
            # [v78] Counting + no longer re-attaching the raw text (it goes to
            # logs only; continuously failing sessions are force-rolled-over
            # by the app layer per _MALFORMED_CONTINUE_COUNT).
            _MALFORMED_CONTINUE_COUNT[chat_id] = _MALFORMED_CONTINUE_COUNT.get(chat_id, 0) + 1
            logger.warning(
                "[v78] malformed tool call raw text (log only, chat=%s, %d total): %.400s",
                str(chat_id)[:8], _MALFORMED_CONTINUE_COUNT[chat_id], interrupt_detail,
            )
            msg = _MALFORMED_TOOL_PROMPT
            if attempt >= max_continue - 1:
                # [v78-c] Escalating disclosure: this is the final continuation
                # (one more failure means the limit is hit and a rollover
                # triggers); attach the raw text to help the model locate the
                # error precisely.
                msg += _MALFORMED_TOOL_FINAL_SUFFIX.format(raw=interrupt_detail[:400])
        elif interrupt_reason == "empty_continuation" or not visible.seen:
            msg = _CONTINUE_PROMPT_EMPTY
        else:
            msg = _CONTINUE_PROMPT
        files = []
        # [v61] Boundary sentinel: makes app.stream_response reset the
        # splitter's think_done latch so the continuation turn's leading
        # "\n<think>" is recognized as reasoning again instead of leaking into
        # content (see the _RetryBoundary docstring). An empty string,
        # contributing nothing to the content.
        yield _RetryBoundary()


async def upload_file(file_bytes, file_name, file_content_type, auth_token):
    # Backup: WAF cookies not required with Android headers. Kept as fallback:
    # cookie = await get_cookies()
    session = await get_session()
    url = "https://chat.deepseek.com/api/v0/file/upload_file"
    file_size = len(file_bytes)
    pow_response = await solve_create_pow("/api/v0/file/upload_file", auth_token)
    boundary = b"----WebKitFormBoundaryTB0pXOQR2RL219Hu"
    safe_name = re.sub(r"[^ -~]", "_", file_name).replace('"', "_") or "file.bin"
    body_parts = [
        b"--" + boundary + b"\r\n",
        f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'.encode("utf-8"),
        f"Content-Type: {file_content_type}\r\n\r\n".encode("utf-8"),
        file_bytes,
        b"\r\n--" + boundary + b"--\r\n",
    ]
    reconstructed_body = b"".join(body_parts)
    headers = get_headers(auth_token, pow_response)
    headers.update({
        "content-type": f"multipart/form-data; boundary={boundary.decode('utf-8')}",
        "x-file-size": str(file_size),
    })
    response = await post_with_failover(
        "/api/v0/file/upload_file",
        data=reconstructed_body, headers=headers,
        # cookies=cookie,  # Backup WAF fallback
        timeout=aiohttp.ClientTimeout(total=120),
    )
    async with response:
        resp_json = await response.json()
    js_data = _biz_data(resp_json, "file/upload_file")
    file_id = js_data["id"]
    yield ("uploaded", file_id)
    status = js_data["status"]
    headers = get_headers(auth_token)
    deadline = time.time() + 300
    while status in ["PENDING", "PARSING"] and time.time() < deadline:
        yield ("uploaded", file_id)
        await asyncio.sleep(0.3)
        async with session.get(
            "https://chat.deepseek.com/api/v0/file/fetch_files?file_ids=" + file_id,
            headers=headers,
            # cookies=cookie,  # Backup WAF fallback
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            js_data = _biz_data(await resp.json(), "file/fetch_files")["files"][0]
        status = js_data["status"]
    if status == "SUCCESS":
        tp_data = datetime.fromtimestamp(js_data["updated_at"], timezone.utc)
        yield ("success", {
            "file_id": file_id,
            "openai_timestamp": int(js_data["updated_at"]),
            "size": js_data["file_size"],
            "anthropic_timestamp": tp_data.strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
    else:
        yield ("error", file_id)


async def get_file_content(auth_token, file_id):
    # Backup: WAF cookies not required with Android headers. Kept as fallback:
    # cookie = await get_cookies()
    session = await get_session()
    headers = get_headers(auth_token)
    async with session.get(
        "https://chat.deepseek.com/api/v0/file/fetch_files?file_ids=" + file_id,
        headers=headers,
        # cookies=cookie,  # Backup WAF fallback
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        resp_json = await resp.json()
    js_data = _biz_data(resp_json, "file/fetch_files")["files"][0]
    yield mimetypes.guess_type(js_data["file_name"])[0]
    deadline = time.time() + 60
    while js_data.get("status") in ("PENDING", "PARSING") and time.time() < deadline and not js_data.get("signed_path"):
        await asyncio.sleep(0.5)
        async with session.get(
            "https://chat.deepseek.com/api/v0/file/fetch_files?file_ids=" + file_id,
            headers=headers,
            # cookies=cookie,  # Backup WAF fallback
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            js_data = _biz_data(await resp.json(), "file/fetch_files")["files"][0]
    if not js_data.get("signed_path"):
        return
    file_path = "https://files.deepseeksvc.com/api" + js_data["signed_path"] + "&ty=r"
    async with session.get(file_path) as data:
        async for chunk in data.content.iter_chunked(8192):
            if chunk:
                yield chunk
