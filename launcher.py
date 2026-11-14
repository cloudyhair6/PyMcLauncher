import sys
import json
import os
import re
import threading
import logging
from typing import Optional, List, Dict
from pathlib import Path

# Redirect Python bytecode cache (__pycache__) into .cache/pycache to keep workspace root clean
_root_dir = Path(__file__).resolve().parent
_cache_dir = _root_dir / ".cache"
_pycache_dir = _cache_dir / "pycache"
try:
    _pycache_dir.mkdir(parents=True, exist_ok=True)
    sys.pycache_prefix = str(_pycache_dir)
except Exception:
    pass

# Guard sys.stdout and sys.stderr when launched via pythonw.exe (windowless Desktop/Start shortcuts)
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

def get_python_cli_executable() -> str:
    """Return python.exe (not pythonw.exe) so child QProcesses have working stdout/stderr pipes."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        console_exe = exe.with_name("python.exe")
        if console_exe.exists():
            return str(console_exe)
    return sys.executable

def get_python_gui_executable() -> str:
    """Return pythonw.exe (if available) so Desktop/Start shortcuts don't spawn a console window."""
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe":
        gui_exe = exe.with_name("pythonw.exe")
        if gui_exe.exists():
            return str(gui_exe)
    return sys.executable

# Ensure backend directory is in sys.path
_backend_dir = _root_dir / "backend"
if str(_backend_dir) not in sys.path:
    sys.path.insert(0, str(_backend_dir))
if str(_root_dir) not in sys.path:
    sys.path.insert(0, str(_root_dir))

def get_script_path(script_name: str) -> str:
    """Resolve backend script path, preferring backend/ directory."""
    b_path = _backend_dir / script_name
    if b_path.exists():
        return str(b_path)
    r_path = _root_dir / script_name
    if r_path.exists():
        return str(r_path)
    return script_name

def get_image_path(rel_path: str) -> str:
    """Resolve image path from images/ (with fallback to files/)."""
    img_path = _root_dir / "images" / rel_path
    if img_path.exists():
        return str(img_path)
    files_path = _root_dir / "files" / rel_path
    if files_path.exists():
        return str(files_path)
    return str(img_path)

# Pre-import download and launch modules in-process so they share Mojang manifest/version caches
# and avoid repeated Python interpreter startup overhead during the launch pipeline
import download_java
import download_minecraft
import extract_source_code
import download_libraries
import download_assets
import command_executor

# Suppress harmless Chromium hardware acceleration warnings
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-logging"
os.environ["QTWEBENGINE_DISABLE_SANDBOX"] = "1"

from PyQt6 import QtCore
from PyQt6.QtCore import Qt, QUrl, QUrlQuery, QProcess, QTimer, QRect, QEvent, QTime
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QGridLayout, 
                             QLabel, QProgressBar, QTextEdit, QHBoxLayout, QPushButton, QMessageBox, 
                             QStackedWidget, QComboBox, QDialog, QCheckBox, QStyledItemDelegate, QStyle, QFileDialog, QGroupBox, QRadioButton,
                             QListWidget, QListWidgetItem, QSpinBox)
from PyQt6.QtGui import QFont, QIcon, QPalette, QColor, QImage, QPainter, QPixmap, QMouseEvent, QCursor
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEngineProfile
from utils import (
    get_dir, get_available_versions, get_downloaded_versions, get_quick_play_options,
    delete_version_files, load_launcher_config, 
    save_launcher_config, get_auth_cache, save_auth_cache, clear_auth_cache, get_active_profile_id, load_accounts, save_accounts, 
    has_any_downloads, delete_all_downloads, has_any_source, delete_source_folder, fetch_player_textures,
    is_minecraft_window_visible, get_desktop_dir, get_start_menu_programs_dir, get_applications_dir,
    create_windows_shortcut, create_macos_shortcut,
    calculate_locator_bar_color, get_windows_linked_microsoft_accounts
)

import struct
from PyQt6.QtCore import QByteArray, QBuffer, QIODevice
from PyQt6.QtWidgets import QFileDialog, QFormLayout, QLineEdit, QInputDialog, QMessageBox


def _save_qimage_as_ico(img: QImage, ico_path: Path) -> Optional[Path]:
    """Encode a 64x64 QImage into a valid Windows .ico file containing PNG data."""
    try:
        ico_path.parent.mkdir(parents=True, exist_ok=True)
        scaled = img.scaled(64, 64, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation)
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        scaled.save(buf, "PNG")
        png_bytes = bytes(ba)
        w = scaled.width() if scaled.width() < 256 else 0
        h = scaled.height() if scaled.height() < 256 else 0
        header = struct.pack("<HHH", 0, 1, 1)
        entry = struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(png_bytes), 22)
        ico_path.write_bytes(header + entry + png_bytes)
        return ico_path
    except Exception:
        return None


def get_pymclauncher_ico_path() -> Optional[Path]:
    """Return the official PyMcLauncher.ico file path if it exists."""
    candidates = [
        _root_dir / "images" / "icon" / "PyMcLauncher.ico",
    ]
    try:
        resolved = Path(get_image_path("icon/PyMcLauncher.ico"))
        candidates.append(resolved)
    except Exception:
        pass
    for cand in candidates:
        if cand.exists() and cand.stat().st_size > 0:
            return cand
    return None


def get_pymclauncher_icon() -> QIcon:
    """Return the official PyMcLauncher QIcon using SVG with ICO fallback."""
    icon = QIcon()
    svg_candidates = [
        _root_dir / "images" / "icon" / "PyMcLauncher.svg",
    ]
    try:
        svg_candidates.append(Path(get_image_path("icon/PyMcLauncher.svg")))
    except Exception:
        pass
    for svg_path in svg_candidates:
        if svg_path.exists() and svg_path.stat().st_size > 0:
            icon.addFile(str(svg_path))
            break

    ico_path = get_pymclauncher_ico_path()
    if ico_path and ico_path.exists():
        icon.addFile(str(ico_path))

    if icon.isNull():
        fallback_ico = ensure_default_minecraft_ico()
        if fallback_ico and fallback_ico.exists():
            icon = QIcon(str(fallback_ico))
    return icon


def ensure_default_minecraft_ico() -> Optional[Path]:
    """Return official PyMcLauncher.ico if present, or generate and return fallback grass block .ico in .cache/icons/."""
    official_ico = get_pymclauncher_ico_path()
    if official_ico and official_ico.exists():
        return official_ico

    ico_path = _cache_dir / "icons" / "pymclauncher_grass.ico"
    if ico_path.exists() and ico_path.stat().st_size > 100:
        return ico_path

    # Draw a 16x16 pixel-art Minecraft grass block and scale to 64x64
    img = QImage(16, 16, QImage.Format.Format_ARGB32)
    img.fill(QColor(0, 0, 0, 0))
    p = QPainter(img)

    # Dirt base
    p.fillRect(1, 1, 14, 14, QColor("#866043"))
    # Dirt texture speckles
    for dx, dy, col in [
        (2, 6, "#6b4c35"), (5, 8, "#9c7252"), (9, 7, "#6b4c35"), (12, 6, "#593e2b"),
        (3, 10, "#9c7252"), (7, 11, "#6b4c35"), (11, 10, "#9c7252"), (4, 13, "#593e2b"),
        (8, 13, "#9c7252"), (13, 12, "#6b4c35"), (2, 12, "#6b4c35"), (10, 13, "#593e2b"),
    ]:
        p.fillRect(dx, dy, 1, 1, QColor(col))

    # Grass top and overhanging blades
    p.fillRect(1, 1, 14, 3, QColor("#5b8731"))
    for gx, gy, gh, col in [
        (1, 4, 2, "#5b8731"), (2, 4, 1, "#6da13b"), (4, 4, 2, "#5b8731"), (5, 4, 3, "#4c7228"),
        (7, 4, 1, "#6da13b"), (8, 4, 2, "#5b8731"), (10, 4, 3, "#5b8731"), (11, 4, 1, "#4c7228"),
        (13, 4, 2, "#6da13b"), (14, 4, 1, "#5b8731"),
        (2, 1, 1, "#6da13b"), (6, 2, 1, "#6da13b"), (11, 1, 1, "#6da13b"), (9, 2, 1, "#4c7228"),
    ]:
        p.fillRect(gx, gy, 1, gh, QColor(col))

    # Crisp dark border
    p.setPen(QColor("#2c1e14"))
    p.drawRect(0, 0, 15, 15)
    p.end()

    return _save_qimage_as_ico(img, ico_path)


def image_to_ico(source_path: str, ico_name: str) -> Optional[Path]:
    """Convert a PNG/JPG/SVG/ICO image file into a Windows .ico file in .cache/icons/."""
    src = Path(source_path)
    if not src.exists():
        return ensure_default_minecraft_ico()
    if src.suffix.lower() == ".ico":
        return src
    if "pymclauncher" in src.name.lower():
        official_ico = get_pymclauncher_ico_path()
        if official_ico and official_ico.exists():
            return official_ico
    img = QImage(str(src))
    if img.isNull():
        return ensure_default_minecraft_ico()
    safe_stem = re.sub(r'[<>:"/\\|?*]', '_', ico_name)
    out_ico = _cache_dir / "icons" / f"{safe_stem}.ico"
    return _save_qimage_as_ico(img, out_ico) or ensure_default_minecraft_ico()


