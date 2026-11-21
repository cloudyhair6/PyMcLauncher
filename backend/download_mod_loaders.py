"""
Mod loader discovery, Forge/NeoForge/Meta indexing, installation, and CLI support for PyMcLauncher.

Supported loaders:
- Fabric, Quilt, Legacy Fabric, Babric, BTA (Babric), Ornithe
- Forge (Maven metadata + promotions + files.minecraftforge.net HTML installer index)
- NeoForge
- LiteLoader
- Rift
- NilLoader
- Risugami's ModLoader
- Java Agent
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

_pkg_dir = Path(__file__).resolve().parent
_root_dir = _pkg_dir.parent if _pkg_dir.name == "backend" else _pkg_dir
for _p in [str(_pkg_dir), str(_root_dir)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import requests
import requests.adapters
from utils import (
    download_file,
    get_available_versions,
    get_dir,
    get_downloaded_versions,
    get_version_json,
    sync_existing_version_configs,
)

USER_AGENT = "PyMcLauncher/2.x ModLoaderIndexer"
CACHE_TTL_SECONDS = 1800

_session = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=64, pool_maxsize=64)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)
_session.headers.update({"User-Agent": USER_AGENT})

_ACTIVE_PROCS_LOCK = threading.Lock()
_ACTIVE_INSTALLER_PROCS: Set[subprocess.Popen] = set()


def cancel_active_installs():
    """Terminate any running external mod loader installer subprocesses (e.g. Forge Java installer)."""
    with _ACTIVE_PROCS_LOCK:
        procs = list(_ACTIVE_INSTALLER_PROCS)
    for proc in procs:
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except Exception:
                    proc.kill()
        except Exception:
            pass

MOD_LOADERS: List[Dict] = [
    {
        "id": "fabric",
        "name": "Fabric",
        "color": "#d6b893",
        "description": "Lightweight modular mod loader via meta.fabricmc.net",
        "requires_local_file": False,
    },
    {
        "id": "forge",
        "name": "Forge",
        "color": "#6f7cf5",
        "description": "Minecraft Forge via Maven & files.minecraftforge.net index",
        "requires_local_file": False,
    },
    {
        "id": "neoforge",
        "name": "NeoForge",
        "color": "#e68a4d",
        "description": "NeoForged mod loader via maven.neoforged.net",
        "requires_local_file": False,
    },
    {
        "id": "babric",
        "name": "Babric",
        "color": "#9ca3af",
        "description": "Fabric fork targeting Minecraft Beta 1.7.3 via meta.babric.net",
        "requires_local_file": False,
    },
    {
        "id": "bta_babric",
        "name": "BTA (Babric)",
        "color": "#4ade80",
        "description": "Better than Adventure! with Babric support",
        "requires_local_file": False,
    },
    {
        "id": "java_agent",
        "name": "Java Agent",
        "color": "#a8a29e",
        "description": "Launch any Minecraft version with a custom -javaagent JAR",
        "requires_local_file": True,
    },
    {
        "id": "legacy_fabric",
        "name": "Legacy Fabric",
        "color": "#94a3b8",
        "description": "Fabric backport for 1.3.2–1.13.2 via meta.legacyfabric.net",
        "requires_local_file": False,
    },
    {
        "id": "liteloader",
        "name": "LiteLoader",
        "color": "#60a5fa",
        "description": "Lightweight tweak loader via dl.liteloader.com",
        "requires_local_file": False,
    },
    {
        "id": "risugami",
        "name": "Risugami's ModLoader",
        "color": "#a1a1aa",
        "description": "Classic ModLoader for legacy Minecraft (archive links or local zip/jar)",
        "requires_local_file": False,
    },
    {
        "id": "nilloader",
        "name": "NilLoader",
        "color": "#ec4899",
        "description": "Minimal universal JVM agent mod loader via repo.sleeping.town",
        "requires_local_file": False,
    },
    {
        "id": "ornithe",
        "name": "Ornithe",
        "color": "#38bdf8",
        "description": "Fabric toolchain for pre-1.14 Minecraft via meta.ornithemc.net",
        "requires_local_file": False,
    },
    {
        "id": "quilt",
        "name": "Quilt",
        "color": "#a855f7",
        "description": "Quilt mod loader via meta.quiltmc.org",
        "requires_local_file": False,
    },
    {
        "id": "rift",
        "name": "Rift",
        "color": "#cbd5e1",
        "description": "Lightweight mod loader for Minecraft 1.13 / 1.13.2",
        "requires_local_file": False,
    },
]

_META_ENDPOINTS: Dict[str, str] = {
    "fabric": "https://meta.fabricmc.net/v2",
    "quilt": "https://meta.quiltmc.org/v3",
    "legacy_fabric": "https://meta.legacyfabric.net/v2",
    "babric": "https://meta.babric.net/v2",
    "ornithe": "https://meta.ornithemc.net/v3",
}


def _strip_meta_ver(vid: str, prefix: str, mc_ver: str) -> Tuple[str, Optional[str]]:
    """Extract (base_mc_version, loader_build) cleanly even when build or mc_version contains hyphens."""
    rest = vid[len(prefix):] if vid.lower().startswith(prefix.lower()) else vid
    if mc_ver:
        suffix = f"-{mc_ver}"
        if rest.endswith(suffix) and len(rest) > len(suffix):
            return mc_ver, rest[:-len(suffix)]
        return mc_ver, (rest or None)
    if "-" in rest:
        build, base = rest.split("-", 1)
        return base, build
    return vid, None


def detect_version_loader(ver_id: str, ver_json: Optional[dict] = None) -> Tuple[str, str, Optional[str]]:
    """
    Identify which loader a downloaded version ID belongs to.
    Returns (loader_id, base_mc_version, loader_build_version) where vanilla -> ('none', ver_id, None).
    """
    vid = str(ver_id or "")
    vl = vid.lower()
    mc_ver = str((ver_json or {}).get("inheritsFrom") or "").strip()
    main_cls = str((ver_json or {}).get("mainClass") or "").lower()
    libs = [lib for lib in ((ver_json or {}).get("libraries") or []) if isinstance(lib, dict)]
    lib_str = " ".join(str(lib.get("name") or "").lower() for lib in libs)
    jvm_args = ((ver_json or {}).get("arguments") or {}).get("jvm") or []
    jvm_str = " ".join(str(a).lower() for a in jvm_args)

    # Check Java Agent / NilLoader / Risugami / Rift / LiteLoader explicit patterns first
    if "-javaagent-" in vl or ("-javaagent:" in jvm_str and "nilloader" not in vl and "nilloader" not in jvm_str):
        base = mc_ver or vid.split("-javaagent-", 1)[0]
        return "java_agent", base, "custom-agent"
    if "-nilloader-" in vl or "nilloader" in jvm_str or "nilloader" in lib_str:
        m = re.match(r"^(.+?)-nilloader-(.+)$", vid, re.IGNORECASE)
        return "nilloader", (mc_ver or (m.group(1) if m else vid)), (m.group(2) if m else None)
    if "-modloader-" in vl:
        m = re.match(r"^(.+?)-modloader-(.+)$", vid, re.IGNORECASE)
        return "risugami", (mc_ver or (m.group(1) if m else vid)), (m.group(2) if m else None)
    if "-rift-" in vl or "riftloader" in main_cls or "org.dimdev:rift:" in lib_str:
        m = re.match(r"^(.+?)-rift-(.+)$", vid, re.IGNORECASE)
        return "rift", (mc_ver or (m.group(1) if m else vid)), (m.group(2) if m else None)
    if "liteloader" in vl or "liteloader" in lib_str:
        m = re.match(r"^(.+?)-LiteLoader(.+)$", vid, re.IGNORECASE)
        return "liteloader", (mc_ver or (m.group(1) if m else vid)), (m.group(2) if m else None)

    # Meta loaders (Legacy Fabric, Quilt, Ornithe, BTA Babric, Babric, Fabric)
    if "legacy-fabric" in vl or "net.legacyfabric" in lib_str:
        base, build = _strip_meta_ver(vid, "legacy-fabric-loader-", mc_ver)
        return "legacy_fabric", base, build
    if vl.startswith("quilt-loader-") or "org.quiltmc" in lib_str:
        base, build = _strip_meta_ver(vid, "quilt-loader-", mc_ver)
        return "quilt", base, build
    if "ornithe" in vl or "calamus" in lib_str or "ornithe" in lib_str:
        base, build = _strip_meta_ver(vid, "ornithe-loader-", mc_ver)
        return "ornithe", base, build
    if "babric" in vl or "babric" in lib_str or ("net.fabricmc" in lib_str and mc_ver == "b1.7.3"):
        is_bta = "bta" in vl
        base, build = _strip_meta_ver(vid, "fabric-loader-", mc_ver or "b1.7.3")
        return ("bta_babric" if is_bta else "babric"), base, build
    if vl.startswith("fabric-loader-") or "net.fabricmc:fabric-loader" in lib_str or "net.fabricmc.loader" in main_cls:
        base, build = _strip_meta_ver(vid, "fabric-loader-", mc_ver)
        return "fabric", base, build

    # NeoForge vs Forge
    if vl.startswith("neoforge-") or "net.neoforged" in lib_str or "net.neoforged" in main_cls:
        build = vid.split("-", 1)[1] if "-" in vid else None
        if not mc_ver and build:
            mc_ver = _map_neoforge_to_mc(build) or ""
        return "neoforge", (mc_ver or vid), build
    if "-forge-" in vl or "forge" in vl or "net.minecraftforge" in lib_str or "bootstraplauncher" in main_cls:
        m = re.match(r"^(.+?)-forge-(.+)$", vid, re.IGNORECASE)
        build = m.group(2) if m else None
        if not build:
            # Handle legacy Forge IDs like 1.7.10-Forge10.13.4.1614-1.7.10 or extract from Forge library coord
            for lib in libs:
                coords = str(lib.get("name") or "")
                if coords.lower().startswith(("net.minecraftforge:forge:", "net.minecraftforge:minecraftforge:", "net.minecraftforge:fmlloader:")):
                    parts = coords.split(":")
                    if len(parts) >= 3:
                        ver_part = parts[2].split("@")[0]
                        build = ver_part.split("-", 1)[-1] if "-" in ver_part else ver_part
                        break
        if not build:
            m_leg = re.search(r"forge[- ]?([0-9]+\.[0-9][^\s-]*)", vid, re.IGNORECASE)
            if m_leg:
                build = m_leg.group(1)
            elif "-" in vid:
                parts = vid.split("-")
                if "forge" in [p.lower() for p in parts]:
                    build = parts[-1]
        base = mc_ver or (m.group(1) if m else vid)
        return "forge", base, build

    return "none", vid, None


def get_installed_versions_by_loader() -> Dict:
    """
    Scan versions/ and return per-loader installation metadata cleanly:
    {
      "by_loader": {loader_id: {"mc_versions": set(), "builds": {mc: set()}, "entries": [...]}},
      "by_id": {ver_id: (loader_id, mc_ver, build)}
    }
    """
    by_loader: Dict[str, Dict] = {}
    by_id: Dict[str, Tuple[str, str, Optional[str]]] = {}
    search_dirs: List[Path] = []
    for d_kind in ("versions", "base"):
        try:
            p = get_dir(d_kind)
            cand = p if d_kind == "versions" else (p / "versions")
            if cand.exists() and cand not in search_dirs:
                search_dirs.append(cand)
        except Exception:
            pass

    for ver_id in get_downloaded_versions():
        ver_json = None
        for sd in search_dirs:
            jp = sd / ver_id / f"{ver_id}.json"
            if not jp.is_file():
                jp = sd / f"{ver_id}.json"
            if jp.is_file():
                try:
                    ver_json = json.loads(jp.read_text(encoding="utf-8", errors="ignore"))
                    break
                except Exception:
                    pass
        lid, mc_ver, build = detect_version_loader(ver_id, ver_json)
        if lid == "none":
            has_vanilla_jar = False
            for sd in search_dirs:
                if any(
                    (p.is_file() and p.stat().st_size > 0)
                    for p in (
                        sd / ver_id / f"{ver_id}.jar",
                        sd / ver_id / f"{ver_id}-client.jar",
                        sd / ver_id / "client.jar",
                        sd / f"{ver_id}-client.jar",
                        sd / f"{ver_id}.jar",
                    )
                ):
                    has_vanilla_jar = True
                    break
            if not has_vanilla_jar:
                continue
        by_id[ver_id] = (lid, mc_ver, build)
        slot = by_loader.setdefault(lid, {"mc_versions": set(), "builds": {}, "entries": []})
        slot["mc_versions"].add(mc_ver)
        bset = slot["builds"].setdefault(mc_ver, set())
        bset.add(ver_id)
        if build:
            bset.add(build)
            bset.add(f"{mc_ver}-{build}")
        slot["entries"].append({"version_id": ver_id, "mc_version": mc_ver, "build": build})

    return {"by_loader": by_loader, "by_id": by_id}

_RISUGAMI_ARCHIVES: Dict[str, List[Dict]] = {
    "1.6.2": [{"version": "1.6.2", "url": "https://mcarchive.net/mods/modloader", "file_hint": "ModLoader 1.6.2.zip"}],
    "1.5.2": [{"version": "1.5.2", "url": "https://mcarchive.net/mods/modloader", "file_hint": "ModLoader 1.5.2.zip"}],
    "1.5.1": [{"version": "1.5.1", "url": "https://mcarchive.net/mods/modloader", "file_hint": "ModLoader 1.5.1.zip"}],
    "1.4.7": [{"version": "1.4.7", "url": "https://mcarchive.net/mods/modloader", "file_hint": "ModLoader 1.4.7.zip"}],
    "1.4.6": [{"version": "1.4.6", "url": "https://mcarchive.net/mods/modloader", "file_hint": "ModLoader 1.4.6.zip"}],
    "1.4.5": [{"version": "1.4.5", "url": "https://mcarchive.net/mods/modloader", "file_hint": "ModLoader 1.4.5.zip"}],
    "1.3.2": [{"version": "1.3.2", "url": "https://mcarchive.net/mods/modloader", "file_hint": "ModLoader 1.3.2.zip"}],
    "1.2.5": [{"version": "1.2.5", "url": "https://mcarchive.net/mods/modloader", "file_hint": "ModLoader 1.2.5.zip"}],
    "1.1": [{"version": "1.1", "url": "https://mcarchive.net/mods/modloader", "file_hint": "ModLoader 1.1.zip"}],
    "1.0.0": [{"version": "1.0.0", "url": "https://mcarchive.net/mods/modloader", "file_hint": "ModLoader 1.0.0.zip"}],
    "b1.7.3": [{"version": "B1.7.3", "url": "https://mcarchive.net/mods/modloader", "file_hint": "ModLoader B1.7.3.zip"}],
}

_RIFT_RELEASES: Dict[str, List[Dict]] = {
    "1.13.2": [
        {
            "version": "final (1.13.2)",
            "artifact_url": "https://jitpack.io/com/github/Chocohead/Rift/final/Rift-final.jar",
            "tweak_class": "org.dimdev.riftloader.launch.RiftLoaderClient",
            "tag": "recommended",
        },
        {
            "version": "1.0.4-SNAPSHOT",
            "artifact_url": "https://www.jitpack.io/com/github/DimensionalDevelopment/Rift/1.0.4-SNAPSHOT/Rift-1.0.4-SNAPSHOT.jar",
            "tweak_class": "org.dimdev.riftloader.launch.RiftLoaderClient",
            "tag": "",
        },
    ],
    "1.13": [
        {
            "version": "1.0.4-66 (1.13)",
            "artifact_url": "https://www.jitpack.io/com/github/DimensionalDevelopment/Rift/1.0.4-66/Rift-1.0.4-66.jar",
            "tweak_class": "org.dimdev.riftloader.launch.RiftLoaderClient",
            "tag": "recommended",
        },
    ],
}


def list_mod_loaders() -> List[Dict]:
    return [dict(item) for item in MOD_LOADERS]


def get_mod_loader_info(loader_id: str) -> Optional[Dict]:
    for item in MOD_LOADERS:
        if item["id"] == loader_id:
            return dict(item)
    return None


def _cache_dir() -> Path:
    d = _root_dir / ".cache" / "mod_loaders"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_cache(name: str, ttl: int = CACHE_TTL_SECONDS, allow_stale: bool = False):
    p = _cache_dir() / f"{name}.json"
    if not p.exists():
        return None
    try:
        age = time.time() - p.stat().st_mtime
        if not allow_stale and age > ttl:
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_cache(name: str, payload):
    p = _cache_dir() / f"{name}.json"
    try:
        p.write_text(json.dumps(payload), encoding="utf-8")
    except Exception:
        pass


def _http_get_text(url: str, timeout: int = 12) -> Optional[str]:
    try:
        r = _session.get(url, timeout=timeout)
        if r.ok:
            return r.text
    except Exception:
        pass
    return None


def _http_get_json(url: str, timeout: int = 12):
    txt = _http_get_text(url, timeout=timeout)
    if not txt:
        return None
    try:
        return json.loads(txt)
    except Exception:
        return None


def _ver_sort_key(v: str) -> Tuple[int, ...]:
    nums = [int(x) for x in re.findall(r"\d+", str(v))]
    return tuple(nums) if nums else (0,)


def _mc_version_sort_key(v: str) -> Tuple[int, Tuple[int, ...], str]:
    s = str(v)
    if re.match(r"^\d+\.\d+", s):
        return (3, _ver_sort_key(s), s)
    if s.lower().startswith("b"):
        return (2, _ver_sort_key(s), s)
    if s.lower().startswith("a") or s.lower().startswith("c"):
        return (1, _ver_sort_key(s), s)
    return (0, _ver_sort_key(s), s)


def _emit_progress(cb: Optional[Callable[[float, str], None]], pct: float, status: str, json_mode: bool = False):
    if cb:
        try:
            cb(float(pct), str(status))
        except Exception:
            pass
    if json_mode:
        print(json.dumps({"type": "progress", "progress": float(pct), "status": str(status)}), flush=True)


# ---------------------------------------------------------------------------
# Forge Website & Maven Indexing
# ---------------------------------------------------------------------------
def _scrape_forge_page_links(mc_version: str) -> Dict[str, str]:
    """Index files.minecraftforge.net HTML page for exact Forge installer/universal links."""
    url = f"https://files.minecraftforge.net/net/minecraftforge/forge/index_{urllib.parse.quote(mc_version)}.html"
    html = _http_get_text(url, timeout=15)
    if not html:
        return {}

    links: Dict[str, str] = {}
    # Match direct maven links or adfoc.us redirect wrappers ending in installer.jar / universal.jar / client.zip
    raw_urls = re.findall(r'https?://[^\s"\'<>]+(?:installer\.jar|universal\.jar|universal\.zip|client\.zip)', html, flags=re.IGNORECASE)
    for raw_u in raw_urls:
        clean_u = raw_u
        if "adfoc.us" in clean_u and "url=" in clean_u:
            try:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(clean_u).query)
                if "url" in qs and qs["url"]:
                    clean_u = urllib.parse.unquote(qs["url"][0])
            except Exception:
                pass
        if "maven.minecraftforge.net/net/minecraftforge/forge/" in clean_u:
            # Example: .../forge/1.20.1-47.3.0/forge-1.20.1-47.3.0-installer.jar
            m = re.search(r"/forge/([^/]+)/forge-[^/]+-(installer\.jar|universal\.jar|universal\.zip|client\.zip)", clean_u, flags=re.IGNORECASE)
            if m:
                full_ver = m.group(1)
                kind = m.group(2).lower()
                if full_ver not in links or "installer" in kind:
                    links[full_ver] = clean_u
    return links


def _index_forge_catalog(refresh: bool = False) -> Dict:
    """Build or load cached Forge catalog mapping MC version -> list of Forge builds + installer links."""
    if not refresh:
        cached = _read_cache("forge_index")
        if isinstance(cached, dict) and cached.get("by_mc"):
            return cached

    promos_data = _http_get_json("https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json")
    promos = (promos_data or {}).get("promos") or {}

    xml_text = _http_get_text("https://maven.minecraftforge.net/net/minecraftforge/forge/maven-metadata.xml", timeout=15)
    by_mc: Dict[str, List[Dict]] = {}
    if xml_text:
        try:
            root = ET.fromstring(xml_text)
            for ver_el in root.findall(".//versioning/versions/version"):
                full_ver = (ver_el.text or "").strip()
                if "-" not in full_ver:
                    continue
                mc_ver, forge_ver = full_ver.split("-", 1)
                tag = ""
                if promos.get(f"{mc_ver}-recommended") == forge_ver:
                    tag = "recommended"
                elif promos.get(f"{mc_ver}-latest") == forge_ver:
                    tag = "latest"
                dl_url = f"https://maven.minecraftforge.net/net/minecraftforge/forge/{full_ver}/forge-{full_ver}-installer.jar"
                by_mc.setdefault(mc_ver, []).append({
                    "version": forge_ver,
                    "full_id": full_ver,
                    "mc_version": mc_ver,
                    "download_url": dl_url,
                    "tag": tag,
                })
        except Exception:
            pass

    # If XML was unreachable or incomplete, scrape Forge index landing page as backup
    if not by_mc:
        index_html = _http_get_text("https://files.minecraftforge.net/net/minecraftforge/forge/", timeout=15) or ""
        mc_matches = re.findall(r"index_([0-9]+\.[0-9]+(?:\.[0-9]+)?(?:_pre[0-9]+)?)\.html", index_html)
        for mc_ver in sorted(set(mc_matches), key=_mc_version_sort_key, reverse=True):
            by_mc.setdefault(mc_ver, [])
        for k, forge_ver in promos.items():
            if "-" in k:
                mc_ver, badge = k.rsplit("-", 1)
                full_ver = f"{mc_ver}-{forge_ver}"
                entries = by_mc.setdefault(mc_ver, [])
                if not any(e["version"] == forge_ver for e in entries):
                    entries.append({
                        "version": forge_ver,
                        "full_id": full_ver,
                        "mc_version": mc_ver,
                        "download_url": f"https://maven.minecraftforge.net/net/minecraftforge/forge/{full_ver}/forge-{full_ver}-installer.jar",
                        "tag": badge if badge in ("recommended", "latest") else "",
                    })

    for mc_ver, items in by_mc.items():
        items.sort(key=lambda x: (_ver_sort_key(x["version"]), x["tag"] == "recommended"), reverse=True)

    if by_mc:
        catalog = {"by_mc": by_mc, "promos": promos}
        _write_cache("forge_index", catalog)
        return catalog

    stale = _read_cache("forge_index", allow_stale=True)
    return stale if isinstance(stale, dict) else {"by_mc": {}, "promos": {}}


# ---------------------------------------------------------------------------
# NeoForge Indexing
# ---------------------------------------------------------------------------
def _map_neoforge_to_mc(nf_ver: str) -> Optional[str]:
    """Map NeoForge version strings (e.g. 21.1.72 -> 1.21.1, 20.4.237 -> 1.20.4, 47.1.106 -> 1.20.1)."""
    parts = nf_ver.split("-")[0].split(".")
    if len(parts) < 2:
        return None
    try:
        major = int(parts[0])
        minor = int(parts[1])
    except ValueError:
        return None
    if major == 47:
        return "1.20.1"
    if major >= 20:
        return f"1.{major}.{minor}" if minor > 0 else f"1.{major}"
    return None


def _index_neoforge_catalog(refresh: bool = False) -> Dict[str, List[Dict]]:
    if not refresh:
        cached = _read_cache("neoforge_index")
        if isinstance(cached, dict) and cached.get("by_mc"):
            return cached["by_mc"]

    by_mc: Dict[str, List[Dict]] = {}
    nf_json = _http_get_json("https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/neoforge")
    for ver in (nf_json or {}).get("versions") or []:
        mc_ver = _map_neoforge_to_mc(ver)
        if not mc_ver:
            continue
        is_beta = "beta" in ver.lower() or "alpha" in ver.lower()
        dl_url = f"https://maven.neoforged.net/releases/net/neoforged/neoforge/{ver}/neoforge-{ver}-installer.jar"
        by_mc.setdefault(mc_ver, []).append({
            "version": ver,
            "full_id": ver,
            "mc_version": mc_ver,
            "download_url": dl_url,
            "tag": "beta" if is_beta else "",
            "legacy_forge_group": False,
        })

    # Legacy 1.20.1 NeoForge releases under net.neoforged:forge
    legacy_nf = _http_get_json("https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/forge")
    for ver in (legacy_nf or {}).get("versions") or []:
        if "-" in ver:
            mc_ver, nf_v = ver.split("-", 1)
            dl_url = f"https://maven.neoforged.net/releases/net/neoforged/forge/{ver}/forge-{ver}-installer.jar"
            by_mc.setdefault(mc_ver, []).append({
                "version": nf_v,
                "full_id": ver,
                "mc_version": mc_ver,
                "download_url": dl_url,
                "tag": "",
                "legacy_forge_group": True,
            })

    for mc_ver, items in by_mc.items():
        items.sort(key=lambda x: _ver_sort_key(x["version"]), reverse=True)
        if items and not items[0].get("tag"):
            items[0]["tag"] = "latest"

    if by_mc:
        _write_cache("neoforge_index", {"by_mc": by_mc})
        return by_mc

    stale = _read_cache("neoforge_index", allow_stale=True)
    return (stale or {}).get("by_mc", {})


# ---------------------------------------------------------------------------
# Version Discovery API (used by UI and CLI)
# ---------------------------------------------------------------------------
def get_minecraft_versions_for_loader(
    loader_id: str,
    include_snapshots: bool = False,
    refresh: bool = False,
) -> List[Dict]:
    """Return supported Minecraft versions for the given loader, newest first."""
    loader_id = (loader_id or "").lower().strip()
    cache_key = f"mc_vers_{loader_id}_{'all' if include_snapshots else 'rel'}"
    if not refresh:
        cached = _read_cache(cache_key)
        if isinstance(cached, list) and cached:
            return cached

    results: List[Dict] = []

    if loader_id in _META_ENDPOINTS:
        base_url = _META_ENDPOINTS[loader_id]
        data = _http_get_json(f"{base_url}/versions/game")
        if isinstance(data, list):
            for entry in data:
                if isinstance(entry, dict):
                    vid = str(entry.get("version") or "")
                    stable = bool(entry.get("stable", True))
                    if vid and (include_snapshots or stable):
                        results.append({
                            "id": vid,
                            "type": "release" if stable else "snapshot",
                            "stable": stable,
                        })

    elif loader_id == "forge":
        catalog = _index_forge_catalog(refresh=refresh)
        for mc_ver in sorted(catalog.get("by_mc", {}).keys(), key=_mc_version_sort_key, reverse=True):
            is_pre = any(tok in mc_ver.lower() for tok in ("pre", "rc", "w"))
            if include_snapshots or not is_pre:
                results.append({
                    "id": mc_ver,
                    "type": "snapshot" if is_pre else "release",
                    "stable": not is_pre,
                })

    elif loader_id == "neoforge":
        by_mc = _index_neoforge_catalog(refresh=refresh)
        for mc_ver in sorted(by_mc.keys(), key=_mc_version_sort_key, reverse=True):
            results.append({"id": mc_ver, "type": "release", "stable": True})

    elif loader_id == "liteloader":
        ll_data = _http_get_json("https://dl.liteloader.com/versions/versions.json")
        versions_map = (ll_data or {}).get("versions") or {}
        for mc_ver in sorted(versions_map.keys(), key=_mc_version_sort_key, reverse=True):
            results.append({"id": mc_ver, "type": "release", "stable": True})

    elif loader_id == "bta_babric":
        results.append({"id": "b1.7.3", "type": "old_beta", "stable": True})

    elif loader_id == "rift":
        for mc_ver in ("1.13.2", "1.13"):
            results.append({"id": mc_ver, "type": "release", "stable": True})

    elif loader_id == "risugami":
        for mc_ver in sorted(_RISUGAMI_ARCHIVES.keys(), key=_mc_version_sort_key, reverse=True):
            results.append({
                "id": mc_ver,
                "type": "old_beta" if mc_ver.startswith("b") else "release",
                "stable": True,
            })

    elif loader_id in ("nilloader", "java_agent"):
        avail = get_available_versions() or []
        for vid, vtype, _ in avail:
            if include_snapshots or vtype == "release":
                results.append({
                    "id": vid,
                    "type": vtype,
                    "stable": vtype == "release",
                })

    if results:
        _write_cache(cache_key, results)
        return results

    stale = _read_cache(cache_key, allow_stale=True)
    return stale if isinstance(stale, list) else []


def get_loader_versions(loader_id: str, mc_version: str, refresh: bool = False) -> List[Dict]:
    """Return available loader versions for (loader_id, mc_version)."""
    loader_id = (loader_id or "").lower().strip()
    mc_version = str(mc_version or "").strip()
    if not mc_version:
        return []

    cache_key = f"loader_vers_{loader_id}_{re.sub(r'[^A-Za-z0-9._-]', '_', mc_version)}"
    if not refresh:
        cached = _read_cache(cache_key)
        if isinstance(cached, list) and cached:
            return cached

    versions_out: List[Dict] = []

    if loader_id in _META_ENDPOINTS:
        base_url = _META_ENDPOINTS[loader_id]
        url = f"{base_url}/versions/loader/{urllib.parse.quote(mc_version)}"
        data = _http_get_json(url)
        if isinstance(data, list):
            for idx, entry in enumerate(data):
                loader_obj = entry.get("loader") or {}
                lver = str(loader_obj.get("version") or "")
                if not lver:
                    continue
                stable = bool(loader_obj.get("stable", True))
                versions_out.append({
                    "version": lver,
                    "full_id": lver,
                    "tag": "recommended" if (idx == 0 and stable) else ("stable" if stable else "beta"),
                    "mc_version": mc_version,
                })

    elif loader_id == "forge":
        catalog = _index_forge_catalog(refresh=refresh)
        items = list(catalog.get("by_mc", {}).get(mc_version) or [])
        scraped = _scrape_forge_page_links(mc_version)
        for item in items:
            row = dict(item)
            if row["full_id"] in scraped:
                row["download_url"] = scraped[row["full_id"]]
            versions_out.append(row)
        # Add any builds discovered only on files.minecraftforge.net HTML page
        seen_ids = {x["full_id"] for x in versions_out}
        for full_id, dl_url in scraped.items():
            if full_id not in seen_ids:
                fver = full_id.split("-", 1)[1] if "-" in full_id else full_id
                versions_out.append({
                    "version": fver,
                    "full_id": full_id,
                    "mc_version": mc_version,
                    "download_url": dl_url,
                    "tag": "",
                })
        versions_out.sort(key=lambda x: (_ver_sort_key(x["version"]), x["tag"] == "recommended"), reverse=True)

    elif loader_id == "neoforge":
        by_mc = _index_neoforge_catalog(refresh=refresh)
        versions_out = [dict(x) for x in (by_mc.get(mc_version) or [])]

    elif loader_id == "liteloader":
        ll_data = _http_get_json("https://dl.liteloader.com/versions/versions.json") or {}
        ver_node = (ll_data.get("versions") or {}).get(mc_version) or {}
        repo_url = (ver_node.get("repo") or {}).get("url") or "https://dl.liteloader.com/versions/"
        arts = ver_node.get("artefacts") or {}
        ll_group = arts.get("com.mumfrey:liteloader") or {}
        for key, info in ll_group.items():
            if not isinstance(info, dict):
                continue
            ver = str(info.get("version") or key)
            fpath = info.get("file") or f"liteloader-{ver}.jar"
            versions_out.append({
                "version": ver,
                "full_id": ver,
                "mc_version": mc_version,
                "tag": "latest" if key == "latest" else "",
                "extra": {
                    "tweakClass": info.get("tweakClass") or "com.mumfrey.liteloader.launch.LiteLoaderTweaker",
                    "libraries": info.get("libraries") or [],
                    "repo_url": repo_url,
                    "file": fpath,
                },
            })
        # Also allow snapshots if only snapshots node exists
        snap_group = ((ver_node.get("snapshots") or {}).get("libraries"))
        if not versions_out and isinstance(snap_group, list):
            snap_ver = f"{mc_version}-SNAPSHOT"
            versions_out.append({
                "version": snap_ver,
                "full_id": snap_ver,
                "mc_version": mc_version,
                "tag": "snapshot",
                "extra": {
                    "tweakClass": "com.mumfrey.liteloader.launch.LiteLoaderTweaker",
                    "libraries": snap_group,
                    "repo_url": repo_url,
                    "file": "",
                },
            })

    elif loader_id == "bta_babric":
        # Check BTA download repo + Babric loader versions
        rel_data = _http_get_json("https://api.github.com/repos/Better-than-Adventure/bta-download-repo/releases?per_page=20")
        babric_loaders = _http_get_json("https://meta.babric.net/v2/versions/loader/b1.7.3") or []
        latest_babric = ""
        if isinstance(babric_loaders, list) and babric_loaders:
            latest_babric = str((babric_loaders[0].get("loader") or {}).get("version") or "")
        if isinstance(rel_data, list):
            for idx, rel in enumerate(rel_data):
                tag_name = str(rel.get("tag_name") or rel.get("name") or "")
                assets = rel.get("assets") or []
                client_url = ""
                for a in assets:
                    aname = str(a.get("name") or "").lower()
                    if aname.endswith(".jar") and "server" not in aname:
                        client_url = str(a.get("browser_download_url") or "")
                        break
                if tag_name:
                    versions_out.append({
                        "version": f"BTA {tag_name}" + (f" + Babric {latest_babric}" if latest_babric else ""),
                        "full_id": tag_name,
                        "mc_version": mc_version,
                        "download_url": client_url,
                        "tag": "latest" if idx == 0 else "",
                        "extra": {"babric_loader": latest_babric, "release_tag": tag_name},
                    })
        if not versions_out and latest_babric:
            for idx, entry in enumerate(babric_loaders[:10]):
                lver = str((entry.get("loader") or {}).get("version") or "")
                if lver:
                    versions_out.append({
                        "version": f"Babric {lver} (for BTA)",
                        "full_id": lver,
                        "mc_version": mc_version,
                        "tag": "latest" if idx == 0 else "",
                        "extra": {"babric_loader": lver},
                    })

    elif loader_id == "nilloader":
        xml_text = _http_get_text("https://repo.sleeping.town/com/unascribed/nilloader/maven-metadata.xml")
        if xml_text:
            try:
                root = ET.fromstring(xml_text)
                vers = [v.text.strip() for v in root.findall(".//versioning/versions/version") if v.text and v.text.strip()]
                vers.sort(key=_ver_sort_key, reverse=True)
                for idx, v in enumerate(vers):
                    versions_out.append({
                        "version": v,
                        "full_id": v,
                        "mc_version": mc_version,
                        "download_url": f"https://repo.sleeping.town/com/unascribed/nilloader/{v}/nilloader-{v}.jar",
                        "tag": "latest" if idx == 0 else "",
                    })
            except Exception:
                pass

    elif loader_id == "rift":
        for item in _RIFT_RELEASES.get(mc_version, []):
            versions_out.append({
                "version": item["version"],
                "full_id": item["version"].split()[0],
                "mc_version": mc_version,
                "download_url": item["artifact_url"],
                "tag": item.get("tag", ""),
                "extra": {"tweak_class": item["tweak_class"]},
            })

    elif loader_id == "risugami":
        for item in _RISUGAMI_ARCHIVES.get(mc_version, []):
            versions_out.append({
                "version": f"ModLoader {item['version']} (Local .zip/.jar or MCArchive)",
                "full_id": item["version"],
                "mc_version": mc_version,
                "download_url": item["url"],
                "tag": "classic",
                "extra": {"file_hint": item.get("file_hint", ""), "allows_local": True},
            })

    elif loader_id == "java_agent":
        versions_out.append({
            "version": "Custom Java Agent (.jar)",
            "full_id": "custom-agent",
            "mc_version": mc_version,
            "tag": "local file",
            "extra": {"allows_local": True},
        })

    if versions_out:
        _write_cache(cache_key, versions_out)
        return versions_out

    stale = _read_cache(cache_key, allow_stale=True)
    return stale if isinstance(stale, list) else []


# ---------------------------------------------------------------------------
# Installation Helpers
# ---------------------------------------------------------------------------
def _maven_rel_path(coords: str) -> Optional[str]:
    clean = str(coords or "").strip()
    ext = "jar"
    if "@" in clean:
        clean, ext = clean.rsplit("@", 1)
    parts = clean.split(":")
    if len(parts) < 3:
        return None
    grp, art, ver = parts[0], parts[1], parts[2]
    cls = parts[3] if len(parts) >= 4 else ""
    fname = f"{art}-{ver}-{cls}.{ext}" if cls else f"{art}-{ver}.{ext}"
    return f"{grp.replace('.', '/')}/{art}/{ver}/{fname}"


def _ensure_vanilla_mojang_layout(base_dir: Path, mc_version: str, cb=None, json_mode=False) -> Tuple[bool, Optional[Path]]:
    """Ensure Mojang-standard versions/<mc>/<mc>.json + .jar and launcher_profiles.json exist."""
    import download_minecraft

    _emit_progress(cb, 8, f"Ensuring vanilla base Minecraft {mc_version}...", json_mode)
    ok = download_minecraft.download_version(mc_version, jar_type="client", json_mode=False, extract_source=False)
    if not ok:
        return False, None

    v_json = get_version_json(mc_version, resolve_inheritance=False)
    v_dir = get_dir("versions")
    std_dir = v_dir / mc_version
    std_dir.mkdir(parents=True, exist_ok=True)
    std_json = std_dir / f"{mc_version}.json"
    if v_json and not std_json.exists():
        std_json.write_text(json.dumps(v_json, indent=2), encoding="utf-8")

    jar_src_candidates = [
        v_dir / f"{mc_version}-client.jar",
        v_dir / f"{mc_version}.jar",
        std_dir / f"{mc_version}.jar",
        std_dir / f"{mc_version}-client.jar",
    ]
    std_jar = std_dir / f"{mc_version}.jar"
    if not std_jar.exists():
        for src in jar_src_candidates:
            if src.exists() and src.stat().st_size > 0:
                shutil.copy2(src, std_jar)
                break

    lp_path = base_dir / "launcher_profiles.json"
    if not lp_path.exists():
        lp_path.write_text(json.dumps({"profiles": {}}, indent=2), encoding="utf-8")

    return std_jar.exists(), std_jar


def _download_version_json_libraries(version_json: dict, libs_dir: Path, cb=None, json_mode=False, start_pct=45.0, end_pct=95.0) -> List[str]:
    """Download libraries declared by a mod loader profile in parallel."""
    libs = list(version_json.get("libraries") or [])
    tasks = []
    seen_dest = set()
    for lib in libs:
        if not isinstance(lib, dict):
            continue
        rel_path = None
        url = None
        sha1 = None
        dl = lib.get("downloads") or {}
        if isinstance(dl.get("artifact"), dict) and dl["artifact"].get("url"):
            rel_path = dl["artifact"].get("path")
            url = dl["artifact"].get("url")
            sha1 = dl["artifact"].get("sha1")
        elif lib.get("name"):
            rel_path = _maven_rel_path(lib["name"])
            if rel_path:
                base_u = (lib.get("url") or "https://libraries.minecraft.net/").rstrip("/")
                url = f"{base_u}/{rel_path}"
                sha1 = lib.get("sha1")
        if not rel_path or not url:
            continue
        dest = libs_dir / rel_path
        if dest.exists() and dest.stat().st_size > 0:
            continue
        key = str(dest).lower()
        if key in seen_dest:
            continue
        seen_dest.add(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tasks.append((url, dest, sha1))

    if not tasks:
        return []

    total = len(tasks)
    completed = 0
    failed: List[str] = []
    lock = threading.Lock()
    _emit_progress(cb, start_pct, f"Downloading {total} loader libraries in parallel...", json_mode)

    def _dl_one(url: str, dest: Path, sha1: Optional[str]) -> Tuple[bool, str]:
        ok = download_file(url, dest, expected_sha1=sha1, show_progress=False, json_output=False)
        return ok, dest.name

    workers = min(12, total)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_dl_one, u, d, s) for (u, d, s) in tasks]
        for fut in as_completed(futures):
            ok, fname = fut.result()
            with lock:
                completed += 1
                if not ok:
                    failed.append(fname)
                pct = start_pct + (completed / total) * (end_pct - start_pct)
                _emit_progress(cb, pct, f"Downloading loader libraries ({completed}/{total}): {fname}", json_mode)

    return failed


def _install_meta_loader(loader_id: str, mc_version: str, loader_ver: str, cb=None, json_mode=False) -> Tuple[bool, str, str]:
    base_url = _META_ENDPOINTS[loader_id]
    prof_url = f"{base_url}/versions/loader/{urllib.parse.quote(mc_version)}/{urllib.parse.quote(loader_ver)}/profile/json"
    _emit_progress(cb, 30, f"Fetching {loader_id} profile for {mc_version} ({loader_ver})...", json_mode)
    prof_json = _http_get_json(prof_url, timeout=20)
    if not isinstance(prof_json, dict) or not prof_json.get("mainClass"):
        return False, "", f"Failed to fetch profile JSON from {prof_url}."

    ver_id = str(prof_json.get("id") or f"{loader_id}-loader-{loader_ver}-{mc_version}")
    if "inheritsFrom" not in prof_json:
        prof_json["inheritsFrom"] = mc_version

    failed = _download_version_json_libraries(prof_json, get_dir("libraries"), cb=cb, json_mode=json_mode)
    if failed:
        return False, "", f"Failed to download {len(failed)} required library file(s): {', '.join(failed[:3])}"

    v_dir = get_dir("versions") / ver_id
    v_dir.mkdir(parents=True, exist_ok=True)
    (v_dir / f"{ver_id}.json").write_text(json.dumps(prof_json, indent=2), encoding="utf-8")

    _emit_progress(cb, 100, f"Installed {ver_id}", json_mode)
    return True, ver_id, f"Successfully installed {ver_id}."


def _resolve_forge_mojang_root(base_dir: Path, versions_dir: Path, libs_dir: Path) -> Path:
    """Return the Mojang-standard root directory containing both versions/ and libraries/."""
    v_res = versions_dir.resolve()
    l_res = libs_dir.resolve()
    if v_res == (base_dir / "versions").resolve() and l_res == (base_dir / "libraries").resolve():
        return base_dir
    if v_res.name == "versions" and l_res.name == "libraries" and v_res.parent.resolve() == l_res.parent.resolve():
        return v_res.parent.resolve()
    return base_dir


def _copy_tree_merge(src: Path, dest: Path):
    """Merge files from src directory tree into dest directory tree."""
    if not src.exists() or src.resolve() == dest.resolve():
        return
    dest.mkdir(parents=True, exist_ok=True)
    for root, _dirs, files in os.walk(src):
        rel = Path(root).relative_to(src)
        target_dir = dest / rel
        target_dir.mkdir(parents=True, exist_ok=True)
        for fname in files:
            s_file = Path(root) / fname
            d_file = target_dir / fname
            if not d_file.exists() or d_file.stat().st_size == 0:
                shutil.copy2(s_file, d_file)


def _sync_forge_outputs(
    mojang_root: Path,
    versions_dir: Path,
    libs_dir: Path,
    expected_ver_id: str,
    mc_version: str,
    loader_id: str,
) -> Optional[str]:
    """Locate the generated version JSON under mojang_root/versions or versions_dir and sync outputs."""
    m_vers = mojang_root / "versions"
    m_libs = mojang_root / "libraries"

    cand_ids = [expected_ver_id] if expected_ver_id else []
    for search_root in (versions_dir, m_vers):
        if not search_root.exists():
            continue
        for vid in cand_ids:
            if (search_root / vid / f"{vid}.json").is_file():
                if search_root.resolve() != versions_dir.resolve():
                    _copy_tree_merge(search_root / vid, versions_dir / vid)
                _copy_tree_merge(m_libs, libs_dir)
                return vid

    # Scan for a newly written forge/neoforge version matching mc_version
    for search_root in (versions_dir, m_vers):
        if not search_root.exists():
            continue
        for d in sorted(search_root.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True):
            if d.is_dir() and mc_version in d.name and loader_id in d.name.lower():
                if (d / f"{d.name}.json").is_file():
                    if search_root.resolve() != versions_dir.resolve():
                        _copy_tree_merge(d, versions_dir / d.name)
                    _copy_tree_merge(m_libs, libs_dir)
                    return d.name
    return None


def _run_forge_installer_popen(
    cmd: List[str],
    cwd: Path,
    loader_id: str,
    cb=None,
    json_mode: bool = False,
) -> Tuple[int, str]:
    """Run Forge/NeoForge --installClient via Popen and stream parsed progress cleanly."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=flags,
    )
    with _ACTIVE_PROCS_LOCK:
        _ACTIVE_INSTALLER_PROCS.add(proc)

    tail_lines: List[str] = []
    pct = 48.0
    total_patches = 0
    patched_count = 0
    last_emit_ts = 0.0

    def _emit_now(new_pct: float, msg: str, force: bool = False):
        nonlocal pct, last_emit_ts
        pct = max(pct, min(97.0, float(new_pct)))
        now = time.monotonic()
        if force or (now - last_emit_ts) >= 0.12:
            last_emit_ts = now
            _emit_progress(cb, pct, msg, json_mode)

    try:
        if proc.stdout is not None:
            for raw_line in iter(proc.stdout.readline, ""):
                line = raw_line.strip()
                if not line:
                    continue
                tail_lines.append(line)
                if len(tail_lines) > 40:
                    tail_lines.pop(0)

                low = line.lower()
                if "considering library" in low or "downloading library" in low or "downloading" in low:
                    _emit_now(min(62.0, pct + 0.35), f"Downloading {loader_id} installer libraries...")
                elif "building processors" in low:
                    _emit_now(64.0, f"Building {loader_id} processors...", force=True)
                elif "loading patches file" in low:
                    _emit_now(65.0, f"Loading {loader_id} binary patches...", force=True)
                elif low.startswith("loaded ") and "patch" in low:
                    m = re.search(r"loaded\s+(\d+)\s+patch", low)
                    if m:
                        total_patches = max(1, int(m.group(1)))
                    _emit_now(66.0, f"Applying {total_patches or ''} client patches...", force=True)
                elif line.startswith("Patching "):
                    patched_count += 1
                    if total_patches > 0:
                        frac = min(1.0, patched_count / total_patches)
                        _emit_now(66.0 + frac * 28.0, f"Patching client classes ({patched_count}/{total_patches})...")
                    elif patched_count % 25 == 0:
                        _emit_now(min(93.0, 66.0 + patched_count * 0.02), f"Patching client classes ({patched_count})...")
                elif "task:" in low or "remapping" in low or "splitting" in low:
                    _emit_now(min(94.0, pct + 2.0), f"{loader_id}: {line[:70]}", force=True)
                elif "successfully installed" in low or "finished!" in low:
                    _emit_now(97.0, f"Finalizing {loader_id} profile...", force=True)

        proc.wait(timeout=300)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        return -1, "Installer timed out after 300 seconds."
    finally:
        with _ACTIVE_PROCS_LOCK:
            _ACTIVE_INSTALLER_PROCS.discard(proc)
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except Exception:
                pass

    return int(proc.returncode or 0), "\n".join(tail_lines)


