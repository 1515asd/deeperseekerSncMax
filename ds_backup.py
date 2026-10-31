"""
ds_backup.py — deeperseeker tokens table backup/restore module
==============================================================

Background
----------
The Belmo free tier does not promise container filesystem persistence (the
pricing page lists no disk quota for the free tier; redeploy = new container,
files written at runtime are not guaranteed to survive). The only
non-rebuildable asset in deeperseeker's SQLite is the `tokens` table (DeepSeek
tokens are entered purely by hand, with no automatic capture logic); losing
`sessions` / `session_map` / `files` / `chat_memory` only causes clients to
reconnect and rebuild — no long-term loss.

This module backs up/restores only the tokens table + key configuration,
encrypts it as a stream using a PBKDF2-derived keystream, and pushes it to an
external target; on startup, if the tokens table is empty, it restores
automatically.

Zero third-party dependencies (stdlib only).

Configuration (environment variables)
-------------------------------------
DEEPSEEKER_BACKUP_SECRET   Required. Backup encryption password (a random
                           string of 24+ characters recommended). When unset,
                           this module is fully and silently disabled, with
                           no impact on the main program.
DEEPSEEKER_BACKUP_TARGET   telegram | gist | file | none (default none)
DEEPSEEKER_BACKUP_INTERVAL Background backup interval in seconds
                           (default 21600 = 6 hours)
DEEPSEEKER_BACKUP_TG_BOT_TOKEN / DEEPSEEKER_BACKUP_TG_CHAT_ID
                           telegram target: Bot Token + chat_id receiving the
                           backup
DEEPSEEKER_BACKUP_GIST_TOKEN / DEEPSEEKER_BACKUP_GIST_ID
                           gist target: GitHub token + the ID of an existing
                           private Gist (the Gist must be created manually
                           first; afterwards this module only updates it)
DEEPSEEKER_BACKUP_FILE_PATH
                           file target: local path of the backup file

Integration (add one line each near the top of app.py, before __main__)
-----------------------------------------------------------------------
    import ds_backup
    ds_backup.restore_if_needed()          # startup restore: pull backup if tokens empty
    asyncio.create_task(ds_backup.background_loop())  # recurring scheduled backup

Restore logic
-------------
Restore triggers only when the tokens table is empty (fresh container / lost
database); when the table is non-empty, local data is never touched (local
first — the backup is disaster recovery only and never overwrites live data).
"""

import asyncio
import base64
import hashlib
import json
import os
import sqlite3
import time
import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_SECRET = os.getenv("DEEPSEEKER_BACKUP_SECRET", "").strip()
_TARGET = os.getenv("DEEPSEEKER_BACKUP_TARGET", "none").strip().lower()
_INTERVAL = max(300, int(os.getenv("DEEPSEEKER_BACKUP_INTERVAL", "21600")))
_TG_TOKEN = os.getenv("DEEPSEEKER_BACKUP_TG_BOT_TOKEN", "").strip()
_TG_CHAT = os.getenv("DEEPSEEKER_BACKUP_TG_CHAT_ID", "").strip()
_GIST_TOKEN = os.getenv("DEEPSEEKER_BACKUP_GIST_TOKEN", "").strip()
_GIST_ID = os.getenv("DEEPSEEKER_BACKUP_GIST_ID", "").strip()
_FILE_PATH = os.getenv("DEEPSEEKER_BACKUP_FILE_PATH", "").strip()

_DB = os.getenv("DB_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "deeperseeker.db"
)
_SNAPSHOT_NAME = "deeperseeker-tokens-snapshot"

_TG_API = "https://api.telegram.org"
_GIST_API = "https://api.github.com/gists"


# ---------------------------------------------------------------------------
# Encryption / decryption (PBKDF2-SHA256 keystream XOR, zero-dependency stream cipher)
# ---------------------------------------------------------------------------

def _keystream(secret: str, salt: bytes, length: int) -> bytes:
    out = b""
    counter = 0
    while len(out) < length:
        out += hashlib.pbkdf2_hmac(
            "sha256",
            secret.encode("utf-8"),
            salt + counter.to_bytes(4, "big"),
            100_000,
            dklen=32,
        )
        counter += 1
    return out[:length]


