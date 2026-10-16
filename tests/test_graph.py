"""Tests for a9-codex-grapher: DB schema/connections, cluster queries, FTS5 search.

Deterministic by design: everything runs against a fixture database built in
tmp_path. The single integration test against the real, git-ignored
`codex_graph.db` is skipped automatically when that artifact is absent.
"""
import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_DB = REPO_ROOT / "codex_graph.db"

# --- fts_indexer is a standalone script, not a package: load it by path -------
_spec = importlib.util.spec_from_file_location("fts_indexer", REPO_ROOT / "fts_indexer.py")
fts_indexer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fts_indexer)

# --- schema exactly as it exists in codex_graph.db (v1) ----------------------
SCHEMA_DDL = [
    "CREATE TABLE nodes (id TEXT PRIMARY KEY, name TEXT, type TEXT,"
    " size_bytes INTEGER, mtime TEXT, label TEXT)",
    "CREATE TABLE edges (source_id TEXT, target_id TEXT, relationship TEXT)",
    "CREATE INDEX idx_nodes_name ON nodes(name)",
    "CREATE INDEX idx_nodes_type ON nodes(type)",
]

TERMUX = "/data/data/com.termux/files/home"
ROOTBASE = "/storage/emulated/0/RootBase"
R26 = "/storage/ED7B-AD5A/r26"
CHRONO = "/storage/emulated/0/ChronoScouts"
ROOTS_UNDER_TEST = (ROOTBASE, R26)

# (path, type, size) — mtime/label/name derived deterministically.
NODE_PATHS = [
    # termux home (must never appear in RootBase/r26 cluster results)
    (TERMUX, "directory", 0),
    (f"{TERMUX}/README.md", "file", 876),
    (f"{TERMUX}/notes", "directory", 0),
    (f"{TERMUX}/notes/Graveyard_manifest.md", "file", 1200),
    # RootBase
    (ROOTBASE, "directory", 0),
    (f"{ROOTBASE}/Zips", "directory", 0),
    (f"{ROOTBASE}/Zips/GRAVEYARD.zip", "file", 1_300_000_000),
    (f"{ROOTBASE}/Zips/Loosies.zip", "file", 50_000_000),
    (f"{ROOTBASE}/go", "directory", 0),
    (f"{ROOTBASE}/go/main.go", "file", 1000),
    (f"{ROOTBASE}/go/README.md", "file", 50),
    (f"{ROOTBASE}/caf\u00e9_notes", "directory", 0),
    (f"{ROOTBASE}/caf\u00e9_notes/menu.txt", "file", 42),
    (f"{ROOTBASE}/root_2026", "directory", 0),
    (f"{ROOTBASE}/root_2026/directive.md", "file", 747),
    # r26
    (R26, "directory", 0),
    (f"{R26}/BleakNarratives_Archives", "directory", 0),
    (f"{R26}/BleakNarratives_Archives/GRAVEYARD.zip", "file", 100_000_000),
    (f"{R26}/Kindred_r26", "directory", 0),
    (f"{R26}/Kindred_r26/agent.md", "file", 10),
    # ChronoScouts (outside the two roots under test)
    (CHRONO, "directory", 0),
    (f"{CHRONO}/scout.log", "file", 5),
]


def _label(ntype: str) -> str:
    return "Directory" if ntype == "directory" else "File"


@pytest.fixture()
def db_path(tmp_path):
    """A v1-schema codex_graph.db fixture with deterministic contents."""
    path = tmp_path / "codex_graph.db"
    conn = sqlite3.connect(path)
    for ddl in SCHEMA_DDL:
        conn.execute(ddl)
    paths = set()
    for p, ntype, size in NODE_PATHS:
        paths.add(p)
        conn.execute(
            "INSERT INTO nodes (id, name, type, size_bytes, mtime, label)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (p, p.rsplit("/", 1)[-1] or p, ntype, size, "2026-10-15T00:00:00",
             _label(ntype)),
        )
    for p, _, _ in NODE_PATHS:
        parent = p.rsplit("/", 1)[0]
        if parent in paths and parent != p:
            conn.execute(
                "INSERT INTO edges (source_id, target_id, relationship)"
                " VALUES (?, ?, 'CONTAINS')",
                (parent, p),
            )
    conn.commit()
    conn.close()
    return str(path)


