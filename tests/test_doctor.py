import sqlite3
import subprocess

import doctor
import fts_indexer


def make_database(path, *, include_fts=True):
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE nodes (
                id TEXT PRIMARY KEY, name TEXT, type TEXT,
                size_bytes INTEGER, mtime TEXT, label TEXT
            )
            """
        )
        conn.execute(
            "CREATE TABLE edges (source_id TEXT, target_id TEXT, relationship TEXT)"
        )
        conn.execute(
            "INSERT INTO nodes VALUES ('/repo/a.txt', 'a.txt', 'file', 3, '', 'File')"
        )
        if include_fts:
            conn.execute(
                """
                CREATE VIRTUAL TABLE nodes_fts USING fts5(
                    name, path, type UNINDEXED,
                    tokenize='unicode61 remove_diacritics 2'
                )
                """
            )
            conn.execute(
                "INSERT INTO nodes_fts(rowid, name, path, type) "
                "SELECT rowid, name, id, label FROM nodes"
            )


def make_git_repo(path):
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "checkout", "-qb", "doctor-test"], check=True
    )


def test_diagnose_reports_database_counts_fts_and_git(tmp_path):
    database = tmp_path / "graph.db"
    repo = tmp_path / "repo"
    make_database(database)
    make_git_repo(repo)

    report = doctor.diagnose(database, repo)

    assert report["ok"] is True
    assert report["counts"] == {"nodes": 1, "edges": 0, "fts": 1}
    assert [(check["name"], check["status"]) for check in report["checks"]] == [
        ("database", "OK"),
        ("nodes", "OK"),
        ("edges", "OK"),
        ("fts5", "OK"),
        ("git", "OK"),
    ]
    assert "branch doctor-test; working tree clean" in report["checks"][-1]["detail"]


def test_diagnose_marks_missing_database_unhealthy(tmp_path):
    repo = tmp_path / "repo"
    make_git_repo(repo)

    report = doctor.diagnose(tmp_path / "absent.db", repo)

    assert report["ok"] is False
    assert report["checks"][0]["name"] == "database"
    assert "not found" in report["checks"][0]["detail"]


def test_diagnose_reports_missing_fts_table(tmp_path):
    database = tmp_path / "graph.db"
    repo = tmp_path / "repo"
    make_database(database, include_fts=False)
    make_git_repo(repo)

    report = doctor.diagnose(database, repo)

    fts_check = next(check for check in report["checks"] if check["name"] == "fts5")
    assert report["ok"] is False
    assert fts_check["status"] == "FAIL"
    assert "missing" in fts_check["detail"]


def test_diagnose_detects_stale_fts_row_count(tmp_path):
    database = tmp_path / "graph.db"
    repo = tmp_path / "repo"
    make_database(database)
    with sqlite3.connect(database) as conn:
        conn.execute("DELETE FROM nodes_fts")
    make_git_repo(repo)

    report = doctor.diagnose(database, repo)

    fts_check = next(check for check in report["checks"] if check["name"] == "fts5")
    assert report["ok"] is False
    assert fts_check["status"] == "FAIL"
    assert "0/1 rows" in fts_check["detail"]


def test_diagnose_warns_when_git_worktree_is_dirty(tmp_path):
    database = tmp_path / "graph.db"
    repo = tmp_path / "repo"
    make_database(database)
    make_git_repo(repo)
    (repo / "untracked.txt").write_text("pending", encoding="utf-8")

    report = doctor.diagnose(database, repo)

    assert report["ok"] is True
    git_check = next(check for check in report["checks"] if check["name"] == "git")
    assert git_check["status"] == "OK"
    assert "working tree dirty" in git_check["detail"]


def test_main_prints_failure_and_returns_nonzero(tmp_path, capsys):
    repo = tmp_path / "repo"
    make_git_repo(repo)

    result = doctor.main(["--db", str(tmp_path / "absent.db"), "--repo", str(repo)])

    assert result == 1
    output = capsys.readouterr().out
    assert "[FAIL] database:" in output
    assert "Health check failed." in output


def test_doctor_does_not_modify_database(tmp_path):
    database = tmp_path / "graph.db"
    repo = tmp_path / "repo"
    make_database(database)
    make_git_repo(repo)
    with sqlite3.connect(database) as conn:
        before = conn.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()

    doctor.diagnose(database, repo)

    with sqlite3.connect(database) as conn:
        after = conn.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
    assert after == before
