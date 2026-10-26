#!/usr/bin/env bash
# patch_agent_loop.sh - stop the agent from claiming results early and repeating itself.
#
# Observed with opencode (see the debug/prompt dump):
#   * text written BEFORE a tool call ("Added a docstring...") was forwarded to the
#     client, stored in its history and replayed to the model, which then copied the
#     "claim + tool call" pattern. (patch_duckapi.sh introduced that forwarding.)
#   * the same read was requested twice in one reply;
#   * an identical patch was re-sent after it had already succeeded ("Patch failed").
#   * every session spends a Duck.ai message on a title.
#
# main.py:
#   1. The agent stream sends text only when there are no tool calls
#      (set DUCKAI_KEEP_PREAMBLE=1 to restore the old behaviour).
#   2. Identical calls inside one reply are collapsed; a repeat of a mutating call
#      (DUCKAI_NO_REPEAT_TOOLS, default patch,edit,write) that already ran earlier in
#      the conversation is dropped. If nothing is left, the turn ends with text.
#   3. opencode's "Reply with only the title" request is answered locally
#      (DUCKAI_LOCAL_TITLES=0 disables).
# toolrouter.py:
#   4. The closing reminder tells the model to stop after a confirmed result.
#
# Needs patch_duckapi.sh and patch_agent_prompt.sh applied first.
# Usage: ./patch_agent_loop.sh [repo_dir]
set -euo pipefail

cd "${1:-.}"
for f in main.py toolrouter.py; do
  [ -f "$f" ] || { echo "error: $f not found in $(pwd)" >&2; exit 1; }
done
command -v python3 >/dev/null || { echo "error: python3 is required" >&2; exit 1; }

BK=".patch-backup/agent-loop-$(date +%Y%m%d-%H%M%S)-$$"
mkdir -p "$BK"
cp main.py toolrouter.py "$BK"/
echo "backup: $BK"

RC=0
python3 - <<'PY' || RC=$?
import sys

results, files, crlf = [], {}, {}

def load(name):
    with open(name, encoding="utf-8", newline="") as fh:
        raw = fh.read()
    crlf[name] = "\r\n" in raw
    files[name] = raw.replace("\r\n", "\n")

def patch(name, label, old, new):
    text = files[name]
    if new in text:
        results.append(("SKIP", label, "already applied")); return
    n = text.count(old)
    if n != 1:
        results.append(("FAIL", label, "anchor found %d times (expected 1); file differs from the reviewed version" % n)); return
    files[name] = text.replace(old, new, 1)
    results.append(("OK", label, ""))

GROUP = []

def grp(name, label, old, new):
    """Queue an edit that only makes sense together with the others in the group."""
    GROUP.append((old, new))

def flush_group(name, label):
    """Apply every queued edit or none: a half-applied group (e.g. a call that passes a new
    argument to a function that does not accept it yet) would leave the file broken."""
    text = files[name]
    if all(new in text for _, new in GROUP):
        results.append(("SKIP", label, "already applied")); return
    for old, new in GROUP:
        if new in text:
            continue
        n = text.count(old)
        if n != 1:
            results.append(("FAIL", label, "anchor %r found %d times; the whole group was NOT applied" % (old.strip()[:60], n)))
            return
    for old, new in GROUP:
        if new not in text:
            text = text.replace(old, new, 1)
    files[name] = text
    results.append(("OK", label, "%d edits applied together" % len(GROUP)))

load("main.py"); load("toolrouter.py")

# ---- main.py helpers (config + filter + local titles), after compact_system ----------
HELPERS = r'''    return _AGENT_PREAMBLE


# --- agent loop guards (patch_agent_loop) ---------------------------------------------
LOCAL_TITLES = os.getenv("DUCKAI_LOCAL_TITLES", "1") != "0"
_NO_REPEAT = {t.strip() for t in os.getenv("DUCKAI_NO_REPEAT_TOOLS", "patch,edit,write").split(",") if t.strip()}


def _seen_calls(messages) -> set:
    """(tool name, canonical args) of every tool call already present in the history."""
    seen = set()
    for m in messages:
        for tc in (getattr(m, "tool_calls", None) or []):
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                continue
            seen.add((fn.get("name", ""), json.dumps(args, sort_keys=True, ensure_ascii=False)))
    return seen


def _filter_calls(calls, seen) -> list:
    """Collapse identical calls within one reply, and drop a repeat of a mutating call
    (patch/edit/write by default) that already ran earlier in the conversation."""
    kept, local = [], set()
    for tc in calls:
        key = (tc.name, json.dumps(tc.input, sort_keys=True, ensure_ascii=False))
        if key in local:
            continue
        local.add(key)
        if tc.name in _NO_REPEAT and key in (seen or ()):
            logger.warning("dropped repeated %s call (it already ran earlier in this conversation)", tc.name)
            continue
        kept.append(tc)
    return kept


def _is_title_request(prompt: str) -> bool:
    return "Reply with only the title" in prompt


def _local_title(prompt: str) -> str:
    import re
    m = re.search(r"<message>\s*(.*?)\s*</message>", prompt, re.S)
    words = (m.group(1) if m else prompt).split()[:6]
    title = " ".join(words).strip(" .:;,-\"'`")
    return title or "New session"


def _local_title_response(request, prompt: str):
    """Answer opencode's title request without spending a Duck.ai message."""
    title = _local_title(prompt)
    model = resolve_model(request.model)
    cid = _id()
    logger.info("LOCAL title %r (Duck.ai not called)", title)
    if request.stream:
        def chunk(delta, finish):
            return "data: " + json.dumps({
                "id": cid, "object": "chat.completion.chunk", "created": _created(), "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"

        async def gen():
            yield chunk({"role": "assistant", "content": ""}, None)
            yield chunk({"content": title}, None)
            yield chunk({}, "stop")
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")
    return {"id": cid, "object": "chat.completion", "created": _created(), "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": title}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}
'''
grp("main.py", "main: loop-guard helpers", "    return _AGENT_PREAMBLE\n", HELPERS)

