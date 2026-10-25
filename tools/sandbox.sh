#!/usr/bin/env bash
# Build a throwaway playground to try team-sync with real Claude Code sessions:
# a bare "origin", two clones (two "developers") and an extra worktree.
#
#   bash tools/sandbox.sh [dir]        (default: ./.sandbox)
#
# Works with macOS bash 3.2. Needs git and python3.
set -euo pipefail

TOOL_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLUGIN="$TOOL_ROOT/plugins/team-sync"
DIR="${1:-$TOOL_ROOT/.sandbox}"

if [ -e "$DIR" ]; then
  echo "$DIR already exists. Remove it first (rm -rf \"$DIR\") or pass another directory." >&2
  exit 1
fi
mkdir -p "$DIR"
DIR="$(cd "$DIR" && pwd)"

git init -q --bare "$DIR/origin.git"
git -C "$DIR/origin.git" symbolic-ref HEAD refs/heads/main

SEED="$DIR/seed"
git init -q "$SEED"
git -C "$SEED" checkout -q -b main
git -C "$SEED" config user.email seed@example.com
git -C "$SEED" config user.name seed
mkdir -p "$SEED/src/auth" "$SEED/src/api" "$SEED/src/ui"
printf 'export function login() {}\n' > "$SEED/src/auth/login.ts"
printf 'export function getUser() { throw new Error("nope") }\n' > "$SEED/src/auth/user.ts"
printf 'export const routes = []\n' > "$SEED/src/api/routes.ts"
printf 'export function LoginForm() {}\n' > "$SEED/src/ui/LoginForm.tsx"
printf '# Sandbox project\n' > "$SEED/README.md"
cat > "$SEED/.team-sync.json" <<'EOF'
{
  "remote": "origin",
  "fetch_interval_sec": 15,
  "overlap_action": "ask"
}
EOF
git -C "$SEED" add -A
git -C "$SEED" commit -q -m "sandbox project"
git -C "$SEED" remote add origin "$DIR/origin.git"
git -C "$SEED" push -q origin main
rm -rf "$SEED"

clone() {
  git clone -q "$DIR/origin.git" "$DIR/$1"
  git -C "$DIR/$1" config user.email "$1@example.com"
  git -C "$DIR/$1" config user.name "$1"
}
clone sanya
clone anna
git -C "$DIR/sanya" worktree add -q -b tests "$DIR/sanya-tests"

cat <<EOF

Sandbox ready in $DIR

Open three terminals:

  # developer 1, agent "frontend"
  cd "$DIR/sanya" && CLAUDE_TEAM_ROOM=hackathon CLAUDE_TEAM_AGENT=frontend claude --plugin-dir "$PLUGIN"

  # developer 1, second agent "tests" (same machine, other worktree)
  cd "$DIR/sanya-tests" && CLAUDE_TEAM_ROOM=hackathon CLAUDE_TEAM_AGENT=tests claude --plugin-dir "$PLUGIN"

  # developer 2, agent "api" (other 'machine': separate clone, syncs through origin.git)
  cd "$DIR/anna" && CLAUDE_TEAM_ROOM=hackathon CLAUDE_TEAM_AGENT=api claude --plugin-dir "$PLUGIN"

Things to try:
  - In "api": "Claim src/auth/ and change getUser() to return null; announce it as a breaking change."
  - In "frontend": "Edit src/auth/login.ts"  -> the edit is held, Claude explains the overlap.
  - In "frontend": "/team-sync:team"          -> room overview.
  - In "tests": "Ask the api agent for a test fixture."  -> shows up in api's next prompt.

Inspect the shared state any time:
  python3 "$PLUGIN/scripts/team.py" status --all     (run inside any clone)
  git -C "$DIR/origin.git" log --oneline team-sync
EOF
