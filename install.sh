#!/usr/bin/env bash
set -e

# ── options ────────────────────────────────────────────────────────────────────
INJECT_CLAUDE_MD=false
PUBLISHED=false        # set to true once bonsai-py is on PyPI and bonsai-ts is on npm
WITH_HOOK=true
for arg in "$@"; do
  case "$arg" in
    --claude-md) INJECT_CLAUDE_MD=true ;;
    --published) PUBLISHED=true ;;
    --no-hook)   WITH_HOOK=false ;;
    *) echo "Unknown option: $arg"; echo "Usage: $0 [--claude-md] [--published] [--no-hook]"; exit 1 ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

# ── helpers ────────────────────────────────────────────────────────────────────
check_ok()  { echo "  ✓ $*"; }
check_err() { echo "  ✗ $*"; }

# ── prereqs ────────────────────────────────────────────────────────────────────
echo "==> Checking prerequisites"
ok=true
if [ "$PUBLISHED" = true ]; then
  required_cmds=(uv node python3)
else
  required_cmds=(uv node npm python3)
fi
for cmd in "${required_cmds[@]}"; do
  if command -v "$cmd" &>/dev/null; then
    check_ok "$cmd found"
  else
    check_err "$cmd not found — please install it first"
    ok=false
  fi
done
if [ "$ok" = false ]; then exit 1; fi

# ── build (local mode only) ────────────────────────────────────────────────────
if [ "$PUBLISHED" = false ]; then
  echo ""
  echo "==> Building Python package (py/)"
  (cd "$REPO_ROOT/py" && uv sync --quiet)
  check_ok "bonsai-py dependencies installed"

  echo ""
  echo "==> Building TypeScript server (ts/)"
  (cd "$REPO_ROOT/ts" && npm install --silent && npm run build --silent)
  check_ok "bonsai-ts built"
fi

# ── register MCP servers ───────────────────────────────────────────────────────
echo ""
if [ "$PUBLISHED" = true ]; then
  echo "==> Registering published MCP servers in ~/.claude.json"
  python3 - <<'PYEOF'
import json, os

claude_json_path = os.path.expanduser("~/.claude.json")
data = {}
if os.path.exists(claude_json_path):
    try:
        with open(claude_json_path) as f:
            data = json.load(f)
    except json.JSONDecodeError:
        pass

data.setdefault("mcpServers", {})
data["mcpServers"]["bonsai-py"] = {"type": "stdio", "command": "uvx", "args": ["bonsai-py"]}
data["mcpServers"]["bonsai-ts"] = {"type": "stdio", "command": "npx", "args": ["--yes", "bonsai-ts"]}