def _install_forge_or_neoforge(
    loader_id: str,
    mc_version: str,
    version_entry: dict,
    base_dir: Path,
    java_path: Optional[str] = None,
    cb=None,
    json_mode=False,
) -> Tuple[bool, str, str]:
    import download_java
    import logging

    dl_url = version_entry.get("download_url")
    if not dl_url:
        return False, "", f"No installer download URL available for {loader_id} {version_entry.get('version')}."

    with tempfile.TemporaryDirectory(prefix="pymc_forge_inst_", ignore_cleanup_errors=True) as td:
        installer_jar = Path(td) / "installer.jar"
        _emit_progress(cb, 18, f"Downloading {loader_id} installer...", json_mode)
        if not download_file(dl_url, installer_jar, show_progress=False, json_output=False):
            return False, "", f"Failed to download installer from {dl_url}."

        try:
            with zipfile.ZipFile(installer_jar, "r") as zf:
                names = set(zf.namelist())
                if "install_profile.json" not in names:
                    return False, "", "Downloaded archive does not contain install_profile.json."
                prof_data = json.loads(zf.read("install_profile.json").decode("utf-8"))
        except Exception as e:
            return False, "", f"Invalid installer archive: {e}"

        expected_ver_id = str(prof_data.get("version") or (prof_data.get("install") or {}).get("version") or "")
        versions_dir = get_dir("versions")
        libs_dir = get_dir("libraries")

        # Legacy Forge (<1.13) uses {"install": ..., "versionInfo": ...} and doesn't need base MC JAR or Java at install time
        if "install" in prof_data and "versionInfo" in prof_data:
            _emit_progress(cb, 45, f"Extracting legacy Forge {expected_ver_id}...", json_mode)
            ver_info = dict(prof_data["versionInfo"])
            expected_ver_id = str(ver_info.get("id") or expected_ver_id)
            if "inheritsFrom" not in ver_info:
                ver_info["inheritsFrom"] = mc_version
            v_out = versions_dir / expected_ver_id
            v_out.mkdir(parents=True, exist_ok=True)
            (v_out / f"{expected_ver_id}.json").write_text(json.dumps(ver_info, indent=2), encoding="utf-8")

            # Extract bundled universal jar to Maven library path if present
            inst_meta = prof_data.get("install") or {}
            file_path_inside = inst_meta.get("filePath")
            target_coord = inst_meta.get("path")
            if file_path_inside and target_coord:
                rel_p = _maven_rel_path(target_coord)
                if rel_p:
                    dest_lib = libs_dir / rel_p
                    dest_lib.parent.mkdir(parents=True, exist_ok=True)
                    with zipfile.ZipFile(installer_jar, "r") as zf:
                        if file_path_inside in zf.namelist():
                            dest_lib.write_bytes(zf.read(file_path_inside))

            failed_libs = _download_version_json_libraries(ver_info, libs_dir, cb=cb, json_mode=json_mode, start_pct=60.0, end_pct=95.0)
            if failed_libs:
                try:
                    (v_out / f"{expected_ver_id}.json").unlink(missing_ok=True)
                except Exception:
                    pass
                return False, "", f"Failed to download legacy Forge library file(s): {', '.join(failed_libs[:3])}"
            _emit_progress(cb, 100, f"Installed {expected_ver_id}", json_mode)
            return True, expected_ver_id, f"Successfully installed {expected_ver_id}."

        # Modern Forge (1.13+) & NeoForge (--installClient) require vanilla base JAR + Java to remap/patch client classes
        mojang_root = _resolve_forge_mojang_root(base_dir, versions_dir, libs_dir)
        mojang_root.mkdir(parents=True, exist_ok=True)

        ok_base, vanilla_jar = _ensure_vanilla_mojang_layout(mojang_root, mc_version, cb=cb, json_mode=json_mode)
        if not ok_base or not vanilla_jar:
            return False, "", f"Failed to prepare base Minecraft {mc_version} required by {loader_id} installer."

        # Pre-seed mojang_root/versions/<mc_version> if versions_dir lives outside mojang_root
        if (mojang_root / "versions").resolve() != versions_dir.resolve():
            _copy_tree_merge(versions_dir / mc_version, mojang_root / "versions" / mc_version)
        lp_root = mojang_root / "launcher_profiles.json"
        if not lp_root.exists():
            lp_root.write_text(json.dumps({"profiles": {}}, indent=2), encoding="utf-8")

        _emit_progress(cb, 35, f"Ensuring Java runtime for {mc_version}...", json_mode)
        download_java.ensure_java_for_version(mc_version, json_mode=False)
        if not java_path:
            import download_java as dj
            v_json = get_version_json(mc_version, resolve_inheritance=True)
            if v_json and isinstance(v_json.get("javaVersion"), dict):
                comp = v_json["javaVersion"].get("component", "java-runtime-gamma")
                maj = v_json["javaVersion"].get("majorVersion", 17)
                found = dj.find_existing_java_runtime(comp, maj)
                if not found:
                    cand = dj.get_java_executable_in_home(get_dir("java") / comp)
                    if cand.exists():
                        found = cand
                if found:
                    java_path = str(found.absolute())
        java_exec = java_path or "java"

        # Modern Forge (1.13+) & NeoForge: execute official headless --installClient with live progress streaming
        _emit_progress(cb, 48, f"Running {loader_id} client installer...", json_mode)
        rc, proc_tail = _run_forge_installer_popen(
            [java_exec, "-jar", str(installer_jar), "--installClient", str(mojang_root)],
            cwd=mojang_root,
            loader_id=loader_id,
            cb=cb,
            json_mode=json_mode,
        )

        found_vid = _sync_forge_outputs(mojang_root, versions_dir, libs_dir, expected_ver_id, mc_version, loader_id)
        if rc != 0 or not found_vid:
            # Fallback to minecraft_launcher_lib for Forge with live progress callback & quiet urllib3 pool warnings
            if loader_id == "forge":
                pool_logger = logging.getLogger("urllib3.connectionpool")
                prev_lvl = pool_logger.level
                pool_logger.setLevel(logging.ERROR)
                mll_state = {"max": 100, "status": f"Installing Forge via helper..."}

                def _mll_set_max(m_val: int):
                    mll_state["max"] = max(1, int(m_val or 1))

                def _mll_set_status(st: str):
                    if st:
                        mll_state["status"] = str(st)
                        _emit_progress(cb, 62.0, str(st), json_mode)

                def _mll_set_prog(p_val: int):
                    frac = max(0.0, min(1.0, float(p_val) / max(1, mll_state["max"])))
                    pct_mapped = 55.0 + frac * 42.0
                    _emit_progress(cb, pct_mapped, mll_state["status"], json_mode)

                try:
                    import minecraft_launcher_lib as mll
                    _emit_progress(cb, 55, f"Retrying Forge install via minecraft-launcher-lib...", json_mode)
                    mll.forge.install_forge_version(
                        version_entry["full_id"],
                        str(mojang_root),
                        callback={"setMax": _mll_set_max, "setStatus": _mll_set_status, "setProgress": _mll_set_prog},
                        java=java_exec,
                    )
                    found_vid = _sync_forge_outputs(mojang_root, versions_dir, libs_dir, expected_ver_id, mc_version, loader_id)
                except Exception as mll_err:
                    err_tail = (proc_tail or str(mll_err)).strip()[-400:]
                    return False, "", f"{loader_id} installer failed: {err_tail}"
                finally:
                    pool_logger.setLevel(prev_lvl)
            else:
                err_tail = (proc_tail or "Unknown error").strip()[-400:]
                return False, "", f"{loader_id} installer failed: {err_tail}"

        if found_vid and (versions_dir / found_vid / f"{found_vid}.json").is_file():
            _emit_progress(cb, 100, f"Installed {found_vid}", json_mode)
            return True, found_vid, f"Successfully installed {found_vid}."

        ver_json_path = versions_dir / expected_ver_id / f"{expected_ver_id}.json"
        return False, "", f"Installer finished without generating {ver_json_path}."


