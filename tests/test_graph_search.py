import importlib.util
import sqlite3
from pathlib import Path

import fts_indexer


REPO_ROOT = Path(__file__).parents[1]
SCRIPT = REPO_ROOT / "core_framework" / "adapters" / "graph_search.py"
SPEC = importlib.util.spec_from_file_location("graph_search", SCRIPT)
graph_search = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(graph_search)


def create_database(path):
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE nodes (
                id TEXT PRIMARY KEY,
                name TEXT,
                type TEXT,
                size_bytes INTEGER,
                mtime TEXT,
                label TEXT
            )
            """
        )
        connection.executemany(
            "INSERT INTO nodes VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("/repo/GRAVEYARD.zip", "GRAVEYARD.zip", "file", 42, "", "File"),
                ("/repo/notes", "notes", "directory", 0, "", "Directory"),
            ],
        )


def test_search_uses_indexer_for_name_path_type_index(tmp_path, capsys):
    database = tmp_path / "graph.db"
    create_database(database)
    fts_indexer.build_index(str(database), log=lambda *_args, **_kwargs: None)

    result = graph_search.main(["search", "GRAVEYARD", "--db", str(database)])

    assert result == 0
    output = capsys.readouterr().out
    assert "/repo/GRAVEYARD.zip [file] (42 bytes)" in output
    assert output.endswith("1 result(s)\n")


def test_search_supports_existing_name_only_index(tmp_path, capsys):
    database = tmp_path / "graph.db"
    create_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE VIRTUAL TABLE nodes_fts USING fts5(
                name,
                content='nodes',
                content_rowid='rowid'
            )
            """
        )
        connection.execute("INSERT INTO nodes_fts(nodes_fts) VALUES ('rebuild')")

    result = graph_search.main(["search", "GRAVEYARD", "--db", str(database)])

    assert result == 0
    assert "/repo/GRAVEYARD.zip [file] (42 bytes)" in capsys.readouterr().out


def test_search_accepts_fts_boolean_expression(tmp_path, capsys):
    database = tmp_path / "graph.db"
    create_database(database)
    fts_indexer.build_index(str(database), log=lambda *_args, **_kwargs: None)

    result = graph_search.main(
        ["search", "GRAVEYARD NOT Kindred", "--db", str(database)]
    )

    assert result == 0
    assert "/repo/GRAVEYARD.zip [file] (42 bytes)" in capsys.readouterr().out


def test_search_reports_missing_index_without_writing(tmp_path, capsys):
    database = tmp_path / "graph.db"
    create_database(database)

    result = graph_search.main(["search", "GRAVEYARD", "--db", str(database)])

    assert result == 2
    assert "FTS index is missing" in capsys.readouterr().err
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='nodes_fts'"
        ).fetchone() is None


def test_search_reports_invalid_match_expression(tmp_path, capsys):
    database = tmp_path / "graph.db"
    create_database(database)
    fts_indexer.build_index(str(database), log=lambda *_args, **_kwargs: None)

    result = graph_search.main(["search", '"unterminated', "--db", str(database)])

    assert result == 1
    assert "FTS5 query failed" in capsys.readouterr().err
