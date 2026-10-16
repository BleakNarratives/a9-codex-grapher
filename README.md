# A9 Codex Grapher

Scan files into a portable manifest, build a searchable SQLite graph, and query
file names or paths with FTS5. The checked-in provenance CSVs are a reproducible
snapshot; a fresh clone does not need the ignored `codex_graph.db` artifact.

## Rebuild and search

From the repository root, using Python 3 with SQLite FTS5 enabled:

```bash
python csv_to_sqlite.py
python query_codex_graph.py index --device-id chromebook-crostini
python query_codex_graph.py clusters --device-id chromebook-crostini --limit 10
python core_framework/adapters/graph_search.py search 'GRAVEYARD NOT Kindred' --limit 10
```

The builder reads `provenance/graphs/codex_nodes.csv` and
`provenance/graphs/codex_edges.csv`, creates `codex_graph.db` with the FTS5
index, and refuses to overwrite an existing database. `query_codex_graph.py
index` materializes root and top-level cluster summaries; `clusters` lists the
largest ones. `prefix` queries a path and its descendants using the nodes
primary-key index:

```bash
python query_codex_graph.py prefix /storage/emulated/0/RootBase --limit 20
```

The default roots are the known Termux roots. For other devices, provide roots
explicitly as repeatable `--root ALIAS=/absolute/path` options and a stable
`--device-id`. To choose another database path:

```bash
python csv_to_sqlite.py --db /path/to/codex_graph.db
python query_codex_graph.py index --db /path/to/codex_graph.db \
  --device-id moto-4-5g-2024 \
  --root projects=/path/to/projects
python core_framework/adapters/graph_search.py search '"root_2026"' \
  --db /path/to/codex_graph.db --limit 5
```

To intentionally replace a database, pass `--replace`; the new database is
built first and only published after import and indexing succeed.

## Generate a new device snapshot

On a device that has the configured source roots, run:

```bash
python fast_comp.py
python export_ndjson_graph.py
```

Then transfer `~/codex_nodes.csv` and `~/codex_edges.csv` to the coordinating
machine and import them:

```bash
python csv_to_sqlite.py \
  --nodes /path/to/codex_nodes.csv \
  --edges /path/to/codex_edges.csv \
  --db /path/to/device_graph.db
```

The current importer builds one graph database from one CSV snapshot; automated
multi-device merging, device identity, and snapshot history are not implemented
yet. See [ROADMAP.md](ROADMAP.md) for the handoff design and remaining work.

## Tests

```bash
python -m pytest -q
```

The pipeline uses only the Python standard library at runtime. Pytest is needed
only to run the test suite. SQLite must be compiled with FTS5 support.
