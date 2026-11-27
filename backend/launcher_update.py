"""
Update checking, changelog display, and requirements installation for PyMcLauncher.

Features:
- Startup check: shows "Up to date - version {version}" or "Update available: {current} -> {latest}" at the bottom.
- When clicking Update: shows version changes from the older version to the newer version in Markdown format
  (built from commit descriptions via GitHub compare API) with an option to proceed with installation.
- After updating & restarting: automatically displays the Markdown changelog of what changed.
- Requirements check: checks if requirements.txt changed or any package/dependency is not installed/outdated.
  If needed, launches pip install with a progress bar and status output.
"""
import base64
import concurrent.futures
import hashlib
import json
import os
import platform
import re
import shutil
import ssl
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from PyQt6.QtCore import Qt, QThread, QTimer, QProcess, pyqtSignal
    from PyQt6.QtGui import QFont
    from PyQt6.QtWidgets import (
        QApplication, QComboBox, QDialog, QFormLayout, QGroupBox, QHBoxLayout,
        QLabel, QMessageBox, QProgressBar, QPushButton, QTextBrowser, QTextEdit,
        QVBoxLayout,
    )
    HAS_PYQT6 = True
except ImportError:
    HAS_PYQT6 = False
    Qt = QTimer = QProcess = QFont = None

    class QThread:  # type: ignore[no-redef]
        pass

    class QDialog:  # type: ignore[no-redef]
        pass

    def pyqtSignal(*args, **kwargs):  # type: ignore[no-redef]
        return None

OWNER = "cloudyhair6"
REPO = "PyMcLauncher"
USER_AGENT = "Python-Latest-Version-Detector"
DEFAULT_OPTION = "Minecraft Launcher for PC"
DEFAULT_LAUNCHER_VERSION = "v3.1-pre03"


def _resolve_root_dir() -> Path:
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        if sys.platform == "darwin":
            for parent in exe.parents:
                if parent.suffix == ".app":
                    app_parent = parent.parent
                    if os.access(app_parent, os.W_OK):
                        return app_parent
                    support_dir = Path.home() / "Library" / "Application Support" / "PyMcLauncher"
                    support_dir.mkdir(parents=True, exist_ok=True)
                    return support_dir
        return exe.parent
    pkg_dir = Path(__file__).resolve().parent
    return pkg_dir.parent if pkg_dir.name == "backend" else pkg_dir


def _resolve_resource_dir() -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and meipass:
        return Path(meipass)
    pkg_dir = Path(__file__).resolve().parent
    return pkg_dir.parent if pkg_dir.name == "backend" else pkg_dir


_pkg_dir = Path(__file__).resolve().parent
ROOT_DIR = _resolve_root_dir()
RESOURCE_DIR = _resolve_resource_dir()
CONFIG_FILE = "launcher_config.json"
LEGACY_CONFIG_FILE = "updater_config.json"
CONFIG_PATH = ROOT_DIR / CONFIG_FILE
LEGACY_CONFIG_PATH = ROOT_DIR / LEGACY_CONFIG_FILE
REQUIREMENTS_PATH = ROOT_DIR / "backend" / "requirements.txt"
LEGACY_REQUIREMENTS_PATH = ROOT_DIR / "requirements.txt"
CACHE_DIR = ROOT_DIR / ".cache"
PENDING_PATH = CACHE_DIR / "update_pending.json"
REQ_HASH_PATH = CACHE_DIR / "requirements.sha256"
INSTALLER_PATH = ROOT_DIR / "installer.py"


def _get_requirements_path() -> Path:
    for base in (ROOT_DIR, RESOURCE_DIR):
        for rel in (Path("backend") / "requirements.txt", Path("requirements.txt")):
            cand = base / rel
            if cand.exists():
                return cand
    return REQUIREMENTS_PATH

_certs_ensured = False


def ensure_system_certificates():
    """
    Ensure SSL root certificates are installed and verified:
    - On macOS: runs 'Install Certificates.command' or installs/links certifi.
    - On Windows: ensures certifi is installed so OpenSSL CA bundle is up to date.
    """
    global _certs_ensured
    if _certs_ensured:
        return
    _certs_ensured = True

    if getattr(sys, "frozen", False):
        return

    sys_name = platform.system().lower()
    if sys_name == "darwin":
        print("[*] Checking macOS SSL certificates...", flush=True)
        py_ver = f"{sys.version_info.major}.{sys.version_info.minor}"
        candidates = [
            f"/Applications/Python {py_ver}/Install Certificates.command",
            os.path.join(os.path.dirname(sys.executable), "Install Certificates.command"),
            os.path.join(os.path.dirname(os.path.dirname(sys.executable)), "Install Certificates.command"),
        ]
        try:
            for app_dir in Path("/Applications").glob("Python 3.*"):
                cmd_file = app_dir / "Install Certificates.command"
                if cmd_file.exists():
                    candidates.append(str(cmd_file))
        except Exception:
            pass

        for cand in candidates:
            if os.path.exists(cand):
                try:
                    print(f"[*] Running macOS certificate installer: {cand}...", flush=True)
                    res = subprocess.run(["/bin/sh", cand], capture_output=True, text=True, timeout=45)
                    if res.returncode == 0:
                        print("[OK] Successfully ran Install Certificates.command on macOS", flush=True)
                        break
                    else:
                        print(f"[!] Certificate command warning (exit code {res.returncode}): {res.stderr.strip()}", file=sys.stderr, flush=True)
                except Exception as e:
                    print(f"[!] Could not run {cand}: {e}", file=sys.stderr, flush=True)

        try:
            import certifi  # noqa: F401
        except ImportError:
            print("[*] Installing certifi package for SSL certificate verification...", flush=True)
            try:
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "--upgrade", "certifi", "--quiet"],
                    capture_output=True,
                    timeout=45,
                )
            except Exception:
                pass

        try:
            import certifi
            cafile = ssl.get_default_verify_paths().openssl_cafile
            if cafile and not os.path.exists(cafile):
                os.makedirs(os.path.dirname(cafile), exist_ok=True)
                try:
                    os.symlink(certifi.where(), cafile)
                    print(f"[OK] Linked OpenSSL cafile to certifi ({cafile})", flush=True)
                except Exception:
                    shutil.copy2(certifi.where(), cafile)
                    print(f"[OK] Copied certifi certificates to {cafile}", flush=True)
        except Exception:
            pass

    elif sys_name == "windows":
        try:
            import certifi  # noqa: F401
        except ImportError:
            print("[*] Installing certifi for Windows SSL certificate verification...", flush=True)
            try:
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "certifi", "--quiet"],
                    capture_output=True,
                    timeout=45,
                    creationflags=creationflags,
                )
            except Exception as e:
                print(f"[!] Notice: certifi auto-install skipped: {e}", file=sys.stderr, flush=True)


def _get_default_token():
    try:
        payload = "PTIqBRMJMw4SNh0yNzIxMy4LABtiPSAxAhkTPRQ4MQo8Lmg8Ywtjag=="
        return bytes([b ^ 0x5A for b in base64.b64decode(payload)]).decode("utf-8")
    except Exception:
        return ""


def get_auth_headers(allow_token=True):
    headers = {"User-Agent": USER_AGENT}
    if allow_token:
        token = os.environ.get("GITHUB_TOKEN", "").strip() or _get_default_token()
        if token:
            headers["Authorization"] = f"token {token}"
    return headers


def _get_auth_headers():
    return get_auth_headers(allow_token=True)


class NoAuthRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req:
            new_req.headers = {k: v for k, v in new_req.headers.items() if k.lower() != "authorization"}
        return new_req


def is_ssl_verify_error(err):
    """Detect if an exception is an SSL certificate verification failure (common on macOS)."""
    if isinstance(err, ssl.SSLError):
        return True
    if isinstance(err, urllib.error.URLError):
        reason = getattr(err, "reason", None)
        if isinstance(reason, ssl.SSLError):
            return True
        r_str = str(reason).lower()
        if any(w in r_str for w in ("certificate", "ssl", "verify", "issuer", "handshake")):
            return True
    return False


def _create_ssl_context(verify=True):
    if not verify:
        try:
            return ssl._create_unverified_context()
        except Exception:
            return None
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    try:
        return ssl.create_default_context()
    except Exception:
        return None


def _download_via_curl(url, headers=None, timeout=30):
    """Fallback download using system curl (uses native macOS / system certificates)."""
    try:
        curl_bin = shutil.which("curl")
        if not curl_bin:
            return None
        cmd = [curl_bin, "-sSL", "--connect-timeout", "10", "--max-time", str(timeout)]
        if headers:
            for k, v in headers.items():
                cmd.extend(["-H", f"{k}: {v}"])
        cmd.append(url)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        res = subprocess.run(cmd, capture_output=True, timeout=timeout + 5, creationflags=flags)
        if res.returncode == 0 and res.stdout is not None:
            return res.stdout
    except Exception as e:
        print(f"[!] curl download error: {e}", file=sys.stderr, flush=True)
    return None


def download_raw_url(url, extra_headers=None, timeout=15):
    """
    Download raw content from a URL with automatic SSL certificate verification
    fallback (for macOS Python installs without certificates) and system curl fallback.
    """
    if not _certs_ensured:
        ensure_system_certificates()

    is_api = "api.github.com" in url
    auth_options = (True, False) if is_api else (False,)

    verified_ctx = _create_ssl_context(verify=True)
    unverified_ctx = _create_ssl_context(verify=False)
    last_error = None

    for use_auth in auth_options:
        headers = get_auth_headers(allow_token=use_auth)
        if extra_headers:
            headers.update(extra_headers)
        if not use_auth and "Authorization" in headers:
            del headers["Authorization"]

        if verified_ctx:
            opener = urllib.request.build_opener(
                NoAuthRedirectHandler, urllib.request.HTTPSHandler(context=verified_ctx)
            )
            req = urllib.request.Request(url, headers=headers)
            try:
                with opener.open(req, timeout=timeout) as resp:
                    if resp.status == 200:
                        return resp.read()
            except urllib.error.HTTPError as e:
                last_error = f"HTTP {e.code}: {e.reason}"
                if e.code == 401 and use_auth:
                    continue
                print(f"[!] HTTP error {e.code} fetching {url}: {e.reason}", file=sys.stderr, flush=True)
                break
            except Exception as e:
                last_error = str(e)
                if is_ssl_verify_error(e):
                    print("[*] SSL verification failed. Retrying with unverified context...", flush=True)
                else:
                    print(f"[!] Network error fetching {url}: {e}", file=sys.stderr, flush=True)

        if unverified_ctx:
            fallback_opener = urllib.request.build_opener(
                NoAuthRedirectHandler, urllib.request.HTTPSHandler(context=unverified_ctx)
            )
            req = urllib.request.Request(url, headers=headers)
            try:
                with fallback_opener.open(req, timeout=timeout) as resp:
                    if resp.status == 200:
                        return resp.read()
            except urllib.error.HTTPError as e:
                last_error = f"HTTP {e.code}: {e.reason}"
                if e.code == 401 and use_auth:
                    continue
                print(f"[!] HTTP error {e.code} (unverified) fetching {url}: {e.reason}", file=sys.stderr, flush=True)
                break
            except Exception as e:
                last_error = str(e)

    curl_headers = {"User-Agent": USER_AGENT}
    if extra_headers:
        curl_headers.update(extra_headers)
    curl_data = _download_via_curl(url, headers=curl_headers, timeout=max(timeout, 30))
    if curl_data:
        return curl_data

    if last_error:
        print(f"[Error] Failed to download {url}: {last_error}", file=sys.stderr, flush=True)
    return None


