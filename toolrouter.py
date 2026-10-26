"""Relay-side tool routing for Duck.ai (which has NO native tool-calling).

WHY THIS EXISTS
---------------
Duck.ai's web models (GPT/Claude free tier) never emit tool-call markup, no matter
how the prompt is phrased (verified across 10 live requests: 3 formats x 3 models).
This module bridges that gap WITHOUT executing tools on the server (the agent
already has filesystem/shell access and sandboxing):

  Agent --tools=[Read,Bash,...]--> relay detects intent --> returns tool_use block
  Agent executes locally, sends tool_result back
  relay injects tool_result as context --> Duck.ai answers grounded

The relay only SYNTHESIZES the tool_use block from the user's request; it never
runs the tool. This is the safe, agent-compatible shape.

INTENT PARSING
--------------
Agents (Claude Code / Codex / PI) phrase requests very literally ("Read the file
X", "run `cmd`", "find files matching *.go"). We match the latest user turn
against each registered tool's intent patterns and extract arguments. If nothing
matches, we return None and the request flows to Duck.ai as a normal chat.
"""
from __future__ import annotations

import os
import json
import logging
import re
from dataclasses import dataclass
from typing import List, Optional

# Tools this relay knows how to route. Names match Claude Code's built-ins so
# agents recognise them. Extend here to add coverage.
KNOWN_TOOLS = {
    "Read",
    "Write",
    "Edit",
    "Bash",
    "Glob",
    "Grep",
    "WebFetch",
}


@dataclass
class RoutedToolCall:
    name: str
    input: dict


# --- intent patterns (latest user turn -> (tool_name, arg_extractor)) ---

def _read_args(text: str) -> Optional[dict]:
    # "read README.md" / "show me the file /path/x" / "cat src/main.go"
    m = re.search(r'\b(?:read|cat|show|open|display|print|view)\b\s+(?:the\s+|me\s+)?(?:file\s+)?[`"\'"]?([^\s`"\'"]+\.\w+|/[^`"\'"\s]+)[`"\'"]?', text, re.IGNORECASE)
    if m:
        return {"file_path": m.group(1)}
    return None


def _write_args(text: str) -> Optional[dict]:
    # "write 'content' to path" / "create file path with ..."
    m = re.search(r'\bwrite\b[^`"\'"]*[`"\'"]?([^\s`"\'"]+\.\w+|/[^`"\'"\s]+)[`"\'"]?', text, re.IGNORECASE)
    if m:
        return {"file_path": m.group(1), "content": ""}
    return None
def _edit_args(text: str) -> Optional[dict]:
    m = re.search(r'\bedit\b[^`"\'"]*[`"\'"]?([^\s`"\'"]+\.\w+|/[^`"\'"\s]+)[`"\'"]?', text, re.IGNORECASE)
    if m:
        return {"file_path": m.group(1), "old_string": "", "new_string": ""}
    return None


def _bash_args(text: str) -> Optional[dict]:
    # fenced shell block: ```sh / ```bash ... ```
    m = re.search(r'```(?:sh|bash|shell|zsh|cmd)?\s*\n(.*?)```', text, re.DOTALL | re.IGNORECASE)
    if m:
        return {"command": m.group(1).strip()}
    # "run: cmd" / "execute cmd" / "run the command cmd" -> explicit prefix, take as-is
    m = re.search(r'\b(?:run|execute)\b\s*(?:the\s+)?(?:command\s+)?[:`"\'"]?\s*([^`"\'"\n]{2,300})', text, re.IGNORECASE)
    if m:
        cmd = m.group(1).strip().strip('`"\'"')
        if cmd:
            return {"command": cmd}
    return None




def _glob_args(text: str) -> Optional[dict]:
    # "find files matching **/*.py" / "glob **/*.py" / "list all *.md"
    # grab the first whitespace-delimited token after the keyword that looks
    # like a glob (contains * or / or is a *.ext pattern).
    m = re.search(
        r'\b(?:glob|find\s+files?\s+matching|list\s+(?:all\s+)?files?\s+matching)\b\s+(\S*[\*\/]\S*|\*\S*|\S+\.\w+)',
        text, re.IGNORECASE,
    )
    if m:
        return {"pattern": m.group(1)}
    return None

