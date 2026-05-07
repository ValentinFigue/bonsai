# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.2.1] - 2026-05-07

### Added
- `--no-hook` flag on `install.sh`: skips hook registration entirely, useful when aether manages the hook lifecycle
- `<!-- aether:start -->` guard in `install.sh --claude-md`: skips bonsai block injection when an aether unified block is already present, preventing duplicate content

### Changed
- `templates/CLAUDE.md` sentinel markers (`<!-- bonsai:start/end -->`) moved from the template file into `install.sh`, making sentinel ownership explicit and aligning with cairn's pattern

## [0.2.0] - 2026-05-07

### Added
- `hooks/post-bonsai.sh` — new PostToolUse hook (matcher: `Write|Edit|MultiEdit`): nudges to verify reference integrity after edits that look like renames or signature changes
- `# suite:skip` bypass marker: unified cross-suite bypass accepted alongside `# bonsai:skip` in `enforce-bonsai.sh`
- "Works well with" suite table in README linking to temper, cairn, and whetstone
- Curl-pipe install option in README (`curl -fsSL … | bash`)

### Changed
- `hooks/enforce-bonsai.sh` expanded: now intercepts `rg`, `ripgrep`, `ag`, `ack`, `perl`, `xargs` chains, and `mv`/`git mv`/`cp` on source files; nudge messages are operation-specific (search / mutate / move)
- Source file extension coverage extended to `.js`, `.jsx`, `.mjs`
- `templates/CLAUDE.md` rewritten with proactive trigger rules, temper integration cues, and a "when NOT to use bonsai" section
- `install.sh` now registers both PreToolUse (`enforce-bonsai.sh`) and PostToolUse (`post-bonsai.sh`) hooks
- `uninstall.sh` now removes both hooks
- README Publishing section removed (install.sh handles everything end-users need)

## [0.1.0] - 2026-05-06

### Changed
- Bash nudge hook replaced: `prompt` type (LLM-evaluated, caused over-blocking of `git`, `gh`, and other unrelated commands) switched to `command` type backed by a deterministic shell script (`hooks/enforce-bonsai.sh`)
- Hook is now advisory only (exit 1 = warn but allow), not a hard blocker

### Added
- `hooks/enforce-bonsai.sh` — deterministic PreToolUse hook: nudges on `grep`/`sed`/`awk`/`find` against `.py`/`.ts`/`.tsx` files, passes everything else through silently
- `# bonsai:skip` bypass marker: append to any command to silence the nudge when no bonsai alternative exists (Bash ignores it as a comment)
- `bonsai enable-hook` now auto-migrates an installed `prompt` type hook to the new `command` type
- `bonsai enable-hook` prints the installed script path as a reminder to re-run if the repo is moved

## [0.0.2] - 2026-05-06

### Added
- `install.sh` / `uninstall.sh` for one-shot global installation with optional `--claude-md` flag to inject bonsai guidance into `~/.claude/CLAUDE.md`
- `bin/bonsai` CLI: `install`, `uninstall`, `status`, `enable-hook`, `disable-hook`, `update`
- `--published` flag on `install` / `update` to switch MCP registration to `uvx` / `npx` once packages land on PyPI and npm
- `templates/CLAUDE.md` — bonsai tool reference table injected into `~/.claude/CLAUDE.md`
- `templates/bash_nudge_prompt.txt` — single source of truth for the Bash hook prompt (shared by `install.sh`, `scripts/setup.sh`, and `bin/bonsai`)

## [0.0.1] - 2026-05-05

### Added

**bonsai-py** — AST-based Python refactoring MCP server:
- `pyrename` — rename a symbol across an entire project
- `pymove` — move a module file and update all imports
- `pymovesymbol` — move a symbol between modules
- `pysignature` — add, remove, rename, reorder, or set defaults on function parameters, with automatic call-site rewriting
- `pyfindrefs` — find all references to a symbol
- `pycallers` — find all callers of a function
- `pyfindunused` — detect dead code, unused parameters, and unused imports
- `pygrep` — regex search across Python files

**bonsai-ts** — TypeScript/TSX refactoring MCP server:
- `tsrename` — rename a symbol across a TypeScript project
- `tsmove` — move a file and update all imports
- `tsmovesymbol` — move a symbol between modules
- `tssignature` — modify function signatures with call-site rewriting
- `tsfindrefs` — find all references to a symbol

All mutating tools default to `dry_run=True`.