def _get_redirect_tag(url):
    """Extract release tag from GitHub's /releases/latest HTTP redirect."""
    for verify in (True, False):
        try:
            ctx = _create_ssl_context(verify=verify)
            handlers = [urllib.request.HTTPSHandler(context=ctx)] if ctx else []
            opener = urllib.request.build_opener(*handlers)
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with opener.open(req, timeout=15) as resp:
                final_url = resp.geturl()
                if "/releases/tag/" in final_url:
                    tag = final_url.split("/releases/tag/")[-1].split("?")[0].strip("/")
                    if tag:
                        return urllib.parse.unquote(tag)
        except Exception:
            pass

    curl_bin = shutil.which("curl")
    if curl_bin:
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            res = subprocess.run(
                [curl_bin, "-sIL", "--connect-timeout", "10", "--max-time", "15", "-H", f"User-Agent: {USER_AGENT}", url],
                capture_output=True,
                text=True,
                timeout=20,
                creationflags=flags,
            )
            for line in res.stdout.splitlines():
                if line.lower().startswith("location:"):
                    loc = line.split(":", 1)[1].strip()
                    if "/releases/tag/" in loc:
                        tag = loc.split("/releases/tag/")[-1].split("?")[0].strip("/")
                        if tag:
                            return urllib.parse.unquote(tag)
        except Exception:
            pass
    return None


def _http_get(url: str, timeout: int = 15):
    data = download_raw_url(url, extra_headers={"Accept": "application/vnd.github+json"}, timeout=timeout)
    if data is None:
        raise RuntimeError(f"Could not fetch {url}")
    return url, data


def _read_json_dict(path: Path) -> dict:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _is_ignore_versions_key(key: str) -> bool:
    norm = re.sub(r"\s+", " ", str(key or "").strip().lower().replace("_", " "))
    return bool(re.match(r"^(?:old\s+)?ignor\w*\s+versions?$", norm))


def _is_settings_key(key: str, val=None) -> bool:
    norm = re.sub(r"\s+", " ", str(key or "").strip().lower().replace("_", " "))
    if norm in ("updater settings", "updater config", "settings", "defaults", "config", "updater"):
        return isinstance(val, dict) and not any(k in val for k in ("win", "macos", "other", "Files"))
    return False


def _find_settings_block(download_options: dict | None = None) -> dict:
    src = download_options if isinstance(download_options, dict) and download_options else _load_local_download_options()
    if not isinstance(src, dict):
        return {}
    merged: dict = {}
    for k, v in src.items():
        if _is_settings_key(k, v):
            merged.update(v)
    for k, v in src.items():
        if not isinstance(v, (dict, list)) or str(k).strip().lower() in ("channels", "build types", "release assets", "assets"):
            merged[k] = v
    return merged


def _lookup_setting(mapping: dict | None, *names: str):
    if not isinstance(mapping, dict):
        return None
    wanted = {re.sub(r"\s+", " ", n.strip().lower().replace("_", " ")) for n in names}
    for k, v in mapping.items():
        norm = re.sub(r"\s+", " ", str(k).strip().lower().replace("_", " "))
        if norm in wanted:
            return v
    return None


def get_default_channel(download_options: dict | None = None) -> str:
    settings = _find_settings_block(download_options)
    cfg_val = _lookup_setting(settings, "default channel", "channel")
    if cfg_val:
        c = str(cfg_val).strip().lower()
        if any(k in c for k in ("pre", "beta", "alpha", "rc", "nightly")):
            return "prerelease"
        return "stable"
    return "stable"


def extract_channels(download_options: dict | None = None) -> list[str]:
    settings = _find_settings_block(download_options)
    cfg_list = _lookup_setting(settings, "channels", "valid channels")
    if isinstance(cfg_list, list):
        cleaned = [normalize_channel(x, download_options=download_options) for x in cfg_list if str(x).strip()]
        dedup = list(dict.fromkeys(cleaned))
        if dedup:
            return dedup
    return ["stable", "prerelease"]


def extract_build_types(download_options: dict | None = None, selected_opts=None) -> list[str]:
    sources = []
    if isinstance(download_options, dict) and download_options:
        sources.append(download_options)
    local_opts = _load_local_download_options()
    if isinstance(local_opts, dict) and local_opts and local_opts is not download_options:
        sources.append(local_opts)

    found: list[str] = []
    reserved_non_variant = {"win", "macos", "other", "files", "update ignore files", "release assets", "assets"}
    for src in sources:
        opt_names = extract_download_option_names(src)
        inspect_opts = [o for o in (selected_opts or opt_names) if o in opt_names] or opt_names
        for opt in inspect_opts:
            opt_val = src.get(opt)
            if not isinstance(opt_val, dict):
                continue
            for k, v in opt_val.items():
                if isinstance(v, dict) and str(k).strip().lower() not in reserved_non_variant:
                    if str(k) not in found:
                        found.append(str(k))
        if isinstance(src.get("compiled"), dict) and "compiled" not in found:
            found.append("compiled")
        if found:
            return found

    settings = _find_settings_block(download_options)
    cfg_bts = _lookup_setting(settings, "build types", "valid build types")
    if isinstance(cfg_bts, list) and cfg_bts:
        return [str(x).strip() for x in cfg_bts if str(x).strip()]
    return ["source", "compiled"]


def get_default_build_type(download_options: dict | None = None) -> str:
    settings = _find_settings_block(download_options)
    if getattr(sys, "frozen", False):
        cfg_frozen = _lookup_setting(settings, "default compiled build type")
        if cfg_frozen:
            return str(cfg_frozen).strip()
        avail = extract_build_types(download_options)
        for cand in avail:
            if "comp" in cand.lower():
                return cand
        return "compiled"
    cfg_val = _lookup_setting(settings, "default build type", "build type")
    if cfg_val:
        return str(cfg_val).strip()
    avail = extract_build_types(download_options)
    return avail[0] if avail else "source"


def normalize_channel(channel: str | None, download_options: dict | None = None) -> str:
    raw = channel if (channel is not None and str(channel).strip()) else get_default_channel(download_options)
    c = str(raw).strip().lower()
    if any(k in c for k in ("pre", "beta", "alpha", "rc", "nightly")):
        return "prerelease"
    return "stable"


def normalize_build_type(build_type: str | None, download_options: dict | None = None) -> str:
    raw = build_type if (build_type is not None and str(build_type).strip()) else get_default_build_type(download_options)
    val = str(raw).strip()
    b = val.lower()
    avail = extract_build_types(download_options)
    for item in avail:
        if item.lower() == b:
            return item
    if any(k in b for k in ("comp", "bin", "exe", "app")):
        for item in avail:
            if "comp" in item.lower():
                return item
        return "compiled"
    if any(k in b for k in ("src", "source", "python")):
        for item in avail:
            if "source" in item.lower():
                return item
        return "source"
    return b


def normalize_version_tag(tag: str | None) -> str:
    t = str(tag or "").strip().lower()
    while t.startswith("v") and len(t) > 1:
        t = t[1:]
    return t


def _load_local_download_options() -> dict:
    for base in (ROOT_DIR, RESOURCE_DIR):
        for p in (base / "download_options.json", base / "updater" / "download_options.json"):
            data = _read_json_dict(p)
            if data:
                return data
    return {}


def get_ignored_old_versions(download_options: dict | None = None) -> list[str]:
    """Return all blocked legacy version strings configured in download_options.json ('Old Ignore versions')."""
    sources = []
    if isinstance(download_options, dict) and download_options:
        sources.append(download_options)
    local_opts = _load_local_download_options()
    if isinstance(local_opts, dict) and local_opts and local_opts is not download_options:
        sources.append(local_opts)

    for src in sources:
        for k, v in src.items():
            if _is_ignore_versions_key(k) and isinstance(v, list):
                ignored: list[str] = []
                seen_norm = set()
                for item in v:
                    s = str(item or "").strip()
                    n = normalize_version_tag(s)
                    if s and n not in seen_norm:
                        seen_norm.add(n)
                        ignored.append(s)
                return ignored
    return []


def is_version_ignored(tag: str | None, ignored_versions: list[str] | None = None, download_options: dict | None = None) -> bool:
    if not tag:
        return False
    norm = normalize_version_tag(tag)
    raw = str(tag).strip().lower()
    blocked = ignored_versions if ignored_versions is not None else get_ignored_old_versions(download_options)
    for b in blocked:
        bs = str(b or "").strip().lower()
        if raw == bs or norm == normalize_version_tag(bs):
            return True
    return False


def save_updater_config(updater_cfg: dict, install_dir: str | Path | None = None):
    """Save updater state under launcher_config.json['updater'] without clobbering launcher settings."""
    base = Path(install_dir) if install_dir else ROOT_DIR
    base.mkdir(parents=True, exist_ok=True)
    cfg_path = base / CONFIG_FILE
    legacy_path = base / LEGACY_CONFIG_FILE

    default_bt = get_default_build_type()
    default_ch = get_default_channel()
    full_cfg = _read_json_dict(cfg_path)
    prev_updater = full_cfg.get("updater") if isinstance(full_cfg.get("updater"), dict) else {}
    channel = normalize_channel(updater_cfg.get("channel") or prev_updater.get("channel") or default_ch)
    build_type = normalize_build_type(updater_cfg.get("build_type") or prev_updater.get("build_type") or default_bt)

    full_cfg["updater"] = {
        "current_version": str(updater_cfg.get("current_version") or prev_updater.get("current_version") or DEFAULT_LAUNCHER_VERSION),
        "selected_options": list(updater_cfg.get("selected_options") or prev_updater.get("selected_options") or [DEFAULT_OPTION]),
        "channel": channel,
        "build_type": build_type,
        "install_dir": str(updater_cfg.get("install_dir") or base),
    }
    tmp_path = cfg_path.with_suffix(".json.tmp")
    try:
        tmp_path.write_text(json.dumps(full_cfg, indent=4), encoding="utf-8")
        os.replace(tmp_path, cfg_path)
    except Exception:
        cfg_path.write_text(json.dumps(full_cfg, indent=4), encoding="utf-8")
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass

    if legacy_path.exists():
        try:
            legacy_path.unlink()
        except Exception:
            pass


