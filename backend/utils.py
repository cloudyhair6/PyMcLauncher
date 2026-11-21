"""Shared utilities for Minecraft launcher scripts."""

import base64
from contextlib import contextmanager
import json
import os
import platform
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional
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
_http_adapter = requests.adapters.HTTPAdapter(pool_connections=64, pool_maxsize=64)
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

LINKABLE_GAME_SUBFOLDERS = ("saves", "resourcepacks", "shaderpacks", "screenshots")
_PROFILE_TLS = threading.local()


@contextmanager
def profile_context(profile_name: Optional[str]):
    """Temporarily direct get_dir(name) calls in the current thread to profile_name without mutating config."""
    prev = getattr(_PROFILE_TLS, "active_profile", None)
    _PROFILE_TLS.active_profile = profile_name
    try:
        yield
    finally:
        _PROFILE_TLS.active_profile = prev


def _resolve_profile_base(dirs: dict, active: str) -> Path:
    if isinstance(dirs, dict) and dirs.get("base"):
        b = Path(dirs["base"])
        return b if b.is_absolute() else ROOT_DIR / b
    return get_download_dir()


def get_dir(name: str, profile_name: str = None, _seen: set = None) -> Path:
    """Get the path for a specific directory type from config or defaults."""
    if _custom_download_dir and not profile_name:
        base = _custom_download_dir
        if name == 'base':
            return base
        return base / name

    config = load_launcher_config()
    active = profile_name or getattr(_PROFILE_TLS, "active_profile", None) or config.get("active_profile", "Default")
    profiles = config.get("profiles", {})

    if active in profiles:
        dirs = profiles[active]
    else:
        dirs = config.get("directories", {})
    if not isinstance(dirs, dict):
        dirs = {}

    base = _resolve_profile_base(dirs, active)
    if name == 'base':
        return base

    # Support linking saves / resourcepacks / shaderpacks / screenshots from another profile or custom folder
    if name in LINKABLE_GAME_SUBFOLDERS:
        link_mode = dirs.get(f"{name}_link_mode")
        if link_mode == "profile":
            target_prof = dirs.get(f"{name}_link_profile")
            seen = set(_seen or ())
            if target_prof and target_prof in profiles and target_prof not in seen:
                seen.add(active)
                return get_dir(name, profile_name=target_prof, _seen=seen)

    if name in dirs and str(dirs[name] or "").strip():
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


def resolve_library_file(rel_path: str, libraries_dir: Path = None, expected_sha1: str = None) -> Path:
    """Resolve a library JAR path inside `libraries_dir`, syncing from profile/base fallbacks if needed."""
    import shutil
    primary_dir = Path(libraries_dir) if libraries_dir else get_dir("libraries")
    rel = Path(str(rel_path or "").replace("\\", "/"))
    primary = primary_dir / rel
    try:
        if primary.is_file() and primary.stat().st_size > 0:
            if not expected_sha1 or verify_sha1(primary, expected_sha1):
                return primary
    except Exception:
        pass

    candidates = [
        get_dir("base") / "libraries",
        get_download_dir() / "libraries",
    ]
    try:
        cfg = load_launcher_config()
        for pname, pdirs in (cfg.get("profiles") or {}).items():
            if isinstance(pdirs, dict):
                candidates.append(_resolve_profile_base(pdirs, pname) / "libraries")
                if pdirs.get("libraries"):
                    lp = Path(pdirs["libraries"])
                    candidates.append(lp if lp.is_absolute() else ROOT_DIR / lp)
    except Exception:
        pass

    seen = set()
    try:
        seen.add(str(primary_dir.resolve()).lower())
    except Exception:
        pass
    for lib_root in candidates:
        try:
            key = str(lib_root.resolve()).lower()
        except Exception:
            key = str(lib_root).lower()
        if key in seen:
            continue
        seen.add(key)
        cand = lib_root / rel
        try:
            if cand.is_file() and cand.stat().st_size > 0:
                if expected_sha1 and not verify_sha1(cand, expected_sha1):
                    continue
                try:
                    primary.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(cand, primary)
                    if primary.is_file() and primary.stat().st_size > 0:
                        return primary
                except Exception:
                    return cand
        except Exception:
            pass
    return primary
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
    file_path = Path(file_path)
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = http_session.get(url, timeout=30, stream=True)
            response.raise_for_status()

            total_size = int(response.headers.get('content-length', 0))
            downloaded = 0
            last_percent = -1

            file_path.parent.mkdir(parents=True, exist_ok=True)
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

        except (requests.RequestException, OSError) as e:
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

def _merge_inherited_version_json(child: dict, parent: dict) -> dict:
    """Merge a child mod-loader version JSON onto its parent Mojang version JSON."""
    merged = dict(parent)
    for k, v in child.items():
        if k in ("libraries", "arguments", "inheritsFrom"):
            continue
        merged[k] = v

    # Libraries: child libraries first (so overrides/ASM/Mixin precede vanilla on classpath), then parent
    child_libs = list(child.get("libraries") or [])
    parent_libs = list(parent.get("libraries") or [])
    seen_coords = set()
    combined_libs = []
    for lib in child_libs:
        name = str(lib.get("name") or "")
        parts = name.split(":")
        # Distinguish distinct classifiers (4th part) while overriding same group:artifact:classifier
        if len(parts) >= 2:
            cls_part = parts[3] if len(parts) >= 4 else ""
            seen_coords.add((parts[0], parts[1], cls_part))
        combined_libs.append(lib)
    for lib in parent_libs:
        name = str(lib.get("name") or "")
        parts = name.split(":")
        if len(parts) >= 2:
            cls_part = parts[3] if len(parts) >= 4 else ""
            coord = (parts[0], parts[1], cls_part)
            # Only drop duplicate when neither has platform-specific classifiers map
            if coord in seen_coords and "natives" not in lib and not (len(parts) >= 4 and "natives" in cls_part):
                continue
        combined_libs.append(lib)
    merged["libraries"] = combined_libs

    # Arguments: concatenate JVM and game args preserving parent defaults + child additions
    p_args = parent.get("arguments")
    c_args = child.get("arguments")
    if isinstance(p_args, dict) or isinstance(c_args, dict):
        p_jvm = list((p_args or {}).get("jvm") or [])
        c_jvm = list((c_args or {}).get("jvm") or [])
        p_game = list((p_args or {}).get("game") or [])
        c_game = list((c_args or {}).get("game") or [])
        # Legacy parent (only minecraftArguments) + modern child (arguments.game) fallback
        if not p_game and isinstance(parent.get("minecraftArguments"), str) and not c_game:
            p_game = parent["minecraftArguments"].split()
        merged["arguments"] = {
            "jvm": p_jvm + c_jvm,
            "game": p_game + c_game,
        }

    base_id = parent.get("_jar_version_id") or child.get("jar") or parent.get("jar") or parent.get("id")
    if base_id:
        merged["_jar_version_id"] = base_id
    if child.get("inheritsFrom"):
        merged["_inherits_from"] = child.get("inheritsFrom")
    return merged


