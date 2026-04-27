# bonsai

[![PyPI version](https://img.shields.io/pypi/v/bonsai)](https://pypi.org/project/bonsai/)
[![Python 3.10+](https://img.shields.io/pypi/pyversions/bonsai)](https://pypi.org/project/bonsai/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

AST-based Python refactoring tools for Claude Code. Find symbol references, detect dead code, rename identifiers, move files and symbols, and update function signatures — driven by static analysis, with no language server or type-checking daemon required.

## Install

```bash
pip install bonsai && python -m bonsai --install
```

Restart Claude Code. All 7 tools are immediately available.

**Zero-install alternative** (requires `uv`, after PyPI publish):

```bash
uvx bonsai --install
```

## How it works

`bonsai` runs as an MCP server that Claude Code connects to over stdio. When you describe a refactoring in natural language, Claude picks the right tool, calls it with the correct arguments, and shows you the result. No slash commands, no Bash permissions needed for the tools.

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

## Configuration

Add a `[tool.bonsai]` section to your project's `pyproject.toml` to customise dead-code detection.

Each setting has two variants:

- **`extra_*`** — merged with the built-in defaults (additive)
- **base key** — replaces the built-in defaults entirely

```toml
[tool.bonsai]
# Extend the built-in decorator list (get, post, route, task, fixture, classmethod, …)
dead_code_extra_decorators = ["api_view", "login_required", "permission_classes"]

# Replace the built-in decorator list entirely
# dead_code_decorators = ["route", "task"]

# Extend the built-in entry-point list (main, handler, lambda_handler, setUp, upgrade, …)
dead_code_extra_entry_points = ["run", "execute", "on_ready"]

# Replace the built-in entry-point list entirely
# dead_code_entry_points = ["main", "handler"]

# Extend the built-in skip-dirs list (migrations, tests, test, alembic)
dead_code_extra_skip_dirs = ["fixtures", "scripts"]

# Replace the built-in skip-dirs list entirely (e.g. to scan test files)
# dead_code_skip_dirs = ["migrations", "alembic"]
```

## Development

```bash
git clone https://github.com/valentinfigue/bonsai
cd bonsai
pip install -e .
```

Run the MCP server directly:

```bash
python -m bonsai           # start the stdio MCP server
python -m bonsai --install # write config to ~/.claude/settings.json
```

Publish a new version to PyPI:

```bash
pip install hatch
hatch build
hatch publish
```

## License

MIT — see [LICENSE](LICENSE).