def _install_liteloader(mc_version: str, version_entry: dict, cb=None, json_mode=False) -> Tuple[bool, str, str]:
    lver = str(version_entry.get("version") or mc_version)
    extra = version_entry.get("extra") or {}
    tweak_class = extra.get("tweakClass") or "com.mumfrey.liteloader.launch.LiteLoaderTweaker"
    repo_url = (extra.get("repo_url") or "https://dl.liteloader.com/versions/").rstrip("/")
    jar_file = extra.get("file") or ""
    ver_id = f"{mc_version}-LiteLoader{lver}"

    libs: List[Dict] = []
    if jar_file:
        coords = f"com.mumfrey:liteloader:{lver}"
        rel_p = _maven_rel_path(coords)
        libs.append({"name": coords, "url": repo_url + "/"})
        if rel_p:
            dl_target = get_dir("libraries") / rel_p
            if not dl_target.exists():
                jar_url = f"{repo_url}/{rel_p}"
                if not download_file(jar_url, dl_target, show_progress=False):
                    # Legacy flat url under versions repo
                    download_file(f"{repo_url}/{mc_version}/{jar_file}", dl_target, show_progress=False)

    for dep in (extra.get("libraries") or []):
        if isinstance(dep, dict) and dep.get("name"):
            dep_copy = dict(dep)
            if not dep_copy.get("url") and "liteloader" in str(dep_copy["name"]).lower():
                dep_copy["url"] = repo_url + "/"
            libs.append(dep_copy)

    parent_json = get_version_json(mc_version, resolve_inheritance=True) or {}
    profile = {
        "id": ver_id,
        "inheritsFrom": mc_version,
        "type": "release",
        "mainClass": "net.minecraft.launchwrapper.Launch",
        "libraries": libs,
    }
    if isinstance(parent_json.get("arguments"), dict):
        profile["arguments"] = {"game": ["--tweakClass", tweak_class]}
    else:
        parent_mc_args = parent_json.get("minecraftArguments") or ""
        profile["minecraftArguments"] = f"{parent_mc_args} --tweakClass {tweak_class}".strip()

    failed = _download_version_json_libraries(profile, get_dir("libraries"), cb=cb, json_mode=json_mode, start_pct=50.0, end_pct=95.0)
    if failed:
        return False, "", f"Failed to download LiteLoader library file(s): {', '.join(failed[:3])}"

    v_out = get_dir("versions") / ver_id
    v_out.mkdir(parents=True, exist_ok=True)
    (v_out / f"{ver_id}.json").write_text(json.dumps(profile, indent=2), encoding="utf-8")

    _emit_progress(cb, 100, f"Installed {ver_id}", json_mode)
    return True, ver_id, f"Successfully installed {ver_id}."


