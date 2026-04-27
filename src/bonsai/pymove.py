"""pymove — AST-based safe Python module mover.

Moves Python files/directories and rewrites all import references
across the project so nothing breaks.
"""

import argparse
import ast
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ._common import collect_python_files, find_project_root, path_to_module, resolve_relative_import

# ─── Models ───────────────────────────────────────────────────────────────────


@dataclass
class ImportRef:
    """A single import statement extracted from a source file.

    ``kind`` is either ``"import"`` (bare ``import x``) or ``"from"``
    (``from x import y``). ``level`` holds the number of leading dots for
    relative imports. ``names`` is a list of ``(name, asname)`` pairs.
    All line/column values are 1-based as returned by the AST.
    """

    lineno: int
    col_offset: int
    end_lineno: int
    end_col_offset: int
    original_text: str
    kind: str  # "import" | "from"
    module: str | None
    names: list[tuple[str, str | None]]  # [(name, asname), ...]
    level: int  # relative import dots


@dataclass
class RewritePlan:
    """Pending import rewrites for a single file.

    ``rewrites`` is a list of ``(start_line_0idx, end_line_0idx, new_text)``
    tuples that replace a contiguous run of lines with *new_text*.
    """

    filepath: Path
    original_lines: list[str] = field(default_factory=list)
    rewrites: list[tuple[int, int, str]] = field(default_factory=list)


# ─── AST Import Extraction ───────────────────────────────────────────────────


def extract_imports(filepath: Path) -> list[ImportRef]:
    """Parse all import statements from *filepath* and return them as :class:`ImportRef` objects.

    Args:
        filepath: Source file to read and parse.

    Returns:
        List of :class:`ImportRef` objects, one per import node in the AST.
        Returns an empty list on read or parse error.
    """
    try:
        source = filepath.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []

    try:
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return []

    lines = source.splitlines(keepends=True)
    imports = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            start = node.lineno
            end = node.end_lineno or node.lineno
            original = "".join(lines[start - 1 : end])
            imports.append(
                ImportRef(
                    lineno=start,
                    col_offset=node.col_offset,
                    end_lineno=end,
                    end_col_offset=node.end_col_offset or 0,
                    original_text=original,
                    kind="import",
                    module=None,
                    names=[(alias.name, alias.asname) for alias in node.names],
                    level=0,
                )
            )
        elif isinstance(node, ast.ImportFrom):
            start = node.lineno
            end = node.end_lineno or node.lineno
            original = "".join(lines[start - 1 : end])
            imports.append(
                ImportRef(
                    lineno=start,
                    col_offset=node.col_offset,
                    end_lineno=end,
                    end_col_offset=node.end_col_offset or 0,
                    original_text=original,
                    kind="from",
                    module=node.module,
                    names=[(alias.name, alias.asname) for alias in node.names],
                    level=node.level,
                )
            )

    return imports


# ─── Compute Rewrites ────────────────────────────────────────────────────────


def needs_rewrite(
    imp: ImportRef,
    importing_file: Path,
    root: Path,
    old_module: str,
    old_package: str | None,
) -> bool:
    """Return ``True`` if *imp* references a module path that is being moved.

    Args:
        imp: Import statement to evaluate.
        importing_file: File that contains *imp*.
        root: Project root (used for relative import resolution).
        old_module: Dotted module name of the file/package being moved.
        old_package: Package prefix when moving a package, else ``None``.

    Returns:
        ``True`` if the import needs to be rewritten after the move.
    """
    if imp.kind == "import":
        for name, _ in imp.names:
            if name == old_module or (old_package and name.startswith(old_package + ".")):
                return True

    elif imp.kind == "from":
        if imp.level > 0:
            resolved = resolve_relative_import(importing_file, root, imp.level, imp.module)
            if resolved:
                if resolved == old_module or (old_package and resolved.startswith(old_package + ".")):
                    return True
                if resolved == old_package:
                    return True
                if imp.module is None:
                    for name, _ in imp.names:
                        parent = resolve_relative_import(importing_file, root, imp.level, name)
                        if parent and (parent == old_module or (old_package and parent.startswith(old_package + "."))):
                            return True
        else:
            if imp.module:
                full = imp.module
                if full == old_module or (old_package and full.startswith(old_package + ".")):
                    return True
                for name, _ in imp.names:
                    candidate = f"{full}.{name}" if full else name
                    if candidate == old_module or (old_package and candidate.startswith(old_package + ".")):
                        return True

    return False


