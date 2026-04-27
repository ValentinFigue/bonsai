"""pysignature — Change a Python function's signature and update all call sites.

Supports adding, removing, renaming, and reordering parameters, and changing
defaults. All call sites across the project are rewritten automatically.
"""

import argparse
import ast
import io
import sys
import tokenize
from dataclasses import dataclass
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
    parse_symbol_ref,
    resolve_relative_import,
)


@dataclass
class TrackedImport:
    """An import statement that brings the target function into scope.

    ``is_module_import`` is ``True`` when the file imports the module
    (``import mod``) rather than the function directly (``from mod import fn``).
    In that case ``module_alias`` holds the local name used for the module.
    """

    node: ast.stmt
    local_name: str
    is_module_import: bool
    module_alias: str | None = None


def find_imports_of_symbol(
    tree: ast.Module,
    filepath: Path,
    root: Path,
    target_module: str,
    target_symbol: str,
) -> list[TrackedImport]:
    """Find all import nodes in *tree* that bring *target_symbol* into scope.

    Args:
        tree: Parsed AST of the file to inspect.
        filepath: Path of the file (used for relative import resolution).
        root: Project root.
        target_module: Dotted module name where the symbol is defined.
        target_symbol: Name of the symbol whose call sites are being rewritten.

    Returns:
        List of :class:`TrackedImport` objects for each matching import.
    """
    results = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == target_module or alias.name.startswith(target_module + "."):
                    results.append(
                        TrackedImport(
                            node=node,
                            local_name=target_symbol,
                            is_module_import=True,
                            module_alias=alias.asname or alias.name.split(".")[0],
                        )
                    )
        elif isinstance(node, ast.ImportFrom):
            resolved = (
                resolve_relative_import(filepath, root, node.level, node.module) if node.level > 0 else node.module
            )
            if resolved == target_module:
                for alias in node.names:
                    if alias.name in (target_symbol, "*"):
                        results.append(
                            TrackedImport(
                                node=node,
                                local_name=alias.asname or target_symbol,
                                is_module_import=False,
                            )
                        )
    return results


# ─── Signature Logic ─────────────────────────────────────────────────────────


@dataclass
class ParamInfo:
    """Metadata for a single function parameter.

    ``kind`` is one of ``"regular"``, ``"keyword_only"``, ``"positional_only"``,
    ``"*args"``, or ``"**kwargs"``.
    ``annotation`` and ``default`` are unparsed source strings, or ``None``.
    """

    name: str
    annotation: str | None = None
    default: str | None = None
    kind: str = "regular"  # regular, keyword_only, positional_only, *args, **kwargs


@dataclass
class SignatureChange:
    """A single requested change to a function signature.

    ``action`` is one of ``"add"``, ``"remove"``, ``"rename"``, ``"reorder"``,
    or ``"set_default"``. The remaining fields are action-specific.
    """

    action: str  # add, remove, rename, reorder, set_default
    param_name: str
    new_name: str | None = None
    new_type: str | None = None
    new_default: str | None = None
    new_order: list[str] | None = None
    position: int | None = None


