"""
Standalone bootstrap installer for PyMcLauncher.

Usage:
  Place this script in the directory where you want to install PyMcLauncher and run:
    python installer.py

Features:
- Fetches available download options from the latest release of cloudyhair6/PyMcLauncher.
- Presents a graphical checkbox menu to choose options (supports PyQt6, PyQt5, or native Tkinter fallback).
- Downloads all selected platform files with concurrent downloads and a progress bar.
- Combines updater state inside launcher_config.json (under "updater") so the launcher and auto-updater work seamlessly.
- Self-deletion: If this installer script itself is NOT listed in the selected download options,
  it automatically removes itself from disk after a successful installation.
"""
import base64
import concurrent.futures
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

OWNER = "cloudyhair6"
REPO = "PyMcLauncher"
USER_AGENT = "Python-Latest-Version-Detector"
CONFIG_FILE = "launcher_config.json"
LEGACY_UPDATER_CONFIG_FILE = "updater_config.json"
DEFAULT_OPTION = "Minecraft Launcher for PC"
DEFAULT_LAUNCHER_VERSION = "v3.1-pre03"


def _get_install_dir():
    """Return persistent target installation directory (works for both source script & frozen binary)."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        if sys.platform == "darwin":
            for parent in exe.parents:
                if parent.suffix == ".app":
                    return str(parent.parent)
        return str(exe.parent)
    return os.path.abspath(os.path.dirname(os.path.abspath(__file__)))


def _get_resource_path(rel_path):
    """Resolve resource path from install_dir or bundled PyInstaller _MEIPASS directory."""
    candidates = [_get_install_dir()]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(meipass)
    candidates.append(os.path.abspath(os.path.dirname(os.path.abspath(__file__))))
    for base in candidates:
        cand = os.path.join(base, rel_path.replace("/", os.sep))
        if os.path.exists(cand):
            return cand
    return os.path.join(candidates[0], rel_path.replace("/", os.sep))


# --- GitHub API & Network Helpers ---
_certs_ensured = False


def ensure_system_certificates():
    """
    Ensure SSL root certificates are installed and verified:
    - On macOS: runs 'Install Certificates.command' (official Python.org script) or installs
      and links certifi into OpenSSL's certificate path.
    - On Windows: ensures certifi is installed so Python's OpenSSL CA bundle is up to date.
    """
    global _certs_ensured
    if _certs_ensured:
        return
    _certs_ensured = True

    if getattr(sys, "frozen", False):
        return

    import platform
    sys_name = platform.system().lower()

    if sys_name == "darwin":
        print("[*] Checking macOS SSL certificates...", flush=True)
        py_ver = f"{sys.version_info.major}.{sys.version_info.minor}"
        candidates = [
            f"/Applications/Python {py_ver}/Install Certificates.command",
            os.path.join(os.path.dirname(sys.executable), "Install Certificates.command"),
            os.path.join(os.path.dirname(os.path.dirname(sys.executable)), "Install Certificates.command"),
        ]
        # Wildcard search /Applications/Python 3.*/
        try:
            for app_dir in Path("/Applications").glob("Python 3.*"):
                cmd_file = app_dir / "Install Certificates.command"
                if cmd_file.exists():
                    candidates.append(str(cmd_file))
        except Exception:
            pass

        executed = False
        for cand in candidates:
            if os.path.exists(cand):
                try:
                    print(f"[*] Running macOS certificate installer: {cand}...", flush=True)
                    res = subprocess.run(
                        ["/bin/sh", cand],
                        capture_output=True,
                        text=True,
                        timeout=45
                    )
                    if res.returncode == 0:
                        print("[OK] Successfully ran Install Certificates.command on macOS", flush=True)
                        executed = True
                        break
                    else:
                        print(f"[!] Certificate command warning (exit code {res.returncode}): {res.stderr.strip()}", file=sys.stderr, flush=True)
                except Exception as e:
                    print(f"[!] Could not run {cand}: {e}", file=sys.stderr, flush=True)

        # In addition, ensure certifi is installed for this specific python environment
        try:
            import certifi
        except ImportError:
            print("[*] Installing certifi package for SSL certificate verification...", flush=True)
            try:
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "--upgrade", "certifi", "--quiet"],
                    capture_output=True,
                    timeout=45
                )
            except Exception:
                pass

        # Link or copy certifi to OpenSSL's expected cafile if missing
        try:
            import certifi
            cafile = ssl.get_default_verify_paths().openssl_cafile
            if cafile and not os.path.exists(cafile):
                cadir = os.path.dirname(cafile)
                os.makedirs(cadir, exist_ok=True)
                try:
                    os.symlink(certifi.where(), cafile)
                    print(f"[OK] Linked OpenSSL cafile to certifi ({cafile})", flush=True)
                except Exception:
                    import shutil
                    shutil.copy2(certifi.where(), cafile)
                    print(f"[OK] Copied certifi certificates to {cafile}", flush=True)
        except Exception:
            pass

    elif sys_name == "windows":
        # On Windows, ensure certifi is installed for Python's OpenSSL CA bundle
        try:
            import certifi
        except ImportError:
            print("[*] Installing certifi for Windows SSL certificate verification...", flush=True)
            try:
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "certifi", "--quiet"],
                    capture_output=True,
                    timeout=45,
                    creationflags=creationflags
                )
                import certifi
                print(f"[OK] Installed certifi at {certifi.where()}", flush=True)
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


def _download_via_curl(url, headers=None):
    """Fallback download using system curl (uses native macOS / system certificates)."""
    try:
        import shutil
        curl_bin = shutil.which("curl")
        if not curl_bin:
            return None
        cmd = [curl_bin, "-sSL", "--connect-timeout", "10", "--max-time", "30"]
        if headers:
            for k, v in headers.items():
                cmd.extend(["-H", f"{k}: {v}"])
        cmd.append(url)
        res = subprocess.run(cmd, capture_output=True, timeout=35)
        if res.returncode == 0 and res.stdout:
            return res.stdout
    except Exception as e:
        print(f"[!] curl download error: {e}", file=sys.stderr, flush=True)
    return None


def download_raw_url(url, extra_headers=None, timeout=15):
    """
    Downloads raw content from a URL with automatic SSL certificate verification
    fallback (for macOS Python installs without certificates) and system curl fallback.
    """
    if not _certs_ensured:
        ensure_system_certificates()

    is_api = "api.github.com" in url
    # Only send GitHub Authorization token to api.github.com, never to raw.githubusercontent.com or web pages
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

        # 1. Try standard verified SSL context
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
                    continue  # Retry without token
                print(f"[!] HTTP error {e.code} fetching {url}: {e.reason}", file=sys.stderr, flush=True)
                break
            except Exception as e:
                last_error = str(e)
                if is_ssl_verify_error(e):
                    print(f"[*] SSL verification failed on macOS. Retrying with unverified context...", flush=True)
                else:
                    print(f"[!] Network error fetching {url}: {e}", file=sys.stderr, flush=True)

        # 2. Try unverified context fallback (crucial on macOS if 'Install Certificates.command' wasn't run)
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

    # 3. Fallback: Try system curl (native macOS / Linux tool with native OS keychain certs)
    curl_headers = {"User-Agent": USER_AGENT}
    if extra_headers:
        curl_headers.update(extra_headers)
    curl_data = _download_via_curl(url, headers=curl_headers)
    if curl_data:
        return curl_data

    if last_error:
        print(f"[Error] Failed to download {url}: {last_error}", file=sys.stderr, flush=True)
    return None


def _get_redirect_tag(url):
    """Extract release tag from GitHub's /releases/latest HTTP redirect."""
    # Method A: Try urllib with both verified and unverified SSL
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

    # Method B: Try curl -sIL to inspect Location header
    import shutil
    curl_bin = shutil.which("curl")
    if curl_bin:
        try:
            res = subprocess.run(
                [curl_bin, "-sIL", "--connect-timeout", "10", "--max-time", "15", "-H", f"User-Agent: {USER_AGENT}", url],
                capture_output=True,
                text=True,
                timeout=20,
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


def _is_ignore_versions_key(key):
    norm = re.sub(r"\s+", " ", str(key or "").strip().lower().replace("_", " "))
    return bool(re.match(r"^(?:old\s+)?ignor\w*\s+versions?$", norm))


def _is_settings_key(key, val=None):
    norm = re.sub(r"\s+", " ", str(key or "").strip().lower().replace("_", " "))
    if norm in ("updater settings", "updater config", "settings", "defaults", "config", "updater"):
        return isinstance(val, dict) and not any(k in val for k in ("win", "macos", "other", "Files"))
    return False


def _find_settings_block(download_options=None):
    src = download_options if isinstance(download_options, dict) and download_options else _load_local_download_options()
    if not isinstance(src, dict):
        return {}
    merged = {}
    for k, v in src.items():
        if _is_settings_key(k, v):
            merged.update(v)
    for k, v in src.items():
        if not isinstance(v, (dict, list)) or str(k).strip().lower() in ("channels", "build types", "release assets", "assets"):
            merged[k] = v
    return merged


def _lookup_setting(mapping, *names):
    if not isinstance(mapping, dict):
        return None
    wanted = {n.strip().lower().replace("_", " "): n for n in names}
    for k, v in mapping.items():
        norm = re.sub(r"\s+", " ", str(k).strip().lower().replace("_", " "))
        if norm in wanted:
            return v
    return None


def get_default_channel(download_options=None):
    settings = _find_settings_block(download_options)
    cfg_val = _lookup_setting(settings, "default channel", "channel")
    if cfg_val:
        val = str(cfg_val).strip().lower()
        if any(k in val for k in ("pre", "beta", "alpha", "rc", "nightly")):
            return "prerelease"
        return "stable"
    return "stable"


def extract_channels(download_options=None):
    settings = _find_settings_block(download_options)
    cfg_list = _lookup_setting(settings, "channels", "valid channels")
    if isinstance(cfg_list, list):
        cleaned = [normalize_channel(x, download_options=download_options) for x in cfg_list if str(x).strip()]
        dedup = list(dict.fromkeys(cleaned))
        if dedup:
            return dedup
    return ["stable", "prerelease"]


def extract_build_types(download_options=None, selected_opts=None):
    sources = []
    if isinstance(download_options, dict) and download_options:
        sources.append(download_options)
    local_opts = _load_local_download_options()
    if isinstance(local_opts, dict) and local_opts and local_opts is not download_options:
        sources.append(local_opts)

    found = []
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


def get_default_build_type(download_options=None):
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


def normalize_channel(channel, download_options=None):
    """Normalize update channel using download_options.json defaults when unspecified."""
    raw = channel if (channel is not None and str(channel).strip()) else get_default_channel(download_options)
    val = str(raw).strip().lower()
    if any(k in val for k in ("pre", "beta", "alpha", "rc", "nightly")):
        return "prerelease"
    return "stable"


def normalize_build_type(build_type, download_options=None):
    """Normalize build type against variants declared in download_options.json."""
    raw = build_type if (build_type is not None and str(build_type).strip()) else get_default_build_type(download_options)
    val = str(raw).strip()
    low = val.lower()
    avail = extract_build_types(download_options)
    for item in avail:
        if item.lower() == low:
            return item
    if low in ("compiled", "binary", "exe", "app", "release"):
        for item in avail:
            if "comp" in item.lower():
                return item
        return "compiled"
    if low in ("source", "src", "python"):
        for item in avail:
            if "source" in item.lower():
                return item
        return "source"
    return low


def normalize_version_tag(tag):
    """Normalize a tag string for case-insensitive comparison without leading 'v'."""
    s = str(tag or "").strip().lower()
    if s.startswith("v"):
        s = s[1:]
    return s


def _load_json_dict(path):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
    except Exception:
        pass
    return {}


def _load_local_download_options():
    """Load local download_options.json from install dir, bundled resources, or cwd as fallback."""
    candidates = [
        os.path.join(_get_install_dir(), "download_options.json"),
        _get_resource_path("download_options.json"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "download_options.json"),
        os.path.join(os.getcwd(), "download_options.json"),
    ]
    for path in candidates:
        data = _load_json_dict(path)
        if data:
            return data
    return {}


def get_ignored_old_versions(download_options=None):
    """Return blocked legacy version strings configured in download_options.json ('Old Ignore versions')."""
    sources = []
    if isinstance(download_options, dict) and download_options:
        sources.append(download_options)
    local_opts = _load_local_download_options()
    if isinstance(local_opts, dict) and local_opts and local_opts is not download_options:
        sources.append(local_opts)

    for src in sources:
        for key, val in src.items():
            if _is_ignore_versions_key(key) and isinstance(val, list):
                return [str(item).strip() for item in val if str(item).strip()]
    return []


def is_version_ignored(tag, ignored_versions=None, download_options=None):
    """Return True if tag matches any entry in 'Old Ignore versions' (with or without leading 'v')."""
    norm_tag = normalize_version_tag(tag)
    if not norm_tag:
        return True
    ignored = ignored_versions if ignored_versions is not None else get_ignored_old_versions(download_options)
    ignored_norm = {normalize_version_tag(v) for v in ignored if str(v).strip()}
    return norm_tag in ignored_norm


def _version_sort_key(v_str):
    """Build semver-aware comparison tuple where final release > prerelease on equal base numbers."""
    raw = normalize_version_tag(v_str)
    if not raw:
        return ((0,), 1, (0,), "")
    match = re.match(r"^([0-9]+(?:\.[0-9]+)*)(?:[-._]*(.*))?$", raw)
    if match:
        base_str, suffix = match.group(1), (match.group(2) or "").strip()
    else:
        nums = re.findall(r"\d+", raw)
        base_tuple = tuple(int(x) for x in nums) if nums else (0,)
        is_pre = bool(re.search(r"(pre|alpha|beta|rc|dev)", raw))
        return (base_tuple, 0 if is_pre else 1, base_tuple, raw)

    base_tuple = tuple(int(p) for p in base_str.split(".")) if base_str else (0,)
    if not suffix:
        return (base_tuple, 1, (0,), "")
    suffix_nums = tuple(int(x) for x in re.findall(r"\d+", suffix))
    is_pre = bool(re.search(r"(pre|alpha|a|beta|b|rc|dev)", suffix))
    return (base_tuple, 0 if is_pre else 1, suffix_nums, suffix)


def is_newer_version(current, latest):
    """Return True if latest is strictly newer than current (handling prereleases properly)."""
    if not current or not latest:
        return False
    return _version_sort_key(latest) > _version_sort_key(current)


def fetch_releases(include_prereleases=True, per_page=50):
    """Fetch published GitHub releases (non-draft), optionally filtering out prereleases."""
    url = f"https://api.github.com/repos/{OWNER}/{REPO}/releases?per_page={int(per_page)}"
    content = download_raw_url(url)
    if not content:
        return []
    try:
        data = json.loads(content.decode("utf-8"))
        if not isinstance(data, list):
            return []
        releases = []
        for rel in data:
            if not isinstance(rel, dict) or rel.get("draft"):
                continue
            if not include_prereleases and rel.get("prerelease"):
                continue
            if rel.get("tag_name"):
                releases.append(rel)
        return releases
    except Exception as e:
        print(f"[!] Error parsing releases list JSON: {e}", file=sys.stderr, flush=True)
        return []


def get_release_for_tag(tag, releases=None):
    """Fetch metadata & assets dict for a specific release tag."""
    if not tag:
        return None
    norm = normalize_version_tag(tag)
    if isinstance(releases, list):
        for rel in releases:
            if isinstance(rel, dict) and normalize_version_tag(rel.get("tag_name")) == norm:
                return rel

    candidates = [tag]
    stripped = str(tag).lstrip("vV")
    if stripped and stripped != tag:
        candidates.append(stripped)
    elif not str(tag).lower().startswith("v"):
        candidates.append(f"v{tag}")

    for cand in candidates:
        url = f"https://api.github.com/repos/{OWNER}/{REPO}/releases/tags/{urllib.parse.quote(cand)}"
        content = download_raw_url(url)
        if content:
            try:
                data = json.loads(content.decode("utf-8"))
                if isinstance(data, dict) and data.get("tag_name"):
                    return data
            except Exception:
                pass
    return None


def _looks_like_prerelease_tag(tag):
    return bool(re.search(r"[-._](pre|alpha|beta|rc|dev)\b", normalize_version_tag(tag)))


def get_latest_version(channel=None, download_options=None):
    """Resolve the latest non-ignored release tag for the chosen channel ('stable' or 'prerelease')."""
    eff_channel = normalize_channel(channel)
    print(f"[*] Checking latest {eff_channel} release for {OWNER}/{REPO} on GitHub...", flush=True)

    if eff_channel == "prerelease":
        releases = fetch_releases(include_prereleases=True, per_page=50)
        valid = [
            rel for rel in releases
            if not is_version_ignored(rel.get("tag_name"), download_options=download_options)
        ]
        if valid:
            valid.sort(key=lambda r: _version_sort_key(r.get("tag_name")), reverse=True)
            tag = valid[0]["tag_name"]
            print(f"[OK] Latest pre-release/release found via GitHub Releases API: {tag}", flush=True)
            return tag

    # 1. GitHub Releases /latest API (returns latest non-prerelease, non-draft)
    url = f"https://api.github.com/repos/{OWNER}/{REPO}/releases/latest"
    content = download_raw_url(url)
    if content:
        try:
            data = json.loads(content.decode("utf-8"))
            tag = data.get("tag_name")
            if tag and not is_version_ignored(tag, download_options=download_options):
                if eff_channel == "stable":
                    print(f"[OK] Latest version found via GitHub Releases API: {tag}", flush=True)
                    return tag
            if "message" in data:
                print(f"[!] GitHub API response: {data['message']}", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"[!] Error parsing release JSON: {e}", file=sys.stderr, flush=True)

    # 1b. Scan releases list for stable non-ignored if /latest failed or matched ignored
    if eff_channel == "stable":
        releases = fetch_releases(include_prereleases=False, per_page=50)
        valid = [
            rel for rel in releases
            if not is_version_ignored(rel.get("tag_name"), download_options=download_options)
        ]
        if valid:
            valid.sort(key=lambda r: _version_sort_key(r.get("tag_name")), reverse=True)
            tag = valid[0]["tag_name"]
            print(f"[OK] Latest stable version found via GitHub Releases list: {tag}", flush=True)
            return tag

    # 2. Web redirect fallback
    web_url = f"https://github.com/{OWNER}/{REPO}/releases/latest"
    print(f"[*] Attempting redirect inspection on {web_url}...", flush=True)
    tag = _get_redirect_tag(web_url)
    if tag and not is_version_ignored(tag, download_options=download_options):
        print(f"[OK] Latest version found via web redirect: {tag}", flush=True)
        return tag

    # 3. GitHub Tags API fallback
    tags_url = f"https://api.github.com/repos/{OWNER}/{REPO}/tags?per_page=50"
    content = download_raw_url(tags_url)
    if content:
        try:
            tags = json.loads(content.decode("utf-8"))
            if isinstance(tags, list):
                tag_names = [
                    t["name"] for t in tags
                    if isinstance(t, dict) and t.get("name")
                    and not is_version_ignored(t["name"], download_options=download_options)
                ]
                if eff_channel == "stable":
                    stable_tags = [t for t in tag_names if not _looks_like_prerelease_tag(t)]
                    if stable_tags:
                        tag_names = stable_tags
                if tag_names:
                    tag_names.sort(key=_version_sort_key, reverse=True)
                    print(f"[OK] Latest version found via GitHub Tags API: {tag_names[0]}", flush=True)
                    return tag_names[0]
        except Exception as e:
            print(f"[!] Error parsing tags JSON: {e}", file=sys.stderr, flush=True)

    print("[Error] Could not determine the latest version from any GitHub endpoint.", file=sys.stderr, flush=True)
    return None


def list_installable_versions(channel=None, download_options=None, include_prereleases=None):
    """Return sorted release dicts (newest first) excluding versions in 'Old Ignore versions'."""
    eff_channel = normalize_channel(channel)
    incl_pre = (eff_channel == "prerelease") if include_prereleases is None else bool(include_prereleases)
    releases = fetch_releases(include_prereleases=incl_pre, per_page=50)

    if not releases:
        # Tags endpoint fallback if Releases API is unavailable
        content = download_raw_url(f"https://api.github.com/repos/{OWNER}/{REPO}/tags?per_page=50")
        if content:
            try:
                tags = json.loads(content.decode("utf-8"))
                if isinstance(tags, list):
                    for t in tags:
                        if not isinstance(t, dict) or not t.get("name"):
                            continue
                        is_pre = _looks_like_prerelease_tag(t["name"])
                        if not incl_pre and is_pre:
                            continue
                        releases.append({
                            "tag_name": t["name"],
                            "name": t["name"],
                            "prerelease": is_pre,
                            "assets": [],
                        })
            except Exception:
                pass

    seen = set()
    filtered = []
    for rel in releases:
        tag = rel.get("tag_name")
        if not tag or is_version_ignored(tag, download_options=download_options):
            continue
        norm = normalize_version_tag(tag)
        if norm in seen:
            continue
        seen.add(norm)
        filtered.append(rel)

    filtered.sort(key=lambda r: _version_sort_key(r.get("tag_name")), reverse=True)
    return filtered


def fetch_download_options(tag, release_info=None):
    print(f"[*] Fetching download options for {tag}...", flush=True)
    # 1. Check if download_options.json is attached as a GitHub release asset on this tag
    if isinstance(release_info, dict):
        for asset in (release_info.get("assets") or []):
            if str(asset.get("name") or "").strip().lower() == "download_options.json":
                url = asset.get("browser_download_url") or asset.get("url")
                if url:
                    content = download_raw_url(url)
                    if content:
                        try:
                            opts = json.loads(content.decode("utf-8"))
                            print(f"[OK] Loaded download_options.json from release asset ({tag})", flush=True)
                            return opts
                        except Exception:
                            pass

    # 2. Check repository contents & raw GitHub URLs
    for ref in (tag, tag.lstrip("v") if tag.startswith("v") else f"v{tag}", "main"):
        for path in ("download_options.json", "updater/download_options.json"):
            api_url = f"https://api.github.com/repos/{OWNER}/{REPO}/contents/{path}?ref={ref}"
            content = download_raw_url(api_url, extra_headers={"Accept": "application/vnd.github.v3.raw"})
            if content:
                try:
                    opts = json.loads(content.decode("utf-8"))
                    print(f"[OK] Successfully fetched {path} ({ref})", flush=True)
                    return opts
                except Exception:
                    pass

            raw_url = f"https://raw.githubusercontent.com/{OWNER}/{REPO}/{ref}/{path}"
            content = download_raw_url(raw_url)
            if content:
                try:
                    opts = json.loads(content.decode("utf-8"))
                    print(f"[OK] Successfully fetched {path} from raw URL ({ref})", flush=True)
                    return opts
                except Exception:
                    pass

    # 3. Last resort: check release assets if release_info was not passed
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
                                opts = json.loads(content.decode("utf-8"))
                                print(f"[OK] Loaded download_options.json from release asset ({tag})", flush=True)
                                return opts
                            except Exception:
                                pass

    print(f"[Error] Could not find download_options.json for tag {tag}", file=sys.stderr, flush=True)
    return None


def get_repo_tree(tag):
    url = f"https://api.github.com/repos/{OWNER}/{REPO}/git/trees/{tag}?recursive=1"
    content = download_raw_url(url)
    if content:
        try:
            return json.loads(content.decode("utf-8")).get("tree", [])
        except Exception:
            pass
    return []


def download_file_content(path, tag):
    for ref in (tag, tag.lstrip("v") if tag.startswith("v") else f"v{tag}", "main"):
        api_url = f"https://api.github.com/repos/{OWNER}/{REPO}/contents/{urllib.parse.quote(path)}?ref={ref}"
        content = download_raw_url(api_url, extra_headers={"Accept": "application/vnd.github.v3.raw"})
        if content:
            return content

    for ref in (tag, tag.lstrip("v") if tag.startswith("v") else f"v{tag}"):
        raw_url = f"https://raw.githubusercontent.com/{OWNER}/{REPO}/{ref}/{urllib.parse.quote(path)}"
        content = download_raw_url(raw_url)
        if content:
            return content
    return None


def get_platform_key():
    """Return normalized platform key used by download_options.json ('win', 'macos', or 'other')."""
    if sys.platform == "win32" or os.name == "nt":
        return "win"
    if sys.platform == "darwin":
        return "macos"
    return "other"


def extract_download_option_names(download_options):
    """Return selectable product option names, skipping metadata keys like 'Old Ignore versions' & settings."""
    if not isinstance(download_options, dict):
        return []
    names = []
    has_default = DEFAULT_OPTION in download_options and isinstance(download_options.get(DEFAULT_OPTION), dict)
    for key, val in download_options.items():
        if _is_ignore_versions_key(key) or _is_settings_key(key, val):
            continue
        if not isinstance(val, (dict, list)):
            continue
        low = str(key).strip().lower()
        if low in ("compiled", "source") and has_default and isinstance(val, dict) and any(k in val for k in ("win", "macos", "other")):
            continue
        names.append(key)
    return names


def _parse_download_source_prefs(mapping):
    """Extract optional 'Download From' mode and explicit 'Release Assets' list from a config dict."""
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

    explicit_assets = []
    raw_assets = _lookup_setting(mapping, "release assets", "assets", "release archive", "archive", "asset")
    if isinstance(raw_assets, str) and raw_assets.strip():
        explicit_assets.append(raw_assets.strip())
    elif isinstance(raw_assets, list):
        explicit_assets.extend(str(a).strip() for a in raw_assets if str(a).strip())
    return mode, explicit_assets


def resolve_option_platform_data(opt_data, platform_key=None, build_type=None, all_options=None):
    """Resolve Files, Update Ignore Files, Message, Download From & Release Assets dynamically."""
    plat = platform_key or get_platform_key()
    eff_build = normalize_build_type(build_type, download_options=all_options)
    empty = {"Files": [], "Update Ignore Files": [], "Message": None, "Download From": None, "Release Assets": []}
    if isinstance(opt_data, list):
        return {"Files": list(opt_data), "Update Ignore Files": [], "Message": None, "Download From": None, "Release Assets": []}
    if not isinstance(opt_data, dict):
        return empty

    selected_layer = opt_data
    direct_plat_keys = {"win", "macos", "other"}
    variant_keys = [
        k for k, v in opt_data.items()
        if isinstance(v, dict) and str(k).strip().lower() not in direct_plat_keys
    ]
    has_build_layer = bool(variant_keys)
    is_legacy_flat = not has_build_layer and any(k in opt_data for k in direct_plat_keys)

    if has_build_layer:
        matched_key = next((k for k in variant_keys if k.lower() == eff_build.lower()), None)
        if matched_key and isinstance(opt_data.get(matched_key), dict):
            selected_layer = opt_data[matched_key]
        elif eff_build == "compiled" and isinstance(all_options, dict) and isinstance(all_options.get("compiled"), dict):
            selected_layer = all_options["compiled"]
        elif isinstance(opt_data.get("source"), dict):
            selected_layer = opt_data["source"]
        else:
            selected_layer = opt_data[variant_keys[0]]
    elif eff_build == "compiled":
        if isinstance(all_options, dict) and isinstance(all_options.get("compiled"), dict):
            selected_layer = all_options["compiled"]
        elif is_legacy_flat:
            local_opts = _load_local_download_options()
            local_default = local_opts.get(DEFAULT_OPTION) if isinstance(local_opts, dict) else None
            if isinstance(local_default, dict) and isinstance(local_default.get("compiled"), dict):
                selected_layer = local_default["compiled"]
            elif isinstance(local_opts, dict) and isinstance(local_opts.get("compiled"), dict):
                selected_layer = local_opts["compiled"]

    target = selected_layer
    if any(k in selected_layer for k in direct_plat_keys):
        target = selected_layer.get(plat)
        if not isinstance(target, dict):
            for fallback_k in ("other", "macos", "win"):
                if isinstance(selected_layer.get(fallback_k), dict):
                    target = selected_layer[fallback_k]
                    break
        if not isinstance(target, dict):
            return empty

    # Layer Download From & Release Assets: root/settings -> option -> variant -> platform
    dl_from = None
    rel_assets = []
    for layer in (_find_settings_block(all_options), opt_data, selected_layer, target):
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


def collect_options_plan_details(download_options, selected_opts=None, platform_key=None, build_type=None):
    """Collect Files, Update Ignore Files, Messages, Download From mode & Release Assets for selected options."""
    eff_build = normalize_build_type(build_type, download_options=download_options)
    empty = {
        "paths": [],
        "ignore_files": [],
        "messages": [],
        "download_from": None,
        "release_assets": [],
        "build_type": eff_build,
    }
    if not isinstance(download_options, dict) or not download_options:
        return empty

    valid_names = extract_download_option_names(download_options)
    if not valid_names:
        return empty

    chosen = [opt for opt in (selected_opts or []) if opt in valid_names]
    if not chosen:
        chosen = [DEFAULT_OPTION] if DEFAULT_OPTION in valid_names else [valid_names[0]]

    paths = []
    ignore_files = []
    messages = []
    release_assets = []
    download_from = None
    for opt in chosen:
        resolved = resolve_option_platform_data(
            download_options[opt],
            platform_key=platform_key,
            build_type=eff_build,
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
        "build_type": eff_build,
    }


def collect_options_plan(download_options, selected_opts=None, platform_key=None, build_type=None):
    """Collect deduplicated Files, Update Ignore Files, and Messages for selected options."""
    details = collect_options_plan_details(
        download_options,
        selected_opts=selected_opts,
        platform_key=platform_key,
        build_type=build_type,
    )
    return details["paths"], details["ignore_files"], details["messages"]


def load_updater_state(install_dir):
    """Load updater state from launcher_config.json['updater'] with legacy updater_config.json migration."""
    cfg_path = os.path.join(install_dir, CONFIG_FILE)
    legacy_path = os.path.join(install_dir, LEGACY_UPDATER_CONFIG_FILE)

    launcher_cfg = _load_json_dict(cfg_path)
    updater = launcher_cfg.get("updater")
    if isinstance(updater, dict) and updater:
        state = dict(updater)
        state["channel"] = normalize_channel(state.get("channel"))
        state["build_type"] = normalize_build_type(state.get("build_type"))
        return state

    # Fallback to root keys if manually merged, or legacy updater_config.json
    if any(k in launcher_cfg for k in ("current_version", "selected_options", "install_dir")):
        return {
            "current_version": launcher_cfg.get("current_version"),
            "selected_options": launcher_cfg.get("selected_options", [DEFAULT_OPTION]),
            "install_dir": launcher_cfg.get("install_dir", install_dir),
            "channel": normalize_channel(launcher_cfg.get("channel")),
            "build_type": normalize_build_type(launcher_cfg.get("build_type")),
        }

    legacy_cfg = _load_json_dict(legacy_path)
    if legacy_cfg:
        save_updater_state(
            install_dir,
            legacy_cfg.get("current_version", DEFAULT_LAUNCHER_VERSION),
            legacy_cfg.get("selected_options", [DEFAULT_OPTION]),
            channel=legacy_cfg.get("channel"),
            build_type=legacy_cfg.get("build_type"),
        )
        legacy_cfg["channel"] = normalize_channel(legacy_cfg.get("channel"))
        legacy_cfg["build_type"] = normalize_build_type(legacy_cfg.get("build_type"))
        return legacy_cfg
    return {}


def save_updater_state(install_dir, current_version, selected_options, channel=None, build_type=None):
    """Save updater state under launcher_config.json['updater'] without clobbering launcher settings."""
    os.makedirs(install_dir, exist_ok=True)
    cfg_path = os.path.join(install_dir, CONFIG_FILE)
    legacy_path = os.path.join(install_dir, LEGACY_UPDATER_CONFIG_FILE)

    launcher_cfg = _load_json_dict(cfg_path)
    prev_updater = launcher_cfg.get("updater") if isinstance(launcher_cfg.get("updater"), dict) else {}
    eff_channel = normalize_channel(channel if channel is not None else prev_updater.get("channel"))
    eff_build_type = normalize_build_type(build_type if build_type is not None else prev_updater.get("build_type"))

    launcher_cfg["updater"] = {
        "current_version": current_version,
        "selected_options": list(selected_options),
        "install_dir": install_dir,
        "channel": eff_channel,
        "build_type": eff_build_type,
    }
    tmp_path = cfg_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(launcher_cfg, f, indent=4)
        os.replace(tmp_path, cfg_path)
    except Exception:
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(launcher_cfg, f, indent=4)
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

    if os.path.exists(legacy_path):
        try:
            os.remove(legacy_path)
        except Exception:
            pass


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


def _is_ignored_rel_path(rel_path, ignore_set):
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
    build_type=None,
    platform_key=None,
):
    """Match configured Files / dirs / explicit Release Assets against GitHub release assets (including .zip bundles)."""
    import fnmatch

    plat = platform_key or get_platform_key()
    bt_low = str(build_type or "").strip().lower()
    rel_list = release_assets if isinstance(release_assets, list) else []
    ignore_set = {Path(ig).as_posix().strip("/").lower() for ig in (ignore_files or [])}
    matched = []
    seen_urls = set()

    def _add_asset(name, url, size=None, target_path="", extract_zip=False, extract_prefix="", matched_targets=None, full_bundle=False):
        if not url or url in seen_urls:
            return
        seen_urls.add(url)
        matched.append({
            "name": name,
            "url": url,
            "browser_download_url": url,
            "size": size,
            "target_path": target_path or name,
            "target_rel": target_path or name,
            "extract_zip": bool(extract_zip),
            "extract_prefix": extract_prefix,
            "matched_paths": list(matched_targets or []),
            "full_bundle": bool(full_bundle),
        })

    # 1. Explicit Release Assets / Archive config entries
    for exp in (explicit_assets or []):
        exp_s = str(exp or "").strip()
        if not exp_s:
            continue
        if exp_s.startswith(("http://", "https://")):
            bname = os.path.basename(urllib.parse.urlparse(exp_s).path) or "release_asset"
            _add_asset(
                bname,
                exp_s,
                target_path=bname,
                extract_zip=bname.lower().endswith(".zip"),
                matched_targets=list(paths or []),
                full_bundle=bname.lower().endswith(".zip"),
            )
            continue

        exp_low = exp_s.lower()
        for asset in rel_list:
            if not isinstance(asset, dict):
                continue
            name = str(asset.get("name") or "").strip()
            url = str(asset.get("browser_download_url") or asset.get("url") or "").strip()
            if not name or not url or _is_ignored_rel_path(name, ignore_set):
                continue
            name_low = name.lower()
            if name_low == exp_low or name_low == f"{exp_low}.zip" or fnmatch.fnmatch(name_low, exp_low):
                _add_asset(
                    name,
                    url,
                    size=asset.get("size"),
                    target_path=name,
                    extract_zip=name_low.endswith(".zip"),
                    matched_targets=list(paths or []),
                    full_bundle=name_low.endswith(".zip"),
                )

    if not paths:
        return matched

    unmatched_paths = []
    for target in paths:
        raw_target = str(target).strip()
        if raw_target.startswith(("http://", "https://")):
            bname = os.path.basename(urllib.parse.urlparse(raw_target).path) or "asset"
            _add_asset(bname, raw_target, target_path=bname, extract_zip=bname.lower().endswith(".zip"), matched_targets=[raw_target])
            continue

        norm_target = Path(raw_target).as_posix().strip()
        is_dir = raw_target.endswith("/") or norm_target.endswith("/")
        clean_target = norm_target.rstrip("/")
        if not clean_target or _is_ignored_rel_path(clean_target, ignore_set):
            continue
        target_base = os.path.basename(clean_target)
        clean_low = clean_target.lower()
        base_low = target_base.lower()
        stem_low = os.path.splitext(base_low)[0]

        found_for_target = False
        for asset in rel_list:
            if not isinstance(asset, dict):
                continue
            name = str(asset.get("name") or "").strip()
            url = str(asset.get("browser_download_url") or asset.get("url") or "").strip()
            if not name or not url:
                continue
            name_low = name.lower()
            if _is_ignored_rel_path(name_low, ignore_set):
                continue

            if is_dir:
                if name_low in (f"{clean_low}.zip", f"{base_low}.zip"):
                    _add_asset(
                        name,
                        url,
                        size=asset.get("size"),
                        target_path=clean_target,
                        extract_zip=True,
                        extract_prefix=clean_target,
                        matched_targets=[raw_target],
                    )
                    found_for_target = True
                elif name_low.startswith(clean_low + "/"):
                    _add_asset(
                        name,
                        url,
                        size=asset.get("size"),
                        target_path=name,
                        extract_zip=False,
                        matched_targets=[raw_target],
                    )
                    found_for_target = True
            else:
                if name_low in (clean_low, base_low):
                    _add_asset(
                        name,
                        url,
                        size=asset.get("size"),
                        target_path=clean_target,
                        extract_zip=name_low.endswith(".zip"),
                        matched_targets=[raw_target],
                    )
                    found_for_target = True
                elif name_low.endswith(".zip") and name_low in (f"{clean_low}.zip", f"{base_low}.zip", f"{stem_low}.zip"):
                    _add_asset(
                        name,
                        url,
                        size=asset.get("size"),
                        target_path=clean_target,
                        extract_zip=True,
                        matched_targets=[raw_target],
                    )
                    found_for_target = True

        if not found_for_target:
            unmatched_paths.append(raw_target)

    # 3. Variant / platform archive bundle fallback if unmatched paths remain (e.g. source.zip, PyMcLauncher-win.zip)
    if unmatched_paths and rel_list:
        bundle_names = set()
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
            name = str(asset.get("name") or "").strip()
            url = str(asset.get("browser_download_url") or asset.get("url") or "").strip()
            if not name or not url:
                continue
            if name.lower() in bundle_names:
                _add_asset(
                    name,
                    url,
                    size=asset.get("size"),
                    target_path=name,
                    extract_zip=True,
                    matched_targets=list(unmatched_paths),
                    full_bundle=True,
                )
    return matched