def rewrite_import_line(
    imp: ImportRef,
    importing_file: Path,
    root: Path,
    old_module: str,
    new_module: str,
    old_package: str | None,
    new_package: str | None,
) -> str:
    """Rewrite a single import statement to reflect the new module path.

    Args:
        imp: Import statement to rewrite.
        importing_file: File containing *imp* (used for relative resolution).
        root: Project root.
        old_module: Original dotted module name.
        new_module: New dotted module name after the move.
        old_package: Original package prefix (package moves only), else ``None``.
        new_package: New package prefix (package moves only), else ``None``.

    Returns:
        Rewritten import statement as a source string (including trailing newline).
        Returns ``imp.original_text`` unchanged if the rewrite cannot be determined.
    """

    def replace_module_prefix(m: str) -> str:
        if m == old_module:
            return new_module
        if old_package and m.startswith(old_package + "."):
            suffix = m[len(old_package) :]
            return (new_package or new_module) + suffix
        return m

    if imp.kind == "import":
        new_names = []
        for name, asname in imp.names:
            new_name = replace_module_prefix(name)
            if asname:
                new_names.append(f"{new_name} as {asname}")
            else:
                new_names.append(new_name)
        return " " * imp.col_offset + "import " + ", ".join(new_names) + "\n"

    elif imp.kind == "from":
        if imp.level > 0:
            resolved = resolve_relative_import(importing_file, root, imp.level, imp.module)
            if resolved:
                new_mod = replace_module_prefix(resolved)
            else:
                if imp.module is None:
                    new_name_parts = []
                    for name, asname in imp.names:
                        candidate = resolve_relative_import(importing_file, root, imp.level, name)
                        if candidate and (
                            candidate == old_module or (old_package and candidate.startswith(old_package + "."))
                        ):
                            new_mod = replace_module_prefix(candidate)
                            alias_part = f" as {asname}" if asname else ""
                            mod_parts = new_mod.rsplit(".", 1)
                            if len(mod_parts) == 2:
                                new_name_parts.append(f"from {mod_parts[0]} import {mod_parts[1]}{alias_part}")
                            else:
                                new_name_parts.append(f"import {new_mod}{alias_part}")
                        else:
                            alias_part = f" as {asname}" if asname else ""
                            dots = "." * imp.level
                            new_name_parts.append(f"from {dots} import {name}{alias_part}")
                    return " " * imp.col_offset + "\n".join(new_name_parts) + "\n"
                return imp.original_text

            names_str = ", ".join(f"{n} as {a}" if a else n for n, a in imp.names)
            return " " * imp.col_offset + f"from {new_mod} import {names_str}\n"
        else:
            if imp.module:
                new_mod = replace_module_prefix(imp.module)
                new_names = []
                changed = new_mod != imp.module
                for name, asname in imp.names:
                    full = f"{imp.module}.{name}"
                    if full == old_module or (old_package and full.startswith(old_package + ".")):
                        new_full = replace_module_prefix(full)
                        new_parent, _, new_leaf = new_full.rpartition(".")
                        if not changed:
                            new_mod = new_parent if new_parent else new_mod
                        alias_part = f" as {asname}" if asname else ""
                        new_names.append(f"{new_leaf}{alias_part}")
                    else:
                        alias_part = f" as {asname}" if asname else ""
                        new_names.append(f"{name}{alias_part}")

                names_str = ", ".join(new_names)
                return " " * imp.col_offset + f"from {new_mod} import {names_str}\n"

    return imp.original_text


# ─── Internal Imports (inside the moved file itself) ─────────────────────────


