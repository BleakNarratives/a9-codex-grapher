# A9 Codex Grapher & High-Scale Indexer

Lightweight, zero-hash metadata indexing and topological graph extraction suite built for high-density, multi-device Android/Termux environments. 

## Features
- **Fast Indexer (`fast_comp.py`)**: Non-blocking inode walker that streams nodes directly into NDJSON (`a9_codex_fast.ndjson`) without RAM bloat or IO lockups.
- **Graph Exporter (`export_ndjson_graph.py`)**: Converts NDJSON node streams into Neo4j-compatible `codex_nodes.csv` and `codex_edges.csv` mapping `CONTAINS` relationships.
- **Graph Query CLI (`query_codex_graph.py`)**: Fast search utility to query nodes by path or extension across multi-hundred-thousand node CSVs.
- **SQLite Full-Text Search (`add_fts5_indexes.py`)**: Builds an FTS5 index over SQLite node names for instant filename searches; the index is maintained by database triggers.
- **Graph Search Adapter (`core_framework/adapters/graph_search.py`)**: Read-only FTS5 CLI for searching node names and paths with MATCH expressions.
- **Manifest Diff (`diff_manifests.py`)**: Cross-model verification tool to compare outputs between fast NDJSON passes and full model manifests.

Run `python add_fts5_indexes.py` to index `codex_graph.db`, or pass a different database path. Search indexed names with SQLite's FTS5 `MATCH` operator against `nodes_fts` (for example, `SELECT n.id FROM nodes_fts f JOIN nodes n ON n.rowid = f.rowid WHERE nodes_fts MATCH 'README'`).

Use `python core_framework/adapters/graph_search.py search 'GRAVEYARD NOT Kindred' --limit 10` to query the indexed graph. The adapter opens the database read-only and supports both the legacy name-only index and the current name/path/type index.

## Artifacts Generated
- `~/a9_codex_fast.ndjson` (313k+ nodes)
- `~/codex_nodes.csv`
- `~/codex_edges.csv`