def extract_params(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ParamInfo]:
    """Extract all parameters from *func_node* as :class:`ParamInfo` objects.

    Handles positional-only, regular, ``*args``, keyword-only, and ``**kwargs``
    parameters. Defaults are represented as unparsed source strings via
    ``ast.unparse``.

    Args:
        func_node: The function definition AST node to extract from.

    Returns:
        Ordered list of :class:`ParamInfo` objects matching the parameter list.
    """
    params = []

    # positional-only
    n_pos_only = len(func_node.args.posonlyargs)
    n_regular = len(func_node.args.args)
    all_defaults = func_node.args.defaults
    # defaults align to the END of (posonlyargs + args)
    n_total_positional = n_pos_only + n_regular
    defaults_offset = n_total_positional - len(all_defaults)

    for i, arg in enumerate(func_node.args.posonlyargs):
        di = i - defaults_offset
        default = ast.unparse(all_defaults[di]) if 0 <= di < len(all_defaults) else None
        params.append(
            ParamInfo(
                name=arg.arg,
                annotation=ast.unparse(arg.annotation) if arg.annotation else None,
                default=default,
                kind="positional_only",
            )
        )

    for i, arg in enumerate(func_node.args.args):
        di = n_pos_only + i - defaults_offset
        default = ast.unparse(all_defaults[di]) if 0 <= di < len(all_defaults) else None
        params.append(
            ParamInfo(
                name=arg.arg,
                annotation=ast.unparse(arg.annotation) if arg.annotation else None,
                default=default,
                kind="regular",
            )
        )

    if func_node.args.vararg:
        params.append(
            ParamInfo(
                name=func_node.args.vararg.arg,
                annotation=ast.unparse(func_node.args.vararg.annotation) if func_node.args.vararg.annotation else None,
                kind="*args",
            )
        )

    for i, arg in enumerate(func_node.args.kwonlyargs):
        kd = func_node.args.kw_defaults[i] if i < len(func_node.args.kw_defaults) else None
        params.append(
            ParamInfo(
                name=arg.arg,
                annotation=ast.unparse(arg.annotation) if arg.annotation else None,
                default=ast.unparse(kd) if kd is not None else None,
                kind="keyword_only",
            )
        )

    if func_node.args.kwarg:
        params.append(
            ParamInfo(
                name=func_node.args.kwarg.arg,
                annotation=ast.unparse(func_node.args.kwarg.annotation) if func_node.args.kwarg.annotation else None,
                kind="**kwargs",
            )
        )

    return params


def params_to_str(params: list[ParamInfo]) -> str:
    """Serialize *params* back to a comma-separated parameter-list string."""
    parts = []
    needs_slash = False
    added_slash = False
    seen_star = False

    for p in params:
        if p.kind == "positional_only":
            needs_slash = True
        elif needs_slash and not added_slash:
            parts.append("/")
            added_slash = True

        s = p.name
        if p.kind == "*args":
            s = f"*{p.name}"
            seen_star = True
        elif p.kind == "**kwargs":
            s = f"**{p.name}"
        elif p.kind == "keyword_only" and not seen_star:
            parts.append("*")
            seen_star = True

        if p.annotation:
            s += f": {p.annotation}"
        if p.default is not None:
            # PEP 8: spaces around = when annotation is present, no spaces otherwise
            s += f" = {p.default}" if p.annotation else f"={p.default}"
        parts.append(s)

    if needs_slash and not added_slash:
        parts.append("/")

    return ", ".join(parts)


def mutate_params(params: list[ParamInfo], changes: list[SignatureChange]) -> list[ParamInfo]:
    """Apply *changes* to *params* in order and return the updated parameter list.

    Args:
        params: Current parameter list (will not be mutated; a copy is made).
        changes: Ordered list of signature changes to apply.

    Returns:
        New parameter list after all changes have been applied.
    """
    result = list(params)
    for ch in changes:
        if ch.action == "remove":
            result = [p for p in result if p.name != ch.param_name]

        elif ch.action == "add":
            new_p = ParamInfo(
                name=ch.param_name,
                annotation=ch.new_type,
                default=ch.new_default,
            )
            if ch.position is not None:
                result.insert(ch.position, new_p)
            else:
                # Insert before *args / **kwargs / keyword_only
                idx = len(result)
                for i, p in enumerate(result):
                    if p.kind in ("*args", "**kwargs", "keyword_only"):
                        idx = i
                        break
                result.insert(idx, new_p)

        elif ch.action == "rename":
            for p in result:
                if p.name == ch.param_name and ch.new_name is not None:
                    p.name = ch.new_name

        elif ch.action == "set_default":
            for p in result:
                if p.name == ch.param_name:
                    p.default = ch.new_default
                    if ch.new_type:
                        p.annotation = ch.new_type

        elif ch.action == "reorder" and ch.new_order:
            by_name = {p.name: p for p in result}
            reordered = [by_name[n] for n in ch.new_order if n in by_name]
            rest = [p for p in result if p.name not in ch.new_order]
            result = reordered + rest

    return result


class CallFinder(ast.NodeVisitor):
    """AST visitor that collects all ``ast.Call`` nodes for a target function."""

    def __init__(self, func_name: str, is_module_import: bool, module_alias: str | None) -> None:
        self.func_name = func_name
        self.is_module_import = is_module_import
        self.module_alias = module_alias
        self.calls: list[ast.Call] = []

    def visit_Call(self, node: ast.Call) -> None:
        """Record *node* if it calls the target function."""
        if self.is_module_import:
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == self.module_alias
                and node.func.attr == self.func_name
            ):
                self.calls.append(node)
        else:
            if isinstance(node.func, ast.Name) and node.func.id == self.func_name:
                self.calls.append(node)
        self.generic_visit(node)