class CreateShortcutDialog(QDialog):
    """Dialog to create a Desktop shortcut or pin to Start Menu for a version, world/Quick Play, or profile."""
    def __init__(self, launcher_gui: 'LauncherGUI'):
        super().__init__(launcher_gui)
        self.launcher_gui = launcher_gui
        self.setWindowTitle("Create Shortcut / Pin to Start")
        self.setMinimumWidth(480)
        self._user_edited_name = False
        self._custom_icon_path = None

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # 1. Target Selection Group
        target_group = QGroupBox("Launch Target")
        target_form = QFormLayout(target_group)
        target_form.setSpacing(8)

        # Profile selector
        self.profile_combo = QComboBox()
        config = load_launcher_config()
        profiles = list(config.get("profiles", {"Default": {}}).keys())
        if not profiles:
            profiles = ["Default"]
        for p_name in profiles:
            self.profile_combo.addItem(p_name, p_name)
        current_prof = launcher_gui.profile_combo.currentText() if hasattr(launcher_gui, "profile_combo") else "Default"
        p_idx = self.profile_combo.findText(current_prof)
        if p_idx >= 0:
            self.profile_combo.setCurrentIndex(p_idx)
        target_form.addRow("Profile:", self.profile_combo)

        # Version selector
        self.version_combo = QComboBox()
        target_form.addRow("Minecraft Version:", self.version_combo)

        # World / Quick Play selector
        self.qp_combo = QComboBox()
        target_form.addRow("World / Quick Play:", self.qp_combo)

        layout.addWidget(target_group)

        # 2. Shortcut Details & Location Group
        details_group = QGroupBox("Shortcut Details & Destination")
        details_layout = QVBoxLayout(details_group)
        details_layout.setSpacing(8)

        name_form = QFormLayout()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Enter shortcut name...")
        self.name_edit.textEdited.connect(self._on_name_edited_by_user)
        name_form.addRow("Shortcut Name:", self.name_edit)
        details_layout.addLayout(name_form)

        # Icon preview row
        icon_row = QHBoxLayout()
        icon_row.addWidget(QLabel("Shortcut Icon:"))
        self.icon_preview_lbl = QLabel()
        self.icon_preview_lbl.setFixedSize(32, 32)
        self.icon_preview_lbl.setScaledContents(True)
        icon_row.addWidget(self.icon_preview_lbl)

        self.icon_source_lbl = QLabel("Default Minecraft Icon")
        icon_row.addWidget(self.icon_source_lbl, stretch=1)

        self.browse_icon_btn = QPushButton("Browse...")
        self.browse_icon_btn.clicked.connect(self._browse_custom_icon)
        icon_row.addWidget(self.browse_icon_btn)

        self.reset_icon_btn = QPushButton("Reset")
        self.reset_icon_btn.clicked.connect(self._reset_icon)
        self.reset_icon_btn.setVisible(False)
        icon_row.addWidget(self.reset_icon_btn)
        details_layout.addLayout(icon_row)

        # Checkboxes for Desktop & Start Menu
        self.desktop_cb = QCheckBox("Create shortcut on Desktop")
        self.desktop_cb.setChecked(True)
        details_layout.addWidget(self.desktop_cb)

        self.start_menu_cb = QCheckBox("Add to Applications folder" if sys.platform == "darwin" else "Pin to Start / Add to Start Menu")
        self.start_menu_cb.setChecked(True)
        details_layout.addWidget(self.start_menu_cb)

        layout.addWidget(details_group)

        # 3. Bottom Action Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.create_btn = QPushButton("Create Shortcut")
        self.create_btn.setDefault(True)
        self.create_btn.clicked.connect(self._on_create_clicked)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(self.create_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        # Populate versions and quick play for the initial profile
        self._populate_versions_and_qp(initial=True)

        # Connect change signals
        self.profile_combo.currentIndexChanged.connect(lambda _: self._populate_versions_and_qp(initial=False))
        self.version_combo.currentIndexChanged.connect(self._on_target_selection_changed)
        self.qp_combo.currentIndexChanged.connect(self._on_target_selection_changed)

    def _on_name_edited_by_user(self, _text: str):
        self._user_edited_name = True

    def _populate_versions_and_qp(self, initial: bool = False):
        prof_name = self.profile_combo.currentText()
        prev_ver = self.launcher_gui.version_combo.currentData() if initial else self.version_combo.currentData()
        prev_qp = self.launcher_gui.quick_play_combo.currentData() if (initial and hasattr(self.launcher_gui, "quick_play_combo")) else self.qp_combo.currentData()

        self.version_combo.blockSignals(True)
        self.version_combo.clear()
        self.version_combo.addItem("Latest Version (Release)", "latest-release")
        self.version_combo.addItem("Latest Version (Snapshot)", "latest-snapshot")
        self.version_combo.addItem("Profile's Default / Last Played Version", None)

        seen_versions = set()
        # Add versions from the main launcher combo first
        for i in range(self.launcher_gui.version_combo.count()):
            v_text = self.launcher_gui.version_combo.itemText(i)
            v_id = self.launcher_gui.version_combo.itemData(i)
            if v_id and v_id not in seen_versions:
                self.version_combo.addItem(v_text, v_id)
                seen_versions.add(v_id)

        # Also include any downloaded versions specifically in the chosen profile
        for dl_v in sorted(get_downloaded_versions(profile_name=prof_name)):
            if dl_v not in seen_versions:
                self.version_combo.addItem(f"{dl_v} [Downloaded]", dl_v)
                seen_versions.add(dl_v)

        if prev_ver:
            v_idx = self.version_combo.findData(prev_ver)
            if v_idx >= 0:
                self.version_combo.setCurrentIndex(v_idx)
        else:
            self.version_combo.setCurrentIndex(0)
        self.version_combo.blockSignals(False)

        # Populate Quick Play options for the chosen profile
        self.qp_combo.blockSignals(True)
        self.qp_combo.clear()
        self.qp_combo.addItem("No Quick Play (Launch to Title Screen)", None)

        seen_qp = set()
        try:
            for qp in get_quick_play_options(include_realms=False, profile_name=prof_name):
                key = (qp.get("type"), str(qp.get("id")))
                if key not in seen_qp:
                    self.qp_combo.addItem(qp["display"], qp)
                    seen_qp.add(key)
        except Exception:
            pass

        # Also pull any already-loaded Realms from the main window's quick_play_combo
        if hasattr(self.launcher_gui, "quick_play_combo"):
            for i in range(self.launcher_gui.quick_play_combo.count()):
                qp_data = self.launcher_gui.quick_play_combo.itemData(i)
                if isinstance(qp_data, dict) and qp_data.get("type") == "realms":
                    key = ("realms", str(qp_data.get("id")))
                    if key not in seen_qp:
                        self.qp_combo.addItem(qp_data["display"], qp_data)
                        seen_qp.add(key)

        if isinstance(prev_qp, dict):
            target_key = (prev_qp.get("type"), str(prev_qp.get("id")))
            for i in range(self.qp_combo.count()):
                d = self.qp_combo.itemData(i)
                if isinstance(d, dict) and (d.get("type"), str(d.get("id"))) == target_key:
                    self.qp_combo.setCurrentIndex(i)
                    break
        self.qp_combo.blockSignals(False)

        self._on_target_selection_changed()

    def _on_target_selection_changed(self):
        if not self._user_edited_name or not self.name_edit.text().strip():
            self.name_edit.setText(self._build_suggested_name())
        self._update_icon_preview()

    def _build_suggested_name(self) -> str:
        prof = self.profile_combo.currentText()
        ver = self.version_combo.currentData()
        qp = self.qp_combo.currentData()

        if ver in ("latest", "latest-release"):
            base = "Minecraft (Latest)"
        elif ver == "latest-snapshot":
            base = "Minecraft (Latest Snapshot)"
        elif ver:
            base = f"Minecraft {ver}"
        else:
            base = "Minecraft"
        if prof and prof != "Default":
            base += f" ({prof})"

        if isinstance(qp, dict):
            disp = qp.get("display", "")
            for prefix in ("World: ", "Server: ", "Realm: "):
                if disp.startswith(prefix):
                    disp = disp[len(prefix):]
                    break
            disp = re.sub(r"\s*\[Expired\]$", "", disp).strip()
            if disp:
                return f"{base} - {disp}"
        return base

    def _browse_custom_icon(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Shortcut Icon",
            "",
            "Icons & Images (*.ico *.png *.jpg *.jpeg *.bmp)"
        )
        if path:
            self._custom_icon_path = path
            self.reset_icon_btn.setVisible(True)
            self._update_icon_preview()

    def _reset_icon(self):
        self._custom_icon_path = None
        self.reset_icon_btn.setVisible(False)
        self._update_icon_preview()

    def _get_effective_icon_source(self) -> Optional[str]:
        if self._custom_icon_path and Path(self._custom_icon_path).exists():
            return self._custom_icon_path
        qp = self.qp_combo.currentData()
        if isinstance(qp, dict) and qp.get("icon_path") and Path(qp["icon_path"]).exists():
            return qp["icon_path"]
        svg_cand = _root_dir / "images" / "icon" / "PyMcLauncher.svg"
        if svg_cand.exists():
            return str(svg_cand)
        ico_cand = get_pymclauncher_ico_path()
        if ico_cand and ico_cand.exists():
            return str(ico_cand)
        default_ico = ensure_default_minecraft_ico()
        return str(default_ico) if default_ico else None

    def _update_icon_preview(self):
        src = self._get_effective_icon_source()
        if self._custom_icon_path and src == self._custom_icon_path:
            self.icon_source_lbl.setText(f"Custom ({Path(src).name})")
        elif isinstance(self.qp_combo.currentData(), dict) and self.qp_combo.currentData().get("icon_path") == src and src:
            self.icon_source_lbl.setText("World Icon (icon.png)")
        else:
            self.icon_source_lbl.setText("Default PyMcLauncher Icon")

        if src and Path(src).exists():
            pix = QPixmap(str(src))
            if not pix.isNull():
                self.icon_preview_lbl.setPixmap(
                    pix.scaled(32, 32, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation)
                )

    def _on_create_clicked(self):
        raw_name = self.name_edit.text().strip()
        if not raw_name:
            QMessageBox.warning(self, "Missing Name", "Please enter a name for the shortcut.")
            return

        to_desktop = self.desktop_cb.isChecked()
        to_start = self.start_menu_cb.isChecked()
        if not to_desktop and not to_start:
            QMessageBox.warning(self, "No Destination Selected", "Please select at least one destination (Desktop or Start Menu).")
            return

        safe_filename = re.sub(r'[<>:"/\\|?*]', "-", raw_name).strip(". ")
        if not safe_filename:
            safe_filename = "Minecraft Shortcut"

        prof = self.profile_combo.currentText()
        ver = self.version_combo.currentData()
        qp = self.qp_combo.currentData()

        launcher_script = str(Path(__file__).resolve())
        arg_parts = [f'"{launcher_script}"', "--shortcut-launch"]
        if prof:
            arg_parts.append(f'--profile "{prof}"')
        if ver:
            arg_parts.append(f'--version "{ver}"')
        if isinstance(qp, dict) and qp.get("type") and qp.get("id"):
            arg_parts.append(f'--quick-play-type "{qp["type"]}"')
            arg_parts.append(f'--quick-play-id "{qp["id"]}"')
            if qp.get("display"):
                clean_disp = str(qp["display"]).replace('"', "'")
                arg_parts.append(f'--quick-play-display "{clean_disp}"')

        arguments_str = " ".join(arg_parts)
        target_exe = get_python_gui_executable()
        working_dir = str(_root_dir)

        icon_src = self._get_effective_icon_source()
        ico_file = image_to_ico(icon_src, safe_filename) if icon_src else ensure_default_minecraft_ico()
        ico_str = str(ico_file.resolve()) if ico_file and ico_file.exists() else None

        desc_parts = ["Launch Minecraft"]
        if ver:
            desc_parts.append(f"v{ver}")
        if prof:
            desc_parts.append(f"(Profile: {prof})")
        if isinstance(qp, dict) and qp.get("display"):
            desc_parts.append(f"- {qp['display']}")
        description = " ".join(desc_parts)

        created_locations = []
        errors = []

        if sys.platform == "darwin":
            if to_desktop:
                desktop_app = get_desktop_dir() / f"{safe_filename}.app"
                ok, msg = create_macos_shortcut(
                    dest_path=desktop_app,
                    target_exe=target_exe,
                    arguments=arguments_str,
                    working_dir=working_dir,
                    icon_path=ico_str,
                    name=name
                )
                if ok:
                    created_locations.append(f"Desktop: {desktop_app}")
                else:
                    errors.append(f"Desktop shortcut failed: {msg}")

            if to_start:
                apps_dir = get_applications_dir()
                app_dest = apps_dir / f"{safe_filename}.app"
                ok, msg = create_macos_shortcut(
                    dest_path=app_dest,
                    target_exe=target_exe,
                    arguments=arguments_str,
                    working_dir=working_dir,
                    icon_path=ico_str,
                    name=name
                )
                if ok:
                    created_locations.append(f"Applications: {app_dest}")
                else:
                    errors.append(f"Applications shortcut failed: {msg}")
        else:
            if to_desktop:
                desktop_lnk = get_desktop_dir() / f"{safe_filename}.lnk"
                ok, msg = create_windows_shortcut(
                    lnk_path=desktop_lnk,
                    target_exe=target_exe,
                    arguments=arguments_str,
                    working_dir=working_dir,
                    icon_path=ico_str,
                    description=description,
                    pin_to_start=False
                )
                if ok:
                    created_locations.append(f"Desktop: {desktop_lnk}")
                else:
                    errors.append(f"Desktop shortcut failed: {msg}")

            if to_start:
                start_lnk = get_start_menu_programs_dir() / f"{safe_filename}.lnk"
                ok, msg = create_windows_shortcut(
                    lnk_path=start_lnk,
                    target_exe=target_exe,
                    arguments=arguments_str,
                    working_dir=working_dir,
                    icon_path=ico_str,
                    description=description,
                    pin_to_start=True
                )
                if ok:
                    created_locations.append(f"Start Menu: {start_lnk}")
                else:
                    errors.append(f"Start Menu shortcut failed: {msg}")

        for loc in created_locations:
            self.launcher_gui.log(f"[Shortcut] Created {loc}")

        if errors and not created_locations:
            QMessageBox.critical(self, "Shortcut Error", "\n".join(errors))
            return

        summary = "Successfully created shortcut(s):\n\n" + "\n".join(f"• {loc}" for loc in created_locations)
        if errors:
            summary += "\n\nWarnings:\n" + "\n".join(errors)
        if to_start:
            if sys.platform == "darwin":
                summary += "\n\nTip: The shortcut is now in your Applications folder and can be launched directly or dragged to the macOS Dock."
            elif sys.platform == "win32":
                summary += "\n\nTip: The shortcut is now in your Start Menu ('All apps' and search). To pin it to your front Start tiles on Windows 11, right-click the shortcut in Start and select 'Pin to Start'."
        QMessageBox.information(self, "Shortcut Created", summary)
        self.accept()


class DirectoriesDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Custom Directories")
        self.setFixedSize(500, 450)
        
        self.config = load_launcher_config()
        self.profiles = self.config.get("profiles", {
            "Default": {
                "base": "minecraft_downloads",
                "assets": "minecraft_downloads/assets",
                "libraries": "minecraft_downloads/libraries",
                "versions": "minecraft_downloads/versions",
                "java": "minecraft_downloads/java",
                "saves": "minecraft_downloads/saves",
                "indexes": "minecraft_downloads/indexes",
                "source": "minecraft_downloads/source"
            }
        })
        self.active_profile = self.config.get("active_profile", "Default")
        if self.active_profile not in self.profiles:
            self.active_profile = list(self.profiles.keys())[0] if self.profiles else "Default"
            if not self.profiles:
                self.profiles["Default"] = {"base": "minecraft_downloads"}
                
        layout = QVBoxLayout(self)
        
        # Profile Selector
        prof_layout = QHBoxLayout()
        prof_layout.addWidget(QLabel("Profile:"))
        self.prof_combo = QComboBox()
        self.prof_combo.addItems(self.profiles.keys())
        self.prof_combo.setCurrentText(self.active_profile)
        self.prof_combo.currentTextChanged.connect(self.load_profile)
        prof_layout.addWidget(self.prof_combo, stretch=1)
        
        add_btn = QPushButton("New")
        add_btn.clicked.connect(self.add_profile)
        del_btn = QPushButton("Delete")
        del_btn.clicked.connect(self.delete_profile)
        prof_layout.addWidget(add_btn)
        prof_layout.addWidget(del_btn)
        layout.addLayout(prof_layout)
        
        layout.addWidget(QLabel("")) # Spacer
        
        self.form_layout = QFormLayout()
        self.inputs = {}
        
        # Base Directory
        base_layout = QHBoxLayout()
        self.base_input = QLineEdit()
        self.base_input.textChanged.connect(self.on_base_changed)
        base_btn = QPushButton("Browse...")
        base_btn.clicked.connect(lambda: self.browse_dir("base", self.base_input))
        base_layout.addWidget(self.base_input)
        base_layout.addWidget(base_btn)
        self.form_layout.addRow("Base (All):", base_layout)
        
        # Subdirectories
        for key in ["assets", "libraries", "versions", "java", "saves", "indexes", "source"]:
            row_layout = QHBoxLayout()
            inp = QLineEdit()
            btn = QPushButton("Browse...")
            btn.clicked.connect(lambda checked=False, k=key, i=inp: self.browse_dir(k, i))
            row_layout.addWidget(inp)
            row_layout.addWidget(btn)
            self.inputs[key] = inp
            self.form_layout.addRow(f"{key.capitalize()}:", row_layout)
            
        layout.addLayout(self.form_layout)
        
        

        
        btn_layout = QHBoxLayout()
        save_btn = QPushButton("Save & Set Active")
        save_btn.clicked.connect(self.accept)
        btn_layout.addStretch()
        btn_layout.addWidget(save_btn)
        
        layout.addLayout(btn_layout)
        
        # Load initially
        self.is_loading = True
        self.load_profile(self.active_profile)
        self.is_loading = False
        
    def load_profile(self, prof_name):
        self.is_loading = True
        dirs = self.profiles.get(prof_name, {})
        self.base_input.setText(dirs.get("base", ""))
        for key, inp in self.inputs.items():
            inp.setText(dirs.get(key, ""))
        self.is_loading = False
        
    def add_profile(self):
        text, ok = QInputDialog.getText(self, "New Profile", "Profile Name:")
        if ok and text and text not in self.profiles:
            # Copy current text values to new profile
            new_dirs = {"base": self.base_input.text()}
            for key, inp in self.inputs.items():
                new_dirs[key] = inp.text()
            self.profiles[text] = new_dirs
            self.prof_combo.addItem(text)
            self.prof_combo.setCurrentText(text)
            
    def delete_profile(self):
        prof = self.prof_combo.currentText()
        if len(self.profiles) <= 1:
            QMessageBox.warning(self, "Warning", "Cannot delete the last profile.")
            return
        reply = QMessageBox.question(self, "Confirm Delete", f"Delete profile '{prof}'?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            del self.profiles[prof]
            self.prof_combo.removeItem(self.prof_combo.currentIndex())
        
    def on_base_changed(self, new_base):
        if self.is_loading: return
        new_base = new_base.strip().replace('\\', '/')
        if not new_base:
            for key, inp in self.inputs.items():
                inp.setText("")
            return
        if not new_base.endswith('/'):
            new_base += '/'
        for key, inp in self.inputs.items():
            inp.setText(f"{new_base}{key}")
            
    def browse_dir(self, key, line_edit):
        folder = QFileDialog.getExistingDirectory(self, f"Select {key.capitalize()} Directory")
        if folder:
            line_edit.setText(folder)
            
    def save_directories(self):
        # Save current inputs to the selected profile
        prof = self.prof_combo.currentText()
        new_dirs = {"base": self.base_input.text()}
        for key, inp in self.inputs.items():
            new_dirs[key] = inp.text()
        self.profiles[prof] = new_dirs
        
        self.config["profiles"] = self.profiles
        self.config["active_profile"] = prof
        save_launcher_config(self.config)


def run_deletion_with_progress(parent_widget, title: str, worker_fn, log_callback=None) -> bool:
    """
    Run a deletion callable `worker_fn(progress_cb, log_cb)` in a background QThread while displaying
    a responsive modal progress dialog (indeterminate bar while indexing, 0-100% bar while deleting).
    """
    dlg = QDialog(parent_widget)
    dlg.setWindowTitle(title)
    dlg.setFixedSize(460, 120)
    dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
    dlg.setWindowFlags(dlg.windowFlags() & ~Qt.WindowType.WindowCloseButtonHint)
    if parent_widget and parent_widget.styleSheet():
        dlg.setStyleSheet(parent_widget.styleSheet())

    vlayout = QVBoxLayout(dlg)
    vlayout.setContentsMargins(20, 20, 20, 20)
    vlayout.setSpacing(12)

    status_lbl = QLabel("Indexing files to delete...")
    status_lbl.setWordWrap(True)
    vlayout.addWidget(status_lbl)

    pbar = QProgressBar()
    pbar.setRange(0, 0)  # Start in indeterminate indexing mode
    vlayout.addWidget(pbar)

    class _DeleteWorker(QtCore.QThread):
        progress_sig = QtCore.pyqtSignal(float, str)
        log_sig = QtCore.pyqtSignal(str)
        done_sig = QtCore.pyqtSignal(bool)

        def run(self_thread):
            try:
                res = worker_fn(self_thread.progress_sig.emit, self_thread.log_sig.emit)
                self_thread.done_sig.emit(bool(res))
            except Exception as e:
                self_thread.log_sig.emit(f"[Error] {e}")
                self_thread.done_sig.emit(False)

    result_holder = [False]
    worker = _DeleteWorker(dlg)

    def _on_progress(pct: float, msg: str):
        status_lbl.setText(msg)
        if pct < 0:
            if pbar.maximum() != 0:
                pbar.setRange(0, 0)
        else:
            if pbar.maximum() == 0:
                pbar.setRange(0, 100)
            pbar.setValue(int(pct))
            pbar.setFormat(f"{pct:.1f}%")

    def _on_log(msg: str):
        if log_callback:
            log_callback(msg)

    def _on_done(ok: bool):
        result_holder[0] = ok
        dlg.accept()

    worker.progress_sig.connect(_on_progress)
    worker.log_sig.connect(_on_log)
    worker.done_sig.connect(_on_done)
    worker.start()
    dlg.exec()
    worker.wait()
    return result_holder[0]


class VersionOptionsDialog(QDialog):
    def __init__(self, current_filters, show_downloaded_only, skip_verification, show_console, remember_version, hide_release_date, dev_mode=False, show_access_token=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Version Options")
        self.setFixedSize(360, 465)
        self.setStyleSheet(parent.styleSheet())
        layout = QVBoxLayout(self)
        
        self.filters = current_filters
        self.show_downloaded_only = show_downloaded_only
        self.skip_verification = skip_verification
        self.show_console = show_console
        self.show_access_token = show_access_token
        self.checkboxes = {}
        
        # Version types
        types = [("release", "Releases"), ("snapshot", "Snapshots"), ("old_beta", "Old Beta"), ("old_alpha", "Old Alpha")]
        for t_id, t_name in types:
            cb = QCheckBox(t_name)
            cb.setChecked(t_id in self.filters)
            layout.addWidget(cb)
            self.checkboxes[t_id] = cb
            
        layout.addSpacing(10)
        
        # Downloaded only
        self.downloaded_cb = QCheckBox("Show only downloaded versions")
        self.downloaded_cb.setChecked(self.show_downloaded_only)
        layout.addWidget(self.downloaded_cb)
        
        # Skip verification
        self.skip_verify_cb = QCheckBox("Skip verification for downloaded versions")
        self.skip_verify_cb.setChecked(self.skip_verification)
        layout.addWidget(self.skip_verify_cb)
        
        # Show Console
        self.show_console_cb = QCheckBox("Show Console Log")
        self.show_console_cb.setChecked(self.show_console)
        layout.addWidget(self.show_console_cb)

        # Show Access Token in Console Log (off by default)
        self.show_access_token_cb = QCheckBox("Show Access Token in Console Log")
        self.show_access_token_cb.setChecked(self.show_access_token)
        layout.addWidget(self.show_access_token_cb)
        
        # Remember Version
        self.remember_cb = QCheckBox("Remember last played version")
        self.remember_cb.setChecked(remember_version)
        layout.addWidget(self.remember_cb)

        # Hide Release Date
        self.hide_date_cb = QCheckBox("Hide release date in version list")
        self.hide_date_cb.setChecked(hide_release_date)
        layout.addWidget(self.hide_date_cb)

        # Source Mode (--source)
        self.dev_mode_cb = QCheckBox("Source Mode (--source: unpack readable assets & source)")
        self.dev_mode_cb.setChecked(dev_mode)
        layout.addWidget(self.dev_mode_cb)

        # Garbage Collect Logs Older Than setting
        config = load_launcher_config()
        log_row = QHBoxLayout()
        self.gc_delete_logs_cb = QCheckBox("Auto-delete logs older than:")
        self.gc_delete_logs_cb.setChecked(config.get("gc_delete_logs", True))
        self.gc_log_age_spin = QSpinBox()
        self.gc_log_age_spin.setRange(0, 9999)
        self.gc_log_age_spin.setValue(int(config.get("gc_log_max_age_value", 7)))
        self.gc_log_age_unit = QComboBox()
        self.gc_log_age_unit.addItems(["Minutes", "Hours", "Days"])
        self.gc_log_age_unit.setCurrentText(config.get("gc_log_max_age_unit", "Days"))
        self.gc_delete_logs_cb.toggled.connect(self.gc_log_age_spin.setEnabled)
        self.gc_delete_logs_cb.toggled.connect(self.gc_log_age_unit.setEnabled)
        self.gc_log_age_spin.setEnabled(self.gc_delete_logs_cb.isChecked())
        self.gc_log_age_unit.setEnabled(self.gc_delete_logs_cb.isChecked())
        log_row.addWidget(self.gc_delete_logs_cb)
        log_row.addWidget(self.gc_log_age_spin)
        log_row.addWidget(self.gc_log_age_unit)
        layout.addLayout(log_row)
        
        btn_layout = QHBoxLayout()
        
        any_game_running = bool(parent and len(getattr(parent, 'running_instances', [])) > 0)

        # Delete All Downloads button
        if has_any_downloads():
            self.delete_all_btn = QPushButton("Delete All Downloads")
            self.delete_all_btn.setStyleSheet("color: red; font-weight: bold;")
            self.delete_all_btn.clicked.connect(self.on_delete_all_clicked)
            if any_game_running:
                self.delete_all_btn.setEnabled(False)
                self.delete_all_btn.setToolTip("Close all running Minecraft instances before deleting downloads.")
            btn_layout.addWidget(self.delete_all_btn)

        # Delete Source Folder button
        if has_any_source():
            self.delete_src_btn = QPushButton("Delete Source Folder")
            self.delete_src_btn.setStyleSheet("color: #cc4400; font-weight: bold;")
            self.delete_src_btn.clicked.connect(self.on_delete_source_clicked)
            if any_game_running:
                self.delete_src_btn.setEnabled(False)
                self.delete_src_btn.setToolTip("Close all running Minecraft instances before deleting source/dev assets.")
            btn_layout.addWidget(self.delete_src_btn)
            
        ok_btn = QPushButton("Save")
        ok_btn.clicked.connect(self.accept)
        btn_layout.addStretch()
        btn_layout.addWidget(ok_btn)
        layout.addLayout(btn_layout)
        
    def on_delete_all_clicked(self):
        reply = QMessageBox.question(self, 'Confirm Delete All', 
                                     "Are you sure you want to delete ALL downloaded Minecraft versions, assets, and libraries?\n\nThis will free up space but you will need to re-download everything to play.\n\n(This will NOT delete your saves, worlds, source folder, or configurations)",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                     QMessageBox.StandardButton.No)
        
        if reply == QMessageBox.StandardButton.Yes:
            self.parent().log("")
            self.parent().log("=== Starting Deletion ===")
            ok = run_deletion_with_progress(
                self,
                "Deleting All Downloads...",
                lambda prog_cb, log_cb: delete_all_downloads(progress_callback=prog_cb, log_callback=log_cb),
                log_callback=self.parent().log
            )
            if ok:
                self.parent().log("[System] Successfully deleted all downloaded files.")
                self.parent().status_label.setText("Deletion complete.")
                QMessageBox.information(self, "Success", "All downloads have been successfully deleted.")
                self.accept()
            else:
                self.parent().log("[Error] Failed to delete some files.")
                QMessageBox.warning(self, "Error", "Failed to delete some files. They may be in use.")
        
    def get_selected_filters(self):
        return [t_id for t_id, cb in self.checkboxes.items() if cb.isChecked()]
        
    def get_downloaded_only(self):
        return self.downloaded_cb.isChecked()
        
    
    def get_hide_release_date(self):
        return self.hide_date_cb.isChecked()

    def get_skip_verification(self):
        return self.skip_verify_cb.isChecked()
        
    def get_show_console(self):
        return self.show_console_cb.isChecked()

    def get_show_access_token(self):
        return self.show_access_token_cb.isChecked()

    def get_remember_version(self):
        return self.remember_cb.isChecked()

    def on_delete_source_clicked(self):
        reply = QMessageBox.question(
            self,
            'Confirm Delete Source Folder',
            f"Are you sure you want to delete the extracted source code folder ({get_dir('source')}) and unpacked dev-mode assets in {get_dir('assets')} (e.g., icons, minecraft, etc.)?\n\n(Normal cached game assets in assets/objects and assets/indexes will NOT be deleted.)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.parent().log("[System] Deleting extracted source folder and unpacked dev-mode assets...")
            ok = run_deletion_with_progress(
                self,
                "Deleting Source Folder & Dev Assets...",
                lambda prog_cb, log_cb: delete_source_folder(progress_callback=prog_cb, log_callback=log_cb),
                log_callback=self.parent().log
            )
            if ok:
                self.parent().log("[System] Successfully deleted source folder and unpacked dev-mode assets.")
                QMessageBox.information(self, "Success", "The source folder and unpacked dev-mode assets have been deleted.")
                if hasattr(self, 'delete_src_btn'):
                    self.delete_src_btn.setVisible(False)
            else:
                self.parent().log("[Error] Failed to delete source folder.")
                QMessageBox.warning(self, "Error", "Failed to delete some files in the source folder.")

    def get_dev_mode(self):
        return self.dev_mode_cb.isChecked()

    def get_log_gc_settings(self):
        return (
            self.gc_delete_logs_cb.isChecked(),
            self.gc_log_age_spin.value(),
            self.gc_log_age_unit.currentText()
        )

from PyQt6.QtGui import QPainter, QColor, QPixmap, QImage, QMouseEvent
from PyQt6 import QtCore
from PyQt6.QtCore import Qt, QRect
from PyQt6.QtWebEngineCore import QWebEnginePage

class CustomWebPage(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, message, lineNumber, sourceID):
        if "THREE.WebGLProgram" in message or "warning X4713" in message or "Sample Bias" in message:
            return
        # print(f"JS Console [{level}]: {message} (line {lineNumber})")
        super().javaScriptConsoleMessage(level, message, lineNumber, sourceID)


def get_animation_js(anim_type):
    inner = ""
    if anim_type == "idle":
        inner = """
            window.skinViewer.animation = new skinview3d.IdleAnimation();
            window.skinViewer.playerObject.backEquipment = window.hasCapeLoaded ? 'cape' : null;
            if (!window.hasCapeLoaded && window.skinViewer.playerObject.cape) window.skinViewer.playerObject.cape.visible = false;
            window.skinViewer.playerObject.rotation.x = 0;
            window.skinViewer.playerObject.rotation.y = 0;
            window.skinViewer.playerObject.position.y = 0;
        """
    elif anim_type == "walk":
        inner = """
            window.skinViewer.animation = new skinview3d.WalkingAnimation();
            window.skinViewer.playerObject.backEquipment = window.hasCapeLoaded ? 'cape' : null;
            if (!window.hasCapeLoaded && window.skinViewer.playerObject.cape) window.skinViewer.playerObject.cape.visible = false;
            window.skinViewer.playerObject.rotation.x = 0;
            window.skinViewer.playerObject.rotation.y = 0;
            window.skinViewer.playerObject.position.y = 0;
        """
    elif anim_type == "swim":
        inner = """
            window.skinViewer.playerObject.backEquipment = window.hasCapeLoaded ? 'cape' : null;
            if (!window.hasCapeLoaded && window.skinViewer.playerObject.cape) window.skinViewer.playerObject.cape.visible = false;
            window.skinViewer.animation = new skinview3d.FunctionAnimation(function(player, progress, delta) {
                try {
                    player.rotation.x = Math.PI / 2;
                    player.rotation.y = 0;
                    player.rotation.z = 0;
                    let phase = (progress * 3) % (Math.PI * 2);
                    let zOffset, xOffset;
                    if (phase < Math.PI) {
                        let p = phase / Math.PI;
                        zOffset = Math.sin(p * Math.PI / 2) * 1.5;
                        xOffset = Math.sin(p * Math.PI / 2) * 0.4;
                    } else {
                        let p = (phase - Math.PI) / Math.PI;
                        zOffset = Math.cos(p * Math.PI / 2) * 1.5;
                        xOffset = Math.sin(p * Math.PI) * 1.2 + Math.cos(p * Math.PI / 2) * 0.4;
                    }
                    player.skin.leftArm.rotation.z = zOffset;
                    player.skin.rightArm.rotation.z = -zOffset;
                    player.skin.leftArm.rotation.x = Math.PI + xOffset;
                    player.skin.rightArm.rotation.x = Math.PI + xOffset;
                    player.skin.leftLeg.rotation.x = 0.6 * Math.sin(progress * 6);
                    player.skin.rightLeg.rotation.x = 0.6 * Math.sin(progress * 6 + Math.PI);
                    player.skin.leftLeg.rotation.z = 0;
                    player.skin.rightLeg.rotation.z = 0;
                    player.skin.head.rotation.x = -Math.PI / 4;
                    player.skin.head.rotation.y = 0;
                } catch(e) { console.error("Swim err: ", e); }
            });
        """
    elif anim_type == "fly":
        inner = """
            window.skinViewer.playerObject.backEquipment = 'elytra';
            window.skinViewer.animation = new skinview3d.FunctionAnimation(function(player, progress, delta) {
                try {
                    player.rotation.x = Math.PI / 2;
                    player.rotation.y = 0;
                    player.rotation.z = 0;
                    player.elytra.leftWing.rotation.x = 0.3;
                    player.elytra.leftWing.rotation.z = 1.2;
                    player.elytra.rightWing.rotation.x = 0.3;
                    player.elytra.rightWing.rotation.z = -1.2;
                    let t = progress * 8;
                    player.skin.leftArm.rotation.x = 0.2 * Math.sin(t + Math.PI);
                    player.skin.rightArm.rotation.x = 0.2 * Math.sin(t);
                    player.skin.leftArm.rotation.z = 0;
                    player.skin.rightArm.rotation.z = 0;
                    player.skin.leftLeg.rotation.x = 0.2 * Math.sin(t);
                    player.skin.rightLeg.rotation.x = 0.2 * Math.sin(t + Math.PI);
                    player.skin.leftLeg.rotation.z = 0;
                    player.skin.rightLeg.rotation.z = 0;
                    player.skin.head.rotation.x = -Math.PI / 4;
                    player.skin.head.rotation.y = 0;
                } catch(e) { console.error("Fly err: ", e); }
            });
        """
    else:
        return ""

    template = """
        (function() {
            function applyAnim() {
                if (window.skinViewer && window.skinViewer.playerObject) {
                    {inner_code}
                } else {
                    setTimeout(applyAnim, 50);
                }
            }
            applyAnim();
        })();
    """
    return template.replace("{inner_code}", inner)

class ChangeSkinDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Change Skin / Cape")
        self.setFixedSize(650, 520)
        
        # Local paths for preview
        self.preview_skin_path = None
        self.original_skin_path = None
        self.is_slim = False
        
        self.auth_data = parent.auth_data if hasattr(parent, 'auth_data') else {}
        self.owned_capes = []
        self.active_cape_id = None
        
        # Fetch capes
        access_token = self.auth_data.get('mc_access_token')
        if access_token:
            import requests
            try:
                resp = requests.get("https://api.minecraftservices.com/minecraft/profile", 
                                    headers={"Authorization": f"Bearer {access_token}"})
                if resp.status_code == 200:
                    profile = resp.json()
                    self.owned_capes = profile.get('capes', [])
                    for cape in self.owned_capes:
                        if cape.get('state') == 'ACTIVE':
                            self.active_cape_id = cape.get('id')
            except Exception as e:
                print(f"Failed to fetch capes: {e}")
        
        import shutil
        cache_dir = f".cache/{get_active_profile_id()}"
        os.makedirs(cache_dir, exist_ok=True)
        if os.path.exists(os.path.join(cache_dir, "skin.png")):
            shutil.copy(os.path.join(cache_dir, "skin.png"), os.path.join(cache_dir, "preview_skin.png"))
            self.preview_skin_path = "preview_skin.png"
            
        self.preview_cape_path = None
        if os.path.exists(os.path.join(cache_dir, "cape.png")):
            shutil.copy(os.path.join(cache_dir, "cape.png"), os.path.join(cache_dir, "preview_cape.png"))
            self.preview_cape_path = "preview_cape.png"
            
        # Background: 3D preview overlaying the whole window
        self.web_view = QWebEngineView(self)
        self.web_view.setPage(CustomWebPage(self.web_view))
        self.web_view.setGeometry(0, 0, 350, 520)
        self.web_view.page().setBackgroundColor(Qt.GlobalColor.transparent)
        
        # Right side: Options container overlay
        self.options_container = QWidget(self)
        self.options_container.setGeometry(350, 20, 280, 480)
        
        right_layout = QVBoxLayout(self.options_container)
        right_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        
        # Skin Row
        skin_btn = QPushButton("Browse Skin (.png)")
        skin_btn.clicked.connect(self.browse_skin)
        self.skin_label = QLabel("Current Skin loaded" if self.preview_skin_path else "No file selected")
        right_layout.addWidget(skin_btn)
        right_layout.addWidget(self.skin_label)
        right_layout.addSpacing(15)
        
        # Cape Selection
        self.cape_combo = QComboBox()
        self.cape_combo.addItem("No Cape", None)
        for cape in self.owned_capes:
            self.cape_combo.addItem(cape.get('alias', 'Unknown Cape'), cape.get('id'))
            
        if self.active_cape_id:
            index = self.cape_combo.findData(self.active_cape_id)
            if index >= 0:
                self.cape_combo.setCurrentIndex(index)
                
        self.cape_combo.currentIndexChanged.connect(self.change_cape)
                
        right_layout.addWidget(QLabel("Cape Selection:"))
        right_layout.addWidget(self.cape_combo)
        right_layout.addSpacing(15)
        
        # Model Type
        self.model_combo = QComboBox()
        self.model_combo.addItems(["Classic", "Slim"])
        self.model_combo.currentTextChanged.connect(self.change_model)
        right_layout.addWidget(QLabel("Model Type:"))
        right_layout.addWidget(self.model_combo)
        right_layout.addSpacing(15)
        
        # Animation Controls
        anim_group = QGroupBox("Animation")
        anim_layout = QVBoxLayout()
        
        self.spin_cb = QCheckBox("Toggle Spinning")
        self.spin_cb.setChecked(True)
        self.spin_cb.toggled.connect(self.toggle_spinning)
        anim_layout.addWidget(self.spin_cb)
        
        self.anim_idle = QRadioButton("Idle")
        self.anim_walk = QRadioButton("Walking")
        self.anim_swim = QRadioButton("Swimming")
        self.anim_fly = QRadioButton("Flying (with elytra on)")
        
        self.anim_walk.setChecked(True)
        
        self.anim_idle.toggled.connect(self.change_animation)
        self.anim_walk.toggled.connect(self.change_animation)
        self.anim_swim.toggled.connect(self.change_animation)
        self.anim_fly.toggled.connect(self.change_animation)
        
        anim_layout.addWidget(self.anim_idle)
        anim_layout.addWidget(self.anim_walk)
        anim_layout.addWidget(self.anim_swim)
        anim_layout.addWidget(self.anim_fly)
        
        anim_layout.addSpacing(10)
        self.apply_to_main_cb = QCheckBox("Apply to main page 3d model after saved")
        self.apply_to_main_cb.setChecked(True)
        anim_layout.addWidget(self.apply_to_main_cb)
        
        anim_group.setLayout(anim_layout)
        right_layout.addWidget(anim_group)
        
        right_layout.addStretch()
        
        # Bottom Buttons
        btn_layout = QHBoxLayout()
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self.save_clicked)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(save_btn)
        right_layout.addLayout(btn_layout)
        
        
        self.update_preview()
        
    def browse_skin(self):
        file_name, _ = QFileDialog.getOpenFileName(self, "Select Skin", "", "Images (*.png)")
        if file_name:
            import shutil, time
            cache_dir = f".cache/{get_active_profile_id()}"
            self.preview_skin_path = f"preview_skin_{int(time.time())}.png"
            self.original_skin_path = file_name
            shutil.copy(file_name, os.path.join(cache_dir, self.preview_skin_path))
            self.skin_label.setText(os.path.basename(file_name))
            self.update_preview()
            
    def toggle_spinning(self, checked):
        spin_val = 'true' if checked else 'false'
        js = f"""
        (function() {{
            function applySpin() {{
                if (window.skinViewer && window.skinViewer.playerObject) {{
                    window.skinViewer.autoRotate = {spin_val};
                }} else {{
                    setTimeout(applySpin, 50);
                }}
            }}
            applySpin();
        }})();
        """
        self.web_view.page().runJavaScript(js)
        
    def change_animation(self, checked=None):
        print(f"change_animation called, idle={self.anim_idle.isChecked()}, walk={self.anim_walk.isChecked()}, swim={self.anim_swim.isChecked()}, fly={self.anim_fly.isChecked()}")
        
        anim_type = "idle"
        if self.anim_idle.isChecked(): anim_type = "idle"
        elif self.anim_walk.isChecked(): anim_type = "walk"
        elif self.anim_swim.isChecked(): anim_type = "swim"
        elif self.anim_fly.isChecked(): anim_type = "fly"
        
        js = get_animation_js(anim_type)
            
        if js:
            # Test if runJavaScript works at all from here
            self.web_view.page().runJavaScript("console.log('JS TEST FROM change_animation');")
            # Run the actual animation
            wrapped_js = f"try {{ {js} console.log('Animation applied successfully'); }} catch(err) {{ console.error('Animation error: ' + err.message); }}"
            print(f"Running JS (length={len(wrapped_js)})")
            self.web_view.page().runJavaScript(wrapped_js)
            
    def change_cape(self, index):
        cape_id = self.cape_combo.currentData()
        if not cape_id:
            self.preview_cape_path = None
        else:
            cape_url = None
            for cape in self.owned_capes:
                if cape.get('id') == cape_id:
                    cape_url = cape.get('url')
                    break
            
            if cape_url:
                import requests
                import os
                try:
                    r = requests.get(cape_url)
                    if r.status_code == 200:
                        import time
                        cache_dir = f".cache/{get_active_profile_id()}"
                        self.preview_cape_path = f"preview_cape_{int(time.time())}.png"
                        with open(os.path.join(cache_dir, self.preview_cape_path), 'wb') as f:
                            f.write(r.content)
                except:
                    pass
        self.update_preview()
            
    def change_model(self, text):
        self.is_slim = (text == "Slim")
        self.update_preview()
        
    def update_preview(self):
        s_path = self.preview_skin_path if self.preview_skin_path else ""
        c_path = self.preview_cape_path if self.preview_cape_path else ""
        
        import time
        # removed ?v cache busting for local files
        
        model = "slim" if getattr(self, "is_slim", False) else "default"
        back_eq = "elytra" if hasattr(self, "anim_fly") and self.anim_fly.isChecked() else "cape"
        
        if getattr(self, "preview_loaded", False):
            js = f"""
            if (window.skinViewer) {{
                if ("{s_path}") window.skinViewer.loadSkin("{s_path}", {{ model: "{model}" }});
                if ("{c_path}") {{
                    window.hasCapeLoaded = true;
                    window.skinViewer.loadCape("{c_path}", {{ backEquipment: "{back_eq}" }});
                }} else {{
                    window.hasCapeLoaded = false;
                    window.skinViewer.loadCape(null);
                    if (window.skinViewer.playerObject) {{
                        window.skinViewer.playerObject.backEquipment = null;
                        if (window.skinViewer.playerObject.cape) window.skinViewer.playerObject.cape.visible = false;
                    }}
                }}
            }}
            """
            self.web_view.page().runJavaScript(js)
            # Re-apply animation state
            self.change_animation()
            self.toggle_spinning(self.spin_cb.isChecked())
        else:
            from utils import get_active_profile_id
            base_url = QUrl.fromLocalFile(str(Path(f".cache/{get_active_profile_id()}/skin_viewer.html").absolute()))
            query = QUrlQuery()
            if s_path: query.addQueryItem("skin", s_path)
            if c_path: query.addQueryItem("cape", c_path)
            query.addQueryItem("model", model)
            query.addQueryItem("back", back_eq)
            query.addQueryItem("offset_x", "150")
            base_url.setQuery(query)
            
            self.web_view.load(base_url)
            self.preview_loaded = True
            # Wait for page to actually load before running JS
            self.web_view.loadFinished.connect(self._on_preview_loaded)
            
    def _on_preview_loaded(self, ok):
        if ok:
            self.change_animation()
            self.toggle_spinning(self.spin_cb.isChecked())
        
    def save_clicked(self):
        access_token = self.auth_data.get('mc_access_token')
        if not access_token:
            QMessageBox.critical(self, "Error", "Minecraft Access Token not found. Please re-login.")
            if self.parent() and hasattr(self.parent().window(), "login_btn"): self.parent().window().login_btn.setVisible(True)
            return

        from PyQt6.QtWidgets import QProgressDialog
        progress = QProgressDialog("Updating player profile...", "Cancel", 0, 100, self)
        progress.setWindowTitle("Saving")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setCancelButton(None)
        progress.setValue(10)
        progress.show()
        QApplication.processEvents()

        import requests
        headers = {"Authorization": f"Bearer {access_token}"}

        messages = []
        has_errors = False

        # Handle Skin
        progress.setValue(30)
        QApplication.processEvents()
        if self.original_skin_path:
            progress.setLabelText("Uploading new skin...")
            QApplication.processEvents()
            url = "https://api.minecraftservices.com/minecraft/profile/skins"
            variant = "slim" if self.is_slim else "classic"
            data = {"variant": variant}
            try:
                with open(self.original_skin_path, 'rb') as f:
                    files = {"file": (os.path.basename(self.original_skin_path), f, "image/png")}
                    response = requests.post(url, headers=headers, data=data, files=files)
                if response.status_code == 200:
                    messages.append("Skin successfully updated.")
                else:
                    has_errors = True
                    messages.append(f"Skin Upload Error ({response.status_code}): {response.text}")
                    if response.status_code == 401:
                        if self.parent() and hasattr(self.parent().window(), "login_btn"): self.parent().window().login_btn.setVisible(True)
            except Exception as e:
                has_errors = True
                messages.append(f"Skin Upload Exception: {str(e)}")

        # Handle Cape
        progress.setValue(60)
        QApplication.processEvents()
        selected_cape_id = self.cape_combo.currentData()
        if selected_cape_id != self.active_cape_id:
            progress.setLabelText("Updating cape selection...")
            QApplication.processEvents()
            try:
                if selected_cape_id is None:
                    c_resp = requests.delete("https://api.minecraftservices.com/minecraft/profile/capes/active", headers=headers)
                else:
                    c_resp = requests.put("https://api.minecraftservices.com/minecraft/profile/capes/active", headers=headers, json={"capeId": selected_cape_id})

                if c_resp.status_code in (200, 204):
                    messages.append("Cape successfully updated.")
                else:
                    has_errors = True
                    messages.append(f"Cape Update Error ({c_resp.status_code}): {c_resp.text}")
                    if c_resp.status_code == 401:
                        if self.parent() and hasattr(self.parent().window(), "login_btn"): self.parent().window().login_btn.setVisible(True)
            except Exception as e:
                has_errors = True
                messages.append(f"Cape Update Exception: {str(e)}")

        progress.setValue(90)
        QApplication.processEvents()

        if self.apply_to_main_cb.isChecked():
            config = load_launcher_config()
            if self.anim_idle.isChecked(): config["main_animation"] = "idle"
            elif self.anim_walk.isChecked(): config["main_animation"] = "walk"
            elif self.anim_swim.isChecked(): config["main_animation"] = "swim"
            elif self.anim_fly.isChecked(): config["main_animation"] = "fly"
            config["main_spin"] = self.spin_cb.isChecked()
            save_launcher_config(config)

        progress.setValue(100)
        QApplication.processEvents()

        if not self.original_skin_path and selected_cape_id == self.active_cape_id:
            self.accept()
            return

        if has_errors:
            QMessageBox.critical(self, "Update Failed", "\n".join(messages))
        elif messages:
            QMessageBox.information(self, "Success", "\n".join(messages))
            self.accept()
        else:
            self.accept()

class LocatorBarWidget(QWidget):
    """
    Interactive Minecraft Locator Bar widget using all vanilla sprites and .mcmeta definitions:
    - locator_bar_background.png (12x5 nine-slice with 5px left/right border caps)
    - locator_bar_arrow_up.png & locator_bar_arrow_down.png (animated 7x5 frames)
    - locator_bar_dot/default_0.png : Square (0-179 blocks)
    - locator_bar_dot/default_1.png : Circle (179-230 blocks)
    - locator_bar_dot/default_2.png : Small square (230-281 blocks)
    - locator_bar_dot/default_3.png : Small circle (>281 blocks)
    - locator_bar_dot/bowtie.png    : Bowtie (<64 blocks when Bowtie style is used, or pinned)
    """
    ICON_MODES = [
        ("auto", "Auto (Distance from Cursor)"),
        ("default_0", "Square (0-179 blocks)"),
        ("default_1", "Circle (179-230 blocks)"),
        ("default_2", "Small square (230-281 blocks)"),
        ("default_3", "Small circle (>281 blocks)"),
        ("bowtie", "Bowtie (Bowtie Style)"),
    ]

    def __init__(self, color_hex, parent=None, status_callback=None):
        super().__init__(parent)
        self.setFixedHeight(54)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.status_callback = status_callback
        self.bg_scale = 3
        self.dot_x = -1
        self.mouse_y = 0
        self.pitch_dir = "none"  # "none", "up", "down"
        self.distance_blocks = 0
        self.distance_pct = 0
        self.scroll_distance_offset = 0
        self.mode_index = 0
        self.color = QColor(color_hex)

        self.track_timer = QTimer(self)
        self.track_timer.timeout.connect(self._track_mouse)
        self.track_timer.start(16)

        self.anim_timer = QTimer(self)
        self.anim_timer.timeout.connect(self.update)
        self.anim_timer.start(50)

        import os
        import json
        bg_path = get_image_path("locator_bar/locator_bar_background.png")
        arrow_up_path = get_image_path("locator_bar/locator_bar_arrow_up.png")
        arrow_down_path = get_image_path("locator_bar/locator_bar_arrow_down.png")

        self.bg_pixmap = QPixmap(bg_path)

        self.bg_left = 5
        self.bg_right = 5
        if os.path.exists(bg_path + ".mcmeta"):
            try:
                with open(bg_path + ".mcmeta", "r") as f:
                    meta = json.load(f)
                    border = meta.get("gui", {}).get("scaling", {}).get("border", {})
                    self.bg_left = border.get("left", 5)
                    self.bg_right = border.get("right", 5)
            except Exception:
                pass

        def load_arrow_meta(path):
            frames = [{"index": 0, "time": 10}, {"index": 1, "time": 4}]
            frame_height = 5
            if os.path.exists(path + ".mcmeta"):
                try:
                    with open(path + ".mcmeta", "r") as f:
                        meta = json.load(f)
                        anim = meta.get("animation", {})
                        if "frames" in anim:
                            frames = anim["frames"]
                        if "height" in anim:
                            frame_height = anim["height"]
                except Exception:
                    pass
            # time is in ticks, 1 tick = 50ms
            total_duration = sum([f.get("time", 1) * 50 for f in frames])
            return frames, frame_height, total_duration

        self.arrow_up_meta = load_arrow_meta(arrow_up_path)
        self.arrow_down_meta = load_arrow_meta(arrow_down_path)

        # Helper to load and tint images using exact Minecraft multiplicative tinting
        def load_tinted_pixmap(path, is_arrow=False):
            raw_img = QImage(path)
            if raw_img.isNull():
                return QPixmap()

            if not is_arrow:
                raw_img = raw_img.convertToFormat(QImage.Format.Format_ARGB32)
                for y in range(raw_img.height()):
                    for x in range(raw_img.width()):
                        pixel = raw_img.pixelColor(x, y)
                        if pixel.alpha() > 0:
                            tinted = QColor(
                                (pixel.red() * self.color.red()) // 255,
                                (pixel.green() * self.color.green()) // 255,
                                (pixel.blue() * self.color.blue()) // 255,
                                pixel.alpha()
                            )
                            raw_img.setPixelColor(x, y, tinted)
            scale_factor = self.bg_scale
            return QPixmap.fromImage(raw_img).scaled(
                raw_img.width() * scale_factor,
                raw_img.height() * scale_factor,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation
            )

        # Load all 5 waypoint dot sprites from locator_bar/locator_bar_dot/
        self.dot_pixmaps = {}
        for sprite_name in ("default_0", "default_1", "default_2", "default_3", "bowtie"):
            sprite_path = get_image_path(f"locator_bar/locator_bar_dot/{sprite_name}.png")
            self.dot_pixmaps[sprite_name] = load_tinted_pixmap(sprite_path)

        self.dot_pixmap = self.dot_pixmaps.get("default_0", QPixmap())
        self.arrow_up_pixmap = load_tinted_pixmap(arrow_up_path, is_arrow=True)
        self.arrow_down_pixmap = load_tinted_pixmap(arrow_down_path, is_arrow=True)
        self._last_status_str = ""
        self._update_tooltip_and_status()

    def _get_sprite_for_distance(self, dist_blocks: int) -> tuple[str, str]:
        """
        Return (sprite_key, description) based on Minecraft's Locator Bar distance thresholds:
        - Square       (default_0): 0-179 blocks
        - Circle       (default_1): 179-230 blocks
        - Small square (default_2): 230-281 blocks
        - Small circle (default_3): >281 blocks
        """
        mode_key, _ = self.ICON_MODES[self.mode_index]
        if mode_key == "default_0":
            return "default_0", "Square (0-179 blocks)"
        elif mode_key == "default_1":
            return "default_1", "Circle (179-230 blocks)"
        elif mode_key == "default_2":
            return "default_2", "Small square (230-281 blocks)"
        elif mode_key == "default_3":
            return "default_3", "Small circle (>281 blocks)"
        elif mode_key == "bowtie":
            if dist_blocks < 64:
                return "bowtie", f"Bowtie ({dist_blocks}m, <64 blocks)"
            elif dist_blocks < 179:
                return "default_0", f"Square ({dist_blocks}m, 64-179 blocks)"
            elif dist_blocks < 230:
                return "default_1", f"Circle ({dist_blocks}m, 179-230 blocks)"
            elif dist_blocks <= 281:
                return "default_2", f"Small square ({dist_blocks}m, 230-281 blocks)"
            else:
                return "default_3", f"Small circle ({dist_blocks}m, >281 blocks)"

        # Auto distance mode (percentage of distance to the top/bottom edge of the window)
        pct_str = f"{self.distance_pct}%"
        if dist_blocks < 179:
            return "default_0", f"Square ({dist_blocks} blocks, {pct_str} | 0-179)"
        elif dist_blocks < 230:
            return "default_1", f"Circle ({dist_blocks} blocks, {pct_str} | 179-230)"
        elif dist_blocks <= 281:
            return "default_2", f"Small square ({dist_blocks} blocks, {pct_str} | 230-281)"
        else:
            return "default_3", f"Small circle ({dist_blocks} blocks, {pct_str} | >281)"

    def _update_tooltip_and_status(self):
        _, desc = self._get_sprite_for_distance(self.distance_blocks)
        mode_key, mode_label = self.ICON_MODES[self.mode_index]
        if mode_key == "auto":
            status_str = f"Waypoint: {desc}"
        else:
            status_str = f"Waypoint: {desc} [Pinned - Click bar to cycle]"
        if status_str != self._last_status_str:
            self._last_status_str = status_str
            self.setToolTip(
                f"{status_str}\n"
                "• Move mouse away from bar or scroll wheel to change distance:\n"
                "  Square (0-179) | Circle (179-230) | Small square (230-281) | Small circle (>281)\n"
                "• Click the Locator Bar to cycle / pin icon styles (including Bowtie)"
            )
            if self.status_callback:
                try:
                    self.status_callback(status_str)
                except Exception:
                    pass

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self.mode_index = (self.mode_index + 1) % len(self.ICON_MODES)
            self._update_tooltip_and_status()
            self.update()
        elif event.button() == Qt.MouseButton.RightButton:
            self.mode_index = 0
            self.scroll_distance_offset = 0
            self._update_tooltip_and_status()
            self.update()
        super().mousePressEvent(event)

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta != 0:
            step = 25 if delta > 0 else -25
            self.scroll_distance_offset = max(0, min(350, self.scroll_distance_offset + step))
            self._track_mouse(force_update=True)
            event.accept()
            return
        super().wheelEvent(event)

    def resizeEvent(self, event):
        if self.dot_x < 0:
            self.dot_x = self.width() // 2
        super().resizeEvent(event)

    def _track_mouse(self, force_update: bool = False):
        global_pos = QCursor.pos()
        win = self.window()
        in_window = False
        if win and win.isVisible():
            widget = QApplication.widgetAt(global_pos)
            if widget and widget.window() == win:
                in_window = True
            elif win.frameGeometry().contains(global_pos):
                in_window = True

        old_state = (self.dot_x, self.pitch_dir, self.distance_blocks, self.distance_pct, self.mode_index)

        bar_h = 5 * self.bg_scale
        bar_top = (self.height() - bar_h) // 2
        bar_bottom = bar_top + bar_h
        bar_center_y = self.height() / 2.0

        # Max distance in Minecraft's WaypointStyle is DEFAULT_FAR_DISTANCE = 332 blocks (100% at window edge)
        max_blocks = 332

        if in_window and win:
            local_pos = self.mapFromGlobal(global_pos)
            self.dot_x = max(0, min(self.width(), local_pos.x()))
            self.mouse_y = local_pos.y()

            win_top_local = self.mapFromGlobal(win.mapToGlobal(QtCore.QPoint(0, 0))).y()
            win_bottom_local = self.mapFromGlobal(win.mapToGlobal(QtCore.QPoint(0, win.height()))).y()

            # Pitch direction: show Up/Down arrows only when cursor is vertically outside the waypoint dot area
            dot_margin = 2 * self.bg_scale
            up_start_y = bar_top - dot_margin
            down_start_y = bar_bottom + dot_margin

            if self.mouse_y < up_start_y:
                self.pitch_dir = "up"
                outside_px = float(up_start_y - self.mouse_y)
                max_up_px = max(1.0, float(up_start_y - win_top_local))
                pct = max(0.0, min(1.0, outside_px / max_up_px))
            elif self.mouse_y > down_start_y:
                self.pitch_dir = "down"
                outside_px = float(self.mouse_y - down_start_y)
                max_down_px = max(1.0, float(win_bottom_local - down_start_y))
                pct = max(0.0, min(1.0, outside_px / max_down_px))
            else:
                self.pitch_dir = "none"
                pct = 0.0

            self.distance_blocks = int(round(pct * max_blocks)) + self.scroll_distance_offset
            self.distance_pct = min(100, int(round((self.distance_blocks / max_blocks) * 100)))
        else:
            # Keep waypoint dot centered on the bar when cursor is outside the launcher window
            self.dot_x = self.width() // 2 if self.width() > 0 else 0
            self.mouse_y = int(bar_center_y)
            self.pitch_dir = "none"
            self.distance_blocks = self.scroll_distance_offset
            self.distance_pct = min(100, int(round((self.distance_blocks / max_blocks) * 100)))

        if force_update or (self.dot_x, self.pitch_dir, self.distance_blocks, self.distance_pct, self.mode_index) != old_state:
            self._update_tooltip_and_status()
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        bg_scale = self.bg_scale
        h = 5 * bg_scale
        y_offset = (self.height() - h) // 2

        if not self.bg_pixmap.isNull():
            scaled_bg = self.bg_pixmap.scaled(
                self.bg_pixmap.width() * bg_scale,
                self.bg_pixmap.height() * bg_scale,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.FastTransformation
            )

            h = scaled_bg.height()
            y_offset = (self.height() - h) // 2

            # Use the full 5-pixel nine-slice border caps defined in locator_bar_background.png.mcmeta
            left_cap_w = self.bg_left * bg_scale
            right_cap_w = self.bg_right * bg_scale

            # Left cap (x = 0..4 in 12x5 sprite, including the green gradient fade)
            painter.drawPixmap(0, y_offset, scaled_bg.copy(0, 0, left_cap_w, h))

            # Center slice (x = 5..6 in 12x5 sprite)
            center_src_x = self.bg_left * bg_scale
            center_src_w = max(1, (self.bg_pixmap.width() - self.bg_left - self.bg_right)) * bg_scale
            center_rect = QRect(left_cap_w, y_offset, max(0, self.width() - left_cap_w - right_cap_w), h)
            center_pixmap = scaled_bg.copy(center_src_x, 0, center_src_w, h)
            painter.drawPixmap(center_rect, center_pixmap)

            # Right cap (x = 7..11 in 12x5 sprite, including the green gradient fade)
            painter.drawPixmap(
                self.width() - right_cap_w,
                y_offset,
                scaled_bg.copy(scaled_bg.width() - right_cap_w, 0, right_cap_w, h)
            )

        sprite_key, _ = self._get_sprite_for_distance(self.distance_blocks)
        active_dot_pixmap = self.dot_pixmaps.get(sprite_key, self.dot_pixmap)

        dot_x = self.dot_x if self.dot_x >= 0 else (self.width() // 2)
        if not active_dot_pixmap.isNull() and self.width() > 0:
            dot_w = active_dot_pixmap.width()
            dot_h = active_dot_pixmap.height()
            # In Minecraft LocatorBar.class, the 9x9 dot is clamped within the 182px bar (182 - 9 = 173 range)
            # and drawn at y = top - 2 (which centers the 9px dot over the 5px bar)
            draw_x = max(0, min(self.width() - dot_w, int(dot_x - dot_w / 2)))
            draw_y = y_offset - 2 * bg_scale
            painter.drawPixmap(draw_x, draw_y, active_dot_pixmap)

            # Draw animated up/down pitch arrow if cursor is above or below the bar
            # In Minecraft LocatorBar.class: arrow is 7x5, drawn at x = dot_x + 1, y = top - 6 (up) or top + 6 (down)
            if self.pitch_dir != "none":
                t = QTime.currentTime().msecsSinceStartOfDay()

                def get_arrow_frame(meta, current_time_ms):
                    frames, frame_height, total_duration = meta
                    mod_t = current_time_ms % total_duration if total_duration > 0 else 0
                    current_t = 0
                    frame_idx = 0
                    for f in frames:
                        f_time = f.get("time", 1) * 50
                        if mod_t < current_t + f_time:
                            frame_idx = f.get("index", 0)
                            break
                        current_t += f_time
                    return frame_idx, frame_height * bg_scale

                arrow_draw_x = draw_x + 1 * bg_scale
                if self.pitch_dir == "up" and hasattr(self, 'arrow_up_pixmap') and not self.arrow_up_pixmap.isNull():
                    frame_idx, arr_h = get_arrow_frame(self.arrow_up_meta, t)
                    arr_w = self.arrow_up_pixmap.width()
                    src_rect = QRect(0, frame_idx * arr_h, arr_w, arr_h)
                    arrow_draw_y = y_offset - 6 * bg_scale
                    painter.drawPixmap(arrow_draw_x, arrow_draw_y, self.arrow_up_pixmap, src_rect.x(), src_rect.y(), src_rect.width(), src_rect.height())
                elif self.pitch_dir == "down" and hasattr(self, 'arrow_down_pixmap') and not self.arrow_down_pixmap.isNull():
                    frame_idx, arr_h = get_arrow_frame(self.arrow_down_meta, t)
                    arr_w = self.arrow_down_pixmap.width()
                    src_rect = QRect(0, frame_idx * arr_h, arr_w, arr_h)
                    arrow_draw_y = y_offset + 6 * bg_scale
                    painter.drawPixmap(arrow_draw_x, arrow_draw_y, self.arrow_down_pixmap, src_rect.x(), src_rect.y(), src_rect.width(), src_rect.height())

class PlayerDataOptionsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Player Data Options")
        self.setFixedSize(300, 250)
        self.setStyleSheet(parent.styleSheet() if parent else "")
        layout = QVBoxLayout(self)
        
        self.config = load_launcher_config()
        self.options = self.config.get("player_data_options", {
            "show_3d_skin": True,
            "show_change_skin_btn": True,
            "show_java_profile": True,
            "show_xbox_profile": True,
            "show_locator_bar": True
        })
        
        self.checkboxes = {}
        
        options_map = [
            ("show_3d_skin", "Show 3D Skin Render"),
            ("show_change_skin_btn", "Show Change Skin/Cape Button"),
            ("show_java_profile", "Show Java Profile Info"),
            ("show_xbox_profile", "Show Xbox Profile Info"),
            ("show_locator_bar", "Show Locator Bar")
        ]
        
        for key, label in options_map:
            cb = QCheckBox(label)
            cb.setChecked(self.options.get(key, True))
            layout.addWidget(cb)
            self.checkboxes[key] = cb
            
        btn_layout = QHBoxLayout()
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self.save_and_close)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        
        btn_layout.addWidget(save_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)
        
    def save_and_close(self):
        for key, cb in self.checkboxes.items():
            self.options[key] = cb.isChecked()
            
        self.config["player_data_options"] = self.options
        save_launcher_config(self.config)
        self.accept()

class ProfileBannerWidget(QWidget):
    def __init__(self, auth_data, parent=None, progress_callback=None):
        super().__init__(parent)
        self.auth_data = auth_data
        
        self.config = load_launcher_config()
        self.options = self.config.get("player_data_options", {
            "show_3d_skin": True,
            "show_change_skin_btn": True,
            "show_java_profile": True,
            "show_xbox_profile": True,
            "show_locator_bar": True
        })

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 5)
        main_layout.setSpacing(5)

        top_layout = QHBoxLayout()

        # 1. Fetch textures and generate HTML
        textures = fetch_player_textures(get_active_profile_id(), auth_data.get('mc_uuid'), auth_data.get('xbox_gamerpic_url'), progress_callback)

        # 2. Left: 3D Skin Renderer and Change Skin button
        self.left_widget = QWidget()
        left_layout = QVBoxLayout(self.left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)

        self.web_view = QWebEngineView()
        
        # Use CustomWebPage to silence warnings
        try:
            self.custom_page = CustomWebPage(self.web_view.page().profile(), self.web_view)
            self.web_view.setPage(self.custom_page)
        except Exception:
            pass
            
        self.web_view.setFixedSize(200, 180)
        self.web_view.page().setBackgroundColor(Qt.GlobalColor.transparent)
        self.html_url = textures.get('html_url')
        if self.html_url:
            if self.options.get("show_3d_skin", True):
                from PyQt6.QtCore import QUrlQuery
                base_url = QUrl.fromLocalFile(self.html_url)
                query = QUrlQuery()
                if textures.get('skin'):
                    query.addQueryItem("skin", textures.get('skin'))
                if textures.get('cape'):
                    query.addQueryItem("cape", textures.get('cape'))
                base_url.setQuery(query)
                self.web_view.load(base_url)
            self.web_view.loadFinished.connect(self._on_load_finished)
        self.web_view.setVisible(self.options.get("show_3d_skin", True))
        left_layout.addWidget(self.web_view)

        left_layout.addSpacing(0)  # Fixed missing button
        
        self.change_skin_btn = QPushButton("Change Skin/Cape")
        self.change_skin_btn.setFixedWidth(150)
        self.change_skin_btn.clicked.connect(self.open_change_skin_dialog)
        self.change_skin_btn.setVisible(self.options.get("show_change_skin_btn", True))
        left_layout.addWidget(self.change_skin_btn, alignment=Qt.AlignmentFlag.AlignHCenter)

        top_layout.addWidget(self.left_widget)

        # 3. Center: Minecraft Username & Options
        self.center_widget = QWidget()
        center_layout = QVBoxLayout(self.center_widget)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)

        self.options_btn = QPushButton("Player Data Options")
        self.options_btn.setFixedSize(130, 24)
        self.options_btn.clicked.connect(self.open_player_data_options)
        
        btn_layout = QHBoxLayout()
        btn_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        btn_layout.addWidget(self.options_btn)
        center_layout.addLayout(btn_layout)

        self.java_info_widget = QWidget()
        java_layout = QVBoxLayout(self.java_info_widget)
        java_layout.setContentsMargins(0, 0, 0, 0)
        java_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        mc_name_layout = QHBoxLayout()
        mc_name_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        mc_name = QLabel(auth_data.get('mc_username', 'Unknown'))
        mc_name.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        mc_name_layout.addWidget(mc_name)

        from PyQt6.QtGui import QPixmap
        import os
        mc_logo_path = get_image_path("minecraft-logo-svg-vector.svg")
        if os.path.exists(mc_logo_path):
            mc_logo = QLabel()
            mc_logo_pix = QPixmap(mc_logo_path)
            if not mc_logo_pix.isNull():
                mc_logo_pix = mc_logo_pix.scaled(32, 32, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            mc_logo.setPixmap(mc_logo_pix)
            mc_name_layout.addWidget(mc_logo)

        java_layout.addLayout(mc_name_layout)

        mc_uuid = auth_data.get('mc_uuid', '')
        locator_color = calculate_locator_bar_color(mc_uuid) if mc_uuid else auth_data.get('locator_bar_color', '#FFFFFF')
        color_label = QLabel(f"Your locator bar color is: {locator_color}")
        color_label.setFont(QFont("Segoe UI", 9))
        java_layout.addWidget(color_label, alignment=Qt.AlignmentFlag.AlignCenter)

        self.waypoint_status_label = QLabel("Waypoint: Square (0 blocks | 0-179)")
        self.waypoint_status_label.setFont(QFont("Segoe UI", 8))
        self.waypoint_status_label.setStyleSheet("color: #666666;")
        self.waypoint_status_label.setVisible(self.options.get("show_locator_bar", True))
        java_layout.addWidget(self.waypoint_status_label, alignment=Qt.AlignmentFlag.AlignCenter)

        self.java_info_widget.setVisible(self.options.get("show_java_profile", True))
        center_layout.addWidget(self.java_info_widget)

        top_layout.addWidget(self.center_widget, stretch=1)

        # 4. Right: Xbox Info
        self.right_widget = QWidget()
        right_layout = QHBoxLayout(self.right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        xbox_info_layout = QVBoxLayout()
        xbox_info_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)
        xbox_info_layout.setSpacing(0)
        xbox_info_layout.setContentsMargins(0, 0, 10, 0)

        gt_row = QHBoxLayout()
        gt_row.setAlignment(Qt.AlignmentFlag.AlignRight)
        gt_row.setSpacing(4)
        xbox_icon_path = get_image_path("icons8-xbox-24.svg")
        if os.path.exists(xbox_icon_path):
            xbox_logo = QLabel()
            xbox_logo_pix = QPixmap(xbox_icon_path)
            if not xbox_logo_pix.isNull():
                xbox_logo_pix = xbox_logo_pix.scaled(18, 18, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            xbox_logo.setPixmap(xbox_logo_pix)
            gt_row.addWidget(xbox_logo)

        xbox_name = QLabel(auth_data.get('xbox_gamertag', ''))
        xbox_name.setFont(QFont("Segoe UI", 12))
        gt_row.addWidget(xbox_name)
        xbox_info_layout.addLayout(gt_row)

        gs_row = QHBoxLayout()
        gs_row.setAlignment(Qt.AlignmentFlag.AlignRight)
        gs_row.setSpacing(4)
        gamerscore_val = auth_data.get('xbox_gamerscore')
        if gamerscore_val:
            gamerscore_icon_path = get_image_path("xbox-gamerscore.svg")
            if os.path.exists(gamerscore_icon_path):
                gs_logo = QLabel()
                gs_logo_pix = QPixmap(gamerscore_icon_path)
                if not gs_logo_pix.isNull():
                    gs_logo_pix = gs_logo_pix.scaled(14, 14, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                gs_logo.setPixmap(gs_logo_pix)
                gs_row.addWidget(gs_logo)
            
            gs_label = QLabel(gamerscore_val)
            gs_label.setFont(QFont("Segoe UI", 10))
            gs_label.setStyleSheet("color: #666666;")
            gs_row.addWidget(gs_label)
            xbox_info_layout.addLayout(gs_row)
            
        presence = auth_data.get('xbox_presence_state', 'Offline')
        
        status_row = QHBoxLayout()
        status_row.setAlignment(Qt.AlignmentFlag.AlignRight)
        status_row.setSpacing(6)
        
        self.status_dot = QLabel()
        self.status_dot.setFixedSize(10, 10)
        
        status_color = "#32CD32" if presence == "Online" else "#AAAAAA"
        self.status_dot.setStyleSheet(f"background-color: {status_color}; border-radius: 5px;")
        status_row.addWidget(self.status_dot)
        
        self.status_label_text = QLabel(presence)
        self.status_label_text.setStyleSheet("color: #555555;")
        status_row.addWidget(self.status_label_text)
        
        xbox_info_layout.addLayout(status_row)

        right_layout.addLayout(xbox_info_layout)

        if textures.get('gamerpic'):
            pic_label = QLabel()
            pixmap = QPixmap(textures['gamerpic'])
            if not pixmap.isNull():
                pixmap = pixmap.scaled(50, 50, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            pic_label.setPixmap(pixmap)
            right_layout.addWidget(pic_label)

        self.right_widget.setVisible(self.options.get("show_xbox_profile", True))
        top_layout.addWidget(self.right_widget)
        main_layout.addLayout(top_layout)
        
        # 5. Bottom: Interactive Locator Bar spanning full width
        def _on_locator_status(text):
            if hasattr(self, 'waypoint_status_label') and self.waypoint_status_label:
                self.waypoint_status_label.setText(text)
                win = self.window()
                if hasattr(win, 'update_window_size'):
                    win.update_window_size()

        self.locator_bar = LocatorBarWidget(
            locator_color,
            status_callback=_on_locator_status
        )
        self.locator_bar.setVisible(self.options.get("show_locator_bar", True))
        main_layout.addWidget(self.locator_bar)

    def minimumSizeHint(self):
        return self.sizeHint()

    def sizeHint(self):
        top_h = 0
        show_skin = self.options.get("show_3d_skin", True)
        show_btn = self.options.get("show_change_skin_btn", True)
        if show_skin:
            top_h = max(top_h, 180 + (35 if show_btn else 0))
        elif show_btn:
            top_h = max(top_h, 35)

        if self.options.get("show_java_profile", True):
            top_h = max(top_h, 110)
        else:
            top_h = max(top_h, 35)

        if self.options.get("show_xbox_profile", True):
            top_h = max(top_h, 65)

        total_h = top_h
        if self.options.get("show_locator_bar", True):
            total_h += 54 + 5

        total_h += 15  # top (10) and bottom (5) layout margins

        w = 610
        if hasattr(self, 'waypoint_status_label') and self.waypoint_status_label and self.waypoint_status_label.isVisible():
            txt = self.waypoint_status_label.text()
            fm = self.waypoint_status_label.fontMetrics()
            text_w = fm.horizontalAdvance(txt) if hasattr(fm, 'horizontalAdvance') else fm.width(txt)
            w = max(w, 200 + text_w + 40 + 180)
        return QtCore.QSize(w, total_h)


    def open_player_data_options(self):
        dialog = PlayerDataOptionsDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.config = load_launcher_config()
            self.options = self.config.get("player_data_options", {})
            self.update_visibility()
            
    def update_visibility(self):
        show_skin = self.options.get("show_3d_skin", True)
        if self.web_view.isVisible() != show_skin:
            self.web_view.setVisible(show_skin)
            
        if hasattr(self, 'skin_spacer'):
            from PyQt6.QtWidgets import QSizePolicy
            self.skin_spacer.changeSize(1, -30 if show_skin else 0, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
            self.left_widget.layout().invalidate()

        self.change_skin_btn.setVisible(self.options.get("show_change_skin_btn", True))
        self.java_info_widget.setVisible(self.options.get("show_java_profile", True))
        self.right_widget.setVisible(self.options.get("show_xbox_profile", True))
        self.locator_bar.setVisible(self.options.get("show_locator_bar", True))
        if hasattr(self, 'waypoint_status_label') and self.waypoint_status_label:
            self.waypoint_status_label.setVisible(self.options.get("show_locator_bar", True))
        
        window = self.window()
        if hasattr(window, 'update_window_size'):
            window.update_window_size()

    def cleanup(self):
        if hasattr(self, 'web_view') and self.web_view:
            try:
                page = self.web_view.page()
                self.web_view.setPage(None)
                if page:
                    page.deleteLater()
            except:
                pass

    def _on_load_finished(self, ok):
        if ok:
            config = load_launcher_config()
            anim = config.get("main_animation", "idle")
            spin = config.get("main_spin", True)
            
            js = get_animation_js(anim)
            spin_val = 'true' if spin else 'false'
            spin_js = f"""
            (function() {{
                function applySpin() {{
                    if (window.skinViewer && window.skinViewer.playerObject) {{
                        window.skinViewer.autoRotate = {spin_val};
                    }} else {{
                        setTimeout(applySpin, 50);
                    }}
                }}
                applySpin();
            }})();
            """
            
            self.web_view.page().runJavaScript(js)
            self.web_view.page().runJavaScript(spin_js)

    def open_change_skin_dialog(self):
        dialog = ChangeSkinDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            # Overwrite cache with the new skin/cape to instantly update preview seamlessly via JS
            import shutil
            import os
            import time
            import re
            
            cache_dir = f".cache/{get_active_profile_id()}"
            
            if dialog.original_skin_path:
                shutil.copy(dialog.original_skin_path, os.path.join(cache_dir, "skin.png"))
                
            # Copy cape if it was previewed
            if getattr(dialog, "preview_cape_path", None):
                shutil.copy(os.path.join(cache_dir, dialog.preview_cape_path), os.path.join(cache_dir, "cape.png"))
            elif dialog.cape_combo.currentData() is None:
                if os.path.exists(os.path.join(cache_dir, "cape.png")):
                    os.remove(os.path.join(cache_dir, "cape.png"))
                    
            # Use Javascript to update via Data URIs
            import base64
            s_data = ""
            if os.path.exists(os.path.join(cache_dir, "skin.png")):
                with open(os.path.join(cache_dir, "skin.png"), "rb") as f:
                    s_data = "data:image/png;base64," + base64.b64encode(f.read()).decode('utf-8')

            c_data = ""
            if os.path.exists(os.path.join(cache_dir, "cape.png")):
                with open(os.path.join(cache_dir, "cape.png"), "rb") as f:
                    c_data = "data:image/png;base64," + base64.b64encode(f.read()).decode('utf-8')

            config = load_launcher_config()
            anim = config.get("main_animation", "idle")
            spin = config.get("main_spin", True)
            back_eq = "elytra" if anim == "fly" else "cape"

            model = "slim" if dialog.is_slim else "default"
            js = f"""
            if (window.skinViewer) {{
                if ("{s_data}") window.skinViewer.loadSkin("{s_data}", {{ model: "{model}" }});
                if ("{c_data}") {{
                    window.hasCapeLoaded = true;
                    window.skinViewer.loadCape("{c_data}", {{ backEquipment: "{back_eq}" }});
                }} else {{
                    window.hasCapeLoaded = false;
                    window.skinViewer.loadCape(null);
                    if (window.skinViewer.playerObject) {{
                        window.skinViewer.playerObject.backEquipment = null;
                        if (window.skinViewer.playerObject.cape) window.skinViewer.playerObject.cape.visible = false;
                    }}
                }}
            }}
            """
            self.web_view.page().runJavaScript(js)
            
            anim_js = get_animation_js(anim)
            spin_val = 'true' if spin else 'false'
            spin_js = f"""
            (function() {{
                function applySpin() {{
                    if (window.skinViewer && window.skinViewer.playerObject) {{
                        window.skinViewer.autoRotate = {spin_val};
                    }} else {{
                        setTimeout(applySpin, 50);
                    }}
                }}
                applySpin();
            }})();
            """
            
            self.web_view.page().runJavaScript(anim_js)
            self.web_view.page().runJavaScript(spin_js)


class AccountSwitchDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Switch Account")
        self.setFixedSize(350, 400)
        
        self.wants_new_account = False
        
        layout = QVBoxLayout()
        self.setLayout(layout)
        
        label = QLabel("Select an account:")
        label.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        layout.addWidget(label)
        
        self.list_widget = QListWidget()
        self.list_widget.setStyleSheet("QListWidget::item { padding: 10px; border-bottom: 1px solid #ccc; } QListWidget::item:selected { background-color: #0078D7; color: white; }")
        self.list_widget.setIconSize(QtCore.QSize(40, 40))
        layout.addWidget(self.list_widget)
        
        self.accounts = load_accounts()
        self.profiles = self.accounts.get("profiles", {})
        active_id = self.accounts.get("active_profile")
        
        for pid, pdata in self.profiles.items():
            name = pdata.get("mc_username", "Unknown Account")
            gt = pdata.get("xbox_gamertag", "")
            display = f"{name}"
            if gt and gt != name:
                display += f" ({gt})"
            
            item = QListWidgetItem(display)
            item.setData(Qt.ItemDataRole.UserRole, pid)
            if pid == active_id:
                item.setText(display + " (Active)")
                font = item.font()
                font.setBold(True)
                item.setFont(font)
                
            import os
            import urllib.request
            os.makedirs(f".cache/{pid}", exist_ok=True)
            pic_path = f".cache/{pid}/gamerpic.png"
            if not os.path.exists(pic_path):
                url = pdata.get("xbox_gamerpic_url")
                if url:
                    try:
                        urllib.request.urlretrieve(url, pic_path)
                    except:
                        pass
            if os.path.exists(pic_path):
                item.setIcon(QIcon(pic_path))
                
            self.list_widget.addItem(item)
            
        btn_layout = QHBoxLayout()
        
        self.new_btn = QPushButton("Add New Account")
        self.new_btn.clicked.connect(self.add_new_account)
        btn_layout.addWidget(self.new_btn)
        
        self.remove_btn = QPushButton("Remove Selected")
        self.remove_btn.clicked.connect(self.remove_account)
        btn_layout.addWidget(self.remove_btn)
        
        layout.addLayout(btn_layout)
        
        self.select_btn = QPushButton("Select Account")
        self.select_btn.clicked.connect(self.select_account)
        layout.addWidget(self.select_btn)
        
    def add_new_account(self):
        self.wants_new_account = True
        self.accept()
        
    def remove_account(self):
        item = self.list_widget.currentItem()
        if not item: return
        pid = item.data(Qt.ItemDataRole.UserRole)
        
        if pid in self.profiles:
            del self.profiles[pid]
            # Delete cache
            import shutil
            import os
            cache_dir = f".cache/{pid}"
            if os.path.exists(cache_dir):
                try: shutil.rmtree(cache_dir, ignore_errors=True)
                except: pass
                
        if self.accounts.get("active_profile") == pid:
            if len(self.profiles) > 0:
                self.accounts["active_profile"] = list(self.profiles.keys())[0]
            else:
                self.accounts["active_profile"] = None
                
        save_accounts(self.accounts)
        self.accept()
        
    def select_account(self):
        item = self.list_widget.currentItem()
        if not item: return
        pid = item.data(Qt.ItemDataRole.UserRole)
        self.accounts["active_profile"] = pid
        save_accounts(self.accounts)
        self.accept()


class WindowsLinkedAccountsDialog(QDialog):
    """
    Dialog presented before login that displays all Microsoft accounts currently linked
    with Windows and asks whether the user wants to log in with one of them or proceed
    to the other login method.
    """
    def __init__(self, linked_accounts: List[Dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Microsoft Accounts Linked with Windows")
        self.setFixedSize(440, 360)

        self.linked_accounts = linked_accounts or []
        self.selected_account: Optional[Dict] = None
        self.use_other_method: bool = False

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        header_lbl = QLabel(
            "The following Microsoft account(s) are currently linked with Windows.\n"
            "Would you like to log in with one of these accounts?"
        )
        header_lbl.setWordWrap(True)
        header_lbl.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        layout.addWidget(header_lbl)

        self.list_widget = QListWidget()
        self.list_widget.setStyleSheet(
            "QListWidget::item { padding: 10px; border-bottom: 1px solid #ccc; } "
            "QListWidget::item:selected { background-color: #0078D7; color: white; }"
        )
        self.list_widget.setIconSize(QtCore.QSize(32, 32))
        self.list_widget.itemDoubleClicked.connect(lambda _: self.login_with_selected())
        layout.addWidget(self.list_widget, stretch=1)

        ms_icon_path = get_image_path("icons8-xbox-24.svg")
        default_icon = QIcon(ms_icon_path) if os.path.exists(ms_icon_path) else QIcon()

        for acc in self.linked_accounts:
            email = acc.get("email", "")
            disp = acc.get("display_name", "")
            source = acc.get("source", "Windows Account")
            mc_name = acc.get("mc_username", "")
            gt = acc.get("xbox_gamertag", "")

            # Show Minecraft username or Xbox Gamertag first if known
            primary_name = mc_name or gt or disp
            if primary_name and primary_name.lower() != email.lower():
                line1 = f"{primary_name} ({email})"
            else:
                line1 = email

            details = [source]
            if mc_name and primary_name != mc_name:
                details.append(f"Minecraft: {mc_name}")
            elif gt and primary_name != gt and gt.lower() != email.lower():
                details.append(f"Xbox: {gt}")
            text = f"{line1}\n   [{' | '.join(details)}]"

            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, acc)

            pid = acc.get("cached_profile_id")
            pic_path = f".cache/{pid}/gamerpic.png" if pid else ""
            if pic_path and os.path.exists(pic_path):
                item.setIcon(QIcon(pic_path))
            elif not default_icon.isNull():
                item.setIcon(default_icon)

            self.list_widget.addItem(item)

        if self.list_widget.count() > 0:
            self.list_widget.setCurrentRow(0)

        self.login_selected_btn = QPushButton("Yes, Login with Selected Account")
        self.login_selected_btn.setDefault(True)
        self.login_selected_btn.setFixedHeight(32)
        self.login_selected_btn.setStyleSheet("font-weight: bold;")
        self.login_selected_btn.clicked.connect(self.login_with_selected)
        layout.addWidget(self.login_selected_btn)

        bottom_row = QHBoxLayout()
        self.other_method_btn = QPushButton("No, Use Other Login Method")
        self.other_method_btn.setFixedHeight(30)
        self.other_method_btn.clicked.connect(self.proceed_other_method)
        bottom_row.addWidget(self.other_method_btn, stretch=1)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setFixedHeight(30)
        self.cancel_btn.clicked.connect(self.reject)
        bottom_row.addWidget(self.cancel_btn)

        layout.addLayout(bottom_row)

    def login_with_selected(self):
        item = self.list_widget.currentItem()
        if not item and self.list_widget.count() > 0:
            item = self.list_widget.item(0)
        if not item:
            return
        self.selected_account = item.data(Qt.ItemDataRole.UserRole)
        self.use_other_method = False
        self.accept()

    def proceed_other_method(self):
        self.selected_account = None
        self.use_other_method = True
        self.accept()


class DownloadPipelineWorker(QtCore.QThread):
    progress_sig = QtCore.pyqtSignal(int, int, str, float, str)
    log_sig = QtCore.pyqtSignal(str)
    done_sig = QtCore.pyqtSignal(bool, object, str)

    def __init__(self, version, auth_data, is_offline, is_trial, dev_mode, quick_play, skip_verification, parent=None):
        super().__init__(parent)
        self.version = version
        self.auth_data = auth_data or {}
        self.is_offline = is_offline
        self.is_trial = is_trial
        self.dev_mode = dev_mode
        self.quick_play = quick_play
        self.skip_verification = skip_verification
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True

    def run(self):
        try:
            is_downloaded = self.version in get_downloaded_versions()
            need_downloads = not (is_downloaded and (self.skip_verification or self.is_offline) and not self.dev_mode)

            steps = []
            if need_downloads:
                steps.append((
                    "Java",
                    f"python backend/download_java.py --version {self.version}",
                    lambda: download_java.ensure_java_for_version(self.version, json_mode=True)
                ))
                steps.append((
                    "Game Client",
                    f"python backend/download_minecraft.py --client --version {self.version}",
                    lambda: download_minecraft.download_version(self.version, jar_type="client", json_mode=True, is_dev_mode=self.dev_mode, extract_source=False)
                ))
                if self.dev_mode:
                    steps.append((
                        "JAR Resources",
                        f"python backend/extract_source_code.py --version {self.version} --client --step resources",
                        lambda: extract_source_code.extract_version_source(version_id=self.version, jar_type="client", json_output=True, step="resources")
                    ))
                    steps.append((
                        "Decompiling Classes",
                        f"python backend/extract_source_code.py --version {self.version} --client --step decompile",
                        lambda: extract_source_code.extract_version_source(version_id=self.version, jar_type="client", json_output=True, step="decompile")
                    ))
                steps.append((
                    "Libraries",
                    f"python backend/download_libraries.py --version {self.version}{' --source' if self.dev_mode else ''}",
                    lambda: download_libraries.download_libraries(self.version, json_mode=True, is_dev_mode=self.dev_mode)
                ))
                steps.append((
                    "Assets",
                    f"python backend/download_assets.py --version {self.version}{' --source' if self.dev_mode else ''}",
                    lambda: download_assets.download_assets(self.version, json_mode=True, is_dev_mode=self.dev_mode)
                ))

            total_steps = len(steps) + 1  # +1 for launching game
            current_step_info = ["", 1]

            def on_redirect_line(line: str):
                line = line.strip()
                if not line:
                    return
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict) and obj.get("type") == "progress":
                        task_pct = float(obj.get("progress", 0))
                        status_str = obj.get("status", "")
                        self.progress_sig.emit(
                            current_step_info[1],
                            total_steps,
                            current_step_info[0],
                            task_pct,
                            status_str
                        )
                        return
                except Exception:
                    pass
                self.log_sig.emit(line)

            class _ThreadStdout:
                def __init__(self, callback):
                    self.callback = callback
                    self.buf = ""
                def write(self, text):
                    self.buf += text
                    while "\n" in self.buf:
                        chunk, self.buf = self.buf.split("\n", 1)
                        chunk = chunk.strip("\r")
                        if chunk:
                            self.callback(chunk)
                def flush(self):
                    if self.buf.strip():
                        self.callback(self.buf.strip())
                        self.buf = ""

            orig_stdout = sys.stdout
            orig_stderr = sys.stderr

            try:
                redirector = _ThreadStdout(on_redirect_line)
                sys.stdout = redirector
                sys.stderr = redirector

                for idx, (step_name, step_cmd, step_fn) in enumerate(steps):
                    if self._is_cancelled:
                        self.done_sig.emit(False, None, "Launch sequence cancelled.")
                        return

                    step_num = idx + 1
                    current_step_info[0] = step_name
                    current_step_info[1] = step_num

                    self.progress_sig.emit(step_num, total_steps, step_name, 0.0, f"[{step_num}/{total_steps}] Verifying {step_name}...")
                    self.log_sig.emit(f"\n> Running step [{step_num}/{total_steps}]: {step_name}")
                    self.log_sig.emit(f"  Command: {step_cmd}")

                    success = step_fn()
                    redirector.flush()

                    if not success:
                        self.done_sig.emit(False, None, f"Step '{step_name}' failed.")
                        return

                    self.progress_sig.emit(step_num, total_steps, step_name, 100.0, f"[{step_num}/{total_steps}] {step_name} ready.")
            finally:
                sys.stdout = orig_stdout
                sys.stderr = orig_stderr

            if self._is_cancelled:
                self.done_sig.emit(False, None, "Launch sequence cancelled.")
                return

            # Prepare launch command
            launch_step_num = total_steps
            launch_cmd_str = f"python backend/command_executor.py --version {self.version}"
            if self.is_offline:
                launch_cmd_str += " --offline"
            if self.is_trial:
                launch_cmd_str += " --demo"
            if self.quick_play:
                launch_cmd_str += f" --quickPlayType {self.quick_play.get('type')} --quickPlayId {self.quick_play.get('id')}"

            self.progress_sig.emit(launch_step_num, total_steps, "Launching Game...", 0.0, f"[{launch_step_num}/{total_steps}] Preparing launch command...")
            self.log_sig.emit(f"\n> Running step [{launch_step_num}/{total_steps}]: Launching Game...")
            self.log_sig.emit(f"  Command: {launch_cmd_str}")

            executor = command_executor.CommandExecutor(self.version)
            executor.offline = self.is_offline
            executor.demo = self.is_trial
            if self.quick_play:
                executor.quickPlayType = self.quick_play.get('type')
                executor.quickPlayId = self.quick_play.get('id')

            executor.variables['auth_player_name'] = self.auth_data.get('mc_username', 'OfflinePlayer')
            executor.variables['auth_uuid'] = self.auth_data.get('mc_uuid', '00000000-0000-0000-0000-000000000000')
            executor.variables['auth_access_token'] = self.auth_data.get('mc_access_token', 'offline')
            executor.variables['auth_xuid'] = self.auth_data.get('xbox_xuid', 'offline')
            executor.variables['clientid'] = self.auth_data.get('launch_client_id', 'PyMcLauncher')

            prepared = executor.prepare_launch_command()
            if not prepared:
                self.done_sig.emit(False, None, "Failed to prepare launch command.")
                return

            filled_command, cwd = prepared
            self.done_sig.emit(True, (filled_command, cwd), "")

        except Exception as e:
            self.done_sig.emit(False, None, str(e))


class InstanceLogWindow(QMainWindow):
    """Dedicated log window for an individual Minecraft instance."""
    def __init__(self, launcher, inst: dict, parent=None):
        super().__init__(parent)
        self.launcher = launcher
        self.inst = inst

        ver = inst.get("version", "Unknown")
        prof = inst.get("profile", "Default")
        user = inst.get("username", "")
        user_str = f" ({user})" if user else ""

        self.setWindowTitle(f"Minecraft {ver} - Profile: {prof}{user_str} - Game Log")
        self.resize(800, 500)
        if parent:
            self.setWindowIcon(parent.windowIcon())
            self.setStyleSheet(parent.styleSheet())

        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        # Top bar with info and controls
        top_bar = QHBoxLayout()
        self.status_lbl = QLabel(f"Instance: Minecraft {ver} | Profile: {prof}{user_str}")
        self.status_lbl.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        top_bar.addWidget(self.status_lbl, stretch=1)

        self.auto_scroll_cb = QCheckBox("Auto-scroll")
        self.auto_scroll_cb.setChecked(True)
        top_bar.addWidget(self.auto_scroll_cb)

        self.wrap_cb = QCheckBox("Wrap Lines")
        self.wrap_cb.setChecked(True)
        self.wrap_cb.toggled.connect(self._toggle_wrap)
        top_bar.addWidget(self.wrap_cb)

        copy_btn = QPushButton("Copy All")
        copy_btn.setFixedWidth(80)
        copy_btn.clicked.connect(self._copy_all)
        top_bar.addWidget(copy_btn)

        clear_btn = QPushButton("Clear")
        clear_btn.setFixedWidth(70)
        clear_btn.clicked.connect(self._clear_log)
        top_bar.addWidget(clear_btn)

        layout.addLayout(top_bar)

        # Text area
        self.text_edit = QTextEdit()
        self.text_edit.setReadOnly(True)
        self.text_edit.setFont(QFont("Consolas", 10))
        self.text_edit.setStyleSheet("""
            QTextEdit {
                background-color: #1e1e1e;
                color: #dcdcdc;
                border: 1px solid #3c3c3c;
                border-radius: 4px;
            }
        """)
        self.text_edit.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        layout.addWidget(self.text_edit, stretch=1)

        # Bottom status bar
        self.footer_lbl = QLabel("State: Running")
        self.footer_lbl.setStyleSheet("color: #44bb44; font-size: 11px;")
        layout.addWidget(self.footer_lbl)

        # Load existing buffered lines
        for raw_line in inst.get("log_lines", []):
            formatted = self.launcher._format_log_line(raw_line)
            self.text_edit.append(formatted)

        self._scroll_to_bottom()

    def _toggle_wrap(self, checked: bool):
        if checked:
            self.text_edit.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        else:
            self.text_edit.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)

    def _copy_all(self):
        text = self.text_edit.toPlainText()
        if text:
            clipboard = QApplication.clipboard()
            if clipboard:
                clipboard.setText(text)

    def _clear_log(self):
        self.text_edit.clear()

    def append_log(self, formatted_line: str):
        self.text_edit.append(formatted_line)
        if self.auto_scroll_cb.isChecked():
            self._scroll_to_bottom()

    def _scroll_to_bottom(self):
        scrollbar = self.text_edit.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def update_instance_state(self, is_running: bool, exit_code: Optional[int] = None):
        ver = self.inst.get("version", "Unknown")
        prof = self.inst.get("profile", "Default")
        if is_running:
            self.footer_lbl.setText("State: Running")
            self.footer_lbl.setStyleSheet("color: #44bb44; font-size: 11px;")
        else:
            if exit_code == 0:
                self.footer_lbl.setText("State: Process finished successfully (exit code 0)")
                self.footer_lbl.setStyleSheet("color: #888888; font-size: 11px;")
            else:
                self.footer_lbl.setText(f"State: Process exited with code {exit_code}")
                self.footer_lbl.setStyleSheet("color: #ff5555; font-size: 11px; font-weight: bold;")
            self.setWindowTitle(f"Minecraft {ver} ({prof}) - Process Finished (Code {exit_code})")

    def closeEvent(self, event):
        event.accept()


class LauncherLogWindow(QMainWindow):
    """Detached window mirroring the launcher's main console log."""
    closed_sig = QtCore.pyqtSignal()

    def __init__(self, launcher):
        super().__init__(None)
        self.launcher = launcher
        self.setWindowTitle("PyMcLauncher - Log")
        self.resize(800, 500)
        self.setWindowIcon(launcher.windowIcon())

        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        # Top bar with controls
        top_bar = QHBoxLayout()
        self.title_lbl = QLabel("Launcher Console Log")
        self.title_lbl.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        top_bar.addWidget(self.title_lbl, stretch=1)

        self.auto_scroll_cb = QCheckBox("Auto-scroll")
        self.auto_scroll_cb.setChecked(True)
        top_bar.addWidget(self.auto_scroll_cb)

        self.wrap_cb = QCheckBox("Wrap Lines")
        self.wrap_cb.setChecked(True)
        self.wrap_cb.toggled.connect(self._toggle_wrap)
        top_bar.addWidget(self.wrap_cb)

        copy_btn = QPushButton("Copy All")
        copy_btn.setFixedWidth(80)
        copy_btn.clicked.connect(self._copy_all)
        top_bar.addWidget(copy_btn)

        clear_btn = QPushButton("Clear")
        clear_btn.setFixedWidth(70)
        clear_btn.clicked.connect(self._clear_log)
        top_bar.addWidget(clear_btn)

        layout.addLayout(top_bar)

        # Text area with dark console styling
        self.text_edit = QTextEdit()
        self.text_edit.setReadOnly(True)
        self.text_edit.setFont(QFont("Consolas", 10))
        self.text_edit.setStyleSheet("""
            QTextEdit {
                background-color: #1e1e1e;
                color: #dcdcdc;
                border: 1px solid #3c3c3c;
                border-radius: 4px;
            }
        """)
        self.text_edit.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        layout.addWidget(self.text_edit, stretch=1)

        self.refresh()

    def _toggle_wrap(self, checked: bool):
        if checked:
            self.text_edit.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        else:
            self.text_edit.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)

    def _copy_all(self):
        text = self.text_edit.toPlainText()
        if text:
            clipboard = QApplication.clipboard()
            if clipboard:
                clipboard.setText(text)

    def _clear_log(self):
        self.text_edit.clear()
        if hasattr(self.launcher, 'results_text'):
            self.launcher.results_text.clear()
        if hasattr(self.launcher, '_raw_log_lines'):
            self.launcher._raw_log_lines = []

    def refresh(self):
        self.text_edit.clear()
        lines = getattr(self.launcher, '_raw_log_lines', [])
        if lines:
            for raw_line in lines:
                formatted = self.launcher._format_log_line(raw_line)
                self.text_edit.append(formatted)
        elif hasattr(self.launcher, 'results_text') and self.launcher.results_text.toPlainText():
            self.text_edit.setPlainText(self.launcher.results_text.toPlainText())
        self._scroll_to_bottom()

    def append_line(self, formatted: str):
        self.text_edit.append(formatted)
        if self.auto_scroll_cb.isChecked():
            self._scroll_to_bottom()

    def _scroll_to_bottom(self):
        sb = self.text_edit.verticalScrollBar()
        sb.setValue(sb.maximum())

    def closeEvent(self, event):
        self.closed_sig.emit()
        event.accept()


def set_taskbar_progress_state(widget, state: str):
    """Set the native Windows taskbar progress state ('none', 'normal', 'error') for a window."""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        flags = {"none": 0x0, "normal": 0x2, "error": 0x4}[state]
        ole32 = ctypes.windll.ole32
        ole32.CoInitialize(None)

        class GUID(ctypes.Structure):
            _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort), ("d", ctypes.c_ubyte * 8)]

        def mk(a, b, c, *d):
            return GUID(a, b, c, (ctypes.c_ubyte * 8)(*d))

        clsid = mk(0x56FDF344, 0xFD6D, 0x11D0, 0x95, 0x8A, 0x00, 0x60, 0x97, 0xC9, 0xA0, 0x90)
        iid = mk(0xEA1AFB91, 0x9E28, 0x4B86, 0x90, 0xE9, 0x9E, 0x9E, 0x5A, 0xEF, 0xAF)
        ptr = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(ptr))
        if hr != 0 or not ptr.value:
            return
        vtbl = ctypes.cast(ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
        hwnd = wintypes.HWND(int(widget.winId()))
        HrInit = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(vtbl[3])
        SetValue = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.HWND, ctypes.c_ulonglong, ctypes.c_ulonglong)(vtbl[9])
        SetState = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.HWND, ctypes.c_int)(vtbl[10])
        Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])
        HrInit(ptr)
        if state == "error":
            SetValue(ptr, hwnd, 100, 100)
        SetState(ptr, hwnd, flags)
        Release(ptr)
    except Exception:
        pass


