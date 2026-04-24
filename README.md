# claude-pytools

[![PyPI version](https://img.shields.io/pypi/v/claude-pytools)](https://pypi.org/project/claude-pytools/)
[![Python 3.10+](https://img.shields.io/pypi/pyversions/claude-pytools)](https://pypi.org/project/claude-pytools/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

AST-based Python refactoring tools for Claude Code. Find symbol references, detect dead code, rename identifiers, move files and symbols, and update function signatures — driven by static analysis, with no language server or type-checking daemon required.

## Install

```bash
pip install claude-pytools && python -m claude_pytools --install
```

Restart Claude Code. All 7 tools are immediately available.

**Zero-install alternative** (requires `uv`, after PyPI publish):

```bash
uvx claude-pytools --install
```

## How it works

`claude-pytools` runs as an MCP server that Claude Code connects to over stdio. When you describe a refactoring in natural language, Claude picks the right tool, calls it with the correct arguments, and shows you the result. No slash commands, no Bash permissions needed for the tools.

The tools use Python's `ast` module to parse source files directly — the only runtime dependency is `mcp[cli]`. They work on any Python 3.10+ project regardless of framework.

## Tools

| Tool | What it does |
|------|-------------|
| `pyfindrefs` | Find all references to a symbol: definitions, imports, calls, decorators, base classes |
| `pycallers` | Find every call site of a function or method (call-type only; for all reference types use `pyfindrefs`) |
| `pyfindunused` | Detect dead top-level functions/classes, unused parameters, and unused imports |
| `pymove` | Move or rename a Python file/package and rewrite all imports |
| `pymovesymbol` | Move a single function or class to a different module |
| `pyrename` | Scope-aware rename across the entire project |
| `pysignature` | Change a function's signature and update all call sites |

## Usage examples

Say these things to Claude Code — no special syntax required:

**Find references**
> "Where is `User` used across the project?"
> "Find all imports of `create_user`."

Claude calls: `pyfindrefs("src.models:User")`

**Find callers only**
> "Who calls `send_email`?"
> "What calls `PaymentService.charge`?"

Claude calls: `pycallers("src.services.email:send_email")`

**Find dead code**
> "Find unused functions in this project."
> "What imports are never used in `utils.py`?"

Claude calls: `pyfindunused(dead_code=True, imports=True)`

**Move a file**
> "Move `src/utils/helpers.py` to `src/core/helpers.py` and fix all imports."

Claude calls: `pymove("src/utils/helpers.py", "src/core/helpers.py", dry_run=True)`, shows the diff, then applies on confirmation.

**Move a symbol**
> "Move the `format_date` function from `src.utils` to `src.utils.dates`."

Claude calls: `pymovesymbol("src.utils:format_date", "src.utils.dates")`

**Rename**
> "Rename `User` to `Account` everywhere."
> "Rename the `save` method on `User` to `persist`."

Claude calls: `pyrename("src.models:User", "Account", dry_run=True)`

**Change a signature**
> "Add a `timeout: int = 30` parameter to `create_user`."
> "Remove the `legacy_flag` parameter from `process_payment` and update all call sites."

Claude calls: `pysignature("src.api:create_user", add=["timeout int 30"], dry_run=True)`

All mutating tools (`pymove`, `pymovesymbol`, `pyrename`, `pysignature`) support `dry_run=True`. Claude uses dry-run by default and asks for confirmation before applying changes.

## Limitations

- **No type inference.** Method attribution is best-effort: `pycallers("src.models:User.save")` finds all `.save()` calls, not just those on `User` instances. It may include false positives from other classes with a `save` method.
- **No runtime analysis.** Dynamic patterns like `getattr(obj, method_name)()` are invisible to AST analysis.
- **Public symbols only for dead-code detection.** `pyfindunused --dead-code` skips private symbols (names starting with `_`), framework-decorated functions, and test files — but may still produce false positives if symbols are referenced dynamically.
- **Single project tree.** All tools operate on a project rooted at `pyproject.toml` / `.git`. For monorepos, pass `project_root` explicitly.

## Development

```bash
git clone https://github.com/valentinfigue/claude-pytools
cd claude-pytools
pip install -e .
```

Run the MCP server directly:

```bash
python -m claude_pytools           # start the stdio MCP server
python -m claude_pytools --install # write config to ~/.claude/settings.json
```

Publish a new version to PyPI:

```bash
pip install hatch
hatch build
hatch publish
```

## License

MIT — see [LICENSE](LICENSE).