def _install_nilloader(mc_version: str, version_entry: dict, cb=None, json_mode=False) -> Tuple[bool, str, str]:
    nver = str(version_entry.get("version") or "1.3.0")
    dl_url = version_entry.get("download_url") or f"https://repo.sleeping.town/com/unascribed/nilloader/{nver}/nilloader-{nver}.jar"
    ver_id = f"{mc_version}-nilloader-{nver}"

    rel_path = _maven_rel_path(f"com.unascribed:nilloader:{nver}")
    jar_dest = get_dir("libraries") / rel_path
    _emit_progress(cb, 45, f"Downloading NilLoader {nver}...", json_mode)
    if not jar_dest.exists() and not download_file(dl_url, jar_dest, show_progress=False):
        return False, "", f"Failed to download NilLoader from {dl_url}."

    profile = {
        "id": ver_id,
        "inheritsFrom": mc_version,
        "type": "release",
        "arguments": {
            "jvm": [f"-javaagent:{str(jar_dest.resolve())}"],
        },
        "libraries": [],
    }
    v_out = get_dir("versions") / ver_id
    v_out.mkdir(parents=True, exist_ok=True)
    (v_out / f"{ver_id}.json").write_text(json.dumps(profile, indent=2), encoding="utf-8")
    _emit_progress(cb, 100, f"Installed {ver_id}", json_mode)
    return True, ver_id, f"Successfully installed {ver_id}."