def load_config(install_dir: str | Path | None = None) -> dict:
    """Load updater config from launcher_config.json['updater'], migrating legacy updater_config.json."""
    base = Path(install_dir) if install_dir else ROOT_DIR
    cfg_path = base / CONFIG_FILE
    legacy_path = base / LEGACY_CONFIG_FILE

    default_bt = get_default_build_type()
    default_ch = get_default_channel()
    full_cfg = _read_json_dict(cfg_path)
    updater = full_cfg.get("updater")
    if isinstance(updater, dict) and updater:
        out = dict(updater)
        out["channel"] = normalize_channel(out.get("channel") or default_ch)
        out["build_type"] = normalize_build_type(out.get("build_type") or default_bt)
        return out

    if any(k in full_cfg for k in ("current_version", "selected_options", "install_dir")):
        migrated = {
            "current_version": str(full_cfg.get("current_version") or DEFAULT_LAUNCHER_VERSION),
            "selected_options": list(full_cfg.get("selected_options") or [DEFAULT_OPTION]),
            "channel": normalize_channel(full_cfg.get("channel") or default_ch),
            "build_type": normalize_build_type(full_cfg.get("build_type") or default_bt),
            "install_dir": str(full_cfg.get("install_dir") or base),
        }
        save_updater_config(migrated, base)
        return migrated

    legacy_cfg = _read_json_dict(legacy_path)
    if legacy_cfg:
        migrated = {
            "current_version": str(legacy_cfg.get("current_version") or DEFAULT_LAUNCHER_VERSION),
            "selected_options": list(legacy_cfg.get("selected_options") or [DEFAULT_OPTION]),
            "channel": normalize_channel(legacy_cfg.get("channel") or default_ch),
            "build_type": normalize_build_type(legacy_cfg.get("build_type") or default_bt),
            "install_dir": str(legacy_cfg.get("install_dir") or base),
        }
        save_updater_config(migrated, base)
        return migrated

    default_cfg = {
        "current_version": DEFAULT_LAUNCHER_VERSION,
        "selected_options": [DEFAULT_OPTION],
        "channel": default_ch,
        "build_type": default_bt,
        "install_dir": str(base),
    }
    try:
        save_updater_config(default_cfg, base)
    except Exception:
        pass
    return default_cfg


def get_current_version() -> str:
    cfg = load_config()
    ver = cfg.get("current_version")
    if not ver:
        ver = DEFAULT_LAUNCHER_VERSION
        cfg["current_version"] = ver
        try:
            save_updater_config(cfg)
        except Exception:
            pass
    return str(ver)


def _version_sort_key(v_str: str) -> tuple[tuple[int, ...], int, tuple[int, ...]]:
    s = normalize_version_tag(v_str)
    m = re.match(r"^([0-9]+(?:\.[0-9]+)*)(?:[-._]*(.*))?$", s)
    if not m:
        nums = tuple(int(x) for x in re.findall(r"\d+", s))
        return (nums, 1, ())
    base_nums = tuple(int(x) for x in m.group(1).split(".") if x.isdigit())
    # Strip trailing zeros beyond 2 components for stable comparisons while preserving 1.9 vs 1.10
    suffix = (m.group(2) or "").strip()
    if suffix:
        pre_nums = tuple(int(x) for x in re.findall(r"\d+", suffix))
        return (base_nums, 0, pre_nums)
    return (base_nums, 1, ())


