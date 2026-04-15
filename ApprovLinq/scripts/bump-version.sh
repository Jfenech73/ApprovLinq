#!/usr/bin/env bash
# Reads ApprovLinq/VERSION, increments the patch number, and writes it back.
# Automatically called by the pre-commit hook.

set -euo pipefail

VERSION_FILE="$(git rev-parse --show-toplevel)/ApprovLinq/VERSION"

if [ ! -f "$VERSION_FILE" ]; then
  echo "0.0.1" > "$VERSION_FILE"
  git add "$VERSION_FILE"
  exit 0
fi

current=$(cat "$VERSION_FILE" | tr -d '[:space:]')
IFS='.' read -r major minor patch <<< "$current"

patch=$((patch + 1))
new_version="${major}.${minor}.${patch}"

echo "$new_version" > "$VERSION_FILE"
echo "Version bumped: $current -> $new_version"
git add "$VERSION_FILE"
