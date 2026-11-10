import json
import os
import shutil
import stat
import sys
from pathlib import Path
import time

_pkg_dir = Path(__file__).resolve().parent
_root_dir = _pkg_dir.parent if _pkg_dir.name == "backend" else _pkg_dir
for _p in [str(_pkg_dir), str(_root_dir)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from utils import get_download_dir, get_dir, get_downloaded_versions, load_launcher_config, load_accounts, get_active_profile_id


def _on_rm_error(func, path, exc_info):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass


def _safe_unlink(file_path: Path):
    try:
        os.chmod(file_path, stat.S_IWRITE)
    except Exception:
        pass
    try:
        file_path.unlink()
    except Exception:
        pass


def is_allowed_by_rules(rules):
    # Evaluates Mojang version.json rules for OS, architecture, and features
    import platform
    import sys
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

    # If the first rule is a conditional disallow, implicit default is allow; otherwise disallow
    first_rule = rules[0]
    if first_rule.get('action') == 'disallow' and ('os' in first_rule or 'features' in first_rule):
        allowed = True
    else:
        allowed = False

    for rule in rules:
        action = rule.get('action', 'allow')
        if 'features' in rule:
            # Feature-gated rules are not unconditionally allowed
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
                elif 'arm' in req_arch and not is_arm:
                    match = False
        if match:
            allowed = (action == 'allow')

    return allowed


def _expand_dir_for_deletion(dir_path: Path, files_out: list, dirs_out: list, progress_callback=None, last_emit_ref=None):
    """Walk a directory bottom-up and append all files and subdirectories to files_out and dirs_out."""
    for root, dirs, files in os.walk(dir_path, topdown=False):
        root_p = Path(root)
        for fname in files:
            files_out.append((fname, root_p / fname))
            if progress_callback and last_emit_ref is not None:
                now = time.monotonic()
                if (now - last_emit_ref[0]) >= 0.04:
                    progress_callback(-1, f"Indexing unused files... ({len(files_out) + len(dirs_out)} items to delete)")
                    last_emit_ref[0] = now
        dirs_out.append((root_p.name, root_p))


def _clean_launcher_cache(log_callback=print):
    """Remove orphaned profile folders and obsolete token files from the .cache folder."""
    import re
    cache_root = _root_dir / ".cache"
    if not cache_root.is_dir():
        return
    try:
        known = set((load_accounts() or {}).get("profiles", {}).keys())
    except Exception:
        return  # cannot tell which profiles are valid; delete nothing
    try:
        known.add(get_active_profile_id())
    except Exception:
        pass
    uuid_re = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    week_ago = time.time() - 7 * 86400
    for entry in list(cache_root.iterdir()):
        try:
            if entry.is_dir() and uuid_re.match(entry.name) and entry.name not in known:
                if entry.stat().st_mtime < week_ago:
                    log_callback(f"[Garbage Collection] Removing orphaned profile cache: {entry.name}")
                    shutil.rmtree(entry, onerror=_on_rm_error)
            elif entry.is_file() and entry.name == "wam_tokens.json":
                log_callback("[Garbage Collection] Removing obsolete wam_tokens.json")
                _safe_unlink(entry)
        except Exception:
            pass


def perform_garbage_collection(log_callback=print, progress_callback=None):
    _clean_launcher_cache(log_callback)

    base_dir = get_dir("base")
    versions_dir = get_dir("versions")
    assets_dir = get_dir("assets")
    indexes_dir = get_dir("indexes")
    java_dir = get_dir("java")
    libs_dir = get_dir("libraries")

    used_asset_indexes = set()
    used_asset_objects = set()
    used_asset_names = set()
    used_java_components = set()
    used_libraries = set()
    needs_legacy_resources = False

    if progress_callback:
        progress_callback(-1, "Indexing installed versions and dependencies...")

    downloaded_versions = get_downloaded_versions()

    files_to_delete = []
    dirs_to_delete = []
    empty_check_dirs = []
    last_emit = [0.0]

    def _maybe_emit_index(label="Indexing unused files..."):
        if progress_callback:
            now = time.monotonic()
            if (now - last_emit[0]) >= 0.04:
                progress_callback(-1, f"{label} ({len(files_to_delete) + len(dirs_to_delete)} items to delete)")
                last_emit[0] = now

    # 0. Clean up incomplete or orphaned version folders/files in versions_dir
    if versions_dir.exists():
        for item in list(versions_dir.iterdir()):
            if item.is_dir():
                if item.name not in downloaded_versions:
                    log_callback(f"[Garbage Collection] Removing incomplete version folder: {item.name}")
                    _expand_dir_for_deletion(item, files_to_delete, dirs_to_delete, progress_callback, last_emit)
            elif item.is_file():
                if item.name.endswith("-client.jar"):
                    v_name = item.name[:-len("-client.jar")]
                    if v_name not in downloaded_versions:
                        log_callback(f"[Garbage Collection] Removing orphaned client jar: {item.name}")
                        files_to_delete.append((item.name, item))
                elif item.name.endswith(".json"):
                    v_name = item.stem
                    if v_name not in downloaded_versions:
                        log_callback(f"[Garbage Collection] Removing orphaned version json: {item.name}")
                        files_to_delete.append((item.name, item))
            _maybe_emit_index("Indexing versions...")

    # 1. Gather all required dependencies from still-downloaded versions
    for v_id in downloaded_versions:
        json_path = versions_dir / v_id / f"{v_id}.json"
        if not json_path.exists():
            alt_json = versions_dir / f"{v_id}.json"
            if alt_json.exists():
                json_path = alt_json
            else:
                continue

        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                v_info = json.load(f)
        except Exception:
            continue

        # Assets
        asset_index_info = v_info.get('assetIndex') or {}
        asset_id = asset_index_info.get('id', 'legacy')
        used_asset_indexes.add(asset_id)
        if asset_id == 'legacy' or asset_id == 'pre-1.6':
            needs_legacy_resources = True

        for candidate_idx in (assets_dir / "indexes" / f"{asset_id}.json", indexes_dir / f"{asset_id}.json"):
            if candidate_idx.exists():
                try:
                    with open(candidate_idx, 'r', encoding='utf-8') as f:
                        idx_data = json.load(f)
                        for obj_name, obj in idx_data.get('objects', {}).items():
                            used_asset_names.add(str(Path(obj_name).as_posix()))
                            h = obj.get('hash')
                            if h:
                                used_asset_objects.add(h)
                except Exception:
                    pass

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
                name_parts = lib.get('name', '').split(':')
                if len(name_parts) >= 3:
                    pkg, name, ver = name_parts[0], name_parts[1], name_parts[2]
                    pkg_path = pkg.replace('.', '/')
                    jar_name = f"{name}-{ver}.jar"
                    used_libraries.add(f"{pkg_path}/{name}/{ver}/{jar_name}")

    # 2. Index unused files across all directories

    # 2a. Java Runtimes
    if java_dir.exists():
        for comp_dir in list(java_dir.iterdir()):
            if comp_dir.is_dir() and comp_dir.name not in used_java_components:
                log_callback(f"[Garbage Collection] Removing unused Java component: {comp_dir.name}")
                _expand_dir_for_deletion(comp_dir, files_to_delete, dirs_to_delete, progress_callback, last_emit)
            elif comp_dir.is_file() and comp_dir.name.endswith('.archive'):
                files_to_delete.append((comp_dir.name, comp_dir))

    # 2b. Asset Indexes (check both assets/indexes and indexes_dir)
    for idx_folder in {assets_dir / "indexes", indexes_dir}:
        if idx_folder.exists():
            for idx_file in list(idx_folder.iterdir()):
                if idx_file.is_file() and idx_file.name.endswith('.json'):
                    asset_id = idx_file.stem
                    if asset_id not in used_asset_indexes:
                        log_callback(f"[Garbage Collection] Removing unused asset index: {idx_file.name}")
                        files_to_delete.append((idx_file.name, idx_file))

    # 2c. Asset Objects
    removed_objects = 0
    objects_dir = assets_dir / "objects"
    if objects_dir.exists():
        for sub_dir in list(objects_dir.iterdir()):
            if sub_dir.is_dir():
                for obj_file in list(sub_dir.iterdir()):
                    if obj_file.is_file() and obj_file.name not in used_asset_objects:
                        files_to_delete.append((obj_file.name, obj_file))
                        removed_objects += 1
                    _maybe_emit_index("Indexing asset objects...")
                empty_check_dirs.append(sub_dir)

    # 2c-dev. Dev-Mode Unpacked Assets in assets_dir (outside indexes, objects, virtual, skins)
    removed_dev_assets = 0
    if assets_dir.exists():
        reserved_roots = {"indexes", "objects", "virtual", "skins"}
        for item in list(assets_dir.iterdir()):
            if item.name in reserved_roots:
                continue
            if item.is_file():
                rel_name = item.name
                if rel_name not in used_asset_names:
                    files_to_delete.append((item.name, item))
                    removed_dev_assets += 1
            elif item.is_dir():
                for root, dirs, files in os.walk(item, topdown=False):
                    root_p = Path(root)
                    for file in files:
                        fp = root_p / file
                        rel_name = str(fp.relative_to(assets_dir).as_posix())
                        if rel_name not in used_asset_names:
                            files_to_delete.append((file, fp))
                            removed_dev_assets += 1
                        _maybe_emit_index("Indexing unpacked dev assets...")
                    empty_check_dirs.append(root_p)

    # 2d. Legacy Resources
    resources_dir = base_dir / "resources"
    if resources_dir.exists() and not needs_legacy_resources:
        log_callback("[Garbage Collection] Removing unused legacy resources folder.")
        _expand_dir_for_deletion(resources_dir, files_to_delete, dirs_to_delete, progress_callback, last_emit)

    legacy_virtual = assets_dir / "virtual"
    if legacy_virtual.exists() and not needs_legacy_resources:
        log_callback("[Garbage Collection] Removing unused legacy virtual assets.")
        _expand_dir_for_deletion(legacy_virtual, files_to_delete, dirs_to_delete, progress_callback, last_emit)

    # 2e. Libraries
    removed_libs = 0
    if libs_dir.exists():
        for root, dirs, files in os.walk(libs_dir, topdown=False):
            root_path = Path(root)
            for file in files:
                if file.endswith('.jar') or file.endswith('.zip') or file.endswith('.sha1'):
                    file_path = root_path / file
                    rel_path = str(file_path.relative_to(libs_dir).as_posix())

                    # Ignore natives folder and cached CFR decompiler jar
                    if 'natives' in rel_path.split('/') or rel_path.startswith('org/benf/cfr/'):
                        continue

                    if file.endswith('.jar') or file.endswith('.zip'):
                        if rel_path not in used_libraries:
                            files_to_delete.append((file, file_path))
                            removed_libs += 1
                    elif file.endswith('.sha1'):
                        parent_jar_rel = rel_path[:-len('.sha1')]
                        if parent_jar_rel not in used_libraries and not (root_path / file[:-len('.sha1')]).exists():
                            files_to_delete.append((file, file_path))
                _maybe_emit_index("Indexing libraries...")

            if root_path != libs_dir:
                empty_check_dirs.append(root_path)

    # 2f. Natives
    natives_dir = libs_dir / "natives"
    if natives_dir.exists():
        _expand_dir_for_deletion(natives_dir, files_to_delete, dirs_to_delete, progress_callback, last_emit)

    # 2g. Logs cleanup based on user-configured retention time
    removed_logs = 0
    config = load_launcher_config()
    age_val = float(config.get("gc_log_max_age_value", 7))
    age_unit = config.get("gc_log_max_age_unit", "Days").lower()
    if config.get("gc_delete_logs", True):
        if age_unit.startswith("min"):
            max_age_seconds = age_val * 60.0
        elif age_unit.startswith("hour"):
            max_age_seconds = age_val * 3600.0
        else:
            max_age_seconds = age_val * 86400.0

        now_ts = time.time()

        for log_folder_name in ("logs", "crash-reports"):
            log_dir = base_dir / log_folder_name
            if log_dir.exists() and log_dir.is_dir():
                for item in list(log_dir.rglob("*")):
                    if item.is_file():
                        try:
                            if (now_ts - item.stat().st_mtime) >= max_age_seconds:
                                files_to_delete.append((item.name, item))
                                removed_logs += 1
                        except Exception:
                            pass
                empty_check_dirs.append(log_dir)

        dl_log_json = base_dir / "downloads" / "log.json"
        if dl_log_json.exists() and dl_log_json.is_file():
            try:
                if (now_ts - dl_log_json.stat().st_mtime) >= max_age_seconds:
                    files_to_delete.append((dl_log_json.name, dl_log_json))
                    removed_logs += 1
                    empty_check_dirs.append(base_dir / "downloads")
            except Exception:
                pass

        if base_dir.exists():
            for item in list(base_dir.glob("*.log")):
                if item.is_file():
                    try:
                        if (now_ts - item.stat().st_mtime) >= max_age_seconds:
                            files_to_delete.append((item.name, item))
                            removed_logs += 1
                    except Exception:
                        pass

    # 3. Execute deletions with progress reporting
    total_to_delete = len(files_to_delete) + len(dirs_to_delete)
    if total_to_delete > 0:
        done = 0
        last_del_emit = 0.0
        if progress_callback:
            progress_callback(0.0, f"Deleting unused files (0/{total_to_delete} - 0.0%)...")

        for disp_name, fp in files_to_delete:
            _safe_unlink(fp)
            done += 1
            if progress_callback:
                now = time.monotonic()
                if (now - last_del_emit) >= 0.035 or done == total_to_delete:
                    pct = (done / total_to_delete) * 100.0
                    short_name = disp_name[-40:] if len(disp_name) > 40 else disp_name
                    progress_callback(pct, f"Deleting unused files ({done}/{total_to_delete} - {pct:.1f}%): {short_name}")
                    last_del_emit = now

        for disp_name, dp in dirs_to_delete:
            try:
                if dp.exists():
                    dp.rmdir()
            except Exception:
                try:
                    shutil.rmtree(dp, onerror=_on_rm_error)
                except Exception:
                    pass
            done += 1
            if progress_callback:
                now = time.monotonic()
                if (now - last_del_emit) >= 0.035 or done == total_to_delete:
                    pct = (done / total_to_delete) * 100.0
                    short_name = disp_name[-40:] if len(disp_name) > 40 else disp_name
                    progress_callback(pct, f"Deleting unused files ({done}/{total_to_delete} - {pct:.1f}%): {short_name}")
                    last_del_emit = now

    # Prune empty directories
    for d in empty_check_dirs:
        try:
            if d.exists() and not any(d.iterdir()):
                d.rmdir()
        except Exception:
            pass

    if removed_objects > 0:
        log_callback(f"[Garbage Collection] Removed {removed_objects} unused asset objects.")
    if removed_dev_assets > 0:
        log_callback(f"[Garbage Collection] Removed {removed_dev_assets} unused dev-mode asset files.")
    if removed_libs > 0:
        log_callback(f"[Garbage Collection] Removed {removed_libs} unused libraries.")
    if removed_logs > 0:
        log_callback(f"[Garbage Collection] Deleted {removed_logs} log file(s) older than {int(age_val)} {age_unit}.")

    if progress_callback:
        progress_callback(100.0, "Cleanup complete.")


if __name__ == "__main__":
    import argparse
    import sys
    parser = argparse.ArgumentParser(
        description="Garbage collection and asset cleanup utility for Minecraft Launcher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python garbage_collect.py
  python garbage_collect.py --help
"""
    )
    parser.add_argument("--dir", type=str, help="Custom Minecraft base directory")
    args = parser.parse_args()
    if args.dir:
        from utils import set_download_dir
        set_download_dir(args.dir)
    perform_garbage_collection()
