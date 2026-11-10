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
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, QTimer, QProcess, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QMessageBox, QProgressBar, QPushButton,
    QTextBrowser, QTextEdit, QVBoxLayout,
)

OWNER = "cloudyhair6"
REPO = "PyMcLauncher"
USER_AGENT = "Python-Latest-Version-Detector"

_pkg_dir = Path(__file__).resolve().parent
ROOT_DIR = _pkg_dir.parent if _pkg_dir.name == "backend" else _pkg_dir
CONFIG_PATH = ROOT_DIR / "updater_config.json"
REQUIREMENTS_PATH = ROOT_DIR / "requirements.txt"
CACHE_DIR = ROOT_DIR / ".cache"
PENDING_PATH = CACHE_DIR / "update_pending.json"
REQ_HASH_PATH = CACHE_DIR / "requirements.sha256"
INSTALLER_PATH = ROOT_DIR / "installer.py"


def _get_auth_headers():
    try:
        import installer
        return installer.get_auth_headers()
    except Exception:
        return {"User-Agent": USER_AGENT}


def _http_get(url: str, timeout: int = 15):
    headers = _get_auth_headers()
    headers["Accept"] = "application/vnd.github+json"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.geturl(), resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 401 and "Authorization" in headers:
            # Token invalid/expired, retry anonymously
            del headers["Authorization"]
            req_anon = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req_anon, timeout=timeout) as resp:
                return resp.geturl(), resp.read()
        raise


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        except Exception:
            pass

    # Default/initial config if updater_config.json is missing
    default_cfg = {
        "current_version": "v1.7",
        "selected_options": ["PC App only (comes with development soon will without)"],
        "install_dir": str(ROOT_DIR),
    }
    try:
        CONFIG_PATH.write_text(json.dumps(default_cfg, indent=4), encoding="utf-8")
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
            CONFIG_PATH.write_text(json.dumps(cfg, indent=4), encoding="utf-8")
        except Exception:
            pass
    return str(ver)


def get_latest_version() -> str | None:
    """Detect latest release tag via GitHub API, web redirect, or atom feed."""
    try:
        import installer
        ver = installer.get_latest_version()
        if ver:
            return str(ver)
    except Exception:
        pass

    try:
        _, data = _http_get(f"https://api.github.com/repos/{OWNER}/{REPO}/releases/latest")
        tag = json.loads(data.decode("utf-8")).get("tag_name")
        if tag:
            return str(tag)
    except Exception:
        pass

    try:
        final_url, _ = _http_get(f"https://github.com/{OWNER}/{REPO}/releases/latest")
        if "/releases/tag/" in final_url:
            tag = final_url.split("/releases/tag/")[-1].split("?")[0].strip("/")
            if tag:
                return urllib.parse.unquote(tag)
    except Exception:
        pass

    return None


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


# --- Requirements Verification ---
def _read_requirements() -> list[str]:
    reqs = []
    if not REQUIREMENTS_PATH.exists():
        return reqs
    try:
        for line in REQUIREMENTS_PATH.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line and not line.startswith("-"):
                reqs.append(line)
    except Exception:
        pass
    return reqs


def requirements_hash() -> str:
    try:
        return hashlib.sha256(REQUIREMENTS_PATH.read_bytes()).hexdigest()
    except Exception:
        return ""


def _parse_version_tuple(v_str: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", v_str)
    return tuple(int(x) for x in nums)


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
    if not REQUIREMENTS_PATH.exists():
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
        reasons.append("requirements.txt has been modified")
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

    def __init__(self, parent, old_version: str, new_version: str, for_confirmation: bool = False):
        super().__init__(parent)
        self.for_confirmation = for_confirmation
        self.old_version = old_version
        self.new_version = new_version

        title = f"Version Changes ({old_version} -> {new_version})" if for_confirmation else "What's New in Update"
        self.setWindowTitle(title)
        self.resize(620, 480)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)

        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(True)
        self.browser.setMarkdown(
            f"## Version Changes: `{old_version}` -> `{new_version}`\n\n_Loading commit descriptions from GitHub..._"
        )
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
        self._thread.result.connect(self.browser.setMarkdown)
        self._thread.start()


