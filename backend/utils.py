"""Shared utilities for Minecraft launcher scripts."""

import base64
import json
import os
from pathlib import Path
import requests
import requests.adapters

MINECRAFT_VERSION_MANIFEST_URL = "https://launchermeta.mojang.com/mc/game/version_manifest.json"

def get_root_dir() -> Path:
    p = Path(__file__).resolve().parent
    if p.name == "backend":
        return p.parent
    return p

ROOT_DIR = get_root_dir()
DOWNLOAD_DIR = ROOT_DIR / "minecraft_downloads"

def get_image_path(rel_path: str) -> str:
    """Resolve an image path from the 'images' folder (with fallback to 'files')."""
    p = ROOT_DIR / "images" / rel_path
    if p.exists():
        return str(p)
    p_files = ROOT_DIR / "files" / rel_path
    if p_files.exists():
        return str(p_files)
    return str(p)

# Shared HTTP session with connection pooling to avoid repeated TCP/TLS handshakes across steps
http_session = requests.Session()
_http_adapter = requests.adapters.HTTPAdapter(pool_connections=32, pool_maxsize=32)
http_session.mount("https://", _http_adapter)
http_session.mount("http://", _http_adapter)

# In-memory caches to avoid redundant disk reads and network fetches across steps
_manifest_memory_cache = None
_manifest_memory_time = 0.0
_version_json_memory_cache = {}
_launcher_config_cache = None
_launcher_config_mtime = -1.0

# Global directory override
_custom_download_dir = None

def set_download_dir(custom_dir):
    """Set a custom download directory (use before calling ensure_download_dir)."""
    global _custom_download_dir
    if custom_dir:
        _custom_download_dir = Path(custom_dir)
    else:
        _custom_download_dir = None

def get_download_dir():
    """Get the current download directory (custom or default)."""
    if _custom_download_dir:
        return _custom_download_dir
    return DOWNLOAD_DIR

def get_dir(name: str, profile_name: str = None) -> Path:
    """Get the path for a specific directory type from config or defaults."""
    if _custom_download_dir and not profile_name:
        base = _custom_download_dir
        if name == 'base': return base
        return base / name
        
    config = load_launcher_config()
    
    active = profile_name or config.get("active_profile", "Default")
    profiles = config.get("profiles", {})
    
    if active in profiles:
        dirs = profiles[active]
    else:
        dirs = config.get("directories", {})
        
    # Resolve base
    if "base" in dirs:
        b = Path(dirs["base"])
        base = b if b.is_absolute() else ROOT_DIR / b
    else:
        base = get_download_dir()
        
    if name == 'base': return base
    
    if name in dirs:
        sub = Path(dirs[name])
        return sub if sub.is_absolute() else ROOT_DIR / sub
    
    return base / name


def ensure_download_dir():
    """Create download directory structure if it doesn't exist."""
    get_download_dir().mkdir(parents=True, exist_ok=True)
    get_dir("versions").mkdir(parents=True, exist_ok=True)
    get_dir("assets").mkdir(parents=True, exist_ok=True)
    get_dir("indexes").mkdir(parents=True, exist_ok=True)
    get_dir("libraries").mkdir(parents=True, exist_ok=True)
    get_dir("java").mkdir(parents=True, exist_ok=True)

def fetch_version_manifest(use_cache=True):
    """Fetch the version manifest from Mojang's launcher (with in-memory + local disk cache for fast startup)."""
    import time
    global _manifest_memory_cache, _manifest_memory_time
    now = time.time()
    if use_cache and _manifest_memory_cache is not None and (now - _manifest_memory_time) < 1800:
        return _manifest_memory_cache

    cache_file = ROOT_DIR / ".cache" / "version_manifest.json"
    if use_cache and cache_file.exists():
        try:
            # If cached within the last 30 minutes, use it immediately without blocking startup
            if (now - cache_file.stat().st_mtime) < 1800:
                with open(cache_file, "r", encoding="utf-8") as f:
                    _manifest_memory_cache = json.load(f)
                    _manifest_memory_time = now
                    return _manifest_memory_cache
        except Exception:
            pass
    try:
        response = http_session.get(MINECRAFT_VERSION_MANIFEST_URL, timeout=5)
        response.raise_for_status()
        data = response.json()
        _manifest_memory_cache = data
        _manifest_memory_time = now
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception:
            pass
        return data
    except requests.RequestException as e:
        if cache_file.exists():
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    _manifest_memory_cache = json.load(f)
                    _manifest_memory_time = now
                    return _manifest_memory_cache
            except Exception:
                pass
        print(f"Error fetching version manifest: {e}")
        return None

def get_available_versions():
    """Get list of all available Minecraft versions with their types and release dates."""
    manifest = fetch_version_manifest()
    if not manifest:
        return []
    
    versions = []
    seen = set()
    if 'versions' in manifest:
        for version in manifest['versions']:
            version_id = version.get("id", "Unknown")
            version_type = version.get("type", "unknown")
            release_time = version.get("releaseTime", "Unknown")
            # Format release time (e.g., "2024-08-08T12:00:00+00:00" -> "2024-08-08")
            if "T" in release_time:
                release_date = release_time.split("T")[0]
            else:
                release_date = release_time
                
            if version_id not in seen:
                versions.append((version_id, version_type, release_date))
                seen.add(version_id)
    
    # Return in the original chronological order provided by Mojang
    return versions

def get_version_info(version_id):
    """Get detailed info for a specific version."""
    manifest = fetch_version_manifest()
    if not manifest:
        return None
    
    if 'versions' in manifest:
        for version in manifest['versions']:
            if version.get("id") == version_id:
                return version
    
    return None