def rewrite_internal_imports(
    filepath: Path,
    root: Path,
    old_module: str,
    new_module: str,
) -> list[str] | None:
    """Rewrite relative imports inside *filepath* to absolute form after a move.

    Args:
        filepath: The file that was just moved (at its *new* location).
        root: Project root.
        old_module: Original dotted module name (before the move).
        new_module: New dotted module name (after the move).

    Returns:
        Updated lines if any internal relative imports were rewritten, else ``None``.
    """
    try:
        source = filepath.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None

    try:
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return None

    lines = source.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"

    edits = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level > 0:
            resolved = resolve_relative_import(filepath, root, node.level, node.module)
            if resolved is None:
                continue

            start = node.lineno - 1
            end = (node.end_lineno or node.lineno) - 1
            names_str = ", ".join(f"{a.name} as {a.asname}" if a.asname else a.name for a in node.names)
            indent = " " * node.col_offset
            new_line = f"{indent}from {resolved} import {names_str}\n"
            edits.append((start, end, new_line))

    if not edits:
        return None

    edits.sort(key=lambda e: e[0], reverse=True)
    for start, end, replacement in edits:
        lines[start : end + 1] = [replacement]

    return lines


# ─── Main Logic ──────────────────────────────────────────────────────────────


def compute_destination(src: Path, dst: Path) -> Path:
    """Return the actual destination path for *src* given a *dst* target.

    If *dst* is an existing directory or ends with ``/``, *src*'s filename is
    appended; otherwise *dst* is used as-is.
    """
    if dst.is_dir() or str(dst).endswith("/"):
        return dst / src.name
    return dst


def plan_move(
    src: Path,
    dst: Path,
    root: Path,
    dry_run: bool = False,
) -> list[RewritePlan]:
    """Compute all import rewrite plans needed to move *src* to *dst*.

    Does not modify any files; callers apply plans via :func:`apply_plan`.

    Args:
        src: Source file or package directory (will be resolved).
        dst: Destination path (will be resolved).
        root: Project root (will be resolved).
        dry_run: Unused here; present for API symmetry with :func:`execute_move`.

    Returns:
        List of :class:`RewritePlan` objects, one per file that needs rewriting.
        Returns an empty list on error (messages printed to stderr).
    """
    src = src.resolve()
    dst = dst.resolve()
    root = root.resolve()

    is_package = src.is_dir()

    if is_package:
        old_module = path_to_module(src / "__init__.py", root)
        old_package = old_module
        new_module = (
            path_to_module(dst / "__init__.py", root)
            if (dst / "__init__.py").exists()
            else ".".join(dst.resolve().relative_to(root).parts)
        )
        new_package = new_module
    else:
        old_module = path_to_module(src, root)
        old_package = None
        new_module_path = compute_destination(src, dst) if dst.is_dir() or str(dst).endswith("/") else dst
        try:
            rel = new_module_path.resolve().relative_to(root)
        except ValueError:
            print(f"ERROR: Destination {dst} is outside project root {root}", file=sys.stderr)
            return []
        parts = list(rel.parts)
        if parts[-1].endswith(".py"):
            parts[-1] = parts[-1][:-3]
        if parts[-1] == "__init__":
            parts = parts[:-1]
        new_module = ".".join(parts)
        new_package = None

    if not old_module:
        print(f"ERROR: Could not determine module path for {src}", file=sys.stderr)
        return []

    if not new_module:
        print(f"ERROR: Could not determine module path for {dst}", file=sys.stderr)
        return []

    py_files = collect_python_files(root)
    plans = []

    for pyfile in py_files:
        if pyfile.resolve() == src.resolve():
            continue

        imports = extract_imports(pyfile)
        file_rewrites = []

        for imp in imports:
            if needs_rewrite(imp, pyfile, root, old_module, old_package):
                new_text = rewrite_import_line(
                    imp,
                    pyfile,
                    root,
                    old_module,
                    new_module,
                    old_package,
                    new_package,
                )
                if new_text != imp.original_text:
                    file_rewrites.append((imp.lineno - 1, (imp.end_lineno or imp.lineno) - 1, new_text))

        if file_rewrites:
            try:
                lines = pyfile.read_text(encoding="utf-8").splitlines(keepends=True)
            except (UnicodeDecodeError, OSError):
                continue
            plan = RewritePlan(filepath=pyfile, original_lines=lines, rewrites=file_rewrites)
            plans.append(plan)

    return plans


