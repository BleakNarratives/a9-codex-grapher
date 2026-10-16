#!/usr/bin/env python3
import json
import os
import sys

MANIFESTS = {
    "fast_ndjson": os.path.expanduser("~/a9_codex_fast.ndjson"),
    "full_json": os.path.expanduser("~/a9_codex_compendium.json"),
    "cli_json": os.path.expanduser("~/.gemini/tmp/home/compendium_output.json")
}

def analyze_manifest(name, path):
    if not os.path.exists(path):
        return {"status": "NOT_FOUND", "count": 0, "size_mb": 0}

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
                nodes = raw.get("nodes", raw if isinstance(raw, list) else [])
                count = len(nodes)
                for item in nodes:
                    t = item.get("type", "unknown")
                    types[t] = types.get(t, 0) + 1
                    
        return {
            "status": "READY",
            "count": count,
            "size_mb": round(file_size, 2),
            "breakdown": types
        }
    except Exception as e:
        return {"status": f"ERROR: {str(e)}", "count": 0, "size_mb": round(file_size, 2)}

def main():
    print("=" * 60)
    print("      A9 CODEX MANIFEST CROSS-MODEL COMPARISON")
    print("=" * 60)

    for name, path in MANIFESTS.items():
        res = analyze_manifest(name, path)
        print(f"\n")
        print(f"  Path:   {path}")
        print(f"  Status: {res['status']}")
        if res['status'] == "READY":
            print(f"  Nodes:  {res['count']:,}")
            print(f"  Size:   {res['size_mb']} MB")
            print(f"  Types:  {res.get('breakdown', {})}")

    print("\n" + "=" * 60)

if __name__ == "__main__":
    main()