def find_matching_paren(
    lines: list[str],
    start_line: int,
    start_col: int,
) -> tuple[int, int] | None:
    """Locate the closing parenthesis that matches the ``(`` at *start_line*/*start_col*.

    Uses the tokenizer on the source from *start_line* onward so that
    string literals and comments containing ``(`` or ``)`` are handled correctly.

    Args:
        lines: All source lines of the file.
        start_line: 0-based line index of the opening ``(``.
        start_col: Column index of the opening ``(``.

    Returns:
        ``(line_idx, col)`` of the matching ``)``, or ``None`` on tokenizer error.
    """
    slice_src = "".join(lines[start_line:])
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(slice_src).readline))
    except tokenize.TokenError:
        return None
    depth = 0
    found_open = False
    for tok in tokens:
        if tok.type != tokenize.OP:
            continue
        tok_row, tok_col = tok.start  # 1-based row within slice
        abs_line = start_line + tok_row - 1
        abs_col = tok_col
        if not found_open:
            if abs_line == start_line and abs_col == start_col and tok.string == "(":
                found_open = True
                depth = 1
        else:
            if tok.string == "(":
                depth += 1
            elif tok.string == ")":
                depth -= 1
                if depth == 0:
                    return abs_line, abs_col
    return None


def rewrite_call(
    call: ast.Call,
    lines: list[str],
    old_params: list[ParamInfo],
    changes: list[SignatureChange],
) -> FileEdit | None:
    """Rewrite the argument list of *call* to match the new signature.

    Converts positional arguments to keyword form for clarity, applies renames
    and removals, and inserts ``# TODO`` placeholders for required new parameters
    without defaults.

    Args:
        call: The ``ast.Call`` node whose arguments to rewrite.
        lines: Source lines of the file containing *call*.
        old_params: Parameter list of the function *before* the change.
        changes: Signature changes being applied.

    Returns:
        A :class:`FileEdit` replacing the argument span, or ``None`` if no
        rewrite is needed or the parenthesis span cannot be located.
    """
    if not call.args and not call.keywords:
        # Check if we need to add a non-default arg
        needs_add = any(ch.action == "add" and ch.new_default is None for ch in changes)
        if not needs_add:
            return None

    # Build current argument map: param_name -> value_text
    # positional args map to param names by index (skip self/cls)
    non_self_params = [p for p in old_params if p.name not in ("self", "cls")]
    param_names = [p.name for p in non_self_params]

    # current_kwargs: name -> unparsed value text
    current_kwargs: dict[str, str] = {}

    for i, arg in enumerate(call.args):
        if i < len(param_names):
            current_kwargs[param_names[i]] = ast.unparse(arg)

    for kw in call.keywords:
        if kw.arg:
            current_kwargs[kw.arg] = ast.unparse(kw.value)
        else:
            # **spread — leave as-is, can't analyze
            current_kwargs[f"**{ast.unparse(kw.value)}"] = f"**{ast.unparse(kw.value)}"

    # Apply changes
    for ch in changes:
        if ch.action == "remove":
            current_kwargs.pop(ch.param_name, None)
        elif ch.action == "rename" and ch.new_name:
            if ch.param_name in current_kwargs:
                current_kwargs[ch.new_name] = current_kwargs.pop(ch.param_name)
        elif ch.action == "add" and ch.new_default is None:
            if ch.param_name not in current_kwargs:
                current_kwargs[ch.param_name] = f"...  # TODO: provide {ch.param_name}"

    # Determine how to reconstruct: keyword form for clarity
    new_parts = []
    for name, value in current_kwargs.items():
        if name.startswith("**"):
            new_parts.append(value)
        else:
            new_parts.append(f"{name}={value}")

    new_args_str = ", ".join(new_parts)

    paren_line = call.lineno - 1
    func_end_col = call.func.end_col_offset if hasattr(call.func, "end_col_offset") else 0
    raw_line = lines[paren_line] if paren_line < len(lines) else ""
    paren_col = raw_line.find("(", func_end_col)
    if paren_col == -1:
        return None

    result = find_matching_paren(lines, paren_line, paren_col)
    if result is None:
        return None
    close_line_idx, close_col = result

    return FileEdit(
        start_line=paren_line,
        end_line=close_line_idx,
        start_col=paren_col,
        end_col=close_col + 1,
        new_text=f"({new_args_str})",
        description="Rewrite call args",
    )


