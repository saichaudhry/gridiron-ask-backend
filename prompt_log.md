# Prompt log

**Tool:** Claude Code (terminal agent), model Claude Opus 5.5, with its advisor
reviewer. The backend itself calls Claude Sonnet 5 through the Anthropic API.

## Key prompts

1. **Choosing the project.** I pasted the full HW4 assignment into Claude Code
   and let it look at my existing projects. It proposed building on my HW3
   Kalshi board: a backend that answers questions about the markets, because
   the Claude key is a real secret that must stay server-side, while the
   Kalshi data is public.

2. **Scope.** The advisor pushed for one real endpoint (`POST /ask`) plus
   `/health`, following the assignment's warning that the platforms, not the
   code, are the hard part. `/sports` was added only to fill the dropdown.

3. **Degrade instead of fail.** "Make `/ask` work with no API key: return the
   markets with `answer: null` and a warning." This let every path be tested
   locally before a key existed, and keeps the page useful if the key is
   misconfigured on Render.

4. **Verify the API before using it.** Before writing the live refresh,
   Claude checked with curl that Kalshi's `GET /markets?tickers=a,b,c` accepts
   a comma-separated batch, and found that prices come back as dollar strings
   (`yes_bid_dollars`), not cents.

5. **Fixing retrieval from test output.** Testing real questions exposed three
   bugs that were then fixed:
   - "Josh Allen MVP odds" matched a game with a different Allen. Player
     matches now need the full name in the question.
   - "Who is favoured ... and what is the spread?" dropped the moneyline.
     The moneyline is now always kept.
   - "Super Bowl" and "World Series" matched nothing, because Kalshi titles
     say "Pro Football Champion". Common championship names are now mapped
     to "champion", and the Super Bowl series (`KXSB`), which the board's
     snapshot omits, is fetched live from Kalshi.

6. **Render cold starts.** "The free tier sleeps. Ping `/health` on page load,
   show a waking state, and use a long timeout." The frontend does all three.

7. **Local testing without editing the URL.** "Let `?api=` override the
   backend URL," which avoids the assignment's warning about pushing a
   frontend still pointed at localhost.

8. **CORS.** Allow only `https://saichaudhry.github.io`, plus any localhost
   port for testing. Opening the page from `file://` sends `Origin: null`,
   so the docs say to serve it with `python3 -m http.server`.

## Testing done

- curl against every error path: empty question, unknown sport, non-JSON
  body, over-long question, 404, 405, rate limit (429), and a rejected API key.
- CORS preflight from the Pages origin allowed, a foreign origin refused.
- Headless Chrome (Playwright) loading the real page against the local
  backend: status pill, sport list, empty-question validation, an example
  question end to end, no horizontal scroll at 390 px, and the error message
  when the backend is down.
