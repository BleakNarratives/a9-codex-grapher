"""Tests for device_id merge tooling (merge_snapshots.py).

Fixture graphs are built in tmp_path; the real codex_graph.db is never written
by these tests.
"""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


merge_snapshots = _load("merge_snapshots")
diff_manifests = _load("diff_manifests")
fts_indexer = _load("fts_indexer")

ROOTBASE = "/storage/emulated/0/RootBase"
R26 = "/storage/ED7B-AD5A/r26"
TERMUX = "/data/data/com.termux/files/home"

V1_SCHEMA = [
    "CREATE TABLE nodes (id TEXT PRIMARY KEY, name TEXT, type TEXT,"
    " size_bytes INTEGER, mtime TEXT, label TEXT)",
    "CREATE TABLE edges (source_id TEXT, target_id TEXT, relationship TEXT)",
    "CREATE INDEX idx_nodes_name ON nodes(name)",
    "CREATE INDEX idx_nodes_type ON nodes(type)",
]

SOURCE_RECORDS = [
    {"path": ROOTBASE, "type": "directory", "size": 0},
    {"path": f"{ROOTBASE}/Zips", "type": "directory", "size": 0},
    {"path": f"{ROOTBASE}/Zips/GRAVEYARD.zip", "type": "file", "size": 500},
    {"path": R26, "type": "directory", "size": 0},
    {"path": f"{R26}/Kindred_r26", "type": "directory", "size": 0},
    {"path": f"{R26}/Kindred_r26/agent.md", "type": "file", "size": 10},
]


@pytest.fixture()
def source_ndjson(tmp_path):
    path = tmp_path / "snapshot.ndjson"
    path.write_text("\n".join(json.dumps(r) for r in SOURCE_RECORDS) + "\n",
                    encoding="utf-8")
    return str(path)


@pytest.fixture()
def target_db(tmp_path):
    path = tmp_path / "codex_graph.db"
    conn = sqlite3.connect(path)
    for ddl in V1_SCHEMA:
        conn.execute(ddl)
    # a pre-existing local row that COLLIDES with an incoming id but differs
    conn.execute(
        "INSERT INTO nodes (id, name, type, size_bytes, mtime, label)"
        " VALUES (?, 'Zips', 'directory', 777, '2026-01-01', 'Directory')",
        (f"{ROOTBASE}/Zips",),
    )
    conn.commit()
    conn.close()
    return str(path)


def _count(db, table):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_merge_inserts_and_namespaces(source_ndjson, target_db):
    summary = merge_snapshots.merge(
        source=source_ndjson, target=target_db, device_id="pixel-7", with_fts=False)

    assert summary["outcome"] == "merged"
    assert summary["verified"] == 1
    assert summary["nodes_total"] == len(SOURCE_RECORDS)
    # 6 incoming, 1 collides with the pre-existing row -> 5 inserted
    assert summary["nodes_inserted"] == 5
    assert summary["nodes_skipped"] == 1
    assert _count(target_db, "nodes") == 6  # 1 pre-existing + 5 new

    conn = sqlite3.connect(target_db)
    try:
        new_row = conn.execute(
            "SELECT device_id, snapshot_at FROM nodes WHERE id = ?",
            (f"{R26}/Kindred_r26/agent.md",)).fetchone()
        assert new_row[0] == "pixel-7"
        assert new_row[1] == summary["snapshot_at"]
        # collision must NOT overwrite local rows: size and namespace untouched
        collision = conn.execute(
            "SELECT size_bytes, device_id FROM nodes WHERE id = ?",
            (f"{ROOTBASE}/Zips",)).fetchone()
        assert collision == (777, None)
    finally:
        conn.close()


