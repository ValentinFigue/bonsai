"""pyfindunused — find unused Python symbols across the project.

Three detectors:
  --dead-code   Top-level functions/classes with no cross-module refs (default)
  --params      Function parameters never used in the body
  --imports     Imports never used within their file
"""

import argparse
import ast
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from ._common import (
    PyToolsConfig,
    collect_python_files,
    find_project_root,
    get_lines,
    load_config,
    module_aliases_for_file,
    parse_file,
    python_roots,
    resolve_relative_import,
)

# Decorators implying framework registration — function is not dead.
_FRAMEWORK_DECORATORS = {
    "get",
    "post",
    "put",
    "delete",
    "patch",
    "head",
    "options",
    "websocket",
    "route",
    "task",
    "shared_task",
    "periodic_task",
    "fixture",
    "mark",
    "command",
    "group",
    "on_startup",
    "on_shutdown",
    "on_event",
    "receiver",
    "signal",
    "classmethod",
    "staticmethod",
    "overload",
}

# Names that are implicitly called by a framework or runtime.
_ENTRY_POINTS = {
    "main",
    "cli",
    "app",
    "create_app",
    "application",
    "handler",
    "lambda_handler",
    "upgrade",
    "downgrade",  # Alembic
    "run_migrations_online",
    "run_migrations_offline",
    "setUp",
    "tearDown",  # unittest
    "setUpClass",
    "tearDownClass",
    "startup",
    "shutdown",
}

# Directory names to skip for dead-code detection.
_DEAD_SKIP: frozenset[str] = frozenset({"migrations", "tests", "test", "alembic"})


@dataclass
class UnusedResult:
    filepath: str
    line: int
    kind: str  # dead_code | unused_param | unused_import
    name: str
    detail: str


def _decorator_names(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> set[str]:
    """Return the set of bare decorator names applied to *node*."""
    names: set[str] = set()
    for dec in node.decorator_list:
        if isinstance(dec, ast.Name):
            names.add(dec.id)
        elif isinstance(dec, ast.Attribute):
            names.add(dec.attr)
        elif isinstance(dec, ast.Call):
            func = dec.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _names_in_stmts(stmts: list[ast.stmt]) -> set[str]:
    """Return all ``ast.Name`` identifiers that appear anywhere inside *stmts*."""
    names: set[str] = set()
    for stmt in stmts:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Name):
                names.add(node.id)
    return names


def _snippet(fpath: Path, lineno: int) -> str:
    """Return the stripped source line at *lineno* (1-based) of *fpath*, or ``""``."""
    lines = get_lines(fpath)
    if lines and 1 <= lineno <= len(lines):
        return lines[lineno - 1].strip()
    return ""


def _skip_for_dead(fpath: Path, skip_dirs: frozenset[str]) -> bool:
    """Return ``True`` if any path component of *fpath* is in *skip_dirs*."""
    return any(part in skip_dirs for part in fpath.parts)


# ── dead code ─────────────────────────────────────────────────────────────────


