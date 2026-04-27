import argparse
import json
import os
import sys
from pathlib import Path


def _install(settings_path: Path, command: str) -> None:
    settings = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            print(f"WARNING: Could not parse {settings_path}, creating fresh config.", file=sys.stderr)

    settings.setdefault("mcpServers", {})["bonsai"] = {
        "type": "stdio",
        "command": command,
        "args": ["-m", "bonsai"],
    }

    allow = settings.setdefault("permissions", {}).setdefault("allow", [])
    if "mcp__bonsai__*" not in allow:
        allow.append("mcp__bonsai__*")

    tmp = settings_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n")
    os.replace(tmp, settings_path)

    print(f"Installed. Added 'bonsai' MCP server to {settings_path}")
    print("Restart Claude Code to load the bonsai MCP server.")
    print()
    print("Available tools: pyfindrefs, pycallers, pyfindunused, pymove, pymovesymbol, pyrename, pysignature")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="bonsai: AST-based Python refactoring tools for Claude Code",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Add bonsai MCP server to ~/.claude/settings.json and exit",
    )
    parser.add_argument(
        "--settings",
        default=str(Path.home() / ".claude" / "settings.json"),
        help="Path to Claude Code settings.json (default: ~/.claude/settings.json)",
    )

    args = parser.parse_args()

    if args.install:
        _install(Path(args.settings), sys.executable)
        return

    from .server import mcp
    mcp.run()


if __name__ == "__main__":
    main()
