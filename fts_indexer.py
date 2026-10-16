#!/usr/bin/env python3
"""fts_indexer.py — build/rebuild the FTS5 search index over codex_graph.db.

Adds a `nodes_fts` FTS5 virtual table for fast full-text search across node names
and full paths, with rowid alignment to the `nodes` table so results join back:

    SELECT n.* FROM nodes n JOIN nodes_fts f ON n.rowid = f.rowid
    WHERE nodes_fts MATCH ? ORDER BY bm25(nodes_fts);

Design notes (see ROADMAP.md section 3):
  * `type` is stored UNINDEXED: searchable for display, never matched.
  * Sync strategy is rebuild-on-demand inside one transaction, not triggers —
    `nodes` is append-only in practice (snapshots replace whole trees).
  * Idempotent: an already-matching index is left untouched unless --rebuild.

Usage:
    ./fts_indexer.py                      # index ./codex_graph.db (next to this script)
    ./fts_indexer.py --db /path/db.sqlite # index another database
    ./fts_indexer.py --dry-run            # report planned work, write nothing
    ./fts_indexer.py --rebuild            # drop and rebuild the index
    ./fts_indexer.py --verify root --verify GRAVEYARD   # run sample searches

Exit codes: 0 ok, 2 database missing/unusable, 3 index stale (re-run with
--rebuild), 4 FTS5 unavailable, 1 unexpected error.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

FTS_TABLE = "nodes_fts"
DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "codex_graph.db")
DEFAULT_BATCH = 20_000

# External references are avoided on purpose: the FTS table owns its content so
# rebuilds are a single self-contained transaction and snippet()/highlight() work.
CREATE_FTS_SQL = f"""
    CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5(
        name,
        path,
        type UNINDEXED,
        tokenize = 'unicode61 remove_diacritics 2'
    )
"""

# Page through nodes in rowid order; nodes_fts.rowid == nodes.rowid.
POPULATE_PAGE_SQL = """
    SELECT rowid, name, id, label
    FROM nodes
    WHERE rowid > ?
    ORDER BY rowid
    LIMIT ?
