"""
Modul Indexing Repositori (Sub-Task 17, Anggota A).

Membangun Code Graph (AST Symbol Extraction, Caller-Callee Graph, Imports)
dan Search Index untuk repositori multi-file. Menjadi fondasi untuk Sub-Task 18
(Context Retrieval & Impact Analysis).
"""
from __future__ import annotations

import ast
import fnmatch
import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ParameterInfo:
    name: str
    annotation: str | None = None
    default: str | None = None


@dataclass
class FunctionSymbol:
    name: str
    file_path: str
    line_start: int
    line_end: int
    parameters: list[ParameterInfo] = field(default_factory=list)
    docstring: str | None = None
    source_code: str = ""
    is_method: bool = False
    parent_class: str | None = None


@dataclass
class ClassSymbol:
    name: str
    file_path: str
    line_start: int
    line_end: int
    bases: list[str] = field(default_factory=list)
    docstring: str | None = None
    methods: list[str] = field(default_factory=list)


@dataclass
class CallSite:
    caller_function: str | None
    callee_name: str
    file_path: str
    line_number: int
    arguments: list[str] = field(default_factory=list)


@dataclass
class ImportInfo:
    module: str
    name: str | None
    alias: str | None
    line_number: int


class CodeVisitor(ast.NodeVisitor):
    """Parses a Python file AST to extract symbols, classes, imports, and function calls."""

    def __init__(self, file_path: str, source_code: str):
        self.file_path = file_path
        self.source_code = source_code
        self.source_lines = source_code.splitlines()

        self.functions: list[FunctionSymbol] = []
        self.classes: list[ClassSymbol] = []
        self.call_sites: list[CallSite] = []
        self.imports: list[ImportInfo] = []

        self._current_class: str | None = None
        self._current_function: str | None = None

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.append(
                ImportInfo(
                    module=alias.name,
                    name=None,
                    alias=alias.asname,
                    line_number=node.lineno,
                )
            )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        for alias in node.names:
            self.imports.append(
                ImportInfo(
                    module=module,
                    name=alias.name,
                    alias=alias.asname,
                    line_number=node.lineno,
                )
            )
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        prev_class = self._current_class
        self._current_class = node.name

        bases = []
        for base in node.bases:
            if isinstance(base, ast.Name):
                bases.append(base.id)
            elif isinstance(base, ast.Attribute):
                try:
                    bases.append(f"{ast.unparse(base.value)}.{base.attr}")
                except Exception:
                    bases.append(base.attr)

        class_sym = ClassSymbol(
            name=node.name,
            file_path=self.file_path,
            line_start=node.lineno,
            line_end=getattr(node, "end_lineno", node.lineno),
            bases=bases,
            docstring=ast.get_docstring(node),
        )
        self.classes.append(class_sym)

        self.generic_visit(node)
        self._current_class = prev_class

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._handle_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._handle_function(node)

    def _handle_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        prev_function = self._current_function
        func_name = node.name
        if self._current_class:
            func_name = f"{self._current_class}.{node.name}"
            for cls in self.classes:
                if cls.name == self._current_class and cls.file_path == self.file_path:
                    cls.methods.append(node.name)

        self._current_function = func_name

        params = []
        args = node.args
        defaults_offset = len(args.args) - len(args.defaults)
        for i, arg in enumerate(args.args):
            default_val = None
            if i >= defaults_offset:
                def_node = args.defaults[i - defaults_offset]
                try:
                    default_val = ast.unparse(def_node)
                except Exception:
                    default_val = "..."
            anno = None
            if arg.annotation:
                try:
                    anno = ast.unparse(arg.annotation)
                except Exception:
                    anno = None
            params.append(ParameterInfo(name=arg.arg, annotation=anno, default=default_val))

        docstring = ast.get_docstring(node)
        line_start = node.lineno
        line_end = getattr(node, "end_lineno", line_start)
        source_slice = "\n".join(self.source_lines[line_start - 1 : line_end])

        self.functions.append(
            FunctionSymbol(
                name=func_name,
                file_path=self.file_path,
                line_start=line_start,
                line_end=line_end,
                parameters=params,
                docstring=docstring,
                source_code=source_slice,
                is_method=bool(self._current_class),
                parent_class=self._current_class,
            )
        )

        self.generic_visit(node)
        self._current_function = prev_function

    def visit_Call(self, node: ast.Call) -> None:
        callee_name = ""
        if isinstance(node.func, ast.Name):
            callee_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            callee_name = node.func.attr

        if callee_name:
            args = []
            for arg in node.args:
                try:
                    args.append(ast.unparse(arg))
                except Exception:
                    args.append("...")
            self.call_sites.append(
                CallSite(
                    caller_function=self._current_function,
                    callee_name=callee_name,
                    file_path=self.file_path,
                    line_number=node.lineno,
                    arguments=args,
                )
            )
        self.generic_visit(node)


