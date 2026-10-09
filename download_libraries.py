"""Download Minecraft libraries for a specific version."""

import argparse
import sys
import json
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
import os

# Enable UTF-8 output on Windows
if sys.platform == 'win32':
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)

from utils import (
    get_dir,
    ensure_download_dir,
    get_available_versions,
    get_version_json,
    verify_sha1,
    download_file,
    set_download_dir,
    get_download_dir
)
import requests
import requests.adapters

import os
MAX_WORKERS = (os.cpu_count() or 4) * 4  # Dynamically scale parallel downloads based on CPU cores

# Shared session for connection pooling
session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=MAX_WORKERS, pool_maxsize=MAX_WORKERS)
session.mount('https://', adapter)
session.mount('http://', adapter)

# Global settings
dev_mode = False
download_dir_name = "minecraft_downloads"
json_output = False

# Thread-safe progress tracking
progress_lock = Lock()
progress_data = {
    'completed_size': 0,
    'total_size': 0,
    'completed': 0,
    'failed': 0,
    'start_time': 0,
    'current_lib': ''
}

def format_size(bytes_size):
    """Format bytes to human-readable size."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_size < 1024:
            return f"{bytes_size:.1f}{unit}"
        bytes_size /= 1024
    return f"{bytes_size:.1f}TB"

def format_time(seconds):
    """Format seconds to human-readable time."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        return f"{seconds/60:.1f}m"
    else:
        return f"{seconds/3600:.1f}h"

def download_library_worker(lib_name, lib_url, lib_size, lib_hash, lib_path):
    """Download a single library file."""
    # Create parent directories
    lib_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Check if already downloaded and valid
    if lib_path.exists() and lib_hash:
        if verify_sha1(lib_path, lib_hash):
            with progress_lock:
                progress_data['completed'] += 1
                progress_data['completed_size'] += lib_size
                progress_data['current_lib'] = lib_name
            return True, lib_name, lib_size
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = session.get(lib_url, timeout=30, stream=True)
            response.raise_for_status()

            with open(lib_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=16384):
                    if chunk:
                        f.write(chunk)

            # Verify SHA-1 if provided
            if lib_hash and not verify_sha1(lib_path, lib_hash):
                lib_path.unlink()
                if attempt < max_retries - 1:
                    import time
                    time.sleep(1)
                    continue
                return False, lib_name, lib_size

            with progress_lock:
                progress_data['completed'] += 1
                progress_data['completed_size'] += lib_size
                progress_data['current_lib'] = lib_name

            return True, lib_name, lib_size

        except Exception as e:
            if attempt < max_retries - 1:
                import time
                time.sleep(1)
                continue
            return False, lib_name, lib_size
        
def update_progress_display(total_libraries):
    """Display current progress with ETA."""
    with progress_lock:
        completed = progress_data['completed']
        completed_size = progress_data['completed_size']
        total_size = progress_data['total_size']
        start_time = progress_data['start_time']
        current_lib = progress_data['current_lib']
    
    elapsed = time.time() - start_time
    percent = (completed_size / total_size) * 100 if total_size > 0 else 0
    
    # Calculate speed based on completed size
    if elapsed > 1:
        speed = completed_size / elapsed
    else:
        speed = 0
    
    # Calculate ETA based on remaining files and average size per file
    remaining_files = total_libraries - completed
    if completed > 0 and speed > 0:
        avg_size_per_file = completed_size / completed
        remaining_size = remaining_files * avg_size_per_file
        eta_seconds = remaining_size / speed
        eta_str = format_time(eta_seconds)
    else:
        eta_str = "calculating..."
    
    # Display progress line with fixed width format
    speed_str = format_size(speed) + "/s"
    
    # Limit library name to fit in terminal (max 50 chars)
    lib_display = current_lib[:50] if current_lib else "libraries"
    
    # Create the full status line
    status_line = f"[{completed}/{total_libraries}] {percent:.1f}% | Speed: {speed_str:>9s} | ETA: {eta_str:>6s} | {lib_display}"
    
    global json_output
    if globals().get('json_output', False):
        print(json.dumps({"type": "progress", "progress": percent, "status": status_line}))
    else:
        # Write to stdout with carriage return to overwrite the line
        sys.stdout.write(f"\r{status_line:<120}")
        sys.stdout.flush()

def list_versions():
    """Display all available Minecraft versions."""
    print("Fetching available versions...")
    versions = get_available_versions()
    
    if not versions:
        print("Error: Could not fetch version list")
        return
    
    print(f"\nAvailable Minecraft versions ({len(versions)} total):")
    print("-" * 70)
    print(f"{'Version':<20} {'Type':<15} {'Release Date':<30}")
    print("-" * 70)
    
    for version_id, version_type in versions:
        type_label = version_type.replace("old_", "").upper()
        print(f"{version_id:<20} {type_label:<15}")
    
    print("-" * 70)