@pytest.fixture()
def indexed_db(db_path):
    """The fixture DB with a freshly built FTS5 index."""
    summary = fts_indexer.build_index(db_path, log=lambda *_a, **_k: None)
    assert summary["indexed"] == len(NODE_PATHS)
    return db_path


def top_clusters(conn, roots=ROOTS_UNDER_TEST, limit=10):
    """Cluster aggregation: rank top-level subtrees under each root by bytes.

    Mirrors the query documented in ROADMAP.md section 6.
    """
    import collections

    clusters = collections.defaultdict(
        lambda: {"files": 0, "dirs": 0, "bytes": 0, "root": ""}
    )
    for root in roots:
        for pid, ntype, size in conn.execute(
            "SELECT id, type, size_bytes FROM nodes WHERE id = ? OR id LIKE ?",
            (root, root + "/%"),
        ):
            rest = pid[len(root):].lstrip("/")
            if not rest:
                continue
            key = f"{root}/{rest.split('/', 1)[0]}"
            c = clusters[key]
            c["root"] = root
            if ntype == "directory":
                c["dirs"] += 1
            else:
                c["files"] += 1
                c["bytes"] += size or 0
    ranked = sorted(clusters.items(), key=lambda kv: kv[1]["bytes"], reverse=True)
    return ranked[:limit]


# ---------------------------------------------------------------- connections


