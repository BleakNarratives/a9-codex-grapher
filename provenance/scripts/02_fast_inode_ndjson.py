#!/usr/bin/env python3
import os
import sys
import json
from datetime import datetime

# Key paths
TARGET_PATHS = [
    os.path.expanduser("~"),
    "/storage/emulated/0/RootBase",
    "/storage/emulated/0/ChronoScouts",
    "/storage/ED7B-AD5A/r26",
]

# Paths/Extensions to skip completely to prevent infinite loops or lockups
SKIP_DIRS = {".git", "node_modules", ".cache", "__pycache__", "proc", "sys", "dev"}

OUTPUT_FILE = os.path.expanduser("~/a9_codex_fast.ndjson")

def fast_index():
    count = 0
    sys.stderr.write(f"[+] Starting Fast Indexing -> {OUTPUT_FILE}\n")
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as out:
        for base in TARGET_PATHS:
            if not os.path.exists(base):
                sys.stderr.write(f"[!] Skip missing: {base}\n")
                continue
            
            sys.stderr.write(f"[*] Scanning: {base}...\n")
            
            for root, dirs, files in os.walk(base, topdown=True, followlinks=False):
                # Prune noisy/heavy directories in-place
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".tmp")]
                
                # Write directory entry
                try:
                    st = os.stat(root, follow_symlinks=False)
                    out.write(json.dumps({
                        "path": root,
                        "type": "directory",
                        "size": 0,
                        "mtime": st.st_mtime
                    }) + "\n")
                    count += 1
                except (PermissionError, OSError):
                    pass

                # Write file entries
                for f in files:
                    full_path = os.path.join(root, f)
                    try:
                        st = os.stat(full_path, follow_symlinks=False)
                        out.write(json.dumps({
                            "path": full_path,
                            "type": "file",
                            "size": st.st_size,
                            "mtime": st.st_mtime
                        }) + "\n")
                        count += 1
                        
                        if count % 2000 == 0:
                            sys.stderr.write(f"  -> Indexed {count} nodes...\n")
                    except (PermissionError, OSError):
                        continue

    sys.stderr.write(f"\n[✓] Fast pass complete! Indexed {count} nodes into {OUTPUT_FILE}\n")

if __name__ == "__main__":
    fast_index()
