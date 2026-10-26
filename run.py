"""Bootstrap the server: check the port, build the venv, seed .env, run uvicorn.

Replaces the old start.bat/stop.bat, whose hardcoded interpreter path pointed at
another user's profile and so only worked by falling through to PATH.

Usage:
    python run.py                  # start, open the dashboard when healthy
    python run.py --no-browser     # start headless
    python run.py --port 9000
    python run.py --reload         # uvicorn autoreload (dev)
    python run.py --check          # setup + port check only, don't boot
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
LOG = ROOT / "server.log"
DEFAULT_PORT = 8080
MIN_PYTHON = (3, 10)  # enforced by check_python()


def venv_python() -> Path | None:
    """The venv interpreter, or None if the venv was never created."""
    exe = VENV / "Scripts" / "python.exe"
    if exe.exists():
        return exe
    exe = VENV / "bin" / "python"  # non-Windows layout
    return exe if exe.exists() else None


def _duckapi_env_path():
    from pathlib import Path
    return Path(__file__).resolve().parent / ".env"


def _env_value(name: str) -> str:
    """Lookup for DUCKAI_* settings: the process environment wins, then .env."""
    value = os.getenv(name, "").strip()
    if value:
        return value
    try:
        for line in _duckapi_env_path().read_text(encoding="utf-8").splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _duckapi_read_key():
    """The API key the server will use: the environment wins, then .env."""
    return _env_value("DUCKAI_API_KEY")


def _duckapi_ensure_key():
    """First-run setup: create .env and generate DUCKAI_API_KEY when there is none.

    Never overwrites a key that is already set. DUCKAI_AUTOKEY=0 disables it.
    """
    import os, re, secrets, shutil
    if os.getenv("DUCKAI_AUTOKEY", "1") == "0" or os.getenv("DUCKAI_API_KEY", "").strip():
        return
    path = _duckapi_env_path()
    if not path.exists():
        example = path.with_name("example.env")
        if example.exists():
            shutil.copyfile(example, path)
        else:
            path.write_text("", encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    m = re.search(r"^DUCKAI_API_KEY=(.*)$", text, re.M)
    if m and m.group(1).strip().strip('"').strip("'"):
        return
    key = secrets.token_urlsafe(32)
    if m:
        text = text[:m.start()] + "DUCKAI_API_KEY=" + key + text[m.end():]
    else:
        text = text.rstrip("\n") + ("\n" if text.strip() else "") + "DUCKAI_API_KEY=" + key + "\n"
    path.write_text(text, encoding="utf-8")
    print("  first run : generated DUCKAI_API_KEY in .env")


def _dashboard_url(port):
    """Dashboard URL with the key in the fragment (never sent to the server)."""
    from urllib.parse import quote
    key = _duckapi_read_key()
    base = f"http://localhost:{port}/"
    return base + "#key=" + quote(key, safe="") if key else base


# --- opencode registration (interactive merge; never writes without consent) ---
OPENCODE_EXAMPLE = ROOT / "opencode.duck.example.json"


def opencode_config_paths() -> list[Path]:
    """Where the user's opencode config may live, in priority order."""
    env_cfg = os.getenv("OPENCODE_CONFIG")
    if env_cfg:  # opencode honors this override; never fall back past it
        return [Path(env_cfg).expanduser()]
    xdg = os.getenv("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return [
        base / "opencode" / "opencode.json",
        base / "opencode" / "opencode.jsonc",
        Path.home() / ".opencode.json",
    ]


def _relay_provider(cfg: dict, port: int):
    """An existing provider entry pointed at THIS relay (same host and port)."""
    for key, prov in (cfg.get("provider") or {}).items():
        u = str(((prov or {}).get("options") or {}).get("baseURL", ""))
        if u and f":{port}" in u and any(h in u for h in ("//localhost", "//127.0.0.1")):
            return key
    return None