def find_dead_code(
    all_files: list[Path],
    root: Path,
    config: PyToolsConfig | None = None,
) -> list[UnusedResult]:
    """Find top-level public functions and classes with no cross-module references.

    Uses a two-pass approach: pass 1 collects definitions; pass 2 scans all
    files for imports or intra-file name references to those definitions.

    Args:
        all_files: Full list of ``.py`` files to analyse.
        root: Project root (used to compute module names).
        config: Optional project configuration; defaults to :class:`PyToolsConfig`.

    Returns:
        List of :class:`UnusedResult` entries for unreferenced symbols.
    """
    if config is None:
        config = PyToolsConfig()

    effective_decorators = (
        config.dead_code_decorators if config.dead_code_decorators is not None else _FRAMEWORK_DECORATORS
    ) | config.dead_code_extra_decorators
    effective_entry_points = (
        config.dead_code_entry_points if config.dead_code_entry_points is not None else _ENTRY_POINTS
    ) | config.dead_code_extra_entry_points
    effective_skip = (
        config.dead_code_skip_dirs if config.dead_code_skip_dirs is not None else _DEAD_SKIP
    ) | config.dead_code_extra_skip_dirs

    py_roots = python_roots(root)
    files = [f for f in all_files if not _skip_for_dead(f, effective_skip)]

    # Pass 1: collect top-level public definitions and __all__ per file
    defs: dict[str, tuple[Path, int, str]] = {}  # "mod:name" → (fpath, lineno, name)
    all_exports: dict[Path, set[str]] = {}

    for fpath in files:
        tree = parse_file(fpath)
        if not tree:
            continue
        modules = module_aliases_for_file(fpath, py_roots)
        if not modules:
            continue

        exports: set[str] = set()
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == "__all__":
                        val = node.value
                        if isinstance(val, (ast.List, ast.Tuple)):
                            for elt in val.elts:
                                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                    exports.add(elt.value)
        all_exports[fpath] = exports

        for node in ast.iter_child_nodes(tree):
            if not isinstance(
                node,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
            ):
                continue
            name = node.name
            if name.startswith("_"):
                continue
            if name in effective_entry_points:
                continue
            if name.startswith("test_") or name.endswith("_test"):
                continue
            if _decorator_names(node) & effective_decorators:
                continue
            for m in modules:
                defs[f"{m}:{name}"] = (fpath, node.lineno, name)

    # Pass 2: build referenced set
    referenced: set[str] = set()

    local_def_names: dict[Path, set[str]] = {}
    for fpath, _, name in defs.values():
        local_def_names.setdefault(fpath, set()).add(name)

    for fpath in all_files:  # scan full project, not just filtered
        tree = parse_file(fpath)
        if not tree:
            continue

        # Cross-file imports
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level > 0:
                    resolved = resolve_relative_import(fpath, root, node.level, node.module)
                else:
                    resolved = node.module
                if not resolved:
                    continue
                for a in node.names:
                    if a.name != "*":
                        referenced.add(f"{resolved}:{a.name}")

        # Intra-file Name references to local definitions
        modules = module_aliases_for_file(fpath, py_roots)
        defs_here = local_def_names.get(fpath, set())
        if defs_here and modules:
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id in defs_here:
                    for m in modules:
                        referenced.add(f"{m}:{node.id}")

        # __all__ exports
        for exported in all_exports.get(fpath, set()):
            for m in module_aliases_for_file(fpath, py_roots):
                referenced.add(f"{m}:{exported}")

    # Collect unreferenced, deduplicating by physical location
    seen: set[tuple[Path, int]] = set()
    results: list[UnusedResult] = []
    for key, (fpath, lineno, name) in sorted(defs.items(), key=lambda x: (str(x[1][0]), x[1][1])):
        loc = (fpath, lineno)
        if loc in seen:
            continue
        variants = [k for k, v in defs.items() if v == (fpath, lineno, name)]
        if any(k in referenced for k in variants):
            continue
        seen.add(loc)
        results.append(
            UnusedResult(
                filepath=str(fpath.relative_to(root)),
                line=lineno,
                kind="dead_code",
                name=name,
                detail=_snippet(fpath, lineno),
            )
        )
    return results


# ── unused parameters ─────────────────────────────────────────────────────────


def find_unused_params(all_files: list[Path], root: Path) -> list[UnusedResult]:
    """Find function parameters that are never referenced in the function body.

    Skips ``self``, ``cls``, and underscore-prefixed parameters.

    Args:
        all_files: ``.py`` files to scan.
        root: Project root (used to compute relative file paths for output).

    Returns:
        List of :class:`UnusedResult` entries with ``kind="unused_param"``.
    """
    results: list[UnusedResult] = []

    for fpath in all_files:
        tree = parse_file(fpath)
        if not tree:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            args = node.args
            params = []
            all_args = args.args + args.posonlyargs + args.kwonlyargs
            for arg in all_args:
                if arg.arg in ("self", "cls") or arg.arg.startswith("_"):
                    continue
                params.append((arg.arg, arg.lineno))

            if not params:
                continue

            used = _names_in_stmts(node.body)
            fn_snippet = _snippet(fpath, node.lineno)

            for param_name, param_line in params:
                if param_name not in used:
                    results.append(
                        UnusedResult(
                            filepath=str(fpath.relative_to(root)),
                            line=param_line,
                            kind="unused_param",
                            name=param_name,
                            detail=f"in {node.name}() — {fn_snippet}",
                        )
                    )

    return results


# ── unused imports ────────────────────────────────────────────────────────────