def _write_bytes_atomic(local_fp, data):
    parent = os.path.dirname(os.path.abspath(local_fp))
    os.makedirs(parent, exist_ok=True)
    tmp_fp = local_fp + ".tmp"
    with open(tmp_fp, "wb") as f:
        f.write(data)
    os.replace(tmp_fp, local_fp)


def _extract_zip_bytes(zip_bytes, install_dir, ignore_files=None, extract_prefix=""):
    import io
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

        for info in infos:
            member_rel = Path(info.filename).as_posix().lstrip("./")
            if not member_rel:
                continue
            eff_rel = f"{clean_prefix}/{member_rel}" if needs_prefix else member_rel
            if _is_ignored_rel_path(eff_rel, ignore_set):
                continue
            dest_path = os.path.realpath(os.path.join(base_real, eff_rel.replace("/", os.sep)))
            if dest_path != base_real and not dest_path.startswith(base_real + os.sep):
                continue
            if info.is_dir():
                os.makedirs(dest_path, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            with zf.open(info, "r") as src:
                _write_bytes_atomic(dest_path, src.read())
            if os.name != "nt":
                perm = (info.external_attr >> 16) & 0o777
                if perm:
                    try:
                        os.chmod(dest_path, perm)
                    except Exception:
                        pass
                elif ".app/contents/macos/" in eff_rel.lower() or eff_rel.endswith((".command", ".sh", ".py")):
                    try:
                        os.chmod(dest_path, 0o755)
                    except Exception:
                        pass


def perform_download_concurrently(files, tag, install_dir, progress_cb=None, assets=None, ignore_files=None):
    os.makedirs(install_dir, exist_ok=True)
    file_list = list(files or [])
    asset_list = list(assets or [])
    total = len(file_list) + len(asset_list)
    completed = 0
    lock = threading.Lock()
    errors = []

    def task_file(fp):
        nonlocal completed
        try:
            local_fp = os.path.join(install_dir, fp.replace("/", os.sep))
            data = download_file_content(fp, tag)
            if data is not None:
                _write_bytes_atomic(local_fp, data)
                if os.name != "nt" and (fp.endswith(".command") or fp.endswith(".sh") or fp.endswith(".py")):
                    try:
                        os.chmod(local_fp, 0o755)
                    except Exception:
                        pass
            else:
                with lock:
                    errors.append(f"Empty/missing file: {fp}")
        except Exception as e:
            print(f"Error downloading {fp}: {e}")
            with lock:
                errors.append(f"{fp}: {e}")
        finally:
            with lock:
                completed += 1
                if progress_cb:
                    progress_cb(completed, total, fp)

    def task_asset(asset):
        nonlocal completed
        name = asset.get("name") or asset.get("target_path") or "release_asset"
        url = asset.get("url") or asset.get("browser_download_url")
        try:
            data = download_raw_url(url, extra_headers={"Accept": "application/octet-stream"}, timeout=120)
            if not data:
                raise RuntimeError(f"Failed to download release asset '{name}'")
            if asset.get("extract_zip"):
                _extract_zip_bytes(
                    data,
                    install_dir,
                    ignore_files=ignore_files,
                    extract_prefix=asset.get("extract_prefix", ""),
                )
            else:
                dest_fp = os.path.join(
                    install_dir,
                    (asset.get("target_rel") or asset.get("target_path") or name).replace("/", os.sep),
                )
                _write_bytes_atomic(dest_fp, data)
                if os.name != "nt" and not name.lower().endswith((".txt", ".json", ".md")):
                    try:
                        os.chmod(dest_fp, 0o755)
                    except Exception:
                        pass
        except Exception as e:
            print(f"Error downloading release asset {name}: {e}", file=sys.stderr, flush=True)
            with lock:
                errors.append(f"Asset {name}: {e}")
        finally:
            with lock:
                completed += 1
                if progress_cb:
                    progress_cb(completed, total, name)

    if total == 0:
        return

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(task_file, f) for f in file_list]
        futures.extend(executor.submit(task_asset, a) for a in asset_list)
        concurrent.futures.wait(futures)

    if asset_list and any(err.startswith("Asset ") for err in errors):
        raise RuntimeError("; ".join(errors))


