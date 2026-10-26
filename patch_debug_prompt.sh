#!/usr/bin/env bash
# patch_debug_prompt.sh - opt-in dump of the exact text the relay types into Duck.ai.
#
# Off by default. Start the relay with  DUCKAI_DEBUG_PROMPT=1 python3 run.py  and every
# request writes debug/prompt-<timestamp>.txt (the final, post-truncation prompt).
# The files contain whatever the client sent (file paths, project text), so review them
# before sharing and delete them when done.
#
# Usage: ./patch_debug_prompt.sh [repo_dir]
set -euo pipefail

cd "${1:-.}"
[ -f duckai.py ] || { echo "error: duckai.py not found in $(pwd)" >&2; exit 1; }
command -v python3 >/dev/null || { echo "error: python3 is required" >&2; exit 1; }

BK=".patch-backup/debug-prompt-$(date +%Y%m%d-%H%M%S)-$$"
mkdir -p "$BK"
cp duckai.py "$BK"/
echo "backup: $BK"

RC=0
python3 - <<'PY' || RC=$?
import sys

with open("duckai.py", encoding="utf-8", newline="") as fh:
    raw = fh.read()
crlf = "\r\n" in raw
text = raw.replace("\r\n", "\n")

OLD = "        await ta.fill(prompt)\n"
NEW = '''        if os.getenv("DUCKAI_DEBUG_PROMPT") == "1":  # opt-in debugging aid
            try:
                import time as _time
                os.makedirs("debug", exist_ok=True)
                _path = "debug/prompt-%d.txt" % int(_time.time() * 1000)
                with open(_path, "w", encoding="utf-8") as _fh:
                    _fh.write(prompt)
                logger.warning("DUCKAI_DEBUG_PROMPT: wrote %s (%d chars)", _path, len(prompt))
            except OSError:
                pass
        await ta.fill(prompt)
'''
if "DUCKAI_DEBUG_PROMPT" in text:
    print("  [SKIP] debug prompt dump  already applied"); code = 0
elif text.count(OLD) != 1:
    print("  [FAIL] debug prompt dump  anchor `await ta.fill(prompt)` found %d times (expected 1)" % text.count(OLD)); code = 2
else:
    text = text.replace(OLD, NEW, 1)
    with open("duckai.py", "w", encoding="utf-8", newline="") as fh:
        fh.write(text.replace("\n", "\r\n") if crlf else text)
    print("  [OK  ] debug prompt dump"); code = 0
sys.exit(code)
PY

if ! python3 -m py_compile duckai.py; then
  cp "$BK"/duckai.py duckai.py
  echo "error: patched duckai.py does not compile; original restored from $BK" >&2
  exit 1
fi
echo "compile check: ok"
[ "$RC" -eq 0 ] || exit 2
echo "done. Use:  DUCKAI_DEBUG_PROMPT=1 python3 run.py   (then send ONE request; read debug/prompt-*.txt)"
