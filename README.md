<div align="center">

# SncMax

**Self-hosted DeepSeek gateway that speaks the OpenAI & Anthropic API standards.**

[![License: MIT](https://img.shields.io/github/license/1515asd/deeperseekerSncMax.svg)](LICENSE)
[![Last Commit](https://img.shields.io/github/last-commit/1515asd/deeperseekerSncMax/main.svg)](https://github.com/1515asd/deeperseekerSncMax/commits/main)
[![Stars](https://img.shields.io/github/stars/1515asd/deeperseekerSncMax.svg)](https://github.com/1515asd/deeperseekerSncMax/stargazers)
![Release](https://img.shields.io/badge/release-Snc1-2ea44f.svg)

Point any OpenAI/Anthropic-compatible client at this server and it just works —
streaming, tool calling, and long-context session management out of the box.

**Works with** · Claude Code · Cline · LibreChat · Open WebUI · LobeChat · Cherry Studio · Dify · anything OpenAI/Anthropic-compatible

[English](README.md) | [简体中文](README.zh-CN.md)

</div>

> **Snc1 · Current release**: the method change is done and usage stays the same — log in via your browser, grab the token, paste it into the dashboard. For how to enable page mode and how it works, see the [User Guide](docs/GUIDE.md).

## What Snc1 brings

- **A new approach** — problems are eliminated at the source; the old "upstream changes, everything breaks" patch cycle is gone
- **Full-speed burst** — measured ~**63%** reduction in the server's own overhead (7.0ms → 2.6ms per turn); upstream latency is upstream's business, ours is now at the floor
- **A brand-new panel** — redesigned after Apple's Human Interface Guidelines: graceful low-cost animations, 5-day remember-me, multi-Key management, mobile layout; the old panel is retired
- **Multi-Key** — add multiple client Keys from the panel, toggle or delete anytime, the default Key always works
- **Less chasing, fewer updates** — captchas, device fingerprints, and policy changes official adds in the future barely reach this side anymore; the repo no longer needs to release with every upstream move

> See the [User Guide](docs/GUIDE.md) for how to enable page mode and how it works.

## What this fork adds

Compared with upstream DeeperSeeker (AmanCode22/deeperseeker) and, for that matter, the whole DeepSeek reverse-proxy scene, we are without question the most balanced. The basics are all inherited from upstream; here we list only what we added, effects only, no recipes:

- **Thinking and body each in their place, no empty replies** — official now splits a thinking reply into fragments plus a whole-packet snapshot; the body is no longer swallowed into the thinking box, and follow-up turns no longer answer empty because of a missing field
- **Long tasks run to the end** — a half-written answer picks up where it cut off, already-generated text is kept, unattended jobs don't lose the whole run
- **Which account works and which is limited, spelled out** — the panel marks every account normal / cooling / muted / expired with plain-word notes, "Check status" asks each one for its real state, and "Restore" is always safe
- **Reinstall or switch machines, the pool comes back** — the token table is encrypted and backed up on a schedule; an empty pool on startup restores itself, container rebuilds lose nothing
- **Malformed tool calls never reach the body** — the 16 known shapes of broken tool markers are intercepted before output
- **A gate against duplicate calls** — a factual note is injected only when the same command is resent verbatim; loops are caught as they happen, normal requests are untouched
- **Tutorials in both Chinese and English** — the panel and the full guide ship in two languages, covering install, the dashboard, and the FAQ

And the biggest one: the entire DeepSeek reverse-proxy scene is fighting bans and detection head-on. Official keeps shipping all kinds of risk control and sending assembly parts, so every other project has to adapt again and again, hits an error halfway through a run, and then you wait for it to slowly update. Our upgrade hands the sending step back to official's own page — we are almost the same as official sending it itself. Not finding a loophole, not fighting it, we simply don't take that road.

What does that get you? First, the part that is easiest to misread, said plainly: changing how a message is sent does NOT make the account safe. Whether an account gets banned is roughly fifty-fifty — about a 50% chance, a coin flip — and nobody can tell in advance whether it will be yours. So do not treat "banned only in rare cases" as any kind of promise, never use your primary account, and do not keep just one. What is certain, and the one real benefit, is narrower: the sending step is handed back to the official page itself, so we no longer go head-on against the official sending protocol; however often official updates its risk control and sending parts, you do not have to re-adapt again and again, and you will not be left waiting for other projects to slowly catch up after an error halfway through a run — that part we genuinely no longer need. Unless official closes this road one day, or something we cannot foresee turns up. In short: the account no longer takes the hits on the sending protocol, but the ban risk is still there, roughly a coin flip (about 50%) — use throwaway accounts only, and the risk is yours.

## What is this

Based on upstream [`AmanCode22/deeperseeker`](https://github.com/AmanCode22/deeperseeker) (MIT), this project wraps DeepSeek's free web tier into a standard API — no official API key needed — exposing both OpenAI Chat Completions and Anthropic Messages. This repo maintains a long-term self-hosted deployment on free hosting platforms.

## Three approaches, three trade-offs

Free-tier setups come down to three roads — **tokens** (log in yourself, paste into your own panel: your data, your control, but tokens need re-extracting), **browser** (a page stays open as the sender: the truest form, but your device must be always-on), and **community projects** (third-party public bridges: zero setup, but shared pools crowd, privacy is out of your hands, and they can vanish anytime). What they share: no official API key, all ride the free web tier, all deserve a throwaway account. As for risk control, we've handed the sending step back to official's own page, so there is no more head-on confrontation with the sending protocol — but "not fighting it head-on" is not "you won't be banned": a ban is roughly a coin flip, about a 50% chance, and official can flag any account on its own terms at any time. So across all three roads the same rule holds: throwaway accounts only, and the risk is yours.

![Tokens · Browser · Community projects — comparison](docs/comparison-en.png)

## Where to follow updates

- **Dev line (main)**: [commit history](https://github.com/1515asd/deeperseekerSncMax/commits/main)
- **English stable line (english)**: [commit history](https://github.com/1515asd/deeperseekerSncMax/commits/english)

| Version | Date | Notes |
|---|---|---|
| **Snc1** | 11-08 | **Method + panel generational change (current).** Sender replaced (how to enable it and how it works are in the User Guide; the send step is handed to the logged-in official page itself); full-chain speedup (own overhead -63%); panel rebuilt after Apple HIG: animations, 5-day remember-me, multi-Key, usage charts, mobile cards; fingerprint content demoted to appendix (optional), and absorbs the preceding test-period updates (performance close-out, super panel) |
| v90.3 | 11-07 | Thinking + body output restored: official now splits a thinking reply into fragments plus a whole-packet snapshot the old parser ignored, swallowing the body into the thinking box. `DEEPSEEKER_FRAGMENT_FIX=0` reverts |
| v90.2 | 11-07 | Follow-up turns must carry the `shumei_device_id` key (missing = instant empty reply); empty answers no longer cached by the duplicate guard |
| v90.1 | 11-07 | Device-fingerprint bookkeeping (panel-layer only); Quick-Instructions tutorial replaced by the verified browser-login + bookmarklet route |
| v90.0 | 11-07 | Usage-model generational change (rebuilt on v89.7): in-bridge login and hif risk-control headers removed; "user-device birth" model |
| v89.7 | 11-05 | Mute-detection fix (nested mute envelope no longer missed); per-token "Check status" button in the pool |
| v89.6 | 11-05 | Two NameError-always-500 endpoints fixed; panel status system rebuilt (quarantine reasons persisted, five states) |
| v89.5 | 11-05 | Bare-JSON tool calls deterministically auto-repaired (four gates, zero extra round-trips) |
| v89.4 | 11-05 | Bare-JSON tool-call self-healing (diagnostic continuation; normal replies byte-identical) |
| v89.3 | 11-04 | Zero false quarantines: auth-worded soft evidence no longer punished once, 3-strike threshold, success resets |
| v89.2 | 11-04 | Duplicate-call gate wording reduced to plain factual statements |
| v89 | 11-04 | Duplicate-call gate (intervenes only while a loop is actually happening) |
| v88 | 11-04 | Ported upstream PR #94 hardening: business-level errors classified and trigger rotation |
| v87 | 11-04 | Fixed the "conversation amnesia" chain (stale PoW credentials → false empty-stream → session-map poisoning) |
| v86 | 11-01 | Admin panel redesign with deep health checks; Docker deployment files (PR #1, thanks @anch665) |
| v85 | 11-01 | Stable release for the 10-31 protocol change: native message numbering, empty-stream auto-continuation, tool-marker leak blocking |
| v82–v84 | 10-31 ~ 11-01 | Emergency fixes for the protocol change (test builds) |

## Recovering old conversations (started before the protocol change)

Sessions started before 2026-10-31 had their server-side state invalidated by the protocol change. **History is not lost** — two recovery paths:

1. **Wait**: once the old session expires (usually hours), send a new message; the client re-sends full history automatically and memory rebuilds
2. **Impatient**: log into the DeepSeek web app, delete that conversation, then send a message to rebuild

## Quick start

**First time? Read the [usage guide](docs/GUIDE.md)** — plain-language install, panel, concepts, and FAQ. Chinese version: [docs/GUIDE.zh-CN.md](docs/GUIDE.zh-CN.md).

Local run:

```bash
git clone https://github.com/1515asd/deeperseekerSncMax.git
cd deeperseekerSncMax
python3 -m venv env && source env/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in config before starting
python3 app.py
```

Docker / Podman:

```bash
cp .env.example .env
docker compose up -d          # or podman-compose up -d
```

Panel: `http://localhost:4000/`

> Free hosting platforms (Pterodactyl family): the panel injects `SERVER_PORT`, so start with `PORT=$SERVER_PORT python app.py`; the `wasm/` directory must be deployed in full; point `DB_PATH` at a persistent volume to keep the token pool across rebuilds.

## Configuration (`.env`)

| Variable | Meaning | Default |
|---|---|---|
| `DEEPSEEKER_API_KEY` | Bearer key required by the API (more can be added from the panel) | `dseeker` |
| `DEEPSEEKER_ADMIN_USER` | Panel username | `admin` |
| `DEEPSEEKER_ADMIN_PASSWORD` | Panel password | `admin` |
| `HOST` | Listen address (`127.0.0.1` local only, `0.0.0.0` open) | `127.0.0.1` |
| `PORT` | Server port | `4000` |
| `DEEPSEEKER_MAX_HISTORY_TOKENS` | History injection cap for the first message | `24000` |
| `DEEPSEEKER_MAX_TOOL_RESULT_TOKENS` | Tool-result injection cap | `12000` |
| `DEEPSEEKER_MEMORY_LIMIT_TOKENS` | Memory cap before rolling continuation | `393228` |
| `DEEPSEEKER_MAX_OUTPUT_TOKENS` | Per-response output cap | `8192` |
| `DEEPSEEKER_MAX_SUMMARY_TOKENS` | Handoff-summary budget | `4096` |

> **Two defaults that bite**: (1) `dseeker` above is the value pre-filled in `.env.example` — it applies when you copy that file to `.env`. If `DEEPSEEKER_API_KEY` is **not set** at all (plain `python app.py` / `docker run` with no `.env`), the service **generates** a random `dsk-` key into `api_key.txt` in the data dir (mode 0600, printed once on first boot); read it there, since `dseeker` will then return 401. (2) `docker compose` aborts the start only when `DEEPSEEKER_ADMIN_PASSWORD` is unset or empty — it does not check password strength, and `.env.example` already ships `admin`, so copying it to `.env` passes; set a strong password yourself. Running the code directly with the default `admin/admin` only logs a warning and still starts. See the [User Guide, Security notes](docs/GUIDE.md).
>
> For the full variable list (backup, streaming and context tunables), see [`.env.example`](.env.example). Backup snapshots are encrypted and sent off-host — read "Backup and outbound data" in the guide before enabling it.

## Getting a token

1. Log into `chat.deepseek.com` in an incognito window (new devices may require an email OTP — that's the official flow)
2. Extract the token (pick one):
   - **Desktop / Android (with dev tools)**: run `JSON.parse(localStorage.getItem("userToken")).value` in the F12 console
   - **iPhone / iPad (Safari bookmarklet)**: Safari cannot run `javascript:` directly — bookmark any page, edit the bookmark, replace its URL with the snippet below, then tap it while on the logged-in `chat.deepseek.com` page:

   ```javascript
   javascript:(function(){var t='';try{var r=localStorage.getItem('userToken');if(r){try{t=JSON.parse(r).value||r;}catch(e){t=r;}}}catch(e){alert('Read failed: '+e.message);return;}if(!t||t.length<20){alert('No userToken found.');return;}var a=document.createElement('textarea');a.value=t;a.style.cssText='position:fixed;left:-9999px';document.body.appendChild(a);a.select();a.setSelectionRange(0,999999);document.execCommand('copy');a.remove();alert('Copied. length '+t.length+'\n'+t.substring(0,12)+'...');})();
   ```

3. Paste the token into the panel (`/dashboard`), close the incognito window

## Token lifetime & panel states (plain words)

**When do tokens expire?** Whenever DeepSeek's server decides — unknowable from here. Expired tokens surface as client `401` (incl. `INVALID_TOKEN`) or `503 No tokens available`, with the panel showing "likely expired". **Extract a new token, add it, delete the old one** — the "Restore" button won't help; a dead token is dead.

**What do panel states mean?**

| State | Meaning | What to do |
|---|---|---|
| 🟢 Normal | Working | Nothing |
| 🔵 Cooling | Rate-limited, resting | Recovers automatically |
| 🟠 Confirmed muted | DeepSeek explicitly reported the account muted (with end time) | Wait it out, then "Restore" |
| 🟠 Likely muted | Account suspected temporarily restricted | "Restore" after the ban lifts; if the web app chats fine, it was a false positive — just "Restore" |
| 🔴 Likely expired | Token dead (expired or logged out) | Extract a new one, add, delete the old; "Restore" won't help |
| 🔴 Quarantined | Left rotation for another reason (see the note column) | "Restore" once upstream is confirmed healthy |

**Can't tell expired from muted?** Use the "**Check status**" button — it asks DeepSeek directly about each token (normal / muted until X / revoked) and auto-quarantines confirmed mutes. Or log into `chat.deepseek.com`: re-login prompt = expired; mute popup = muted; chatting fine = false positive. The "Restore" button is always safe — a truly dead token gets re-quarantined automatically.

**Prompt-enhance switch** (panel "Feature switches"): with `reasoning_effort=max`-style requests, an extra "think slowly, verify from angles" instruction is appended — deeper answers, slightly slower, more tokens. Toggleable anytime; off = byte-identical to the old behavior.

## API endpoints

- **OpenAI-compatible**: `http://localhost:4000/v1` — `/v1/chat/completions` (streaming/non-streaming), `/v1/responses`, `/v1/models`, `/v1/files`
- **Anthropic-compatible**: `http://localhost:4000` — `/v1/messages`, `/v1/files/upload`
- **Model**: `v4.1flash` (the single default, native vision). Any `model` value is normalized to it; `/v1/models` also lists `anthropic/claude-v4.1flash` for Claude Desktop discovery

## Disclaimer

⚠️ Automated use violates DeepSeek's terms of service. This project is not affiliated with DeepSeek. Use a dedicated throwaway account, never your personal one. Whether an account gets banned is roughly a coin flip — about a 50% chance — not "banned only in rare cases"; handing the sending step back to the official page only avoids head-on confrontation with the sending protocol, it cannot stop official from banning an account under its own risk control. That risk is not small and nobody can tell which account it lands on; it is entirely yours. Don't abuse it, personal use only.

## License

MIT — see [LICENSE](LICENSE). Copyright belongs to upstream author AmanCode22; this fork's improvements are released under the same license.

## Appendix · About the device fingerprint (optional, display only)

The fingerprint field in the panel is **for filing/display only**: it helps you tell tokens apart, is never sent with any request, and has nothing to do with how this project runs. **Leaving it empty is perfectly fine; there is no official enforcement against it. Feel free to skip it.**
