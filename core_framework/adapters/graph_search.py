#!/usr/bin/env python3
"""Search the codex graph's FTS5 index.

Examples:
    python core_framework/adapters/graph_search.py search 'GRAVEYARD'
    python core_framework/adapters/graph_search.py search '"root 2026"' --limit 5
"""

import argparse
import sqlite3
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import fts_indexer


def _positive_int(value):
    limit = int(value)
    if limit < 1:
        raise argparse.ArgumentTypeError("limit must be greater than zero")
    return limit


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search", help="search the FTS5 index")
    search.add_argument("expression", help="FTS5 MATCH expression")
    search.add_argument(
        "--db",
        default=fts_indexer.DEFAULT_DB,
        help=f"SQLite graph database (default: {fts_indexer.DEFAULT_DB})",
    )
    search.add_argument("--limit", type=_positive_int, default=20)
    return parser


def _search_name_only(conn, expression, limit):
    return conn.execute(
        """
        SELECT n.id AS path, n.name AS name, n.type AS type,
               n.size_bytes AS size_bytes, bm25(nodes_fts) AS rank,
               snippet(nodes_fts, 0, '[', ']', '...', 8) AS match_snippet
        FROM nodes_fts
        JOIN nodes n ON n.rowid = nodes_fts.rowid
        WHERE nodes_fts MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (expression, limit),
    ).fetchall()


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        conn = fts_indexer.connect(args.db, readonly=True)
    except (FileNotFoundError, sqlite3.OperationalError) as exc:
        print(f"graph_search: cannot open database: {exc}", file=sys.stderr)
        return 2

    try:
        if not fts_indexer.table_exists(conn, fts_indexer.FTS_TABLE):
            print(
                f"graph_search: FTS index is missing; build it with "
                f"`python fts_indexer.py --db {args.db!r}`",
                file=sys.stderr,
            )
            return 2

        columns = tuple(
            row["name"]
            for row in conn.execute("PRAGMA table_xinfo(nodes_fts)")
            if row["hidden"] == 0
        )
        if columns == ("name",):
            rows = _search_name_only(conn, args.expression, args.limit)
        elif columns == fts_indexer.EXPECTED_FTS_COLUMNS:
            rows = fts_indexer.search(conn, args.expression, limit=args.limit)
        else:
            print(
                f"graph_search: unsupported nodes_fts columns {columns}; "
                "rebuild with `python fts_indexer.py --rebuild`",
                file=sys.stderr,
            )
            return 2

        for row in rows:
            size = row["size_bytes"] or 0
            print(f"{row['path']} [{row['type']}] ({size} bytes)")
        print(f"{len(rows)} result(s)")
        return 0
    except sqlite3.OperationalError as exc:
        print(f"graph_search: FTS5 query failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
