"""pyrename — Scope-aware Python symbol renamer.

Renames a function, class, method, or constant across the entire project
using AST analysis. Does not rename unrelated local variables that happen
to share the name — only the symbol at the specified module path.
"""

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from ._common import (
    FileChanges,
    FileEdit,
    apply_changes,
    collect_python_files,
    find_project_root,
    get_lines,
    module_to_path,
    parse_file,
    resolve_relative_import,
)


def parse_symbol_ref(ref: str) -> tuple[str, str, str | None]:
    if ":" not in ref:
        print(f"ERROR: Symbol reference must be 'module:symbol' (got '{ref}')", file=sys.stderr)
        sys.exit(1)
    module, symbol_path = ref.split(":", 1)
    parts = symbol_path.split(".", 1)
    return module, parts[0], parts[1] if len(parts) > 1 else None


@dataclass
class TrackedImport:
    node: ast.stmt
    local_name: str
    is_module_import: bool
    module_alias: str | None = None
    lineno: int = 0
    end_lineno: int = 0


def find_imports_of_symbol(
    tree: ast.Module,
    filepath: Path,
    root: Path,
    target_module: str,
    target_symbol: str,
) -> list[TrackedImport]:
    results = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == target_module or alias.name.startswith(target_module + "."):
                    results.append(TrackedImport(
                        node=node,
                        local_name=target_symbol,
                        is_module_import=True,
                        module_alias=alias.asname or alias.name.split(".")[0],
                        lineno=node.lineno,
                        end_lineno=node.end_lineno or node.lineno,
                    ))
        elif isinstance(node, ast.ImportFrom):
            resolved = (
                resolve_relative_import(filepath, root, node.level, node.module)
                if node.level > 0 else node.module
            )
            if resolved == target_module:
                for alias in node.names:
                    if alias.name in (target_symbol, "*"):
                        results.append(TrackedImport(
                            node=node,
                            local_name=alias.asname or alias.name if alias.name != "*" else target_symbol,
                            is_module_import=False,
                            lineno=node.lineno,
                            end_lineno=node.end_lineno or node.lineno,
                        ))
            # from parent import module_leaf (then module_leaf.symbol)
            parent = target_module.rsplit(".", 1)
            if len(parent) == 2 and resolved == parent[0]:
                for alias in node.names:
                    if alias.name == parent[1]:
                        results.append(TrackedImport(
                            node=node,
                            local_name=target_symbol,
                            is_module_import=True,
                            module_alias=alias.asname or alias.name,
                            lineno=node.lineno,
                            end_lineno=node.end_lineno or node.lineno,
                        ))
    return results


# ─── Rename Logic ─────────────────────────────────────────────────────────────