def apply_plan(plan: RewritePlan) -> None:
    """Write the rewritten lines from *plan* back to disk."""
    lines = list(plan.original_lines)
    for start, end, new_text in sorted(plan.rewrites, key=lambda r: r[0], reverse=True):
        lines[start : end + 1] = [new_text]
    plan.filepath.write_text("".join(lines), encoding="utf-8")


def execute_move(
    src: Path,
    dst: Path,
    root: Path | None = None,
    dry_run: bool = False,
) -> bool:
    """Move *src* to *dst* and rewrite all import references across the project.

    Args:
        src: Python file or package directory to move.
        dst: Destination path (file path or directory).
        root: Project root; auto-detected from *src* if ``None``.
        dry_run: Preview changes without modifying any files.

    Returns:
        ``True`` on success, ``False`` if a fatal error occurred.
    """
    src = Path(src).resolve()
    if root is None:
        root = find_project_root(src)
    else:
        root = Path(root).resolve()

    dst = Path(dst)
    if not dst.is_absolute():
        dst = Path.cwd() / dst
    dst = dst.resolve()

    actual_dst = compute_destination(src, dst) if src.is_file() else dst

    print(f"{'[DRY RUN] ' if dry_run else ''}Moving Python module:")
    print(f"  From: {src}")
    print(f"  To:   {actual_dst}")

    old_module = path_to_module(src, root)
    if not old_module:
        print("ERROR: Cannot determine module path for source.", file=sys.stderr)
        return False

    try:
        rel = actual_dst.relative_to(root)
    except ValueError:
        print("ERROR: Destination is outside project root.", file=sys.stderr)
        return False
    parts = list(rel.parts)
    if parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    new_module = ".".join(parts)

    print(f"  Module: {old_module} -> {new_module}")
    print(f"  Root:   {root}")
    print()

    plans = plan_move(src, actual_dst, root, dry_run)
    internal_lines = rewrite_internal_imports(src, root, old_module, new_module)

    total_changes = sum(len(p.rewrites) for p in plans)
    affected_files = len(plans)
    print(f"  Files affected:  {affected_files}")
    print(f"  Import rewrites: {total_changes}")
    if internal_lines is not None:
        print("  Internal imports also need updating")
    print()

    if dry_run:
        for plan in plans:
            print(f"  Would rewrite: {plan.filepath}")
            for start, end, new_text in plan.rewrites:
                old_text = "".join(plan.original_lines[start : end + 1]).rstrip("\n")
                print(f"    L{start + 1}: {old_text}")
                print(f"     ->  {new_text.rstrip()}")
        print()
        print("[DRY RUN] No files were modified.")
        return True

    for plan in plans:
        apply_plan(plan)
        print(f"  Rewrote imports in {plan.filepath.relative_to(root)}")

    actual_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(actual_dst))
    print(f"  Moved {src.relative_to(root)} -> {actual_dst.relative_to(root)}")

    if internal_lines is not None:
        actual_dst.write_text("".join(internal_lines), encoding="utf-8")
        print(f"  Updated internal imports in {actual_dst.relative_to(root)}")

    for parent in actual_dst.parents:
        if parent == root:
            break
        init = parent / "__init__.py"
        if not init.exists():
            init.touch()
            print(f"  Created {init.relative_to(root)}")

    print()
    print("Done! Run your tests to verify everything works.")
    return True


# ─── CLI ─────────────────────────────────────────────────────────────────────


def main() -> None:
    """CLI entry point: parse arguments and invoke :func:`execute_move`."""
    parser = argparse.ArgumentParser(
        description="Safely move Python modules with AST-based import rewriting.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("source", help="Python file or package to move")
    parser.add_argument("destination", help="New location (file path or directory)")
    parser.add_argument("--project-root", "-r", help="Project root (auto-detected if omitted)")
    parser.add_argument("--dry-run", "-n", action="store_true", help="Show what would change without modifying files")

    args = parser.parse_args()

    src = Path(args.source)
    if not src.exists():
        print(f"ERROR: Source {src} does not exist.", file=sys.stderr)
        sys.exit(1)

    success = execute_move(
        src=src,
        dst=Path(args.destination),
        root=Path(args.project_root) if args.project_root else None,
        dry_run=args.dry_run,
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
