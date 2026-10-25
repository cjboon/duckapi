"""Parsing of the tool-call XML envelope a chat model may emit.

Duck.ai is chat-only (no function-calling API). When a reply contains the
envelope, main.py turns it into a proper tool_use / tool_calls block so agents
can execute the call and send a tool_result back; otherwise the text flows
through unchanged. (Teaching the model the protocol is the caller's job;
intent-based synthesis lives in toolrouter.py.)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional

_TOOL_CALL_RE = re.compile(
    r"<tool_call\s+name=\"([^\"]+)\"\s*>(.*?)</tool_call>", re.DOTALL
)


@dataclass
class ToolCall:
    name: str
    input: dict


def parse_tool_call(text: str) -> Optional[ToolCall]:
    """Extract the first tool-call envelope from model output.

    Returns None if the model replied in plain text (no tool call).
    """
    m = _TOOL_CALL_RE.search(text)
    if not m:
        return None
    name = m.group(1)
    raw = m.group(2).strip()
    # strip accidental markdown fences
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
    try:
        inp = json.loads(raw)
    except json.JSONDecodeError:
        # best-effort: if not JSON, pass as a single "input" string
        inp = {"input": raw}
    if not isinstance(inp, dict):
        inp = {"input": raw}
    return ToolCall(name=name, input=inp)


def split_text_and_tool(text: str):
    """Split model output into (preamble_text, tool_call).

    preamble_text is any natural-language text before the tool envelope (may be "").
    """
    m = _TOOL_CALL_RE.search(text)
    if not m:
        return text.strip(), None
    preamble = text[: m.start()].strip()
    tc = parse_tool_call(text)
    return preamble, tc


# ---------------------------------------------------------------------------
# Multi-envelope parsing (agent upgrade, step 1).
# The original _TOOL_CALL_RE above (terminator `</tool_call>`) is untouched
# and stays the back-compat matcher. This section adds a documented alias for
# it so multiple envelopes per reply are parsed in order. The close tag
# terminates the payload safely: a `>` inside JSON (e.g. {"command":
# "echo a > b"}) cannot truncate it.
# ---------------------------------------------------------------------------
_TOOL_CALL_NL_RE = re.compile(
    r'<tool_call\s+name="([^"]+)"\s*>(.*?)</tool_call>', re.DOTALL
)


def _clean_payload(raw: str):
    """Return (name, args dict) for one envelope payload, or None when invalid."""
    payload = raw.strip()
    payload = re.sub(r"^```(?:json)?\s*|\s*```$", "", payload, flags=re.IGNORECASE)
    payload = payload.strip().rstrip(">").strip()
    try:
        inp = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(inp, dict):
        return None
    return inp


def parse_tool_calls(text: str) -> List[ToolCall]:
    """Extract every valid tool-call envelope, in the order they appear.

    Handles both terminator styles: `>` on its own line (preferred, safe for
    `>` characters inside JSON values) and the original module regex's bare
    first-`>` terminator (kept for back-compat). Invalid payloads are skipped
    rather than guessed at: a partial argument object would still run a tool.
    """
    matches = list(_TOOL_CALL_NL_RE.finditer(text))
    if not matches:
        matches = list(_TOOL_CALL_RE.finditer(text))

    calls: List[ToolCall] = []
    for m in matches:
        inp = _clean_payload(m.group(2))
        if inp is None:
            continue
        calls.append(ToolCall(name=m.group(1), input=inp))
    return calls


def split_text_and_tools(text: str) -> tuple:
    """Split model output into (preamble_text, [ToolCall, ...]).

    preamble_text is everything outside the envelopes (may be ""), with the
    envelope markup stripped so it can be shown as plain assistant content.
    """
    matches = list(_TOOL_CALL_NL_RE.finditer(text))
    if not matches:
        matches = list(_TOOL_CALL_RE.finditer(text))
    if not matches:
        return text.strip(), []
    calls: List[ToolCall] = []
    preamble = text
    for m in reversed(matches):
        inp = _clean_payload(m.group(2))
        if inp is not None:
            calls.append(ToolCall(name=m.group(1), input=inp))
        preamble = preamble[: m.start()] + preamble[m.end():]
    calls.reverse()  # restore original order
    return preamble.strip(), calls
