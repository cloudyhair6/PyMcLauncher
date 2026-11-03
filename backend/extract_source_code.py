#!/usr/bin/env python3
"""
Download and extract deobfuscated/decompiled Java source code and resources
for any Minecraft version (supports official Mojang ProGuard mappings for 1.14.4+
and direct bytecode decompilation via CFR for all versions from Alpha/Beta to latest).
"""

import argparse
import atexit
from concurrent.futures import ThreadPoolExecutor
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

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
    get_version_json,
    download_file,
    set_download_dir,
)

CFR_VERSION = "0.152"
CFR_URL = f"https://repo1.maven.org/maven2/org/benf/cfr/{CFR_VERSION}/cfr-{CFR_VERSION}.jar"


def emit_progress(percent: float, status: str, json_output: bool = False):
    if json_output:
        print(json.dumps({"type": "progress", "progress": round(percent, 1), "status": status}), flush=True)
    else:
        sys.stdout.write(f"\r{status:<100}")
        sys.stdout.flush()


def find_java_executable(version_id: str = None) -> str:
    """Find a working Java executable from the launcher's java directory or system PATH."""
    exe_name = "java.exe" if platform.system() == "Windows" else "java"
    java_dir = get_dir("java")

    # 1. If version_id is known, check its specific component first
    if version_id:
        v_json = get_version_json(version_id)
        if v_json:
            comp = v_json.get("javaVersion", {}).get("component")
            if comp and (java_dir / comp).exists():
                candidate = java_dir / comp / "bin" / exe_name
                if candidate.exists():
                    return str(candidate.absolute())
                found = list((java_dir / comp).rglob(exe_name))
                if found:
                    return str(found[0].absolute())

    # 2. Check any downloaded Java runtime in java_dir
    if java_dir.exists():
        found = list(java_dir.rglob(exe_name))
        if found:
            return str(found[0].absolute())

    # 3. Fallback to system java
    return "java"


def ensure_cfr_jar(json_output: bool = False) -> Path:
    """Ensure the CFR Java decompiler JAR is downloaded."""
    cfr_path = get_dir("libraries") / "org" / "benf" / "cfr" / CFR_VERSION / f"cfr-{CFR_VERSION}.jar"
    if cfr_path.exists() and cfr_path.stat().st_size > 100_000:
        return cfr_path

    cfr_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading CFR Java decompiler ({CFR_VERSION})...", flush=True)
    if download_file(CFR_URL, cfr_path, json_output=json_output):
        return cfr_path
    raise RuntimeError(f"Failed to download CFR decompiler from {CFR_URL}")


def _write_file_bytes(args):
    target_path, data = args
    with open(target_path, "wb") as dst:
        dst.write(data)


