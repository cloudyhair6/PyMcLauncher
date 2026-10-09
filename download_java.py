#!/usr/bin/env python3
"""
Download Java Runtime for Minecraft
Automatically downloads the correct Java version based on Minecraft version
from the Minecraft launcher manifest
"""

import json
import requests
import argparse
import subprocess
import sys
import os
import zipfile
import platform
from pathlib import Path
from typing import Optional, Dict
import logging
import hashlib

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

# Import utils for formatting
sys.path.insert(0, str(Path(__file__).parent))
try:
    from utils import get_download_dir, get_dir, set_download_dir
    json_output = False
except ImportError:
    def get_download_dir():
        return Path('minecraft_downloads')
    
    def set_download_dir(path):
        pass


def get_minecraft_version_manifest() -> Optional[Dict]:
    """Get Minecraft version manifest"""
    try:
        response = requests.get(
            'https://launchermeta.mojang.com/mc/game/version_manifest.json',
            timeout=10
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Failed to fetch version manifest: {e}")
        return None


def get_version_info(version_id: str) -> Optional[Dict]:
    """Get detailed version info"""
    try:
        manifest = get_minecraft_version_manifest()
        if not manifest:
            return None
        
        # Find version URL
        version_url = None
        for version in manifest.get('versions', []):
            if version['id'] == version_id:
                version_url = version['url']
                break
        
        if not version_url:
            logger.error(f"Version {version_id} not found in manifest")
            return None
        
        # Get version details
        response = requests.get(version_url, timeout=10)
        response.raise_for_status()
        return response.json()
    
    except Exception as e:
        logger.error(f"Failed to get version info for {version_id}: {e}")
        return None


def get_java_version_info(version_id: str) -> Optional[Dict]:
    """Get Java version info for Minecraft version"""
    version_info = get_version_info(version_id)
    if not version_info:
        return None
    
    java_version = version_info.get('javaVersion', {})
    if not java_version:
        logger.warning(f"No Java version info found for {version_id}, using default Java 17")
        return {
            'major_version': 17,
            'component': 'java-runtime-alpha',
            'minecraft_version': version_id
        }
    
    return {
        'major_version': java_version.get('majorVersion'),
        'component': java_version.get('component'),
        'minecraft_version': version_id
    }


def get_java_download_url(major_version: int) -> Optional[str]:
    """Get Java runtime download URL from Eclipse Adoptium API"""
    try:
        system = platform.system().lower()
        arch = 'x64' if sys.maxsize > 2**32 else 'x86'
        
        if system == 'windows':
            os_name = 'windows'
        elif system == 'darwin':
            os_name = 'mac'
        elif system == 'linux':
            os_name = 'linux'
        else:
            logger.error(f"Unsupported OS: {system}")
            return None
            
        # Adoptium API for latest JDK of a specific major version
        url = f"https://api.adoptium.net/v3/binary/latest/{major_version}/ga/{os_name}/{arch}/jdk/hotspot/normal/eclipse"
        return url
        
    except Exception as e:
        logger.error(f"Failed to generate Java download URL: {e}")
        return None


def get_system_info() -> Dict[str, str]:
    """Get system information"""
    system = platform.system().lower()
    arch = 'x64' if sys.maxsize > 2**32 else 'x86'
    
    if system == 'windows':
        return {'os': 'windows', 'arch': arch, 'ext': 'zip'}
    elif system == 'darwin':
        return {'os': 'macos', 'arch': arch, 'ext': 'tar.gz'}
    elif system == 'linux':
        return {'os': 'linux', 'arch': arch, 'ext': 'tar.gz'}
    else:
        return {'os': 'unknown', 'arch': arch, 'ext': 'tar.gz'}


def download_java_runtime(major_version: int, component: str, output_dir: Optional[Path] = None) -> bool:
    """Download Java runtime from Adoptium API."""
    import json
    try:
        if output_dir is None:
            output_dir = get_dir("java")
        
        output_dir.mkdir(parents=True, exist_ok=True)
        java_home = output_dir / component
        
        # Check if already downloaded
        java_exec = java_home / 'bin' / ('java.exe' if platform.system() == 'Windows' else 'java')

        import os, stat, shutil
        def remove_readonly(func, path, excinfo):
            try:
                os.chmod(path, stat.S_IWRITE)
                func(path)
            except Exception:
                pass

        if java_exec.exists():
            if globals().get('json_output', False):
                print(json.dumps({"type": "progress", "progress": 100, "status": "Verifying Java installation..."}))
                sys.stdout.flush()
            if verify_java_installation(str(java_exec)):
                logger.info(f"[OK] Java runtime {component} (v{major_version}) already downloaded")
                return True
            else:
                logger.warning(f"Java runtime {component} is corrupt or incomplete. Re-downloading...")
                try:
                    shutil.rmtree(java_home, onerror=remove_readonly)
                except Exception as e:
                    logger.warning(f"Failed to clean corrupt Java installation: {e}")
            
        logger.info(f"Downloading Java runtime {component} (Version {major_version}) from Adoptium...")
        
        download_url = get_java_download_url(major_version)
        if not download_url:
            logger.error(f"Could not generate Adoptium URL for Java {major_version}")
            return False
            
        # Adoptium returns a redirect to the actual binary
        logger.info(f"Fetching from: {download_url}")
        
        tmp_archive = output_dir / f"{component}.archive"
        with requests.get(download_url, stream=True, timeout=60, allow_redirects=True) as r:
            r.raise_for_status()
            total_size = int(r.headers.get('content-length', 0))
            downloaded = 0
            last_percent = -1
            with open(tmp_archive, 'wb') as f:
                for chunk in r.iter_content(8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_size:
                            percent = (downloaded / total_size) * 100
                            if globals().get('json_output', False):
                                if percent - last_percent >= 1.0 or percent == 100:
                                    print(json.dumps({"type": "progress", "progress": percent, "status": f"Downloading Java: {percent:.1f}%"}))
                                    sys.stdout.flush()
                                    last_percent = percent
                            else:
                                sys.stdout.write(f"\rDownloading Java: {percent:.1f}%")
                                sys.stdout.flush()
            if not globals().get('json_output', False):
                print()
                        
        logger.info("Extracting Java runtime...")
        try:
            if platform.system() == 'Windows':
                with zipfile.ZipFile(tmp_archive, 'r') as z:
                    # Adoptium zips usually have a root folder like 'jdk-17.0.8+7-jre'
                    # We want to extract its contents directly into java_home, or just extract and rename
                    members = z.infolist()
                    total_members = len(members)
                    last_percent = -1
                    for i, member in enumerate(members):
                        z.extract(member, output_dir)
                        percent = (i / total_members) * 100
                        if globals().get('json_output', False):
                            if percent - last_percent >= 1.0 or i == total_members - 1:
                                print(json.dumps({"type": "progress", "progress": percent, "status": f"Extracting Java: {percent:.1f}%"}))
                                sys.stdout.flush()
                                last_percent = percent
                        else:
                            sys.stdout.write(f"\rExtracting Java: {percent:.1f}%")
                            sys.stdout.flush()
                    if not globals().get('json_output', False):
                        print()
                    root_folder = z.namelist()[0].split('/')[0]
                    extracted_dir = output_dir / root_folder
                    if extracted_dir.exists() and extracted_dir != java_home:
                        if java_home.exists():
                            shutil.rmtree(java_home, onerror=remove_readonly)
                        extracted_dir.rename(java_home)
            else:
                import tarfile
                with tarfile.open(tmp_archive, 'r:gz') as t:
                    members = t.getmembers()
                    total_members = len(members)
                    last_percent = -1
                    for i, member in enumerate(members):
                        t.extract(member, output_dir)
                        percent = (i / total_members) * 100
                        if globals().get('json_output', False):
                            if percent - last_percent >= 1.0 or i == total_members - 1:
                                print(json.dumps({"type": "progress", "progress": percent, "status": f"Extracting Java: {percent:.1f}%"}))
                                sys.stdout.flush()
                                last_percent = percent
                        else:
                            sys.stdout.write(f"\rExtracting Java: {percent:.1f}%")
                            sys.stdout.flush()
                    if not globals().get('json_output', False):
                        print()
                    root_folder = t.getnames()[0].split('/')[0]
                    extracted_dir = output_dir / root_folder
                    if extracted_dir.exists() and extracted_dir != java_home:
                        if java_home.exists():
                            shutil.rmtree(java_home, onerror=remove_readonly)
                        extracted_dir.rename(java_home)
        except Exception as e:
            logger.warning(f"Failed to extract runtime archive: {e}")
            return False
        finally:
            try:
                tmp_archive.unlink()
            except Exception:
                pass
                
        if java_exec.exists() and verify_java_installation(str(java_exec)):
            logger.info(f"[OK] Java runtime {component} downloaded successfully!")
            return True
        else:
            logger.error("Java runtime extracted but verification failed!")
            return False
            
    except Exception as e:
        logger.error(f"Failed to download Java runtime: {e}")
        return False


def verify_java_installation(java_path: Optional[str] = None) -> bool:
    """Verify Java installation"""
    try:
        if java_path is None:
            java_path = 'java'
        
        result = subprocess.run(
            [java_path, '-version'],
            capture_output=True,
            text=True,
            timeout=5
        )
        
        if result.returncode == 0:
            logger.info(f"[OK] Java found: {result.stderr.split()[2] if result.stderr else 'unknown version'}")
            return True
        else:
            logger.error("Java not found or not in PATH")
            return False
    
    except Exception as e:
        logger.error(f"Failed to verify Java: {e}")
        return False


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description='Download Java runtime for Minecraft',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Get Java version for Minecraft 1.21.1
  python download_java.py --version 1.21.1 --info
  
  # Download Java for Minecraft 1.21.1
  python download_java.py --version 1.21.1
  
  # Download Java to custom directory
  python download_java.py --version 1.21.1 --dir my_games/
  
  # Check system Java installation
  python download_java.py --check
  
  # List all supported Java versions
  python download_java.py --list-versions
        """
    )
    
    parser.add_argument(
        '--version',
        help='Minecraft version (e.g., 1.21.1)'
    )
    
    parser.add_argument(
        '--dir',
        help='Custom directory for downloads'
    )
    
    parser.add_argument(
        '--info',
        action='store_true',
        help='Show Java version info for Minecraft version'
    )
    
    parser.add_argument(
        '--check',
        action='store_true',
        help='Check if Java is installed on system'
    )
    
    parser.add_argument(
        '--java-path',
        help='Path to Java executable'
    )
    
    parser.add_argument(
        '--list-versions',
        action='store_true',
        help='List supported Minecraft versions'
    )
    
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output progress as JSON"
    )
    
    args = parser.parse_args()
    
    global json_output
    json_output = args.json
    
    # Set custom directory if provided
    if args.dir:
        set_download_dir(args.dir)
    
    # Check system Java
    if args.check:
        logger.info("Checking system Java installation...")
        if verify_java_installation(args.java_path):
            logger.info("[OK] Java is available on system")
            sys.exit(0)
        else:
            logger.error("[FAILURE] Java not found on system")
            sys.exit(1)
    
    # List versions
    if args.list_versions:
        logger.info("Fetching Minecraft versions...")
        manifest = get_minecraft_version_manifest()
        if manifest:
            versions = manifest.get('versions', [])
            logger.info(f"Found {len(versions)} versions:")
            for v in versions[:10]:
                logger.info(f"  - {v['id']} ({v['type']})")
            logger.info(f"  ... and {len(versions) - 10} more")
        sys.exit(0)
    
    # Get version info
    if not args.version:
        if not args.check and not args.list_versions:
            parser.print_help()
        return
    
    if args.info:
        logger.info(f"Getting Java version info for Minecraft {args.version}...")
        java_info = get_java_version_info(args.version)
        if java_info:
            logger.info(f"Java Version: {java_info.get('major_version')}")
            logger.info(f"Component: {java_info.get('component')}")
            print(json.dumps(java_info, indent=2))
            sys.exit(0)
        else:
            logger.error("Failed to get Java version info")
            sys.exit(1)
    
    # Download Java
    logger.info(f"Preparing Java download for Minecraft {args.version}...")
    
    java_info = get_java_version_info(args.version)
    if not java_info:
        logger.error("[FAILURE] Could not determine Java version")
        sys.exit(1)
    
    component = java_info.get('component')
    logger.info(f"[INFO] Java version {java_info.get('major_version')} required")
    logger.info(f"[INFO] Component: {component}")
    
    if download_java_runtime(java_info.get('major_version'), component, get_dir("java") if args.dir else None):
        logger.info("[SUCCESS] Java runtime ready")
        sys.exit(0)
    else:
        logger.error("[FAILURE] Failed to download Java runtime")
        sys.exit(1)


if __name__ == '__main__':
    main()
