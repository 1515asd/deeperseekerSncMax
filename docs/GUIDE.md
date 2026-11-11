# DeeperSeeker User Guide (English)

> **How this guide is written**: every concept appears twice — **plain English
> first, the technical term second**. The plain version says "what it is, what
> you do, what counts as working" and assumes no background; the technical
> version says "what actually happens underneath" so you can debug problems and
> compare with other tools.
>
> Also, **almost every failure has a written fix**: Chapter 6 is a FAQ and
> Chapter 7 is a troubleshooting manual organised by symptom, each entry laid
> out as "symptom → cause → fix → how to confirm it's fixed". When something
> breaks, start in Chapter 7 by symptom, then go back to the matching chapter
> for thereasoning behind it.
>
> 中文版：[GUIDE.zh-CN.md](GUIDE.zh-CN.md) · Main README (config table & version
> notes): [../README.md](../README.md) · Full `.env` variable list:
> [../.env.example](../.env.example) · Questions:
> [open an issue](https://github.com/1515asd/deeperseekerSncMax/issues) (English
> or Chinese both fine)

---

## Contents

| Chapter | When to read it |
|---|---|
| 1. What this is | First contact — what it can and cannot do |
| 2. Getting started | Install, configure, add a token, connect a client, verify |
| 3. Enabling page mode (Snc1, optional) | To use the "let the official page send it" path |
| 4. Dashboard tour | What each part of the panel does |
| 5. How it works | Understand sessions, IDs, summaries, rotation |
| 6. FAQ | Look up a specific question |
| 7. Troubleshooting manual | **Start here when it breaks**: symptom → cause → fix → verify |
| 8. Security & outbound data | Read before going public or enabling backups |
| 9. Config quick reference | Where to find / change a given variable |

---

## 1. What this is?

### Plain English

You run a small program on your own machine (or a server). It turns "chatting
on the DeepSeek website" into a standard AI API.

Once it's an API, all your AI tools — Claude Code for coding, Cherry Studio,
Open WebUI, Cline, LibreChat, LobeChat, Dify, and anything else that lets you
fill in "an API address and a key" — can connect to it and work as if they were
talking to a paid official API: streaming output, tool calling, long-running
conversations. The difference is that **the traffic rides on the free DeepSeek
web tier, so no official API Key is needed**.

Think of it this way: the DeepSeek website originally only had "an interface
for humans to click". This program wires a "line for programs" into it. Your
tools plug into that line and can talk; what the website sees is a normal
browser session.

### Technical terms

DeeperSeeker is a **self-hosted reverse-proxy gateway**.

- It holds your DeepSeek web session credential (token, a session JWT) and
  talks to the chat backend on the browser's behalf;
- It exposes standard protocol endpoints: OpenAI Chat Completions
  (`/v1/chat/completions`) and Anthropic Messages (`/v1/messages`), plus
  compatibility endpoints such as `/v1/responses`, `/v1/models`, `/v1/files`;
- Internally it handles session mapping, PoW challenge solving, multi-account
  pool rotation, upstream failover, summarize-and-rollover context compression,
  and tool-call parsing and repair;
- Credentials, usage, and sessions all live in a local database and, by
  default, never leave the host.

One line: **the free web-tier quota with an official-API developer experience.**

### What it can and cannot do

| Can | Cannot / not guaranteed |
|---|---|
| Connect OpenAI / Anthropic-compatible clients to the free DeepSeek web tier | Does not provide an official API Key, and is not equivalent to the paid official API's quota or stability |
| Streaming and non-streaming output, tool calling, long-conversation continuity | When upstream (DeepSeek) changes its protocol, rate-limits, or does maintenance, this cannot magically remove that |
| Automatic multi-account rotation, auto-isolation of broken accounts | Cannot revive a banned account — a dead token must be re-extracted |
| Store the token pool, sessions, and usage locally | Nothing leaves the host by default; data leaves only if you actively enable a backup target |
| Bilingual (EN/ZH) panel and docs | Does not promise account safety — automated use violates DeepSeek's terms; the risk is yours |

> **One concrete advantage, with a concrete scenario** (instead of a vague
> "it's stable"): with many setups, you let a model run a long task unattended,
> and it quietly breaks halfway — or stops mid-answer — so when you come back
> you find the task was never finished and has to be redone from scratch, which
> is frustrating and delays everything. This project specifically handles that
> class of "breaks halfway" problem: transient upstream hiccups, a stream that
> suddenly drops, or a model stopping mid-sentence are auto-continued and the
> result is collected (technical: "empty-stream auto-retry" and "failover" in
> Chapter 5). It does **not** promise "never an error" — it turns "one error
> loses all progress" into "in most cases it picks itself back up".

### What you need before starting

1. A machine that can stay on (your PC, a NAS, a VPS, a free host — any works);
2. Python 3.11 or newer (cold-start verified on 3.11 and 3.13; not claimed to
   cover every 3.x release);
3. A **dedicated throwaway** DeepSeek account — **never your primary one**;
4. One client you want to connect (pick any);
5. Five minutes and a text editor.

---

## 2. Getting started

### 2.1 Environment

**Plain**: check Python is installed and new enough, then fetch the repo.