class LauncherGUI(QMainWindow):
    def __init__(self, startup_args=None):
        super().__init__()
        self.setWindowTitle("PyMcLauncher")
        self._startup_args = startup_args
        try:
            self.setWindowIcon(get_pymclauncher_icon())
        except Exception:
            pass

        self.is_offline = False
        self.browser_window = None
        self.process = None
        self._step_worker = None
        self._pipeline_worker = None
        self.auth_data = get_auth_cache()
        self.is_just_logging_in = False
        self.is_pipeline_running = False
        self.is_authenticating = False
        self.running_instances = []
        self._next_instance_id = 1
        self._raw_log_lines = []
        
        # Removing all custom CSS to let the native Windows 11/10 dll (windowsvista style) 
        # render all the controls (dropdowns, progress bars, buttons) natively.
        
        # Main Layout
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.layout = QVBoxLayout(self.central_widget)
        self.layout.setContentsMargins(20, 20, 20, 35)
        self.layout.setSpacing(10)
        
        # Profile Banner Area
        self.banner_container = QVBoxLayout()
        self.layout.addLayout(self.banner_container)
        
        # Top bar (Version selector)
        # Profile Selector
        prof_layout = QHBoxLayout()
        self.profile_combo = QComboBox()
        self.profile_combo.currentIndexChanged.connect(self.on_profile_changed)
        self.shortcut_btn = QPushButton("Create Shortcut")
        self.shortcut_btn.setToolTip("Create a Desktop shortcut or pin to Start Menu for a version, world/Quick Play, or profile")
        self.shortcut_btn.clicked.connect(self.open_create_shortcut_dialog)
        prof_layout.addWidget(QLabel("Profile:"))
        prof_layout.addWidget(self.profile_combo, stretch=1)
        prof_layout.addWidget(self.shortcut_btn)
        self.layout.addLayout(prof_layout)
        
        # Load profiles
        self.reload_profiles_combo()

        self.top_layout = QHBoxLayout()
        self.version_label = QLabel("Version:")
        self.version_combo = QComboBox()
        self.version_combo.currentIndexChanged.connect(self.on_version_changed)
        
        self.options_btn = QPushButton("Options")
        self.options_btn.clicked.connect(self.open_options_dialog)
        
        self.dirs_btn = QPushButton("Directories")
        self.dirs_btn.clicked.connect(self.open_directories_dialog)
        
        self.login_btn = QPushButton("Login")
        self.login_btn.clicked.connect(self.do_login)
        
        self.switch_account_btn = QPushButton("Switch Account")
        self.switch_account_btn.clicked.connect(self.switch_account)
        self.logout_btn = QPushButton("Logout of all accounts")
        self.logout_btn.clicked.connect(self.logout)
        
        # Hide logout button if there is no auth cache
        import os, json, time
        accounts = load_accounts()
        is_logged_in = bool(accounts.get("active_profile"))
        is_expired = False
        active_id = accounts.get("active_profile")
        if active_id and active_id in accounts.get("profiles", {}):
            pass
                
        self.logout_btn.setVisible(is_logged_in)
        self.login_btn.setVisible(not is_logged_in or is_expired)
        
        self.top_layout.addWidget(self.version_label)
        self.top_layout.addWidget(self.version_combo, stretch=1)
        self.top_layout.addWidget(self.options_btn)
        self.top_layout.addWidget(self.dirs_btn)
        self.top_layout.addWidget(self.login_btn)
        self.top_layout.addWidget(self.switch_account_btn)
        self.top_layout.addWidget(self.logout_btn)
        self.layout.addLayout(self.top_layout)

        config = load_launcher_config()
        self.show_console = config.get("show_console", False)
        self.show_access_token = config.get("show_access_token", False)
        self.remember_version = config.get("remember_version", False)
        self.last_played_version = config.get("last_played_version", None)

        # Update Banner
        self.step_progress_bar = QProgressBar()
        self.step_progress_bar.setTextVisible(True)
        self.step_progress_bar.setRange(0, 100)
        self.step_progress_bar.setValue(0)
        self.step_progress_bar.setVisible(False)
        self.layout.addWidget(self.step_progress_bar)

        self.task_progress_bar = QProgressBar()
        self.task_progress_bar.setTextVisible(True)
        self.task_progress_bar.setRange(0, 100)
        self.task_progress_bar.setValue(0)
        self.task_progress_bar.setFormat("Current Task: %p%")
        self.task_progress_bar.setVisible(False)
        self.layout.addWidget(self.task_progress_bar)
        
        self.banner_widget = None
        if is_logged_in:
            self.step_progress_bar.setVisible(True)
            self.step_progress_bar.setValue(10)
            self.step_progress_bar.setFormat("Loading profile and textures... (%p%)")
        QTimer.singleShot(0, self.update_profile_banner)

        # Console Output
        self.results_text = QTextEdit()
        self.results_text.setReadOnly(True)
        self.results_text.setVisible(self.show_console)
        self.log_window = None
        self.pop_log_btn = QPushButton("Pop Out Log")
        self.pop_log_btn.setToolTip("Show the log in its own window")
        self.pop_log_btn.clicked.connect(self.toggle_log_popout)
        self.pop_log_btn.setVisible(self.show_console)
        self.layout.addWidget(self.pop_log_btn, alignment=Qt.AlignmentFlag.AlignRight)
        self.layout.addWidget(self.results_text, stretch=1000)

        # Status Label
        self.status_label = QLabel("Loading profile and textures..." if is_logged_in else "Ready to launch.")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.status_label.setMinimumWidth(610) # Forces layout sizeHint width to 650 (610 + 20*2 margins)
        self.layout.addWidget(self.status_label)
        
        self.warning_label = QLabel("Warning: This specific version will not automatically update.")
        self.warning_label.setStyleSheet("color: #cc8800; font-style: italic;")
        self.warning_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.warning_label.setVisible(False)
        self.layout.addWidget(self.warning_label)

        # Play Button, Stop Button, and Delete Button Layout
        self.action_layout = QHBoxLayout()
        self.play_button = QPushButton("PLAY MINECRAFT")
        self.play_button.clicked.connect(self.start_launch_sequence)

        self.stop_button = QPushButton("Stop Game")
        self.stop_button.setStyleSheet("color: #cc0000; font-weight: bold;")
        self.stop_button.clicked.connect(self.stop_running_games)
        self.stop_button.setVisible(False)
        
        self.delete_button = QPushButton("Delete")
        self.delete_button.clicked.connect(self.delete_selected_version)
        self.delete_button.setVisible(False)
        
        self.setup_quick_play()
        self.action_layout.addWidget(self.play_button, stretch=3)
        self.action_layout.addWidget(self.stop_button, stretch=1)
        self.action_layout.addWidget(self.delete_button, stretch=1)
        self.layout.addLayout(self.action_layout)

        # Running Minecraft Instances section (shown when >1 instance is running)
        self.instances_group = QGroupBox("Running Minecraft Instances")
        self.instances_layout = QVBoxLayout(self.instances_group)
        self.instances_layout.setContentsMargins(10, 8, 10, 8)
        self.instances_layout.setSpacing(6)
        self.instances_group.setVisible(False)
        self.layout.addWidget(self.instances_group)
        
        # Dummy widget to enforce minimum layout width of 610 (plus 40 margins = 650 window width)
        self.dummy_width_widget = QWidget()
        self.dummy_width_widget.setFixedSize(610, 0)
        self.layout.addWidget(self.dummy_width_widget)
        self.layout.insertStretch(self.layout.indexOf(self.status_label), 1)
        self.update_window_size()
        
        # Pipeline State
        self.pipeline_steps = []
        self.current_step = 0
        self.selected_version = "1.21.1"
        
        self.version_filters = config.get("version_filters", ["release"])
        self.show_downloaded_only = config.get("show_downloaded_only", False)
        self.skip_verification = config.get("skip_verification", True)
        self.dev_mode = config.get("dev_mode", False)
        
        self.load_versions()

        if self._startup_args:
            self.apply_startup_args(self._startup_args)

        # Start a silent background refresh to update skin/presence
        if is_logged_in and not is_expired:
            self.refresh_process = QProcess()
            self.refresh_process.setProgram(get_python_cli_executable())
            self.refresh_process.setArguments([get_script_path("ms_accounts.py"), "--json", "--profile", get_active_profile_id()])
            self.refresh_process.readyReadStandardOutput.connect(self.handle_refresh_stdout)
            self.refresh_process.start()

        # Run startup garbage collection after UI is shown
        QTimer.singleShot(100, self.run_startup_garbage_collection)

        # Update status row (bottom of the window), changelog after updates, and requirements check
        try:
            from launcher_update import UpdateController
            self.update_controller = UpdateController(self)
        except Exception as e:
            print(f"[Warning] Update checker unavailable: {e}")

    def open_create_shortcut_dialog(self):
        dlg = CreateShortcutDialog(self)
        dlg.exec()

    def apply_startup_args(self, args):
        """Apply CLI arguments passed from a Desktop or Start Menu shortcut and trigger auto-launch if requested."""
        try:
            # 1. Select Profile if specified
            if getattr(args, "profile", None):
                p_idx = self.profile_combo.findText(args.profile)
                if p_idx >= 0 and self.profile_combo.currentIndex() != p_idx:
                    self.profile_combo.setCurrentIndex(p_idx)

            # 2. Select Version if specified
            if getattr(args, "version", None):
                target_ver = args.version
                if target_ver in ("latest", "latest-release", "latest-snapshot"):
                    resolved_ver = None
                    try:
                        avail = list(get_available_versions() or [])
                        if target_ver == "latest-snapshot":
                            for v_id, v_type, _ in avail:
                                if v_type in ("snapshot", "release"):
                                    resolved_ver = v_id
                                    break
                        else:
                            for v_id, v_type, _ in avail:
                                if v_type == "release":
                                    resolved_ver = v_id
                                    break
                    except Exception:
                        pass
                    if not resolved_ver and self.version_combo.count() > 0:
                        resolved_ver = self.version_combo.itemData(0)
                    if resolved_ver:
                        target_ver = resolved_ver

                v_idx = self.version_combo.findData(target_ver)
                if v_idx < 0:
                    is_dl = target_ver in get_downloaded_versions()
                    label = f"{target_ver} [Downloaded]" if is_dl else target_ver
                    self.version_combo.insertItem(0, label, target_ver)
                    if is_dl:
                        self.version_combo.setItemData(0, True, Qt.ItemDataRole.UserRole + 1)
                    v_idx = 0
                self.version_combo.setCurrentIndex(v_idx)

            # 3. Select Quick Play World / Server / Realm if specified
            qp_type = getattr(args, "quick_play_type", None)
            qp_id = getattr(args, "quick_play_id", None)
            if qp_type and qp_id and hasattr(self, "quick_play_combo"):
                matched_idx = -1
                for i in range(self.quick_play_combo.count()):
                    d = self.quick_play_combo.itemData(i)
                    if isinstance(d, dict) and d.get("type") == qp_type and str(d.get("id")) == str(qp_id):
                        matched_idx = i
                        break
                if matched_idx < 0:
                    disp = getattr(args, "quick_play_display", None) or f"{qp_type.title()}: {qp_id}"
                    qp_obj = {"type": qp_type, "id": qp_id, "display": disp}
                    self.quick_play_combo.addItem(disp, qp_obj)
                    matched_idx = self.quick_play_combo.count() - 1
                self.quick_play_combo.setCurrentIndex(matched_idx)

            # 4. Trigger launch sequence automatically when launched via shortcut
            if getattr(args, "shortcut_launch", False):
                self.log("[Shortcut] Auto-launching from shortcut...")
                QTimer.singleShot(250, self.start_launch_sequence)
        except Exception as e:
            self.log(f"[Warning] Failed to apply shortcut arguments: {e}")

    def run_startup_garbage_collection(self):
        class _GCThread(QtCore.QThread):
            log_msg = QtCore.pyqtSignal(str)
            def run(self_thread):
                try:
                    from garbage_collect import perform_garbage_collection
                    perform_garbage_collection(log_callback=self_thread.log_msg.emit)
                except Exception as e:
                    self_thread.log_msg.emit(f"Error during startup garbage collection: {e}")
        self._gc_thread = _GCThread(self)
        self._gc_thread.log_msg.connect(self.log)
        self._gc_thread.start()

    def _kill_qprocess_tree(self, proc: QProcess):
        if not proc or proc.state() == QProcess.ProcessState.NotRunning:
            return
        pid = proc.processId()
        if pid:
            if os.name == 'nt':
                import subprocess
                try:
                    subprocess.run(
                        ['taskkill', '/F', '/T', '/PID', str(pid)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                    )
                except Exception:
                    pass
            else:
                import signal
                try:
                    os.kill(pid, signal.SIGKILL)
                except Exception:
                    pass
        try:
            proc.kill()
        except Exception:
            pass

    def closeEvent(self, event):
        if getattr(self, '_pipeline_worker', None) and self._pipeline_worker.isRunning():
            self._pipeline_worker.cancel()
            self._pipeline_worker.wait(1000)
        self._kill_qprocess_tree(getattr(self, 'process', None))
        for inst in list(getattr(self, 'running_instances', [])):
            if inst.get('window_timer'):
                inst['window_timer'].stop()
            if inst.get('fallback_timer'):
                inst['fallback_timer'].stop()
            if inst.get('startup_timer'):
                inst['startup_timer'].stop()
            log_win = inst.get('log_window')
            if log_win:
                try:
                    log_win.close()
                except Exception:
                    pass
            self._kill_qprocess_tree(inst.get('process'))
        if getattr(self, 'refresh_process', None) and self.refresh_process.state() != QProcess.ProcessState.NotRunning:
            self.refresh_process.kill()
        event.accept()

    def update_ui_state(self):
        """Enable/disable buttons and update labels accurately based on current launcher & game state."""
        is_busy = bool(getattr(self, 'is_pipeline_running', False) or getattr(self, 'is_authenticating', False))
        running_list = getattr(self, 'running_instances', [])
        started_instances = [inst for inst in running_list if inst.get('started')]
        running_count = len(started_instances)

        idx = self.version_combo.currentIndex() if hasattr(self, 'version_combo') else -1
        selected_ver = self.version_combo.currentData() if idx >= 0 else None
        has_versions = bool(hasattr(self, 'version_combo') and self.version_combo.count() > 0 and selected_ver is not None)
        is_selected_downloaded = bool(self.version_combo.itemData(idx, Qt.ItemDataRole.UserRole + 1)) if idx >= 0 else False
        is_selected_ver_running = any(inst.get('version') == selected_ver for inst in running_list)

        # Play / Start Another Instance Button
        if hasattr(self, 'play_button'):
            self.play_button.setEnabled(not is_busy and has_versions)
            if running_count > 0:
                self.play_button.setText("START ANOTHER INSTANCE")
            elif getattr(self, 'is_offline', False):
                self.play_button.setText("PLAY MINECRAFT OFFLINE")
            elif getattr(self, 'auth_data', None) and self.auth_data.get("owns_minecraft") is False:
                self.play_button.setText("PLAY MINECRAFT TRIAL")
            else:
                self.play_button.setText("PLAY MINECRAFT")

        # Stop Game Button (in main action bar)
        if hasattr(self, 'stop_button'):
            if running_count == 1:
                self.stop_button.setText("Stop Game")
                self.stop_button.setVisible(True)
                self.stop_button.setEnabled(True)
            elif running_count > 1:
                self.stop_button.setText("Stop All Games")
                self.stop_button.setVisible(True)
                self.stop_button.setEnabled(True)
            elif is_busy and getattr(self, '_pipeline_worker', None) and self._pipeline_worker.isRunning():
                self.stop_button.setText("Cancel Launch")
                self.stop_button.setVisible(True)
                self.stop_button.setEnabled(True)
            else:
                self.stop_button.setVisible(False)

        # Delete Button
        if hasattr(self, 'delete_button'):
            self.delete_button.setVisible(is_selected_downloaded)
            self.delete_button.setEnabled(not is_busy and is_selected_downloaded and not is_selected_ver_running)
            if is_selected_ver_running:
                self.delete_button.setToolTip("Cannot delete this version while it is currently running.")
            else:
                self.delete_button.setToolTip("")

        # Dropdowns & Top Bar Buttons
        if hasattr(self, 'version_combo'):
            self.version_combo.setEnabled(not is_busy and has_versions)
        if hasattr(self, 'profile_combo'):
            self.profile_combo.setEnabled(not is_busy)
        if hasattr(self, 'quick_play_combo') and self.quick_play_combo:
            self.quick_play_combo.setEnabled(not is_busy and has_versions)
        if hasattr(self, 'options_btn'):
            self.options_btn.setEnabled(not is_busy)
        if hasattr(self, 'dirs_btn'):
            self.dirs_btn.setEnabled(not is_busy)
        self._refresh_auth_buttons()
        if hasattr(self, 'login_btn'):
            self.login_btn.setEnabled(not is_busy)
        if hasattr(self, 'switch_account_btn'):
            self.switch_account_btn.setEnabled(not is_busy)
        if hasattr(self, 'logout_btn'):
            self.logout_btn.setEnabled(not is_busy and len(running_list) == 0)
            if len(running_list) > 0:
                self.logout_btn.setToolTip("Close running Minecraft instances before logging out.")
            else:
                self.logout_btn.setToolTip("")

        if hasattr(self, 'banner_widget') and self.banner_widget:
            try:
                if hasattr(self.banner_widget, 'change_skin_btn') and self.banner_widget.change_skin_btn:
                    self.banner_widget.change_skin_btn.setEnabled(not is_busy)
                if hasattr(self.banner_widget, 'options_btn') and self.banner_widget.options_btn:
                    self.banner_widget.options_btn.setEnabled(not is_busy)
            except RuntimeError:
                self.banner_widget = None

    def update_running_instances_ui(self):
        """Show the running instances list below the main buttons when >1 instance is running."""
        if not hasattr(self, 'instances_group') or not hasattr(self, 'instances_layout'):
            return

        while self.instances_layout.count():
            item = self.instances_layout.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)
                w.deleteLater()

        running_list = getattr(self, 'running_instances', [])
        if len(running_list) > 1:
            for inst in running_list:
                row_widget = QWidget()
                row_layout = QHBoxLayout(row_widget)
                row_layout.setContentsMargins(4, 2, 4, 2)
                row_layout.setSpacing(8)

                prof_name = inst.get('profile') or 'Default'
                user_name = inst.get('username') or ''
                ver_name = inst.get('version') or 'Unknown'
                started = inst.get('started', False)
                status_tag = "" if started else " (Starting...)"

                if user_name:
                    info_text = f"Profile: {prof_name} ({user_name})   |   Version: {ver_name}{status_tag}"
                else:
                    info_text = f"Profile: {prof_name}   |   Version: {ver_name}{status_tag}"

                info_lbl = QLabel(info_text)
                info_lbl.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
                row_layout.addWidget(info_lbl, stretch=1)

                logs_btn = QPushButton("Show Logs")
                logs_btn.setFixedWidth(95)
                logs_btn.clicked.connect(lambda checked=False, target_inst=inst: self.open_instance_log_window(target_inst))
                row_layout.addWidget(logs_btn)

                stop_btn = QPushButton("Stop Game")
                stop_btn.setStyleSheet("color: #cc0000; font-weight: bold;")
                stop_btn.setFixedWidth(100)
                stop_btn.clicked.connect(lambda checked=False, target_inst=inst: self.stop_instance(target_inst))
                row_layout.addWidget(stop_btn)

                self.instances_layout.addWidget(row_widget)

            self.instances_group.setVisible(True)
        else:
            self.instances_group.setVisible(False)

        self.update_ui_state()
        self.update_window_size()

    def open_instance_log_window(self, inst: dict):
        """Open or bring to front the dedicated log window for a specific Minecraft instance."""
        if not inst:
            return
        log_win = inst.get("log_window")
        if not log_win:
            log_win = InstanceLogWindow(self, inst, parent=self)
            inst["log_window"] = log_win
        log_win.show()
        log_win.raise_()
        log_win.activateWindow()

    def stop_instance(self, inst: dict):
        """Stop a specific running Minecraft instance."""
        if not inst:
            return
        inst['stopped_by_user'] = True
        if inst.get('window_timer'):
            inst['window_timer'].stop()
            inst['window_timer'] = None
        if inst.get('fallback_timer'):
            inst['fallback_timer'].stop()
            inst['fallback_timer'] = None
        if inst.get('startup_timer'):
            inst['startup_timer'].stop()
            inst['startup_timer'] = None
        ver = inst.get('version', 'Unknown')
        prof = inst.get('profile', 'Default')
        self.log(f"[System] Stopping Minecraft {ver} (Profile: {prof})...")
        self._kill_qprocess_tree(inst.get('process'))

    def stop_running_games(self):
        """Stop running game(s) or cancel the active launch pipeline."""
        if getattr(self, '_pipeline_worker', None) and self._pipeline_worker.isRunning():
            self.log("[System] Cancelling launch sequence...")
            self._pipeline_worker.cancel()
        for inst in list(self.running_instances):
            self.stop_instance(inst)

    def handle_refresh_stdout(self):
        try:
            data = self.refresh_process.readAllStandardOutput().data().decode('utf-8')
            for line in data.splitlines():
                line = line.strip()
                if not line: continue
                try:
                    obj = json.loads(line)
                    if "mc_username" in obj:
                        self.auth_data = obj
                        save_auth_cache(obj)
                        # Remove the old banner and recreate it so the updated skin/presence show
                        self.update_profile_banner()
                except:
                    pass
        except:
            pass
    def set_progress_visible(self, visible):
        self.step_progress_bar.setVisible(visible)
        self.task_progress_bar.setVisible(visible)
        self.update_window_size()

    def on_version_changed(self, index):
        if index < 0:
            if hasattr(self, 'warning_label'):
                self.warning_label.setVisible(False)
            self.update_ui_state()
            return
        if hasattr(self, 'warning_label'):
            self.warning_label.setVisible(self.remember_version)
        self.update_ui_state()
        
    def delete_selected_version(self):
        version_id = self.version_combo.currentData()
        if not version_id: return
        if any(inst.get('version') == version_id for inst in getattr(self, 'running_instances', [])):
            QMessageBox.warning(self, "Version In Use", f"Minecraft {version_id} is currently running. Stop the game before deleting it.")
            return
        
        reply = QMessageBox.question(self, 'Confirm Delete', 
                                     f"Are you sure you want to delete Minecraft {version_id}?\n\n(This will NOT delete your saves, worlds, or source folder)",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                     QMessageBox.StandardButton.No)
        
        if reply == QMessageBox.StandardButton.Yes:
            self.log(f"[System] Deleting version {version_id}...")

            def _do_delete_and_gc(prog_cb, log_cb):
                if not delete_version_files(version_id, log_callback=log_cb, progress_callback=prog_cb):
                    return False
                log_cb(f"[System] Successfully deleted Minecraft {version_id}.")
                try:
                    from garbage_collect import perform_garbage_collection
                    log_cb("[System] Running garbage collection to clean up orphaned assets/libraries...")
                    perform_garbage_collection(log_callback=log_cb, progress_callback=prog_cb)
                    log_cb("[System] Garbage collection finished.")
                except Exception as e:
                    log_cb(f"[Error] Failed to run garbage collection: {e}")
                return True

            ok = run_deletion_with_progress(
                self,
                f"Deleting Minecraft {version_id}...",
                _do_delete_and_gc,
                log_callback=self.log
            )
            self.load_versions()
            if not ok:
                self.log(f"[Error] Failed to delete Minecraft {version_id}.")

    def logout(self):
        reply = QMessageBox.question(self, 'Confirm Logout', 
                                     "Are you sure you want to logout? You will need to sign in with Microsoft again.",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self.set_progress_visible(True)
            self.step_progress_bar.setFormat("Logging out...")
            self.task_progress_bar.setFormat("Current Task: %p%")
            self.task_progress_bar.setValue(25)
            self.status_label.setText("Removing authentication cache...")
            self.log("Removing authentication cache...")
            QApplication.processEvents()
            
            clear_auth_cache()
            self.task_progress_bar.setValue(50)
                
            self.status_label.setText("Clearing player textures...")
            self.log("Clearing player textures...")
            QApplication.processEvents()
            
            from utils import clear_texture_cache
            clear_texture_cache()
            
            self.task_progress_bar.setValue(75)
            self.status_label.setText("Clearing browser cache...")
            self.log("Clearing browser cache...")
            QApplication.processEvents()
            # Clear web engine cache
            profile = QWebEngineProfile.defaultProfile()
            profile.clearHttpCache()
            profile.cookieStore().deleteAllCookies()
            
            self.task_progress_bar.setValue(100)
            self.step_progress_bar.setFormat("%p%")
            self.step_progress_bar.setValue(0)
            self.task_progress_bar.setFormat("Current Task: %p%")
            self.task_progress_bar.setValue(0)
            self.log("[System] Successfully logged out.")
            
            self.auth_data = get_auth_cache()
            is_logged_in = (self.auth_data is not None)
            
            self.logout_btn.setVisible(is_logged_in)
            self.login_btn.setVisible(not is_logged_in)
            
            if is_logged_in:
                self.status_label.setText("Switched to next account.")
                self.update_profile_banner()
            else:
                self.status_label.setText("Logout complete.")
                if hasattr(self, 'banner_widget') and self.banner_widget:
                    try:
                        self.banner_widget.setVisible(False)
                    except RuntimeError:
                        self.banner_widget = None
            self.set_progress_visible(False)
            self.update_ui_state()
            QApplication.processEvents()

    
    def switch_account(self):
        dialog = AccountSwitchDialog(self)
        if dialog.exec():
            # Handle reloading logic
            self.auth_data = get_auth_cache()
            is_logged_in = (self.auth_data is not None)
            
            self.logout_btn.setVisible(is_logged_in)
            self.login_btn.setVisible(not is_logged_in)
            
            if is_logged_in:
                self.update_profile_banner()
            else:
                if hasattr(self, 'banner_widget') and self.banner_widget:
                    try:
                        self.banner_widget.setVisible(False)
                    except RuntimeError:
                        self.banner_widget = None
            
            self.update_ui_state()

            if dialog.wants_new_account:
                # Clear banner for new account before logging in
                self.banner_widget = None
                while self.banner_container.count():
                    item = self.banner_container.takeAt(0)
                    widget = item.widget() if item else None
                    if widget:
                        if hasattr(widget, 'cleanup'):
                            widget.cleanup()
                        widget.setParent(None)
                        widget.deleteLater()
                self._force_new_login = True
                self.do_login()

    def _prompt_windows_linked_accounts(self) -> tuple:
        """
        Check for Microsoft accounts currently linked with Windows and ask the user
        if they want to log in with one of those accounts before proceeding to the
        other login method.
        Returns (should_proceed: bool, selected_account: Optional[dict]).
        """
        try:
            linked_accounts = get_windows_linked_microsoft_accounts()
        except Exception as e:
            self.log(f"[Warning] Could not query Windows-linked Microsoft accounts: {e}")
            linked_accounts = []

        if not linked_accounts:
            return True, None

        dialog = WindowsLinkedAccountsDialog(linked_accounts, parent=self)
        result = dialog.exec()
        if result != QDialog.DialogCode.Accepted:
            self.log("[System] Login cancelled by user.")
            return False, None

        if dialog.use_other_method or not dialog.selected_account:
            self.log("[System] Proceeding with standard login method...")
            return True, None

        selected = dialog.selected_account
        self.log(f"[System] Selected Windows-linked Microsoft account: {selected.get('email')}")
        return True, selected

    def do_login(self):
        self.clear_progress_error()
        self.task_progress_bar.setVisible(False)
        self.step_progress_bar.setVisible(True)
        self.step_progress_bar.setRange(0, 100)
        self.step_progress_bar.setValue(5)
        self.step_progress_bar.setFormat("Checking accounts... (%p%)")
        self.status_label.setText("Checking accounts...")
        QApplication.processEvents()

        proceed, selected_win_acc = self._prompt_windows_linked_accounts()
        if not proceed:
            self._force_new_login = False
            self.step_progress_bar.setVisible(False)
            self.update_profile_banner()
            self.update_ui_state()
            return

        self.clear_progress_error()
        self.log("Starting Microsoft authentication...")
        self.is_just_logging_in = True
        self.is_authenticating = True
        self._login_succeeded = False
        self._browser_auth_done = False
        self._pending_login_hint = selected_win_acc.get("email") if isinstance(selected_win_acc, dict) else None

        self.status_label.setText("Authenticating with Microsoft...")
        self.update_ui_state()
        
        self.step_progress_bar.setRange(0, 100)
        self.step_progress_bar.setValue(20)
        self.step_progress_bar.setFormat("Authenticating with Microsoft... (%p%)")
        self.step_progress_bar.setVisible(True)

        current_profile = get_active_profile_id()
        force = getattr(self, "_force_new_login", False)
        cached_pid = selected_win_acc.get("cached_profile_id") if isinstance(selected_win_acc, dict) else None

        if cached_pid:
            # Reuse the existing profile associated with this Windows-linked Microsoft account
            self.previous_active_profile = current_profile
            accounts = load_accounts()
            accounts["active_profile"] = cached_pid
            save_accounts(accounts)
            self._auth_cancelled_profile = cached_pid
        elif force or self._pending_login_hint:
            self.previous_active_profile = current_profile
            self._auth_cancelled_profile = get_active_profile_id(True)
        else:
            self.previous_active_profile = None
            self._auth_cancelled_profile = current_profile

        cmd_args = [get_script_path("ms_accounts.py"), "--json", "--profile", self._auth_cancelled_profile]
        if sys.platform == "win32" and isinstance(selected_win_acc, dict) and self._pending_login_hint:
            cmd_args.extend(["--wam", "--login-hint", self._pending_login_hint, "--hwnd", str(int(self.winId()))])
            wam_id = selected_win_acc.get("wam_account_id") or selected_win_acc.get("cid")
            if wam_id:
                cmd_args.extend(["--wam-id", str(wam_id)])
        else:
            cmd_args.extend(["--device"])

        self.process = QProcess()
        self.process.setProgram(get_python_cli_executable())
        self.process.setArguments(cmd_args)
        self._force_new_login = False
        self.process.readyReadStandardOutput.connect(self.handle_auth_stdout)
        self.process.readyReadStandardError.connect(self.handle_stderr)
        self.process.finished.connect(self.auth_finished)
        self.process.start()

    def update_profile_banner(self):
        # Clear existing banner and immediately clear self.banner_widget reference
        # so QApplication.processEvents() inside banner_progress cannot touch a deleted C++ widget
        self.banner_widget = None
        while self.banner_container.count():
            item = self.banner_container.takeAt(0)
            widget = item.widget()
            if widget:
                if hasattr(widget, 'cleanup'):
                    widget.cleanup()
                widget.setParent(None)
                widget.deleteLater()
                
        auth_data = get_auth_cache()
        if auth_data:
            def banner_progress(percent, msg):
                self.step_progress_bar.setVisible(True)
                self.step_progress_bar.setRange(0, 100)
                self.step_progress_bar.setValue(percent)
                self.step_progress_bar.setFormat(f"{msg} ({percent}%)")
                self.status_label.setText(msg)
                QApplication.processEvents()

            self.banner_widget = ProfileBannerWidget(auth_data, self, progress_callback=banner_progress)
            h = self.banner_widget.sizeHint()
            if h.isValid() and h.height() > 0:
                self.banner_widget.setMinimumHeight(h.height())
                self._banner_last_h = h.height()
            elif getattr(self, "_banner_last_h", 0):
                self.banner_widget.setMinimumHeight(self._banner_last_h)
            self.banner_container.addWidget(self.banner_widget)
            
            # Hide the progress bar after it's done loading, unless a launch pipeline or authentication is active
            if (
                not getattr(self, 'is_pipeline_running', False)
                and not getattr(self, 'is_authenticating', False)
                and (not getattr(self, 'task_progress_bar', None) or not self.task_progress_bar.isVisible())
            ):
                self.step_progress_bar.setValue(100)
                self.step_progress_bar.setFormat("Ready! (100%)")
                self.status_label.setText("Ready to launch.")
                QTimer.singleShot(600, lambda: self.step_progress_bar.setVisible(False) if not getattr(self, 'is_pipeline_running', False) and not getattr(self, 'is_authenticating', False) else None)
        else:
            if not getattr(self, 'is_pipeline_running', False) and not getattr(self, 'is_authenticating', False):
                self.step_progress_bar.setVisible(False)
                self.status_label.setText("Ready to launch.")

        self.update_ui_state()
        self.update_window_size()

    def _refresh_auth_buttons(self):
        """Show Login when no account is signed in; otherwise show Switch/Logout."""
        if not all(hasattr(self, n) for n in ("login_btn", "logout_btn", "switch_account_btn")):
            return
        try:
            accounts = load_accounts() or {}
            active = accounts.get("active_profile")
            logged_in = bool(active and active in accounts.get("profiles", {}))
        except Exception:
            logged_in = False
        self.login_btn.setVisible(not logged_in)
        self.logout_btn.setVisible(logged_in)
        self.switch_account_btn.setVisible(logged_in)

    def _apply_console_visibility(self):
        popped = bool(self.log_window and self.log_window.isVisible())
        self.results_text.setVisible(self.show_console and not popped)
        self.pop_log_btn.setVisible(self.show_console)
        self.pop_log_btn.setText("Dock Log" if popped else "Pop Out Log")
        self.update_window_size()
        if not self.results_text.isVisible():
            self.resize(self.width(), self.sizeHint().height())  # collapse the space the console used
        else:
            self._refresh_console_log()

    def toggle_log_popout(self):
        if self.log_window and self.log_window.isVisible():
            self.log_window.close()
            return
        if not self.log_window:
            self.log_window = LauncherLogWindow(self)
            self.log_window.closed_sig.connect(lambda: QTimer.singleShot(0, self._apply_console_visibility))
        else:
            self.log_window.refresh()
        self.log_window.show()
        self.log_window.raise_()
        self.log_window.activateWindow()
        self._apply_console_visibility()

    def _lock_banner_minimum(self):
        """Stop the profile banner from being squashed below its natural size."""
        bw = getattr(self, "banner_widget", None)
        if not bw:
            return
        try:
            if bw.isVisible() or not bw.isHidden():
                h = bw.sizeHint()
                if h.isValid() and h.height() > bw.minimumHeight():
                    bw.setMinimumHeight(h.height())
        except RuntimeError:
            self.banner_widget = None

    def update_window_size(self):
        # The window can be freely enlarged: the console (when visible) stretches with it, and the
        # layout's minimum size keeps all controls readable. Height only shrink-wraps when no console is shown.
        self.layout.setSizeConstraint(QVBoxLayout.SizeConstraint.SetMinimumSize)
        self.layout.setContentsMargins(20, 15, 20, 15)
        if not hasattr(self, 'dummy_width_widget'):
            return  # UI still being constructed
        console_shown = self.results_text.isVisible()
        self.results_text.setMinimumHeight(300 if console_shown else 0)
        self.setMaximumHeight(16777215)
        self._lock_banner_minimum()
        self.layout.invalidate()
        self.layout.activate()
        hint = self.layout.sizeHint()

        min_w = max(650, hint.width())
        min_h = hint.height()
        self.setMinimumSize(min_w, min_h)
        self.resize(max(self.width(), min_w), max(self.height(), min_h))

        # Re-measure once banner settles without resetting height to 0
        if not getattr(self, "_min_remeasure_pending", False):
            self._min_remeasure_pending = True

            def _again():
                self._min_remeasure_pending = False
                try:
                    self._lock_banner_minimum()
                    self.layout.invalidate()
                    self.layout.activate()
                    h = self.layout.sizeHint()
                    mw = max(650, h.width())
                    mh = h.height()
                    self.setMinimumSize(mw, mh)
                    self.resize(max(self.width(), mw), max(self.height(), mh))
                except RuntimeError:
                    pass
            QTimer.singleShot(400, _again)
            QTimer.singleShot(1500, _again)

    def load_versions(self):
        old_selected = self.version_combo.currentData()
        if old_selected is None and self.remember_version and self.last_played_version:
            old_selected = self.last_played_version

        self.version_combo.clear()
        QApplication.processEvents()

        versions = list(get_available_versions() or [])
        downloaded = get_downloaded_versions()

        if self.show_downloaded_only and not downloaded:
            self.show_downloaded_only = False
            config = load_launcher_config()
            config["show_downloaded_only"] = False
            save_launcher_config(config)

        count = 0
        if not versions:
            self.is_offline = True
            for v_id in sorted(downloaded):
                self.version_combo.addItem(f"{v_id} (Offline) [Downloaded]", v_id)
                idx = self.version_combo.count() - 1
                self.version_combo.setItemData(idx, True, Qt.ItemDataRole.UserRole + 1)
                count += 1
            if count > 0:
                self.results_text.append(f"[System] Offline mode. Loaded {count} downloaded versions from directory.")
            else:
                self.results_text.append("[System] Offline mode. No downloaded versions available to play.")
        else:
            self.is_offline = False
            config = load_launcher_config()
            hide_date = config.get("hide_release_date", False)
            for v_id, v_type, v_date in versions:
                if v_type not in self.version_filters:
                    continue
                is_downloaded = v_id in downloaded
                if self.show_downloaded_only and not is_downloaded:
                    continue

                type_display = v_type.replace('_', ' ').title()
                if hide_date or not v_date:
                    text = f"{v_id} ({type_display})"
                else:
                    text = f"{v_id} ({type_display} - {v_date})"
                    
                if is_downloaded:
                    text += " [Downloaded]"
                    
                self.version_combo.addItem(text, v_id)
                if is_downloaded:
                    idx = self.version_combo.count() - 1
                    self.version_combo.setItemData(idx, True, Qt.ItemDataRole.UserRole + 1)

                count += 1
            self.results_text.append(f"[System] Loaded {count} versions.")

        if count > 0:
            idx_to_select = 0
            if old_selected:
                for i in range(self.version_combo.count()):
                    if self.version_combo.itemData(i) == old_selected:
                        idx_to_select = i
                        break
            self.version_combo.setCurrentIndex(idx_to_select)

        # Ensure UI updates to reflect the initially selected version
        self.on_version_changed(self.version_combo.currentIndex())
        self.update_ui_state()

    def reload_profiles_combo(self):
        config = load_launcher_config()
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        profiles = config.get("profiles", {"Default": {}})
        self.profile_combo.addItems(profiles.keys())
        active = config.get("active_profile", "Default")
        if active in profiles:
            self.profile_combo.setCurrentText(active)
        self.profile_combo.blockSignals(False)

    def on_profile_changed(self):
        config = load_launcher_config()
        config["active_profile"] = self.profile_combo.currentText()
        save_launcher_config(config)
        self.load_versions()
        self.setup_quick_play()

    def open_directories_dialog(self):
        dialog = DirectoriesDialog(self)
        if dialog.exec():
            dialog.save_directories()
            self.reload_profiles_combo()
            self.load_versions()
            self.setup_quick_play()

    
    def open_options_dialog(self):
        config = load_launcher_config()
        hide_release_date = config.get("hide_release_date", False)
        dev_mode = config.get("dev_mode", getattr(self, "dev_mode", False))
        show_access_token = config.get("show_access_token", getattr(self, "show_access_token", False))
        downloaded = get_downloaded_versions()
        dialog = VersionOptionsDialog(
            self.version_filters,
            self.show_downloaded_only,
            self.skip_verification,
            self.show_console,
            self.remember_version,
            hide_release_date,
            dev_mode,
            show_access_token,
            self
        )

        # Disable "Show only downloaded" if none exist
        if not downloaded:
            self.show_downloaded_only = False
            dialog.downloaded_cb.setChecked(False)
            dialog.downloaded_cb.setEnabled(False)
            dialog.downloaded_cb.setToolTip("No versions have been fully downloaded yet.")
            
        if dialog.exec():
            self.version_filters = dialog.get_selected_filters()
            self.show_downloaded_only = dialog.get_downloaded_only()
            self.skip_verification = dialog.get_skip_verification()
            self.show_console = dialog.get_show_console()
            
            old_show_token = getattr(self, 'show_access_token', False)
            self.show_access_token = dialog.get_show_access_token()
            self.remember_version = dialog.get_remember_version()
            self.hide_release_date = dialog.get_hide_release_date()
            self.dev_mode = dialog.get_dev_mode()

            self._apply_console_visibility()

            # Save to config
            config = load_launcher_config()
            config["version_filters"] = self.version_filters
            config["show_downloaded_only"] = self.show_downloaded_only
            config["skip_verification"] = self.skip_verification
            config["show_console"] = self.show_console
            config["show_access_token"] = self.show_access_token
            config["remember_version"] = self.remember_version
            config["hide_release_date"] = self.hide_release_date
            config["dev_mode"] = self.dev_mode
            gc_del_logs, gc_log_val, gc_log_unit = dialog.get_log_gc_settings()
            config["gc_delete_logs"] = gc_del_logs
            config["gc_log_max_age_value"] = gc_log_val
            config["gc_log_max_age_unit"] = gc_log_unit
            save_launcher_config(config)
            if old_show_token != self.show_access_token:
                self._refresh_console_log()
            try:
                from garbage_collect import perform_garbage_collection
                perform_garbage_collection(log_callback=self.log)
            except Exception:
                pass
            
            self.update_profile_banner()
            self.load_versions()
            if not self.running_instances and not self.is_pipeline_running:
                self.status_label.setText("Ready to launch.")

    def setup_quick_play(self):
        if hasattr(self, 'quick_play_combo'):
            self.action_layout.removeWidget(self.quick_play_combo)
            self.quick_play_combo.deleteLater()
            
        self.quick_play_combo = QComboBox()
        self.quick_play_combo.setMinimumHeight(35)
        self.quick_play_combo.addItem("No Quick Play", None)
        try:
            from utils import get_quick_play_options, get_dir
            for qp in get_quick_play_options(get_dir("base"), include_realms=False):
                self.quick_play_combo.addItem(qp['display'], qp)
        except Exception as e:
            print("Failed to load quick play options:", e)
            
        # Insert it before the play button
        self.action_layout.insertWidget(0, self.quick_play_combo, stretch=2)
        self.update_ui_state()

        # Load Realms asynchronously so window startup is instant
        class _RealmsThread(QtCore.QThread):
            loaded = QtCore.pyqtSignal(list)
            def run(self_thread):
                try:
                    from utils import fetch_realms_options
                    self_thread.loaded.emit(fetch_realms_options())
                except Exception:
                    pass

        def _on_realms_loaded(realms_list):
            if not hasattr(self, 'quick_play_combo') or not self.quick_play_combo:
                return
            existing_keys = set()
            for i in range(self.quick_play_combo.count()):
                d = self.quick_play_combo.itemData(i)
                if isinstance(d, dict):
                    existing_keys.add((d.get("type"), str(d.get("id"))))
            for qp in realms_list:
                if (qp.get("type"), str(qp.get("id"))) in existing_keys:
                    continue
                self.quick_play_combo.addItem(qp['display'], qp)
                if qp.get('expired'):
                    idx = self.quick_play_combo.count() - 1
                    item = self.quick_play_combo.model().item(idx)
                    if item:
                        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)

        self._realms_thread = _RealmsThread(self)
        self._realms_thread.loaded.connect(_on_realms_loaded)
        self._realms_thread.start()

    def _format_log_line(self, line: str) -> str:
        if getattr(self, 'show_access_token', False):
            return line
        cleaned = line
        if hasattr(self, 'auth_data') and self.auth_data:
            token = self.auth_data.get('mc_access_token')
            if token and token != 'offline' and len(token) > 5 and token in cleaned:
                cleaned = cleaned.replace(token, "[ACCESS_TOKEN_HIDDEN]")
        cleaned = re.sub(r'(--accessToken\s+)(\S+)', r'\1[ACCESS_TOKEN_HIDDEN]', cleaned)
        cleaned = re.sub(r'("auth_access_token":\s*")[^"]+(")', r'\1[ACCESS_TOKEN_HIDDEN]\2', cleaned)
        cleaned = re.sub(r'(\baccessToken=)[^\s&,)]+', r'\1[ACCESS_TOKEN_HIDDEN]', cleaned)
        return cleaned

    def _refresh_console_log(self):
        if not hasattr(self, 'results_text'):
            return
        self.results_text.clear()
        for raw_line in getattr(self, '_raw_log_lines', []):
            self.results_text.append(self._format_log_line(raw_line))
        scrollbar = self.results_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        if getattr(self, 'log_window', None) and self.log_window.isVisible():
            self.log_window.refresh()

    def log(self, message):
        clean_msg = message.strip()
        if not hasattr(self, '_raw_log_lines'):
            self._raw_log_lines = []
        self._raw_log_lines.append(clean_msg)
        formatted = self._format_log_line(clean_msg)
        self.results_text.append(formatted)
        scrollbar = self.results_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        if getattr(self, 'log_window', None) and self.log_window.isVisible():
            self.log_window.append_line(formatted)

    def check_opengl_backend_prompt(self):
        """Prompt user before changing preferredGraphicsBackend to OpenGL in options.txt."""
        try:
            base_dir = get_dir("base")
            options_txt = base_dir / "options.txt"
            if not options_txt.exists():
                return
            opt_content = options_txt.read_text(encoding="utf-8", errors="ignore")
            has_default_backend = bool(re.search(r'preferredGraphicsBackend\s*:\s*["\']?default["\']?', opt_content))
            has_unclean = bool(re.search(r'startedCleanly\s*:\s*false', opt_content))
            if has_default_backend and has_unclean:
                reply = QMessageBox.warning(
                    self,
                    "Graphics Settings Warning",
                    "Minecraft previously had an unclean startup (it may have crashed).\n\n"
                    "Do you want to change your settings to prefer OpenGL (preferredGraphicsBackend: \"opengl\")?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes
                )
                if reply == QMessageBox.StandardButton.Yes:
                    new_content = re.sub(
                        r'preferredGraphicsBackend\s*:\s*["\']?default["\']?',
                        'preferredGraphicsBackend:"opengl"',
                        opt_content
                    )
                    options_txt.write_text(new_content, encoding="utf-8")
                    self.log('[INFO] User confirmed: Switched preferredGraphicsBackend to "opengl" in options.txt')
                else:
                    new_content = re.sub(
                        r'startedCleanly\s*:\s*false',
                        'startedCleanly:true',
                        opt_content
                    )
                    options_txt.write_text(new_content, encoding="utf-8")
                    self.log('[INFO] User declined: Kept preferredGraphicsBackend as "default" in options.txt')
        except Exception as e:
            self.log(f"[Warning] Failed checking graphics backend settings: {e}")

    def start_launch_sequence(self):
        self._is_source_pipeline = False
        self.selected_version = self.version_combo.currentData()
        if not self.selected_version:
            return

        self.clear_progress_error()
        self.check_opengl_backend_prompt()

        self.selected_profile_name = self.profile_combo.currentText() if hasattr(self, 'profile_combo') else "Default"
        self.selected_quick_play = self.quick_play_combo.currentData() if hasattr(self, 'quick_play_combo') else None
        self.is_pipeline_running = True
        self.update_ui_state()
        if not self.running_instances:
            self.results_text.clear()
            self._raw_log_lines = []
        else:
            self.log("")
        
        self.log(f"=== Starting Launch Sequence for Minecraft {self.selected_version} ===")
        self.setWindowTitle(f"Minecraft - {self.selected_version} - Downloading...")
        
        # Ensure task progress bar format is reset from any previous "Game Running!" state
        self.task_progress_bar.setFormat("Current Task: %p%")
        
        # Check Auth Cache first
        cached_auth = get_auth_cache()
        if cached_auth:
            self.log("[System] Found valid authentication cache. Skipping login.")
            self.auth_data = cached_auth
            # Skip Step 1 and go to Step 2 (start the pipeline)
            self.start_download_pipeline()
            return
        
        if getattr(self, 'is_offline', False):
            self.log("[System] Offline mode detected. Skipping online authentication.")
            
            cache_loaded = False
            try:
                if Path('.cache/active_profile.txt').exists():
                    with open('.cache/active_profile.txt', 'r') as f:
                        active_profile = f.read().strip()
                    account_file = Path(f'.cache/{active_profile}/account.json')
                    if account_file.exists():
                        with open(account_file, 'r') as f:
                            acc = json.load(f)
                        self.auth_data = {
                            'mc_username': acc.get('mc_username', 'OfflinePlayer'),
                            'mc_uuid': acc.get('mc_uuid', '00000000-0000-0000-0000-000000000000'),
                            'mc_access_token': acc.get('mc_access_token', 'offline'),
                            'xbox_xuid': acc.get('xbox_xuid', 'offline'),
                            'launch_client_id': acc.get('launch_client_id', 'PyMcLauncher')
                        }
                        self.log("[System] Loaded offline auth from .cache folder.")
                        cache_loaded = True
            except Exception as e:
                self.log(f"[Warning] Failed to load from .cache: {e}")

            if not cache_loaded:
                config_path = get_dir("versions") / self.selected_version / "command_config.json"
                if config_path.exists():
                    try:
                        with open(config_path, "r") as f:
                            cfg = json.load(f)
                        self.auth_data = {
                            'mc_username': cfg.get('auth_player_name', 'OfflinePlayer'),
                            'mc_uuid': cfg.get('auth_uuid', '00000000-0000-0000-0000-000000000000'),
                            'mc_access_token': cfg.get('auth_access_token', 'offline'),
                            'xbox_xuid': cfg.get('auth_xuid', 'offline'),
                            'launch_client_id': cfg.get('clientid', 'PyMcLauncher')
                        }
                        self.log("[System] Loaded offline auth from command_config.json.")
                    except Exception as e:
                        self.log(f"[Error] Failed to parse command_config: {e}")
                        self.auth_data = {'mc_username': 'OfflinePlayer', 'mc_uuid': '0', 'mc_access_token': '0', 'xbox_xuid': '0', 'launch_client_id': '0'}
                else:
                    self.log("[System] No command_config found. Using generic offline profile.")
                    self.auth_data = {'mc_username': 'OfflinePlayer', 'mc_uuid': '0', 'mc_access_token': '0', 'xbox_xuid': '0', 'launch_client_id': '0'}
            self.start_download_pipeline()
            return

        # Step 1: Authentication
        proceed, selected_win_acc = self._prompt_windows_linked_accounts()
        if not proceed:
            self._force_new_login = False
            self.is_pipeline_running = False
            self.setWindowTitle("PyMcLauncher")
            self.status_label.setText("Launch cancelled.")
            self.update_ui_state()
            return

        self._pending_login_hint = selected_win_acc.get("email") if isinstance(selected_win_acc, dict) else None
        cached_pid = selected_win_acc.get("cached_profile_id") if isinstance(selected_win_acc, dict) else None
        if cached_pid:
            accounts = load_accounts()
            accounts["active_profile"] = cached_pid
            save_accounts(accounts)
            target_profile_id = cached_pid
        else:
            target_profile_id = get_active_profile_id(getattr(self, "_force_new_login", False))

        self.is_authenticating = True
        self.set_progress_visible(True)
        self.status_label.setText("[1/6] Authenticating with Microsoft...")
        self.step_progress_bar.setRange(0, 100)
        self.step_progress_bar.setValue(0)
        self.step_progress_bar.setFormat("Step 1/6 - Authentication")
        self.task_progress_bar.setRange(0, 0)
        self.task_progress_bar.setFormat("Current Task: %p%")
        
        self.is_just_logging_in = False
        self._browser_auth_done = False
        cmd_args = [get_script_path("ms_accounts.py"), "--json", "--profile", target_profile_id]
        if sys.platform == "win32" and isinstance(selected_win_acc, dict) and self._pending_login_hint:
            cmd_args.extend(["--wam", "--login-hint", self._pending_login_hint, "--hwnd", str(int(self.winId()))])
            wam_id = selected_win_acc.get("wam_account_id") or selected_win_acc.get("cid")
            if wam_id:
                cmd_args.extend(["--wam-id", str(wam_id)])
        else:
            cmd_args.extend(["--device"])

        self.process = QProcess()
        self.process.setProgram(get_python_cli_executable())
        self.process.setArguments(cmd_args)
        self._force_new_login = False
        self.process.readyReadStandardOutput.connect(self.handle_auth_stdout)
        self.process.readyReadStandardError.connect(self.handle_stderr)
        self.process.finished.connect(self.auth_finished)
        self.process.start()

    def handle_auth_stdout(self):
        data = self.process.readAllStandardOutput().data().decode('utf-8')
        for line in data.splitlines():
            line = line.strip()
            if not line: continue
            
            try:
                obj = json.loads(line)
                if not isinstance(obj, dict):
                    self.log(f"AUTH: {line}")
                    continue
            except json.JSONDecodeError:
                self.log(f"AUTH: {line}")
                continue

            if obj.get("status") == "pending_login":
                url = obj.get("verification_uri")
                code = obj.get("user_code")
                hint = obj.get("login_hint") or getattr(self, "_pending_login_hint", None)
                if code:
                    self.log(f"[System] Authentication code: {code} (Copied to clipboard)")
                    try:
                        from PyQt6.QtGui import QGuiApplication
                        cb = QGuiApplication.clipboard()
                        if cb:
                            cb.setText(code)
                    except Exception:
                        pass
                self.log("[System] Waiting for user to complete Microsoft login in popup window...")
                self.open_browser(url, login_hint=hint, user_code=code)
            elif "mc_username" in obj:
                if getattr(self, "_pending_login_hint", None) and not obj.get("msa_email"):
                    obj["msa_email"] = self._pending_login_hint
                self.auth_data = obj
                self._login_succeeded = True
                save_auth_cache(obj)
                self._cleanup_browser()
                self.step_progress_bar.setVisible(True)
                self.step_progress_bar.setRange(0, 100)
                self.step_progress_bar.setValue(50)
                self.step_progress_bar.setFormat("Loading profile and textures... (%p%)")
                self.status_label.setText("Loading profile and textures...")
                QApplication.processEvents()
            elif "error" in obj:
                self.log(f"[Error] {obj.get('error')}: {obj.get('details')}")

    def _finalize_auth(self, obj):
        save_auth_cache(obj)
        self.logout_btn.setVisible(True)
        self.login_btn.setVisible(False)
        self.update_profile_banner()
        self.log(f"[Success] Authenticated as {obj['mc_username']}")

    def handle_stderr(self):
        if not self.process: return
        data = self.process.readAllStandardError().data().decode('utf-8')
        for line in data.splitlines():
            line = line.strip()
            if line:
                self.log(line)

    def auth_finished(self, exitCode, exitStatus):
        self._cleanup_browser()

        if exitCode != 0 or not self.auth_data:
            self.is_authenticating = False
            self.step_progress_bar.setVisible(False)
            self.step_progress_bar.setRange(0, 100)
            self.is_pipeline_running = False
            self.log("[Error] Authentication failed or cancelled.")
            self.status_label.setText("Authentication Failed.")
            self.show_progress_error("Authentication", "Login Failed or Cancelled")
            
            # Revert profile changes if cancelled
            if getattr(self, 'previous_active_profile', None):
                accounts = load_accounts()
                accounts['active_profile'] = self.previous_active_profile
                save_accounts(accounts)
                
                # Cleanup the cancelled profile folder
                cancelled_prof = getattr(self, '_auth_cancelled_profile', None)
                if cancelled_prof and cancelled_prof != self.previous_active_profile:
                    import shutil, os
                    path = f".cache/{cancelled_prof}"
                    if os.path.exists(path):
                        shutil.rmtree(path, ignore_errors=True)
                
                self.previous_active_profile = None
                self._auth_cancelled_profile = None

            self.auth_data = get_auth_cache() # Reload the reverted profile's auth data
            self.update_profile_banner() # Show previous account's banner
            is_logged_in = bool(self.auth_data)
            self.logout_btn.setVisible(is_logged_in)
            self.login_btn.setVisible(not is_logged_in)
            self.update_running_instances_ui()
            self.update_ui_state()
            return

        # Keep the loading bar visible while finalizing authentication & loading profile textures
        self.step_progress_bar.setVisible(True)
        self.step_progress_bar.setRange(0, 100)
        self.step_progress_bar.setValue(60)
        self.step_progress_bar.setFormat("Finalizing authentication... (%p%)")
        self.status_label.setText("Finalizing authentication...")
        QApplication.processEvents()

        self._finalize_auth(self.auth_data)
        self.is_authenticating = False

        if self.is_just_logging_in:
            self.is_pipeline_running = False
            self.step_progress_bar.setRange(0, 100)
            self.step_progress_bar.setValue(100)
            self.step_progress_bar.setFormat("Login complete! (100%)")
            self.log("[System] Login complete. Ready to play.")
            self.status_label.setText("Ready to launch.")
            self.update_ui_state()
            self.update_window_size()
            QTimer.singleShot(800, lambda: self.step_progress_bar.setVisible(False) if not getattr(self, 'is_pipeline_running', False) and not getattr(self, 'is_authenticating', False) else None)
        else:
            self.start_download_pipeline()
        
    def start_download_pipeline(self):
        self.clear_progress_error()
        self.is_pipeline_running = True
        self.task_progress_bar.setFormat("Current Task: %p%")
        self.set_progress_visible(True)
        self.update_ui_state()
        self.log("")
        self.log("=== Authentication Complete. Proceeding to Downloads ===")

        is_trial = bool(self.auth_data and self.auth_data.get("owns_minecraft") is False)
        self._pipeline_worker = DownloadPipelineWorker(
            version=self.selected_version,
            auth_data=self.auth_data,
            is_offline=getattr(self, 'is_offline', False),
            is_trial=is_trial,
            dev_mode=getattr(self, 'dev_mode', False),
            quick_play=getattr(self, 'selected_quick_play', None),
            skip_verification=getattr(self, 'skip_verification', True),
            parent=self
        )
        self._pipeline_worker.progress_sig.connect(self._on_worker_progress)
        self._pipeline_worker.log_sig.connect(self.log)
        self._pipeline_worker.done_sig.connect(self._on_worker_done)
        self._pipeline_worker.start()

    def _on_worker_progress(self, current_step: int, total_steps: int, step_name: str, task_percent: float, status_str: str):
        if not self.is_pipeline_running:
            return
        base_percent = ((current_step - 1) / total_steps) * 100
        added_percent = (task_percent / 100.0) * (100.0 / total_steps)
        self.step_progress_bar.setValue(int(base_percent + added_percent))
        self.step_progress_bar.setFormat(f"Step {current_step}/{total_steps} - %p%")

        self.task_progress_bar.setRange(0, 100)
        self.task_progress_bar.setValue(int(task_percent))
        self.task_progress_bar.setFormat("Current Task: %p%")

        if status_str:
            self.status_label.setText(status_str)

    def _on_worker_done(self, success: bool, result, error_msg: str):
        self._pipeline_worker = None
        if not success:
            self.is_pipeline_running = False
            self.log("")
            self.log(f"[Error] {error_msg}")
            started_remaining = [x for x in getattr(self, 'running_instances', []) if x.get('started')]
            if started_remaining:
                # Other instances are already running: inform user without breaking the running games
                rem_count = len(started_remaining)
                self.status_label.setText(f"[Error] Launch failed: {error_msg}. {rem_count} instance{'s' if rem_count > 1 else ''} still running.")
                self.task_progress_bar.setRange(0, 100)
                self.task_progress_bar.setValue(100)
                self.task_progress_bar.setFormat(f"Game Running! ({rem_count} instances)" if rem_count > 1 else "Game Running!")
                self.step_progress_bar.setRange(0, 100)
                self.step_progress_bar.setValue(100)
            else:
                self.status_label.setText(f"Launch Failed: {error_msg}")
                self.show_progress_error("Launch Pipeline", error_msg)
            self.update_running_instances_ui()
            self.update_ui_state()
            return

        self.clear_progress_error()
        filled_command, cwd = result
        self.load_versions()

        self.step_progress_bar.setValue(100)
        self.step_progress_bar.setFormat("Step Done - 100%")
        self.task_progress_bar.setRange(0, 0)
        self.task_progress_bar.setFormat("Launching...")
        self.status_label.setText(f"Launching Minecraft {self.selected_version}...")

        self.log("")
        self.log(f"[COMMAND] {' '.join(filled_command)}")

        self._launch_game_instance(filled_command, cwd)

    def _launch_game_instance(self, command: list, cwd: str):
        """Spawn a dedicated QProcess for a Minecraft game instance so multiple instances can run concurrently."""
        game_proc = QProcess(self)
        if cwd:
            game_proc.setWorkingDirectory(cwd)
        program = command[0]
        args = command[1:]
        game_proc.setProgram(program)
        game_proc.setArguments(args)

        inst_id = self._next_instance_id
        self._next_instance_id += 1

        profile_name = getattr(self, 'selected_profile_name', None) or (
            self.profile_combo.currentText() if hasattr(self, 'profile_combo') else "Default"
        )
        mc_username = self.auth_data.get('mc_username', 'Player') if self.auth_data else 'Player'

        inst = {
            "id": inst_id,
            "process": game_proc,
            "version": self.selected_version,
            "profile": profile_name,
            "username": mc_username,
            "started": False,
            "stopped_by_user": False,
            "window_timer": None,
            "fallback_timer": None,
            "log_lines": [],
            "log_window": None,
        }
        self.running_instances.append(inst)
        if len(self.running_instances) > 1:
            self.log(f"[System] Starting instance {inst_id} ({self.selected_version}). Individual game logs are available via 'Show Logs'.")
        self.update_ui_state()

        game_proc.readyReadStandardOutput.connect(lambda i=inst: self._handle_instance_stdout(i))
        game_proc.readyReadStandardError.connect(lambda i=inst: self._handle_instance_stderr(i))
        game_proc.finished.connect(
            lambda exitCode, exitStatus, i=inst: self._on_instance_finished(i, exitCode, exitStatus)
        )
        game_proc.start()

        # Window detection timer: polls every 150ms for the actual game window
        window_timer = QTimer(self)
        window_timer.setInterval(150)
        window_timer.timeout.connect(lambda i=inst: self._poll_instance_window(i))
        inst["window_timer"] = window_timer
        window_timer.start()

        # Fallback timer (60s) in case window enumeration doesn't catch it
        fallback_timer = QTimer(self)
        fallback_timer.setSingleShot(True)
        fallback_timer.timeout.connect(lambda i=inst: self._mark_instance_started(i))
        inst["fallback_timer"] = fallback_timer
        fallback_timer.start(60000)

    def _poll_instance_window(self, inst: dict):
        if inst.get("started") or inst not in self.running_instances:
            if inst.get("window_timer"):
                inst["window_timer"].stop()
                inst["window_timer"] = None
            return

        proc = inst.get("process")
        if not proc or proc.state() == QProcess.ProcessState.NotRunning:
            if inst.get("window_timer"):
                inst["window_timer"].stop()
                inst["window_timer"] = None
            return

        pid = proc.processId()
        if not pid:
            return

        try:
            if is_minecraft_window_visible(pid, inst.get("version", "")):
                self._mark_instance_started(inst)
        except Exception:
            pass

    def _mark_instance_started(self, inst: dict):
        if inst.get("started") or inst not in self.running_instances:
            return
        proc = inst.get("process")
        if not proc or proc.state() == QProcess.ProcessState.NotRunning:
            return

        if inst.get("window_timer"):
            inst["window_timer"].stop()
            inst["window_timer"] = None
        if inst.get("fallback_timer"):
            inst["fallback_timer"].stop()
            inst["fallback_timer"] = None
        if inst.get("startup_timer"):
            inst["startup_timer"].stop()
            inst["startup_timer"] = None

        inst["started"] = True
        self.is_pipeline_running = False
        self.clear_progress_error()

        self.step_progress_bar.setRange(0, 100)
        self.step_progress_bar.setValue(100)
        self.step_progress_bar.setFormat("Step Done - 100%")
        self.task_progress_bar.setRange(0, 100)
        self.task_progress_bar.setValue(100)

        started_count = len([x for x in self.running_instances if x.get("started")])
        if started_count > 1:
            self.task_progress_bar.setFormat(f"Game Running! ({started_count} instances)")
            self.setWindowTitle(f"PyMcLauncher - {started_count} Instances Playing")
            self.status_label.setText(f"Minecraft is running ({started_count} active instances).")
        else:
            self.task_progress_bar.setFormat("Game Running!")
            self.setWindowTitle(f"Minecraft - {inst['version']} - Playing")
            self.status_label.setText(f"Minecraft {inst['version']} is now running.")

        if self.remember_version:
            config = load_launcher_config()
            config["last_played_version"] = inst["version"]
            save_launcher_config(config)
            self.last_played_version = inst["version"]

        self.log(f"[SUCCESS] Minecraft {inst['version']} ({inst['profile']}) launched successfully!")
        self.update_running_instances_ui()

    def _check_instance_output_line(self, inst: dict, line: str):
        if not inst.get("started"):
            if (
                "OpenAL initialized" in line
                or "Sound engine started" in line
                or "Created: " in line
                or "Backend library: LWJGL" in line
            ):
                self._mark_instance_started(inst)

    def _log_instance_line(self, inst: dict, line: str):
        if "log_lines" not in inst:
            inst["log_lines"] = []
        inst["log_lines"].append(line)

        log_win = inst.get("log_window")
        if log_win and hasattr(log_win, "append_log"):
            log_win.append_log(self._format_log_line(line))

        # When only 1 instance is running, mirror to the main console log
        # When multiple instances are running, main console log displays main python program logs
        running_count = len(getattr(self, 'running_instances', []))
        if running_count <= 1:
            self.log(line)

    def _handle_instance_stdout(self, inst: dict):
        proc = inst.get("process")
        if not proc:
            return
        data = proc.readAllStandardOutput().data().decode("utf-8", errors="replace")
        for line in data.splitlines():
            line = line.strip()
            if not line:
                continue
            self._check_instance_output_line(inst, line)
            self._log_instance_line(inst, line)

    def _handle_instance_stderr(self, inst: dict):
        proc = inst.get("process")
        if not proc:
            return
        data = proc.readAllStandardError().data().decode("utf-8", errors="replace")
        for line in data.splitlines():
            line = line.strip()
            if not line:
                continue
            self._check_instance_output_line(inst, line)
            self._log_instance_line(inst, line)

    def show_progress_error(self, step_title: str, error_detail: str):
        """Show a failure using the native progress bar style plus the red Windows taskbar error state."""
        self.step_progress_bar.setStyleSheet("")
        self.task_progress_bar.setStyleSheet("")
        self.step_progress_bar.setRange(0, 100)
        self.step_progress_bar.setValue(0)
        self.step_progress_bar.setFormat(f"Failed: {step_title}")

        self.task_progress_bar.setRange(0, 100)
        self.task_progress_bar.setValue(0)
        self.task_progress_bar.setFormat(f"Error: {error_detail}")

        self.set_progress_visible(True)
        set_taskbar_progress_state(self, "error")

    def clear_progress_error(self):
        """Restore default progress bar styling and clear the taskbar error state."""
        self.step_progress_bar.setStyleSheet("")
        self.task_progress_bar.setStyleSheet("")
        set_taskbar_progress_state(self, "none")

    def _on_instance_finished(self, inst: dict, exitCode: int, exitStatus):
        if inst.get("window_timer"):
            inst["window_timer"].stop()
            inst["window_timer"] = None
        if inst.get("fallback_timer"):
            inst["fallback_timer"].stop()
            inst["fallback_timer"] = None
        if inst.get("startup_timer"):
            inst["startup_timer"].stop()
            inst["startup_timer"] = None

        was_started = inst.get("started", False)
        stopped_by_user = inst.get("stopped_by_user", False)
        is_crash = (exitCode != 0 and not stopped_by_user)

        if inst in self.running_instances:
            self.running_instances.remove(inst)

        ver = inst.get("version", "Unknown")
        prof = inst.get("profile", "Default")

        log_win = inst.get("log_window")
        if log_win and hasattr(log_win, "update_instance_state"):
            log_win.update_instance_state(is_running=False, exit_code=exitCode)

        self.log("")
        if stopped_by_user:
            self.log(f"=== Minecraft {ver} ({prof}) Stopped! ===")
        elif is_crash:
            if not was_started:
                self.log(f"[Error] Minecraft {ver} ({prof}) crashed during startup with code {exitCode}.")
            else:
                self.log(f"[Error] Minecraft {ver} ({prof}) crashed with exit code {exitCode}.")
        else:
            self.log(f"=== Minecraft {ver} ({prof}) Closed! ===")

        started_remaining = [x for x in self.running_instances if x.get("started")]
        unstarted_remaining = [x for x in self.running_instances if not x.get("started")]

        # If this instance was still starting, ensure pipeline is marked not running
        if not was_started and not unstarted_remaining:
            self.is_pipeline_running = False

        if started_remaining:
            # Other instances are still running
            # Reset task progress bar from any indeterminate (0, 0) state
            self.task_progress_bar.setRange(0, 100)
            self.task_progress_bar.setValue(100)
            self.step_progress_bar.setRange(0, 100)
            self.step_progress_bar.setValue(100)

            rem_count = len(started_remaining)
            if rem_count > 1:
                self.task_progress_bar.setFormat(f"Game Running! ({rem_count} instances)")
                self.setWindowTitle(f"PyMcLauncher - {rem_count} Instances Playing")
            else:
                self.task_progress_bar.setFormat("Game Running!")
                self.setWindowTitle(f"Minecraft - {started_remaining[0]['version']} - Playing")

            surviving_text = f"{rem_count} instance{'s' if rem_count > 1 else ''} still running"
            if is_crash:
                phase = "during startup" if not was_started else "unexpectedly"
                self.status_label.setText(f"[Crash] Minecraft {ver} ({prof}) crashed {phase} (code {exitCode}). {surviving_text}.")
            elif stopped_by_user:
                self.status_label.setText(f"Stopped Minecraft {ver} ({prof}). {surviving_text}.")
            else:
                self.status_label.setText(f"Minecraft {ver} ({prof}) closed. {surviving_text}.")

            self.update_running_instances_ui()
            self.update_ui_state()

        elif unstarted_remaining:
            # Another instance is currently in the middle of launching
            if is_crash:
                self.log(f"[Warning] Minecraft {ver} ({prof}) crashed (code {exitCode}) while next instance is starting.")
            self.update_running_instances_ui()
            self.update_ui_state()

        else:
            # 0 instances left
            self.update_running_instances_ui()
            if not self.is_pipeline_running:
                if is_crash:
                    self.show_progress_error(
                        f"Minecraft {ver} Crashed",
                        f"Process exited with code {exitCode}"
                    )
                    self.status_label.setText(f"Launch Failed: Minecraft {ver} ({prof}) exited with code {exitCode}.")
                    self.update_ui_state()
                else:
                    self.status_label.setText("Game stopped." if stopped_by_user else "Game closed.")
                    self.reset_ui()

    def reset_ui(self):
        self.is_pipeline_running = False
        self.is_authenticating = False
        self.clear_progress_error()
        started_remaining = [x for x in getattr(self, 'running_instances', []) if x.get('started')]
        if started_remaining:
            if len(started_remaining) > 1:
                self.setWindowTitle(f"PyMcLauncher - {len(started_remaining)} Instances Playing")
                self.task_progress_bar.setFormat(f"Game Running! ({len(started_remaining)} instances)")
            else:
                self.setWindowTitle(f"Minecraft - {started_remaining[0]['version']} - Playing")
                self.task_progress_bar.setFormat("Game Running!")
            self.step_progress_bar.setRange(0, 100)
            self.step_progress_bar.setValue(100)
            self.task_progress_bar.setRange(0, 100)
            self.task_progress_bar.setValue(100)
            self.update_running_instances_ui()
            self.update_ui_state()
            return

        self.setWindowTitle("PyMcLauncher")
        self.step_progress_bar.setRange(0, 100)
        self.step_progress_bar.setValue(100)
        self.step_progress_bar.setFormat("%p%")
        self.task_progress_bar.setRange(0, 100)
        self.task_progress_bar.setValue(100)
        self.task_progress_bar.setFormat("Current Task: %p%")
        self.set_progress_visible(False)
        self.update_running_instances_ui()
        self.update_ui_state()

    # --- Browser Window Code (from old GUI) ---
    def open_browser(self, url, login_hint: Optional[str] = None, user_code: Optional[str] = None):
        class LoginBrowserWindow(QMainWindow):
            def __init__(self, parent_launcher):
                super().__init__()
                self.parent_launcher = parent_launcher
            def closeEvent(self, event):
                if getattr(self, '_is_closing', False):
                    super().closeEvent(event)
                    return
                self._is_closing = True
                
                # Immediately destroy the browser to violently abort any active Passkey/WebAuthn prompts
                self.parent_launcher._cleanup_browser()
                
                if (
                    getattr(self.parent_launcher, 'is_just_logging_in', False)
                    and not getattr(self.parent_launcher, '_login_succeeded', False)
                    and not getattr(self.parent_launcher, '_browser_auth_done', False)
                ):
                    if getattr(self.parent_launcher, 'process', None):
                        self.parent_launcher.process.kill()
                super().closeEvent(event)

        self._browser_login_hint = (login_hint or "").strip() or None
        self._browser_user_code = (user_code or "").strip() or None
        self._browser_auth_done = False
        self.browser_window = LoginBrowserWindow(self)
        if user_code:
            self.browser_window.setWindowTitle(f"Microsoft Login - Code: {user_code} (Copied to Clipboard)")
        else:
            self.browser_window.setWindowTitle("Microsoft Login - Minecraft Launcher")
        self.browser_window.setFixedSize(600, 750)
        
        self.browser_central = QWidget()
        main_layout = QVBoxLayout(self.browser_central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        if user_code:
            code_banner = QWidget()
            code_banner.setStyleSheet("background-color: #0078D7; color: white;")
            banner_layout = QHBoxLayout(code_banner)
            banner_layout.setContentsMargins(12, 8, 12, 8)
            banner_lbl = QLabel(f"Code: <b><font size='+1'>{user_code}</font></b>  (Copied to clipboard - paste if prompted)")
            banner_lbl.setStyleSheet("color: white; font-size: 13px;")
            banner_layout.addWidget(banner_lbl, stretch=1)
            copy_btn = QPushButton("Copy Code")
            copy_btn.setStyleSheet("background-color: white; color: #0078D7; font-weight: bold; border-radius: 4px; padding: 4px 12px;")
            copy_btn.clicked.connect(lambda: QGuiApplication.clipboard().setText(user_code))
            banner_layout.addWidget(copy_btn)
            main_layout.addWidget(code_banner)

        view_container = QWidget()
        self.browser_layout = QGridLayout(view_container)
        self.browser_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(view_container, stretch=1)

        self._webauthn_js_active = False
        self._webauthn_os_active = False

        self.loading_widget = QWidget()
        self.loading_widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.loading_widget.setAutoFillBackground(True)
        self.loading_widget.setStyleSheet("background-color: rgba(32, 32, 32, 220);")
        loading_layout = QVBoxLayout(self.loading_widget)
        loading_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.loading_label = QLabel("Loading Secure Login...")
        self.loading_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.loading_label.setStyleSheet(
            "color: white; font-size: 18px; font-weight: bold; "
            "background-color: rgba(20, 20, 20, 210); padding: 18px 28px; border-radius: 8px;"
        )
        loading_layout.addWidget(self.loading_label)
        
        from utils import get_active_profile_id
        import os
        active_id = get_active_profile_id()
        profile_path = os.path.abspath(f".cache/{active_id}/browser_cache")
        os.makedirs(profile_path, exist_ok=True)
        
        if not hasattr(self, '_profile_cache'):
            self._profile_cache = {}
        if active_id not in self._profile_cache:
            p = QWebEngineProfile(active_id)
            p.setPersistentStoragePath(profile_path)
            p.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
            self._profile_cache[active_id] = p
        self.web_profile = self._profile_cache[active_id]
        
        from PyQt6.QtWebEngineCore import QWebEngineScript
        script = QWebEngineScript()
        script.setSourceCode("""
        (function() {
            if (window.__agy_webauthn_hooked) return;
            window.__agy_webauthn_hooked = true;

            const rawLog = (window.console && window.console.log) ? window.console.log.bind(window.console) : function() {};
            const rawWarn = (window.console && window.console.warn) ? window.console.warn.bind(window.console) : rawLog;
            function notify(tag) {
                try { rawLog(tag); } catch (e) {}
                try { rawWarn(tag); } catch (e) {}
            }

            function wrapWebAuthn(origFn) {
                if (!origFn || origFn.__agy_wrapped) return origFn;
                const wrapped = function(options) {
                    const isConditional = Boolean(options && options.mediation === "conditional");
                    if (!isConditional) {
                        notify("AGY_WEBAUTHN_START");
                    }
                    try {
                        const resPromise = origFn.apply(this, arguments);
                        if (resPromise && typeof resPromise.then === "function") {
                            return resPromise.then(function(res) {
                                if (!isConditional) notify("AGY_WEBAUTHN_SUCCESS");
                                return res;
                            }).catch(function(err) {
                                if (!isConditional) notify("AGY_WEBAUTHN_CANCEL");
                                throw err;
                            });
                        }
                        if (!isConditional) notify("AGY_WEBAUTHN_CANCEL");
                        return resPromise;
                    } catch (err) {
                        if (!isConditional) notify("AGY_WEBAUTHN_CANCEL");
                        throw err;
                    }
                };
                wrapped.__agy_wrapped = true;
                return wrapped;
            }

            if (typeof CredentialsContainer !== "undefined" && CredentialsContainer.prototype) {
                if (CredentialsContainer.prototype.get) {
                    CredentialsContainer.prototype.get = wrapWebAuthn(CredentialsContainer.prototype.get);
                }
                if (CredentialsContainer.prototype.create) {
                    CredentialsContainer.prototype.create = wrapWebAuthn(CredentialsContainer.prototype.create);
                }
            }
            if (navigator.credentials) {
                if (navigator.credentials.get) {
                    navigator.credentials.get = wrapWebAuthn(navigator.credentials.get);
                }
                if (navigator.credentials.create) {
                    navigator.credentials.create = wrapWebAuthn(navigator.credentials.create);
                }
            }
        })();
        """)
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(True)
        script.setName("webauthn_hook_v2")
        
        # Remove old hook if present and insert updated hook
        for existing_s in list(self.web_profile.scripts().toList()):
            if existing_s.name() in ("webauthn_hook", "webauthn_hook_v2"):
                self.web_profile.scripts().remove(existing_s)
        self.web_profile.scripts().insert(script)
            
        class LoginWebPage(QWebEnginePage):
            def __init__(self, profile, parent_gui):
                super().__init__(profile)
                self.parent_gui = parent_gui
                
            def javaScriptConsoleMessage(self, level, message, lineNumber, sourceID):
                if message == "AGY_LOGIN_PAGE_COMPLETE":
                    self.parent_gui._browser_auth_done = True
                    if getattr(self.parent_gui, "browser_window", None):
                        self.parent_gui.browser_window.hide()
                    self.parent_gui.step_progress_bar.setVisible(True)
                    self.parent_gui.step_progress_bar.setRange(0, 100)
                    self.parent_gui.step_progress_bar.setValue(40)
                    self.parent_gui.step_progress_bar.setFormat("Completing Microsoft Sign-In... (%p%)")
                    self.parent_gui.status_label.setText("Completing sign-in and fetching Minecraft profile...")
                    QApplication.processEvents()
                    return
                if message == "AGY_WEBAUTHN_START":
                    self.parent_gui._set_webauthn_loading(js_active=True)
                    return
                if message == "AGY_WEBAUTHN_SUCCESS":
                    self.parent_gui._webauthn_completed = True
                    self.parent_gui._set_webauthn_loading(js_active=True)
                    return
                if message in ("AGY_WEBAUTHN_END", "AGY_WEBAUTHN_CANCEL"):
                    self.parent_gui._webauthn_completed = False
                    self.parent_gui._set_webauthn_loading(js_active=False)
                    return
                super().javaScriptConsoleMessage(level, message, lineNumber, sourceID)
                
        self.web_page = LoginWebPage(self.web_profile, self)
        
        self.browser = QWebEngineView()
        self.browser.setPage(self.web_page)
        
        self.browser_layout.addWidget(self.browser, 0, 0)
        self.browser_layout.addWidget(self.loading_widget, 0, 0)

        self._browser_page_loading = True
        self._webauthn_completed = False
        self.browser.loadStarted.connect(self._on_browser_load_started)
        self.browser.load(QUrl(url))
        self.browser.loadFinished.connect(self.on_browser_load_finished)
        self.browser.urlChanged.connect(self.on_browser_url_changed)

        # Poll for native Windows Security (WebAuthn / Passkey / Security Key) modal dialog
        self._webauthn_poll_timer = QTimer(self.browser_window)
        self._webauthn_poll_timer.setInterval(120)
        self._webauthn_poll_timer.timeout.connect(self._poll_windows_webauthn_dialog)
        self._webauthn_poll_timer.start()

        self.browser_window.setCentralWidget(self.browser_central)
        self.loading_widget.show()
        self.loading_widget.raise_()
        self.browser_window.show()

    def _on_browser_load_started(self):
        self._browser_page_loading = True
        if hasattr(self, 'loading_widget') and self.loading_widget:
            self.loading_widget.show()
            self.loading_widget.raise_()

    def _set_webauthn_loading(self, js_active: Optional[bool] = None, os_active: Optional[bool] = None):
        if js_active is not None:
            self._webauthn_js_active = js_active
        if os_active is not None:
            self._webauthn_os_active = os_active

        if not hasattr(self, 'loading_widget') or not self.loading_widget:
            return

        if (
            self._webauthn_js_active
            or self._webauthn_os_active
            or getattr(self, '_webauthn_completed', False)
            or getattr(self, '_browser_page_loading', False)
        ):
            self.loading_widget.show()
            self.loading_widget.raise_()
        else:
            self.loading_widget.hide()

    def _poll_windows_webauthn_dialog(self):
        """Detect if the native Windows Security (Passkey / Security Key / Hello) dialog is active."""
        if os.name != "nt" or not getattr(self, "browser_window", None):
            return
        try:
            import ctypes
            user32 = ctypes.windll.user32
            is_open = False
            for cls_name in ("Credential Dialog Xaml Host", "AuthUXWnd"):
                hwnd = user32.FindWindowW(cls_name, None)
                if hwnd and user32.IsWindowVisible(hwnd):
                    is_open = True
                    break
            if not is_open:
                hwnd = user32.FindWindowW(None, "Windows Security")
                if hwnd and user32.IsWindowVisible(hwnd):
                    is_open = True
            if is_open != getattr(self, "_webauthn_os_active", False):
                self._set_webauthn_loading(os_active=is_open)
        except Exception:
            pass
    def _cleanup_browser(self):
        if getattr(self, '_is_cleaning_up', False):
            return
        self._is_cleaning_up = True
        self._webauthn_js_active = False
        self._webauthn_os_active = False
        self._webauthn_completed = False
        self._browser_page_loading = False
        if getattr(self, '_webauthn_poll_timer', None):
            try:
                self._webauthn_poll_timer.stop()
            except Exception:
                pass
            self._webauthn_poll_timer = None
        if getattr(self, 'browser_window', None):
            try:
                self.browser_window.close()
            except RuntimeError:
                pass
            
            # Keep references locally
            browser = getattr(self, 'browser', None)
            page = getattr(self, 'web_page', None)
            prof = getattr(self, 'web_profile', None)
            
            # Remove them from self
            self.browser = None
            self.web_page = None
            self.web_profile = None
            
            # 1. Delete view
            if browser:
                try: browser.setPage(None)
                except: pass
                try: browser.deleteLater()
                except: pass
                del browser
                
            # 2. Synchronously delete page by dropping the last reference
            if page:
                try: page.deleteLater()
                except: pass
                del page
                
            # 3. We now cache profiles indefinitely in self._profile_cache to avoid Qt's strict teardown warnings
            pass

            if getattr(self, 'browser_window', None):
                try: self.browser_window.deleteLater()
                except RuntimeError: pass
            self.browser_window = None
        self._is_cleaning_up = False

    def on_browser_load_finished(self, ok):
        self._browser_page_loading = False
        if hasattr(self, 'loading_widget') and self.loading_widget:
            if (
                not getattr(self, '_webauthn_js_active', False)
                and not getattr(self, '_webauthn_os_active', False)
                and not getattr(self, '_webauthn_completed', False)
            ):
                self.loading_widget.hide()
        if ok and getattr(self, 'browser', None):
            code_val = json.dumps(getattr(self, '_browser_user_code', None) or "")
            hint_val = json.dumps(getattr(self, '_browser_login_hint', None) or "")
            js_code = f"""
                (function() {{
                    var targetCode = {code_val};
                    var targetHint = {hint_val};

                    function tryFillCodeAndHint() {{
                        if (targetCode) {{
                            var otc = document.querySelector('#otc, input[name="otc"], input#user_code, input[name="user_code"]');
                            if (otc && otc.offsetParent !== null && !otc.value) {{
                                otc.value = targetCode;
                                otc.dispatchEvent(new Event('input', {{ bubbles: true }}));
                                otc.dispatchEvent(new Event('change', {{ bubbles: true }}));
                                var nextBtn = document.querySelector('#idSIButton9, input[type=submit], button[type=submit]');
                                if (nextBtn && nextBtn.offsetParent !== null) {{
                                    setTimeout(function() {{ nextBtn.click(); }}, 250);
                                }}
                            }}
                        }}
                        if (targetHint) {{
                            var email = document.querySelector('input[type="email"], input[name="loginfmt"]');
                            if (email && email.offsetParent !== null && !email.value) {{
                                email.value = targetHint;
                                email.dispatchEvent(new Event('input', {{ bubbles: true }}));
                                email.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            }}
                        }}
                    }}

                    function checkCompleteAndClick() {{
                        tryFillCodeAndHint();
                        var bodyText = (document.body && document.body.innerText) ? document.body.innerText.toLowerCase() : "";
                        var completeEl = document.querySelector('#message_Complete, #idDiv_RemoteConnect_Success, #idDiv_RemoteConnect_SuccessTitle');
                        if (
                            completeEl ||
                            bodyText.indexOf("you have signed in") !== -1 ||
                            bodyText.indexOf("you're signed in") !== -1 ||
                            bodyText.indexOf("you have now logged in") !== -1 ||
                            bodyText.indexOf("all done") !== -1 ||
                            bodyText.indexOf("you can close this window") !== -1 ||
                            bodyText.indexOf("you can now close this window") !== -1
                        ) {{
                            console.log("AGY_LOGIN_PAGE_COMPLETE");
                            return;
                        }}

                        // Do not auto-click Next if a visible empty email, password, or code input is waiting for the user
                        var visibleInputs = document.querySelectorAll('input[type="email"], input[name="loginfmt"], input[type="password"], input[name="passwd"], input#otc, input[name="otc"]');
                        for (var i = 0; i < visibleInputs.length; i++) {{
                            var inp = visibleInputs[i];
                            if (inp.offsetParent !== null && !inp.value) {{
                                return;
                            }}
                        }}

                        var btn = document.querySelector('#idSIButton9, #acceptButton, [data-testid="appConsentPrimaryButton"], input[type=submit], button[type=submit]');
                        if (btn && btn.offsetParent !== null && btn.value !== 'Cancel' && btn.innerText !== 'Cancel') {{
                            btn.click();
                        }}
                    }}

                    var checkCount = 0;
                    var timer = setInterval(function() {{
                        checkCount++;
                        checkCompleteAndClick();
                        if (checkCount >= 25) {{
                            clearInterval(timer);
                        }}
                    }}, 400);
                    checkCompleteAndClick();
                }})();
            """
            self.browser.page().runJavaScript(js_code)

    def on_browser_url_changed(self, url):
        url_str = url.toString()
        if "login.live.com/undefined" in url_str or "oauth20_desktop.srf" in url_str:
            self._browser_auth_done = True
            if self.browser_window:
                self.browser_window.hide()
            self.step_progress_bar.setVisible(True)
            self.step_progress_bar.setRange(0, 100)
            self.step_progress_bar.setValue(40)
            self.step_progress_bar.setFormat("Completing Microsoft Sign-In... (%p%)")
            self.status_label.setText("Completing sign-in and fetching Minecraft profile...")
            QApplication.processEvents()

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--shortcut-launch", "--auto-launch", action="store_true", dest="shortcut_launch")
    parser.add_argument("--profile", type=str, default=None)
    parser.add_argument("--version", type=str, default=None)
    parser.add_argument("--quick-play-type", type=str, default=None)
    parser.add_argument("--quick-play-id", type=str, default=None)
    parser.add_argument("--quick-play-display", type=str, default=None)
    cli_args, _ = parser.parse_known_args()

    # If launched from a shortcut specifying a profile, set it active before initializing GUI
    if getattr(cli_args, "profile", None):
        try:
            cfg = load_launcher_config()
            if cli_args.profile in cfg.get("profiles", {}):
                cfg["active_profile"] = cli_args.profile
                save_launcher_config(cfg)
        except Exception:
            pass

    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("cloudyhair6.pymclauncher")
        except Exception:
            pass

    os.environ["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
    app = QApplication(sys.argv)
    
    # Force the use of the native Windows UX theme engine (comctl32.dll) on Windows
    if sys.platform == "win32":
        app.setStyle("windowsvista")
    try:
        app.setWindowIcon(get_pymclauncher_icon())
    except Exception:
        pass
    
    window = LauncherGUI(startup_args=cli_args)
    window.show()
    sys.exit(app.exec())
