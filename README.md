<div align="center">

# DeeperSeeker

**Self-hosted DeepSeek gateway that speaks the OpenAI & Anthropic API standards.**

[![License: MIT](https://img.shields.io/github/license/1515asd/rh-deeperseeker.svg)](LICENSE)
[![Last Commit](https://img.shields.io/github/last-commit/1515asd/rh-deeperseeker/main.svg)](https://github.com/1515asd/rh-deeperseeker/commits/main)
[![Stars](https://img.shields.io/github/stars/1515asd/rh-deeperseeker.svg)](https://github.com/1515asd/rh-deeperseeker/stargazers)
![Release](https://img.shields.io/badge/release-v89-2ea44f.svg)

Point any OpenAI/Anthropic-compatible client at this server and it just works —
streaming, tool calling, and long-context session management out of the box.

**Works with** · Claude Code · Cline · LibreChat · Open WebUI · LobeChat · Cherry Studio · Dify · anything OpenAI/Anthropic-compatible

[English](README.md) | [简体中文](README.zh-CN.md)

</div>

## What is this

This project is based on upstream [`AmanCode22/deeperseeker`](https://github.com/AmanCode22/deeperseeker)
(MIT): it wraps the free DeepSeek web tier into a standard API — no official API
key needed — and exposes both OpenAI Chat Completions and Anthropic Messages
endpoints. This fork maintains long-running, self-hosted deployments on
free-tier platforms, keeping up with protocol changes and stability fixes.

## Where to see updates

- **Dev line (main)**: [commit history](https://github.com/1515asd/rh-deeperseeker/commits/main)
- **English stable line (english)**: [commit history](https://github.com/1515asd/rh-deeperseeker/commits/english)

`main` is the dev line; once a fix lands there, the `english` line follows
within days (syncs keep every piece of content in English). What each version
did:

| Version | Date | What changed |
|---|---|---|
| v89.3 | Nov 04 | Zero false-positive quarantines: the auth-wording classification of HTTP 200 business errors is soft evidence — a single hit no longer penalizes the account at all (zero cost on a wording fluke); quarantine now requires 3 consecutive auth-shaped business failures (configurable), and one successful turn resets the count. A real HTTP 401 (hard evidence) still quarantines immediately. Fixes the last v88 residual — English business wording that merely contains an auth-shaped word could sideline a healthy account indefinitely |
| v89.2 | Nov 04 | The duplicate-call gate note is now purely factual: it only states "this command appeared verbatim N-1 times before" and "the Result above is the genuine output of this call" — zero instructions, zero inference. It can never conflict with a user-mandated repeat, and it no longer vouches for results it cannot verify |
| v89 | Nov 04 | Duplicate-call gate (from a 2026-11-01 production post-mortem): when the same command is resent verbatim ≥2 times, a system note is injected into the result. It intervenes only while a loop is happening — normal-request prompts are byte-identical to the previous version |
| v88 | Nov 04 | Ported the upstream PR #94 hardening: business-level errors delivered as HTTP 200 (flagged account, killed web session, rate-limit wall) are now classified and trigger token rotation instead of pinning dead accounts; failures during new-chat creation / PoW setup are retried — outside the signature lock, so requests no longer hang |
| v87 | Nov 04 | Fixed the memory-loss chain. The root cause was introduced by this fork's own earlier optimizations (PoW credential prefetch + empty-stream auto-continue) — upstream has neither mechanism and never had this issue: prefetched credentials stopped expiring → after idle periods a stale credential got rejected (40301) → misread as an empty stream → auto-continue fired as an orphan new message → the session map got poisoned. PoW credentials now carry a hard 10-minute TTL, business errors are classified correctly, and auto-continue only fires after upstream acceptance. Invisible to clients (HTTP 200) |
| v86 | Nov 01 | Redesigned admin dashboard with deep-health integration; Docker deployment files added (PR #1, thanks @anch665) |
| v85 | Nov 01 | Stable release for the Oct 31 protocol change: native message-ID protocol (first message omits `parent_message_id`, follow-ups use real server-broadcast IDs), empty-stream auto-retry, tool-markup leak gate, old-chat self-healing. **Everyone should run v85 or newer** |
| v82–v84 | Oct 31 – Nov 01 | Emergency protocol fixes (test releases) |

> **Tracking upstream issue reports**: we watch the upstream project's
> [issues](https://github.com/AmanCode22/deeperseeker/issues) and PRs for
> problem reports and protocol changes, and port any relevant fixes here
> promptly — v88 was ported from upstream PR #94.

## What this fork adds

The base capabilities — the free web tier wrapped as a standard API, multi-account
rotation, long-context rollover, the built-in dashboard — are all inherited from
upstream and won't be repeated here. Below is only what we added on top, each
with a concrete scenario:

- **An answer that breaks mid-stream continues automatically** — e.g. you give
  the model an unattended long task and go do something else; when the upstream
  stream dies mid-answer, the server resumes writing in the SAME reply (≤2
  retries, everything already generated is kept), and the task finishes —
  instead of the answer silently stopping halfway and your whole task getting
  delayed. Upstream can only resend the whole turn or surface an error.
- **No tool-call garbage in the chat text** — e.g. the model occasionally
  emits a truncated tool-call marker; all 16 known shapes are intercepted
  before they reach the output, so fragments like `<tool_call>` never leak
  into the visible reply.
- **A token pool that survives container rebuilds** — e.g. free platforms
  rebuild containers without warning; the token table is encrypted and backed
  up off-site every 6 hours, and on startup an empty pool is restored
  automatically — no re-harvesting tokens one by one.
- **Two language lines** — `main` ships Chinese docs alongside English;
  [`english`](https://github.com/1515asd/rh-deeperseeker/tree/english) is a
  fully-English line (comments, logs, dashboard, docs — no other language)
  with code identical to `main`.

## Recovering old conversations (started before the protocol change)

Server-side chat state created before 2026-10-31 was invalidated by the
protocol change. Your history is **not lost** — two ways to recover:

1. **Wait**: once the old server-side chat expires (usually a few hours), send
   a message again — the client resends the full history and memory rebuilds.
2. **Or delete it now**: on the DeepSeek website, find that conversation,
   delete it, then message again — a fresh server-side chat is created and the
   full history is resent.

## Quickstart

**New here?** Read the **[User Guide](docs/GUIDE.md)** — a plain-language
walkthrough covering setup, the dashboard, how it works, and the FAQ. Also in
[简体中文](docs/GUIDE.zh-CN.md).

Local:

```bash
git clone https://github.com/1515asd/rh-deeperseeker.git
cd rh-deeperseeker
python3 -m venv env && source env/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your values first
python3 app.py
```

Docker / Podman:

```bash
cp .env.example .env
docker compose up -d          # or podman-compose up -d
```

Dashboard: `http://localhost:4000/`

> Free Pterodactyl-style hosts: the panel injects `SERVER_PORT` while this app
> reads `PORT`, so start with `PORT=$SERVER_PORT python app.py`. The `wasm/`
> directory must be deployed in full; point `DB_PATH` at a persistent volume to
> keep the token pool across container rebuilds.

## Configuration (`.env`)

| Variable | Description | Default |
|---|---|---|
| `DEEPSEEKER_API_KEY` | Bearer API key required to access endpoints | `dseeker` |
| `DEEPSEEKER_ADMIN_USER` | Dashboard login username | `admin` |
| `DEEPSEEKER_ADMIN_PASSWORD` | Dashboard login password | `admin` |
| `HOST` | Bind address (`127.0.0.1` local only, `0.0.0.0` expose) | `127.0.0.1` |
| `PORT` | Server port | `4000` |
| `DEEPSEEKER_MAX_HISTORY_TOKENS` | History-injection cap on first-message prompt builds | `24000` |
| `DEEPSEEKER_MAX_TOOL_RESULT_TOKENS` | Tool-result injection cap | `12000` |
| `DEEPSEEKER_MEMORY_LIMIT_TOKENS` | Remembered-context limit that triggers rollover | `393228` |
| `DEEPSEEKER_MAX_OUTPUT_TOKENS` | Per-response output cap | `8192` |
| `DEEPSEEKER_MAX_SUMMARY_TOKENS` | Handoff-summary budget | `4096` |

## Auth token setup

1. Open a private window and log in at `chat.deepseek.com`.
2. Extract the token — pick the method for your device:
   - **Desktop / Android (devtools)**: console (F12) →
     `JSON.parse(localStorage.getItem("userToken")).value`
   - **iPhone / iPad (Safari bookmarklet)**: Safari won't run `javascript:`
     typed into the address bar — bookmark any page, edit the bookmark, replace
     its address with the code below, then tap it on a logged-in
     `chat.deepseek.com` page to copy the token:

   ```javascript
   javascript:(function(){var t='';try{var r=localStorage.getItem('userToken');if(r){try{t=JSON.parse(r).value||r;}catch(e){t=r;}}}catch(e){alert('Read failed: '+e.message);return;}if(!t||t.length<20){alert('No userToken found.');return;}var a=document.createElement('textarea');a.value=t;a.style.cssText='position:fixed;left:-9999px';document.body.appendChild(a);a.select();a.setSelectionRange(0,999999);document.execCommand('copy');a.remove();alert('Copied. length '+t.length+'\n'+t.substring(0,12)+'...');})();
   ```

3. Paste the token into the dashboard (`/dashboard`), close the private window.

## API endpoints

- **OpenAI-compatible**: `http://localhost:4000/v1` — `/v1/chat/completions` (streaming & non-streaming), `/v1/responses`, `/v1/models`, `/v1/files`
- **Anthropic-compatible**: `http://localhost:4000` — `/v1/messages`, `/v1/files/upload`
- **Model**: `v4.1flash` — the single default model (vision-capable). Whatever `model` value the client sends is normalized to it; `/v1/models` also lists `anthropic/claude-v4.1flash` for Claude Desktop auto-discovery.

## Disclaimer

⚠️ Automated use violates DeepSeek's Terms of Use. This project is not
affiliated with DeepSeek. Use a dedicated throwaway account, never your
personal one; accounts may be banned at any time. Use responsibly, personal
purposes only.

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 AmanCode22 (upstream author).
This fork's improvements are released under the same MIT license.
