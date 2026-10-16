#!/usr/bin/env python3
"""Add a rebuildable FTS5 name index to codex_graph.db."""

import argparse
import sqlite3
from pathlib import Path


DEFAULT_DB_PATH = Path(__file__).with_name("codex_graph.db")


def add_fts5_indexes(db_path=DEFAULT_DB_PATH):
    """Create and populate the nodes_fts index, including synchronization triggers."""
    connection = sqlite3.connect(db_path)
    try:
        with connection:
            connection.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
                    name,
                    content='nodes',
                    content_rowid='rowid',
                    tokenize='unicode61'
                )
                """
            )
            connection.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS nodes_fts_insert
                AFTER INSERT ON nodes BEGIN
                    INSERT INTO nodes_fts(rowid, name)
                    VALUES (new.rowid, COALESCE(new.name, ''));
                END;

                CREATE TRIGGER IF NOT EXISTS nodes_fts_delete
                AFTER DELETE ON nodes BEGIN
                    INSERT INTO nodes_fts(nodes_fts, rowid, name)
                    VALUES ('delete', old.rowid, COALESCE(old.name, ''));
                END;

                CREATE TRIGGER IF NOT EXISTS nodes_fts_update
                AFTER UPDATE OF name ON nodes BEGIN
                    INSERT INTO nodes_fts(nodes_fts, rowid, name)
                    VALUES ('delete', old.rowid, COALESCE(old.name, ''));
                    INSERT INTO nodes_fts(rowid, name)
                    VALUES (new.rowid, COALESCE(new.name, ''));
                END;
                """
            )
            connection.execute("INSERT INTO nodes_fts(nodes_fts) VALUES ('rebuild')")
            return connection.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "database",
        nargs="?",
        default=str(DEFAULT_DB_PATH),
        help=f"SQLite database to index (default: {DEFAULT_DB_PATH})",
    )
    args = parser.parse_args()
    count = add_fts5_indexes(args.database)
    print(f"Indexed names for {count:,} nodes in {args.database}")


if __name__ == "__main__":
    main()
