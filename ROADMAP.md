# A9 Codex Grapher — Architecture Roadmap

Status: living document. Grounded in the repository as checked out; every claim about the
current schema was read from `codex_graph.db` itself (`sqlite_master`), not assumed.

## 1. Current state (verified)

### Pipeline scripts (repo root, byte-identical copies live in `provenance/scripts/`)

| Script | Role | Output |
|---|---|---|
| `fast_comp.py` (= `02_fast_inode_ndjson.py`) | Streaming inode walker; `os.stat` per node, no hashing, prunes `SKIP_DIRS` | `~/a9_codex_fast.ndjson` (313,432 nodes) |
| `export_ndjson_graph.py` (= `03_ndjson_to_neo4j_csv.py`) | NDJSON -> Neo4j-style CSVs with `CONTAINS` edges | `~/codex_nodes.csv`, `~/codex_edges.csv` |
| `query_codex_graph.py` (= `04_graph_query_cli.py`) | Naive linear scan of nodes CSV by term/extension | stdout |
| `diff_manifests.py` (= `05_cross_model_diff.py`) | Cross-model manifest comparison (counts, type breakdown) | stdout |
| `generate_compendium.py` (= `01_gemini_cli_full_sha256.py`) | Full SHA256 pass, one `sha256sum` fork per file; hardcoded Termux paths | `compendium_output.json` |
| `fts_indexer.py` (new) | Builds/rebuilds the FTS5 search index over `codex_graph.db` | `nodes_fts` in the DB |

Provenance bundles (`provenance/scripts|manifests|graphs/`) are frozen snapshots and are not
edited; fixes land in the repo-root copies.

### `codex_graph.db` — schema as it exists today

```sql
CREATE TABLE nodes (id TEXT PRIMARY KEY, name TEXT, type TEXT,
                    size_bytes INTEGER, mtime TEXT, label TEXT);
CREATE TABLE edges (source_id TEXT, target_id TEXT, relationship TEXT);
CREATE INDEX idx_nodes_name ON nodes(name);
CREATE INDEX idx_nodes_type ON nodes(type);
```

Observed contents: 313,432 nodes (278,690 files / 34,742 directories), 313,428 edges — the
node count matches `a9_codex_fast.ndjson` line-for-line. Root prefixes present:
Termux home (232,617), RootBase (73,349), r26 (7,453), ChronoScouts (13).

### Known gaps

1. **No builder script.** Nothing in the repo creates `codex_graph.db` from the CSVs; the
   DB is a git-ignored hand-built artifact. A `csv_to_sqlite.py` loader closes this gap and
   makes the DB reproducible.
2. **No path index.** Cluster/prefix queries rely on `LIKE '/root/%'` scans over 313k rows.
3. **No full-text search.** `query_codex_graph.py` is an O(n) CSV scan.
4. **No device provenance.** `id` paths are absolute Android paths; there is no record of
   which device produced a snapshot or when.
5. **Directories carry `size_bytes = 0`**, so "how big is this subtree" cannot be answered
   without summing children at query time.

## 2. Target schema (v2)

Additive only — v1 tables stay readable by every existing script.

```sql
-- device/root registry: one row per scanned root, per device, per snapshot
CREATE TABLE roots (
    device_id   TEXT NOT NULL,   -- stable UUID of the producing device
    snapshot_at TEXT NOT NULL,   -- ISO-8601 UTC
    root_alias  TEXT NOT NULL,   -- 'termux-home' | 'RootBase' | 'r26' | 'ChronoScouts'
    root_path   TEXT NOT NULL,   -- absolute path as seen on the producing device
    PRIMARY KEY (device_id, snapshot_at, root_alias)
);

-- materialized top-level subtree aggregates (the "clusters" view), refreshed by rebuild
CREATE TABLE clusters (
    device_id   TEXT NOT NULL,
    snapshot_at TEXT NOT NULL,
    cluster_path TEXT NOT NULL,      -- <root>/<first path component>
    file_count  INTEGER NOT NULL,
    dir_count   INTEGER NOT NULL,
    total_bytes INTEGER NOT NULL,
    PRIMARY KEY (device_id, snapshot_at, cluster_path)
);

-- optional but cheap: makes prefix/cluster queries index-driven instead of scans
CREATE INDEX idx_nodes_id_prefix ON nodes(id);

-- FTS5 search index (built by fts_indexer.py)
CREATE VIRTUAL TABLE nodes_fts USING fts5(
    name, path, type UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);
```

Migration rules: v2 objects are created only by explicit tooling, never by ad-hoc edits;
dropping `nodes_fts`/`clusters`/`roots` always returns the DB to a valid v1 state.