tmp = claude_json_path + ".tmp"
with open(tmp, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
os.replace(tmp, claude_json_path)
print("  ✓ bonsai-py registered (uvx bonsai-py)")
print("  ✓ bonsai-ts registered (npx bonsai-ts)")
PYEOF
else
  echo "==> Registering local MCP servers in ~/.claude.json"
  python3 - "$REPO_ROOT" <<'PYEOF'
import json, os, sys

repo_root = sys.argv[1]
claude_json_path = os.path.expanduser("~/.claude.json")

data = {}
if os.path.exists(claude_json_path):
    try:
        with open(claude_json_path) as f:
            data = json.load(f)
    except json.JSONDecodeError:
        pass

data.setdefault("mcpServers", {})
data["mcpServers"]["bonsai-py"] = {
    "type": "stdio",
    "command": "uv",
    "args": ["run", "--directory", os.path.join(repo_root, "py"), "bonsai-py"],
}
data["mcpServers"]["bonsai-ts"] = {
    "type": "stdio",
    "command": "node",
    "args": [os.path.join(repo_root, "ts", "dist", "server.js")],
}

tmp = claude_json_path + ".tmp"
with open(tmp, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
os.replace(tmp, claude_json_path)
print("  ✓ bonsai-py registered (local build)")
print("  ✓ bonsai-ts registered (local build)")
PYEOF
fi

# ── permissions ────────────────────────────────────────────────────────────────
echo ""
echo "==> Adding MCP permissions to ~/.claude/settings.json"

python3 - <<'PYEOF'
import json, os

settings_path = os.path.expanduser("~/.claude/settings.json")
settings = {}
if os.path.exists(settings_path):
    try:
        with open(settings_path) as f:
            settings = json.load(f)
    except json.JSONDecodeError:
        pass

allow = settings.setdefault("permissions", {}).setdefault("allow", [])
added = []
for entry in ["mcp__bonsai_py__*", "mcp__bonsai_ts__*"]:
    if entry not in allow:
        allow.append(entry)
        added.append(entry)

os.makedirs(os.path.dirname(settings_path), exist_ok=True)
tmp = settings_path + ".tmp"
with open(tmp, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
os.replace(tmp, settings_path)

if added:
    for e in added:
        print(f"  ✓ {e} permission added")
else:
    print("  ✓ permissions already present")
PYEOF

if [ "$WITH_HOOK" = true ]; then
# ── hooks ──────────────────────────────────────────────────────────────────────
echo ""
echo "==> Registering PreToolUse and PostToolUse hooks in ~/.claude/settings.json"

PRE_HOOK_SCRIPT="$REPO_ROOT/hooks/enforce-bonsai.sh"
POST_HOOK_SCRIPT="$REPO_ROOT/hooks/post-bonsai.sh"
chmod +x "$PRE_HOOK_SCRIPT" "$POST_HOOK_SCRIPT"

python3 - "$PRE_HOOK_SCRIPT" "$POST_HOOK_SCRIPT" <<'PYEOF'
import json, os, sys

pre_script  = sys.argv[1]
post_script = sys.argv[2]
settings_path = os.path.expanduser("~/.claude/settings.json")
settings = {}
if os.path.exists(settings_path):
    try:
        with open(settings_path) as f:
            settings = json.load(f)
    except json.JSONDecodeError:
        pass

# PreToolUse — Bash nudge hook
pre = settings.setdefault("hooks", {}).setdefault("PreToolUse", [])
# Remove all stale bonsai Bash hooks (old prompt type and any command type), then re-add.
pre[:] = [
    h for h in pre
    if not (
        h.get("matcher") == "Bash" and
        any(
            (hook.get("type") == "prompt" and "bonsai AST tool" in hook.get("prompt", "")) or
            (hook.get("type") == "command" and "enforce-bonsai" in hook.get("command", ""))
            for hook in h.get("hooks", [])
        )
    )
]
pre.append({"matcher": "Bash", "hooks": [{"type": "command", "command": pre_script}]})

# PostToolUse — reference-drift nudge hook
post = settings.setdefault("hooks", {}).setdefault("PostToolUse", [])
post[:] = [
    h for h in post
    if not (
        h.get("matcher") in ("Write|Edit|MultiEdit", "Write", "Edit", "MultiEdit") and
        any("post-bonsai" in hook.get("command", "") for hook in h.get("hooks", []))
    )
]
post.append({
    "matcher": "Write|Edit|MultiEdit",
    "hooks": [{"type": "command", "command": post_script}],
})

os.makedirs(os.path.dirname(settings_path), exist_ok=True)
tmp = settings_path + ".tmp"
with open(tmp, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
os.replace(tmp, settings_path)
print("  ✓ PreToolUse Bash nudge hook registered (enforce-bonsai.sh)")
print("  ✓ PostToolUse reference-drift hook registered (post-bonsai.sh)")
PYEOF
else
  check_ok "hook registration skipped (--no-hook)"
fi

# ── skills ─────────────────────────────────────────────────────────────────────
echo ""
echo "==> Installing skills to ~/.claude/skills/"
mkdir -p "$HOME/.claude/skills"
for skill_dir in "$REPO_ROOT/skills/"/*/; do
  skill_name=$(basename "$skill_dir")
  rsync -r "$skill_dir" "$HOME/.claude/skills/$skill_name/"
  check_ok "skill: $skill_name"
done

# ── CLI binary ─────────────────────────────────────────────────────────────────
echo ""
echo "==> Installing bonsai CLI to ~/.local/bin/bonsai"
mkdir -p "$HOME/.local/bin"
ln -sf "$REPO_ROOT/bin/bonsai" "$HOME/.local/bin/bonsai"
chmod +x "$REPO_ROOT/bin/bonsai"
check_ok "~/.local/bin/bonsai -> $REPO_ROOT/bin/bonsai"

if ! command -v bonsai &>/dev/null; then
  echo ""
  echo "  Note: ~/.local/bin is not on your PATH."
  echo "  Add this to your shell profile to use the 'bonsai' command:"
  echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

# ── CLAUDE.md injection ────────────────────────────────────────────────────────
if [ "$INJECT_CLAUDE_MD" = true ]; then
  echo ""
  echo "==> Injecting bonsai guidance into ~/.claude/CLAUDE.md"

  CLAUDE_MD_PATH="$HOME/.claude/CLAUDE.md"
  TEMPLATE_PATH="$REPO_ROOT/templates/CLAUDE.md"
  mkdir -p "$HOME/.claude"

  if [ -f "$CLAUDE_MD_PATH" ] && grep -q "<!-- aether:start -->" "$CLAUDE_MD_PATH"; then
    check_ok "aether block present — bonsai section already covered (skipping)"
  elif [ -f "$CLAUDE_MD_PATH" ] && grep -q "<!-- bonsai:start -->" "$CLAUDE_MD_PATH"; then
    check_ok "bonsai section already present (skipping)"
  else
    printf "\n" >> "$CLAUDE_MD_PATH"
    {
      echo "<!-- bonsai:start -->"
      cat "$TEMPLATE_PATH"
      echo "<!-- bonsai:end -->"
    } >> "$CLAUDE_MD_PATH"
    check_ok "bonsai section added to $CLAUDE_MD_PATH"
  fi
fi

# ── done ───────────────────────────────────────────────────────────────────────
echo ""
echo "Done. Restart Claude Code to load the MCP servers."
if [ "$INJECT_CLAUDE_MD" = false ]; then
  echo "Tip: run './install.sh --claude-md' to also add bonsai guidance to ~/.claude/CLAUDE.md"
fi
echo "Run 'bonsai status' to verify the installation."
