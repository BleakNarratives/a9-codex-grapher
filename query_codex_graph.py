#!/usr/bin/env python3
import csv
import os
import sys

NODES_CSV = os.path.expanduser("~/codex_nodes.csv")

def query_graph(search_term=None, ext=None, limit=20):
    if not os.path.exists(NODES_CSV):
        print("[!] codex_nodes.csv not found.")
        return

    print(f"[+] Querying Graph (Term: '{search_term or 'ANY'}', Ext: '{ext or 'ANY'}')...\n")
    matches = 0

    with open(NODES_CSV, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            path = row["id:ID"]
            
            # Extension filter
            if ext and not path.endswith(ext):
                continue
            
            # Keyword filter
            if search_term and search_term.lower() not in path.lower():
                continue

            size_kb = round(int(row["size_bytes"] or 0) / 1024, 2)
            print(f"[{row[':LABEL']}] {path} ({size_kb} KB)")
            matches += 1

            if matches >= limit:
                print(f"\n[!] Display limit reached ({limit} results shown).")
                break

    print(f"\n[✓] Matching Nodes Found: {matches}")

if __name__ == "__main__":
    term = sys.argv[1] if len(sys.argv) > 1 else None
    extension = sys.argv[2] if len(sys.argv) > 2 else None
    query_graph(term, extension)