def get_version_json(version_id, resolve_inheritance: bool = True, _seen: set = None):
    """Download and return the version.json for a specific version (resolving inheritsFrom cleanly)."""
    import json
    from pathlib import Path

    if _seen is None:
        _seen = set()
    if version_id in _seen:
        return None
    _seen = _seen | {version_id}

    v_dir = get_dir("versions")
    b_dir = get_dir("base")
    local_path = v_dir / version_id / f'{version_id}.json'
    cache_key = (str(local_path), version_id, bool(resolve_inheritance))
    if cache_key in _version_json_memory_cache:
        return _version_json_memory_cache[cache_key]

    raw_data = None
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
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        raw_data = loaded
                        break
            except Exception:
                pass

    if raw_data is None:
        version_info = get_version_info(version_id)
        if not version_info or 'url' not in version_info:
            print(f"Version {version_id} not found or has no URL")
            return None

        try:
            response = http_session.get(version_info['url'], timeout=10)
            response.raise_for_status()
            raw_data = response.json()

            # Save to local cache
            try:
                local_path.parent.mkdir(parents=True, exist_ok=True)
                with open(local_path, 'w', encoding='utf-8') as f:
                    json.dump(raw_data, f, indent=2)
            except Exception as e:
                print(f"Failed to cache version JSON: {e}")
        except requests.RequestException as e:
            print(f"Error fetching version JSON for {version_id}: {e}")
            return None

    result = raw_data
    parent_id = raw_data.get("inheritsFrom") if isinstance(raw_data, dict) else None
    if resolve_inheritance and parent_id and parent_id != version_id:
        parent_data = get_version_json(parent_id, resolve_inheritance=True, _seen=_seen)
        if isinstance(parent_data, dict):
            result = _merge_inherited_version_json(raw_data, parent_data)

    _version_json_memory_cache[cache_key] = result
    return result

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
        else:
            try:
                subprocess.run(['pkill', '-f', 'cfr-'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
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
    cleaned_profiles = {}
    for pid, pdata in list(profiles.items()):
        if not isinstance(pdata, dict):
            modified = True
            continue
        # Drop incomplete ghost profiles (e.g. test/interrupted entries lacking both token and UUID)
        has_tok = bool(str(pdata.get("mc_access_token") or "").strip())
        has_uuid = bool(str(pdata.get("mc_uuid") or "").strip())
        if not has_tok and not has_uuid:
            modified = True
            continue
        if pdata.get("mc_uuid"):
            expected_color = calculate_locator_bar_color(pdata.get("mc_uuid"))
            if expected_color != "#FFFFFF" and pdata.get("locator_bar_color") != expected_color:
                pdata["locator_bar_color"] = expected_color
                modified = True
        cleaned_profiles[pid] = pdata

    profiles = cleaned_profiles
    accounts["profiles"] = profiles
    active_id = prefer_pid or accounts.get("active_profile")

    if len(profiles) <= 1:
        if active_id not in profiles:
            accounts["active_profile"] = next(iter(profiles.keys()), None)
            if accounts.get("active_profile") != active_id:
                modified = True
        elif prefer_pid and accounts.get("active_profile") != active_id:
            accounts["active_profile"] = active_id
            modified = True
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
            old_email = matched_group[1].get("msa_email") if isinstance(matched_group[1], dict) else None
            if old_email and isinstance(pdata, dict) and not pdata.get("msa_email"):
                pdata["msa_email"] = old_email
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

def save_auth_cache_for_profile(profile_id, auth_data):
    """Save auth_data under profile_id, set it active, deduplicate, and persist accounts."""
    accounts = load_accounts()
    pid = profile_id or accounts.get("active_profile") or str(uuid.uuid4())
    if "profiles" not in accounts or not isinstance(accounts["profiles"], dict):
        accounts["profiles"] = {}
    accounts["profiles"][pid] = auth_data
    accounts["active_profile"] = pid
    accounts, _ = _deduplicate_accounts(accounts, prefer_pid=pid)
    save_accounts(accounts)
    return accounts.get("active_profile")

def save_auth_cache(auth_data):
    accounts = load_accounts()
    active_id = accounts.get("active_profile")
    if not active_id or active_id not in (accounts.get("profiles") or {}):
        if not active_id:
            active_id = str(uuid.uuid4())
    if "profiles" not in accounts or not isinstance(accounts["profiles"], dict):
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
    else:
        if progress_callback: progress_callback(40, "Loading cached textures...")

    if not js_path.exists() or js_path.stat().st_size < 10000:
        if progress_callback: progress_callback(75, "Setting up 3D skin viewer...")
        bundle_urls = [
            "https://unpkg.com/skinview3d@3.0.0/bundles/skinview3d.bundle.js",
            "https://cdn.jsdelivr.net/npm/skinview3d@3.0.0/bundles/skinview3d.bundle.js",
        ]
        for b_url in bundle_urls:
            try:
                r = requests.get(b_url, timeout=12)
                if r.status_code == 200 and len(r.content) > 10000:
                    with open(js_path, "wb") as f:
                        f.write(r.content)
                    break
            except Exception as e:
                print(f"Failed to download skinview3d from {b_url}: {e}")

    if progress_callback: progress_callback(100, "Profile ready")

    # Generate skin_viewer.html (supports live background color sync & responsive WebGL resize)
    html_content = r"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Skin Viewer</title>
    <style>
        html, body { width: 100%; height: 100%; margin: 0; padding: 0; background: transparent; overflow: hidden; }
        #skin_container { width: 100%; height: 100%; display: block; }
        #skin_container canvas { display: block; width: 100%; height: 100%; }
    </style>
</head>
<body>
<div id="skin_container"></div>
<script src="../skinview3d.bundle.js"></script>
<script>
    (function() {
        const urlParams = new URLSearchParams(window.location.search);
        const skin = urlParams.get('skin');
        const cape = urlParams.get('cape');
        const model = urlParams.get('model') || 'classic';
        const backEq = urlParams.get('back') || 'cape';
        const rawBg = (urlParams.get('bg') || 'transparent').trim();

        function sanitizeBg(val) {
            if (!val || val === 'transparent') return 'transparent';
            if (/^#[0-9a-fA-F]{3,8}$/.test(val)) return val;
            if (/^rgba?\([0-9.,\s]+\)$/.test(val)) return val;
            return 'transparent';
        }

        window.setViewerBackground = function(color) {
            const safe = sanitizeBg((color || '').trim());
            document.documentElement.style.backgroundColor = safe;
            document.body.style.backgroundColor = safe;
        };

        window.setViewerBackground(rawBg);

        function initViewer() {
            if (typeof skinview3d === 'undefined' || !skinview3d.SkinViewer) {
                console.error('skinview3d library failed to load.');
                return;
            }
            const container = document.getElementById('skin_container');
            const rect = container.getBoundingClientRect();
            const initW = Math.max(Math.round(rect.width || window.innerWidth || 0), 200);
            const initH = Math.max(Math.round(rect.height || window.innerHeight || 0), 180);

            const skinViewer = new skinview3d.SkinViewer({
                canvas: document.createElement("canvas"),
                width: initW,
                height: initH
            });
            skinViewer.background = null;

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
            container.appendChild(skinViewer.canvas);
            window.skinViewer = skinViewer;

            window.pauseViewer = function() {
                if (window.skinViewer) {
                    window.skinViewer.renderPaused = true;
                }
            };

            window.resumeViewer = function() {
                if (window.skinViewer) {
                    window.skinViewer.renderPaused = false;
                }
            };

            window.__syncViewerSize = function() {
                const r = container.getBoundingClientRect();
                const w = Math.max(Math.round(r.width || window.innerWidth || 0), 1);
                const h = Math.max(Math.round(r.height || window.innerHeight || 0), 1);
                if (w > 1 && h > 1 && window.skinViewer && typeof window.skinViewer.setSize === 'function') {
                    window.skinViewer.setSize(w, h);
                }
            };

            window.addEventListener('resize', window.__syncViewerSize);
            if (typeof ResizeObserver !== 'undefined') {
                new ResizeObserver(window.__syncViewerSize).observe(container);
            }
            requestAnimationFrame(window.__syncViewerSize);
            setTimeout(window.__syncViewerSize, 120);
        }

        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', initViewer);
        } else {
            initViewer();
        }
    })();
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
    if platform.system() == 'Darwin':
        try:
            cmd = [
                'osascript', '-e',
                f'''
                tell application "System Events"
                    if {pid or 0} > 0 then
                        set procs to every process whose unix id is {pid or 0}
                        if (count of procs) > 0 then
                            return (count of (every window of (item 1 of procs))) > 0
                        end if
                    end if
                    set mcProcs to (every process whose name contains "java" or name contains "Minecraft")
                    repeat with p in mcProcs
                        if (count of (every window of p)) > 0 then
                            return true
                        end if
                    end repeat
                    return false
                end tell
                '''
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=1)
            return 'true' in res.stdout.lower()
        except Exception:
            return False

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


def is_fullscreen_app_active(exclude_pid: int = None) -> bool:
    """Return True when a third-party/game fullscreen window is in the foreground."""
    import platform
    my_pid = int(exclude_pid or os.getpid())
    if platform.system() == 'Darwin':
        try:
            cmd = [
                'osascript', '-e',
                'tell application "System Events" to get value of attribute "AXFullScreen" '
                'of front window of (first process whose frontmost is true)'
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=0.6)
            return 'true' in res.stdout.lower()
        except Exception:
            return False

    if os.name != 'nt':
        return False

    try:
        import ctypes
        from ctypes import wintypes

        # 1. Inspect the foreground window first so our own launcher or desktop windows never pause the UI
        user32 = ctypes.windll.user32
        fg = user32.GetForegroundWindow()
        if not fg or not user32.IsWindowVisible(fg) or user32.IsIconic(fg):
            return False

        fg_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(fg, ctypes.byref(fg_pid))
        if int(fg_pid.value) == my_pid:
            return False

        cls_buf = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(fg, cls_buf, 128)
        if cls_buf.value in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
            return False

        # Skip UWP/system cloaked windows or floating tool windows
        try:
            cloaked = ctypes.c_int(0)
            if ctypes.windll.dwmapi.DwmGetWindowAttribute(fg, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) == 0 and cloaked.value:
                return False
        except Exception:
            pass
        ex_style = user32.GetWindowLongW(fg, -20)  # GWL_EXSTYLE
        if ex_style & 0x00000080:  # WS_EX_TOOLWINDOW
            return False

        # 2. Native Windows D3D exclusive fullscreen / presentation state (ignore QUNS_BUSY=2 which triggers under Focus Assist/desktop)
        try:
            state = ctypes.c_int(0)
            hr = ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state))
            if hr == 0 and state.value in (3, 4):  # QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE
                return True
        except Exception:
            pass

        # 3. Borderless fullscreen check against nearest monitor, excluding normal maximized captioned desktop apps
        style = user32.GetWindowLongW(fg, -16)  # GWL_STYLE
        has_caption = (style & 0x00C00000) == 0x00C00000  # WS_CAPTION
        if user32.IsZoomed(fg) and has_caption:
            return False

        win_rect = wintypes.RECT()
        if not user32.GetWindowRect(fg, ctypes.byref(win_rect)):
            return False

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        hmon = user32.MonitorFromWindow(fg, 2)  # MONITOR_DEFAULTTONEAREST
        if not hmon:
            return False
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            return False

        mon = mi.rcMonitor
        return (
            win_rect.left <= mon.left + 2
            and win_rect.top <= mon.top + 2
            and win_rect.right >= mon.right - 2
            and win_rect.bottom >= mon.bottom - 2
        )
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


def get_applications_dir() -> Path:
    """Resolve the user's Applications / Programs directory across platforms."""
    if os.name == 'nt':
        return get_start_menu_programs_dir()
    elif sys.platform == 'darwin':
        user_apps = Path.home() / "Applications"
        user_apps.mkdir(parents=True, exist_ok=True)
        return user_apps
    else:
        user_apps = Path.home() / ".local" / "share" / "applications"
        user_apps.mkdir(parents=True, exist_ok=True)
        return user_apps


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
    import base64
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

        ps_script = (
            "$ws = New-Object -ComObject WScript.Shell\n"
            "$s = $ws.CreateShortcut($env:SC_LNK_PATH)\n"
            "$s.TargetPath = $env:SC_TARGET\n"
            "$s.Arguments = $env:SC_ARGS\n"
            "$s.WorkingDirectory = $env:SC_WORKDIR\n"
            "if ($env:SC_ICON) { $s.IconLocation = $env:SC_ICON }\n"
            "if ($env:SC_DESC) { $s.Description = $env:SC_DESC }\n"
            "$s.Save()\n"
            "if ($env:SC_PIN -eq '1') {\n"
            "  try {\n"
            "    $sh = New-Object -ComObject Shell.Application\n"
            "    $folder = $sh.Namespace($env:SC_PARENT)\n"
            "    if ($folder) {\n"
            "      $item = $folder.ParseName($env:SC_NAME)\n"
            "      if ($item) {\n"
            "        $verb = $item.Verbs() | Where-Object { $_.Name -and ($_.Name.Replace('&','') -match 'Pin to Start') } | Select-Object -First 1\n"
            "        if ($verb) { $verb.DoIt() }\n"
            "      }\n"
            "    }\n"
            "  } catch {}\n"
            "}\n"
        )

        encoded_cmd = base64.b64encode(ps_script.encode("utf-16le")).decode("ascii")

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded_cmd],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=creationflags
        )
        if lnk_path.exists():
            return True, str(lnk_path)
        err = res.stderr.strip()
        if "_x000D__x000A_" in err:
            import re
            clean = re.sub(r'<[^>]+>', ' ', err)
            clean = clean.replace("_x000D__x000A_", "\n").strip()
            err = clean or err
        return False, (err or f"PowerShell exited with code {res.returncode}")
    except Exception as e:
        return False, str(e)


