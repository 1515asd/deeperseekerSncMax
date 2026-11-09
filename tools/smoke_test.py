#!/usr/bin/env python3
"""Cold-start smoke test — stdlib only, no third-party imports.

Verifies the endpoint behaviour documented in docs/GUIDE*.md ("Enabling page
mode") and the auth defaults. Called by .github/workflows/ci.yml on every push
so a broken default can no longer ship unnoticed.

Reference combination: Python 3.11.1 + the exact pins in requirements.txt.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEY = "dseeker"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def status(port: int, path: str, key: str | None = None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if key:
        req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode(errors="ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="ignore")
    except Exception as e:  # noqa: BLE001 - report any transport failure as -1
        return -1, str(e)


def boot(page_mode: bool):
    port = free_port()
    # DB_PATH decides where the main DB, the generated api_key.txt and the
    # cookie file land (see functions.data_dir / cookie_file_path), so one
    # value moves all three off the repo root into a throwaway directory.
    sandbox = tempfile.mkdtemp(prefix="deeperseeker-smoke-")
    env = dict(os.environ)
    # app.py calls load_dotenv() on the repo-root .env with override=False,
    # which only fills in keys that are NOT already present. Pre-seed every
    # sensitive key with a safe value instead of deleting it: deleting would
    # let .env re-inject a developer real backup target and credentials.
    env.update({
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "DEEPSEEKER_API_KEY": KEY,
        "DEEPSEEKER_ADMIN_PASSWORD": "ci-smoke-only",
        "DB_PATH": os.path.join(sandbox, "deeperseeker.db"),
        "DEEPSEEKER_COOKIE_PATH": os.path.join(sandbox, "aws_cookies_deepseek.json"),
        "DEEPSEEKER_BACKUP_TARGET": "none",
        "DEEPSEEKER_BACKUP_SECRET": "",
        "DEEPSEEKER_BACKUP_INTERVAL": "21600",
        "DEEPSEEKER_BACKUP_TG_BOT_TOKEN": "",
        "DEEPSEEKER_BACKUP_TG_CHAT_ID": "",
        "DEEPSEEKER_BACKUP_GIST_TOKEN": "",
        "DEEPSEEKER_BACKUP_GIST_ID": "",
        "DEEPSEEKER_BACKUP_FILE_PATH": "",
        "DEEPSEEKER_STREAM_DEBUG": "0",
    })
    if page_mode:
        env["DEEPSEEKER_PAGE_MODE"] = "1"
    else:
        # Keep the key present (as "0") rather than popping it: an absent key
        # is exactly what load_dotenv() would fill back in from a local .env.
        env["DEEPSEEKER_PAGE_MODE"] = "0"

    p = subprocess.Popen(
        [sys.executable, "app.py"], cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            if p.poll() is not None:
                return None, "service process exited before listening"
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.3)
        else:
            return None, "service did not open a port within 20s"
        time.sleep(0.8)
        result = {
            "login": status(port, "/login")[0],
            "models_no_key": status(port, "/v1/models")[0],
            "pending": status(port, "/page_mode/pending", KEY)[0],
            "pending_no_key": status(port, "/page_mode/pending")[0],
        }
        return result, None
    finally:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
        shutil.rmtree(sandbox, ignore_errors=True)


checks: list[bool] = []


def expect(name: str, got, want) -> None:
    ok = got == want
    print(("  PASS  " if ok else "  FAIL  ") + f"{name}: {got} (expected {want})")
    checks.append(ok)


print("=== page mode OFF (default) ===")
off, err = boot(False)
if err:
    print("  FAIL  boot:", err)
    sys.exit(1)
expect("/login", off["login"], 200)
expect("/v1/models without key", off["models_no_key"], 401)
expect("/page_mode/pending when disabled", off["pending"], 404)

print("=== page mode ON ===")
on, err = boot(True)
if err:
    print("  FAIL  boot:", err)
    sys.exit(1)
expect("/login", on["login"], 200)
expect("/page_mode/pending with correct key", on["pending"], 200)
expect("/page_mode/pending without key", on["pending_no_key"], 401)

print()
if not all(checks):
    print("SMOKE FAILED")
    sys.exit(1)
print("SMOKE PASS")
sys.exit(0)
