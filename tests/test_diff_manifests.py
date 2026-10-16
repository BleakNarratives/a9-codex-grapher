"""Tests for the canonical handoff digest in diff_manifests.py."""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


diff_manifests = _load("diff_manifests")

ENTRIES = [
    {"path": "/b/two.txt", "type": "file", "size": 20},
    {"path": "/a/one.txt", "type": "file", "size": 10},
    {"path": "/a", "type": "directory", "size": 0},
]


def _write_ndjson(path, records):
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return str(path)


def test_digest_is_order_independent(tmp_path):
    forward = _write_ndjson(tmp_path / "forward.ndjson", ENTRIES)
    reverse = _write_ndjson(tmp_path / "reverse.ndjson", list(reversed(ENTRIES)))
    assert diff_manifests.manifest_digest(forward) == \
        diff_manifests.manifest_digest(reverse)


def test_digest_changes_with_size_and_path(tmp_path):
    base = _write_ndjson(tmp_path / "base.ndjson", ENTRIES)
    resized = _write_ndjson(tmp_path / "resized.ndjson",
                            [dict(ENTRIES[0], size=21), ENTRIES[1], ENTRIES[2]])
    renamed = _write_ndjson(tmp_path / "renamed.ndjson",
                            [dict(ENTRIES[0], path="/b/twoo.txt"), ENTRIES[1], ENTRIES[2]])
    base_digest = diff_manifests.manifest_digest(base)
    assert diff_manifests.manifest_digest(resized) != base_digest
    assert diff_manifests.manifest_digest(renamed) != base_digest


def test_digest_normalizes_size_types(tmp_path):
    ints = _write_ndjson(tmp_path / "ints.ndjson", [{"path": "/x", "size": 876}])
    strings = _write_ndjson(tmp_path / "strings.ndjson", [{"path": "/x", "size": "876"}])
    absent = _write_ndjson(tmp_path / "absent.ndjson", [{"path": "/x"}])
    zero = _write_ndjson(tmp_path / "zero.ndjson", [{"path": "/x", "size": 0}])
    assert diff_manifests.manifest_digest(ints) == diff_manifests.manifest_digest(strings)
    assert diff_manifests.manifest_digest(absent) == diff_manifests.manifest_digest(zero)


def test_ndjson_json_and_sqlite_digests_agree(tmp_path):
    ndjson = _write_ndjson(tmp_path / "nodes.ndjson", ENTRIES)

    json_path = tmp_path / "nodes.json"
    json_path.write_text(json.dumps(ENTRIES), encoding="utf-8")

    db_path = tmp_path / "nodes.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, size_bytes INTEGER)")
    for record in ENTRIES:
        conn.execute("INSERT INTO nodes VALUES (?, ?)", (record["path"], record["size"]))
    conn.commit()
    conn.close()

    digests = {
        diff_manifests.manifest_digest(ndjson),
        diff_manifests.manifest_digest(str(json_path)),
        diff_manifests.manifest_digest(str(db_path)),
    }
    assert len(digests) == 1, f"formats must agree, got {digests}"


def test_analyze_manifest_keeps_legacy_keys_and_adds_digest(tmp_path):
    ndjson = _write_ndjson(tmp_path / "nodes.ndjson", ENTRIES)
    res = diff_manifests.analyze_manifest("test", ndjson)
    assert res["status"] == "READY"
    assert res["count"] == 3
    assert res["size_mb"] >= 0
    assert res["breakdown"] == {"file": 2, "directory": 1}
    assert res["digest"] == diff_manifests.manifest_digest(ndjson)


def test_analyze_missing_manifest_reports_no_digest(tmp_path):
    res = diff_manifests.analyze_manifest("missing", str(tmp_path / "nope.ndjson"))
    assert res["status"] == "NOT_FOUND"
    assert res["digest"] is None


def test_main_prints_digest_and_match_verdict(tmp_path, capsys, monkeypatch):
    a = _write_ndjson(tmp_path / "a.ndjson", ENTRIES)
    b = _write_ndjson(tmp_path / "b.ndjson", list(reversed(ENTRIES)))
    c = _write_ndjson(tmp_path / "c.ndjson", [dict(ENTRIES[0], size=999)])
    monkeypatch.setattr(diff_manifests, "MANIFESTS", {"pass_a": a, "pass_b": b, "pass_c": c})

    diff_manifests.main()
    out = capsys.readouterr().out
    assert out.count("Digest: sha256:") == 3
    assert "pass_a vs pass_b: MATCH" in out
    assert "pass_a vs pass_c: MISMATCH" in out
    assert "pass_b vs pass_c: MISMATCH" in out


def test_main_skips_verdict_when_no_ready_manifests(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        diff_manifests, "MANIFESTS",
        {"gone": str(tmp_path / "absent.ndjson")},
    )
    diff_manifests.main()
    out = capsys.readouterr().out
    assert "Handoff digest comparison" not in out