def download_libraries(version_id):
    """Download Minecraft libraries for a specific version using parallel downloads."""
    global dev_mode, download_dir_name
    
    ensure_download_dir()
    
    print(f"Downloading Minecraft {version_id} libraries...")
    
    # Get version JSON which contains library information
    version_json = get_version_json(version_id)
    if not version_json:
        print(f"Error: Could not fetch metadata for version {version_id}")
        return False
    
    # Check if libraries section exists
    if 'libraries' not in version_json:
        print(f"Error: No libraries found for version {version_id}")
        return False
    
    libraries = version_json['libraries']
    print(f"Found {len(libraries)} libraries to download")
    
    # Prepare libraries directory
    libraries_dir = get_dir("libraries")
    libraries_dir.mkdir(parents=True, exist_ok=True)
    
    # Collect all libraries to download
    libs_to_download = []
    total_size = 0
    
    print("Verifying existing libraries (this may take a moment)...")
    last_verify_percent = -1
    for idx, library in enumerate(libraries):
        if globals().get('json_output', False):
            percent = (idx / len(libraries)) * 100
            if percent - last_verify_percent >= 2.0 or idx == len(libraries) - 1:
                print(json.dumps({"type": "progress", "progress": percent, "status": f"Verifying Libraries: {percent:.1f}%"}))
                sys.stdout.flush()
                last_verify_percent = percent

        if 'downloads' in library:
            downloads = library['downloads']
            
            # Handle regular artifacts
            if 'artifact' in downloads:
                artifact = downloads['artifact']
                lib_size = artifact.get('size', 0)
                lib_hash = artifact.get('sha1', '')
                lib_path = libraries_dir / artifact['path']
                lib_url = artifact['url']
                lib_name = artifact['path'].split('/')[-1]
                
                # Only add if not already downloaded
                if not (lib_path.exists() and verify_sha1(lib_path, lib_hash)):
                    libs_to_download.append((lib_name, lib_url, lib_size, lib_hash, lib_path))
                    total_size += lib_size
                else:
                    # Mark as already completed
                    with progress_lock:
                        progress_data['completed'] += 1
                        progress_data['completed_size'] += lib_size
            
            # Handle classifiers (natives, etc.)
            if 'classifiers' in downloads:
                classifiers = downloads['classifiers']
                for classifier_key, classifier in classifiers.items():
                    lib_size = classifier.get('size', 0)
                    lib_hash = classifier.get('sha1', '')
                    lib_path = libraries_dir / classifier['path']
                    lib_url = classifier['url']
                    lib_name = classifier['path'].split('/')[-1]
                    
                    if not (lib_path.exists() and verify_sha1(lib_path, lib_hash)):
                        libs_to_download.append((lib_name, lib_url, lib_size, lib_hash, lib_path))
                        total_size += lib_size
                    else:
                        with progress_lock:
                            progress_data['completed'] += 1
                            progress_data['completed_size'] += lib_size
    
    if not libs_to_download:
        print(f"[OK] All {len(libraries)} libraries already downloaded")
        return True
    
    total_libraries = len(libraries)
    mode_str = " (dev-mode)" if dev_mode else ""
    print(f"Downloading {len(libs_to_download)} new libraries ({format_size(total_size)} total){mode_str}")
    print(f"Starting parallel download with {MAX_WORKERS} concurrent connections...\n")
    
    # Initialize progress data
    with progress_lock:
        progress_data['total_size'] = total_size
        progress_data['start_time'] = time.time()
        progress_data['current_lib'] = ''
    
    failed_libraries = []
    
    # Use ThreadPoolExecutor for parallel downloads
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {}
        
        for lib_name, lib_url, lib_size, lib_hash, lib_path in libs_to_download:
            future = executor.submit(download_library_worker, lib_name, lib_url, lib_size, lib_hash, lib_path)
            futures[future] = lib_name
        
        # Process completed downloads
        for future in as_completed(futures):
            success, lib_name, lib_size = future.result()
            
            if not success:
                with progress_lock:
                    progress_data['failed'] += 1
                failed_libraries.append(lib_name)
            
            update_progress_display(total_libraries)
    
    # Final summary
    print()  # New line after progress
    
    with progress_lock:
        downloaded = progress_data['completed'] - len(libraries) + len(libs_to_download)
        failed = progress_data['failed']
    
    print(f"\n{'='*70}")
    print(f"Library Download Complete!")
    print(f"{'='*70}")
    print(f"Downloaded: {len(libs_to_download)} new libraries ({format_size(total_size)})")
    print(f"Already cached: {total_libraries - len(libs_to_download)} libraries")
    if failed_libraries:
        print(f"Failed: {len(failed_libraries)} libraries")
        for lib in failed_libraries[:5]:
            print(f"  - {lib}")
        if len(failed_libraries) > 5:
            print(f"  ... and {len(failed_libraries) - 5} more")
        return False
    else:
        print(f"[OK] Successfully downloaded all libraries for version {version_id}")
        print(f"Location: {libraries_dir}")
        print(f"{'='*70}\n")
        return True

def main():
    global dev_mode, download_dir_name
    
    parser = argparse.ArgumentParser(
        description="Download Minecraft libraries for a specific version",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\nExamples:\n  python download_libraries.py --version list\n  python download_libraries.py --version 1.21.1\n  python download_libraries.py --version 1.21.1 --dev-mode\n  python download_libraries.py --version 1.21.1 --dir custom_minecraft/\n        """
    )
    
    parser.add_argument(
        "--version",
        required=True,
        help='Minecraft version to download or "list" to see available versions'
    )
    
    parser.add_argument(
        "--dev-mode",
        action="store_true",
        help="Dev mode: save to dev_minecraft_downloads/"
    )
    
    parser.add_argument(
        "--dir",
        type=str,
        help="Custom download directory (default: minecraft_downloads/)"
    )
    
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output progress as JSON"
    )
    
    args = parser.parse_args()
    
    global json_output
    json_output = args.json
    
    # Handle directory setup
    if args.dir:
        set_download_dir(args.dir)
        download_dir_name = args.dir
    elif args.dev_mode:
        dev_mode = True
        download_dir_name = "dev_minecraft_downloads"
        set_download_dir(download_dir_name)
    
    if args.version.lower() == "list":
        list_versions()
    else:
        success = download_libraries(args.version)
        sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()