def create_macos_shortcut(
    dest_path: Path,
    target_exe: str,
    arguments: str,
    working_dir: str,
    icon_path: str = None,
    name: str = "Minecraft"
) -> tuple:
    """
    Create a native macOS .app application bundle or .command launcher shortcut (macOS Big Sur 11.0+).
    Returns (success: bool, message: str).
    """
    try:
        dest_path = Path(dest_path).resolve()
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        # If dest_path ends with .app, create a macOS application bundle
        if dest_path.suffix == ".app":
            import shutil
            import re
            if dest_path.exists():
                if dest_path.is_dir():
                    shutil.rmtree(dest_path)
                else:
                    dest_path.unlink()

            macos_dir = dest_path / "Contents" / "MacOS"
            resources_dir = dest_path / "Contents" / "Resources"
            macos_dir.mkdir(parents=True, exist_ok=True)
            resources_dir.mkdir(parents=True, exist_ok=True)

            launcher_script = macos_dir / "launcher"
            safe_name = re.sub(r'[^A-Za-z0-9_.-]', '', name) or "PyMcLauncher"

            script_content = (
                "#!/bin/bash\n"
                f'cd "{working_dir}"\n'
                f'exec "{target_exe}" launcher.py {arguments}\n'
            )
            launcher_script.write_text(script_content, encoding="utf-8")
            os.chmod(launcher_script, 0o755)

            # Info.plist
            plist_content = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                '<plist version="1.0">\n'
                '<dict>\n'
                '    <key>CFBundleExecutable</key>\n'
                '    <string>launcher</string>\n'
                '    <key>CFBundleIdentifier</key>\n'
                f'    <string>com.cloudyhair6.pymclauncher.{safe_name.lower()}</string>\n'
                '    <key>CFBundleName</key>\n'
                f'    <string>{name}</string>\n'
                '    <key>CFBundlePackageType</key>\n'
                '    <string>APPL</string>\n'
                '    <key>CFBundleShortVersionString</key>\n'
                '    <string>1.0</string>\n'
                '    <key>CFBundleIconFile</key>\n'
                '    <string>app.icns</string>\n'
                '    <key>NSBluetoothAlwaysUsageDescription</key>\n'
                '    <string>PyMcLauncher uses Bluetooth for passkey and security key sign-in.</string>\n'
                '    <key>NSBluetoothPeripheralUsageDescription</key>\n'
                '    <string>PyMcLauncher uses Bluetooth for passkey and security key sign-in.</string>\n'
                '</dict>\n'
                '</plist>\n'
            )
            (dest_path / "Contents" / "Info.plist").write_text(plist_content, encoding="utf-8")

            if icon_path and Path(icon_path).exists():
                try:
                    src_icon = Path(icon_path)
                    if src_icon.suffix.lower() == ".icns":
                        shutil.copy2(src_icon, resources_dir / "app.icns")
                    elif src_icon.suffix.lower() in (".png", ".ico", ".svg"):
                        shutil.copy2(src_icon, resources_dir / f"app{src_icon.suffix.lower()}")
                except Exception:
                    pass

            return True, str(dest_path)
        else:
            # Create a standard executable .command script
            script_content = (
                "#!/bin/bash\n"
                f'cd "{working_dir}"\n'
                f'exec "{target_exe}" launcher.py {arguments}\n'
            )
            dest_path.write_text(script_content, encoding="utf-8")
            os.chmod(dest_path, 0o755)
            return True, str(dest_path)
    except Exception as e:
        return False, str(e)


def get_windows_linked_microsoft_accounts() -> list:
    """
    Discover personal Microsoft accounts (MSA) currently linked with Windows
    (Settings > Accounts > Your info / Email & accounts, IdentityCRL, Personal OneDrive,
    XboxLive, Office LiveId, and Windows MicrosoftAccount security groups).

    Excludes school/work organizational accounts (Azure AD / Entra ID / .edu / .ac / etc.)
    and never scrapes raw binary cache files.

    Returns a deduplicated list of dicts:
        [
            {
                "email": "user@example.com",
                "display_name": "Optional Display Name",
                "source": "Windows Account",
                "cid": "optional_msa_cid",
                "wam_account_id": "optional_wam_id",
                "cached_profile_id": "optional_profile_uuid",
                "mc_username": "OptionalMCName",
                "xbox_gamertag": "OptionalGamertag",
            },
            ...
        ]
    """
    if os.name != "nt":
        return []

    import re

    email_re = re.compile(r"^[A-Za-z0-9._%+-]{2,}@[A-Za-z0-9.-]{2,}\.[A-Za-z]{2,12}$")

    ignored_domains = {
        "example.com",
        "localhost",
        "microsoft.com",
        "windows.com",
        "xbox.com",
        "xboxlive.com",
        "minecraft.net",
        "mojang.com",
    }

    # Known consumer email providers and valid top-level domains
    known_consumer_domains = {
        "outlook.com", "hotmail.com", "live.com", "msn.com", "passport.com",
        "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "icloud.com",
        "me.com", "mac.com", "aol.com", "protonmail.com", "proton.me",
        "zoho.com", "gmx.com", "gmx.de", "web.de", "mail.com", "fastmail.com",
        "btinternet.com", "sky.com", "virginmedia.com", "talktalk.net",
        "comcast.net", "att.net", "verizon.net", "sbcglobal.net", "cox.net",
        "charter.net", "shaw.ca", "rogers.com", "bell.net", "telus.net",
        "optusnet.com.au", "bigpond.com", "xtra.co.nz", "free.fr", "orange.fr",
        "wanadoo.fr", "sfr.fr", "laposte.net", "t-online.de", "freenet.de",
        "libero.it", "alice.it", "tin.it", "virgilio.it", "wp.pl", "onet.pl",
        "interia.pl", "o2.pl", "seznam.cz", "qq.com", "163.com", "126.com",
        "naver.com", "daum.net", "hanmail.net", "yahoo.co.uk", "yahoo.co.jp",
        "yahoo.ca", "yahoo.com.au", "yahoo.fr", "yahoo.de", "yahoo.es",
        "yahoo.it", "yahoo.com.br", "hotmail.co.uk", "hotmail.fr", "hotmail.de",
        "hotmail.it", "hotmail.es", "hotmail.ca", "live.co.uk", "live.fr",
        "live.de", "live.it", "live.nl", "live.ca", "live.com.au",
        "outlook.co.uk", "outlook.fr", "outlook.de", "outlook.es", "outlook.it",
        "outlook.jp", "outlook.com.au", "outlook.com.br", "outlook.kr",
    }

    valid_tlds = {
        "com", "net", "org", "io", "me", "co", "us", "uk", "ca", "au", "nz",
        "de", "fr", "es", "it", "nl", "be", "ch", "at", "se", "no", "dk",
        "fi", "ie", "pt", "pl", "cz", "sk", "hu", "ro", "bg", "gr", "tr",
        "ru", "ua", "il", "ae", "sa", "za", "in", "sg", "my", "ph", "th",
        "vn", "id", "jp", "kr", "tw", "hk", "cn", "br", "mx", "ar", "cl",
        "co", "pe", "xyz", "dev", "app", "tech", "online", "site", "email",
        "live", "outlook", "hotmail", "msn",
    }

    def _is_valid_personal_email(clean: str) -> bool:
        if not email_re.match(clean):
            return False
        local_part, domain = clean.rsplit("@", 1)
        domain = domain.lower()
        if domain in ignored_domains:
            return False

        # Reject school, university, government, and Azure AD tenant domains
        school_or_org_patterns = (
            ".edu", ".ac.", ".sch.", ".school", ".k12.", ".gov", ".mil",
            ".onmicrosoft.com", ".internal", ".local", ".corp", ".lan",
            "student.", "students.", "school.", "college.", "uni.", "university.",
        )
        for pat in school_or_org_patterns:
            if domain.endswith(pat) or pat in domain:
                return False

        # Validate domain labels and TLD so bogus strings like GC@i.xi are rejected
        labels = domain.split(".")
        if len(labels) < 2:
            return False
        if any(len(lbl) < 2 for lbl in labels[:-1]):
            return False
        tld = labels[-1]
        if domain not in known_consumer_domains and tld not in valid_tlds:
            return False

        # Ignore machine/GUID-like local parts
        if len(local_part) >= 32 and all(c in "0123456789abcdefABCDEF-" for c in local_part):
            return False
        return True

    discovered = {}  # lower_email -> dict

    def _add_account(email_str: str, display_name: str = "", source: str = "Windows Account", cid: str = ""):
        if not email_str or not isinstance(email_str, str):
            return
        clean = email_str.strip().strip("\x00\"'<>()[]{};, ")
        if "\\" in clean:
            prefix, rest = clean.split("\\", 1)
            # Only allow MicrosoftAccount\ prefix, never AzureAD\ or domain\ work accounts
            if prefix.lower() not in ("microsoftaccount", "msaccount", "live"):
                return
            clean = rest.strip()
        if clean.lower().startswith("microsoftaccount:user="):
            clean = clean.split("=", 1)[-1].strip()
        if not _is_valid_personal_email(clean):
            return

        key = clean.lower()
        disp = (display_name or "").strip().strip("\x00")
        if disp.lower() == key:
            disp = ""
        cid_clean = (cid or "").strip().strip("\x00")

        if key not in discovered:
            discovered[key] = {
                "email": clean,
                "display_name": disp,
                "source": source,
                "cid": cid_clean,
                "wam_account_id": cid_clean,
                "cached_profile_id": None,
                "mc_username": "",
                "xbox_gamertag": "",
            }
        else:
            if disp and not discovered[key].get("display_name"):
                discovered[key]["display_name"] = disp
            if cid_clean and not discovered[key].get("cid"):
                discovered[key]["cid"] = cid_clean
                if not discovered[key].get("wam_account_id"):
                    discovered[key]["wam_account_id"] = cid_clean
            if source == "Windows Account" and discovered[key].get("source") != "Windows Account":
                discovered[key]["source"] = source

    # 1. Windows Registry checks (winreg) — Personal Microsoft Accounts only
    try:
        import winreg

        def _read_reg_str(key_handle, val_name: str) -> str:
            try:
                val, _ = winreg.QueryValueEx(key_handle, val_name)
                return str(val).strip() if val else ""
            except Exception:
                return ""

        # 1a. HKCU\Software\Microsoft\IdentityCRL\UserExtendedProperties & StoredIdentities
        for sub_path, src_label in [
            (r"Software\Microsoft\IdentityCRL\UserExtendedProperties", "Windows Account"),
            (r"Software\Microsoft\IdentityCRL\StoredIdentities", "Windows Account"),
        ]:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub_path) as k:
                    num_sub, _, _ = winreg.QueryInfoKey(k)
                    for i in range(num_sub):
                        sub_name = winreg.EnumKey(k, i)
                        disp_name = ""
                        cid_val = ""
                        try:
                            with winreg.OpenKey(k, sub_name) as sk:
                                cid_val = (
                                    _read_reg_str(sk, "PUID")
                                    or _read_reg_str(sk, "puid")
                                    or _read_reg_str(sk, "cid")
                                    or _read_reg_str(sk, "CID")
                                )
                                disp_name = (
                                    _read_reg_str(sk, "DisplayName")
                                    or _read_reg_str(sk, "FriendlyName")
                                    or " ".join(
                                        p for p in (_read_reg_str(sk, "FirstName"), _read_reg_str(sk, "LastName")) if p
                                    )
                                )
                        except Exception:
                            pass
                        _add_account(sub_name, disp_name, src_label, cid=cid_val)
            except Exception:
                pass

        # 1b. HKCU\Software\Microsoft\OneDrive\Accounts\Personal (Personal only, skip Business/School)
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\OneDrive\Accounts\Personal") as sk:
                email_val = _read_reg_str(sk, "UserEmail") or _read_reg_str(sk, "Email")
                disp_val = _read_reg_str(sk, "DisplayName") or _read_reg_str(sk, "UserName")
                cid_val = _read_reg_str(sk, "cid") or _read_reg_str(sk, "CID")
                if email_val:
                    _add_account(email_val, disp_val, "Windows Account", cid=cid_val)
        except Exception:
            pass

        # 1c. HKCU\Software\Microsoft\Office\16.0\Common\Identity\Identities (Consumer 'live' accounts only, skip 'aad'/'orgid' school/work)
        for office_ver in ("16.0", "15.0"):
            try:
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    rf"Software\Microsoft\Office\{office_ver}\Common\Identity\Identities",
                ) as k:
                    num_sub, _, _ = winreg.QueryInfoKey(k)
                    for i in range(num_sub):
                        sub_name = winreg.EnumKey(k, i)
                        try:
                            with winreg.OpenKey(k, sub_name) as sk:
                                prov_type = _read_reg_str(sk, "ProviderType").lower()
                                sub_lower = sub_name.lower()
                                # Strictly require consumer 'live' identity; skip 'aad', 'orgid', 'adal' (school/work)
                                if prov_type == "live" or sub_lower.endswith("_liveid"):
                                    email_val = _read_reg_str(sk, "EmailAddress") or _read_reg_str(sk, "SigninName")
                                    disp_val = _read_reg_str(sk, "FriendlyName")
                                    cid_val = _read_reg_str(sk, "UserId") or sub_name.split("_")[0]
                                    if email_val:
                                        _add_account(email_val, disp_val, "Windows Account", cid=cid_val)
                        except Exception:
                            pass
            except Exception:
                pass

        # 1d. HKCU\Software\Microsoft\XboxLive
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\XboxLive") as k:
                email_val = _read_reg_str(k, "UserName") or _read_reg_str(k, "Email")
                gt_val = _read_reg_str(k, "Gamertag")
                cid_val = _read_reg_str(k, "cid") or _read_reg_str(k, "CID")
                if email_val:
                    _add_account(email_val, gt_val, "Windows / Xbox", cid=cid_val)
        except Exception:
            pass

        # 1e. HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Authentication\LogonUI
        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\Authentication\LogonUI",
            ) as k:
                disp_val = _read_reg_str(k, "LastLoggedOnDisplayName")
                for vname in ("LastLoggedOnUser", "LastLoggedOnSAMUser", "SelectedUser"):
                    u_val = _read_reg_str(k, vname)
                    if "@" in u_val and ("\\" not in u_val or u_val.lower().startswith("microsoftaccount\\")):
                        _add_account(u_val, disp_val, "Windows Sign-In")
        except Exception:
            pass
    except Exception:
        pass

    # 2. Check Windows security groups / credential manager (whoami /groups & cmdkey /list) for MicrosoftAccount
    try:
        import subprocess
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        res = subprocess.run(
            ["whoami", "/groups"],
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=creationflags,
        )
        if res.returncode == 0 and res.stdout:
            for line in res.stdout.splitlines():
                if "MicrosoftAccount\\" in line:
                    part = "MicrosoftAccount\\" + line.split("MicrosoftAccount\\", 1)[1].split()[0].strip()
                    _add_account(part, "", "Windows Sign-In")
    except Exception:
        pass

    try:
        import subprocess
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        res = subprocess.run(
            ["cmdkey", "/list"],
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=creationflags,
        )
        if res.returncode == 0 and res.stdout:
            for line in res.stdout.splitlines():
                if "MicrosoftAccount:user=" in line:
                    user_part = line.split("MicrosoftAccount:user=", 1)[1].split()[0].strip()
                    _add_account(user_part, "", "Windows Account")
    except Exception:
        pass

    # 3. Match discovered emails against Windows CloudExperienceHost TokenBroker account files (.tbacct)
    #    so wam_account_id uses the exact TokenBroker account ID when available.
    try:
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        tb_dir = os.path.join(
            local_app_data,
            "Packages",
            "Microsoft.Windows.CloudExperienceHost_cw5n1h2txyewy",
            "AC",
            "TokenBroker",
            "Accounts",
        )
        if os.path.isdir(tb_dir) and discovered:
            for fname in os.listdir(tb_dir):
                if not fname.lower().endswith(".tbacct"):
                    continue
                stem = os.path.splitext(fname)[0]
                fpath = os.path.join(tb_dir, fname)
                try:
                    with open(fpath, "rb") as f:
                        raw = f.read(65536)
                    text_utf16 = raw.decode("utf-16le", errors="ignore").lower()
                    text_utf8 = raw.decode("utf-8", errors="ignore").lower()
                    for email_key, acc_info in discovered.items():
                        if email_key in text_utf16 or email_key in text_utf8:
                            acc_info["wam_account_id"] = stem
                except Exception:
                    pass
    except Exception:
        pass

    # 4. Cross-reference with existing launcher accounts and wam_tokens.json
    try:
        wam_tokens_file = ROOT_DIR / ".cache" / "wam_tokens.json"
        if wam_tokens_file.exists():
            wam_data = json.loads(wam_tokens_file.read_text(encoding="utf-8")) or {}
            for email_key, winfo in wam_data.items():
                if email_key in discovered and isinstance(winfo, dict) and winfo.get("wam_account_id"):
                    discovered[email_key]["wam_account_id"] = str(winfo["wam_account_id"])
    except Exception:
        pass

    try:
        accounts_data = load_accounts()
        for pid, pdata in accounts_data.get("profiles", {}).items():
            if not isinstance(pdata, dict):
                continue
            saved_email = str(pdata.get("msa_email") or "").strip().lower()
            if saved_email and saved_email in discovered:
                discovered[saved_email]["cached_profile_id"] = pid
                discovered[saved_email]["mc_username"] = pdata.get("mc_username", "")
                discovered[saved_email]["xbox_gamertag"] = pdata.get("xbox_gamertag", "")
                if pdata.get("wam_account_id"):
                    discovered[saved_email]["wam_account_id"] = pdata["wam_account_id"]
    except Exception:
        pass

    return list(discovered.values())


