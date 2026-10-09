"""Download Minecraft assets for a specific version."""

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
    set_download_dir,
    get_download_dir,
    DOWNLOAD_DIR
)
import requests
import requests.adapters

ASSET_CDN_URL = "https://resources.download.minecraft.net"
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
    'current_asset': ''
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

def download_asset_worker(asset_name, asset_hash, asset_size, assets_dir):
    """Download a single asset file."""
    asset_url = f"{ASSET_CDN_URL}/{asset_hash[:2]}/{asset_hash}"
    
    if dev_mode:
        # In dev mode, create proper folder structure (e.g., minecraft/sounds/ambient/weather/)
        asset_path = assets_dir / asset_name
        asset_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        # Normal mode: organize by hash (e.g., 00/005ff4be...)
        asset_dir = assets_dir / asset_hash[:2]
        asset_dir.mkdir(parents=True, exist_ok=True)
        asset_path = asset_dir / asset_hash
    
    # Verification is now done beforehand
    # This worker only downloads
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = session.get(asset_url, timeout=30, stream=True)
            response.raise_for_status()

            with open(asset_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=16384):
                    if chunk:
                        f.write(chunk)

            # Verify SHA-1
            if not verify_sha1(asset_path, asset_hash):
                asset_path.unlink()
                if attempt < max_retries - 1:
                    time.sleep(1)
                    continue
                return False, asset_name, asset_size

            with progress_lock:
                progress_data['completed'] += 1
                progress_data['completed_size'] += asset_size
                progress_data['current_asset'] = asset_name

            return True, asset_name, asset_size

        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(1)
                continue
            return False, asset_name, asset_size
        
def update_progress_display(total_assets):
    """Display current progress with ETA."""
    with progress_lock:
        completed = progress_data['completed']
        completed_size = progress_data['completed_size']
        total_size = progress_data['total_size']
        start_time = progress_data['start_time']
        current_asset = progress_data['current_asset']
    
    elapsed = time.time() - start_time
    percent = (completed_size / total_size) * 100 if total_size > 0 else 0
    
    # Calculate speed based on completed asset size
    if elapsed > 1:
        speed = completed_size / elapsed
    else:
        speed = 0
    
    # Calculate ETA based on remaining assets and average size per asset
    remaining_assets = total_assets - completed
    if completed > 0 and speed > 0:
        avg_size_per_asset = completed_size / completed
        remaining_size = remaining_assets * avg_size_per_asset
        eta_seconds = remaining_size / speed
        eta_str = format_time(eta_seconds)
    else:
        eta_str = "calculating..."
    
    # Display progress line with fixed width format to prevent wrapping
    # Format: [completed/total] percent% | Speed: X/s | ETA: X | asset_name (truncated if needed)
    speed_str = format_size(speed) + "/s"
    
    # Limit asset name to fit in terminal (max 50 chars)
    asset_display = current_asset[:50] if current_asset else "assets"
    
    # Create the full status line
    status_line = f"[{completed}/{total_assets}] {percent:.1f}% | Speed: {speed_str:>9s} | ETA: {eta_str:>6s} | {asset_display}"
    
    global json_output
    if globals().get('json_output', False):
        # We only want to output JSON progress if it changed significantly to avoid spam, 
        # or we just output it and let the launcher handle it.
        # But to avoid massive spam, let's just output it. Python's print will add 

        print(json.dumps({"type": "progress", "progress": percent, "status": status_line}))
    else:
        # Write to stdout with carriage return to overwrite the line
        sys.stdout.write(f"\r{status_line:<120}")
        sys.stdout.flush()

def list_versions_with_assets():
    """Display all available Minecraft versions."""
    print("Fetching available versions...")
    versions = get_available_versions()
    
    if not versions:
        print("Error: Could not fetch version list")
        return
    
    print(f"\nAvailable Minecraft versions with assets ({len(versions)} total):")
    print("-" * 70)
    print(f"{'Version':<20} {'Type':<15} {'Release Date':<30}")
    print("-" * 70)
    
    for version_id, version_type, release_date in versions:
        type_label = version_type.replace("old_", "").upper()
        print(f"{version_id:<20} {type_label:<15} {release_date:<30}")
    
    print("-" * 70)

