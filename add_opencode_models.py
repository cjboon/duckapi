#!/usr/bin/env python3
"""Add Duck.ai models to the opencode provider that points at the local relay.

    python3 add_opencode_models.py PATH_TO_opencode.json [model-id ...] [--env]

Defaults to claude-haiku-4-5 and mistral-small-2603. Makes a timestamped backup first.
 Like the startup merge, the .env key is embedded into the provider block, so
opencode needs no exported environment variable. Pass --env to keep the
"{env:DUCKAI_API_KEY}" placeholder (the export-one-liner pattern) instead.
Plain JSON only: if the file has comments, nothing is changed.
"""
import json, shutil, sys, time
from pathlib import Path

args = [a for a in sys.argv[1:] if a != "--env"]
keep_placeholder = len(args) != len(sys.argv[1:])
if len(args) < 1:
    sys.exit(__doc__)
path = Path(args[0]).expanduser()
wanted = args[1:] or ["claude-haiku-4-5", "mistral-small-2603"]

try:
    cfg = json.loads(path.read_text(encoding="utf-8"))
except (OSError, ValueError) as e:
    sys.exit("cannot read %s as plain JSON (%s). Add the models by hand instead." % (path, e))

# The relay's own .env tells the helper its key and its port, so detection
# mirrors run.py's merge: a provider whose baseURL points at THIS relay.
env = {}
try:
    for line in (Path(__file__).resolve().parent / ".env").read_text(encoding="utf-8").splitlines():
        for name in ("DUCKAI_API_KEY", "PORT"):
            if line.startswith(name + "="):
                env[name] = line.split("=", 1)[1].strip().strip('"').strip("'")
except OSError:
    pass
env_key = env.get("DUCKAI_API_KEY", "")
port = env.get("PORT", "8080") or "8080"

providers = cfg.get("provider") or {}
local = [k for k, v in providers.items()
         if ":" + port in str((v.get("options") or {}).get("baseURL", ""))
         and any(h in str((v.get("options") or {}).get("baseURL", ""))
                 for h in ("127.0.0.1", "localhost"))]
if not local:
    sys.exit("no provider with a localhost:%s baseURL in %s. Providers found: %s" % (port, path, list(providers) or "none"))
name = local[0]

backup = path.with_name(path.name + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
shutil.copyfile(path, backup)
models = providers[name].setdefault("models", {})
for m in wanted:
    if m in models:
        print("already declared:", m)
        continue
    models[m] = {"name": "%s (Duck.ai)" % m, "limit": {"context": 12000, "output": 2000}}
    print("added:", m)

opts = providers[name].setdefault("options", {})
if keep_placeholder:
    if opts.get("apiKey") != "{env:DUCKAI_API_KEY}":
        opts["apiKey"] = "{env:DUCKAI_API_KEY}"
        print("--env: apiKey pointed at the {env:DUCKAI_API_KEY} placeholder")
elif env_key and opts.get("apiKey") != env_key:
    opts["apiKey"] = env_key
    print("apiKey: the .env key is embedded now (nothing to export)")
elif not env_key:
    print("apiKey left as-is: no DUCKAI_API_KEY in the relay .env (open access)")

path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
print("provider '%s' updated; backup: %s" % (name, backup))
print("select in opencode with /models, or set \"model\": one of:")
for m in wanted:
    print("   %s/%s" % (name, m))