**Technical**: runtime dependencies are pinned in `requirements.txt` (8 direct
dependencies, all `==` exact versions), isolated in a virtual environment so
they don't collide with other Python projects on the system.

```bash
python3 --version        # need 3.11 or newer; 3.11 / 3.13 verified
git clone https://github.com/1515asd/deeperseekerSncMax.git
cd deeperseekerSncMax
python3 -m venv deeperseeker_env
source deeperseeker_env/bin/activate        # Windows: deeperseeker_env\Scripts\activate
pip install -r requirements.txt
```

Line by line (if a line hangs, see the right column):

| Command | What it does | If it fails |
|---|---|---|
| `python3 --version` | Check the version | `command not found` → install Python; below 3.11 → upgrade |
| `git clone ...` | Fetch the code | Network timeout → switch network, or download the ZIP from the repo page |
| `python3 -m venv ...` | Create the venv | Missing `venv` / `ensurepip` → install the OS package `python3-venv` |
| `source .../activate` | Enter the venv | Your prompt should show `(deeperseeker_env)`; if not, activation failed |
| `pip install -r requirements.txt` | Install the 8 pinned deps | Won't install → see 7.1; don't edit the pinned versions |

> **Why the deps are pinned**: the 8 versions in `requirements.txt` are the exact
> set that actually passes "install → cold start → endpoint smoke" for this
> service. A new upstream release of any one package can silently change
> behavior; pinning keeps us all talking about the same set. To upgrade, bump
> the version and let CI smoke tests run first. **Don't strip the `==` for
> convenience** — that hands "it works" over to luck.

### 2.2 Configure and start

**Plain**: `.env` is this program's "settings file". On first use, decide at
least three things — **the dashboard password, the API key your clients use,
and the bind address**. The repo ships a template `.env.example`; copy it and
edit.

**Technical**: `.env` holds environment variables
(`DEEPSEEKER_ADMIN_PASSWORD`, `DEEPSEEKER_API_KEY`, `HOST` / `PORT`, …) loaded
at startup by `python-dotenv`. The full list is in the template itself and the
main README's Configuration section; this section covers only the minimum set.

```bash
cp .env.example .env     # copy the template, then edit it
python3 app.py
```

The three minimum values:

| Variable | Value in template | What to change it to | If unchanged |
|---|---|---|---|
| `DEEPSEEKER_ADMIN_PASSWORD` | `admin` | A strong password | A bare run only logs a warning and still starts; `docker compose` refuses to start when it is *unset*, but does not reject a weak one — see 7.6 and Chapter 8 |
| `DEEPSEEKER_API_KEY` | `dseeker` | Your own random string | You've set the service key to a public default; anyone can call your API |
| `HOST` | `127.0.0.1` | Keep it for local use; use `0.0.0.0` for remote access | `0.0.0.0` exposes it to the network — pair it with a strong password and HTTPS, see Chapter 8 |

