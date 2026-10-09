import base64
import requests
"""Shared utilities for Minecraft launcher scripts."""

import requests
import json
import os
from pathlib import Path

MINECRAFT_VERSION_MANIFEST_URL = "https://launchermeta.mojang.com/mc/game/version_manifest.json"
DOWNLOAD_DIR = Path(__file__).parent / "minecraft_downloads"

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

def get_dir(name: str) -> Path:
    """Get the path for a specific directory type from config or defaults."""
    if _custom_download_dir:
        base = _custom_download_dir
        if name == 'base': return base
        return base / name
        
    config = load_launcher_config()
    
    active = config.get("active_profile", "Default")
    profiles = config.get("profiles", {})
    
    if active in profiles:
        dirs = profiles[active]
    else:
        dirs = config.get("directories", {})
        
    if name in dirs:
        return Path(dirs[name])
    
    # Fallback to base
    if "base" in dirs:
        base = Path(dirs["base"])
    else:
        base = get_download_dir()
        
    if name == 'base': return base
    return base / name


def ensure_download_dir():
    """Create download directory structure if it doesn't exist."""
    get_download_dir().mkdir(parents=True, exist_ok=True)
    get_dir("versions").mkdir(parents=True, exist_ok=True)
    get_dir("assets").mkdir(parents=True, exist_ok=True)
    get_dir("indexes").mkdir(parents=True, exist_ok=True)
    get_dir("libraries").mkdir(parents=True, exist_ok=True)
    get_dir("java").mkdir(parents=True, exist_ok=True)

