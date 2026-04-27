"""FastMCP server exposing all bonsai refactoring tools as MCP endpoints."""

import contextlib
import io
import sys
from collections.abc import Callable

from mcp.server.fastmcp import FastMCP

from .pycallers import main as _pycallers_main
from .pyfindrefs import main as _pyfindrefs_main
from .pyfindunused import main as _pyfindunused_main
from .pymove import main as _pymove_main
from .pymovesymbol import main as _pymovesymbol_main
from .pyrename import main as _pyrename_main
from .pysignature import main as _pysignature_main

mcp = FastMCP("pytools")


def _run(main_fn: Callable[[], None], argv: list[str]) -> str:
    """Invoke *main_fn* with *argv* and return its combined stdout/stderr output.

    Captures stdout and stderr via ``contextlib.redirect_*``, temporarily
    replaces ``sys.argv``, and suppresses ``SystemExit`` so CLI tools can be
    called safely from within the MCP server process.

    Args:
        main_fn: A CLI ``main()`` function that reads ``sys.argv``.
        argv: Argument vector to set (``argv[0]`` is the program name).

    Returns:
        Stripped combined output string, or ``"Done."`` when the tool produced
        no output.
    """
    buf = io.StringIO()
    err = io.StringIO()
    old_argv = sys.argv
    sys.argv = argv
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            try:
                main_fn()
            except SystemExit:
                pass
    finally:
        sys.argv = old_argv
    out = buf.getvalue().rstrip()
    error_out = err.getvalue().rstrip()
    if error_out:
        return f"{out}\n{error_out}".strip()
    return out or "Done."


@mcp.tool()
def pyfindrefs(target: str, project_root: str | None = None) -> str:
    """Find all references to a Python symbol across the project: definitions, imports, calls, decorators, base classes.

    Args:
        target: Symbol in 'module:Symbol' or 'module:Class.method' format. E.g. 'src.models:User'
        project_root: Absolute path to project root (auto-detected from cwd if omitted)
    """
    argv = ["pyfindrefs", target]
    if project_root:
        argv += ["--project-root", project_root]
    return _run(_pyfindrefs_main, argv)


@mcp.tool()
def pycallers(target: str, project_root: str | None = None) -> str:
    """Find every call site of a Python function or method across the project. Returns only call-type references, not imports or definitions. For all reference types use pyfindrefs.

    Args:
        target: Symbol in 'module:function' or 'module:Class.method' format. E.g. 'src.api.views:create_user'
        project_root: Absolute path to project root (auto-detected from cwd if omitted)
    """
    argv = ["pycallers", target]
    if project_root:
        argv += ["--project-root", project_root]
    return _run(_pycallers_main, argv)


@mcp.tool()
def pyfindunused(
    project_root: str | None = None,
    dead_code: bool = True,
    params: bool = True,
    imports: bool = True,
    file_path: str | None = None,
) -> str:
    """Find unused Python symbols: dead top-level functions/classes, unused parameters, unused imports.

    Args:
        project_root: Absolute path to project root (auto-detected from cwd if omitted)
        dead_code: Find top-level functions/classes with no cross-module references
        params: Find function parameters never used in the body
        imports: Find imports never referenced in their file
        file_path: Restrict --params or --imports analysis to a specific file or directory
    """
    argv = ["pyfindunused"]
    if dead_code:
        argv.append("--dead-code")
    if params:
        argv.append("--params")
    if imports:
        argv.append("--imports")
    if project_root:
        argv += ["--project-root", project_root]
    if file_path:
        argv.append(file_path)
    return _run(_pyfindunused_main, argv)


@mcp.tool()
def pymove(
    source: str,
    destination: str,
    project_root: str | None = None,
    dry_run: bool = False,
) -> str:
    """Move or rename a Python file/package and rewrite all import references across the project. For moving a single function or class between files, use pymovesymbol instead.

    Args:
        source: Path to the Python file or package directory to move (relative or absolute)
        destination: New location — a file path or a directory
        project_root: Absolute path to project root (auto-detected if omitted)
        dry_run: Preview changes without modifying any files
    """
    argv = ["pymove", source, destination]
    if project_root:
        argv += ["--project-root", project_root]
    if dry_run:
        argv.append("--dry-run")
    return _run(_pymove_main, argv)


@mcp.tool()
def pymovesymbol(
    target: str,
    dest_module: str,
    project_root: str | None = None,
    dry_run: bool = False,
) -> str:
    """Move a single Python function or class to a different module, rewriting all imports. For moving an entire file or package, use pymove instead.

    Args:
        target: Symbol in 'module:Symbol' format. E.g. 'src.utils.helpers:format_date'
        dest_module: Destination module in dotted notation. E.g. 'src.utils.dates'
        project_root: Absolute path to project root (auto-detected if omitted)
        dry_run: Preview changes without modifying any files
    """
    argv = ["pymovesymbol", target, dest_module]
    if project_root:
        argv += ["--project-root", project_root]
    if dry_run:
        argv.append("--dry-run")
    return _run(_pymovesymbol_main, argv)


@mcp.tool()
def pyrename(
    target: str,
    new_name: str,
    project_root: str | None = None,
    dry_run: bool = False,
) -> str:
    """Scope-aware rename of a Python symbol (function, class, method, variable, constant) across the entire project. Does not rename unrelated local variables that happen to share the name — only the symbol at the specified module path.

    Args:
        target: Symbol in 'module:Symbol' or 'module:Class.method' format. E.g. 'src.models:User'
        new_name: The new name for the symbol
        project_root: Absolute path to project root (auto-detected if omitted)
        dry_run: Preview changes without modifying any files
    """
    argv = ["pyrename", target, new_name]
    if project_root:
        argv += ["--project-root", project_root]
    if dry_run:
        argv.append("--dry-run")
    return _run(_pyrename_main, argv)


@mcp.tool()
def pysignature(
    target: str,
    add: list[str] | None = None,
    remove: list[str] | None = None,
    rename: list[str] | None = None,
    reorder: list[str] | None = None,
    set_default: list[str] | None = None,
    project_root: str | None = None,
    dry_run: bool = False,
) -> str:
    """Change a Python function's signature and update all call sites across the project.

    Args:
        target: Function in 'module:function' or 'module:Class.method' format
        add: Parameters to add, each as 'NAME', 'NAME TYPE', or 'NAME TYPE DEFAULT'. E.g. ['timeout int 30']
        remove: Parameter names to remove. E.g. ['legacy_flag']
        rename: Parameters to rename, each as 'OLD NEW'. E.g. ['user_id uid']
        reorder: New parameter order as a list of names. E.g. ['name', 'email', 'role']
        set_default: Change defaults, each as 'NAME VALUE' or 'NAME VALUE TYPE'. E.g. ['timeout 60 int']
        project_root: Absolute path to project root (auto-detected if omitted)
        dry_run: Preview changes without modifying any files
    """
    argv = ["pysignature", target]
    for item in add or []:
        argv += ["--add"] + item.split()
    for name in remove or []:
        argv += ["--remove", name]
    for pair in rename or []:
        argv += ["--rename"] + pair.split()
    if reorder:
        argv += ["--reorder"] + reorder
    for item in set_default or []:
        argv += ["--set-default"] + item.split()
    if project_root:
        argv += ["--project-root", project_root]
    if dry_run:
        argv.append("--dry-run")
    return _run(_pysignature_main, argv)