def _parse_version_tuple(v_str: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", str(v_str or "")))


def is_newer_version(current: str, latest: str) -> bool:
    if not latest or current == latest:
        return False
    cur_k = _version_sort_key(current)
    lat_k = _version_sort_key(latest)
    if cur_k[0] and lat_k[0]:
        return lat_k > cur_k
    return current.strip() != latest.strip()


def fetch_releases(include_prereleases: bool = True, per_page: int = 50) -> list[dict]:
    """Fetch GitHub releases list (optionally including pre-releases, excluding drafts)."""
    url = f"https://api.github.com/repos/{OWNER}/{REPO}/releases?per_page={int(per_page)}"
    content = download_raw_url(url, extra_headers={"Accept": "application/vnd.github+json"})
    out: list[dict] = []
    if content:
        try:
            items = json.loads(content.decode("utf-8"))
            if isinstance(items, list):
                for r in items:
                    if not isinstance(r, dict) or r.get("draft"):
                        continue
                    is_pre = bool(r.get("prerelease"))
                    if not include_prereleases and is_pre:
                        continue
                    tag = str(r.get("tag_name") or "").strip()
                    if not tag:
                        continue
                    out.append({
                        "tag_name": tag,
                        "name": str(r.get("name") or tag),
                        "prerelease": is_pre,
                        "published_at": str(r.get("published_at") or ""),
                        "assets": [
                            {
                                "name": str(a.get("name") or ""),
                                "browser_download_url": str(a.get("browser_download_url") or ""),
                                "size": int(a.get("size") or 0),
                            }
                            for a in (r.get("assets") or [])
                            if isinstance(a, dict) and a.get("name") and a.get("browser_download_url")
                        ],
                    })
        except Exception:
            pass
    return out


def get_release_for_tag(tag: str, releases: list[dict] | None = None) -> dict | None:
    norm = normalize_version_tag(tag)
    if releases:
        for r in releases:
            if r.get("tag_name") == tag or normalize_version_tag(r.get("tag_name")) == norm:
                return r
    url = f"https://api.github.com/repos/{OWNER}/{REPO}/releases/tags/{urllib.parse.quote(str(tag), safe='')}"
    content = download_raw_url(url, extra_headers={"Accept": "application/vnd.github+json"})
    if content:
        try:
            r = json.loads(content.decode("utf-8"))
            if isinstance(r, dict) and r.get("tag_name"):
                return {
                    "tag_name": str(r.get("tag_name")),
                    "name": str(r.get("name") or r.get("tag_name")),
                    "prerelease": bool(r.get("prerelease")),
                    "published_at": str(r.get("published_at") or ""),
                    "assets": [
                        {
                            "name": str(a.get("name") or ""),
                            "browser_download_url": str(a.get("browser_download_url") or ""),
                            "size": int(a.get("size") or 0),
                        }
                        for a in (r.get("assets") or [])
                        if isinstance(a, dict) and a.get("name") and a.get("browser_download_url")
                    ],
                }
        except Exception:
            pass
    return None


def _is_tag_prerelease_name(tag: str) -> bool:
    low = str(tag or "").lower()
    return any(tok in low for tok in ("-pre", "pre", "-alpha", "-beta", "-rc", "test"))


def get_latest_version(channel: str | None = None, download_options: dict | None = None) -> str | None:
    """Resolve the latest release tag for the chosen channel ('stable' or 'prerelease')."""
    ch = normalize_channel(channel if channel is not None else load_config().get("channel"))
    ignored = get_ignored_old_versions(download_options)

    if ch == "prerelease":
        rels = fetch_releases(include_prereleases=True)
        valid = [r for r in rels if not is_version_ignored(r["tag_name"], ignored_versions=ignored)]
        if valid:
            best = max(valid, key=lambda r: _version_sort_key(r["tag_name"]))
            return best["tag_name"]

    url = f"https://api.github.com/repos/{OWNER}/{REPO}/releases/latest"
    content = download_raw_url(url, extra_headers={"Accept": "application/vnd.github+json"})
    if content:
        try:
            data = json.loads(content.decode("utf-8"))
            tag = data.get("tag_name")
            if tag and not is_version_ignored(tag, ignored_versions=ignored):
                return str(tag)
        except Exception:
            pass

    tag = _get_redirect_tag(f"https://github.com/{OWNER}/{REPO}/releases/latest")
    if tag and not is_version_ignored(tag, ignored_versions=ignored):
        return tag

    content = download_raw_url(
        f"https://api.github.com/repos/{OWNER}/{REPO}/tags",
        extra_headers={"Accept": "application/vnd.github+json"},
    )
    if content:
        try:
            tags = json.loads(content.decode("utf-8"))
            if isinstance(tags, list):
                cands = []
                for t in tags:
                    tname = str((t or {}).get("name") or "").strip()
                    if not tname or is_version_ignored(tname, ignored_versions=ignored):
                        continue
                    if ch == "stable" and _is_tag_prerelease_name(tname):
                        continue
                    cands.append(tname)
                if cands:
                    return max(cands, key=_version_sort_key)
        except Exception:
            pass

    return None


def list_installable_versions(
    channel: str | None = None,
    download_options: dict | None = None,
    include_prereleases: bool | None = None,
) -> list[dict]:
    """Return available versions (upgrades & downgrades), excluding 'Old Ignore versions'."""
    ch = normalize_channel(channel if channel is not None else load_config().get("channel"))
    inc_pre = (ch == "prerelease") if include_prereleases is None else bool(include_prereleases)
    ignored = get_ignored_old_versions(download_options)

    rels = fetch_releases(include_prereleases=inc_pre, per_page=50)
    entries: list[dict] = []
    seen = set()
    for r in rels:
        tag = r["tag_name"]
        if is_version_ignored(tag, ignored_versions=ignored) or tag in seen:
            continue
        seen.add(tag)
        entries.append(r)

    if not entries:
        content = download_raw_url(
            f"https://api.github.com/repos/{OWNER}/{REPO}/tags",
            extra_headers={"Accept": "application/vnd.github+json"},
        )
        if content:
            try:
                tags = json.loads(content.decode("utf-8"))
                if isinstance(tags, list):
                    for t in tags:
                        tname = str((t or {}).get("name") or "").strip()
                        if not tname or tname in seen or is_version_ignored(tname, ignored_versions=ignored):
                            continue
                        is_pre = _is_tag_prerelease_name(tname)
                        if not inc_pre and is_pre:
                            continue
                        seen.add(tname)
                        entries.append({
                            "tag_name": tname,
                            "name": tname,
                            "prerelease": is_pre,
                            "published_at": "",
                            "assets": [],
                        })
            except Exception:
                pass

    entries.sort(key=lambda r: _version_sort_key(r["tag_name"]), reverse=True)
    return entries


def fetch_download_options(tag: str, release_info: dict | None = None):
    if isinstance(release_info, dict):
        for asset in (release_info.get("assets") or []):
            if str(asset.get("name") or "").strip().lower() == "download_options.json":
                url = asset.get("browser_download_url") or asset.get("url")
                if url:
                    content = download_raw_url(url)
                    if content:
                        try:
                            return json.loads(content.decode("utf-8"))
                        except Exception:
                            pass

    for ref in (tag, tag.lstrip("v") if tag.startswith("v") else f"v{tag}", "main"):
        for path in ("download_options.json", "updater/download_options.json"):
            api_url = f"https://api.github.com/repos/{OWNER}/{REPO}/contents/{path}?ref={ref}"
            content = download_raw_url(api_url, extra_headers={"Accept": "application/vnd.github.v3.raw"})
            if content:
                try:
                    return json.loads(content.decode("utf-8"))
                except Exception:
                    pass

            raw_url = f"https://raw.githubusercontent.com/{OWNER}/{REPO}/{ref}/{path}"
            content = download_raw_url(raw_url)
            if content:
                try:
                    return json.loads(content.decode("utf-8"))
                except Exception:
                    pass

    if release_info is None:
        rel = get_release_for_tag(tag)
        if isinstance(rel, dict):
            for asset in (rel.get("assets") or []):
                if str(asset.get("name") or "").strip().lower() == "download_options.json":
                    url = asset.get("browser_download_url") or asset.get("url")
                    if url:
                        content = download_raw_url(url)
                        if content:
                            try:
                                return json.loads(content.decode("utf-8"))
                            except Exception:
                                pass
    return None


def get_repo_tree(tag: str):
    for ref in (tag, tag.lstrip("v") if tag.startswith("v") else f"v{tag}", "main"):
        url = f"https://api.github.com/repos/{OWNER}/{REPO}/git/trees/{ref}?recursive=1"
        content = download_raw_url(url, extra_headers={"Accept": "application/vnd.github+json"})
        if content:
            try:
                tree = json.loads(content.decode("utf-8")).get("tree", [])
                if tree:
                    return tree
            except Exception:
                pass
    return []


def download_file_content(path: str, tag: str):
    encoded_path = urllib.parse.quote(path)
    refs = (tag, tag.lstrip("v") if tag.startswith("v") else f"v{tag}", "main")
    for ref in refs:
        api_url = f"https://api.github.com/repos/{OWNER}/{REPO}/contents/{encoded_path}?ref={ref}"
        content = download_raw_url(api_url, extra_headers={"Accept": "application/vnd.github.v3.raw"})
        if content is not None:
            return content

    for ref in refs:
        raw_url = f"https://raw.githubusercontent.com/{OWNER}/{REPO}/{ref}/{encoded_path}"
        content = download_raw_url(raw_url)
        if content is not None:
            return content
    return None


def get_platform_key() -> str:
    """Return normalized platform key used by download_options.json ('win', 'macos', or 'other')."""
    if sys.platform == "win32" or os.name == "nt":
        return "win"
    if sys.platform == "darwin":
        return "macos"
    return "other"


def extract_download_option_names(download_options: dict | None) -> list[str]:
    """Return installable product option names, filtering out metadata & settings entries."""
    if not isinstance(download_options, dict):
        return []
    names = []
    has_default = DEFAULT_OPTION in download_options and isinstance(download_options.get(DEFAULT_OPTION), dict)
    for k, v in download_options.items():
        if _is_ignore_versions_key(k) or _is_settings_key(k, v):
            continue
        if not isinstance(v, (dict, list)):
            continue
        norm_k = str(k).strip().lower().replace("_", " ")
        if norm_k in ("compiled", "source") and has_default and isinstance(v, dict) and any(p in v for p in ("win", "macos", "other")):
            continue
        names.append(str(k))
    return names


def _parse_download_source_prefs(mapping: dict | None) -> tuple[str | None, list[str]]:
    if not isinstance(mapping, dict):
        return None, []
    mode = None
    use_rel = _lookup_setting(mapping, "use release assets", "from releases", "download from releases", "use releases")
    if isinstance(use_rel, bool):
        mode = "releases" if use_rel else "repo"

    src_val = _lookup_setting(mapping, "download from", "download source", "source from", "asset mode")
    if src_val is not None:
        s = str(src_val).strip().lower().replace("_", " ").replace("-", " ")
        if any(tok in s for tok in ("release", "asset")):
            mode = "releases"
        elif any(tok in s for tok in ("repo", "git", "tree", "source", "src", "code")):
            mode = "repo"
        elif s in ("auto", "both"):
            mode = "auto"

    explicit_assets: list[str] = []
    raw_assets = _lookup_setting(mapping, "release assets", "assets", "release archive", "archive", "asset")
    if isinstance(raw_assets, str) and raw_assets.strip():
        explicit_assets.append(raw_assets.strip())
    elif isinstance(raw_assets, list):
        explicit_assets.extend(str(a).strip() for a in raw_assets if str(a).strip())
    return mode, explicit_assets


def resolve_option_platform_data(
    opt_data,
    platform_key: str | None = None,
    build_type: str | None = None,
    all_options: dict | None = None,
) -> dict:
    """Resolve Files, Update Ignore Files, Message, Download From & Release Assets dynamically."""
    plat = platform_key or get_platform_key()
    bt = normalize_build_type(build_type, download_options=all_options)
    empty = {"Files": [], "Update Ignore Files": [], "Message": None, "Download From": None, "Release Assets": []}
    if isinstance(opt_data, list):
        return {"Files": list(opt_data), "Update Ignore Files": [], "Message": None, "Download From": None, "Release Assets": []}
    if not isinstance(opt_data, dict):
        return empty

    variant_node = opt_data
    direct_plat_keys = {"win", "macos", "other"}
    variant_keys = [
        k for k, v in opt_data.items()
        if isinstance(v, dict) and str(k).strip().lower() not in direct_plat_keys
    ]
    has_variant_keys = bool(variant_keys)
    is_legacy_flat = not has_variant_keys and any(k in opt_data for k in direct_plat_keys)

    if has_variant_keys:
        matched_k = next((k for k in variant_keys if k.lower() == bt.lower()), None)
        if matched_k and isinstance(opt_data.get(matched_k), dict):
            variant_node = opt_data[matched_k]
        elif bt == "compiled" and isinstance(all_options, dict) and isinstance(all_options.get("compiled"), dict):
            variant_node = all_options["compiled"]
        elif isinstance(opt_data.get("source"), dict):
            variant_node = opt_data["source"]
        else:
            variant_node = opt_data[variant_keys[0]]
    elif bt == "compiled":
        if isinstance(all_options, dict) and isinstance(all_options.get("compiled"), dict):
            variant_node = all_options["compiled"]
        elif is_legacy_flat:
            local_opts = _load_local_download_options()
            local_def = local_opts.get(DEFAULT_OPTION) if isinstance(local_opts, dict) else None
            if isinstance(local_def, dict) and isinstance(local_def.get("compiled"), dict):
                variant_node = local_def["compiled"]
            elif isinstance(local_opts, dict) and isinstance(local_opts.get("compiled"), dict):
                variant_node = local_opts["compiled"]

    target = variant_node
    if any(k in variant_node for k in direct_plat_keys):
        target = variant_node.get(plat)
        if not isinstance(target, dict):
            for fallback_k in ("other", "macos", "win"):
                if isinstance(variant_node.get(fallback_k), dict):
                    target = variant_node[fallback_k]
                    break
        if not isinstance(target, dict):
            return empty

    dl_from = None
    rel_assets: list[str] = []
    for layer in (_find_settings_block(all_options), opt_data, variant_node, target):
        m, ea = _parse_download_source_prefs(layer)
        if m is not None:
            dl_from = m
        for item in ea:
            if item not in rel_assets:
                rel_assets.append(item)

    files = list(target.get("Files") or [])
    ignores = list(target.get("Update Ignore Files") or [])
    msg = target.get("Message")
    return {
        "Files": files,
        "Update Ignore Files": ignores,
        "Message": str(msg).strip() if msg else None,
        "Download From": dl_from,
        "Release Assets": rel_assets,
    }


def collect_options_plan_details(
    download_options,
    selected_opts=None,
    platform_key: str | None = None,
    build_type: str | None = None,
) -> dict:
    bt = normalize_build_type(build_type, download_options=download_options)
    empty = {
        "paths": [],
        "ignore_files": [],
        "messages": [],
        "download_from": None,
        "release_assets": [],
        "build_type": bt,
    }
    if not isinstance(download_options, dict) or not download_options:
        return empty

    available_names = extract_download_option_names(download_options)
    if not available_names:
        return empty

    chosen = [opt for opt in (selected_opts or []) if opt in available_names]
    if not chosen:
        chosen = [DEFAULT_OPTION] if DEFAULT_OPTION in available_names else [available_names[0]]

    paths: list[str] = []
    ignore_files: list[str] = []
    messages: list[str] = []
    release_assets: list[str] = []
    download_from = None
    for opt in chosen:
        resolved = resolve_option_platform_data(
            download_options[opt],
            platform_key=platform_key,
            build_type=bt,
            all_options=download_options,
        )
        for p in resolved["Files"]:
            if p not in paths:
                paths.append(p)
        for ig in resolved["Update Ignore Files"]:
            if ig not in ignore_files:
                ignore_files.append(ig)
        msg = resolved.get("Message")
        if msg and msg not in messages:
            messages.append(msg)
        for ra in (resolved.get("Release Assets") or []):
            if ra not in release_assets:
                release_assets.append(ra)
        if resolved.get("Download From"):
            download_from = resolved["Download From"]

    return {
        "paths": paths,
        "ignore_files": ignore_files,
        "messages": messages,
        "download_from": download_from,
        "release_assets": release_assets,
        "build_type": bt,
    }


def collect_options_plan(
    download_options,
    selected_opts=None,
    platform_key: str | None = None,
    build_type: str | None = None,
):
    """Collect deduplicated Files, Update Ignore Files, and Messages for selected options & build_type."""
    details = collect_options_plan_details(
        download_options,
        selected_opts=selected_opts,
        platform_key=platform_key,
        build_type=build_type,
    )
    return details["paths"], details["ignore_files"], details["messages"]


def resolve_files_to_download(paths, tree_objects, ignore_files=None):
    ignore_set = {Path(ig).as_posix().strip("/").lower() for ig in (ignore_files or [])}
    files = []
    for path in paths:
        norm_path = Path(path).as_posix()
        is_dir = path.endswith("/") or norm_path.endswith("/")
        clean = norm_path.rstrip("/")
        for obj in tree_objects:
            if obj.get("type") == "blob":
                op = Path(obj["path"]).as_posix()
                if is_dir and op.startswith(clean + "/"):
                    if not any(op.lower() == ig or op.lower().startswith(ig + "/") for ig in ignore_set):
                        files.append(obj["path"])
                elif not is_dir:
                    if op == clean or op.startswith(clean + "/"):
                        if not any(op.lower() == ig or op.lower().startswith(ig + "/") for ig in ignore_set):
                            files.append(obj["path"])
    return sorted(list(set(files)))


def _is_ignored_rel_path(rel_path: str, ignore_set: set[str]) -> bool:
    if not ignore_set:
        return False
    norm = Path(str(rel_path)).as_posix().strip("/").lower()
    if not norm:
        return False
    return any(norm == ig or norm.startswith(ig + "/") for ig in ignore_set)


def resolve_release_assets(
    paths,
    release_assets,
    ignore_files=None,
    explicit_assets=None,
    build_type: str | None = None,
    platform_key: str | None = None,
) -> list[dict]:
    """Match configured Files, dirs & explicit Release Assets against GitHub release assets."""
    import fnmatch

    plat = platform_key or get_platform_key()
    bt_low = str(build_type or "").strip().lower()
    rel_list = release_assets if isinstance(release_assets, list) else []
    ignore_set = {Path(ig).as_posix().strip("/").lower() for ig in (ignore_files or [])}
    matched: list[dict] = []
    seen_urls = set()

    def _add_asset(
        name: str,
        url: str,
        size=None,
        target_rel: str = "",
        extract_zip: bool = False,
        extract_prefix: str = "",
        matched_targets=None,
        full_bundle: bool = False,
    ):
        if not url or url in seen_urls:
            return
        seen_urls.add(url)
        matched.append({
            "name": name,
            "url": url,
            "browser_download_url": url,
            "size": size,
            "target_rel": target_rel or name,
            "target_path": target_rel or name,
            "extract_zip": bool(extract_zip),
            "extract_prefix": extract_prefix,
            "matched_paths": list(matched_targets or []),
            "full_bundle": bool(full_bundle),
        })

    # 1. Explicit Release Assets / Archive entries configured in download_options.json
    for exp in (explicit_assets or []):
        exp_s = str(exp or "").strip()
        if not exp_s:
            continue
        if exp_s.startswith(("http://", "https://")):
            bname = os.path.basename(urllib.parse.urlparse(exp_s).path) or "release_asset"
            _add_asset(
                bname,
                exp_s,
                target_rel=bname,
                extract_zip=bname.lower().endswith(".zip"),
                matched_targets=list(paths or []),
                full_bundle=bname.lower().endswith(".zip"),
            )
            continue

        exp_low = exp_s.lower()
        for asset in rel_list:
            if not isinstance(asset, dict):
                continue
            a_name = str(asset.get("name") or "").strip()
            a_low = a_name.lower()
            a_url = str(asset.get("browser_download_url") or asset.get("url") or "").strip()
            if not a_name or not a_url or _is_ignored_rel_path(a_name, ignore_set):
                continue
            if a_low == exp_low or a_low == f"{exp_low}.zip" or fnmatch.fnmatch(a_low, exp_low):
                _add_asset(
                    a_name,
                    a_url,
                    size=asset.get("size"),
                    target_rel=a_name,
                    extract_zip=a_low.endswith(".zip"),
                    matched_targets=list(paths or []),
                    full_bundle=a_low.endswith(".zip"),
                )

    if not paths:
        return matched

    unmatched_paths: list[str] = []
    for req_path in paths:
        raw_req = str(req_path).strip()
        if raw_req.startswith(("http://", "https://")):
            bname = os.path.basename(urllib.parse.urlparse(raw_req).path) or "asset"
            _add_asset(bname, raw_req, target_rel=bname, extract_zip=bname.lower().endswith(".zip"), matched_targets=[raw_req])
            continue

        norm = Path(raw_req).as_posix().strip()
        is_dir_pattern = raw_req.endswith("/") or norm.endswith("/")
        clean = norm.rstrip("/")
        if not clean or _is_ignored_rel_path(clean, ignore_set):
            continue
        clean_low = clean.lower()
        base_name = os.path.basename(clean).lower()
        stem = os.path.splitext(base_name)[0].lower()

        found_for_req = False
        for asset in rel_list:
            if not isinstance(asset, dict):
                continue
            a_name = str(asset.get("name") or "").strip()
            a_low = a_name.lower()
            a_url = str(asset.get("browser_download_url") or asset.get("url") or "").strip()
            if not a_name or not a_url or _is_ignored_rel_path(a_low, ignore_set):
                continue

            if is_dir_pattern:
                if a_low in (f"{clean_low}.zip", f"{base_name}.zip"):
                    _add_asset(
                        a_name,
                        a_url,
                        size=asset.get("size"),
                        target_rel=clean,
                        extract_zip=True,
                        extract_prefix=clean,
                        matched_targets=[raw_req],
                    )
                    found_for_req = True
                elif a_low.startswith(clean_low + "/"):
                    _add_asset(
                        a_name,
                        a_url,
                        size=asset.get("size"),
                        target_rel=a_name,
                        extract_zip=False,
                        matched_targets=[raw_req],
                    )
                    found_for_req = True
            else:
                if a_low in (clean_low, base_name):
                    _add_asset(
                        a_name,
                        a_url,
                        size=asset.get("size"),
                        target_rel=clean,
                        extract_zip=a_low.endswith(".zip"),
                        matched_targets=[raw_req],
                    )
                    found_for_req = True
                elif a_low.endswith(".zip"):
                    if a_low in (f"{clean_low}.zip", f"{base_name}.zip", f"{stem}.zip") or (
                        base_name.endswith(".app") and ("mac" in a_low or "darwin" in a_low or ".app" in a_low) and stem in a_low
                    ) or (
                        base_name.endswith(".exe") and ("win" in a_low or ".exe" in a_low) and stem in a_low
                    ):
                        _add_asset(
                            a_name,
                            a_url,
                            size=asset.get("size"),
                            target_rel=clean,
                            extract_zip=True,
                            matched_targets=[raw_req],
                        )
                        found_for_req = True

        if not found_for_req:
            unmatched_paths.append(raw_req)

    # 3. Variant / platform archive bundle fallback if unmatched paths remain (e.g. source.zip, PyMcLauncher-win.zip)
    if unmatched_paths and rel_list:
        bundle_names: set[str] = set()
        if bt_low:
            bundle_names.update({
                f"{bt_low}.zip",
                f"{bt_low}-{plat}.zip",
                f"pymclauncher-{bt_low}.zip",
                f"pymclauncher-{bt_low}-{plat}.zip",
            })
        if bt_low == "source":
            bundle_names.update({"src.zip", f"src-{plat}.zip", "pymclauncher-src.zip"})
        else:
            plat_aliases = [plat] + (["windows"] if plat == "win" else (["darwin", "mac"] if plat == "macos" else ["linux"]))
            for pa in plat_aliases:
                bundle_names.update({f"pymclauncher-{pa}.zip", f"pymclauncher_{pa}.zip"})
            bundle_names.add("pymclauncher.zip")

        for asset in rel_list:
            if not isinstance(asset, dict):
                continue
            a_name = str(asset.get("name") or "").strip()
            a_url = str(asset.get("browser_download_url") or asset.get("url") or "").strip()
            if not a_name or not a_url:
                continue
            if a_name.lower() in bundle_names:
                _add_asset(
                    a_name,
                    a_url,
                    size=asset.get("size"),
                    target_rel=a_name,
                    extract_zip=True,
                    matched_targets=list(unmatched_paths),
                    full_bundle=True,
                )

    return matched


def _write_bytes_atomic(dest_path: Path, data: bytes, make_exec: bool = False):
    import time
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fp = dest_path.with_name(dest_path.name + ".tmp")
    last_err = None
    for attempt in range(10):
        try:
            tmp_fp.write_bytes(data)
            os.replace(tmp_fp, dest_path)
            last_err = None
            break
        except PermissionError as e:
            last_err = e
            time.sleep(0.35)
        except Exception as e:
            last_err = e
            break
    if last_err is not None:
        try:
            dest_path.write_bytes(data)
        finally:
            if tmp_fp.exists():
                try:
                    tmp_fp.unlink()
                except Exception:
                    pass
    if make_exec and os.name != "nt":
        try:
            os.chmod(dest_path, 0o755)
        except Exception:
            pass


def _extract_zip_bytes(zip_bytes: bytes, install_dir: str, ignore_files=None, extract_prefix: str = ""):
    import io
    import zipfile

    base_real = os.path.realpath(install_dir)
    ignore_set = {Path(ig).as_posix().strip("/").lower() for ig in (ignore_files or [])}
    clean_prefix = Path(extract_prefix).as_posix().strip("/") if extract_prefix else ""

    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        infos = [info for info in zf.infolist() if info.filename and not info.filename.startswith("/")]
        needs_prefix = False
        if clean_prefix:
            pref_low = clean_prefix.lower()
            has_prefix = any(
                Path(info.filename).as_posix().lstrip("./").lower() == pref_low
                or Path(info.filename).as_posix().lstrip("./").lower().startswith(pref_low + "/")
                for info in infos
            )
            needs_prefix = not has_prefix

        for member in infos:
            member_rel = Path(member.filename).as_posix().lstrip("./")
            if not member_rel:
                continue
            eff_rel = f"{clean_prefix}/{member_rel}" if needs_prefix else member_rel
            if _is_ignored_rel_path(eff_rel, ignore_set):
                continue
            dest_str = os.path.realpath(os.path.join(base_real, eff_rel.replace("/", os.sep)))
            if dest_str != base_real and not dest_str.startswith(base_real + os.sep):
                continue
            out_p = Path(dest_str)
            if member.is_dir():
                out_p.mkdir(parents=True, exist_ok=True)
                continue
            is_exec = ".app/contents/macos/" in eff_rel.lower() or eff_rel.endswith((".sh", ".command", ".py"))
            _write_bytes_atomic(out_p, zf.read(member.filename), make_exec=is_exec)
            if os.name != "nt":
                perm = (member.external_attr >> 16) & 0o777
                if perm:
                    try:
                        os.chmod(out_p, perm)
                    except Exception:
                        pass


def perform_download_concurrently(files, tag, install_dir, progress_cb=None, assets=None, ignore_files=None):
    os.makedirs(install_dir, exist_ok=True)
    asset_list = list(assets or [])
    total = len(files) + len(asset_list)
    completed = 0
    lock = threading.Lock()

    def task(fp):
        nonlocal completed
        try:
            local_fp = Path(install_dir) / fp.replace("/", os.sep)
            data = download_file_content(fp, tag)
            if data is not None:
                is_exec = fp.endswith((".command", ".sh", ".py")) or ".app/Contents/MacOS/" in fp
                _write_bytes_atomic(local_fp, data, make_exec=is_exec)
        except Exception as e:
            print(f"Error downloading {fp}: {e}", file=sys.stderr, flush=True)
        finally:
            with lock:
                completed += 1
                if progress_cb:
                    progress_cb(completed, total, fp)

    def asset_task(asset_info):
        nonlocal completed
        name = asset_info["name"]
        try:
            url = asset_info.get("browser_download_url") or asset_info.get("url")
            data = download_raw_url(url, timeout=120)
            if data is not None:
                if asset_info.get("extract_zip"):
                    _extract_zip_bytes(
                        data,
                        install_dir,
                        ignore_files=ignore_files,
                        extract_prefix=asset_info.get("extract_prefix", ""),
                    )
                else:
                    rel_target = asset_info.get("target_rel") or asset_info.get("target_path") or name
                    local_fp = Path(install_dir) / rel_target.replace("/", os.sep)
                    is_exec = name.lower().endswith((".sh", ".command", ".py")) or (os.name != "nt" and "." not in name)
                    _write_bytes_atomic(local_fp, data, make_exec=is_exec)
        except Exception as e:
            print(f"Error downloading release asset {name}: {e}", file=sys.stderr, flush=True)
        finally:
            with lock:
                completed += 1
                if progress_cb:
                    progress_cb(completed, total, name)

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(task, f) for f in files] + [executor.submit(asset_task, a) for a in asset_list]
        concurrent.futures.wait(futures)