"""


class IndexerError(RuntimeError):
    """Raised for expected, reportable failures (missing tables, no FTS5)."""


class RebuildRequiredError(IndexerError):
    """Existing index is stale or has a foreign schema; --rebuild is required."""


# Kept for backward compatibility with callers written against the first cut.
StaleIndexError = RebuildRequiredError

EXPECTED_FTS_COLUMNS = ("name", "path", "type")


def connect(db_path: str, readonly: bool = False) -> sqlite3.Connection:
    """Open the database. readonly uses a URI connection and cannot write."""
    if readonly:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    else:
        if not os.path.exists(db_path):
            raise FileNotFoundError(db_path)
        conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def fts5_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS temp._fts_probe USING fts5(x)"
        )
    except sqlite3.OperationalError:
        return False
    conn.execute("DROP TABLE IF EXISTS temp._fts_probe")
    return True


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def count_nodes(conn: sqlite3.Connection) -> int:
    if not table_exists(conn, "nodes"):
        raise IndexerError("database has no 'nodes' table — not a codex_graph.db?")
    return conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]


def count_fts(conn: sqlite3.Connection) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {FTS_TABLE}").fetchone()[0]


def ensure_fts(conn: sqlite3.Connection) -> None:
    """Create the FTS5 virtual table if it does not exist."""
    conn.execute(CREATE_FTS_SQL)


def fts_columns(conn: sqlite3.Connection) -> tuple:
    """Column names of an existing nodes_fts (empty tuple when absent).

    A database may already carry a foreign FTS definition (e.g. the
    external-content, name-only index with sync triggers that shipped with the
    original 230 MB artifact); callers use this to detect that instead of
    assuming this script's schema.
    """
    if not table_exists(conn, FTS_TABLE):
        return ()
    rows = conn.execute(f"PRAGMA fts5_columns({FTS_TABLE})").fetchall()
    return tuple(r[1] for r in rows if r[2] == 0)  # skip special columns


def drop_fts_triggers(conn: sqlite3.Connection) -> list:
    """Drop triggers that reference nodes_fts; returns their names.

    Needed when replacing a foreign external-content index whose sync triggers
    would otherwise error out against the new table.
    """
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND sql LIKE ?",
        (f"%{FTS_TABLE}%",)
    )]
    for n in names:
        conn.execute(f"DROP TRIGGER IF EXISTS {n}")
    return names


def populate_fts(
    conn: sqlite3.Connection,
    batch_size: int = DEFAULT_BATCH,
    log=lambda _msg: None,
) -> int:
    """Bulk-insert every node into the FTS table, preserving rowid alignment."""
    last_rowid = 0
    total = 0
    while True:
        rows = conn.execute(POPULATE_PAGE_SQL, (last_rowid, batch_size)).fetchall()
        if not rows:
            break
        conn.executemany(
            f"INSERT INTO {FTS_TABLE}(rowid, name, path, type) VALUES (?, ?, ?, ?)",
            [(r["rowid"], r["name"], r["id"], r["label"]) for r in rows],
        )
        last_rowid = rows[-1]["rowid"]
        total += len(rows)
        log(f"  -> indexed {total:,} nodes...")
    return total


def rebuild(conn: sqlite3.Connection, batch_size: int = DEFAULT_BATCH,
            log=lambda _msg: None) -> int:
    """(Re)create the FTS index from `nodes` in a single transaction."""
    conn.isolation_level = None  # explicit transaction control
    conn.execute("BEGIN")
    try:
        conn.execute(f"DROP TABLE IF EXISTS {FTS_TABLE}")
        dropped = drop_fts_triggers(conn)
        if dropped:
            log(f"  -> dropped legacy sync triggers: {', '.join(dropped)}")
        ensure_fts(conn)
        total = populate_fts(conn, batch_size=batch_size, log=log)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return total


def search(conn: sqlite3.Connection, query: str, limit: int = 20):
    """Run an FTS5 MATCH query; returns rows joined back to `nodes` by rowid.

    Schema-aware: snippet() targets the `path` column when this script's index
    is present, falls back to `name` for foreign (e.g. name-only) FTS tables.
    """
    cols = fts_columns(conn)
    if "path" in cols:
        snippet_expr = f"snippet({FTS_TABLE}, 1, '[', ']', '…', 8)"
    elif "name" in cols:
        snippet_expr = f"snippet({FTS_TABLE}, 0, '[', ']', '…', 8)"
    else:
        snippet_expr = "NULL"
    sql = f"""
        SELECT n.rowid AS rowid, n.id AS path, n.name AS name, n.type AS type,
               n.size_bytes AS size_bytes,
               bm25({FTS_TABLE}) AS rank,
               {snippet_expr} AS path_snippet
        FROM {FTS_TABLE} f
        JOIN nodes n ON n.rowid = f.rowid
        WHERE {FTS_TABLE} MATCH ?
        ORDER BY rank
        LIMIT ?
    """
    return conn.execute(sql, (query, limit)).fetchall()


def build_index(
    db_path: str,
    rebuild_flag: bool = False,
    dry_run: bool = False,
    batch_size: int = DEFAULT_BATCH,
    log=print,
) -> dict:
    """Ensure a fresh FTS index for db_path; returns a summary dict.

    dry_run opens the database read-only and reports what *would* happen.
    Raises IndexerError for expected reportable states.
    """
    summary = {"db": db_path, "dry_run": dry_run, "action": "", "nodes": 0, "indexed": 0}

    conn = connect(db_path, readonly=dry_run)
    try:
        if not fts5_available(conn):
            raise IndexerError("this SQLite build has no FTS5 support")

        n_nodes = count_nodes(conn)
        summary["nodes"] = n_nodes
        has_fts = table_exists(conn, FTS_TABLE)
        cols = fts_columns(conn)
        n_fts = count_fts(conn) if has_fts else 0

        if dry_run:
            if not has_fts:
                summary["action"] = f"would create {FTS_TABLE} and index {n_nodes:,} nodes"
            elif cols != EXPECTED_FTS_COLUMNS:
                summary["action"] = (
                    f"would replace foreign-schema {FTS_TABLE} {cols} with "
                    f"{EXPECTED_FTS_COLUMNS} and drop its sync triggers "
                    f"({n_fts:,} rows -> {n_nodes:,})"
                )
            elif rebuild_flag:
                summary["action"] = f"would rebuild {FTS_TABLE} ({n_fts:,} rows -> {n_nodes:,})"
            elif n_fts == n_nodes:
                summary["action"] = f"already up to date ({n_fts:,} rows); no write needed"
            else:
                summary["action"] = f"STALE ({n_fts:,} vs {n_nodes:,} rows); --rebuild required"
            return summary

        if has_fts and cols != EXPECTED_FTS_COLUMNS and not rebuild_flag:
            raise RebuildRequiredError(
                f"existing {FTS_TABLE} has columns {cols} (foreign schema); "
                f"rerun with --rebuild to replace it with {EXPECTED_FTS_COLUMNS}"
            )

        if has_fts and not rebuild_flag:
            if n_fts == n_nodes:
                summary["action"] = f"already up to date ({n_fts:,} rows)"
                return summary
            raise RebuildRequiredError(
                f"{FTS_TABLE} has {n_fts:,} rows but nodes has {n_nodes:,} "
                f"— rerun with --rebuild"
            )

        started = time.monotonic()
        indexed = rebuild(conn, batch_size=batch_size, log=lambda m: log(m, file=sys.stderr))
        elapsed = time.monotonic() - started
        summary["indexed"] = indexed
        summary["action"] = f"rebuilt {FTS_TABLE} with {indexed:,} rows in {elapsed:.1f}s"
        return summary
    finally:
        conn.close()


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fts_indexer.py",
        description="Build/rebuild the FTS5 search index (nodes_fts) over codex_graph.db.",
    )
    p.add_argument("--db", default=DEFAULT_DB, help=f"database path (default: {DEFAULT_DB})")
    p.add_argument("--dry-run", action="store_true",
                   help="report planned work; opens the database read-only, writes nothing")
    p.add_argument("--rebuild", action="store_true",
                   help="drop and rebuild an existing index")
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH,
                   help=f"rows per insert page (default: {DEFAULT_BATCH})")
    p.add_argument("--verify", action="append", metavar="QUERY", default=None,
                   help="run an FTS5 search after indexing (repeatable)")
    p.add_argument("--limit", type=int, default=20, help="max rows per --verify query")
    p.add_argument("--quiet", action="store_true", help="suppress progress output")
    return p


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    log = (lambda *a, **k: None) if args.quiet else print

    if not os.path.exists(args.db):
        print(f"[!] database not found: {args.db}", file=sys.stderr)
        return 2

    try:
        summary = build_index(
            args.db,
            rebuild_flag=args.rebuild,
            dry_run=args.dry_run,
            batch_size=args.batch_size,
            # build_index's progress callback passes file=; swallow it here
            log=lambda m, **_k: log(m),
        )
    except RebuildRequiredError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 3
    except IndexerError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 2
    except sqlite3.OperationalError as exc:
        print(f"[!] sqlite error: {exc}", file=sys.stderr)
        return 2

    tag = "[dry-run] " if summary["dry_run"] else ""
    log(f"[+] {tag}{summary['action']}  (db: {summary['db']})")

    if args.verify:
        conn = connect(args.db, readonly=True)
        try:
            for q in args.verify:
                try:
                    rows = search(conn, q, limit=args.limit)
                except sqlite3.OperationalError as exc:
                    print(f"[!] verify query {q!r} failed: {exc}", file=sys.stderr)
                    return 1
                print(f"[=] {q!r}: {len(rows)} hit(s)")
                for r in rows[:3]:
                    print(f"      {r['path']}")
        finally:
            conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
