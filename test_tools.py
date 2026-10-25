#!/usr/bin/env python3
"""Tool-calling smoke test for the relay.

Usage: python test_tools.py [N=5] [model]
Every run is a real Duck.ai request (slow, and bans are per-IP), so keep N small.
Part 1: does the model emit a valid tool call?   Part 2: does history round-trip?
"""
import json, os, sys, time, urllib.request

def cfg(key, default=""):
    if os.getenv(key):
        return os.getenv(key)
    try:
        for line in open(".env", encoding="utf-8"):
            line = line.strip()
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return default

N = int(sys.argv[1]) if len(sys.argv) > 1 else 5
MODEL = sys.argv[2] if len(sys.argv) > 2 else cfg("DUCKAI_MODEL", "gpt-5.6-luna")
URL = "http://127.0.0.1:%s/v1/chat/completions" % cfg("PORT", "8080")
KEY = cfg("DUCKAI_API_KEY")

TOOLS = [{"type": "function", "function": {
    "name": "read", "description": "Read a file from disk",
    "parameters": {"type": "object",
                   "properties": {"filePath": {"type": "string"}},
                   "required": ["filePath"]}}}]

def call(messages):
    body = json.dumps({"model": MODEL, "messages": messages, "tools": TOOLS}).encode()
    headers = {"Content-Type": "application/json"}
    if KEY:
        headers["Authorization"] = "Bearer " + KEY
    req = urllib.request.Request(URL, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())["choices"][0]

ok = 0
print("Part 1: tool-call emission (%d runs, model=%s)" % (N, MODEL))
for i in range(N):
    t0 = time.time()
    try:
        ch = call([{"role": "user", "content": "What do the install instructions in README.md say?"}])
        tcs = ch["message"].get("tool_calls") or []
        good = ch["finish_reason"] == "tool_calls" and tcs and \
            tcs[0]["function"]["name"] == "read" and \
            "filePath" in json.loads(tcs[0]["function"]["arguments"])
        shown = tcs[0]["function"] if tcs else (ch["message"].get("content") or "")[:80]
    except Exception as e:
        good, shown = False, "error: %s" % e
    ok += bool(good)
    print("  run %d: %s (%.1fs) %s" % (i + 1, "PASS" if good else "FAIL", time.time() - t0, shown))
print("  => %d/%d valid tool calls\n" % (ok, N))

rt, runs = 0, min(N, 3)
print("Part 2: history round-trip (%d runs)" % runs)
history = [
    {"role": "user", "content": "What do the install instructions in README.md say?"},
    {"role": "assistant", "content": None, "tool_calls": [{
        "id": "call_1", "type": "function",
        "function": {"name": "read", "arguments": json.dumps({"filePath": "README.md"})}}]},
    {"role": "tool", "tool_call_id": "call_1",
     "content": "Install: run `pip install duckapi-demo` then `duckapi start`."},
]
for i in range(runs):
    try:
        ch = call(history)
        text = ch["message"].get("content") or ""
        good = ch["finish_reason"] == "stop" and "pip install" in text
        shown = text[:80]
    except Exception as e:
        good, shown = False, "error: %s" % e
    rt += bool(good)
    print("  run %d: %s %s" % (i + 1, "PASS" if good else "FAIL", shown))
print("  => %d/%d grounded answers" % (rt, runs))