def detect_relaunch_script(install_dir: Path | str | None = None, build_type: str | None = None) -> str | None:
    """Return the platform-appropriate relaunch binary or setup-start script filename if present."""
    base = Path(install_dir) if install_dir else ROOT_DIR
    bt = normalize_build_type(build_type if build_type is not None else load_config(base).get("build_type"))
    if bt == "compiled":
        if os.name == "nt" and (base / "PyMcLauncher.exe").exists():
            return "PyMcLauncher.exe"
        if sys.platform == "darwin" and (base / "PyMcLauncher.app").exists():
            return "PyMcLauncher.app"

    if os.name == "nt":
        candidates = ("setup-start.bat",)
    elif sys.platform == "darwin":
        candidates = ("setup-start.command", "setup-start.sh")
    else:
        candidates = ("setup-start.sh", "setup-start.command")

    for name in candidates:
        if (base / name).exists():
            return name
    return None


def _launch_relaunch_target(install_dir: str, relaunch_script: str | None = None):
    candidate_dirs = [install_dir, os.path.dirname(install_dir), os.getcwd()]
    if relaunch_script:
        for cdir in candidate_dirs:
            sp = os.path.join(cdir, relaunch_script)
            if os.path.exists(sp):
                if sp.lower().endswith(".exe"):
                    subprocess.Popen([sp], cwd=cdir, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
                    return True
                if sp.lower().endswith(".app") and sys.platform == "darwin":
                    subprocess.Popen(["open", sp], cwd=cdir, start_new_session=True)
                    return True
                if os.name != "nt" and (sp.endswith(".command") or sp.endswith(".sh")):
                    try:
                        os.chmod(sp, 0o755)
                    except Exception:
                        pass
                if os.name == "nt":
                    subprocess.Popen(
                        ["cmd.exe", "/c", sp],
                        cwd=cdir,
                        creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
                    )
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", sp], cwd=cdir, start_new_session=True)
                else:
                    subprocess.Popen(["bash", sp], cwd=cdir, start_new_session=True)
                return True

    launcher_py = os.path.join(install_dir, "launcher.py")
    if os.path.exists(launcher_py):
        subprocess.Popen([sys.executable, launcher_py], cwd=install_dir, start_new_session=(os.name != "nt"))
        return True
    return False


def run_update_check(
    install_dir=None,
    relaunch_script=None,
    target_version: str | None = None,
    channel: str | None = None,
    build_type: str | None = None,
):
    """Download an update or downgrade (from GitHub Release Assets or Repo Tree) without requiring installer.py."""
    ensure_system_certificates()
    if not install_dir:
        install_dir = str(ROOT_DIR)
    install_dir = os.path.abspath(install_dir)
    cfg = load_config(install_dir)

    selected_opts = cfg.get("selected_options", [DEFAULT_OPTION])
    eff_channel = normalize_channel(channel if channel is not None else cfg.get("channel"))

    ver = target_version or get_latest_version(channel=eff_channel)
    if not ver:
        return False

    rel_info = get_release_for_tag(ver) if target_version else None
    download_options = fetch_download_options(ver, release_info=rel_info)
    if not download_options:
        download_options = _load_local_download_options()
    if not download_options:
        return False

    eff_channel = normalize_channel(
        channel if channel is not None else cfg.get("channel"),
        download_options=download_options,
    )
    eff_build_type = normalize_build_type(
        build_type if build_type is not None else cfg.get("build_type"),
        download_options=download_options,
    )

    if is_version_ignored(ver, download_options=download_options):
        print(f"[Error] Version '{ver}' is listed in 'Old Ignore versions' and cannot be installed.", file=sys.stderr, flush=True)
        return False

    plan = collect_options_plan_details(
        download_options,
        selected_opts,
        build_type=eff_build_type,
    )
    paths = plan["paths"]
    ignore_files = plan["ignore_files"]
    messages = plan["messages"]
    configured_from = plan["download_from"]
    explicit_assets = plan["release_assets"]

    is_compiled_like = ("comp" in eff_build_type.lower()) or ("bin" in eff_build_type.lower())
    has_urls = any(str(p).strip().startswith(("http://", "https://")) for p in paths)
    check_releases = bool(
        configured_from in ("releases", "auto")
        or explicit_assets
        or has_urls
        or (configured_from != "repo" and is_compiled_like)
    )

    assets: list[dict] = []
    files: list[str] = []
    if check_releases:
        if rel_info is None:
            rel_info = get_release_for_tag(ver)
        assets = resolve_release_assets(
            paths,
            (rel_info or {}).get("assets"),
            ignore_files=ignore_files,
            explicit_assets=explicit_assets,
            build_type=eff_build_type,
        )

    matched_targets = set()
    has_full_bundle = any(a.get("full_bundle") for a in assets)
    for a in assets:
        matched_targets.update(a.get("matched_paths") or [])
    unmatched_paths = [] if has_full_bundle else [p for p in paths if p not in matched_targets]

    need_repo_tree = False
    if configured_from == "repo":
        need_repo_tree = bool(unmatched_paths)
    elif configured_from == "auto":
        need_repo_tree = bool(unmatched_paths)
    elif configured_from == "releases":
        need_repo_tree = False
    elif is_compiled_like:
        need_repo_tree = bool(unmatched_paths) and not assets
    else:
        need_repo_tree = bool(unmatched_paths)

    if need_repo_tree:
        tree = get_repo_tree(ver)
        files = resolve_files_to_download(unmatched_paths, tree, ignore_files=ignore_files)

    if not files and not assets:
        print(
            f"[Error] No downloadable files or release assets found in {ver} for build_type='{eff_build_type}'.",
            file=sys.stderr,
            flush=True,
        )
        return False

    perform_download_concurrently(files, ver, install_dir, assets=assets, ignore_files=ignore_files)
    save_updater_config(
        {
            "current_version": ver,
            "selected_options": selected_opts,
            "channel": eff_channel,
            "build_type": eff_build_type,
            "install_dir": install_dir,
        },
        install_dir=install_dir,
    )

    if messages:
        for msg in messages:
            print(f"[Info] {msg}", flush=True)
        pending_file = Path(install_dir) / ".cache" / "update_pending.json"
        try:
            pending_data = _read_json_dict(pending_file)
            pending_data["messages"] = messages
            pending_file.parent.mkdir(parents=True, exist_ok=True)
            pending_file.write_text(json.dumps(pending_data), encoding="utf-8")
        except Exception:
            pass

    target_script = relaunch_script or detect_relaunch_script(install_dir, build_type=eff_build_type)
    _launch_relaunch_target(install_dir, target_script)
    return True


def build_changelog_markdown(old_version: str, new_version: str, is_downgrade: bool | None = None) -> str:
    """Fetch commit descriptions between two tags and format them as Markdown (supports downgrades)."""
    downgrade = (not is_newer_version(old_version, new_version) and old_version != new_version) if is_downgrade is None else bool(is_downgrade)
    if downgrade:
        title = f"## Version Downgrade: `{old_version}` -> `{new_version}`\n\n_Commits present in `{old_version}` after `{new_version}`:_\n\n"
        base_ref, head_ref = new_version, old_version
    else:
        title = f"## Version Changes: `{old_version}` -> `{new_version}`\n\n"
        base_ref, head_ref = old_version, new_version
    commits = []
    try:
        url = (
            f"https://api.github.com/repos/{OWNER}/{REPO}/compare/"
            f"{urllib.parse.quote(str(base_ref), safe='')}...{urllib.parse.quote(str(head_ref), safe='')}"
        )
        _, data = _http_get(url, timeout=20)
        commits = json.loads(data.decode("utf-8")).get("commits", [])
    except Exception as e:
        return title + f"_Could not load commit descriptions ({e})._\n"

    if not commits:
        return title + "_No commits found between these versions._\n"

    lines = []
    for c in commits:
        msg = (c.get("commit", {}) or {}).get("message") or ""
        msg = msg.strip()
        if not msg:
            continue
        msg_lines = msg.splitlines()
        first_line = msg_lines[0].strip()
        lines.append(f"### {first_line}")
        body_lines = [ln.strip() for ln in msg_lines[1:] if ln.strip()]
        if body_lines:
            for bline in body_lines:
                if bline.startswith(("-", "*", "•")):
                    lines.append(f"{bline}")
                else:
                    lines.append(f"- {bline}")
        lines.append("")

    return title + "\n".join(lines).strip() + "\n"


def _marker_matches(marker_str: str) -> bool:
    if not marker_str or not marker_str.strip():
        return True
    try:
        from packaging.markers import Marker
        return bool(Marker(marker_str.strip()).evaluate())
    except Exception:
        expr = marker_str.strip()
        env = {
            "sys_platform": sys.platform,
            "os_name": os.name,
            "platform_release": platform.release(),
        }
        try:
            for k, v in env.items():
                expr = re.sub(rf"\b{k}\b", repr(v), expr)
            return bool(eval(expr, {"__builtins__": {}}, {}))
        except Exception:
            return True


# --- Requirements Verification ---
def _read_requirements() -> list[str]:
    reqs = []
    req_path = _get_requirements_path()
    if not req_path.exists():
        return reqs
    try:
        for line in req_path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            if ";" in line:
                req_part, marker_part = line.split(";", 1)
                if not _marker_matches(marker_part):
                    continue
                line = req_part.strip()
            if line:
                reqs.append(line)
    except Exception:
        pass
    return reqs


def requirements_hash() -> str:
    try:
        return hashlib.sha256(_get_requirements_path().read_bytes()).hexdigest()
    except Exception:
        return ""


def find_unsatisfied_requirements() -> list[str]:
    import importlib.metadata as md

    missing = []
    for line in _read_requirements():
        m = re.match(r"^([a-zA-Z0-9_\-\.]+)(?:\[(.*?)\])?\s*([<>=!~]+)?\s*([0-9a-zA-Z\.\-]+)?", line)
        if not m:
            pkg_name = re.split(r"[\[<>=!~ ;]", line, 1)[0].strip()
            try:
                md.version(pkg_name)
            except md.PackageNotFoundError:
                missing.append(line)
            continue

        pkg_name, extras, op, req_ver = m.groups()
        try:
            installed_ver = md.version(pkg_name)
        except md.PackageNotFoundError:
            missing.append(line)
            continue

        if op == ">=" and req_ver:
            if _parse_version_tuple(installed_ver) < _parse_version_tuple(req_ver):
                missing.append(f"{line} (installed: {installed_ver})")
                continue

        if extras and "broker" in extras.lower() and os.name == "nt":
            try:
                md.version("pymsalruntime")
            except md.PackageNotFoundError:
                missing.append(f"{line} (missing pymsalruntime for broker)")
                continue

    return missing


def requirements_need_install() -> tuple[bool, list[str]]:
    if getattr(sys, "frozen", False):
        return False, []
    if not _get_requirements_path().exists():
        return False, []

    reasons = []
    unsatisfied = find_unsatisfied_requirements()
    if unsatisfied:
        reasons.append("Missing / outdated packages: " + ", ".join(unsatisfied))

    stored_hash = ""
    try:
        if REQ_HASH_PATH.exists():
            stored_hash = REQ_HASH_PATH.read_text(encoding="utf-8").strip()
    except Exception:
        pass

    curr_hash = requirements_hash()
    if stored_hash and stored_hash != curr_hash:
        reasons.append("backend/requirements.txt has been modified")
    elif not stored_hash and not reasons:
        # First time running and all requirements are already met
        mark_requirements_installed()

    return bool(reasons), reasons


def mark_requirements_installed():
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        REQ_HASH_PATH.write_text(requirements_hash(), encoding="utf-8")
    except Exception:
        pass


# --- Background Threads ---
class _VersionCheckThread(QThread):
    result = pyqtSignal(object)

    def __init__(self, channel: str | None = None, parent=None):
        super().__init__(parent)
        self.channel = channel

    def run(self):
        self.result.emit(get_latest_version(channel=self.channel))


class _ReleasesListThread(QThread):
    result = pyqtSignal(list)

    def __init__(self, include_prereleases: bool = True, parent=None):
        super().__init__(parent)
        self.include_prereleases = include_prereleases

    def run(self):
        try:
            items = list_installable_versions(include_prereleases=self.include_prereleases)
        except Exception:
            items = []
        self.result.emit(items)


class _RequirementsCheckThread(QThread):
    result = pyqtSignal(bool, list)

    def run(self):
        try:
            need, reasons = requirements_need_install()
        except Exception:
            need, reasons = False, []
        self.result.emit(need, reasons)


class _ChangelogThread(QThread):
    result = pyqtSignal(str)

    def __init__(self, old: str, new: str, parent=None):
        super().__init__(parent)
        self.old = old
        self.new = new

    def run(self):
        self.result.emit(build_changelog_markdown(self.old, self.new))


# --- Dialogs ---
class ChangelogDialog(QDialog):
    """Displays commit descriptions between two versions in Markdown format (supports upgrades & downgrades)."""

    def __init__(
        self,
        parent,
        old_version: str,
        new_version: str,
        for_confirmation: bool = False,
        extra_messages: list[str] | None = None,
        is_downgrade: bool | None = None,
    ):
        super().__init__(parent)
        self.for_confirmation = for_confirmation
        self.old_version = old_version
        self.new_version = new_version
        self.is_downgrade = (
            (not is_newer_version(old_version, new_version) and old_version != new_version)
            if is_downgrade is None
            else bool(is_downgrade)
        )
        self.extra_messages = [str(m).strip() for m in (extra_messages or []) if str(m).strip()]

        if for_confirmation:
            action_word = "Downgrade" if self.is_downgrade else "Version Changes"
            title = f"{action_word} ({old_version} -> {new_version})"
        else:
            title = "What's New in Update"
        self.setWindowTitle(title)
        self.resize(620, 480)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)

        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(True)
        self.browser.setMarkdown(self._compose_markdown(
            f"## Version Changes: `{old_version}` -> `{new_version}`\n\n_Loading commit descriptions from GitHub..._"
        ))
        layout.addWidget(self.browser, stretch=1)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch(1)

        if for_confirmation:
            self.cancel_btn = QPushButton("Cancel")
            self.cancel_btn.clicked.connect(self.reject)
            btn_layout.addWidget(self.cancel_btn)

            btn_text = f"Downgrade to {new_version}" if self.is_downgrade else f"Update to {new_version}"
            self.update_btn = QPushButton(btn_text)
            self.update_btn.setDefault(True)
            self.update_btn.setStyleSheet("font-weight: bold;")
            self.update_btn.clicked.connect(self.accept)
            btn_layout.addWidget(self.update_btn)
        else:
            self.close_btn = QPushButton("Close")
            self.close_btn.setDefault(True)
            self.close_btn.clicked.connect(self.accept)
            btn_layout.addWidget(self.close_btn)

        layout.addLayout(btn_layout)

        self._thread = _ChangelogThread(old_version, new_version, self)
        self._thread.result.connect(lambda md: self.browser.setMarkdown(self._compose_markdown(md)))
        self._thread.start()

    def _compose_markdown(self, body_md: str) -> str:
        if not self.extra_messages:
            return body_md
        notes = "\n\n".join(f"> **Note:** {msg}" for msg in self.extra_messages)
        return f"{body_md.rstrip()}\n\n---\n\n{notes}\n"


