"""
Gridiron Ask: a small Flask backend for the Gridiron Board.

POST /ask takes a plain-English question about one sport ("Who is favoured in
Bills vs Dolphins?"), finds the Kalshi markets that match it, re-prices those
markets live from Kalshi, and asks Claude to answer using only those numbers.

Why a backend: the Claude API key must stay secret, so it lives here as an
environment variable and never reaches the browser. The Kalshi data is public.

Endpoints
    GET  /health   liveness check, also used by the frontend to wake Render
    GET  /sports   the sports the board covers
    POST /ask      {"question": str, "sport": str} -> answer + markets
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from flask import Flask, jsonify, request
from flask_cors import CORS

# ---------------------------------------------------------------- config ---

SNAPSHOT_BASE = os.environ.get(
    "SNAPSHOT_BASE", "https://saichaudhry.github.io/kalshi-nfl-board/data")
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
USER_AGENT = "gridiron-ask/1.0 (CMU 15-113 coursework)"

SNAPSHOT_TTL = 10 * 60       # seconds a downloaded sport snapshot is reused
MAX_QUESTION = 300           # characters
MAX_MARKETS = 40             # markets sent to Claude and back to the page
MAX_GAMES = 4
RATE_LIMIT = 10              # /ask calls per client per minute

# Browsers only get responses from these origins. The regex also admits any
# localhost port so the page can be tested with `python3 -m http.server`.
DEFAULT_ORIGINS = "https://saichaudhry.github.io"
ALLOWED_ORIGINS = [o.strip() for o in
                   os.environ.get("ALLOWED_ORIGINS", DEFAULT_ORIGINS).split(",")
                   if o.strip()]
LOCAL_ORIGIN = re.compile(r"^http://(localhost|127\.0\.0\.1)(:\d+)?$")

app = Flask(__name__)
CORS(app, origins=ALLOWED_ORIGINS + [LOCAL_ORIGIN])


class ApiError(Exception):
    """An error with an HTTP status and a message that is safe to show users."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


@app.errorhandler(ApiError)
def handle_api_error(exc):
    return jsonify({"error": exc.message}), exc.status


@app.errorhandler(404)
def handle_404(_exc):
    return jsonify({"error": "No such endpoint. Try GET /health or POST /ask."}), 404


@app.errorhandler(405)
def handle_405(_exc):
    return jsonify({"error": "Wrong HTTP method for this endpoint."}), 405


@app.errorhandler(Exception)
def handle_unexpected(exc):
    app.logger.exception("Unhandled error: %s", exc)
    return jsonify({"error": "Something went wrong on the server."}), 500


# ------------------------------------------------------------ HTTP utils ---

def get_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# -------------------------------------------------------------- snapshot ---
# The Gridiron Board's GitHub Action publishes one JSON file per sport every
# hour. It already groups ~50,000 Kalshi markets by game and bet type, so the
# backend reuses it to decide WHICH markets a question is about, then asks
# Kalshi for the current prices of just those.

_cache = {}
_cache_lock = threading.Lock()


def cached(key, loader):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < SNAPSHOT_TTL:
            return hit[1]
    value = loader()
    with _cache_lock:
        _cache[key] = (now, value)
    return value


def load_index():
    def loader():
        try:
            return get_json(f"{SNAPSHOT_BASE}/index.json")
        except (urllib.error.URLError, ValueError, TimeoutError) as exc:
            raise ApiError(502, "Could not load the list of sports from the board.") from exc
    return cached("index", loader)


def sport_entries():
    return {s["key"]: s for s in load_index().get("sports", [])}


def load_sport(key):
    entry = sport_entries().get(key)
    if not entry:
        raise ApiError(400, f"Unknown sport '{key}'. Call GET /sports for the list.")

    def loader():
        try:
            return get_json(f"{SNAPSHOT_BASE}/{entry['file']}", timeout=30)
        except (urllib.error.URLError, ValueError, TimeoutError) as exc:
            raise ApiError(502, f"Could not load {entry['label']} markets from the board.") from exc
    return cached(f"sport:{key}", loader)