def encrypt(plaintext: bytes, secret: str = None) -> str:
    """Encrypt into base64(salt + ciphertext)."""
    secret = secret or _SECRET
    salt = os.urandom(16)
    ks = _keystream(secret, salt, len(plaintext))
    ct = bytes(a ^ b for a, b in zip(plaintext, ks))
    return base64.b64encode(salt + ct).decode("ascii")


def decrypt(payload: str, secret: str = None) -> bytes:
    """Decrypt the output of encrypt(). With a wrong password the data is garbage (caught by JSON validation)."""
    secret = secret or _SECRET
    raw = base64.b64decode(payload)
    salt, ct = raw[:16], raw[16:]
    ks = _keystream(secret, salt, len(ct))
    return bytes(a ^ b for a, b in zip(ct, ks))


# ---------------------------------------------------------------------------
# Snapshot export / restore (touches only the tokens table)
# ---------------------------------------------------------------------------

def _db_connect():
    conn = sqlite3.connect(_DB, timeout=10)
    return conn


def token_count() -> int:
    try:
        conn = _db_connect()
        n = conn.execute("SELECT COUNT(*) FROM tokens").fetchone()[0]
        conn.close()
        return n
    except Exception:
        return -1  # table missing or db unreadable


def export_snapshot() -> dict:
    """Export the tokens table + metadata."""
    conn = _db_connect()
    rows = conn.execute(
        "SELECT id, alias, token, status, rate_limited_until, last_used FROM tokens"
    ).fetchall()
    conn.close()
    return {
        "v": 1,
        "created_at": int(time.time()),
        "table": "tokens",
        "tokens": [
            {
                "id": r[0],
                "alias": r[1],
                "token": r[2],
                "status": r[3],
                "rate_limited_until": r[4],
                "last_used": r[5],
            }
            for r in rows
        ],
    }