class UpdaterSettingsDialog(QDialog):
    """Configure Release Channel (Stable/Pre-release), Build Type (Source/Compiled), and Upgrade/Downgrade version."""

    def __init__(self, parent, controller: "UpdateController"):
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle("Updater Options — Channel, Build Type & Downgrade")
        self.resize(520, 340)
        cfg = load_config()
        self._channel = normalize_channel(cfg.get("channel"))
        self._build_type = normalize_build_type(cfg.get("build_type"))
        self._chosen_target_version: str | None = None
        self._action_requested = "save"  # "save" or "install"
        self._rthread = None

        v = QVBoxLayout(self)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(10)

        prefs_box = QGroupBox("Updater Channel & Download Type")
        form = QFormLayout(prefs_box)
        form.setSpacing(8)

        cur_lbl = QLabel(f"{controller.current}  (Channel: {self._channel}, Build: {self._build_type})")
        cur_lbl.setStyleSheet("font-weight: bold;")
        form.addRow("Installed Version:", cur_lbl)

        local_opts = _load_local_download_options()
        self.channel_combo = QComboBox()
        for ch_val in extract_channels(local_opts):
            lbl = "Pre-releases (Beta / Test Releases)" if ch_val == "prerelease" else "Stable Releases"
            self.channel_combo.addItem(lbl, ch_val)
        idx_c = self.channel_combo.findData(self._channel)
        if idx_c >= 0:
            self.channel_combo.setCurrentIndex(idx_c)
        self.channel_combo.currentIndexChanged.connect(lambda _i: self._load_versions_async())
        form.addRow("Release Channel:", self.channel_combo)

        self.build_combo = QComboBox()
        for bt_val in extract_build_types(local_opts):
            low = bt_val.lower()
            if low == "source":
                bt_lbl = "Source Code (source)"
            elif low == "compiled":
                bt_lbl = "Compiled Application (compiled)"
            else:
                bt_lbl = f"{bt_val.replace('_', ' ').title()} ({bt_val})"
            self.build_combo.addItem(bt_lbl, bt_val)
        idx_b = self.build_combo.findData(self._build_type)
        if idx_b >= 0:
            self.build_combo.setCurrentIndex(idx_b)
        form.addRow("Build / Format:", self.build_combo)
        v.addWidget(prefs_box)

        ver_box = QGroupBox("Install Specific Version / Downgrade")
        vb = QVBoxLayout(ver_box)
        vb.setSpacing(6)
        self.version_combo = QComboBox()
        self.version_combo.addItem("Loading versions from GitHub...", None)
        vb.addWidget(self.version_combo)

        ignored = get_ignored_old_versions()
        self.ignore_note = QLabel(
            f"Legacy versions blocked from downgrade ('Old Ignore versions'): {', '.join(ignored)}"
            if ignored
            else "All published non-ignored versions shown."
        )
        self.ignore_note.setWordWrap(True)
        self.ignore_note.setStyleSheet("color: gray; font-size: 11px;")
        vb.addWidget(self.ignore_note)
        v.addWidget(ver_box)

        btn_row = QHBoxLayout()
        self.install_sel_btn = QPushButton("Install / Downgrade Selected Version...")
        self.install_sel_btn.setEnabled(False)
        self.install_sel_btn.clicked.connect(self._on_install_selected)
        btn_row.addWidget(self.install_sel_btn)
        btn_row.addStretch(1)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        self.save_btn = QPushButton("Save Preferences && Check")
        self.save_btn.setDefault(True)
        self.save_btn.setStyleSheet("font-weight: bold;")
        self.save_btn.clicked.connect(self._on_save_prefs)
        btn_row.addWidget(self.save_btn)
        v.addLayout(btn_row)

        self._load_versions_async()

    def _load_versions_async(self):
        inc_pre = (self.channel_combo.currentData() == "prerelease")
        self.version_combo.clear()
        self.version_combo.addItem("Loading versions from GitHub...", None)
        self.install_sel_btn.setEnabled(False)
        self._rthread = _ReleasesListThread(include_prereleases=inc_pre, parent=self)
        self._rthread.result.connect(self._populate_versions)
        self._rthread.start()

    def _populate_versions(self, items: list[dict]):
        self.version_combo.clear()
        if not items:
            self.version_combo.addItem("Could not load versions from GitHub", None)
            self.install_sel_btn.setEnabled(False)
            return

        cur = self.controller.current
        for idx, rel in enumerate(items):
            tag = rel["tag_name"]
            tags = []
            if idx == 0:
                tags.append("Latest")
            if rel.get("prerelease"):
                tags.append("Pre-release")
            if tag == cur:
                tags.append("Current")
            elif cur and not is_newer_version(cur, tag):
                tags.append("Downgrade")
            elif cur and is_newer_version(cur, tag):
                tags.append("Update")
            suffix = f"  [{' | '.join(tags)}]" if tags else ""
            self.version_combo.addItem(f"{tag}{suffix}", tag)

        self.install_sel_btn.setEnabled(True)

    def _on_save_prefs(self):
        self._action_requested = "save"
        self.accept()

    def _on_install_selected(self):
        ver = self.version_combo.currentData()
        if not ver:
            return
        self._chosen_target_version = str(ver)
        self._action_requested = "install"
        self.accept()

    def selected_channel(self) -> str:
        return str(self.channel_combo.currentData() or "stable")

    def selected_build_type(self) -> str:
        return str(self.build_combo.currentData() or "source")