def _grep_args(text: str) -> Optional[dict]:
    # quoted: grep "TODO" in src/ | search for 'pattern' in path
    m = re.search(r'\b(?:grep|search\s+for|find)\b[^`"\'"]*?["\']([^"\']+)["\']', text, re.IGNORECASE)
    if not m:
        # unquoted: grab token(s) between the verb and "in/under/within"
        m = re.search(r'\b(?:grep|search\s+for)\b\s+([^\s`"\'"]+)(?:\s+(?:in|under|within)\s+([`"\'"]?[^\s`"\'"]+[`"\'"]?))?', text, re.IGNORECASE)
        if m:
            pattern = m.group(1)
            path = m.group(2)
            args = {"pattern": pattern}
            if path:
                args["path"] = path.strip('`"\'"')
            return args
    if m:
        pattern = m.group(1)
        path_m = re.search(r'\b(?:in|under|within)\s+([`"\'"]?[^\s`"\'"]+[`"\'"]?)', text, re.IGNORECASE)
        args = {"pattern": pattern}
        if path_m:
            args["path"] = path_m.group(1).strip('`"\'"')
        return args
    return None


def _webfetch_args(text: str) -> Optional[dict]:
    # "fetch https://..." / "web fetch url"
    m = re.search(r'(https?://[^\s`"\'")\]]+)', text)
    if m and re.search(r'\b(?:fetch|web\s*fetch|open\s+url|visit)\b', text, re.IGNORECASE):
        return {"url": m.group(1)}
    return None


_INTENTS = {
    "Read": _read_args,
    "Write": _write_args,
    "Edit": _edit_args,
    "Bash": _bash_args,
    "Glob": _glob_args,
    "Grep": _grep_args,
    "WebFetch": _webfetch_args,
}


def _last_user_text(messages: List[dict]) -> str:
    """Extract the most recent user-role text from an Anthropic/OpenAI message list."""
    for m in reversed(messages):
        role = m.get("role", "")
        if role not in ("user", "tool"):
            continue
        content = m.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_result":
                        # a tool_result turn means the loop already executed; skip routing
                        return ""
                elif isinstance(block, str):
                    parts.append(block)
            return " ".join(parts)
        return ""
    return ""


def route_intent(messages: List[dict], tools: List[dict]) -> Optional[RoutedToolCall]:
    """Decide whether the latest user request maps to a registered tool call.

    `tools` is the client's tool list (Anthropic or OpenAI shape). Only tools whose
    name is in KNOWN_TOOLS and is actually offered by the client are routed.
    Returns a RoutedToolCall, or None to fall through to normal chat.
    """
    if not tools:
        return None
    client_tool_names = set()
    for t in tools:
        name = None
        if t.get("type") == "function" and isinstance(t.get("function"), dict):
            name = t["function"].get("name")
        else:
            name = t.get("name")
        if name:
            client_tool_names.add(name)

    text = _last_user_text(messages)
    if not text.strip():
        return None

    # Try known tools in priority order (Bash last so Read/Glob win on overlap).
    order = ["Read", "Glob", "Grep", "Write", "Edit", "WebFetch", "Bash"]
    for name in order:
        if name not in client_tool_names or name not in _INTENTS:
            continue
        args = _INTENTS[name](text)
        if args:
            return RoutedToolCall(name=name, input=args)
    return None


def has_tool_result(messages: List[dict]) -> bool:
    """True when the client already returned a tool_result (loop is mid-execution)."""
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "")
        if role == "tool":
            return True
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    return True
    return False


# ---------------------------------------------------------------------------
# Protocol injection (agent upgrade, step 2).
# Duck.ai models cannot call tools natively. When an agent client advertises
# tools, we append a compact protocol block to the END of the flattened
# prompt: duckai._clamp_prompt keeps the head 10% and tail 90%, so protocol
# instructions placed last always survive into the live context. The actual
# envelopes the model emits are parsed by tools.parse_tool_calls (step 3
# converts them into client-shaped tool_calls).
# ---------------------------------------------------------------------------

