# Gridiron Ask (backend)

A small Flask backend that lets people ask plain-English questions about
Kalshi sports prediction markets. It powers the **Ask the Board** page of
[Gridiron Board](https://saichaudhry.github.io/kalshi-nfl-board/), my HW3 project.

- **Frontend (GitHub Pages):** https://saichaudhry.github.io/kalshi-nfl-board/ask.html
  ([source](https://github.com/saichaudhry/kalshi-nfl-board), files `ask.html`, `js/ask.js`, `css/ask.css`)
- **Backend (Render):** https://gridiron-ask.onrender.com
- Built for CMU 15-113, HW4.

Ask "Who wins the Super Bowl?" or "Josh Allen MVP odds" and the backend:

1. loads the board's hourly snapshot for that sport and picks the markets the
   question is about (team names, full player names, bet types, championships),
2. re-prices those markets **live** from Kalshi's public API,
3. sends the question and those prices to **Claude**, which explains what the
   market implies, and
4. returns the answer and the markets as JSON.

Why this needs a backend: the Claude API key has to stay secret. Anything in
frontend JavaScript is visible to every visitor, so the key lives only here, as
an environment variable on Render.

## Endpoints

All responses are JSON. Errors always look like `{"error": "human-readable message"}`.

### `GET /health`

Liveness check. The frontend calls it on page load so Render's free tier
starts waking up while the user is still typing.

```json
{"status": "ok", "model": "claude-sonnet-5", "ai": true}
```

`ai` is `false` when no `ANTHROPIC_API_KEY` is configured.

### `GET /sports`

The sports the board covers, used to fill the frontend's dropdown.

```json
{"fetchedAt": "2026-09-26T17:29:42+00:00",
 "sports": [{"key": "nfl", "label": "NFL", "name": "Pro Football", "markets": 19301}, ...]}
```

### `POST /ask`

Request body:

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `question` | string | yes | 1 to 300 characters |
| `sport` | string | no | one of the keys from `/sports`; defaults to `nfl` |

Response (200):

```json
{
  "question": "Who wins the Super Bowl?",
  "sport": "nfl",
  "answer": "Buffalo and the Rams lead at about 12% each...",
  "warning": null,
  "live": true,
  "snapshotAt": "2026-09-26T17:31:09+00:00",
  "model": "claude-sonnet-5",
  "markets": [
    {"ticker": "KXSB-27-BUF", "group": "2027 Pro Football Champion (2026-27)",
     "date": null, "label": "Buffalo", "type": "Champion",
     "bid": 12, "ask": 13, "last": 12, "volume": 7656822, "status": "active"}
  ]
}
```

- Prices are in cents. A contract pays $1, so 12 cents means about a 12% implied chance.
- `live` is `true` when prices were re-fetched from Kalshi for this request,
  `false` when Kalshi was unreachable and snapshot prices were used instead.
- `answer` is `null` and `warning` explains why when Claude is unavailable
  (no key, key rejected, rate limited). The markets are still returned, so
  the page stays useful.

Errors:

| Status | When |
| --- | --- |
| 400 | body is not JSON, `question` is missing, empty, or over 300 characters, or `sport` is unknown |
| 404 / 405 | unknown path or wrong method |
| 429 | more than 10 questions per minute from one client |
| 502 | the board snapshot could not be downloaded |
| 500 | anything unexpected (logged on the server, generic message to the user) |

Try it:

```bash
curl https://gridiron-ask.onrender.com/health
curl -X POST https://gridiron-ask.onrender.com/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "Who wins the Super Bowl?", "sport": "nfl"}'
```

## How the frontend talks to the backend

The frontend is a static page on GitHub Pages
(`ask.html` + `js/ask.js` in the kalshi-nfl-board repo). It uses `fetch()`:

| When | Call | What the page does with the response |
| --- | --- | --- |
| page load | `GET /health` | shows "Server ready" or "Waking the server…" in the header; notes when the AI summary is off |
| page load | `GET /sports` | fills the sport dropdown (falls back to a built-in list if this fails) |
| user submits a question, or clicks an example chip | `POST /ask` | shows Claude's answer, any warning, whether prices are live, and a table of the markets used with bid, ask, implied probability and volume |

Error handling on the page:

- Empty or over-long questions are caught before any request is sent.
- Every request has a 75 second timeout, since Render's free tier can take
  about 50 seconds to wake. After 8 seconds the page says it may be waking up.
- Network failures, timeouts, and non-2xx responses show a red message. For
  non-2xx responses the message is the backend's own `error` text.
- All backend text is inserted with `textContent`, never `innerHTML`.

The backend URL is one constant, `DEFAULT_API`, in `js/ask.js`. For local
testing add `?api=http://127.0.0.1:5000` to the page URL instead of editing it,
so there is nothing to remember to change back before pushing.

## Running it locally

Needs Python 3.9 or newer.

```bash
git clone https://github.com/saichaudhry/gridiron-ask-backend.git
cd gridiron-ask-backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export ANTHROPIC_API_KEY=sk-ant-...     # optional; without it /ask returns markets and a warning
python app.py                            # http://127.0.0.1:5000
```

Check it:

```bash
curl http://127.0.0.1:5000/health
curl -X POST http://127.0.0.1:5000/ask -H "Content-Type: application/json" \
  -d '{"question": "Josh Allen MVP odds", "sport": "nfl"}'
```

Run the frontend against it, from a checkout of kalshi-nfl-board:

```bash
python3 -m http.server 8000
# open http://localhost:8000/ask.html?api=http://127.0.0.1:5000
```

Serve the page over `http://localhost` rather than opening the file directly.
A `file://` page sends `Origin: null`, which CORS rejects.

### Environment variables

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `ANTHROPIC_API_KEY` | for AI answers | none | Claude API key. Secret. |
| `ANTHROPIC_MODEL` | no | `claude-sonnet-5` | which Claude model answers |
| `ALLOWED_ORIGINS` | no | `https://saichaudhry.github.io` | comma-separated origins allowed by CORS; `localhost` on any port is always allowed |
| `SNAPSHOT_BASE` | no | the board's GitHub Pages `data/` folder | where sport snapshots are downloaded from |
| `PORT` | no | 5000 locally; Render sets it | port for `python app.py` |

`.env.example` lists them. If you prefer a file, copy it to `.env`, which is
gitignored, and load it with `set -a; source .env; set +a`.

## Deploying to Render

`render.yaml` describes the service, so deployment is:

1. Render dashboard, **New**, **Blueprint**, pick this repo.
2. When prompted, paste the Claude key into `ANTHROPIC_API_KEY`.
3. Deploy. Render runs `pip install -r requirements.txt` and starts
   `gunicorn app:app`. `/health` is the health check.

If Render assigns a URL other than `https://gridiron-ask.onrender.com`,
update `DEFAULT_API` in the frontend's `js/ask.js`.

## How secrets are handled

- The only secret is `ANTHROPIC_API_KEY`. It is read with `os.environ` on the
  server and set in the Render dashboard (`sync: false` in `render.yaml`
  means Render asks for it and never reads it from the repo).
- It never appears in this repo, in the frontend, or in any response. The
  browser only ever talks to this backend, never to Claude.
- `.gitignore` excludes `.env`, `.env.*`, `*.pem` and `*.key`.
- Kalshi market data is public, so no Kalshi key is used. This backend never
  touches Kalshi's account or portfolio endpoints.
- Abuse limits protect the key's budget: CORS only admits the GitHub Pages
  origin and localhost, questions are capped at 300 characters, each client
  gets 10 questions per minute, and Claude answers are capped at 500 tokens.
  CORS stops other websites, not scripts, so the rate limit is the real guard.

## Files

```
app.py            the whole backend: endpoints, market selection, live refresh, Claude call
requirements.txt  pinned dependencies
render.yaml       Render blueprint (build, start, env vars)
.python-version   Python version for Render
.env.example      variable names only, no values
prompt_log.md     AI tools and key prompts used
```
