#!/usr/bin/env python3
"""merge_snapshots.py — import a snapshot into codex_graph.db with device namespacing.

Implements the cross-device handoff described in ROADMAP.md section 4:

  1. VERIFY   the source artifact's handoff digest (SHA256 over sorted
              path\\0size lines, computed by diff_manifests.manifest_digest)
              before anything is written. A mismatch aborts the import — the
              artifact stays quarantined — and the attempt is audited.
  2. IMPORT   nodes with INSERT OR IGNORE under (device_id, snapshot_at)
              columns, so another device's snapshot never overwrites rows
              already in the target.
  3. RECORD   the roots the snapshot covered, and append an audit row to the
              `imports` table (source device, digest, counts, outcome).
  4. REBUILD  the FTS5 index so merged rows become searchable.

The target schema stays backward compatible: new columns are added with
ALTER TABLE (nullable), and `roots`/`imports` are created only if absent.

Usage:
    python merge_snapshots.py --source ~/a9_codex_fast.ndjson \\
        --target codex_graph.db --device-id pixel-7 --expect-digest <hex>

Exit codes: 0 ok, 2 usage/IO error, 3 digest mismatch (nothing imported).
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone

import diff_manifests
import fts_indexer

DEFAULT_TARGET = os.path.join(os.path.dirname(os.path.abspath(__file__)), "codex_graph.db")

# Known scan roots of the fleet; matched by path prefix to populate `roots`.
ROOT_ALIASES = (
    ("termux-home", "/data/data/com.termux/files/home"),
    ("RootBase", "/storage/emulated/0/RootBase"),
    ("ChronoScouts", "/storage/emulated/0/ChronoScouts"),
    ("r26", "/storage/ED7B-AD5A/r26"),
)

V1_NODE_COLUMNS = ("id", "name", "type", "size_bytes", "mtime", "label")

DDL = """
    CREATE TABLE IF NOT EXISTS roots (
        device_id   TEXT NOT NULL,
        snapshot_at TEXT NOT NULL,
        root_alias  TEXT NOT NULL,
        root_path   TEXT NOT NULL,
        PRIMARY KEY (device_id, snapshot_at, root_alias)
    );
    CREATE TABLE IF NOT EXISTS imports (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id       TEXT NOT NULL,
        snapshot_at     TEXT NOT NULL,
        source          TEXT NOT NULL,
        digest          TEXT,
        expected_digest TEXT,
        nodes_total     INTEGER NOT NULL DEFAULT 0,
        nodes_inserted  INTEGER NOT NULL DEFAULT 0,
        nodes_skipped   INTEGER NOT NULL DEFAULT 0,
        edges_total     INTEGER NOT NULL DEFAULT 0,
        edges_inserted  INTEGER NOT NULL DEFAULT 0,
        verified        INTEGER NOT NULL DEFAULT 0,
        outcome         TEXT NOT NULL,
        imported_at     TEXT NOT NULL
    );
