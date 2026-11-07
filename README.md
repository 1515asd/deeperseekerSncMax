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

> [!WARNING]
> **Upgrade notice for existing users**: if you are running v89.7 or earlier, please update to **v89.8**. Older versions have issues that affect you —
> 1. A muted account is shown as "Active" and surfaces `502 "biz error 0"` to clients; since v89.7 mutes are detected via upstream hard evidence and quarantined immediately (with the unmute time).
> 2. Before v89.6, the panel's "Restore" button and the file-upload endpoint returned a 500 on every call.
> 3. Since v89.8 chat requests carry the `x-hif-leim` anti-abuse token the official client always sends — every third-party proxy historically omitted it, and since early November DeepSeek treats the missing header as direct proof of a non-official client (fast bans/mutes). If your accounts get muted within minutes of use, upgrade.
> Upgrade by pulling the latest code and restarting — data and the token pool are compatible automatically, no manual migration. Afterwards, press "Probe tokens" under the token pool to see each token's real status (healthy / muted until X / invalidated).

> [!IMPORTANT]
> **November 2026 risk-control reality: switch to bridge-native login; stop hand-extracting browser tokens.**
> Since November, upstream revokes whole sessions when a token born in a browser is used from the bridge (or any other environment) — the server treats it as session theft: the token dies instantly, the browser gets logged out too, and the practical symptom is rapid bans plus ever-more-frequent re-extraction from the browser.
> Bridge-native login lets the session be born and used inside the bridge (android protocol); when a token dies, the bridge re-logs-in automatically — no more repeated browser extraction. Dashboard → "Add account / token" → Method 1 (recommended). Browser-token pasting is kept but demoted to an emergency fallback.

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
| v89.13 | Nov 07 | 1) Bridge login device-fingerprint fix: the real android app's login body carries a Shumei (SMSdk) device fingerprint; community testing shows random/fabricated UUIDs are rejected with `RISK_DEVICE_DETECTED` (biz_code=11) — which is exactly what v89.12's deterministic uuid5 is. The login form gains an optional "device fingerprint" field (fetched once via `SMSdk.getDeviceId()`, persisted and reused — including by auto re-login); when absent it falls back to uuid5 and reports risk_device honestly. Login request headers now match the real client shape (x-device-id UUID + x-device-model). 2) Browser-token extraction demoted from primary status: the dashboard and README now state plainly that browser-born tokens get session-revoked when used across environments (rapid bans + frequent re-extraction); token pasting is kept as an emergency fallback and bridge-native login is the recommended path | 
| v89.12 | Nov 07 | Optional bridge-native login (inert until used): accounts can log in inside the bridge via the android protocol — sessions are born and used in the same environment, sidestepping the November session-revocation policy that kills browser-extracted tokens within minutes. Native tokens self-heal (auto re-login, conservatively throttled to ~4 attempts/day). Passwords stored locally only, never logged. Existing token-add flow unchanged | 
| v89.8 | Nov 06 | Chat-completion requests now carry the `x-hif-leim` risk-control token — a short-lived token (TTL 600s) the official client fetches from `hif-leim.deepseek.com` and every third-party proxy historically omitted. Community packet capture (Oct 2026) proved the upstream needs zero behavioral statistics to identify a non-official client when this header is missing — matching the November mute/ban wave. The token comes from an unauthenticated endpoint (no account traffic, no account identity), is cached per TTL with a 30s refresh margin, and a failed fetch backs off 60s and degrades to "no header" — identical to all previous behavior: requests are never blocked and stale tokens are never sent. Env switch `DEEPSEEKER_HIF_ENABLED` (default on). The success path gains exactly one header the official client already sends; everything else is byte-identical |
| v89.7 | Nov 05 | Muted-account detection fixed (production-verified Nov 5 evening: a muted account was shown as "Active" and surfaced as 502 "biz error 0"): 1) upstream mute reports hide inside a nested envelope (outer code=0, inner biz_code=5 "user is muted") which the old code never looked at — now detected via the upstream `is_muted==1` boolean (hard evidence), hit = immediate quarantine with the unmute time in the reason; 2) new "Muted (confirmed)" dashboard state, distinct from "Likely muted"; 3) a "Probe tokens" button under the token pool asks DeepSeek for each token's real status (healthy / muted until X / invalidated) and auto-quarantines confirmed-muted tokens; 4) a silent startup scan of ACTIVE tokens so muted accounts no longer wear an "Active" badge overnight. Every change touches only failure paths; the success path is byte-identical |
| v89.6 | Nov 05 | Three classes of fixes (production-verified Nov 5 evening): 1) the pool-restore endpoint and the file-upload endpoint have returned a NameError 500 on every call since v77 — both fixed, with a repo-wide static scan confirming no similar residue; 2) the dashboard state system rebuilt — quarantine reasons are persisted and shown as five color-coded states (likely expired / likely muted / quarantined-other with the concrete reason), mute-shaped business errors no longer masquerade as healthy after cooldown, and every state carries plain-language guidance plus a self-check recipe; Restore stays always-safe (a dead token re-quarantines automatically); 3) the effort-instruction chain fixed — `reasoning_effort=max` strengthening instructions now actually apply for the first time, with a dashboard toggle (off = byte-identical to the previous release) |
| v89.5 | Nov 05 | Bare-JSON tool calls upgraded from "send back for rewrite" to deterministic auto-repair: when all four gates pass (the whole body is exactly one JSON object, the tool name matches this turn's real tool list, arguments is a dict, and all schema-required fields are present), the salvage layer re-wraps the lost `<tool_call>` tags and the call flows through the existing parse/delivery chain — zero extra upstream round-trip, arguments preserved verbatim, and the session history is no longer polluted by the leaked JSON. Any gate failing falls back to the v89.4 rewrite path — byte-identical to the previous version |
| v89.4 | Nov 05 | Bare-JSON tool-call self-healing: the model occasionally emits a tool call as bare JSON without `<tool_call>` wrapping (production-verified Nov 5, 00:23), which leaks into the reply body and stalls the session. When the whole body is exactly one JSON object whose tool name matches this turn's real tool list, the existing v62 diagnostic-continue kicks in — the bridge executes nothing and lets the model resend a well-formed call or hand back a final answer. Normal replies stay byte-identical |
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

## Token lifetime & dashboard states (plain language)

**When do tokens expire?** Unpredictably — DeepSeek's server decides, and there is no way to know in advance. When it happens, clients get `401` (with `INVALID_TOKEN`) or `503 No tokens available`, and the dashboard state column shows "Likely expired". The fix is to **extract a fresh token, add it, and delete the old one** — pressing "Restore" will not help; an expired token stays dead.

**What do the dashboard states mean?**

| State | Meaning | What to do |
|---|---|---|
| 🟢 Active | Working normally | Nothing |
| 🔵 Cooling down | Requests came too fast, brief pause | Recovers automatically, no action needed |
| 🟠 Muted (confirmed) | DeepSeek explicitly reports the account is muted (with unmute time) | The token cannot send messages until the mute expires — no action needed; press "Restore" after the mute ends |
| 🟠 Likely muted | Account probably temporarily restricted | Wait out the mute, then press "Restore"; if the account chats fine on the website, it was a false positive — just press "Restore" |
| 🔴 Likely expired | Token dead (expired or logged out) | Extract a fresh token, add it, delete the old one; "Restore" won't help |
| 🔴 Quarantined | Left rotation for another reason (see the note column) | Press "Restore" once the upstream is healthy; when unsure, verify first as described above |

**Can't tell expired from muted?** Press the "**Probe tokens**" button under the token pool — it asks DeepSeek directly for each token's real status (healthy / muted until X / invalidated) and auto-quarantines confirmed-muted tokens. You can also log into `chat.deepseek.com` with that account: a re-login prompt means expired; a mute notice when sending a message means muted; normal chatting means false positive. The "Restore" button is always safe — a truly dead token simply re-quarantines automatically, no harm done.

**Prompt enhancement toggle** (dashboard "Features" section): when a client sends an effort level such as `reasoning_effort=max`, an extra "think carefully, verify from angles" instruction is attached, giving deeper answers at the cost of a bit of latency and tokens. Toggle it in the dashboard anytime — effective immediately; when off, behavior is byte-identical to the previous release.

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