> **One very easy trap (don't mix the two kinds of key)**: if you set
> **no** `DEEPSEEKER_API_KEY` at all (delete the line, no `.env` loaded), the
> service does **not** fall back to `dseeker` — it **generates** a random
> `dsk-` key into `api_key.txt` in the data dir (mode 0600, printed once in the
> first-boot log). Filling in `dseeker` then always returns 401; you must read
> the real key from that file. Conversely, copying the template `.env` makes
> `dseeker` the effective value. Only one of these two applies — mixing them
> gives you a permanent 401 (see 7.2).

Three ways to run it:

| Way | Command | Good for |
|---|---|---|
| Bare local | `python3 app.py` | Development, trying it out, single machine |
| Docker / Podman | `cp .env.example .env` then `docker compose up -d` | Long-running, need a persistent volume |
| Free host (Pterodactyl-style) | `PORT=$SERVER_PORT python app.py` | The panel injects `SERVER_PORT` instead of `PORT`; the whole `wasm/` dir must be deployed; `DB_PATH` must point at a persistent volume or the token pool is lost on rebuild |

> Three things not to miss on hosts: **upload the entire `wasm/` dir**, **use
> `SERVER_PORT` as the port fallback**, **point `DB_PATH` at a persistent
> volume**. Missing any one shows up as, respectively, an immediate startup
> error, a port mismatch, or an empty token pool after every restart. See 7.8.

### 2.3 Open the dashboard

Browse to `http://localhost:4000/` (use the machine's address if remote) and
log in with the credentials from `.env`.

- Defaults are `DEEPSEEKER_ADMIN_USER` / `DEEPSEEKER_ADMIN_PASSWORD` =
  `admin` / `admin` — **change them**, especially on a public host; the startup
  log repeats the warning.
- Login page won't open, or always 401 → see 7.2.

The `4000` in the address comes from `PORT`; the Dockerfile `EXPOSE`, the
compose port mapping, `.env.example`, and both READMEs all align on this port.
Change it everywhere together or health checks break (see 7.4).

### 2.4 Add your first token

**Plain**: the service needs to know *which DeepSeek account* to chat as. That
credential is a **token** you extract from your own browser and paste into the
panel. The dashboard home page carries the full illustrated guide; all three
methods are written out here.

**Technical**: the token is the session JWT the website keeps in `localStorage`
(key `userToken`). Holding it server-side lets the bridge talk to the chat
backend on the browser's behalf; a token is equivalent to the account itself —
leaking it is leaking the account.

**Step 1**: open a private/incognito window and log in at `chat.deepseek.com`.
A new device may ask for an email code — that's the official flow, just follow
it.

**Step 2**: pick one extraction method for your device.

| Device | How |
|---|---|
| Desktop / Android (with dev tools) | F12 → console → run `JSON.parse(localStorage.getItem("userToken")).value`, copy the output |
| iPhone / iPad (Safari bookmark) | Safari can't run `javascript:` directly — bookmark any page → edit that bookmark → replace its URL entirely with the bookmarklet below → on the logged-in `chat.deepseek.com` page, tap the bookmark; the token is copied to the clipboard |
| iPhone Shortcuts | The panel home page gives an equivalent Shortcuts route, same result |

The Safari bookmarklet (copy the whole thing, replace the bookmark URL):

```javascript
javascript:(function(){var t='';try{var r=localStorage.getItem('userToken');if(r){try{t=JSON.parse(r).value||r;}catch(e){t=r;}}}catch(e){alert('Read failed: '+e.message);return;}if(!t||t.length<20){alert('No userToken found.');return;}var a=document.createElement('textarea');a.value=t;a.style.cssText='position:fixed;left:-9999px';document.body.appendChild(a);a.select();a.setSelectionRange(0,999999);document.execCommand('copy');a.remove();alert('Copied. length '+t.length+'\n'+t.substring(0,12)+'...');})();
```

If it pops `No userToken found.`, you're not logged in on that page or you're
on a different domain — go back to Step 1.

**Step 3**: in the panel click "Add token" → paste → save, then close the
incognito window.

**Add several accounts if you can**: the service rotates between them and
switches away automatically when one is rate-limited (technical: multi-account
pool rotation + automatic failover). With only one account, once it's
rate-limited or expired the service is simply unavailable.

> Tokens expire, and **when is decided by DeepSeek's servers — this side cannot
> predict it**. How expiry shows up, how to tell, and how to replace a token:
> see 6.1 and 7.3.

### 2.5 Connect your AI tool

**Plain**: fill in three things in your client — the API address, the key, and
the model name — then chat as usual.

**Technical**: the client sends a standard OpenAI / Anthropic request to this
service; the service translates the protocol, manages the session, and talks
upstream; model names are normalized, so whatever `model` the client sends maps
to the same single default model.

OpenAI-compatible clients (most tools):

| Field | Value | Note |
|---|---|---|
| Base URL | `http://localhost:4000/v1` | Use the machine's address if remote; mind the trailing `/v1` |
| API key | the `DEEPSEEKER_API_KEY` from `.env` | If the service generated its key, read the `dsk-` value from `api_key.txt` in the data dir |
| Model | `v4.1flash` | The single default, vision-capable; any other value is normalized to it |

Anthropic-compatible clients (e.g. Claude Code): Base URL is
`http://localhost:4000` (no `/v1`); the other two are the same.

Minimal `curl` check (replace `YOUR_KEY`):

```bash
curl -s http://localhost:4000/v1/models -H "Authorization: Bearer YOUR_KEY" | head -c 400
```

A JSON model list means the whole chain works; 401 means the key is wrong, a
refused connection means the service isn't up — see 7.2 and 7.1 respectively.

### 2.6 First-run verification: how to confirm it "really works"

Self-check in this order; each step has an explicit expectation:

1. **Service up**: no traceback in the startup log, and
   `http://localhost:4000/login` returns 200 (the login page).
2. **Auth works**: `/v1/models` without a key → 401; with the right key → 200.
   If reversed, `DEEPSEEKER_API_KEY` isn't taking effect the way you think (see
   the 2.2 trap).
3. **Token usable**: that account's state in the panel is "normal", not
   "cooldown / muted / expired / quarantined".
4. **Real chat**: send "hi" from a client, see streaming output; today's record
   appears in usage stats.
5. **Longer input**: paste a longer block, confirm a non-empty, non-truncated
   reply.

Any step off → go to the matching Chapter 7 section.

### 2.7 Quick troubleshooting for this chapter

| Symptom (plain) | Technical cause | Fix |
|---|---|---|
| `python3 app.py` exits with an error immediately | Deps missing / Python too old / port taken | 7.1 |
| Panel won't open | Not listening, `HOST` vs access address mismatch, wrong port | 7.1, 7.4 |
| Login always fails | Used a value other than the template default, or `.env` not loaded | 7.2 |
| `/v1/models` always 401 | Used `dseeker` but the service generated a random `dsk-` key (or vice versa) | 2.2, 7.2 |
| Client connects but reply is empty | Token expired / muted / pool empty | 7.3 |
| Token pool empty after every restart | `DB_PATH` points at a temp dir | 7.8 |

---

## 3. Enabling page mode (Snc1, optional)

### Plain English

After Chapter 2 you're on the **classic path**: the server assembles the
request and sends it to official itself.

The other route described on the home page — "let the official page send it" —
is called **page mode**. It needs three extra things: **turn on a switch,
install a small userscript in your browser, and keep that page logged in to the
same account**. Miss any one and a message waits until timeout and returns an
explicit error — it does **not** silently fall back to the classic path.

The upside: the send step is almost identical to the official page sending it
itself, so you don't have to keep adapting to upstream protocol changes. The
cost: you must keep a browser tab open, and the mode has several limits (3.5).

**It is optional**: leave it off and you're on the classic path, everything
else unchanged.

### Technical terms

This path is page mode. The server only builds a URL with `q=` and
`autosend=1` and queues it; it no longer constructs a completion request. The
companion userscript `page_mode_carrier.user.js` claims the URL from
`/page_mode/pending` and loads it via `location.href` inside the logged-in
official page, so the page sends the message itself. The server reads the reply
back through the read-only `fetch_page` / `history_messages` endpoints.
Multi-turn context is carried by the official page's own session; the server no
longer does session mapping or summary rollover.

### 3.1 Turn on the switch

Edit `.env`, add one line, then **restart the service**:

```bash
DEEPSEEKER_PAGE_MODE=1
```

- The switch accepts `1` / `true` / `yes` / `on`; `.env.example` has a
  commented template line — remove the leading `#`.
- Delete the line (or leave it unset) and you're back on the classic path,
  nothing else changes.
- To tune the wait, add `DEEPSEEKER_PAGE_TIMEOUT=300` (seconds, default 300).
  What a timeout reports and how to adjust: 3.6 and 7.9.
- **Restart after changing it** — this variable is read only at startup.

### 3.2 Install the companion userscript

`page_mode_carrier.user.js` is a userscript; install it with Tampermonkey /
Violentmonkey or similar. It runs only on `https://chat.deepseek.com/*`. Before
installing, change the two defaults at the top to your own:

```js
const BRIDGE = GM_getValue("BRIDGE", "http://127.0.0.1:4000");  // the service's address if it runs elsewhere
const KEY    = GM_getValue("KEY",    "dseeker");                 // your DEEPSEEKER_API_KEY from .env
```

> If the service uses a **generated** key (no `DEEPSEEKER_API_KEY` in `.env`),
> the `dseeker` default won't work — read the `dsk-` key from `api_key.txt` in
> the data dir and use that instead. Same trap as 2.2.

`KEY` can also be supplied by running
`localStorage.setItem("ds_page_mode_key", "your-key")` in the official page's
console; the script falls back to it. **Never commit a real API key to a public
repo.**

### 3.3 Keep the same account logged in

Open `https://chat.deepseek.com/`, log in with **the same account as the token
in your dashboard**, and leave the tab open (a background tab is fine). The
script polls every 4 seconds and navigates to the pending URL to send. If the
browser account differs from the token, the reply can't be read back and the
request waits until timeout (see 7.9).

### 3.4 Verify it's working

```bash
curl -H "Authorization: Bearer YOUR_KEY" http://127.0.0.1:4000/page_mode/pending
```

| Response | Meaning | What to do |
|---|---|---|
| `404 {"error":"page mode disabled"}` | Switch is off | Back to 3.1: confirm `DEEPSEEKER_PAGE_MODE=1` and a restart |
| `200 {"pending":[]}` | On and healthy | Send a message and observe |
| `401 {"error":"Invalid API key"}` | `KEY` in the script is wrong | Back to 3.2, recheck the key, see 7.2 |

Then send any message and watch the browser tab navigate and send it.

### 3.5 Limits to know before using it

- **No attachments**: image / file parts are rejected with an explicit 400 —
  page mode carries text only. Turn the switch off to send attachments (7.10).
- **800-character cap per prompt**: longer input first degrades to the last
  user message; only if that still exceeds the cap does it return 400. Nothing
  is silently truncated.
- **No silent fallback**: script missing, page logged out, account mismatch, or
  tab closed all end in an explicit error after timeout, never a quiet switch
  back to the classic path. This is deliberate — you learn where the problem is
  instead of getting an answer from who-knows-where.
- **Multi-turn memory lives in the page**: on this path the official page's own
  session carries the context; the server does no session mapping or summary
  rollover.

### 3.6 Going back

Delete `DEEPSEEKER_PAGE_MODE=1`, restart, and nothing else needs changing.

### 3.7 Read-back shape (always web-shaped in page mode, effectiveness NOT verified)

In page mode, the **send** step is done by the official page itself, but reading the reply back is still the server making requests. In the old code those read-back requests wore an **Android outfit** we stitched together (`Dalvik/2.1.0`, `x-client-platform: android`), while the send step in that same session wore the official page's web outfit. It is like swiping your own face at the door, then handing over a fake ID when someone checks the record — the problem is not how good the fake ID is, it is that the ID should not exist at all: a real official session's read-back traffic has no Android headers.

This step now has **no switch and nothing to turn on**: as long as page mode (see 3.4) is enabled, the two read-only read-back GETs always use web-shaped headers, and the legacy Android read-back branch has been removed. To name a concrete string: the read-back `user-agent` changes from `Dalvik/2.1.0 (Linux; U; Android 14; Pixel 7)` to `Mozilla/5.0 ... Chrome/126`, plus the `sec-ch-ua` / `sec-fetch-*` headers a real browser always sends.

**Three things to be clear about**: (1) it changes the *shape only* — the token still lives in the server DB and the outbound IP is unchanged, both left alone by this project's red lines; (2) whether the upstream accepts this read-back shape, and whether the account is any safer with it, has **no measured evidence here — no promises**; (3) it follows page mode — **without page mode the read-back path is never reached**, so this step does nothing.

#### The technical version

The two read-only GETs in `page_chat_pipeline.py` (`fetch_page` / `history_messages`) always use a set of web-shaped headers (13 keys, listed in `WEB_READ_HEADER_KEYS`) and do NOT carry PoW / `x-hif-leim` (those belong to completion requests). `functions.get_headers()` itself is **not touched by a single character** — the classic path, probes, token and outbound IP all stay as they were. This change lands only on page mode's own read-back pipeline.

As with the existing "no silent fallback" rule: a failed read-back will NOT quietly switch back to any legacy header set; it still ends in the same explicit error / timeout (see 3.5, 7.9). You can tell at a glance that this header set did not work, instead of getting an answer from who-knows-where; the trade-off is that it will not quietly cover for you either.

#### How to go back

There is no separate switch for this step. To return to the classic path used before page mode, remove `DEEPSEEKER_PAGE_MODE=1` from `.env` and restart (see 3.4); nothing else changes.

#### If something goes wrong

| Symptom | Cause | What to do |
|---|---|---|
| Messages keep timing out | Upstream did not accept this read-back shape, or the token / IP was flagged | Remove `DEEPSEEKER_PAGE_MODE=1`, restart back to the classic path, confirm read-back works again; do not keep retrying in the page |
| No effect after editing | Page mode is off, or the variable is read once at startup | Make sure `DEEPSEEKER_PAGE_MODE=1` is in `.env`, then restart the service, see 7.7 |
| Want to know which headers were used | — | See `_get_json` in `page_chat_pipeline.py`: read-back is always `web_read_headers()`, with no other branch |
| Worried about account risk | This step is shape only, it changes neither token nor IP | See the README for the honest account-risk wording; this item does not promise "no ban" |

> This step puts a shape back where theory says it belongs; it is not "no more bans after this change". The token host and outbound IP stay per the red lines, and effectiveness is left to real-world use to verify.

y per the
> red lines, and effectiveness is left to real-world use to verify.

---

## 4. Dashboard tour

One page covers all operations. Each block below has a plain / technical
version:

| Area | Plain | Technical |
|---|---|---|
| Six stat cards | How many accounts work, total accounts, requests in flight, today's consumption | Active tokens / pool capacity / in-flight concurrency / today's request & token aggregates / session-mapping rows |
| Upstream health | "Is DeepSeek reachable right now?" — click "Probe now" or wait for the automatic 30s check | Deep health probe: a real end-to-end round trip (incl. PoW), same path as `/health?deep=1` |
| Token pool table | Each account's state, current work, cooldown left; broken ones can be restored manually | State machine (ACTIVE / RATE_LIMITED / QUARANTINED), in-flight counters, cooldown timers, quarantine restore |
| Usage statistics | Which day, which key, how much | usage table aggregated by day × masked key |

Everything auto-refreshes every 10 seconds — no manual reload. Light/dark
follows the system theme.

---

## 5. How it works (optional reading)

### Plain English

1. Your tool sends a conversation → the service checks its "match book" for a
   previous session → a hit continues that upstream session, a miss opens a new
   one and packs the full history into the first message.
2. Every DeepSeek reply broadcasts a message ID; the service remembers it and
   continues from that exact number next turn — no guessing, a wrong number is
   rejected upstream.
3. Nearing the size limit, the old content is summarized into a "story so far",
   a new session opens, and the conversation continues — you notice nothing,
   and the model doesn't lose its memory.
4. An account gets rate-limited → the next one takes over with the context
   carried along.
5. An upstream hiccup (the model stops mid-answer, or returns nothing) is
   treated as an interruption and auto-continued, up to two retries.

### Technical terms

1. **Session mapping**: a canonicalized SHA-256 signature over the client
   message history; a hit reuses the upstream session (incremental send), a
   miss goes through the full-history-injection first-message path.
2. **Real message IDs**: since 2026-10-31 DeepSeek broadcasts
   `response_message_id` at stream start; the server captures it as the next
   turn's `parent_message_id` (the legacy +2 increment is gone).
3. **Summarize-and-rollover**: nearing the memory ceiling (~393K tokens) a
   handoff summary fires; a rolling summary + the recent tail + the newest
   message form the new session's first message. Related thresholds are listed
   as comments in `.env.example`.
4. **Failover**: 401 / 403 / 429 auto-quarantines the token; a replacement takes
   over with full context transfer.
5. **Empty-stream auto-retry**: upstream 502s / silent zero-output streams are
   treated as interruptions and continued automatically (≤2 retries).

---

## 6. FAQ

**6.1 Q: Do tokens expire? What does expiry look like?**
Yes, and the timing is decided by DeepSeek's servers — this side can't predict
it. It shows up as auth-type errors, that account's panel state turning
"quarantined / cooldown", or the client returning empty. **Fix**: re-extract a
fresh token per 2.4 and replace the old one in the panel; keeping several
accounts lets rotation switch away automatically so the service doesn't stop.
See 7.3.

**6.2 Q: My old conversations "lost their memory" after upgrading.**
DeepSeek changed its protocol on 2026-10-31; conversations started before that
need one "re-introduction". Either **wait** for the old session to expire then
send a message (clients always send full history, so one message restores it),
or **sign in on the web app and delete that conversation**, then send a message
to start a fresh session. Technically, mappings from that window carry stale
`parent_message_id` values that upstream rejects with biz_code 26; the
poisoned-session rebuild deletes the mapping and rebuilds from the
first-message path on failure.

**6.3 Q: The model stops mid-answer.**
The current version auto-continues on its own. If you still hit it, resend —
it's usually a transient upstream glitch. Resending the same message won't
double-execute (the idempotency replay window covers **successful** turns only;
failed turns always retry normally). If it keeps stopping, see 7.5.

**6.4 Q: Is tool calling (function calling) reliable?**
Yes: multi-format tool-call parsing with a leak gate (malformed tool markup
never reaches the output) and argument repair. HTML / XML as argument values
works fine too.

**6.5 Q: My account got banned.**
Use throwaway accounts. Banned accounts are auto-quarantined out of the pool
while the rest keep serving (failover). **Never use your primary account** —
automated use violates DeepSeek's terms; the ban risk is yours. See Chapter 8.

**6.6 Q: Can I use the paid official API too?**
`.env.example` has a group of "exit door" settings starting with
`DEEPSEEKER_MODE`, `DEEPSEEKER_OFFICIAL_API_KEY`, `DEEPSEEKER_OFFICIAL_BASE`
(off by default). That's a separate outlet, not the free web tier described in
this guide; exact values and meanings are in the `.env.example` comments —
don't mix them with the token flow above.

**6.7 Q: I changed `.env` but nothing changed.**
Almost every environment variable is read **once at startup**. Restart the
service after editing; this is especially true for `DEEPSEEKER_PAGE_MODE`
(3.1). If a change has no effect, first confirm you restarted — see 7.7.

**6.8 Q: Does my data leave the machine?**
Not by default. The token pool, sessions, and usage are in a local database;
only if you actively set `DEEPSEEKER_BACKUP_TARGET` to `telegram` / `gist` /
`file` does an encrypted snapshot leave the host. See Chapter 8 "Backup and
outbound data".

---

## 7. Troubleshooting manual

> **How to use this chapter**: find the row closest to your symptom, follow
> "cause → fix", then confirm with "how to verify it's fixed". Section numbers
> in a fix point back to thereasoning behind it.

### 7.1 Install / startup errors

- **Symptom**: `pip install -r requirements.txt` goes red, or `python3 app.py`
  exits with a traceback immediately.
- **Likely causes**: ① Python older than 3.11; ② venv not activated, packages
  went to the system; ③ network can't fetch packages; ④ port 4000 already
  taken by another program; ⑤ host is missing the `wasm/` dir.
- **Fix**:
  1. `python3 --version` → confirm ≥ 3.11; upgrade if not.
  2. Confirm the prompt shows `(deeperseeker_env)`; if not, re-run
     `source .../activate`.
  3. Can't fetch → switch network or a working package mirror and reinstall;
     **don't change the versions in `requirements.txt`**.
  4. Port taken → find it (`lsof -i :4000` or `netstat -tlnp | grep 4000`),
     close that program, or change the port per 7.4.
  5. On a host → confirm the whole `wasm/` dir is present and `DB_PATH` points
     at a persistent volume (7.8).
- **Verify fixed**: `python3 app.py` no longer tracebacks and logs that it's
  listening; `http://localhost:4000/login` returns 200.

### 7.2 Panel won't open / login fails / always 401

- **Symptom**: browser can't open the panel; login always fails; or
  `/v1/models` always 401.
- **Likely causes**: ① service isn't up (back to 7.1); ② `HOST` vs your access
  address mismatch — bound to `127.0.0.1` but accessed via a LAN IP; ③ **the
  two kinds of key got mixed** (2.2): a copied `.env` makes `dseeker`
  effective, but you may have no `.env` loaded and the service generated a
  `dsk-` key, or vice versa; ④ `.env` wasn't loaded at all (wrong dir, wrong
  filename).
