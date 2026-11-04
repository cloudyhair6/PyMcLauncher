"""Download Minecraft libraries for a specific version."""

import argparse
import sys
import json
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
import os

# Enable UTF-8 output on Windows without replacing/closing sys.stdout.buffer
if sys.platform == 'win32':
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)

_pkg_dir = Path(__file__).resolve().parent
_root_dir = _pkg_dir.parent if _pkg_dir.name == "backend" else _pkg_dir
for _p in [str(_pkg_dir), str(_root_dir)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

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
                try:
                    lib_path.unlink(missing_ok=True)
                except Exception:
                    pass
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
            try:
                lib_path.unlink(missing_ok=True)
            except Exception:
                pass
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
    
    for version_id, version_type, release_date in versions:
        type_label = version_type.replace("old_", "").upper()
        print(f"{version_id:<20} {type_label:<15} {release_date:<30}")
    
    print("-" * 70)

def download_libraries(version_id, json_mode=None, is_dev_mode=None):
    """Download Minecraft libraries for a specific version using parallel downloads."""
    global dev_mode, download_dir_name, json_output
    if json_mode is not None:
        json_output = json_mode
    if is_dev_mode is not None:
        dev_mode = is_dev_mode

    with progress_lock:
        progress_data['completed_size'] = 0
        progress_data['total_size'] = 0
        progress_data['completed'] = 0
        progress_data['failed'] = 0
        progress_data['start_time'] = time.time()
        progress_data['current_lib'] = ''
    
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
    
    from garbage_collect import is_allowed_by_rules
    libraries = [lib for lib in version_json['libraries'] if is_allowed_by_rules(lib.get('rules', []))]
    print(f"Found {len(libraries)} libraries for current OS")
    
    # Prepare libraries directory
    libraries_dir = get_dir("libraries")
    libraries_dir.mkdir(parents=True, exist_ok=True)
    
    # Gather all candidate artifacts & native classifiers for current OS
    import platform
    sysname = platform.system().lower()
    os_key = 'osx' if sysname == 'darwin' else ('windows' if sysname == 'windows' else 'linux')
    arch_bits = '64' if sys.maxsize > 2**32 else '32'

    candidate_items = []
    for library in libraries:
        if 'downloads' not in library:
            continue
        downloads = library['downloads']
        if 'artifact' in downloads:
            artifact = downloads['artifact']
            candidate_items.append((
                artifact['path'].split('/')[-1],
                artifact['url'],
                artifact.get('size', 0),
                artifact.get('sha1', ''),
                libraries_dir / artifact['path'],
            ))
        if 'classifiers' in downloads:
            classifiers = downloads['classifiers']
            natives_map = library.get('natives')
            if isinstance(natives_map, dict):
                raw_cls_key = natives_map.get(os_key)
                target_keys = [raw_cls_key.replace('${arch}', arch_bits)] if raw_cls_key else []
            else:
                target_keys = [
                    k for k in classifiers.keys()
                    if os_key in k.lower() or ('osx' if os_key == 'osx' else os_key) in k.lower()
                ] or list(classifiers.keys())
            for classifier_key in target_keys:
                if classifier_key not in classifiers:
                    continue
                classifier = classifiers[classifier_key]
                candidate_items.append((
                    classifier['path'].split('/')[-1],
                    classifier['url'],
                    classifier.get('size', 0),
                    classifier.get('sha1', ''),
                    libraries_dir / classifier['path'],
                ))

    libs_to_download = []
    total_size = 0
    print("Verifying existing libraries (this may take a moment)...")

    def _verify_lib_item(item):
        lib_name, lib_url, lib_size, lib_hash, lib_path = item
        is_valid = lib_path.exists() and (not lib_hash or verify_sha1(lib_path, lib_hash))
        return is_valid, item

    total_candidates = max(len(candidate_items), 1)
    last_verify_percent = -1
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as verify_pool:
        for idx, (is_valid, item) in enumerate(verify_pool.map(_verify_lib_item, candidate_items)):
            lib_name, lib_url, lib_size, lib_hash, lib_path = item
            if globals().get('json_output', False):
                percent = ((idx + 1) / total_candidates) * 100
                if percent - last_verify_percent >= 2.0 or idx == total_candidates - 1:
                    print(json.dumps({"type": "progress", "progress": percent, "status": f"Verifying Libraries: {percent:.1f}%"}))
                    sys.stdout.flush()
                    last_verify_percent = percent
            if not is_valid:
                libs_to_download.append(item)
                total_size += lib_size
            else:
                with progress_lock:
                    progress_data['completed'] += 1
                    progress_data['completed_size'] += lib_size
    
    if not libs_to_download:
        print(f"[OK] All {len(libraries)} libraries already downloaded")
        return True
    
    total_libraries = len(libs_to_download)
    mode_str = " (dev-mode)" if dev_mode else ""
    print(f"Downloading {len(libs_to_download)} new libraries ({format_size(total_size)} total){mode_str}")
    print(f"Starting parallel download with {MAX_WORKERS} concurrent connections...\n")
    
    # Initialize progress data for the missing libraries to download
    with progress_lock:
        progress_data['completed'] = 0
        progress_data['completed_size'] = 0
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
    print(f"Already cached: {len(libraries) - len(libs_to_download)} libraries")
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
    
    # Handle directory setup (respect active profile in launcher_config.json unless --dir is passed)
    if args.dev_mode:
        dev_mode = True
    if args.dir:
        set_download_dir(args.dir)
        download_dir_name = args.dir
    
    if args.version.lower() == "list":
        list_versions()
    else:
        success = download_libraries(args.version)
        sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()

