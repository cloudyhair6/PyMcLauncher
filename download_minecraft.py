"""Download Minecraft game files for a specific version."""

import argparse
import sys
import json
import subprocess
import zipfile
from pathlib import Path
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
    
    for version_id, version_type in versions:
        type_label = version_type.replace("old_", "").upper()
        print(f"{version_id:<20} {type_label:<15}")
    
    print("-" * 70)

def download_version(version_id, jar_type="client"):
    """Download Minecraft game JAR for a specific version."""
    global dev_mode, download_dir_name
    
    ensure_download_dir()
    
    print(f"Downloading Minecraft {version_id} ({jar_type})...")
    
    # Get version info
    version_info = get_version_info(version_id)
    if not version_info:
        print(f"Error: Version {version_id} not found")
        return False
    
    # Get version JSON which contains download URLs
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
            return True
        elif not expected_sha1:
            print(f"[OK] Version {version_id} ({jar_type}) already exists.")
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
    """Extract source code from mapping files if available."""
    global download_dir_name
    import subprocess
    
    print(f"\nExtracting source code for {version_id} ({jar_type})...")
    
    # Check for mapping files in version JSON
    if 'downloads' not in version_json:
        print("No mappings available")
        return
    
    mapping_key = f"{jar_type}_mappings"
    if mapping_key not in version_json['downloads']:
        print(f"No {jar_type} mappings available for version {version_id}")
        return
    
    mappings_download = version_json['downloads'][mapping_key]
    mappings_url = mappings_download['url']
    
    # Download mappings file
    mappings_dir = get_download_dir() / "mappings"
    mappings_dir.mkdir(parents=True, exist_ok=True)
    mappings_path = mappings_dir / f"{version_id}-{jar_type}.txt"
    
    print(f"Downloading {jar_type} mappings...")
    if not download_file(mappings_url, mappings_path):
        print(f"Failed to download {jar_type} mappings")
        return
    
    print(f"[OK] Downloaded mappings to {mappings_path}")
    
    # Extract deobfuscated source code
    source_dir = get_download_dir() / "source" / f"{version_id}-{jar_type}"
    
    print(f"\nGenerating deobfuscated Java source files...")
    print(f"This may take a minute or two...\n")
    
    try:
        # Call the extract_source_code.py script
        script_path = Path(__file__).parent / "extract_source_code.py"
        result = subprocess.run(
            [sys.executable, str(script_path), 
             "--mappings", str(mappings_path),
             "--output", str(source_dir)],
            capture_output=False
        )
        
        if result.returncode == 0:
            print(f"[OK] Source code extracted to: {source_dir}")
        else:
            print(f"[FAIL] Failed to extract source code")
    
    except Exception as e:
        print(f"Error extracting source code: {e}")

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
        # Determine JAR type
        jar_type = "server" if args.server else "client"
        
        success = download_version(args.version, jar_type)
        sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()
