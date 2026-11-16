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
        QApplication, QDialog, QHBoxLayout, QLabel, QMessageBox, QProgressBar, QPushButton,
        QTextBrowser, QTextEdit, QVBoxLayout,
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

_pkg_dir = Path(__file__).resolve().parent
ROOT_DIR = _pkg_dir.parent if _pkg_dir.name == "backend" else _pkg_dir
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
    if REQUIREMENTS_PATH.exists():
        return REQUIREMENTS_PATH
    if LEGACY_REQUIREMENTS_PATH.exists():
        return LEGACY_REQUIREMENTS_PATH
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


def save_updater_config(updater_cfg: dict, install_dir: str | Path | None = None):
    """Save updater state under launcher_config.json['updater'] without clobbering launcher settings."""
    base = Path(install_dir) if install_dir else ROOT_DIR
    base.mkdir(parents=True, exist_ok=True)
    cfg_path = base / CONFIG_FILE
    legacy_path = base / LEGACY_CONFIG_FILE

    full_cfg = _read_json_dict(cfg_path)
    full_cfg["updater"] = {
        "current_version": str(updater_cfg.get("current_version") or "v1.7"),
        "selected_options": list(updater_cfg.get("selected_options") or [DEFAULT_OPTION]),
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

    full_cfg = _read_json_dict(cfg_path)
    updater = full_cfg.get("updater")
    if isinstance(updater, dict) and updater:
        return dict(updater)

    if any(k in full_cfg for k in ("current_version", "selected_options", "install_dir")):
        migrated = {
            "current_version": str(full_cfg.get("current_version") or "v1.7"),
            "selected_options": list(full_cfg.get("selected_options") or [DEFAULT_OPTION]),
            "install_dir": str(full_cfg.get("install_dir") or base),
        }
        save_updater_config(migrated, base)
        return migrated

    legacy_cfg = _read_json_dict(legacy_path)
    if legacy_cfg:
        migrated = {
            "current_version": str(legacy_cfg.get("current_version") or "v1.7"),
            "selected_options": list(legacy_cfg.get("selected_options") or [DEFAULT_OPTION]),
            "install_dir": str(legacy_cfg.get("install_dir") or base),
        }
        save_updater_config(migrated, base)
        return migrated

    default_cfg = {
        "current_version": "v1.7",
        "selected_options": [DEFAULT_OPTION],
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
        ver = "v1.7"
        cfg["current_version"] = ver
        try:
            save_updater_config(cfg)
        except Exception:
            pass
    return str(ver)


def get_latest_version() -> str | None:
    """Resolve the latest release tag using GitHub API, web redirect, and tags endpoints."""
    url = f"https://api.github.com/repos/{OWNER}/{REPO}/releases/latest"
    content = download_raw_url(url, extra_headers={"Accept": "application/vnd.github+json"})
    if content:
        try:
            data = json.loads(content.decode("utf-8"))
            tag = data.get("tag_name")
            if tag:
                return str(tag)
        except Exception:
            pass

    tag = _get_redirect_tag(f"https://github.com/{OWNER}/{REPO}/releases/latest")
    if tag:
        return tag

    content = download_raw_url(
        f"https://api.github.com/repos/{OWNER}/{REPO}/tags",
        extra_headers={"Accept": "application/vnd.github+json"},
    )
    if content:
        try:
            tags = json.loads(content.decode("utf-8"))
            if isinstance(tags, list) and tags and "name" in tags[0]:
                return str(tags[0]["name"])
        except Exception:
            pass

    return None


def _parse_version_tuple(v_str: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", v_str)
    return tuple(int(x) for x in nums)


def is_newer_version(current: str, latest: str) -> bool:
    if not latest or current == latest:
        return False
    cur_t = _parse_version_tuple(current)
    lat_t = _parse_version_tuple(latest)
    if cur_t and lat_t:
        return lat_t > cur_t
    return current.strip() != latest.strip()


def fetch_download_options(tag: str):
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


def resolve_option_platform_data(opt_data, platform_key: str | None = None) -> dict:
    """Resolve Files, Update Ignore Files, and Message for nested platform or legacy flat options."""
    plat = platform_key or get_platform_key()
    empty = {"Files": [], "Update Ignore Files": [], "Message": None}
    if isinstance(opt_data, list):
        return {"Files": list(opt_data), "Update Ignore Files": [], "Message": None}
    if not isinstance(opt_data, dict):
        return empty

    target = opt_data
    if any(k in opt_data for k in ("win", "macos", "other")):
        target = opt_data.get(plat)
        if not isinstance(target, dict):
            for fallback_k in ("other", "macos", "win"):
                if isinstance(opt_data.get(fallback_k), dict):
                    target = opt_data[fallback_k]
                    break
        if not isinstance(target, dict):
            return empty

    files = list(target.get("Files") or [])
    ignores = list(target.get("Update Ignore Files") or [])
    msg = target.get("Message")
    return {
        "Files": files,
        "Update Ignore Files": ignores,
        "Message": str(msg).strip() if msg else None,
    }


def collect_options_plan(download_options, selected_opts=None, platform_key: str | None = None):
    """Collect deduplicated Files, Update Ignore Files, and Messages for selected options."""
    if not isinstance(download_options, dict) or not download_options:
        return [], [], []

    chosen = [opt for opt in (selected_opts or []) if opt in download_options]
    if not chosen:
        chosen = [DEFAULT_OPTION] if DEFAULT_OPTION in download_options else [next(iter(download_options.keys()))]

    paths = []
    ignore_files = []
    messages = []
    for opt in chosen:
        resolved = resolve_option_platform_data(download_options[opt], platform_key=platform_key)
        for p in resolved["Files"]:
            if p not in paths:
                paths.append(p)
        for ig in resolved["Update Ignore Files"]:
            if ig not in ignore_files:
                ignore_files.append(ig)
        msg = resolved.get("Message")
        if msg and msg not in messages:
            messages.append(msg)
    return paths, ignore_files, messages


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
                elif not is_dir and op == clean:
                    if not any(op.lower() == ig for ig in ignore_set):
                        files.append(obj["path"])
    return sorted(list(set(files)))


def perform_download_concurrently(files, tag, install_dir, progress_cb=None):
    os.makedirs(install_dir, exist_ok=True)
    total = len(files)
    completed = 0
    lock = threading.Lock()

    def task(fp):
        nonlocal completed
        try:
            local_fp = os.path.join(install_dir, fp.replace("/", os.sep))
            os.makedirs(os.path.dirname(local_fp), exist_ok=True)
            data = download_file_content(fp, tag)
            if data is not None:
                tmp_fp = local_fp + ".tmp"
                try:
                    with open(tmp_fp, "wb") as f:
                        f.write(data)
                    os.replace(tmp_fp, local_fp)
                except Exception:
                    with open(local_fp, "wb") as f:
                        f.write(data)
                    if os.path.exists(tmp_fp):
                        try:
                            os.remove(tmp_fp)
                        except Exception:
                            pass
                if os.name != "nt" and (fp.endswith(".command") or fp.endswith(".sh") or fp.endswith(".py")):
                    try:
                        os.chmod(local_fp, 0o755)
                    except Exception:
                        pass
        except Exception as e:
            print(f"Error downloading {fp}: {e}", file=sys.stderr, flush=True)
        finally:
            with lock:
                completed += 1
                if progress_cb:
                    progress_cb(completed, total, fp)

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(task, f) for f in files]
        concurrent.futures.wait(futures)


def detect_relaunch_script(install_dir: Path | str | None = None) -> str | None:
    """Return the platform-appropriate setup-start script filename if present."""
    base = Path(install_dir) if install_dir else ROOT_DIR
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


def run_update_check(install_dir=None, relaunch_script=None):
    """Download the latest release files directly without requiring installer.py."""
    ensure_system_certificates()
    if not install_dir:
        install_dir = str(ROOT_DIR)
    install_dir = os.path.abspath(install_dir)
    cfg = load_config(install_dir)

    selected_opts = cfg.get("selected_options", [DEFAULT_OPTION])
    ver = get_latest_version()
    if not ver:
        return
    download_options = fetch_download_options(ver)
    if not download_options:
        return

    paths, ignore_files, messages = collect_options_plan(download_options, selected_opts)
    tree = get_repo_tree(ver)
    files = resolve_files_to_download(paths, tree, ignore_files=ignore_files)
    if not files:
        return

    perform_download_concurrently(files, ver, install_dir)
    save_updater_config(
        {
            "current_version": ver,
            "selected_options": selected_opts,
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

    target_script = relaunch_script or detect_relaunch_script(install_dir)
    _launch_relaunch_target(install_dir, target_script)


def build_changelog_markdown(old_version: str, new_version: str) -> str:
    """Fetch commit descriptions between two tags and format them as Markdown."""
    title = f"## Version Changes: `{old_version}` -> `{new_version}`\n\n"
    commits = []
    try:
        url = (
            f"https://api.github.com/repos/{OWNER}/{REPO}/compare/"
            f"{urllib.parse.quote(str(old_version), safe='')}...{urllib.parse.quote(str(new_version), safe='')}"
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

    def run(self):
        self.result.emit(get_latest_version())


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
    """Displays commit descriptions between two versions in Markdown format."""

    def __init__(self, parent, old_version: str, new_version: str, for_confirmation: bool = False, extra_messages: list[str] | None = None):
        super().__init__(parent)
        self.for_confirmation = for_confirmation
        self.old_version = old_version
        self.new_version = new_version
        self.extra_messages = [str(m).strip() for m in (extra_messages or []) if str(m).strip()]

        title = f"Version Changes ({old_version} -> {new_version})" if for_confirmation else "What's New in Update"
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

            self.update_btn = QPushButton(f"Update to {new_version}")
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
    """Manages update checking, display in the launcher footer, and requirements auto-check."""

    def __init__(self, launcher):
        self.launcher = launcher
        self.latest = None
        self.current = get_current_version()
        self._status_kind = "checking"
        self._is_dark = False

        # Add update row at the bottom of the launcher layout
        self.row_layout = QHBoxLayout()
        self.label = QLabel("Checking for updates...")
        self.button = QPushButton("Update")
        self.button.setVisible(False)
        self.button.clicked.connect(self.start_update)

        self.row_layout.addWidget(self.label, stretch=1)
        self.row_layout.addWidget(self.button)
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
        self._vthread = _VersionCheckThread()
        self._vthread.result.connect(self._on_latest_received)
        self._vthread.start()

    def _on_latest_received(self, latest: str | None):
        self.latest = latest
        cur = self.current

        if not latest:
            shown = f" (current: {cur})" if cur else ""
            self.label.setText(f"Could not check for updates{shown}.")
            self._status_kind = "error"
        elif not cur or cur == latest:
            self.label.setText(f"Up to date - version {cur or latest}")
            self._status_kind = "uptodate"
        else:
            self.label.setText(f"Update available: {cur} -> {latest}")
            self._status_kind = "update"
            self.button.setText(f"Update to {latest}")
            self.button.setVisible(True)

        self.apply_theme(self._is_dark)
        self.launcher.update_window_size()

    def _check_requirements(self):
        self._rthread = _RequirementsCheckThread()
        self._rthread.result.connect(self._on_requirements_checked)
        self._rthread.start()

    def _on_requirements_checked(self, need_install: bool, reasons: list[str]):
        if need_install:
            dlg = RequirementsInstallDialog(self.launcher, reasons)
            dlg.exec()

    def start_update(self):
        if not self.latest or not self.current or self.current == self.latest:
            return

        if getattr(self.launcher, "running_instances", None):
            QMessageBox.warning(
                self.launcher,
                "Game Running",
                "Please close all running Minecraft instances before updating the launcher.",
            )
            return

        # Show the version changes in Markdown format for confirmation
        dialog = ChangelogDialog(self.launcher, self.current, self.latest, for_confirmation=True)
        if not dialog.exec():
            return

        # Proceed with update directly via backend/launcher_update.py (works even when installer.py deleted itself)
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            PENDING_PATH.write_text(json.dumps({"from": self.current, "to": self.latest}), encoding="utf-8")

            python_exe = sys.executable
            if python_exe.lower().endswith("pythonw.exe"):
                cand = python_exe[:-len("pythonw.exe")] + "python.exe"
                if os.path.exists(cand):
                    python_exe = cand

            updater_script = str(Path(__file__).resolve())
            cmd = [
                python_exe,
                updater_script,
                "--update-check",
                "--install-dir",
                str(ROOT_DIR),
            ]
            relaunch = detect_relaunch_script(ROOT_DIR)
            if relaunch:
                cmd.extend(["--relaunch", relaunch])

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


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Minecraft Launcher updater and requirements checker CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python launcher_update.py --check
  python launcher_update.py --requirements
  python launcher_update.py --version
  python launcher_update.py --update-check --install-dir .
"""
    )
    parser.add_argument("--check", action="store_true", help="Check for available releases on GitHub")
    parser.add_argument("--requirements", action="store_true", help="Check installed Python packages against backend/requirements.txt")
    parser.add_argument("--version", action="store_true", help="Display current launcher version")
    parser.add_argument("--update-check", action="store_true", help="Perform silent update check and download")
    parser.add_argument("--relaunch", type=str, default=None, help="Script to relaunch after updating")
    parser.add_argument("--install-dir", type=str, default=None, help="Target installation directory")

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

    args, _ = parser.parse_known_args()

    if args.update_check:
        run_update_check(args.install_dir, args.relaunch)
        sys.exit(0)

    if args.version:
        print(f"Current Launcher Version: {get_current_version()}")

    if args.requirements:
        missing = find_unsatisfied_requirements()
        if missing:
            print(f"[Warning] Missing requirements ({len(missing)}):")
            for m in missing:
                print(f"  - {m}")
        else:
            print("[OK] All packages in backend/requirements.txt are installed.")

    if args.check:
        cur = get_current_version()
        print(f"Current version: {cur}")
        print("Checking GitHub releases for updates...")
        latest = get_latest_version()
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