- **Fix**:
  1. `curl http://localhost:4000/login` → is it 200? If not, back to 7.1.
  2. Remote access fails: set `HOST=0.0.0.0`, restart, and confirm the firewall
     allows the port; security preconditions in Chapter 8.
  3. 401: decide which key case you're in. `.env` present with
     `DEEPSEEKER_API_KEY` set → use that value; not set at all → read the real
     `dsk-` key from `api_key.txt` in the data dir. The dashboard login uses
     `DEEPSEEKER_ADMIN_USER` / `DEEPSEEKER_ADMIN_PASSWORD`, a **different
     thing** from this API key — don't cross-fill them.
  4. Check `.env` is at the project root and named exactly `.env` (not
     `.env.txt`).
- **Verify fixed**: login page opens and you can log in; `/v1/models` returns
  200 with the right key, 401 without.

### 7.3 Client connects but reply is empty / token state abnormal

- **Symptom**: requests reach the service but the reply is empty or an auth
  error; the account shows "cooldown / muted / expired / quarantined" in the
  panel.
- **Likely causes**: token expired, rate-limited, banned, account muted, or the
  pool has no usable account at all.
- **Fix**:
  1. Check the token pool table; for any "quarantined / cooldown" account, add
     a fresh token per 2.4.
  2. Keep several accounts so rotation switches away from bad ones (end of 2.4).
  3. Manually confirm on the official website that the account can send — if
     the web app itself can't send, it's an account-side problem this service
     can't fix.