def find_func_in_tree(
    tree: ast.Module, func_name: str, class_name: str | None = None
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Locate a function (or method) definition node in *tree*.

    Args:
        tree: Module AST to search.
        func_name: Name of the function to find.
        class_name: When set, search only inside the class with this name.

    Returns:
        The matching ``FunctionDef`` or ``AsyncFunctionDef`` node, or ``None``.
    """
    if class_name:
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == func_name:
                        return child
    else:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
                return node
    return None


def do_signature(
    target_ref: str,
    changes: list[SignatureChange],
    root: Path,
    dry_run: bool,
) -> bool:
    """Apply signature *changes* to *target_ref* and rewrite all call sites.

    Args:
        target_ref: Function reference in ``"module:function"`` or
            ``"module:Class.method"`` format.
        changes: Ordered list of :class:`SignatureChange` objects to apply.
        root: Project root directory.
        dry_run: Preview changes without writing files.

    Returns:
        ``True`` on success, ``False`` if the module or function cannot be found.
    """
    module_name, symbol_name, method_name = parse_symbol_ref(target_ref)
    actual_func = method_name or symbol_name

    print(f"{'[DRY RUN] ' if dry_run else ''}Changing signature:")
    print(f"  {module_name}:{symbol_name}" + (f".{method_name}" if method_name else ""))
    for ch in changes:
        if ch.action == "add":
            print(
                f"    + add '{ch.param_name}'"
                + (f": {ch.new_type}" if ch.new_type else "")
                + (f" = {ch.new_default}" if ch.new_default is not None else "  [no default]")
            )
        elif ch.action == "remove":
            print(f"    - remove '{ch.param_name}'")
        elif ch.action == "rename":
            print(f"    ~ rename '{ch.param_name}' -> '{ch.new_name}'")
        elif ch.action == "reorder":
            print(f"    reorder: {ch.new_order}")
        elif ch.action == "set_default":
            print(f"    = set default '{ch.param_name}' = {ch.new_default}")
    print(f"  Root: {root}")
    print()

    def_path = find_module_path(module_name, root)
    if def_path is None or not def_path.exists():
        print(f"ERROR: Cannot find module '{module_name}'", file=sys.stderr)
        return False

    def_tree = parse_file(def_path)
    if def_tree is None:
        print(f"ERROR: Cannot parse {def_path}", file=sys.stderr)
        return False

    func_node = find_func_in_tree(def_tree, actual_func, symbol_name if method_name else None)
    if func_node is None:
        print(f"ERROR: Cannot find '{actual_func}' in {def_path}", file=sys.stderr)
        return False

    old_params = extract_params(func_node)
    new_params = mutate_params(list(old_params), changes)

    print(f"  Old: ({params_to_str(old_params)})")
    print(f"  New: ({params_to_str(new_params)})")
    print()

    def_lines = get_lines(def_path)
    if def_lines is None:
        return False

    # Find the paren span of the def
    start_l = func_node.lineno - 1
    def_line = def_lines[start_l]
    paren_col = def_line.find("(", func_node.col_offset)
    if paren_col == -1:
        print("ERROR: Cannot find opening paren in function def", file=sys.stderr)
        return False

    paren_result = find_matching_paren(def_lines, start_l, paren_col)
    if paren_result is None:
        print("ERROR: Cannot find closing paren in function def", file=sys.stderr)
        return False
    close_l, close_c = paren_result

    all_changes: list[FileChanges] = []
    fc_def = FileChanges(filepath=def_path)
    fc_def.edits.append(
        FileEdit(
            start_line=start_l,
            end_line=close_l,
            start_col=paren_col,
            end_col=close_c + 1,
            new_text=f"({params_to_str(new_params)})",
            description="Rewrite def signature",
        )
    )
    all_changes.append(fc_def)

    # Rewrite call sites
    for pyfile in collect_python_files(root):
        tree = parse_file(pyfile)
        if tree is None:
            continue
        lines = get_lines(pyfile)
        if lines is None:
            continue

        is_def = pyfile.resolve() == def_path.resolve()

        if is_def:
            finder = CallFinder(actual_func, False, None)
            finder.visit(tree)
            fc = fc_def
        else:
            imports = find_imports_of_symbol(
                tree, pyfile, root, module_name, symbol_name if not method_name else symbol_name
            )
            if not imports:
                continue
            imp = imports[0]
            finder = CallFinder(
                func_name=actual_func,
                is_module_import=imp.is_module_import,
                module_alias=imp.module_alias,
            )
            finder.visit(tree)
            if not finder.calls:
                continue
            fc = FileChanges(filepath=pyfile)
            all_changes.append(fc)

        for call in finder.calls:
            edit = rewrite_call(call, lines, old_params, changes)
            if edit:
                fc.edits.append(edit)

    total = sum(len(fc.edits) for fc in all_changes)
    print(f"  Files to modify: {len(all_changes)}")
    print(f"  Total edits:     {total}")
    print()

    count = apply_changes(all_changes, dry_run=dry_run)

    if dry_run:
        print(f"\n[DRY RUN] Would modify {count} file(s).")
    else:
        print(f"\n  Done! Modified {count} file(s).")
        if any(ch.action == "remove" for ch in changes):
            print("  Warning: removed params — verify **kwargs forwarding patterns.")
        if any(ch.action == "add" and ch.new_default is None for ch in changes):
            print("  Warning: added params without defaults — search for '# TODO: provide' markers.")
    return True


# ─── CLI ─────────────────────────────────────────────────────────────────────


def main() -> None:
    """CLI entry point: parse arguments and invoke :func:`do_signature`."""
    parser = argparse.ArgumentParser(
        description="Change a Python function signature and update all call sites.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("target", help="module:function or module:Class.method")
    parser.add_argument(
        "--add",
        metavar="NAME",
        nargs="+",
        action="append",
        dest="adds",
        default=[],
        help="Add a parameter: NAME [TYPE [DEFAULT]]",
    )
    parser.add_argument(
        "--remove", metavar="NAME", action="append", dest="removes", default=[], help="Remove a parameter"
    )
    parser.add_argument(
        "--rename",
        metavar=("OLD", "NEW"),
        nargs=2,
        action="append",
        dest="renames",
        default=[],
        help="Rename a parameter",
    )
    parser.add_argument(
        "--reorder", metavar="NAME", nargs="+", dest="reorder", help="Specify the desired parameter order"
    )
    parser.add_argument(
        "--set-default",
        metavar="NAME",
        nargs="+",
        action="append",
        dest="set_defaults",
        default=[],
        help="Change a parameter default: NAME VALUE [TYPE]",
    )
    parser.add_argument("--project-root", "-r", help="Project root (auto-detected if omitted)")
    parser.add_argument("--dry-run", "-n", action="store_true")

    args = parser.parse_args()

    if not args.adds and not args.removes and not args.renames and not args.reorder and not args.set_defaults:
        parser.error("Specify at least one change (--add, --remove, --rename, --reorder, --set-default)")

    changes: list[SignatureChange] = []

    for parts in args.adds:
        changes.append(
            SignatureChange(
                action="add",
                param_name=parts[0],
                new_type=parts[1] if len(parts) > 1 else None,
                new_default=parts[2] if len(parts) > 2 else None,
            )
        )

    for name in args.removes:
        changes.append(SignatureChange(action="remove", param_name=name))

    for old, new in args.renames:
        changes.append(SignatureChange(action="rename", param_name=old, new_name=new))

    if args.reorder:
        changes.append(SignatureChange(action="reorder", param_name="", new_order=args.reorder))

    for parts in args.set_defaults:
        if len(parts) < 2:
            parser.error("--set-default requires NAME and VALUE")
        changes.append(
            SignatureChange(
                action="set_default",
                param_name=parts[0],
                new_default=parts[1],
                new_type=parts[2] if len(parts) > 2 else None,
            )
        )

    root = Path(args.project_root).resolve() if args.project_root else find_project_root(Path.cwd())
    success = do_signature(args.target, changes, root, args.dry_run)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