def is_allowed_by_rules(rules: list) -> bool:
    """Evaluates Mojang version.json rules for OS, architecture, and features."""
    import platform
    if not rules:
        return True

    sysname = platform.system().lower()
    if sysname == 'windows':
        os_name = 'windows'
    elif sysname == 'darwin':
        os_name = 'osx'
    else:
        os_name = 'linux'

    is_64bit = sys.maxsize > 2**32
    machine = platform.machine().lower()
    is_arm = 'arm' in machine or 'aarch' in machine

    first_rule = rules[0]
    if first_rule.get('action') == 'disallow' and ('os' in first_rule or 'features' in first_rule):
        allowed = True
    else:
        allowed = False

    for rule in rules:
        action = rule.get('action', 'allow')
        if 'features' in rule:
            continue
        os_rule = rule.get('os')
        match = True
        if isinstance(os_rule, dict):
            if 'name' in os_rule and os_rule['name'] != os_name:
                match = False
            if 'arch' in os_rule:
                req_arch = os_rule['arch'].lower()
                if req_arch == 'x86' and (is_64bit or is_arm):
                    match = False
                elif req_arch in ('x64', 'x86_64', 'amd64') and (not is_64bit or is_arm):
                    match = False
                elif ('arm' in req_arch or 'aarch' in req_arch) and not is_arm:
                    match = False
            if 'version' in os_rule and sysname == 'darwin':
                ver_pattern = os_rule['version']
                # Older Mojang manifests hardcoded ^10\. for all macOS versions before Big Sur (macOS 11.0+)
                if ver_pattern != '^10\\.' and ver_pattern != r'^10\.':
                    import re
                    mac_ver = platform.mac_ver()[0]
                    if mac_ver and not re.search(ver_pattern, mac_ver):
                        match = False
        if match:
            allowed = (action == 'allow')

    return allowed


def safe_profile_folder_name(name: str) -> str:
    """Return a clean filesystem-safe folder name for a profile."""
    import re
    cleaned = re.sub(r'[^A-Za-z0-9._-]+', '_', (name or '').strip()).strip('._-')
    return cleaned or "profile"


def build_profile_dirs(
    profile_name: str,
    base_dir: str = None,
    isolate_versions: bool = False,
    link_saves_mode: str = "isolated",
    link_saves_profile: str = None,
    custom_saves_dir: str = None,
    **extra,
) -> dict:
    """Construct a complete, normalized profile directory entry for launcher_config.json."""
    slug = safe_profile_folder_name(profile_name)
    if base_dir:
        base_str = str(base_dir).replace('\\', '/')
    elif profile_name == "Default":
        base_str = "minecraft_downloads"
    else:
        base_str = f"minecraft_downloads/profiles/{slug}"

    shared_root = "minecraft_downloads"
    if isolate_versions:
        ver_str = f"{base_str.rstrip('/')}/versions"
    else:
        ver_str = f"{shared_root}/versions"

    if link_saves_mode == "custom" and custom_saves_dir:
        saves_str = str(custom_saves_dir).replace('\\', '/')
    elif link_saves_mode == "profile" and link_saves_profile:
        # Fallback path string if target removed later; live resolution follows saves_link_profile
        saves_str = f"{base_str.rstrip('/')}/saves"
    else:
        link_saves_mode = "isolated"
        saves_str = f"{base_str.rstrip('/')}/saves"

    entry = {
        "base": base_str,
        "assets": f"{shared_root}/assets",
        "libraries": f"{shared_root}/libraries",
        "versions": ver_str,
        "java": f"{shared_root}/java",
        "saves": saves_str,
        "indexes": f"{shared_root}/indexes",
        "source": f"{shared_root}/source",
        "isolate_versions": bool(isolate_versions),
        "saves_link_mode": link_saves_mode,
    }
    if link_saves_profile:
        entry["saves_link_profile"] = link_saves_profile
    for k, v in extra.items():
        if v is not None:
            entry[k] = v
    return entry