- **Verify fixed**: at least one account is "normal" in the panel; "hi" gets a
  streaming reply; today's record appears in usage stats.

### 7.4 Changing the port / health check breaks

- **Symptom**: after changing `PORT` the container is reported unhealthy, or
  compose can't reach it.
- **Likely cause**: the port has to align in several places — `PORT`, the
  Dockerfile `EXPOSE`, the compose port mapping, the health check probing
  `localhost:4000`, and both READMEs. Changing one alone desyncs them.
- **Fix**: either change every location consistently, or **just don't change
  the port** — keeping the default 4000 is easiest. For remote access, put a
  reverse proxy in front of 4000 rather than changing the port.
- **Verify fixed**: `curl http://localhost:4000/login` returns 200; the
  container `HEALTHCHECK` passes.

### 7.5 Model stops mid-answer / keeps returning empty

- **Symptom**: output is cut off halfway, or a whole reply is empty.
- **Likely cause**: transient upstream hiccup, 502, or a silent zero-output
  stream. Normally the service auto-continues (Chapter 5) — a one-off needs no
  action.
- **Fix**: one-off → resend once; persistent → check the panel's "Upstream
  health" probe or click "Probe now" to confirm it's really upstream; keep more
  tokens so it can switch. The advanced switches (first-byte timeout, empty-
  stream retry thresholds, …) are listed as comments in `.env.example` — be
  sure which one you mean before changing any.
