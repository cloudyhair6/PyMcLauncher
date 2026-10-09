import json
import os
import shutil
from pathlib import Path
from utils import get_download_dir, get_dir, get_downloaded_versions

def is_allowed_by_rules(rules):
    # Same as in command_executor.py
    import platform
    if not rules:
        return True
    
    os_name = platform.system().lower()
    if os_name == 'windows':
        os_name = 'windows'
    elif os_name == 'darwin':
        os_name = 'osx'
    elif os_name == 'linux':
        os_name = 'linux'
        
    action = 'disallow'
    for rule in rules:
        if 'os' in rule:
            if rule['os'].get('name') == os_name:
                action = rule['action']
        else:
            action = rule['action']
            
    return action == 'allow'

def perform_garbage_collection(log_callback=print):
    downloads = get_download_dir()
    
    used_asset_indexes = set()
    used_asset_objects = set()
    used_java_components = set()
    used_libraries = set()
    needs_legacy_resources = False
    
    downloaded_versions = get_downloaded_versions()
    
    # 1. Gather all required dependencies from still-downloaded versions
    for v_id in downloaded_versions:
        json_path = downloads / "versions" / v_id / f"{v_id}.json"
        if not json_path.exists():
            continue
            
        with open(json_path, 'r', encoding='utf-8') as f:
            v_info = json.load(f)
            
        # Assets
        asset_index_info = v_info.get('assetIndex') or {}
        asset_id = asset_index_info.get('id', 'legacy')
        used_asset_indexes.add(asset_id)
        if asset_id == 'legacy' or asset_id == 'pre-1.6':
            needs_legacy_resources = True
            
        index_file = downloads / "assets" / "indexes" / f"{asset_id}.json"
        if index_file.exists():
            with open(index_file, 'r', encoding='utf-8') as f:
                idx_data = json.load(f)
                for obj in idx_data.get('objects', {}).values():
                    h = obj.get('hash')
                    if h:
                        used_asset_objects.add(h)
        
        # Java Component
        java_version = v_info.get('javaVersion', {})
        component = java_version.get('component', 'jre-legacy')
        used_java_components.add(component)
        
        # Libraries
        for lib in v_info.get('libraries', []):
            if not is_allowed_by_rules(lib.get('rules', [])):
                continue
            
            downloads_node = lib.get('downloads', {})
            artifact = downloads_node.get('artifact', {})
            path_str = artifact.get('path')
            if path_str:
                used_libraries.add(str(Path(path_str).as_posix()))
                
            classifiers = downloads_node.get('classifiers', {})
            for cls_key, cls_artifact in classifiers.items():
                c_path = cls_artifact.get('path')
                if c_path:
                    used_libraries.add(str(Path(c_path).as_posix()))
                    
            if not artifact and not classifiers:
                # Old format fallback
                name_parts = lib['name'].split(':')
                if len(name_parts) >= 3:
                    pkg, name, ver = name_parts[0], name_parts[1], name_parts[2]
                    pkg_path = pkg.replace('.', '/')
                    jar_name = f"{name}-{ver}.jar"
                    used_libraries.add(f"{pkg_path}/{name}/{ver}/{jar_name}")

    # 2. Cleanup unused files
    
    # 2a. Java Runtimes
    java_dir = downloads / "java"
    if java_dir.exists():
        for comp_dir in java_dir.iterdir():
            if comp_dir.is_dir() and comp_dir.name not in used_java_components:
                log_callback(f"[Garbage Collection] Removing unused Java component: {comp_dir.name}")
                shutil.rmtree(comp_dir, ignore_errors=True)
                
    # 2b. Asset Indexes
    indexes_dir = downloads / "assets" / "indexes"
    if indexes_dir.exists():
        for idx_file in indexes_dir.iterdir():
            if idx_file.is_file() and idx_file.name.endswith('.json'):
                asset_id = idx_file.stem
                if asset_id not in used_asset_indexes:
                    log_callback(f"[Garbage Collection] Removing unused asset index: {idx_file.name}")
                    try: idx_file.unlink()
                    except: pass
                    
    # 2c. Asset Objects
    objects_dir = downloads / "assets" / "objects"
    if objects_dir.exists():
        for sub_dir in objects_dir.iterdir():
            if sub_dir.is_dir():
                for obj_file in list(sub_dir.iterdir()):
                    if obj_file.is_file() and obj_file.name not in used_asset_objects:
                        try: obj_file.unlink()
                        except: pass
                # Remove empty subdirs
                if not any(sub_dir.iterdir()):
                    try: sub_dir.rmdir()
                    except: pass
                    
    # 2d. Legacy Resources
    resources_dir = downloads / "resources"
    if resources_dir.exists() and not needs_legacy_resources:
        log_callback("[Garbage Collection] Removing unused legacy resources folder.")
        shutil.rmtree(resources_dir, ignore_errors=True)
        
    legacy_virtual = downloads / "assets" / "virtual"
    if legacy_virtual.exists() and not needs_legacy_resources:
        log_callback("[Garbage Collection] Removing unused legacy virtual assets.")
        shutil.rmtree(legacy_virtual, ignore_errors=True)
        
    # 2e. Libraries
    libs_dir = downloads / "libraries"
    if libs_dir.exists():
        for root, dirs, files in os.walk(libs_dir, topdown=False):
            root_path = Path(root)
            for file in files:
                if file.endswith('.jar') or file.endswith('.zip') or file.endswith('.sha1'):
                    file_path = root_path / file
                    # Calculate relative path from libraries folder
                    rel_path = str(file_path.relative_to(libs_dir).as_posix())
                    
                    # Ignore natives folder since they get extracted and renamed
                    if 'natives' in rel_path.split('/'):
                        continue
                        
                    # If this is a jar, check if it's used
                    if file.endswith('.jar'):
                        if rel_path not in used_libraries:
                            try: file_path.unlink()
                            except: pass
                    # If it's a sha1 file, we can't easily check it in used_libraries since that only tracks jars.
                    # Just delete sha1 if the corresponding jar doesn't exist
                    if file.endswith('.sha1'):
                        if not (root_path / file.replace('.sha1', '')).exists():
                            try: file_path.unlink()
                            except: pass

            # Remove empty directories
            if root_path != libs_dir and not any(root_path.iterdir()):
                try: root_path.rmdir()
                except: pass
                
    # 2f. Natives
    natives_dir = libs_dir / "natives"
    if natives_dir.exists():
        shutil.rmtree(natives_dir, ignore_errors=True)
