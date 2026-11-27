"""
Shared compiler utilities, Windows PE version-resource generator, source version updater,
Authenticode signer, and PyQt6/Tkinter GUI for compile_launcher.py and compile_installer.py.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = ROOT_DIR / "backend"
LAUNCHER_UPDATE_PATH = BACKEND_DIR / "launcher_update.py"
INSTALLER_PATH = ROOT_DIR / "installer.py"

LANGUAGE_CHOICES = [
    ("English (United States)", 0x0409, "040904B0"),
    ("English (United Kingdom)", 0x0809, "080904B0"),
    ("English (Australia)", 0x0C09, "0C0904B0"),
    ("English (Canada)", 0x1009, "100904B0"),
    ("Language Neutral", 0x0000, "000004B0"),
]
_LANG_BY_NAME = {name.lower(): (lcid, table) for name, lcid, table in LANGUAGE_CHOICES}
_LANG_BY_HEX = {f"{lcid:04x}": (name, lcid, table) for name, lcid, table in LANGUAGE_CHOICES}


@dataclass
class BuildMetadata:
    target_kind: str  # "launcher" or "installer"
    version: str
    is_prerelease: bool
    product_name: str
    file_description: str
    company_name: str
    copyright_text: str
    language_name: str = "English (United States)"
    update_source_version: bool = True
    sync_default_channel: bool = False
    out_dir: str = str(ROOT_DIR / "dist")
    onedir: bool = False
    clean: bool = True
    console: bool = False
    install_reqs: bool = False
    pfx_path: str = ""
    pfx_password: str = ""
    cert_thumbprint: str = ""
    timestamp_url: str = "http://timestamp.digicert.com"
    codesign_identity: str = ""


def looks_like_prerelease(version: str) -> bool:
    return bool(re.search(r"(?:^|[-._])(pre|alpha|beta|rc|dev)\b", str(version or "").strip(), re.IGNORECASE))


def resolve_language(language_spec: str) -> tuple[str, int, str]:
    raw = str(language_spec or "English (United States)").strip()
    low = raw.lower()
    if low in _LANG_BY_NAME:
        lcid, table = _LANG_BY_NAME[low]
        canonical = next(n for n, lid, _ in LANGUAGE_CHOICES if lid == lcid)
        return canonical, lcid, table
    hex_clean = low.lstrip("0x").zfill(4)
    if hex_clean in _LANG_BY_HEX:
        return _LANG_BY_HEX[hex_clean]
    if re.fullmatch(r"[0-9a-f]{1,4}", low.lstrip("0x")):
        lcid = int(low.lstrip("0x"), 16)
        return raw, lcid, f"{lcid:04X}04B0"
    return "English (United States)", 0x0409, "040904B0"


def parse_windows_version_tuple(version_str: str) -> tuple[int, int, int, int]:
    """Convert tags like 'v3.1', 'v3.1-pre01', or '3.2.4' to a valid 4-int Windows PE version tuple."""
    raw = str(version_str or "1.0.0.0").strip()
    m = re.match(r"^[vV]?([0-9]+(?:\.[0-9]+)*)(?:[-._]*(.*))?$", raw)
    nums: list[int] = []
    suffix_nums: list[int] = []
    if m:
        nums = [int(x) for x in m.group(1).split(".") if x.isdigit()]
        suffix = (m.group(2) or "").strip()
        if suffix:
            suffix_nums = [int(x) for x in re.findall(r"\d+", suffix)]
    else:
        nums = [int(x) for x in re.findall(r"\d+", raw)]

    while len(nums) < 3:
        nums.append(0)
    major, minor, patch = nums[0], nums[1], nums[2]
    build = nums[3] if len(nums) > 3 else (suffix_nums[0] if suffix_nums else 0)
    return tuple(max(0, min(int(n), 65535)) for n in (major, minor, patch, build))  # type: ignore[return-value]


DOWNLOAD_OPTIONS_PATH = ROOT_DIR / "download_options.json"


def read_source_version_and_channel() -> tuple[str, str]:
    """Read DEFAULT_LAUNCHER_VERSION from launcher_update.py and Default Channel from download_options.json."""
    ver = "v3.1"
    ch = ""
    try:
        txt = LAUNCHER_UPDATE_PATH.read_text(encoding="utf-8")
        mv = re.search(r'^DEFAULT_LAUNCHER_VERSION\s*=\s*["\']([^"\']+)["\']', txt, re.MULTILINE)
        if mv:
            ver = mv.group(1).strip()
        mc = re.search(r'^DEFAULT_CHANNEL\s*=\s*["\']([^"\']+)["\']', txt, re.MULTILINE)
        if mc:
            ch = mc.group(1).strip().lower()
    except Exception:
        pass
    try:
        if DOWNLOAD_OPTIONS_PATH.exists():
            import json
            opts = json.loads(DOWNLOAD_OPTIONS_PATH.read_text(encoding="utf-8"))
            if isinstance(opts, dict):
                raw_ch = opts.get("Default Channel")
                if not raw_ch and isinstance(opts.get("Updater Settings"), dict):
                    raw_ch = opts["Updater Settings"].get("Default Channel")
                if raw_ch:
                    ch = str(raw_ch).strip().lower()
    except Exception:
        pass
    if not ch:
        ch = "prerelease" if looks_like_prerelease(ver) else "stable"
    return ver, ch


def _replace_python_assignment(path: Path, var_name: str, new_val: str) -> bool:
    if not path.exists():
        return False
    orig = path.read_text(encoding="utf-8")
    pattern = rf'^({re.escape(var_name)}\s*=\s*)["\']([^"\']*)["\']'
    updated, count = re.subn(pattern, rf'\g<1>"{new_val}"', orig, flags=re.MULTILINE)
    if count > 0 and updated != orig:
        path.write_bytes(updated.replace("\r\n", "\n").encode("utf-8"))
        return True
    return count > 0


def sync_version_in_sources(
    version: str,
    default_channel: str | None = None,
    log_cb=None,
) -> list[str]:
    """Update DEFAULT_LAUNCHER_VERSION in source files and optional Default Channel in download_options.json."""
    clean_ver = str(version or "").strip()
    if not clean_ver:
        raise ValueError("Version string cannot be empty.")

    updated_files: list[str] = []
    for target in (LAUNCHER_UPDATE_PATH, INSTALLER_PATH):
        changed_ver = _replace_python_assignment(target, "DEFAULT_LAUNCHER_VERSION", clean_ver)
        if changed_ver:
            try:
                updated_files.append(str(target.relative_to(ROOT_DIR)))
            except ValueError:
                updated_files.append(str(target))

    if default_channel in ("stable", "prerelease") and DOWNLOAD_OPTIONS_PATH.exists():
        import json
        try:
            opts = json.loads(DOWNLOAD_OPTIONS_PATH.read_text(encoding="utf-8"))
            if isinstance(opts, dict) and opts.get("Default Channel") != default_channel:
                opts["Default Channel"] = default_channel
                DOWNLOAD_OPTIONS_PATH.write_bytes((json.dumps(opts, indent=4) + "\n").encode("utf-8"))
                try:
                    updated_files.append(str(DOWNLOAD_OPTIONS_PATH.relative_to(ROOT_DIR)))
                except ValueError:
                    updated_files.append(str(DOWNLOAD_OPTIONS_PATH))
        except Exception:
            pass

    if log_cb:
        msg = f"[*] Source version set to '{clean_ver}'"
        if default_channel in ("stable", "prerelease"):
            msg += f", Default Channel='{default_channel}' (in download_options.json)"
        msg += f" in {', '.join(updated_files) or 'no changes needed'}."
        log_cb(msg)
    return updated_files


def default_metadata_for(target_kind: str) -> BuildMetadata:
    ver, ch = read_source_version_and_channel()
    is_pre = (ch == "prerelease") or looks_like_prerelease(ver)
    if target_kind == "installer":
        return BuildMetadata(
            target_kind="installer",
            version=ver,
            is_prerelease=is_pre,
            product_name="PyMcLauncher Installer",
            file_description="PyMcLauncher Installer",
            company_name="cloudyhair6",
            copyright_text="Copyright © 2026 cloudyhair6",
        )
    return BuildMetadata(
        target_kind="launcher",
        version=ver,
        is_prerelease=is_pre,
        product_name="PyMcLauncher",
        file_description="PyMcLauncher",
        company_name="cloudyhair6",
        copyright_text="Copyright © 2026 cloudyhair6",
    )


def ensure_build_dependencies(force_install: bool = False, log_cb=None):
    """Ensure PyInstaller (and backend/requirements.txt if missing) are installed."""
    need_install = force_install
    if not need_install:
        res = subprocess.run(
            [sys.executable, "-m", "PyInstaller", "--version"],
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            need_install = True

    if need_install:
        req_file = BACKEND_DIR / "requirements.txt"
        if log_cb:
            log_cb(f"[*] Installing dependencies from {req_file}...")
        else:
            print(f"[*] Installing dependencies from {req_file}...", flush=True)
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--prefer-binary", "-r", str(req_file)],
            check=True,
        )


def prepare_icon(work_dir: Path) -> str | None:
    """Return platform-appropriate icon path (.ico on Windows, .icns/.ico on macOS)."""
    ico_src = ROOT_DIR / "images" / "icon" / "PyMcLauncher.ico"
    png_src = ROOT_DIR / "images" / "icon" / "PyMcLauncher-banner.png"
    if sys.platform == "darwin":
        icns_dest = work_dir / "PyMcLauncher.icns"
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            from PIL import Image
            src = ico_src if ico_src.exists() else png_src
            if src.exists():
                Image.open(src).save(icns_dest, format="ICNS")
                if icns_dest.exists():
                    return str(icns_dest)
        except Exception:
            pass
    return str(ico_src) if ico_src.exists() else None


def generate_windows_version_file(meta: BuildMetadata, work_dir: Path, exe_filename: str) -> Path:
    """Generate a PyInstaller VSVersionInfo file setting Properties -> Details, Language & Pre-release flags."""
    import PyInstaller.utils.win32.versioninfo as vi

    work_dir.mkdir(parents=True, exist_ok=True)
    ver_tuple = parse_windows_version_tuple(meta.version)
    dotted_ver = ".".join(str(x) for x in ver_tuple)
    prod_ver_str = str(meta.version).strip() or dotted_ver
    if meta.is_prerelease and "pre-release" not in prod_ver_str.lower() and not looks_like_prerelease(prod_ver_str):
        prod_ver_str = f"{prod_ver_str} (Pre-release)"

    _, lcid, string_table_id = resolve_language(meta.language_name)
    internal_name = os.path.splitext(exe_filename)[0]
    flags = 0x2 if meta.is_prerelease else 0x0  # VS_FF_PRERELEASE = 0x2

    vinfo = vi.VSVersionInfo(
        ffi=vi.FixedFileInfo(
            filevers=ver_tuple,
            prodvers=ver_tuple,
            mask=0x3F,
            flags=flags,
            OS=0x40004,  # VOS_NT_WINDOWS32
            fileType=0x1,  # VFT_APP
            subtype=0x0,
            date=(0, 0),
        ),
        kids=[
            vi.StringFileInfo([
                vi.StringTable(
                    string_table_id,
                    [
                        vi.StringStruct("CompanyName", meta.company_name.strip()),
                        vi.StringStruct("FileDescription", meta.file_description.strip() or meta.product_name.strip()),
                        vi.StringStruct("FileVersion", dotted_ver),
                        vi.StringStruct("InternalName", internal_name),
                        vi.StringStruct("LegalCopyright", meta.copyright_text.strip()),
                        vi.StringStruct("OriginalFilename", exe_filename),
                        vi.StringStruct("ProductName", meta.product_name.strip()),
                        vi.StringStruct("ProductVersion", prod_ver_str),
                        vi.StringStruct("SpecialBuild", "Pre-release" if meta.is_prerelease else ""),
                    ],
                )
            ]),
            vi.VarFileInfo([vi.VarStruct("Translation", [lcid, 1200])]),
        ],
    )

    out_path = work_dir / f"{internal_name}_version_info.txt"
    out_path.write_text(str(vinfo), encoding="utf-8")
    return out_path


def patch_macos_info_plist(app_path: Path, meta: BuildMetadata):
    """Populate CFBundleShortVersionString, CFBundleName, and NSHumanReadableCopyright in Info.plist."""
    plist_path = app_path / "Contents" / "Info.plist"
    if not plist_path.exists():
        return
    try:
        import plistlib
        with open(plist_path, "rb") as f:
            data = plistlib.load(f)
        ver_tuple = parse_windows_version_tuple(meta.version)
        data["CFBundleName"] = meta.product_name.strip()
        data["CFBundleDisplayName"] = meta.file_description.strip() or meta.product_name.strip()
        data["CFBundleShortVersionString"] = str(meta.version).strip().lstrip("vV")
        data["CFBundleVersion"] = ".".join(str(x) for x in ver_tuple)
        if meta.copyright_text.strip():
            data["NSHumanReadableCopyright"] = meta.copyright_text.strip()
        with open(plist_path, "wb") as f:
            plistlib.dump(data, f)
    except Exception:
        pass


def create_macos_zip(app_path: Path, zip_path: Path):
    if zip_path.exists():
        zip_path.unlink()
    ditto_bin = shutil.which("ditto")
    if ditto_bin:
        subprocess.run(
            [ditto_bin, "-c", "-k", "--sequesterRsrc", "--keepParent", str(app_path), str(zip_path)],
            check=True,
        )
        return
    zip_bin = shutil.which("zip")
    if zip_bin:
        subprocess.run([zip_bin, "-qry", str(zip_path), app_path.name], cwd=str(app_path.parent), check=True)


def _find_signtool() -> str | None:
    in_path = shutil.which("signtool")
    if in_path:
        return in_path
    kits_root = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Windows Kits" / "10" / "bin"
    if kits_root.exists():
        cands = sorted(kits_root.glob("**/x64/signtool.exe"), reverse=True)
        if cands:
            return str(cands[0])
    return None


def sign_artifact_if_requested(artifact: Path, meta: BuildMetadata, log_cb=None):
    """Apply Authenticode signing on Windows (.pfx or Cert Thumbprint) or codesign on macOS."""
    def log(msg: str):
        if log_cb:
            log_cb(msg)
        else:
            print(msg, flush=True)

    if sys.platform == "darwin" and meta.codesign_identity.strip() and artifact.exists():
        log(f"[*] Signing macOS bundle with identity '{meta.codesign_identity.strip()}'...")
        subprocess.run(
            ["codesign", "--force", "--deep", "--sign", meta.codesign_identity.strip(), str(artifact)],
            check=True,
        )
        log("[OK] macOS codesign succeeded.")
        return

    if sys.platform != "win32" or not artifact.is_file():
        return

    pfx = meta.pfx_path.strip()
    thumb = meta.cert_thumbprint.strip()
    if not pfx and not thumb:
        return

    ts_url = meta.timestamp_url.strip() or "http://timestamp.digicert.com"
    signtool = _find_signtool()
    if signtool:
        cmd = [signtool, "sign", "/fd", "SHA256", "/tr", ts_url, "/td", "SHA256"]
        if pfx:
            cmd.extend(["/f", pfx])
            if meta.pfx_password:
                cmd.extend(["/p", meta.pfx_password])
        elif thumb:
            cmd.extend(["/sha1", thumb])
        if meta.file_description.strip():
            cmd.extend(["/d", meta.file_description.strip()])
        cmd.append(str(artifact))
        log(f"[*] Signing {artifact.name} with signtool.exe...")
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"signtool failed:\n{res.stderr or res.stdout}")
        log(f"[OK] Authenticode signed: {artifact.name}")
        return

    # Fallback: PowerShell Set-AuthenticodeSignature
    log(f"[*] signtool.exe not found; signing {artifact.name} via PowerShell Set-AuthenticodeSignature...")
    env = os.environ.copy()
    env["PYMC_SIGN_TARGET"] = str(artifact)
    env["PYMC_SIGN_TS"] = ts_url
    if pfx:
        env["PYMC_SIGN_PFX"] = pfx
        env["PYMC_SIGN_PASS"] = meta.pfx_password
        ps_body = (
            "$ErrorActionPreference = 'Stop'; "
            "if ($env:PYMC_SIGN_PASS) { "
            "$sec = ConvertTo-SecureString -String $env:PYMC_SIGN_PASS -AsPlainText -Force; "
            "$cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($env:PYMC_SIGN_PFX, $sec); "
            "} else { "
            "$cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($env:PYMC_SIGN_PFX); "
            "}; "
            "$res = Set-AuthenticodeSignature -FilePath $env:PYMC_SIGN_TARGET -Certificate $cert -HashAlgorithm SHA256 -TimestampServer $env:PYMC_SIGN_TS; "
            "if ($res.Status -ne 'Valid') { throw ('Signature status: ' + $res.Status + ' - ' + $res.StatusMessage) }"
        )
    else:
        env["PYMC_SIGN_THUMB"] = thumb
        ps_body = (
            "$ErrorActionPreference = 'Stop'; "
            "$cert = Get-ChildItem -Path Cert:\\CurrentUser\\My\\$env:PYMC_SIGN_THUMB; "
            "if (-not $cert) { throw 'Certificate thumbprint not found in Cert:\\CurrentUser\\My' }; "
            "$res = Set-AuthenticodeSignature -FilePath $env:PYMC_SIGN_TARGET -Certificate $cert -HashAlgorithm SHA256 -TimestampServer $env:PYMC_SIGN_TS; "
            "if ($res.Status -ne 'Valid') { throw ('Signature status: ' + $res.Status + ' - ' + $res.StatusMessage) }"
        )
    res = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_body],
        env=env,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        raise RuntimeError(f"Authenticode signing failed:\n{res.stderr or res.stdout}")
    log(f"[OK] Authenticode signed via PowerShell: {artifact.name}")


def run_subprocess_streaming(cmd: list[str], cwd: Path, log_cb=None):
    if log_cb is None:
        subprocess.run(cmd, cwd=str(cwd), check=True)
        return
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=flags,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        log_cb(line.rstrip("\r\n"))
    rc = proc.wait()
    if rc != 0:
        raise subprocess.CalledProcessError(rc, cmd)


def build_arg_parser(target_kind: str, description: str) -> argparse.ArgumentParser:
    defaults = default_metadata_for(target_kind)
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--gui", action="store_true", help="Launch graphical compiler window (default when run with no flags)")
    p.add_argument("--no-gui", "--cli", action="store_true", dest="no_gui", help="Run headless CLI build without opening GUI")
    p.add_argument("--version", type=str, default=defaults.version, help=f"Version tag (current: {defaults.version})")
    p.add_argument("--prerelease", action="store_true", help="Mark compiled executable & metadata as Pre-release")
    p.add_argument("--stable", action="store_true", help="Explicitly mark compiled executable as Stable release")
    p.add_argument("--no-update-source-version", action="store_true", help="Do not modify DEFAULT_LAUNCHER_VERSION in source files")
    p.add_argument("--sync-default-channel", action="store_true", help="Also set 'Default Channel' in download_options.json to prerelease/stable")
    p.add_argument("--product-name", type=str, default=defaults.product_name, help="PE ProductName / macOS CFBundleName")
    p.add_argument("--description", type=str, default=defaults.file_description, help="PE FileDescription / Windows Taskbar label")
    p.add_argument("--author", "--company", "--publisher", type=str, default=defaults.company_name, dest="company_name", help="PE CompanyName (Author / Publisher)")
    p.add_argument("--copyright", type=str, default=defaults.copyright_text, dest="copyright_text", help="PE LegalCopyright string")
    p.add_argument("--language", type=str, default=defaults.language_name, help="PE VersionInfo language (e.g. 'English (United States)', 'English (United Kingdom)', or '0409')")
    p.add_argument("--out-dir", type=str, default=defaults.out_dir, help="Output directory (default: dist)")
    p.add_argument("--onedir", action="store_true", help="Build directory bundle instead of single-file executable")
    p.add_argument("--no-clean", action="store_true", help="Reuse previous PyInstaller cache")
    p.add_argument("--console", action="store_true", help="Keep console window attached for debugging")
    p.add_argument("--install-reqs", action="store_true", help="Force pip install -r backend/requirements.txt first")
    p.add_argument("--pfx", type=str, default="", help="Optional .pfx/.p12 Code Signing certificate path for Windows Authenticode signing")
    p.add_argument("--pfx-password", type=str, default="", help="Optional password for --pfx certificate")
    p.add_argument("--cert-thumbprint", type=str, default="", help="Optional SHA-1 certificate thumbprint in Windows Cert:\\CurrentUser\\My")
    p.add_argument("--timestamp-url", type=str, default="http://timestamp.digicert.com", help="RFC3161 timestamp server URL")
    p.add_argument("--codesign-identity", type=str, default="", help="Optional macOS codesign identity")
    return p


def metadata_from_args(args: argparse.Namespace, target_kind: str) -> BuildMetadata:
    is_pre = True if args.prerelease else (False if args.stable else looks_like_prerelease(args.version))
    return BuildMetadata(
        target_kind=target_kind,
        version=args.version.strip(),
        is_prerelease=is_pre,
        product_name=args.product_name,
        file_description=args.description,
        company_name=args.company_name,
        copyright_text=args.copyright_text,
        language_name=args.language,
        update_source_version=not args.no_update_source_version,
        sync_default_channel=bool(args.sync_default_channel),
        out_dir=args.out_dir,
        onedir=bool(args.onedir),
        clean=not bool(args.no_clean),
        console=bool(args.console),
        install_reqs=bool(args.install_reqs),
        pfx_path=args.pfx,
        pfx_password=args.pfx_password,
        cert_thumbprint=args.cert_thumbprint,
        timestamp_url=args.timestamp_url,
        codesign_identity=args.codesign_identity,
    )


def run_compiler_entrypoint(target_kind: str, description: str, build_fn, argv: list[str] | None = None):
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_arg_parser(target_kind, description)
    args = parser.parse_args(raw_argv)
    meta = metadata_from_args(args, target_kind)

    open_gui = args.gui or (len(raw_argv) == 0)
    if open_gui and not args.no_gui:
        run_compiler_gui(meta, build_fn)
        return
    build_fn(meta)


def run_compiler_gui(initial_meta: BuildMetadata, build_fn):
    """Launch PyQt6 GUI (with Tkinter fallback) for configuring metadata, updating version, signing, and compiling."""
    try:
        from PyQt6 import QtCore, QtGui, QtWidgets
        _run_qt_compiler_gui(QtCore, QtGui, QtWidgets, initial_meta, build_fn)
        return
    except ImportError:
        pass
    _run_tk_compiler_gui(initial_meta, build_fn)


def _run_qt_compiler_gui(QtCore, QtGui, QtWidgets, initial_meta: BuildMetadata, build_fn):
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"cloudyhair6.pymclauncher.compiler.{initial_meta.target_kind}")
        except Exception:
            pass

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    title_kind = "Launcher" if initial_meta.target_kind == "launcher" else "Installer"
    app.setApplicationName(f"PyMcLauncher {title_kind} Compiler")
    app.setApplicationDisplayName(f"PyMcLauncher {title_kind} Compiler")

    ico_path = ROOT_DIR / "images" / "icon" / "PyMcLauncher.ico"
    svg_path = ROOT_DIR / "images" / "icon" / "PyMcLauncher.svg"
    app_icon = QtGui.QIcon()
    if svg_path.exists():
        app_icon.addFile(str(svg_path))
    if ico_path.exists():
        app_icon.addFile(str(ico_path))
    if not app_icon.isNull():
        app.setWindowIcon(app_icon)

    win = QtWidgets.QWidget()
    win.setWindowTitle(f"PyMcLauncher {title_kind} Compiler")
    win.resize(720, 680)
    root_layout = QtWidgets.QVBoxLayout(win)
    root_layout.setContentsMargins(16, 16, 16, 16)
    root_layout.setSpacing(10)

    hdr = QtWidgets.QLabel(f"Compile {title_kind} ({'launcher.py' if initial_meta.target_kind == 'launcher' else 'installer.py'})")
    hdr.setStyleSheet("font-size: 16px; font-weight: bold;")
    root_layout.addWidget(hdr)

    # 1. Version & Release Box
    ver_box = QtWidgets.QGroupBox("Version & Source Sync")
    ver_form = QtWidgets.QFormLayout(ver_box)

    ver_edit = QtWidgets.QLineEdit(initial_meta.version)
    ver_edit.setPlaceholderText("e.g. v3.1 or v3.1-pre01")
    ver_form.addRow("Release Version:", ver_edit)

    pre_cb = QtWidgets.QCheckBox("Mark as Pre-release (sets Windows PE VS_FF_PRERELEASE flag + label)")
    pre_cb.setChecked(initial_meta.is_prerelease)
    ver_form.addRow("", pre_cb)

    sync_src_cb = QtWidgets.QCheckBox("Update DEFAULT_LAUNCHER_VERSION in backend/launcher_update.py && installer.py")
    sync_src_cb.setChecked(initial_meta.update_source_version)
    ver_form.addRow("", sync_src_cb)

    sync_ch_cb = QtWidgets.QCheckBox("Also set 'Default Channel' in download_options.json to match Pre-release")
    sync_ch_cb.setChecked(initial_meta.sync_default_channel)
    ver_form.addRow("", sync_ch_cb)

    def _on_ver_typed(txt: str):
        if looks_like_prerelease(txt):
            pre_cb.setChecked(True)

    ver_edit.textChanged.connect(_on_ver_typed)
    root_layout.addWidget(ver_box)

    # 2. Windows Details & Taskbar Metadata Box
    meta_box = QtWidgets.QGroupBox("Executable Properties, Taskbar Label && Metadata")
    meta_form = QtWidgets.QFormLayout(meta_box)

    prod_edit = QtWidgets.QLineEdit(initial_meta.product_name)
    desc_edit = QtWidgets.QLineEdit(initial_meta.file_description)
    comp_edit = QtWidgets.QLineEdit(initial_meta.company_name)
    copy_edit = QtWidgets.QLineEdit(initial_meta.copyright_text)
    lang_combo = QtWidgets.QComboBox()
    for lang_name, _, _ in LANGUAGE_CHOICES:
        lang_combo.addItem(lang_name, lang_name)
    idx_lang = lang_combo.findData(initial_meta.language_name)
    if idx_lang >= 0:
        lang_combo.setCurrentIndex(idx_lang)

    meta_form.addRow("Product Name:", prod_edit)
    meta_form.addRow("File Description (Taskbar Name):", desc_edit)
    meta_form.addRow("Author / Publisher (CompanyName):", comp_edit)
    meta_form.addRow("Copyright:", copy_edit)
    meta_form.addRow("Language:", lang_combo)
    root_layout.addWidget(meta_box)

    # 3. Build & Output Options Box
    bld_box = QtWidgets.QGroupBox("Build Options")
    bld_v = QtWidgets.QVBoxLayout(bld_box)
    out_row = QtWidgets.QHBoxLayout()
    out_edit = QtWidgets.QLineEdit(initial_meta.out_dir)
    browse_out = QtWidgets.QPushButton("Browse...")

    def _pick_out():
        chosen = QtWidgets.QFileDialog.getExistingDirectory(win, "Select Output Directory", out_edit.text())
        if chosen:
            out_edit.setText(chosen)

    browse_out.clicked.connect(_pick_out)
    out_row.addWidget(QtWidgets.QLabel("Output Folder:"))
    out_row.addWidget(out_edit, stretch=1)
    out_row.addWidget(browse_out)
    bld_v.addLayout(out_row)

    flags_row = QtWidgets.QHBoxLayout()
    onedir_cb = QtWidgets.QCheckBox("One-Dir bundle (faster startup vs single .exe)")
    onedir_cb.setChecked(initial_meta.onedir)
    clean_cb = QtWidgets.QCheckBox("Clean build cache")
    clean_cb.setChecked(initial_meta.clean)
    console_cb = QtWidgets.QCheckBox("Attach console window")
    console_cb.setChecked(initial_meta.console)
    reqs_cb = QtWidgets.QCheckBox("Re-install requirements")
    reqs_cb.setChecked(initial_meta.install_reqs)
    for w in (onedir_cb, clean_cb, console_cb, reqs_cb):
        flags_row.addWidget(w)
    bld_v.addLayout(flags_row)
    root_layout.addWidget(bld_box)

    # 4. Authenticode Code Signing Box
    sign_box = QtWidgets.QGroupBox("Authenticode Digital Code Signing (Optional — Fixes UAC 'Unknown publisher')")
    sign_form = QtWidgets.QFormLayout(sign_box)

    pfx_row = QtWidgets.QHBoxLayout()
    pfx_edit = QtWidgets.QLineEdit(initial_meta.pfx_path)
    pfx_edit.setPlaceholderText("Optional .pfx / .p12 code signing certificate file")
    browse_pfx = QtWidgets.QPushButton("Browse...")

    def _pick_pfx():
        fp, _ = QtWidgets.QFileDialog.getOpenFileName(win, "Select Code Signing Certificate", "", "Certificates (*.pfx *.p12);;All Files (*)")
        if fp:
            pfx_edit.setText(fp)

    browse_pfx.clicked.connect(_pick_pfx)
    pfx_row.addWidget(pfx_edit, stretch=1)
    pfx_row.addWidget(browse_pfx)

    pass_edit = QtWidgets.QLineEdit(initial_meta.pfx_password)
    pass_edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
    pass_edit.setPlaceholderText("Certificate password (if any)")

    thumb_edit = QtWidgets.QLineEdit(initial_meta.cert_thumbprint)
    thumb_edit.setPlaceholderText("Or Windows Cert:\\CurrentUser\\My SHA-1 thumbprint")
    ts_edit = QtWidgets.QLineEdit(initial_meta.timestamp_url)

    uac_note = QtWidgets.QLabel(
        "Note: Windows Explorer Properties & Taskbar labels use Version Info above, while Windows 'Run as administrator' "
        "UAC shows 'Verified publisher' only when signed with a Windows-trusted Authenticode Code Signing certificate."
    )
    uac_note.setWordWrap(True)
    uac_note.setStyleSheet("color: #888; font-size: 11px;")

    sign_form.addRow(".pfx / .p12 File:", pfx_row)
    sign_form.addRow("Certificate Password:", pass_edit)
    sign_form.addRow("Store Thumbprint:", thumb_edit)
    sign_form.addRow("Timestamp URL:", ts_edit)
    sign_form.addRow(uac_note)
    root_layout.addWidget(sign_box)

    # 5. Build Log & Progress
    log_view = QtWidgets.QTextEdit()
    log_view.setReadOnly(True)
    log_view.setMinimumHeight(140)
    log_view.setStyleSheet("font-family: Consolas, monospace; font-size: 11px;")
    root_layout.addWidget(log_view, stretch=1)

    pbar = QtWidgets.QProgressBar()
    pbar.setRange(0, 100)
    pbar.setValue(0)
    root_layout.addWidget(pbar)

    btn_row = QtWidgets.QHBoxLayout()
    sync_only_btn = QtWidgets.QPushButton("Update Source Version Only")
    open_dist_btn = QtWidgets.QPushButton("Open Output Folder")
    compile_btn = QtWidgets.QPushButton(f"Compile {title_kind}")
    compile_btn.setDefault(True)
    compile_btn.setStyleSheet("font-weight: bold; padding: 6px 14px;")
    btn_row.addWidget(sync_only_btn)
    btn_row.addWidget(open_dist_btn)
    btn_row.addStretch(1)
    btn_row.addWidget(compile_btn)
    root_layout.addLayout(btn_row)

    def _collect_meta() -> BuildMetadata:
        return BuildMetadata(
            target_kind=initial_meta.target_kind,
            version=ver_edit.text().strip() or initial_meta.version,
            is_prerelease=pre_cb.isChecked(),
            product_name=prod_edit.text().strip() or initial_meta.product_name,
            file_description=desc_edit.text().strip() or prod_edit.text().strip() or initial_meta.file_description,
            company_name=comp_edit.text().strip(),
            copyright_text=copy_edit.text().strip(),
            language_name=str(lang_combo.currentData() or "English (United States)"),
            update_source_version=sync_src_cb.isChecked(),
            sync_default_channel=sync_ch_cb.isChecked(),
            out_dir=out_edit.text().strip() or str(ROOT_DIR / "dist"),
            onedir=onedir_cb.isChecked(),
            clean=clean_cb.isChecked(),
            console=console_cb.isChecked(),
            install_reqs=reqs_cb.isChecked(),
            pfx_path=pfx_edit.text().strip(),
            pfx_password=pass_edit.text(),
            cert_thumbprint=thumb_edit.text().strip(),
            timestamp_url=ts_edit.text().strip() or "http://timestamp.digicert.com",
        )

    def _append_log(msg: str):
        log_view.append(msg)

    def _on_sync_only():
        meta = _collect_meta()
        ch_val = ("prerelease" if meta.is_prerelease else "stable") if meta.sync_default_channel else None
        try:
            changed = sync_version_in_sources(meta.version, default_channel=ch_val, log_cb=_append_log)
            QtWidgets.QMessageBox.information(
                win,
                "Version Updated",
                f"Updated DEFAULT_LAUNCHER_VERSION to '{meta.version}' in:\n" + ("\n".join(changed) or "(already up to date)"),
            )
        except Exception as e:
            QtWidgets.QMessageBox.critical(win, "Update Error", str(e))

    def _on_open_out():
        target = Path(out_edit.text().strip() or str(ROOT_DIR / "dist"))
        target.mkdir(parents=True, exist_ok=True)
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(target)))

    class _BuildWorker(QtCore.QThread):
        log_sig = QtCore.pyqtSignal(str)
        done_sig = QtCore.pyqtSignal(bool, str)

        def __init__(self, meta: BuildMetadata):
            super().__init__()
            self.meta = meta

        def run(self):
            try:
                artifact = build_fn(self.meta, log_cb=self.log_sig.emit)
                self.done_sig.emit(True, str(artifact))
            except Exception as e:
                self.done_sig.emit(False, str(e))

    def _on_compile():
        meta = _collect_meta()
        log_view.clear()
        compile_btn.setEnabled(False)
        sync_only_btn.setEnabled(False)
        pbar.setRange(0, 0)

        worker = _BuildWorker(meta)
        win._build_worker = worker
        worker.log_sig.connect(_append_log)

        def _on_done(ok: bool, info: str):
            pbar.setRange(0, 100)
            pbar.setValue(100 if ok else 0)
            compile_btn.setEnabled(True)
            sync_only_btn.setEnabled(True)
            if ok:
                QtWidgets.QMessageBox.information(win, "Build Complete", f"Successfully compiled:\n{info}")
            else:
                QtWidgets.QMessageBox.critical(win, "Build Failed", f"Compilation failed:\n{info}")

        worker.done_sig.connect(_on_done)
        worker.start()

    sync_only_btn.clicked.connect(_on_sync_only)
    open_dist_btn.clicked.connect(_on_open_out)
    compile_btn.clicked.connect(_on_compile)

    win.show()
    app.exec()


def _run_tk_compiler_gui(initial_meta: BuildMetadata, build_fn):
    import threading
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk

    root = tk.Tk()
    title_kind = "Launcher" if initial_meta.target_kind == "launcher" else "Installer"
    root.title(f"PyMcLauncher {title_kind} Compiler")
    root.geometry("680x640")

    frm = tk.Frame(root, padx=12, pady=10)
    frm.pack(fill=tk.BOTH, expand=True)

    ver_var = tk.StringVar(value=initial_meta.version)
    pre_var = tk.BooleanVar(value=initial_meta.is_prerelease)
    sync_src_var = tk.BooleanVar(value=initial_meta.update_source_version)
    sync_ch_var = tk.BooleanVar(value=initial_meta.sync_default_channel)
    prod_var = tk.StringVar(value=initial_meta.product_name)
    desc_var = tk.StringVar(value=initial_meta.file_description)
    comp_var = tk.StringVar(value=initial_meta.company_name)
    copy_var = tk.StringVar(value=initial_meta.copyright_text)
    lang_var = tk.StringVar(value=initial_meta.language_name)
    out_var = tk.StringVar(value=initial_meta.out_dir)
    pfx_var = tk.StringVar(value=initial_meta.pfx_path)
    pass_var = tk.StringVar(value=initial_meta.pfx_password)

    rows = [
        ("Release Version:", tk.Entry(frm, textvariable=ver_var)),
        ("Product Name:", tk.Entry(frm, textvariable=prod_var)),
        ("File Description (Taskbar):", tk.Entry(frm, textvariable=desc_var)),
        ("Author / Publisher:", tk.Entry(frm, textvariable=comp_var)),
        ("Copyright:", tk.Entry(frm, textvariable=copy_var)),
        ("Language:", ttk.Combobox(frm, textvariable=lang_var, values=[n for n, _, _ in LANGUAGE_CHOICES], state="readonly")),
        ("Output Folder:", tk.Entry(frm, textvariable=out_var)),
        ("Signing .pfx (Optional):", tk.Entry(frm, textvariable=pfx_var)),
        (".pfx Password:", tk.Entry(frm, textvariable=pass_var, show="*")),
    ]
    for r_idx, (lbl, widget) in enumerate(rows):
        tk.Label(frm, text=lbl, anchor="w").grid(row=r_idx, column=0, sticky="w", pady=2, padx=(0, 8))
        widget.grid(row=r_idx, column=1, sticky="ew", pady=2)
    frm.columnconfigure(1, weight=1)

    chk_frm = tk.Frame(frm)
    chk_frm.grid(row=len(rows), column=0, columnspan=2, sticky="ew", pady=4)
    tk.Checkbutton(chk_frm, text="Pre-release", variable=pre_var).pack(side=tk.LEFT)
    tk.Checkbutton(chk_frm, text="Update source version", variable=sync_src_var).pack(side=tk.LEFT, padx=8)
    tk.Checkbutton(chk_frm, text="Sync JSON Default Channel", variable=sync_ch_var).pack(side=tk.LEFT)

    log_box = scrolledtext.ScrolledText(frm, height=12)
    log_box.grid(row=len(rows) + 1, column=0, columnspan=2, sticky="nsew", pady=6)
    frm.rowconfigure(len(rows) + 1, weight=1)

    def _append(msg: str):
        root.after(0, lambda m=msg: (log_box.insert(tk.END, m + "\n"), log_box.see(tk.END)))

    btn_compile = tk.Button(frm, text=f"Compile {title_kind}")

    def _start():
        btn_compile.config(state="disabled")
        log_box.delete("1.0", tk.END)
        meta = BuildMetadata(
            target_kind=initial_meta.target_kind,
            version=ver_var.get().strip(),
            is_prerelease=pre_var.get(),
            product_name=prod_var.get().strip(),
            file_description=desc_var.get().strip() or prod_var.get().strip(),
            company_name=comp_var.get().strip(),
            copyright_text=copy_var.get().strip(),
            language_name=lang_var.get(),
            update_source_version=sync_src_var.get(),
            sync_default_channel=sync_ch_var.get(),
            out_dir=out_var.get().strip(),
            pfx_path=pfx_var.get().strip(),
            pfx_password=pass_var.get(),
        )

        def _worker():
            try:
                art = build_fn(meta, log_cb=_append)
                root.after(0, lambda: (btn_compile.config(state="normal"), messagebox.showinfo("Done", f"Compiled:\n{art}")))
            except Exception as e:
                err = str(e)
                root.after(0, lambda m=err: (btn_compile.config(state="normal"), messagebox.showerror("Error", m)))

        threading.Thread(target=_worker, daemon=True).start()

    btn_compile.config(command=_start)
    btn_compile.grid(row=len(rows) + 2, column=0, columnspan=2, pady=6)
    root.mainloop()