- **Verify fixed**: several consecutive messages complete; "Upstream health"
  shows reachable.

### 7.6 compose vs. the "default password" (easily misunderstood)

- **Symptom**: assuming "using `docker compose` blocks a weak password", or
  conversely "why did it start with no password set".
- **Stated plainly**:
  - compose's `DEEPSEEKER_ADMIN_PASSWORD` uses the `:?` form, meaning "**refuse
    to start when the variable is unset or empty**". It does **not** check
    password strength.
  - The effective value of that line in `.env.example` is `admin`. After
    `cp .env.example .env`, the variable **has a value**, so `:?` passes
    straight through — a weak `admin` password gets in. Set a strong one
    yourself.
  - With `python app.py` or `docker run` (bypassing compose), the default
    `admin` / `admin` only logs a warning and the service starts anyway; it
    doesn't block.
- **Fix**: whatever you run, **set a strong password yourself**; don't expect
  compose to handle that for you.
- **Verify fixed**: `.env` has a strong, non-`admin` value for
  `DEEPSEEKER_ADMIN_PASSWORD`; after a restart the new password logs in and the
  old one doesn't.

### 7.7 Config change has no effect

- **Symptom**: you changed a value in `.env`, behavior didn't change.
- **Cause**: environment variables are basically read **once at startup**;
  editing them while running does not hot-reload.
