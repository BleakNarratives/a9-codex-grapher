#!/usr/bin/env python3
"""Cross-model manifest comparison plus the canonical handoff digest.

The handoff digest (ROADMAP.md section 4) is SHA256 over the sorted
``path\\0size_bytes`` lines of a manifest. Both sender and receiver compute it
over the artifact itself, so a transfer can be verified without re-reading
file contents or re-walking the tree.

Supported manifest formats: NDJSON node streams, JSON arrays (or
``{"nodes": [...]}`` objects), and ``codex_graph.db`` SQLite databases.
"""
import hashlib
import json
import os
import sqlite3
import sys

MANIFESTS = {
    "fast_ndjson": os.path.expanduser("~/a9_codex_fast.ndjson"),
    "full_json": os.path.expanduser("~/a9_codex_compendium.json"),
    "cli_json": os.path.expanduser("~/.gemini/tmp/home/compendium_output.json")
}

_SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3")


def _norm_size(value):
    """Canonical size text so int 876 and string "876" digest identically."""
    if value is None:
        return "0"
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, (int, float)):
        return str(int(value))
    return str(value)


def _is_sqlite(path):
    if path.endswith(_SQLITE_SUFFIXES):
        return True
    try:
        with open(path, "rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def iter_manifest_entries(path):
    """Yield ``(path, size)`` for every node in an NDJSON/JSON/SQLite manifest."""
    if _is_sqlite(path):
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            for row in conn.execute("SELECT id, size_bytes FROM nodes"):
                yield row[0] or "", row[1]
        finally:
            conn.close()
        return

    if path.endswith(".ndjson"):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    data = json.loads(line)
                    yield data.get("path") or "", data.get("size")
        return

    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    nodes = raw.get("nodes", []) if isinstance(raw, dict) else raw
    for item in nodes:
        yield item.get("path") or "", item.get("size")


def manifest_digest(path):
    """SHA256 over sorted ``path\\0size_bytes`` lines — the handoff digest.

    Ordering is normalized so two devices that scanned the same tree in
    different orders produce the same digest. The digest never raises on a
    malformed entry size: sizes are canonicalized by ``_norm_size``.
    """
    hasher = hashlib.sha256()
    for entry_path, size in sorted(iter_manifest_entries(path), key=lambda e: e[0]):
        hasher.update(f"{entry_path}\x00{_norm_size(size)}\n".encode("utf-8"))
    return hasher.hexdigest()


def analyze_manifest(name, path):
    if not os.path.exists(path):
        return {"status": "NOT_FOUND", "count": 0, "size_mb": 0, "digest": None}

    file_size = os.path.getsize(path) / (1024 * 1024)
    count = 0
    types = {}

    try:
        if path.endswith(".ndjson"):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        count += 1
                        data = json.loads(line)
                        t = data.get("type", "unknown")
                        types[t] = types.get(t, 0) + 1
        else:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
                # a bare JSON list (e.g. generate_compendium output) is a valid manifest
                nodes = raw.get("nodes", []) if isinstance(raw, dict) else raw
                count = len(nodes)
                for item in nodes:
                    t = item.get("type", "unknown")
                    types[t] = types.get(t, 0) + 1

        return {
            "status": "READY",
            "count": count,
            "size_mb": round(file_size, 2),
            "breakdown": types,
            "digest": manifest_digest(path)
        }
    except Exception as e:
        return {"status": f"ERROR: {str(e)}", "count": 0,
                "size_mb": round(file_size, 2), "digest": None}


def main():
    print("=" * 60)
    print("      A9 CODEX MANIFEST CROSS-MODEL COMPARISON")
    print("=" * 60)

    ready = []
    for name, path in MANIFESTS.items():
        res = analyze_manifest(name, path)
        print(f"\n")
        print(f"  Path:   {path}")
        print(f"  Status: {res['status']}")
        if res['status'] == "READY":
            print(f"  Nodes:  {res['count']:,}")
            print(f"  Size:   {res['size_mb']} MB")
            print(f"  Types:  {res.get('breakdown', {})}")
            print(f"  Digest: sha256:{res['digest']}")
            ready.append((name, res["digest"]))

    # Handoff verdict: equal digests mean both sides saw identical path+size
    # content, regardless of scan order or manifest format.
    if len(ready) >= 2:
        print("\n  Handoff digest comparison:")
        for i in range(len(ready)):
            for j in range(i + 1, len(ready)):
                (na, da), (nb, db) = ready[i], ready[j]
                verdict = "MATCH" if da == db else "MISMATCH"
                print(f"    {na} vs {nb}: {verdict}")

    print("\n" + "=" * 60)


if __name__ == "__main__":
    main()