class RenameVisitor(ast.NodeVisitor):
    def __init__(
        self,
        target_name: str,
        new_name: str,
        is_module_import: bool,
        module_alias: str | None,
        is_definition_file: bool,
        target_method: str | None = None,
    ):
        self.target_name = target_name
        self.new_name = new_name
        self.is_module_import = is_module_import
        self.module_alias = module_alias
        self.is_definition_file = is_definition_file
        self.target_method = target_method
        self.edits: list[FileEdit] = []
        self._scopes: list[set[str]] = [set()]
        self._in_target_class = False

    def _shadowed(self, name: str) -> bool:
        return any(name in s for s in self._scopes)

    def _push(self):
        self._scopes.append(set())

    def _pop(self):
        self._scopes.pop()

    def _add_local(self, name: str):
        if self._scopes:
            self._scopes[-1].add(name)

    def _rename_def(self, node, keyword_len: int):
        """Emit an edit to rename a def/class name token."""
        self.edits.append(FileEdit(
            start_line=node.lineno - 1,
            end_line=node.lineno - 1,
            start_col=node.col_offset + keyword_len,
            end_col=node.col_offset + keyword_len + len(node.name),
            new_text=self.new_name,
            description=f"Rename def {node.name}",
        ))

    def visit_FunctionDef(self, node):
        if self.is_definition_file and not self.target_method and node.name == self.target_name:
            self._rename_def(node, 4)  # len("def ")
        if self._in_target_class and self.target_method and node.name == self.target_method:
            self._rename_def(node, 4)

        self._push()
        for arg in node.args.args + node.args.posonlyargs + node.args.kwonlyargs:
            self._add_local(arg.arg)
        if node.args.vararg:
            self._add_local(node.args.vararg.arg)
        if node.args.kwarg:
            self._add_local(node.args.kwarg.arg)
        self.generic_visit(node)
        self._pop()

    def visit_AsyncFunctionDef(self, node):
        if self.is_definition_file and not self.target_method and node.name == self.target_name:
            self._rename_def(node, 10)  # len("async def ")
        if self._in_target_class and self.target_method and node.name == self.target_method:
            self._rename_def(node, 10)

        self._push()
        for arg in node.args.args + node.args.posonlyargs + node.args.kwonlyargs:
            self._add_local(arg.arg)
        if node.args.vararg:
            self._add_local(node.args.vararg.arg)
        if node.args.kwarg:
            self._add_local(node.args.kwarg.arg)
        self.generic_visit(node)
        self._pop()

    def visit_ClassDef(self, node):
        if self.is_definition_file and not self.target_method and node.name == self.target_name:
            self._rename_def(node, 6)  # len("class ")

        prev = self._in_target_class
        if self.is_definition_file and self.target_method and node.name == self.target_name:
            self._in_target_class = True

        self._push()
        self.generic_visit(node)
        self._pop()
        self._in_target_class = prev

    def visit_Name(self, node):
        if self.target_method:
            self.generic_visit(node)
            return
        if not self.is_module_import and node.id == self.target_name:
            if not self._shadowed(node.id) or self.is_definition_file:
                self.edits.append(FileEdit(
                    start_line=node.lineno - 1,
                    end_line=node.lineno - 1,
                    start_col=node.col_offset,
                    end_col=node.col_offset + len(node.id),
                    new_text=self.new_name,
                    description=f"Rename ref {node.id}",
                ))
        self.generic_visit(node)

    def visit_Attribute(self, node):
        # module.symbol rename
        if self.is_module_import and not self.target_method:
            if (isinstance(node.value, ast.Name) and
                    node.value.id == self.module_alias and
                    node.attr == self.target_name):
                col = node.value.end_col_offset + 1
                self.edits.append(FileEdit(
                    start_line=node.lineno - 1,
                    end_line=node.lineno - 1,
                    start_col=col,
                    end_col=col + len(node.attr),
                    new_text=self.new_name,
                    description=f"Rename {self.module_alias}.{node.attr}",
                ))

        # ClassName.method or obj.method rename (conservative: only ClassName.method)
        if self.target_method and node.attr == self.target_method:
            if isinstance(node.value, ast.Name) and node.value.id == self.target_name:
                col = node.value.end_col_offset + 1
                self.edits.append(FileEdit(
                    start_line=node.lineno - 1,
                    end_line=node.lineno - 1,
                    start_col=col,
                    end_col=col + len(node.attr),
                    new_text=self.new_name,
                    description=f"Rename .{node.attr}",
                ))

        self.generic_visit(node)

    def visit_Assign(self, node):
        for target in node.targets:
            if isinstance(target, ast.Name):
                if not (self.is_definition_file and target.id == self.target_name):
                    self._add_local(target.id)
        self.generic_visit(node)

    def visit_For(self, node):
        if isinstance(node.target, ast.Name):
            self._add_local(node.target.id)
        self.generic_visit(node)

    def visit_With(self, node):
        for item in node.items:
            if item.optional_vars and isinstance(item.optional_vars, ast.Name):
                self._add_local(item.optional_vars.id)
        self.generic_visit(node)

    def visit_ExceptHandler(self, node):
        if node.name:
            self._add_local(node.name)
        self.generic_visit(node)


def rename_import_name(
    node: ast.ImportFrom,
    lines: list[str],
    old_name: str,
    new_name: str,
) -> list[FileEdit]:
    """Rename a symbol name in a `from x import old_name` statement."""
    edits = []
    for alias in node.names:
        if alias.name != old_name or alias.asname is not None:
            continue
        # Python 3.10+: alias has lineno/col_offset
        if hasattr(alias, "lineno") and alias.lineno:
            edits.append(FileEdit(
                start_line=alias.lineno - 1,
                end_line=alias.lineno - 1,
                start_col=alias.col_offset,
                end_col=alias.col_offset + len(old_name),
                new_text=new_name,
                description=f"Rename imported name {old_name}",
            ))
        else:
            # Fallback: search in the raw text of the import statement
            start_l = node.lineno - 1
            end_l = (node.end_lineno or node.lineno) - 1
            text = "".join(lines[start_l:end_l + 1])
            for m in re.finditer(r"\b" + re.escape(old_name) + r"\b", text):
                pos = m.start()
                if pos > 0 and text[pos - 1] == ".":
                    continue
                before = text[:pos]
                line_off = before.count("\n")
                col = pos - (before.rindex("\n") + 1 if "\n" in before else 0)
                edits.append(FileEdit(
                    start_line=start_l + line_off,
                    end_line=start_l + line_off,
                    start_col=col,
                    end_col=col + len(old_name),
                    new_text=new_name,
                    description="Rename in import",
                ))
                break
    return edits


