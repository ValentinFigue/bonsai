## AST refactoring with bonsai

When editing `.py`, `.ts`, `.tsx`, `.js`, or `.jsx` files, prefer bonsai MCP
tools over text tools. Text tools miss re-exports, aliased imports, and type
references — they silently break code when renaming or moving symbols.

### Tool reference

| Operation | Python | TypeScript |
|---|---|---|
| Find all references | `pyfindrefs` | `tsfindrefs` |
| Rename a symbol | `pyrename` | `tsrename` |
| Move a file (rewrites imports) | `pymove` | `tsmove` |
| Move a symbol between modules | `pymovesymbol` | `tsmovesymbol` |
| Change a function signature | `pysignature` | `tssignature` |
| Find dead code | `pyfindunused` | — |
| Regex search (AST-aware) | `pygrep` | — |

Always request `--dry-run` first on any mutating tool. Review the diff, then apply.

### When to reach for bonsai proactively

Reach for bonsai — without waiting for the hook to nudge — in these situations:

- **Before deleting a function or class**: run `pyfindunused` first to confirm
  it has no live references. Deletion without this check silently orphans callers.
- **After a temper Design finding about naming**: if temper flags a misleading
  name, use `pyrename` / `tsrename` rather than sed — the rename must propagate
  everywhere, not just the definition site.
- **After a temper Correctness finding about a function signature**: use
  `pysignature` to propagate the fix to all call sites in one pass.
- **When this session has touched more than 3 files in the same module**: run
  `pyfindunused` across the module before committing — incremental edits
  across multiple files often leave orphaned symbols behind.
- **When moving a file for any reason**: always use `pymove` / `tsmove`, never
  raw `mv`. Even for untracked files — if they will be imported later, moving
  them with bonsai builds the correct import path from the start.

### When NOT to use bonsai

- New files with no importers yet — raw file creation is fine
- Config files, Markdown, JSON, YAML — bonsai operates on source ASTs only
- Comment-only or docstring-only changes — no symbol impact, text edit is fine
- Exploratory `grep` to understand a codebase — use raw grep, no structural
  change is being made
