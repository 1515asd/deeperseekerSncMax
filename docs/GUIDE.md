# DeeperSeeker User Guide (English)

> Every concept gets two versions: **plain English first, the technical term second.** Experts can skim for the terms; newcomers can follow the plain text step by step.

---

## 1. What is this?

**Plain version**: You run a small program on your own machine (or server) that turns "chatting on the DeepSeek website" into a proper AI API. All your AI tools — Claude Code, Cherry Studio, Open WebUI, anything that speaks OpenAI or Anthropic — can connect to it and work just like a paid official API, except it runs on the free DeepSeek web tier.

**Technical version**: DeeperSeeker is a self-hosted **reverse-proxy gateway**. It holds your DeepSeek web-session credentials (tokens) and exposes standard OpenAI Chat Completions (`/v1/chat/completions`) and Anthropic Messages (`/v1/messages`) endpoints, handling session mapping, PoW challenge solving, multi-account rotation, and context rollover internally.

In one line: **the free web-tier quota with an official-API developer experience.**

---

## 2. Running it in 5 minutes

### 1. Install and start

```bash
git clone https://github.com/1515asd/deeperseekerSncMax.git
cd deeperseekerSncMax
python3 -m venv deeperseeker_env
source deeperseeker_env/bin/activate        # Windows: deeperseeker_env\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                        # skip if there is no .env.example; see below
python3 app.py
```

**Plain**: `.env` is the program's settings file. On first run you only need three things: the dashboard login password, an API key for your clients, and the bind address. The full list lives in the main README's Configuration section.

**Technical**: `.env` carries environment variables (DEEPSEEKER_ADMIN_PASSWORD, DEEPSEEKER_API_KEY, HOST/PORT, …) loaded at startup.

### 2. Open the dashboard

Browse to `http://your-host:4000/` and log in with the credentials you set in `.env` (defaults are admin/admin — **change them**, especially on a public host; the startup log will nag you about it).

### 3. Add your first DeepSeek account

**Plain**: the program needs to know which DeepSeek account to chat with. That credential is called a *token*, extracted from your browser. The dashboard front page has a full step-by-step guide with three methods (F12 console one-liner / iPhone Shortcuts / Safari bookmarklet) — pick whichever suits your device.

**Technical**: the token is the session JWT stored in the site's localStorage (`userToken`). Holding it server-side lets the bridge talk to the chat backend on the browser's behalf.

Click "Add token" in the dashboard → paste → done. Adding several accounts is even better — the program rotates between them automatically (**technical**: multi-account pool rotation with automatic rate-limit failover).

### 4. Connect your AI tool

For the OpenAI flavor (most tools use this):

- **Base URL**: `http://your-host:4000/v1`
- **API key**: the `DEEPSEEKER_API_KEY` you set in `.env`
- **Model**: `v4.1flash` (the single default; any model value is normalized to it)

Anthropic-flavored clients (e.g. Claude Code) use Base URL `http://your-host:4000`; everything else is the same.

---

## 3. Dashboard tour

One page covers all operations:

| Area | Plain | Technical |
|---|---|---|
| Six stat cards | At a glance: how many accounts work, total accounts, requests in flight, what today consumed | Active tokens / pool capacity / in-flight concurrency / today's request & token aggregates / session-mapping rows |
| Upstream health | "Is DeepSeek reachable right now?" — hit "Probe now" or wait for the automatic 30s check | Deep health probe: a real end-to-end round trip (incl. PoW), same path as `/health?deep=1` |
| Token pool table | Each account's state, what it's doing, cooldown left; broken ones can be restored manually | Pool operations view: state machine (ACTIVE/RATE_LIMITED/QUARANTINED), in-flight counters, cooldown timers, quarantine restore |
| Usage statistics | Which day, which key, how much | usage table aggregated by day × masked key |

Everything auto-refreshes every 10 seconds. Light/dark follows your system theme.

---

## 4. How it works (internals — optional reading)

**Plain version**:

1. Your tool sends a conversation → the program checks its "match book" to see if this conversation was seen before → if yes it continues the existing upstream session; if no it opens a fresh one and packs the entire history into the first message.
2. Every reply from DeepSeek broadcasts a "message ID"; the program remembers it and continues from that exact number next turn — no guessing; a wrong number gets rejected upstream.
3. When a conversation nears the size limit, the program summarizes the old content into a "story so far", opens a new session and continues — you notice nothing, and the model doesn't forget.
4. Account rate-limited? The next account takes over automatically, carrying the conversation context with it.
5. Upstream hiccups (e.g. the model stops mid-answer) trigger automatic continuation, up to two retries.

**Technical version**:

1. **Session mapping**: a canonicalized SHA-256 signature over the client message history; a hit reuses the upstream session (incremental send), a miss goes through the full-history-injection first-message path.
2. **Real message IDs**: since 2026-10-31 DeepSeek broadcasts `response_message_id` at stream start; the server captures it as the next turn's `parent_message_id` — zero guessing anywhere in the chain (the legacy +2 increment is long gone).
3. **Summarize-and-rollover**: nearing the memory ceiling (~393K tokens) a handoff summary fires; a rolling summary + the recent tail + the newest message form the new session's first message.
4. **Failover**: 401/403/429 auto-quarantines the token; a replacement takes over with full context transfer.
5. **Empty-stream auto-retry**: upstream 502s / silent zero-output streams are treated as interruptions and continued automatically (≤2 retries).

---

## 5. FAQ

### Q: My old conversations "lost their memory" after upgrading

**Plain**: DeepSeek changed their protocol on 2026-10-31; conversations from before need one "re-introduction". Two ways to recover:
1. **Wait**: let the old session expire, then just send a message — your client always sends the full history anyway, so one message brings it all back.
2. **Impatient**: sign in on the DeepSeek website → find that conversation → **delete it** → come back and send a message; a fresh session starts.

**Technical**: session mappings from the protocol-change window carry stale `parent_message_id` values; the upstream rejects them with biz_code 26. The poisoned-session self-healing path deletes the mapping and rebuilds from the first-message path on failure; you can also delete the upstream session manually to speed things up.

### Q: The model stops mid-answer

The server auto-continues on its own. If you still hit it, it's usually a transient upstream glitch — resend. Resending the same message won't double-execute (the idempotency replay window only covers **successful** turns; failed turns always retry normally).

### Q: Is tool calling (function calling) reliable?

Yes. Multi-format tool-call parsing with a leak gate (malformed tool markup never reaches the output) and argument repair. Writing HTML/XML as argument values works fine.

### Q: My account got banned

Use throwaway accounts. Banned accounts are auto-quarantined out of the pool while the rest keep serving (failover). **Never use your primary account.**

---

## 6. Security notes (important)

- Change the dashboard password (default admin/admin).
- `DEEPSEEKER_API_KEY` is the key to your service — don't use a weak value.
- A token equals the account itself; never share it or paste it into someone else's dashboard.
- Automated use violates DeepSeek's terms; ban risk is yours. Don't run a public relay.
- Prefer binding to localhost (HOST=127.0.0.1); if you need remote access, put it behind a VPN or reverse proxy with HTTPS.

---

Questions? Open an [issue](https://github.com/1515asd/deeperseekerSncMax/issues) (English or Chinese both fine). Chinese version: [GUIDE.zh-CN.md](GUIDE.zh-CN.md).