CLOSE_TAG = "<" + "/" + "tool_call" + ">"


def _first_sentence(text: str, cap: int = 140) -> str:
    """Compact a tool description to one colon-free, fence-free sentence."""
    s = " ".join(str(text or "").split())
    for cut in (". ", ".\n", "! ", "? "):
        idx = s.find(cut)
        if 0 < idx < cap:
            s = s[: idx + 1]
            break
    if len(s) > cap:
        s = s[: cap - 1].rstrip() + "…"
    return s.replace('"', "'")


def _tool_entry(t: dict) -> dict:
    """Normalize OpenAI ({type:function,...}) and Anthropic ({name,...}) shapes."""
    fn = t.get("function") if isinstance(t.get("function"), dict) else t
    name = fn.get("name")
    schema = fn.get("parameters") if "parameters" in fn else fn.get("input_schema")
    return {"name": name, "desc": fn.get("description", ""), "schema": schema}


def _render_tool(entry: dict) -> str:
    lines = [f"- {entry['name']}: {_first_sentence(entry['desc'])}"]
    schema = entry.get("schema")
    if isinstance(schema, dict) and schema:
        try:
            lines.append(
                "  schema: " + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
            )
        except (TypeError, ValueError):
            lines.append("  schema: <unrenderable>")
    return "\n".join(lines)


def render_tool_block(tools: list) -> str:
    """Compact 'Tools' + protocol section for appending to a Duck.ai prompt."""
    entries = [_tool_entry(t) for t in (tools or [])]
    entries = [e for e in entries if e["name"]]
    if not entries:
        return ""
    rendered, total = [], 0
    cap = int(os.getenv("DUCKAI_TOOL_BLOCK_CAP", "6000"))
    for e in entries:
        chunk = _render_tool(e)
        if total + len(chunk) > cap:
            break
        rendered.append(chunk)
        total += len(chunk)
    body = "\n".join(rendered)
    # [patch_toolrouter] log omitted tools
    if len(rendered) < len(entries):
        logging.getLogger("duckai2api").warning(
            "tool block capped at %d chars: %d of %d tools omitted (%s)",
            cap, len(entries) - len(rendered), len(entries),
            ", ".join(str(e["name"]) for e in entries[len(rendered):]),
        )
    note = "  (some tools omitted for space)\n" if len(rendered) < len(entries) else ""
    return (
        "## Using tools\n\n"
        "You ARE connected to the user's real project through the tools listed below. "
        "You CAN read, search and modify files and run commands, but only by emitting "
        "tool envelopes: the caller executes each one and returns a [tool_result]. "
        "Never say you cannot access files or lack tools; emit an envelope instead. "
        "To use a tool, reply with "
        f"envelopes in EXACTLY this format:\n\n"
        f'<tool_call name="TOOL_NAME">\n'
        '{"argument": "value"}\n'
        f"{CLOSE_TAG}\n\n"
        "Rules:\n"
        "- Use ONLY the tools listed below, with the exact argument names shown.\n"
        "- The inside of each envelope must be exactly one valid JSON object.\n"
        "- You may emit several envelopes in one reply to chain steps.\n"
        "- No commentary inside envelopes; keep any preface text before the first one.\n"
        "- If the latest [tool_result] already answers the user, reply directly "
        "and stop using envelopes.\n"
        "- If no tool is needed, just answer normally with no envelope.\n\n"
        "Tools:\n"
        f"{body}\n{note}\n"
        "Reminder: you have these tools right now. To look at or change anything in the "
        "project, emit a tool envelope; do not claim you lack access. "
"Never describe a change as done before its [tool_result] confirms it, and never repeat a "
"tool call that already succeeded. Once a [tool_result] shows the task is complete, reply "
"with one short plain-text summary and NO tool_call.\n"
    )


def build_tool_aware_prompt(base_prompt: str, tools: list) -> str:
    """base conversation prompt + tool-protocol block (empty block = unchanged)."""
    block = render_tool_block(tools)
    if not block:
        return base_prompt
    return base_prompt.rstrip() + "\n\n" + block.rstrip() + "\n"
