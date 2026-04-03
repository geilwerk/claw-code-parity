from __future__ import annotations

import ast
import re
from collections import defaultdict, deque
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent
RUST_CRATES_ROOT = REPO_ROOT / 'rust' / 'crates'

RUST_STRUCT_RE = re.compile(r'^(?:pub(?:\([^)]*\))?\s+)?struct\s+([A-Za-z_][A-Za-z0-9_]*)')
RUST_ENUM_RE = re.compile(r'^(?:pub(?:\([^)]*\))?\s+)?enum\s+([A-Za-z_][A-Za-z0-9_]*)')
RUST_TRAIT_RE = re.compile(r'^(?:pub(?:\([^)]*\))?\s+)?trait\s+([A-Za-z_][A-Za-z0-9_]*)')
RUST_FUNCTION_RE = re.compile(
    r'^(?:pub(?:\([^)]*\))?\s+)?(?:const\s+)?(?:async\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)'
)
RUST_CALL_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_:.\']*)\s*\(')
RUST_TYPED_LET_RE = re.compile(r'\blet\s+(?:mut\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([^=;]+)')
RUST_PATH_LET_RE = re.compile(r'\blet\s+(?:mut\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([A-Za-z_][A-Za-z0-9_:<>]*)')
RUST_CONTROL_CALL_HEADS = {'if', 'for', 'loop', 'match', 'return', 'while'}

PYTHON_BUILTIN_QNAMES = {
    'dict': 'python.builtins.dict',
    'len': 'python.builtins.len',
    'list': 'python.builtins.list',
    'max': 'python.builtins.max',
    'min': 'python.builtins.min',
    'set': 'python.builtins.set',
    'sorted': 'python.builtins.sorted',
}
PYTHON_CONTAINER_METHOD_QNAMES = (
    'python.builtins.list.append',
    'python.builtins.list.extend',
    'python.builtins.list.pop',
    'python.builtins.dict.get',
    'python.builtins.dict.items',
    'python.builtins.dict.pop',
    'python.builtins.dict.values',
    'python.builtins.set.add',
)


@dataclass(frozen=True)
class CodeSymbol:
    qualified_name: str
    module: str
    file_path: str
    line: int
    kind: str
    language: str

    def to_dict(self) -> dict[str, str | int]:
        return {
            'qualified_name': self.qualified_name,
            'module': self.module,
            'file_path': self.file_path,
            'line': self.line,
            'kind': self.kind,
            'language': self.language,
        }


@dataclass(frozen=True)
class CallEdge:
    caller: str
    callee: str
    line: int
    language: str

    def to_dict(self) -> dict[str, str | int]:
        return {
            'caller': self.caller,
            'callee': self.callee,
            'line': self.line,
            'language': self.language,
        }


@dataclass(frozen=True)
class ImportEdge:
    importer: str
    imported: str
    language: str

    def to_dict(self) -> dict[str, str]:
        return {
            'importer': self.importer,
            'imported': self.imported,
            'language': self.language,
        }


@dataclass(frozen=True)
class YieldEvent:
    producer: str
    event_type: str | None
    keys: tuple[str, ...]
    line: int
    language: str

    def to_dict(self) -> dict[str, str | int | list[str] | None]:
        return {
            'producer': self.producer,
            'event_type': self.event_type,
            'keys': list(self.keys),
            'line': self.line,
            'language': self.language,
        }


@dataclass(frozen=True)
class TracePath:
    nodes: tuple[str, ...]

    def as_lines(self) -> list[str]:
        if not self.nodes:
            return ['No path found.']
        return [f'{idx + 1}. {node}' for idx, node in enumerate(self.nodes)]

    def to_dict(self) -> dict[str, list[str]]:
        return {'nodes': list(self.nodes)}


