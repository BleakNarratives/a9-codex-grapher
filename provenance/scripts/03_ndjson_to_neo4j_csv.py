#!/usr/bin/env python3
import json
import csv
import os

NDJSON_PATH = os.path.expanduser("~/a9_codex_fast.ndjson")
NODES_OUT = os.path.expanduser("~/codex_nodes.csv")
EDGES_OUT = os.path.expanduser("~/codex_edges.csv")

def convert_ndjson_to_graph():
    if not os.path.exists(NDJSON_PATH):
        print(f"[!] NDJSON manifest not found at {NDJSON_PATH}")
        return

    print(f"[+] Streaming NDJSON from {NDJSON_PATH}...")
    
    # First pass: Collect all paths to validate edge targets
    path_set = set()
    with open(NDJSON_PATH, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    data = json.loads(line)
                    path_set.add(data["path"])
                except json.JSONDecodeError:
                    continue

    print(f"[+] Total unique paths in memory: {len(path_set):,}")

    # Second pass: Stream out Nodes and Edges directly to CSV
    node_count = 0
    edge_count = 0

    with open(NDJSON_PATH, "r", encoding="utf-8") as f_in, \
         open(NODES_OUT, "w", newline="", encoding="utf-8") as f_nodes, \
         open(EDGES_OUT, "w", newline="", encoding="utf-8") as f_edges:

        writer_nodes = csv.writer(f_nodes)
        writer_edges = csv.writer(f_edges)

        writer_nodes.writerow(["id:ID", "name", "type", "size_bytes", "mtime", ":LABEL"])
        writer_edges.writerow([":START_ID", ":END_ID", ":TYPE"])

        for line in f_in:
            if not line.strip():
                continue
            
            try:
                n = json.loads(line)
                p = n["path"]
                name = os.path.basename(p) or p
                label = "Directory" if n["type"] == "directory" else "File"

                writer_nodes.writerow([p, name, n["type"], n.get("size", 0), n.get("mtime", ""), label])
                node_count += 1

                # Generate CONTAINS edge if parent folder exists in target set
                parent = os.path.dirname(p)
                if parent in path_set and parent != p:
                    writer_edges.writerow([parent, p, "CONTAINS"])
                    edge_count += 1

                if node_count % 50000 == 0:
                    print(f"  -> Exported {node_count:,} nodes...")

            except json.JSONDecodeError:
                continue

    print(f"\n[✓] Graph Export Complete!")
    print(f"  - Nodes CSV: {NODES_OUT} ({node_count:,} records)")
    print(f"  - Edges CSV: {EDGES_OUT} ({edge_count:,} relationships)")

if __name__ == "__main__":
    convert_ndjson_to_graph()