"""


class MergeError(RuntimeError):
    """Expected, reportable failure (schema mismatch, bad source)."""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _column_names(conn, table):
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def ensure_schema(conn) -> None:
    """Add the v2 handoff objects to a v1 database (additive only)."""
    cols = _column_names(conn, "nodes")
    if not cols:
        raise MergeError("target has no 'nodes' table — not a codex_graph.db?")
    missing = [c for c in V1_NODE_COLUMNS if c not in cols]
    if missing:
        raise MergeError(f"target nodes table is missing columns: {', '.join(missing)}")
    if "device_id" not in cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN device_id TEXT")
    if "snapshot_at" not in cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN snapshot_at TEXT")
    conn.executescript(DDL)


def read_source(source):
    """Return (rows, edges) where rows are node tuples ready for insert."""
    if not os.path.exists(source):
        raise MergeError(f"source not found: {source}")
    rows = []
    if _looks_sqlite(source):
        conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        try:
            has_edges = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='edges'"
            ).fetchone()
            for row in conn.execute(
                "SELECT id, name, type, size_bytes, mtime, label FROM nodes"
            ):
                rows.append(row)
            edges = list(conn.execute("SELECT source_id, target_id, relationship FROM edges")) if has_edges else []
        finally:
            conn.close()
        return rows, edges

    edges = []
    if source.endswith(".ndjson"):
        import json
        with open(source, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                data = json.loads(line)
                path = data.get("path") or ""
                ntype = data.get("type", "file")
                rows.append((
                    path,
                    path.rsplit("/", 1)[-1] or path,
                    ntype,
                    int(data.get("size") or 0),
                    data.get("mtime", ""),
                    "Directory" if ntype == "directory" else "File",
                ))
    else:
        import json
        with open(source, "r", encoding="utf-8") as f:
            raw = json.load(f)
        nodes = raw.get("nodes", raw if isinstance(raw, list) else [])
        for item in nodes:
            path = item.get("path") or ""
            ntype = item.get("type", "file")
            size = item.get("size", 0)
            rows.append((
                path,
                path.rsplit("/", 1)[-1] or path,
                ntype,
                int(size) if isinstance(size, (int, float)) and not isinstance(size, bool) else 0,
                item.get("mtime", ""),
                "Directory" if ntype == "directory" else "File",
            ))
    return rows, edges


def _looks_sqlite(path):
    if path.endswith((".db", ".sqlite", ".sqlite3")):
        return True
    try:
        with open(path, "rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def detected_roots(rows, device_id, snapshot_at):
    roots = []
    seen = set()
    for row in rows:
        node_id = row[0] or ""
        for alias, prefix in ROOT_ALIASES:
            if alias in seen:
                continue
            if node_id == prefix or node_id.startswith(prefix + "/"):
                roots.append((device_id, snapshot_at, alias, prefix))
                seen.add(alias)
    return roots


def merge(source, target, device_id, snapshot_at=None, expect_digest=None,
           dry_run=False, with_fts=True):
    """Merge ``source`` into ``target``. Returns a summary dict; raises MergeError."""
    snapshot_at = snapshot_at or utc_now()
    if not device_id:
        raise MergeError("--device-id is required")
    if not os.path.exists(source):
        raise MergeError(f"source not found: {source}")

    digest = diff_manifests.manifest_digest(source)
    summary = {
        "source": source, "target": target, "device_id": device_id,
        "snapshot_at": snapshot_at, "digest": digest,
        "expected_digest": expect_digest, "nodes_total": 0, "nodes_inserted": 0,
        "nodes_skipped": 0, "edges_total": 0, "edges_inserted": 0,
        "roots": [], "verified": 1, "outcome": "", "dry_run": dry_run,
    }

    verified = expect_digest is None or expect_digest == digest
    summary["verified"] = 1 if verified else 0

    conn = sqlite3.connect(target) if not dry_run else sqlite3.connect(
        f"file:{target}?mode=ro", uri=True)
    try:
        if not verified:
            # Quarantine: audit the rejected attempt, import nothing.
            summary["outcome"] = "rejected_digest_mismatch"
            if not dry_run:
                ensure_schema(conn)
                with conn:
                    _audit(conn, summary)
            return summary

        rows, edges = read_source(source)
        # dry-run holds a read-only handle: validate the target, never write.
        cols = _column_names(conn, "nodes")
        missing = [c for c in V1_NODE_COLUMNS if c not in cols]
        if not cols:
            raise MergeError("target has no 'nodes' table — not a codex_graph.db?")
        if missing:
            raise MergeError(f"target nodes table is missing columns: {', '.join(missing)}")
        if not dry_run:
            ensure_schema(conn)
        summary["nodes_total"] = len(rows)
        summary["edges_total"] = len(edges)

        existing = {r[0] for r in conn.execute("SELECT id FROM nodes")}
        new_rows = [r for r in rows if r[0] not in existing]
        summary["nodes_inserted"] = len(new_rows)
        summary["nodes_skipped"] = len(rows) - len(new_rows)

        edge_pairs = None
        if edges:
            have = {tuple(r) for r in conn.execute(
                "SELECT source_id, target_id, relationship FROM edges")}
            edge_pairs = [e for e in edges if tuple(e) not in have]
            summary["edges_inserted"] = len(edge_pairs)

        summary["roots"] = detected_roots(rows, device_id, snapshot_at)
        summary["outcome"] = "merged" if not dry_run else "dry_run"

        if dry_run:
            return summary

        with conn:
            conn.executemany(
                "INSERT OR IGNORE INTO nodes (id, name, type, size_bytes, mtime, label,"
                " device_id, snapshot_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [r + (device_id, snapshot_at) for r in new_rows],
            )
            if edge_pairs:
                conn.executemany(
                    "INSERT INTO edges (source_id, target_id, relationship)"
                    " SELECT ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM edges"
                    " WHERE source_id=? AND target_id=? AND relationship=?)",
                    [e + e for e in edge_pairs],
                )
            if summary["roots"]:
                conn.executemany(
                    "INSERT OR IGNORE INTO roots (device_id, snapshot_at, root_alias,"
                    " root_path) VALUES (?, ?, ?, ?)", summary["roots"])
            _audit(conn, summary)

        if with_fts:
            try:
                fts_indexer.build_index(target, rebuild_flag=True,
                                        log=lambda *_a, **_k: None)
            except fts_indexer.IndexerError as exc:
                summary["outcome"] = f"merged_without_fts: {exc}"
        return summary
    finally:
        conn.close()


def _audit(conn, summary):
    conn.execute(
        "INSERT INTO imports (device_id, snapshot_at, source, digest, expected_digest,"
        " nodes_total, nodes_inserted, nodes_skipped, edges_total, edges_inserted,"
        " verified, outcome, imported_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (summary["device_id"], summary["snapshot_at"], summary["source"],
         summary["digest"], summary["expected_digest"], summary["nodes_total"],
         summary["nodes_inserted"], summary["nodes_skipped"], summary["edges_total"],
         summary["edges_inserted"], summary["verified"], summary["outcome"], utc_now()),
    )


def _parser():
    p = argparse.ArgumentParser(prog="merge_snapshots.py", description=__doc__)
    p.add_argument("--source", required=True,
                   help="snapshot to import (.ndjson, .json, or a SQLite graph db)")
    p.add_argument("--target", default=DEFAULT_TARGET, help="database to merge into")
    p.add_argument("--device-id", required=True, help="stable id of the producing device")
    p.add_argument("--snapshot-at", default=None, help="ISO-8601 UTC timestamp of the snapshot")
    p.add_argument("--expect-digest", default=None,
                   help="handoff digest the sender reported; mismatch aborts the import")
    p.add_argument("--dry-run", action="store_true",
                   help="report what would change; opens the target read-only")
    p.add_argument("--no-fts", action="store_true", help="skip the post-merge FTS rebuild")
    return p


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        summary = merge(
            source=args.source, target=args.target, device_id=args.device_id,
            snapshot_at=args.snapshot_at, expect_digest=args.expect_digest,
            dry_run=args.dry_run, with_fts=not args.no_fts,
        )
    except MergeError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 2

    prefix = "[dry-run] " if summary["dry_run"] else ""
    print(f"[+] {prefix}outcome: {summary['outcome']}")
    print(f"    device:  {summary['device_id']} @ {summary['snapshot_at']}")
    print(f"    digest:  sha256:{summary['digest']}"
          + (f" (expected {summary['expected_digest']})" if summary["expected_digest"] else ""))
    print(f"    nodes:   {summary['nodes_total']:,} total, "
          f"{summary['nodes_inserted']:,} inserted, {summary['nodes_skipped']:,} skipped")
    print(f"    edges:   {summary['edges_total']:,} total, "
          f"{summary['edges_inserted']:,} inserted")
    if summary["roots"]:
        print(f"    roots:   {', '.join(r[2] for r in summary['roots'])}")

    return 0 if summary["verified"] else 3


if __name__ == "__main__":
    sys.exit(main())
