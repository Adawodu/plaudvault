#!/usr/bin/env bash
# Keep the product bible's generated sections in step with the repository.
#
# post-commit rather than pre-commit on purpose: the generated blocks describe commits
# that have already happened, and a pre-commit hook that rewrites a tracked file mid-commit
# either amends behind your back or leaves the tree dirty. This amends the commit it just
# observed, only when the docs actually changed, and only when that is safe to do.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK="$ROOT/.git/hooks/post-commit"

cat > "$HOOK" <<'EOF'
#!/usr/bin/env bash
# installed by scripts/install-hooks.sh — regenerates docs/PRODUCT-BIBLE.md
set -uo pipefail

# Guard against recursion: our own `git commit --amend` re-fires this hook.
[ -n "${PLAUDVAULT_DOCS_SYNC:-}" ] && exit 0
export PLAUDVAULT_DOCS_SYNC=1

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT" || exit 0

PY="$ROOT/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3 || true)"
[ -x "$PY" ] || exit 0

"$PY" scripts/sync-docs.py >/dev/null 2>&1 || exit 0
git diff --quiet -- docs/PRODUCT-BIBLE.md && exit 0

# Never amend something that is already published, mid-rebase, or mid-merge.
if [ -d "$ROOT/.git/rebase-merge" ] || [ -d "$ROOT/.git/rebase-apply" ] \
   || [ -f "$ROOT/.git/MERGE_HEAD" ] || [ -f "$ROOT/.git/CHERRY_PICK_HEAD" ]; then
  echo "  [docs] bible refreshed but left unstaged (rebase/merge in progress)"
  exit 0
fi
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if git rev-parse --verify --quiet "origin/$BRANCH" >/dev/null; then
  if git merge-base --is-ancestor HEAD "origin/$BRANCH" 2>/dev/null; then
    echo "  [docs] bible refreshed but left unstaged (HEAD is already on origin/$BRANCH)"
    exit 0
  fi
fi

git add docs/PRODUCT-BIBLE.md
git commit --amend --no-edit --no-verify >/dev/null
echo "  [docs] PRODUCT-BIBLE.md refreshed and folded into this commit"
EOF

chmod +x "$HOOK"
echo "installed $HOOK"
echo
echo "  After every commit the bible's generated blocks are rebuilt and amended in."
echo "  Hand-written sections are untouched — only text inside BEGIN/END markers moves."
echo "  Remove with:  rm $HOOK"
