#!/usr/bin/env bash
set -euo pipefail

config_root="${XDG_CONFIG_HOME:-$HOME/.config}/chromium"
cache_root="${XDG_CACHE_HOME:-$HOME/.cache}/chromium"

close_chromium() {
  local names=(chromium chromium-browser)
  local name running

  # Ask Chromium to close cleanly.
  for name in "${names[@]}"; do
    if pgrep -u "$UID" -x "$name" >/dev/null; then
      echo "Closing Chromium..."
      pkill -TERM -u "$UID" -x "$name" || true
    fi
  done

  # Wait up to 10 seconds for it to exit.
  for ((i = 0; i < 10; i++)); do
    running=0
    for name in "${names[@]}"; do
      if pgrep -u "$UID" -x "$name" >/dev/null; then
        running=1
      fi
    done
    (( running == 0 )) && return 0
    sleep 1
  done

  # Force-close any remaining Chromium processes.
  for name in "${names[@]}"; do
    if pgrep -u "$UID" -x "$name" >/dev/null; then
      echo "Chromium did not close; force-closing remaining processes..."
      pkill -KILL -u "$UID" -x "$name" || true
    fi
  done
}

printf 'This will close Chromium if it is running and permanently delete:\n'
printf '  %s\n' "$cache_root" "$config_root"
printf 'This removes all Chromium profiles and data, including cookies, history,\n'
printf 'bookmarks, Local Storage, saved passwords, extensions, and settings.\n'
read -r -p "Continue? [y/N] " reply
[[ "$reply" =~ ^[Yy]$ ]] || exit 0

close_chromium

if [[ -d "$cache_root" ]]; then
  rm -rf -- "$cache_root"
  echo "Removed: $cache_root"
fi

if [[ -d "$config_root" ]]; then
  rm -rf -- "$config_root"
  echo "Removed: $config_root"
fi

echo "Done. Chromium will create fresh data the next time it starts."