@dataclass(frozen=True)
class CodeIndex:
    symbols: tuple[CodeSymbol, ...]
    call_edges: tuple[CallEdge, ...]
    import_edges: tuple[ImportEdge, ...]
    yield_events: tuple[YieldEvent, ...]

    def symbol_map(self) -> dict[str, CodeSymbol]:
        return {symbol.qualified_name: symbol for symbol in self.symbols}

    def resolve_symbol(self, name: str) -> str:
        exact = self.symbol_map().get(name)
        if exact:
            return exact.qualified_name
        suffix_matches = sorted(
            symbol.qualified_name
            for symbol in self.symbols
            if symbol.qualified_name.endswith(name)
        )
        if len(suffix_matches) == 1:
            return suffix_matches[0]
        if not suffix_matches:
            raise KeyError(f'Unknown symbol: {name}')
        preview = ', '.join(suffix_matches[:6])
        raise KeyError(f'Ambiguous symbol {name!r}: {preview}')

    def find_symbols(self, query: str, limit: int = 20) -> list[CodeSymbol]:
        needle = query.lower()
        matches = [
            symbol
            for symbol in self.symbols
            if needle in symbol.qualified_name.lower()
            or needle in symbol.file_path.lower()
            or needle in symbol.kind.lower()
            or needle in symbol.language.lower()
        ]
        return matches[:limit]

    def module_names(self) -> set[str]:
        return {symbol.module for symbol in self.symbols} | {edge.importer for edge in self.import_edges}

    def resolve_module(self, name: str) -> str:
        modules = self.module_names()
        if name in modules:
            return name

        exact_symbol = self.symbol_map().get(name)
        if exact_symbol:
            return exact_symbol.module

        module_suffix_matches = sorted(module for module in modules if module.endswith(name))
        if len(module_suffix_matches) == 1:
            return module_suffix_matches[0]

        symbol_suffix_matches = sorted(
            {symbol.module for symbol in self.symbols if symbol.qualified_name.endswith(name)}
        )
        if len(symbol_suffix_matches) == 1:
            return symbol_suffix_matches[0]

        if module_suffix_matches or symbol_suffix_matches:
            preview = ', '.join((module_suffix_matches + symbol_suffix_matches)[:6])
            raise KeyError(f'Ambiguous module {name!r}: {preview}')
        raise KeyError(f'Unknown module or symbol: {name}')

    def callers_of(self, target: str) -> list[CallEdge]:
        resolved = self.resolve_symbol(target)
        return sorted(
            [edge for edge in self.call_edges if edge.callee == resolved],
            key=lambda edge: (edge.caller, edge.line),
        )

    def callees_of(self, source: str) -> list[CallEdge]:
        resolved = self.resolve_symbol(source)
        return sorted(
            [edge for edge in self.call_edges if edge.caller == resolved],
            key=lambda edge: (edge.callee, edge.line),
        )

    def events_of(self, source: str) -> list[YieldEvent]:
        resolved = self.resolve_symbol(source)
        return sorted(
            [event for event in self.yield_events if event.producer == resolved],
            key=lambda event: (event.line, event.event_type or ''),
        )

    def imports_of(self, source: str, include_external: bool = False) -> list[ImportEdge]:
        resolved = self.resolve_module(source)
        internal_modules = self.module_names()
        return sorted(
            [
                edge
                for edge in self.import_edges
                if edge.importer == resolved and (include_external or edge.imported in internal_modules)
            ],
            key=lambda edge: (edge.imported, edge.language),
        )

    def importers_of(self, target: str, include_external: bool = False) -> list[ImportEdge]:
        resolved = self.resolve_module(target)
        internal_modules = self.module_names()
        return sorted(
            [
                edge
                for edge in self.import_edges
                if edge.imported == resolved and (include_external or edge.importer in internal_modules)
            ],
            key=lambda edge: (edge.importer, edge.language),
        )

    def trace_path(self, start: str, target: str) -> TracePath:
        start_qname = self.resolve_symbol(start)
        target_qname = self.resolve_symbol(target)
        if start_qname == target_qname:
            return TracePath(nodes=(start_qname,))

        adjacency: dict[str, set[str]] = defaultdict(set)
        for edge in self.call_edges:
            adjacency[edge.caller].add(edge.callee)

        queue: deque[str] = deque([start_qname])
        prev: dict[str, str | None] = {start_qname: None}

        while queue:
            current = queue.popleft()
            for neighbor in sorted(adjacency.get(current, ())):
                if neighbor in prev:
                    continue
                prev[neighbor] = current
                if neighbor == target_qname:
                    path: list[str] = [target_qname]
                    walk: str | None = current
                    while walk is not None:
                        path.append(walk)
                        walk = prev[walk]
                    path.reverse()
                    return TracePath(nodes=tuple(path))
                queue.append(neighbor)

        return TracePath(nodes=())

    def trace_import_path(self, start: str, target: str) -> TracePath:
        start_module = self.resolve_module(start)
        target_module = self.resolve_module(target)
        if start_module == target_module:
            return TracePath(nodes=(start_module,))

        internal_modules = self.module_names()
        adjacency: dict[str, set[str]] = defaultdict(set)
        for edge in self.import_edges:
            if edge.importer in internal_modules and edge.imported in internal_modules:
                adjacency[edge.importer].add(edge.imported)

        queue: deque[str] = deque([start_module])
        prev: dict[str, str | None] = {start_module: None}

        while queue:
            current = queue.popleft()
            for neighbor in sorted(adjacency.get(current, ())):
                if neighbor in prev:
                    continue
                prev[neighbor] = current
                if neighbor == target_module:
                    path: list[str] = [target_module]
                    walk: str | None = current
                    while walk is not None:
                        path.append(walk)
                        walk = prev[walk]
                    path.reverse()
                    return TracePath(nodes=tuple(path))
                queue.append(neighbor)

        return TracePath(nodes=())

    def graph_payload(
        self,
        kind: str,
        scope: str = 'all',
        include_external: bool = False,
        focus: str | None = None,
        max_depth: int = 1,
        direction: str = 'both',
    ) -> dict[str, object]:
        if kind not in {'imports', 'calls'}:
            raise ValueError(f'Unsupported graph kind: {kind}')
        if scope not in {'all', 'python', 'rust'}:
            raise ValueError(f'Unsupported graph scope: {scope}')
        if max_depth < 0:
            raise ValueError('max_depth must be >= 0')
        if direction not in {'both', 'in', 'out'}:
            raise ValueError(f'Unsupported graph direction: {direction}')

        if kind == 'calls':
            edges = [
                edge
                for edge in self.call_edges
                if scope == 'all' or edge.language == scope
            ]
            nodes = sorted({edge.caller for edge in edges} | {edge.callee for edge in edges})
            resolved_focus = None
            if focus:
                resolved_focus = self.resolve_symbol(focus)
                nodes, edges = self._filter_graph_edges(
                    edges=edges,
                    nodes=set(nodes),
                    get_source=lambda edge: edge.caller,
                    get_target=lambda edge: edge.callee,
                    focus=resolved_focus,
                    max_depth=max_depth,
                    direction=direction,
                )
            return {
                'kind': kind,
                'scope': scope,
                'focus': resolved_focus,
                'max_depth': max_depth if focus else None,
                'direction': direction if focus else None,
                'nodes': [{'id': node} for node in nodes],
                'edges': [edge.to_dict() for edge in edges],
            }

        internal_modules = self.module_names()
        edges = [
            edge
            for edge in self.import_edges
            if (scope == 'all' or edge.language == scope)
            and (include_external or edge.imported in internal_modules)
        ]
        nodes = sorted({edge.importer for edge in edges} | {edge.imported for edge in edges})
        resolved_focus = None
        if focus:
            resolved_focus = self.resolve_module(focus)
            nodes, edges = self._filter_graph_edges(
                edges=edges,
                nodes=set(nodes),
                get_source=lambda edge: edge.importer,
                get_target=lambda edge: edge.imported,
                focus=resolved_focus,
                max_depth=max_depth,
                direction=direction,
            )
        return {
            'kind': kind,
            'scope': scope,
            'include_external': include_external,
            'focus': resolved_focus,
            'max_depth': max_depth if focus else None,
            'direction': direction if focus else None,
            'nodes': [{'id': node} for node in nodes],
            'edges': [edge.to_dict() for edge in edges],
        }

    def render_dot(
        self,
        kind: str,
        scope: str = 'all',
        include_external: bool = False,
        focus: str | None = None,
        max_depth: int = 1,
        direction: str = 'both',
    ) -> str:
        payload = self.graph_payload(
            kind=kind,
            scope=scope,
            include_external=include_external,
            focus=focus,
            max_depth=max_depth,
            direction=direction,
        )
        lines = [f'digraph {kind} {{', '  rankdir=LR;']
        for node in payload['nodes']:
            lines.append(f'  "{node["id"]}";')
        for edge in payload['edges']:
            label = f' [label="{edge["language"]}"]' if scope == 'all' else ''
            if kind == 'calls':
                lines.append(f'  "{edge["caller"]}" -> "{edge["callee"]}"{label};')
            else:
                lines.append(f'  "{edge["importer"]}" -> "{edge["imported"]}"{label};')
        lines.append('}')
        return '\n'.join(lines)

    def _filter_graph_edges(
        self,
        edges: list[CallEdge] | list[ImportEdge],
        nodes: set[str],
        get_source,
        get_target,
        focus: str,
        max_depth: int,
        direction: str,
    ) -> tuple[list[str], list[CallEdge] | list[ImportEdge]]:
        if focus not in nodes:
            raise KeyError(f'Unknown graph focus: {focus}')

        forward: dict[str, set[str]] = defaultdict(set)
        reverse: dict[str, set[str]] = defaultdict(set)
        for edge in edges:
            source = get_source(edge)
            target = get_target(edge)
            forward[source].add(target)
            reverse[target].add(source)

        visited: set[str] = {focus}
        queue: deque[tuple[str, int]] = deque([(focus, 0)])

        while queue:
            node, depth = queue.popleft()
            if depth >= max_depth:
                continue
            neighbors: set[str] = set()
            if direction in {'both', 'out'}:
                neighbors.update(forward.get(node, ()))
            if direction in {'both', 'in'}:
                neighbors.update(reverse.get(node, ()))
            for neighbor in sorted(neighbors):
                if neighbor in visited:
                    continue
                visited.add(neighbor)
                queue.append((neighbor, depth + 1))

        filtered_edges = [
            edge
            for edge in edges
            if get_source(edge) in visited and get_target(edge) in visited
        ]
        filtered_nodes = sorted(visited)
        return filtered_nodes, filtered_edges

    def summary(self) -> dict[str, object]:
        modules_by_language: dict[str, set[str]] = defaultdict(set)
        symbol_counts_by_language: dict[str, int] = defaultdict(int)
        kind_counts: dict[str, int] = defaultdict(int)
        import_counts_by_language: dict[str, int] = defaultdict(int)
        call_counts_by_language: dict[str, int] = defaultdict(int)
        event_counts_by_language: dict[str, int] = defaultdict(int)

        for symbol in self.symbols:
            modules_by_language[symbol.language].add(symbol.module)
            symbol_counts_by_language[symbol.language] += 1
            kind_counts[symbol.kind] += 1

        for edge in self.import_edges:
            modules_by_language[edge.language].add(edge.importer)
            import_counts_by_language[edge.language] += 1

        for edge in self.call_edges:
            call_counts_by_language[edge.language] += 1

        for event in self.yield_events:
            event_counts_by_language[event.language] += 1

        languages = sorted(
            set(modules_by_language)
            | set(symbol_counts_by_language)
            | set(import_counts_by_language)
            | set(call_counts_by_language)
            | set(event_counts_by_language)
        )
        language_breakdown = {
            language: {
                'modules': len(modules_by_language[language]),
                'symbols': symbol_counts_by_language[language],
                'import_edges': import_counts_by_language[language],
                'call_edges': call_counts_by_language[language],
                'yield_events': event_counts_by_language[language],
            }
            for language in languages
        }

        fan_out: dict[str, int] = defaultdict(int)
        for edge in self.call_edges:
            fan_out[edge.caller] += 1

        return {
            'modules': sum(len(modules) for modules in modules_by_language.values()),
            'symbols': len(self.symbols),
            'kinds': dict(sorted(kind_counts.items())),
            'import_edges': len(self.import_edges),
            'call_edges': len(self.call_edges),
            'yield_events': len(self.yield_events),
            'languages': language_breakdown,
            'top_callers': [
                {'symbol': symbol_name, 'count': count}
                for symbol_name, count in sorted(fan_out.items(), key=lambda item: (-item[1], item[0]))[:10]
            ],
        }

    def to_dict(self) -> dict[str, object]:
        return {
            'summary': self.summary(),
            'symbols': [symbol.to_dict() for symbol in self.symbols],
            'import_edges': [edge.to_dict() for edge in self.import_edges],
            'call_edges': [edge.to_dict() for edge in self.call_edges],
            'yield_events': [event.to_dict() for event in self.yield_events],
        }

    def render_index_summary(self) -> str:
        summary = self.summary()
        kinds = summary['kinds']
        lines = [
            '# Code Analysis Index',
            '',
            f"Modules: {summary['modules']}",
            f"Symbols: {summary['symbols']}",
            f"Classes: {kinds.get('class', 0)}",
            f"Structs: {kinds.get('struct', 0)}",
            f"Enums: {kinds.get('enum', 0)}",
            f"Traits: {kinds.get('trait', 0)}",
            f"Functions: {kinds.get('function', 0)}",
            f"Methods: {kinds.get('method', 0)}",
            f"Import edges: {summary['import_edges']}",
            f"Call edges: {summary['call_edges']}",
            f"Yield events: {summary['yield_events']}",
            '',
            'Language breakdown:',
        ]
        for language, values in summary['languages'].items():
            lines.append(
                f"- {language}: modules={values['modules']} symbols={values['symbols']} "
                f"import_edges={values['import_edges']} call_edges={values['call_edges']} "
                f"yield_events={values['yield_events']}"
            )
        lines.extend(['', 'Top symbols by caller fan-out:'])
        for entry in summary['top_callers']:
            lines.append(f"- {entry['symbol']} ({entry['count']} calls)")
        return '\n'.join(lines)