class RequirementsInstallDialog(QDialog):
    """Executes pip install -r requirements.txt with a live progress bar and log viewer."""

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
        self.log_text.setFontFamily("Consolas")
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

        python_exe = sys.executable
        if python_exe.lower().endswith("pythonw.exe"):
            cand = python_exe[:-len("pythonw.exe")] + "python.exe"
            if os.path.exists(cand):
                python_exe = cand

        self.proc.start(
            python_exe,
            ["-m", "pip", "install", "--progress-bar", "off", "--disable-pip-version-check", "-r", str(REQUIREMENTS_PATH)],
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

        # Add update row at the bottom of the launcher layout
        self.row_layout = QHBoxLayout()
        self.label = QLabel("Checking for updates...")
        self.label.setStyleSheet("color: gray;")
        self.button = QPushButton("Update")
        self.button.setVisible(False)
        self.button.clicked.connect(self.start_update)

        self.row_layout.addWidget(self.label, stretch=1)
        self.row_layout.addWidget(self.button)
        self.launcher.layout.addLayout(self.row_layout)

        # Trigger background checks shortly after GUI is displayed
        QTimer.singleShot(1000, self._startup)

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
        if old_ver and new_ver:
            dlg = ChangelogDialog(self.launcher, old_ver, new_ver, for_confirmation=False)
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
            self.label.setStyleSheet("color: gray;")
        elif not cur or cur == latest:
            self.label.setText(f"Up to date - version {cur or latest}")
            self.label.setStyleSheet("color: #2e8b2e; font-weight: 500;")
        else:
            self.label.setText(f"Update available: {cur} -> {latest}")
            self.label.setStyleSheet("color: #cc8800; font-weight: bold;")
            self.button.setText(f"Update to {latest}")
            self.button.setVisible(True)

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

        if not INSTALLER_PATH.exists():
            QMessageBox.critical(
                self.launcher,
                "Installer Missing",
                f"installer.py was not found at {INSTALLER_PATH}.",
            )
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

        # Proceed with update
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            PENDING_PATH.write_text(json.dumps({"from": self.current, "to": self.latest}), encoding="utf-8")

            python_exe = sys.executable
            if python_exe.lower().endswith("pythonw.exe"):
                cand = python_exe[:-len("pythonw.exe")] + "python.exe"
                if os.path.exists(cand):
                    python_exe = cand

            cmd = [
                python_exe,
                str(INSTALLER_PATH),
                "--update-check",
                "--install-dir",
                str(ROOT_DIR),
            ]
            relaunch = "setup-start.bat"
            if (ROOT_DIR / relaunch).exists():
                cmd.extend(["--relaunch", relaunch])

            flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            subprocess.Popen(cmd, cwd=str(ROOT_DIR), creationflags=flags)
        except Exception as e:
            QMessageBox.critical(self.launcher, "Update Failed", f"Could not launch installer.py: {e}")
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
"""
    )
    parser.add_argument("--check", action="store_true", help="Check for available releases on GitHub")
    parser.add_argument("--requirements", action="store_true", help="Check installed Python packages against requirements.txt")
    parser.add_argument("--version", action="store_true", help="Display current launcher version")

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

    args = parser.parse_args()

    if args.version:
        print(f"Current Launcher Version: {get_current_version()}")

    if args.requirements:
        missing = find_unsatisfied_requirements()
        if missing:
            print(f"[Warning] Missing requirements ({len(missing)}):")
            for m in missing:
                print(f"  - {m}")
        else:
            print("[OK] All packages in requirements.txt are installed.")

    if args.check:
        cur = get_current_version()
        print(f"Current version: {cur}")
        print("Checking GitHub releases for updates...")
        latest = get_latest_release_tag()
        if latest and is_newer_version(cur, latest):
            print(f"[Update Available] A new version is available: {latest} (current: {cur})")
            diff = fetch_commit_descriptions(cur, latest)
            if diff:
                print("\nChanges:")
                print(diff)
        elif latest:
            print(f"[Up to Date] Launcher is up to date ({cur}).")
        else:
            print("[Info] Could not query remote release tag.")

