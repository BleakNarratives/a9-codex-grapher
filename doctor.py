#!/usr/bin/env python3
"""Quick read-only health check for the local graph database and Git checkout."""

import argparse
import sqlite3
import subprocess
import sys
from pathlib import Path

import fts_indexer


REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_DB = REPO_ROOT / "codex_graph.db"


def _check(name, ok, detail, *, warning=False):
    return {
        "name": name,
        "status": "WARN" if warning else ("OK" if ok else "FAIL"),
        "ok": ok,
        "detail": detail,
    }


def diagnose(db_path=DEFAULT_DB, repo_path=REPO_ROOT):
    """Return diagnostic checks without modifying the database or Git checkout."""
    db_path = Path(db_path)
    checks = []
    counts = {"nodes": None, "edges": None, "fts": None}

    if not db_path.is_file():
        checks.append(_check("database", False, f"not found: {db_path}"))
    else:
        conn = None
        try:
            conn = fts_indexer.connect(str(db_path), readonly=True)
            if not fts_indexer.table_exists(conn, "nodes"):
                checks.append(
                    _check("database", False, "opened successfully but nodes table is missing")
                )
            elif not fts_indexer.table_exists(conn, "edges"):
                checks.append(
                    _check("database", False, "opened successfully but edges table is missing")
                )
            else:
                counts["nodes"] = conn.execute(
                    "SELECT COUNT(*) FROM nodes"
                ).fetchone()[0]
                counts["edges"] = conn.execute(
                    "SELECT COUNT(*) FROM edges"
                ).fetchone()[0]
                checks.append(_check("database", True, f"readable: {db_path}"))
                checks.append(
                    _check("nodes", True, f"{counts['nodes']:,} row(s)")
                )
                checks.append(
                    _check("edges", True, f"{counts['edges']:,} row(s)")
                )

                if not fts_indexer.fts5_available(conn):
                    checks.append(_check("fts5", False, "SQLite FTS5 support is unavailable"))
                elif not fts_indexer.table_exists(conn, fts_indexer.FTS_TABLE):
                    checks.append(_check("fts5", False, "nodes_fts table is missing"))
                else:
                    columns = fts_indexer.fts_columns(conn)
                    supported_schemas = (
                        fts_indexer.EXPECTED_FTS_COLUMNS,
                        ("name",),
                    )
                    if columns not in supported_schemas:
                        checks.append(
                            _check("fts5", False, f"unsupported nodes_fts columns: {columns}")
                        )
                    else:
                        counts["fts"] = fts_indexer.count_fts(conn)
                        # Exercise MATCH parsing without requiring any particular term.
                        conn.execute(
                            f"SELECT rowid FROM {fts_indexer.FTS_TABLE} "
                            f"WHERE {fts_indexer.FTS_TABLE} MATCH ? LIMIT 0",
                            ("doctorprobe",),
                        ).fetchall()
                        counts_match = counts["fts"] == counts["nodes"]
                        checks.append(
                            _check(
                                "fts5",
                                counts_match,
                                f"queryable schema {columns}; "
                                f"{counts['fts']:,}/{counts['nodes']:,} rows",
                            )
                        )
        except (OSError, sqlite3.Error, fts_indexer.IndexerError) as exc:
            checks.append(_check("database", False, f"{type(exc).__name__}: {exc}"))
        finally:
            if conn is not None:
                conn.close()

    try:
        branch = subprocess.run(
            ["git", "-C", str(repo_path), "branch", "--show-current"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(repo_path), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        state = "clean" if not status else "dirty"
        checks.append(
            _check("git", True, f"branch {branch or '(detached HEAD)'}; working tree {state}")
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        checks.append(_check("git", False, f"unable to inspect repository: {exc}", warning=True))

    return {
        "ok": all(check["ok"] for check in checks if check["status"] != "WARN"),
        "counts": counts,
        "checks": checks,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite graph database")
    parser.add_argument(
        "--repo",
        type=Path,
        default=REPO_ROOT,
        help="Git worktree to inspect (default: this repository)",
    )
    args = parser.parse_args(argv)

    report = diagnose(args.db, args.repo)
    for check in report["checks"]:
        print(f"[{check['status']}] {check['name']}: {check['detail']}")
    print("Health check passed." if report["ok"] else "Health check failed.")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