def fetch_version_manifest():
    """Fetch the version manifest from Mojang's launcher."""
    try:
        response = requests.get(MINECRAFT_VERSION_MANIFEST_URL, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
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
            response = requests.get(url, timeout=30, stream=True)
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
                                    print(json.dumps({"type": "progress", "progress": percent, "status": f"Downloading: {percent:.1f}%"}))
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
                    return False
            return True

        except requests.RequestException as e:
            if attempt < max_retries - 1:
                import time
                time.sleep(1)
                continue
            print(f"Error downloading {url}: {e}")
            return False

def verify_sha1(file_path, expected_sha1):
    """Verify SHA-1 hash of a file."""
    import hashlib
    
    sha1_hash = hashlib.sha1()
    try:
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(4096), b""):
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
    """Download and return the version.json for a specific version."""
    import json
    from pathlib import Path
    
    # Try to load from local cache first
    local_path = get_dir("versions") / version_id / f'{version_id}.json'
    if local_path.exists():
        try:
            with open(local_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass

    version_info = get_version_info(version_id)
    if not version_info or 'url' not in version_info:
        print(f"Version {version_id} not found or has no URL")
        return None
    
    try:
        response = requests.get(version_info['url'], timeout=10)
        response.raise_for_status()
        data = response.json()
        
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
    """Load the launcher config."""
    import json
    config_path = Path("launcher_config.json")
    if config_path.exists():
        try:
            with open(config_path, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_launcher_config(config):
    """Save the launcher config."""
    import json
    with open("launcher_config.json", "w") as f:
        json.dump(config, f, indent=4)

def get_downloaded_versions():
    """Get the set of downloaded version IDs dynamically by scanning the versions directory."""
    versions_dir = get_dir('versions')
    if not versions_dir.exists():
        return set()

    downloaded = set()
    for d in versions_dir.iterdir():
        if d.is_dir():
            # Check if jar exists
            jar_path = versions_dir / f"{d.name}-client.jar"
            if jar_path.exists():
                downloaded.add(d.name)
    return downloaded




import os
import shutil
import time
from pathlib import Path



import os
import shutil
import time
from pathlib import Path



import os
import shutil
import time
from pathlib import Path

def delete_version_files(version_id):
    """Delete the downloaded version files."""
    jar_path = get_dir("versions") / f"{version_id}-client.jar"
    json_path = get_dir("versions") / f"{version_id}.json"
    version_dir = get_dir("versions") / version_id

    try:
        for p in (jar_path, json_path):
            if p.exists():
                try:
                    p.unlink()
                except Exception as e:
                    print(f"Error deleting file {p}: {e}")

        if version_dir.exists():
            try:
                shutil.rmtree(version_dir)
            except Exception as e:
                print(f"Error deleting dir {version_dir}: {e}")

        return True
    except Exception as e:
        print(f"Error deleting version files for {version_id}: {e}")
        return False

def has_any_downloads():
    versions_dir = get_dir("versions")
    if versions_dir.exists():
        for d in versions_dir.iterdir():
            if d.is_dir() and (versions_dir / f"{d.name}-client.jar").exists(): return True
    return False

def delete_all_downloads(progress_callback=None):
    try:
        # Just delete the entire active profile base dir
        base_dir = get_dir("base")
        if base_dir.exists():
            shutil.rmtree(base_dir)
        return True
    except Exception as e:
        return False



def clear_texture_cache():
    pass

def load_accounts():
    config_file = Path("launcher_config.json")
    if config_file.exists():
        try:
            with open(config_file, "r") as f:
                config = json.load(f)
                return config.get("accounts", {})
        except:
            pass
    return {}

def save_accounts(accounts):
    config_file = Path("launcher_config.json")
    config = {}
    if config_file.exists():
        with open(config_file, "r") as f:
            config = json.load(f)
    config["accounts"] = accounts
    with open(config_file, "w") as f:
        json.dump(config, f, indent=4)







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

def clear_auth_cache():
    accounts = load_accounts()
    active_id = accounts.get("active_profile")
    if active_id and active_id in accounts.get("profiles", {}):
        del accounts["profiles"][active_id]
        save_accounts(accounts)

def save_auth_cache(auth_data):
    accounts = load_accounts()
    active_id = accounts.get("active_profile")
    if not active_id:
        active_id = get_active_profile_id(force_new=True)
    if "profiles" not in accounts:
        accounts["profiles"] = {}
    accounts["profiles"][active_id] = auth_data
    save_accounts(accounts)

def get_quick_play_options(minecraft_dir):
    options = []
    
    # 1. Singleplayer Worlds
    saves_dir = get_dir("saves")
    if saves_dir.exists():
        for d in saves_dir.iterdir():
            if d.is_dir() and (d / "level.dat").exists():
                options.append({
                    "type": "singleplayer",
                    "id": d.name,
                    "display": f"World: {d.name}"
                })

    # 2. Multiplayer Servers
    servers_dat = get_dir("base") / "servers.dat"
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
            
    return options

def fetch_player_textures(profile_id, uuid, gamerpic_url=None, progress_callback=None):
    if progress_callback: progress_callback(0, "Fetching textures...")
    cache_dir = Path(f".cache/{profile_id}")
    cache_dir.mkdir(parents=True, exist_ok=True)
    
    skin_path = cache_dir / "skin.png"
    cape_path = cache_dir / "cape.png"
    gamerpic_path = cache_dir / "gamerpic.png"
    html_path = cache_dir / "skin_viewer.html"

    # Clean old files
    for p in [skin_path, cape_path, gamerpic_path, html_path]:
        if p.exists():
            try:
                p.unlink()
            except:
                pass

    # Fetch Minecraft textures
    if uuid:
        try:
            resp = requests.get(f"https://sessionserver.mojang.com/session/minecraft/profile/{uuid}")
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
                                r = requests.get(skin_url)
                                if r.status_code == 200:
                                    with open(skin_path, 'wb') as f:
                                        f.write(r.content)
                                        
                            # Download Cape
                            if 'CAPE' in textures:
                                cape_url = textures['CAPE']['url']
                                r = requests.get(cape_url)
                                if r.status_code == 200:
                                    with open(cape_path, 'wb') as f:
                                        f.write(r.content)
        except Exception as e:
            print(f"Failed to fetch Minecraft textures: {e}")

    if progress_callback: progress_callback(50, "Fetching gamerpic...")

    # Fetch Gamerpic
    if gamerpic_url:
        try:
            r = requests.get(gamerpic_url)
            if r.status_code == 200:
                with open(gamerpic_path, 'wb') as f:
                    f.write(r.content)
        except Exception as e:
            print(f"Failed to fetch gamerpic: {e}")

    
    js_path = Path('.cache') / "skinview3d.bundle.js"
    if not js_path.exists():
        try:
            r = requests.get("https://unpkg.com/skinview3d@3.0.0/bundles/skinview3d.bundle.js")
            if r.status_code == 200:
                with open(js_path, "wb") as f:
                    f.write(r.content)
        except Exception as e:
            print(f"Failed to download skinview3d: {e}")

    if progress_callback: progress_callback(90, "Generating HTML...")

    # Generate skin_viewer.html
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
        height: window.innerHeight,
        skin: skin ? skin : null
    });
    
    if (cape) {
        skinViewer.loadCape(cape, { backEquipment: backEq });
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

    if progress_callback: progress_callback(100, "Done")

    return {
        "skin": "skin.png" if skin_path.exists() else None,
        "cape": "cape.png" if cape_path.exists() else None,
        "gamerpic": "gamerpic.png" if gamerpic_path.exists() else None,
        "html_url": str(html_path.absolute()).replace('\\', '/')
    }