@dataclass(frozen=True)
class _RustScope:
    name: str
    depth: int


@dataclass
class _RustFunctionScope:
    qualified_name: str
    depth: int
    owner_qname: str | None
    local_types: dict[str, str]


class _ImportAndSymbolCollector(ast.NodeVisitor):
    def __init__(self, module: str, is_package: bool) -> None:
        self.module = module
        self.is_package = is_package
        self.aliases: dict[str, str] = {}
        self.symbols: list[CodeSymbol] = []
        self.class_stack: list[str] = []
        self.class_names: set[str] = set()
        self.class_field_types: dict[str, dict[str, str]] = defaultdict(dict)
        self.function_depth = 0

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            imported = alias.name
            asname = alias.asname or imported.split('.')[-1]
            self.aliases[asname] = imported

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module_name = _resolve_imported_module(self.module, self.is_package, node.level, node.module)
        for alias in node.names:
            if alias.name == '*':
                continue
            asname = alias.asname or alias.name
            imported = f'{module_name}.{alias.name}' if module_name else alias.name
            self.aliases[asname] = imported

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qname = f'{self.module}.{node.name}'
        self.class_names.add(qname)
        self.symbols.append(
            CodeSymbol(
                qualified_name=qname,
                module=self.module,
                file_path='',
                line=node.lineno,
                kind='class',
                language='python',
            )
        )
        self.class_stack.append(node.name)
        self.generic_visit(node)
        self.class_stack.pop()

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if self.class_stack and self.function_depth == 0 and isinstance(node.target, ast.Name):
            class_qname = f'{self.module}.{".".join(self.class_stack)}'
            resolved_type = self._resolve_annotation_type(node.annotation)
            if resolved_type:
                self.class_field_types[class_qname][node.target.id] = resolved_type
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._record_function(node)
        self.function_depth += 1
        self.generic_visit(node)
        self.function_depth -= 1

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._record_function(node)
        self.function_depth += 1
        self.generic_visit(node)
        self.function_depth -= 1

    def _record_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if self.class_stack:
            qname = f'{self.module}.{".".join(self.class_stack)}.{node.name}'
            kind = 'method'
        else:
            qname = f'{self.module}.{node.name}'
            kind = 'function'
        self.symbols.append(
            CodeSymbol(
                qualified_name=qname,
                module=self.module,
                file_path='',
                line=node.lineno,
                kind=kind,
                language='python',
            )
        )

    def _resolve_annotation_type(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            if node.id in PYTHON_BUILTIN_QNAMES:
                return PYTHON_BUILTIN_QNAMES[node.id]
            return self.aliases.get(node.id, f'{self.module}.{node.id}')
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in PYTHON_BUILTIN_QNAMES:
                return PYTHON_BUILTIN_QNAMES[node.value]
            return self.aliases.get(node.value, f'{self.module}.{node.value}')
        if isinstance(node, ast.Subscript):
            return self._resolve_annotation_type(node.value)
        if isinstance(node, ast.Attribute):
            parts: list[str] = []
            current: ast.AST | None = node
            while isinstance(current, ast.Attribute):
                parts.append(current.attr)
                current = current.value
            if isinstance(current, ast.Name):
                parts.append(current.id)
                dotted = '.'.join(reversed(parts))
                head, _, tail = dotted.partition('.')
                if head in self.aliases:
                    return f'{self.aliases[head]}.{tail}' if tail else self.aliases[head]
                return dotted
        return None


class _CallCollector(ast.NodeVisitor):
    def __init__(
        self,
        module: str,
        aliases: dict[str, str],
        class_names: set[str],
        symbol_map: dict[str, CodeSymbol],
        class_field_types: dict[str, dict[str, str]],
    ) -> None:
        self.module = module
        self.aliases = aliases
        self.class_names = class_names
        self.symbol_map = symbol_map
        self.class_field_types = class_field_types
        self.calls: list[CallEdge] = []
        self.yield_events: list[YieldEvent] = []
        self.class_stack: list[str] = []
        self.function_stack: list[str] = []
        self.local_types_stack: list[dict[str, str]] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.class_stack.append(node.name)
        self.generic_visit(node)
        self.class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        inferred = self._infer_instance_type(node.value)
        if inferred and self.local_types_stack:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.local_types_stack[-1][target.id] = inferred
        for target in node.targets:
            if not isinstance(target, ast.Name):
                self.visit(target)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
            inferred = self._infer_instance_type(node.value)
            if inferred and self.local_types_stack and isinstance(node.target, ast.Name):
                self.local_types_stack[-1][node.target.id] = inferred
        if self.local_types_stack and isinstance(node.target, ast.Name):
            annotated = self._resolve_annotation_type(node.annotation)
            if annotated:
                self.local_types_stack[-1].setdefault(node.target.id, annotated)
        self.visit(node.target)

    def visit_Call(self, node: ast.Call) -> None:
        caller = self._current_caller()
        callee = self._resolve_callable(node.func)
        if caller and callee:
            self.calls.append(CallEdge(caller=caller, callee=callee, line=node.lineno, language='python'))
        self.generic_visit(node)

    def visit_Yield(self, node: ast.Yield) -> None:
        producer = self._current_caller()
        event = self._extract_yield_event(producer, node)
        if event:
            self.yield_events.append(event)
        self.generic_visit(node)

    def visit_YieldFrom(self, node: ast.YieldFrom) -> None:
        self.generic_visit(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if self.class_stack:
            qname = f'{self.module}.{".".join(self.class_stack)}.{node.name}'
        else:
            qname = f'{self.module}.{node.name}'
        self.function_stack.append(qname)
        self.local_types_stack.append({})
        self.generic_visit(node)
        self.local_types_stack.pop()
        self.function_stack.pop()

    def _current_caller(self) -> str | None:
        if not self.function_stack:
            return None
        return self.function_stack[-1]

    def _current_class_qname(self) -> str | None:
        if not self.class_stack:
            return None
        return f'{self.module}.{".".join(self.class_stack)}'

    def _lookup_local_type(self, name: str) -> str | None:
        for scope in reversed(self.local_types_stack):
            if name in scope:
                return scope[name]
        return None

    def _lookup_class_field_type(self, name: str) -> str | None:
        class_qname = self._current_class_qname()
        if not class_qname:
            return None
        return self.class_field_types.get(class_qname, {}).get(name)

    def _resolve_annotation_type(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            if node.id in PYTHON_BUILTIN_QNAMES:
                return PYTHON_BUILTIN_QNAMES[node.id]
            return self.aliases.get(node.id, f'{self.module}.{node.id}')
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in PYTHON_BUILTIN_QNAMES:
                return PYTHON_BUILTIN_QNAMES[node.value]
            return self.aliases.get(node.value, f'{self.module}.{node.value}')
        if isinstance(node, ast.Subscript):
            return self._resolve_annotation_type(node.value)
        if isinstance(node, ast.Attribute):
            parts: list[str] = []
            current: ast.AST | None = node
            while isinstance(current, ast.Attribute):
                parts.append(current.attr)
                current = current.value
            if isinstance(current, ast.Name):
                parts.append(current.id)
                dotted = '.'.join(reversed(parts))
                head, _, tail = dotted.partition('.')
                if head in self.aliases:
                    return f'{self.aliases[head]}.{tail}' if tail else self.aliases[head]
                return dotted
        return None

    def _resolve_name(self, name: str) -> str | None:
        local_type = self._lookup_local_type(name)
        if local_type:
            return local_type
        if name in self.aliases:
            return self.aliases[name]
        same_module = f'{self.module}.{name}'
        if same_module in self.symbol_map:
            return same_module
        same_class = self._current_class_qname()
        if same_class:
            method_name = f'{same_class}.{name}'
            if method_name in self.symbol_map:
                return method_name
        if name in PYTHON_BUILTIN_QNAMES:
            return PYTHON_BUILTIN_QNAMES[name]
        return None

    def _resolve_callable(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return self._resolve_name(node.id)

        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id == 'self':
                class_qname = self._current_class_qname()
                if class_qname:
                    return f'{class_qname}.{node.attr}'

            if (
                isinstance(node.value, ast.Attribute)
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id == 'self'
            ):
                field_type = self._lookup_class_field_type(node.value.attr)
                if field_type:
                    candidate = f'{field_type}.{node.attr}'
                    if candidate in self.symbol_map:
                        return candidate

            if isinstance(node.value, ast.Name):
                base = self._resolve_name(node.value.id)
                if base:
                    return f'{base}.{node.attr}'

            if isinstance(node.value, ast.Call):
                instance_type = self._infer_instance_type(node.value)
                if instance_type:
                    candidate = f'{instance_type}.{node.attr}'
                    return candidate if candidate in self.symbol_map else None

            instance_type = self._infer_instance_type(node.value)
            if instance_type:
                candidate = f'{instance_type}.{node.attr}'
                if candidate in self.symbol_map:
                    return candidate

        return None

    def _infer_instance_type(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.List | ast.ListComp):
            return PYTHON_BUILTIN_QNAMES['list']
        if isinstance(node, ast.Dict | ast.DictComp):
            return PYTHON_BUILTIN_QNAMES['dict']
        if isinstance(node, ast.Set | ast.SetComp):
            return PYTHON_BUILTIN_QNAMES['set']
        if isinstance(node, ast.Name):
            resolved = self._resolve_name(node.id)
            if resolved in self.class_names:
                return resolved
            if resolved in PYTHON_BUILTIN_QNAMES.values():
                return resolved
            return None

        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                resolved = self._resolve_name(node.func.id)
                if resolved in self.class_names:
                    return resolved
                if resolved in PYTHON_BUILTIN_QNAMES.values():
                    return resolved
            if isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Name):
                    base = self._resolve_name(node.func.value.id)
                    if base in self.class_names:
                        return base
                if isinstance(node.func.value, ast.Call):
                    return self._infer_instance_type(node.func.value)
        return None

    def _extract_yield_event(self, producer: str | None, node: ast.Yield) -> YieldEvent | None:
        if not producer or not isinstance(node.value, ast.Dict):
            return None

        keys: list[str] = []
        event_type: str | None = None

        for key_node, value_node in zip(node.value.keys, node.value.values):
            if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
                return None
            key = key_node.value
            keys.append(key)
            if key == 'type' and isinstance(value_node, ast.Constant) and isinstance(value_node.value, str):
                event_type = value_node.value

        return YieldEvent(
            producer=producer,
            event_type=event_type,
            keys=tuple(keys),
            line=node.lineno,
            language='python',
        )


def _resolve_imported_module(current_module: str, is_package: bool, level: int, imported_module: str | None) -> str:
    parts = current_module.split('.')
    if level > 0:
        anchor = parts if is_package else parts[:-1]
        base_parts = anchor[: len(anchor) - (level - 1)]
    else:
        base_parts = parts if is_package else parts[:-1]
    if imported_module:
        return '.'.join(base_parts + imported_module.split('.'))
    return '.'.join(base_parts)


def _module_name_for_path(path: Path, package_root: Path) -> str:
    relative = path.relative_to(package_root.parent)
    if path.name == '__init__.py':
        return '.'.join(relative.parent.parts)
    return '.'.join(relative.with_suffix('').parts)


def _python_builtin_symbols() -> list[CodeSymbol]:
    builtin_symbols = [
        CodeSymbol(
            qualified_name=qname,
            module='python.builtins',
            file_path='<builtin>',
            line=0,
            kind='builtin',
            language='python',
        )
        for qname in sorted(set(PYTHON_BUILTIN_QNAMES.values()) | set(PYTHON_CONTAINER_METHOD_QNAMES))
    ]
    return builtin_symbols


def _rust_module_name_for_path(path: Path) -> str:
    relative = path.relative_to(RUST_CRATES_ROOT)
    crate_name = relative.parts[0]
    src_relative = path.relative_to(RUST_CRATES_ROOT / crate_name / 'src')
    if src_relative.name in {'lib.rs', 'main.rs'}:
        tail: list[str] = []
    elif src_relative.name == 'mod.rs':
        tail = list(src_relative.parent.parts)
    else:
        tail = [*src_relative.parent.parts, src_relative.stem]
    return '::'.join(['rust', crate_name, *tail]) if tail else f'rust::{crate_name}'


def _split_top_level(value: str, delimiter: str = ',') -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for char in value:
        if char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
        if char == delimiter and depth == 0:
            part = ''.join(current).strip()
            if part:
                parts.append(part)
            current = []
            continue
        current.append(char)
    tail = ''.join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def _expand_rust_use_target(target: str) -> list[str]:
    cleaned = target.strip().rstrip(';')
    if '{' not in cleaned:
        if ' as ' in cleaned:
            cleaned = cleaned.split(' as ', 1)[0].strip()
        return [cleaned]

    prefix, remainder = cleaned.split('{', 1)
    inner, suffix = remainder.rsplit('}', 1)
    base = prefix.rstrip(':').strip()
    suffix = suffix.strip()
    expanded: list[str] = []
    for part in _split_top_level(inner):
        if part == 'self':
            combined = base
        elif base:
            combined = f'{base}::{part}'
        else:
            combined = part
        if suffix:
            combined = f'{combined}{suffix}'
        expanded.extend(_expand_rust_use_target(combined))
    return expanded


def _normalize_rust_use_target(
    current_module: str,
    target: str,
    module_names: set[str],
    workspace_crates: set[str],
) -> str:
    cleaned = target.strip().lstrip(':')
    if not cleaned:
        return current_module

    current_parts = current_module.split('::')
    crate_root = '::'.join(current_parts[:2])

    if cleaned.startswith('crate::'):
        return f'{crate_root}::{cleaned[7:]}'
    if cleaned.startswith('self::'):
        return f'{current_module}::{cleaned[6:]}'
    if cleaned.startswith('super::'):
        base_parts = current_parts[:-1]
        remainder = cleaned
        while remainder.startswith('super::'):
            remainder = remainder[7:]
            if len(base_parts) > 2:
                base_parts = base_parts[:-1]
        return '::'.join([*base_parts, remainder]) if remainder else '::'.join(base_parts)

    first_segment = cleaned.split('::', 1)[0]
    if first_segment in workspace_crates:
        return f'rust::{cleaned}'

    child_prefix = f'{current_module}::{first_segment}'
    if any(name == child_prefix or name.startswith(f'{child_prefix}::') for name in module_names):
        return f'{current_module}::{cleaned}'

    crate_child_prefix = f'{crate_root}::{first_segment}'
    if any(name == crate_child_prefix or name.startswith(f'{crate_child_prefix}::') for name in module_names):
        return f'{crate_root}::{cleaned}'

    return cleaned


def _strip_rust_line_comment(line: str) -> str:
    return line.split('//', 1)[0]


def _strip_rust_generics(value: str) -> str:
    cleaned = value
    while True:
        updated = re.sub(r'::?<[^<>]*>', '', cleaned)
        if updated == cleaned:
            return updated
        cleaned = updated


def _parse_rust_impl_target(header: str) -> str | None:
    cleaned = header.split('{', 1)[0].split('where', 1)[0].strip()
    if ' for ' in cleaned:
        candidate = cleaned.split(' for ', 1)[1]
    else:
        candidate = cleaned.split('impl', 1)[1]
    candidate = re.sub(r'<[^<>]*>', '', candidate).strip()
    candidate = candidate.lstrip('&')
    segments = [segment for segment in candidate.split('::') if segment]
    if not segments:
        return None
    last_segment = segments[-1]
    match = re.search(r'([A-Za-z_][A-Za-z0-9_]*)', last_segment)
    return match.group(1) if match else None


def _rust_source_files() -> list[Path]:
    if not RUST_CRATES_ROOT.exists():
        return []
    return sorted(
        path
        for path in RUST_CRATES_ROOT.rglob('*.rs')
        if 'src' in path.relative_to(RUST_CRATES_ROOT).parts
    )


def _build_python_index() -> tuple[list[CodeSymbol], list[ImportEdge], list[CallEdge], list[YieldEvent]]:
    symbols: list[CodeSymbol] = _python_builtin_symbols()
    imports: list[ImportEdge] = []
    call_edges: list[CallEdge] = []
    yield_events: list[YieldEvent] = []
    alias_maps: dict[str, dict[str, str]] = {}
    class_names: set[str] = set()
    class_field_types: dict[str, dict[str, str]] = {}

    python_files = sorted(path for path in PACKAGE_ROOT.rglob('*.py') if '__pycache__' not in path.parts)
    parsed_trees: dict[str, tuple[Path, ast.AST]] = {}

    for path in python_files:
        module = _module_name_for_path(path, PACKAGE_ROOT)
        tree = ast.parse(path.read_text(), filename=str(path))
        parsed_trees[module] = (path, tree)
        collector = _ImportAndSymbolCollector(module, path.name == '__init__.py')
        collector.visit(tree)
        alias_maps[module] = collector.aliases
        class_names.update(collector.class_names)
        class_field_types.update(collector.class_field_types)
        for symbol in collector.symbols:
            symbols.append(
                CodeSymbol(
                    qualified_name=symbol.qualified_name,
                    module=symbol.module,
                    file_path=str(path),
                    line=symbol.line,
                    kind=symbol.kind,
                    language='python',
                )
            )
        for imported in sorted(set(collector.aliases.values())):
            imports.append(ImportEdge(importer=module, imported=imported, language='python'))

    symbol_map = {symbol.qualified_name: symbol for symbol in symbols}

    for module, (_, tree) in parsed_trees.items():
        collector = _CallCollector(module, alias_maps[module], class_names, symbol_map, class_field_types)
        collector.visit(tree)
        call_edges.extend(collector.calls)
        yield_events.extend(collector.yield_events)

    return symbols, imports, call_edges, yield_events


def _build_rust_index() -> tuple[list[CodeSymbol], list[ImportEdge]]:
    rust_files = _rust_source_files()
    if not rust_files:
        return [], []

    workspace_crates = {path.name for path in RUST_CRATES_ROOT.iterdir() if path.is_dir()}
    module_names = {_rust_module_name_for_path(path) for path in rust_files}

    symbols: list[CodeSymbol] = []
    imports: list[ImportEdge] = []

    for path in rust_files:
        module = _rust_module_name_for_path(path)
        lines = path.read_text().splitlines()
        brace_depth = 0
        scopes: list[_RustScope] = []
        pending_impl_target: str | None = None
        pending_trait_name: str | None = None
        use_buffer: list[str] = []

        for lineno, raw_line in enumerate(lines, start=1):
            line = _strip_rust_line_comment(raw_line).strip()
            if not line:
                continue

            if use_buffer:
                use_buffer.append(line)
                if ';' in line:
                    statement = ' '.join(use_buffer)
                    import_spec = re.sub(r'^(?:pub\s+)?use\s+', '', statement).rstrip(';')
                    for target in _expand_rust_use_target(import_spec):
                        imports.append(
                            ImportEdge(
                                importer=module,
                                imported=_normalize_rust_use_target(module, target, module_names, workspace_crates),
                                language='rust',
                            )
                        )
                    use_buffer = []
            elif re.match(r'^(?:pub\s+)?use\b', line):
                use_buffer = [line]
                if ';' in line:
                    statement = ' '.join(use_buffer)
                    import_spec = re.sub(r'^(?:pub\s+)?use\s+', '', statement).rstrip(';')
                    for target in _expand_rust_use_target(import_spec):
                        imports.append(
                            ImportEdge(
                                importer=module,
                                imported=_normalize_rust_use_target(module, target, module_names, workspace_crates),
                                language='rust',
                            )
                        )
                    use_buffer = []

            module_match = re.match(r'^(?:pub\s+)?mod\s+([A-Za-z_][A-Za-z0-9_]*)\s*[;{]', line)
            if module_match:
                imports.append(
                    ImportEdge(
                        importer=module,
                        imported=f'{module}::{module_match.group(1)}',
                        language='rust',
                    )
                )

            struct_match = RUST_STRUCT_RE.match(line)
            if struct_match:
                symbols.append(
                    CodeSymbol(
                        qualified_name=f'{module}::{struct_match.group(1)}',
                        module=module,
                        file_path=str(path),
                        line=lineno,
                        kind='struct',
                        language='rust',
                    )
                )

            enum_match = RUST_ENUM_RE.match(line)
            if enum_match:
                symbols.append(
                    CodeSymbol(
                        qualified_name=f'{module}::{enum_match.group(1)}',
                        module=module,
                        file_path=str(path),
                        line=lineno,
                        kind='enum',
                        language='rust',
                    )
                )

            trait_match = RUST_TRAIT_RE.match(line)
            if trait_match:
                trait_name = trait_match.group(1)
                symbols.append(
                    CodeSymbol(
                        qualified_name=f'{module}::{trait_name}',
                        module=module,
                        file_path=str(path),
                        line=lineno,
                        kind='trait',
                        language='rust',
                    )
                )
                if '{' in line:
                    scopes.append(_RustScope(name=trait_name, depth=brace_depth + line.count('{')))
                else:
                    pending_trait_name = trait_name

            if line.startswith('impl ') or line.startswith('unsafe impl ') or line.startswith('default impl '):
                impl_target = _parse_rust_impl_target(line)
                if impl_target:
                    if '{' in line:
                        scopes.append(_RustScope(name=impl_target, depth=brace_depth + line.count('{')))
                    else:
                        pending_impl_target = impl_target

            if pending_impl_target and '{' in line and not (
                line.startswith('impl ') or line.startswith('unsafe impl ') or line.startswith('default impl ')
            ):
                scopes.append(_RustScope(name=pending_impl_target, depth=brace_depth + line.count('{')))
                pending_impl_target = None

            if pending_trait_name and '{' in line and not trait_match:
                scopes.append(_RustScope(name=pending_trait_name, depth=brace_depth + line.count('{')))
                pending_trait_name = None

            function_match = RUST_FUNCTION_RE.match(line)
            if function_match:
                owner = scopes[-1].name if scopes else None
                symbol_name = function_match.group(1)
                qualified_name = f'{module}::{owner}::{symbol_name}' if owner else f'{module}::{symbol_name}'
                symbols.append(
                    CodeSymbol(
                        qualified_name=qualified_name,
                        module=module,
                        file_path=str(path),
                        line=lineno,
                        kind='method' if owner else 'function',
                        language='rust',
                    )
                )

            brace_depth += line.count('{') - line.count('}')
            while scopes and brace_depth < scopes[-1].depth:
                scopes.pop()

        if use_buffer:
            statement = ' '.join(use_buffer)
            import_spec = re.sub(r'^(?:pub\s+)?use\s+', '', statement).rstrip(';')
            for target in _expand_rust_use_target(import_spec):
                imports.append(
                    ImportEdge(
                        importer=module,
                        imported=_normalize_rust_use_target(module, target, module_names, workspace_crates),
                        language='rust',
                    )
                )

    return symbols, imports


def _rust_alias_map(module: str, import_edges: list[ImportEdge]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for edge in import_edges:
        if edge.importer != module or edge.language != 'rust':
            continue
        aliases[edge.imported.split('::')[-1]] = edge.imported
    return aliases


def _normalize_rust_symbol_reference(
    current_module: str,
    value: str,
    alias_map: dict[str, str],
    current_owner_qname: str | None,
    module_names: set[str],
    workspace_crates: set[str],
    symbol_map: dict[str, CodeSymbol],
) -> str | None:
    cleaned = _strip_rust_generics(value).strip().lstrip('&').removeprefix('mut ').strip()
    if not cleaned:
        return None
    if cleaned.startswith('Self::') and current_owner_qname:
        candidate = f'{current_owner_qname}::{cleaned[6:]}'
        return candidate if candidate in symbol_map else None
    if cleaned in alias_map and alias_map[cleaned] in symbol_map:
        return alias_map[cleaned]
    if '::' in cleaned:
        first_segment, remainder = cleaned.split('::', 1)
        if first_segment in alias_map:
            candidate = f'{alias_map[first_segment]}::{remainder}'
            return candidate if candidate in symbol_map else None
        candidate = _normalize_rust_use_target(current_module, cleaned, module_names, workspace_crates)
        if candidate in symbol_map:
            return candidate
    same_module = f'{current_module}::{cleaned}'
    if same_module in symbol_map:
        return same_module
    if current_owner_qname:
        same_owner = f'{current_owner_qname}::{cleaned}'
        if same_owner in symbol_map:
            return same_owner
    return None


def _infer_rust_local_types(
    current_module: str,
    line: str,
    alias_map: dict[str, str],
    current_owner_qname: str | None,
    module_names: set[str],
    workspace_crates: set[str],
    symbol_map: dict[str, CodeSymbol],
    local_types: dict[str, str],
) -> None:
    typed_match = RUST_TYPED_LET_RE.search(line)
    if typed_match:
        resolved = _normalize_rust_symbol_reference(
            current_module,
            typed_match.group(2).strip(),
            alias_map,
            current_owner_qname,
            module_names,
            workspace_crates,
            symbol_map,
        )
        if resolved:
            local_types[typed_match.group(1)] = resolved

    path_match = RUST_PATH_LET_RE.search(line)
    if not path_match:
        return
    variable_name, type_candidate = path_match.groups()
    normalized_candidate = _strip_rust_generics(type_candidate)
    if normalized_candidate.endswith('::new') or normalized_candidate.endswith('::default'):
        normalized_candidate = normalized_candidate.rsplit('::', 1)[0]
    resolved = _normalize_rust_symbol_reference(
        current_module,
        normalized_candidate,
        alias_map,
        current_owner_qname,
        module_names,
        workspace_crates,
        symbol_map,
    )
    if resolved:
        local_types[variable_name] = resolved


def _resolve_rust_call_target(
    current_module: str,
    raw_expr: str,
    alias_map: dict[str, str],
    current_owner_qname: str | None,
    local_types: dict[str, str],
    module_names: set[str],
    workspace_crates: set[str],
    symbol_map: dict[str, CodeSymbol],
) -> str | None:
    expr = _strip_rust_generics(raw_expr).strip()
    if not expr:
        return None
    if expr in RUST_CONTROL_CALL_HEADS:
        return None
    if expr.startswith('self.') and current_owner_qname:
        candidate = f'{current_owner_qname}::{expr.split(".", 1)[1]}'
        return candidate if candidate in symbol_map else None
    if '.' in expr:
        base, method = expr.rsplit('.', 1)
        if base in local_types:
            candidate = f'{local_types[base]}::{method}'
            return candidate if candidate in symbol_map else None
        if base == 'self' and current_owner_qname:
            candidate = f'{current_owner_qname}::{method}'
            return candidate if candidate in symbol_map else None
        return None
    return _normalize_rust_symbol_reference(
        current_module,
        expr,
        alias_map,
        current_owner_qname,
        module_names,
        workspace_crates,
        symbol_map,
    )


def _extract_rust_call_edges(
    caller: str,
    current_module: str,
    line: str,
    lineno: int,
    alias_map: dict[str, str],
    current_owner_qname: str | None,
    local_types: dict[str, str],
    module_names: set[str],
    workspace_crates: set[str],
    symbol_map: dict[str, CodeSymbol],
) -> list[CallEdge]:
    call_edges: list[CallEdge] = []
    for match in RUST_CALL_RE.finditer(_strip_rust_generics(line)):
        callee = _resolve_rust_call_target(
            current_module=current_module,
            raw_expr=match.group(1),
            alias_map=alias_map,
            current_owner_qname=current_owner_qname,
            local_types=local_types,
            module_names=module_names,
            workspace_crates=workspace_crates,
            symbol_map=symbol_map,
        )
        if callee:
            call_edges.append(CallEdge(caller=caller, callee=callee, line=lineno, language='rust'))
    return call_edges


def _build_rust_call_edges(
    rust_symbols: list[CodeSymbol],
    rust_imports: list[ImportEdge],
) -> list[CallEdge]:
    rust_files = _rust_source_files()
    if not rust_files:
        return []

    workspace_crates = {path.name for path in RUST_CRATES_ROOT.iterdir() if path.is_dir()}
    module_names = {_rust_module_name_for_path(path) for path in rust_files}
    symbol_map = {
        symbol.qualified_name: symbol
        for symbol in rust_symbols
        if symbol.language == 'rust'
    }
    call_edges: list[CallEdge] = []

    for path in rust_files:
        module = _rust_module_name_for_path(path)
        alias_map = _rust_alias_map(module, rust_imports)
        lines = path.read_text().splitlines()
        brace_depth = 0
        scopes: list[_RustScope] = []
        function_scopes: list[_RustFunctionScope] = []
        pending_impl_target: str | None = None
        pending_trait_name: str | None = None
        pending_function: tuple[str, str | None] | None = None

        for lineno, raw_line in enumerate(lines, start=1):
            line = _strip_rust_line_comment(raw_line).strip()
            if not line:
                continue

            trait_match = RUST_TRAIT_RE.match(line)
            if trait_match:
                trait_name = trait_match.group(1)
                if '{' in line:
                    scopes.append(_RustScope(name=trait_name, depth=brace_depth + line.count('{')))
                else:
                    pending_trait_name = trait_name

            if line.startswith('impl ') or line.startswith('unsafe impl ') or line.startswith('default impl '):
                impl_target = _parse_rust_impl_target(line)
                if impl_target:
                    if '{' in line:
                        scopes.append(_RustScope(name=impl_target, depth=brace_depth + line.count('{')))
                    else:
                        pending_impl_target = impl_target

            if pending_impl_target and '{' in line and not (
                line.startswith('impl ') or line.startswith('unsafe impl ') or line.startswith('default impl ')
            ):
                scopes.append(_RustScope(name=pending_impl_target, depth=brace_depth + line.count('{')))
                pending_impl_target = None

            if pending_trait_name and '{' in line and not trait_match:
                scopes.append(_RustScope(name=pending_trait_name, depth=brace_depth + line.count('{')))
                pending_trait_name = None

            function_match = RUST_FUNCTION_RE.match(line)
            body_fragment = line
            if function_match:
                owner_qname = f'{module}::{scopes[-1].name}' if scopes else None
                symbol_name = function_match.group(1)
                function_qname = f'{owner_qname}::{symbol_name}' if owner_qname else f'{module}::{symbol_name}'
                if '{' in line:
                    function_scopes.append(
                        _RustFunctionScope(
                            qualified_name=function_qname,
                            depth=brace_depth + line.count('{'),
                            owner_qname=owner_qname,
                            local_types={},
                        )
                    )
                    body_fragment = line.split('{', 1)[1]
                else:
                    pending_function = (function_qname, owner_qname)
                    body_fragment = ''
            elif pending_function and '{' in line:
                function_qname, owner_qname = pending_function
                function_scopes.append(
                    _RustFunctionScope(
                        qualified_name=function_qname,
                        depth=brace_depth + line.count('{'),
                        owner_qname=owner_qname,
                        local_types={},
                    )
                )
                pending_function = None
                body_fragment = line.split('{', 1)[1]

            current_function = function_scopes[-1] if function_scopes else None
            if current_function and body_fragment:
                _infer_rust_local_types(
                    current_module=module,
                    line=body_fragment,
                    alias_map=alias_map,
                    current_owner_qname=current_function.owner_qname,
                    module_names=module_names,
                    workspace_crates=workspace_crates,
                    symbol_map=symbol_map,
                    local_types=current_function.local_types,
                )
                call_edges.extend(
                    _extract_rust_call_edges(
                        caller=current_function.qualified_name,
                        current_module=module,
                        line=body_fragment,
                        lineno=lineno,
                        alias_map=alias_map,
                        current_owner_qname=current_function.owner_qname,
                        local_types=current_function.local_types,
                        module_names=module_names,
                        workspace_crates=workspace_crates,
                        symbol_map=symbol_map,
                    )
                )

            brace_depth += line.count('{') - line.count('}')
            while function_scopes and brace_depth < function_scopes[-1].depth:
                function_scopes.pop()
            while scopes and brace_depth < scopes[-1].depth:
                scopes.pop()

    return call_edges


@lru_cache(maxsize=1)
def build_code_index() -> CodeIndex:
    python_symbols, python_imports, python_calls, python_yield_events = _build_python_index()
    rust_symbols, rust_imports = _build_rust_index()
    rust_calls = _build_rust_call_edges(rust_symbols, rust_imports)

    all_symbols = python_symbols + rust_symbols
    deduped_symbols = sorted(
        {
            (symbol.qualified_name, symbol.line, symbol.file_path, symbol.kind, symbol.language): symbol
            for symbol in all_symbols
        }.values(),
        key=lambda symbol: (symbol.qualified_name, symbol.line, symbol.file_path),
    )
    deduped_imports = sorted(
        {
            (edge.importer, edge.imported, edge.language): edge
            for edge in (python_imports + rust_imports)
        }.values(),
        key=lambda edge: (edge.language, edge.importer, edge.imported),
    )
    deduped_calls = sorted(
        {
            (edge.caller, edge.callee, edge.line, edge.language): edge
            for edge in (python_calls + rust_calls)
        }.values(),
        key=lambda edge: (edge.language, edge.caller, edge.callee, edge.line),
    )
    deduped_yield_events = sorted(
        {
            (event.producer, event.event_type, event.keys, event.line, event.language): event
            for event in python_yield_events
        }.values(),
        key=lambda event: (event.language, event.producer, event.line, event.event_type or ''),
    )
    return CodeIndex(
        symbols=tuple(deduped_symbols),
        call_edges=tuple(deduped_calls),
        import_edges=tuple(deduped_imports),
        yield_events=tuple(deduped_yield_events),
    )
