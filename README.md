# DuckBridge

**Duck.ai's free models as a local OpenAI- and Anthropic-compatible API — built for coding agents.**

Duck.ai has no public API. This project launches a real browser — your installed
Chromium or Chrome, driven by [Playwright](https://playwright.dev/) — operates
Duck.ai's ordinary web page, and exposes what comes back as standard `/v1`
endpoints. Any OpenAI- or Anthropic-compatible client (OpenCode, Claude Code,
the SDKs, plain `curl`) points at `http://127.0.0.1:8080` and talks to the
site's models as if they were an ordinary hosted API.

It also generates images, and ships a small web dashboard for status, logs,
and a playground.

This is unofficial automation of a consumer website. See
[Cautions](#cautions) before putting traffic through it.

---

## Why this exists

- Coding agents already speak the OpenAI and Anthropic protocols; Duck.ai
  speaks neither.
- Between them sits this relay: prompt flattening, tool-call bridging, browser
  sessions that stay alive per model, and session reuse that keeps multi-turn
  agents fast.
- The design goal throughout: make agent clients work *reliably*, not just
  demonstrably — envelopes must parse, repeats must not loop, history must
  round-trip, and failures must be diagnosable from logs rather than guessed.

---

## Quick start

```bash
git clone https://github.com/cjboon/duckbridge.git
cd duckbridge
python run.py
```

That is the whole install. On first run it creates a virtualenv, installs
`requirements.txt`, seeds `.env` from `example.env`, and generates a random
`DUCKAI_API_KEY` (unless one is already set, or `DUCKAI_AUTOKEY=0`). It then
starts uvicorn as a child process, tees output to `server.log` and the console,
and opens the dashboard once `/health` passes.

Smoke test (`export` is the same line `run.py` prints at startup):

```bash
export DUCKAI_API_KEY=$(grep '^DUCKAI_API_KEY=' .env | cut -d= -f2-)
curl -s http://127.0.0.1:8080/v1/chat/completions -H "Content-Type: application/json" -H "Authorization: Bearer $DUCKAI_API_KEY" -d '{"model":"gpt-5.6-luna","messages":[{"role":"user","content":"Say PONG"}]}'
```

---

## Using it with OpenCode

1. Start the relay (`python run.py`). At startup it checks the opencode
   config (`OPENCODE_CONFIG`, then `~/.config/opencode/opencode.json[c]`)
   and offers to merge in the relay's provider — naming exactly what would
   be added and asking `[y/N]`. Nothing is written without a yes; a
   timestamped backup is made first; only the relay's own provider/model
   entries are touched, the rest of the config stays as-is. Already-in-sync
   configs make this a silent no-op.
2. Launch opencode with the relay's key exported — the same line `run.py`
   prints at every start:

   ```bash
   export DUCKAI_API_KEY=$(grep '^DUCKAI_API_KEY=' /path/to/duckbridge/.env | cut -d= -f2-)
   ```
3. Pick a model with `/models` — `duckapi/gpt-5.6-luna` is the registered
   default. Opencode's other providers (including its free models) are
   untouched and remain available as usual.

The provider block this merge writes lives in
`opencode.duck.example.json`. To do it by hand instead — same result as the
startup prompt, plus a timestamped backup:

```bash
python3 add_opencode_models.py ~/.config/opencode/opencode.jsonc [model-id ...]
```

It appends any missing models to the provider whose `baseURL` points at the
relay's port, with right-sized limits.

Notes on the merged provider:

- `limit.context: 12000` reflects Duck.ai's composer cap (~12K characters).
  Keeping it honest stops the agent from stuffing more in than can be typed.
- The example disables no opencode tools. If a tool misbehaves through this
  relay, disable it selectively with, e.g.:
  `"tools": { "webfetch": false, "task": false, "todowrite": false,
  "todoread": false }` — those four have historically caused the most
  follow-up confusion here.

## Using it with Claude Code

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8080
export ANTHROPIC_AUTH_TOKEN=$DUCKAI_API_KEY
```

`/v1/messages` speaks the Anthropic Messages shape — streamed, with `tool_use`
content blocks for tool calls.

## Any OpenAI-compatible client

Base URL `http://127.0.0.1:8080/v1`, key from `.env`. Streaming and
non-streaming both work; tool calls arrive as ordinary `tool_calls` blocks.

---

## How it works

1. A request hits `/v1/chat/completions`, `/v1/messages`, or `/v1/responses`;
   its messages are flattened into a single role-labelled prompt. Duck.ai's
   page is one textarea, so context must arrive in the message body — the
   full conversation is re-sent every turn, all turns preserved.
2. A per-model browser session is created on demand: a real browser tab
   pointing at Duck.ai, launched headless with the binary at
   `DUCKAI_CHROME_PATH` (this deployment: `/usr/bin/chromium`; the code
   otherwise falls back to system Chrome). Untracked profile, `navigator.webdriver`
   hidden, a real user-agent.
3. The page is **warmed** until the site's own JS has finished booting — a
   readiness poll (composer present + `WARM_MIN` floor, `WARM_MAX` deadline)
   instead of a blind sleep.
4. The driver types the prompt into the composer through the page's own UI
   and clicks send. The site's app computes its challenge and request
   context itself; a `fetch` hook installed in the page tees the response as
   it streams (`clone().body.getReader()`), so tokens arrive as the page
   actually produces them.
5. **Session reuse**: a follow-up request's flattened prompt usually *extends*
   the previous one (stateless agents re-send full history each turn). When
   it does, only the delta is typed into the same live page — no re-warm, no
   re-sending history. Anything else falls back to a fresh warmed page with
   the full flattened prompt. Either way the model sees the whole
   conversation. Set `DUCKAI_NEW_CHAT=1` to force a fresh page per request.
6. Tool-call envelopes in the reply are parsed and re-shaped into strict
   OpenAI/Anthropic structures. That is the next section.

### Agent tool bridging

Agent clients demand function calling; Duck.ai has no tool-call API. Seven
cooperating parts bridge the gap:

1. **Protocol injection.** When a request advertises tools, a compact block
   is appended to the flattened prompt describing the envelope format:

   ```
   <tool_call name="ToolName">
   {"arg": "value"}
   >
   ```

   plus the client's real tool list (names, one-sentence descriptions, JSON
   schemas) and rules of use: only listed tools, exactly one JSON object per
   envelope, several envelopes may chain one step, stop once a
   `[tool_result]` answers the question. The block sits at the very end of
   the prompt so head+tail truncation (see [Limitations](#limitations))
   always keeps it in the live context.

2. **Envelope parsing.** Replies are scanned for envelopes and converted into
   strict OpenAI `tool_calls` / Anthropic `tool_use` blocks. The parser is
   tolerant on purpose: a missing close tag, fenced JSON, quote-style
   variants, and `>` characters inside JSON values all parse. Invalid
   payloads are skipped rather than guessed — a partial argument object
   would still run a tool.

3. **Buffered agent streams.** With tools present, the full answer is
   collected and parsed *before* anything is emitted, so a half-received
   envelope can never leak into the transcript as plain text. Cost: the
   first visible chunk arrives only after the model finishes its turn.
   Plain (toolless) requests stream token-by-token the whole way.

4. **Loop guards.** Agent clients re-send the whole history every turn, and
   models repeat themselves. The relay collapses identical calls within one
   reply, drops a repeat of a mutating call (`DUCKAI_NO_REPEAT_TOOLS`,
   default `patch,edit,write`) that already ran earlier in the conversation,
   suppresses pre-call chatter ("Added a docstring…") so the client's
   history stays clean (`DUCKAI_KEEP_PREAMBLE=1` to restore forwarding), and
   ends the turn with explanatory text if every emitted call was a repeat.

5. **Local session titles.** opencode's "Reply with only the title" request
   is answered locally with the first words of the message; no turn is spent
   on it (`DUCKAI_LOCAL_TITLES=0` to disable).

6. **Compact system prompts.** Agent clients ship ~10K-character system
   prompts that crowd out the 12K window. When tools are present, the relay
   swaps the system prompt for a short agent preamble plus the last
   `DUCKAI_SYSTEM_KEEP` (600) characters of the client's own prompt (where
   environment context usually sits). `DUCKAI_COMPACT_SYSTEM=0` to disable;
   `DUCKAI_TOOL_RESULT_CAP` (3000) truncates oversized tool results per turn.

7. **Intent routing (experimental, opt-in).** `DUCKAI_TOOL_ROUTING=1` adds a
   relay-side regex that synthesizes a `tool_calls` block straight from the
   user's phrasing ("run `cmd`", "read README.md") without calling Duck.ai at
   all. Off by default — a mis-hit yields `content: null`, which agents
   report as "no response from model" — so treat it as a per-deployment
   gamble.

---

## API

### OpenAI-compatible

| Method | Path | Notes |
|---|---|---|
| `POST` | `/v1/chat/completions` | Streaming and non-streaming, tool calls |
| `POST` | `/v1/responses` | OpenAI Responses shape, incl. `function_call` output items |
| `GET` | `/v1/models` | Live catalog (`/duckchat/v1/models`), static snapshot as fallback |
| `POST` | `/v1/images/generations` | See [Image generation](#image-generation) |
| `GET` | `/v1/images/content/{id}` | Fetch a generated image by id |

### Anthropic-compatible

| Method | Path | Notes |
|---|---|---|
| `POST` | `/v1/messages` | Anthropic Messages shape, streaming + `tool_use` |

### Dashboard

| Method | Path | Notes |
|---|---|---|
| `GET` | `/` | The dashboard |
| `GET` | `/api/status` | Uptime, sessions, page age, per-session state |
| `GET` | `/api/logs` | Ring-buffer tail, last 500 records |
| `POST` | `/api/chat` | Streaming playground chat (reports first-token vs total) |
| `POST` | `/api/images` | Playground image generation |
| `GET` | `/api/models` | Catalog for the UI |
| `POST` | `/api/shutdown` | Stop the server from the UI |
| `GET` | `/health` | Liveness probe |

Interactive docs at `/docs` while the server runs.

The dashboard's **Stop** button drains in-flight streams (it counts the last
`http.response.body` of each request via a pure-ASGI middleware, because
Starlette's own middleware releases *before* a StreamingResponse finishes and
would exit mid-answer), closes the browsers, then exits. It can stop the
server; nothing can start it — a page served by a dying process disappears
with it. Starting is `python run.py`.

---

## Models

Models resolve through a live catalog fetched from the public
`/duckchat/v1/models` (1-hour TTL), with a static list as fallback. Any alias
below works as input; the real id is what `/v1/models` returns.

| Model | Label |
|---|---|
| `gpt-5.6-luna` | GPT-5.6 Luna *(default)* |
| `gpt-5.6-terra` | GPT-5.6 Terra |
| `gpt-5.6-sol` | GPT-5.6 Sol |
| `gpt-5.4-mini` | GPT-5.4 mini |
| `claude-sonnet-4-6` | Claude Sonnet 4.6 |
| `claude-haiku-4-5` | Claude Haiku 4.5 |
| `claude-opus-4-8` | Claude Opus 4.8 |
| `mistral-small-2603` | Mistral Small 4 |
| `tinfoil/gpt-oss-120b` | gpt-oss 120B |
| `tinfoil/gemma4-31b` | Gemma 4 31B |

Aliases: `gpt-4o`, `gpt-4o-mini`, `o3-mini` → `gpt-5.4-mini`;
`claude-3-5-sonnet` → `claude-sonnet-4-6`; `claude-3-opus` →
`claude-opus-4-8`; `claude-3-haiku` → `claude-haiku-4-5`; `gpt-image-1`,
`gpt-image-2`, `dall-e-3` → `gpt-5.6-luna` (image generation). Unknown ids
pass through, so a new Duck.ai model works before this table is updated.

---

## Image generation

Duck.ai has no standalone image API. The relay drives an ordinary chat turn
that makes the model invoke its native **GenerateImage** tool (backend
"GPT Image 2", surfaced through `gpt-5.6-luna`), then returns the base64 JPEG
from the SSE success-frame.

```bash
curl http://127.0.0.1:8080/v1/images/generations \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $DUCKAI_API_KEY" \
  -d '{
    "model": "gpt-image-1",
    "prompt": "a red apple on a wooden table, soft natural light",
    "n": 1,
    "size": "1024x1024",
    "response_format": "url"
  }'
```

- `response_format: "url"` returns a stable local URL. A browser cannot
  attach an auth header to an `<img src>`, so `/v1/images/content/{id}` also
  accepts `?key=`; ids are unguessable uuid4 prefixes — a second path to the
  same secret, not a hole in it.
- `b64_json` returns the raw bytes inline.
- `n` is capped at 4; each image takes **15–20s**, generated sequentially.
- Images live in an in-memory cache (last 50). Restarting drops them.
- In the dashboard: switch the composer to **Image** mode — the model picker
  becomes a size picker and results render inline.

---

## Configuration

All settings live in `.env`, seeded from `example.env`, which documents each
one.

| Variable | Default | Meaning |
|---|---|---|
| `DUCKAI_API_KEY` | *(auto-generated)* | Bearer token for `/v1/*` and `/api/*`. Empty = open access |
| `DUCKAI_BASE` | `https://duck.ai` | Upstream host |
| `DUCKAI_CHROME_PATH` | *(auto)* | Browser binary (your Chromium or Chrome; falls back to system Chrome) |
| `DUCKAI_MODEL` | `gpt-5.6-luna` | Default model when the client omits one |
| `DUCKAI_NEW_CHAT` | `0` | `0` reuses the session (fast) · `1` = fresh chat per request |
| `DUCKAI_PREWARM` | `1` | Warm browser+page at startup instead of first request |
| `DUCKAI_WARM_MIN` | `2.0` | Hard floor for page warmup, seconds |
| `DUCKAI_WARM_MAX` | `7.0` | Deadline for the readiness poll, seconds |
| `DUCKAI_PROXY` | *(empty)* | Single proxy (`http`/`https`/`socks5`) |
| `DUCKAI_PROXIES` | *(empty)* | Proxy pool, comma-separated; rotates on rate-limit/ban responses |
| `DUCKAI_TOOL_ROUTING` | `0` | Experimental intent→tool synthesis |
| `DUCKAI_TOOL_RESULT_CAP` | `3000` | Max chars of a tool result fed back per turn |
| `DUCKAI_TOOL_BLOCK_CAP` | `6000` | Max chars of injected tool-protocol text |
| `DUCKAI_COMPACT_SYSTEM` | `1` | Swap agent system prompts for a short preamble |
| `DUCKAI_SYSTEM_KEEP` | `600` | Tail of the client system prompt to keep |
| `DUCKAI_LOCAL_TITLES` | `1` | Answer session-title requests locally |
| `DUCKAI_NO_REPEAT_TOOLS` | `patch,edit,write` | Mutating calls not allowed to repeat |
| `DUCKAI_KEEP_PREAMBLE` | `0` | `1` = forward text emitted before tool calls |
| `DUCKAI_DEBUG_PROMPT` | `0` | `1` = dump typed prompts to `debug/prompt-*.txt` |
| `DUCKAI_HOST` | `127.0.0.1` | Bind address |
| `PORT` | `8080` | Server port |

The server binds `127.0.0.1` for a reason: if you change `DUCKAI_HOST`, set
`DUCKAI_API_KEY` first. With an empty key the server is open to whatever can
reach it, and there is no rate limiting — you would be the one paying for
what runs through your IP. Proxy credentials are masked (`***@host:port`)
everywhere they are echoed — API responses, dashboard, and logs never see
them.

---

## Performance

Measured against Duck.ai previously:

| | Before | After |
|---|---|---|
| First token (cold) | 10.2s | **5.5s** |
| Per turn, session reuse | 10.3s | **~3s** |

Two changes get you there. **Warmup** polls until the page is genuinely ready
instead of sleeping a fixed 7s. **Session reuse** types only the delta of the
follow-up prompt, which is also why the composer cap is rarely hit in
practice. `DUCKAI_PREWARM=1` (default) moves browser startup into server
boot, so the very first caller skips the remaining setup. The dashboard's
chat tab shows live first-token vs total per turn — the numbers are visible,
not vibes.

---

## Debugging and diagnostics

- **`server.log`** — the durable record, teed to the console; truncated on
  each start, so one run's log is one run's story.
- **`/api/logs`** (or the **Logs** tab) — the last 500 records from an
  in-memory ring buffer, so a failure is inspectable in the browser.
- **`DUCKAI_DEBUG_PROMPT=1`** — dump exactly what gets typed into Duck.ai
  *after* truncation, to `debug/prompt-<timestamp>.txt`. These files contain
  whatever the client sent (file paths, project text) — review before
  sharing and delete when done.
- **`diag_duckai.py`** — what does Duck.ai show a fresh browser, same config
  as the relay? Loads the page, prints URL/dialogs/buttons/textarea state,
  saves `debug/diag.png`, sends nothing:

  ```bash
  .venv/bin/python diag_duckai.py            # visible window, waits for Enter
  .venv/bin/python diag_duckai.py --headless # report + screenshot only
  ```

  A consent/limit dialog or a greyed-out composer explains a "message box is
  disabled" failure in seconds instead of a hung send.
- **`test_tools.py`** — live smoke test against the running relay: does the
  model emit a valid tool call (`finish_reason: tool_calls`), and does
  tool-result history round-trip into a grounded answer:

  ```bash
  .venv/bin/python test_tools.py [N=5] [model]
  ```

  Every run is a real Duck.ai request — keep N small.
- **An error (418 / challenge) on every request, or an empty answer** —
  the site is refusing your IP or fingerprint. `diag_duckai.py` shows which
  case you're in. The supported mitigations: a proxy pool (`DUCKAI_PROXIES`)
  and, if a particular browser build is getting flagged, pointing
  `DUCKAI_CHROME_PATH` at a different one.
- **`.bak` files / `patch_*.sh` / `.patch-backup/`** — provenance, not code.
  The relay grew through small, idempotent, backup-first shell patches; the
  timestamped snapshots show how each behavior came to be. Keep or delete
  freely; nothing needs them.

---

## Cautions

- **Unofficial automation of a consumer site.** Automation of the ordinary
  web UI is the kind of thing terms of service tend to prohibit. Whether
  your use is permitted is between you and Duck.ai, not something this
  README can grant.
- **Refusals are part of the terrain.** Duck.ai can rate-limit or block an
  IP (observed as `418` / `ERR_CHALLENGE`-style errors), sometimes with no
  cooldown or `Retry-After` — and on shared IPs, someone else's volume can
  cause it. Proxy pools and a different browser binary are the mitigations
  this project ships; waiting is not.
- **The site is the product.** Duck.ai can change its page at any time, and
  this breaks on their schedule, not yours.
- **Don't expose it.** Binds `127.0.0.1`, no rate limiting, and a
  `DUCKAI_API_KEY` you should set before changing that.

---

## Limitations

- **One browser tab per model.** The tab holds live conversation state, so
  sends to the same model serialize; different models run in parallel.
- **Agent turns are buffered**, so "streaming" in tool mode arrives as one
  chunk after generation completes.
- **Prompt cap.** ~12,000 chars into the composer; long conversations are
  clamped head+tail with a visible `[context truncated]` marker. Usage
  counts in responses are zero — no upstream token accounting exists to
  copy.
- **Images are slow** — 15–20s each, sequential.

---

## Project layout

```
duckbridge/
├── run.py            # the only entry point: venv, deps, .env, uvicorn
├── main.py           # FastAPI app, /v1/* routes, agent-loop guards
├── duckai.py         # Playwright driver, model catalog, warmup, session reuse
├── tools.py          # tool-call envelope parsing (tolerant)
├── toolrouter.py     # protocol injection + experimental intent routing
├── dashboard.py      # dashboard + playground routes
├── dashboard.html    # the UI — pure HTML/CSS/JS, no build step
├── example.env       # documented config template
├── requirements.txt
├── diag_duckai.py    # fresh-browser diagnostic (same config as the relay)
├── test_tools.py     # live tool-bridge smoke test
├── add_opencode_models.py      # register relay models in an opencode config
├── opencode.duck.example.json  # example opencode provider snippet
└── patch_*.sh        # applied history patches (idempotent, backup-first)
```

---

## License

MIT — see [LICENSE](LICENSE). The MIT license covers the source code, and
nothing else. It is not a grant of rights from Duck.ai, and it cannot be.
Your use of Duck.ai through it is a matter of site terms and jurisdiction —
not something this project can license.
