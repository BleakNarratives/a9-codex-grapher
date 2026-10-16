# A9 Codex Grapher & High-Scale Indexer

Lightweight, zero-hash metadata indexing and topological graph extraction suite built for high-density, multi-device Android/Termux environments. 

## Features
- **Fast Indexer (`fast_comp.py`)**: Non-blocking inode walker that streams nodes directly into NDJSON (`a9_codex_fast.ndjson`) without RAM bloat or IO lockups.
- **Graph Exporter (`export_ndjson_graph.py`)**: Converts NDJSON node streams into Neo4j-compatible `codex_nodes.csv` and `codex_edges.csv` mapping `CONTAINS` relationships.
- **Graph Query CLI (`query_codex_graph.py`)**: Fast search utility to query nodes by path or extension across multi-hundred-thousand node CSVs.
- **Manifest Diff (`diff_manifests.py`)**: Cross-model verification tool to compare outputs between fast NDJSON passes and full model manifests.

## Artifacts Generated
- `~/a9_codex_fast.ndjson` (313k+ nodes)
- `~/codex_nodes.csv`
- `~/codex_edges.csv`