- **Fix**: restart the service (true for `DEEPSEEKER_PAGE_MODE`, `HOST`,
  `PORT`, `DEEPSEEKER_API_KEY`, …); for Docker use
  `docker compose up -d --force-recreate` or restart the container.
- **Verify fixed**: after the restart, confirm the new value took effect using
  the relevant chapter's self-check.

### 7.8 Token pool empty after every restart / data not persistent

- **Symptom**: after a container or host rebuild, all accounts are gone and
  usage history is cleared.
- **Cause**: `DB_PATH` points at a temp dir, or the host has no persistent
  volume mounted; in Docker the data volume wasn't kept.
- **Fix**: point `DB_PATH` at a persistent volume path (Docker default is
  `/app/data/deeperseeker.db`, on a named volume); on hosts confirm that dir is
  persisted. `DEEPSEEKER_COOKIE_PATH` likewise.
- **Verify fixed**: add a token → restart → token and usage are still there.

### 7.9 Page mode waits until timeout / returns an explicit error

- **Symptom**: with page mode on, a message never returns and errors after
  `DEEPSEEKER_PAGE_TIMEOUT`.
- **Cause (three things, all required — Chapter 3)**: switch not on + not
  restarted, browser script missing or `KEY` wrong, official page not logged
  into **the same account** or the tab was closed.
- **Fix**: use the 3.4 `curl` table to locate it — 404 → 3.1, 401 → 3.2, 200
  but nothing sends → check the page's logged-in account and whether the tab is
  still open; account mismatch is especially common.
- **Verify fixed**: 3.4 returns `200 {"pending":[]}`; a message makes the tab
  auto-navigate and send; the reply is read back.

### 7.10 Attachments rejected in page mode

- **Symptom**: a request with an image / file returns 400.
- **Cause**: page mode **carries text only**, no attachments — an explicit
  limit, not a bug (3.5).
- **Fix**: delete `DEEPSEEKER_PAGE_MODE=1`, restart back to the classic path,
  then send; or use plain text this time.
- **Verify fixed**: with page mode off, the same attachment request sends.

### 7.11 Backups not generated / enabled but nothing sent out

- **Symptom**: you configured a backup but see no snapshot, or nothing arrives
  at Telegram / Gist.
