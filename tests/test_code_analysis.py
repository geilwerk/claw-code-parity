from __future__ import annotations

import json
import subprocess
import sys
import unittest

from src.code_analysis import build_code_index


class CodeAnalysisTests(unittest.TestCase):
    def test_code_index_builds_nontrivial_graph(self) -> None:
        index = build_code_index()
        self.assertGreaterEqual(len(index.symbols), 40)
        self.assertGreaterEqual(len(index.import_edges), 20)
        self.assertGreaterEqual(len(index.call_edges), 20)

    def test_exact_symbol_and_trace_resolution(self) -> None:
        index = build_code_index()
        resolved = index.resolve_symbol('src.main.main')
        self.assertEqual(resolved, 'src.main.main')

        path = index.trace_path('src.main.main', 'src.query_engine.QueryEnginePort.render_summary')
        self.assertGreaterEqual(len(path.nodes), 2)
        self.assertEqual(path.nodes[0], 'src.main.main')
        self.assertEqual(path.nodes[-1], 'src.query_engine.QueryEnginePort.render_summary')

    def test_variable_inference_finds_runtime_submit_message_calls(self) -> None:
        index = build_code_index()
        callers = index.callers_of('src.query_engine.QueryEnginePort.submit_message')
        caller_names = {edge.caller for edge in callers}
        self.assertIn('src.runtime.PortRuntime.bootstrap_session', caller_names)
        self.assertIn('src.runtime.PortRuntime.run_turn_loop', caller_names)

    def test_python_field_type_resolution_finds_query_engine_state_calls(self) -> None:
        index = build_code_index()

        submit_callees = {edge.callee for edge in index.callees_of('src.query_engine.QueryEnginePort.submit_message')}
        self.assertIn('src.models.UsageSummary.add_turn', submit_callees)
        self.assertIn('src.transcript.TranscriptStore.append', submit_callees)

        compact_callees = {edge.callee for edge in index.callees_of('src.query_engine.QueryEnginePort.compact_messages_if_needed')}
        self.assertIn('src.transcript.TranscriptStore.compact', compact_callees)

        path = index.trace_path(
            'src.query_engine.QueryEnginePort.stream_submit_message',
            'src.transcript.TranscriptStore.compact',
        )
        self.assertEqual(path.nodes[0], 'src.query_engine.QueryEnginePort.stream_submit_message')
        self.assertEqual(path.nodes[-1], 'src.transcript.TranscriptStore.compact')
        self.assertIn('src.query_engine.QueryEnginePort.submit_message', path.nodes)
        self.assertIn('src.query_engine.QueryEnginePort.compact_messages_if_needed', path.nodes)

    def test_rust_symbols_and_imports_are_indexed(self) -> None:
        index = build_code_index()
        resolved = index.resolve_symbol('rust::runtime::session::Session')
        self.assertEqual(resolved, 'rust::runtime::session::Session')

        session_symbols = index.find_symbols('rust::runtime::session::Session', limit=5)
        self.assertTrue(any(symbol.language == 'rust' for symbol in session_symbols))

        runtime_imports = {
            edge.imported
            for edge in index.import_edges
            if edge.importer == 'rust::runtime'
        }
        self.assertIn('rust::runtime::session', runtime_imports)

    def test_import_navigation_supports_rust_module_tracing(self) -> None:
        index = build_code_index()
        runtime_imports = index.imports_of('rust::runtime')
        runtime_import_targets = {edge.imported for edge in runtime_imports}
        self.assertIn('rust::runtime::session', runtime_import_targets)

        session_importers = index.importers_of('rust::runtime::session')
        session_importer_names = {edge.importer for edge in session_importers}
        self.assertIn('rust::runtime', session_importer_names)

        path = index.trace_import_path('rust::runtime', 'rust::runtime::session')
        self.assertEqual(path.nodes[0], 'rust::runtime')
        self.assertEqual(path.nodes[-1], 'rust::runtime::session')

    def test_rust_call_graph_traces_local_session_methods(self) -> None:
        index = build_code_index()
        callers = index.callers_of('rust::runtime::session::Session::append_persisted_message')
        caller_names = {edge.caller for edge in callers}
        self.assertIn('rust::runtime::session::Session::push_message', caller_names)

        path = index.trace_path(
            'rust::runtime::session::Session::push_user_text',
            'rust::runtime::session::Session::append_persisted_message',
        )
        self.assertEqual(path.nodes[0], 'rust::runtime::session::Session::push_user_text')
        self.assertEqual(path.nodes[-1], 'rust::runtime::session::Session::append_persisted_message')
        self.assertIn('rust::runtime::session::Session::push_message', path.nodes)

    def test_graph_focus_limits_import_and_call_neighborhoods(self) -> None:
        index = build_code_index()

        import_graph = index.graph_payload(
            'imports',
            scope='rust',
            focus='rust::runtime',
            max_depth=1,
            direction='out',
        )
        import_nodes = {node['id'] for node in import_graph['nodes']}
        self.assertIn('rust::runtime', import_nodes)
        self.assertIn('rust::runtime::session', import_nodes)
        self.assertNotIn('rust::api', import_nodes)

        call_graph = index.graph_payload(
            'calls',
            scope='rust',
            focus='rust::runtime::session::Session::push_user_text',
            max_depth=2,
            direction='out',
        )
        call_nodes = {node['id'] for node in call_graph['nodes']}
        self.assertIn('rust::runtime::session::Session::push_user_text', call_nodes)
        self.assertIn('rust::runtime::session::Session::push_message', call_nodes)
        self.assertIn('rust::runtime::session::Session::append_persisted_message', call_nodes)

    def test_code_analysis_json_outputs(self) -> None:
        index_result = subprocess.run(
            [sys.executable, '-m', 'src.main', 'code-index', '--json'],
            check=True,
            capture_output=True,
            text=True,
        )
        symbols_result = subprocess.run(
            [sys.executable, '-m', 'src.main', 'code-symbols', 'rust::runtime::session::Session', '--limit', '10', '--json'],
            check=True,
            capture_output=True,
            text=True,
        )
        callers_result = subprocess.run(
            [sys.executable, '-m', 'src.main', 'code-callers', 'QueryEnginePort.submit_message', '--json'],
            check=True,
            capture_output=True,
            text=True,
        )
        import_trace_result = subprocess.run(
            [sys.executable, '-m', 'src.main', 'code-import-trace', 'rust::runtime', 'rust::runtime::session', '--json'],
            check=True,
            capture_output=True,
            text=True,
        )
        graph_result = subprocess.run(
            [sys.executable, '-m', 'src.main', 'code-graph', 'imports', '--scope', 'rust', '--json'],
            check=True,
            capture_output=True,
            text=True,
        )
        rust_callers_result = subprocess.run(
            [sys.executable, '-m', 'src.main', 'code-callers', 'rust::runtime::session::Session::append_persisted_message', '--json'],
            check=True,
            capture_output=True,
            text=True,
        )
        python_state_callers_result = subprocess.run(
            [sys.executable, '-m', 'src.main', 'code-callers', 'src.transcript.TranscriptStore.compact', '--json'],
            check=True,
            capture_output=True,
            text=True,
        )
        rust_trace_result = subprocess.run(
            [
                sys.executable,
                '-m',
                'src.main',
                'code-trace',
                'rust::runtime::session::Session::push_user_text',
                'rust::runtime::session::Session::append_persisted_message',
                '--json',
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        python_state_trace_result = subprocess.run(
            [
                sys.executable,
                '-m',
                'src.main',
                'code-trace',
                'src.query_engine.QueryEnginePort.stream_submit_message',
                'src.transcript.TranscriptStore.compact',
                '--json',
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        focused_graph_result = subprocess.run(
            [
                sys.executable,
                '-m',
                'src.main',
                'code-graph',
                'calls',
                '--scope',
                'rust',
                '--focus',
                'rust::runtime::session::Session::push_user_text',
                '--depth',
                '2',
                '--direction',
                'out',
                '--json',
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        index_payload = json.loads(index_result.stdout)
        symbols_payload = json.loads(symbols_result.stdout)
        callers_payload = json.loads(callers_result.stdout)
        import_trace_payload = json.loads(import_trace_result.stdout)
        graph_payload = json.loads(graph_result.stdout)
        rust_callers_payload = json.loads(rust_callers_result.stdout)
        python_state_callers_payload = json.loads(python_state_callers_result.stdout)
        rust_trace_payload = json.loads(rust_trace_result.stdout)
        python_state_trace_payload = json.loads(python_state_trace_result.stdout)
        focused_graph_payload = json.loads(focused_graph_result.stdout)

        self.assertIn('python', index_payload['summary']['languages'])
        self.assertIn('rust', index_payload['summary']['languages'])
        self.assertGreater(index_payload['summary']['languages']['rust']['symbols'], 0)
        self.assertGreater(index_payload['summary']['languages']['rust']['call_edges'], 0)
        self.assertTrue(
            any(result['qualified_name'] == 'rust::runtime::session::Session' for result in symbols_payload['results'])
        )
        self.assertEqual(callers_payload['symbol'], 'src.query_engine.QueryEnginePort.submit_message')
        self.assertTrue(callers_payload['callers'])
        self.assertEqual(import_trace_payload['path']['nodes'][0], 'rust::runtime')
        self.assertEqual(import_trace_payload['path']['nodes'][-1], 'rust::runtime::session')
        self.assertEqual(graph_payload['kind'], 'imports')
        self.assertTrue(any(edge['imported'] == 'rust::runtime::session' for edge in graph_payload['edges']))
        self.assertTrue(
            any(edge['caller'] == 'rust::runtime::session::Session::push_message' for edge in rust_callers_payload['callers'])
        )
        self.assertTrue(
            any(edge['caller'] == 'src.query_engine.QueryEnginePort.compact_messages_if_needed' for edge in python_state_callers_payload['callers'])
        )
        self.assertEqual(
            rust_trace_payload['path']['nodes'][0],
            'rust::runtime::session::Session::push_user_text',
        )
        self.assertEqual(
            rust_trace_payload['path']['nodes'][-1],
            'rust::runtime::session::Session::append_persisted_message',
        )
        self.assertEqual(
            python_state_trace_payload['path']['nodes'][0],
            'src.query_engine.QueryEnginePort.stream_submit_message',
        )
        self.assertEqual(
            python_state_trace_payload['path']['nodes'][-1],
            'src.transcript.TranscriptStore.compact',
        )
        self.assertEqual(
            focused_graph_payload['focus'],
            'rust::runtime::session::Session::push_user_text',
        )
        self.assertEqual(focused_graph_payload['max_depth'], 2)
        self.assertTrue(
            any(node['id'] == 'rust::runtime::session::Session::append_persisted_message' for node in focused_graph_payload['nodes'])
        )

    def test_code_analysis_clis_run(self) -> None:
        index_result = subprocess.run(
            [sys.executable, '-m', 'src.main', 'code-index'],
            check=True,
            capture_output=True,
            text=True,
        )
        symbols_result = subprocess.run(
            [sys.executable, '-m', 'src.main', 'code-symbols', 'submit_message', '--limit', '5'],
            check=True,
            capture_output=True,
            text=True,
        )
        callers_result = subprocess.run(
            [sys.executable, '-m', 'src.main', 'code-callers', 'QueryEnginePort.submit_message'],
            check=True,
            capture_output=True,
            text=True,
        )
        trace_result = subprocess.run(
            [sys.executable, '-m', 'src.main', 'code-trace', 'src.main.main', 'src.query_engine.QueryEnginePort.render_summary'],
            check=True,
            capture_output=True,
            text=True,
        )
        import_trace_result = subprocess.run(
            [sys.executable, '-m', 'src.main', 'code-import-trace', 'rust::runtime', 'rust::runtime::session'],
            check=True,
            capture_output=True,
            text=True,
        )
        graph_result = subprocess.run(
            [sys.executable, '-m', 'src.main', 'code-graph', 'imports', '--scope', 'rust'],
            check=True,
            capture_output=True,
            text=True,
        )
        rust_trace_result = subprocess.run(
            [
                sys.executable,
                '-m',
                'src.main',
                'code-trace',
                'rust::runtime::session::Session::push_user_text',
                'rust::runtime::session::Session::append_persisted_message',
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        python_state_trace_result = subprocess.run(
            [
                sys.executable,
                '-m',
                'src.main',
                'code-trace',
                'src.query_engine.QueryEnginePort.stream_submit_message',
                'src.transcript.TranscriptStore.compact',
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        focused_graph_result = subprocess.run(
            [
                sys.executable,
                '-m',
                'src.main',
                'code-graph',
                'calls',
                '--scope',
                'rust',
                '--focus',
                'rust::runtime::session::Session::push_user_text',
                '--depth',
                '2',
                '--direction',
                'out',
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn('Code Analysis Index', index_result.stdout)
        self.assertIn('- rust:', index_result.stdout)
        self.assertIn('src.query_engine.QueryEnginePort.submit_message', symbols_result.stdout)
        self.assertIn('Callers of src.query_engine.QueryEnginePort.submit_message', callers_result.stdout)
        self.assertIn('src.main.main', trace_result.stdout)
        self.assertIn('src.query_engine.QueryEnginePort.render_summary', trace_result.stdout)
        self.assertIn('rust::runtime', import_trace_result.stdout)
        self.assertIn('rust::runtime::session', import_trace_result.stdout)
        self.assertIn('digraph imports {', graph_result.stdout)
        self.assertIn('"rust::runtime" -> "rust::runtime::session"', graph_result.stdout)
        self.assertIn('rust::runtime::session::Session::push_user_text', rust_trace_result.stdout)
        self.assertIn('rust::runtime::session::Session::append_persisted_message', rust_trace_result.stdout)
        self.assertIn('src.query_engine.QueryEnginePort.stream_submit_message', python_state_trace_result.stdout)
        self.assertIn('src.transcript.TranscriptStore.compact', python_state_trace_result.stdout)
        self.assertIn('"rust::runtime::session::Session::push_user_text" -> "rust::runtime::session::Session::push_message"', focused_graph_result.stdout)