## 3. FTS5 search design

- **Table:** `nodes_fts(name, path, type UNINDEXED)`. `path` mirrors `nodes.id` (the full
  absolute path); `type` is stored for display but never tokenized, so searching `File`
  does not match every file node.
- **Rowid alignment:** `nodes_fts.rowid` is kept identical to `nodes.rowid`, so search
  results join back with `nodes n JOIN nodes_fts f ON n.rowid = f.rowid` — no extra
  mapping table.
- **Tokenizer:** `unicode61 remove_diacritics 2`. Paths are punctuation-heavy
  (`snake_case`, `v1.38.2`, `index.crates.io`); unicode61 splits on punctuation, which is
  what we want (`root_2026` is findable as `root` or `2026`), and diacritic folding makes
  `cafe` match `café`.
- **Sync strategy:** rebuild-on-demand, not triggers. `nodes` is append-only in practice
  (snapshots replace whole trees), so `fts_indexer.py --rebuild` inside one transaction is
  both the correct and the cheap answer. Trigger-based incremental sync is explicitly out
  of scope until `nodes` becomes incrementally mutable.
- **Query patterns supported:** single term, prefix (`root*`), phrase (`"root_2026"`),
  boolean (`kindred AND archives`), column-filtered (`name:graveyard`). Verified by
  `tests/test_graph.py`.
- **Cost:** index text is the short `name` + `path` columns only (no file contents), so the
  index should stay in the tens of MB against the current ~200 MB DB.

## 4. Cross-device handoff logic

The fleet producing manifests: the Termux phone (`/data/data/com.termux/files/home`,
RootBase, ChronoScouts on shared storage), the r26 SD card (`/storage/ED7B-AD5A/r26`), and
this Linux box (`/home/bleaknarratives`). Handoff is snapshot-shaped:

1. **Snapshot (device A):** run `fast_comp.py` (+ optional SHA256 pass) -> NDJSON; record
   `roots(device_id, snapshot_at, ...)` rows.
2. **Transfer:** ship NDJSON (or the built DB) plus a manifest containing
   `node_count`, `edge_count`, and a **content digest** = SHA256 over the sorted
   `path\0size_bytes` lines. The digest catches truncation/reorder without re-hashing files.
3. **Verify (device B):** recompute digest and counts from the received artifact; mismatch
   aborts the import — the artifact is treated as untrusted input until it verifies
   (per repository security boundaries: provenance and source hashes must survive handoff).
4. **Remap:** paths are rewritten through an explicit root-mapping table, e.g.
   `/data/data/com.termux/files/home` -> `/home/bleaknarratives`. Remapping is declared,
   never guessed; unmapped paths stay quarantined under their original prefix so a wrong
   guess cannot silently corrupt the graph.
5. **Merge:** imported nodes are namespaced by `device_id`/`snapshot_at` rather than
   overwriting local rows; `clusters` and `nodes_fts` are rebuilt after merge.
6. **Audit:** every import appends a row with source device, digest, counts, and outcome.

Unverified or rejected snapshots remain quarantined — they never become searchable context
(`nodes_fts` is rebuilt only from merged, verified data).

## 5. Testing strategy

`tests/test_graph.py` (pytest):

- schema/connection checks against a deterministic fixture DB in `tmp_path` (never the
  live artifact);
- cluster aggregation: ranking, counts, per-root totals, termux exclusion;
- FTS5: creation, rowid alignment, case/prefix/phrase/boolean matching, tokenizer
  folding, `type UNINDEXED` behavior, rebuild idempotency, dry-run safety;
- one integration test against the real `codex_graph.db`, auto-skipped when the
  git-ignored artifact is absent, so a fresh clone still passes.

## 6. Performance and resource notes

- Cluster queries today scan ~81k rows under both roots via `LIKE`; the `idx_nodes_id_prefix`
  index turns that into an index range scan once adopted by the query tooling.
- Indexing 313k rows into FTS5 is a single-transaction bulk insert (seconds, not minutes).
- Building/rebuilding writes a rollback journal roughly the size of the touched pages:
  budget free disk ≈ DB size (200 MB headroom) before running against the live artifact.

## 7. Sequenced next steps

1. `csv_to_sqlite.py` loader so the DB is reproducible from the CSVs. (not yet done)
2. `roots`/`clusters` population + indexed prefix queries in `query_codex_graph.py`. (not yet done)
3. Handoff digests implemented in `diff_manifests.py`. (not yet done)
4. Snapshot merge tooling with `device_id` namespacing. (not yet done)
