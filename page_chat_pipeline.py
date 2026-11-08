"""Server-side pipeline for page mode: build the URL, register the intent, read the reply back. The send itself happens in the official page."""

import asyncio
import logging
import os
import re
import threading
import time
import uuid
from urllib.parse import urlencode

import aiohttp

import functions

logger = logging.getLogger("page_chat_pipeline")

PAGE_MODE_ENV = "DEEPSEEKER_PAGE_MODE"
PAGE_TIMEOUT_ENV = "DEEPSEEKER_PAGE_TIMEOUT"
POLL_INTERVAL_S = 2.0
AUTOSEND_MAX_LEN = 800
PENDING_MAX = 64

RESPONSE_TYPES = {"RESPONSE", "TEMPLATE_RESPONSE"}
THINK_TYPES = {"THINK"}
_FINISHED = {"FINISHED", "FINISH", "DONE", "COMPLETED", "SUCCESS", "OK"}
_WIP = {"PENDING", "WIP", "GENERATING", "IN_PROGRESS", "RUNNING", "CREATED", ""}


def page_mode_enabled():
    """Off unless DEEPSEEKER_PAGE_MODE is set."""
    return os.getenv(PAGE_MODE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def page_timeout():
    try:
        v = float(os.getenv(PAGE_TIMEOUT_ENV, "300"))
        return v if v > 0 else 300.0
    except (TypeError, ValueError):
        return 300.0


class PageChatError(Exception):
    """Error carrying an HTTP status for the caller."""

    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = int(status)
        self.detail = detail


def build_autosend_url(prompt, search=False, thinking=False, base=None):
    """Build the autosend URL. Pure string construction; overlong prompts are rejected, never truncated."""
    q = (prompt or "").strip()
    if not q:
        raise PageChatError(400, "page mode: empty prompt")
    if len(q) > AUTOSEND_MAX_LEN:
        raise PageChatError(
            400, "page mode: prompt exceeds %d-char autosend limit" % AUTOSEND_MAX_LEN)
    base = (base or functions.UPSTREAM_BASES[0]).rstrip("/")
    params = {"q": q, "autosend": "1"}
    if search:
        params["search"] = "1"
    if thinking:
        params["thinking"] = "1"
    return base + "/?" + urlencode(params)


_pending = []
_pending_lock = threading.Lock()


def register_intent(url, prompt):
    intent = {
        "id": uuid.uuid4().hex[:12],
        "url": url,
        "prompt_preview": re.sub(r"\s+", " ", (prompt or ""))[:60],
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with _pending_lock:
        _pending.append(intent)
        del _pending[:-PENDING_MAX]
    return intent


def take_pending():
    """Hand off all pending URLs and clear the queue."""
    with _pending_lock:
        out = _pending[:]
        _pending.clear()
    return out


def pending_count():
    with _pending_lock:
        return len(_pending)


async def _get_json(url, token, timeout_s=15.0):
    """One GET, returns (status, body); network errors are returned, not raised."""
    session = await functions.get_session()
    try:
        async with session.get(
            url,
            headers=functions.get_headers(token),
            timeout=aiohttp.ClientTimeout(total=timeout_s),
        ) as resp:
            body = await resp.json(content_type=None)
            return resp.status, body
    except Exception as exc:
        return None, {"_error": str(exc)[:200]}


async def _assert_alive(token):
    """Fail early when the read token is invalid or muted."""
    r = await functions.probe_token_mute(token)
    v = r.get("verdict")
    if v == "invalid":
        raise PageChatError(503, "page mode: read token invalid upstream (%s)" % (r.get("detail") or ""))
    if v == "muted":
        raise PageChatError(429, "page mode: account muted until %s" % (r.get("mute_until") or "?"))


def _g(d, *names):
    """Tolerant key lookup across several candidate names."""
    for n in names:
        v = d.get(n)
        if v is not None:
            return v
    return None


def _norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def _find_dict_lists(obj, key_pred):
    """Find lists whose items are dicts carrying one of the given keys."""
    found = []

    def walk(x):
        if isinstance(x, list):
            dicts = [i for i in x if isinstance(i, dict)]
            if dicts and any(any(k in d for k in key_pred) for d in dicts):
                found.append(dicts)
            for i in x:
                walk(i)
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)

    walk(obj)
    return found


def _ts_key(d):
    for v in (_g(d, "updated_at", "update_time", "last_message_at", "created_at", "inserted_at"),):
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _frag_text(msg, types):
    out = []
    for f in (_g(msg, "fragments") or []):
        if isinstance(f, dict) and f.get("type") in types and isinstance(f.get("content"), str):
            out.append(f["content"])
    return "".join(out)


def _locate_user_message(msgs, marker, t0):
    """Find this turn's user message by prompt tail, falling back to timestamp."""
    best = (-1, None)
    for i, m in enumerate(msgs):
        role = str(_g(m, "role") or "").upper()
        if role != "USER":
            continue
        text = _norm(_frag_text(m, {"REQUEST"}) or _g(m, "content") or "")
        if marker and marker in text:
            best = (i, m)
    if best[1] is not None:
        return best
    for i, m in enumerate(msgs):
        role = str(_g(m, "role") or "").upper()
        if role != "USER":
            continue
        ts = _ts_key(m)
        if ts and ts >= t0 - 10:
            best = (i, m)
    return best


def _locate_reply(msgs, user_idx):
    """Find the reply via parent id, falling back to the next assistant message."""
    if user_idx < 0 or user_idx >= len(msgs):
        return None
    um = msgs[user_idx]
    uid = _g(um, "message_id", "messageId", "id")
    if uid is not None:
        for m in msgs:
            if str(_g(m, "role") or "").upper() != "ASSISTANT":
                continue
            pid = _g(m, "parent_id", "parentId", "parent_message_id")
            if pid is not None and str(pid) == str(uid):
                return m
    for m in reversed(msgs[user_idx + 1:]):
        if str(_g(m, "role") or "").upper() == "ASSISTANT":
            return m
    return None


def _settled(reply, unknown_status):
    """Done when status is finished (or status is absent and a body exists)."""
    if reply is None:
        return False
    sv = _g(reply, "status", "message_status", "state", "gen_status")
    if sv is None:
        return True
    if isinstance(sv, str):
        su = sv.strip().upper()
        if su in _FINISHED:
            return True
        if su in _WIP:
            return False
        unknown_status.add(sv)
        return False
    unknown_status.add(repr(sv))
    return False


def _log_field_observation(msgs, unknown_status):
    """On timeout, log key names and status enums only, never message content."""
    try:
        roles = sorted({str(_g(m, "role")) for m in msgs if isinstance(m, dict)})
        keys = sorted({k for m in msgs[:5] if isinstance(m, dict) for k in m.keys()})
        logger.warning(
            "page mode timeout field-observation: msgs=%d roles=%s msg_keys=%s unknown_status=%s",
            len(msgs), roles, keys, sorted(unknown_status))
    except Exception:
        pass


async def poll_reply(token, prompt, timeout_s=None, interval=POLL_INTERVAL_S,
                     base=None, search=False):
    """Poll the read-only endpoints until the reply is finished; return (body, thinking)."""
    timeout_s = timeout_s or page_timeout()
    base = (base or functions.UPSTREAM_BASES[0]).rstrip("/")
    deadline = time.time() + timeout_s
    t0 = time.time()
    marker = _norm(prompt)[-48:]
    unknown_status = set()
    last_alive = 0.0
    msgs = []

    while time.time() < deadline:
        if time.time() - last_alive > 30:
            await _assert_alive(token)
            last_alive = time.time()

        sess_id = None
        st, page_body = await _get_json(base + "/api/v0/chat_session/fetch_page", token)
        if st == 200:
            for lst in _find_dict_lists(page_body, ("chat_session_id", "session_id")):
                cand = max(lst, key=_ts_key)
                v = _g(cand, "chat_session_id", "session_id", "id")
                if v is not None:
                    sess_id = v
                    break

        if sess_id is not None:
            st2, hist = await _get_json(
                base + "/api/v0/chat/history_messages?chat_session_id=" + str(sess_id), token)
            if st2 == 200:
                msgs = []
                for lst in _find_dict_lists(hist, ("fragments", "role")):
                    for m in lst:
                        if isinstance(m, dict) and (isinstance(m.get("fragments"), list) or "role" in m):
                            msgs.append(m)

        user_idx, user_msg = _locate_user_message(msgs, marker, t0)
        if user_msg is not None:
            reply = _locate_reply(msgs, user_idx)
            if _settled(reply, unknown_status):
                text = _frag_text(reply, RESPONSE_TYPES)
                if text.strip():
                    think = _frag_text(reply, THINK_TYPES)
                    return text, think

        await asyncio.sleep(interval)

    _log_field_observation(msgs, unknown_status)
    raise PageChatError(
        504,
        "page mode: no finished reply within %.0fs (did the carrier load the URL? "
        "is the browser account the same as the read token?)" % timeout_s)