class RequirementsInstallDialog(QDialog):
    """Executes pip install -r backend/requirements.txt with a live progress bar and log viewer."""

    def __init__(self, parent, reasons: list[str]):
        super().__init__(parent)
        self.setWindowTitle("Installing Python Requirements")
        self.resize(580, 380)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)

        desc = "Updating Python dependencies required by PyMcLauncher:\n" + "\n".join(f"• {r}" for r in reasons)
        self.desc_label = QLabel(desc)
        self.desc_label.setWordWrap(True)
        layout.addWidget(self.desc_label)

        self.status_label = QLabel("Starting pip...")
        layout.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(5)
        layout.addWidget(self.progress_bar)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        mono_font = QFont("Menlo" if sys.platform == "darwin" else "Consolas")
        mono_font.setStyleHint(QFont.StyleHint.Monospace)
        self.log_text.setFont(mono_font)
        layout.addWidget(self.log_text, stretch=1)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch(1)
        self.close_btn = QPushButton("Close")
        self.close_btn.setEnabled(False)
        self.close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(self.close_btn)
        layout.addLayout(btn_layout)

        all_reqs = _read_requirements()
        self.total_reqs = max(len(all_reqs), 1)
        self.collected_count = 0

        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._handle_output)
        self.proc.finished.connect(self._handle_finished)

        ensure_system_certificates()

        python_exe = sys.executable
        if python_exe.lower().endswith("pythonw.exe"):
            cand = python_exe[:-len("pythonw.exe")] + "python.exe"
            if os.path.exists(cand):
                python_exe = cand

        self.proc.start(
            python_exe,
            [
                "-m", "pip", "install",
                "--prefer-binary",
                "--progress-bar", "off",
                "--disable-pip-version-check",
                "-r", str(_get_requirements_path()),
            ],
        )

    def _handle_output(self):
        raw = bytes(self.proc.readAllStandardOutput()).decode("utf-8", errors="replace")
        for line in raw.splitlines():
            line = line.rstrip()
            if not line:
                continue
            self.log_text.append(line)
            sb = self.log_text.verticalScrollBar()
            sb.setValue(sb.maximum())

            low = line.lower()
            if low.startswith("collecting") or low.startswith("requirement already satisfied"):
                self.collected_count += 1
                pct = min(75, int((self.collected_count / self.total_reqs) * 70) + 10)
                self.progress_bar.setValue(pct)
                self.status_label.setText(line[:80])
            elif "installing collected packages" in low:
                self.progress_bar.setValue(85)
                self.status_label.setText("Installing packages...")
            elif "successfully installed" in low:
                self.progress_bar.setValue(100)
                self.status_label.setText("Installation successful!")

    def _handle_finished(self, code: int, status):
        if code == 0:
            self.progress_bar.setValue(100)
            self.status_label.setText("All requirements are up to date!")
            mark_requirements_installed()
        else:
            self.status_label.setText(f"Installation failed (exit code {code}). Check the log above.")
        self.close_btn.setEnabled(True)

    def reject(self):
        if self.proc.state() != QProcess.ProcessState.NotRunning:
            return  # Prevent closing dialog while pip is actively running
        super().reject()