def _install_rift(mc_version: str, version_entry: dict, cb=None, json_mode=False) -> Tuple[bool, str, str]:
    rver = str(version_entry.get("full_id") or "final")
    dl_url = version_entry.get("download_url")
    tweak_class = (version_entry.get("extra") or {}).get("tweak_class") or "org.dimdev.riftloader.launch.RiftLoaderClient"
    ver_id = f"{mc_version}-rift-{rver}"

    coords = f"org.dimdev:rift:{rver}"
    rel_p = _maven_rel_path(coords)
    jar_dest = get_dir("libraries") / rel_p
    _emit_progress(cb, 45, f"Downloading Rift {rver}...", json_mode)
    if dl_url and not jar_dest.exists():
        if not download_file(dl_url, jar_dest, show_progress=False):
            return False, "", f"Failed to download Rift JAR from {dl_url}."

    libs = [
        {"name": coords},
        {"name": "net.minecraft:launchwrapper:1.12", "url": "https://libraries.minecraft.net/"},
        {"name": "org.ow2.asm:asm-all:5.2", "url": "https://repo1.maven.org/maven2/"},
        {"name": "org.spongepowered:mixin:0.7.11-SNAPSHOT", "url": "https://repo.spongepowered.org/maven/"},
    ]
    profile = {
        "id": ver_id,
        "inheritsFrom": mc_version,
        "type": "release",
        "mainClass": "net.minecraft.launchwrapper.Launch",
        "arguments": {
            "game": ["--tweakClass", tweak_class],
        },
        "libraries": libs,
    }
    failed = _download_version_json_libraries({"libraries": libs[1:]}, get_dir("libraries"), cb=cb, json_mode=json_mode, start_pct=60.0, end_pct=95.0)
    if failed:
        return False, "", f"Failed to download Rift library file(s): {', '.join(failed[:3])}"

    v_out = get_dir("versions") / ver_id
    v_out.mkdir(parents=True, exist_ok=True)
    (v_out / f"{ver_id}.json").write_text(json.dumps(profile, indent=2), encoding="utf-8")

    _emit_progress(cb, 100, f"Installed {ver_id}", json_mode)
    return True, ver_id, f"Successfully installed {ver_id}."


