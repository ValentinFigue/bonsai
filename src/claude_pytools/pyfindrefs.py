#!/usr/bin/env python3
"""Find all references to a Python symbol across the project using AST."""

import argparse
import ast
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

if __package__:
    from ._common import (
        collect_python_files,
        find_project_root,
        get_lines,
        module_to_path,
        parse_file,
        path_to_module,
        resolve_relative_import,
    )
else:
    sys.path.insert(0, str(Path(__file__).parent))
    from _common import (
        collect_python_files,
        find_project_root,
        get_lines,
        module_to_path,
        parse_file,
        path_to_module,
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
    if 1 <= lineno <= len(lines):
        return lines[lineno - 1].strip()
    return ""


def _build_parent_map(tree: ast.AST) -> dict:
    pm = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            pm[id(child)] = node
    return pm


def _classify_name(node: ast.Name, pm: dict) -> str | None:
    parent = pm.get(id(node))
    gp = pm.get(id(parent)) if parent else None

    # Skip alias targets in imports
    if isinstance(parent, ast.alias):
        return None
    if isinstance(gp, (ast.ImportFrom, ast.Import)):
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
        if isinstance(gp, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if parent in gp.decorator_list:
                return "decorator"
        return "call"

    # Base class
    if isinstance(parent, ast.ClassDef) and node in parent.bases:
        return "base_class"

    return "name"


def _classify_attr(node: ast.Attribute, pm: dict) -> str:
    parent = pm.get(id(node))
    gp = pm.get(id(parent)) if parent else None

    if isinstance(parent, ast.Call) and parent.func is node:
        if isinstance(gp, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if parent in gp.decorator_list:
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
    tree = parse_file(fpath)
    lines = get_lines(fpath)
    if tree is None or lines is None:
        return []

    pm = _build_parent_map(tree)
    seen: dict[int, str] = {}
    search_name = method or symbol

    def add(lineno: int, ref_type: str):
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
            ref_type = _classify_name(node, pm)
            if ref_type is not None:
                add(node.lineno, ref_type)

        elif isinstance(node, ast.Attribute) and node.attr == search_name:
            if module_aliases and isinstance(node.value, ast.Name) and node.value.id in module_aliases:
                add(node.lineno, _classify_attr(node, pm))
            elif method is not None:
                # .method() on any receiver — best-effort without type inference
                add(node.lineno, _classify_attr(node, pm))

    return [
        Ref(
            filepath=str(fpath.relative_to(root)),
            line=lineno,
            ref_type=ref_type,
            snippet=_snippet(lines, lineno),
        )
        for lineno, ref_type in sorted(seen.items())
    ]


def _python_roots(root: Path) -> list[Path]:
    """Find Python package roots: project root + immediate subdirs with pyproject.toml/setup.py."""
    roots = [root]
    try:
        for child in root.iterdir():
            if child.is_dir() and any((child / m).exists() for m in ["pyproject.toml", "setup.py", "setup.cfg"]):
                roots.append(child)
    except OSError:
        pass
    return roots


def _module_aliases_for_file(fpath: Path, roots: list[Path]) -> list[str]:
    """Compute all possible module names for a file (handles multiple Python roots)."""
    names = []
    for r in roots:
        m = path_to_module(fpath, r)
        if m:
            names.append(m)
    return names


def _find_importers(
    symbol: str,
    module_name: str,
    def_file: Path | None,
    all_files: list[Path],
    root: Path,
) -> tuple[dict[Path, str], dict[Path, list[str]]]:
    """Pass 1: find files that import the symbol directly or import the module."""
    direct: dict[Path, str] = {}      # filepath → local alias
    module_imp: dict[Path, list[str]] = {}  # filepath → [module local names]

    # Build the full set of module names for the def file (handles multiple Python roots)
    py_roots = _python_roots(root)
    target_modules: set[str] = {module_name}
    if def_file:
        for name in _module_aliases_for_file(def_file, py_roots):
            target_modules.add(name)

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
    if ":" not in target:
        print(f"Error: expected module:symbol or module:Class.method, got '{target}'", file=sys.stderr)
        sys.exit(1)

    module_name, _, sym = target.rpartition(":")
    method: str | None = None
    symbol = sym
    if "." in sym:
        symbol, _, method = sym.partition(".")

    all_files = collect_python_files(project_root)
    def_file = module_to_path(module_name, project_root)

    direct, module_imp = _find_importers(symbol, module_name, def_file, all_files, project_root)

    refs: list[Ref] = []
    scanned: set[Path] = set()

    def scan(fpath: Path, direct_aliases: list[str], mod_aliases: list[str], is_def: bool):
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


def print_refs(refs: list[Ref]):
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


def main():
    parser = argparse.ArgumentParser(description="Find all references to a Python symbol.")
    parser.add_argument("target", help="module:Symbol or module:Class.method")
    parser.add_argument("--project-root", help="Project root directory")
    parser.add_argument("--json", action="store_true", help="Output as JSON array")
    args = parser.parse_args()

    start = Path(args.project_root) if args.project_root else Path.cwd()
    root = Path(args.project_root) if args.project_root else find_project_root(start)

    refs = find_refs(args.target, root)

    if args.json:
        print(json.dumps([asdict(r) for r in refs], indent=2))
    else:
        print_refs(refs)


if __name__ == "__main__":
    main()
