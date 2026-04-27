"""pymovesymbol — Move a Python function or class to a different module.

Extracts the symbol from its source file, appends it to the destination,
leaves a backward-compat comment in the original, and rewrites all import
references across the project to point to the new location.
"""

import argparse
import ast
import sys
from pathlib import Path

from ._common import (
    FileChanges,
    FileEdit,
    apply_changes,
    collect_python_files,
    find_module_path,
    find_project_root,
    get_lines,
    parse_file,
    resolve_relative_import,
)


def module_to_new_path(module: str, root: Path) -> Path:
    """Convert a dotted module name to the path where its source file would live.

    Unlike :func:`module_to_path`, this does not check whether the file exists;
    it is used to compute the destination path for a new module.
    """
    parts = module.split(".")
    return root / Path(*parts[:-1]) / (parts[-1] + ".py") if len(parts) > 1 else root / (parts[0] + ".py")


def parse_symbol_ref(ref: str) -> tuple[str, str]:
    """Parse a ``module:Symbol`` reference; exit with an error for ``module:Class.method``.

    Args:
        ref: Reference string in ``"module:symbol"`` format.
            A ``"."`` in the symbol part is rejected (methods cannot be moved).

    Returns:
        A ``(module_name, symbol_name)`` tuple.
    """
    if ":" not in ref:
        print(f"ERROR: Symbol reference must be 'module:symbol' (got '{ref}')", file=sys.stderr)
        sys.exit(1)
    module, symbol = ref.split(":", 1)
    if "." in symbol:
        print("ERROR: Cannot move individual methods. Move the whole class instead.", file=sys.stderr)
        sys.exit(1)
    return module, symbol


# ─── Move-Symbol Logic ───────────────────────────────────────────────────────


def extract_symbol(filepath: Path, symbol_name: str) -> tuple[str, int, int] | None:
    """Extract a symbol's source text from a file.

    Returns (source_text, start_line_0idx, end_line_0idx).
    Includes decorators. start/end are inclusive 0-indexed line numbers.
    """
    tree = parse_file(filepath)
    if tree is None:
        return None
    lines = get_lines(filepath)
    if lines is None:
        return None

    for node in ast.iter_child_nodes(tree):
        name = getattr(node, "name", None)
        if name != symbol_name:
            continue
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue

        start = node.lineno - 1
        if node.decorator_list:
            start = node.decorator_list[0].lineno - 1
        end = (node.end_lineno or node.lineno) - 1

        source = "".join(lines[start : end + 1])
        return source, start, end

    # Also handle top-level assignments: MY_CONST = ...
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == symbol_name:
                    start = node.lineno - 1
                    end = (node.end_lineno or node.lineno) - 1
                    source = "".join(lines[start : end + 1])
                    return source, start, end

    return None