- **Cause**: the default `DEEPSEEKER_BACKUP_TARGET=none` does nothing; every
  non-`none` target **requires** `DEEPSEEKER_BACKUP_SECRET` or encryption can't
  happen; `telegram` / `gist` also need their own token / id (Chapter 8 table).
- **Fix**: check each variable for your target; the interval
  `DEEPSEEKER_BACKUP_INTERVAL` defaults to 21600 seconds (clamped to a minimum
  of 300 in code), so don't expect an immediate send.
- **Verify fixed**: after one cycle, an encrypted snapshot appears at the
  target; no errors in the log.

### 7.12 General order when none of the above matches

1. Is the service up: `curl http://localhost:4000/login` → 200? (7.1)
2. Is the key right: hit `/v1/models` with and without it (7.2).
3. Are tokens good: panel state, and can the official web app send manually
   (7.3).
4. Is upstream reachable: the panel's "Upstream health" probe (7.5).
5. Did config take effect: if you edited `.env`, confirm a restart (7.7).
6. Is data still there: compare the token pool before/after a restart (7.8).
7. Still stuck: open an [issue](https://github.com/1515asd/deeperseekerSncMax/issues)
   with the startup log and reproduction steps (English or Chinese both fine);
   **never paste a real token, API key, or password into an issue**.

---

## 8. Security & outbound data

- Change the dashboard password (defaults `DEEPSEEKER_ADMIN_USER` /
  `DEEPSEEKER_ADMIN_PASSWORD` = `admin` / `admin`). compose's `:?` blocks
  "unset", not "weak" — see 7.6.
- `DEEPSEEKER_API_KEY` is the key to your service — don't use a weak value, and
  don't confuse it with the auto-generated `dsk-` key (2.2, 7.2).
- A token equals the account itself; never share it, paste it into someone
  else's dashboard, or commit it to a public repo.
- Automated use violates DeepSeek's terms; ban risk is yours. Don't run a
  public relay, and **never use your primary account**.
- Prefer binding to localhost (`HOST=127.0.0.1`); for remote access, put it
  behind a VPN / reverse proxy + HTTPS, not a bare service on the public
  internet.
- If `DEEPSEEKER_API_KEY` is not set at all, the service generates a random
  `dsk-` key into `api_key.txt` in the data dir (mode 0600, printed once on
  first boot); `dseeker` then always returns 401.
- The fingerprint field in the panel is **for filing/display only** — it helps
  you recognise where a token came from and is never sent with any request.

### Backup and outbound data (off by default)

`ds_backup.py` periodically packs the **token pool, sessions, file index and
chat memory** from the DB, encrypts it with `DEEPSEEKER_BACKUP_SECRET`, and
ships it out. The default `DEEPSEEKER_BACKUP_TARGET=none` means nothing leaves
the host; `telegram` / `gist` / `file` do send data off-host:

| Target | Required variables | Destination |
|---|---|---|
| `telegram` | `DEEPSEEKER_BACKUP_TG_BOT_TOKEN`, `DEEPSEEKER_BACKUP_TG_CHAT_ID` | Telegram Bot `sendDocument` |
| `gist` | `DEEPSEEKER_BACKUP_GIST_TOKEN`, `DEEPSEEKER_BACKUP_GIST_ID` | A GitHub private Gist (create it first; the module creates one if the ID is empty) |
| `file` | `DEEPSEEKER_BACKUP_FILE_PATH` | A local path on this host |

- Every target other than `none` **requires** `DEEPSEEKER_BACKUP_SECRET`
  (24+ random chars recommended); the snapshot is encrypted before upload. Lose
  the secret and the backup is unrecoverable.
- `DEEPSEEKER_BACKUP_INTERVAL` defaults to 21600 seconds (6h), clamped to a
  minimum of 300 in code.
- All backup variables are listed in `.env.example`. The Gist token needs the
  `gist` scope; never commit bot tokens or the encryption secret. If something
  breaks, see 7.11.

---

## 9. Config quick reference

| I want to… | Where to look / what to change |
|---|---|
| See every variable available | `.env.example` (every item has a comment), or the main README's Configuration section |
| Change the dashboard password / user | `DEEPSEEKER_ADMIN_PASSWORD`, `DEEPSEEKER_ADMIN_USER` in `.env` |
| Change the client key | `DEEPSEEKER_API_KEY` in `.env`; or delete it to auto-generate → read `api_key.txt` |
| Change bind address / port | `HOST`, `PORT` in `.env` (port change: see 7.4; best not to) |
| Turn page mode on / off | `DEEPSEEKER_PAGE_MODE` in `.env` (Chapter 3) |
| Tune context / retry thresholds | The `DEEPSEEKER_` variables listed as comments in `.env.example` — uncomment and set as needed |
| Enable backups / switch target | `DEEPSEEKER_BACKUP_TARGET` plus the target's variables in `.env` (Chapter 8) |
| Something broke | Chapter 7, by symptom |

> After editing `.env`, **restart the service** for it to take effect (7.7).
> Unsure what a variable does? Read the comment line right above it in
> `.env.example` — don't guess from its name.

---

Questions? Open an [issue](https://github.com/1515asd/deeperseekerSncMax/issues)
(English or Chinese both fine). 中文版：[GUIDE.zh-CN.md](GUIDE.zh-CN.md).
