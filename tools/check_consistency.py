#!/usr/bin/env python3
"""Static consistency checks: docs / .env.example / Dockerfile / compose vs code.

Pure stdlib, no third-party imports, so CI can run it immediately after
checkout. Exit code 1 on any failure.

Why this exists: the "front page promises X, code defaults to Y" class of bug
kept slipping through because nothing compared prose with code. Every check
below guards one concrete drift that has actually happened in this repo.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Server-side code lives at the repo root. Scanning tools/ would pull in this
# very checker (its own string literals look like config names) and the login
# CLI, whose variables are not service .env settings.
PY_FILES = sorted(ROOT.glob("*.py"))
DOC_FILES = [
    ROOT / "README.md",
    ROOT / "README.zh-CN.md",
    ROOT / "docs/GUIDE.md",
    ROOT / "docs/GUIDE.zh-CN.md",
    ROOT / "docker-compose.yml",
    ROOT / ".env.example",
]
# Build-time ARG, not a runtime .env variable -> never required in .env.example
BUILD_ONLY = {"DEEPSEEKER_BUILD_COMMIT"}
NON_PREFIX_VARS = {"DB_PATH", "SERVER_PORT", "PORT", "HOST", "TRUSTED_PROXIES"}

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + ((" -- " + detail) if detail else ""))
    if not ok:
        failures.append(name)


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="ignore")


env_text = read(ROOT / ".env.example")
code_text = "\n".join(read(p) for p in PY_FILES)
docs = {p: read(p) for p in DOC_FILES}

# --- 1. every variable the code reads must be documented -------------------
doc_vars = set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]{3,})\s*=", env_text, re.M))
# String literals in code capture both os.getenv("X") and indirect references
# such as PAGE_MODE_ENV = "DEEPSEEKER_PAGE_MODE".
code_vars = set(re.findall(r"[\"']([A-Z][A-Z0-9_]{3,})[\"']", code_text))
code_vars = {
    v for v in code_vars
    if (v.startswith("DEEPSEEKER_") and re.fullmatch(r"DEEPSEEKER_[A-Z0-9][A-Z0-9_]*", v))
    or v in NON_PREFIX_VARS
} - BUILD_ONLY

missing = sorted(code_vars - doc_vars)
check("代码读取的配置项都在 .env.example 列出", not missing, ", ".join(missing))

# --- 2. every documented variable must actually exist in code --------------
phantom = sorted(v for v in doc_vars if v not in code_text)
check(".env.example 未列出代码中不存在的项", not phantom, ", ".join(phantom))

# --- 3. no invented switches in prose / comments ---------------------------
# This is what caught DEEPSEEKER_ALLOW_DEFAULT_ADMIN: a switch mentioned only
# in a compose comment, implemented nowhere.
mentioned: set[str] = set()
for p, t in docs.items():
    mentioned |= set(re.findall(r"\bDEEPSEEKER_[A-Z0-9_]+\b", t))
ghosts = sorted(mentioned - doc_vars - code_vars - BUILD_ONLY)
check("文档/注释里没有虚构的环境开关", not ghosts, ", ".join(ghosts))

# --- 4. port agreement across four files -----------------------------------
df = read(ROOT / "Dockerfile")
comp = read(ROOT / "docker-compose.yml")
rm, zh = read(ROOT / "README.md"), read(ROOT / "README.zh-CN.md")
expose = re.search(r"^EXPOSE\s+(\d+)", df, re.M)
cport = re.search(r'-\s*"[\d.]+:\d+:(\d+)"', comp)
envport = re.search(r"^PORT=(\d+)", env_text, re.M)
rmport = re.search(r"localhost:(\d+)", rm)
zhport = re.search(r"localhost:(\d+)", zh)
ports = [m.group(1) if m else "?" for m in (expose, cport, envport, rmport, zhport)]
check("端口 Dockerfile/compose/.env/README 中英一致", len(set(ports)) == 1, "/".join(ports))

# --- 5. documented default API key matches .env.example --------------------
env_key = re.search(r"^DEEPSEEKER_API_KEY=(\S+)", env_text, re.M)
key_ok = bool(env_key) and (
    f"`{env_key.group(1)}`" in rm and f"`{env_key.group(1)}`" in zh
)
check("README 中英的默认 API Key 与 .env.example 生效值一致", key_ok,
      env_key.group(1) if env_key else "未找到")

# --- 6. page mode stays OFF by default and is documented as optional -------
pm_lines = [l for l in env_text.splitlines() if "DEEPSEEKER_PAGE_MODE=" in l]
check("页面模式开关默认注释（关闭）", bool(pm_lines) and all(l.strip().startswith("#") for l in pm_lines))
gz, ge = read(ROOT / "docs/GUIDE.zh-CN.md"), read(ROOT / "docs/GUIDE.md")
check("GUIDE 中英写明页面模式可选且给出开启方式",
      "可选" in gz and "DEEPSEEKER_PAGE_MODE=1" in gz
      and "optional" in ge and "DEEPSEEKER_PAGE_MODE=1" in ge)

# --- 7. banned wording that was already cleaned up -------------------------
banned = [
    "细节暂不展开", "等稳定下来再补文档", "Details aren't spelled out here",
    "details not spelled out here", "rh-deeperseeker", "cd SncMax",
    "deepseek-v4.1-flash", "照着做就能跑起来", "follow along and it runs",
    "DEEPSEEKER_ALLOW_DEFAULT_ADMIN", "REFUSES to boot",
]
leftovers = [f"{p.name}:{b}" for p, t in docs.items() for b in banned if b in t]
check("已废弃/不实措辞无残留", not leftovers, ", ".join(leftovers))

# --- 8. CI must exist, and comments must not cite a non-existent CI --------
workflows = list((ROOT / ".github/workflows").glob("*.yml")) + list((ROOT / ".github/workflows").glob("*.yaml"))
check("存在 CI workflow", bool(workflows))
check("Dockerfile 不再引用不存在的 release CI", "release CI" not in df)

# --- 9. dependencies must carry version constraints ------------------------
req_lines = [l.strip() for l in read(ROOT / "requirements.txt").splitlines()
             if l.strip() and not l.strip().startswith("#")]
unpinned = [l for l in req_lines if not re.search(r"(==|>=|~=|<=)", l)]
check("requirements.txt 所有依赖都有版本约束", not unpinned, ", ".join(unpinned))

# --- 10. compile target present -------------------------------------------
check("存在可编译的 Python 源码", bool(PY_FILES), f"{len(PY_FILES)} 个文件")

print()
if failures:
    print(f"FAILED: {len(failures)} 项不一致 —— 修好文档或代码后重跑")
    sys.exit(1)
print("ALL PASS: 文档 / 配置 / 代码 一致")
sys.exit(0)
