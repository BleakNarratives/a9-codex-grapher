import csv
import sqlite3

import csv_to_sqlite
import fts_indexer


def write_csv(path, headers, rows):
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(headers)
        writer.writerows(rows)


def make_csvs(tmp_path):
    nodes = tmp_path / "nodes.csv"
    edges = tmp_path / "edges.csv"
    write_csv(
        nodes,
        csv_to_sqlite.NODE_COLUMNS,
        [
            ("/repo", "repo", "directory", 0, "1.0", "Directory"),
            ("/repo/README.md", "README.md", "file", 24, "2.0", "File"),
        ],
    )
    write_csv(
        edges,
        csv_to_sqlite.EDGE_COLUMNS,
        [("/repo", "/repo/README.md", "CONTAINS")],
    )
    return nodes, edges


def test_build_database_imports_graph_and_search_index(tmp_path):
    nodes, edges = make_csvs(tmp_path)
    database = tmp_path / "codex_graph.db"

    summary = csv_to_sqlite.build_database(
        nodes, edges, database, batch_size=1
    )

    assert summary == {
        "database": str(database),
        "nodes": 2,
        "edges": 1,
        "indexed": 2,
    }
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 1
        assert connection.execute(
            "SELECT name FROM nodes_fts WHERE nodes_fts MATCH 'README'"
        ).fetchone() == ("README.md",)
        assert fts_indexer.count_fts(connection) == 2


def test_build_refuses_to_replace_existing_database_by_default(tmp_path):
    nodes, edges = make_csvs(tmp_path)
    database = tmp_path / "codex_graph.db"
    database.write_bytes(b"keep existing artifact")

    try:
        csv_to_sqlite.build_database(nodes, edges, database)
    except FileExistsError as exc:
        assert "--replace" in str(exc)
    else:
        raise AssertionError("existing database should not be overwritten")
    assert database.read_bytes() == b"keep existing artifact"


def test_build_replace_publishes_complete_new_database(tmp_path):
    nodes, edges = make_csvs(tmp_path)
    database = tmp_path / "codex_graph.db"
    database.write_bytes(b"old artifact")

    csv_to_sqlite.build_database(nodes, edges, database, replace=True)

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM nodes_fts").fetchone()[0] == 2
    assert not list(tmp_path.glob(".codex_graph.db.*.tmp"))


def test_build_failure_preserves_existing_db_and_cleans_temporary_file(tmp_path):
    nodes, edges = make_csvs(tmp_path)
    with nodes.open("a", encoding="utf-8") as output:
        output.write('/repo,duplicate,directory,0,3,Directory\n')
    database = tmp_path / "codex_graph.db"
    database.write_bytes(b"old artifact")

    try:
        csv_to_sqlite.build_database(nodes, edges, database, replace=True)
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("duplicate IDs should fail the database build")

    assert database.read_bytes() == b"old artifact"
    assert not list(tmp_path.glob(".codex_graph.db.*.tmp"))


def test_build_rejects_bad_headers(tmp_path):
    nodes = tmp_path / "nodes.csv"
    edges = tmp_path / "edges.csv"
    write_csv(nodes, ["path", "name"], [])
    write_csv(edges, csv_to_sqlite.EDGE_COLUMNS, [])

    try:
        csv_to_sqlite.build_database(nodes, edges, tmp_path / "graph.db")
    except ValueError as exc:
        assert "missing columns" in str(exc)
    else:
        raise AssertionError("invalid CSV headers should be rejected")