def _is_reparse_or_symlink(p: Path) -> bool:
    """Return True if path is a POSIX symlink or Windows symlink/NTFS directory junction."""
    try:
        if p.is_symlink():
            return True
    except Exception:
        pass
    if os.name == 'nt':
        try:
            st = os.lstat(str(p))
            return bool(getattr(st, 'st_file_attributes', 0) & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
        except Exception:
            pass
    return False


def _remove_link_only(link_path: Path) -> bool:
    """Remove a symlink or Windows junction without deleting target directory contents."""
    try:
        if link_path.is_symlink():
            link_path.unlink()
            return True
        if os.name == 'nt' and _is_reparse_or_symlink(link_path):
            os.rmdir(str(link_path))
            return True
    except Exception:
        pass
    return False


def _create_os_dir_link(link_p: Path, target_p: Path) -> tuple[bool, str]:
    """Attempt native symlink (POSIX/macOS/Win DevMode) then Windows NTFS junction (`mklink /J`)."""
    try:
        os.symlink(str(target_p.resolve()), str(link_p), target_is_directory=True)
        return True, f"Linked {link_p.name} -> {target_p}"
    except OSError as sym_err:
        if os.name == 'nt':
            try:
                res = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(link_p), str(target_p.resolve())],
                    capture_output=True, text=True, timeout=10
                )
                if res.returncode == 0 and (link_p.exists() or _is_reparse_or_symlink(link_p)):
                    return True, f"Junctioned {link_p.name} -> {target_p}"
                err = (res.stderr or res.stdout or str(sym_err)).strip()
                return False, err
            except Exception as j_err:
                return False, str(j_err)
        return False, str(sym_err)


def _sync_dir_newer(src: Path, dst: Path):
    """Copy missing or newer files from `src` into `dst` safely without deleting existing extras."""
    import shutil
    if not src.exists() or not src.is_dir():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.name == ".pymc_link_target":
            continue
        out = dst / item.name
        if item.is_dir():
            _sync_dir_newer(item, out)
        elif item.is_file():
            try:
                if not out.exists() or item.stat().st_mtime > out.stat().st_mtime + 0.5:
                    shutil.copy2(str(item), str(out))
            except Exception:
                pass


def link_profile_subfolder(link_path: Path, target_path: Path) -> tuple[bool, str]:
    """
    Create a safe directory link (symlink on POSIX/macOS, symlink or NTFS junction on Windows)
    at `link_path` pointing to `target_path`, or fall back cleanly to bidirectional mirror sync
    if the underlying filesystem disallows symlinks/junctions (e.g. exFAT / sandbox).
    """
    import shutil
    try:
        link_p = Path(link_path)
        target_p = Path(target_path)
        target_p.mkdir(parents=True, exist_ok=True)
        link_p.parent.mkdir(parents=True, exist_ok=True)

        if str(link_p.resolve()) == str(target_p.resolve()) and (link_p.exists() or _is_reparse_or_symlink(link_p)):
            if _is_reparse_or_symlink(link_p):
                return True, f"Already linked to {target_p}"
            return True, f"Same directory ({target_p})"

        # Probe whether OS directory links work in this parent folder BEFORE modifying link_p
        probe = link_p.parent / f".pymc_link_probe_{os.getpid()}"
        if probe.exists() or _is_reparse_or_symlink(probe):
            _remove_link_only(probe)
        can_link, link_msg = _create_os_dir_link(probe, target_p)
        if _is_reparse_or_symlink(probe) or probe.exists():
            _remove_link_only(probe)
            if probe.exists():
                try:
                    probe.rmdir()
                except Exception:
                    pass

        if can_link:
            if _is_reparse_or_symlink(link_p):
                _remove_link_only(link_p)
            elif link_p.exists():
                if link_p.is_dir():
                    marker = link_p / ".pymc_link_target"
                    if marker.exists():
                        marker.unlink(missing_ok=True)
                    for child in list(link_p.iterdir()):
                        dest = target_p / child.name
                        if dest.exists():
                            dest = target_p / f"{child.name}_imported"
                            idx = 2
                            while dest.exists():
                                dest = target_p / f"{child.name}_imported_{idx}"
                                idx += 1
                        shutil.move(str(child), str(dest))
                    link_p.rmdir()
                else:
                    return False, f"Existing file conflicts with folder link: {link_p}"
            ok, msg = _create_os_dir_link(link_p, target_p)
            if ok:
                return True, msg

        # Filesystem fallback (e.g. exFAT or restricted sandbox): keep bidirectional newer-file sync + marker
        link_p.mkdir(parents=True, exist_ok=True)
        marker = link_p / ".pymc_link_target"
        _sync_dir_newer(link_p, target_p)
        _sync_dir_newer(target_p, link_p)
        marker.write_text(str(target_p.resolve()), encoding="utf-8")
        return True, f"Mirror-synced {link_p.name} <-> {target_p} ({link_msg})"
    except Exception as e:
        return False, str(e)


def unlink_profile_subfolder(link_path: Path) -> bool:
    """Break a folder link cleanly (never deleting target contents) and restore an isolated directory."""
    link_p = Path(link_path)
    if _is_reparse_or_symlink(link_p):
        _remove_link_only(link_p)
    marker = link_p / ".pymc_link_target"
    if marker.is_file():
        try:
            marker.unlink()
        except Exception:
            pass
    link_p.mkdir(parents=True, exist_ok=True)
    return True


def sync_profile_mirror_links_back(profile_name: str = None):
    """Sync changes back to link targets for any folders using mirror-sync fallback (e.g. after game exit)."""
    config = load_launcher_config()
    active = profile_name or config.get("active_profile", "Default")
    base_dir = get_dir("base", profile_name=active)
    for folder in LINKABLE_GAME_SUBFOLDERS:
        link_p = base_dir / folder
        marker = link_p / ".pymc_link_target"
        if not _is_reparse_or_symlink(link_p) and marker.is_file():
            try:
                target_str = marker.read_text(encoding="utf-8").strip()
                if target_str:
                    _sync_dir_newer(link_p, Path(target_str))
            except Exception:
                pass


def ensure_profile_links(profile_name: str = None) -> list[str]:
    """Synchronize linked subfolders (saves, resourcepacks, shaderpacks, screenshots) for a profile."""
    messages = []
    config = load_launcher_config()
    profiles = config.get("profiles", {})
    active = profile_name or config.get("active_profile", "Default")
    dirs = profiles.get(active, {})
    if not isinstance(dirs, dict):
        return messages
    base_dir = get_dir("base", profile_name=active)
    base_dir.mkdir(parents=True, exist_ok=True)

    for folder in LINKABLE_GAME_SUBFOLDERS:
        mode = dirs.get(f"{folder}_link_mode", "isolated")
        link_p = base_dir / folder
        if mode in ("profile", "custom"):
            target_p = get_dir(folder, profile_name=active)
            if str(target_p.resolve()) != str(link_p.resolve()) or _is_reparse_or_symlink(link_p):
                if str(target_p.resolve()) != str(link_p.resolve()):
                    ok, msg = link_profile_subfolder(link_p, target_p)
                    if ok:
                        messages.append(msg)
        else:
            if _is_reparse_or_symlink(link_p) or (link_p / ".pymc_link_target").is_file():
                unlink_profile_subfolder(link_p)
                messages.append(f"Restored isolated {folder} folder for {active}")
    return messages


def _inspect_jar_mod_hint(jar_path: Path) -> Optional[str]:
    """Return loader hint ('fabric', 'quilt', 'neoforge', 'forge') if jar looks like a mod archive."""
    import zipfile
    try:
        with zipfile.ZipFile(jar_path, "r") as z:
            names = set(z.namelist())
            if "quilt.mod.json" in names:
                return "quilt"
            if "fabric.mod.json" in names:
                return "fabric"
            if "META-INF/neoforge.mods.toml" in names:
                return "neoforge"
            if "META-INF/mods.toml" in names or "mcmod.info" in names:
                return "forge"
    except Exception:
        pass
    return None


def _looks_like_mc_root_dir(d: Path) -> bool:
    """Return True if folder `d` resembles a Minecraft/instance root directory."""
    if not d.is_dir():
        return False
    markers = ("saves", "versions", "config", "options.txt", "servers.dat", "manifest.json", "modrinth.index.json", "instance.cfg", ".minecraft", "overrides")
    return any((d / m).exists() for m in markers)


def _discover_worlds_in_dir(root: Path, max_depth: int = 2) -> list[Path]:
    """Find world directories (containing level.dat) up to `max_depth` below `root`, handling custom layouts."""
    found: list[Path] = []
    seen_resolved = set()
    if (root / "level.dat").is_file():
        rp = str(root.resolve())
        seen_resolved.add(rp)
        found.append(root)
        return found

    def _walk(cur: Path, depth: int):
        if depth > max_depth or not cur.is_dir():
            return
        try:
            entries = sorted(cur.iterdir(), key=lambda x: x.name.lower())
        except Exception:
            return
        for item in entries:
            if not item.is_dir():
                continue
            if item.name.lower() in ("assets", "libraries", "versions", "logs", "crash-reports", "natives", "runtime", ".cache"):
                continue
            if (item / "level.dat").is_file():
                rp = str(item.resolve())
                if rp not in seen_resolved:
                    seen_resolved.add(rp)
                    found.append(item)
            elif depth < max_depth:
                _walk(item, depth + 1)

    _walk(root, 1)
    return found


