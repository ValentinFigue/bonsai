#!/usr/bin/env python3
import ast
import os
from dataclasses import dataclass, field
from pathlib import Path

SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".tox", ".nox", ".venv", "venv", "env", ".env", "node_modules",
    ".eggs", "build", "dist", ".ruff_cache",
}


def find_project_root(start: Path) -> Path:
    current = start.resolve()
    if current.is_file():
        current = current.parent
    for d in [current, *current.parents]:
        if any((d / m).exists() for m in ["pyproject.toml", "setup.py", "setup.cfg", ".git"]):
            return d
    return start.resolve().parent


def collect_python_files(root: Path) -> list[Path]:
    results = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for f in filenames:
            if f.endswith(".py"):
                results.append(Path(dirpath) / f)
    return results


def path_to_module(filepath: Path, root: Path) -> str | None:
    try:
        rel = filepath.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    parts = list(rel.parts)
    if not parts:
        return None
    if parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else None


def module_to_path(module: str, root: Path) -> Path | None:
    parts = module.split(".")
    fp = root / Path(*parts[:-1]) / (parts[-1] + ".py") if len(parts) > 1 else root / (parts[0] + ".py")
    if fp.exists():
        return fp
    pkg = root / Path(*parts) / "__init__.py"
    return pkg if pkg.exists() else None


def read_source(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def parse_file(path: Path) -> ast.Module | None:
    source = read_source(path)
    if source is None:
        return None
    try:
        return ast.parse(source, filename=str(path))
    except SyntaxError:
        return None


def get_lines(path: Path) -> list[str] | None:
    source = read_source(path)
    if source is None:
        return None
    return source.splitlines(keepends=True)


def resolve_relative_import(
    importing_file: Path, root: Path, level: int, module: str | None
) -> str | None:
    file_module = path_to_module(importing_file, root)
    if file_module is None:
        return None
    parts = file_module.split(".")
    if importing_file.name != "__init__.py":
        parts = parts[:-1]
    if level > len(parts):
        return None
    base_parts = parts[:len(parts) - (level - 1)] if level >= 1 else parts
    if module:
        return ".".join(base_parts + module.split("."))
    return ".".join(base_parts)


@dataclass
class FileEdit:
    start_line: int   # 0-indexed
    end_line: int     # 0-indexed, inclusive
    start_col: int
    end_col: int
    new_text: str
    description: str = ""


@dataclass
class FileChanges:
    filepath: Path
    edits: list[FileEdit] = field(default_factory=list)

    def apply(self, lines: list[str]) -> list[str]:
        result = list(lines)
        for edit in sorted(self.edits, key=lambda e: (e.start_line, e.start_col), reverse=True):
            if edit.start_line == edit.end_line:
                line = result[edit.start_line]
                result[edit.start_line] = line[:edit.start_col] + edit.new_text + line[edit.end_col:]
            else:
                first = result[edit.start_line][:edit.start_col]
                last = result[edit.end_line][edit.end_col:]
                result[edit.start_line:edit.end_line + 1] = [first + edit.new_text + last]
        return result


def apply_changes(changes: list[FileChanges], dry_run: bool = False) -> int:
    modified = 0
    for fc in changes:
        if not fc.edits:
            continue
        lines = get_lines(fc.filepath)
        if lines is None:
            continue
        new_lines = fc.apply(lines)
        if dry_run:
            print(f"  Would modify: {fc.filepath}")
            for edit in sorted(fc.edits, key=lambda e: e.start_line):
                old = "".join(lines[edit.start_line:edit.end_line + 1]).rstrip("\n")
                print(f"    L{edit.start_line + 1}: {old[:120]}")
                print(f"     ->  {edit.new_text.rstrip()[:120]}")
        else:
            fc.filepath.write_text("".join(new_lines), encoding="utf-8")
            print(f"  Updated {fc.filepath} ({len(fc.edits)} edit{'s' if len(fc.edits) != 1 else ''})")
        modified += 1
    return modified
