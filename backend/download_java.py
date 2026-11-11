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

# Ensure backend and root directories are in sys.path
_pkg_dir = Path(__file__).resolve().parent
_root_dir = _pkg_dir.parent if _pkg_dir.name == "backend" else _pkg_dir
for _p in [str(_pkg_dir), str(_root_dir)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from utils import get_download_dir, get_dir, set_download_dir, ROOT_DIR
    json_output = False
except ImportError:
    ROOT_DIR = _root_dir
    def get_download_dir():
        return ROOT_DIR / 'minecraft_downloads'
    
    def set_download_dir(path):
        pass


def get_minecraft_version_manifest() -> Optional[Dict]:
    """Get Minecraft version manifest using shared utils cache"""
    try:
        from utils import fetch_version_manifest
        return fetch_version_manifest()
    except Exception:
        pass
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
    """Get detailed version info (using shared in-memory + disk cache first)"""
    try:
        from utils import get_version_json
        cached = get_version_json(version_id)
        if cached:
            return cached
    except Exception:
        pass
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
        machine = platform.machine().lower()
        is_arm = 'arm' in machine or 'aarch' in machine

        if system == 'windows':
            os_name = 'windows'
            arch = 'arm64' if is_arm else ('x64' if sys.maxsize > 2**32 else 'x86')
        elif system == 'darwin':
            os_name = 'mac'
            # Adoptium officially provides 'aarch64' for Apple Silicon (macOS Big Sur 11.0+)
            # and 'x64' for Intel Macs. Note: For Java <= 8, Adoptium mac builds are x64 only
            # (which runs via macOS Rosetta 2).
            if is_arm:
                arch = 'x64' if major_version <= 8 else 'aarch64'
            else:
                arch = 'x64'
        elif system == 'linux':
            os_name = 'linux'
            arch = 'aarch64' if is_arm else ('x64' if sys.maxsize > 2**32 else 'x86')
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
    machine = platform.machine().lower()
    is_arm = 'arm' in machine or 'aarch' in machine
    
    if is_arm:
        arch = 'arm64' if system == 'darwin' else 'aarch64'
    else:
        arch = 'x64' if sys.maxsize > 2**32 else 'x86'
    
    if system == 'windows':
        return {'os': 'windows', 'arch': arch, 'ext': 'zip'}
    elif system == 'darwin':
        return {'os': 'macos', 'arch': arch, 'ext': 'tar.gz'}
    elif system == 'linux':
        return {'os': 'linux', 'arch': arch, 'ext': 'tar.gz'}
    else:
        return {'os': 'unknown', 'arch': arch, 'ext': 'tar.gz'}


def safely_remove_path(path: Path) -> bool:
    """Safely remove a file, symlink, junction, or directory without deleting junction target contents."""
    import stat, shutil
    path_str = str(path)
    try:
        if platform.system() == 'Windows':
            # In Python on Windows, junctions are directory reparse points.
            # os.path.islink is True for directory symlinks/junctions in Python 3.8+
            if os.path.islink(path_str):
                try:
                    os.unlink(path_str)
                    return True
                except OSError:
                    pass
            # Windows junctions can be unlinked with os.rmdir without deleting target directory contents
            try:
                os.rmdir(path_str)
                return True
            except OSError:
                pass
        else:
            if path.is_symlink():
                path.unlink()
                return True

        if path.is_dir():
            def remove_readonly(func, p, excinfo):
                try:
                    os.chmod(p, stat.S_IWRITE)
                    func(p)
                except Exception:
                    pass
            shutil.rmtree(path_str, onerror=remove_readonly)
            return True
        elif os.path.lexists(path_str):
            try:
                os.chmod(path_str, stat.S_IWRITE)
            except Exception:
                pass
            os.unlink(path_str)
            return True
        return True
    except Exception as e:
        logger.warning(f"Failed to safely remove {path}: {e}")
        return False


def is_java_version_compatible(actual_major: int, expected_major: int) -> bool:
    """
    Check if an installed Java major version is compatible with the Minecraft expected Java version.
    - Java 8 (jre-legacy): Strictly requires Java 8. Older versions of Minecraft (<= 1.16.5) rely
      on URLClassLoader in LaunchClassLoader (ClassCastException on Java 9+).
    - Java 16 (java-runtime-alpha): Compatible with Java 16 or 17.
    - Java 17 (java-runtime-beta / gamma): Compatible with Java 17 up to 21.
    - Java 21 (java-runtime-delta): Compatible with Java 21+.
    - Java 25 (java-runtime-epsilon): Compatible with Java 25+.
    """
    if expected_major <= 8:
        return actual_major == 8
    elif expected_major == 16:
        return 16 <= actual_major <= 17
    elif expected_major == 17:
        return 17 <= actual_major <= 21
    elif expected_major == 21:
        return actual_major >= 21
    elif expected_major >= 25:
        return actual_major >= expected_major
    else:
        return actual_major == expected_major


def get_java_executable_in_home(java_home: Path) -> Path:
    """Locate java binary in standard bin directory or macOS JDK bundle layout (Contents/Home/bin/java)."""
    exe_name = 'java.exe' if platform.system() == 'Windows' else 'java'
    std = java_home / 'bin' / exe_name
    if std.exists():
        return std
    mac_bundle = java_home / 'Contents' / 'Home' / 'bin' / exe_name
    if mac_bundle.exists():
        return mac_bundle
    if java_home.is_dir():
        found = list(java_home.rglob(exe_name))
        if found:
            return found[0]
    return std


def download_java_runtime(major_version: int, component: str, output_dir: Optional[Path] = None) -> bool:
    """Download Java runtime from Adoptium API."""
    import json
    try:
        if output_dir is None:
            output_dir = get_dir("java")
        
        output_dir.mkdir(parents=True, exist_ok=True)
        java_home = output_dir / component
        
        # Check if already downloaded / exists
        java_exec = get_java_executable_in_home(java_home)

        if os.path.lexists(str(java_home)):
            if java_exec.exists():
                if globals().get('json_output', False):
                    print(json.dumps({"type": "progress", "progress": 100, "status": "Verifying Java installation..."}))
                    sys.stdout.flush()
                if verify_java_installation(str(java_exec), expected_major_version=major_version):
                    logger.info(f"[OK] Java runtime {component} (v{major_version}) already downloaded")
                    return True
                else:
                    logger.warning(f"Java runtime {component} is corrupt or incompatible. Cleaning up...")
                    safely_remove_path(java_home)
            else:
                logger.warning(f"Java runtime {component} directory is broken. Cleaning up...")
                safely_remove_path(java_home)

        # Java Runtime Re-Use: Check existing runtimes across all directories & system before downloading
        existing_exec = find_existing_java_runtime(component, major_version)
        if existing_exec and existing_exec.exists():
            # Support macOS bundle parent (Contents/Home) or standard parent
            if 'Contents' in existing_exec.parts:
                existing_home = existing_exec.parent.parent.parent.parent
            else:
                existing_home = existing_exec.parent.parent
            logger.info(f"[INFO] Re-using existing Java runtime for {component} (v{major_version}) from {existing_home}")
            if os.path.lexists(str(java_home)):
                try:
                    if java_home.resolve() == existing_home.resolve():
                        logger.info(f"[OK] Java runtime {component} (v{major_version}) ready via runtime re-use")
                        return True
                except Exception:
                    pass
                safely_remove_path(java_home)

            try:
                if platform.system() == 'Windows':
                    import _winapi
                    _winapi.CreateJunction(str(existing_home.resolve()), str(java_home))
                    logger.info(f"[OK] Created junction for {component} in {output_dir}")
                else:
                    java_home.symlink_to(existing_home, target_is_directory=True)
            except Exception as e:
                logger.warning(f"Could not create junction/symlink for runtime re-use: {e}")

            java_exec = get_java_executable_in_home(java_home)
            if java_exec.exists():
                logger.info(f"[OK] Java runtime {component} (v{major_version}) ready via runtime re-use")
                return True
            else:
                logger.warning(f"Runtime re-use failed to link java binary. Falling back to downloading {component}...")
                safely_remove_path(java_home)
            
        logger.info(f"Downloading Java runtime {component} (Version {major_version}) from Adoptium...")
        
        download_url = get_java_download_url(major_version)
        if not download_url:
            logger.error(f"Could not generate Adoptium URL for Java {major_version}")
            return False
            
        # Adoptium returns a redirect to the actual binary
        logger.info(f"Fetching from: {download_url}")
        
        tmp_archive = output_dir / f"{component}.archive"
        try:
            r = requests.get(download_url, stream=True, timeout=60, allow_redirects=True)
            if r.status_code == 404 and platform.system().lower() == 'darwin':
                # If aarch64 was requested and not found, try x64 as fallback for macOS Rosetta 2
                alt_url = f"https://api.adoptium.net/v3/binary/latest/{major_version}/ga/mac/x64/jdk/hotspot/normal/eclipse"
                logger.info(f"aarch64 binary not found. Retrying with x64 Rosetta fallback: {alt_url}")
                r = requests.get(alt_url, stream=True, timeout=60, allow_redirects=True)
            r.raise_for_status()
        except Exception as e:
            logger.error(f"Failed to connect to Java download URL: {e}")
            return False

        with r:
            total_size = int(r.headers.get('content-length', 0))
            downloaded = 0
            last_percent = -1
            with open(tmp_archive, 'wb') as f:
                for chunk in r.iter_content(8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_size:
                            dl_percent = (downloaded / total_size) * 100.0
                            overall_percent = dl_percent * 0.5
                            if globals().get('json_output', False):
                                if dl_percent - last_percent >= 1.0 or dl_percent >= 100.0:
                                    print(json.dumps({"type": "progress", "progress": overall_percent, "status": f"Downloading Java ({dl_percent:.1f}%): {overall_percent:.1f}%"}))
                                    sys.stdout.flush()
                                    last_percent = dl_percent
                            else:
                                sys.stdout.write(f"\rDownloading Java: {dl_percent:.1f}% (Overall: {overall_percent:.1f}%)")
                                sys.stdout.flush()
            if not globals().get('json_output', False):
                print()
                        
        logger.info("Extracting Java runtime...")
        try:
            if platform.system() == 'Windows':
                with zipfile.ZipFile(tmp_archive, 'r') as z:
                    # Adoptium zips usually have a root folder like 'jdk-17.0.8+7-jre'
                    members = z.infolist()
                    total_members = max(len(members), 1)
                    last_percent = -1
                    for i, member in enumerate(members):
                        z.extract(member, output_dir)
                        ext_percent = ((i + 1) / total_members) * 100.0
                        overall_percent = 50.0 + (ext_percent * 0.5)
                        if globals().get('json_output', False):
                            if ext_percent - last_percent >= 1.0 or i == total_members - 1:
                                print(json.dumps({"type": "progress", "progress": overall_percent, "status": f"Extracting Java ({ext_percent:.1f}%): {overall_percent:.1f}%"}))
                                sys.stdout.flush()
                                last_percent = ext_percent
                        else:
                            sys.stdout.write(f"\rExtracting Java: {ext_percent:.1f}% (Overall: {overall_percent:.1f}%)")
                            sys.stdout.flush()
                    if not globals().get('json_output', False):
                        print()
                    root_folder = z.namelist()[0].split('/')[0]
                    extracted_dir = output_dir / root_folder
                    if extracted_dir.exists() and extracted_dir != java_home:
                        if os.path.lexists(str(java_home)):
                            safely_remove_path(java_home)
                        extracted_dir.rename(java_home)
            else:
                import tarfile
                with tarfile.open(tmp_archive, 'r:gz') as t:
                    members = t.getmembers()
                    total_members = max(len(members), 1)
                    last_percent = -1
                    for i, member in enumerate(members):
                        t.extract(member, output_dir)
                        ext_percent = ((i + 1) / total_members) * 100.0
                        overall_percent = 50.0 + (ext_percent * 0.5)
                        if globals().get('json_output', False):
                            if ext_percent - last_percent >= 1.0 or i == total_members - 1:
                                print(json.dumps({"type": "progress", "progress": overall_percent, "status": f"Extracting Java ({ext_percent:.1f}%): {overall_percent:.1f}%"}))
                                sys.stdout.flush()
                                last_percent = ext_percent
                        else:
                            sys.stdout.write(f"\rExtracting Java: {ext_percent:.1f}% (Overall: {overall_percent:.1f}%)")
                            sys.stdout.flush()
                    if not globals().get('json_output', False):
                        print()
                    root_folder = t.getnames()[0].split('/')[0]
                    extracted_dir = output_dir / root_folder
                    if extracted_dir.exists() and extracted_dir != java_home:
                        if os.path.lexists(str(java_home)):
                            safely_remove_path(java_home)
                        extracted_dir.rename(java_home)

            # On macOS, Adoptium tarballs have Contents/Home layout.
            # Create a symlink or direct bin access if bin is inside Contents/Home
            contents_home = java_home / 'Contents' / 'Home'
            if contents_home.is_dir() and not (java_home / 'bin').exists():
                try:
                    (java_home / 'bin').symlink_to(contents_home / 'bin')
                except Exception:
                    pass

            java_exec = get_java_executable_in_home(java_home)

            # Ensure execution permissions on non-Windows
            if platform.system() != 'Windows' and java_exec.exists():
                try:
                    os.chmod(str(java_exec), 0o755)
                except Exception:
                    pass

            # On macOS Big Sur and up, strip Gatekeeper quarantine attributes to prevent execution block
            if platform.system() == 'Darwin' and os.path.lexists(str(java_home)):
                try:
                    subprocess.run(['xattr', '-cr', str(java_home)], capture_output=True, timeout=10)
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"Failed to extract runtime archive: {e}")
            return False
        finally:
            try:
                tmp_archive.unlink()
            except Exception:
                pass
                
        java_exec = get_java_executable_in_home(java_home)
        if java_exec.exists() and verify_java_installation(str(java_exec), expected_major_version=major_version):
            logger.info(f"[OK] Java runtime {component} downloaded successfully!")
            return True
        else:
            logger.error("Java runtime extracted but verification failed!")
            return False
            
    except Exception as e:
        logger.error(f"Failed to download Java runtime: {e}")
        return False


_VERIFIED_JAVA_DISK_CACHE = ROOT_DIR / ".cache" / "verified_java.json"
_verified_java_mem_cache = {}


def _load_verified_java_cache() -> Dict:
    try:
        if _VERIFIED_JAVA_DISK_CACHE.exists():
            with open(_VERIFIED_JAVA_DISK_CACHE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_verified_java_cache(cache_data: Dict):
    try:
        _VERIFIED_JAVA_DISK_CACHE.parent.mkdir(parents=True, exist_ok=True)
        with open(_VERIFIED_JAVA_DISK_CACHE, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, indent=2)
    except Exception:
        pass


def parse_java_major_version(ver_str: str) -> Optional[int]:
    """Parse major version number from java -version output or version string (e.g. '1.8.0_351' -> 8, '25.0.5' -> 25)."""
    try:
        clean = ver_str.strip('"\'; \r\n')
        parts = clean.split('.')
        if parts:
            if parts[0] == '1' and len(parts) > 1:
                return int(parts[1])
            return int(parts[0])
    except Exception:
        pass
    return None


def get_java_version_details(java_exec: str) -> Optional[Dict]:
    """Query java executable and return {'major_version': int, 'version_str': str} with disk mtime caching."""
    p = Path(java_exec)
    if not p.exists():
        return None
    try:
        mtime = p.stat().st_mtime
        key = str(p.resolve())
        if key in _verified_java_mem_cache and _verified_java_mem_cache[key].get("mtime") == mtime:
            return _verified_java_mem_cache[key]

        disk_cache = _load_verified_java_cache()
        if key in disk_cache and disk_cache[key].get("mtime") == mtime:
            _verified_java_mem_cache[key] = disk_cache[key]
            return disk_cache[key]

        result = subprocess.run(
            [str(p.resolve()), '-version'],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
        )
        if result.returncode == 0:
            lines = (result.stderr or result.stdout or "").splitlines()
            ver_str = "unknown"
            for line in lines:
                if "version" in line.lower():
                    spl = line.split()
                    if len(spl) >= 3:
                        ver_str = spl[2].strip('"')
                        break
            major = parse_java_major_version(ver_str)
            entry = {
                "mtime": mtime,
                "version_str": ver_str,
                "major_version": major
            }
            _verified_java_mem_cache[key] = entry
            disk_cache[key] = entry
            _save_verified_java_cache(disk_cache)
            return entry
    except Exception:
        pass
    return None


def verify_java_installation(java_path: Optional[str] = None, expected_major_version: Optional[int] = None) -> bool:
    """Verify Java installation (using fast persistent mtime caching)."""
    try:
        if java_path is None:
            java_path = 'java'
        p = Path(java_path)
        if p.is_file():
            details = get_java_version_details(str(p))
            if details and details.get("major_version"):
                if expected_major_version is not None:
                    if not is_java_version_compatible(details["major_version"], expected_major_version):
                        return False
                logger.info(f"[OK] Java found: \"{details.get('version_str')}\"")
                return True

        # Fallback for system PATH 'java'
        result = subprocess.run(
            [java_path, '-version'],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
        )
        if result.returncode == 0:
            lines = (result.stderr or result.stdout or "").splitlines()
            ver_str = "unknown"
            for line in lines:
                if "version" in line.lower():
                    spl = line.split()
                    if len(spl) >= 3:
                        ver_str = spl[2].strip('"')
                        break
            major = parse_java_major_version(ver_str)
            if expected_major_version is not None:
                if not major or not is_java_version_compatible(major, expected_major_version):
                    return False
            logger.info(f"[OK] Java found: \"{ver_str}\"")
            return True
        return False
    except Exception as e:
        logger.error(f"Failed to verify Java: {e}")
        return False


def find_existing_java_runtime(component: str, major_version: int) -> Optional[Path]:
    """
    Search for an already downloaded and verified Java runtime matching the requested component / major version.
    Looks across:
    1. Active profile java directory
    2. Default java directory (minecraft_downloads/java)
    3. Global .cache/java
    4. Any other profiles configured in launcher_config.json
    5. System Java (JAVA_HOME, Program Files)
    """
    exe_name = 'java.exe' if platform.system() == 'Windows' else 'java'
    candidate_homes = []

    # 1. Configured java dir
    try:
        candidate_homes.append(get_dir("java") / component)
    except Exception:
        pass

    # 2. Default java dir
    candidate_homes.append(ROOT_DIR / "minecraft_downloads" / "java" / component)

    # 3. .cache/java
    candidate_homes.append(ROOT_DIR / ".cache" / "java" / component)

    # 4. Other profiles in config
    try:
        from utils import load_launcher_config
        config = load_launcher_config()
        for prof in config.get("profiles", {}).values():
            if isinstance(prof, dict):
                if "java" in prof:
                    candidate_homes.append(Path(prof["java"]) / component)
                if "base" in prof:
                    candidate_homes.append(Path(prof["base"]) / "java" / component)
    except Exception:
        pass

    # Check candidate homes
    seen = set()
    for home in candidate_homes:
        try:
            resolved = str(home.resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
        except Exception:
            pass

        if home.exists() and home.is_dir():
            java_exec = get_java_executable_in_home(home)
            if java_exec.exists() and verify_java_installation(str(java_exec), expected_major_version=major_version):
                return java_exec

    # 5. Check JAVA_HOME
    java_home_env = os.environ.get("JAVA_HOME")
    if java_home_env:
        jh = Path(java_home_env)
        j_bin = get_java_executable_in_home(jh)
        if j_bin.exists() and verify_java_installation(str(j_bin), expected_major_version=major_version):
            return j_bin

    # 6. Check common Windows JDK/JRE directories
    if platform.system() == 'Windows':
        pfiles = os.environ.get("ProgramFiles", r"C:\Program Files")
        pfiles86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        common_roots = [
            Path(pfiles) / "Java",
            Path(pfiles) / "Eclipse Adoptium",
            Path(pfiles) / "Microsoft",
            Path(pfiles) / "BellSoft",
            Path(pfiles) / "Amazon Corretto",
            Path(pfiles86) / "Java",
            Path(pfiles86) / "Eclipse Adoptium",
        ]
        for c_root in common_roots:
            if c_root.exists():
                for sub in c_root.iterdir():
                    if sub.is_dir():
                        j_bin = get_java_executable_in_home(sub)
                        if j_bin.exists() and verify_java_installation(str(j_bin), expected_major_version=major_version):
                            return j_bin

    # 7. Check common macOS JDK directories (Big Sur 11.0+)
    if platform.system() == 'Darwin':
        mac_roots = [
            Path("/Library/Java/JavaVirtualMachines"),
            Path.home() / "Library/Java/JavaVirtualMachines",
            Path("/System/Library/Java/JavaVirtualMachines"),
            Path("/opt/homebrew/opt"),
            Path("/usr/local/opt"),
        ]
        for m_root in mac_roots:
            if m_root.exists():
                try:
                    for sub in m_root.iterdir():
                        if sub.is_dir():
                            j_bin = get_java_executable_in_home(sub)
                            if j_bin.exists() and verify_java_installation(str(j_bin), expected_major_version=major_version):
                                return j_bin
                except Exception:
                    pass

        # Try macOS native /usr/libexec/java_home tool
        try:
            res = subprocess.run(
                ["/usr/libexec/java_home", "-v", str(major_version)],
                capture_output=True,
                text=True,
                timeout=5
            )
            if res.returncode == 0 and res.stdout.strip():
                jh = Path(res.stdout.strip())
                j_bin = get_java_executable_in_home(jh)
                if j_bin.exists() and verify_java_installation(str(j_bin), expected_major_version=major_version):
                    return j_bin
        except Exception:
            pass

    return None


def ensure_java_for_version(version_id: str, json_mode: bool = True, custom_dir: Optional[str] = None) -> bool:
    """In-process entry point to verify or download Java for a Minecraft version."""
    global json_output
    json_output = json_mode
    if custom_dir:
        set_download_dir(custom_dir)

    logger.info(f"Preparing Java download for Minecraft {version_id}...")
    java_info = get_java_version_info(version_id)
    if not java_info:
        logger.error("[FAILURE] Could not determine Java version")
        return False

    component = java_info.get('component')
    logger.info(f"[INFO] Java version {java_info.get('major_version')} required")
    logger.info(f"[INFO] Component: {component}")

    if download_java_runtime(java_info.get('major_version'), component, get_dir("java") if custom_dir else None):
        logger.info("[SUCCESS] Java runtime ready")
        return True
    else:
        logger.error("[FAILURE] Failed to download Java runtime")
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
