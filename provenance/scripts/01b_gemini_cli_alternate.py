
import os
import subprocess
import json
import hashlib
import datetime

def get_file_metadata(filepath):
    metadata = {
        "path": filepath,
        "type": "unknown",
        "size": 0,
        "mtime": 0,
        "sha256": None,
        "permissions": None
    }

    try:
        stat_info = os.stat(filepath)
        metadata["size"] = stat_info.st_size
        metadata["mtime"] = stat_info.st_mtime
        metadata["permissions"] = oct(stat_info.st_mode & 0o777) # Get octal permissions

        if os.path.isfile(filepath):
            metadata["type"] = "file"
            # Calculate SHA256 hash for files
            sha256_hash = hashlib.sha256()
            with open(filepath, "rb") as f:
                # Read and update hash in chunks to handle large files efficiently
                for byte_block in iter(lambda: f.read(4096), b""):
                    sha256_hash.update(byte_block)
            metadata["sha256"] = sha256_hash.hexdigest()
        elif os.path.isdir(filepath):
            metadata["type"] = "directory"
        
    except FileNotFoundError:
        print(f"Warning: Path not found - {filepath}", file=sys.stderr)
        return None
    except PermissionError:
        print(f"Warning: Permission denied for - {filepath}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"Error processing {filepath}: {e}", file=sys.stderr)
        return None
    
    return metadata

def generate_compendium(base_paths):
    compendium = []
    
    for base_path in base_paths:
        if not os.path.exists(base_path):
            print(f"Warning: Base path does not exist - {base_path}", file=sys.stderr)
            continue
        
        # Traverse directory or handle specific file paths
        if os.path.isfile(base_path):
            metadata = get_file_metadata(base_path)
            if metadata:
                compendium.append(metadata)
        else: # It's a directory
            for root, dirs, files in os.walk(base_path):
                for name in dirs:
                    full_path = os.path.join(root, name)
                    metadata = get_file_metadata(full_path)
                    if metadata:
                        compendium.append(metadata)
                for name in files:
                    full_path = os.path.join(root, name)
                    metadata = get_file_metadata(full_path)
                    if metadata:
                        compendium.append(metadata)
    
    return compendium

if __name__ == "__main__":
    import sys

    # Define the base paths as per user's request
    # Note: ~ will be expanded by os.path.expanduser
    target_paths = [
        os.path.expanduser("~"),
        "/storage/emulated/0/RootBase",
        "/storage/emulated/0/ChronoScouts",
        "/storage/ED7B-AD5A/r26"
    ]

    # Handle specific file/directory searches for GRAVEYARD.zip and root_2026
    # This will be done by iterating through these base paths and adding specific files/dirs found
    # For now, the os.walk will find them, but if they are explicitly mentioned,
    # we might want to ensure they are captured even if os.walk misses them due to permissions etc.
    # For simplicity, os.walk will cover them if they are within the base_paths.

    print(json.dumps(generate_compendium(target_paths), indent=2))
