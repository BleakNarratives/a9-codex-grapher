import os
import json
import subprocess
import sys
import datetime # For human-readable mtime

def get_file_metadata(filepath):
    metadata = {
        "path": filepath,
        "type": "N/A",
        "size": "N/A",
        "mtime": "N/A",
        "permissions": "N/A",
        "sha256": "N/A",
        "error": None
    }

    try:
        # Check if it's a file or directory
        if os.path.isfile(filepath):
            metadata["type"] = "file"
            metadata["size"] = os.path.getsize(filepath)
            metadata["mtime"] = datetime.datetime.fromtimestamp(os.path.getmtime(filepath)).isoformat() # ISO format
            metadata["permissions"] = oct(os.stat(filepath).st_mode)[-3:]

            # Get SHA256 hash - handle large files carefully
            hasher_result = subprocess.run(['sha256sum', filepath], capture_output=True, text=True, check=False)
            if hasher_result.returncode == 0:
                metadata["sha256"] = hasher_result.stdout.split(' ')[0]
            else:
                metadata["error"] = f"SHA256 error: {hasher_result.stderr.strip()}"

        elif os.path.isdir(filepath):
            metadata["type"] = "directory"
            metadata["mtime"] = datetime.datetime.fromtimestamp(os.path.getmtime(filepath)).isoformat()
            metadata["permissions"] = oct(os.stat(filepath).st_mode)[-3:]

        else:
            metadata["error"] = "Path exists but is neither file nor directory (e.g., symlink, special file)"

    except FileNotFoundError:
        metadata["error"] = "File or directory not found"
    except PermissionError:
        metadata["error"] = "Permission denied"
    except Exception as e:
        metadata["error"] = f"An unexpected error occurred: {str(e)}"

    return metadata

def main():
    target_paths = [
        "/data/data/com.termux/files/home",
        "/storage/emulated/0/RootBase",
        "/storage/emulated/0/ChronoScouts",
        "/storage/ED7B-AD5A/r26",
    ]

    specific_name_patterns = {
        "GRAVEYARD.zip": "/storage/emulated/0/RootBase",
        "root_2026": "/storage/ED7B-AD5A/r26",
    }

    compendium_data = []
    processed_paths = set() # To avoid duplicate entries for patterns found by os.walk

    for base_path in target_paths:
        for root, dirs, files in os.walk(base_path):
            for name in files:
                filepath = os.path.join(root, name)
                if filepath not in processed_paths:
                    compendium_data.append(get_file_metadata(filepath))
                    processed_paths.add(filepath)
            for name in dirs:
                dirpath = os.path.join(root, name)
                if dirpath not in processed_paths:
                    compendium_data.append(get_file_metadata(dirpath))
                    processed_paths.add(dirpath)
        
    # Handle specific name patterns by searching within their respective base directories
    for pattern, base_dir in specific_name_patterns.items():
        try:
            # Using find command as os.walk might miss certain patterns or be less efficient for specific names
            # It will also find files/dirs matching the pattern *anywhere* under base_dir
            find_cmd = ['find', base_dir, '-name', pattern]
            find_process = subprocess.run(find_cmd, capture_output=True, text=True, check=True)
            for found_path in find_process.stdout.splitlines():
                found_path = found_path.strip()
                if found_path and found_path not in processed_paths:
                    compendium_data.append(get_file_metadata(found_path))
                    processed_paths.add(found_path)
        except subprocess.CalledProcessError as e:
            sys.stderr.write(f"Error searching for pattern {pattern} in {base_dir}: {e.stderr}\n")
        except Exception as e:
            sys.stderr.write(f"An unexpected error occurred during pattern search ({pattern} in {base_dir}): {str(e)}\n")

    output_file_path = os.path.join("/data/data/com.termux/files/home/.gemini/tmp/home", "compendium_output.json")
    with open(output_file_path, "w", encoding="utf-8") as f:
        json.dump(compendium_data, f, indent=2)
    sys.stdout.write(f"Compendium generated successfully at: {output_file_path}\n")

if __name__ == "__main__":
    main()