def analyze_import_directory(source_dir: Path) -> dict:
    """
    Scan a Minecraft, Modpack, Prism/MultiMC, CurseForge, or mixed backup directory
    and detect its contents, accidental subfolder selection, non-standard layouts, and loader/MC hints.
    """
    orig_dir = Path(source_dir)
    if not orig_dir.exists():
        return {"exists": False, "resolved_root": str(orig_dir)}

    root = orig_dir
    picked_subfolder_kind = None
    parent_candidate = None
    accidental_warning = None

    # Auto-descend into nested .minecraft / minecraft / overrides folder if outer wrapper was picked
    if (root / ".minecraft").is_dir() and not ((root / "mods").is_dir() or (root / "saves").is_dir()):
        root = root / ".minecraft"
    elif (root / "minecraft").is_dir() and not ((root / "mods").is_dir() or (root / "saves").is_dir()):
        root = root / "minecraft"
    elif (root / "overrides").is_dir() and not ((root / "mods").is_dir() or (root / "saves").is_dir()):
        root = root / "overrides"

    # Detect if the user accidentally clicked inside a subfolder (`mods/`, `saves/`, single world, `resourcepacks/`, etc.)
    folder_lower = orig_dir.name.lower()
    if (orig_dir / "level.dat").is_file() and not (orig_dir / "saves").is_dir():
        picked_subfolder_kind = "single_world"
        cand = orig_dir.parent.parent if orig_dir.parent.name.lower() in ("saves", "worlds") else orig_dir.parent
        if _looks_like_mc_root_dir(cand):
            parent_candidate = str(cand)
            accidental_warning = (
                f"It looks like you picked a single world folder ('{orig_dir.name}') inside '{cand.name}'. "
                f"You can switch to '{cand.name}' to import the full Minecraft setup, or import just this world into saves/."
            )
    elif folder_lower in ("mods", "saves", "worlds", "resourcepacks", "shaderpacks", "config", "versions", "screenshots"):
        has_mc_children = any((orig_dir / m).exists() for m in ("mods", "saves", "versions", "options.txt"))
        if not has_mc_children:
            picked_subfolder_kind = "saves" if folder_lower == "worlds" else folder_lower
            if _looks_like_mc_root_dir(orig_dir.parent):
                parent_candidate = str(orig_dir.parent)
                accidental_warning = (
                    f"It looks like you selected just a '{orig_dir.name}' subfolder inside '{orig_dir.parent.name}'. "
                    f"Click 'Use Full Parent Folder' to import everything (worlds, mods & settings), or continue with only '{orig_dir.name}'."
                )

    detected_loader = None
    detected_mc = None
    suggested_name = root.parent.name if root.name.lower() in (".minecraft", "minecraft", "overrides") and root.parent.name else root.name
    if picked_subfolder_kind and parent_candidate:
        suggested_name = Path(parent_candidate).name

    # Inspect Prism / MultiMC / CurseForge / Modrinth metadata in root or parent
    for base_meta in (root, orig_dir, root.parent):
        mmc_pack = base_meta / "mmc-pack.json"
        if mmc_pack.is_file() and not detected_mc:
            try:
                pack_data = json.loads(mmc_pack.read_text(encoding="utf-8", errors="ignore"))
                for comp in pack_data.get("components", []):
                    uid = str(comp.get("uid", "")).lower()
                    ver = str(comp.get("version", ""))
                    if uid == "net.minecraft":
                        detected_mc = ver
                    elif "fabric" in uid:
                        detected_loader = "fabric"
                    elif "quilt" in uid:
                        detected_loader = "quilt"
                    elif "neoforge" in uid:
                        detected_loader = "neoforge"
                    elif "forge" in uid:
                        detected_loader = "forge"
            except Exception:
                pass

        inst_cfg = base_meta / "instance.cfg"
        if inst_cfg.is_file():
            try:
                for line in inst_cfg.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if line.startswith("name=") and line.split("=", 1)[1].strip():
                        suggested_name = line.split("=", 1)[1].strip()
                        break
            except Exception:
                pass

        cf_manifest = base_meta / "manifest.json"
        if cf_manifest.is_file() and not detected_mc:
            try:
                cf = json.loads(cf_manifest.read_text(encoding="utf-8", errors="ignore"))
                if cf.get("name"):
                    suggested_name = str(cf["name"]).strip()
                mc_blk = cf.get("minecraft") or {}
                if mc_blk.get("version"):
                    detected_mc = str(mc_blk["version"])
                for ml in mc_blk.get("modLoaders", []):
                    ml_id = str(ml.get("id", "")).lower()
                    for cand in ("neoforge", "fabric", "quilt", "forge"):
                        if cand in ml_id:
                            detected_loader = cand
                            break
            except Exception:
                pass

        mr_idx = base_meta / "modrinth.index.json"
        if mr_idx.is_file() and not detected_mc:
            try:
                mr = json.loads(mr_idx.read_text(encoding="utf-8", errors="ignore"))
                if mr.get("name"):
                    suggested_name = str(mr["name"]).strip()
                deps = mr.get("dependencies") or {}
                if deps.get("minecraft"):
                    detected_mc = str(deps["minecraft"])
                for k in ("fabric-loader", "quilt-loader", "neoforge", "forge"):
                    if k in deps:
                        detected_loader = k.replace("-loader", "")
                        break
            except Exception:
                pass

    # Collect mods cleanly whether inside mods/, picked directly, or loose in custom layout
    mod_paths: list[Path] = []
    seen_mods = set()
    mods_scan_dirs = [root / "mods"] if (root / "mods").is_dir() else []
    if (root / "overrides" / "mods").is_dir():
        mods_scan_dirs.append(root / "overrides" / "mods")
    if picked_subfolder_kind == "mods":
        mods_scan_dirs.append(orig_dir)
    for md in mods_scan_dirs:
        for p in sorted(md.iterdir(), key=lambda x: x.name.lower()):
            if p.is_file() and (p.suffix.lower() in (".jar", ".zip") or p.name.lower().endswith(".jar.disabled")):
                rp = str(p.resolve())
                if rp not in seen_mods:
                    seen_mods.add(rp)
                    mod_paths.append(p)
                    if not detected_loader and p.suffix.lower() == ".jar":
                        detected_loader = _inspect_jar_mod_hint(p)

    loose_mods: list[Path] = []
    if picked_subfolder_kind != "mods":
        for p in sorted(root.iterdir(), key=lambda x: x.name.lower()):
            if p.is_file() and p.suffix.lower() == ".jar":
                hint = _inspect_jar_mod_hint(p)
                if hint or "client" not in p.name.lower():
                    rp = str(p.resolve())
                    if rp not in seen_mods:
                        seen_mods.add(rp)
                        loose_mods.append(p)
                        mod_paths.append(p)
                        if hint and not detected_loader:
                            detected_loader = hint

    # Also catch mod folders if user picked a `mods` folder whose contents are ONLY loose jars -> classify cleanly without warning confusion
    if not mod_paths and not picked_subfolder_kind:
        pass

    # Discover worlds cleanly regardless of folder nesting (`saves/`, `Worlds/`, loose, or single world)
    world_paths = _discover_worlds_in_dir(root, max_depth=2)
    saves_sub = (root / "saves").resolve() if (root / "saves").is_dir() else None
    loose_worlds = [w for w in world_paths if not saves_sub or w.parent.resolve() != saves_sub]
    is_single_world = (root / "level.dat").is_file() and not (root / "saves").exists()

    versions_found = []
    if (root / "versions").is_dir():
        for d in (root / "versions").iterdir():
            if d.is_dir() and (d / f"{d.name}.json").is_file():
                versions_found.append(d.name)
            elif d.is_file() and d.suffix.lower() == ".json":
                versions_found.append(d.stem)

    resourcepacks = [p for p in ((root / "resourcepacks").iterdir() if (root / "resourcepacks").is_dir() else []) if not p.name.startswith(".")]
    shaderpacks = [p for p in ((root / "shaderpacks").iterdir() if (root / "shaderpacks").is_dir() else []) if not p.name.startswith(".")]
    screenshots = [p for p in ((root / "screenshots").iterdir() if (root / "screenshots").is_dir() else []) if p.is_file()]

    # Detect non-standard / custom layout (e.g. loose worlds/mods without standard saves/mods/versions folders)
    has_nonstandard_layout = bool(
        (loose_worlds and not is_single_world and not (root / "saves").is_dir())
        or loose_mods
        or (root / "overrides").is_dir()
    )

    return {
        "exists": True,
        "resolved_root": str(root),
        "orig_dir": str(orig_dir),
        "suggested_name": suggested_name or "Imported Profile",
        "detected_loader": detected_loader or "none",
        "detected_mc_version": detected_mc,
        "picked_subfolder_kind": picked_subfolder_kind,
        "parent_candidate": parent_candidate,
        "accidental_warning": accidental_warning,
        "has_nonstandard_layout": has_nonstandard_layout,
        "mods_count": len(mod_paths),
        "loose_mods_count": len(loose_mods),
        "mod_paths": [str(p) for p in mod_paths],
        "worlds_count": len(world_paths),
        "loose_worlds_count": len(loose_worlds),
        "world_paths": [str(w) for w in world_paths],
        "versions_found": sorted(set(versions_found)),
        "resourcepacks_count": len(resourcepacks),
        "shaderpacks_count": len(shaderpacks),
        "screenshots_count": len(screenshots),
        "has_config": (root / "config").is_dir() or (root / "overrides" / "config").is_dir(),
        "has_options_txt": (root / "options.txt").is_file(),
        "has_servers_dat": (root / "servers.dat").is_file(),
        "is_single_world": is_single_world,
    }


def _copy_merge_tree(src: Path, dst: Path):
    import shutil
    if src.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
        for item in src.iterdir():
            _copy_merge_tree(item, dst / item.name)
    elif src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists():
            shutil.copy2(str(src), str(dst))


def import_worlds_to_profile(sources: list, profile_name: str, as_links: bool = False) -> list[str]:
    """
    Import worlds from any list of world folders, saves/ folders, custom backup folders, or .zip archives
    Cleanly organizes discovered `level.dat` worlds into `get_dir("saves", profile_name=profile_name)`.
    """
    import zipfile
    import tempfile
    import shutil
    saves_dir = get_dir("saves", profile_name=profile_name)
    saves_dir.mkdir(parents=True, exist_ok=True)
    imported_names: list[str] = []

    def _next_dest(w_name: str) -> Path:
        cand = saves_dir / w_name
        if not cand.exists():
            return cand
        idx = 2
        while (saves_dir / f"{w_name}_{idx}").exists():
            idx += 1
        return saves_dir / f"{w_name}_{idx}"

    for raw_src in sources:
        sp = Path(raw_src)
        if sp.is_file() and sp.suffix.lower() == ".zip":
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as ztd:
                try:
                    with zipfile.ZipFile(sp, "r") as zf:
                        zf.extractall(ztd)
                    for w_dir in _discover_worlds_in_dir(Path(ztd), max_depth=3):
                        w_name = sp.stem if w_dir == Path(ztd) else w_dir.name
                        dest = _next_dest(w_name)
                        shutil.copytree(str(w_dir), str(dest))
                        imported_names.append(dest.name)
                except Exception:
                    pass
        elif sp.is_dir():
            for w_dir in _discover_worlds_in_dir(sp, max_depth=2):
                dest = _next_dest(w_dir.name)
                if as_links:
                    ok, _ = link_profile_subfolder(dest, w_dir)
                    if ok:
                        imported_names.append(dest.name)
                else:
                    shutil.copytree(str(w_dir), str(dest))
                    imported_names.append(dest.name)
    return imported_names