def _install_bta_babric(mc_version: str, version_entry: dict, cb=None, json_mode=False) -> Tuple[bool, str, str]:
    extra = version_entry.get("extra") or {}
    babric_loader = extra.get("babric_loader") or ""
    tag = str(version_entry.get("full_id") or "bta")
    dl_url = version_entry.get("download_url")

    if babric_loader:
        ok, ver_id, msg = _install_meta_loader("babric", "b1.7.3", babric_loader, cb=cb, json_mode=json_mode)
        if ok and dl_url:
            mods_dir = get_dir("base") / "mods"
            mods_dir.mkdir(parents=True, exist_ok=True)
            bta_jar = mods_dir / f"bta-{tag}.jar"
            if not bta_jar.exists():
                download_file(dl_url, bta_jar, show_progress=False)
        if ok:
            return True, ver_id, f"Installed BTA ({tag}) via {ver_id}."

    return False, "", "Could not resolve Babric loader for BTA."


def _install_risugami_or_jarmod(
    mc_version: str,
    version_entry: dict,
    vanilla_jar: Path,
    local_file: Optional[str] = None,
    cb=None,
    json_mode=False,
) -> Tuple[bool, str, str]:
    if not local_file:
        return (
            False,
            "",
            f"Risugami's ModLoader requires a local ModLoader .zip/.jar file for Minecraft {mc_version}.\n"
            f"You can obtain it at {(version_entry.get('download_url') or 'https://mcarchive.net/mods/modloader')} and browse for the archive above.",
        )

    patch_path = Path(local_file)
    if not patch_path.exists():
        return False, "", f"Selected archive not found: {local_file}"

    tag_ver = str(version_entry.get("full_id") or mc_version)
    ver_id = f"{mc_version}-modloader-{tag_ver}"
    v_out = get_dir("versions") / ver_id
    v_out.mkdir(parents=True, exist_ok=True)
    out_jar = v_out / f"{ver_id}.jar"

    _emit_progress(cb, 50, f"Patching {mc_version} JAR with {patch_path.name}...", json_mode)
    mod_entries: Dict[str, bytes] = {}
    with zipfile.ZipFile(patch_path, "r") as pz:
        for member in pz.infolist():
            if not member.is_dir() and not member.filename.upper().startswith("META-INF/"):
                mod_entries[member.filename] = pz.read(member)

    with zipfile.ZipFile(vanilla_jar, "r") as vz, zipfile.ZipFile(out_jar, "w", zipfile.ZIP_DEFLATED) as oz:
        for member in vz.infolist():
            if member.is_dir() or member.filename.upper().startswith("META-INF/"):
                continue
            if member.filename in mod_entries:
                continue
            oz.writestr(member.filename, vz.read(member))
        for fname, data in mod_entries.items():
            oz.writestr(fname, data)

    parent_json = get_version_json(mc_version, resolve_inheritance=True) or {}
    profile = {
        "id": ver_id,
        "inheritsFrom": mc_version,
        "type": parent_json.get("type", "release"),
        "jar": ver_id,
        "libraries": [],
    }
    (v_out / f"{ver_id}.json").write_text(json.dumps(profile, indent=2), encoding="utf-8")
    _emit_progress(cb, 100, f"Installed {ver_id}", json_mode)
    return True, ver_id, f"Successfully patched and installed {ver_id}."


