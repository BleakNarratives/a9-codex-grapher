#!/usr/bin/env python3
"""Build an indexed SQLite graph from the repository's Neo4j-style CSV files."""

import argparse
import csv
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import fts_indexer


REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_NODES_CSV = REPO_ROOT / "provenance" / "graphs" / "codex_nodes.csv"
DEFAULT_EDGES_CSV = REPO_ROOT / "provenance" / "graphs" / "codex_edges.csv"
DEFAULT_DB = REPO_ROOT / "codex_graph.db"
NODE_COLUMNS = ("id:ID", "name", "type", "size_bytes", "mtime", ":LABEL")
EDGE_COLUMNS = (":START_ID", ":END_ID", ":TYPE")
SCHEMA = """
    CREATE TABLE nodes (
        id TEXT PRIMARY KEY,
        name TEXT,
        type TEXT,
        size_bytes INTEGER,
        mtime TEXT,
        label TEXT
    );
    CREATE TABLE edges (
        source_id TEXT,
        target_id TEXT,
        relationship TEXT
    );
"""


def _rows(csv_path, required_columns, description):
    with open(csv_path, newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        missing = set(required_columns) - set(reader.fieldnames or ())
        if missing:
            raise ValueError(
                f"{description} CSV {csv_path} is missing columns: "
                f"{', '.join(sorted(missing))}"
            )
        yield from reader


def _import_nodes(conn, csv_path, batch_size):
    count = 0
    batch = []
    for line_number, row in enumerate(_rows(csv_path, NODE_COLUMNS, "nodes"), 2):
        try:
            batch.append(
                (
                    row["id:ID"],
                    row["name"],
                    row["type"],
                    int(row["size_bytes"] or 0),
                    row["mtime"],
                    row[":LABEL"],
                )
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"invalid size_bytes in nodes CSV {csv_path} at line {line_number}"
            ) from exc
        if not batch[-1][0]:
            raise ValueError(f"empty node id in {csv_path} at line {line_number}")
        if len(batch) >= batch_size:
            conn.executemany(
                "INSERT INTO nodes VALUES (?, ?, ?, ?, ?, ?)", batch
            )
            count += len(batch)
            batch.clear()
    if batch:
        conn.executemany("INSERT INTO nodes VALUES (?, ?, ?, ?, ?, ?)", batch)
        count += len(batch)
    return count


def _import_edges(conn, csv_path, batch_size):
    count = 0
    batch = []
    for line_number, row in enumerate(_rows(csv_path, EDGE_COLUMNS, "edges"), 2):
        values = (row[":START_ID"], row[":END_ID"], row[":TYPE"])
        if not values[0] or not values[1]:
            raise ValueError(f"empty edge endpoint in {csv_path} at line {line_number}")
        batch.append(values)
        if len(batch) >= batch_size:
            conn.executemany("INSERT INTO edges VALUES (?, ?, ?)", batch)
            count += len(batch)
            batch.clear()
    if batch:
        conn.executemany("INSERT INTO edges VALUES (?, ?, ?)", batch)
        count += len(batch)
    return count


def build_database(
    nodes_csv=DEFAULT_NODES_CSV,
    edges_csv=DEFAULT_EDGES_CSV,
    db_path=DEFAULT_DB,
    *,
    replace=False,
    batch_size=10_000,
):
    """Build a complete database beside the target, then atomically publish it."""
    nodes_csv = Path(nodes_csv)
    edges_csv = Path(edges_csv)
    db_path = Path(db_path)
    if batch_size < 1:
        raise ValueError("batch_size must be greater than zero")
    if not nodes_csv.is_file():
        raise FileNotFoundError(f"nodes CSV not found: {nodes_csv}")
    if not edges_csv.is_file():
        raise FileNotFoundError(f"edges CSV not found: {edges_csv}")
    if db_path.exists() and not replace:
        raise FileExistsError(
            f"database already exists: {db_path} (pass --replace to overwrite)"
        )

    db_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{db_path.name}.", suffix=".tmp", dir=db_path.parent
    )
    os.close(fd)
    try:
        conn = sqlite3.connect(temp_name)
        try:
            with conn:
                conn.executescript(SCHEMA)
                node_count = _import_nodes(conn, nodes_csv, batch_size)
                edge_count = _import_edges(conn, edges_csv, batch_size)
                conn.execute("CREATE INDEX idx_nodes_name ON nodes(name)")
                conn.execute("CREATE INDEX idx_nodes_type ON nodes(type)")
            conn.close()
            fts_summary = fts_indexer.build_index(
                temp_name,
                rebuild_flag=True,
                log=lambda *_args, **_kwargs: None,
            )
            os.replace(temp_name, db_path)
            return {
                "database": str(db_path),
                "nodes": node_count,
                "edges": edge_count,
                "indexed": fts_summary["indexed"],
            }
        except Exception:
            conn.close()
            raise
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _positive_int(value):
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=Path, default=DEFAULT_NODES_CSV)
    parser.add_argument("--edges", type=Path, default=DEFAULT_EDGES_CSV)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--replace",
        action="store_true",
        help="replace an existing database after the new database is built",
    )
    parser.add_argument("--batch-size", type=_positive_int, default=10_000)
    args = parser.parse_args(argv)

    try:
        result = build_database(
            args.nodes,
            args.edges,
            args.db,
            replace=args.replace,
            batch_size=args.batch_size,
        )
    except (OSError, ValueError, sqlite3.Error, fts_indexer.IndexerError) as exc:
        print(f"csv_to_sqlite: {exc}", file=sys.stderr)
        return 2

    print(
        f"Built {result['database']}: {result['nodes']:,} nodes, "
        f"{result['edges']:,} edges, {result['indexed']:,} FTS rows"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