def do_move_symbol(
    target_ref: str,
    dest_module: str,
    root: Path,
    dry_run: bool,
) -> bool:
    """Move a single symbol from its source module to *dest_module*.

    Steps performed:
    1. Extract the symbol (with decorators) from the source file.
    2. Replace it in the source with a backward-compatibility comment.
    3. Append it to the destination file (or create the file if it doesn't exist).
    4. Rewrite all ``from source_module import symbol`` statements across the project.

    Args:
        target_ref: Symbol reference in ``"module:Symbol"`` format.
        dest_module: Dotted name of the destination module.
        root: Project root directory.
        dry_run: Preview changes without writing files.

    Returns:
        ``True`` on success, ``False`` if a fatal error occurred.
    """
    module_name, symbol_name = parse_symbol_ref(target_ref)

    print(f"{'[DRY RUN] ' if dry_run else ''}Moving symbol:")
    print(f"  {module_name}:{symbol_name} -> {dest_module}:{symbol_name}")
    print(f"  Root: {root}")
    print()

    src_path = find_module_path(module_name, root)
    if src_path is None or not src_path.exists():
        print(f"ERROR: Cannot find source module '{module_name}'", file=sys.stderr)
        return False

    dst_path = find_module_path(dest_module, root)
    create_dst = dst_path is None or not dst_path.exists()
    if create_dst:
        dst_path = module_to_new_path(dest_module, root)

    # dst_path is always non-None here: module_to_path returning None triggered
    # create_dst, which assigns module_to_new_path (always returns a Path).
    if dst_path is None:
        print(f"ERROR: Cannot determine destination path for '{dest_module}'", file=sys.stderr)
        return False

    extraction = extract_symbol(src_path, symbol_name)
    if extraction is None:
        print(f"ERROR: Cannot find '{symbol_name}' in {src_path}", file=sys.stderr)
        return False

    symbol_source, sym_start, sym_end = extraction

    print(f"  Source file: {src_path.relative_to(root)}")
    print(f"  Dest file:   {dst_path.relative_to(root)}" + (" [new]" if create_dst else ""))
    print(f"  Symbol size: {len(symbol_source.splitlines())} lines")
    print()

    src_lines = get_lines(src_path)
    if src_lines is None:
        return False

    all_changes: list[FileChanges] = []

    # ── Step 1: Replace symbol in source with a backward-compat comment ──────

    # Also swallow trailing blank lines after the symbol
    remove_end = sym_end
    while remove_end + 1 < len(src_lines) and src_lines[remove_end + 1].strip() == "":
        remove_end += 1

    compat_comment = (
        f"# NOTE: {symbol_name} was moved to {dest_module}\n"
        f"# from {dest_module} import {symbol_name}  # uncomment for backward compat\n"
    )

    fc_src = FileChanges(filepath=src_path)
    fc_src.edits.append(
        FileEdit(
            start_line=sym_start,
            end_line=remove_end,
            start_col=0,
            end_col=len(src_lines[remove_end]),
            new_text=compat_comment,
            description=f"Replace {symbol_name} with compat comment",
        )
    )
    all_changes.append(fc_src)

    # ── Step 2: Append / create the destination ───────────────────────────────

    if not create_dst and dst_path.exists():
        dst_lines = get_lines(dst_path)
        if dst_lines is None:
            return False
        append_text = "\n\n\n" + symbol_source
        if not symbol_source.endswith("\n"):
            append_text += "\n"

        fc_dst = FileChanges(filepath=dst_path)
        last_l = len(dst_lines) - 1
        last_c = len(dst_lines[last_l]) if dst_lines else 0
        fc_dst.edits.append(
            FileEdit(
                start_line=last_l,
                end_line=last_l,
                start_col=last_c,
                end_col=last_c,
                new_text=append_text,
                description=f"Append {symbol_name}",
            )
        )
        all_changes.append(fc_dst)
    else:
        # Create destination file
        if dry_run:
            print(f"  Would create {dst_path}")
        else:
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            content = symbol_source
            if not content.endswith("\n"):
                content += "\n"
            dst_path.write_text(content, encoding="utf-8")
            print(f"  Created {dst_path.relative_to(root)}")

    # ── Step 3: Rewrite import references across the project ─────────────────

    for pyfile in collect_python_files(root):
        if pyfile.resolve() == src_path.resolve():
            continue
        if pyfile.resolve() == dst_path.resolve():
            continue

        tree = parse_file(pyfile)
        if tree is None:
            continue
        lines = get_lines(pyfile)
        if lines is None:
            continue

        fc = FileChanges(filepath=pyfile)

        for node in ast.iter_child_nodes(tree):
            if not isinstance(node, ast.ImportFrom):
                continue

            resolved = resolve_relative_import(pyfile, root, node.level, node.module) if node.level > 0 else node.module
            if resolved != module_name:
                continue

            # Does this import include our symbol?
            symbol_aliases = [a for a in node.names if a.name == symbol_name]
            if not symbol_aliases:
                continue

            other_aliases = [a for a in node.names if a.name != symbol_name]
            indent = " " * node.col_offset

            start_l = node.lineno - 1
            end_l = (node.end_lineno or node.lineno) - 1
            end_col = len(lines[end_l])

            alias = symbol_aliases[0]
            asname_part = f" as {alias.asname}" if alias.asname else ""

            if other_aliases:
                # Split: keep original import for others, add new one for moved symbol
                other_str = ", ".join(f"{a.name} as {a.asname}" if a.asname else a.name for a in other_aliases)
                new_text = (
                    f"{indent}from {module_name} import {other_str}\n"
                    f"{indent}from {dest_module} import {symbol_name}{asname_part}\n"
                )
            else:
                new_text = f"{indent}from {dest_module} import {symbol_name}{asname_part}\n"

            fc.edits.append(
                FileEdit(
                    start_line=start_l,
                    end_line=end_l,
                    start_col=0,
                    end_col=end_col,
                    new_text=new_text,
                    description=f"Repoint import of {symbol_name} to {dest_module}",
                )
            )

        if fc.edits:
            all_changes.append(fc)

    total = sum(len(fc.edits) for fc in all_changes)
    print(f"  Files to modify: {len(all_changes)}")
    print(f"  Import rewrites: {total}")
    print()

    count = apply_changes(all_changes, dry_run=dry_run)

    if dry_run:
        print("\n[DRY RUN] No files modified.")
    else:
        print(f"\n  Done! Modified {count} file(s).")
        print(f"  Note: Check if {dst_path.name} needs the imports that {symbol_name} depends on.")
        print(f"  Tip: run 'ruff check {dst_path}' to spot missing imports.")

    return True


# ─── CLI ─────────────────────────────────────────────────────────────────────


def main() -> None:
    """CLI entry point: parse arguments and invoke :func:`do_move_symbol`."""
    parser = argparse.ArgumentParser(
        description="Move a Python function or class to a different module.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("target", help="module:Symbol (e.g. src.utils:format_date)")
    parser.add_argument("dest_module", help="Destination module (e.g. src.utils.dates)")
    parser.add_argument("--project-root", "-r", help="Project root (auto-detected if omitted)")
    parser.add_argument("--dry-run", "-n", action="store_true", help="Preview changes without modifying files")

    args = parser.parse_args()
    root = Path(args.project_root).resolve() if args.project_root else find_project_root(Path.cwd())

    success = do_move_symbol(args.target, args.dest_module, root, args.dry_run)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
