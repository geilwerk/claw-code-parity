# Code Analysis Helper

This is a working note for the mixed Python/Rust code-analysis helper in `src/code_analysis.py`.
It is intentionally optimized for handoff and pressure-testing, not polished end-user docs yet.

## Status

- Branch: `feat/code-analysis-import-trace`
- Latest helper commit at time of writing: `ab969b2`
- Current scope:
  - Python AST-backed symbol index
  - Python call graph
  - Rust symbol index
  - Rust import graph
  - Heuristic Rust local call graph
  - Graph export with focus/depth/direction filters

## Commands

- `python3 -m src.main code-index`
- `python3 -m src.main code-index --json`
- `python3 -m src.main code-symbols <query>`
- `python3 -m src.main code-callers <symbol>`
- `python3 -m src.main code-callees <symbol>`
- `python3 -m src.main code-trace <start> <target>`
- `python3 -m src.main code-imports <module-or-symbol>`
- `python3 -m src.main code-importers <module-or-symbol>`
- `python3 -m src.main code-import-trace <start> <target>`
- `python3 -m src.main code-graph imports|calls [--scope all|python|rust]`
- `python3 -m src.main code-graph ... --focus <symbol-or-module> --depth <n> --direction both|in|out`

## Pressure-Test Findings

### Confirmed useful

- `src.runtime.PortRuntime.bootstrap_session`
  - Good composition-layer target.
  - The helper correctly shows it as an orchestration entrypoint.
- `src.query_engine.QueryEnginePort.stream_submit_message`
  - The helper correctly shows it as a thin streaming wrapper over `submit_message`.
- `src.query_engine.QueryEnginePort.submit_message`
  - Initially under-modeled.
  - Now correctly resolves typed field-backed calls such as:
    - `src.models.UsageSummary.add_turn`
    - `src.transcript.TranscriptStore.append`
    - `src.transcript.TranscriptStore.compact` via `compact_messages_if_needed`
- `src.query_engine.QueryEnginePort.persist_session`
  - Already modeled well by the current helper.
  - Good example of a wrapper that still produces a useful semantic path:
    - `src.runtime.PortRuntime.bootstrap_session`
    - `src.query_engine.QueryEnginePort.persist_session`
    - `src.session_store.save_session`
- `src.runtime.PortRuntime.route_prompt`
  - Modeled well enough for structural routing flow.
  - The helper correctly shows the two `_collect_matches(...)` calls.
  - Remaining weakness is in data-shaping semantics, not core call structure.
- `src.query_engine.QueryEnginePort.stream_submit_message`
  - Structural trace is correct: it yields some events, then delegates to `submit_message`.
  - Current helper does not expose the yielded event contract as analysis output.
  - Observed event types from a real run:
    - `message_start`
    - `command_match`
    - `tool_match`
    - `message_delta`
    - `message_stop`

### Current examples

- `python3 -m src.main code-trace src.query_engine.QueryEnginePort.stream_submit_message src.transcript.TranscriptStore.compact`
  - Expected path:
    1. `src.query_engine.QueryEnginePort.stream_submit_message`
    2. `src.query_engine.QueryEnginePort.submit_message`
    3. `src.query_engine.QueryEnginePort.compact_messages_if_needed`
    4. `src.transcript.TranscriptStore.compact`

- `python3 -m src.main code-trace rust::runtime::session::Session::push_user_text rust::runtime::session::Session::append_persisted_message`
  - Expected path:
    1. `rust::runtime::session::Session::push_user_text`
    2. `rust::runtime::session::Session::push_message`
    3. `rust::runtime::session::Session::append_persisted_message`

- `python3 -m src.main code-graph calls --scope rust --focus rust::runtime::session::Session::push_user_text --depth 2 --direction out`
  - Produces a small, readable DOT subgraph instead of dumping the whole call graph.

- `python3 -m src.main code-callees src.runtime.PortRuntime.route_prompt`
  - Expected structure:
    - `src.runtime.PortRuntime._collect_matches`
    - `src.runtime.PortRuntime._collect_matches`

## Known Gaps

- Python builtins and container mutations are still mostly opaque.
  - Example: `self.mutable_messages.append(...)` and `self.permission_denials.extend(...)` are not modeled as meaningful semantic edges.
- Python data-shaping via comprehensions and builtins is only shallowly represented.
  - Examples: `sorted(...)`, `max(...)`, `list.pop(...)`, `list.append(...)`, and comprehension-heavy selection logic in `route_prompt()`.
- Generator/event-schema understanding is missing.
  - The helper can trace that `stream_submit_message` calls `submit_message`, but it does not model yielded event shapes as first-class analysis output.
- Python control-flow and branch semantics are shallow.
  - We see callable edges, not branch conditions, dominance, or early-return structure.
- Rust call graph is intentionally heuristic.
  - Best at local/file-scoped method flow.
  - Not yet trustworthy for rich trait dispatch, macros, or broad cross-module resolution.
- Graph export is useful for neighborhood views, but not yet ideal for very large focused subgraphs.
  - No ranking, edge collapsing, or semantic grouping yet.

## Candidate Next Pressure Tests

- `src.query_engine.QueryEnginePort.persist_session`
- `src.runtime.PortRuntime.route_prompt`
- `src.query_engine.QueryEnginePort._render_structured_output`
- Rust runtime flows that cross module boundaries instead of staying within one impl block
- Event-shape extraction for streaming generators

## Likely Next Improvements

- Add Python field-backed builtin/container mutation modeling where it is semantically useful.
- Add a lightweight event-schema mode for generator methods that yield dict literals.
- Improve Rust cross-module call resolution using import aliases plus simple type propagation across more assignment patterns.
- Add filtered graph export to files for Graphviz workflows if needed later.