def test_connect_readonly_refuses_write(db_path):
    conn = fts_indexer.connect(db_path, readonly=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE nope (x)")
    finally:
        conn.close()


def test_connect_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        fts_indexer.connect(str(tmp_path / "absent.db"))


def test_schema_matches_v1(db_path):
    conn = sqlite3.connect(db_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(nodes)")]
    assert cols == ["id", "name", "type", "size_bytes", "mtime", "label"]
    edge_cols = [r[1] for r in conn.execute("PRAGMA table_info(edges)")]
    assert edge_cols == ["source_id", "target_id", "relationship"]
    idx = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='nodes'")}
    assert {"idx_nodes_name", "idx_nodes_type"} <= idx
    conn.close()


def test_node_counts_and_labels(db_path):
    conn = fts_indexer.connect(db_path, readonly=True)
    try:
        assert fts_indexer.count_nodes(conn) == len(NODE_PATHS)
        n_files = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE type='file'").fetchone()[0]
        n_dirs = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE type='directory'").fetchone()[0]
        assert n_files + n_dirs == len(NODE_PATHS)
        assert n_files > 0 and n_dirs > 0
    finally:
        conn.close()


# ------------------------------------------------------------ cluster queries


def test_cluster_ranking(db_path):
    conn = fts_indexer.connect(db_path, readonly=True)
    try:
        ranked = top_clusters(conn)
    finally:
        conn.close()
    paths = [p for p, _ in ranked]
    assert paths == [
        f"{ROOTBASE}/Zips",                          # 1.35 GB
        f"{R26}/BleakNarratives_Archives",           # 100 MB
        f"{ROOTBASE}/go",                            # 1050 B
        f"{ROOTBASE}/root_2026",                     # 747 B
        f"{ROOTBASE}/caf\u00e9_notes",               # 42 B
        f"{R26}/Kindred_r26",                        # 10 B
    ]
    stats = {p: v for p, v in ranked}
    assert stats[f"{ROOTBASE}/Zips"]["bytes"] == 1_350_000_000
    assert stats[f"{ROOTBASE}/Zips"]["files"] == 2
    assert stats[f"{ROOTBASE}/go"]["dirs"] >= 0


def test_cluster_query_excludes_other_roots(db_path):
    conn = fts_indexer.connect(db_path, readonly=True)
    try:
        ranked = top_clusters(conn, limit=len(NODE_PATHS))
    finally:
        conn.close()
    paths = [p for p, _ in ranked]
    assert not any(p.startswith(TERMUX) for p in paths)
    assert not any(p.startswith(CHRONO) for p in paths)
    assert all(p.startswith((ROOTBASE + "/", R26 + "/")) for p in paths)


def test_cluster_root_totals(db_path):
    conn = fts_indexer.connect(db_path, readonly=True)
    try:
        ranked = top_clusters(conn, limit=len(NODE_PATHS))
    finally:
        conn.close()
    rb = sum(v["bytes"] for p, v in ranked if v["root"] == ROOTBASE)
    r26 = sum(v["bytes"] for p, v in ranked if v["root"] == R26)
    assert rb == 1_300_000_000 + 50_000_000 + 1000 + 50 + 42 + 747
    assert r26 == 100_000_000 + 10


def test_edges_reference_existing_nodes(db_path):
    conn = fts_indexer.connect(db_path, readonly=True)
    try:
        orphans = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE NOT EXISTS"
            " (SELECT 1 FROM nodes n WHERE n.id = edges.source_id)"
            " OR NOT EXISTS (SELECT 1 FROM nodes n WHERE n.id = edges.target_id)"
        ).fetchone()[0]
        assert orphans == 0
        n_edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        assert n_edges == len(NODE_PATHS) - 1  # every node but the roots has a parent
    finally:
        conn.close()


# ------------------------------------------------------------------ FTS5


def test_fts_rowid_alignment(indexed_db):
    conn = fts_indexer.connect(indexed_db, readonly=True)
    try:
        assert fts_indexer.count_fts(conn) == fts_indexer.count_nodes(conn)
        mismatched = conn.execute(
            "SELECT COUNT(*) FROM nodes_fts f JOIN nodes n ON n.rowid = f.rowid"
            " WHERE f.path != n.id"
        ).fetchone()[0]
        assert mismatched == 0
    finally:
        conn.close()


def test_fts_match_returns_expected_path(indexed_db):
    conn = fts_indexer.connect(indexed_db, readonly=True)
    try:
        rows = fts_indexer.search(conn, "GRAVEYARD")
    finally:
        conn.close()
    paths = [r["path"] for r in rows]
    assert f"{ROOTBASE}/Zips/GRAVEYARD.zip" in paths
    assert f"{R26}/BleakNarratives_Archives/GRAVEYARD.zip" in paths
    # bm25 ranks the 100 MB archive's smaller sibling folder context first or
    # second; both must be present and ranks strictly ascending (lower = better)
    ranks = [r["rank"] for r in rows if "GRAVEYARD.zip" in r["path"]]
    assert ranks == sorted(ranks)


def test_fts_case_insensitive(indexed_db):
    conn = fts_indexer.connect(indexed_db, readonly=True)
    try:
        lower = fts_indexer.search(conn, "graveyard")
        upper = fts_indexer.search(conn, "GRAVEYARD")
        mixed = fts_indexer.search(conn, "GraveYard")
    finally:
        conn.close()
    assert [r["path"] for r in lower] == [r["path"] for r in upper] == \
        [r["path"] for r in mixed]


def test_fts_prefix_search(indexed_db):
    conn = fts_indexer.connect(indexed_db, readonly=True)
    try:
        rows = fts_indexer.search(conn, "kind*")
    finally:
        conn.close()
    assert any(r["path"].endswith("/Kindred_r26/agent.md") for r in rows)


def test_fts_phrase_search(indexed_db):
    conn = fts_indexer.connect(indexed_db, readonly=True)
    try:
        phrase = fts_indexer.search(conn, '"root_2026"')
        loose = fts_indexer.search(conn, "root 2026")
    finally:
        conn.close()
    assert any(r["path"] == f"{ROOTBASE}/root_2026/directive.md" for r in phrase)
    # unquoted terms are AND-ed: both tokens must appear in the same row
    assert any(r["path"] == f"{ROOTBASE}/root_2026/directive.md" for r in loose)


def test_fts_boolean_search(indexed_db):
    conn = fts_indexer.connect(indexed_db, readonly=True)
    try:
        rows = fts_indexer.search(conn, "GRAVEYARD AND NOT Kindred")
    finally:
        conn.close()
    assert rows, "boolean query returned nothing"
    assert all("Kindred" not in r["path"] for r in rows)


def test_fts_unicode_diacritic_folding(indexed_db):
    conn = fts_indexer.connect(indexed_db, readonly=True)
    try:
        rows = fts_indexer.search(conn, "cafe")
        folded = [r["path"] for r in rows]
    finally:
        conn.close()
    assert any("caf\u00e9_notes" in p for p in folded)


def test_fts_type_column_not_indexed(indexed_db):
    conn = fts_indexer.connect(indexed_db, readonly=True)
    try:
        rows = fts_indexer.search(conn, "File")
        rows_dir = fts_indexer.search(conn, "Directory")
    finally:
        conn.close()
    assert rows == [] and rows_dir == []


def test_fts_snippet_and_rank_fields(indexed_db):
    conn = fts_indexer.connect(indexed_db, readonly=True)
    try:
        rows = fts_indexer.search(conn, "manifest")
    finally:
        conn.close()
    assert rows, "expected a hit for 'manifest'"
    hit = rows[0]
    assert hit["path"].endswith("/notes/Graveyard_manifest.md")
    assert "[" in hit["path_snippet"] and "]" in hit["path_snippet"]
    assert isinstance(hit["rank"], float)


# ------------------------------------------------------- index lifecycle / CLI


def test_dry_run_creates_nothing(db_path):
    summary = fts_indexer.build_index(db_path, dry_run=True,
                                      log=lambda *_a, **_k: None)
    assert summary["dry_run"] is True
    assert "would create" in summary["action"]
    conn = sqlite3.connect(db_path)
    try:
        assert not fts_indexer.table_exists(conn, fts_indexer.FTS_TABLE)
    finally:
        conn.close()


def test_build_is_idempotent(indexed_db):
    summary = fts_indexer.build_index(indexed_db, log=lambda *_a, **_k: None)
    assert "already up to date" in summary["action"]
    assert summary["indexed"] == 0


def test_stale_index_detected_and_rebuild_fixes_it(indexed_db):
    conn = sqlite3.connect(indexed_db)
    conn.execute(
        "INSERT INTO nodes (id, name, type, size_bytes, mtime, label)"
        " VALUES (?, 'newfile.md', 'file', 1, '2026-10-15T00:00:00', 'File')",
        (f"{TERMUX}/newfile.md",),
    )
    conn.commit()
    conn.close()

    with pytest.raises(fts_indexer.StaleIndexError):
        fts_indexer.build_index(indexed_db, log=lambda *_a, **_k: None)

    summary = fts_indexer.build_index(indexed_db, rebuild_flag=True,
                                      log=lambda *_a, **_k: None)
    assert summary["indexed"] == len(NODE_PATHS) + 1

    conn = fts_indexer.connect(indexed_db, readonly=True)
    try:
        assert fts_indexer.count_fts(conn) == fts_indexer.count_nodes(conn)
        assert any(r["path"].endswith("newfile.md")
                   for r in fts_indexer.search(conn, "newfile"))
    finally:
        conn.close()


def test_cli_main_end_to_end(db_path, capsys):
    rc = fts_indexer.main(["--db", db_path, "--quiet", "--verify", "GRAVEYARD"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "hit(s)" in out
    conn = fts_indexer.connect(db_path, readonly=True)
    try:
        assert fts_indexer.table_exists(conn, fts_indexer.FTS_TABLE)
    finally:
        conn.close()


def test_cli_missing_db_exits_2(tmp_path, capsys):
    rc = fts_indexer.main(["--db", str(tmp_path / "nope.db"), "--quiet"])
    assert rc == 2
    assert "not found" in capsys.readouterr().err


# ------------------------------------------------------- integration (optional)


@pytest.mark.skipif(not REAL_DB.exists(),
                    reason="codex_graph.db is a git-ignored local artifact")
def test_real_db_readonly_search():
    conn = fts_indexer.connect(str(REAL_DB), readonly=True)
    try:
        n_nodes = fts_indexer.count_nodes(conn)
        assert n_nodes > 300_000
        n_edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        assert n_edges > 300_000
        if not fts_indexer.table_exists(conn, fts_indexer.FTS_TABLE):
            pytest.skip("nodes_fts not materialized in the local artifact yet")
        assert fts_indexer.count_fts(conn) == n_nodes
        rows = fts_indexer.search(conn, "GRAVEYARD", limit=5)
        assert rows, "expected GRAVEYARD hits in the real artifact"
    finally:
        conn.close()