def import_mods_to_profile(sources: list, profile_name: str) -> tuple[list[str], list[str]]:
    """
    Copy selected .jar/.jar.disabled mod files or folders of mods into the profile's mods/ folder.
    Returns `(imported_filenames, detected_loader_hints)`.
    """
    import shutil
    mods_dir = get_dir("mods", profile_name=profile_name)
    mods_dir.mkdir(parents=True, exist_ok=True)
    imported: list[str] = []
    hints: list[str] = []

    def _collect(p: Path):
        if p.is_file() and (p.suffix.lower() in (".jar", ".zip") or p.name.lower().endswith(".jar.disabled")):
            dest = mods_dir / p.name
            if not dest.exists():
                shutil.copy2(str(p), str(dest))
            imported.append(p.name)
            if p.suffix.lower() == ".jar":
                h = _inspect_jar_mod_hint(p)
                if h and h not in hints:
                    hints.append(h)
        elif p.is_dir():
            scan = p / "mods" if (p / "mods").is_dir() else p
            for ch in sorted(scan.iterdir(), key=lambda x: x.name.lower()):
                if ch.is_file():
                    _collect(ch)

    for raw in sources:
        _collect(Path(raw))
    return imported, hints


def import_profile_from_directory(
    source_dir: Path,
    profile_name: str,
    mode: str = "organize",
    share_versions: bool = True,
    link_saves_from: str = None,
    auto_sort_loose: bool = True,
    preferred_loader: str = None,
    preferred_version: str = None,
    set_active: bool = True,
    ram_mb: int = None,
    include_mods: bool = True,
    include_worlds: bool = True,
    include_resourcepacks: bool = True,
    include_shaderpacks: bool = True,
    include_config: bool = True,
    include_versions: bool = True,
) -> dict:
    """
    Create or import a launcher profile from `source_dir`:
    - mode='organize': copies & auto-sorts files cleanly into `minecraft_downloads/profiles/<slug>/`
    - mode='in_place': points profile directly at the resolved directory (optionally sorting loose files inside it)
    Supports selective imports and non-standard / accidentally-selected `mods/` or `saves/` layouts.
    """
    import shutil
    info = analyze_import_directory(source_dir)
    if not info.get("exists"):
        raise FileNotFoundError(f"Directory does not exist: {source_dir}")

    src_root = Path(info["resolved_root"])
    p_name = (profile_name or info.get("suggested_name") or "Imported").strip()
    slug = safe_profile_folder_name(p_name)

    copied_items = 0
    if mode == "organize":
        rel_base = f"minecraft_downloads/profiles/{slug}"
        target_base = ROOT_DIR / rel_base
        target_base.mkdir(parents=True, exist_ok=True)

        folders_map = []
        if include_mods:
            folders_map.append("mods")
        if include_config:
            folders_map.append("config")
        if include_resourcepacks:
            folders_map.append("resourcepacks")
        if include_shaderpacks:
            folders_map.append("shaderpacks")
        folders_map.append("screenshots")
        if include_worlds and not link_saves_from:
            folders_map.append("saves")
        if include_versions and not share_versions and (src_root / "versions").is_dir():
            folders_map.append("versions")

        for sub in folders_map:
            for base_cand in (src_root, src_root / "overrides"):
                src_sub = base_cand / sub
                if src_sub.is_dir():
                    _copy_merge_tree(src_sub, target_base / sub)
                    copied_items += 1

        # Copy all discovered worlds (including nested custom folders or single world picks) cleanly into saves/
        if include_worlds and not link_saves_from:
            saves_dest = target_base / "saves"
            saves_dest.mkdir(parents=True, exist_ok=True)
            for wp_str in info.get("world_paths") or []:
                wp = Path(wp_str)
                if wp.is_dir():
                    dest_w = saves_dest / wp.name
                    if not dest_w.exists():
                        _copy_merge_tree(wp, dest_w)
                        copied_items += 1

        # Copy all discovered mods (handling picked mods/ folder OR loose mod jars in root)
        if include_mods and auto_sort_loose:
            (target_base / "mods").mkdir(parents=True, exist_ok=True)
            for mp_str in info.get("mod_paths") or []:
                mp = Path(mp_str)
                if mp.is_file():
                    dest = target_base / "mods" / mp.name
                    if not dest.exists():
                        shutil.copy2(str(mp), str(dest))
                        copied_items += 1

        if include_config:
            for base_cand in (src_root, src_root / "overrides"):
                for fname in ("options.txt", "servers.dat"):
                    fp = base_cand / fname
                    if fp.is_file() and not (target_base / fname).exists():
                        shutil.copy2(str(fp), str(target_base / fname))
                        copied_items += 1

        base_cfg_val = rel_base
    else:
        target_base = src_root
        target_base.mkdir(parents=True, exist_ok=True)
        if auto_sort_loose and not info.get("is_single_world"):
            for p in list(target_base.iterdir()):
                if p.is_file() and p.suffix.lower() == ".jar":
                    hint = _inspect_jar_mod_hint(p)
                    if hint or "client" not in p.name.lower():
                        (target_base / "mods").mkdir(parents=True, exist_ok=True)
                        dest = target_base / "mods" / p.name
                        if not dest.exists():
                            shutil.move(str(p), str(dest))
                            copied_items += 1
                elif p.is_dir() and (p / "level.dat").exists():
                    (target_base / "saves").mkdir(parents=True, exist_ok=True)
                    dest = target_base / "saves" / p.name
                    if not dest.exists():
                        shutil.move(str(p), str(dest))
                        copied_items += 1
        base_cfg_val = str(target_base.resolve()).replace('\\', '/')

    link_mode = "profile" if link_saves_from else "isolated"
    entry = build_profile_dirs(
        p_name,
        base_dir=base_cfg_val,
        isolate_versions=not bool(share_versions),
        link_saves_mode=link_mode,
        link_saves_profile=link_saves_from,
        preferred_loader=preferred_loader or info.get("detected_loader"),
        preferred_version=preferred_version or (info["versions_found"][0] if info.get("versions_found") else None),
        ram_mb=ram_mb,
    )

    config = load_launcher_config()
    profiles = config.get("profiles") or {}
    if not isinstance(profiles, dict):
        profiles = {}
    profiles[p_name] = entry
    config["profiles"] = profiles
    if set_active:
        config["active_profile"] = p_name
    save_launcher_config(config)

    link_msgs = ensure_profile_links(p_name)
    return {
        "profile_name": p_name,
        "entry": entry,
        "analysis": info,
        "copied_items": copied_items,
        "link_messages": link_msgs,
    }


def inspect_profile_directory_links(profile_name: str) -> list[dict]:
    """
    Inspect every directory path of `profile_name` and automatically detect:
    - Configured cross-profile links (`saves_link_profile`, etc.)
    - Active filesystem symlinks, Windows NTFS junctions, or mirror-sync fallback markers
    - Shared engine/version directories used by multiple profiles
    - Reverse links where OTHER profiles link to or share this exact directory!
    """
    config = load_launcher_config()
    profiles = config.get("profiles") or {}
    dirs = profiles.get(profile_name, {})
    if not isinstance(dirs, dict):
        dirs = {}

    dir_defs = [
        ("base", "Base (Game Dir)"),
        ("saves", "Saves (Worlds)"),
        ("versions", "Versions"),
        ("mods", "Mods"),
        ("resourcepacks", "Resource Packs"),
        ("shaderpacks", "Shader Packs"),
        ("screenshots", "Screenshots"),
        ("assets", "Assets"),
        ("libraries", "Libraries"),
        ("indexes", "Indexes"),
        ("java", "Java"),
        ("source", "Source"),
    ]

    # Map every resolved path -> set of OTHER profiles pointing to that same path
    reverse_users: dict[tuple[str, str], list[str]] = {}
    for other_name in profiles.keys():
        if other_name == profile_name:
            continue
        for k, _ in dir_defs:
            try:
                rp = str(get_dir(k, profile_name=other_name).resolve())
                reverse_users.setdefault((k, rp), []).append(other_name)
            except Exception:
                pass

    results: list[dict] = []
    base_dir = get_dir("base", profile_name=profile_name)
    for key, label in dir_defs:
        resolved_p = get_dir(key, profile_name=profile_name)
        resolved_str = str(resolved_p.resolve())
        cfg_val = str(dirs.get(key) or "")
        link_p = (base_dir / key) if key in LINKABLE_GAME_SUBFOLDERS else resolved_p

        link_mode = dirs.get(f"{key}_link_mode", "isolated") if key in LINKABLE_GAME_SUBFOLDERS else None
        link_target_prof = dirs.get(f"{key}_link_profile") if key in LINKABLE_GAME_SUBFOLDERS else None

        is_sym = False
        is_junc = False
        is_mirror = False
        real_fs_target = None
        if _is_reparse_or_symlink(link_p):
            try:
                if link_p.is_symlink():
                    is_sym = True
                else:
                    is_junc = True
                real_fs_target = str(link_p.resolve())
            except Exception:
                pass
        elif (link_p / ".pymc_link_target").is_file():
            is_mirror = True
            try:
                real_fs_target = (link_p / ".pymc_link_target").read_text(encoding="utf-8").strip()
            except Exception:
                pass

        shared_by = reverse_users.get((key, resolved_str), [])

        if link_mode == "profile" and link_target_prof:
            mech = "Symlink" if is_sym else ("Junction" if is_junc else ("Mirror Sync" if is_mirror else "Pending Link"))
            badge = f"Linked -> Profile '{link_target_prof}' ({mech})"
            status = "linked_profile"
        elif link_mode == "custom":
            mech = "Symlink" if is_sym else ("Junction" if is_junc else ("Mirror Sync" if is_mirror else "Custom"))
            badge = f"Linked -> Custom Folder ({mech})"
            status = "linked_custom"
        elif is_sym or is_junc:
            mech = "OS Symlink" if is_sym else "NTFS Junction"
            badge = f"{mech} -> {real_fs_target or resolved_str}"
            status = "os_symlink" if is_sym else "os_junction"
        elif is_mirror:
            badge = f"Mirror-Synced <-> {real_fs_target or resolved_str}"
            status = "mirror_sync"
        elif shared_by:
            preview = ", ".join(shared_by[:3]) + ("..." if len(shared_by) > 3 else "")
            badge = f"Shared with {len(shared_by)} profile(s): {preview}"
            status = "shared_with_others"
        else:
            badge = "Isolated (This profile only)"
            status = "isolated"

        results.append({
            "key": key,
            "label": label,
            "configured": cfg_val or str(resolved_p),
            "resolved": resolved_str,
            "exists": resolved_p.exists(),
            "status": status,
            "badge": badge,
            "shared_by": shared_by,
        })
    return results