# ---- main.py: use them in chat_completions ---------------------------------------------
grp("main.py", "main: local title + history of calls",
"    prompt = flatten_conversation(system, turns)\n",
'''    prompt = flatten_conversation(system, turns)
    if LOCAL_TITLES and not request.tools and _is_title_request(prompt):
        return _local_title_response(request, prompt)
    seen_calls = _seen_calls(request.messages)
''')

grp("main.py", "main: agent stream takes the call history",
"async def _openai_agent_stream(session: DuckAISession, prompt: str, model: str, chat_id: str, rewrite: Optional[dict] = None):\n",
"async def _openai_agent_stream(session: DuckAISession, prompt: str, model: str, chat_id: str, rewrite: Optional[dict] = None, seen=None):\n")

grp("main.py", "main: pass the history to the agent stream",
"_openai_agent_stream(session, agent_prompt, model, _id(), rewrite),\n",
"_openai_agent_stream(session, agent_prompt, model, _id(), rewrite, seen_calls),\n")

grp("main.py", "main: filter repeated calls in the agent stream",
'''    preamble, calls = split_text_and_tools("".join(buf))
    # Strict OpenAI clients expect the first chunk's delta to carry role.
''',
'''    preamble, calls = split_text_and_tools("".join(buf))
    if calls:
        kept = _filter_calls(calls, seen)
        if not kept:  # every call was a repeat: end the turn instead of looping
            preamble = preamble or "Stopped: the model repeated a tool call that had already run (see the earlier result)."
        calls = kept
    # Strict OpenAI clients expect the first chunk's delta to carry role.
''')

flush_group("main.py", "main: loop guards (helpers, call history, filter, local titles)")

# ---- main.py: revert the preamble forwarding added by patch_duckapi.sh ----------------
patch("main.py", "main: no text alongside tool calls (revert)",
'''        if preamble:
            yield f"data: {json.dumps(chunk_obj({'content': preamble}, None))}\\n\\n"
        for i, tc in enumerate(calls):
''',
'''        if preamble and os.getenv("DUCKAI_KEEP_PREAMBLE") == "1":  # off by default
            yield f"data: {json.dumps(chunk_obj({'content': preamble}, None))}\\n\\n"
        for i, tc in enumerate(calls):
''')

# ---- toolrouter.py: stop instruction ---------------------------------------------------
patch("toolrouter.py", "toolrouter: stop after a confirmed result",
'"project, emit a tool envelope; do not claim you lack access.\\n"',
'''"project, emit a tool envelope; do not claim you lack access. "
"Never describe a change as done before its [tool_result] confirms it, and never repeat a "
"tool call that already succeeded. Once a [tool_result] shows the task is complete, reply "
"with one short plain-text summary and NO tool_call.\\n"''')

for name, body in files.items():
    with open(name, "w", encoding="utf-8", newline="") as fh:
        fh.write(body.replace("\n", "\r\n") if crlf[name] else body)

w = max(len(r[1]) for r in results)
bad = 0
for s, label, note in results:
    print("  [%-4s] %-*s  %s" % (s, w, label, note))
    bad += s == "FAIL"
sys.exit(2 if bad else 0)
PY

if ! python3 -m py_compile main.py toolrouter.py; then
  cp "$BK"/main.py "$BK"/toolrouter.py .
  echo "error: patched files do not compile; originals restored from $BK" >&2
  exit 1
fi
echo "compile check: ok"

if [ "$RC" -ne 0 ]; then
  echo "Some edits FAILED (see [FAIL] lines). Successful ones were still applied; originals in $BK." >&2
  echo "Most likely cause: patch_duckapi.sh / patch_agent_prompt.sh were not both applied to main.py." >&2
  exit 2
fi
echo "done. Restart the relay."