def download_assets(version_id):
    """Download all assets for a specific Minecraft version using parallel downloads."""
    global dev_mode, download_dir_name
    
    ensure_download_dir()
    
    print(f"Downloading Minecraft {version_id} assets...")
    
    # Get version JSON which contains asset index URL
    version_json = get_version_json(version_id)
    if not version_json:
        print(f"Error: Could not fetch metadata for version {version_id}")
        return False
    
    # Check if assetIndex exists
    if 'assetIndex' not in version_json:
        print(f"Error: No asset index available for version {version_id}")
        return False
    
    asset_index_info = version_json['assetIndex']
    asset_index_url = asset_index_info['url']
    asset_index_id = asset_index_info.get('id', 'unknown')
    
    # Download asset index
    print(f"Downloading asset index for {version_id}...")
    
    # Store the asset index inside the assets/indexes directory (what the game expects)
    index_dir = get_dir("assets") / "indexes"
    index_dir.mkdir(parents=True, exist_ok=True)
    index_path = index_dir / f"{asset_index_id}.json"
    
    from utils import download_file
    if not download_file(asset_index_url, index_path):
        print(f"Error: Failed to download asset index")
        return False
    
    # Parse asset index
    try:
        with open(index_path, 'r') as f:
            asset_index = json.load(f)
    except Exception as e:
        print(f"Error: Could not parse asset index: {e}")
        return False
    
    # Download all assets
    if 'objects' not in asset_index:
        print("Error: Asset index has no objects")
        return False
    
    assets = asset_index['objects']
    total_assets = len(assets)
    
    # Calculate total size
    total_size = sum(asset_info.get('size', 0) for asset_info in assets.values())
    
    mode_str = " (dev-mode)" if dev_mode else ""
    print(f"Found {total_assets} assets to download ({format_size(total_size)} total){mode_str}")
    # Set up assets directory
    if dev_mode:
        assets_dir = get_dir("assets")
    else:
        assets_dir = get_dir("assets") / "objects"
    
    assets_dir.mkdir(parents=True, exist_ok=True)
    
    print("Verifying existing assets...")
    missing_assets = {}
    
    # Initialize progress tracking for verification
    progress_data['completed_size'] = 0
    progress_data['total_size'] = total_size
    progress_data['completed'] = 0
    progress_data['failed'] = 0
    progress_data['start_time'] = time.time()
    progress_data['current_asset'] = ''
    
    # Phase 1: Verify existing
    last_verify_percent = -1
    for idx, (asset_name, asset_info) in enumerate(assets.items()):
        if globals().get('json_output', False):
            percent = (idx / len(assets)) * 100
            if percent - last_verify_percent >= 2.0 or idx == len(assets) - 1:
                print(json.dumps({"type": "progress", "progress": percent, "status": f"Verifying Assets: {percent:.1f}%"}))
                sys.stdout.flush()
                last_verify_percent = percent

        asset_hash = asset_info.get('hash', '')
        asset_size = asset_info.get('size', 0)
        
        if not asset_hash:
            continue
            
        if dev_mode:
            asset_path = assets_dir / asset_name
        else:
            asset_path = assets_dir / asset_hash[:2] / asset_hash
            
        if asset_path.exists():
            progress_data['completed'] += 1
            progress_data['completed_size'] += asset_size
            progress_data['current_asset'] = asset_name
            update_progress_display(total_assets)
        else:
            missing_assets[asset_name] = asset_info
            
    print()  # Newline after verify progress
    
    # Phase 2: Download missing
    failed_assets = []
    if missing_assets:
        missing_size = sum(a.get('size', 0) for a in missing_assets.values())
        print(f"Downloading {len(missing_assets)} missing assets ({format_size(missing_size)})...")
        print(f"Starting parallel download with {MAX_WORKERS} concurrent connections...")
        
        progress_data['start_time'] = time.time()  # Reset timer for speed calculation
        
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {}
            
            for asset_name, asset_info in missing_assets.items():
                asset_hash = asset_info.get('hash', '')
                asset_size = asset_info.get('size', 0)
                
                future = executor.submit(download_asset_worker, asset_name, asset_hash, asset_size, assets_dir)
                futures[future] = asset_name
            
            # Process completed downloads
            for future in as_completed(futures):
                success, asset_name, asset_size = future.result()
                
                if not success:
                    with progress_lock:
                        progress_data['failed'] += 1
                    failed_assets.append(asset_name)
                
                update_progress_display(total_assets)
    
    # Final summary
    print()  # New line after progress
    elapsed = time.time() - progress_data['start_time']
    
    print(f"\n" + "="*70)
    print(f"Download Complete!")
    print(f"="*70)
    print(f"Total Time: {format_time(elapsed)}")
    print(f"Average Speed: {format_size(progress_data['completed_size'] / elapsed if elapsed > 0 else 0)}/s")
    print(f"Downloaded: {format_size(progress_data['completed_size'])} / {format_size(total_size)}")
    print(f"Assets: {progress_data['completed']}/{total_assets}")
    
    if failed_assets:
        print(f"\n[FAIL] Failed to download {len(failed_assets)} assets:")
        for asset in failed_assets[:10]:
            print(f"  - {asset}")
        if len(failed_assets) > 10:
            print(f"  ... and {len(failed_assets) - 10} more")
        return False
    else:
        # Phase 3: Reconstruct legacy assets if needed
        if asset_index_id in ['legacy', 'pre-1.6'] and not dev_mode:
            print("\nReconstructing legacy assets (this may take a moment)...")
            resources_dir = get_dir("resources")
            virtual_legacy_dir = get_dir("assets") / "virtual" / "legacy"
            
            # The game uses resources/ and some tweakers use virtual/legacy/
            import shutil
            for asset_name, asset_info in assets.items():
                asset_hash = asset_info.get('hash', '')
                if not asset_hash: continue
                
                obj_path = assets_dir / asset_hash[:2] / asset_hash
                if not obj_path.exists(): continue
                
                res_path = resources_dir / asset_name
                virt_path = virtual_legacy_dir / asset_name
                
                res_path.parent.mkdir(parents=True, exist_ok=True)
                virt_path.parent.mkdir(parents=True, exist_ok=True)
                
                if not res_path.exists():
                    shutil.copy2(obj_path, res_path)
                if not virt_path.exists():
                    shutil.copy2(obj_path, virt_path)
            print("[OK] Legacy assets reconstructed successfully")
            
        print(f"\n[OK] Successfully downloaded all {total_assets} assets for version {version_id}")
        return True

def main():
    global dev_mode, download_dir_name
    
    parser = argparse.ArgumentParser(
        description="Download Minecraft assets",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\nExamples:\n  python download_assets.py --version list\n  python download_assets.py --version 1.21.1\n  python download_assets.py --version 1.21.1 --dev-mode\n  python download_assets.py --version 1.21.1 --dir custom_dir/\n        """
    )
    
    parser.add_argument(
        "--version",
        required=True,
        help='Minecraft version assets to download or "list" to see available versions'
    )
    
    parser.add_argument(
        "--dev-mode",
        action="store_true",
        help="Download assets with proper folder structure (e.g., minecraft/sounds/ambient/) instead of hash-based, saves to dev_minecraft_downloads/"
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
        list_versions_with_assets()
    else:
        success = download_assets(args.version)
        sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()