def download_file(url, file_path, expected_sha1=None, show_progress=True, json_output=False):
    """Download a file and optionally verify its SHA-1 hash with retries."""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = http_session.get(url, timeout=30, stream=True)
            response.raise_for_status()

            total_size = int(response.headers.get('content-length', 0))
            downloaded = 0
            last_percent = -1

            with open(file_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if show_progress and total_size:
                            percent = (downloaded / total_size) * 100
                            if json_output:
                                if percent - last_percent >= 1.0 or percent == 100:
                                    import json
                                    print(json.dumps({"type": "progress", "progress": percent, "status": f"Downloading: {percent:.1f}%"}), flush=True)
                                    last_percent = percent
                            else:
                                print(f"\rDownloading: {percent:.1f}%", end="")

            if show_progress and total_size:
                if not json_output:
                    print("\rDownload completed!      ")

            if expected_sha1:
                if not verify_sha1(file_path, expected_sha1):
                    if attempt < max_retries - 1:
                        import time
                        time.sleep(1)
                        continue
                    try:
                        file_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    return False
            return True

        except requests.RequestException as e:
            if attempt < max_retries - 1:
                import time
                time.sleep(1)
                continue
            print(f"Error downloading {url}: {e}")
            try:
                file_path.unlink(missing_ok=True)
            except Exception:
                pass
            return False

def verify_sha1(file_path, expected_sha1):
    """Verify SHA-1 hash of a file using fast C-level file_digest when available."""
    import hashlib
    
    try:
        with open(file_path, 'rb') as f:
            if hasattr(hashlib, 'file_digest'):
                actual_sha1 = hashlib.file_digest(f, 'sha1').hexdigest()
            else:
                sha1_hash = hashlib.sha1()
                for chunk in iter(lambda: f.read(131072), b""):
                    sha1_hash.update(chunk)
                actual_sha1 = sha1_hash.hexdigest()
        
        if actual_sha1.lower() == expected_sha1.lower():
            return True
        else:
            print(f"SHA-1 mismatch! Expected: {expected_sha1}, Got: {actual_sha1}")
            return False
    except Exception as e:
        print(f"Error verifying {file_path}: {e}")
        return False

def get_version_json(version_id):
    """Download and return the version.json for a specific version (with in-memory and disk cache)."""
    import json
    from pathlib import Path
    
    v_dir = get_dir("versions")
    b_dir = get_dir("base")
    local_path = v_dir / version_id / f'{version_id}.json'
    cache_key = (str(local_path), version_id)
    if cache_key in _version_json_memory_cache:
        return _version_json_memory_cache[cache_key]

    # Try to load from local disk cache first across standard candidate locations
    for candidate_path in [
        local_path,
        v_dir / f'{version_id}.json',
        b_dir / 'versions' / version_id / f'{version_id}.json',
        b_dir / f'{version_id}.json',
    ]:
        if candidate_path.exists():
            try:
                with open(candidate_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        _version_json_memory_cache[cache_key] = data
                        return data
            except Exception:
                pass

    version_info = get_version_info(version_id)
    if not version_info or 'url' not in version_info:
        print(f"Version {version_id} not found or has no URL")
        return None
    
    try:
        response = http_session.get(version_info['url'], timeout=10)
        response.raise_for_status()
        data = response.json()
        _version_json_memory_cache[cache_key] = data
        
        # Save to local cache
        try:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            with open(local_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"Failed to cache version JSON: {e}")
            
        return data
    except requests.RequestException as e:
        print(f"Error fetching version JSON for {version_id}: {e}")
        return None

def load_launcher_config():
    """Load the launcher config with in-memory mtime caching."""
    import json
    global _launcher_config_cache, _launcher_config_mtime
    config_path = ROOT_DIR / "launcher_config.json"
    if config_path.exists():
        try:
            mtime = config_path.stat().st_mtime
            if _launcher_config_cache is not None and mtime == _launcher_config_mtime:
                return dict(_launcher_config_cache)
            with open(config_path, "r") as f:
                _launcher_config_cache = json.load(f)
                _launcher_config_mtime = mtime
                return dict(_launcher_config_cache)
        except Exception:
            pass
    return {}

def save_launcher_config(config):
    """Save the launcher config and update in-memory cache."""
    import json
    global _launcher_config_cache, _launcher_config_mtime
    config_path = ROOT_DIR / "launcher_config.json"
    with open(config_path, "w") as f:
        json.dump(config, f, indent=4)
    try:
        _launcher_config_cache = dict(config)
        _launcher_config_mtime = config_path.stat().st_mtime
    except Exception:
        _launcher_config_cache = None
        _launcher_config_mtime = -1.0

def sync_existing_version_configs(profile_name: str = None, downloaded_versions=None):
    """
    Update existing versions/<version>/command_config.json and launcher_config.json
    when loading or switching to a directory that already has downloaded versions.
    """
    try:
        v_dir = get_dir("versions", profile_name=profile_name)
        b_dir = get_dir("base", profile_name=profile_name)
        a_dir = get_dir("assets", profile_name=profile_name)
        l_dir = get_dir("libraries", profile_name=profile_name)
        j_dir = get_dir("java", profile_name=profile_name)

        if downloaded_versions is not None and v_dir.exists():
            base_str = str(b_dir.resolve())
            assets_str = str(a_dir.resolve())
            libs_str = str(l_dir.resolve())
            java_str = str(j_dir.resolve())

            for v_id in downloaded_versions:
                cfg_path = v_dir / v_id / "command_config.json"
                if not cfg_path.exists():
                    continue
                try:
                    with open(cfg_path, "r", encoding="utf-8") as f:
                        cfg = json.load(f)
                    if not isinstance(cfg, dict):
                        continue

                    changed = False
                    old_base = cfg.get("minecraft_downloads") or cfg.get("game_directory")

                    for k, new_val in (
                        ("minecraft_downloads", base_str),
                        ("game_directory", base_str),
                        ("game_assets", assets_str),
                        ("assets_root", assets_str),
                        ("natives_directory", str((l_dir / "natives" / v_id).resolve())),
                    ):
                        if cfg.get(k) != new_val:
                            cfg[k] = new_val
                            changed = True

                    if old_base and isinstance(old_base, str) and old_base != base_str:
                        if isinstance(cfg.get("java_path"), str) and old_base in cfg["java_path"]:
                            cfg["java_path"] = cfg["java_path"].replace(old_base, base_str)
                            changed = True
                        if isinstance(cfg.get("classpath"), str) and old_base in cfg["classpath"]:
                            cfg["classpath"] = cfg["classpath"].replace(old_base, base_str)
                            changed = True

                    # Remove any legacy raw tokens from command_config.json if present
                    for secret_key in ("auth_access_token", "auth_session", "session"):
                        if secret_key in cfg:
                            del cfg[secret_key]
                            changed = True

                    if changed:
                        with open(cfg_path, "w", encoding="utf-8") as f:
                            json.dump(cfg, f, indent=2)
                except Exception:
                    pass

        if downloaded_versions is not None:
            config = load_launcher_config()
            dl_list = sorted(downloaded_versions)
            if config.get("downloaded_versions") != dl_list:
                config["downloaded_versions"] = dl_list
                save_launcher_config(config)
    except Exception:
        pass


def get_downloaded_versions(profile_name: str = None):
    """
    Get the set of downloaded version IDs dynamically by scanning only the versions directory:
    - Configured versions directory
    - Base / 'versions' directory
    Detects both standard Minecraft formats (<version>/<version>.jar, <version>/<version>.json)
    and PyMcLauncher formats (<version>-client.jar, <version>.json).
    """
    candidate_dirs = []
    try:
        v_dir = get_dir('versions', profile_name=profile_name)
        if v_dir.exists():
            candidate_dirs.append(v_dir)
    except Exception:
        pass

    try:
        b_dir = get_dir('base', profile_name=profile_name)
        if b_dir.exists():
            sub_v = b_dir / 'versions'
            if sub_v.exists() and sub_v not in candidate_dirs:
                candidate_dirs.append(sub_v)
    except Exception:
        pass

    downloaded = set()
    ignored_version_names = {
        "assets", "libraries", "versions", "java", "saves", "indexes", "objects",
        "source", "resources", "logs", "crash-reports", "screenshots", "shaderpacks",
        "resourcepacks", "mods", "config", ".cache", "bin", "natives", "virtual",
        "legacy", "pre-1.6", "runtime", "jre-legacy", "java-runtime-alpha",
        "java-runtime-beta", "java-runtime-gamma", "java-runtime-delta", "java-runtime-epsilon",
        "downloads", "telemetry", "server-resource-packs", "debug", "backups",
        "datapacks", "schematics", "custom_skins", "capes", "realms_persistence"
    }
    ignored_json = {
        "command_config.json", "launcher_config.json", "directories.json", 
        "quickPlay.json", "verified_java.json", "version_manifest.json", 
        "version_manifest_v2.json", "launcher_profiles.json", "profile.json", 
        "usercache.json", "usernamecache.json", "realms_persistence.json",
        "treatment_tags.json", "telemetry_info.json", "launcher_accounts.json",
        "whitelist.json", "ops.json", "banned-players.json", "banned-ips.json", "log.json"
    }

    def _is_valid_version_json(json_path: Path, expected_id: str = None) -> bool:
        """Verify that a .json file is actually a Minecraft version manifest and not game/launcher data."""
        try:
            if not json_path.is_file() or json_path.stat().st_size == 0 or json_path.stat().st_size > 5_000_000:
                return False
            with open(json_path, "r", encoding="utf-8", errors="ignore") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return False
            if expected_id and data.get("id") and str(data.get("id")) != expected_id:
                return False
            version_keys = {"mainClass", "libraries", "inheritsFrom", "arguments", "minecraftArguments", "assetIndex"}
            return bool(version_keys & data.keys())
        except Exception:
            return False

    def _is_nonempty_file(p: Path) -> bool:
        try:
            return p.is_file() and p.stat().st_size > 0
        except Exception:
            return False

    for search_dir in candidate_dirs:
        try:
            for item in search_dir.iterdir():
                if item.is_dir():
                    v_name = item.name
                    if v_name.lower() in ignored_version_names:
                        continue
                    has_json = _is_valid_version_json(item / f"{v_name}.json", expected_id=v_name)
                    has_jar = (
                        _is_nonempty_file(item / f"{v_name}.jar")
                        or _is_nonempty_file(item / f"{v_name}-client.jar")
                        or _is_nonempty_file(item / "client.jar")
                        or _is_nonempty_file(search_dir / f"{v_name}-client.jar")
                        or _is_nonempty_file(search_dir / f"{v_name}.jar")
                    )
                    if has_jar or has_json:
                        downloaded.add(v_name)
                elif item.is_file():
                    name = item.name
                    if name.endswith("-client.jar") and _is_nonempty_file(item):
                        v_name = name[:-11]
                        if v_name.lower() not in ignored_version_names:
                            downloaded.add(v_name)
                    elif name.endswith(".jar") and _is_nonempty_file(item):
                        v_name = name[:-4]
                        if v_name.lower() not in ignored_version_names:
                            downloaded.add(v_name)
                    elif name.endswith(".json") and name not in ignored_json:
                        v_name = name[:-5]
                        if v_name.lower() not in ignored_version_names and _is_valid_version_json(item, expected_id=v_name):
                            downloaded.add(v_name)
        except Exception:
            pass

    sync_existing_version_configs(profile_name=profile_name, downloaded_versions=downloaded)
    return downloaded




import shutil
import time

def _to_extended_path(p: Path) -> str:
    p_str = str(p.resolve())
    if os.name == 'nt' and not p_str.startswith('\\\\?\\'):
        return '\\\\?\\' + p_str
    return p_str


def _index_and_delete_targets(targets, progress_callback=None, log_callback=None) -> bool:
    """
    Two-phase deletion designed for both SSDs and HDDs:
    Phase 1: Index all files and directories under `targets` while emitting `progress_callback(-1, ...)`
             so the UI displays a responsive indexing animation with live item count.
    Phase 2: Delete indexed files and directories bottom-up while emitting `progress_callback(0..100, ...)`
             so the UI displays an accurate progress bar.
    """
    import stat

    def _log(msg):
        if log_callback:
            log_callback(msg)
        else:
            print(msg)

    def _on_rm_error(func, path, exc_info):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except Exception:
            pass

    files_to_delete = []
    dirs_to_delete = []
    last_emit = 0.0

    if progress_callback:
        progress_callback(-1, "Indexing files to delete... (0 items found)")

    for label, target_path in targets:
        if not target_path.exists():
            continue
        if target_path.is_file() or target_path.is_symlink():
            files_to_delete.append((f"{label}/{target_path.name}", _to_extended_path(target_path)))
            now = time.monotonic()
            if progress_callback and (now - last_emit) >= 0.04:
                total_found = len(files_to_delete) + len(dirs_to_delete)
                progress_callback(-1, f"Indexing {label}... ({total_found} items found)")
                last_emit = now
        elif target_path.is_dir():
            ext_root = _to_extended_path(target_path)
            for root, dirs, files in os.walk(ext_root, topdown=False):
                for fname in files:
                    fpath = os.path.join(root, fname)
                    files_to_delete.append((fname, fpath))
                    now = time.monotonic()
                    if progress_callback and (now - last_emit) >= 0.04:
                        total_found = len(files_to_delete) + len(dirs_to_delete)
                        progress_callback(-1, f"Indexing {label}... ({total_found} items found)")
                        last_emit = now
                dirs_to_delete.append((os.path.basename(root) or label, root))
                now = time.monotonic()
                if progress_callback and (now - last_emit) >= 0.04:
                    total_found = len(files_to_delete) + len(dirs_to_delete)
                    progress_callback(-1, f"Indexing {label}... ({total_found} items found)")
                    last_emit = now

    total_items = len(files_to_delete) + len(dirs_to_delete)
    if total_items == 0:
        if progress_callback:
            progress_callback(100, "Nothing to delete.")
        return True

    if progress_callback:
        progress_callback(0, f"Deleting 0/{total_items} items (0.0%)...")

    done = 0
    last_emit = 0.0

    # Delete all files first
    for disp_name, fpath in files_to_delete:
        try:
            os.unlink(fpath)
        except PermissionError:
            try:
                os.chmod(fpath, stat.S_IWRITE)
                os.unlink(fpath)
            except Exception:
                pass
        except FileNotFoundError:
            pass
        except Exception:
            pass

        done += 1
        now = time.monotonic()
        if progress_callback and ((now - last_emit) >= 0.035 or done == total_items):
            pct = (done / total_items) * 100.0
            short_name = disp_name[-40:] if len(disp_name) > 40 else disp_name
            progress_callback(pct, f"Deleting ({done}/{total_items} - {pct:.1f}%): {short_name}")
            last_emit = now

    # Delete directories bottom-up
    for disp_name, dpath in dirs_to_delete:
        try:
            os.rmdir(dpath)
        except PermissionError:
            try:
                os.chmod(dpath, stat.S_IWRITE)
                os.rmdir(dpath)
            except Exception:
                try:
                    shutil.rmtree(dpath, onerror=_on_rm_error)
                except Exception:
                    pass
        except FileNotFoundError:
            pass
        except OSError:
            try:
                shutil.rmtree(dpath, onerror=_on_rm_error)
            except Exception:
                pass

        done += 1
        now = time.monotonic()
        if progress_callback and ((now - last_emit) >= 0.035 or done == total_items):
            pct = (done / total_items) * 100.0
            short_name = disp_name[-40:] if len(disp_name) > 40 else disp_name
            progress_callback(pct, f"Deleting ({done}/{total_items} - {pct:.1f}%): {short_name}")
            last_emit = now

    if progress_callback:
        progress_callback(100, f"Deleted {total_items} items.")
    return True


def delete_version_files(version_id, log_callback=None, progress_callback=None):
    """Delete the downloaded version files with indexing and progress reporting."""
    def log(msg):
        if log_callback:
            log_callback(msg)
        else:
            print(msg)

    jar_path = get_dir("versions") / f"{version_id}-client.jar"
    json_path = get_dir("versions") / f"{version_id}.json"
    version_dir = get_dir("versions") / version_id

    targets = []
    for p in (jar_path, json_path, version_dir):
        if p.exists():
            targets.append((p.name, p))

    try:
        ok = _index_and_delete_targets(targets, progress_callback=progress_callback, log_callback=log_callback)
        for _, p in targets:
            if not p.exists():
                log(f"Deleted: {p}")
        return ok
    except Exception as e:
        log(f"Error deleting version files for {version_id}: {e}")
        return False


def has_any_downloads():
    for folder_key in ("versions", "libraries", "java", "indexes"):
        d = get_dir(folder_key)
        if d.exists() and any(d.iterdir()):
            return True
    assets_dir = get_dir("assets")
    if assets_dir.exists():
        for sub in ("objects", "indexes", "virtual"):
            sd = assets_dir / sub
            if sd.exists() and any(sd.iterdir()):
                return True
    return False


def has_any_source():
    source_dir = get_dir("source")
    if source_dir.exists() and any(source_dir.iterdir()):
        return True
    mappings_dir = get_dir("base") / "mappings"
    if mappings_dir.exists() and any(mappings_dir.iterdir()):
        return True
    assets_dir = get_dir("assets")
    reserved_assets = {"indexes", "objects", "virtual", "skins"}
    if assets_dir.exists():
        for item in assets_dir.iterdir():
            if item.name not in reserved_assets:
                return True
    return False


def delete_source_folder(progress_callback=None, log_callback=None):
    import subprocess
    try:
        # Kill any lingering CFR decompiler java processes that might be locking the source folder
        if os.name == 'nt':
            try:
                subprocess.run(
                    ['powershell', '-NoProfile', '-Command',
                     "Get-CimInstance Win32_Process -Filter \"Name = 'java.exe'\" | Where-Object { $_.CommandLine -like '*cfr-*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5
                )
            except Exception:
                pass

        targets = [
            ("source", get_dir("source")),
            ("mappings", get_dir("base") / "mappings")
        ]

        # Also include unpacked dev-mode assets inside assets_dir (e.g., assets/icons, assets/minecraft, assets/pack.mcmeta)
        assets_dir = get_dir("assets")
        reserved_assets = {"indexes", "objects", "virtual", "skins"}
        if assets_dir.exists():
            for item in list(assets_dir.iterdir()):
                if item.name not in reserved_assets:
                    targets.append((f"assets/{item.name}", item))

        return _index_and_delete_targets(targets, progress_callback=progress_callback, log_callback=log_callback)
    except Exception as e:
        print(f"Error deleting source folder: {e}")
        return False


def delete_all_downloads(progress_callback=None, log_callback=None):
    try:
        # Delete downloaded versions, assets, indexes, libraries, java, and resources without touching saves/worlds
        targets = [
            ("versions", get_dir("versions")),
            ("assets", get_dir("assets")),
            ("indexes", get_dir("indexes")),
            ("libraries", get_dir("libraries")),
            ("java", get_dir("java")),
            ("resources", get_dir("base") / "resources")
        ]
        return _index_and_delete_targets(targets, progress_callback=progress_callback, log_callback=log_callback)
    except Exception:
        return False


def clear_texture_cache():
    import shutil
    import stat
    import os
    def on_rm_error(func, path, exc_info):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except Exception:
            pass
    cache_root = ROOT_DIR / ".cache"
    if cache_root.exists():
        for item in list(cache_root.iterdir()):
            if item.is_dir():
                for fname in ("skin.png", "cape.png", "gamerpic.png", "skin_viewer.html"):
                    fpath = item / fname
                    if fpath.exists():
                        try:
                            fpath.unlink()
                        except Exception:
                            pass

def calculate_locator_bar_color(mc_uuid_hex: str) -> str:
    """
    Calculate the vanilla Minecraft Locator Bar waypoint color from a player's UUID:
    ARGB.setBrightness(ARGB.color(255, uuid.hashCode()), 0.9F)
    """
    import colorsys
    try:
        clean_hex = str(mc_uuid_hex or "").replace("-", "").strip()
        if len(clean_hex) < 32 or clean_hex.startswith("00000000"):
            return "#FFFFFF"
        most = int(clean_hex[:16], 16)
        least = int(clean_hex[16:32], 16)
        hilo = most ^ least
        hash_val = ((hilo >> 32) ^ hilo) & 0xFFFFFFFF
        raw_color = hash_val & 0xFFFFFF
        r = ((raw_color >> 16) & 0xFF) / 255.0
        g = ((raw_color >> 8) & 0xFF) / 255.0
        b = (raw_color & 0xFF) / 255.0
        h, s, _ = colorsys.rgb_to_hsv(r, g, b)
        nr, ng, nb = colorsys.hsv_to_rgb(h, s, 0.9)
        return f"#{int(round(nr * 255)):02X}{int(round(ng * 255)):02X}{int(round(nb * 255)):02X}"
    except Exception:
        return "#FFFFFF"


def _deduplicate_accounts(accounts: dict, prefer_pid: str = None) -> tuple[dict, bool]:
    """
    Ensure accounts['profiles'] has at most one entry per Minecraft/Xbox account
    (matched by mc_uuid, xbox_xuid, or case-insensitive mc_username).
    Returns (accounts, was_modified).
    """
    if not isinstance(accounts, dict):
        return {}, False
    profiles = accounts.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        return accounts, False

    modified = False
    for pid, pdata in profiles.items():
        if isinstance(pdata, dict) and pdata.get("mc_uuid"):
            expected_color = calculate_locator_bar_color(pdata.get("mc_uuid"))
            if expected_color != "#FFFFFF" and pdata.get("locator_bar_color") != expected_color:
                pdata["locator_bar_color"] = expected_color
                modified = True

    if len(profiles) <= 1:
        return accounts, modified

    import shutil
    import stat

    def _on_rm_error(func, path, exc_info):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except Exception:
            pass

    def _account_keys(pdata: dict):
        if not isinstance(pdata, dict):
            return []
        keys = []
        mc_uuid = str(pdata.get("mc_uuid") or "").strip().replace("-", "").lower()
        if mc_uuid and mc_uuid != "0" and not mc_uuid.startswith("00000000"):
            keys.append(("uuid", mc_uuid))
        xuid = str(pdata.get("xbox_xuid") or "").strip().lower()
        if xuid and xuid not in ("0", "offline"):
            keys.append(("xuid", xuid))
        uname = str(pdata.get("mc_username") or "").strip().lower()
        if uname and uname not in ("unknown", "offlineplayer", "player"):
            keys.append(("uname", uname))
        return keys

    active_id = prefer_pid or accounts.get("active_profile")

    # Group profile IDs that represent the same account (preserving insertion order so newest is last)
    groups = []  # list of [pids_list, latest_pdata, key_set]
    for pid, pdata in list(profiles.items()):
        akeys = set(_account_keys(pdata))
        matched_group = None
        if akeys:
            for grp in groups:
                if akeys & grp[2]:
                    matched_group = grp
                    break
        if matched_group is not None:
            matched_group[0].append(pid)
            matched_group[1] = pdata  # Later entry has fresher auth data/tokens
            matched_group[2].update(akeys)
            modified = True
        else:
            groups.append([[pid], pdata, set(akeys)])

    if not modified:
        return accounts, False

    new_profiles = {}
    for pid_list, latest_data, _ in groups:
        if len(pid_list) == 1:
            new_profiles[pid_list[0]] = latest_data
            continue

        # Prefer the newly-authenticated prefer_pid, otherwise the newest pid (last in pid_list)
        if prefer_pid and prefer_pid in pid_list:
            kept_pid = prefer_pid
        else:
            kept_pid = pid_list[-1]

        new_profiles[kept_pid] = latest_data
        kept_cache = ROOT_DIR / ".cache" / kept_pid

        for dup_pid in pid_list:
            if dup_pid == kept_pid:
                continue
            if active_id == dup_pid:
                active_id = kept_pid
            dup_cache = ROOT_DIR / ".cache" / dup_pid
            if dup_cache.exists():
                try:
                    kept_cache.mkdir(parents=True, exist_ok=True)
                    for item in dup_cache.iterdir():
                        dest = kept_cache / item.name
                        if not dest.exists():
                            if item.is_dir():
                                shutil.copytree(item, dest, dirs_exist_ok=True)
                            else:
                                shutil.copy2(item, dest)
                    shutil.rmtree(dup_cache, onerror=_on_rm_error)
                except Exception:
                    pass

    accounts["profiles"] = new_profiles
    if active_id and active_id in new_profiles:
        accounts["active_profile"] = active_id
    elif new_profiles:
        accounts["active_profile"] = next(iter(new_profiles.keys()))
    else:
        accounts["active_profile"] = None

    return accounts, True


def load_accounts():
    config = load_launcher_config()
    accounts = config.get("accounts", {})
    if not isinstance(accounts, dict):
        return {}
    accounts, modified = _deduplicate_accounts(accounts)
    if modified:
        config["accounts"] = accounts
        save_launcher_config(config)
    return accounts


def save_accounts(accounts):
    accounts, _ = _deduplicate_accounts(accounts)
    config = load_launcher_config()
    config["accounts"] = accounts
    save_launcher_config(config)


def get_active_profile_id(force_new=False):
    import uuid
    accounts = load_accounts()
    if force_new or not accounts.get("active_profile"):
        new_id = str(uuid.uuid4())
        accounts["active_profile"] = new_id
        save_accounts(accounts)
        return new_id
    return accounts.get("active_profile")


import time
import uuid

def get_auth_cache():
    accounts = load_accounts()
    active_id = accounts.get("active_profile")
    if active_id and active_id in accounts.get("profiles", {}):
        return accounts["profiles"][active_id]
    return None

def clear_auth_cache(all_accounts=True):
    import shutil
    import stat
    import os
    def on_rm_error(func, path, exc_info):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except Exception:
            pass
    accounts = load_accounts()
    if all_accounts:
        for pid in list(accounts.get("profiles", {}).keys()):
            p_dir = ROOT_DIR / ".cache" / pid
            if p_dir.exists():
                shutil.rmtree(p_dir, onerror=on_rm_error)
        accounts["profiles"] = {}
        accounts["active_profile"] = None
        save_accounts(accounts)
    else:
        active_id = accounts.get("active_profile")
        if active_id and active_id in accounts.get("profiles", {}):
            del accounts["profiles"][active_id]
            p_dir = ROOT_DIR / ".cache" / active_id
            if p_dir.exists():
                shutil.rmtree(p_dir, onerror=on_rm_error)
            remaining = list(accounts.get("profiles", {}).keys())
            accounts["active_profile"] = remaining[0] if remaining else None
            save_accounts(accounts)

def save_auth_cache(auth_data):
    accounts = load_accounts()
    active_id = accounts.get("active_profile")
    if not active_id:
        active_id = get_active_profile_id(force_new=True)
    if "profiles" not in accounts:
        accounts["profiles"] = {}
    accounts["profiles"][active_id] = auth_data
    accounts["active_profile"] = active_id
    accounts, _ = _deduplicate_accounts(accounts, prefer_pid=active_id)
    save_accounts(accounts)

def fetch_realms_options():
    realms = []
    try:
        auth = get_auth_cache()
        if auth and 'mc_access_token' in auth and 'mc_uuid' in auth:
            token = auth['mc_access_token']
            uuid = auth['mc_uuid']
            username = auth.get('mc_username', 'Player')
            cookies = {
                'sid': f'token:{token}:{uuid}',
                'user': username,
                'version': '1.20.4'
            }
            headers = {
                'Authorization': f'Bearer {token}',
                'User-Agent': 'Minecraft/1.20.4'
            }
            r = requests.get('https://pc.realms.minecraft.net/worlds', cookies=cookies, headers=headers, timeout=2.0)
            if r.status_code == 200:
                realms_data = r.json()
                for server in realms_data.get('servers', []):
                    expired = server.get('expired', False)
                    name = server.get('name', 'Realm')
                    r_id = server.get('id')
                    disp_str = f"Realm: {name}"
                    if expired:
                        disp_str += " (Expired)"
                    realms.append({
                        "type": "realms",
                        "id": r_id,
                        "display": disp_str,
                        "expired": expired
                    })
    except Exception:
        pass
    return realms

def get_quick_play_options(minecraft_dir=None, include_realms=True, profile_name: str = None):
    options = []
    
    # 1. Singleplayer Worlds
    saves_dir = get_dir("saves", profile_name=profile_name)
    if saves_dir.exists():
        for d in saves_dir.iterdir():
            if d.is_dir() and (d / "level.dat").exists():
                icon_file = d / "icon.png"
                options.append({
                    "type": "singleplayer",
                    "id": d.name,
                    "display": f"World: {d.name}",
                    "icon_path": str(icon_file) if icon_file.exists() else None
                })

    # 2. Multiplayer Servers
    base_dir = Path(minecraft_dir) if minecraft_dir and not profile_name else get_dir("base", profile_name=profile_name)
    servers_dat = base_dir / "servers.dat"
    if servers_dat.exists():
        try:
            import nbtlib
            nbt_file = nbtlib.load(str(servers_dat))
            if 'servers' in nbt_file:
                for server in nbt_file['servers']:
                    name = str(server.get('name', 'Server'))
                    ip = str(server.get('ip', ''))
                    if ip:
                        options.append({
                            "type": "multiplayer",
                            "id": ip,
                            "display": f"Server: {name} ({ip})"
                        })
        except Exception as e:
            print(f"Failed to parse servers.dat: {e}")

    # 3. Realms (optional so UI startup can load Realms asynchronously)
    if include_realms:
        options.extend(fetch_realms_options())

    return options

def fetch_player_textures(profile_id, uuid, gamerpic_url=None, progress_callback=None, force_refresh=False):
    cache_dir = ROOT_DIR / ".cache" / profile_id
    cache_dir.mkdir(parents=True, exist_ok=True)
    
    skin_path = cache_dir / "skin.png"
    cape_path = cache_dir / "cape.png"
    gamerpic_path = cache_dir / "gamerpic.png"
    html_path = cache_dir / "skin_viewer.html"
    js_path = ROOT_DIR / ".cache" / "skinview3d.bundle.js"

    need_network = force_refresh or not skin_path.exists() or (gamerpic_url and not gamerpic_path.exists())

    if need_network:
        if progress_callback: progress_callback(10, "Fetching textures...")
        for p in [skin_path, cape_path]:
            if p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass

        # Fetch Minecraft textures
        if uuid:
            try:
                resp = requests.get(f"https://sessionserver.mojang.com/session/minecraft/profile/{uuid}", timeout=4)
                if resp.status_code == 200:
                    profile = resp.json()
                    for prop in profile.get('properties', []):
                        if prop.get('name') == 'textures':
                            b64_value = prop.get('value', '')
                            if b64_value:
                                textures_json = json.loads(base64.b64decode(b64_value).decode('utf-8'))
                                textures = textures_json.get('textures', {})
                                
                                # Download Skin
                                if 'SKIN' in textures:
                                    skin_url = textures['SKIN']['url']
                                    r = requests.get(skin_url, timeout=4)
                                    if r.status_code == 200:
                                        with open(skin_path, 'wb') as f:
                                            f.write(r.content)
                                            
                                # Download Cape
                                if 'CAPE' in textures:
                                    cape_url = textures['CAPE']['url']
                                    r = requests.get(cape_url, timeout=4)
                                    if r.status_code == 200:
                                        with open(cape_path, 'wb') as f:
                                            f.write(r.content)
            except Exception as e:
                print(f"Failed to fetch Minecraft textures: {e}")

        if progress_callback: progress_callback(50, "Fetching gamerpic...")

        # Fetch Gamerpic
        if gamerpic_url and (force_refresh or not gamerpic_path.exists()):
            try:
                r = requests.get(gamerpic_url, timeout=4)
                if r.status_code == 200:
                    with open(gamerpic_path, 'wb') as f:
                        f.write(r.content)
            except Exception as e:
                print(f"Failed to fetch gamerpic: {e}")

    if not js_path.exists():
        try:
            r = requests.get("https://unpkg.com/skinview3d@3.0.0/bundles/skinview3d.bundle.js", timeout=5)
            if r.status_code == 200:
                with open(js_path, "wb") as f:
                    f.write(r.content)
        except Exception as e:
            print(f"Failed to download skinview3d: {e}")

    # Generate skin_viewer.html (always ensure latest HTML with proper null cape handling)
    html_content = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Skin Viewer</title>
    <style>
        body { margin: 0; padding: 0; background: transparent; overflow: hidden; }
        #skin_container { width: 100vw; height: 100vh; }
    </style>
</head>
<body>
<div id="skin_container"></div>
<script src="../skinview3d.bundle.js"></script>
<script>
    const urlParams = new URLSearchParams(window.location.search);
    const skin = urlParams.get('skin');
    const cape = urlParams.get('cape');
    const model = urlParams.get('model') || 'classic';
    const backEq = urlParams.get('back') || 'cape';
    
    let skinViewer = new skinview3d.SkinViewer({
        canvas: document.createElement("canvas"),
        width: window.innerWidth,
        height: window.innerHeight
    });
    if (skin) {
        skinViewer.loadSkin(skin, { model: model === 'classic' ? 'default' : model });
    }
    
    window.hasCapeLoaded = Boolean(cape);
    if (cape) {
        skinViewer.loadCape(cape, { backEquipment: backEq });
    } else {
        skinViewer.loadCape(null);
        if (skinViewer.playerObject) {
            skinViewer.playerObject.backEquipment = null;
            if (skinViewer.playerObject.cape) skinViewer.playerObject.cape.visible = false;
            if (skinViewer.playerObject.elytra) skinViewer.playerObject.elytra.visible = false;
        }
    }
    
    skinViewer.camera.position.set(-20, 10, 40);
    skinViewer.controls.enableZoom = false;
    skinViewer.controls.enablePan = false;
    document.getElementById("skin_container").appendChild(skinViewer.canvas);

    window.skinViewer = skinViewer;
</script>
</body>
</html>"""
    
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    if need_network and progress_callback:
        progress_callback(100, "Done")

    return {
        "skin": "skin.png" if skin_path.exists() else None,
        "cape": "cape.png" if cape_path.exists() else None,
        "gamerpic": str(gamerpic_path) if gamerpic_path.exists() else None,
        "html_url": str(html_path.absolute()).replace('\\', '/')
    }


def get_process_tree_pids(parent_pid: int) -> set:
    """Return parent_pid plus all descendant child PIDs on Windows."""
    if os.name != 'nt' or not parent_pid:
        return {parent_pid} if parent_pid else set()
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32

        TH32CS_SNAPPROCESS = 0x00000002
        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ('dwSize', wintypes.DWORD),
                ('cntUsage', wintypes.DWORD),
                ('th32ProcessID', wintypes.DWORD),
                ('th32DefaultHeapID', ctypes.c_size_t),
                ('th32ModuleID', wintypes.DWORD),
                ('cntThreads', wintypes.DWORD),
                ('th32ParentProcessID', wintypes.DWORD),
                ('pcPriClassBase', wintypes.LONG),
                ('dwFlags', wintypes.DWORD),
                ('szExeFile', ctypes.c_char * 260)
            ]

        h_snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if h_snap == -1 or h_snap == 0:
            return {parent_pid}

        pe = PROCESSENTRY32()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32)

        children_map = {}
        if kernel32.Process32First(h_snap, ctypes.byref(pe)):
            while True:
                p_id = int(pe.th32ProcessID)
                pp_id = int(pe.th32ParentProcessID)
                children_map.setdefault(pp_id, []).append(p_id)
                if not kernel32.Process32Next(h_snap, ctypes.byref(pe)):
                    break
        kernel32.CloseHandle(h_snap)

        all_pids = set()
        queue = [parent_pid]
        while queue:
            curr = queue.pop(0)
            all_pids.add(curr)
            for ch in children_map.get(curr, []):
                if ch not in all_pids:
                    queue.append(ch)
        return all_pids
    except Exception:
        return {parent_pid} if parent_pid else set()


def is_minecraft_window_visible(pid: int = None, version: str = None) -> bool:
    """Return True if an actual visible top-level game window exists for the given PID tree or Minecraft version."""
    if os.name != 'nt':
        return False
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32

        pids = get_process_tree_pids(pid) if pid else set()
        found = [False]

        def enum_cb(hwnd, _):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True

                length = user32.GetWindowTextLengthW(hwnd)
                title = ""
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    title = buf.value

                win_pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(win_pid))
                w_pid = int(win_pid.value)

                # If pid was provided, window must belong to that process tree
                if pids:
                    if w_pid not in pids:
                        return True
                else:
                    # No pid provided: check if title has Minecraft
                    if "Minecraft" not in title:
                        return True

                rect = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                w = rect.right - rect.left
                h = rect.bottom - rect.top
                # Normal Minecraft game window is at least 250x200
                if w >= 250 and h >= 200:
                    found[0] = True
                    return False
            except Exception:
                pass
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(WNDENUMPROC(enum_cb), 0)
        return found[0]
    except Exception:
        return False


def get_desktop_dir() -> Path:
    """Resolve the user's Windows Desktop directory (including OneDrive redirection)."""
    if os.name == 'nt':
        try:
            import ctypes
            import ctypes.wintypes
            buf = ctypes.create_unicode_buffer(ctypes.wintypes.MAX_PATH)
            # CSIDL_DESKTOPDIRECTORY = 0x0010
            ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buf)
            if buf.value:
                return Path(buf.value)
        except Exception:
            pass
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders') as key:
                val = os.path.expandvars(winreg.QueryValueEx(key, 'Desktop')[0])
                if val:
                    return Path(val)
        except Exception:
            pass
    onedrive_desktop = Path.home() / "OneDrive" / "Desktop"
    if onedrive_desktop.exists():
        return onedrive_desktop
    return Path.home() / "Desktop"


def get_start_menu_programs_dir() -> Path:
    """Resolve the user's Windows Start Menu Programs directory."""
    if os.name == 'nt':
        try:
            import ctypes
            import ctypes.wintypes
            buf = ctypes.create_unicode_buffer(ctypes.wintypes.MAX_PATH)
            # CSIDL_PROGRAMS = 0x0002
            ctypes.windll.shell32.SHGetFolderPathW(None, 0x0002, None, 0, buf)
            if buf.value:
                return Path(buf.value)
        except Exception:
            pass
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders') as key:
                val = os.path.expandvars(winreg.QueryValueEx(key, 'Programs')[0])
                if val:
                    return Path(val)
        except Exception:
            pass
    appdata = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def create_windows_shortcut(
    lnk_path: Path,
    target_exe: str,
    arguments: str,
    working_dir: str,
    icon_path: str = None,
    description: str = "",
    pin_to_start: bool = False
) -> tuple:
    """
    Create a Windows .lnk shortcut using WScript.Shell COM object via PowerShell
    and optionally invoke the 'Pin to Start' shell verb.
    Returns (success: bool, message: str).
    """
    import subprocess
    try:
        lnk_path = Path(lnk_path).resolve()
        lnk_path.parent.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["SC_LNK_PATH"] = str(lnk_path)
        env["SC_TARGET"] = str(target_exe)
        env["SC_ARGS"] = str(arguments)
        env["SC_WORKDIR"] = str(working_dir)
        env["SC_ICON"] = str(icon_path) if icon_path else ""
        env["SC_DESC"] = str(description)
        env["SC_PIN"] = "1" if pin_to_start else "0"
        env["SC_PARENT"] = str(lnk_path.parent)
        env["SC_NAME"] = str(lnk_path.name)

        ps_cmd = (
            "$ws = New-Object -ComObject WScript.Shell; "
            "$s = $ws.CreateShortcut($env:SC_LNK_PATH); "
            "$s.TargetPath = $env:SC_TARGET; "
            "$s.Arguments = $env:SC_ARGS; "
            "$s.WorkingDirectory = $env:SC_WORKDIR; "
            "if ($env:SC_ICON) { $s.IconLocation = $env:SC_ICON }; "
            "if ($env:SC_DESC) { $s.Description = $env:SC_DESC }; "
            "$s.Save(); "
            "if ($env:SC_PIN -eq '1') { "
            "  try { "
            "    $sh = New-Object -ComObject Shell.Application; "
            "    $folder = $sh.Namespace($env:SC_PARENT); "
            "    if ($folder) { "
            "      $item = $folder.ParseName($env:SC_NAME); "
            "      if ($item) { "
            "        $verb = $item.Verbs() | Where-Object { ($_.Name -replace '&','') -match 'Pin to Start' } | Select-Object -First 1; "
            "        if ($verb) { $verb.DoIt() } "
            "      } "
            "    } "
            "  } catch {} "
            "}"
        )

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=creationflags
        )
        if res.returncode == 0 and lnk_path.exists():
            return True, str(lnk_path)
        return False, (res.stderr.strip() or f"PowerShell exited with code {res.returncode}")
    except Exception as e:
        return False, str(e)