# ------------------------------------------------------------- retrieval ---

STOPWORDS = set("""a an and are at be best bet by can do does for from game games
how i if in is it me most of on or over please price odds show tell than that the
this to under vs versus what whats when which who whos will win wins with would
favourite chance chances odds likely probability
""".split())

BET_HINTS = {
    "spread": ("spread",), "cover": ("spread",),
    "total": ("total",), "points": ("total",), "over": ("total",), "under": ("total",),
    "moneyline": ("moneyline",), "ml": ("moneyline",),
    "prop": ("prop",), "props": ("prop",), "touchdown": ("prop",), "touchdowns": ("prop",),
    "td": ("prop",), "yards": ("prop",), "passing": ("prop",), "rushing": ("prop",),
    "receiving": ("prop",), "strikeouts": ("prop",), "hits": ("prop",), "rebounds": ("prop",),
    "assists": ("prop",), "goals": ("prop",),
}


# Kalshi titles say "Pro Football Champion", not "Super Bowl". Rewrite the
# common names for each championship so they match those titles.
PHRASES = {
    "super bowl": "champion", "world series": "champion", "stanley cup": "champion",
    "nba finals": "champion", "wnba finals": "champion", "natty": "champion",
    "national title": "champion", "win it all": "champion",
}
STEMS = {"championship": "champion", "champions": "champion", "champ": "champion",
         "title": "champion", "ring": "champion", "favored": "favourite",
         "favorite": "favourite", "favoured": "favourite"}


def words(text):
    text = (text or "").lower()
    for phrase, repl in PHRASES.items():
        text = text.replace(phrase, repl)
    return [STEMS.get(w, w) for w in re.findall(r"[a-z0-9']+", text)]


def team_aliases(teams):
    """Map abbreviation -> set of lowercase words that refer to that team."""
    out = {}
    for abbr, t in teams.items():
        names = {abbr.lower()}
        for field in ("name", "location", "nick", "short"):
            names.update(w for w in words(t.get(field)) if w not in STOPWORDS and len(w) > 2)
        out[abbr] = names
    return out


def score_text(tokens, text):
    hay = set(words(text))
    return sum(1 for t in tokens if t in hay)


# Championship series whose tickers fall outside the board's sport prefixes,
# so the snapshot never contains them. Fetched live from Kalshi on demand.
EXTRA_FUTURES = {"nfl": ["KXSB"]}


def live_futures(sport):
    out = []
    for series in EXTRA_FUTURES.get(sport, []):
        url = f"{KALSHI_BASE}/events?" + urllib.parse.urlencode(
            {"series_ticker": series, "status": "open", "with_nested_markets": "true", "limit": 5})
        try:
            events = get_json(url, timeout=10).get("events", [])
        except (urllib.error.URLError, ValueError, TimeoutError):
            continue
        for e in events:
            out.append({
                "title": e.get("title", ""), "subtitle": e.get("sub_title", ""),
                "markets": [{
                    "ticker": m["ticker"], "label": m.get("yes_sub_title") or m.get("subtitle"),
                    "typeLabel": "Champion", "last": to_cents(m.get("last_price_dollars")),
                    "bid": None, "ask": None, "volume": int(float(m.get("volume_fp") or 0)),
                } for m in e.get("markets", [])],
            })
    return out