def opencode_sync(port: int = 8080) -> None:
    """Offer to register this relay in the user's opencode config.

    The provider block comes from opencode.duck.example.json (single source
    of truth). Merging means: create the 'duckapi' provider, or append the
    missing models to an existing provider already pointed at this relay.
    The .env key is embedded into the written block, so launching opencode
    needs no exported environment variable at all - and the offer recurs
    with consent when an already-embedded key no longer matches .env (e.g.
    after a key rotation). Placeholder-only, fully-synced configs keep the
    env-var pattern deliberately and stay silent.

    Consent comes before any write: the prompt says exactly what would
    change, [y/N] decides, and a non-interactive terminal only gets the
    manual command. Already-synced configs make the function a silent
    no-op, so booting the relay stays quiet. The user's other providers,
    their default model, and tool settings are never touched.
    """
    try:
        example = json.loads(OPENCODE_EXAMPLE.read_text(encoding="utf-8"))
        example_provider = (example.get("provider") or {}).get("duckapi") or {}
        wanted = example_provider.get("models") or {}
        if not wanted:
            return
    except (OSError, ValueError):
        return

    path = next((p for p in opencode_config_paths() if p.exists()), None)
    if path is None:
        return  # no opencode config anywhere: don't invent one

    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        print(f"  opencode  : {path} is not parseable JSON (comments?) -"
              " merge the relay's models in by hand (see opencode.duck.example.json)")
        return

    existing = _relay_provider(cfg, port)
    current_key = _env_value("DUCKAI_API_KEY")
    placeholder = "{env:DUCKAI_API_KEY}"
    if existing:
        models = cfg["provider"][existing].setdefault("models", {})
        missing = {m: spec for m, spec in wanted.items() if m not in models}
        api_key = str((cfg["provider"][existing].get("options") or {}).get("apiKey") or "")
        # Embed the .env key unless the config deliberately holds the env-var
        # pattern with nothing else pending - that user keeps their pattern.
        embed = bool(current_key) and api_key != current_key and (
            bool(missing) or api_key != placeholder)
        if not missing and not embed:
            return  # nothing to do; stay silent on every boot
        parts = []
        if missing:
            parts.append(f"add {', '.join(sorted(missing))} to provider '{existing}'")
        if embed:
            parts.append("set its apiKey from .env (so nothing needs exporting)")

        def merge():
            if missing:
                models.update(missing)
            if embed:
                cfg["provider"][existing].setdefault("options", {})["apiKey"] = current_key
        verb = " and ".join(parts)
    else:
        if current_key:
            verb = (f"register provider 'duckapi' ({', '.join(sorted(wanted))}) "
                    "with your .env key embedded (no environment export needed)")

            def merge():
                block = copy.deepcopy(example_provider)
                block.setdefault("options", {})["apiKey"] = current_key
                cfg.setdefault("provider", {})["duckapi"] = block
        else:
            verb = f"register provider 'duckapi' ({', '.join(sorted(wanted))}) (open access - no key in .env)"

            def merge():
                cfg.setdefault("provider", {})["duckapi"] = copy.deepcopy(example_provider)

    if not sys.stdin.isatty():
        print(f"  opencode  : {path} needs an update ({verb}) - run interactively")
        print("              to be asked, or merge by hand:")
        print(f"                python3 add_opencode_models.py {path}")
        return

    print(f"  opencode  : {path}")
    print(f"              would {verb} so opencode can reach this relay.")
    print("              nothing else in the file changes.")
    try:
        answer = input("              merge now? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer not in ("y", "yes"):
        print("  opencode  : skipped. merge later with:")
        print(f"                python3 add_opencode_models.py {path}")
        return

    backup = path.with_name(path.name + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
    shutil.copyfile(path, backup)
    merge()
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(f"  opencode  : merged into {path} (backup: {backup.name})")
    if current_key:
        print("              the .env key is embedded in the config now - nothing to export.")


_duckapi_ensure_key()


def port_busy(port: int, host: str = "127.0.0.1") -> bool:
    """True if something already accepts connections on `port`.

    Probes by connecting rather than parsing netstat: the question is "will
    uvicorn fail to bind", and only a real connect answers that.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def check_python() -> bool:
    """False plus a fix-it line when the running Python is older than MIN_PYTHON.

    Uses no 3.10-only *syntax* on purpose: annotations are deferred by the
    module-level `from __future__ import annotations`, but executable syntax
    is not - an old `python run.py` must reach this message instead of dying
    with a SyntaxError before anything is printed.
    """
    if sys.version_info >= MIN_PYTHON:
        return True
    floor = ".".join(map(str, MIN_PYTHON))
    print(f"DuckBridge needs Python {floor}+ - found {sys.version.split()[0]}.")
    print(f"Install a newer Python and rerun (Windows:  py -{floor} run.py).")
    return False


# Standard browser names on PATH and install locations per OS. The relay
# always drives the user's real Chrome/Chromium - never the browser bundled
# with Playwright (see duckai.py for why) - so a browser existing is part of
# setup, and a boot without one must say so plainly.
BROWSER_NAMES = ("chromium", "chromium-browser", "google-chrome",
                 "google-chrome-stable", "chrome")
BROWSER_PATHS = {
    "Linux": ("/usr/bin/chromium", "/usr/bin/chromium-browser",
              "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
              "/snap/bin/chromium", "/opt/google/chrome/google-chrome"),
    "Darwin": ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
               "/Applications/Chromium.app/Contents/MacOS/Chromium"),
    "Windows": (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files\Chromium\Application\chrome.exe"),
}


def find_browser() -> tuple[str, str, str]:
    """Where the browser Playwright will drive comes from. Read-only.

    In order: an explicit DUCKAI_CHROME_PATH (environment, then .env - and it
    must exist), then PATH, then standard install locations for this OS.
    Nothing writes anything. The relay still boots when nothing is found,
    because Playwright has its own channel="chrome" fallback at launch;
    the failure then surfaces on the first chat request, far less legibly.
    Returns (level, detail, hint): level is "ok" or "warn", detail is the
    one banner line, hint is plain multi-line text for warnings.
    """
    override = _env_value("DUCKAI_CHROME_PATH")
    if override:
        if os.path.exists(override):
            return "ok", f"{override} (DUCKAI_CHROME_PATH)", ""
        return "warn", f"{override} (DUCKAI_CHROME_PATH) does not exist", (
            "fix or remove DUCKAI_CHROME_PATH in .env; standard locations\n"
            "take over once it is gone.")
    found = ""
    for name in BROWSER_NAMES:
        found = shutil.which(name)
        if found:
            break
    if not found:
        for candidate in BROWSER_PATHS.get(platform.system(), ()):
            if os.path.exists(candidate):
                found = candidate
                break
    if found:
        return "ok", found + " (standard install)", ""
    return "warn", "no Google Chrome or Chromium found in standard locations", (
        "the relay still boots, but no chat can run without a browser;\n"
        "install Chrome or set DUCKAI_CHROME_PATH in .env (example.env).")


def ensure_venv() -> Path:
    """Return the venv interpreter, creating it from example.env's requirements
    on first run. Idempotent: an existing venv is reused untouched."""
    py = venv_python()
    if py:
        return py

    print("Creating venv and installing dependencies (first run only)...")
    subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
    py = venv_python()
    if not py:  # pragma: no cover - venv module just created it
        raise RuntimeError(f"venv created but no interpreter found under {VENV}")
    subprocess.run(
        [str(py), "-m", "pip", "install", "-q", "-r", str(ROOT / "requirements.txt")],
        check=True,
    )
    return py


def ensure_env() -> None:
    """Seed .env from example.env when absent. Never overwrites an existing one -
    it holds the user's API key and proxy settings."""
    env = ROOT / ".env"
    if env.exists():
        return
    shutil.copyfile(ROOT / "example.env", env)
    print(f"Created .env from example.env (open access, no API key set).")


def wait_healthy(port: int, timeout: float = 90.0) -> bool:
    """Poll /health until the server answers or `timeout` elapses.

    The dashboard is opened only after this passes, so the browser never lands
    on a connection-refused page during the ~10s prewarm.
    """
    url = f"http://127.0.0.1:{port}/health"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    return False


def serve(py: Path, port: int, reload: bool) -> int:
    """Run uvicorn as a child process, teeing its output to server.log and the
    console. A child (rather than uvicorn.run in-process) keeps Ctrl-C simple:
    the signal reaches uvicorn directly and this process just reaps it.
    """
    cmd = [
        str(py), "-m", "uvicorn", "main:app",
        "--host", os.getenv("DUCKAI_HOST", "127.0.0.1"), "--port", str(port),
    ]
    if reload:
        cmd.append("--reload")

    print(f"Starting uvicorn on port {port}...")
    print(f"  dashboard : http://localhost:{port}/")
    print("  api key   : stored in .env (DUCKAI_API_KEY); the dashboard tab opens with it filled in")
    print("  opencode  : provider merge embeds the .env key; nothing needs exporting")
    print("  key export: only for {env:...}-style client configs:")
    print("              export DUCKAI_API_KEY=$(grep '^DUCKAI_API_KEY=' .env | cut -d= -f2-)")
    print(f"  log file  : {LOG}")

    # Truncate rather than append: one run's log is one run's story, and a
    # stale tail from a crashed run is actively misleading while debugging.
    with LOG.open("w", encoding="utf-8", errors="replace") as log:
        proc = subprocess.Popen(
            cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                log.write(line)
                log.flush()
                sys.stdout.write(line)
                sys.stdout.flush()
            return proc.wait()
        except KeyboardInterrupt:
            print("\nShutting down...")
            proc.terminate()
            try:
                return proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Start the DuckAI2API server.")
    ap.add_argument("--port", type=int, default=int(os.getenv("PORT", DEFAULT_PORT)))
    ap.add_argument("--no-browser", action="store_true", help="don't open the dashboard")
    ap.add_argument("--reload", action="store_true", help="uvicorn autoreload (dev)")
    ap.add_argument("--check", action="store_true", help="setup + port check only, don't boot")
    args = ap.parse_args()

    if not check_python():
        return 1

    if port_busy(args.port):
        print(f"Port {args.port} is already in use - something else is listening there.")
        print(f"Close it first, or start on another port:  python run.py --port {args.port + 1}")
        return 1

    ensure_env()
    py = ensure_venv()
    print(f"Interpreter: {py}")

    browser_level, browser_detail, browser_hint = find_browser()
    print(f"  browser   : {browser_detail}")
    for hint_line in browser_hint.splitlines():
        print(f"              {hint_line}")

    if args.check:
        print(f"Port {args.port} is free. Setup OK.")
        return 0

    opencode_sync(args.port)

    # Open the dashboard only once the server actually answers, so the browser
    # never lands on a connection-refused page during the ~10s prewarm.
    if not args.no_browser and not args.reload:
        threading.Thread(
            target=lambda: (wait_healthy(args.port) and webbrowser.open(_dashboard_url(args.port))),
            daemon=True,
        ).start()

    return serve(py, args.port, args.reload)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
