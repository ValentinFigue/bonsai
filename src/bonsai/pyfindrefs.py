"""Find all references to a Python symbol across the project using AST."""

import argparse
import ast
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from ._common import (
    collect_python_files,
    find_module_path,
    find_project_root,
    get_lines,
    module_aliases_for_file,
    parse_file,
    python_roots,
    resolve_relative_import,
)

_REF_PRIORITY = {
    "definition": 0,
    "import": 1,
    "call": 2,
    "decorator": 3,
    "base_class": 4,
    "name": 5,
    "attribute": 6,
}


@dataclass
class Ref:
    filepath: str
    line: int
    ref_type: str
    snippet: str


def _snippet(lines: list[str], lineno: int) -> str:
    """Return the stripped source line at *lineno* (1-based), or ``""``."""
    if 1 <= lineno <= len(lines):
        return lines[lineno - 1].strip()
    return ""


def _build_parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    """Build a mapping from each node's ``id()`` to its parent node."""
    parent_map: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent_map[id(child)] = node
    return parent_map


def _classify_name(node: ast.Name, parent_map: dict[int, ast.AST]) -> str | None:
    """Classify how *node* references a symbol and return a ref-type string.

    Args:
        node: The ``ast.Name`` node whose usage is being classified.
        parent_map: Mapping from ``id(node)`` to parent, from :func:`_build_parent_map`.

    Returns:
        One of ``"decorator"``, ``"call"``, ``"base_class"``, ``"name"``, or
        ``None`` if the node is part of an import statement or a definition header
        (which are handled elsewhere).
    """
    parent = parent_map.get(id(node))
    grandparent = parent_map.get(id(parent)) if parent else None

    # Skip alias targets in imports
    if isinstance(parent, ast.alias):
        return None
    if isinstance(grandparent, (ast.ImportFrom, ast.Import)):
        return None

    # Skip the symbol name in a def/class statement (handled separately)
    if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        if getattr(parent, "name", None) == node.id:
            return None
        # Direct decorator: @symbol
        if node in parent.decorator_list:
            return "decorator"

    # Decorator factory (@symbol(args)) or plain call
    if isinstance(parent, ast.Call) and parent.func is node:
        if isinstance(grandparent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if parent in grandparent.decorator_list:
                return "decorator"
        return "call"

    # Base class
    if isinstance(parent, ast.ClassDef) and node in parent.bases:
        return "base_class"

    return "name"


def _classify_attr(node: ast.Attribute, parent_map: dict[int, ast.AST]) -> str:
    """Classify how *node* (a dotted attribute access) references a symbol.

    Args:
        node: The ``ast.Attribute`` node to classify.
        parent_map: Mapping from ``id(node)`` to parent, from :func:`_build_parent_map`.

    Returns:
        One of ``"decorator"``, ``"call"``, ``"base_class"``, or ``"attribute"``.
    """
    parent = parent_map.get(id(node))
    grandparent = parent_map.get(id(parent)) if parent else None

    if isinstance(parent, ast.Call) and parent.func is node:
        if isinstance(grandparent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if parent in grandparent.decorator_list:
                return "decorator"
        return "call"

    if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        if node in parent.decorator_list:
            return "decorator"

    if isinstance(parent, ast.ClassDef) and node in parent.bases:
        return "base_class"

    return "attribute"


def _scan_file(
    fpath: Path,
    root: Path,
    direct_aliases: list[str],
    module_aliases: list[str],
    symbol: str,
    method: str | None,
    is_def_file: bool,
) -> list[Ref]:
    """Scan a single file for all references to a target symbol.

    Args:
        fpath: File to scan.
        root: Project root (used to compute relative paths for output).
        direct_aliases: Local names that refer directly to the symbol
            (e.g. the import alias or the symbol name itself).
        module_aliases: Local names that refer to the module containing the symbol
            (used for ``module.symbol`` attribute access patterns).
        symbol: Top-level symbol name being searched.
        method: Method name when searching for ``Class.method``, else ``None``.
        is_def_file: ``True`` when *fpath* is the file that defines the symbol
            (enables definition-node detection).

    Returns:
        Sorted list of :class:`Ref` objects, one per unique line where a
        reference is found. When multiple reference types occur on the same
        line, the highest-priority type wins (see ``_REF_PRIORITY``).
    """
    tree = parse_file(fpath)
    lines = get_lines(fpath)
    if tree is None or lines is None:
        return []

    parent_map = _build_parent_map(tree)
    seen: dict[int, str] = {}
    search_name = method or symbol

    def add(lineno: int, ref_type: str) -> None:
        existing = seen.get(lineno)
        if existing is None or _REF_PRIORITY.get(ref_type, 9) < _REF_PRIORITY.get(existing, 9):
            seen[lineno] = ref_type

    for node in ast.walk(tree):
        if not hasattr(node, "lineno"):
            continue

        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                local = a.asname or a.name
                if a.name == symbol or local in direct_aliases:
                    add(node.lineno, "import")

        elif isinstance(node, ast.Import):
            for a in node.names:
                local = a.asname or a.name
                if local in module_aliases:
                    add(node.lineno, "import")

        elif is_def_file and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == search_name:
                add(node.lineno, "definition")

        elif isinstance(node, ast.Name) and node.id in direct_aliases:
            ref_type = _classify_name(node, parent_map)
            if ref_type is not None:
                add(node.lineno, ref_type)

        elif isinstance(node, ast.Attribute) and node.attr == search_name:
            if module_aliases and isinstance(node.value, ast.Name) and node.value.id in module_aliases:
                add(node.lineno, _classify_attr(node, parent_map))
            elif method is not None:
                # .method() on any receiver — best-effort without type inference
                add(node.lineno, _classify_attr(node, parent_map))

    return [
        Ref(
            filepath=str(fpath.relative_to(root)),
            line=lineno,
            ref_type=ref_type,
            snippet=_snippet(lines, lineno),
        )
        for lineno, ref_type in sorted(seen.items())
    ]


def _find_importers(
    symbol: str,
    module_name: str,
    def_file: Path | None,
    all_files: list[Path],
    root: Path,
) -> tuple[dict[Path, str], dict[Path, list[str]]]:
    """Pass 1: find files that import the symbol directly or import the module."""
    direct: dict[Path, str] = {}  # filepath → local alias
    module_imp: dict[Path, list[str]] = {}  # filepath → [module local names]

    # Build the full set of module names for the def file (handles multiple Python roots)
    py_roots = python_roots(root)
    target_modules: set[str] = {module_name}
    if def_file:
        target_modules.update(module_aliases_for_file(def_file, py_roots))

    for fpath in all_files:
        tree = parse_file(fpath)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level > 0:
                    resolved = resolve_relative_import(fpath, root, node.level, node.module)
                else:
                    resolved = node.module
                if resolved not in target_modules:
                    continue
                for a in node.names:
                    if a.name == symbol or a.name == "*":
                        local = a.asname or a.name
                        if local == "*":
                            local = symbol
                        direct[fpath] = local
                        break
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name in target_modules:
                        local = a.asname or a.name.split(".")[-1]
                        module_imp.setdefault(fpath, []).append(local)

    return direct, module_imp


def find_refs(target: str, project_root: Path) -> list[Ref]:
    """Find all references to *target* across the project.

    Args:
        target: Symbol reference in ``"module:Symbol"`` or
            ``"module:Class.method"`` format.
        project_root: Root directory to scan.

    Returns:
        List of :class:`Ref` objects sorted by file path and line number.
        Exits with code 1 if *target* is malformed.
    """
    if ":" not in target:
        print(f"Error: expected module:symbol or module:Class.method, got '{target}'", file=sys.stderr)
        sys.exit(1)

    module_name, _, sym = target.rpartition(":")
    method: str | None = None
    symbol = sym
    if "." in sym:
        symbol, _, method = sym.partition(".")

    all_files = collect_python_files(project_root)
    def_file = find_module_path(module_name, project_root)

    direct, module_imp = _find_importers(symbol, module_name, def_file, all_files, project_root)

    refs: list[Ref] = []
    scanned: set[Path] = set()

    def scan(fpath: Path, direct_aliases: list[str], mod_aliases: list[str], is_def: bool) -> None:
        if fpath in scanned:
            return
        scanned.add(fpath)
        refs.extend(_scan_file(fpath, project_root, direct_aliases, mod_aliases, symbol, method, is_def))

    if def_file:
        scan(def_file, [method or symbol], [], True)

    for fpath, local_alias in direct.items():
        if fpath != def_file:
            scan(fpath, [local_alias], [], False)

    for fpath, mod_names in module_imp.items():
        if fpath not in scanned:
            scan(fpath, [], mod_names, False)

    return refs


def print_refs(refs: list[Ref]) -> None:
    """Print *refs* grouped by reference type in priority order."""
    if not refs:
        print("No references found.")
        return

    order = ["definition", "import", "call", "decorator", "base_class", "name", "attribute"]
    by_type: dict[str, list[Ref]] = {}
    for ref in refs:
        by_type.setdefault(ref.ref_type, []).append(ref)

    total = 0
    for ref_type in order:
        group = by_type.get(ref_type, [])
        if not group:
            continue
        print(f"\n{ref_type.upper()} ({len(group)})")
        for ref in sorted(group, key=lambda r: (r.filepath, r.line)):
            path_str = f"{ref.filepath}:{ref.line}"
            print(f"  {path_str:<60}  {ref.snippet[:80]}")
            total += 1

    print(f"\n{total} reference{'s' if total != 1 else ''} found.")


def main() -> None:
    """CLI entry point: parse arguments and invoke :func:`find_refs`."""
    parser = argparse.ArgumentParser(description="Find all references to a Python symbol.")
    parser.add_argument("target", help="module:Symbol or module:Class.method")
    parser.add_argument("--project-root", help="Project root directory")
    parser.add_argument("--json", action="store_true", help="Output as JSON array")
    args = parser.parse_args()

    root = Path(args.project_root) if args.project_root else find_project_root(Path.cwd())

    refs = find_refs(args.target, root)

    if args.json:
        print(json.dumps([asdict(r) for r in refs], indent=2))
    else:
        print_refs(refs)


if __name__ == "__main__":
    main()