def _install_java_agent(
    mc_version: str,
    local_file: Optional[str],
    agent_args: Optional[str] = None,
    cb=None,
    json_mode=False,
) -> Tuple[bool, str, str]:
    if not local_file:
        return False, "", "Please select a .jar file for the Java Agent."
    src = Path(local_file)
    if not src.exists():
        return False, "", f"Java Agent file not found: {local_file}"

    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", src.stem).strip("-") or "agent"
    ver_id = f"{mc_version}-javaagent-{safe_stem}"

    agents_dir = get_dir("libraries") / "custom_agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    dest_jar = agents_dir / src.name
    shutil.copy2(src, dest_jar)

    jvm_flag = f"-javaagent:{str(dest_jar.resolve())}"
    if agent_args and agent_args.strip():
        jvm_flag += f"={agent_args.strip()}"

    profile = {
        "id": ver_id,
        "inheritsFrom": mc_version,
        "type": "release",
        "arguments": {
            "jvm": [jvm_flag],
        },
        "libraries": [],
    }
    v_out = get_dir("versions") / ver_id
    v_out.mkdir(parents=True, exist_ok=True)
    (v_out / f"{ver_id}.json").write_text(json.dumps(profile, indent=2), encoding="utf-8")
    _emit_progress(cb, 100, f"Installed {ver_id}", json_mode)
    return True, ver_id, f"Successfully installed Java Agent profile {ver_id}."


