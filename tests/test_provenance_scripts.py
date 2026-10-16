import csv
import importlib.util
import json
import os
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).parents[1] / "provenance" / "scripts"


def load_script(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fast_indexer = load_script("fast_inode_ndjson", "02_fast_inode_ndjson.py")
converter = load_script("ndjson_to_neo4j_csv", "03_ndjson_to_neo4j_csv.py")


def read_ndjson(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_fast_index_writes_directory_and_file_metadata_and_prunes_skipped_dirs(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "scan"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (root / "root.txt").write_text("root", encoding="utf-8")
    (nested / "child.txt").write_text("child", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".tmp-cache").mkdir()
    (root / "node_modules").mkdir()

    output = tmp_path / "manifest.ndjson"
    monkeypatch.setattr(fast_indexer, "TARGET_PATHS", [str(root)])
    monkeypatch.setattr(fast_indexer, "OUTPUT_FILE", str(output))

    fast_indexer.fast_index()

    rows = read_ndjson(output)
    entries = {row["path"]: row for row in rows}
    assert set(entries) == {str(root), str(nested), str(root / "root.txt"), str(nested / "child.txt")}
    assert entries[str(root)]["type"] == "directory"
    assert entries[str(root)]["size"] == 0
    assert entries[str(root / "root.txt")]["type"] == "file"
    assert entries[str(root / "root.txt")]["size"] == 4
    assert all("mtime" in row for row in rows)
    assert "Indexed 4 nodes" in capsys.readouterr().err


def test_fast_index_skips_missing_bases_and_permission_errors(tmp_path, monkeypatch, capsys):
    root = tmp_path / "scan"
    root.mkdir()
    denied_file = root / "denied.txt"
    good_file = root / "good.txt"
    denied_file.write_text("denied", encoding="utf-8")
    good_file.write_text("good", encoding="utf-8")
    output = tmp_path / "manifest.ndjson"
    real_stat = os.stat
    root_stat_calls = 0

    def selective_stat(path, *args, **kwargs):
        nonlocal root_stat_calls
        path = os.fspath(path)
        if path == str(root):
            root_stat_calls += 1
            if root_stat_calls > 1:
                raise PermissionError("denied")
        if path == str(denied_file):
            raise PermissionError("denied")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(fast_indexer, "TARGET_PATHS", [str(tmp_path / "missing"), str(root)])
    monkeypatch.setattr(fast_indexer, "OUTPUT_FILE", str(output))
    monkeypatch.setattr(fast_indexer.os, "stat", selective_stat)

    fast_indexer.fast_index()

    rows = read_ndjson(output)
    assert {row["path"] for row in rows} == {str(good_file)}
    assert "Skip missing" in capsys.readouterr().err


def test_fast_index_reports_progress_at_two_thousand_nodes(tmp_path, monkeypatch, capsys):
    root = tmp_path / "scan"
    root.mkdir()
    for index in range(1999):
        (root / f"file-{index}").touch()
    output = tmp_path / "manifest.ndjson"
    monkeypatch.setattr(fast_indexer, "TARGET_PATHS", [str(root)])
    monkeypatch.setattr(fast_indexer, "OUTPUT_FILE", str(output))

    fast_indexer.fast_index()

    assert len(read_ndjson(output)) == 2000
    assert "Indexed 2000 nodes" in capsys.readouterr().err


def test_converter_writes_nodes_and_only_valid_parent_edges(tmp_path, monkeypatch, capsys):
    root = str(tmp_path / "root")
    child = os.path.join(root, "folder")
    nested_file = os.path.join(child, "report.txt")
    outside_file = str(tmp_path / "outside.txt")
    manifest = tmp_path / "input.ndjson"
    records = [
        {"path": root, "type": "directory"},
        {"path": child, "type": "directory", "size": 1, "mtime": 2.5},
        {"path": nested_file, "type": "file", "size": 2048, "mtime": 3},
        {"path": outside_file, "type": "file"},
    ]
    manifest.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n\n{invalid json\n",
        encoding="utf-8",
    )
    nodes_path = tmp_path / "nodes.csv"
    edges_path = tmp_path / "edges.csv"
    monkeypatch.setattr(converter, "NDJSON_PATH", str(manifest))
    monkeypatch.setattr(converter, "NODES_OUT", str(nodes_path))
    monkeypatch.setattr(converter, "EDGES_OUT", str(edges_path))

    converter.convert_ndjson_to_graph()

    with nodes_path.open(encoding="utf-8", newline="") as nodes_file:
        nodes = list(csv.reader(nodes_file))
    with edges_path.open(encoding="utf-8", newline="") as edges_file:
        edges = list(csv.reader(edges_file))
    assert nodes[0] == ["id:ID", "name", "type", "size_bytes", "mtime", ":LABEL"]
    assert nodes[1:] == [
        [root, "root", "directory", "0", "", "Directory"],
        [child, "folder", "directory", "1", "2.5", "Directory"],
        [nested_file, "report.txt", "file", "2048", "3", "File"],
        [outside_file, "outside.txt", "file", "0", "", "File"],
    ]
    assert edges == [
        [":START_ID", ":END_ID", ":TYPE"],
        [root, child, "CONTAINS"],
        [child, nested_file, "CONTAINS"],
    ]
    assert "Total unique paths in memory: 4" in capsys.readouterr().out


def test_converter_missing_input_reports_and_does_not_create_outputs(
    tmp_path, monkeypatch, capsys
):
    manifest = tmp_path / "missing.ndjson"
    nodes_path = tmp_path / "nodes.csv"
    edges_path = tmp_path / "edges.csv"
    monkeypatch.setattr(converter, "NDJSON_PATH", str(manifest))
    monkeypatch.setattr(converter, "NODES_OUT", str(nodes_path))
    monkeypatch.setattr(converter, "EDGES_OUT", str(edges_path))

    converter.convert_ndjson_to_graph()

    assert "manifest not found" in capsys.readouterr().out
    assert not nodes_path.exists()
    assert not edges_path.exists()


def test_converter_reports_progress_for_fifty_thousand_nodes(tmp_path, monkeypatch, capsys):
    root = str(tmp_path / "root")
    record = json.dumps({"path": root, "type": "directory"})
    manifest = tmp_path / "input.ndjson"
    manifest.write_text((record + "\n") * 50000, encoding="utf-8")
    monkeypatch.setattr(converter, "NDJSON_PATH", str(manifest))
    monkeypatch.setattr(converter, "NODES_OUT", str(tmp_path / "nodes.csv"))
    monkeypatch.setattr(converter, "EDGES_OUT", str(tmp_path / "edges.csv"))

    converter.convert_ndjson_to_graph()

    assert "Exported 50,000 nodes" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("record", "expected_label"),
    [
        ({"path": "/", "type": "directory"}, "Directory"),
        ({"path": "/", "type": "file"}, "File"),
    ],
)
def test_converter_root_path_uses_full_path_as_name_and_emits_no_self_edge(
    tmp_path, monkeypatch, record, expected_label
):
    manifest = tmp_path / "input.ndjson"
    manifest.write_text(json.dumps(record) + "\n", encoding="utf-8")
    monkeypatch.setattr(converter, "NDJSON_PATH", str(manifest))
    monkeypatch.setattr(converter, "NODES_OUT", str(tmp_path / "nodes.csv"))
    monkeypatch.setattr(converter, "EDGES_OUT", str(tmp_path / "edges.csv"))

    converter.convert_ndjson_to_graph()

    with (tmp_path / "nodes.csv").open(encoding="utf-8", newline="") as source:
        rows = list(csv.reader(source))
    with (tmp_path / "edges.csv").open(encoding="utf-8", newline="") as source:
        edges = list(csv.reader(source))
    assert rows[1][0:3] == ["/", "/", record["type"]]
    assert rows[1][-1] == expected_label
    assert edges == [[":START_ID", ":END_ID", ":TYPE"]]