def find_unused_imports(all_files: list[Path], root: Path) -> list[UnusedResult]:
    """Find imports in each file that are never referenced outside the import block.

    Excludes imports inside ``TYPE_CHECKING`` blocks and ``__future__`` imports.
    Handles string annotations by scanning ``ast.Constant`` nodes.

    Args:
        all_files: ``.py`` files to scan.
        root: Project root (used to compute relative file paths for output).

    Returns:
        List of :class:`UnusedResult` entries with ``kind="unused_import"``.
    """
    results: list[UnusedResult] = []

    for fpath in all_files:
        tree = parse_file(fpath)
        if not tree:
            continue

        # Collect names imported inside TYPE_CHECKING blocks
        type_checking_names: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            test = node.test
            is_tc = (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
                isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
            )
            if not is_tc:
                continue
            for child in ast.walk(node):
                if isinstance(child, (ast.ImportFrom, ast.Import)):
                    for a in child.names:
                        type_checking_names.add(a.asname or a.name)

        imports: list[tuple[str, int, str]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module == "__future__":
                    continue
                for a in node.names:
                    if a.name == "*":
                        continue
                    local = a.asname or a.name
                    if local not in type_checking_names:
                        imports.append((local, node.lineno, _snippet(fpath, node.lineno)))
            elif isinstance(node, ast.Import):
                for a in node.names:
                    local = a.asname or a.name
                    if local not in type_checking_names:
                        imports.append((local, node.lineno, _snippet(fpath, node.lineno)))

        if not imports:
            continue

        import_lines = {ln for (_, ln, _) in imports}
        used: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.lineno not in import_lines:
                used.add(node.id)
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                used.add(node.value.split("[")[0].strip())

        for local_name, lineno, snippet in imports:
            if local_name not in used:
                results.append(
                    UnusedResult(
                        filepath=str(fpath.relative_to(root)),
                        line=lineno,
                        kind="unused_import",
                        name=local_name,
                        detail=snippet,
                    )
                )

    return results


# ── output ────────────────────────────────────────────────────────────────────


def print_results(results: list[UnusedResult], header: str) -> None:
    """Print *results* under *header* in a tabular format, or report none found."""
    if not results:
        print(f"{header}: none found.")
        return
    print(f"\n{header} ({len(results)})")
    for r in results:
        loc = f"{r.filepath}:{r.line}"
        print(f"  {loc:<55}  {r.name:<25}  {r.detail[:55]}")


# ── main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    """CLI entry point: parse arguments and invoke the selected detectors."""
    parser = argparse.ArgumentParser(description="Find unused Python symbols.")
    parser.add_argument(
        "path",
        nargs="?",
        help="Limit to a specific file or directory",
    )
    parser.add_argument(
        "--dead-code",
        action="store_true",
        help="Find unused top-level functions/classes",
    )
    parser.add_argument(
        "--params",
        action="store_true",
        help="Find unused function parameters",
    )
    parser.add_argument(
        "--imports",
        action="store_true",
        help="Find unused imports within files",
    )
    parser.add_argument("--project-root", help="Project root directory")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    any_flag = args.dead_code or args.params or args.imports
    run_dead = args.dead_code or not any_flag
    run_params = args.params or not any_flag
    run_imports = args.imports or not any_flag

    root = Path(args.project_root) if args.project_root else find_project_root(Path.cwd())
    config = load_config(root)

    if args.path:
        target = Path(args.path)
        all_files = [target] if target.is_file() else collect_python_files(target)
    else:
        all_files = collect_python_files(root)

    results: list[UnusedResult] = []
    if run_dead:
        results += find_dead_code(all_files, root, config)
    if run_params:
        results += find_unused_params(all_files, root)
    if run_imports:
        results += find_unused_imports(all_files, root)

    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2))
        return

    dead = [r for r in results if r.kind == "dead_code"]
    params = [r for r in results if r.kind == "unused_param"]
    imports_ = [r for r in results if r.kind == "unused_import"]

    if run_dead:
        print_results(dead, "DEAD CODE (no cross-module references)")
    if run_params:
        print_results(params, "UNUSED PARAMETERS")
    if run_imports:
        print_results(imports_, "UNUSED IMPORTS")

    total = len(results)
    print(f"\n{total} issue{'s' if total != 1 else ''} found.")


if __name__ == "__main__":
    main()