def restore_snapshot(snapshot: dict) -> int:
    """Write a snapshot back into the tokens table (assumes the table exists; creates it first if not). Returns the number of restored rows."""
    conn = _db_connect()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alias TEXT,
            token TEXT,
            status TEXT DEFAULT 'ACTIVE',
            rate_limited_until REAL,
            last_used REAL
        )"""
    )
    n = 0
    for t in snapshot.get("tokens", []):
        conn.execute(
            "INSERT OR REPLACE INTO tokens (id, alias, token, status, rate_limited_until, last_used)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                t.get("id"),
                t.get("alias"),
                t.get("token"),
                t.get("status") or "ACTIVE",
                t.get("rate_limited_until"),
                t.get("last_used"),
            ),
        )
        n += 1
    conn.commit()
    conn.close()
    return n


def restore_if_needed() -> str:
    """Automatically restore from backup when the tokens table is empty. Returns an action description (for logging)."""
    if not _SECRET or _TARGET in ("", "none"):
        return "backup disabled"
    n = token_count()
    if n != 0:
        return f"local tokens={n}, skip restore"
    try:
        payload = _download()
        snapshot = json.loads(decrypt(payload))
        if snapshot.get("table") != "tokens":
            return "snapshot table mismatch, abort"
        restored = restore_snapshot(snapshot)
        return f"restored {restored} tokens from {_TARGET} snapshot (age {int(time.time()) - snapshot.get('created_at', 0)}s)"
    except Exception as e:
        return f"restore failed: {e}"


# ---------------------------------------------------------------------------
# Transport backends
# ---------------------------------------------------------------------------

def _post_json(url: str, body: dict, headers: dict = None, timeout: int = 30):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_json(url: str, headers: dict = None, timeout: int = 30):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _upload_tg(payload: str) -> str:
    """Upload via the Telegram Bot's sendDocument (text sent as a .enc file)."""
    boundary = "----dsb" + hashlib.md5(str(time.time()).encode()).hexdigest()
    parts = []
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{_TG_CHAT}\r\n".encode())
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{_SNAPSHOT_NAME}.enc\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n".encode()
    )
    parts.append(payload.encode("utf-8"))
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)
    req = urllib.request.Request(
        f"{_TG_API}/bot{_TG_TOKEN}/sendDocument",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not data.get("ok"):
        raise RuntimeError(f"telegram send failed: {data}")
    return f"telegram message_id={data['result']['message_id']}"


def _download_tg() -> str:
    """Find the latest snapshot file in the chat history and download it."""
    updates = _get_json(
        f"{_TG_API}/bot{_TG_TOKEN}/getUpdates?limit=100",
        headers={},
    )
    best = None  # (date, file_id)
    for upd in updates.get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        doc = msg.get("document") or {}
        fname = doc.get("file_name") or ""
        if doc.get("file_id") and _SNAPSHOT_NAME in fname:
            d = msg.get("date", 0)
            if best is None or d > best[0]:
                best = (d, doc["file_id"])
    if not best:
        raise RuntimeError("no snapshot document found in telegram chat")
    file = _get_json(f"{_TG_API}/bot{_TG_TOKEN}/getFile?file_id={best[1]}")
    fpath = file["result"]["file_path"]
    with urllib.request.urlopen(
        f"{_TG_API}/file/bot{_TG_TOKEN}/{fpath}", timeout=60
    ) as resp:
        return resp.read().decode("utf-8")


def _upload_gist(payload: str) -> str:
    """Update a single file in an existing private Gist (requires GIST_ID; creates one if not configured)."""
    body = {"files": {f"{_SNAPSHOT_NAME}.enc": {"content": payload}}}
    if _GIST_ID:
        req = urllib.request.Request(
            f"{_GIST_API}/{_GIST_ID}",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {_GIST_TOKEN}",
                "Content-Type": "application/json",
            },
            method="PATCH",
        )
    else:
        req = urllib.request.Request(
            _GIST_API,
            data=json.dumps({**body, "public": False}).encode("utf-8"),
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {_GIST_TOKEN}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    gid = data.get("id", "")
    return f"gist id={gid}"


def _download_gist() -> str:
    gist = _get_json(
        f"{_GIST_API}/{_GIST_ID}",
        headers={"Authorization": f"Bearer {_GIST_TOKEN}"},
    )
    f = gist.get("files", {}).get(f"{_SNAPSHOT_NAME}.enc")
    if not f:
        raise RuntimeError("snapshot file not found in gist")
    if f.get("truncated"):
        with urllib.request.urlopen(f["raw_url"], timeout=60) as resp:
            return resp.read().decode("utf-8")
    return f.get("content", "")


def _upload_file(payload: str) -> str:
    with open(_FILE_PATH, "w", encoding="utf-8") as f:
        f.write(payload)
    return f"file {_FILE_PATH}"


def _download_file() -> str:
    with open(_FILE_PATH, "r", encoding="utf-8") as f:
        return f.read()


_BACKENDS = {
    "telegram": (_upload_tg, _download_tg),
    "gist": (_upload_gist, _download_gist),
    "file": (_upload_file, _download_file),
}


def _upload(payload: str) -> str:
    up, _ = _BACKENDS[_TARGET]
    return up(payload)


def _download() -> str:
    _, down = _BACKENDS[_TARGET]
    return down()


# ---------------------------------------------------------------------------
# Public main flow
# ---------------------------------------------------------------------------

def backup_now() -> str:
    """Run one backup immediately. Returns a description (success / disabled / failure reason)."""
    if not _SECRET or _TARGET not in _BACKENDS:
        return "backup disabled"
    n = token_count()
    if n <= 0:
        return "no tokens to back up"
    snapshot = export_snapshot()
    payload = encrypt(json.dumps(snapshot, ensure_ascii=False).encode("utf-8"))
    try:
        info = _upload(payload)
        return f"backup ok: {n} tokens -> {info}"
    except Exception as e:
        return f"backup failed: {e}"


async def background_loop():
    """Regular scheduled backup coroutine (started by app.py via create_task)."""
    while True:
        await asyncio.sleep(_INTERVAL)
        try:
            msg = backup_now()
        except Exception as e:
            msg = f"backup loop error: {e}"
        print(f"[ds_backup] {msg}", flush=True)


if __name__ == "__main__":
    print(restore_if_needed())
    print(backup_now())
