#!/usr/bin/env bash
# Run this once after cloning to install the git pre-commit version-bump hook.
# Usage: bash ApprovLinq/scripts/install-hooks.sh

set -euo pipefail

HOOKS_DIR="$(git rev-parse --show-toplevel)/.git/hooks"
HOOK_FILE="$HOOKS_DIR/pre-commit"
BUMP_SCRIPT="$(git rev-parse --show-toplevel)/ApprovLinq/scripts/bump-version.sh"

cat > "$HOOK_FILE" <<'EOF'
#!/usr/bin/env bash
# Auto-increment patch version on every commit.
SCRIPT="$(git rev-parse --show-toplevel)/ApprovLinq/scripts/bump-version.sh"
if [ -f "$SCRIPT" ]; then
  bash "$SCRIPT"
fi
EOF

chmod +x "$HOOK_FILE"
chmod +x "$BUMP_SCRIPT"
echo "Git pre-commit hook installed at $HOOK_FILE"
