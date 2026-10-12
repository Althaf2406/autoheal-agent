"""
Modul Context Retrieval & Impact Analysis (Sub-Task 18, Anggota A).

1. retrieve_context(file_path):
   Mengumpulkan simbol, daftar caller (pemanggil), callee (yang dipanggil),
   dan source code file terkait untuk dimasukkan ke dalam prompt Agent Engineer.

2. check_impact(old_src, new_src):
   Menganalisis breaking changes pada signature fungsi antara kode lama dan kode baru
   (fungsi dihapus/di-rename, penambahan parameter wajib tanpa default)
   dan memeriksa apakah ada call site di repositori yang rusak akibat perubahan tersebut.
   Terintegrasi langsung dengan SAST Guardrails (Sub-Task 12).
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any

from backend.contracts import ContextBundle
from backend.repo_index import CallSite, FunctionSymbol, ParameterInfo, RepoIndex

# Cache atau active repo index untuk digunakan saat guardrails memanggil check_impact(old, new)
_active_index: RepoIndex | None = None


def set_active_index(index: RepoIndex | None) -> None:
    """Sets the active repository index for impact checking."""
    global _active_index
    _active_index = index


def get_active_index() -> RepoIndex | None:
    """Returns the currently active repository index."""
    return _active_index


def _extract_functions_from_ast(source_code: str) -> dict[str, dict[str, Any]]:
    """Extracts function signatures from source code string using AST."""
    if not source_code:
        return {}
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return {}

    funcs: dict[str, dict[str, Any]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            params = []
            args = node.args
            defaults_offset = len(args.args) - len(args.defaults)
            for i, arg in enumerate(args.args):
                has_default = i >= defaults_offset
                params.append({
                    "name": arg.arg,
                    "has_default": has_default,
                })
            required_count = len([p for p in params if not p["has_default"]])
            funcs[node.name] = {
                "name": node.name,
                "line": node.lineno,
                "parameters": params,
                "total_params": len(params),
                "required_params": required_count,
            }
    return funcs


def retrieve_context(
    file_path: str,
    repo_files: dict[str, str] | None = None,
    repo_index: RepoIndex | None = None,
    max_context_files: int = 3,
) -> ContextBundle:
    """
    Retrieves context files, callers, and callees for the given file_path.
    Used by LangGraph node_retrieve_context to augment the Agent Engineer prompt.
    """
    idx = repo_index or _active_index
    if idx is None and repo_files:
        idx = RepoIndex()
        idx.index_files(repo_files)
        set_active_index(idx)

    if idx is None:
        return ContextBundle()

    file_funcs = idx.get_file_symbols(file_path)
    callers: list[str] = []
    callees: list[str] = []
    related_files: dict[str, str] = {}

    for func in file_funcs:
        # 1. Collect callers
        call_sites = idx.get_callers(func.name)
        for cs in call_sites:
            caller_desc = f"{cs.file_path}:{cs.line_number} -> {cs.caller_function or '<module>'}"
            if caller_desc not in callers:
                callers.append(caller_desc)
            # Add caller file to context
            if cs.file_path != file_path and cs.file_path in idx._files:
                related_files[cs.file_path] = idx._files[cs.file_path]

        # 2. Collect callees
        func_callees = idx.get_callees(func.name)
        for callee in func_callees:
            if callee not in callees:
                callees.append(callee)
                # Find which file defines this callee
                callee_sym = idx.get_function(callee)
                if callee_sym and callee_sym.file_path != file_path and callee_sym.file_path in idx._files:
                    related_files[callee_sym.file_path] = idx._files[callee_sym.file_path]

    # Limit context files to max_context_files
    trimmed_files = {k: related_files[k] for k in list(related_files)[:max_context_files]}

    return ContextBundle(
        files=trimmed_files,
        callers=callers,
        callees=callees,
    )


def check_impact(
    old_src: str,
    new_src: str,
    repo_index: RepoIndex | None = None,
) -> list[str]:
    """
    Analyzes whether a patch broke any call sites in the repository:
    - Removed or renamed functions that have callers.
    - Added new required parameters without default values.
    - Removed accepted parameters when callers pass them.
    
    Returns a list of violation descriptions (empty if no breaking changes).
    """
    if not old_src or not new_src:
        return []

    old_funcs = _extract_functions_from_ast(old_src)
    new_funcs = _extract_functions_from_ast(new_src)

    if not old_funcs:
        return []

    idx = repo_index or _active_index
    broken: list[str] = []

    # 1. Check for removed or renamed functions
    for func_name, old_meta in old_funcs.items():
        if func_name not in new_funcs:
            # Function was deleted or renamed
            if idx:
                callers = idx.get_callers(func_name)
                for cs in callers:
                    broken.append(
                        f"{cs.file_path}:{cs.line_number} calls removed or renamed function '{func_name}'"
                    )
            else:
                # If no index, flag the breaking signature change
                broken.append(f"Function '{func_name}' was removed or renamed")

    # 2. Check for modified signatures
    for func_name, old_meta in old_funcs.items():
        if func_name in new_funcs:
            new_meta = new_funcs[func_name]
            old_req = old_meta["required_params"]
            new_req = new_meta["required_params"]
            old_total = old_meta["total_params"]
            new_total = new_meta["total_params"]

            # Scenario A: New required parameter added without default
            if new_req > old_req:
                if idx:
                    callers = idx.get_callers(func_name)
                    for cs in callers:
                        if len(cs.arguments) < new_req:
                            broken.append(
                                f"{cs.file_path}:{cs.line_number} '{func_name}' requires "
                                f"{new_req} arguments but call site provides {len(cs.arguments)}"
                            )
                else:
                    broken.append(
                        f"Function '{func_name}' requires {new_req} arguments (previously required {old_req})"
                    )

            # Scenario B: Function parameter count reduced below what callers pass
            if new_total < old_total:
                if idx:
                    callers = idx.get_callers(func_name)
                    for cs in callers:
                        if len(cs.arguments) > new_total:
                            broken.append(
                                f"{cs.file_path}:{cs.line_number} '{func_name}' accepts at most "
                                f"{new_total} arguments but call site provides {len(cs.arguments)}"
                            )

    return broken