def get_profile_stats(profile_name: str) -> dict:
    """Return quick counts of worlds, mods, packs, versions, and link settings for a profile."""
    config = load_launcher_config()
    dirs = (config.get("profiles") or {}).get(profile_name, {})
    if not isinstance(dirs, dict):
        dirs = {}
    base_dir = get_dir("base", profile_name=profile_name)
    saves_dir = get_dir("saves", profile_name=profile_name)
    mods_dir = get_dir("mods", profile_name=profile_name)
    rp_dir = get_dir("resourcepacks", profile_name=profile_name)
    sp_dir = get_dir("shaderpacks", profile_name=profile_name)

    worlds = []
    if saves_dir.exists():
        worlds = [d.name for d in saves_dir.iterdir() if d.is_dir() and (d / "level.dat").exists()]
    active_mods = []
    disabled_mods = []
    if mods_dir.exists():
        for f in mods_dir.iterdir():
            if f.is_file():
                if f.suffix.lower() == ".jar":
                    active_mods.append(f.name)
                elif f.name.lower().endswith(".jar.disabled"):
                    disabled_mods.append(f.name)

    rp_cnt = len([p for p in rp_dir.iterdir() if not p.name.startswith(".")]) if rp_dir.exists() else 0
    sp_cnt = len([p for p in sp_dir.iterdir() if not p.name.startswith(".")]) if sp_dir.exists() else 0
    vers = get_downloaded_versions(profile_name=profile_name)

    ver_cfg = str(dirs.get("versions", "")).replace("\\", "/")
    is_isolated_vers = bool(dirs.get("isolate_versions")) or (ver_cfg and str((base_dir / "versions").resolve()).replace("\\", "/") == str(get_dir("versions", profile_name=profile_name).resolve()).replace("\\", "/"))

    return {
        "base_dir": str(base_dir),
        "saves_dir": str(saves_dir),
        "mods_dir": str(mods_dir),
        "worlds": sorted(worlds),
        "active_mods": sorted(active_mods),
        "disabled_mods": sorted(disabled_mods),
        "resourcepacks_count": rp_cnt,
        "shaderpacks_count": sp_cnt,
        "versions": sorted(vers),
        "isolate_versions": is_isolated_vers,
        "saves_link_mode": dirs.get("saves_link_mode", "isolated"),
        "saves_link_profile": dirs.get("saves_link_profile"),
        "preferred_loader": dirs.get("preferred_loader"),
        "preferred_version": dirs.get("preferred_version"),
        "ram_mb": dirs.get("ram_mb"),
    }


def rename_profile_in_config(old_name: str, new_name: str) -> tuple[bool, str]:
    new_n = (new_name or "").strip()
    if not new_n:
        return False, "Profile name cannot be empty."
    config = load_launcher_config()
    profiles = config.get("profiles") or {}
    if old_name not in profiles:
        return False, f"Profile '{old_name}' not found."
    if new_n in profiles and new_n != old_name:
        return False, f"Profile '{new_n}' already exists."
    if new_n == old_name:
        return True, "Unchanged."

    new_profiles = {}
    for k, v in profiles.items():
        key = new_n if k == old_name else k
        if isinstance(v, dict):
            for folder in LINKABLE_GAME_SUBFOLDERS:
                if v.get(f"{folder}_link_profile") == old_name:
                    v[f"{folder}_link_profile"] = new_n
        new_profiles[key] = v
    config["profiles"] = new_profiles
    if config.get("active_profile") == old_name:
        config["active_profile"] = new_n
    save_launcher_config(config)
    return True, f"Renamed '{old_name}' to '{new_n}'."


def duplicate_profile_in_config(
    src_name: str,
    new_name: str,
    copy_mods: bool = True,
    copy_saves: bool = False,
    isolate_versions: bool = None,
) -> tuple[bool, str]:
    import shutil
    new_n = (new_name or "").strip()
    if not new_n:
        return False, "New profile name cannot be empty."
    config = load_launcher_config()
    profiles = config.get("profiles") or {}
    if src_name not in profiles:
        return False, f"Source profile '{src_name}' not found."
    if new_n in profiles:
        return False, f"Profile '{new_n}' already exists."

    src_dirs = profiles[src_name] if isinstance(profiles[src_name], dict) else {}
    src_stats = get_profile_stats(src_name)
    iso_v = src_stats["isolate_versions"] if isolate_versions is None else bool(isolate_versions)

    new_entry = build_profile_dirs(
        new_n,
        isolate_versions=iso_v,
        link_saves_mode=src_dirs.get("saves_link_mode", "isolated") if not copy_saves else "isolated",
        link_saves_profile=src_dirs.get("saves_link_profile") if not copy_saves else None,
        preferred_loader=src_dirs.get("preferred_loader"),
        preferred_version=src_dirs.get("preferred_version"),
        ram_mb=src_dirs.get("ram_mb"),
    )
    new_base = ROOT_DIR / new_entry["base"]
    new_base.mkdir(parents=True, exist_ok=True)
    src_base = get_dir("base", profile_name=src_name)

    def _copy_if_exists(sub: str):
        s = src_base / sub
        if s.exists():
            d = new_base / sub
            if s.is_dir():
                shutil.copytree(str(s), str(d), dirs_exist_ok=True)
            elif s.is_file():
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(s), str(d))

    for item_name in ("config", "resourcepacks", "shaderpacks", "options.txt", "servers.dat"):
        _copy_if_exists(item_name)
    if copy_mods:
        _copy_if_exists("mods")
    if copy_saves:
        _copy_if_exists("saves")

    profiles[new_n] = new_entry
    config["profiles"] = profiles
    save_launcher_config(config)
    ensure_profile_links(new_n)
    return True, f"Duplicated '{src_name}' into '{new_n}'."


def get_profile_launch_settings(profile_name: str = None) -> dict:
    """Return optional per-profile launch settings (ram_mb, jvm_args, resolution, preferred loader/version)."""
    config = load_launcher_config()
    active = profile_name or config.get("active_profile", "Default")
    dirs = (config.get("profiles") or {}).get(active, {})
    if not isinstance(dirs, dict):
        dirs = {}
    return {
        "ram_mb": dirs.get("ram_mb"),
        "jvm_args": dirs.get("jvm_args", ""),
        "win_width": dirs.get("win_width"),
        "win_height": dirs.get("win_height"),
        "preferred_loader": dirs.get("preferred_loader"),
        "preferred_version": dirs.get("preferred_version"),
    }


def make_install_queue_id(profile: str, loader_id: str, mc_version: str, build_ver: str = "") -> str:
    return f"{profile or 'Default'}|{(loader_id or 'none').lower()}|{mc_version}|{build_ver or ''}"


def get_pending_install_queue() -> list[dict]:
    """Return persisted list of queued install targets for the Play tab & Versions tab."""
    cfg = load_launcher_config()
    q = cfg.get("pending_install_queue")
    return [dict(item) for item in q if isinstance(item, dict)] if isinstance(q, list) else []


def save_pending_install_queue(items: list[dict]):
    cfg = load_launcher_config()
    cfg["pending_install_queue"] = [dict(item) for item in (items or []) if isinstance(item, dict)]
    save_launcher_config(cfg)


def add_pending_install_items(new_items: list[dict]) -> list[dict]:
    """Add new install targets to the persisted queue (deduplicated by target id)."""
    existing = get_pending_install_queue()
    seen = {str(x.get("id") or make_install_queue_id(x.get("profile", "Default"), x.get("loader_id", "none"), x.get("mc_version", ""), str((x.get("build_entry") or {}).get("version") or ""))) for x in existing}
    added = []
    for raw in (new_items or []):
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        bver = str((item.get("build_entry") or {}).get("version") or item.get("build_version") or "")
        qid = str(item.get("id") or make_install_queue_id(item.get("profile", "Default"), item.get("loader_id", "none"), item.get("mc_version", ""), bver))
        item["id"] = qid
        if qid not in seen:
            seen.add(qid)
            existing.append(item)
            added.append(item)
    save_pending_install_queue(existing)
    return added


def remove_pending_install_items(item_ids: set[str]) -> list[dict]:
    """Remove install targets matching item_ids from the persisted queue."""
    existing = get_pending_install_queue()
    kept = [x for x in existing if str(x.get("id") or "") not in item_ids]
    save_pending_install_queue(kept)
    return kept


if __name__ == "__main__":
    import argparse
    import sys
    parser = argparse.ArgumentParser(
        description="Minecraft Launcher Utilities CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python utils.py --dirs
  python utils.py --accounts
  python utils.py --versions
  python utils.py --windows-accounts
"""
    )
    parser.add_argument("--dirs", action="store_true", help="Print configured launcher directories")
    parser.add_argument("--accounts", action="store_true", help="List saved launcher profiles and active profile")
    parser.add_argument("--versions", action="store_true", help="List downloaded Minecraft versions")
    parser.add_argument("--windows-accounts", action="store_true", help="List personal Microsoft accounts linked with Windows")

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

    args = parser.parse_args()

    if args.dirs:
        print("[Launcher Directories]")
        for dname in ("base", "versions", "assets", "libraries", "natives", "java", "profiles", "shortcuts", "source"):
            print(f"  {dname:12}: {get_dir(dname)}")

    if args.accounts:
        accs = load_accounts() or {}
        active = accs.get("active_profile", "None")
        profs = accs.get("profiles", {})
        print(f"[Launcher Accounts] Active: {active} ({len(profs)} profiles)")
        for pid, pdata in profs.items():
            uname = pdata.get("mc_username", "Unknown")
            email = pdata.get("msa_email", "")
            gt = pdata.get("xbox_gamertag", "")
            print(f"  * {uname} (Profile: {pid}, Email: {email}, Xbox: {gt})")

    if args.versions:
        vers = get_downloaded_versions()
        print(f"[Downloaded Versions] ({len(vers)} total)")
        for v in vers:
            print(f"  - {v}")

    if args.windows_accounts:
        linked = get_windows_linked_microsoft_accounts()
        print(f"[Windows-Linked Microsoft Accounts] ({len(linked)} total)")
        for a in linked:
            em = a.get("email", "")
            gt = a.get("xbox_gamertag", "")
            mc = a.get("mc_username", "")
            print(f"  - {em} (Minecraft: {mc or 'Not yet logged in'}, Xbox: {gt or 'N/A'})")