def extract_version_source(
    version_id: str = None,
    jar_type: str = "client",
    jar_path: Path = None,
    mappings_path: Path = None,
    output_dir: Path = None,
    json_output: bool = False,
    step: str = "all"
) -> bool:
    ensure_download_dir()

    # Resolve version JSON if version_id is provided
    version_json = None
    if version_id:
        version_json = get_version_json(version_id)
        if not version_json:
            print(f"[Error] Could not fetch metadata for version {version_id}", flush=True)
            return False

    # 1. Locate or download the Minecraft JAR
    if jar_path is None:
        if not version_id:
            print("[Error] Either --version or --jar must be specified.", flush=True)
            return False
        jar_path = get_dir("versions") / f"{version_id}-{jar_type}.jar"
        if not jar_path.exists():
            print(f"JAR not found locally at {jar_path}, downloading...", flush=True)
            from download_minecraft import download_version
            if not download_version(version_id, jar_type):
                print(f"[Error] Failed to download {version_id} ({jar_type}) JAR.", flush=True)
                return False

    if not jar_path.exists():
        print(f"[Error] JAR file does not exist: {jar_path}", flush=True)
        return False

    # 2. Determine output directory
    if output_dir is None:
        label = f"{version_id}-{jar_type}" if version_id else jar_path.stem
        output_dir = get_dir("source") / label
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 3. Inspect JAR and extract non-class resources (assets, data, pack.mcmeta, json, etc.) when step is 'all' or 'resources'
    print(f"Inspecting {jar_path.name}...", flush=True)
    try:
        with zipfile.ZipFile(jar_path, "r") as z:
            all_members = z.infolist()
            resource_members = [
                m for m in all_members
                if not m.is_dir()
                and not m.filename.endswith(".class")
                and not m.filename.startswith("META-INF/")
            ]
            top_level_classes = [
                m.filename for m in all_members
                if m.filename.endswith(".class") and "$" not in m.filename
            ]
            total_classes = max(len(top_level_classes), 1)

            if step in ("all", "resources"):
                if resource_members:
                    res_out_dir = output_dir / "resources"
                    res_out_dir.mkdir(parents=True, exist_ok=True)
                    total_res = len(resource_members)
                    print(f"Extracting {total_res} bundled resources from {jar_path.name} to {res_out_dir}...", flush=True)
                    emit_progress(0.0, f"Preparing directories for {total_res} JAR resources...", json_output)

                    # Pre-create all unique parent directories once instead of 18,000+ redundant mkdir calls
                    unique_dirs = {res_out_dir / Path(m.filename).parent for m in resource_members}
                    for d in unique_dirs:
                        d.mkdir(parents=True, exist_ok=True)

                    # Extract in parallel batches using ThreadPoolExecutor and emit frequent UI updates (every ~35ms)
                    max_workers = min(32, (os.cpu_count() or 4) * 4)
                    batch_size = 128
                    last_emit_time = 0.0

                    with ThreadPoolExecutor(max_workers=max_workers) as pool:
                        batch = []
                        for idx, member in enumerate(resource_members):
                            target_path = res_out_dir / member.filename
                            data = z.read(member)
                            batch.append((target_path, data))

                            if len(batch) >= batch_size or idx == total_res - 1:
                                list(pool.map(_write_file_bytes, batch))
                                batch.clear()

                                now = time.monotonic()
                                if (now - last_emit_time) >= 0.035 or idx == total_res - 1:
                                    pct = ((idx + 1) / total_res) * 100.0
                                    short_name = member.filename[-45:] if len(member.filename) > 45 else member.filename
                                    emit_progress(
                                        pct,
                                        f"Extracting JAR resources ({idx + 1}/{total_res} - {pct:.1f}%): {short_name}",
                                        json_output
                                    )
                                    last_emit_time = now
                    if not json_output:
                        print()
                    print(f"[OK] Extracted {total_res} bundled resources to {res_out_dir}", flush=True)
                else:
                    emit_progress(100.0, "No bundled resources in JAR.", json_output)
                    print(f"[Info] No bundled non-class resources found in {jar_path.name}.", flush=True)

                if step == "resources":
                    return True
    except Exception as e:
        print(f"[Error] Failed to read JAR archive {jar_path}: {e}", flush=True)
        return False

    # 4. Check and download official ProGuard mappings if available (Minecraft 1.14.4+)
    if mappings_path is None and version_json:
        mapping_key = f"{jar_type}_mappings"
        downloads_info = version_json.get("downloads", {})
        if mapping_key in downloads_info:
            mappings_url = downloads_info[mapping_key].get("url")
            expected_sha1 = downloads_info[mapping_key].get("sha1")
            if mappings_url:
                mappings_dir = get_dir("base") / "mappings"
                mappings_dir.mkdir(parents=True, exist_ok=True)
                candidate_map = mappings_dir / f"{version_id}-{jar_type}.txt"
                if not candidate_map.exists():
                    emit_progress(0.0, f"Downloading official Mojang {jar_type} mappings...", json_output)
                    print(f"Downloading official Mojang {jar_type} mappings for {version_id}...", flush=True)
                    if download_file(mappings_url, candidate_map, expected_sha1=expected_sha1, json_output=json_output):
                        mappings_path = candidate_map
                else:
                    mappings_path = candidate_map
                    print(f"[OK] Using cached Mojang mappings: {mappings_path}", flush=True)
        else:
            print(
                f"[Info] No separate ProGuard mappings needed/published for {version_id} "
                f"(26.x+ is already unobfuscated; pre-1.14.4 has no official mappings). Decompiling JAR bytecode directly...",
                flush=True
            )

    # 5. Ensure CFR decompiler and Java are available
    try:
        cfr_jar = ensure_cfr_jar(json_output=json_output)
    except Exception as e:
        print(f"[Error] {e}", flush=True)
        return False

    java_bin = find_java_executable(version_id)
    src_out_dir = output_dir / "src"
    src_out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        java_bin,
        "-Xmx2G",
        "-jar",
        str(cfr_jar),
        str(jar_path),
        "--outputdir",
        str(src_out_dir),
        "--silent",
        "false",
        "--rename",
        "true",
        "--caseinsensitivefs",
        "true",
    ]
    if mappings_path and Path(mappings_path).exists():
        cmd.extend(["--obfuscationpath", str(mappings_path)])
        print(f"Decompiling and deobfuscating {total_classes} classes using Mojang mappings...", flush=True)
    else:
        print(f"Decompiling {total_classes} classes from {jar_path.name}...", flush=True)

    print(f"> Running: {' '.join(cmd)}", flush=True)
    emit_progress(0.0, f"Starting Java decompiler (0/{total_classes} classes)...", json_output)

    proc = None
    def _cleanup_cfr():
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
    atexit.register(_cleanup_cfr)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1
        )

        processed = 0
        last_emit_time = 0.0
        other_lines = []
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            if line.startswith("Processing "):
                cls_name = line[len("Processing "):].split(" ")[0]
                processed += 1
                now = time.monotonic()
                if (now - last_emit_time) >= 0.035 or processed >= total_classes:
                    ratio = min(processed / total_classes, 1.0)
                    pct = ratio * 100.0
                    emit_progress(
                        pct,
                        f"Decompiling ({processed}/{total_classes} - {pct:.1f}%): {cls_name}",
                        json_output
                    )
                    last_emit_time = now
            else:
                other_lines.append(line)
                if len(other_lines) > 25:
                    other_lines.pop(0)

        proc.wait()
        if not json_output:
            print()

        if proc.returncode != 0:
            for err_line in other_lines:
                print(f"  [CFR] {err_line}", flush=True)
            print(f"[Error] Decompiler exited with code {proc.returncode}", flush=True)
            return False

        emit_progress(100.0, f"Source extraction complete! Saved to {output_dir}", json_output)
        print(f"\n[OK] Successfully extracted source code for {version_id or jar_path.stem}!", flush=True)
        print(f"  - Java Source Files: {src_out_dir}", flush=True)
        if (output_dir / "resources").exists():
            print(f"  - Bundled Resources: {output_dir / 'resources'}", flush=True)
        if mappings_path and Path(mappings_path).exists():
            print(f"  - Obfuscation Mappings: {mappings_path}", flush=True)
        return True

    except Exception as e:
        print(f"[Error] Failed to run decompiler: {e}", flush=True)
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Download and extract Java source code for any Minecraft version."
    )
    parser.add_argument("--version", help="Minecraft version ID (e.g., 1.21.1, 1.8.9, b1.7.3)")
    parser.add_argument("--client", action="store_true", help="Extract client source (default)")
    parser.add_argument("--server", action="store_true", help="Extract server source")
    parser.add_argument("--jar", help="Optional path to a specific JAR file to decompile")
    parser.add_argument("--mappings", help="Optional path to a ProGuard mappings file")
    parser.add_argument("--output", help="Output directory for extracted source code")
    parser.add_argument("--dir", help="Custom base download directory")
    parser.add_argument("--json", action="store_true", help="Output progress as JSON")
    parser.add_argument(
        "--step",
        choices=["all", "resources", "decompile"],
        default="all",
        help="Which phase to run: 'resources', 'decompile', or 'all' (default)"
    )

    args = parser.parse_args()

    if args.dir:
        set_download_dir(args.dir)

    jar_type = "server" if args.server else "client"
    jar_path = Path(args.jar) if args.jar else None
    mappings_path = Path(args.mappings) if args.mappings else None
    output_dir = Path(args.output) if args.output else None

    success = extract_version_source(
        version_id=args.version,
        jar_type=jar_type,
        jar_path=jar_path,
        mappings_path=mappings_path,
        output_dir=output_dir,
        json_output=args.json,
        step=args.step
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