def pick_markets(snapshot, question, sport=None):
    tokens = [w for w in words(question) if w not in STOPWORDS]
    token_set = set(tokens)
    aliases = team_aliases(snapshot.get("teams", {}))
    bet_types = {bt for t in tokens for bt in BET_HINTS.get(t, ())}

    def full_name_in_question(name):
        parts = [w for w in words(name) if w not in ("jr", "sr", "ii", "iii")]
        return bool(parts) and all(p in token_set for p in parts)

    scored = []
    for game in snapshot.get("games", []):
        s = 0
        for side in (game.get("away"), game.get("home")):
            if side and aliases.get(side, set()) & token_set:
                s += 5
        players = {m["player"] for m in game["markets"] if m.get("player")}
        s += 4 * sum(1 for p in players if full_name_in_question(p))
        if s:
            scored.append((s, "game", game))

    futures = list(snapshot.get("futures", []))
    if "champion" in token_set:
        futures = live_futures(sport) + futures
    for fut in futures:
        s = 3 * score_text(tokens, f"{fut.get('title', '')} {fut.get('subtitle', '')}")
        s += 4 * sum(1 for m in fut["markets"] if full_name_in_question(m.get("label")))
        if s:
            scored.append((s, "future", fut))

    if not scored:
        # No names matched: fall back to the busiest upcoming games so the
        # answer still has something real to talk about.
        games = sorted(snapshot.get("games", []),
                       key=lambda g: -sum(m.get("volume", 0) for m in g["markets"]))
        scored = [(1, "game", g) for g in games[:MAX_GAMES]]

    scored.sort(key=lambda x: -x[0])
    top = scored[0][0]
    groups = [x for x in scored if x[0] >= max(1, top // 2)][:MAX_GAMES]

    picked, seen = [], set()
    per_group = max(6, MAX_MARKETS // len(groups))
    for _score, kind, group in groups:
        markets = group["markets"]
        if kind == "game" and bet_types:
            # Keep the moneyline alongside the hinted bet types: it is the
            # clearest read on who is favoured.
            wanted = bet_types | {"moneyline"}
            narrowed = [m for m in markets if m.get("betType") in wanted]
            markets = narrowed or markets
        if kind == "game":
            # Always lead with the moneyline so "who is favoured" is answerable.
            markets = sorted(markets, key=lambda m: (m.get("betType") != "moneyline",
                                                     -m.get("volume", 0)))
        else:
            markets = sorted(markets, key=lambda m: -(m.get("last") or 0))
        title = group.get("title", "")
        if kind == "future" and group.get("subtitle"):
            title = f"{title} ({group['subtitle']})"
        for m in markets[:per_group]:
            if m["ticker"] in seen:
                continue
            seen.add(m["ticker"])
            picked.append({
                "ticker": m["ticker"],
                "group": title,
                "date": group.get("date"),
                "label": m.get("label"),
                "type": m.get("typeLabel"),
                "bid": m.get("bid"), "ask": m.get("ask"), "last": m.get("last"),
                "volume": m.get("volume", 0),
            })
    return picked[:MAX_MARKETS]


def to_cents(value):
    try:
        return round(float(value) * 100)
    except (TypeError, ValueError):
        return None


def refresh_live(markets):
    """Replace snapshot prices with current ones from Kalshi. Returns success."""
    if not markets:
        return False
    tickers = ",".join(m["ticker"] for m in markets)
    url = f"{KALSHI_BASE}/markets?" + urllib.parse.urlencode(
        {"tickers": tickers, "limit": 200})
    try:
        live = {m["ticker"]: m for m in get_json(url, timeout=10).get("markets", [])}
    except (urllib.error.URLError, ValueError, TimeoutError) as exc:
        app.logger.warning("Live refresh failed, using snapshot prices: %s", exc)
        return False
    for m in markets:
        lm = live.get(m["ticker"])
        if not lm:
            continue
        m["bid"] = to_cents(lm.get("yes_bid_dollars"))
        m["ask"] = to_cents(lm.get("yes_ask_dollars"))
        m["last"] = to_cents(lm.get("last_price_dollars"))
        m["volume"] = int(float(lm.get("volume_fp") or m["volume"] or 0))
        m["status"] = lm.get("status")
    return True


# ---------------------------------------------------------------- Claude ---

SYSTEM_PROMPT = """You are the analyst behind Gridiron Board, a page that shows \
Kalshi sports prediction markets. Answer the user's question using ONLY the \
market data provided. Each Kalshi contract pays $1 if it resolves YES, so a \
price of 62 cents means the market implies roughly a 62% chance. Quote the \
relevant prices and the implied probabilities, name the market you are reading \
from, and say plainly when the data does not cover the question. Keep it under \
150 words, plain text, no markdown headers. Describe what the market implies; \
do not tell the user what to bet."""


def ask_claude(question, sport_label, markets):
    """Returns (answer, warning). Never raises for API problems."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None, "The AI summary is off because ANTHROPIC_API_KEY is not set on the server. The live markets below are still accurate."
    try:
        import anthropic
    except ImportError:
        return None, "The anthropic package is not installed on the server."

    lines = []
    for m in markets:
        price = m["last"] if m["last"] is not None else m["ask"]
        lines.append(f"- [{m['group']}{' ' + m['date'] if m.get('date') else ''}] "
                     f"{m['type']}: {m['label']} | bid {m['bid']}c ask {m['ask']}c "
                     f"last {price}c | volume {m['volume']}")
    prompt = (f"Sport: {sport_label}\nQuestion: {question}\n\n"
              f"Market data (prices in cents):\n" + "\n".join(lines))

    try:
        client = anthropic.Anthropic(api_key=key, timeout=45, max_retries=1)
        msg = client.messages.create(
            model=MODEL, max_tokens=500, system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}])
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        return text.strip() or None, None
    except anthropic.AuthenticationError:
        return None, "The server's Claude API key was rejected. The live markets below are still accurate."
    except anthropic.RateLimitError:
        return None, "Claude is rate limited right now. Try again in a minute."
    except anthropic.APIError as exc:
        app.logger.warning("Claude call failed: %s", exc)
        return None, "Claude could not answer right now. The live markets below are still accurate."


# ------------------------------------------------------------ rate limit ---

_hits = {}
_hits_lock = threading.Lock()


def check_rate_limit():
    client = (request.headers.get("X-Forwarded-For") or request.remote_addr or "?").split(",")[0].strip()
    now = time.time()
    with _hits_lock:
        recent = [t for t in _hits.get(client, []) if now - t < 60]
        if len(recent) >= RATE_LIMIT:
            raise ApiError(429, "Too many questions. Please wait a minute and try again.")
        recent.append(now)
        _hits[client] = recent


# ------------------------------------------------------------- endpoints ---

@app.get("/")
@app.get("/health")
def health():
    return jsonify({"status": "ok", "model": MODEL,
                    "ai": bool(os.environ.get("ANTHROPIC_API_KEY"))})


@app.get("/sports")
def sports():
    index = load_index()
    return jsonify({
        "fetchedAt": index.get("fetchedAt"),
        "sports": [{"key": s["key"], "label": s["label"], "name": s.get("name"),
                    "markets": s.get("stats", {}).get("markets")}
                   for s in index.get("sports", [])],
    })


@app.post("/ask")
def ask():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ApiError(400, "Send a JSON body like {\"question\": \"...\", \"sport\": \"nfl\"}.")
    question = body.get("question")
    sport = body.get("sport", "nfl")
    if not isinstance(question, str) or not question.strip():
        raise ApiError(400, "Please type a question.")
    question = question.strip()
    if len(question) > MAX_QUESTION:
        raise ApiError(400, f"Questions are limited to {MAX_QUESTION} characters.")
    if not isinstance(sport, str):
        raise ApiError(400, "sport must be a string such as \"nfl\".")

    check_rate_limit()
    snapshot = load_sport(sport.lower())
    markets = pick_markets(snapshot, question, sport.lower())
    live = refresh_live(markets)
    answer, warning = ask_claude(question, snapshot.get("label", sport), markets)

    return jsonify({
        "question": question,
        "sport": snapshot.get("sport", sport),
        "answer": answer,
        "warning": warning,
        "live": live,
        "snapshotAt": snapshot.get("fetchedAt"),
        "model": MODEL if answer else None,
        "markets": markets,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="127.0.0.1", port=port, debug=True)
