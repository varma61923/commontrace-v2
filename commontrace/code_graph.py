"""Deterministic Python/TypeScript structure; parsing never imports or executes source."""
from __future__ import annotations

import ast
import hashlib
import os
import re
import uuid
from pathlib import Path

from commontrace import graph, memory_authority, trace_io
from commontrace.exceptions import CapabilityError, DomainError
from commontrace.ingest import _open_regular

MAX_BYTES = 4*1024*1024
MAX_ENTITIES = 10000


def _call_name(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return parent+"."+node.attr if parent else ""
    return ""


def python_structure(text: str) -> tuple[list[dict], list[dict]]:
    tree = ast.parse(text)
    symbols, edges = [], []
    class Visitor(ast.NodeVisitor):
        stack: list[str] = []

        def definition(self, node, kind):
            name = ".".join([*self.stack, node.name])
            symbols.append({"name": name, "kind": kind, "line": node.lineno,
                            "end_line": getattr(node, "end_lineno", node.lineno)})
            parent = ".".join(self.stack)
            edges.append({"source": parent, "target": name, "relation": "contains"})
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_ClassDef(self, node):
            name = ".".join([*self.stack, node.name])
            for base in node.bases:
                target = _call_name(base)
                if target:
                    edges.append({"source": name, "target": target, "relation": "extends"})
            self.definition(node, "class")

        def visit_FunctionDef(self, node):
            self.definition(node, "function")

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Import(self, node):
            for alias in node.names:
                edges.append({"source": ".".join(self.stack), "target": alias.name, "relation": "depends_on"})

        def visit_ImportFrom(self, node):
            module = "."*node.level+(node.module or "")
            for alias in node.names:
                edges.append({"source": ".".join(self.stack), "target": module+"."+alias.name,
                              "relation": "depends_on"})

        def visit_Call(self, node):
            target = _call_name(node.func)
            if target:
                if target.startswith(("self.", "cls.")) and len(self.stack) >= 2:
                    target = self.stack[-2]+"."+target.split(".", 1)[1]
                edges.append({"source": ".".join(self.stack), "target": target, "relation": "uses"})
            self.generic_visit(node)
    Visitor().visit(tree)
    return symbols, edges


def typescript_structure(text: str, *, tsx: bool = False) -> tuple[list[dict], list[dict]]:
    try:
        import tree_sitter_typescript
        from tree_sitter import Language, Parser
    except ImportError:
        raise CapabilityError("TypeScript graphs require commontrace[code]") from None
    source = text.encode()
    language = tree_sitter_typescript.language_tsx() if tsx else tree_sitter_typescript.language_typescript()
    root = Parser(Language(language)).parse(source).root_node
    if root.has_error:
        raise DomainError("TypeScript source contains syntax errors")
    symbols, edges = [], []
    def name(node):
        return source[node.start_byte:node.end_byte].decode() if node else ""
    todo = [(root, "")]
    while todo:
        node, parent = todo.pop()
        if node.type in ("class_declaration", "function_declaration", "method_definition", "interface_declaration"):
            identifier = name(node.child_by_field_name("name"))
            if identifier:
                full = parent+"."+identifier if parent else identifier
                symbols.append({"name": full, "kind": node.type, "line": node.start_point.row+1,
                                "end_line": node.end_point.row+1})
                edges.append({"source": parent, "target": full, "relation": "contains"})
                parent = full
        elif node.type == "variable_declarator":
            value = node.child_by_field_name("value")
            identifier = name(node.child_by_field_name("name"))
            if value and value.type in ("arrow_function", "function_expression") and identifier:
                full = parent+"."+identifier if parent else identifier
                symbols.append({"name": full, "kind": "function", "line": node.start_point.row+1,
                                "end_line": node.end_point.row+1})
                edges.append({"source": parent, "target": full, "relation": "contains"})
                parent = full
        elif node.type == "import_statement":
            target = name(node.child_by_field_name("source")).strip("\"'")
            if target:
                edges.append({"source": parent, "target": target, "relation": "depends_on"})
        elif node.type == "call_expression":
            target = name(node.child_by_field_name("function"))
            if target and re.fullmatch(r"[\w$]+(?:\.[\w$]+)*", target):
                if target.startswith("this.") and "." in parent:
                    target = parent.rsplit(".", 1)[0]+target[4:]
                edges.append({"source": parent, "target": target, "relation": "uses"})
        if len(symbols)+len(edges) > MAX_ENTITIES:
            raise DomainError("code graph exceeds the entity budget")
        todo.extend((child, parent) for child in reversed(node.named_children))
    return symbols, edges


def extract(text: str, *, language: str = "python") -> dict:
    if not isinstance(text, str) or len(text.encode()) > MAX_BYTES:
        raise DomainError("code input exceeds 4 MiB")
    if language not in ("python", "typescript", "tsx"):
        raise DomainError("code language must be python, typescript or tsx")
    try:
        symbols, edges = (python_structure(text) if language == "python" else
                          typescript_structure(text, tsx=language == "tsx"))
    except (SyntaxError, RecursionError):
        raise DomainError("source syntax or nesting exceeds parser limits") from None
    if len(symbols)+len(edges) > MAX_ENTITIES:
        raise DomainError("code graph exceeds the entity budget")
    return {"language": language, "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "symbols": symbols, "edges": edges}


def ingest_file(root: str, filename: str, *, context: list[str], project: str = "") -> dict:
    if not context or any(not isinstance(s, str) or not s.strip() or len(s) > 256 or
                          any(ord(c) < 32 or ord(c) == 127 for c in s) for s in context):
        raise DomainError("code graph requires owner scopes")
    language = {".py": "python", ".ts": "typescript", ".tsx": "tsx"}.get(Path(filename).suffix)
    if language is None:
        raise DomainError("unsupported code file suffix")
    with _open_regular(filename, MAX_BYTES) as stream:
        text = stream.read(MAX_BYTES+1).decode("utf-8")
    parsed = extract(text, language=language)
    labels = sorted(set(context))
    source_key = hashlib.sha256(repr((project, os.path.abspath(filename), labels)).encode()).hexdigest()
    identity = hashlib.sha256((source_key+parsed["sha256"]+uuid.uuid4().hex).encode()).hexdigest()
    prefix = "code:"+identity[:32]+":"
    names = {row["name"] for row in parsed["symbols"]}
    nodes = {"": {"name": os.path.basename(filename), "kind": "file"}}
    nodes.update({row["name"]: row for row in parsed["symbols"]})
    for edge in parsed["edges"]:
        nodes.setdefault(edge["target"], {"name": edge["target"], "kind": "external_symbol"})
    def ident(name):
        return prefix+hashlib.sha256(name.encode()).hexdigest()[:24]
    with memory_authority.restricted_writer("code-graph", "external"):
        source_path = trace_io.write_new(root, title="Code structure "+os.path.basename(filename),
            context="Source SHA256 "+parsed["sha256"], solution="Parsed structure; no source code executed.",
            tags=["code-graph"], extra={"scopes": labels})
        trace = trace_io.read(source_path)[0]
        provenance = {"source_path": source_path, "run_id": identity, "detail": {"source_trace_id": trace["id"]}}
        with graph.batch(root):
            previous = [node.id for node in graph.load_nodes(root).values()
                        if node.properties.get("code_source") == source_key and not node.is_forgotten]
            for name, node in nodes.items():
                graph.add_node(root, ident(name), "file" if not name else "symbol", name=node["name"],
                               properties={**node, "scopes": labels, "source_sha256": parsed["sha256"],
                                           "code_source": source_key,
                                           "source_traces": [trace["id"]]}, provenance=provenance)
            for edge in parsed["edges"]:
                graph.add_edge(root, ident(edge["source"]), ident(edge["target"]), edge["relation"],
                    properties={"scopes": labels, "source_traces": [trace["id"]],
                                "resolution": "local" if edge["target"] in names else "syntactic"},
                    provenance=provenance)
            for node_id in set(previous)-{ident(name) for name in nodes}:
                graph.forget_node(root, node_id, reason="superseded code generation", provenance=provenance)
    return {"source_sha256": parsed["sha256"], "source_trace_id": trace["id"],
            "nodes": len(nodes), "edges": len(parsed["edges"]), "scopes": labels}