class RepoIndex:
    """
    Code Graph and Search Index for a codebase.
    Tracks definitions, callers, callees, imports, and enables symbol lookup and search.
    """

    def __init__(self):
        self._file_hashes: dict[str, str] = {}
        self._files: dict[str, str] = {}
        self.functions: list[FunctionSymbol] = []
        self.classes: list[ClassSymbol] = []
        self.call_sites: list[CallSite] = []
        self.imports: list[ImportInfo] = []

        # Graph mappings
        self.callers_map: dict[str, list[CallSite]] = {}
        self.callees_map: dict[str, set[str]] = {}

    def _hash_content(self, content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def index_files(self, files: dict[str, str], incremental: bool = True) -> int:
        """
        Indexes a dictionary of {file_path: source_code}.
        If incremental is True, only files with changed contents will be re-parsed.
        Returns the number of files parsed.
        """
        changed_paths = []
        for path, content in files.items():
            if not path.endswith(".py"):
                continue
            content_hash = self._hash_content(content)
            if not incremental or self._file_hashes.get(path) != content_hash:
                changed_paths.append(path)
                self._file_hashes[path] = content_hash
                self._files[path] = content

        if not changed_paths:
            return 0

        # Remove previous data for updated files
        self.functions = [f for f in self.functions if f.file_path not in changed_paths]
        self.classes = [c for c in self.classes if c.file_path not in changed_paths]
        self.call_sites = [cs for cs in self.call_sites if cs.file_path not in changed_paths]
        self.imports = [imp for imp in self.imports if imp.file_path not in changed_paths]

        for path in changed_paths:
            code = self._files[path]
            try:
                tree = ast.parse(code, filename=path)
                visitor = CodeVisitor(path, code)
                # Assign file_path to imports as well
                visitor.visit(tree)
                for imp in visitor.imports:
                    imp.file_path = path

                self.functions.extend(visitor.functions)
                self.classes.extend(visitor.classes)
                self.call_sites.extend(visitor.call_sites)
                self.imports.extend(visitor.imports)
            except SyntaxError as e:
                # File has syntax error: skip without crashing the index
                pass

        self._rebuild_graph()
        return len(changed_paths)

    def index_directory(self, root_dir: str, patterns: list[str] | None = None, incremental: bool = True) -> int:
        """Indexes all Python files under root_dir."""
        patterns = patterns or ["*.py"]
        files = {}
        for root, _, filenames in os.walk(root_dir):
            # Ignore hidden or virtual env folders
            if any(part.startswith(".") or part in ("venv", "node_modules", "__pycache__") for part in root.split(os.sep)):
                continue
            for filename in filenames:
                if any(fnmatch.fnmatch(filename, pat) for pat in patterns):
                    full_path = os.path.join(root, filename)
                    rel_path = os.path.relpath(full_path, root_dir).replace("\\", "/")
                    try:
                        with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                            files[rel_path] = f.read()
                    except Exception:
                        pass
        return self.index_files(files, incremental=incremental)

    def _rebuild_graph(self) -> None:
        """Rebuilds caller and callee cross-references."""
        self.callers_map.clear()
        self.callees_map.clear()

        for cs in self.call_sites:
            # Map callee -> callers
            self.callers_map.setdefault(cs.callee_name, []).append(cs)

            # Map caller -> callees
            if cs.caller_function:
                self.callees_map.setdefault(cs.caller_function, set()).add(cs.callee_name)

    def get_function(self, name: str, file_path: str | None = None) -> FunctionSymbol | None:
        """Finds a function by its simple name or full name."""
        for f in self.functions:
            if file_path and f.file_path != file_path:
                continue
            if f.name == name or f.name.endswith(f".{name}"):
                return f
        return None

    def get_callers(self, func_name: str) -> list[CallSite]:
        """Returns all call sites in the repository that invoke func_name."""
        simple_name = func_name.split(".")[-1]
        return list(self.callers_map.get(simple_name, []))

    def get_callees(self, func_name: str) -> list[str]:
        """Returns all functions invoked by the specified function."""
        return sorted(list(self.callees_map.get(func_name, set())))

    def get_file_symbols(self, file_path: str) -> list[FunctionSymbol]:
        """Returns all function symbols defined in a file."""
        return [f for f in self.functions if f.file_path == file_path]

    def get_file_classes(self, file_path: str) -> list[ClassSymbol]:
        """Returns all class symbols defined in a file."""
        return [c for c in self.classes if c.file_path == file_path]

    def get_imports(self, file_path: str) -> list[ImportInfo]:
        """Returns all imports in a file."""
        return [imp for imp in self.imports if getattr(imp, "file_path", "") == file_path]

    def search_similar(self, query: str, top_k: int = 5) -> list[FunctionSymbol]:
        """
        Searches symbols matching the query using token-weighted keyword relevance.
        Matches function names, parameters, docstrings, and source code.
        """
        if not query or not self.functions:
            return []

        tokens = [t.lower() for t in re.findall(r"\w+", query) if len(t) > 2]
        if not tokens:
            return self.functions[:top_k]

        scored: list[tuple[float, FunctionSymbol]] = []
        for func in self.functions:
            score = 0.0
            name_lower = func.name.lower()
            doc_lower = (func.docstring or "").lower()
            src_lower = func.source_code.lower()
            params_lower = " ".join(p.name.lower() for p in func.parameters)

            for token in tokens:
                if token in name_lower:
                    score += 5.0
                if token in params_lower:
                    score += 2.5
                if token in doc_lower:
                    score += 2.0
                if token in src_lower:
                    score += 1.0

            if score > 0:
                scored.append((score, func))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [item[1] for item in scored[:top_k]]

    def export_summary(self) -> dict:
        """Returns an overview of the indexed codebase."""
        return {
            "total_files": len(self._file_hashes),
            "total_functions": len(self.functions),
            "total_classes": len(self.classes),
            "total_call_sites": len(self.call_sites),
            "functions": [f.name for f in self.functions],
            "classes": [c.name for c in self.classes],
        }