def test_merge_records_roots_and_audit(source_ndjson, target_db):
    summary = merge_snapshots.merge(
        source=source_ndjson, target=target_db, device_id="pixel-7", with_fts=False)

    aliases = {r[2] for r in summary["roots"]}
    assert aliases == {"RootBase", "r26"}

    conn = sqlite3.connect(target_db)
    try:
        root_rows = conn.execute(
            "SELECT device_id, root_alias, root_path FROM roots ORDER BY root_alias"
        ).fetchall()
        assert root_rows == [
            ("pixel-7", "RootBase", ROOTBASE),
            ("pixel-7", "r26", R26),
        ]
        audit = conn.execute(
            "SELECT device_id, digest, expected_digest, nodes_total, nodes_inserted,"
            " verified, outcome FROM imports ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    assert audit[0] == "pixel-7"
    assert audit[1] == summary["digest"] == diff_manifests.manifest_digest(source_ndjson)
    assert audit[2] is None
    assert (audit[3], audit[4]) == (6, 5)
    assert audit[5] == 1
    assert audit[6] == "merged"


def test_merge_rebuilds_fts_so_new_rows_are_searchable(source_ndjson, target_db):
    merge_snapshots.merge(source=source_ndjson, target=target_db, device_id="pixel-7")

    conn = fts_indexer.connect(target_db, readonly=True)
    try:
        assert fts_indexer.count_fts(conn) == fts_indexer.count_nodes(conn)
        hits = [r["path"] for r in fts_indexer.search(conn, "GRAVEYARD")]
        assert any(h.endswith("/Zips/GRAVEYARD.zip") for h in hits)
    finally:
        conn.close()


def test_digest_mismatch_quarantines_import(source_ndjson, target_db):
    wrong = "0" * 64
    summary = merge_snapshots.merge(
        source=source_ndjson, target=target_db, device_id="pixel-7",
        expect_digest=wrong, with_fts=False)

    assert summary["outcome"] == "rejected_digest_mismatch"
    assert summary["verified"] == 0
    assert summary["nodes_inserted"] == 0
    assert _count(target_db, "nodes") == 1  # untouched

    conn = sqlite3.connect(target_db)
    try:
        audit = conn.execute(
            "SELECT verified, outcome, expected_digest FROM imports ORDER BY id DESC LIMIT 1"
        ).fetchone()
        node_with_device = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE device_id IS NOT NULL").fetchone()[0]
    finally:
        conn.close()
    assert audit == (0, "rejected_digest_mismatch", wrong)
    assert node_with_device == 0


def test_digest_match_is_reported(source_ndjson, target_db):
    good = diff_manifests.manifest_digest(source_ndjson)
    summary = merge_snapshots.merge(
        source=source_ndjson, target=target_db, device_id="pixel-7",
        expect_digest=good, with_fts=False)
    assert summary["outcome"] == "merged"
    assert summary["verified"] == 1


def test_repeat_merge_is_idempotent(source_ndjson, target_db):
    merge_snapshots.merge(source=source_ndjson, target=target_db,
                          device_id="pixel-7", with_fts=False)
    second = merge_snapshots.merge(source=source_ndjson, target=target_db,
                                   device_id="pixel-7", with_fts=False)
    assert second["nodes_inserted"] == 0
    assert second["nodes_skipped"] == len(SOURCE_RECORDS)
    assert _count(target_db, "nodes") == 6
    assert _count(target_db, "imports") == 2  # both attempts audited


def test_dry_run_writes_nothing(source_ndjson, target_db):
    before_nodes = _count(target_db, "nodes")
    summary = merge_snapshots.merge(
        source=source_ndjson, target=target_db, device_id="pixel-7",
        dry_run=True, with_fts=False)

    assert summary["dry_run"] is True
    assert summary["outcome"] == "dry_run"
    assert summary["nodes_inserted"] == 5  # computed, not applied
    conn = sqlite3.connect(target_db)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "imports" not in tables and "roots" not in tables
        assert "device_id" not in merge_snapshots._column_names(conn, "nodes")
    finally:
        conn.close()
    assert _count(target_db, "nodes") == before_nodes


def test_cli_exit_codes(source_ndjson, target_db, capsys):
    good = diff_manifests.manifest_digest(source_ndjson)

    rc = merge_snapshots.main([
        "--source", source_ndjson, "--target", target_db,
        "--device-id", "pixel-7", "--expect-digest", good, "--no-fts",
    ])
    assert rc == 0
    assert "outcome: merged" in capsys.readouterr().out

    rc = merge_snapshots.main([
        "--source", source_ndjson, "--target", target_db,
        "--device-id", "pixel-7", "--expect-digest", "f" * 64, "--no-fts",
    ])
    assert rc == 3
    out = capsys.readouterr().out
    assert "rejected_digest_mismatch" in out

    rc = merge_snapshots.main([
        "--source", str(Path(target_db).parent / "absent.ndjson"),
        "--target", target_db, "--device-id", "x",
    ])
    assert rc == 2
    assert "source not found" in capsys.readouterr().err


def test_missing_device_id_rejected(source_ndjson, target_db):
    with pytest.raises(merge_snapshots.MergeError):
        merge_snapshots.merge(source=source_ndjson, target=target_db, device_id="")


def test_sqlite_source_merges_nodes_and_edges(tmp_path, target_db):
    """Edges from a DB source are imported with NOT EXISTS dedupe."""
    src = tmp_path / "source_graph.db"
    conn = sqlite3.connect(src)
    for ddl in V1_SCHEMA:
        conn.execute(ddl)
    conn.executemany(
        "INSERT INTO nodes VALUES (?, ?, ?, ?, ?, ?)",
        [(ROOTBASE, "RootBase", "directory", 0, "1", "Directory"),
         (f"{ROOTBASE}/a.txt", "a.txt", "file", 5, "1", "File"),
         (f"{ROOTBASE}/b.txt", "b.txt", "file", 6, "1", "File")],
    )
    conn.executemany("INSERT INTO edges VALUES (?, ?, ?)",
                     [(ROOTBASE, f"{ROOTBASE}/a.txt", "CONTAINS"),
                      (ROOTBASE, f"{ROOTBASE}/b.txt", "CONTAINS")])
    conn.commit()
    conn.close()

    first = merge_snapshots.merge(source=str(src), target=target_db,
                                  device_id="desk-pc", with_fts=False)
    assert first["nodes_inserted"] == 3
    assert first["edges_total"] == 2 and first["edges_inserted"] == 2
    assert _count(target_db, "edges") == 2

    second = merge_snapshots.merge(source=str(src), target=target_db,
                                   device_id="desk-pc", with_fts=False)
    assert second["nodes_inserted"] == 0
    assert second["edges_inserted"] == 0  # NOT EXISTS guard, no duplicates
    assert _count(target_db, "edges") == 2

    # cross-device digest: source DB digest must be stable for the same tree
    assert first["digest"] == diff_manifests.manifest_digest(str(src))