def install_mod_loader(
    loader_id: str,
    mc_version: str,
    loader_version_entry: dict,
    java_path: Optional[str] = None,
    local_file: Optional[str] = None,
    agent_args: Optional[str] = None,
    progress_callback: Optional[Callable[[float, str], None]] = None,
    json_mode: bool = False,
) -> Tuple[bool, str, str]:
    """Install a mod loader profile + dependencies and return (success, version_id, message)."""
    loader_id = (loader_id or "").lower().strip()
    base_dir = get_dir("base").resolve()
    base_dir.mkdir(parents=True, exist_ok=True)

    # Only classic jar-patchers (Risugami) need the vanilla JAR upfront here;
    # modern Forge/NeoForge check inside _install_forge_or_neoforge, and Meta/LiteLoader/Rift/NilLoader/Java Agent inherit cleanly.
    if loader_id in _META_ENDPOINTS:
        ok, ver_id, msg = _install_meta_loader(
            loader_id, mc_version, str(loader_version_entry.get("full_id") or loader_version_entry.get("version")),
            cb=progress_callback, json_mode=json_mode,
        )
    elif loader_id in ("forge", "neoforge"):
        ok, ver_id, msg = _install_forge_or_neoforge(
            loader_id, mc_version, loader_version_entry, base_dir,
            java_path=java_path, cb=progress_callback, json_mode=json_mode,
        )
    elif loader_id == "liteloader":
        ok, ver_id, msg = _install_liteloader(mc_version, loader_version_entry, cb=progress_callback, json_mode=json_mode)
    elif loader_id == "nilloader":
        ok, ver_id, msg = _install_nilloader(mc_version, loader_version_entry, cb=progress_callback, json_mode=json_mode)
    elif loader_id == "rift":
        ok, ver_id, msg = _install_rift(mc_version, loader_version_entry, cb=progress_callback, json_mode=json_mode)
    elif loader_id == "bta_babric":
        ok, ver_id, msg = _install_bta_babric(mc_version, loader_version_entry, cb=progress_callback, json_mode=json_mode)
    elif loader_id == "risugami":
        ok_base, vanilla_jar = _ensure_vanilla_mojang_layout(base_dir, mc_version, cb=progress_callback, json_mode=json_mode)
        if not ok_base or not vanilla_jar:
            return False, "", f"Failed to download base Minecraft {mc_version} for JAR patching."
        ok, ver_id, msg = _install_risugami_or_jarmod(
            mc_version, loader_version_entry, vanilla_jar, local_file=local_file,
            cb=progress_callback, json_mode=json_mode,
        )
    elif loader_id == "java_agent":
        ok, ver_id, msg = _install_java_agent(
            mc_version, local_file=local_file, agent_args=agent_args,
            cb=progress_callback, json_mode=json_mode,
        )
    else:
        return False, "", f"Unsupported loader: {loader_id}"

    if ok:
        try:
            sync_existing_version_configs()
        except Exception:
            pass
    return ok, ver_id, msg


def main():
    parser = argparse.ArgumentParser(description="PyMcLauncher Mod Loaders CLI & Indexer")
    parser.add_argument("--list-loaders", action="store_true", help="List all supported mod loaders")
    parser.add_argument("--mc-versions", action="store_true", help="List supported Minecraft versions for --loader")
    parser.add_argument("--loader-versions", action="store_true", help="List loader builds for --loader and --mc-version")
    parser.add_argument("--install", action="store_true", help="Install a mod loader version")
    parser.add_argument("--loader", type=str, help="Loader ID (e.g. fabric, forge, neoforge, quilt)")
    parser.add_argument("--mc-version", type=str, help="Minecraft version (e.g. 1.20.1)")
    parser.add_argument("--loader-version", type=str, help="Loader build version string")
    parser.add_argument("--local-file", type=str, default=None, help="Local JAR/ZIP for Java Agent or Risugami's ModLoader")
    parser.add_argument("--agent-args", type=str, default=None, help="Optional arguments for Java Agent")
    parser.add_argument("--snapshots", action="store_true", help="Include snapshot Minecraft versions")
    parser.add_argument("--refresh", action="store_true", help="Force refresh remote indexes")
    parser.add_argument("--json", action="store_true", help="Output JSON format")

    if len(sys.argv) == 1:
        parser.print_help()
        return

    args = parser.parse_args()

    if args.list_loaders:
        loaders = list_mod_loaders()
        if args.json:
            print(json.dumps(loaders, indent=2))
        else:
            for item in loaders:
                print(f"{item['id']:<16} {item['name']:<22} {item['description']}")
        return

    if args.mc_versions:
        if not args.loader:
            print("Error: --loader is required with --mc-versions", file=sys.stderr)
            sys.exit(1)
        mc_vers = get_minecraft_versions_for_loader(args.loader, include_snapshots=args.snapshots, refresh=args.refresh)
        if args.json:
            print(json.dumps(mc_vers, indent=2))
        else:
            for v in mc_vers:
                print(f"{v['id']} ({v['type']})")
        return

    if args.loader_versions:
        if not args.loader or not args.mc_version:
            print("Error: --loader and --mc-version are required", file=sys.stderr)
            sys.exit(1)
        l_vers = get_loader_versions(args.loader, args.mc_version, refresh=args.refresh)
        if args.json:
            print(json.dumps(l_vers, indent=2))
        else:
            for v in l_vers:
                tag = f" [{v['tag']}]" if v.get("tag") else ""
                print(f"{v['version']}{tag}")
        return

    if args.install:
        if not args.loader or not args.mc_version:
            print("Error: --loader and --mc-version are required for --install", file=sys.stderr)
            sys.exit(1)
        l_vers = get_loader_versions(args.loader, args.mc_version, refresh=args.refresh)
        selected = None
        if args.loader_version:
            for v in l_vers:
                if v.get("version") == args.loader_version or v.get("full_id") == args.loader_version:
                    selected = v
                    break
        elif l_vers:
            selected = l_vers[0]

        if not selected:
            print(f"Error: Loader version not found for {args.loader} / {args.mc_version}", file=sys.stderr)
            sys.exit(1)

        ok, ver_id, msg = install_mod_loader(
            args.loader,
            args.mc_version,
            selected,
            local_file=args.local_file,
            agent_args=args.agent_args,
            json_mode=args.json,
        )
        if args.json:
            print(json.dumps({"success": ok, "version_id": ver_id, "message": msg}))
        else:
            print(msg)
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
