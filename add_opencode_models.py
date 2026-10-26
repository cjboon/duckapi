#!/usr/bin/env python3
"""Add Duck.ai models to the opencode provider that points at the local relay.

    python3 add_opencode_models.py PATH_TO_opencode.json [model-id ...]

Defaults to claude-haiku-4-5 and mistral-small-2603. Makes a timestamped backup first.
Plain JSON only: if the file has comments, nothing is changed.
"""
import json, shutil, sys, time
from pathlib import Path

if len(sys.argv) < 2:
    sys.exit(__doc__)
path = Path(sys.argv[1]).expanduser()
wanted = sys.argv[2:] or ["claude-haiku-4-5", "mistral-small-2603"]

try:
    cfg = json.loads(path.read_text(encoding="utf-8"))
except (OSError, ValueError) as e:
    sys.exit("cannot read %s as plain JSON (%s). Add the models by hand instead." % (path, e))

providers = cfg.get("provider") or {}
local = [k for k, v in providers.items()
         if any(h in str((v.get("options") or {}).get("baseURL", "")) for h in ("127.0.0.1", "localhost"))]
if not local:
    sys.exit("no provider with a localhost baseURL in %s. Providers found: %s" % (path, list(providers) or "none"))
key = local[0]

backup = path.with_name(path.name + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
shutil.copyfile(path, backup)
models = providers[key].setdefault("models", {})
for m in wanted:
    if m in models:
        print("already declared:", m)
        continue
    models[m] = {"name": "%s (Duck.ai)" % m, "limit": {"context": 12000, "output": 2000}}
    print("added:", m)
path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
print("provider '%s' updated; backup: %s" % (key, backup))
print("select in opencode with /models, or set \"model\": one of:")
for m in wanted:
    print("   %s/%s" % (key, m))
