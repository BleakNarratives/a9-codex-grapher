import importlib.util
import sqlite3
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "add_fts5_indexes.py"
SPEC = importlib.util.spec_from_file_location("add_fts5_indexes", SCRIPT)
fts_indexer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fts_indexer)


def create_database(path):
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE nodes (id TEXT PRIMARY KEY, name TEXT, type TEXT)"
        )
        connection.executemany(
            "INSERT INTO nodes(id, name, type) VALUES (?, ?, ?)",
            [
                ("/repo/fast_inode.py", "fast_inode.py", "file"),
                ("/repo/docs", "docs", "directory"),
                ("/repo/README.md", "README.md", "file"),
            ],
        )


def search(connection, term):
    return connection.execute(
        "SELECT name FROM nodes_fts WHERE nodes_fts MATCH ? ORDER BY name",
        (term,),
    ).fetchall()


def test_add_fts_indexes_builds_name_search_index_and_is_repeatable(tmp_path):
    database = tmp_path / "graph.db"
    create_database(database)

    assert fts_indexer.add_fts5_indexes(database) == 3
    assert fts_indexer.add_fts5_indexes(database) == 3

    with sqlite3.connect(database) as connection:
        assert search(connection, "fast_inode") == [("fast_inode.py",)]
        assert search(connection, "README") == [("README.md",)]
        assert search(connection, "docs") == [("docs",)]


def test_fts_triggers_keep_name_index_synchronized(tmp_path):
    database = tmp_path / "graph.db"
    create_database(database)
    fts_indexer.add_fts5_indexes(database)

    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO nodes(id, name, type) VALUES (?, ?, ?)",
            ("/repo/new_module.py", "new_module.py", "file"),
        )
        connection.execute(
            "UPDATE nodes SET name = ? WHERE id = ?",
            ("renamed_module.py", "/repo/new_module.py"),
        )
        connection.execute("DELETE FROM nodes WHERE id = ?", ("/repo/docs",))

        assert search(connection, "renamed_module") == [("renamed_module.py",)]
        assert search(connection, "new_module") == []
        assert search(connection, "docs") == []