# --- Update Controller for LauncherGUI ---
class UpdateController:
    """Manages update checking, channel/build-type preferences, downgrades, and requirements check."""

    def __init__(self, launcher):
        self.launcher = launcher
        cfg = load_config()
        self.latest = None
        self.current = str(cfg.get("current_version") or get_current_version())
        self.channel = normalize_channel(cfg.get("channel"))
        self.build_type = normalize_build_type(cfg.get("build_type"))
        self._status_kind = "checking"
        self._is_dark = False

        # Add update row at the bottom of the launcher layout
        self.row_layout = QHBoxLayout()
        self.label = QLabel("Checking for updates...")
        self.button = QPushButton("Update")
        self.button.setVisible(False)
        self.button.clicked.connect(lambda _c=False: self.start_update())

        self.options_button = QPushButton("Updater Options...")
        self.options_button.setToolTip("Switch channel (Stable / Pre-release), build type (Source / Compiled), or downgrade")
        self.options_button.clicked.connect(self.open_updater_settings)

        self.row_layout.addWidget(self.label, stretch=1)
        self.row_layout.addWidget(self.button)
        self.row_layout.addWidget(self.options_button)
        self.launcher.layout.addLayout(self.row_layout)
        self.apply_theme(False)

        # Trigger background checks shortly after GUI is displayed
        QTimer.singleShot(1000, self._startup)

    def apply_theme(self, is_dark: bool):
        self._is_dark = bool(is_dark)
        if self._status_kind == "uptodate":
            color = "#5cd65c" if is_dark else "#2e8b2e"
            self.label.setStyleSheet(f"color: {color}; font-weight: 500;")
        elif self._status_kind == "update":
            color = "#ffb347" if is_dark else "#cc8800"
            self.label.setStyleSheet(f"color: {color}; font-weight: bold;")
        else:
            color = "#9aa0a6" if is_dark else "#666666"
            self.label.setStyleSheet(f"color: {color};")

    def _startup(self):
        self._show_pending_changelog()
        self._check_version()
        self._check_requirements()

    def _show_pending_changelog(self):
        if not PENDING_PATH.exists():
            return
        try:
            data = json.loads(PENDING_PATH.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
        try:
            PENDING_PATH.unlink()
        except Exception:
            pass

        old_ver = data.get("from")
        new_ver = data.get("to")
        messages = data.get("messages") if isinstance(data.get("messages"), list) else None
        if old_ver and new_ver:
            dlg = ChangelogDialog(self.launcher, old_ver, new_ver, for_confirmation=False, extra_messages=messages)
            dlg.exec()

    def _check_version(self):
        self.label.setText("Checking for updates...")
        self._status_kind = "checking"
        self.apply_theme(self._is_dark)
        self._vthread = _VersionCheckThread(channel=self.channel)
        self._vthread.result.connect(self._on_latest_received)
        self._vthread.start()

    def _on_latest_received(self, latest: str | None):
        self.latest = latest
        cur = self.current
        mode_tag = f"{self.channel}/{self.build_type}"

        if not latest:
            shown = f" (current: {cur}, {mode_tag})" if cur else f" ({mode_tag})"
            self.label.setText(f"Could not check for updates{shown}.")
            self._status_kind = "error"
            self.button.setVisible(False)
        elif not cur or cur == latest:
            self.label.setText(f"Up to date - version {cur or latest} ({mode_tag})")
            self._status_kind = "uptodate"
            self.button.setVisible(False)
        else:
            verb = "Update available" if is_newer_version(cur, latest) else "Target release"
            self.label.setText(f"{verb}: {cur} -> {latest} ({mode_tag})")
            self._status_kind = "update"
            btn_verb = "Update" if is_newer_version(cur, latest) else "Switch"
            self.button.setText(f"{btn_verb} to {latest}")
            self.button.setVisible(True)

        self.apply_theme(self._is_dark)
        self.launcher.update_window_size()

    def open_updater_settings(self):
        dlg = UpdaterSettingsDialog(self.launcher, self)
        if not dlg.exec():
            return
        new_ch = dlg.selected_channel()
        new_bt = dlg.selected_build_type()
        self.channel = new_ch
        self.build_type = new_bt

        cfg = load_config()
        cfg["channel"] = new_ch
        cfg["build_type"] = new_bt
        save_updater_config(cfg)

        if dlg._action_requested == "install" and dlg._chosen_target_version:
            self.start_update(target_version=dlg._chosen_target_version)
        else:
            self._check_version()

    def _check_requirements(self):
        self._rthread = _RequirementsCheckThread()
        self._rthread.result.connect(self._on_requirements_checked)
        self._rthread.start()

    def _on_requirements_checked(self, need_install: bool, reasons: list[str]):
        if need_install:
            dlg = RequirementsInstallDialog(self.launcher, reasons)
            dlg.exec()

    def start_update(self, target_version: str | None = None):
        target = target_version or self.latest
        if not target or not self.current:
            return
        if target == self.current and not target_version:
            return

        if is_version_ignored(target):
            QMessageBox.warning(
                self.launcher,
                "Blocked Legacy Version",
                f"Version '{target}' is listed in 'Old Ignore versions' and cannot be installed.",
            )
            return

        if getattr(self.launcher, "running_instances", None):
            QMessageBox.warning(
                self.launcher,
                "Game Running",
                "Please close all running Minecraft instances before updating the launcher.",
            )
            return

        # Show version changes (supports both upgrades & downgrades!)
        is_down = not is_newer_version(self.current, target) and self.current != target
        dialog = ChangelogDialog(self.launcher, self.current, target, for_confirmation=True, is_downgrade=is_down)
        if not dialog.exec():
            return

        # Proceed via backend/launcher_update.py (preserves channel, build_type & exact target_version)
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            PENDING_PATH.write_text(json.dumps({"from": self.current, "to": target}), encoding="utf-8")

            common_flags = [
                "--update-check",
                "--install-dir",
                str(ROOT_DIR),
                "--channel",
                self.channel,
                "--build-type",
                self.build_type,
                "--target-version",
                target,
            ]
            relaunch = detect_relaunch_script(ROOT_DIR, build_type=self.build_type)
            if relaunch:
                common_flags.extend(["--relaunch", relaunch])

            if getattr(sys, "frozen", False):
                import tempfile
                exe_path = Path(sys.executable).resolve()
                temp_updater = Path(tempfile.gettempdir()) / f"pymclauncher_updater_{os.getpid()}{exe_path.suffix}"
                shutil.copy2(exe_path, temp_updater)
                if os.name != "nt":
                    try:
                        os.chmod(temp_updater, 0o755)
                    except Exception:
                        pass
                cmd = [
                    str(temp_updater),
                    "--internal-update",
                    "--parent-pid",
                    str(os.getpid()),
                    "--self-cleanup",
                    *common_flags,
                ]
            else:
                python_exe = sys.executable
                if python_exe.lower().endswith("pythonw.exe"):
                    cand = python_exe[:-len("pythonw.exe")] + "python.exe"
                    if os.path.exists(cand):
                        python_exe = cand
                updater_script = str(Path(__file__).resolve())
                cmd = [python_exe, updater_script, *common_flags]

            popen_kwargs = {"cwd": str(ROOT_DIR)}
            if os.name == "nt":
                popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            else:
                popen_kwargs["start_new_session"] = True
            subprocess.Popen(cmd, **popen_kwargs)
        except Exception as e:
            QMessageBox.critical(self.launcher, "Update Failed", f"Could not start updater: {e}")
            return

        QApplication.quit()


def _wait_for_pid_exit(pid: int, timeout_s: float = 8.0):
    import time
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if os.name == "nt":
                import ctypes
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.OpenProcess(0x00100000, False, int(pid))
                if not handle:
                    return
                res = kernel32.WaitForSingleObject(handle, 250)
                kernel32.CloseHandle(handle)
                if res == 0:
                    return
            else:
                os.kill(int(pid), 0)
                time.sleep(0.15)
        except OSError:
            return
        except Exception:
            time.sleep(0.15)


def _schedule_temp_binary_cleanup(bin_path: str):
    try:
        target = os.path.abspath(bin_path)
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            subprocess.Popen(
                f'cmd.exe /c timeout /t 2 >nul & del /f /q "{target}"',
                shell=True,
                creationflags=flags,
            )
        else:
            subprocess.Popen(
                f'sleep 2 && rm -f "{target}"',
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
    except Exception:
        pass


def run_cli(argv: list[str] | None = None):
    import argparse
    parser = argparse.ArgumentParser(
        description="Minecraft Launcher updater and requirements checker CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python launcher_update.py --check
  python launcher_update.py --check --channel prerelease
  python launcher_update.py --list-versions --channel prerelease
  python launcher_update.py --requirements
  python launcher_update.py --version
  python launcher_update.py --update-check --install-dir . --channel prerelease --build-type compiled
  python launcher_update.py --update-check --target-version v3.0
"""
    )
    parser.add_argument("--check", action="store_true", help="Check for available releases on GitHub")
    parser.add_argument("--list-versions", action="store_true", help="List available versions (excluding Old Ignore versions)")
    parser.add_argument("--requirements", action="store_true", help="Check installed Python packages against backend/requirements.txt")
    parser.add_argument("--version", action="store_true", help="Display current launcher version")
    parser.add_argument("--update-check", action="store_true", help="Perform silent update/downgrade download")
    parser.add_argument("--channel", type=str, default=None, help="Release channel: stable or prerelease")
    parser.add_argument("--build-type", type=str, default=None, help="Build format: source or compiled")
    parser.add_argument("--target-version", type=str, default=None, help="Specific release tag to install/downgrade to")
    parser.add_argument("--relaunch", type=str, default=None, help="Script to relaunch after updating")
    parser.add_argument("--install-dir", type=str, default=None, help="Target installation directory")
    parser.add_argument("--parent-pid", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--self-cleanup", action="store_true", help=argparse.SUPPRESS)

    raw_args = list(sys.argv[1:] if argv is None else argv)
    if not raw_args:
        parser.print_help()
        return 0

    args, _ = parser.parse_known_args(raw_args)

    if args.parent_pid:
        _wait_for_pid_exit(args.parent_pid)

    exit_code = 0
    try:
        if args.update_check:
            ok = run_update_check(
                args.install_dir,
                args.relaunch,
                target_version=args.target_version,
                channel=args.channel,
                build_type=args.build_type,
            )
            exit_code = 0 if ok is not False else 1
        elif args.version:
            cfg_now = load_config()
            print(f"Current Launcher Version: {get_current_version()} (channel={cfg_now.get('channel')}, build={cfg_now.get('build_type')})")
        elif args.requirements:
            missing = find_unsatisfied_requirements()
            if missing:
                print(f"[Warning] Missing requirements ({len(missing)}):")
                for m in missing:
                    print(f"  - {m}")
            else:
                print("[OK] All packages in backend/requirements.txt are installed.")
        elif args.list_versions:
            vers = list_installable_versions(channel=args.channel)
            for v in vers:
                tag_type = "Pre-release" if v.get("prerelease") else "Release"
                assets_str = ", ".join(a["name"] for a in (v.get("assets") or []))
                print(f"{v['tag_name']:<16} [{tag_type}]  assets=[{assets_str}]")
        elif args.check:
            cur = get_current_version()
            ch = normalize_channel(args.channel if args.channel is not None else load_config().get("channel"))
            print(f"Current version: {cur} (channel: {ch})")
            print("Checking GitHub releases for updates...")
            latest = get_latest_version(channel=ch)
            if latest and is_newer_version(cur, latest):
                print(f"[Update Available] A new version is available: {latest} (current: {cur})")
                diff = build_changelog_markdown(cur, latest)
                if diff:
                    print("\nChanges:")
                    print(diff)
            elif latest and cur != latest:
                print(f"[Different Tag Available] Remote latest tag: {latest} (current: {cur})")
            elif latest:
                print(f"[Up to Date] Launcher is up to date ({cur}).")
            else:
                print("[Info] Could not query remote release tag.")
    finally:
        if args.self_cleanup and getattr(sys, "frozen", False):
            _schedule_temp_binary_cleanup(sys.executable)
    return exit_code


if __name__ == "__main__":
    sys.exit(run_cli())

