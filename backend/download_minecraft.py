"""Download Minecraft game files for a specific version."""

import argparse
import sys
import json
import subprocess
import zipfile
from pathlib import Path
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
    get_version_info,
    get_version_json,
    download_file,
    set_download_dir,
    get_download_dir,
    DOWNLOAD_DIR,
    verify_sha1
)
import requests

# Global settings
dev_mode = False
download_dir_name = "minecraft_downloads"
json_output = False

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

def download_version(version_id, jar_type="client", json_mode=None, is_dev_mode=None):
    """Download Minecraft game JAR for a specific version."""
    global dev_mode, download_dir_name, json_output
    if json_mode is not None:
        json_output = json_mode
    if is_dev_mode is not None:
        dev_mode = is_dev_mode
    
    ensure_download_dir()
    
    print(f"Downloading Minecraft {version_id} ({jar_type})...")
    
    # Get version JSON which contains download URLs (uses local cache if available)
    version_json = get_version_json(version_id)
    if not version_json:
        print(f"Error: Could not fetch metadata for version {version_id}")
        return False
    
    # Check if downloads section exists
    if 'downloads' not in version_json or jar_type not in version_json['downloads']:
        print(f"Error: No {jar_type} download available for version {version_id}")
        return False
    
    jar_download = version_json['downloads'][jar_type]
    download_url = jar_download['url']
    expected_sha1 = jar_download.get('sha1', None)
    
    # Prepare file path
    version_dir = get_dir("versions")
    version_dir.mkdir(parents=True, exist_ok=True)
    file_path = version_dir / f"{version_id}-{jar_type}.jar"
    
    # Check if already downloaded
    if file_path.exists():
        if expected_sha1:
            print(f"Verifying existing {jar_type}.jar (this may take a moment)...")
            if globals().get('json_output', False):
                print(json.dumps({"type": "progress", "progress": 100, "status": f"Verifying {jar_type}.jar..."}))
                sys.stdout.flush()
            if verify_sha1(file_path, expected_sha1):
                print(f"[OK] Version {version_id} ({jar_type}) already exists and verified.")
                if dev_mode:
                    extract_source_code(version_id, jar_type, version_json)
                return True
            print(f"[WARN] Existing {jar_type}.jar failed SHA-1 verification. Re-downloading...")
        elif not expected_sha1:
            print(f"[OK] Version {version_id} ({jar_type}) already exists.")
            if dev_mode:
                extract_source_code(version_id, jar_type, version_json)
            return True
    
    # Download the file
    print(f"Downloading from: {download_url}")
    if download_file(download_url, file_path, expected_sha1, json_output=globals().get('json_output', False)):
        print(f"[OK] Successfully downloaded Minecraft {version_id} ({jar_type})")
        
        # In dev mode, extract source code if available
        if dev_mode:
            extract_source_code(version_id, jar_type, version_json)
        
        return True
    else:
        print(f"[FAIL] Failed to download Minecraft {version_id} ({jar_type})")
        if file_path.exists():
            file_path.unlink()
        return False

def extract_source_code(version_id, jar_type, version_json):
    """Extract and decompile source code for any Minecraft version."""
    from extract_source_code import extract_version_source
    jar_path = get_dir("versions") / f"{version_id}-{jar_type}.jar"
    source_dir = get_dir("source") / f"{version_id}-{jar_type}"
    extract_version_source(
        version_id=version_id,
        jar_type=jar_type,
        jar_path=jar_path,
        output_dir=source_dir,
        json_output=globals().get('json_output', False)
    )

def main():
    global dev_mode, download_dir_name
    
    parser = argparse.ArgumentParser(
        description="Download Minecraft game files (client or server JAR)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\nExamples:\n  python download_minecraft.py --version list\n  python download_minecraft.py --client --version 1.21.1\n  python download_minecraft.py --server --version 1.21.1\n  python download_minecraft.py --client --version 1.21.1 --dev-mode\n  python download_minecraft.py --server --version 1.21.1 --dev-mode\n  python download_minecraft.py --client --version 1.21.1 --dir custom_dir/\n        """
    )
    
    parser.add_argument(
        "--version",
        required=True,
        help='Minecraft version to download or "list" to see available versions'
    )
    
    parser.add_argument(
        "--client",
        action="store_true",
        help="Download client JAR (default)"
    )
    
    parser.add_argument(
        "--server",
        action="store_true",
        help="Download server JAR"
    )
    
    parser.add_argument(
        "--dev-mode",
        action="store_true",
        help="Dev mode: also download and extract source mappings, save to dev_minecraft_downloads/"
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
        # Determine JAR type
        jar_type = "server" if args.server else "client"
        
        success = download_version(args.version, jar_type)
        sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()
