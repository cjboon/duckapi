#!/usr/bin/env bash
set -euo pipefail

visibility="--private"
case "${1:-}" in
  "") ;;
  --public) visibility="--public" ;;
  *)
    echo "Usage: $0 [--public]" >&2
    exit 2
    ;;
esac

command -v gh >/dev/null || { echo "Error: gh is not installed." >&2; exit 1; }
command -v git >/dev/null || { echo "Error: git is not installed." >&2; exit 1; }
gh auth status >/dev/null || { echo "Error: authenticate first with: gh auth login" >&2; exit 1; }

repo_name="$(basename "$PWD")"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git init -b main
fi

remote_name="gh-new"
if git remote get-url "$remote_name" >/dev/null 2>&1; then
  echo "Error: remote '$remote_name' already exists. Inspect it with: git remote -v" >&2
  exit 1
fi

git add -A

if ! git rev-parse --verify HEAD >/dev/null 2>&1 || ! git diff --cached --quiet; then
  git commit -m "Initial commit"
fi

gh repo create "$repo_name" "$visibility" \
  --source=. \
  --remote="$remote_name" \
  --push