def _get_self_target_path():
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        if sys.platform == "darwin":
            for parent in exe.parents:
                if parent.suffix == ".app":
                    return str(parent)
        return str(exe)
    return os.path.abspath(__file__)


def schedule_self_deletion():
    """Schedules self-deletion after process exit using detached background process."""
    try:
        cur_file = _get_self_target_path()
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            subprocess.Popen(
                f'cmd.exe /c timeout /t 2 >nul & del /f /q "{cur_file}"',
                shell=True,
                creationflags=flags,
            )
        else:
            rm_flag = "-rf" if os.path.isdir(cur_file) else "-f"
            try:
                subprocess.Popen(
                    f'sleep 1 && rm {rm_flag} "{cur_file}"',
                    shell=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except Exception:
                try:
                    if os.path.isdir(cur_file):
                        shutil.rmtree(cur_file, ignore_errors=True)
                    else:
                        os.remove(cur_file)
                except Exception:
                    pass
    except Exception as e:
        print(f"Self-deletion error: {e}")


# --- GUI Implementation (PyQt6 -> PyQt5 -> Tkinter Fallback) ---
def run_installer_gui():
    ensure_system_certificates()
    # Try importing PyQt6 first, then PyQt5
    QtModule = None
    try:
        from PyQt6 import QtCore, QtWidgets
        QtModule = (QtCore, QtWidgets, 6)
    except ImportError:
        try:
            from PyQt5 import QtCore, QtWidgets
            QtModule = (QtCore, QtWidgets, 5)
        except ImportError:
            QtModule = None

    if QtModule:
        _run_qt_gui(QtModule)
    else:
        _run_tkinter_gui()


def _execute_install_plan(
    tag,
    install_dir,
    selected_opts=None,
    channel=None,
    build_type=None,
    download_options=None,
    progress_cb=None,
    release_info=None,
):
    """Execute download & installation (from Release Assets and/or Repo Tree) for a given release tag."""
    rel = release_info
    opts = download_options if isinstance(download_options, dict) and download_options else fetch_download_options(tag, release_info=rel)
    if not opts:
        opts = _load_local_download_options()
    if not opts:
        raise RuntimeError(f"download_options.json was not found for release {tag}.")

    eff_channel = normalize_channel(channel, download_options=opts)
    eff_build = normalize_build_type(build_type, download_options=opts)

    if is_version_ignored(tag, download_options=opts):
        raise RuntimeError(f"Version '{tag}' is listed in Old Ignore versions and cannot be installed.")

    valid_names = extract_download_option_names(opts)
    chosen_opts = [opt for opt in (selected_opts or []) if opt in valid_names]
    if not chosen_opts and valid_names:
        chosen_opts = [DEFAULT_OPTION] if DEFAULT_OPTION in valid_names else [valid_names[0]]

    plan = collect_options_plan_details(opts, chosen_opts, build_type=eff_build)
    paths = plan["paths"]
    ignore_files = plan["ignore_files"]
    messages = plan["messages"]
    configured_from = plan["download_from"]
    explicit_assets = plan["release_assets"]
    if not paths and not explicit_assets:
        raise RuntimeError(f"No files or release assets configured for build type '{eff_build}' on {get_platform_key()}.")

    is_compiled_like = ("comp" in eff_build.lower()) or ("bin" in eff_build.lower())
    has_urls = any(str(p).strip().startswith(("http://", "https://")) for p in paths)
    check_releases = bool(
        configured_from in ("releases", "auto")
        or explicit_assets
        or has_urls
        or (configured_from != "repo" and is_compiled_like)
    )

    assets = []
    files = []
    if check_releases:
        if progress_cb:
            progress_cb(0, 1, f"Checking GitHub release assets for {tag}...")
        if rel is None:
            rel = get_release_for_tag(tag)
        assets = resolve_release_assets(
            paths,
            (rel or {}).get("assets", []),
            ignore_files=ignore_files,
            explicit_assets=explicit_assets,
            build_type=eff_build,
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
        # Compiled builds may mix release assets with repo files only if nothing came from releases or non-asset paths exist
        need_repo_tree = bool(unmatched_paths) and not assets
    else:
        need_repo_tree = bool(unmatched_paths)

    if need_repo_tree:
        if progress_cb:
            progress_cb(0, 1, f"Fetching repository file tree for {tag}...")
        tree = get_repo_tree(tag)
        files = resolve_files_to_download(unmatched_paths, tree, ignore_files=ignore_files)

    if not assets and not files:
        mode_hint = f"download_from='{configured_from}'" if configured_from else f"build_type='{eff_build}'"
        raise RuntimeError(
            f"No downloadable files or release assets ({', '.join(explicit_assets or paths)}) found on {tag} ({mode_hint})."
        )

    perform_download_concurrently(files, tag, install_dir, progress_cb=progress_cb, assets=assets, ignore_files=ignore_files)
    save_updater_state(install_dir, tag, chosen_opts, channel=eff_channel, build_type=eff_build)
    return {
        "tag": tag,
        "paths": list(paths),
        "files": list(files),
        "assets": list(assets),
        "messages": list(messages),
        "channel": eff_channel,
        "build_type": eff_build,
    }


def _relaunch_installed_app(install_dir, relaunch_script=None, build_type=None, downloaded_assets=None):
    """Launch installed application (compiled binary or setup script / launcher.py)."""
    eff_build = normalize_build_type(build_type)
    if eff_build == "compiled":
        candidates = []
        for asset in (downloaded_assets or []):
            tp = asset.get("target_path") or asset.get("name")
            if tp:
                candidates.append(tp)
        plat = get_platform_key()
        if plat == "win":
            candidates.append("PyMcLauncher.exe")
        elif plat == "macos":
            candidates.append("PyMcLauncher.app")
        else:
            candidates.extend(["PyMcLauncher", "PyMcLauncher.AppImage"])

        for rel_name in candidates:
            full = os.path.join(install_dir, rel_name)
            if os.path.exists(full):
                if os.name == "nt":
                    subprocess.Popen([full], cwd=install_dir, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
                elif sys.platform == "darwin" and full.endswith(".app"):
                    subprocess.Popen(["open", full], cwd=install_dir, start_new_session=True)
                else:
                    subprocess.Popen([full], cwd=install_dir, start_new_session=True)
                return True

    target_relaunch = relaunch_script
    if not target_relaunch:
        plat = get_platform_key()
        if plat == "win":
            target_relaunch = "setup-start.bat"
        elif plat == "macos":
            target_relaunch = "setup-start.command"
        else:
            target_relaunch = "setup-start.sh"

    if target_relaunch:
        candidate_dirs = [install_dir, os.path.dirname(install_dir), os.getcwd()]
        for cdir in candidate_dirs:
            sp = os.path.join(cdir, target_relaunch)
            if os.path.exists(sp):
                if os.name != "nt" and (sp.endswith(".command") or sp.endswith(".sh")):
                    try:
                        os.chmod(sp, 0o755)
                    except Exception:
                        pass
                if os.name == "nt":
                    subprocess.Popen(["cmd.exe", "/c", sp], cwd=cdir, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
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


def _format_release_label(rel, is_latest=False, current_version=None):
    tag = rel.get("tag_name") or ""
    badges = ["Pre-release" if rel.get("prerelease") else "Stable"]
    if is_latest:
        badges.append("Latest")
    if current_version:
        if normalize_version_tag(tag) == normalize_version_tag(current_version):
            badges.append("Installed")
        elif is_newer_version(tag, current_version):
            badges.append("Downgrade")
        elif is_newer_version(current_version, tag):
            badges.append("Upgrade")
    return f"{tag} [{', '.join(badges)}]"


def _fetch_channel_metadata(channel, preferred_tag=None):
    """Load installable non-ignored releases and download_options for the target release."""
    eff_channel = normalize_channel(channel)
    releases = list_installable_versions(channel=eff_channel)
    if not releases:
        latest_ver = get_latest_version(channel=eff_channel)
        if not latest_ver:
            return None
        releases = [{
            "tag_name": latest_ver,
            "name": latest_ver,
            "prerelease": (eff_channel == "prerelease"),
            "assets": [],
        }]

    target_rel = releases[0]
    target_tag = target_rel["tag_name"]
    if preferred_tag:
        pref_norm = normalize_version_tag(preferred_tag)
        for rel in releases:
            if normalize_version_tag(rel.get("tag_name")) == pref_norm:
                target_rel = rel
                target_tag = rel["tag_name"]
                break

    opts = fetch_download_options(target_tag, release_info=target_rel) or _load_local_download_options()
    if not opts:
        return {"releases": releases, "target_tag": target_tag, "options": None}
    return {"releases": releases, "target_tag": target_tag, "options": opts}


def _run_qt_gui(QtModule):
    QtCore, QtWidgets, qt_ver = QtModule
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("cloudyhair6.pymclauncher.installer")
        except Exception:
            pass
    app = QtWidgets.QApplication(sys.argv)
    try:
        app.setApplicationName("PyMcLauncher Installer")
        app.setApplicationDisplayName("PyMcLauncher Installer")
        app.setOrganizationName(OWNER)
        app.setApplicationVersion(DEFAULT_LAUNCHER_VERSION)
    except Exception:
        pass

    install_dir = _get_install_dir()
    saved_state = load_updater_state(install_dir)
    current_installed_ver = saved_state.get("current_version")
    init_channel = normalize_channel(saved_state.get("channel"))
    init_build_type = normalize_build_type(saved_state.get("build_type"))

    try:
        if qt_ver == 6:
            from PyQt6.QtGui import QIcon
        else:
            from PyQt5.QtGui import QIcon
        inst_icon = QIcon()
        icon_svg = _get_resource_path(os.path.join("images", "icon", "PyMcLauncher.svg"))
        icon_ico = _get_resource_path(os.path.join("images", "icon", "PyMcLauncher.ico"))
        if os.path.exists(icon_svg):
            inst_icon.addFile(icon_svg)
        if os.path.exists(icon_ico):
            inst_icon.addFile(icon_ico)
        if not inst_icon.isNull():
            app.setWindowIcon(inst_icon)
    except Exception:
        pass

    splash = QtWidgets.QProgressDialog("Checking GitHub releases and options...", None, 0, 0)
    splash.setWindowTitle("PyMcLauncher Installer")
    splash.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal if qt_ver == 6 else QtCore.Qt.ApplicationModal)
    splash.show()
    app.processEvents()

    meta = _fetch_channel_metadata(init_channel)
    splash.close()

    if not meta:
        err_msg = (
            "Could not fetch release information from GitHub.\n\n"
            "Please check your internet connection or run: '/Applications/Python 3.x/Install Certificates.command' on macOS."
        )
        print(f"[Error] {err_msg}", file=sys.stderr, flush=True)
        QtWidgets.QMessageBox.critical(None, "Connection Error", err_msg)
        return

    if not meta.get("options"):
        QtWidgets.QMessageBox.critical(
            None, "Error", f"download_options.json was not found in release {meta['target_tag']}."
        )
        return

    state = {
        "releases": meta["releases"],
        "target_tag": meta["target_tag"],
        "options": meta["options"],
    }

    win = QtWidgets.QWidget()
    win.setWindowTitle("PyMcLauncher Installer & Setup")
    win.resize(600, 520)
    layout = QtWidgets.QVBoxLayout(win)
    layout.setContentsMargins(20, 20, 20, 20)
    layout.setSpacing(10)

    title_lbl = QtWidgets.QLabel(f"PyMcLauncher Installation ({state['target_tag']})")
    title_lbl.setStyleSheet("font-size: 16px; font-weight: bold;")
    layout.addWidget(title_lbl)

    if current_installed_ver:
        installed_lbl = QtWidgets.QLabel(f"Detected installed version: {current_installed_ver}")
        installed_lbl.setStyleSheet("color: #666; font-size: 12px;")
        layout.addWidget(installed_lbl)

    cfg_form = QtWidgets.QFormLayout()
    cfg_form.setSpacing(8)

    channel_combo = QtWidgets.QComboBox()
    for ch_val in extract_channels(state["options"]):
        lbl = "Pre-releases (includes pre-release builds)" if ch_val == "prerelease" else "Stable Releases"
        channel_combo.addItem(lbl, ch_val)
    idx_ch = channel_combo.findData(init_channel)
    if idx_ch >= 0:
        channel_combo.setCurrentIndex(idx_ch)
    cfg_form.addRow("Release Channel:", channel_combo)

    build_combo = QtWidgets.QComboBox()

    def populate_build_combo(opts, select_bt=None):
        cur_bt = select_bt if select_bt is not None else build_combo.currentData()
        build_combo.blockSignals(True)
        build_combo.clear()
        for bt in extract_build_types(opts):
            low = bt.lower()
            if low == "source":
                lbl = "Source Code (source)"
            elif low == "compiled":
                lbl = "Compiled Application (compiled)"
            else:
                lbl = f"{bt.replace('_', ' ').title()} ({bt})"
            build_combo.addItem(lbl, bt)
        if cur_bt:
            idx_b = build_combo.findData(cur_bt)
            if idx_b >= 0:
                build_combo.setCurrentIndex(idx_b)
        build_combo.blockSignals(False)

    populate_build_combo(state["options"], select_bt=init_build_type)
    cfg_form.addRow("Build Type:", build_combo)

    ver_row = QtWidgets.QHBoxLayout()
    ver_combo = QtWidgets.QComboBox()
    refresh_btn = QtWidgets.QPushButton("Refresh")
    ver_row.addWidget(ver_combo, stretch=1)
    ver_row.addWidget(refresh_btn)
    cfg_form.addRow("Version (Upgrade / Downgrade):", ver_row)

    layout.addLayout(cfg_form)

    info_lbl = QtWidgets.QLabel("Select components to download and install:")
    layout.addWidget(info_lbl)

    scroll = QtWidgets.QScrollArea()
    scroll.setWidgetResizable(True)
    scroll_content = QtWidgets.QWidget()
    scroll_layout = QtWidgets.QVBoxLayout(scroll_content)
    scroll.setWidget(scroll_content)
    layout.addWidget(scroll, stretch=1)

    checkboxes = {}

    def rebuild_checkboxes():
        while scroll_layout.count():
            item = scroll_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        checkboxes.clear()
        opt_names = extract_download_option_names(state["options"])
        for opt_name in opt_names:
            cb = QtWidgets.QCheckBox(opt_name)
            cb.setChecked(True)
            scroll_layout.addWidget(cb)
            checkboxes[opt_name] = cb
        scroll_layout.addStretch(1)

    def populate_version_combo(select_tag=None):
        ver_combo.blockSignals(True)
        ver_combo.clear()
        rels = state["releases"]
        sel_idx = 0
        pref_norm = normalize_version_tag(select_tag) if select_tag else ""
        for idx, rel in enumerate(rels):
            tag = rel.get("tag_name") or ""
            ver_combo.addItem(_format_release_label(rel, is_latest=(idx == 0), current_version=current_installed_ver), tag)
            if pref_norm and normalize_version_tag(tag) == pref_norm:
                sel_idx = idx
        if ver_combo.count() > 0:
            ver_combo.setCurrentIndex(sel_idx)
            state["target_tag"] = ver_combo.currentData()
        ver_combo.blockSignals(False)

    status_lbl = QtWidgets.QLabel("Ready to install.")
    layout.addWidget(status_lbl)

    def update_action_label():
        sel_tag = ver_combo.currentData() or state["target_tag"]
        sel_build = build_combo.currentData()
        rel_info = next((r for r in state["releases"] if r.get("tag_name") == sel_tag), {})
        assets = rel_info.get("assets", []) if isinstance(rel_info, dict) else []
        title_lbl.setText(f"PyMcLauncher Installation ({sel_tag})")
        if current_installed_ver and sel_tag:
            if normalize_version_tag(sel_tag) == normalize_version_tag(current_installed_ver):
                verb = f"Reinstall {sel_tag} ({sel_build})"
            elif is_newer_version(sel_tag, current_installed_ver):
                verb = f"Downgrade {current_installed_ver} -> {sel_tag} ({sel_build})"
            else:
                verb = f"Upgrade {current_installed_ver} -> {sel_tag} ({sel_build})"
        else:
            verb = f"Ready to install {sel_tag} ({sel_build})."
        if sel_build == "compiled" and len(assets) <= 1 and not any(
            str(a.get("name", "")).lower().endswith((".exe", ".app", ".zip")) for a in assets
        ):
            verb += " [Note: verify compiled release assets exist for this tag]"
        status_lbl.setText(verb)

    populate_version_combo(state["target_tag"])
    rebuild_checkboxes()
    update_action_label()

    pbar = QtWidgets.QProgressBar()
    pbar.setRange(0, 100)
    pbar.setValue(0)
    layout.addWidget(pbar)

    btn_layout = QtWidgets.QHBoxLayout()
    btn_layout.addStretch(1)
    cancel_btn = QtWidgets.QPushButton("Cancel")
    cancel_btn.clicked.connect(win.close)
    btn_layout.addWidget(cancel_btn)

    install_btn = QtWidgets.QPushButton("Download & Install")
    install_btn.setDefault(True)
    install_btn.setStyleSheet("font-weight: bold; padding: 6px 14px;")
    btn_layout.addWidget(install_btn)
    layout.addLayout(btn_layout)

    def set_controls_enabled(enabled):
        channel_combo.setEnabled(enabled)
        build_combo.setEnabled(enabled)
        ver_combo.setEnabled(enabled)
        refresh_btn.setEnabled(enabled)
        install_btn.setEnabled(enabled)
        cancel_btn.setEnabled(enabled)
        for cb in checkboxes.values():
            cb.setEnabled(enabled)

    class MetadataWorker(QtCore.QThread):
        done_sig = QtCore.pyqtSignal(object, str)

        def __init__(self, chan, pref_tag=None):
            super().__init__()
            self.chan = chan
            self.pref_tag = pref_tag

        def run(self):
            try:
                res = _fetch_channel_metadata(self.chan, preferred_tag=self.pref_tag)
                if not res or not res.get("options"):
                    self.done_sig.emit(None, "Could not fetch release list from GitHub.")
                else:
                    self.done_sig.emit(res, "")
            except Exception as e:
                self.done_sig.emit(None, str(e))

    class TagOptionsWorker(QtCore.QThread):
        done_sig = QtCore.pyqtSignal(str, object)

        def __init__(self, tag):
            super().__init__()
            self.tag = tag

        def run(self):
            opts = fetch_download_options(self.tag) or _load_local_download_options()
            self.done_sig.emit(self.tag, opts)

    def reload_channel(pref_tag=None):
        set_controls_enabled(False)
        status_lbl.setText("Fetching releases for selected channel...")
        mw = MetadataWorker(channel_combo.currentData(), pref_tag=pref_tag)
        win._meta_worker = mw

        def on_done(res, err):
            set_controls_enabled(True)
            if not res:
                QtWidgets.QMessageBox.warning(win, "Refresh Failed", err or "Could not fetch releases.")
                update_action_label()
                return
            state["releases"] = res["releases"]
            state["target_tag"] = res["target_tag"]
            state["options"] = res["options"]
            populate_version_combo(res["target_tag"])
            populate_build_combo(res["options"])
            rebuild_checkboxes()
            update_action_label()

        mw.done_sig.connect(on_done)
        mw.start()

    def on_version_changed():
        tag = ver_combo.currentData()
        if not tag:
            return
        state["target_tag"] = tag
        update_action_label()
        tw = TagOptionsWorker(tag)
        win._tag_worker = tw

        def on_tag_opts(loaded_tag, opts):
            if loaded_tag == ver_combo.currentData() and opts:
                state["options"] = opts
                populate_build_combo(opts)
                rebuild_checkboxes()

        tw.done_sig.connect(on_tag_opts)
        tw.start()

    channel_combo.currentIndexChanged.connect(lambda _: reload_channel())
    refresh_btn.clicked.connect(lambda: reload_channel(pref_tag=ver_combo.currentData()))
    ver_combo.currentIndexChanged.connect(lambda _: on_version_changed())
    build_combo.currentIndexChanged.connect(lambda _: update_action_label())

    class DownloadWorker(QtCore.QThread):
        progress_sig = QtCore.pyqtSignal(int, int, str)
        finished_sig = QtCore.pyqtSignal(bool, str)

        def __init__(self, tag, chan, btype, selected_opts, opts_dict):
            super().__init__()
            self.tag = tag
            self.chan = chan
            self.btype = btype
            self.selected_opts = selected_opts
            self.opts_dict = opts_dict
            self.result = None

        def run(self):
            try:
                def on_prog(cur, total, fp):
                    self.progress_sig.emit(cur, total, f"Downloading: {fp}")

                self.result = _execute_install_plan(
                    self.tag,
                    install_dir,
                    selected_opts=self.selected_opts,
                    channel=self.chan,
                    build_type=self.btype,
                    download_options=self.opts_dict,
                    progress_cb=on_prog,
                )
                self.finished_sig.emit(True, "")
            except Exception as e:
                self.finished_sig.emit(False, str(e))

    def on_install_clicked():
        selected = [name for name, cb in checkboxes.items() if cb.isChecked()]
        if not selected:
            QtWidgets.QMessageBox.warning(win, "No Options Selected", "Please select at least one option to install.")
            return

        sel_tag = ver_combo.currentData() or state["target_tag"]
        sel_chan = channel_combo.currentData()
        sel_build = build_combo.currentData()

        if is_version_ignored(sel_tag, download_options=state["options"]):
            QtWidgets.QMessageBox.critical(
                win, "Blocked Version", f"Version {sel_tag} is listed in Old Ignore versions and cannot be installed."
            )
            return

        set_controls_enabled(False)
        worker = DownloadWorker(sel_tag, sel_chan, sel_build, selected, dict(state["options"]))
        win._worker = worker

        def on_prog(cur, total, msg):
            pbar.setValue(int((cur / total) * 100) if total else 0)
            status_lbl.setText(msg[:70])

        def on_finish(success, err):
            if not success:
                QtWidgets.QMessageBox.critical(win, "Installation Failed", f"Installation failed: {err}")
                set_controls_enabled(True)
                update_action_label()
                return

            pbar.setValue(100)
            status_lbl.setText("Installation completed successfully!")
            res = worker.result or {}
            resolved_paths = res.get("paths", [])
            resolved_files = res.get("files", [])
            resolved_assets = res.get("assets", [])
            resolved_messages = res.get("messages", [])

            this_script_name = os.path.basename(_get_self_target_path()).lower()
            all_downloaded_names = {
                os.path.basename(f.rstrip("/")).lower()
                for f in (resolved_paths + resolved_files + [a.get("name", "") for a in resolved_assets])
                if f
            }
            should_delete_self = this_script_name not in all_downloaded_names

            msg_note = "\n\n" + "\n".join(resolved_messages) if resolved_messages else ""
            del_msg = (
                "\n\nNote: Because this installer is not part of the downloaded files, it will now delete itself."
                if should_delete_self
                else ""
            )

            QtWidgets.QMessageBox.information(
                win,
                "Installation Complete",
                f"PyMcLauncher ({sel_tag}, {sel_build}) has been installed successfully!{msg_note}{del_msg}",
            )

            win.close()
            if should_delete_self:
                schedule_self_deletion()
            sys.exit(0)

        worker.progress_sig.connect(on_prog)
        worker.finished_sig.connect(on_finish)
        worker.start()

    install_btn.clicked.connect(on_install_clicked)
    win.show()
    if sys.platform == "darwin":
        try:
            win.raise_()
            win.activateWindow()
        except Exception:
            pass
    app.exec() if qt_ver == 6 else app.exec_()


def _run_tkinter_gui():
    import tkinter as tk
    from tkinter import messagebox, ttk

    root = tk.Tk()
    root.title("PyMcLauncher Installer")
    root.geometry("580x510")
    root.resizable(False, False)

    if sys.platform == "darwin":
        try:
            root.lift()
            root.attributes("-topmost", True)
            root.after_idle(root.attributes, "-topmost", False)
        except Exception:
            pass

    install_dir = _get_install_dir()
    saved_state = load_updater_state(install_dir)
    current_installed_ver = saved_state.get("current_version")
    init_channel = normalize_channel(saved_state.get("channel"))
    init_build_type = normalize_build_type(saved_state.get("build_type"))

    icon_ico = _get_resource_path(os.path.join("images", "icon", "PyMcLauncher.ico"))
    if os.path.exists(icon_ico):
        try:
            root.iconbitmap(icon_ico)
        except Exception:
            pass

    lbl_title = tk.Label(root, text="PyMcLauncher Installer", font=("Arial", 14, "bold"))
    lbl_title.pack(pady=(10, 4))

    lbl_status = tk.Label(root, text="Fetching releases from GitHub...", font=("Arial", 9))
    lbl_status.pack()

    pbar = ttk.Progressbar(root, mode="indeterminate")
    pbar.pack(fill=tk.X, padx=25, pady=6)
    pbar.start(10)

    cfg_frame = tk.LabelFrame(root, text="Update Channel, Build Type & Version", padx=10, pady=8)
    cfg_frame.pack(fill=tk.X, padx=20, pady=4)

    channel_var = tk.StringVar(value=init_channel)
    build_var = tk.StringVar(value=init_build_type)
    ver_label_var = tk.StringVar(value="")

    tk.Label(cfg_frame, text="Channel:").grid(row=0, column=0, sticky="w", padx=4, pady=2)
    channel_menu = ttk.Combobox(
        cfg_frame, textvariable=channel_var, values=extract_channels(), state="disabled", width=18
    )
    channel_menu.grid(row=0, column=1, sticky="w", padx=4, pady=2)

    tk.Label(cfg_frame, text="Build Type:").grid(row=0, column=2, sticky="w", padx=8, pady=2)
    build_menu = ttk.Combobox(
        cfg_frame, textvariable=build_var, values=extract_build_types(), state="disabled", width=16
    )
    build_menu.grid(row=0, column=3, sticky="w", padx=4, pady=2)

    tk.Label(cfg_frame, text="Version:").grid(row=1, column=0, sticky="w", padx=4, pady=2)
    ver_menu = ttk.Combobox(cfg_frame, textvariable=ver_label_var, values=[], state="disabled", width=40)
    ver_menu.grid(row=1, column=1, columnspan=2, sticky="ew", padx=4, pady=2)

    btn_refresh = tk.Button(cfg_frame, text="Refresh", state="disabled")
    btn_refresh.grid(row=1, column=3, sticky="e", padx=4, pady=2)
    cfg_frame.columnconfigure(1, weight=1)

    frame_options = tk.LabelFrame(root, text="Available Download Options", padx=10, pady=8)
    frame_options.pack(fill=tk.BOTH, expand=True, padx=20, pady=4)

    check_vars = {}
    ver_map = {}
    meta_state = {"releases": [], "options": {}}
    btn_install_ref = [None]

    def rebuild_tk_options(opts):
        for w in frame_options.winfo_children():
            w.destroy()
        check_vars.clear()
        for opt_name in extract_download_option_names(opts):
            var = tk.BooleanVar(value=True)
            check_vars[opt_name] = var
            chk = tk.Checkbutton(frame_options, text=opt_name, variable=var, anchor="w")
            chk.pack(fill=tk.X, pady=2)

    def populate_tk_versions(releases, target_tag):
        ver_map.clear()
        labels = []
        selected_label = ""
        pref_norm = normalize_version_tag(target_tag)
        for idx, rel in enumerate(releases):
            tag = rel.get("tag_name") or ""
            lbl = _format_release_label(rel, is_latest=(idx == 0), current_version=current_installed_ver)
            ver_map[lbl] = tag
            labels.append(lbl)
            if pref_norm and normalize_version_tag(tag) == pref_norm:
                selected_label = lbl
        ver_menu["values"] = labels
        if labels:
            ver_label_var.set(selected_label or labels[0])

    def set_tk_controls(state_str):
        combo_state = "readonly" if state_str == "normal" else "disabled"
        channel_menu.config(state=combo_state)
        build_menu.config(state=combo_state)
        ver_menu.config(state=combo_state)
        btn_refresh.config(state=state_str)
        if btn_install_ref[0]:
            btn_install_ref[0].config(state=state_str)

    def reload_tk_channel(preferred_tag=None):
        set_tk_controls("disabled")
        pbar.config(mode="indeterminate")
        pbar.start(10)
        lbl_status.config(text=f"Fetching {channel_var.get()} releases...")

        def worker():
            res = _fetch_channel_metadata(channel_var.get(), preferred_tag=preferred_tag)
            if not res or not res.get("options"):
                root.after(0, lambda: on_reload_error("Could not fetch release metadata from GitHub."))
            else:
                root.after(0, lambda: on_reload_success(res))

        threading.Thread(target=worker, daemon=True).start()

    def on_reload_error(err_msg):
        pbar.stop()
        pbar.config(mode="determinate", value=0)
        set_tk_controls("normal")
        messagebox.showerror("Error", err_msg)

    def on_reload_success(res):
        pbar.stop()
        pbar.config(mode="determinate", value=0)
        meta_state["releases"] = res["releases"]
        meta_state["options"] = res["options"]
        channel_menu["values"] = extract_channels(res["options"])
        build_menu["values"] = extract_build_types(res["options"])
        populate_tk_versions(res["releases"], res["target_tag"])
        rebuild_tk_options(res["options"])
        set_tk_controls("normal")
        cur_tag = ver_map.get(ver_label_var.get(), res["target_tag"])
        lbl_status.config(text=f"Selected: {cur_tag} ({build_var.get()}). Choose options and click Install.")

    channel_menu.bind("<<ComboboxSelected>>", lambda _e: reload_tk_channel())
    build_menu.bind("<<ComboboxSelected>>", lambda _e: lbl_status.config(
        text=f"Selected: {ver_map.get(ver_label_var.get(), '')} ({build_var.get()})."
    ))
    ver_menu.bind("<<ComboboxSelected>>", lambda _e: lbl_status.config(
        text=f"Selected: {ver_map.get(ver_label_var.get(), '')} ({build_var.get()})."
    ))
    btn_refresh.config(command=lambda: reload_tk_channel(preferred_tag=ver_map.get(ver_label_var.get())))

    def do_install():
        selected = [name for name, var in check_vars.items() if var.get()]
        if not selected:
            messagebox.showwarning("Warning", "Please select at least one option to install.")
            return
        chosen_tag = ver_map.get(ver_label_var.get())
        chosen_chan = normalize_channel(channel_var.get())
        chosen_build = normalize_build_type(build_var.get())
        if not chosen_tag:
            messagebox.showwarning("Warning", "Please select a target version.")
            return
        if is_version_ignored(chosen_tag, download_options=meta_state["options"]):
            messagebox.showerror("Blocked Version", f"Version {chosen_tag} is in Old Ignore versions and cannot be installed.")
            return

        set_tk_controls("disabled")
        lbl_status.config(text=f"Installing {chosen_tag} ({chosen_build})...")

        def worker_task(selected_list, ver_str, chan_str, btype_str, opts_dict, dest_dir):
            try:
                def on_prog(cur, total, fp):
                    pct = int((cur / total) * 100) if total else 0
                    short_fp = os.path.basename(fp)
                    root.after(0, lambda p=pct, s=short_fp: (pbar.config(value=p), lbl_status.config(text=f"Downloading: {s}")))

                res = _execute_install_plan(
                    ver_str,
                    dest_dir,
                    selected_opts=selected_list,
                    channel=chan_str,
                    build_type=btype_str,
                    download_options=opts_dict,
                    progress_cb=on_prog,
                )
                paths = res.get("paths", [])
                files = res.get("files", [])
                assets = res.get("assets", [])
                messages = res.get("messages", [])

                this_script = os.path.basename(_get_self_target_path()).lower()
                all_names = {
                    os.path.basename(f.rstrip("/")).lower()
                    for f in (paths + files + [a.get("name", "") for a in assets])
                    if f
                }
                should_delete = this_script not in all_names

                msg_note = ("\n\n" + "\n".join(messages)) if messages else ""
                del_note = (
                    "\n\nNote: Because this installer is not part of the downloaded files, it will now delete itself."
                    if should_delete
                    else ""
                )

                def on_done():
                    messagebox.showinfo(
                        "Success", f"PyMcLauncher ({ver_str}, {btype_str}) installed successfully!{msg_note}{del_note}"
                    )
                    root.destroy()
                    if should_delete:
                        schedule_self_deletion()
                    sys.exit(0)

                root.after(0, on_done)
            except Exception as e:
                err_msg = str(e)
                root.after(0, lambda m=err_msg: (set_tk_controls("normal"), messagebox.showerror("Installation Error", m)))

        threading.Thread(
            target=worker_task,
            args=(list(selected), str(chosen_tag), chosen_chan, chosen_build, dict(meta_state["options"]), str(install_dir)),
            daemon=True,
        ).start()

    btn = tk.Button(root, text="Download & Install", font=("Arial", 10, "bold"), command=do_install, pady=4, state="disabled")
    btn.pack(pady=8)
    btn_install_ref[0] = btn

    def on_initial_error(err_msg):
        pbar.stop()
        print(f"[Error] {err_msg}", file=sys.stderr, flush=True)
        messagebox.showerror("Error", err_msg)
        root.destroy()

    def fetch_meta_thread():
        print("[*] Fetching release metadata from GitHub...", flush=True)
        res = _fetch_channel_metadata(init_channel)
        if not res:
            err = (
                "Could not fetch latest release version from GitHub.\n\n"
                "Please check your internet connection or run the following in Terminal on macOS:\n"
                "'/Applications/Python 3.x/Install Certificates.command'"
            )
            root.after(0, lambda: on_initial_error(err))
            return
        if not res.get("options"):
            err = f"download_options.json was not found in release {res['target_tag']}."
            root.after(0, lambda: on_initial_error(err))
            return
        root.after(0, lambda: on_reload_success(res))

    threading.Thread(target=fetch_meta_thread, daemon=True).start()
    root.mainloop()


def run_update_check(
    install_dir=None,
    relaunch_script=None,
    target_version=None,
    channel=None,
    build_type=None,
    relaunch=True,
):
    """Install/update/downgrade the installation to target_version or latest version on channel."""
    ensure_system_certificates()
    if not install_dir:
        install_dir = _get_install_dir()
    install_dir = os.path.abspath(install_dir)
    cfg = load_updater_state(install_dir)
    selected_opts = cfg.get("selected_options", [DEFAULT_OPTION])
    eff_channel = normalize_channel(channel if channel is not None else cfg.get("channel"))
    eff_build = normalize_build_type(build_type if build_type is not None else cfg.get("build_type"))

    ver = str(target_version).strip() if target_version else get_latest_version(channel=eff_channel)
    if not ver:
        print("[Error] Could not resolve target release version.", file=sys.stderr, flush=True)
        return False

    try:
        res = _execute_install_plan(
            ver,
            install_dir,
            selected_opts=selected_opts,
            channel=eff_channel,
            build_type=eff_build,
        )
    except Exception as e:
        print(f"[Error] {e}", file=sys.stderr, flush=True)
        return False

    for msg in res.get("messages", []):
        print(f"[Info] {msg}", flush=True)

    if relaunch:
        _relaunch_installed_app(
            install_dir,
            relaunch_script=relaunch_script,
            build_type=eff_build,
            downloaded_assets=res.get("assets"),
        )
    return True


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--update-check", action="store_true", help="Perform silent update check and download")
    parser.add_argument("--relaunch", type=str, default=None, help="Script to relaunch")
    parser.add_argument("--no-relaunch", action="store_true", help="Do not launch application after update")
    parser.add_argument("--install-dir", type=str, default=None, help="Target installation directory")
    parser.add_argument("--channel", type=str, default=None, help="Release channel: stable or prerelease")
    parser.add_argument("--build-type", type=str, default=None, help="Build type: source or compiled")
    parser.add_argument("--target-version", type=str, default=None, help="Specific version tag to install or downgrade to")
    parser.add_argument("--list-versions", action="store_true", help="List installable versions (excluding Old Ignore versions)")
    args, _ = parser.parse_known_args()
    if args.list_versions:
        versions = list_installable_versions(channel=args.channel)
        print(json.dumps([{"tag": r.get("tag_name"), "prerelease": bool(r.get("prerelease"))} for r in versions], indent=2))
    elif args.update_check:
        ok = run_update_check(
            args.install_dir,
            args.relaunch,
            target_version=args.target_version,
            channel=args.channel,
            build_type=args.build_type,
            relaunch=not args.no_relaunch,
        )
        if not ok:
            sys.exit(1)
    else:
        run_installer_gui()