def do_rename(target_ref: str, new_name: str, root: Path, dry_run: bool) -> bool:
    module_name, symbol_name, method_name = parse_symbol_ref(target_ref)

    print(f"{'[DRY RUN] ' if dry_run else ''}Renaming:")
    if method_name:
        print(f"  {module_name}:{symbol_name}.{method_name} -> {symbol_name}.{new_name}")
    else:
        print(f"  {module_name}:{symbol_name} -> {new_name}")
    print(f"  Root: {root}")
    print()

    def_path = module_to_path(module_name, root)
    if def_path is None or not def_path.exists():
        print(f"ERROR: Cannot find module '{module_name}' under {root}", file=sys.stderr)
        return False

    all_changes: list[FileChanges] = []

    for pyfile in collect_python_files(root):
        tree = parse_file(pyfile)
        if tree is None:
            continue
        lines = get_lines(pyfile)
        if lines is None:
            continue

        is_def = pyfile.resolve() == def_path.resolve()
        fc = FileChanges(filepath=pyfile)

        if is_def:
            visitor = RenameVisitor(
                target_name=symbol_name,
                new_name=new_name,
                is_module_import=False,
                module_alias=None,
                is_definition_file=True,
                target_method=method_name,
            )
            visitor.visit(tree)
            fc.edits.extend(visitor.edits)
        else:
            imports = find_imports_of_symbol(tree, pyfile, root, module_name, symbol_name)
            if not imports:
                continue
            for imp in imports:
                if not method_name and not imp.is_module_import:
                    if isinstance(imp.node, ast.ImportFrom):
                        fc.edits.extend(rename_import_name(imp.node, lines, symbol_name, new_name))
                visitor = RenameVisitor(
                    target_name=imp.local_name if not method_name else symbol_name,
                    new_name=new_name,
                    is_module_import=imp.is_module_import,
                    module_alias=imp.module_alias,
                    is_definition_file=False,
                    target_method=method_name,
                )
                visitor.visit(tree)
                fc.edits.extend(visitor.edits)

        # Deduplicate by position
        seen: set[tuple] = set()
        unique = []
        for e in fc.edits:
            key = (e.start_line, e.start_col, e.end_line, e.end_col)
            if key not in seen:
                seen.add(key)
                unique.append(e)
        fc.edits = unique

        if fc.edits:
            all_changes.append(fc)

    total = sum(len(fc.edits) for fc in all_changes)
    print(f"  Files to modify: {len(all_changes)}")
    print(f"  Total edits:     {total}")
    print()

    if total == 0:
        print("  No references found.")
        return True

    count = apply_changes(all_changes, dry_run=dry_run)
    if dry_run:
        print(f"\n[DRY RUN] Would modify {count} file(s).")
    else:
        print(f"\n  Done! Modified {count} file(s).")
        if method_name:
            print("  Note: Instance method calls (obj.method()) are not renamed automatically.")
            print("  They require type inference. Verify with: grep -rn '" + method_name + "' .")
    return True


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scope-aware Python symbol renamer.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("target", help="module:Symbol or module:Class.method")
    parser.add_argument("new_name", help="New name for the symbol")
    parser.add_argument("--project-root", "-r", help="Project root (auto-detected if omitted)")
    parser.add_argument("--dry-run", "-n", action="store_true", help="Preview changes without modifying files")

    args = parser.parse_args()

    target_path = module_to_path(args.target.split(":")[0],
                                  Path(args.project_root) if args.project_root else find_project_root(Path.cwd()))
    root = Path(args.project_root).resolve() if args.project_root else find_project_root(
        target_path or Path.cwd()
    )

    success = do_rename(args.target, args.new_name, root, args.dry_run)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
