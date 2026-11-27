import sys
import json
import os
import re
import threading
import logging
from typing import Optional, List, Dict
from pathlib import Path

def _resolve_launcher_root_dir() -> Path:
    """Resolve persistent app data/executable root directory (works in source & compiled builds)."""
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
    return Path(__file__).resolve().parent


# Redirect Python bytecode cache (__pycache__) into .cache/pycache to keep workspace root clean
_root_dir = _resolve_launcher_root_dir()
_resource_dir = Path(getattr(sys, "_MEIPASS", _root_dir))
_cache_dir = _root_dir / ".cache"
_pycache_dir = _cache_dir / "pycache"
try:
    if not getattr(sys, "frozen", False):
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
    """Return python.exe (not pythonw.exe) or compiled executable for child QProcesses."""
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).resolve())
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        console_exe = exe.with_name("python.exe")
        if console_exe.exists():
            return str(console_exe)
    return sys.executable

def get_python_gui_executable() -> str:
    """Return pythonw.exe (if available) or compiled executable so shortcuts don't spawn a console window."""
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).resolve())
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe":
        gui_exe = exe.with_name("pythonw.exe")
        if gui_exe.exists():
            return str(gui_exe)
    return sys.executable

# Ensure backend directory is in sys.path
_backend_dir = _root_dir / "backend"
_res_backend_dir = _resource_dir / "backend"
for _p in (_backend_dir, _res_backend_dir, _root_dir, _resource_dir):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

def get_script_path(script_name: str) -> str:
    """Resolve backend script path, preferring backend/ directory."""
    for base in (_root_dir, _resource_dir):
        b_path = base / "backend" / script_name
        if b_path.exists():
            return str(b_path)
        r_path = base / script_name
        if r_path.exists():
            return str(r_path)
    return script_name

def get_image_path(rel_path: str) -> str:
    """Resolve image path from images/ (checking disk root then bundled resources)."""
    for base in (_root_dir, _resource_dir):
        for folder in ("images", "files"):
            cand = base / folder / rel_path
            if cand.exists():
                return str(cand)
    return str(_root_dir / "images" / rel_path)

# Pre-import download and launch modules in-process so they share Mojang manifest/version caches
# and avoid repeated Python interpreter startup overhead during the launch pipeline
import download_java
import download_minecraft
import extract_source_code
import download_libraries
import download_assets
import command_executor
import download_mod_loaders

# Suppress harmless Chromium hardware acceleration warnings
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-logging"
os.environ["QTWEBENGINE_DISABLE_SANDBOX"] = "1"

from PyQt6 import QtCore
from PyQt6.QtCore import Qt, QUrl, QUrlQuery, QProcess, QTimer, QRect, QEvent, QTime
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QGridLayout, 
                             QLabel, QProgressBar, QTextEdit, QHBoxLayout, QPushButton, QMessageBox, 
                             QStackedWidget, QComboBox, QDialog, QCheckBox, QStyledItemDelegate, QStyle, QFileDialog, QGroupBox, QRadioButton,
                             QListWidget, QListWidgetItem, QAbstractItemView, QSizePolicy, QSpinBox, QSplitter, QButtonGroup, QTabWidget, QScrollArea, QMenu, QFrame,
                             QTreeWidget, QTreeWidgetItem, QHeaderView)
from PyQt6.QtGui import QFont, QIcon, QPalette, QColor, QImage, QPainter, QPixmap, QMouseEvent, QCursor, QTextCursor, QAction
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage
from utils import (
    ROOT_DIR, LINKABLE_GAME_SUBFOLDERS, profile_context, get_dir, get_available_versions, get_downloaded_versions, get_quick_play_options,
    delete_version_files, load_launcher_config, 
    save_launcher_config, get_auth_cache, save_auth_cache, save_auth_cache_for_profile, clear_auth_cache, get_active_profile_id, load_accounts, save_accounts, 
    has_any_downloads, delete_all_downloads, has_any_source, delete_source_folder, fetch_player_textures,
    is_minecraft_window_visible, is_fullscreen_app_active, get_desktop_dir, get_start_menu_programs_dir, get_applications_dir,
    create_windows_shortcut, create_macos_shortcut,
    calculate_locator_bar_color, get_windows_linked_microsoft_accounts,
    build_profile_dirs, analyze_import_directory, import_profile_from_directory,
    import_worlds_to_profile, import_mods_to_profile, inspect_profile_directory_links,
    ensure_profile_links, sync_profile_mirror_links_back, get_profile_stats, rename_profile_in_config, duplicate_profile_in_config,
    make_install_queue_id, get_pending_install_queue, save_pending_install_queue, add_pending_install_items, remove_pending_install_items,
)

import struct
from PyQt6.QtCore import QByteArray, QBuffer, QIODevice
from PyQt6.QtWidgets import QFileDialog, QFormLayout, QLineEdit, QInputDialog, QMessageBox


def _safe_widget_delay(parent_obj, ms: int, fn):
    """Schedule a one-shot callback owned by `parent_obj` so Qt cancels it if the widget is deleted."""
    try:
        t = QTimer(parent_obj)
    except RuntimeError:
        return None
    t.setSingleShot(True)

    def _run():
        try:
            t.deleteLater()
        except RuntimeError:
            pass
        try:
            fn()
        except RuntimeError:
            pass

    t.timeout.connect(_run)
    t.start(ms)
    return t


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


DEFAULT_PERSONALISATION = {
    "theme": "system",  # "system", "light", "dark"
    "show_3d_skin": True,
    "show_change_skin_btn": True,
    "show_java_profile": True,
    "show_xbox_profile": True,
    "show_locator_bar": True,
}


def load_personalisation_options(config: Optional[dict] = None) -> dict:
    """Load personalisation & theme settings with legacy player_data_options compatibility."""
    cfg = config if isinstance(config, dict) else load_launcher_config()
    merged = dict(DEFAULT_PERSONALISATION)
    legacy = cfg.get("player_data_options")
    if isinstance(legacy, dict):
        merged.update({k: legacy[k] for k in legacy if k in DEFAULT_PERSONALISATION})
    pers = cfg.get("personalisation")
    if isinstance(pers, dict):
        merged.update({k: pers[k] for k in pers if k in DEFAULT_PERSONALISATION})
    theme = str(merged.get("theme", "system")).lower()
    if theme not in ("system", "light", "dark"):
        merged["theme"] = "system"
    else:
        merged["theme"] = theme
    return merged


def save_personalisation_options(options: dict, config: Optional[dict] = None):
    """Persist personalisation & theme settings while syncing legacy player_data_options keys."""
    cfg = config if isinstance(config, dict) else load_launcher_config()
    clean = dict(DEFAULT_PERSONALISATION)
    for k in DEFAULT_PERSONALISATION:
        if k in options:
            clean[k] = options[k]
    cfg["personalisation"] = clean
    cfg["player_data_options"] = {
        k: bool(clean.get(k, True))
        for k in (
            "show_3d_skin",
            "show_change_skin_btn",
            "show_java_profile",
            "show_xbox_profile",
            "show_locator_bar",
        )
    }
    save_launcher_config(cfg)


def detect_os_dark_mode() -> bool:
    """Detect whether the host OS currently uses Dark Mode."""
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            ) as key:
                val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
                return int(val) == 0
        except Exception:
            pass
    elif sys.platform == "darwin":
        try:
            import subprocess
            res = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True,
                text=True,
                timeout=1.5,
            )
            if res.returncode == 0 and "dark" in res.stdout.lower():
                return True
            if res.returncode != 0:
                return False
        except Exception:
            pass

    app = QApplication.instance()
    if app is not None:
        try:
            hints = app.styleHints()
            if hasattr(hints, "colorScheme") and hasattr(Qt, "ColorScheme"):
                return hints.colorScheme() == Qt.ColorScheme.Dark
        except Exception:
            pass
    return False


def get_effective_is_dark(mode: Optional[str] = None) -> bool:
    """Resolve effective dark/light state from explicit mode or saved personalisation."""
    theme = (mode or load_personalisation_options().get("theme", "system")).lower()
    if theme == "dark":
        return True
    if theme == "light":
        return False
    return detect_os_dark_mode()


def _build_dark_palette() -> QPalette:
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor("#1e1e1e"))
    pal.setColor(QPalette.ColorRole.WindowText, QColor("#eaeaea"))
    pal.setColor(QPalette.ColorRole.Base, QColor("#252526"))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor("#2d2d30"))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor("#252526"))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor("#eaeaea"))
    pal.setColor(QPalette.ColorRole.Text, QColor("#eaeaea"))
    pal.setColor(QPalette.ColorRole.Button, QColor("#2d2d30"))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor("#eaeaea"))
    pal.setColor(QPalette.ColorRole.BrightText, QColor("#ffffff"))
    pal.setColor(QPalette.ColorRole.Link, QColor("#4da6ff"))
    pal.setColor(QPalette.ColorRole.Highlight, QColor("#0078d7"))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    if hasattr(QPalette.ColorRole, "PlaceholderText"):
        pal.setColor(QPalette.ColorRole.PlaceholderText, QColor("#8a8a8a"))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor("#777777"))
    return pal


def _build_light_palette() -> QPalette:
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor("#f0f0f0"))
    pal.setColor(QPalette.ColorRole.WindowText, QColor("#111111"))
    pal.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor("#f7f7f7"))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor("#ffffdc"))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor("#111111"))
    pal.setColor(QPalette.ColorRole.Text, QColor("#111111"))
    pal.setColor(QPalette.ColorRole.Button, QColor("#e6e6e6"))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor("#111111"))
    pal.setColor(QPalette.ColorRole.BrightText, QColor("#ffffff"))
    pal.setColor(QPalette.ColorRole.Link, QColor("#0066cc"))
    pal.setColor(QPalette.ColorRole.Highlight, QColor("#0078d7"))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    if hasattr(QPalette.ColorRole, "PlaceholderText"):
        pal.setColor(QPalette.ColorRole.PlaceholderText, QColor("#777777"))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor("#888888"))
    return pal


def get_theme_window_bg_hex(is_dark: Optional[bool] = None) -> str:
    """Return concrete window background color hex so embedded WebEngine views match cleanly."""
    dark = get_effective_is_dark() if is_dark is None else bool(is_dark)
    if dark:
        return "#1e1e1e"
    app = QApplication.instance()
    if app is not None:
        try:
            col = app.palette().color(QPalette.ColorRole.Window)
            if col.isValid() and (col.red() + col.green() + col.blue()) > 380:
                return col.name()
        except Exception:
            pass
    return "#ececec" if sys.platform == "darwin" else "#f0f0f0"


def apply_launcher_theme(mode: Optional[str] = None) -> bool:
    """Apply Light, Dark, or System-matched Qt style and palette globally; returns is_dark."""
    pref = (mode or load_personalisation_options().get("theme", "system")).lower()
    is_dark = get_effective_is_dark(pref)
    app = QApplication.instance()
    if app is None:
        return is_dark

    if is_dark:
        app.setStyle("Fusion")
        app.setPalette(_build_dark_palette())
        app.setStyleSheet(
            "QToolTip { color: #eaeaea; background-color: #252526; border: 1px solid #454545; } "
            "QGroupBox { border: 1px solid #3c3c3c; border-radius: 4px; margin-top: 8px; padding-top: 6px; } "
            "QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }"
        )
    else:
        if sys.platform == "win32":
            app.setStyle("windowsvista")
            app.setPalette(app.style().standardPalette())
        elif sys.platform == "darwin" and pref == "light" and detect_os_dark_mode():
            # Force light controls via Fusion even when macOS system appearance is dark
            app.setStyle("Fusion")
            app.setPalette(_build_light_palette())
        else:
            if sys.platform == "darwin":
                app.setStyle("macintosh")
            else:
                app.setStyle("Fusion")
            app.setPalette(app.style().standardPalette())
        app.setStyleSheet("")

    try:
        hints = app.styleHints()
        if hasattr(hints, "setColorScheme") and hasattr(Qt, "ColorScheme"):
            if pref == "system" and hasattr(hints, "unsetColorScheme"):
                hints.unsetColorScheme()
            else:
                hints.setColorScheme(Qt.ColorScheme.Dark if is_dark else Qt.ColorScheme.Light)
    except Exception:
        pass

    return is_dark


def _configure_skin_webview(web_view: QWebEngineView, bg_hex: Optional[str] = None):
    """Configure QWebEngineView with file:// texture access, WebGL, and theme-matched background."""
    profile = web_view.page().profile() if web_view.page() else QWebEngineProfile.defaultProfile()
    page = CustomWebPage(profile, web_view)
    web_view.setPage(page)
    try:
        from PyQt6.QtWebEngineCore import QWebEngineSettings
        settings = web_view.settings()
        for attr_name in (
            "LocalContentCanAccessFileUrls",
            "LocalContentCanAccessRemoteUrls",
            "WebGLEnabled",
            "Accelerated2dCanvasEnabled",
            "JavascriptEnabled",
        ):
            attr = getattr(QWebEngineSettings.WebAttribute, attr_name, None)
            if attr is not None:
                settings.setAttribute(attr, True)
    except Exception:
        pass
    resolved_bg = bg_hex or get_theme_window_bg_hex()
    try:
        page.setBackgroundColor(QColor(resolved_bg))
    except Exception:
        pass
    return page, resolved_bg


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
        current_prof = (
            launcher_gui.get_active_profile_name()
            if hasattr(launcher_gui, "get_active_profile_name")
            else (launcher_gui.profile_combo.currentText() if hasattr(launcher_gui, "profile_combo") else "Default")
        )
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
        details_group = QGroupBox("Shortcut Details && Destination")
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
        for disp_txt, target_key in _DYNAMIC_LATEST_OPTIONS:
            self.version_combo.addItem(disp_txt, target_key)
        self.version_combo.addItem("Profile's Default / Last Played Version", None)

        seen_versions = set()
        loader_names = {item["id"]: item["name"] for item in download_mod_loaders.list_mod_loaders()}
        # Load downloaded versions strictly for the selected profile so isolated profiles stay accurate
        for dl_v in sorted(get_downloaded_versions(profile_name=prof_name)):
            if dl_v not in seen_versions:
                lid, _mc, _b = download_mod_loaders.detect_version_loader(dl_v, None)
                tag = "Vanilla" if lid == "none" else loader_names.get(lid, lid.capitalize())
                self.version_combo.addItem(f"{dl_v} ({tag}) [Downloaded]", dl_v)
                seen_versions.add(dl_v)

        if prev_ver:
            v_idx = self.version_combo.findData(prev_ver)
            if v_idx >= 0:
                self.version_combo.setCurrentIndex(v_idx)
            else:
                self.version_combo.setCurrentIndex(0)
        else:
            self.version_combo.setCurrentIndex(0)
        self.version_combo.blockSignals(False)

        # Populate Quick Play options strictly for the chosen profile
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
            base = "Minecraft (Latest Release)"
        elif ver == "latest-snapshot":
            base = "Minecraft (Latest Snapshot)"
        elif isinstance(ver, str) and ver.startswith("latest-loader:"):
            parts = ver.split(":")
            ldr_part = parts[1].capitalize() if len(parts) > 1 else "Modded"
            mc_part = f" {parts[2]}" if len(parts) > 2 and parts[2] else ""
            base = f"Minecraft (Latest {ldr_part}{mc_part})"
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

        if getattr(sys, "frozen", False):
            target_exe = str(Path(sys.executable).resolve())
            arg_parts = ["--shortcut-launch"]
        else:
            launcher_script = str(Path(__file__).resolve())
            arg_parts = [f'"{launcher_script}"', "--shortcut-launch"]
            target_exe = get_python_gui_executable()
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
                    name=safe_filename
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
                    name=safe_filename
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


class AddProfileDialog(QDialog):
    """
    Unified Add / Import Profile dialog.
    Supports guided presets OR smart folder import with automatic detection when a user
    accidentally selects a subfolder (`mods/`, `saves/`, single world, `overrides/`) or custom layout.
    """

    def __init__(self, launcher_gui: 'LauncherGUI', parent=None):
        super().__init__(parent or launcher_gui)
        self.launcher_gui = launcher_gui
        self.setWindowTitle("Add or Import Profile")
        self.setMinimumWidth(560)
        self._last_analysis = None
        self.created_profile_name = None

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # Source selector
        src_row = QHBoxLayout()
        self.mode_preset_radio = QRadioButton("Create Empty / Preset Profile")
        self.mode_import_radio = QRadioButton("Import Existing Minecraft / Modpack / Worlds / Mods Folder")
        self.mode_preset_radio.setChecked(True)
        self._mode_grp = QButtonGroup(self)
        self._mode_grp.addButton(self.mode_preset_radio)
        self._mode_grp.addButton(self.mode_import_radio)
        self.mode_import_radio.toggled.connect(self._sync_mode)
        src_row.addWidget(self.mode_preset_radio)
        src_row.addWidget(self.mode_import_radio)
        layout.addLayout(src_row)

        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. Survival 1.21, Create Fabric, Modpack...")
        form.addRow("Profile Name:", self.name_edit)

        self.preset_combo = QComboBox()
        self.preset_combo.addItem("Modded Profile (Isolated Mods & Config)", "modded")
        self.preset_combo.addItem("Vanilla Survival", "vanilla")
        self.preset_combo.addItem("High-RAM Modpack (6 GB RAM)", "modpack_6g")
        self.preset_combo.addItem("Snapshot / Testing (Isolated Versions)", "testing")
        self.preset_row_lbl = QLabel("Preset:")
        form.addRow(self.preset_row_lbl, self.preset_combo)
        layout.addLayout(form)

        # Folder import box (shown when Import mode selected)
        self.import_group = QGroupBox("Import Folder && Auto-Organise")
        iv = QVBoxLayout(self.import_group)
        iv.setSpacing(6)

        p_row = QHBoxLayout()
        self.dir_edit = QLineEdit()
        self.dir_edit.setPlaceholderText("Browse .minecraft, modpack, Prism/MultiMC, saves/, mods/, world, or backup folder...")
        browse_btn = QPushButton("Browse Folder...")
        browse_btn.clicked.connect(self._browse_dir)
        scan_btn = QPushButton("Scan")
        scan_btn.clicked.connect(self._scan_dir)
        p_row.addWidget(self.dir_edit, stretch=1)
        p_row.addWidget(browse_btn)
        p_row.addWidget(scan_btn)
        iv.addLayout(p_row)

        # Accidental subfolder warning + 1-click switch to parent
        self.accident_box = QFrame()
        self.accident_box.setFrameShape(QFrame.Shape.StyledPanel)
        ab_v = QVBoxLayout(self.accident_box)
        ab_v.setContentsMargins(8, 6, 8, 6)
        self.accident_lbl = QLabel("")
        self.accident_lbl.setWordWrap(True)
        self.accident_lbl.setStyleSheet("color: #d97706; font-weight: bold;")
        ab_btn_row = QHBoxLayout()
        self.use_parent_btn = QPushButton("Switch to Full Parent Folder Instead")
        self.use_parent_btn.clicked.connect(self._switch_to_parent_candidate)
        ab_btn_row.addWidget(self.use_parent_btn)
        ab_btn_row.addStretch()
        ab_v.addWidget(self.accident_lbl)
        ab_v.addLayout(ab_btn_row)
        self.accident_box.setVisible(False)
        iv.addWidget(self.accident_box)

        self.summary_lbl = QLabel("Pick any Minecraft folder (even just saves/ or mods/ or a messy backup) to scan contents.")
        self.summary_lbl.setWordWrap(True)
        iv.addWidget(self.summary_lbl)

        # Selective import toggles
        sel_grid = QGridLayout()
        self.cb_mods = QCheckBox("Mods (0)")
        self.cb_worlds = QCheckBox("Worlds / Saves (0)")
        self.cb_rp = QCheckBox("Resource Packs (0)")
        self.cb_sp = QCheckBox("Shader Packs (0)")
        self.cb_cfg = QCheckBox("Configs && Settings")
        self.cb_vers = QCheckBox("Versions (0)")
        for cb in (self.cb_mods, self.cb_worlds, self.cb_rp, self.cb_sp, self.cb_cfg):
            cb.setChecked(True)
        self.cb_vers.setChecked(False)
        sel_grid.addWidget(self.cb_mods, 0, 0)
        sel_grid.addWidget(self.cb_worlds, 0, 1)
        sel_grid.addWidget(self.cb_cfg, 0, 2)
        sel_grid.addWidget(self.cb_rp, 1, 0)
        sel_grid.addWidget(self.cb_sp, 1, 1)
        sel_grid.addWidget(self.cb_vers, 1, 2)
        iv.addLayout(sel_grid)

        self.style_org_radio = QRadioButton("Copy && auto-organise into clean PyMcLauncher folder (keeps originals safe)")
        self.style_inplace_radio = QRadioButton("Use selected folder in-place && sort loose files")
        self.style_org_radio.setChecked(True)
        self._style_grp = QButtonGroup(self)
        self._style_grp.addButton(self.style_org_radio)
        self._style_grp.addButton(self.style_inplace_radio)
        iv.addWidget(self.style_org_radio)
        iv.addWidget(self.style_inplace_radio)
        layout.addWidget(self.import_group)

        # Shared profile options
        opt_box = QGroupBox("Profile Options")
        of = QFormLayout(opt_box)
        self.share_vers_cb = QCheckBox("Share downloaded versions across profiles (uncheck for isolated versions)")
        self.share_vers_cb.setChecked(True)
        of.addRow("Versions:", self.share_vers_cb)

        self.link_saves_combo = QComboBox()
        self.link_saves_combo.addItem("Keep Own / Imported Worlds (Isolated)", "")
        cfg = load_launcher_config()
        for existing_p in (cfg.get("profiles") or {}).keys():
            self.link_saves_combo.addItem(f"Link Worlds from '{existing_p}'", existing_p)
        of.addRow("Worlds (saves/):", self.link_saves_combo)

        self.ram_combo = QComboBox()
        for lbl, mb in (("Default (2048 MB)", 0), ("4 GB (4096 MB)", 4096), ("6 GB (6144 MB)", 6144), ("8 GB (8192 MB)", 8192)):
            self.ram_combo.addItem(lbl, mb)
        of.addRow("Memory:", self.ram_combo)

        self.activate_cb = QCheckBox("Switch to this profile on Play tab immediately")
        self.activate_cb.setChecked(True)
        of.addRow("", self.activate_cb)
        layout.addWidget(opt_box)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        self.ok_btn = QPushButton("Create / Import Profile")
        self.ok_btn.clicked.connect(self._submit)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(self.ok_btn)
        layout.addLayout(btn_row)

        self._sync_mode()

    def _sync_mode(self):
        imp = self.mode_import_radio.isChecked()
        self.import_group.setVisible(imp)
        self.preset_combo.setVisible(not imp)
        self.preset_row_lbl.setVisible(not imp)
        self.ok_btn.setText("Import Profile" if imp else "Create Profile")

    def _browse_dir(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Minecraft, Modpack, Saves, or Mods Folder")
        if folder:
            self.dir_edit.setText(folder)
            self._scan_dir()

    def _scan_dir(self):
        raw = self.dir_edit.text().strip()
        if not raw:
            return
        info = analyze_import_directory(Path(raw))
        self._last_analysis = info
        if not info.get("exists"):
            self.summary_lbl.setText("Selected folder does not exist.")
            self.accident_box.setVisible(False)
            return

        if info.get("accidental_warning") and info.get("parent_candidate"):
            self.accident_lbl.setText(info["accidental_warning"])
            parent_name = Path(info["parent_candidate"]).name
            self.use_parent_btn.setText(f"Use Full Parent Folder ('{parent_name}') Instead")
            self.accident_box.setVisible(True)
        else:
            self.accident_box.setVisible(False)

        if not self.name_edit.text().strip():
            self.name_edit.setText(info.get("suggested_name") or "Imported Profile")

        self.cb_mods.setText(f"Mods ({info['mods_count']})")
        self.cb_mods.setChecked(info["mods_count"] > 0)
        self.cb_worlds.setText(f"Worlds ({info['worlds_count']})")
        self.cb_worlds.setChecked(info["worlds_count"] > 0)
        self.cb_rp.setText(f"Resource Packs ({info['resourcepacks_count']})")
        self.cb_rp.setChecked(info["resourcepacks_count"] > 0)
        self.cb_sp.setText(f"Shader Packs ({info['shaderpacks_count']})")
        self.cb_sp.setChecked(info["shaderpacks_count"] > 0)
        self.cb_cfg.setChecked(bool(info.get("has_config") or info.get("has_options_txt") or info.get("has_servers_dat")))
        self.cb_vers.setText(f"Versions ({len(info['versions_found'])})")
        self.cb_vers.setChecked(len(info["versions_found"]) > 0 and not self.share_vers_cb.isChecked())

        extra = " | Non-standard layout auto-sorted" if info.get("has_nonstandard_layout") else ""
        self.summary_lbl.setText(
            f"Root: {info['resolved_root']}\n"
            f"Detected Loader: {info.get('detected_loader', 'none')} (MC {info.get('detected_mc_version') or 'Unknown'}){extra}"
        )

    def _switch_to_parent_candidate(self):
        if self._last_analysis and self._last_analysis.get("parent_candidate"):
            self.dir_edit.setText(self._last_analysis["parent_candidate"])
            self._scan_dir()

    def _submit(self):
        pname = self.name_edit.text().strip()
        if not pname:
            QMessageBox.warning(self, "Name Required", "Please enter a profile name.")
            return
        cfg = load_launcher_config()
        if pname in (cfg.get("profiles") or {}):
            reply = QMessageBox.question(
                self, "Profile Exists",
                f"Profile '{pname}' already exists. Update/merge into it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        ram_val = self.ram_combo.currentData() or None
        link_from = self.link_saves_combo.currentData() or None
        share_v = self.share_vers_cb.isChecked()
        set_act = self.activate_cb.isChecked()

        if self.mode_import_radio.isChecked():
            raw = self.dir_edit.text().strip()
            if not raw or not Path(raw).exists():
                QMessageBox.warning(self, "Folder Required", "Please select a valid folder to import.")
                return
            mode = "organize" if self.style_org_radio.isChecked() else "in_place"
            try:
                import_profile_from_directory(
                    Path(raw),
                    pname,
                    mode=mode,
                    share_versions=share_v,
                    link_saves_from=link_from,
                    auto_sort_loose=True,
                    set_active=set_act,
                    ram_mb=ram_val,
                    include_mods=self.cb_mods.isChecked(),
                    include_worlds=self.cb_worlds.isChecked(),
                    include_resourcepacks=self.cb_rp.isChecked(),
                    include_shaderpacks=self.cb_sp.isChecked(),
                    include_config=self.cb_cfg.isChecked(),
                    include_versions=self.cb_vers.isChecked(),
                )
            except Exception as e:
                QMessageBox.critical(self, "Import Failed", str(e))
                return
        else:
            preset = self.preset_combo.currentData()
            pref_loader = "none" if preset == "vanilla" else None
            if preset == "modpack_6g" and not ram_val:
                ram_val = 6144
            if preset == "testing":
                share_v = False
            profiles = cfg.get("profiles") or {}
            profiles[pname] = build_profile_dirs(
                pname,
                isolate_versions=not share_v,
                link_saves_mode="profile" if link_from else "isolated",
                link_saves_profile=link_from,
                preferred_loader=pref_loader,
                ram_mb=ram_val,
            )
            cfg["profiles"] = profiles
            if set_act:
                cfg["active_profile"] = pname
            save_launcher_config(cfg)
            ensure_profile_links(pname)

        self.created_profile_name = pname
        self.accept()


class ProfilesManagerTab(QWidget):
    """
    Focused Profiles & Directories workspace organised cleanly by task tabs:
    Worlds (Saves), Mods, Versions, Import / Organise Folder, and Directories & Links.
    """

    def __init__(self, launcher_gui: 'LauncherGUI'):
        super().__init__(launcher_gui)
        self.launcher_gui = launcher_gui
        self._is_loading = False
        self._last_analysis = None

        root_layout = QHBoxLayout(self)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(10)

        # Left column: Profiles list & profile lifecycle actions
        left_box = QGroupBox("Profiles")
        left_box.setMinimumWidth(220)
        left_box.setMaximumWidth(270)
        left_v = QVBoxLayout(left_box)
        left_v.setContentsMargins(8, 10, 8, 8)
        left_v.setSpacing(6)

        self.profile_list = QListWidget()
        self.profile_list.currentItemChanged.connect(self._on_list_profile_changed)
        left_v.addWidget(self.profile_list, stretch=1)

        row_a = QHBoxLayout()
        self.new_prof_btn = QPushButton("+ Add / Import")
        self.new_prof_btn.clicked.connect(self.open_add_profile_dialog)
        self.dup_prof_btn = QPushButton("Duplicate")
        self.dup_prof_btn.clicked.connect(self.duplicate_selected_profile)
        row_a.addWidget(self.new_prof_btn)
        row_a.addWidget(self.dup_prof_btn)
        left_v.addLayout(row_a)

        row_b = QHBoxLayout()
        self.rename_prof_btn = QPushButton("Rename")
        self.rename_prof_btn.clicked.connect(self.rename_selected_profile)
        self.del_prof_btn = QPushButton("Delete")
        self.del_prof_btn.clicked.connect(self.delete_selected_profile)
        row_b.addWidget(self.rename_prof_btn)
        row_b.addWidget(self.del_prof_btn)
        left_v.addLayout(row_b)

        self.activate_btn = QPushButton("Use This Profile on Play Tab")
        self.activate_btn.clicked.connect(self.activate_selected_profile)
        left_v.addWidget(self.activate_btn)

        sep_lbl = QLabel("Storage & Source Maintenance")
        sep_lbl.setStyleSheet("color: #9ca3af; font-size: 11px; margin-top: 6px;")
        left_v.addWidget(sep_lbl)

        self.delete_all_versions_btn = QPushButton("Delete All Versions")
        self.delete_all_versions_btn.setStyleSheet("color: #f87171; font-weight: bold;")
        self.delete_all_versions_btn.setToolTip("Delete all downloaded Minecraft versions, assets, and libraries across storage")
        self.delete_all_versions_btn.clicked.connect(self._delete_all_downloads_clicked)
        left_v.addWidget(self.delete_all_versions_btn)

        self.delete_source_btn = QPushButton("Delete Source Folder")
        self.delete_source_btn.setStyleSheet("color: #fb923c; font-weight: bold;")
        self.delete_source_btn.setToolTip("Delete extracted Source Code & unpacked dev-mode assets")
        self.delete_source_btn.clicked.connect(self._delete_source_folder_clicked)
        left_v.addWidget(self.delete_source_btn)

        root_layout.addWidget(left_box)

        # Right column: Profile summary + dedicated management sub-tabs
        right_container = QWidget()
        right_outer = QVBoxLayout(right_container)
        right_outer.setContentsMargins(0, 0, 0, 0)
        right_outer.setSpacing(8)

        self.prof_summary_lbl = QLabel("Selected Profile: Default")
        self.prof_summary_lbl.setWordWrap(True)
        self.prof_summary_lbl.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        right_outer.addWidget(self.prof_summary_lbl)

        self.profile_subtabs = QTabWidget()
        self.worlds_tab = self._build_worlds_tab()
        self.mods_tab = self._build_mods_tab()
        self.versions_tab = self._build_versions_tab()
        self.import_tab = self._build_import_tab()
        self.dirs_tab = self._build_dirs_tab()
        self.source_tab = self._build_source_tab()

        self.profile_subtabs.addTab(self.worlds_tab, "Worlds (Saves)")
        self.profile_subtabs.addTab(self.mods_tab, "Mods")
        self.profile_subtabs.addTab(self.versions_tab, "Versions")
        self.profile_subtabs.addTab(self.import_tab, "Import / Organise")
        self.profile_subtabs.addTab(self.dirs_tab, "Directories && Links")
        if bool(getattr(self.launcher_gui, "dev_mode", False)):
            self.profile_subtabs.addTab(self.source_tab, "Source Code")
        self.profile_subtabs.currentChanged.connect(
            lambda _i: self.launcher_gui._flush_versions_browser_if_dirty()
            if hasattr(self.launcher_gui, "_flush_versions_browser_if_dirty")
            else None
        )
        right_outer.addWidget(self.profile_subtabs, stretch=1)

        root_layout.addWidget(right_container, stretch=1)
        self.refresh_profiles()

    def _selected_profile_name(self) -> Optional[str]:
        item = self.profile_list.currentItem()
        if item:
            return item.data(Qt.ItemDataRole.UserRole) or item.text()
        cfg = load_launcher_config()
        return cfg.get("active_profile", "Default")

    def _build_worlds_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setSpacing(8)

        self.worlds_link_status_lbl = QLabel("Worlds folder: Isolated")
        self.worlds_link_status_lbl.setWordWrap(True)
        v.addWidget(self.worlds_link_status_lbl)

        self.worlds_list_widget = QListWidget()
        self.worlds_list_widget.itemDoubleClicked.connect(lambda _it: self._quick_play_selected_world())
        v.addWidget(self.worlds_list_widget, stretch=1)

        row_btns = QGridLayout()
        row_btns.setSpacing(6)
        imp_w_btn = QPushButton("+ Import World Folder...")
        imp_w_btn.clicked.connect(self._import_world_folders_prompt)
        imp_z_btn = QPushButton("+ Import World .zip...")
        imp_z_btn.clicked.connect(self._import_world_zip_prompt)
        qp_w_btn = QPushButton("▶ Quick Play World")
        qp_w_btn.clicked.connect(self._quick_play_selected_world)
        dup_w_btn = QPushButton("Backup / Duplicate")
        dup_w_btn.clicked.connect(self._duplicate_selected_world)
        open_w_btn = QPushButton("Open saves/")
        open_w_btn.clicked.connect(lambda: self._open_profile_folder("saves"))
        del_w_btn = QPushButton("Delete World")
        del_w_btn.clicked.connect(self._delete_selected_world)
        for i, b in enumerate((imp_w_btn, imp_z_btn, qp_w_btn, dup_w_btn, open_w_btn, del_w_btn)):
            row_btns.addWidget(b, i // 3, i % 3)
        v.addLayout(row_btns)

        link_box = QGroupBox("Link Worlds (saves/) Across Profiles")
        lf = QFormLayout(link_box)
        l_row = QHBoxLayout()
        self.worlds_link_combo = QComboBox()
        self.worlds_link_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.worlds_link_combo.setMinimumContentsLength(12)
        apply_link_btn = QPushButton("Apply Link")
        apply_link_btn.clicked.connect(self._apply_quick_worlds_link)
        copy_srv_btn = QPushButton("Copy servers.dat...")
        copy_srv_btn.clicked.connect(lambda: self._copy_config_file_from_profile("servers.dat"))
        l_row.addWidget(self.worlds_link_combo, stretch=1)
        l_row.addWidget(apply_link_btn)
        l_row.addWidget(copy_srv_btn)
        lf.addRow("Saves Source:", l_row)
        v.addWidget(link_box)
        return page

    def _build_mods_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setSpacing(8)

        pref_box = QGroupBox("Profile Mod Loader && Default Version")
        pf = QFormLayout(pref_box)
        r = QHBoxLayout()
        self.pref_loader_combo = QComboBox()
        self.pref_loader_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.pref_loader_combo.setMinimumContentsLength(10)
        self._populate_loader_combo(self.pref_loader_combo)
        self.pref_version_edit = QLineEdit()
        self.pref_version_edit.setPlaceholderText("Optional version ID (e.g. fabric-loader-0.16.9-1.20.1)")
        save_p_btn = QPushButton("Save Defaults")
        save_p_btn.clicked.connect(self._save_loader_defaults)
        r.addWidget(self.pref_loader_combo)
        r.addWidget(self.pref_version_edit, stretch=1)
        r.addWidget(save_p_btn)
        pf.addRow("Auto-Select:", r)
        v.addWidget(pref_box)

        self.mods_list_widget = QListWidget()
        v.addWidget(self.mods_list_widget, stretch=1)

        m_btns = QGridLayout()
        m_btns.setSpacing(6)
        add_mod_btn = QPushButton("+ Add Mod .jar(s)...")
        add_mod_btn.clicked.connect(self._add_mods_to_profile)
        add_dir_btn = QPushButton("+ Import Mods Folder...")
        add_dir_btn.clicked.connect(self._import_mods_folder_prompt)
        tog_mod_btn = QPushButton("Enable / Disable")
        tog_mod_btn.clicked.connect(self._toggle_selected_mod)
        copy_mod_btn = QPushButton("Copy to Profile...")
        copy_mod_btn.clicked.connect(self._copy_selected_mod_to_profile)
        rem_mod_btn = QPushButton("Remove")
        rem_mod_btn.clicked.connect(self._remove_selected_mod)
        open_mod_btn = QPushButton("Open mods/")
        open_mod_btn.clicked.connect(lambda: self._open_profile_folder("mods"))
        for i, b in enumerate((add_mod_btn, add_dir_btn, tog_mod_btn, copy_mod_btn, rem_mod_btn, open_mod_btn)):
            m_btns.addWidget(b, i // 3, i % 3)
        v.addLayout(m_btns)
        return page

    def _build_versions_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        page = QWidget()
        v = QVBoxLayout(page)
        v.setSpacing(8)

        iso_box = QGroupBox("Version Sharing vs Isolation")
        iv = QHBoxLayout(iso_box)
        self.isolate_vers_cb = QCheckBox("Isolate versions to this profile only")
        self.isolate_vers_cb.setToolTip("Unchecked = share minecraft_downloads/versions across profiles; checked = keep profile-specific versions/")
        save_iso_btn = QPushButton("Apply Isolation")
        save_iso_btn.clicked.connect(self._apply_version_isolation_setting)
        iv.addWidget(self.isolate_vers_cb, stretch=1)
        iv.addWidget(save_iso_btn)
        v.addWidget(iso_box)

        # Downloaded Versions section (ONLY actually downloaded versions for this profile)
        dl_box = QGroupBox("Downloaded Versions")
        dv = QVBoxLayout(dl_box)
        dv.setSpacing(6)

        self.versions_info_lbl = QLabel("")
        self.versions_info_lbl.setWordWrap(True)
        dv.addWidget(self.versions_info_lbl)

        self.versions_list_widget = QListWidget()
        self.versions_list_widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.versions_list_widget.setMinimumHeight(85)
        self.versions_list_widget.setMaximumHeight(135)
        dv.addWidget(self.versions_list_widget)

        v_btns = QGridLayout()
        v_btns.setSpacing(6)
        play_ver_btn = QPushButton("▶ Play Selected Version")
        play_ver_btn.clicked.connect(self._play_selected_profile_version)
        copy_in_btn = QPushButton("Copy Version Into Profile...")
        copy_in_btn.clicked.connect(self._copy_version_into_profile)
        set_def_btn = QPushButton("Set as Profile Default")
        set_def_btn.clicked.connect(self._set_selected_version_default)
        open_ver_btn = QPushButton("Open versions/")
        open_ver_btn.clicked.connect(lambda: self._open_profile_folder("versions"))
        del_ver_btn = QPushButton("Delete Selected Version(s)")
        del_ver_btn.setStyleSheet("color: #f87171; font-weight: bold;")
        del_ver_btn.clicked.connect(self._delete_selected_profile_version)
        del_all_ver_btn = QPushButton("Delete All Versions")
        del_all_ver_btn.setStyleSheet("color: #ef4444; font-weight: bold;")
        del_all_ver_btn.clicked.connect(self._delete_all_downloads_clicked)
        for i, b in enumerate((play_ver_btn, copy_in_btn, set_def_btn, open_ver_btn, del_ver_btn, del_all_ver_btn)):
            v_btns.addWidget(b, i // 3, i % 3)
        dv.addLayout(v_btns)
        v.addWidget(dl_box)

        # Moved here from Play Tab: Mod Loaders + Minecraft Versions (with Latest Release/Snapshot) + Mod Loader Versions (with Latest Recommended/Beta)
        browse_box = QGroupBox("Browse, Multi-Select && Install Minecraft Versions && Mod Loaders")
        bv = QVBoxLayout(browse_box)
        bv.setSpacing(6)
        bv.addWidget(self.launcher_gui.mod_loaders_group)

        self.versions_browser_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.versions_browser_splitter.addWidget(self.launcher_gui.left_versions_panel)
        self.versions_browser_splitter.addWidget(self.launcher_gui.right_loader_panel)
        self.versions_browser_splitter.setStretchFactor(0, 1)
        self.versions_browser_splitter.setStretchFactor(1, 1)
        self.launcher_gui.left_versions_panel.setMinimumHeight(210)
        self.launcher_gui.right_loader_panel.setMinimumHeight(210)
        bv.addWidget(self.versions_browser_splitter, stretch=1)

        # Single Install button + Queue + Launch & Install row (no duplicate Install inside right pane!)
        multi_row = QHBoxLayout()
        multi_row.setSpacing(6)
        self.install_selected_multi_btn = QPushButton("Install Selected Version(s)")
        self.install_selected_multi_btn.setToolTip("Download & install Loader (if chosen), Java, Game Client, Libraries & Assets for all selected versions")
        self.install_selected_multi_btn.clicked.connect(self._install_selected_multi)
        self.queue_selected_multi_btn = QPushButton("+ Add Selected to Queue")
        self.queue_selected_multi_btn.setToolTip("Add selected versions to the editable Install Queue below (visible on Play tab)")
        self.queue_selected_multi_btn.clicked.connect(self._queue_selected_multi)
        self.launch_selected_multi_btn = QPushButton("▶ Launch && Install Selected")
        self.launch_selected_multi_btn.setToolTip("Install the selected version (Java, Client, Libraries, Assets & Loader) and launch Minecraft")
        self.launch_selected_multi_btn.clicked.connect(self._launch_install_selected_multi)
        multi_row.addWidget(self.install_selected_multi_btn)
        multi_row.addWidget(self.queue_selected_multi_btn)
        multi_row.addWidget(self.launch_selected_multi_btn)
        bv.addLayout(multi_row)
        v.addWidget(browse_box, stretch=1)

        # Full Queue Editor (only editable here on Versions tab — Play tab only allows checkboxes + remove!)
        self.versions_queue_group = QGroupBox("Edit Install && Launch Queue")
        qv = QVBoxLayout(self.versions_queue_group)
        qv.setSpacing(6)
        self.versions_queue_list = QListWidget()
        self.versions_queue_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.versions_queue_list.setMinimumHeight(80)
        self.versions_queue_list.setMaximumHeight(130)
        qv.addWidget(self.versions_queue_list)

        q_btns = QGridLayout()
        q_btns.setSpacing(6)
        q_tog_btn = QPushButton("Toggle Just Install / Launch && Install")
        q_tog_btn.clicked.connect(self._versions_queue_toggle_mode)
        q_prof_btn = QPushButton("Change Target Profile...")
        q_prof_btn.clicked.connect(self._versions_queue_change_profile)
        q_up_btn = QPushButton("Move Up")
        q_up_btn.clicked.connect(lambda: self._versions_queue_move(-1))
        q_dn_btn = QPushButton("Move Down")
        q_dn_btn.clicked.connect(lambda: self._versions_queue_move(1))
        q_rem_btn = QPushButton("Remove Selected")
        q_rem_btn.clicked.connect(self._versions_queue_remove)
        q_run_btn = QPushButton("▶ Run Queue Now")
        q_run_btn.clicked.connect(self._versions_queue_run)
        for i, b in enumerate((q_tog_btn, q_prof_btn, q_up_btn, q_dn_btn, q_rem_btn, q_run_btn)):
            q_btns.addWidget(b, i // 3, i % 3)
        qv.addLayout(q_btns)
        v.addWidget(self.versions_queue_group)

        scroll.setWidget(page)
        return scroll

    def _build_import_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        page = QWidget()
        v = QVBoxLayout(page)
        v.setSpacing(8)

        imp_box = QGroupBox("Import && Auto-Organise Folder")
        iv = QVBoxLayout(imp_box)
        iv.setSpacing(6)

        path_row = QHBoxLayout()
        self.import_dir_edit = QLineEdit()
        self.import_dir_edit.setPlaceholderText("Any .minecraft, modpack, Prism/MultiMC, saves/, mods/, world, or backup folder...")
        self.import_browse_btn = QPushButton("Browse...")
        self.import_browse_btn.clicked.connect(self._browse_import_folder)
        self.import_scan_btn = QPushButton("Scan")
        self.import_scan_btn.clicked.connect(self._scan_import_folder)
        path_row.addWidget(self.import_dir_edit, stretch=1)
        path_row.addWidget(self.import_browse_btn)
        path_row.addWidget(self.import_scan_btn)
        iv.addLayout(path_row)

        self.import_accident_box = QFrame()
        self.import_accident_box.setFrameShape(QFrame.Shape.StyledPanel)
        ab_v = QVBoxLayout(self.import_accident_box)
        ab_v.setContentsMargins(8, 6, 8, 6)
        self.import_accident_lbl = QLabel("")
        self.import_accident_lbl.setWordWrap(True)
        self.import_accident_lbl.setStyleSheet("color: #d97706; font-weight: bold;")
        self.import_use_parent_btn = QPushButton("Use Full Parent Folder Instead")
        self.import_use_parent_btn.clicked.connect(self._switch_import_to_parent)
        ab_v.addWidget(self.import_accident_lbl)
        ab_v.addWidget(self.import_use_parent_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        self.import_accident_box.setVisible(False)
        iv.addWidget(self.import_accident_box)

        self.import_preview_lbl = QLabel("Pick any folder to automatically detect mods, worlds, resource packs, shaders, versions & layout.")
        self.import_preview_lbl.setWordWrap(True)
        iv.addWidget(self.import_preview_lbl)

        form = QFormLayout()
        tgt_row = QHBoxLayout()
        self.import_target_new = QRadioButton("New Profile:")
        self.import_target_cur = QRadioButton("Merge Into Selected")
        self.import_target_new.setChecked(True)
        self._imp_tgt_grp = QButtonGroup(self)
        self._imp_tgt_grp.addButton(self.import_target_new)
        self._imp_tgt_grp.addButton(self.import_target_cur)
        self.import_name_edit = QLineEdit()
        self.import_name_edit.setPlaceholderText("Auto-filled name...")
        tgt_row.addWidget(self.import_target_new)
        tgt_row.addWidget(self.import_name_edit, stretch=1)
        tgt_row.addWidget(self.import_target_cur)
        form.addRow("Destination:", tgt_row)

        self.import_mode_organize = QRadioButton("Copy && auto-organise into PyMcLauncher folder (safe)")
        self.import_mode_inplace = QRadioButton("Use chosen folder in-place && sort loose files")
        self.import_mode_organize.setChecked(True)
        self._imp_style_grp = QButtonGroup(self)
        self._imp_style_grp.addButton(self.import_mode_organize)
        self._imp_style_grp.addButton(self.import_mode_inplace)
        form.addRow("Organisation:", self.import_mode_organize)
        form.addRow("", self.import_mode_inplace)

        self.import_share_vers_cb = QCheckBox("Share downloaded versions across profiles")
        self.import_share_vers_cb.setChecked(True)
        self.import_autosort_cb = QCheckBox("Auto-sort loose .jar mods into mods/ && worlds into saves/")
        self.import_autosort_cb.setChecked(True)
        form.addRow("", self.import_share_vers_cb)
        form.addRow("", self.import_autosort_cb)

        self.import_link_saves_combo = QComboBox()
        self.import_link_saves_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.import_link_saves_combo.setMinimumContentsLength(12)
        form.addRow("Link Worlds:", self.import_link_saves_combo)

        sel_grid = QGridLayout()
        self.imp_cb_mods = QCheckBox("Mods")
        self.imp_cb_worlds = QCheckBox("Worlds")
        self.imp_cb_rp = QCheckBox("Resource Packs")
        self.imp_cb_sp = QCheckBox("Shader Packs")
        self.imp_cb_cfg = QCheckBox("Configs && Settings")
        self.imp_cb_vers = QCheckBox("Versions")
        for cb in (self.imp_cb_mods, self.imp_cb_worlds, self.imp_cb_rp, self.imp_cb_sp, self.imp_cb_cfg, self.imp_cb_vers):
            cb.setChecked(True)
        sel_grid.addWidget(self.imp_cb_mods, 0, 0)
        sel_grid.addWidget(self.imp_cb_worlds, 0, 1)
        sel_grid.addWidget(self.imp_cb_cfg, 0, 2)
        sel_grid.addWidget(self.imp_cb_rp, 1, 0)
        sel_grid.addWidget(self.imp_cb_sp, 1, 1)
        sel_grid.addWidget(self.imp_cb_vers, 1, 2)
        form.addRow("Include:", sel_grid)
        iv.addLayout(form)

        self.import_go_btn = QPushButton("Import && Organise Folder")
        self.import_go_btn.clicked.connect(self._run_tab_import)
        iv.addWidget(self.import_go_btn)
        v.addWidget(imp_box)
        v.addStretch()
        scroll.setWidget(page)
        return scroll

    def _build_dirs_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        v = QVBoxLayout(inner)
        v.setSpacing(10)

        # Live Link Status Table across ALL paths (automatically detects cross-profile links, OS symlinks/junctions & shared folders!)
        status_box = QGroupBox("Automatic Path && Link Detection (Every Directory)")
        sv = QVBoxLayout(status_box)
        self.dir_link_rows_layout = QFormLayout()
        self.dir_link_badges: Dict[str, tuple] = {}
        for key, lbl in (
            ("base", "Base (Game Dir):"),
            ("saves", "Worlds (saves/):"),
            ("versions", "Versions:"),
            ("mods", "Mods:"),
            ("resourcepacks", "Resource Packs:"),
            ("shaderpacks", "Shader Packs:"),
            ("screenshots", "Screenshots:"),
            ("assets", "Assets:"),
            ("libraries", "Libraries:"),
            ("indexes", "Indexes:"),
            ("java", "Java:"),
            ("source", "Source:"),
        ):
            r = QHBoxLayout()
            path_lbl = QLabel("")
            path_lbl.setWordWrap(True)
            badge_lbl = QLabel("Isolated")
            badge_lbl.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
            open_b = QPushButton("Open")
            open_b.setFixedWidth(55)
            open_b.clicked.connect(lambda _c=False, k=key: self._open_profile_folder(k))
            r.addWidget(path_lbl, stretch=1)
            r.addWidget(badge_lbl)
            r.addWidget(open_b)
            self.dir_link_badges[key] = (path_lbl, badge_lbl)
            self.dir_link_rows_layout.addRow(lbl, r)
        sv.addLayout(self.dir_link_rows_layout)
        v.addWidget(status_box)

        # Cross-Profile Folder Linking
        link_box = QGroupBox("Link Folders Across Profiles (Saves, Resource Packs, Shaders, Screenshots)")
        lf = QFormLayout(link_box)
        self.link_controls: Dict[str, tuple] = {}
        labels = {
            "saves": "Worlds (saves/):",
            "resourcepacks": "Resource Packs:",
            "shaderpacks": "Shader Packs:",
            "screenshots": "Screenshots:",
        }
        for folder in LINKABLE_GAME_SUBFOLDERS:
            row = QHBoxLayout()
            mode_cb = QComboBox()
            mode_cb.addItem("Isolated (Own Folder)", "isolated")
            mode_cb.addItem("Link to Profile", "profile")
            mode_cb.addItem("Custom Folder", "custom")
            target_cb = QComboBox()
            custom_edit = QLineEdit()
            custom_edit.setPlaceholderText("Custom folder path...")
            browse_btn = QPushButton("Browse...")
            browse_btn.clicked.connect(lambda _c=False, e=custom_edit: self._pick_dir_into(e))

            def _sync_vis(idx=0, m=mode_cb, t=target_cb, c=custom_edit, b=browse_btn):
                md = m.currentData()
                t.setVisible(md == "profile")
                c.setVisible(md == "custom")
                b.setVisible(md == "custom")

            mode_cb.currentIndexChanged.connect(_sync_vis)
            row.addWidget(mode_cb)
            row.addWidget(target_cb, stretch=1)
            row.addWidget(custom_edit, stretch=1)
            row.addWidget(browse_btn)
            self.link_controls[folder] = (mode_cb, target_cb, custom_edit, browse_btn, _sync_vis)
            lf.addRow(labels.get(folder, folder), row)
        v.addWidget(link_box)

        # JVM & Custom Path Overrides
        dirs_box = QGroupBox("Custom Directory Overrides && Launch Memory")
        df = QFormLayout(dirs_box)
        ram_row = QHBoxLayout()
        self.adv_ram_spin = QSpinBox()
        self.adv_ram_spin.setRange(0, 65536)
        self.adv_ram_spin.setSingleStep(512)
        self.adv_ram_spin.setSpecialValueText("Default (2048 MB)")
        self.adv_ram_spin.setSuffix(" MB")
        self.adv_jvm_edit = QLineEdit()
        self.adv_jvm_edit.setPlaceholderText("Extra JVM args (e.g. -XX:+UseZGC)")
        ram_row.addWidget(self.adv_ram_spin)
        ram_row.addWidget(self.adv_jvm_edit, stretch=1)
        df.addRow("Memory & JVM:", ram_row)

        self.adv_dir_inputs: Dict[str, QLineEdit] = {}
        for key, lbl in (
            ("base", "Base (Game Dir):"),
            ("versions", "Versions:"),
            ("saves", "Saves (Worlds):"),
            ("assets", "Assets:"),
            ("libraries", "Libraries:"),
            ("indexes", "Indexes:"),
            ("java", "Java:"),
            ("source", "Source:"),
        ):
            r = QHBoxLayout()
            inp = QLineEdit()
            b = QPushButton("Browse...")
            b.clicked.connect(lambda _c=False, e=inp: self._pick_dir_into(e))
            r.addWidget(inp, stretch=1)
            r.addWidget(b)
            self.adv_dir_inputs[key] = inp
            df.addRow(lbl, r)

        adv_save_row = QHBoxLayout()
        copy_opt_btn = QPushButton("Copy options.txt from Profile...")
        copy_opt_btn.clicked.connect(lambda: self._copy_config_file_from_profile("options.txt"))
        auto_fill_btn = QPushButton("Auto-Fill Subfolders From Base")
        auto_fill_btn.clicked.connect(self._autofill_adv_subdirs)
        save_dirs_btn = QPushButton("Save Directories && Apply Links")
        save_dirs_btn.clicked.connect(self._save_dirs_and_links)
        adv_save_row.addWidget(copy_opt_btn)
        adv_save_row.addWidget(auto_fill_btn)
        adv_save_row.addStretch()
        adv_save_row.addWidget(save_dirs_btn)
        df.addRow("", adv_save_row)
        v.addWidget(dirs_box)
        v.addStretch()
        scroll.setWidget(inner)
        return scroll

    def _build_source_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setSpacing(8)

        src_box = QGroupBox("Minecraft Source Code && Decompiled Assets (Source Mode)")
        sv = QVBoxLayout(src_box)
        sv.setSpacing(6)

        self.source_info_lbl = QLabel("Source directory:")
        self.source_info_lbl.setWordWrap(True)
        sv.addWidget(self.source_info_lbl)

        row = QHBoxLayout()
        row.addWidget(QLabel("Downloaded Version:"))
        self.source_version_combo = QComboBox()
        self.source_version_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.source_version_combo.setMinimumContentsLength(14)
        self.extract_source_btn = QPushButton("Extract / Decompile Source")
        self.extract_source_btn.clicked.connect(self._extract_selected_source)
        row.addWidget(self.source_version_combo, stretch=1)
        row.addWidget(self.extract_source_btn)
        sv.addLayout(row)

        self.source_extract_status_lbl = QLabel("Ready. Turn on Source Mode in Options to auto-extract during launch or extract here.")
        self.source_extract_status_lbl.setWordWrap(True)
        sv.addWidget(self.source_extract_status_lbl)
        v.addWidget(src_box)

        list_box = QGroupBox("Extracted Source Folders")
        lv = QVBoxLayout(list_box)
        self.source_folders_list = QListWidget()
        lv.addWidget(self.source_folders_list, stretch=1)
        btns = QHBoxLayout()
        open_src_btn = QPushButton("Open source/")
        open_src_btn.clicked.connect(lambda: self._open_profile_folder("source"))
        del_src_btn = QPushButton("Delete Source Folder")
        del_src_btn.setStyleSheet("color: #fb923c; font-weight: bold;")
        del_src_btn.clicked.connect(self._delete_source_folder_clicked)
        btns.addWidget(open_src_btn)
        btns.addStretch()
        btns.addWidget(del_src_btn)
        lv.addLayout(btns)
        v.addWidget(list_box, stretch=1)
        return page

    def sync_dev_mode_tab(self):
        """Show the Source Code sub-tab iff Source Mode (dev_mode) is enabled."""
        if not hasattr(self, "profile_subtabs") or not hasattr(self, "source_tab"):
            return
        want = bool(getattr(self.launcher_gui, "dev_mode", False))
        idx = self.profile_subtabs.indexOf(self.source_tab)
        if want and idx < 0:
            self.profile_subtabs.addTab(self.source_tab, "Source Code")
        elif not want and idx >= 0:
            self.profile_subtabs.removeTab(idx)
        pname = self._selected_profile_name()
        if pname:
            self._refresh_source_tab(pname)

    def _refresh_source_tab(self, pname: str):
        any_src = has_any_source()
        if hasattr(self, "delete_source_btn"):
            self.delete_source_btn.setVisible(bool(any_src or getattr(self.launcher_gui, "dev_mode", False)))
        if hasattr(self, "delete_all_versions_btn"):
            self.delete_all_versions_btn.setEnabled(has_any_downloads() and len(getattr(self.launcher_gui, "running_instances", [])) == 0)

        if not hasattr(self, "source_version_combo"):
            return
        src_dir = get_dir("source", profile_name=pname)
        assets_dir = get_dir("assets", profile_name=pname)
        self.source_info_lbl.setText(f"Source directory: {src_dir}\nUnpacked assets directory: {assets_dir}")

        self.source_version_combo.clear()
        for vid in sorted(get_downloaded_versions(profile_name=pname)):
            self.source_version_combo.addItem(vid, vid)
        self.extract_source_btn.setEnabled(self.source_version_combo.count() > 0)

        self.source_folders_list.clear()
        if src_dir.exists():
            for child in sorted(src_dir.iterdir(), key=lambda x: x.name.lower()):
                if child.is_dir():
                    it = QListWidgetItem(f"{child.name} ({child})")
                    it.setData(Qt.ItemDataRole.UserRole, str(child))
                    self.source_folders_list.addItem(it)
        if self.source_folders_list.count() == 0:
            empty_it = QListWidgetItem("No extracted source code folders yet.")
            empty_it.setFlags(Qt.ItemFlag.NoItemFlags)
            self.source_folders_list.addItem(empty_it)

    def _populate_loader_combo(self, cb: QComboBox):
        cb.clear()
        cb.addItem("Keep Current / Any", "")
        cb.addItem("No Mods (Vanilla)", "none")
        for info in getattr(self.launcher_gui, "_loaders", []):
            cb.addItem(info["name"], info["id"])

    def _pick_dir_into(self, edit: QLineEdit):
        folder = QFileDialog.getExistingDirectory(self, "Select Directory")
        if folder:
            edit.setText(folder.replace("\\", "/"))

    def refresh_profiles(self, select_name: str = None):
        self._is_loading = True
        try:
            config = load_launcher_config()
            profiles = config.get("profiles") or {"Default": build_profile_dirs("Default")}
            active = config.get("active_profile", "Default")
            target_sel = select_name or (self.profile_list.currentItem().data(Qt.ItemDataRole.UserRole) if self.profile_list.currentItem() else None) or active

            self.profile_list.clear()
            other_names = list(profiles.keys())

            for pname in profiles.keys():
                try:
                    stats = get_profile_stats(pname)
                    tags = []
                    if pname == active:
                        tags.append("Active")
                    if stats.get("preferred_loader") and stats["preferred_loader"] != "none":
                        tags.append(stats["preferred_loader"].capitalize())
                    if stats.get("worlds"):
                        tags.append(f"{len(stats['worlds'])}w")
                    if stats.get("active_mods"):
                        tags.append(f"{len(stats['active_mods'])}m")
                    if stats.get("versions"):
                        tags.append(f"{len(stats['versions'])}v")
                    suffix = f" [{' | '.join(tags)}]" if tags else ""
                except Exception:
                    suffix = " [Active]" if pname == active else ""
                item = QListWidgetItem(f"{pname}{suffix}")
                item.setData(Qt.ItemDataRole.UserRole, pname)
                self.profile_list.addItem(item)
                if pname == target_sel:
                    self.profile_list.setCurrentItem(item)

            if self.profile_list.currentRow() < 0 and self.profile_list.count() > 0:
                self.profile_list.setCurrentRow(0)

            self.import_link_saves_combo.blockSignals(True)
            self.import_link_saves_combo.clear()
            self.import_link_saves_combo.addItem("Keep Own Worlds (Isolated)", "")
            for n in other_names:
                self.import_link_saves_combo.addItem(f"Link Worlds from '{n}'", n)
            self.import_link_saves_combo.blockSignals(False)

            cur = self._selected_profile_name() or active
            self._load_profile_details(cur)
        finally:
            self._is_loading = False

    def _on_list_profile_changed(self, current, _prev):
        if self._is_loading or not current:
            return
        pname = current.data(Qt.ItemDataRole.UserRole) or current.text()
        self._load_profile_details(pname)

    def _load_profile_details(self, pname: str):
        self._is_loading = True
        try:
            config = load_launcher_config()
            profiles = config.get("profiles") or {}
            dirs = profiles.get(pname, {})
            if not isinstance(dirs, dict):
                dirs = {}
            stats = get_profile_stats(pname)

            link_desc = f"Linked -> {stats['saves_link_profile']}" if stats.get("saves_link_mode") == "profile" else ("Custom" if stats.get("saves_link_mode") == "custom" else "Isolated")
            vers_desc = "Isolated (profile-only)" if stats.get("isolate_versions") else "Shared across profiles"
            self.prof_summary_lbl.setText(
                f"Profile: {pname} | Game Dir: {stats['base_dir']}\n"
                f"Versions: {vers_desc} ({len(stats['versions'])}) | "
                f"Worlds: {len(stats['worlds'])} ({link_desc}) | "
                f"Mods: {len(stats['active_mods'])} active ({len(stats['disabled_mods'])} disabled)"
            )

            # Worlds Tab
            self.worlds_link_status_lbl.setText(f"Current saves/ folder: {stats['saves_dir']} ({link_desc})")
            self.worlds_list_widget.clear()
            for w in stats.get("worlds") or []:
                it = QListWidgetItem(f"{w}" + (f" [Linked from {stats['saves_link_profile']}]" if stats.get("saves_link_profile") else ""))
                it.setData(Qt.ItemDataRole.UserRole, w)
                self.worlds_list_widget.addItem(it)

            self.worlds_link_combo.blockSignals(True)
            self.worlds_link_combo.clear()
            self.worlds_link_combo.addItem("Keep Own Worlds (Isolated)", "")
            for n in profiles.keys():
                if n != pname:
                    self.worlds_link_combo.addItem(f"Link Worlds from '{n}'", n)
            sel_lp = stats.get("saves_link_profile") if stats.get("saves_link_mode") == "profile" else ""
            idx_w = self.worlds_link_combo.findData(sel_lp or "")
            if idx_w >= 0:
                self.worlds_link_combo.setCurrentIndex(idx_w)
            self.worlds_link_combo.blockSignals(False)

            # Mods Tab
            idx_pl = self.pref_loader_combo.findData(dirs.get("preferred_loader") or "")
            self.pref_loader_combo.setCurrentIndex(idx_pl if idx_pl >= 0 else 0)
            self.pref_version_edit.setText(str(dirs.get("preferred_version") or ""))
            self._refresh_mods_list(pname)

            # Versions Tab
            self.isolate_vers_cb.setChecked(bool(stats.get("isolate_versions")))
            self.versions_info_lbl.setText(
                f"Showing {len(stats['versions'])} downloaded version(s) in {'profile-only' if stats.get('isolate_versions') else 'shared'} storage: "
                f"{get_dir('versions', profile_name=pname)}"
            )
            self._refresh_versions_list(pname, bool(stats.get("isolate_versions")))

            # Directories & Links Tab (automatic path/link inspection!)
            link_infos = inspect_profile_directory_links(pname)
            for linfo in link_infos:
                pair = self.dir_link_badges.get(linfo["key"])
                if not pair:
                    continue
                path_lbl, badge_lbl = pair
                path_lbl.setText(linfo["resolved"])
                badge_lbl.setText(linfo["badge"])
                st = linfo["status"]
                if st in ("linked_profile", "linked_custom", "os_symlink", "os_junction", "mirror_sync"):
                    badge_lbl.setStyleSheet("color: #2563eb; font-weight: bold;")
                elif st == "shared_with_others":
                    badge_lbl.setStyleSheet("color: #059669; font-weight: bold;")
                else:
                    badge_lbl.setStyleSheet("color: #6b7280;")

            self.adv_ram_spin.setValue(int(dirs.get("ram_mb") or 0))
            self.adv_jvm_edit.setText(str(dirs.get("jvm_args") or ""))
            for folder, (mode_cb, target_cb, custom_edit, _b, sync_fn) in self.link_controls.items():
                target_cb.clear()
                for n in profiles.keys():
                    if n != pname:
                        target_cb.addItem(n, n)
                lmode = dirs.get(f"{folder}_link_mode", "isolated")
                mi = mode_cb.findData(lmode)
                mode_cb.setCurrentIndex(mi if mi >= 0 else 0)
                tprof = dirs.get(f"{folder}_link_profile") or ""
                ti = target_cb.findData(tprof)
                if ti >= 0:
                    target_cb.setCurrentIndex(ti)
                custom_edit.setText(str(dirs.get(folder) or ""))
                sync_fn()

            for k, inp in self.adv_dir_inputs.items():
                inp.setText(str(dirs.get(k) or ""))

            # Source tab & storage maintenance buttons
            self._refresh_source_tab(pname)
            self.refresh_versions_queue()
            if hasattr(self.launcher_gui, "_refresh_browser_for_profile"):
                self.launcher_gui._refresh_browser_for_profile(pname)
        finally:
            self._is_loading = False

    def _refresh_mods_list(self, pname: str):
        self.mods_list_widget.clear()
        mods_dir = get_dir("mods", profile_name=pname)
        if not mods_dir.exists():
            return
        from utils import _inspect_jar_mod_hint
        for p in sorted(mods_dir.iterdir(), key=lambda x: x.name.lower()):
            if p.is_file() and (p.suffix.lower() in (".jar", ".zip") or p.name.lower().endswith(".jar.disabled")):
                disabled = p.name.lower().endswith(".disabled")
                hint = _inspect_jar_mod_hint(p) if p.suffix.lower() == ".jar" else None
                hint_str = f" [{hint.capitalize()}]" if hint else ""
                label = f"{'[Disabled] ' if disabled else ''}{p.name}{hint_str}"
                it = QListWidgetItem(label)
                it.setData(Qt.ItemDataRole.UserRole, str(p))
                self.mods_list_widget.addItem(it)

    def _refresh_versions_list(self, pname: str, is_isolated: bool):
        self.versions_list_widget.clear()
        vers = sorted(get_downloaded_versions(profile_name=pname))
        prof_v_dir = get_dir("base", profile_name=pname) / "versions"
        for vid in vers:
            is_local = (prof_v_dir / vid).is_dir() or (prof_v_dir / f"{vid}.json").is_file()
            tag = "Profile-Only" if (is_isolated or is_local) else "Shared"
            lid, _mc, _b = download_mod_loaders.detect_version_loader(vid, None)
            ldr_tag = "Vanilla" if lid == "none" else lid.capitalize()
            it = QListWidgetItem(f"{vid} ({ldr_tag} | {tag})")
            it.setData(Qt.ItemDataRole.UserRole, vid)
            self.versions_list_widget.addItem(it)

    # --- Worlds Tab Actions ---
    def _import_world_folders_prompt(self):
        pname = self._selected_profile_name()
        if not pname:
            return
        folder = QFileDialog.getExistingDirectory(self, "Select World Folder or saves/ Folder to Import")
        if not folder:
            return
        imported = import_worlds_to_profile([Path(folder)], pname)
        if not imported:
            QMessageBox.information(self, "No Worlds Found", "No folders containing level.dat were found in that directory.")
            return
        self._sync_launcher_after_profile_change(pname)
        QMessageBox.information(self, "Worlds Imported", f"Imported {len(imported)} world(s): {', '.join(imported)}")

    def _import_world_zip_prompt(self):
        pname = self._selected_profile_name()
        if not pname:
            return
        files, _ = QFileDialog.getOpenFileNames(self, "Select World .zip Archive(s)", "", "Zip Archives (*.zip);;All Files (*)")
        if not files:
            return
        imported = import_worlds_to_profile([Path(f) for f in files], pname)
        if not imported:
            QMessageBox.information(self, "No Worlds Found", "No level.dat world folder found inside selected zip(s).")
            return
        self._sync_launcher_after_profile_change(pname)
        QMessageBox.information(self, "Worlds Imported", f"Imported {len(imported)} world(s): {', '.join(imported)}")

    def _quick_play_selected_world(self):
        pname = self._selected_profile_name()
        it = self.worlds_list_widget.currentItem()
        if not pname or not it:
            return
        w_name = it.data(Qt.ItemDataRole.UserRole)
        self.launcher_gui.quick_play_profile_target(pname, "singleplayer", w_name)

    def _duplicate_selected_world(self):
        pname = self._selected_profile_name()
        it = self.worlds_list_widget.currentItem()
        if not pname or not it:
            return
        w_name = it.data(Qt.ItemDataRole.UserRole)
        saves_dir = get_dir("saves", profile_name=pname)
        src = saves_dir / w_name
        if not src.is_dir():
            return
        import shutil
        dst = saves_dir / f"{w_name}_backup"
        idx = 2
        while dst.exists():
            dst = saves_dir / f"{w_name}_backup_{idx}"
            idx += 1
        shutil.copytree(str(src), str(dst))
        self._sync_launcher_after_profile_change(pname)

    def _delete_selected_world(self):
        pname = self._selected_profile_name()
        it = self.worlds_list_widget.currentItem()
        if not pname or not it:
            return
        w_name = it.data(Qt.ItemDataRole.UserRole)
        target = get_dir("saves", profile_name=pname) / w_name
        if not target.exists():
            return
        reply = QMessageBox.question(
            self, "Delete World",
            f"Permanently delete world '{w_name}' from {target.parent}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        import shutil
        shutil.rmtree(str(target), ignore_errors=True)
        self._sync_launcher_after_profile_change(pname)

    def _apply_quick_worlds_link(self):
        pname = self._selected_profile_name()
        if not pname:
            return
        config = load_launcher_config()
        profiles = config.get("profiles") or {}
        cur = dict(profiles.get(pname) or build_profile_dirs(pname))
        link_prof = self.worlds_link_combo.currentData() or ""
        base_str = str(cur.get("base") or "minecraft_downloads").rstrip("/\\")
        if link_prof and link_prof != pname:
            cur["saves_link_mode"] = "profile"
            cur["saves_link_profile"] = link_prof
        else:
            cur["saves_link_mode"] = "isolated"
            cur.pop("saves_link_profile", None)
            cur["saves"] = f"{base_str}/saves"
        profiles[pname] = cur
        config["profiles"] = profiles
        save_launcher_config(config)
        ensure_profile_links(pname)
        self._sync_launcher_after_profile_change(pname)

    # --- Mods Tab Actions ---
    def _save_loader_defaults(self):
        pname = self._selected_profile_name()
        if not pname:
            return
        config = load_launcher_config()
        profiles = config.get("profiles") or {}
        cur = dict(profiles.get(pname) or build_profile_dirs(pname))
        pref_l = self.pref_loader_combo.currentData()
        if pref_l:
            cur["preferred_loader"] = pref_l
        else:
            cur.pop("preferred_loader", None)
        pref_v = self.pref_version_edit.text().strip()
        if pref_v:
            cur["preferred_version"] = pref_v
        else:
            cur.pop("preferred_version", None)
        profiles[pname] = cur
        config["profiles"] = profiles
        save_launcher_config(config)
        self._sync_launcher_after_profile_change(pname)

    def _add_mods_to_profile(self):
        pname = self._selected_profile_name()
        if not pname:
            return
        files, _ = QFileDialog.getOpenFileNames(self, "Select Mod Archives", "", "Mod Archives (*.jar *.zip);;All Files (*)")
        if not files:
            return
        imported, _hints = import_mods_to_profile([Path(f) for f in files], pname)
        self._load_profile_details(pname)
        self.refresh_profiles(pname)
        self.launcher_gui.refresh_profile_cards()

    def _import_mods_folder_prompt(self):
        pname = self._selected_profile_name()
        if not pname:
            return
        folder = QFileDialog.getExistingDirectory(self, "Select Folder Containing Mods")
        if not folder:
            return
        imported, _hints = import_mods_to_profile([Path(folder)], pname)
        if not imported:
            QMessageBox.information(self, "No Mods Found", "No .jar mods found in the chosen directory.")
            return
        self._load_profile_details(pname)
        self.refresh_profiles(pname)
        self.launcher_gui.refresh_profile_cards()
        QMessageBox.information(self, "Mods Imported", f"Imported {len(imported)} mod file(s) into '{pname}'.")

    def _toggle_selected_mod(self):
        it = self.mods_list_widget.currentItem()
        if not it:
            return
        p = Path(it.data(Qt.ItemDataRole.UserRole))
        if not p.exists():
            return
        if p.name.lower().endswith(".disabled"):
            new_p = p.with_name(p.name[:-9])
        else:
            new_p = p.with_name(p.name + ".disabled")
        p.rename(new_p)
        pname = self._selected_profile_name()
        self._load_profile_details(pname)
        self.refresh_profiles(pname)
        self.launcher_gui.refresh_profile_cards()

    def _copy_selected_mod_to_profile(self):
        pname = self._selected_profile_name()
        it = self.mods_list_widget.currentItem()
        if not pname or not it:
            return
        config = load_launcher_config()
        others = [k for k in (config.get("profiles") or {}).keys() if k != pname]
        if not others:
            QMessageBox.information(self, "No Other Profile", "Create another profile first.")
            return
        dst_p, ok = QInputDialog.getItem(self, "Copy Mod", "Copy mod to profile:", others, 0, False)
        if not ok or not dst_p:
            return
        src = Path(it.data(Qt.ItemDataRole.UserRole))
        if src.is_file():
            import_mods_to_profile([src], dst_p)
            self.refresh_profiles(pname)
            self.launcher_gui.refresh_profile_cards()

    def _remove_selected_mod(self):
        it = self.mods_list_widget.currentItem()
        if not it:
            return
        p = Path(it.data(Qt.ItemDataRole.UserRole))
        if p.exists():
            p.unlink()
        pname = self._selected_profile_name()
        self._load_profile_details(pname)
        self.refresh_profiles(pname)
        self.launcher_gui.refresh_profile_cards()

    # --- Versions Tab Actions ---
    def _apply_version_isolation_setting(self):
        pname = self._selected_profile_name()
        if not pname:
            return
        config = load_launcher_config()
        profiles = config.get("profiles") or {}
        cur = dict(profiles.get(pname) or build_profile_dirs(pname))
        iso = self.isolate_vers_cb.isChecked()
        cur["isolate_versions"] = iso
        base_str = str(cur.get("base") or "minecraft_downloads").rstrip("/\\")
        cur["versions"] = f"{base_str}/versions" if iso else "minecraft_downloads/versions"
        profiles[pname] = cur
        config["profiles"] = profiles
        save_launcher_config(config)
        self._sync_launcher_after_profile_change(pname)

    def _copy_version_into_profile(self):
        pname = self._selected_profile_name()
        if not pname:
            return
        shared_v = ROOT_DIR / "minecraft_downloads" / "versions"
        if not shared_v.exists():
            QMessageBox.information(self, "No Shared Versions", "No versions found in minecraft_downloads/versions.")
            return
        cands = sorted([d.name for d in shared_v.iterdir() if d.is_dir() and (d / f"{d.name}.json").is_file()])
        if not cands:
            QMessageBox.information(self, "No Shared Versions", "No downloadable versions available to copy.")
            return
        ver, ok = QInputDialog.getItem(self, "Copy Version", f"Select downloaded version to copy into '{pname}':", cands, 0, False)
        if not ok or not ver:
            return
        import shutil
        dest_v = get_dir("versions", profile_name=pname)
        dest_v.mkdir(parents=True, exist_ok=True)
        for p in (shared_v / ver, shared_v / f"{ver}.json", shared_v / f"{ver}-client.jar"):
            if p.is_dir():
                shutil.copytree(str(p), str(dest_v / p.name), dirs_exist_ok=True)
            elif p.is_file():
                shutil.copy2(str(p), str(dest_v / p.name))
        self._sync_launcher_after_profile_change(pname)
        QMessageBox.information(self, "Copied", f"Copied version '{ver}' into '{pname}'.")

    def _set_selected_version_default(self):
        pname = self._selected_profile_name()
        it = self.versions_list_widget.currentItem()
        if not pname or not it:
            return
        vid = it.data(Qt.ItemDataRole.UserRole)
        self.pref_version_edit.setText(vid)
        self._save_loader_defaults()

    def _play_selected_profile_version(self):
        pname = self._selected_profile_name()
        it = self.versions_list_widget.currentItem()
        if not pname or not it:
            return
        vid = it.data(Qt.ItemDataRole.UserRole)
        self.launcher_gui.quick_play_profile_target(pname, version_id=vid)

    def refresh_versions_queue(self):
        """Render the editable Install & Launch Queue inside Profiles -> Versions tab."""
        if not hasattr(self, "versions_queue_list"):
            return
        self.versions_queue_list.clear()
        items = get_pending_install_queue()
        if not items:
            empty = QListWidgetItem("Queue is empty — select version(s) above and click '+ Add Selected to Queue'.")
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.versions_queue_list.addItem(empty)
            return
        for item in items:
            m_tag = "[Launch & Install]" if item.get("mode") == "launch" else "[Just Install]"
            lbl = str(item.get("label") or f"{item.get('profile', 'Default')}: {item.get('mc_version', '')}")
            li = QListWidgetItem(f"{m_tag}  {lbl}")
            li.setData(Qt.ItemDataRole.UserRole, str(item.get("id") or ""))
            self.versions_queue_list.addItem(li)

    def _selected_queue_ids(self) -> set[str]:
        sel = self.versions_queue_list.selectedItems() if hasattr(self, "versions_queue_list") else []
        if not sel and hasattr(self, "versions_queue_list") and self.versions_queue_list.currentItem():
            sel = [self.versions_queue_list.currentItem()]
        return {str(it.data(Qt.ItemDataRole.UserRole)) for it in sel if it and it.data(Qt.ItemDataRole.UserRole)}

    def _versions_queue_toggle_mode(self):
        qids = self._selected_queue_ids()
        if not qids:
            return
        items = get_pending_install_queue()
        for it in items:
            if str(it.get("id") or "") in qids:
                it["mode"] = "launch" if it.get("mode") != "launch" else "install"
        save_pending_install_queue(items)
        self.refresh_versions_queue()
        self.launcher_gui.refresh_play_install_queue()

    def _versions_queue_change_profile(self):
        qids = self._selected_queue_ids()
        if not qids:
            return
        cfg = load_launcher_config()
        profiles = list((cfg.get("profiles") or {"Default": {}}).keys())
        if not profiles:
            return
        cur_p = self._selected_profile_name() or profiles[0]
        cur_idx = profiles.index(cur_p) if cur_p in profiles else 0
        target_p, ok = QInputDialog.getItem(self, "Change Target Profile", "Assign queued item(s) to profile:", profiles, cur_idx, False)
        if not ok or not target_p:
            return
        items = get_pending_install_queue()
        for it in items:
            if str(it.get("id") or "") in qids:
                old_p = str(it.get("profile") or "")
                it["profile"] = target_p
                bver = str((it.get("build_entry") or {}).get("version") or "")
                it["id"] = make_install_queue_id(target_p, str(it.get("loader_id") or "none"), str(it.get("mc_version") or ""), bver)
                lbl = str(it.get("label") or "")
                if old_p and lbl.startswith(f"{old_p}: "):
                    it["label"] = f"{target_p}: " + lbl[len(old_p) + 2:]
                else:
                    it["label"] = f"{target_p}: {lbl}"
        save_pending_install_queue(items)
        self.refresh_versions_queue()
        self.launcher_gui.refresh_play_install_queue()
        self.launcher_gui.refresh_profile_cards()

    def _versions_queue_move(self, delta: int):
        qids = self._selected_queue_ids()
        if len(qids) != 1:
            return
        qid = next(iter(qids))
        items = get_pending_install_queue()
        idx = next((i for i, x in enumerate(items) if str(x.get("id") or "") == qid), -1)
        new_idx = idx + delta
        if idx < 0 or new_idx < 0 or new_idx >= len(items):
            return
        items[idx], items[new_idx] = items[new_idx], items[idx]
        save_pending_install_queue(items)
        self.refresh_versions_queue()
        self.versions_queue_list.setCurrentRow(new_idx)
        self.launcher_gui.refresh_play_install_queue()

    def _versions_queue_remove(self):
        qids = self._selected_queue_ids()
        if not qids:
            return
        remove_pending_install_items(qids)
        self.refresh_versions_queue()
        self.launcher_gui.refresh_play_install_queue()
        self.launcher_gui.refresh_profile_cards()

    def _versions_queue_run(self):
        items = get_pending_install_queue()
        if not items:
            return
        launch_items = [x for x in items if x.get("mode") == "launch"]
        self.launcher_gui.start_batch_install(
            items,
            target_profile=str((launch_items[0] if launch_items else items[0]).get("profile") or self._selected_profile_name() or "Default"),
            launch_last=bool(launch_items),
        )

    @staticmethod
    def _format_mc_disp(mc_v: str) -> str:
        if mc_v in ("latest", "latest-release"):
            return "Latest Release"
        if mc_v == "latest-snapshot":
            return "Latest Snapshot"
        return f"MC {mc_v}"

    @staticmethod
    def _format_build_disp(b: dict) -> str:
        bv = str(b.get("version") or b.get("full_id") or "")
        if bv == "__latest_recommended__":
            return "Latest Recommended"
        if bv == "__latest_beta__":
            return "Latest Beta"
        if bv == "__latest__":
            return "Latest Build"
        return bv

    def _collect_selected_install_items(self) -> List[Dict]:
        pname = self._selected_profile_name() or "Default"
        gui = self.launcher_gui
        loader_id = getattr(gui, "_active_loader_id", "none") or "none"
        sel_mc_items = gui.mc_versions_list.selectedItems()
        if not sel_mc_items and gui.mc_versions_list.currentItem():
            sel_mc_items = [gui.mc_versions_list.currentItem()]
        mc_versions = [
            str(it.data(Qt.ItemDataRole.UserRole))
            for it in sel_mc_items
            if it and it.data(Qt.ItemDataRole.UserRole)
        ]
        if not mc_versions:
            return []

        loader_name = "Vanilla" if loader_id == "none" else (gui._loader_map.get(loader_id, {}).get("name") or loader_id)
        items: List[Dict] = []

        if loader_id == "none":
            for mc_v in mc_versions:
                items.append({
                    "id": make_install_queue_id(pname, "none", mc_v),
                    "profile": pname,
                    "loader_id": "none",
                    "mc_version": mc_v,
                    "build_entry": None,
                    "mode": "install",
                    "label": f"{pname}: Vanilla {self._format_mc_disp(mc_v)}",
                })
            return items

        sel_build_items = gui.loader_versions_list.selectedItems()
        if not sel_build_items and gui.loader_versions_list.currentItem():
            sel_build_items = [gui.loader_versions_list.currentItem()]
        builds = [
            dict(it.data(Qt.ItemDataRole.UserRole))
            for it in sel_build_items
            if it and isinstance(it.data(Qt.ItemDataRole.UserRole), dict)
        ]
        local_file = gui.local_file_edit.text().strip() or None if hasattr(gui, "local_file_edit") else None
        agent_args = gui.agent_args_edit.text().strip() or None if hasattr(gui, "agent_args_edit") else None

        if len(mc_versions) == 1 and len(builds) >= 1:
            mc_v = mc_versions[0]
            for b in builds:
                bver = str(b.get("version") or b.get("full_id") or "")
                bdisp = self._format_build_disp(b)
                items.append({
                    "id": make_install_queue_id(pname, loader_id, mc_v, bver),
                    "profile": pname,
                    "loader_id": loader_id,
                    "mc_version": mc_v,
                    "build_entry": b,
                    "local_file": local_file,
                    "agent_args": agent_args,
                    "mode": "install",
                    "label": f"{pname}: {loader_name} {bdisp} ({self._format_mc_disp(mc_v)})",
                })
        else:
            for mc_v in mc_versions:
                if builds and (len(builds) == 1 and (str(builds[0].get("version") or "").startswith("__latest") or str(builds[0].get("mc_version") or mc_v) == mc_v)):
                    b = dict(builds[0])
                else:
                    b = {"version": "__latest_recommended__", "tag": "latest recommended"}
                b_ver = str(b.get("version") or "")
                b_disp = self._format_build_disp(b)
                items.append({
                    "id": make_install_queue_id(pname, loader_id, mc_v, b_ver),
                    "profile": pname,
                    "loader_id": loader_id,
                    "mc_version": mc_v,
                    "build_entry": b,
                    "local_file": local_file,
                    "agent_args": agent_args,
                    "mode": "install",
                    "label": f"{pname}: {loader_name} {b_disp} ({self._format_mc_disp(mc_v)})",
                })
        return items

    def _install_selected_multi(self):
        items = self._collect_selected_install_items()
        if not items:
            QMessageBox.information(self, "No Version Selected", "Select one or more Minecraft versions / mod loader builds first.")
            return
        pname = self._selected_profile_name() or "Default"
        self.launcher_gui.start_batch_install(items, target_profile=pname, launch_last=False)

    def _queue_selected_multi(self):
        items = self._collect_selected_install_items()
        if not items:
            QMessageBox.information(self, "No Version Selected", "Select one or more Minecraft versions / mod loader builds first.")
            return
        added = add_pending_install_items(items)
        self.refresh_versions_queue()
        self.launcher_gui.refresh_play_install_queue()
        self.launcher_gui.refresh_profile_cards()
        QMessageBox.information(
            self,
            "Added to Install Queue",
            f"Added {len(added)} new item(s) ({len(items)} selected) to the Install Queue.\n"
            "You can adjust Just Install vs Launch & Install modes here or on the Play tab.",
        )

    def _launch_install_selected_multi(self):
        items = self._collect_selected_install_items()
        if not items:
            QMessageBox.information(self, "No Version Selected", "Select a version to launch & install first.")
            return
        chosen = dict(items[0])
        chosen["mode"] = "launch"
        pname = self._selected_profile_name() or "Default"
        self.launcher_gui.start_batch_install([chosen], target_profile=pname, launch_last=True)

    def _delete_selected_profile_version(self):
        pname = self._selected_profile_name()
        sel_items = self.versions_list_widget.selectedItems()
        if not sel_items and self.versions_list_widget.currentItem():
            sel_items = [self.versions_list_widget.currentItem()]
        vids = [str(it.data(Qt.ItemDataRole.UserRole)) for it in sel_items if it and it.data(Qt.ItemDataRole.UserRole)]
        if not pname or not vids:
            return
        running = {inst.get("version") for inst in getattr(self.launcher_gui, "running_instances", [])}
        in_use = [v for v in vids if v in running]
        if in_use:
            QMessageBox.warning(self, "Version In Use", f"Stop running Minecraft version(s) before deleting: {', '.join(in_use)}")
            return
        reply = QMessageBox.question(
            self,
            "Delete Version(s)",
            f"Delete {len(vids)} version(s) ({', '.join(vids)}) from {get_dir('versions', profile_name=pname)}?\n\n"
            "(This will NOT delete your worlds, saves, or configs.)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        def _do_multi_del(prog_cb, log_cb):
            ok_all = True
            with profile_context(pname):
                for vid in vids:
                    if not delete_version_files(vid, log_callback=log_cb, progress_callback=prog_cb):
                        ok_all = False
                    else:
                        log_cb(f"[System] Deleted version {vid} from {pname}.")
                try:
                    from garbage_collect import perform_garbage_collection
                    perform_garbage_collection(log_callback=log_cb, progress_callback=prog_cb)
                except Exception as e:
                    log_cb(f"[Warning] Garbage collection skipped: {e}")
            return ok_all

        run_deletion_with_progress(
            self,
            f"Deleting {len(vids)} Version(s)...",
            _do_multi_del,
            log_callback=self.launcher_gui.log,
        )
        self._sync_launcher_after_profile_change(pname)

    def _delete_all_downloads_clicked(self):
        if getattr(self.launcher_gui, "running_instances", []):
            QMessageBox.warning(self, "Game Running", "Close all running Minecraft instances before deleting downloads.")
            return
        reply = QMessageBox.question(
            self,
            "Confirm Delete All Versions",
            "Are you sure you want to delete ALL downloaded Minecraft versions, assets, and libraries?\n\n"
            "(This will NOT delete your saves, worlds, source folder, or configurations.)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        pname = self._selected_profile_name() or "Default"
        self.launcher_gui.log("=== Deleting All Downloaded Versions & Shared Libraries ===")

        def _do_all(prog_cb, log_cb):
            with profile_context(pname):
                return delete_all_downloads(progress_callback=prog_cb, log_callback=log_cb)

        ok = run_deletion_with_progress(
            self,
            "Deleting All Downloads...",
            _do_all,
            log_callback=self.launcher_gui.log,
        )
        self._sync_launcher_after_profile_change(pname)
        if ok:
            QMessageBox.information(self, "Success", "All downloads have been successfully deleted.")
        else:
            QMessageBox.warning(self, "Error", "Failed to delete some files. They may be in use.")

    def _delete_source_folder_clicked(self):
        if getattr(self.launcher_gui, "running_instances", []):
            QMessageBox.warning(self, "Game Running", "Close all running Minecraft instances before deleting source/dev assets.")
            return
        pname = self._selected_profile_name() or "Default"
        reply = QMessageBox.question(
            self,
            "Confirm Delete Source Folder",
            f"Are you sure you want to delete the extracted source code folder ({get_dir('source', profile_name=pname)}) "
            f"and unpacked dev-mode assets in {get_dir('assets', profile_name=pname)}?\n\n"
            "(Normal cached game assets in assets/objects and assets/indexes will NOT be deleted.)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        def _do_src(prog_cb, log_cb):
            with profile_context(pname):
                return delete_source_folder(progress_callback=prog_cb, log_callback=log_cb)

        ok = run_deletion_with_progress(
            self,
            "Deleting Source Folder && Dev Assets...",
            _do_src,
            log_callback=self.launcher_gui.log,
        )
        self._refresh_source_tab(pname)
        if ok:
            self.launcher_gui.log("[System] Deleted extracted source folder and unpacked dev-mode assets.")
            QMessageBox.information(self, "Success", "Source folder and unpacked dev-mode assets deleted.")
        else:
            QMessageBox.warning(self, "Error", "Failed to delete some files in source folder.")

    def _extract_selected_source(self):
        pname = self._selected_profile_name() or "Default"
        vid = self.source_version_combo.currentData() if hasattr(self, "source_version_combo") else None
        if not vid:
            return
        self.extract_source_btn.setEnabled(False)
        self.source_extract_status_lbl.setText(f"Extracting & decompiling {vid} for '{pname}'...")
        self.launcher_gui.log(f"[Source] Extracting & decompiling {vid} ({pname})...")
        worker = _SourceExtractWorker(vid, profile_name=pname, parent=self)

        def _done(ok: bool, msg: str):
            self.extract_source_btn.setEnabled(True)
            self.source_extract_status_lbl.setText(msg if ok else f"Failed: {msg}")
            self._refresh_source_tab(pname)
            if ok:
                self.launcher_gui.log(f"[Source] {msg}")
                QMessageBox.information(self, "Source Extracted", msg)
            else:
                self.launcher_gui.log(f"[Error] Source extract failed: {msg}")
                QMessageBox.warning(self, "Extract Failed", msg)

        worker.finished_extract.connect(_done)
        self.launcher_gui._track_and_start_loader_worker(worker)

    # --- Import / Organise Tab Actions ---
    def _browse_import_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Minecraft / Modpack / Saves / Mods Folder")
        if folder:
            self.import_dir_edit.setText(folder)
            self._scan_import_folder()

    def _scan_import_folder(self):
        raw = self.import_dir_edit.text().strip()
        if not raw:
            return
        info = analyze_import_directory(Path(raw))
        self._last_analysis = info
        if not info.get("exists"):
            self.import_preview_lbl.setText("Folder does not exist.")
            self.import_accident_box.setVisible(False)
            return
        if info.get("accidental_warning") and info.get("parent_candidate"):
            self.import_accident_lbl.setText(info["accidental_warning"])
            p_name = Path(info["parent_candidate"]).name
            self.import_use_parent_btn.setText(f"Use Full Parent Folder ('{p_name}') Instead")
            self.import_accident_box.setVisible(True)
        else:
            self.import_accident_box.setVisible(False)

        if not self.import_name_edit.text().strip():
            self.import_name_edit.setText(info.get("suggested_name") or "Imported Profile")
        self.imp_cb_mods.setText(f"Mods ({info['mods_count']})")
        self.imp_cb_mods.setChecked(info["mods_count"] > 0)
        self.imp_cb_worlds.setText(f"Worlds ({info['worlds_count']})")
        self.imp_cb_worlds.setChecked(info["worlds_count"] > 0)
        self.imp_cb_rp.setText(f"Resource Packs ({info['resourcepacks_count']})")
        self.imp_cb_rp.setChecked(info["resourcepacks_count"] > 0)
        self.imp_cb_sp.setText(f"Shader Packs ({info['shaderpacks_count']})")
        self.imp_cb_sp.setChecked(info["shaderpacks_count"] > 0)
        self.imp_cb_cfg.setChecked(bool(info.get("has_config") or info.get("has_options_txt") or info.get("has_servers_dat")))
        self.imp_cb_vers.setText(f"Versions ({len(info['versions_found'])})")
        self.imp_cb_vers.setChecked(len(info["versions_found"]) > 0 and not self.import_share_vers_cb.isChecked())

        ldr = info.get("detected_loader") or "none"
        mc_v = info.get("detected_mc_version") or "Unknown"
        layout_note = " | Non-standard layout detected (will auto-organise)" if info.get("has_nonstandard_layout") else ""
        self.import_preview_lbl.setText(
            f"Detected Root: {info['resolved_root']}\n"
            f"Loader Hint: {ldr} (MC {mc_v}) | Mods: {info['mods_count']} (loose: {info['loose_mods_count']}) | "
            f"Worlds: {info['worlds_count']} (loose: {info['loose_worlds_count']}){layout_note}"
        )

    def _switch_import_to_parent(self):
        if self._last_analysis and self._last_analysis.get("parent_candidate"):
            self.import_dir_edit.setText(self._last_analysis["parent_candidate"])
            self._scan_import_folder()

    def _run_tab_import(self):
        raw = self.import_dir_edit.text().strip()
        if not raw or not Path(raw).exists():
            QMessageBox.warning(self, "Folder Missing", "Please choose a valid folder to import.")
            return
        if self.import_target_cur.isChecked():
            pname = self._selected_profile_name() or "Default"
            # Merge directly into selected profile's worlds/mods/etc.
            from utils import _copy_merge_tree
            info = analyze_import_directory(Path(raw))
            src_root = Path(info["resolved_root"])
            base_dst = get_dir("base", profile_name=pname)
            count = 0
            if self.imp_cb_worlds.isChecked() and info.get("world_paths"):
                w_imp = import_worlds_to_profile([Path(raw)], pname)
                count += len(w_imp)
            if self.imp_cb_mods.isChecked() and info.get("mod_paths"):
                m_imp, _ = import_mods_to_profile([Path(p) for p in info["mod_paths"]], pname)
                count += len(m_imp)
            for flag, sub in ((self.imp_cb_rp.isChecked(), "resourcepacks"), (self.imp_cb_sp.isChecked(), "shaderpacks"), (self.imp_cb_cfg.isChecked(), "config")):
                if flag and (src_root / sub).is_dir():
                    _copy_merge_tree(src_root / sub, base_dst / sub)
                    count += 1
            self._sync_launcher_after_profile_change(pname)
            QMessageBox.information(self, "Merged", f"Merged {count} item(s)/folder(s) cleanly into profile '{pname}'.")
            return

        pname = self.import_name_edit.text().strip() or (self._last_analysis or {}).get("suggested_name") or Path(raw).name
        config = load_launcher_config()
        if pname in (config.get("profiles") or {}):
            reply = QMessageBox.question(
                self, "Profile Exists",
                f"Profile '{pname}' already exists. Update/merge into it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        link_from = self.import_link_saves_combo.currentData() or None
        mode = "organize" if self.import_mode_organize.isChecked() else "in_place"
        try:
            res = import_profile_from_directory(
                Path(raw),
                pname,
                mode=mode,
                share_versions=self.import_share_vers_cb.isChecked(),
                link_saves_from=link_from,
                auto_sort_loose=self.import_autosort_cb.isChecked(),
                set_active=True,
                include_mods=self.imp_cb_mods.isChecked(),
                include_worlds=self.imp_cb_worlds.isChecked(),
                include_resourcepacks=self.imp_cb_rp.isChecked(),
                include_shaderpacks=self.imp_cb_sp.isChecked(),
                include_config=self.imp_cb_cfg.isChecked(),
                include_versions=self.imp_cb_vers.isChecked(),
            )
            self._sync_launcher_after_profile_change(pname)
            an = res.get("analysis") or {}
            QMessageBox.information(
                self,
                "Import Complete",
                f"Profile '{pname}' is ready!\n\n"
                f"Detected loader: {an.get('detected_loader', 'none')}\n"
                f"Mods: {an.get('mods_count', 0)} | Worlds: {an.get('worlds_count', 0)}\n"
                f"Directory: {res['entry'].get('base')}",
            )
        except Exception as e:
            QMessageBox.critical(self, "Import Failed", str(e))

    # --- Directories & Links Tab Actions ---
    def _save_dirs_and_links(self):
        pname = self._selected_profile_name()
        if not pname:
            return
        config = load_launcher_config()
        profiles = config.get("profiles") or {}
        cur = dict(profiles.get(pname) or {})

        for k, inp in self.adv_dir_inputs.items():
            val = inp.text().strip().replace("\\", "/")
            if val:
                cur[k] = val
            else:
                cur.pop(k, None)

        base_str = str(cur.get("base") or "minecraft_downloads").rstrip("/\\")
        for folder, (mode_cb, target_cb, custom_edit, _b, _s) in self.link_controls.items():
            lmode = mode_cb.currentData() or "isolated"
            cur[f"{folder}_link_mode"] = lmode
            if lmode == "profile" and target_cb.currentData():
                cur[f"{folder}_link_profile"] = target_cb.currentData()
            else:
                cur.pop(f"{folder}_link_profile", None)
            if lmode == "custom" and custom_edit.text().strip():
                cur[folder] = custom_edit.text().strip().replace("\\", "/")
            elif lmode == "isolated" and folder == "saves" and not custom_edit.text().strip():
                cur[folder] = f"{base_str}/saves"

        ram_val = int(self.adv_ram_spin.value())
        if ram_val > 0:
            cur["ram_mb"] = ram_val
        else:
            cur.pop("ram_mb", None)
        jvm_str = self.adv_jvm_edit.text().strip()
        if jvm_str:
            cur["jvm_args"] = jvm_str
        else:
            cur.pop("jvm_args", None)

        profiles[pname] = cur
        config["profiles"] = profiles
        save_launcher_config(config)
        ensure_profile_links(pname)
        self._sync_launcher_after_profile_change(pname)
        QMessageBox.information(self, "Profile Saved", f"Saved directories and links for '{pname}'.")

    def _autofill_adv_subdirs(self):
        base = self.adv_dir_inputs["base"].text().strip().replace("\\", "/").rstrip("/")
        if not base:
            return
        iso = self.isolate_vers_cb.isChecked()
        for k, inp in self.adv_dir_inputs.items():
            if k == "base":
                continue
            if k == "versions" and not iso:
                inp.setText("minecraft_downloads/versions")
            elif k in ("assets", "libraries", "indexes", "java", "source") and base != "minecraft_downloads":
                inp.setText(f"minecraft_downloads/{k}")
            else:
                inp.setText(f"{base}/{k}")

    def _copy_config_file_from_profile(self, filename: str):
        pname = self._selected_profile_name()
        if not pname:
            return
        config = load_launcher_config()
        others = [k for k in (config.get("profiles") or {}).keys() if k != pname]
        if not others:
            QMessageBox.information(self, "No Other Profile", "Create another profile first to copy from.")
            return
        src_p, ok = QInputDialog.getItem(self, f"Copy {filename}", f"Copy {filename} from profile:", others, 0, False)
        if not ok or not src_p:
            return
        src_f = get_dir("base", profile_name=src_p) / filename
        if not src_f.is_file():
            QMessageBox.warning(self, "Not Found", f"'{src_p}' does not have {filename}.")
            return
        import shutil
        dst_f = get_dir("base", profile_name=pname) / filename
        dst_f.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src_f), str(dst_f))
        QMessageBox.information(self, "Copied", f"Copied {filename} from '{src_p}' to '{pname}'.")

    def _open_profile_folder(self, kind: str):
        pname = self._selected_profile_name()
        if not pname:
            return
        d = get_dir(kind, profile_name=pname)
        d.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(str(d))
            elif sys.platform == "darwin":
                import subprocess
                subprocess.Popen(["open", str(d)])
            else:
                import subprocess
                subprocess.Popen(["xdg-open", str(d)])
        except Exception as e:
            QMessageBox.warning(self, "Open Folder Failed", str(e))

    def open_add_profile_dialog(self):
        dlg = AddProfileDialog(self.launcher_gui, self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.created_profile_name:
            self._sync_launcher_after_profile_change(dlg.created_profile_name)

    def duplicate_selected_profile(self):
        src = self._selected_profile_name()
        if not src:
            return
        text, ok = QInputDialog.getText(self, "Duplicate Profile", "New profile name:", text=f"{src} Copy")
        if not ok or not text.strip():
            return
        ok_dup, msg = duplicate_profile_in_config(src, text.strip())
        if not ok_dup:
            QMessageBox.warning(self, "Duplicate Failed", msg)
            return
        self._sync_launcher_after_profile_change(text.strip())

    def rename_selected_profile(self):
        old_n = self._selected_profile_name()
        if not old_n:
            return
        text, ok = QInputDialog.getText(self, "Rename Profile", "New profile name:", text=old_n)
        if not ok or not text.strip() or text.strip() == old_n:
            return
        ok_r, msg = rename_profile_in_config(old_n, text.strip())
        if not ok_r:
            QMessageBox.warning(self, "Rename Failed", msg)
            return
        self._sync_launcher_after_profile_change(text.strip())

    def delete_selected_profile(self):
        pname = self._selected_profile_name()
        config = load_launcher_config()
        profiles = config.get("profiles") or {}
        if len(profiles) <= 1:
            QMessageBox.warning(self, "Cannot Delete", "Cannot delete the last remaining profile.")
            return
        reply = QMessageBox.question(
            self, "Delete Profile",
            f"Remove profile '{pname}' from launcher?\n(Files on disk in its directory will NOT be deleted.)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        profiles.pop(pname, None)
        if config.get("active_profile") == pname:
            config["active_profile"] = next(iter(profiles.keys()))
        config["profiles"] = profiles
        save_launcher_config(config)
        self._sync_launcher_after_profile_change(config["active_profile"])

    def activate_selected_profile(self):
        pname = self._selected_profile_name()
        if not pname:
            return
        config = load_launcher_config()
        config["active_profile"] = pname
        save_launcher_config(config)
        ensure_profile_links(pname)
        self._sync_launcher_after_profile_change(pname)
        if hasattr(self.launcher_gui, "main_tabs"):
            self.launcher_gui.main_tabs.setCurrentIndex(0)

    def _sync_launcher_after_profile_change(self, select_name: str):
        self.launcher_gui.reload_profiles_combo()
        self.launcher_gui.load_versions()
        self.launcher_gui.setup_quick_play()
        self.launcher_gui._apply_profile_preferences(select_name)
        self.launcher_gui.refresh_profile_cards()
        self.refresh_profiles(select_name)


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
        self.dev_mode_cb = QCheckBox("Source Mode (--source: unpack readable assets && source)")
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
        note_lbl = QLabel("Manage Delete All Versions & Source Code under Profiles && Directories.")
        note_lbl.setWordWrap(True)
        note_lbl.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(note_lbl)

        ok_btn = QPushButton("Save")
        ok_btn.clicked.connect(self.accept)
        btn_layout.addStretch()
        btn_layout.addWidget(ok_btn)
        layout.addLayout(btn_layout)

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
        self.web_view.setGeometry(0, 0, 350, 520)
        _configure_skin_webview(self.web_view)
        
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
            bg_hex = get_theme_window_bg_hex()
            try:
                self.web_view.page().setBackgroundColor(QColor(bg_hex))
            except Exception:
                pass
            base_url = QUrl.fromLocalFile(str(Path(f".cache/{get_active_profile_id()}/skin_viewer.html").absolute()))
            query = QUrlQuery()
            if s_path: query.addQueryItem("skin", s_path)
            if c_path: query.addQueryItem("cape", c_path)
            query.addQueryItem("model", model)
            query.addQueryItem("back", back_eq)
            query.addQueryItem("offset_x", "150")
            query.addQueryItem("bg", bg_hex)
            base_url.setQuery(query)
            
            self.web_view.load(base_url)
            self.preview_loaded = True
            # Wait for page to actually load before running JS
            self.web_view.loadFinished.connect(self._on_preview_loaded)
            
    def _on_preview_loaded(self, ok):
        if ok:
            try:
                self.web_view.page().runJavaScript("window.__syncViewerSize && window.__syncViewerSize();")
            except RuntimeError:
                pass
            self.change_animation()
            self.toggle_spinning(self.spin_cb.isChecked())

    def showEvent(self, event):
        super().showEvent(event)
        if getattr(self, "web_view", None) and self.web_view.page():
            _safe_widget_delay(
                self,
                40,
                lambda: self.web_view.page().runJavaScript("window.__syncViewerSize && window.__syncViewerSize();")
                if getattr(self, "web_view", None) and self.web_view.page()
                else None,
            )

    def done(self, result):
        if getattr(self, "web_view", None):
            try:
                page = self.web_view.page()
                self.web_view.setPage(None)
                if page:
                    page.deleteLater()
            except Exception:
                pass
        super().done(result)
        
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
        self._paused = False

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

    def _center_waypoint(self, update_status: bool = True):
        """Center the waypoint dot on the Locator Bar when tracking stops or pauses."""
        self.dot_x = self.width() // 2 if self.width() > 0 else -1
        self.mouse_y = self.height() // 2
        self.pitch_dir = "none"
        self.distance_blocks = self.scroll_distance_offset
        self.distance_pct = min(100, int(round((self.distance_blocks / 332) * 100)))
        if update_status:
            self._update_tooltip_and_status()
        self.update()

    def set_paused(self, paused: bool):
        paused = bool(paused)
        self._paused = paused
        rt = getattr(self, "_resume_timer", None)
        if rt is not None:
            try:
                rt.stop()
            except RuntimeError:
                pass
        if paused:
            if getattr(self, "track_timer", None):
                self.track_timer.stop()
            if getattr(self, "anim_timer", None):
                self.anim_timer.stop()
            self._center_waypoint(update_status=True)
        else:
            def _ensure_started():
                try:
                    if not getattr(self, "_paused", False) and self.isVisible():
                        if getattr(self, "track_timer", None) and not self.track_timer.isActive():
                            self.track_timer.start(16)
                        if getattr(self, "anim_timer", None) and not self.anim_timer.isActive():
                            self.anim_timer.start(50)
                        self._track_mouse(force_update=False)
                except RuntimeError:
                    pass
            try:
                vis = self.isVisible()
            except RuntimeError:
                return
            if vis:
                _ensure_started()
            else:
                self._resume_timer = _safe_widget_delay(self, 80, _ensure_started)

    def pause(self):
        self.set_paused(True)

    def resume(self):
        self.set_paused(False)

    def showEvent(self, event):
        super().showEvent(event)
        if not getattr(self, "_paused", False):
            if getattr(self, "track_timer", None) and not self.track_timer.isActive():
                self.track_timer.start(16)
            if getattr(self, "anim_timer", None) and not self.anim_timer.isActive():
                self.anim_timer.start(50)
            self._track_mouse(force_update=False)

    def hideEvent(self, event):
        if getattr(self, "track_timer", None):
            self.track_timer.stop()
        if getattr(self, "anim_timer", None):
            self.anim_timer.stop()
        self._center_waypoint(update_status=False)
        super().hideEvent(event)

    def resizeEvent(self, event):
        if getattr(self, "_paused", False) or self.dot_x < 0:
            self.dot_x = self.width() // 2
        super().resizeEvent(event)

    def _track_mouse(self, force_update: bool = False):
        if getattr(self, "_paused", False) or not self.isVisible():
            return
        global_pos = QCursor.pos()
        win = self.window()
        in_window = False
        if win and win.isVisible() and win.isActiveWindow():
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

class PersonalisationDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Personalisation")
        self.setMinimumSize(340, 290)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        self.config = load_launcher_config()
        self.options = load_personalisation_options(self.config)

        theme_row = QHBoxLayout()
        theme_label = QLabel("Theme:")
        theme_label.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        self.theme_combo = QComboBox()
        self.theme_combo.addItem("Auto (Follow System)", "system")
        self.theme_combo.addItem("Light Mode", "light")
        self.theme_combo.addItem("Dark Mode", "dark")
        current_theme = self.options.get("theme", "system")
        t_idx = self.theme_combo.findData(current_theme)
        if t_idx >= 0:
            self.theme_combo.setCurrentIndex(t_idx)
        theme_row.addWidget(theme_label)
        theme_row.addWidget(self.theme_combo, stretch=1)
        layout.addLayout(theme_row)

        self.checkboxes = {}

        options_map = [
            ("show_3d_skin", "Show 3D Skin Render"),
            ("show_change_skin_btn", "Show Change Skin/Cape Button"),
            ("show_java_profile", "Show Java Profile Info"),
            ("show_xbox_profile", "Show Xbox Profile Info"),
            ("show_locator_bar", "Show Locator Bar"),
        ]

        for key, label in options_map:
            cb = QCheckBox(label)
            cb.setChecked(bool(self.options.get(key, True)))
            layout.addWidget(cb)
            self.checkboxes[key] = cb

        layout.addStretch(1)

        btn_layout = QHBoxLayout()
        save_btn = QPushButton("Save")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self.save_and_close)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)

        btn_layout.addWidget(save_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

    def save_and_close(self):
        self.options["theme"] = self.theme_combo.currentData() or "system"
        for key, cb in self.checkboxes.items():
            self.options[key] = cb.isChecked()

        save_personalisation_options(self.options, self.config)
        self.accept()


PlayerDataOptionsDialog = PersonalisationDialog


class ProfileBannerWidget(QWidget):
    def __init__(self, auth_data, parent=None, progress_callback=None):
        super().__init__(parent)
        self.auth_data = auth_data

        self.config = load_launcher_config()
        self.options = load_personalisation_options(self.config)
        self.gs_label = None

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 5)
        main_layout.setSpacing(5)

        top_layout = QHBoxLayout()

        # 1. Fetch textures and generate HTML
        textures = fetch_player_textures(get_active_profile_id(), auth_data.get('mc_uuid'), auth_data.get('xbox_gamerpic_url'), progress_callback)
        self._textures = textures

        # 2. Left: 3D Skin Renderer and Change Skin button
        self.left_widget = QWidget()
        left_layout = QVBoxLayout(self.left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)

        self.web_view = QWebEngineView()
        self.web_view.setFixedSize(200, 180)
        self.custom_page, _ = _configure_skin_webview(self.web_view)

        self.html_url = textures.get('html_url')
        self._skin_loaded = False
        self._skin_retry_done = False
        self._game_paused = False
        if self.html_url:
            self.web_view.loadFinished.connect(self._on_load_finished)
            if self.options.get("show_3d_skin", True):
                self._load_skin_viewer()
        self.web_view.setVisible(self.options.get("show_3d_skin", True))
        left_layout.addWidget(self.web_view)

        left_layout.addSpacing(0)  # Fixed missing button
        
        self.change_skin_btn = QPushButton("Change Skin/Cape")
        self.change_skin_btn.setFixedWidth(150)
        self.change_skin_btn.clicked.connect(self.open_change_skin_dialog)
        self.change_skin_btn.setVisible(self.options.get("show_change_skin_btn", True))
        left_layout.addWidget(self.change_skin_btn, alignment=Qt.AlignmentFlag.AlignHCenter)

        top_layout.addWidget(self.left_widget)

        # 3. Center: Minecraft Username & Personalisation
        self.center_widget = QWidget()
        center_layout = QVBoxLayout(self.center_widget)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)

        self.options_btn = QPushButton("Personalisation")
        self.options_btn.setFixedSize(135, 26)
        self.options_btn.clicked.connect(self.open_personalisation)
        
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
            
            self.gs_label = QLabel(gamerscore_val)
            self.gs_label.setFont(QFont("Segoe UI", 10))
            gs_row.addWidget(self.gs_label)
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
        show_loc = self.options.get("show_locator_bar", True)
        self.locator_bar.setVisible(show_loc)
        if not show_loc:
            self.locator_bar.pause()
        main_layout.addWidget(self.locator_bar)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.apply_theme(get_effective_is_dark())

    def _load_skin_viewer(self):
        if getattr(self, "_game_paused", False) or not self.html_url or not getattr(self, "web_view", None):
            return
        from PyQt6.QtCore import QUrlQuery
        bg_hex = get_theme_window_bg_hex()
        try:
            self.web_view.page().setBackgroundColor(QColor(bg_hex))
        except Exception:
            pass
        base_url = QUrl.fromLocalFile(self.html_url)
        query = QUrlQuery()
        textures = getattr(self, "_textures", {}) or {}
        if textures.get("skin"):
            query.addQueryItem("skin", textures["skin"])
        if textures.get("cape"):
            query.addQueryItem("cape", textures["cape"])
        query.addQueryItem("bg", bg_hex)
        base_url.setQuery(query)
        self._skin_loaded = True
        self.web_view.load(base_url)

    def apply_theme(self, is_dark: bool):
        secondary = "#b0b0b0" if is_dark else "#666666"
        presence_color = "#c0c0c0" if is_dark else "#555555"
        if getattr(self, "waypoint_status_label", None):
            self.waypoint_status_label.setStyleSheet(f"color: {secondary};")
        if getattr(self, "gs_label", None):
            self.gs_label.setStyleSheet(f"color: {secondary};")
        if getattr(self, "status_label_text", None):
            self.status_label_text.setStyleSheet(f"color: {presence_color};")

        bg_hex = get_theme_window_bg_hex(is_dark)
        if getattr(self, "web_view", None) and self.web_view.page():
            try:
                self.web_view.page().setBackgroundColor(QColor(bg_hex))
            except Exception:
                pass
            if getattr(self, "_skin_loaded", False) and not getattr(self, "_game_paused", False):
                try:
                    self.web_view.page().runJavaScript(
                        f"window.setViewerBackground && window.setViewerBackground('{bg_hex}');"
                        "window.__syncViewerSize && window.__syncViewerSize();"
                    )
                except RuntimeError:
                    pass

    def showEvent(self, event):
        super().showEvent(event)
        if (
            not getattr(self, "_game_paused", False)
            and getattr(self, "web_view", None)
            and self.web_view.isVisible()
            and self.web_view.page()
        ):
            _safe_widget_delay(
                self,
                60,
                lambda: (
                    self.web_view.page().runJavaScript("window.__syncViewerSize && window.__syncViewerSize();")
                    if not getattr(self, "_game_paused", False) and getattr(self, "web_view", None) and self.web_view.page()
                    else None
                ),
            )

    def minimumSizeHint(self):
        return self.sizeHint()

    def sizeHint(self):
        top_h = 0
        show_skin = self.options.get("show_3d_skin", True)
        show_btn = self.options.get("show_change_skin_btn", True)
        if show_skin:
            top_h = max(top_h, 180 + (34 if show_btn else 0))
        elif show_btn:
            top_h = max(top_h, 34)

        if self.options.get("show_java_profile", True):
            top_h = max(top_h, 110)
        else:
            top_h = max(top_h, 35)

        if self.options.get("show_xbox_profile", True):
            top_h = max(top_h, 65)

        total_h = top_h
        if self.options.get("show_locator_bar", True):
            total_h += 54 + 5

        total_h += 16  # top (10) and bottom (5) layout margins + 1px buffer

        left_w = 200 if show_skin else (155 if show_btn else 0)
        right_w = 0
        if hasattr(self, "right_widget") and self.right_widget and self.options.get("show_xbox_profile", True):
            right_w = max(180, self.right_widget.sizeHint().width())
        text_w = 220
        if hasattr(self, 'waypoint_status_label') and self.waypoint_status_label and self.waypoint_status_label.isVisible():
            txt = self.waypoint_status_label.text()
            fm = self.waypoint_status_label.fontMetrics()
            tw = fm.horizontalAdvance(txt) if hasattr(fm, 'horizontalAdvance') else fm.width(txt)
            text_w = max(text_w, tw + 16)
        # Keep inside compact launcher frame while accommodating full waypoint text + Xbox card
        w = max(610, min(700, left_w + text_w + right_w + 32))
        return QtCore.QSize(w, total_h)

    def open_personalisation(self):
        dialog = PersonalisationDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.config = load_launcher_config()
            self.options = load_personalisation_options(self.config)
            win = self.window()
            if hasattr(win, "apply_theme"):
                win.apply_theme()
            else:
                self.apply_theme(get_effective_is_dark())
            self.update_visibility()

    def open_player_data_options(self):
        self.open_personalisation()

    def update_visibility(self):
        paused = getattr(self, "_game_paused", False)
        show_skin = self.options.get("show_3d_skin", True)
        want_skin_visible = show_skin and not paused
        if want_skin_visible and not getattr(self, "_skin_loaded", False):
            self._load_skin_viewer()
        if self.web_view.isVisible() != want_skin_visible:
            self.web_view.setVisible(want_skin_visible)
        if want_skin_visible and self.web_view.page():
            _safe_widget_delay(
                self,
                50,
                lambda: (
                    self.web_view.page().runJavaScript("window.__syncViewerSize && window.__syncViewerSize();")
                    if not getattr(self, "_game_paused", False) and getattr(self, "web_view", None) and self.web_view.page()
                    else None
                ),
            )

        if hasattr(self, 'skin_spacer'):
            from PyQt6.QtWidgets import QSizePolicy
            self.skin_spacer.changeSize(1, -30 if show_skin else 0, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
            self.left_widget.layout().invalidate()

        self.change_skin_btn.setVisible(self.options.get("show_change_skin_btn", True))
        self.java_info_widget.setVisible(self.options.get("show_java_profile", True))
        self.right_widget.setVisible(self.options.get("show_xbox_profile", True))
        show_loc = self.options.get("show_locator_bar", True)
        self.locator_bar.setVisible(show_loc)
        if hasattr(self.locator_bar, "set_paused"):
            self.locator_bar.set_paused(paused or not show_loc)
        if hasattr(self, 'waypoint_status_label') and self.waypoint_status_label:
            self.waypoint_status_label.setVisible(show_loc)

        window = self.window()
        if hasattr(window, 'update_window_size'):
            window.update_window_size()

    def pause_for_game(self):
        """Stop the 3D skin renderer process and Locator Bar timers while gaming/fullscreen."""
        if getattr(self, "_game_paused", False):
            return
        self._game_paused = True
        seq = getattr(self, "_lifecycle_seq", 0) + 1
        self._lifecycle_seq = seq
        if getattr(self, "locator_bar", None) and hasattr(self.locator_bar, "pause"):
            self.locator_bar.pause()
        if getattr(self, "web_view", None):
            page = None
            try:
                page = self.web_view.page()
                if page:
                    page.runJavaScript("window.pauseViewer && window.pauseViewer();")
            except Exception:
                pass
            try:
                # Hide view before discarding so Qt WebEngine permits LifecycleState.Discarded
                self.web_view.setVisible(False)
                if page and hasattr(page, "setVisible"):
                    page.setVisible(False)
            except Exception:
                pass
            try:
                if page and hasattr(QWebEnginePage, "LifecycleState") and hasattr(page, "setLifecycleState"):
                    page.setLifecycleState(QWebEnginePage.LifecycleState.Discarded)
                    if page.lifecycleState() != QWebEnginePage.LifecycleState.Discarded:
                        page.setLifecycleState(QWebEnginePage.LifecycleState.Frozen)
                        _safe_widget_delay(
                            self,
                            0,
                            lambda p=page, s=self, expected_seq=seq: (
                                p.setLifecycleState(QWebEnginePage.LifecycleState.Discarded)
                                if getattr(s, "_game_paused", False) and getattr(s, "_lifecycle_seq", 0) == expected_seq and p
                                else None
                            ),
                        )
            except Exception:
                pass

    def resume_from_game(self):
        """Restore the 3D skin viewer and Locator Bar timers when returning from a game."""
        if not getattr(self, "_game_paused", False):
            return
        self._game_paused = False
        seq = getattr(self, "_lifecycle_seq", 0) + 1
        self._lifecycle_seq = seq
        show_loc = self.options.get("show_locator_bar", True)
        if getattr(self, "locator_bar", None) and hasattr(self.locator_bar, "set_paused"):
            self.locator_bar.setVisible(show_loc)
            self.locator_bar.set_paused(not show_loc)
            if show_loc:
                _safe_widget_delay(
                    self,
                    80,
                    lambda s=self, expected_seq=seq: (
                        s.locator_bar.set_paused(False)
                        if not getattr(s, "_game_paused", False)
                        and getattr(s, "_lifecycle_seq", 0) == expected_seq
                        and getattr(s, "locator_bar", None)
                        and s.options.get("show_locator_bar", True)
                        else None
                    ),
                )
        show_skin = self.options.get("show_3d_skin", True)
        if getattr(self, "web_view", None):
            try:
                self.web_view.setVisible(show_skin)
                page = self.web_view.page()
                if page and hasattr(page, "setVisible"):
                    page.setVisible(show_skin)
                if page and hasattr(QWebEnginePage, "LifecycleState") and hasattr(page, "setLifecycleState"):
                    page.setLifecycleState(QWebEnginePage.LifecycleState.Active)
            except Exception:
                pass
            if show_skin and self.html_url:
                self._skin_loaded = False
                self._skin_retry_done = False
                _safe_widget_delay(
                    self,
                    0,
                    lambda s=self, expected_seq=seq: (
                        s._load_skin_viewer()
                        if not getattr(s, "_game_paused", False) and getattr(s, "_lifecycle_seq", 0) == expected_seq
                        else None
                    ),
                )

    def cleanup(self):
        if getattr(self, "_cleaned_up", False):
            return
        self._cleaned_up = True
        if getattr(self, "locator_bar", None) and hasattr(self.locator_bar, "pause"):
            try:
                self.locator_bar.pause()
            except Exception:
                pass
        if hasattr(self, 'web_view') and self.web_view:
            try:
                page = self.web_view.page()
                if page:
                    page.runJavaScript("window.pauseViewer && window.pauseViewer();")
                self.web_view.setPage(None)
                if page:
                    page.deleteLater()
            except Exception:
                pass

    def _on_load_finished(self, ok):
        bg_hex = get_theme_window_bg_hex()
        if getattr(self, "_game_paused", False):
            return
        if not ok:
            if not getattr(self, "_skin_retry_done", False) and self.options.get("show_3d_skin", True):
                self._skin_retry_done = True
                _safe_widget_delay(self, 250, self._load_skin_viewer)
            return

        if getattr(self, "web_view", None) and self.web_view.page():
            config = load_launcher_config()
            anim = config.get("main_animation", "idle")
            spin = config.get("main_spin", True)

            js = get_animation_js(anim)
            spin_val = 'true' if spin else 'false'
            sync_js = f"""
            (function() {{
                if (window.resumeViewer) window.resumeViewer();
                if (window.setViewerBackground) window.setViewerBackground('{bg_hex}');
                if (window.__syncViewerSize) window.__syncViewerSize();
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
            self.web_view.page().runJavaScript(sync_js)
            if js:
                self.web_view.page().runJavaScript(js)

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
        
        border_col = "#3c3c3c" if get_effective_is_dark() else "#ccc"
        self.list_widget = QListWidget()
        self.list_widget.setStyleSheet(f"QListWidget::item {{ padding: 10px; border-bottom: 1px solid {border_col}; }} QListWidget::item:selected {{ background-color: #0078D7; color: white; }}")
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
                
            import urllib.request
            p_cache = ROOT_DIR / ".cache" / pid
            pic_path = p_cache / "gamerpic.png"
            url = pdata.get("xbox_gamerpic_url") or pdata.get("xbox_gamerpic")
            if url and not pic_path.exists():
                try:
                    p_cache.mkdir(parents=True, exist_ok=True)
                    urllib.request.urlretrieve(url, str(pic_path))
                except Exception:
                    pass
            if pic_path.exists():
                item.setIcon(QIcon(str(pic_path)))
                
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
    def __init__(self, linked_accounts: List[Dict], parent=None, header_text: Optional[str] = None):
        super().__init__(parent)
        self.setWindowTitle("Microsoft Accounts Linked with Windows")
        self.setFixedSize(440, 360)

        self.linked_accounts = linked_accounts or []
        self.selected_account: Optional[Dict] = None
        self.use_other_method: bool = False

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        header_lbl = QLabel(
            header_text or (
                "The following Microsoft account(s) are currently linked with Windows.\n"
                "Would you like to log in with one of these accounts?"
            )
        )
        header_lbl.setWordWrap(True)
        header_lbl.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        layout.addWidget(header_lbl)

        border_col = "#3c3c3c" if get_effective_is_dark() else "#ccc"
        self.list_widget = QListWidget()
        self.list_widget.setStyleSheet(
            f"QListWidget::item {{ padding: 10px; border-bottom: 1px solid {border_col}; }} "
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


class _ThreadStdout:
    """Line-buffered stdout/stderr redirector that collapses CR frames cleanly for worker threads."""
    def __init__(self, callback):
        self.callback = callback
        self.buf = ""

    def write(self, text):
        if not text:
            return
        self.buf += text.replace("\r\n", "\n")
        while "\n" in self.buf:
            chunk, self.buf = self.buf.split("\n", 1)
            if "\r" in chunk:
                chunk = chunk.rsplit("\r", 1)[-1]
            chunk = chunk.strip()
            if chunk:
                self.callback(chunk)

    def flush(self):
        rem = self.buf.rsplit("\r", 1)[-1].strip() if self.buf else ""
        if rem:
            self.callback(rem)
        self.buf = ""


def _append_plain_log_line(text_edit: QTextEdit, line: str):
    """Append a raw log line safely without Qt HTML tag interpretation."""
    if text_edit is None:
        return
    cursor = text_edit.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.End)
    text_edit.setTextCursor(cursor)
    text_edit.insertPlainText(f"{line}\n")


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

    def _is_version_files_complete(self) -> bool:
        """Verify locally that the client JAR, Java runtime, asset index, and libraries exist on disk."""
        try:
            from utils import get_version_json, is_allowed_by_rules
            v_json = get_version_json(self.version, resolve_inheritance=True)
            if not isinstance(v_json, dict):
                return False

            # 1. Game JAR (handles inherited parent vanilla JAR & custom modded JAR)
            v_dir = get_dir("versions")
            jar_ver = v_json.get("_jar_version_id") or v_json.get("jar") or self.version
            jar_ok = any(
                (p.is_file() and p.stat().st_size > 0)
                for p in (
                    v_dir / self.version / f"{self.version}.jar",
                    v_dir / f"{jar_ver}-client.jar",
                    v_dir / f"{jar_ver}.jar",
                    v_dir / str(jar_ver) / f"{jar_ver}.jar",
                )
            )
            if not jar_ok:
                return False

            # 2. Java runtime
            jv = v_json.get("javaVersion") or {}
            comp = jv.get("component")
            maj = jv.get("majorVersion")
            if comp and maj:
                j_home = get_dir("java") / str(comp)
                j_exec = download_java.get_java_executable_in_home(j_home)
                if not j_exec.exists() and not download_java.find_existing_java_runtime(str(comp), int(maj)):
                    return False

            # 3. Asset index
            a_idx = v_json.get("assetIndex") or {}
            if a_idx.get("id"):
                idx_file = get_dir("assets") / "indexes" / f"{a_idx['id']}.json"
                if not idx_file.is_file():
                    return False

            # 4. Required libraries (vanilla + modded, including processor-generated local Forge/NeoForge JARs)
            from utils import resolve_library_file
            libs_dir = get_dir("libraries")
            for lib in (v_json.get("libraries") or []):
                if not isinstance(lib, dict) or not is_allowed_by_rules(lib.get("rules", [])):
                    continue
                dl = lib.get("downloads") or {}
                art = dl.get("artifact")
                if isinstance(art, dict) and art.get("path"):
                    if not (libs_dir / art["path"]).is_file():
                        resolved_art = resolve_library_file(art["path"], libraries_dir=libs_dir)
                        if not resolved_art.is_file() and art.get("url"):
                            return False
                elif lib.get("name") and lib.get("url"):
                    rel_p = download_mod_loaders._maven_rel_path(lib["name"])
                    if rel_p and not resolve_library_file(rel_p, libraries_dir=libs_dir).is_file():
                        return False
                cls_map = dl.get("classifiers")
                if isinstance(cls_map, dict) and cls_map:
                    import platform as _plat
                    sn = _plat.system().lower()
                    os_k = "osx" if sn == "darwin" else ("windows" if sn == "windows" else "linux")
                    nat = lib.get("natives")
                    if isinstance(nat, dict) and nat.get(os_k):
                        k = nat[os_k].replace("${arch}", "64" if sys.maxsize > 2**32 else "32")
                        entry = cls_map.get(k) or cls_map.get(f"{k}-arm64")
                        if isinstance(entry, dict) and entry.get("path") and entry.get("url"):
                            if not (libs_dir / entry["path"]).is_file():
                                return False
            return True
        except Exception:
            return False

    def run(self):
        try:
            is_downloaded = self.version in get_downloaded_versions()
            can_skip = is_downloaded and (self.skip_verification or self.is_offline) and not self.dev_mode
            if can_skip and not self.is_offline:
                can_skip = self._is_version_files_complete()
            need_downloads = not can_skip

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
            _append_plain_log_line(self.text_edit, formatted)

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
        _append_plain_log_line(self.text_edit, formatted_line)
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
                _append_plain_log_line(self.text_edit, formatted)
        elif hasattr(self.launcher, 'results_text') and self.launcher.results_text.toPlainText():
            self.text_edit.setPlainText(self.launcher.results_text.toPlainText())
        self._scroll_to_bottom()

    def append_line(self, formatted: str):
        _append_plain_log_line(self.text_edit, formatted)
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


class _ModLoaderMCWorker(QtCore.QThread):
    loaded = QtCore.pyqtSignal(int, str, list, str)

    def __init__(self, req_id: int, loader_id: str, include_snapshots: bool, refresh: bool, parent=None):
        super().__init__(parent)
        self.req_id = req_id
        self.loader_id = loader_id
        self.include_snapshots = include_snapshots
        self.refresh = refresh

    def run(self):
        try:
            items = download_mod_loaders.get_minecraft_versions_for_loader(
                self.loader_id,
                include_snapshots=self.include_snapshots,
                refresh=self.refresh,
            )
            self.loaded.emit(self.req_id, self.loader_id, items or [], "")
        except Exception as e:
            self.loaded.emit(self.req_id, self.loader_id, [], str(e))


class _ModLoaderBuildsWorker(QtCore.QThread):
    loaded = QtCore.pyqtSignal(int, str, str, list, str)

    def __init__(self, req_id: int, loader_id: str, mc_version: str, refresh: bool, parent=None):
        super().__init__(parent)
        self.req_id = req_id
        self.loader_id = loader_id
        self.mc_version = mc_version
        self.refresh = refresh

    def run(self):
        try:
            items = download_mod_loaders.get_loader_versions(
                self.loader_id,
                self.mc_version,
                refresh=self.refresh,
            )
            self.loaded.emit(self.req_id, self.loader_id, self.mc_version, items or [], "")
        except Exception as e:
            self.loaded.emit(self.req_id, self.loader_id, self.mc_version, [], str(e))


_DYNAMIC_LATEST_OPTIONS = [
    ("Latest Minecraft (Release)", "latest-release"),
    ("Latest Minecraft (Snapshot)", "latest-snapshot"),
    ("Latest Fabric (Latest Compatible MC)", "latest-loader:fabric"),
    ("Latest Forge (Latest Compatible MC)", "latest-loader:forge"),
    ("Latest NeoForge (Latest Compatible MC)", "latest-loader:neoforge"),
    ("Latest Quilt (Latest Compatible MC)", "latest-loader:quilt"),
    ("Latest Legacy Fabric", "latest-loader:legacy_fabric"),
    ("Latest Ornithe", "latest-loader:ornithe"),
    ("Latest Babric", "latest-loader:babric"),
]


def resolve_dynamic_version_target(
    target_id: str,
    profile_name: Optional[str] = None,
    available_versions: Optional[list] = None,
) -> Dict:
    """
    Resolve a dynamic target ID ('latest-release', 'latest-snapshot', or 'latest-loader:<id>[:<mc>]')
    to concrete Minecraft/loader metadata and whether it is already downloaded for profile_name.
    """
    downloaded = set(get_downloaded_versions(profile_name=profile_name))
    avail = list(available_versions) if available_versions is not None else list(get_available_versions() or [])

    if target_id in ("latest", "latest-release"):
        mc_ver = None
        for v_id, v_type, _d in avail:
            if v_type == "release":
                mc_ver = v_id
                break
        if not mc_ver:
            for v_id in sorted(downloaded, reverse=True):
                lid, _m, _b = download_mod_loaders.detect_version_loader(v_id, None)
                if lid == "none":
                    mc_ver = v_id
                    break
        return {
            "kind": "vanilla",
            "loader_id": "none",
            "mc_version": mc_ver,
            "version_id": mc_ver,
            "build_entry": None,
            "already_downloaded": bool(mc_ver and mc_ver in downloaded),
        }

    if target_id == "latest-snapshot":
        mc_ver = None
        for v_id, v_type, _d in avail:
            if v_type in ("snapshot", "release"):
                mc_ver = v_id
                break
        if not mc_ver and downloaded:
            mc_ver = sorted(downloaded, reverse=True)[0]
        return {
            "kind": "vanilla",
            "loader_id": "none",
            "mc_version": mc_ver,
            "version_id": mc_ver,
            "build_entry": None,
            "already_downloaded": bool(mc_ver and mc_ver in downloaded),
        }

    if isinstance(target_id, str) and target_id.startswith("latest-loader:"):
        parts = target_id.split(":", 2)
        loader_id = parts[1].strip().lower() if len(parts) > 1 else "fabric"
        explicit_mc = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None

        mc_ver = explicit_mc
        if not mc_ver:
            try:
                compat_mcs = download_mod_loaders.get_minecraft_versions_for_loader(loader_id, include_snapshots=False, refresh=False) or []
                if compat_mcs:
                    mc_ver = str(compat_mcs[0].get("id") or "")
            except Exception:
                mc_ver = None
        # Check already-downloaded matching versions in offline/cache situations
        installed_matches = []
        for dl_v in sorted(downloaded, reverse=True):
            dl_lid, dl_mc, _b = download_mod_loaders.detect_version_loader(dl_v, None)
            if dl_lid == loader_id and (not explicit_mc or dl_mc == explicit_mc):
                installed_matches.append((dl_v, dl_mc))

        if not mc_ver and installed_matches:
            mc_ver = installed_matches[0][1]

        build_entry = None
        expected_vid = None
        if mc_ver:
            try:
                builds = download_mod_loaders.get_loader_versions(loader_id, mc_ver, refresh=False) or []
                if builds:
                    build_entry = dict(builds[0])
                    expected_vid = build_entry.get("full_id") or None
            except Exception:
                pass

        # Match against locally installed builds for (loader_id, mc_ver)
        matched_local = None
        for dl_v, dl_mc in installed_matches:
            if expected_vid and dl_v == expected_vid:
                matched_local = dl_v
                break
            if build_entry and build_entry.get("version") and str(build_entry["version"]) in dl_v and dl_mc == mc_ver:
                matched_local = dl_v
                break
        if not matched_local and not build_entry and installed_matches:
            matched_local = installed_matches[0][0]

        ver_id = matched_local or expected_vid
        return {
            "kind": "loader",
            "loader_id": loader_id,
            "mc_version": mc_ver,
            "version_id": ver_id,
            "build_entry": build_entry,
            "already_downloaded": bool(matched_local),
        }

    return {
        "kind": "exact",
        "loader_id": "none",
        "mc_version": target_id,
        "version_id": target_id,
        "build_entry": None,
        "already_downloaded": bool(target_id and target_id in downloaded),
    }


class _ModLoaderInstallWorker(QtCore.QThread):
    progress = QtCore.pyqtSignal(int, str)
    finished_install = QtCore.pyqtSignal(bool, str, str)

    def __init__(
        self,
        loader_id: str,
        mc_version: str,
        version_entry: dict,
        local_file: Optional[str] = None,
        agent_args: Optional[str] = None,
        profile_name: Optional[str] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.loader_id = loader_id
        self.mc_version = mc_version
        self.version_entry = version_entry
        self.local_file = local_file
        self.agent_args = agent_args
        self.profile_name = profile_name

    def run(self):
        try:
            def _cb(pct, msg):
                self.progress.emit(max(0, min(100, int(round(pct)))), str(msg))

            with profile_context(self.profile_name):
                ok, ver_id, msg = download_mod_loaders.install_mod_loader(
                    self.loader_id,
                    self.mc_version,
                    self.version_entry,
                    local_file=self.local_file,
                    agent_args=self.agent_args,
                    progress_callback=_cb,
                    json_mode=False,
                )
            self.finished_install.emit(bool(ok), str(ver_id or ""), str(msg or ""))
        except Exception as e:
            self.finished_install.emit(False, "", str(e))


def resolve_dynamic_loader_build(loader_id: str, mc_ver: str, build_entry: Optional[dict]) -> Optional[dict]:
    """
    Resolve synthetic dynamic build entries ('__latest_recommended__', '__latest_beta__', '__latest__')
    against download_mod_loaders.get_loader_versions(loader_id, mc_ver).
    """
    if not isinstance(build_entry, dict):
        return None
    bver = str(build_entry.get("version") or "")
    if not bver.startswith("__latest"):
        return dict(build_entry)
    builds = download_mod_loaders.get_loader_versions(loader_id, mc_ver, refresh=False) or []
    if not builds:
        return None
    if bver == "__latest_recommended__":
        for b in builds:
            tag = str(b.get("tag") or "").lower()
            if "recommended" in tag or "stable" in tag:
                return dict(b)
        return dict(builds[0])
    if bver == "__latest_beta__":
        for b in builds:
            tag = str(b.get("tag") or "").lower()
            vstr = str(b.get("version") or "").lower()
            if any(k in tag or k in vstr for k in ("beta", "alpha", "pre", "rc", "experimental", "unstable")):
                return dict(b)
        return dict(builds[0])
    return dict(builds[0])


class _BatchInstallWorker(QtCore.QThread):
    """
    Sequentially installs 1 or many queued items (Vanilla MC versions or Mod Loader builds),
    each inside `with profile_context(item["profile"]):` and ALWAYS installs Mod Loader (if modded)
    + Java + Game Client JAR + Libraries + Assets (+ Source if dev_mode), driving both bottom progress bars.
    """
    step_progress = QtCore.pyqtSignal(int, int, str, float, str)  # cur_step, total_steps, step_name, task_pct, status_str
    progress = QtCore.pyqtSignal(int, str, str)                    # overall_pct, current_item_id, status_msg
    log_sig = QtCore.pyqtSignal(str)
    item_finished = QtCore.pyqtSignal(str, bool, str, str)         # item_id, ok, installed_ver_id, msg
    all_finished = QtCore.pyqtSignal(int, int, str, str)           # ok_count, fail_count, launch_profile, launch_ver_id

    def __init__(self, items: List[Dict], dev_mode: bool = False, parent=None):
        super().__init__(parent)
        self.items = [dict(x) for x in (items or [])]
        self.dev_mode = bool(dev_mode)
        self._cancelled = False

    def cancel(self):
        self._cancelled = True
        try:
            download_mod_loaders.cancel_active_installs()
        except Exception:
            pass

    def run(self):
        total_items = max(1, len(self.items))
        ok_count = 0
        fail_count = 0
        launch_profile = ""
        launch_ver = ""

        # Count total sub-steps across items for accurate bottom step bar
        item_step_counts = []
        for item in self.items:
            lid = str(item.get("loader_id") or "none").lower()
            base_s = 4  # Java, Client, Libraries, Assets
            if lid != "none":
                base_s += 1  # Mod Loader
            if self.dev_mode:
                base_s += 2  # Source resources + decompile
            item_step_counts.append(base_s)
        total_steps = max(1, sum(item_step_counts))
        global_step_idx = 0

        for idx, item in enumerate(self.items):
            if self._cancelled:
                break
            qid = str(item.get("id") or "")
            pname = str(item.get("profile") or "Default")
            loader_id = str(item.get("loader_id") or "none").lower()
            raw_mc = str(item.get("mc_version") or "")
            build_entry = item.get("build_entry") if isinstance(item.get("build_entry"), dict) else None
            local_file = item.get("local_file")
            agent_args = item.get("agent_args")
            item_mode = str(item.get("mode") or "install").lower()
            item_steps_n = item_step_counts[idx]

            def _emit_step_prog(s_idx: int, s_name: str, task_pct: float, status_msg: str):
                pct_clamped = max(0.0, min(100.0, float(task_pct)))
                overall = int(max(0, min(100, round((((s_idx - 1) + (pct_clamped / 100.0)) / total_steps) * 100.0))))
                self.step_progress.emit(s_idx, total_steps, s_name, pct_clamped, status_msg)
                self.progress.emit(overall, qid, status_msg)

            def _run_json_step(s_idx: int, s_name: str, fn):
                def _on_line(line: str):
                    line = line.strip()
                    if not line:
                        return
                    try:
                        obj = json.loads(line)
                        if isinstance(obj, dict) and obj.get("type") == "progress":
                            _emit_step_prog(s_idx, s_name, float(obj.get("progress", 0)), str(obj.get("status") or s_name))
                            return
                    except Exception:
                        pass
                    self.log_sig.emit(line)

                orig_out, orig_err = sys.stdout, sys.stderr
                try:
                    redir = _ThreadStdout(_on_line)
                    sys.stdout = redir
                    sys.stderr = redir
                    return fn()
                finally:
                    try:
                        redir.flush()
                    except Exception:
                        pass
                    sys.stdout = orig_out
                    sys.stderr = orig_err

            try:
                with profile_context(pname):
                    if loader_id != "none" and raw_mc in ("latest", "latest-release", "latest-snapshot"):
                        compat = download_mod_loaders.get_minecraft_versions_for_loader(
                            loader_id, include_snapshots=(raw_mc == "latest-snapshot"), refresh=False
                        ) or []
                        mc_ver = ""
                        if raw_mc == "latest-release":
                            for c in compat:
                                if c.get("type", "release") == "release":
                                    mc_ver = str(c.get("id") or "")
                                    break
                        if not mc_ver and compat:
                            mc_ver = str(compat[0].get("id") or "")
                    elif raw_mc in ("latest", "latest-release", "latest-snapshot") or raw_mc.startswith("latest-loader:"):
                        res = resolve_dynamic_version_target(raw_mc, profile_name=pname)
                        loader_id = res.get("loader_id") or loader_id
                        mc_ver = str(res.get("mc_version") or "")
                        if not build_entry and isinstance(res.get("build_entry"), dict):
                            build_entry = res["build_entry"]
                    else:
                        mc_ver = raw_mc

                    if not mc_ver:
                        raise RuntimeError(f"Could not resolve target Minecraft version for {raw_mc or item.get('label')}.")

                    # Step 1 (if Mod Loader): install mod loader JSON + loader libraries
                    if loader_id != "none":
                        global_step_idx += 1
                        cur_s = global_step_idx
                        resolved_build = resolve_dynamic_loader_build(loader_id, mc_ver, build_entry or {"version": "__latest_recommended__"})
                        if not resolved_build:
                            raise RuntimeError(f"No {loader_id} builds available for Minecraft {mc_ver}.")
                        _emit_step_prog(cur_s, f"Mod Loader ({loader_id})", 5.0, f"[{pname}] Installing {loader_id} for {mc_ver}...")
                        ok_ldr, ver_id, ldr_msg = download_mod_loaders.install_mod_loader(
                            loader_id,
                            mc_ver,
                            resolved_build,
                            local_file=local_file,
                            agent_args=agent_args,
                            progress_callback=lambda p, m, cs=cur_s, pn=pname: _emit_step_prog(cs, "Mod Loader", p, f"[{pn}] {m}"),
                            json_mode=False,
                        )
                        if not ok_ldr:
                            raise RuntimeError(ldr_msg or f"Failed to install {loader_id} {mc_ver}")
                        installed_id = str(ver_id or mc_ver)
                    else:
                        installed_id = mc_ver

                    # Step 2: Java runtime (ALWAYS — for BOTH Vanilla & Modded)
                    global_step_idx += 1
                    cur_s = global_step_idx
                    _emit_step_prog(cur_s, "Java", 5.0, f"[{pname}] Preparing Java for {installed_id}...")
                    res_java = _run_json_step(cur_s, "Java", lambda vid=installed_id: download_java.ensure_java_for_version(vid, json_mode=True))
                    if res_java is False:
                        raise RuntimeError(f"Failed preparing Java runtime for {installed_id}.")
                    _emit_step_prog(cur_s, "Java", 100.0, f"[{pname}] Java ready")

                    # Step 3: Game Client JAR (ALWAYS — for BOTH Vanilla & Modded)
                    global_step_idx += 1
                    cur_s = global_step_idx
                    _emit_step_prog(cur_s, "Game Client", 5.0, f"[{pname}] Downloading client for {installed_id}...")
                    res_mc = _run_json_step(
                        cur_s,
                        "Game Client",
                        lambda vid=installed_id, dm=self.dev_mode: download_minecraft.download_version(
                            vid, jar_type="client", json_mode=True, is_dev_mode=dm, extract_source=False
                        ),
                    )
                    if res_mc is False:
                        raise RuntimeError(f"Failed downloading Minecraft client for {installed_id}.")
                    _emit_step_prog(cur_s, "Game Client", 100.0, f"[{pname}] Game Client ready")

                    # Optional Dev / Source extraction
                    if self.dev_mode:
                        global_step_idx += 1
                        cur_s = global_step_idx
                        _emit_step_prog(cur_s, "JAR Resources", 5.0, f"[{pname}] Extracting resources for {installed_id}...")
                        _run_json_step(cur_s, "JAR Resources", lambda vid=installed_id: extract_source_code.extract_version_source(version_id=vid, jar_type="client", json_output=True, step="resources"))
                        global_step_idx += 1
                        cur_s = global_step_idx
                        _emit_step_prog(cur_s, "Decompiling Classes", 5.0, f"[{pname}] Decompiling source for {installed_id}...")
                        _run_json_step(cur_s, "Decompiling Classes", lambda vid=installed_id: extract_source_code.extract_version_source(version_id=vid, jar_type="client", json_output=True, step="decompile"))

                    # Step 4: Libraries (ALWAYS — downloads missing vanilla + loader libraries)
                    global_step_idx += 1
                    cur_s = global_step_idx
                    _emit_step_prog(cur_s, "Libraries", 5.0, f"[{pname}] Downloading libraries for {installed_id}...")
                    res_libs = _run_json_step(
                        cur_s,
                        "Libraries",
                        lambda vid=installed_id, dm=self.dev_mode: download_libraries.download_libraries(vid, json_mode=True, is_dev_mode=dm),
                    )
                    if res_libs is False:
                        raise RuntimeError(f"Failed downloading libraries for {installed_id}.")
                    _emit_step_prog(cur_s, "Libraries", 100.0, f"[{pname}] Libraries ready")

                    # Step 5: Assets (ALWAYS — downloads assets + index)
                    global_step_idx += 1
                    cur_s = global_step_idx
                    _emit_step_prog(cur_s, "Assets", 5.0, f"[{pname}] Downloading assets for {installed_id}...")
                    res_assets = _run_json_step(
                        cur_s,
                        "Assets",
                        lambda vid=installed_id, dm=self.dev_mode: download_assets.download_assets(vid, json_mode=True, is_dev_mode=dm),
                    )
                    if res_assets is False:
                        raise RuntimeError(f"Failed downloading assets for {installed_id}.")
                    _emit_step_prog(cur_s, "Assets", 100.0, f"[{pname}] Installed {installed_id}")

                ok_count += 1
                if item_mode == "launch" or not launch_ver:
                    launch_profile = pname
                    launch_ver = installed_id
                self.item_finished.emit(qid, True, installed_id, f"Installed {installed_id} (Java, Client, Libraries, Assets)")
            except Exception as e:
                fail_count += 1
                global_step_idx = max(global_step_idx, sum(item_step_counts[: idx + 1]))
                self.log_sig.emit(f"[Error] {e}")
                self.item_finished.emit(qid, False, "", str(e))

        self.all_finished.emit(ok_count, fail_count, launch_profile, launch_ver)


class _SourceExtractWorker(QtCore.QThread):
    finished_extract = QtCore.pyqtSignal(bool, str)

    def __init__(self, version_id: str, profile_name: Optional[str] = None, parent=None):
        super().__init__(parent)
        self.version_id = version_id
        self.profile_name = profile_name

    def run(self):
        try:
            with profile_context(self.profile_name):
                res = extract_source_code.extract_version_source(version_id=self.version_id, jar_type="client", json_output=False, step="all")
            ok = bool(res) if isinstance(res, bool) else True
            self.finished_extract.emit(ok, f"Extracted source code & resources for {self.version_id}.")
        except Exception as e:
            self.finished_extract.emit(False, str(e))


class ModLoadersDialog(QDialog):
    """
    Unified Mod Loaders browser & installer.
    Top grid lists all supported loaders; clicking any loader displays compatible
    Minecraft versions on the left side and loader versions on the right side.
    """

    def __init__(self, parent=None, initial_mc_version: Optional[str] = None):
        super().__init__(parent)
        self.setWindowTitle("Mod Loaders - PyMcLauncher")
        self.resize(900, 640)
        self.setMinimumSize(760, 520)

        self.initial_mc_version = initial_mc_version
        self.installed_version_id: Optional[str] = None
        self.select_on_finish = True

        self._loaders = download_mod_loaders.list_mod_loaders()
        self._loader_map = {item["id"]: item for item in self._loaders}
        self._active_loader_id: Optional[str] = None
        self._all_mc_versions: List[Dict] = []
        self._all_loader_versions: List[Dict] = []
        self._mc_req_seq = 0
        self._build_req_seq = 0
        self._mc_worker: Optional[_ModLoaderMCWorker] = None
        self._build_worker: Optional[_ModLoaderBuildsWorker] = None
        self._install_worker: Optional[_ModLoaderInstallWorker] = None
        self._worker_pool: List[QtCore.QThread] = []

        root = QVBoxLayout(self)
        root.setSpacing(10)

        # 1. Top Mod Loader Selector Grid
        loader_group = QGroupBox("Select Mod Loader")
        loader_vbox = QVBoxLayout(loader_group)
        grid = QGridLayout()
        grid.setSpacing(6)
        self.loader_buttons: Dict[str, QPushButton] = {}
        cols = 5
        for idx, info in enumerate(self._loaders):
            lid = info["id"]
            btn = QPushButton(info["name"])
            btn.setCheckable(True)
            btn.setToolTip(info["description"])
            color = info.get("color", "#888888")
            btn.setStyleSheet(
                f"QPushButton {{ border-left: 4px solid {color}; text-align: left; padding: 6px 10px; }}"
                f"QPushButton:checked {{ font-weight: bold; border-left: 6px solid {color}; }}"
            )
            btn.clicked.connect(lambda checked=False, k=lid: self.select_loader(k))
            self.loader_buttons[lid] = btn
            grid.addWidget(btn, idx // cols, idx % cols)
        loader_vbox.addLayout(grid)

        self.loader_desc_label = QLabel("Click any mod loader above to browse compatible Minecraft versions.")
        self.loader_desc_label.setWordWrap(True)
        loader_vbox.addWidget(self.loader_desc_label)
        root.addWidget(loader_group)

        # 2. Split Pane: Left = Minecraft Versions, Right = Mod Loader Versions
        self.splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left Pane: Minecraft Versions
        left_panel = QGroupBox("1. Minecraft Versions")
        left_layout = QVBoxLayout(left_panel)
        left_controls = QHBoxLayout()
        self.mc_search_edit = QLineEdit()
        self.mc_search_edit.setPlaceholderText("Search Minecraft version...")
        self.mc_search_edit.textChanged.connect(self._filter_mc_versions)
        self.snapshots_cb = QCheckBox("Snapshots / Old")
        self.snapshots_cb.stateChanged.connect(lambda _s: self._reload_mc_versions(refresh=False))
        left_controls.addWidget(self.mc_search_edit, stretch=1)
        left_controls.addWidget(self.snapshots_cb)
        left_layout.addLayout(left_controls)

        self.mc_versions_list = QListWidget()
        self.mc_versions_list.currentItemChanged.connect(self._on_mc_version_selected)
        left_layout.addWidget(self.mc_versions_list, stretch=1)
        self.splitter.addWidget(left_panel)

        # Right Pane: Loader Versions for selected Minecraft version
        right_panel = QGroupBox("2. Mod Loader Versions")
        right_layout = QVBoxLayout(right_panel)
        right_controls = QHBoxLayout()
        self.loader_search_edit = QLineEdit()
        self.loader_search_edit.setPlaceholderText("Filter loader builds...")
        self.loader_search_edit.textChanged.connect(self._filter_loader_versions)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setToolTip("Refresh versions from official loader index")
        self.refresh_btn.clicked.connect(self._on_refresh_clicked)
        right_controls.addWidget(self.loader_search_edit, stretch=1)
        right_controls.addWidget(self.refresh_btn)
        right_layout.addLayout(right_controls)

        self.loader_versions_list = QListWidget()
        self.loader_versions_list.currentItemChanged.connect(lambda _cur, _prev: self._update_buttons())
        self.loader_versions_list.itemDoubleClicked.connect(lambda _item: self._start_install(select_after=True))
        right_layout.addWidget(self.loader_versions_list, stretch=1)

        # Optional Local File / Java Agent Bar (visible for Risugami's ModLoader & Java Agent)
        self.local_file_box = QGroupBox("Custom Mod / Java Agent File")
        lf_layout = QFormLayout(self.local_file_box)
        lf_row = QHBoxLayout()
        self.local_file_edit = QLineEdit()
        self.local_file_edit.setPlaceholderText("Select local .jar or .zip file...")
        self.local_file_edit.textChanged.connect(lambda _t: self._update_buttons())
        self.browse_local_btn = QPushButton("Browse...")
        self.browse_local_btn.clicked.connect(self._browse_local_file)
        lf_row.addWidget(self.local_file_edit, stretch=1)
        lf_row.addWidget(self.browse_local_btn)
        lf_layout.addRow("File:", lf_row)
        self.agent_args_edit = QLineEdit()
        self.agent_args_edit.setPlaceholderText("Optional Java Agent arguments (after '=')")
        lf_layout.addRow("Agent Args:", self.agent_args_edit)
        self.local_file_box.setVisible(False)
        right_layout.addWidget(self.local_file_box)

        self.splitter.addWidget(right_panel)
        self.splitter.setSizes([340, 520])
        root.addWidget(self.splitter, stretch=1)

        # 3. Bottom Progress & Actions
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        root.addWidget(self.progress_bar)

        self.status_label = QLabel("Select a Mod Loader to begin.")
        root.addWidget(self.status_label)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.install_btn = QPushButton("Install")
        self.install_btn.setDefault(True)
        self.install_btn.clicked.connect(lambda: self._start_install(select_after=True))
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.reject)
        btn_row.addWidget(self.install_btn)
        btn_row.addWidget(self.close_btn)
        root.addLayout(btn_row)

        self._update_buttons()
        if self._loaders:
            self.select_loader(self._loaders[0]["id"])

    def _browse_local_file(self):
        fpath, _ = QFileDialog.getOpenFileName(
            self,
            "Select Mod / Java Agent Archive",
            "",
            "Java / Mod Archives (*.jar *.zip);;All Files (*)",
        )
        if fpath:
            self.local_file_edit.setText(fpath)

    def select_loader(self, loader_id: str, refresh: bool = False):
        loader_id = (loader_id or "").lower().strip()
        if loader_id not in self._loader_map:
            return
        self._active_loader_id = loader_id
        for lid, btn in self.loader_buttons.items():
            btn.setChecked(lid == loader_id)

        info = self._loader_map[loader_id]
        self.loader_desc_label.setText(f"{info['name']}: {info['description']}")
        requires_file = bool(info.get("requires_local_file"))
        self.local_file_box.setVisible(requires_file)
        if loader_id == "java_agent":
            self.local_file_box.setTitle("Java Agent (.jar) — Required")
            self.agent_args_edit.setVisible(True)
        elif loader_id == "risugami":
            self.local_file_box.setTitle("Local ModLoader (.zip/.jar) — Optional Override")
            self.agent_args_edit.setVisible(False)

        self.loader_versions_list.clear()
        self._all_loader_versions = []
        self._reload_mc_versions(refresh=refresh)

    def _on_refresh_clicked(self):
        if self._active_loader_id:
            self._reload_mc_versions(refresh=True)

    def _reload_mc_versions(self, refresh: bool = False):
        if not self._active_loader_id:
            return
        self._mc_req_seq += 1
        req_id = self._mc_req_seq
        loader_id = self._active_loader_id
        include_snaps = self.snapshots_cb.isChecked()

        self.mc_versions_list.clear()
        self.mc_versions_list.addItem(f"Loading compatible Minecraft versions for {self._loader_map[loader_id]['name']}...")
        self.mc_versions_list.item(0).setFlags(Qt.ItemFlag.NoItemFlags)
        self.loader_versions_list.clear()
        self.status_label.setText(f"Indexing {self._loader_map[loader_id]['name']} versions...")
        self._update_buttons()

        worker = _ModLoaderMCWorker(req_id, loader_id, include_snaps, refresh, self)
        self._mc_worker = worker
        worker.loaded.connect(self._on_mc_versions_loaded)
        self._track_and_start(worker)

    def _on_mc_versions_loaded(self, req_id: int, loader_id: str, items: list, err: str):
        if req_id != self._mc_req_seq or loader_id != self._active_loader_id:
            return
        self._all_mc_versions = items
        self._filter_mc_versions()
        if err:
            self.status_label.setText(f"Warning loading MC versions: {err}")
        elif items:
            self.status_label.setText(f"Loaded {len(items)} Minecraft versions for {self._loader_map[loader_id]['name']}.")
        else:
            self.status_label.setText(f"No versions returned for {self._loader_map[loader_id]['name']}.")

    def _filter_mc_versions(self):
        query = self.mc_search_edit.text().strip().lower()
        prev_item = self.mc_versions_list.currentItem()
        prev_id = prev_item.data(Qt.ItemDataRole.UserRole) if prev_item else None
        downloaded = set(get_downloaded_versions())

        self.mc_versions_list.blockSignals(True)
        self.mc_versions_list.clear()
        target_row = -1
        preferred = prev_id or self.initial_mc_version
        for entry in self._all_mc_versions:
            mc_id = str(entry.get("id") or "")
            if query and query not in mc_id.lower():
                continue
            v_type = entry.get("type", "release")
            label = mc_id
            if v_type != "release":
                label += f" ({v_type})"
            if mc_id in downloaded:
                label += " [Base Downloaded]"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, mc_id)
            self.mc_versions_list.addItem(item)
            if preferred and mc_id == preferred and target_row < 0:
                target_row = self.mc_versions_list.count() - 1
        self.mc_versions_list.blockSignals(False)

        if self.mc_versions_list.count() > 0:
            if target_row < 0:
                target_row = 0
            self.mc_versions_list.setCurrentRow(target_row)
            self._on_mc_version_selected(self.mc_versions_list.currentItem(), None)
        else:
            empty = QListWidgetItem("No matching Minecraft versions.")
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.mc_versions_list.addItem(empty)
            self.loader_versions_list.clear()
        self._update_buttons()

    def _on_mc_version_selected(self, current, _previous):
        if not current:
            self.loader_versions_list.clear()
            self._update_buttons()
            return
        mc_ver = current.data(Qt.ItemDataRole.UserRole)
        if not mc_ver or not self._active_loader_id:
            self.loader_versions_list.clear()
            self._update_buttons()
            return

        self._build_req_seq += 1
        req_id = self._build_req_seq
        loader_id = self._active_loader_id

        self.loader_versions_list.clear()
        self.loader_versions_list.addItem(f"Loading {self._loader_map[loader_id]['name']} builds for Minecraft {mc_ver}...")
        self.loader_versions_list.item(0).setFlags(Qt.ItemFlag.NoItemFlags)
        self.status_label.setText(f"Fetching {self._loader_map[loader_id]['name']} versions for {mc_ver}...")
        self._update_buttons()

        worker = _ModLoaderBuildsWorker(req_id, loader_id, mc_ver, False, self)
        self._build_worker = worker
        worker.loaded.connect(self._on_loader_versions_loaded)
        self._track_and_start(worker)

    def _on_loader_versions_loaded(self, req_id: int, loader_id: str, mc_version: str, items: list, err: str):
        cur_mc = self.mc_versions_list.currentItem()
        cur_mc_id = cur_mc.data(Qt.ItemDataRole.UserRole) if cur_mc else None
        if req_id != self._build_req_seq or loader_id != self._active_loader_id or mc_version != cur_mc_id:
            return
        self._all_loader_versions = items
        self._filter_loader_versions()
        if err:
            self.status_label.setText(f"Warning: {err}")
        else:
            self.status_label.setText(
                f"Found {len(items)} {self._loader_map[loader_id]['name']} build(s) for Minecraft {mc_version}."
            )

    def _filter_loader_versions(self):
        query = self.loader_search_edit.text().strip().lower()
        self.loader_versions_list.clear()
        for entry in self._all_loader_versions:
            ver_str = str(entry.get("version") or entry.get("full_id") or "")
            tag = str(entry.get("tag") or "")
            display = f"{ver_str}  [{tag}]" if tag else ver_str
            if query and query not in display.lower():
                continue
            item = QListWidgetItem(display)
            item.setData(Qt.ItemDataRole.UserRole, entry)
            self.loader_versions_list.addItem(item)

        if self.loader_versions_list.count() > 0:
            self.loader_versions_list.setCurrentRow(0)
        else:
            empty = QListWidgetItem("No loader builds available for this version.")
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.loader_versions_list.addItem(empty)
        self._update_buttons()

    def _update_buttons(self):
        installing = bool(self._install_worker and self._install_worker.isRunning())
        cur_mc = self.mc_versions_list.currentItem()
        cur_build = self.loader_versions_list.currentItem()
        mc_ver = cur_mc.data(Qt.ItemDataRole.UserRole) if cur_mc else None
        build_entry = cur_build.data(Qt.ItemDataRole.UserRole) if cur_build else None

        can_install = bool(not installing and self._active_loader_id and mc_ver and isinstance(build_entry, dict))
        if can_install and self._active_loader_id == "java_agent":
            lf = self.local_file_edit.text().strip()
            can_install = bool(lf and Path(lf).is_file())

        self.install_btn.setEnabled(can_install)
        self.refresh_btn.setEnabled(not installing and bool(self._active_loader_id))
        self.close_btn.setEnabled(not installing)

    def _start_install(self, select_after: bool = True):
        cur_mc = self.mc_versions_list.currentItem()
        cur_build = self.loader_versions_list.currentItem()
        mc_ver = cur_mc.data(Qt.ItemDataRole.UserRole) if cur_mc else None
        build_entry = cur_build.data(Qt.ItemDataRole.UserRole) if cur_build else None
        if not self._active_loader_id or not mc_ver or not isinstance(build_entry, dict):
            return

        local_file = self.local_file_edit.text().strip() or None
        agent_args = self.agent_args_edit.text().strip() or None
        if self._active_loader_id == "java_agent" and (not local_file or not Path(local_file).is_file()):
            QMessageBox.warning(self, "Java Agent File Required", "Please select a valid Java Agent .jar file first.")
            return

        self.select_on_finish = select_after
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(5)
        self.status_label.setText(f"Installing {self._loader_map[self._active_loader_id]['name']} for Minecraft {mc_ver}...")

        worker = _ModLoaderInstallWorker(
            self._active_loader_id,
            mc_ver,
            build_entry,
            local_file=local_file,
            agent_args=agent_args,
            parent=self,
        )
        self._install_worker = worker
        worker.progress.connect(self._on_install_progress)
        worker.finished_install.connect(self._on_install_finished)
        self._update_buttons()
        self._track_and_start(worker)

    def _track_and_start(self, worker: QtCore.QThread):
        self._worker_pool.append(worker)
        worker.finished.connect(
            lambda w=worker: self._worker_pool.remove(w) if w in self._worker_pool else None
        )
        worker.start()

    def _wait_workers(self):
        for w in list(getattr(self, "_worker_pool", [])):
            try:
                if w.isRunning():
                    w.wait(5000)
            except Exception:
                pass

    def done(self, r: int):
        self._wait_workers()
        super().done(r)

    def closeEvent(self, event):
        self._wait_workers()
        super().closeEvent(event)

    def _on_install_progress(self, pct: int, msg: str):
        self.progress_bar.setValue(pct)
        self.status_label.setText(msg)

    def _on_install_finished(self, ok: bool, ver_id: str, msg: str):
        self._update_buttons()
        if ok:
            self.progress_bar.setValue(100)
            self.status_label.setText(msg)
            self.installed_version_id = ver_id
            QMessageBox.information(self, "Mod Loader Installed", f"{msg}\n\nInstalled version ID:\n{ver_id}")
            if self.select_on_finish:
                self.accept()
        else:
            self.progress_bar.setVisible(False)
            self.status_label.setText(f"Installation failed: {msg}")
            QMessageBox.critical(self, "Installation Failed", msg or "Unknown installation error.")


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
        self._shortcut_launch_pending = bool(getattr(startup_args, "shortcut_launch", False))
        self._pending_banner_refresh = False
        self._pending_auth_profile = None
        self._refresh_profile_id = None
        self._has_been_shown = False
        self._game_perf_paused = False
        self._banner_collapsed = bool(load_launcher_config().get("ui_collapse_banner", False))
        self._left_collapsed = bool(load_launcher_config().get("ui_collapse_left", False))
        self._right_collapsed = bool(load_launcher_config().get("ui_collapse_right", False))
        self._saved_left_w = 235
        self._saved_right_w = 245
        self._perf_poll_timer = None
        self.running_instances = []
        self._next_instance_id = 1
        self._raw_log_lines = []

        # Mod Loaders main-page state
        self._loaders = download_mod_loaders.list_mod_loaders()
        self._loader_map = {item["id"]: item for item in self._loaders}
        self._loader_base_names = {"none": "No Mods (Vanilla)", **{item["id"]: item["name"] for item in self._loaders}}
        self._active_loader_id = "none"
        self._available_versions_cache = []
        self._installed_info = {"by_loader": {}, "by_id": {}}
        self._selected_left_mc = None
        self._all_mc_versions = []
        self._all_loader_versions = []
        self._mc_req_seq = 0
        self._build_req_seq = 0
        self._realms_req_seq = 0
        self._mc_worker = None
        self._build_worker = None
        self._install_worker = None
        self._batch_install_worker = None
        self._realms_thread = None
        self._loader_workers = []
        self._syncing_mc_selection = False
        self._defer_profile_card_refresh = False
        self._profiles_tab_dirty = False
        self._versions_browser_dirty = False
        
        config = load_launcher_config()
        self.show_console = config.get("show_console", False)
        self.show_access_token = config.get("show_access_token", False)
        self.remember_version = config.get("remember_version", False)
        self.last_played_version = config.get("last_played_version", None)
        self.version_filters = config.get("version_filters", ["release"])
        self.show_downloaded_only = config.get("show_downloaded_only", False)
        self.skip_verification = config.get("skip_verification", True)
        self.dev_mode = config.get("dev_mode", False)

        # Removing all custom CSS to let the native Windows 11/10 dll (windowsvista style) 
        # render all the controls (dropdowns, progress bars, buttons) natively.
        
        # Outer Window Layout (Play Tab + Profiles & Directories Tab + bottom update status bar)
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.layout = QVBoxLayout(self.central_widget)
        self.layout.setContentsMargins(16, 14, 16, 14)
        self.layout.setSpacing(8)

        self.main_tabs = QTabWidget()
        self.layout.addWidget(self.main_tabs, stretch=1)

        self.play_tab_widget = QWidget()
        play_tab_layout = QVBoxLayout(self.play_tab_widget)
        play_tab_layout.setContentsMargins(4, 2, 4, 2)
        play_tab_layout.setSpacing(4)

        # Collapsible Top Banner toolbar (3D skin preview, Change Skin/Cape, Xbox/MC info & Locator Bar)
        self.layout_toggle_bar = QWidget()
        lt_h = QHBoxLayout(self.layout_toggle_bar)
        lt_h.setContentsMargins(2, 0, 2, 0)
        lt_h.setSpacing(6)

        self.toggle_banner_btn = QPushButton("▲ Hide Profile && 3D Banner")
        self.toggle_banner_btn.setCheckable(True)
        self.toggle_banner_btn.setChecked(True)
        self.toggle_banner_btn.setToolTip("Collapse or expand the top bar (3D skin model, Change Skin/Cape, Xbox/MC info & Locator Bar)")
        self.toggle_banner_btn.clicked.connect(lambda checked: self.set_banner_collapsed(not checked))

        lt_h.addStretch()
        lt_h.addWidget(self.toggle_banner_btn)
        lt_h.addStretch()
        play_tab_layout.addWidget(self.layout_toggle_bar)

        # --- CENTER PLAY TAB CONTENT ---
        self.center_panel = QWidget()
        self.center_layout = QVBoxLayout(self.center_panel)
        self.center_layout.setContentsMargins(8, 4, 8, 4)
        self.center_layout.setSpacing(6)
        play_tab_layout.addWidget(self.center_panel, stretch=1)

        # Collapsible Top Profile Banner Area (3D model, skin button, Personalisation, MC/Xbox profile & Locator Bar)
        self.top_banner_wrapper = QWidget()
        self.top_banner_wrapper.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.banner_container = QVBoxLayout(self.top_banner_wrapper)
        self.banner_container.setContentsMargins(0, 0, 0, 0)
        self.banner_container.setSpacing(0)
        self.center_layout.addWidget(self.top_banner_wrapper)

        # Profile Preview Cards Strip (clicking any card activates that profile — no dropdown / no Quick Play clutter!)
        self.profile_preview_cards: Dict[str, QFrame] = {}
        self.profile_cards_scroll = QScrollArea()
        self.profile_cards_scroll.setWidgetResizable(True)
        self.profile_cards_scroll.setFixedHeight(48)
        self.profile_cards_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.profile_cards_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.profile_cards_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.profile_cards_container = QWidget()
        self.profile_cards_layout = QHBoxLayout(self.profile_cards_container)
        self.profile_cards_layout.setContentsMargins(0, 0, 0, 0)
        self.profile_cards_layout.setSpacing(6)
        self.profile_cards_scroll.setWidget(self.profile_cards_container)
        self.center_layout.addWidget(self.profile_cards_scroll)

        # Main Controls Toolbar (Options | Directories | Create Shortcut | Login | Switch Account | Logout)
        self.top_layout = QHBoxLayout()
        self.options_btn = QPushButton("Options")
        self.options_btn.clicked.connect(self.open_options_dialog)

        self.dirs_btn = QPushButton("Directories")
        self.dirs_btn.clicked.connect(self.open_directories_dialog)

        self.shortcut_btn = QPushButton("Create Shortcut")
        self.shortcut_btn.setToolTip("Create a Desktop shortcut or pin to Start Menu for a profile, version, or world")
        self.shortcut_btn.clicked.connect(self.open_create_shortcut_dialog)

        self.login_btn = QPushButton("Login")
        self.login_btn.clicked.connect(self.do_login)

        self.switch_account_btn = QPushButton("Switch Account")
        self.switch_account_btn.clicked.connect(self.switch_account)
        self.logout_btn = QPushButton("Logout of all accounts")
        self.logout_btn.clicked.connect(self.logout)

        # Hide logout button if there is no valid auth cache
        import os, json, time
        accounts = load_accounts()
        is_expired = False
        active_id = accounts.get("active_profile")
        if not isinstance(self.auth_data, dict) or not self.auth_data.get("mc_access_token"):
            self.auth_data = get_auth_cache()
        is_logged_in = bool(
            isinstance(self.auth_data, dict)
            and self.auth_data.get("mc_access_token")
        )
        if not is_logged_in and active_id and isinstance((accounts.get("profiles") or {}).get(active_id), dict):
            prof_entry = accounts["profiles"][active_id]
            is_logged_in = bool(prof_entry.get("mc_access_token"))

        self.logout_btn.setVisible(is_logged_in)
        self.switch_account_btn.setVisible(is_logged_in)
        self.login_btn.setVisible(not is_logged_in or is_expired)

        self.top_layout.addWidget(self.options_btn)
        self.top_layout.addWidget(self.dirs_btn)
        self.top_layout.addWidget(self.shortcut_btn)
        self.top_layout.addStretch()
        self.top_layout.addWidget(self.login_btn)
        self.top_layout.addWidget(self.switch_account_btn)
        self.top_layout.addWidget(self.logout_btn)
        self.center_layout.addLayout(self.top_layout)

        # Downloaded Versions Selector Row (placed BELOW Options / Directories / Switch Account / Logout!)
        self.downloaded_ver_layout = QHBoxLayout()
        self.version_label = QLabel("Downloaded Versions:")
        self.version_combo = QComboBox()
        self.version_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.version_combo.setMinimumContentsLength(20)
        self.version_combo.setMinimumWidth(160)
        self.version_combo.currentIndexChanged.connect(self.on_version_changed)
        self.downloaded_ver_layout.addWidget(self.version_label)
        self.downloaded_ver_layout.addWidget(self.version_combo, stretch=1)
        self.center_layout.addLayout(self.downloaded_ver_layout)

        # Install & Launch Queue Strip on Play Tab (view-only + 2 checkboxes + remove; add/edit in Profiles -> Versions)
        self.play_queue_group = QGroupBox("Queued Versions (Select Just Install vs Launch && Install)")
        pq_v = QVBoxLayout(self.play_queue_group)
        pq_v.setContentsMargins(8, 6, 8, 6)
        pq_v.setSpacing(4)

        self.play_install_queue_tree = QTreeWidget()
        self.play_install_queue_tree.setColumnCount(3)
        self.play_install_queue_tree.setHeaderLabels(["Queued Version / Profile", "Just Install", "Launch & Install"])
        self.play_install_queue_tree.setRootIsDecorated(False)
        self.play_install_queue_tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        hdr = self.play_install_queue_tree.header()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.play_install_queue_tree.setMinimumHeight(44)
        self.play_install_queue_tree.setMaximumHeight(68)
        self.play_install_queue_tree.itemChanged.connect(self._on_play_queue_item_changed)
        pq_v.addWidget(self.play_install_queue_tree)

        pq_btns = QHBoxLayout()
        pq_btns.setSpacing(6)
        self.play_run_queue_btn = QPushButton("▶ Run Checked (Install / Launch)")
        self.play_run_queue_btn.setToolTip("Install checked 'Just Install' items on their profiles and launch checked 'Launch & Install' items")
        self.play_run_queue_btn.clicked.connect(self._play_tab_run_queue)
        self.play_remove_queue_btn = QPushButton("Remove")
        self.play_remove_queue_btn.setToolTip("Remove selected item(s) from the queue")
        self.play_remove_queue_btn.clicked.connect(self._play_tab_remove_queued)
        self.play_browse_versions_btn = QPushButton("Manage Queue in Versions...")
        self.play_browse_versions_btn.setToolTip("Open Profiles & Directories -> Versions tab to add or modify queued versions")
        self.play_browse_versions_btn.clicked.connect(self.open_versions_browser_tab)
        pq_btns.addWidget(self.play_run_queue_btn)
        pq_btns.addWidget(self.play_remove_queue_btn)
        pq_btns.addStretch()
        pq_btns.addWidget(self.play_browse_versions_btn)
        pq_v.addLayout(pq_btns)
        self.center_layout.addWidget(self.play_queue_group)

        # Build Versions & Loader Builds panels (housed inside Profiles -> Versions sub-tab; single Install button lives below splitter!)
        self.left_versions_panel = QGroupBox("Minecraft Versions (Multi-Select)")
        self.left_versions_panel.setMinimumWidth(220)
        left_panel_layout = QVBoxLayout(self.left_versions_panel)
        left_panel_layout.setContentsMargins(8, 10, 8, 8)
        left_panel_layout.setSpacing(6)

        self.mc_search_edit = QLineEdit()
        self.mc_search_edit.setPlaceholderText("Search Minecraft version...")
        self.mc_search_edit.textChanged.connect(self._filter_mc_versions_panel)
        left_panel_layout.addWidget(self.mc_search_edit)

        self.mc_versions_list = QListWidget()
        self.mc_versions_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.mc_versions_list.currentItemChanged.connect(self._on_left_mc_version_selected)
        self.mc_versions_list.itemSelectionChanged.connect(lambda: self._update_mod_loader_buttons())
        left_panel_layout.addWidget(self.mc_versions_list, stretch=1)

        self.right_loader_panel = QGroupBox("Mod Loader Versions")
        self.right_loader_panel.setMinimumWidth(220)
        right_panel_layout = QVBoxLayout(self.right_loader_panel)
        right_panel_layout.setContentsMargins(8, 10, 8, 8)
        right_panel_layout.setSpacing(6)

        ldr_ctrl_layout = QHBoxLayout()
        self.loader_search_edit = QLineEdit()
        self.loader_search_edit.setPlaceholderText("Filter loader builds...")
        self.loader_search_edit.textChanged.connect(self._filter_loader_versions_panel)
        self.loader_refresh_btn = QPushButton("Refresh")
        self.loader_refresh_btn.setToolTip("Refresh versions from official loader index")
        self.loader_refresh_btn.clicked.connect(self._on_loader_refresh_clicked)
        ldr_ctrl_layout.addWidget(self.loader_search_edit, stretch=1)
        ldr_ctrl_layout.addWidget(self.loader_refresh_btn)
        right_panel_layout.addLayout(ldr_ctrl_layout)

        self.loader_versions_list = QListWidget()
        self.loader_versions_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.loader_versions_list.currentItemChanged.connect(lambda _c, _p: self._update_mod_loader_buttons())
        self.loader_versions_list.itemSelectionChanged.connect(lambda: self._update_mod_loader_buttons())
        self.loader_versions_list.itemDoubleClicked.connect(lambda _it: self.profiles_tab._install_selected_multi() if hasattr(self, "profiles_tab") else None)
        right_panel_layout.addWidget(self.loader_versions_list, stretch=1)

        self.loader_install_status = QLabel("Select a Mod Loader above to view builds.")
        self.loader_install_status.setWordWrap(True)
        right_panel_layout.addWidget(self.loader_install_status)

        # Load profiles into preview cards & queue
        self.reload_profiles_combo()

        # Bottom Step & Task Progress Bars (added to root layout below tabs!)
        self.step_progress_bar = QProgressBar()
        self.step_progress_bar.setTextVisible(True)
        self.step_progress_bar.setRange(0, 100)
        self.step_progress_bar.setValue(0)
        self.step_progress_bar.setVisible(False)

        self.task_progress_bar = QProgressBar()
        self.task_progress_bar.setTextVisible(True)
        self.task_progress_bar.setRange(0, 100)
        self.task_progress_bar.setValue(0)
        self.task_progress_bar.setFormat("Current Task: %p%")
        self.task_progress_bar.setVisible(False)
        
        self.banner_widget = None
        if self._shortcut_launch_pending:
            self.step_progress_bar.setVisible(True)
            self.step_progress_bar.setRange(0, 0)
            self.step_progress_bar.setFormat("Preparing shortcut launch...")
        elif is_logged_in:
            self.step_progress_bar.setVisible(True)
            self.step_progress_bar.setValue(10)
            self.step_progress_bar.setFormat("Loading profile and textures... (%p%)")
        QTimer.singleShot(0, self.update_profile_banner)

        # Middle Mod Loaders Panel (exclusive option radio buttons: No Mods + 13 loaders)
        self.mod_loaders_group = QGroupBox("Mod Loaders")
        ml_vbox = QVBoxLayout(self.mod_loaders_group)
        ml_vbox.setContentsMargins(8, 8, 8, 6)
        ml_vbox.setSpacing(4)

        self.loader_buttons: Dict[str, QRadioButton] = {}
        self.loader_btn_group = QButtonGroup(self)
        self.loader_btn_group.setExclusive(True)

        ml_grid = QGridLayout()
        ml_grid.setHorizontalSpacing(12)
        ml_grid.setVerticalSpacing(4)
        ml_cols = 4

        self.no_mods_btn = QRadioButton("No Mods (Vanilla) (0)")
        self.no_mods_btn.setChecked(True)
        self.no_mods_btn.setToolTip("Play standard unmodded vanilla Minecraft")
        self.no_mods_btn.clicked.connect(lambda _c=False: self.select_loader("none"))
        self.loader_btn_group.addButton(self.no_mods_btn)
        self.loader_buttons["none"] = self.no_mods_btn
        ml_grid.addWidget(self.no_mods_btn, 0, 0)

        for idx, info in enumerate(self._loaders):
            pos = idx + 1
            lid = info["id"]
            rb = QRadioButton(f"{info['name']} (0)")
            rb.setToolTip(info["description"])
            rb.clicked.connect(lambda _c=False, k=lid: self.select_loader(k))
            self.loader_btn_group.addButton(rb)
            self.loader_buttons[lid] = rb
            ml_grid.addWidget(rb, pos // ml_cols, pos % ml_cols)
        ml_vbox.addLayout(ml_grid)

        self.loader_desc_label = QLabel("No Mods (Vanilla): Standard unmodded Minecraft.")
        self.loader_desc_label.setWordWrap(True)
        ml_vbox.addWidget(self.loader_desc_label)

        self.local_file_box = QGroupBox("Custom Mod / Java Agent File")
        lf_layout = QFormLayout(self.local_file_box)
        lf_row = QHBoxLayout()
        self.local_file_edit = QLineEdit()
        self.local_file_edit.setPlaceholderText("Select local .jar or .zip file...")
        self.local_file_edit.textChanged.connect(lambda _t: self._update_mod_loader_buttons())
        self.browse_local_btn = QPushButton("Browse...")
        self.browse_local_btn.clicked.connect(self._browse_loader_local_file)
        lf_row.addWidget(self.local_file_edit, stretch=1)
        lf_row.addWidget(self.browse_local_btn)
        lf_layout.addRow("File:", lf_row)
        self.agent_args_edit = QLineEdit()
        self.agent_args_edit.setPlaceholderText("Optional Java Agent arguments (after '=')")
        lf_layout.addRow("Agent Args:", self.agent_args_edit)
        self.local_file_box.setVisible(False)
        ml_vbox.addWidget(self.local_file_box)
        # Note: self.mod_loaders_group is placed inside Profiles -> Versions tab!

        # Console Output
        self.results_text = QTextEdit()
        self.results_text.setReadOnly(True)
        self.results_text.setVisible(self.show_console)
        self.log_window = None
        self.pop_log_btn = QPushButton("Pop Out Log")
        self.pop_log_btn.setToolTip("Show the log in its own window")
        self.pop_log_btn.clicked.connect(self.toggle_log_popout)
        self.pop_log_btn.setVisible(self.show_console)
        self.center_layout.addWidget(self.pop_log_btn, alignment=Qt.AlignmentFlag.AlignRight)
        self.center_layout.addWidget(self.results_text, stretch=1000)

        # Status Label
        initial_status = (
            "Preparing shortcut launch..."
            if self._shortcut_launch_pending
            else ("Loading profile and textures..." if is_logged_in else "Ready to launch.")
        )
        self.status_label = QLabel(initial_status)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.status_label.setMinimumWidth(660) # Forces center pane width >= 660
        self.center_layout.addWidget(self.status_label)
        
        self.warning_label = QLabel("Warning: This specific version will not automatically update.")
        self.warning_label.setStyleSheet("color: #cc8800; font-style: italic;")
        self.warning_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.warning_label.setVisible(False)
        self.center_layout.addWidget(self.warning_label)

        # Play Button and Stop Button Layout (Delete button moved to Profiles -> Versions tab)
        self.action_layout = QHBoxLayout()
        self.play_button = QPushButton("PLAY MINECRAFT")
        self.play_button.clicked.connect(self.start_launch_sequence)

        self.stop_button = QPushButton("Stop Game")
        self.stop_button.setStyleSheet("color: #cc0000; font-weight: bold;")
        self.stop_button.clicked.connect(self.stop_running_games)
        self.stop_button.setVisible(False)

        self.setup_quick_play()
        self.action_layout.addWidget(self.play_button, stretch=3)
        self.action_layout.addWidget(self.stop_button, stretch=1)
        self.center_layout.addLayout(self.action_layout)

        # Running Minecraft Instances section (shown when >1 instance is running)
        self.instances_group = QGroupBox("Running Minecraft Instances")
        self.instances_layout = QVBoxLayout(self.instances_group)
        self.instances_layout.setContentsMargins(10, 8, 10, 8)
        self.instances_layout.setSpacing(6)
        self.instances_group.setVisible(False)
        self.center_layout.addWidget(self.instances_group)
        
        # Dummy widget to enforce minimum center pane width of 660
        self.dummy_width_widget = QWidget()
        self.dummy_width_widget.setFixedSize(660, 0)
        self.center_layout.addWidget(self.dummy_width_widget)
        self.center_layout.insertStretch(self.center_layout.indexOf(self.status_label), 1)

        # Add Play & Profiles tabs (Profiles -> Versions tab hosts mod_loaders_group, left_versions_panel & right_loader_panel)
        self.profiles_tab = ProfilesManagerTab(self)
        self.main_tabs.addTab(self.play_tab_widget, "Play")
        self.main_tabs.addTab(self.profiles_tab, "Profiles && Directories")
        self.main_tabs.currentChanged.connect(self._on_main_tab_changed)

        # Put both progress bars at the very bottom of the window (below Play & Profiles tabs!)
        self.layout.addWidget(self.step_progress_bar)
        self.layout.addWidget(self.task_progress_bar)

        # Restore persisted banner collapse preference cleanly
        if self._banner_collapsed:
            self.set_banner_collapsed(True, persist=False, update_size=False)

        self.update_window_size()
        
        # Pipeline State
        self.pipeline_steps = []
        self.current_step = 0
        self.selected_version = "1.21.1"
        
        self.load_versions()
        self.select_loader("none")
        self.refresh_play_install_queue()

        if self._startup_args:
            self.apply_startup_args(self._startup_args)

        # Start a silent background refresh to update skin/presence (never show interactive login popups on startup)
        if is_logged_in and not is_expired:
            self._refresh_profile_id = get_active_profile_id()
            self.refresh_process = QProcess()
            self.refresh_process.setProgram(get_python_cli_executable())
            refresh_head = "--internal-ms-auth" if getattr(sys, "frozen", False) else get_script_path("ms_accounts.py")
            self.refresh_process.setArguments([refresh_head, "--json", "--silent", "--profile", self._refresh_profile_id])
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

        self.apply_theme()

        # Periodically monitor running games / fullscreen apps / window visibility to pause heavy 3D WebGL & timers
        self._perf_poll_timer = QTimer(self)
        self._perf_poll_timer.setInterval(1500)
        self._perf_poll_timer.timeout.connect(self._evaluate_game_performance_mode)
        self._perf_poll_timer.start()

    def _is_launch_or_auth_busy(self) -> bool:
        return bool(
            getattr(self, "is_pipeline_running", False)
            or getattr(self, "is_authenticating", False)
            or getattr(self, "_shortcut_launch_pending", False)
        )

    def _is_external_minecraft_foreground(self) -> bool:
        """Return True if an external Minecraft game window currently has OS foreground focus (Windows)."""
        if os.name != "nt":
            return False
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            fg = user32.GetForegroundWindow()
            if not fg or not user32.IsWindowVisible(fg):
                return False
            fg_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(fg, ctypes.byref(fg_pid))
            pid_val = int(fg_pid.value)
            if pid_val == os.getpid():
                return False
            length = user32.GetWindowTextLengthW(fg)
            if length <= 0 or length > 256:
                return False
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(fg, buf, length + 1)
            title = buf.value.strip().lower()
            if not title or "pymclauncher" in title or not title.startswith("minecraft"):
                return False

            # Match actual Minecraft game window classes / Java/Bedrock binaries rather than browser tabs or folders
            cls_buf = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(fg, cls_buf, 128)
            cls_name = cls_buf.value.strip()
            if cls_name in ("GLFW30", "LWJGL", "SunAwtFrame"):
                return True

            proc_exe = ""
            try:
                h_proc = kernel32.OpenProcess(0x1000, False, pid_val)  # PROCESS_QUERY_LIMITED_INFORMATION
                if h_proc:
                    try:
                        p_buf = ctypes.create_unicode_buffer(1024)
                        p_len = wintypes.DWORD(1024)
                        if kernel32.QueryFullProcessImageNameW(h_proc, 0, p_buf, ctypes.byref(p_len)):
                            proc_exe = os.path.basename(p_buf.value).lower()
                    finally:
                        kernel32.CloseHandle(h_proc)
            except Exception:
                pass
            return proc_exe in ("javaw.exe", "java.exe", "minecraft.windows.exe")
        except Exception:
            return False

    def _evaluate_game_performance_mode(self):
        """Pause heavy 3D skin rendering & locator bar timers while Minecraft or any fullscreen game is active."""
        try:
            has_running_mc = bool(getattr(self, "running_instances", []))
            has_shown = getattr(self, "_has_been_shown", False)
            is_hidden_or_min = bool(self.windowState() & Qt.WindowState.WindowMinimized) or (has_shown and not self.isVisible())
            is_fullscreen_other = False
            is_fg_mc = False

            # Never treat our own active foreground window as covered by an external game/fullscreen app
            is_self_active = bool(self.isActiveWindow()) if hasattr(self, "isActiveWindow") else False
            if has_shown and not has_running_mc and not is_hidden_or_min and not is_self_active:
                if os.name == "nt":
                    is_fg_mc = self._is_external_minecraft_foreground()
                    if not is_fg_mc:
                        is_fullscreen_other = is_fullscreen_app_active(exclude_pid=os.getpid())
                elif sys.platform == "darwin":
                    app = QApplication.instance()
                    if app and app.applicationState() != Qt.ApplicationState.ApplicationActive:
                        if not getattr(self, "_macos_fs_probe_disabled", False):
                            try:
                                is_fullscreen_other = is_fullscreen_app_active(exclude_pid=os.getpid())
                            except Exception:
                                self._macos_fs_probe_disabled = True

            should_pause = has_running_mc or is_hidden_or_min or bool(getattr(self, "_banner_collapsed", False)) or is_fullscreen_other or is_fg_mc
            if should_pause == getattr(self, "_game_perf_paused", False):
                return
            self._game_perf_paused = should_pause
            bw = getattr(self, "banner_widget", None)
            if bw:
                if should_pause and hasattr(bw, "pause_for_game"):
                    bw.pause_for_game()
                elif not should_pause and hasattr(bw, "resume_from_game"):
                    bw.resume_from_game()
            if not should_pause and getattr(self, "_pending_banner_refresh", False) and not self._is_launch_or_auth_busy():
                self._pending_banner_refresh = False
                prev_status = self.status_label.text() if hasattr(self, "status_label") else ""
                self.update_profile_banner()
                if prev_status and prev_status not in ("Loading profile and textures...", "Preparing shortcut launch...", "Profile ready"):
                    self.status_label.setText(prev_status)
        except Exception:
            pass

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.WindowStateChange, QEvent.Type.ActivationChange):
            QTimer.singleShot(0, self._evaluate_game_performance_mode)

    def showEvent(self, event):
        self._has_been_shown = True
        super().showEvent(event)
        QTimer.singleShot(50, self._evaluate_game_performance_mode)

    def hideEvent(self, event):
        super().hideEvent(event)
        QTimer.singleShot(0, self._evaluate_game_performance_mode)

    def apply_theme(self):
        is_dark = apply_launcher_theme()
        if getattr(self, "warning_label", None):
            warn_col = "#ffb347" if is_dark else "#cc8800"
            self.warning_label.setStyleSheet(f"color: {warn_col}; font-style: italic;")
        if getattr(self, "stop_button", None):
            stop_col = "#ff6b6b" if is_dark else "#cc0000"
            self.stop_button.setStyleSheet(f"color: {stop_col}; font-weight: bold;")
        if getattr(self, "banner_widget", None) and hasattr(self.banner_widget, "apply_theme"):
            self.banner_widget.apply_theme(is_dark)
        if getattr(self, "update_controller", None) and hasattr(self.update_controller, "apply_theme"):
            self.update_controller.apply_theme(is_dark)
        if getattr(self, "running_instances", None) and hasattr(self, "update_instances_ui"):
            self.update_instances_ui()
        if hasattr(self, "profile_cards_container"):
            self.refresh_profile_cards()

    # --- Main Page Mod Loaders + Left/Right Version Split Sync ---
    def _track_and_start_loader_worker(self, worker: QtCore.QThread):
        self._loader_workers.append(worker)
        worker.finished.connect(
            lambda w=worker: self._loader_workers.remove(w) if w in self._loader_workers else None
        )
        worker.start()

    def _wait_mod_loader_workers(self):
        for w in list(getattr(self, "_loader_workers", [])):
            try:
                if w.isRunning():
                    w.wait(5000)
            except Exception:
                pass

    def _resolve_base_mc_version(self, ver_id: Optional[str]) -> Optional[str]:
        if not ver_id:
            return None
        try:
            for root_dir in (get_dir("versions"), get_dir("base") / "versions"):
                cand = root_dir / str(ver_id) / f"{ver_id}.json"
                if cand.is_file():
                    data = json.loads(cand.read_text(encoding="utf-8"))
                    if isinstance(data, dict) and data.get("inheritsFrom"):
                        return str(data["inheritsFrom"])
        except Exception:
            pass
        return str(ver_id)

    def _browse_loader_local_file(self):
        fpath, _ = QFileDialog.getOpenFileName(
            self,
            "Select Mod / Java Agent Archive",
            "",
            "Java / Mod Archives (*.jar *.zip);;All Files (*)",
        )
        if fpath:
            self.local_file_edit.setText(fpath)

    def _refresh_installed_info(self, profile_name: Optional[str] = None) -> Dict:
        """Build per-loader installed version index using current downloaded versions."""
        if profile_name is None:
            profile_name = getattr(self, "_browser_profile_name", None)
        with profile_context(profile_name):
            downloaded = set(get_downloaded_versions(profile_name=profile_name))
            by_loader: Dict[str, Dict] = {}
            by_id: Dict[str, tuple] = {}
            search_dirs: List[Path] = []
            for d_kind in ("versions", "base"):
                try:
                    p = get_dir(d_kind, profile_name=profile_name)
                    cand = p if d_kind == "versions" else (p / "versions")
                    if cand.exists() and cand not in search_dirs:
                        search_dirs.append(cand)
                except Exception:
                    pass

            for ver_id in downloaded:
                ver_json = None
                has_json_file = False
                has_jar_file = False
                for sd in search_dirs:
                    jp = sd / ver_id / f"{ver_id}.json"
                    if not jp.is_file():
                        jp = sd / f"{ver_id}.json"
                    if jp.is_file():
                        has_json_file = True
                        try:
                            ver_json = json.loads(jp.read_text(encoding="utf-8", errors="ignore"))
                        except Exception:
                            pass
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
                        has_jar_file = True

                lid, mc_ver, build = download_mod_loaders.detect_version_loader(ver_id, ver_json)
                if lid == "none" and has_json_file and not has_jar_file:
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

        self._installed_info = {"by_loader": by_loader, "by_id": by_id}
        self._update_loader_option_counts()
        return self._installed_info

    def _refresh_browser_for_profile(self, pname: str):
        """Refresh the Versions & Mod Loaders browser highlights & counts for the selected profile in Profiles tab."""
        self._browser_profile_name = pname
        self._refresh_installed_info(profile_name=pname)
        if getattr(self, "_active_loader_id", "none") == "none" and hasattr(self, "mc_versions_list"):
            self._populate_mc_versions_for_none()
        elif hasattr(self, "mc_versions_list") and getattr(self, "_all_mc_versions", None):
            self._filter_mc_versions_panel()
            if hasattr(self, "loader_versions_list") and getattr(self, "_all_loader_versions", None):
                self._filter_loader_versions_panel()

    def _update_loader_option_counts(self):
        if not hasattr(self, "loader_buttons") or not hasattr(self, "_loader_base_names"):
            return
        by_loader = (self._installed_info or {}).get("by_loader") or {}
        for lid, btn in self.loader_buttons.items():
            base = self._loader_base_names.get(lid, lid)
            cnt = len((by_loader.get(lid) or {}).get("entries") or [])
            btn.setText(f"{base} ({cnt})")

    def select_loader(self, loader_id: str, refresh: bool = False):
        loader_id = (loader_id or "none").lower().strip()
        if loader_id != "none" and loader_id not in self._loader_map:
            return
        self._active_loader_id = loader_id
        for lid, btn in self.loader_buttons.items():
            btn.setChecked(lid == loader_id)

        self._refresh_installed_info()

        if loader_id == "none":
            self.loader_desc_label.setText("No Mods (Vanilla): Standard unmodded Minecraft.")
            self.local_file_box.setVisible(False)
            self._all_loader_versions = []
            self.loader_search_edit.setEnabled(False)
            self.loader_refresh_btn.setEnabled(False)
            self.loader_versions_list.clear()
            hint_item = QListWidgetItem("No Mods (Vanilla) selected.\n\nPick a Mod Loader in the middle to install builds.")
            hint_item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.loader_versions_list.addItem(hint_item)
            self.loader_install_status.setText("Vanilla selected (no mod loader needed).")
            self._populate_mc_versions_for_none()
            # If version_combo currently holds a modded version, switch to its base vanilla MC version
            cur_ver = self.version_combo.currentData() if hasattr(self, "version_combo") else None
            base_mc = self._resolve_base_mc_version(cur_ver)
            if base_mc and base_mc != cur_ver:
                self._sync_version_combo_to_mc(base_mc)
            self._update_mod_loader_buttons()
            self.update_ui_state()
            return

        self._selected_left_mc = None
        info = self._loader_map[loader_id]
        self.loader_desc_label.setText(f"{info['name']}: {info['description']}")
        requires_file = bool(info.get("requires_local_file"))
        self.local_file_box.setVisible(requires_file)
        if loader_id == "java_agent":
            self.local_file_box.setTitle("Java Agent (.jar) — Required")
            self.agent_args_edit.setVisible(True)
        elif loader_id == "risugami":
            self.local_file_box.setTitle("Local ModLoader (.zip/.jar) — Optional Override")
            self.agent_args_edit.setVisible(False)

        self.loader_search_edit.setEnabled(True)
        self.loader_refresh_btn.setEnabled(True)
        self.loader_versions_list.clear()
        self._all_loader_versions = []
        self._reload_mc_versions_panel(refresh=refresh)

    def _on_loader_refresh_clicked(self):
        if self._active_loader_id and self._active_loader_id != "none":
            self._reload_mc_versions_panel(refresh=True)

    def _populate_mc_versions_for_none(self):
        avail = list(getattr(self, "_available_versions_cache", []) or [])
        if not avail:
            avail = list(get_available_versions() or [])
            self._available_versions_cache = avail
        installed = self._refresh_installed_info()
        vanilla_downloaded = (installed.get("by_loader") or {}).get("none", {}).get("mc_versions", set())
        entries: List[Dict] = []
        if avail:
            for v_id, v_type, _v_date in avail:
                if v_type not in self.version_filters:
                    continue
                if self.show_downloaded_only and v_id not in vanilla_downloaded:
                    continue
                entries.append({"id": v_id, "type": v_type, "stable": v_type == "release"})
        else:
            for v_id in sorted(vanilla_downloaded, reverse=True):
                entries.append({"id": v_id, "type": "release", "stable": True})
        self._all_mc_versions = entries
        self._filter_mc_versions_panel()

    def _reload_mc_versions_panel(self, refresh: bool = False):
        loader_id = self._active_loader_id
        if not loader_id or loader_id == "none":
            self._populate_mc_versions_for_none()
            return
        self._mc_req_seq += 1
        req_id = self._mc_req_seq
        include_snaps = any(t != "release" for t in self.version_filters)

        self.mc_versions_list.blockSignals(True)
        self.mc_versions_list.clear()
        loading_item = QListWidgetItem(f"Loading Minecraft versions for {self._loader_map[loader_id]['name']}...")
        loading_item.setFlags(Qt.ItemFlag.NoItemFlags)
        self.mc_versions_list.addItem(loading_item)
        self.mc_versions_list.blockSignals(False)

        self.loader_versions_list.clear()
        self.loader_install_status.setText(f"Indexing {self._loader_map[loader_id]['name']} versions...")
        self._update_mod_loader_buttons()

        worker = _ModLoaderMCWorker(req_id, loader_id, include_snaps, refresh, self)
        self._mc_worker = worker
        worker.loaded.connect(self._on_mc_versions_loaded)
        self._track_and_start_loader_worker(worker)

    def _on_mc_versions_loaded(self, req_id: int, loader_id: str, items: list, err: str):
        if req_id != self._mc_req_seq or loader_id != self._active_loader_id:
            return
        self._all_mc_versions = items
        self._filter_mc_versions_panel()
        if err:
            self.loader_install_status.setText(f"Warning loading MC versions: {err}")
        elif items:
            self.loader_install_status.setText(f"Loaded {len(items)} Minecraft version(s) for {self._loader_map[loader_id]['name']}.")
        else:
            self.loader_install_status.setText(f"No compatible MC versions found for {self._loader_map[loader_id]['name']}.")

    def _filter_mc_versions_panel(self):
        query = self.mc_search_edit.text().strip().lower()
        prev_item = self.mc_versions_list.currentItem()
        prev_id = prev_item.data(Qt.ItemDataRole.UserRole) if prev_item else None
        combo_ver = self.version_combo.currentData() if hasattr(self, "version_combo") else None
        preferred = prev_id or self._selected_left_mc or self._resolve_base_mc_version(combo_ver)

        installed = self._installed_info or self._refresh_installed_info()
        loader_installed_mcs = (installed.get("by_loader") or {}).get(self._active_loader_id, {}).get("mc_versions", set())

        # In offline mode or if remote index failed, fall back cleanly to locally installed MC versions for this loader
        source_entries = list(self._all_mc_versions)
        if not source_entries and self._active_loader_id != "none" and loader_installed_mcs:
            source_entries = [{"id": mc, "type": "release", "stable": True} for mc in sorted(loader_installed_mcs, reverse=True)]

        self._syncing_mc_selection = True
        self.mc_versions_list.blockSignals(True)
        self.mc_versions_list.clear()
        target_row = -1
        try:
            # Dynamic Latest channels directly inside Minecraft Versions list
            if not self.show_downloaded_only and source_entries:
                rel_entry = next((e for e in source_entries if e.get("type", "release") == "release"), source_entries[0])
                snap_entry = next((e for e in source_entries if e.get("type", "release") != "release"), source_entries[0])
                dyn_rows = [
                    (f"Latest Release ({rel_entry.get('id')})", "latest-release"),
                    (f"Latest Snapshot ({snap_entry.get('id')})", "latest-snapshot"),
                ]
                for dyn_label, dyn_key in dyn_rows:
                    if query and query not in dyn_label.lower() and query not in dyn_key:
                        continue
                    d_item = QListWidgetItem(dyn_label)
                    d_item.setData(Qt.ItemDataRole.UserRole, dyn_key)
                    self.mc_versions_list.addItem(d_item)
                    if preferred == dyn_key and target_row < 0:
                        target_row = self.mc_versions_list.count() - 1

            first_concrete_row = -1
            for entry in source_entries:
                mc_id = str(entry.get("id") or "")
                if query and query not in mc_id.lower():
                    continue
                is_loader_dl = mc_id in loader_installed_mcs
                if self._active_loader_id != "none" and self.show_downloaded_only and not is_loader_dl:
                    continue
                v_type = entry.get("type", "release")
                label = mc_id
                if v_type != "release":
                    label += f" ({v_type})"
                if is_loader_dl:
                    label += " [Downloaded]"
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, mc_id)
                self.mc_versions_list.addItem(item)
                if first_concrete_row < 0:
                    first_concrete_row = self.mc_versions_list.count() - 1
                if preferred and mc_id == preferred and target_row < 0:
                    target_row = self.mc_versions_list.count() - 1
        finally:
            self.mc_versions_list.blockSignals(False)
            self._syncing_mc_selection = False

        if self.mc_versions_list.count() > 0:
            if target_row < 0:
                target_row = first_concrete_row if first_concrete_row >= 0 else 0
            self.mc_versions_list.blockSignals(True)
            self.mc_versions_list.setCurrentRow(target_row)
            self.mc_versions_list.blockSignals(False)
            if self._active_loader_id != "none":
                self._on_left_mc_version_selected(self.mc_versions_list.currentItem(), None)
        else:
            msg_empty = (
                f"No downloaded {self._loader_base_names.get(self._active_loader_id, 'loader')} versions."
                if self.show_downloaded_only and self._active_loader_id != "none"
                else "No matching Minecraft versions."
            )
            empty = QListWidgetItem(msg_empty)
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.mc_versions_list.addItem(empty)
            if self._active_loader_id != "none":
                self.loader_versions_list.clear()
                r_empty = QListWidgetItem(msg_empty)
                r_empty.setFlags(Qt.ItemFlag.NoItemFlags)
                self.loader_versions_list.addItem(r_empty)
        self._update_mod_loader_buttons()

    def _sync_version_combo_to_mc(self, mc_ver: str):
        if not mc_ver or not hasattr(self, "version_combo"):
            return
        if mc_ver in ("latest-release", "latest-snapshot"):
            target_mc = self._resolve_dynamic_mc_from_list(mc_ver)
            mc_ver = target_mc or mc_ver
        idx = self.version_combo.findData(mc_ver)
        if idx >= 0:
            self._selected_left_mc = None
            if self.version_combo.currentIndex() != idx:
                self._syncing_mc_selection = True
                try:
                    self.version_combo.setCurrentIndex(idx)
                finally:
                    self._syncing_mc_selection = False
                self.on_version_changed(idx)
            else:
                self.update_ui_state()
        else:
            self._selected_left_mc = mc_ver
            if not self._is_launch_or_auth_busy():
                self.status_label.setText(f"Selected Minecraft {mc_ver} (not yet downloaded).")
            self.update_ui_state()

    def _resolve_dynamic_mc_from_list(self, logical_mc: str) -> Optional[str]:
        entries = list(self._all_mc_versions or [])
        if not entries:
            return None
        if logical_mc == "latest-release":
            for e in entries:
                if e.get("type", "release") == "release":
                    return str(e.get("id") or "")
        elif logical_mc == "latest-snapshot":
            for e in entries:
                if e.get("type", "release") != "release":
                    return str(e.get("id") or "")
        return str(entries[0].get("id") or "")

    def _on_left_mc_version_selected(self, current, _previous):
        if self._syncing_mc_selection or not current:
            return
        raw_mc = current.data(Qt.ItemDataRole.UserRole)
        if not raw_mc:
            if self._active_loader_id != "none":
                self.loader_versions_list.clear()
            self._update_mod_loader_buttons()
            return

        if self._active_loader_id == "none":
            self._sync_version_combo_to_mc(str(raw_mc))
            self._update_mod_loader_buttons()
            return

        fetch_mc = (
            self._resolve_dynamic_mc_from_list(str(raw_mc))
            if str(raw_mc) in ("latest-release", "latest-snapshot")
            else str(raw_mc)
        )
        if not fetch_mc:
            self.loader_versions_list.clear()
            self._update_mod_loader_buttons()
            return

        self._resolved_left_mc_for_builds = fetch_mc
        self._build_req_seq += 1
        req_id = self._build_req_seq
        loader_id = self._active_loader_id

        self.loader_versions_list.clear()
        loading_item = QListWidgetItem(f"Loading {self._loader_map[loader_id]['name']} builds for {fetch_mc}...")
        loading_item.setFlags(Qt.ItemFlag.NoItemFlags)
        self.loader_versions_list.addItem(loading_item)
        self.loader_install_status.setText(f"Fetching {self._loader_map[loader_id]['name']} builds for {fetch_mc}...")
        self._update_mod_loader_buttons()

        worker = _ModLoaderBuildsWorker(req_id, loader_id, fetch_mc, False, self)
        self._build_worker = worker
        worker.loaded.connect(self._on_loader_versions_loaded)
        self._track_and_start_loader_worker(worker)

    def _on_loader_versions_loaded(self, req_id: int, loader_id: str, mc_version: str, items: list, err: str):
        cur_mc = self.mc_versions_list.currentItem()
        cur_mc_raw = cur_mc.data(Qt.ItemDataRole.UserRole) if cur_mc else None
        cur_mc_resolved = (
            self._resolve_dynamic_mc_from_list(str(cur_mc_raw))
            if cur_mc_raw in ("latest-release", "latest-snapshot")
            else cur_mc_raw
        )
        if req_id != self._build_req_seq or loader_id != self._active_loader_id or mc_version != cur_mc_resolved:
            return
        self._all_loader_versions = items
        self._filter_loader_versions_panel()
        if err:
            self.loader_install_status.setText(f"Warning: {err}")
        else:
            self.loader_install_status.setText(
                f"Found {len(items)} {self._loader_map[loader_id]['name']} build(s) for {mc_version}."
            )

    def _filter_loader_versions_panel(self):
        if self._active_loader_id == "none":
            return
        query = self.loader_search_edit.text().strip().lower()
        cur_mc = self.mc_versions_list.currentItem()
        cur_mc_raw = cur_mc.data(Qt.ItemDataRole.UserRole) if cur_mc else None
        cur_mc_id = (
            self._resolve_dynamic_mc_from_list(str(cur_mc_raw))
            if cur_mc_raw in ("latest-release", "latest-snapshot")
            else cur_mc_raw
        )
        installed = self._installed_info or self._refresh_installed_info()
        ldr_slot = (installed.get("by_loader") or {}).get(self._active_loader_id, {})
        installed_builds = ldr_slot.get("builds", {}).get(cur_mc_id, set())

        # Merge any installed local builds for (loader, mc_version) that might not appear in online index
        entries_to_show = list(self._all_loader_versions or [])
        known_vers = {str(e.get("version") or "") for e in entries_to_show} | {str(e.get("full_id") or "") for e in entries_to_show}
        for inst_entry in (ldr_slot.get("entries") or []):
            if inst_entry.get("mc_version") != cur_mc_id:
                continue
            b_ver = str(inst_entry.get("build") or inst_entry.get("version_id") or "")
            if b_ver and b_ver not in known_vers and str(inst_entry.get("version_id")) not in known_vers:
                entries_to_show.insert(0, {
                    "version": b_ver,
                    "full_id": inst_entry.get("version_id"),
                    "mc_version": cur_mc_id,
                    "tag": "installed",
                })
                known_vers.add(b_ver)

        self.loader_versions_list.clear()

        # Prepend dynamic Latest Recommended / Latest Beta / Latest Build options right inside Mod Loader Versions
        first_concrete_row = -1
        if not self.show_downloaded_only and self._active_loader_id != "java_agent" and entries_to_show and cur_mc_id:
            rec_entry = next(
                (e for e in entries_to_show if any(k in str(e.get("tag") or "").lower() for k in ("recommended", "stable"))),
                entries_to_show[0],
            )
            beta_entry = next(
                (
                    e for e in entries_to_show
                    if any(k in str(e.get("tag") or "").lower() or k in str(e.get("version") or "").lower() for k in ("beta", "alpha", "pre", "rc", "experimental", "unstable"))
                ),
                None,
            )
            dyn_builds = [
                (
                    f"Latest Recommended ({rec_entry.get('version')})",
                    {"version": "__latest_recommended__", "mc_version": cur_mc_id, "tag": "latest recommended"},
                )
            ]
            if beta_entry:
                dyn_builds.append((
                    f"Latest Beta ({beta_entry.get('version')})",
                    {"version": "__latest_beta__", "mc_version": cur_mc_id, "tag": "latest beta"},
                ))
            dyn_builds.append((
                f"Latest Build ({entries_to_show[0].get('version')})",
                {"version": "__latest__", "mc_version": cur_mc_id, "tag": "latest"},
            ))
            for disp_lbl, d_entry in dyn_builds:
                if query and query not in disp_lbl.lower():
                    continue
                item = QListWidgetItem(disp_lbl)
                item.setData(Qt.ItemDataRole.UserRole, d_entry)
                self.loader_versions_list.addItem(item)

        for entry in entries_to_show:
            ver_str = str(entry.get("version") or entry.get("full_id") or "")
            full_id = str(entry.get("full_id") or "")
            tag = str(entry.get("tag") or "")
            is_build_dl = bool(ver_str in installed_builds or (full_id and full_id in installed_builds))
            if self.show_downloaded_only and not is_build_dl:
                continue
            display = f"{ver_str}  [{tag}]" if tag else ver_str
            if is_build_dl:
                display += " [Downloaded]"
            if query and query not in display.lower():
                continue
            item = QListWidgetItem(display)
            item.setData(Qt.ItemDataRole.UserRole, entry)
            self.loader_versions_list.addItem(item)
            if first_concrete_row < 0:
                first_concrete_row = self.loader_versions_list.count() - 1

        if self.loader_versions_list.count() > 0:
            self.loader_versions_list.setCurrentRow(first_concrete_row if first_concrete_row >= 0 else 0)
        else:
            empty_msg = (
                "No downloaded builds for this Minecraft version."
                if self.show_downloaded_only
                else "No loader builds available for this version."
            )
            empty = QListWidgetItem(empty_msg)
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.loader_versions_list.addItem(empty)
        self._update_mod_loader_buttons()

    def _is_mod_loader_installing(self) -> bool:
        return bool(
            (getattr(self, "_install_worker", None) and self._install_worker.isRunning())
            or (getattr(self, "_batch_install_worker", None) and self._batch_install_worker.isRunning())
        )

    def _update_mod_loader_buttons(self):
        if not hasattr(self, "loader_buttons"):
            return
        installing = self._is_mod_loader_installing()
        busy = self._is_launch_or_auth_busy()
        is_mod = bool(self._active_loader_id and self._active_loader_id != "none")
        cur_mc = self.mc_versions_list.currentItem()
        cur_build = self.loader_versions_list.currentItem()
        mc_ver = cur_mc.data(Qt.ItemDataRole.UserRole) if cur_mc else None
        build_entry = cur_build.data(Qt.ItemDataRole.UserRole) if cur_build else None

        can_install = bool(not installing and not busy and mc_ver and (not is_mod or isinstance(build_entry, dict)))
        if can_install and self._active_loader_id == "java_agent":
            lf = self.local_file_edit.text().strip()
            can_install = bool(lf and Path(lf).is_file())

        self.loader_refresh_btn.setEnabled(not installing and not busy and is_mod)
        for btn in self.loader_buttons.values():
            btn.setEnabled(not installing and not busy)
        if hasattr(self, "profiles_tab"):
            pt = self.profiles_tab
            for b_name in ("install_selected_multi_btn", "queue_selected_multi_btn", "launch_selected_multi_btn"):
                b = getattr(pt, b_name, None)
                if b is not None:
                    b.setEnabled(can_install)

    def _start_loader_install(self):
        if hasattr(self, "profiles_tab"):
            self.profiles_tab._install_selected_multi()

    def _on_loader_install_progress(self, pct: int, msg: str):
        self.loader_install_status.setText(msg)
        if hasattr(self, "task_progress_bar"):
            self.task_progress_bar.setValue(pct)
        if not self._is_launch_or_auth_busy():
            self.status_label.setText(msg)

    def _on_loader_install_finished(self, ok: bool, ver_id: str, msg: str):
        self._update_mod_loader_buttons()
        if ok:
            self.loader_install_status.setText(f"Installed: {ver_id}")
            self.log(f"[ModLoader] {msg}")
            self.load_versions()
            self._refresh_installed_info()
            self._filter_mc_versions_panel()
            self._filter_loader_versions_panel()
            if ver_id:
                idx = self.version_combo.findData(ver_id)
                if idx >= 0:
                    self._syncing_mc_selection = True
                    try:
                        self.version_combo.setCurrentIndex(idx)
                    finally:
                        self._syncing_mc_selection = False
                    self.on_version_changed(idx)
            if not self._is_launch_or_auth_busy():
                self.status_label.setText(f"Ready to launch {ver_id}.")
            if hasattr(self, "profiles_tab"):
                self.profiles_tab.refresh_profiles(self.profiles_tab._selected_profile_name())
            QMessageBox.information(self, "Mod Loader Installed", f"{msg}\n\nSelected version:\n{ver_id}")
        else:
            self.loader_install_status.setText(f"Failed: {msg}")
            self.log(f"[Error] Mod loader install failed: {msg}")
            if not self._is_launch_or_auth_busy():
                self.status_label.setText("Mod loader installation failed.")
            QMessageBox.critical(self, "Installation Failed", msg or "Unknown installation error.")

    def open_mod_loaders_dialog(self):
        self.open_versions_browser_tab()
        if self._active_loader_id == "none" and self._loaders:
            self.select_loader(self._loaders[0]["id"])

    def open_versions_browser_tab(self):
        if hasattr(self, "main_tabs") and hasattr(self, "profiles_tab"):
            self.profiles_tab.refresh_profiles()
            if hasattr(self.profiles_tab, "versions_tab") and hasattr(self.profiles_tab, "profile_subtabs"):
                self.profiles_tab.profile_subtabs.setCurrentWidget(self.profiles_tab.versions_tab)
            self.main_tabs.setCurrentWidget(self.profiles_tab)

    def open_create_shortcut_dialog(self):
        dlg = CreateShortcutDialog(self)
        dlg.exec()

    def apply_startup_args(self, args):
        """Apply CLI arguments passed from a Desktop or Start Menu shortcut and trigger auto-launch if requested."""
        try:
            # 1. Select Profile if specified
            if getattr(args, "profile", None):
                self.set_active_profile(args.profile)

            # 2. Select Version if specified (supports concrete IDs AND latest-release / latest-snapshot / latest-loader:*)
            if getattr(args, "version", None):
                target_ver = str(args.version)
                if target_ver in ("latest", "latest-release", "latest-snapshot"):
                    res = resolve_dynamic_version_target(target_ver, profile_name=self.get_active_profile_name())
                    if res.get("version_id"):
                        target_ver = res["version_id"]

                v_idx = self.version_combo.findData(target_ver)
                if v_idx < 0:
                    is_dl = target_ver in get_downloaded_versions(profile_name=self.get_active_profile_name())
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
                self._shortcut_launch_pending = True
                target_ver_disp = self.version_combo.currentData() or getattr(args, "version", None) or "Minecraft"
                self.status_label.setText(f"Preparing launch for Minecraft {target_ver_disp}...")
                self.step_progress_bar.setVisible(True)
                self.step_progress_bar.setRange(0, 0)
                self.step_progress_bar.setFormat(f"Preparing shortcut launch ({target_ver_disp})...")
                self.setWindowTitle(f"Minecraft - {target_ver_disp} - Preparing...")
                self.log("[Shortcut] Auto-launching from shortcut...")
                QTimer.singleShot(50, self.start_launch_sequence)
        except Exception as e:
            self._shortcut_launch_pending = False
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
            proc.waitForFinished(1500)
        except Exception:
            pass

    def closeEvent(self, event):
        if getattr(self, '_perf_poll_timer', None):
            try:
                self._perf_poll_timer.stop()
            except Exception:
                pass
        if getattr(self, '_pipeline_worker', None) and self._pipeline_worker.isRunning():
            self._pipeline_worker.cancel()
            self._pipeline_worker.wait(1000)
        bw_worker = getattr(self, '_batch_install_worker', None)
        if bw_worker is not None:
            try:
                if bw_worker.isRunning():
                    bw_worker.cancel()
                    bw_worker.wait(4000)
            except Exception:
                pass
        rt = getattr(self, '_realms_thread', None)
        if rt is not None:
            try:
                if rt.isRunning():
                    rt.wait(1500)
            except Exception:
                pass
        try:
            download_mod_loaders.cancel_active_installs()
        except Exception:
            pass
        self._wait_mod_loader_workers()
        self._cleanup_browser()
        bw = getattr(self, 'banner_widget', None)
        if bw and hasattr(bw, 'cleanup'):
            try:
                bw.cleanup()
            except Exception:
                pass
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
        refresh_proc = getattr(self, 'refresh_process', None)
        if refresh_proc and refresh_proc.state() != QProcess.ProcessState.NotRunning:
            try:
                refresh_proc.kill()
                refresh_proc.waitForFinished(1500)
            except Exception:
                pass
        event.accept()

    def update_ui_state(self):
        """Enable/disable buttons and update labels accurately based on current launcher & game state."""
        is_busy = self._is_launch_or_auth_busy()
        running_list = getattr(self, 'running_instances', [])
        started_instances = [inst for inst in running_list if inst.get('started')]
        running_count = len(started_instances)

        idx = self.version_combo.currentIndex() if hasattr(self, 'version_combo') else -1
        combo_ver = self.version_combo.currentData() if idx >= 0 else None
        pending_left = getattr(self, "_selected_left_mc", None)
        selected_ver = combo_ver or pending_left
        has_versions = bool(selected_ver is not None)
        is_selected_downloaded = bool(combo_ver is not None and self.version_combo.itemData(idx, Qt.ItemDataRole.UserRole + 1)) if idx >= 0 else False
        is_selected_ver_running = any(inst.get('version') == selected_ver for inst in running_list)

        # Play / Start Another Instance Button
        if hasattr(self, 'play_button'):
            self.play_button.setEnabled(not is_busy and has_versions)
            if running_count > 0:
                self.play_button.setText("START ANOTHER INSTANCE")
            elif getattr(self, 'is_offline', False):
                self.play_button.setText("PLAY MINECRAFT OFFLINE")
            elif pending_left:
                self.play_button.setText(f"PLAY MINECRAFT ({pending_left})")
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

        # Dropdowns & Top Bar Buttons
        if hasattr(self, 'version_combo'):
            self.version_combo.setEnabled(not is_busy and has_versions)
        if hasattr(self, 'quick_play_combo') and self.quick_play_combo:
            self.quick_play_combo.setEnabled(not is_busy and has_versions)
        if hasattr(self, 'options_btn'):
            self.options_btn.setEnabled(not is_busy)
        if hasattr(self, 'dirs_btn'):
            self.dirs_btn.setEnabled(not is_busy)
        if hasattr(self, 'play_install_queue_tree'):
            t = self.play_install_queue_tree
            q_has_items = t.topLevelItemCount() > 0 and t.topLevelItem(0).data(0, Qt.ItemDataRole.UserRole) is not None
            if hasattr(self, 'play_run_queue_btn'):
                self.play_run_queue_btn.setEnabled(not is_busy and not self._is_mod_loader_installing() and q_has_items)
            if hasattr(self, 'play_remove_queue_btn'):
                self.play_remove_queue_btn.setEnabled(not is_busy and not self._is_mod_loader_installing() and q_has_items)
        self._update_mod_loader_buttons()
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
                stop_col = "#ff6b6b" if get_effective_is_dark() else "#cc0000"
                stop_btn.setStyleSheet(f"color: {stop_col}; font-weight: bold;")
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
            target_pid = getattr(self, "_refresh_profile_id", None)
            for line in data.splitlines():
                line = line.strip()
                if not line: continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict) and "mc_username" in obj and obj.get("mc_access_token"):
                        if target_pid:
                            saved_pid = save_auth_cache_for_profile(target_pid, obj)
                        else:
                            save_auth_cache(obj)
                            saved_pid = None
                        cur_active = (load_accounts() or {}).get("active_profile")
                        if not target_pid or cur_active in (target_pid, saved_pid):
                            self.auth_data = obj
                            if self._is_launch_or_auth_busy() or getattr(self, "_game_perf_paused", False):
                                self._pending_banner_refresh = True
                            else:
                                # Remove the old banner and recreate it so the updated skin/presence show
                                self.update_profile_banner()
                except Exception:
                    pass
        except Exception:
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
        if not getattr(self, "_syncing_mc_selection", False):
            self._selected_left_mc = None
        if hasattr(self, 'warning_label'):
            self.warning_label.setVisible(self.remember_version)
        if (
            not getattr(self, "_syncing_mc_selection", False)
            and getattr(self, "_active_loader_id", "none") == "none"
            and hasattr(self, "mc_versions_list")
        ):
            sel_ver = self.version_combo.itemData(index)
            if sel_ver:
                self._syncing_mc_selection = True
                try:
                    for r in range(self.mc_versions_list.count()):
                        it = self.mc_versions_list.item(r)
                        if it and it.data(Qt.ItemDataRole.UserRole) == sel_ver:
                            self.mc_versions_list.setCurrentRow(r)
                            break
                finally:
                    self._syncing_mc_selection = False
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
            is_logged_in = bool(isinstance(self.auth_data, dict) and self.auth_data.get("mc_access_token"))
            
            self.logout_btn.setVisible(is_logged_in)
            self.switch_account_btn.setVisible(is_logged_in)
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
            is_logged_in = bool(isinstance(self.auth_data, dict) and self.auth_data.get("mc_access_token"))
            
            self.logout_btn.setVisible(is_logged_in)
            self.switch_account_btn.setVisible(is_logged_in)
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

    def _prompt_windows_linked_accounts(self, header_text: Optional[str] = None) -> tuple:
        """
        Show Microsoft accounts linked with Windows when WAM did not complete sign-in.
        Returns (should_proceed: bool, selected_account: Optional[dict]).
        """
        try:
            linked_accounts = get_windows_linked_microsoft_accounts()
        except Exception as e:
            self.log(f"[Warning] Could not query Windows-linked Microsoft accounts: {e}")
            linked_accounts = []

        if not linked_accounts:
            return True, None

        dialog = WindowsLinkedAccountsDialog(linked_accounts, parent=self, header_text=header_text)
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

    def _revert_cancelled_auth_profile(self):
        accounts = load_accounts() or {}
        profiles = accounts.get("profiles") or {}
        prev_prof = getattr(self, "previous_active_profile", None)
        if prev_prof and prev_prof in profiles and accounts.get("active_profile") != prev_prof:
            accounts["active_profile"] = prev_prof
            save_accounts(accounts)

        cancelled_prof = getattr(self, "_auth_cancelled_profile", None)
        if cancelled_prof and cancelled_prof != prev_prof and cancelled_prof not in profiles:
            import shutil
            cache_path = ROOT_DIR / ".cache" / cancelled_prof
            if cache_path.exists():
                shutil.rmtree(cache_path, ignore_errors=True)

        self.previous_active_profile = None
        self._auth_cancelled_profile = None
        self._pending_auth_profile = None

    def _begin_auth_flow(
        self,
        from_launch: bool = False,
        selected_win_acc: Optional[dict] = None,
        force_popup: bool = False,
        preserve_force_new: Optional[bool] = None,
    ):
        import uuid as _uuid
        self.clear_progress_error()
        if not from_launch:
            self.log("Starting Microsoft authentication...")
        self.is_just_logging_in = not from_launch
        self.is_authenticating = True
        self._auth_from_launch = bool(from_launch)
        self._login_succeeded = False
        self._browser_auth_done = False
        self._pending_login_hint = selected_win_acc.get("email") if isinstance(selected_win_acc, dict) else None

        if from_launch:
            self.set_progress_visible(True)
            self.status_label.setText("[1/6] Authenticating with Microsoft...")
            self.step_progress_bar.setRange(0, 100)
            self.step_progress_bar.setValue(0)
            self.step_progress_bar.setFormat("Step 1/6 - Authentication")
            self.task_progress_bar.setRange(0, 0)
            self.task_progress_bar.setFormat("Current Task: %p%")
        else:
            self.task_progress_bar.setVisible(False)
            self.status_label.setText("Authenticating with Microsoft...")
            self.step_progress_bar.setRange(0, 100)
            self.step_progress_bar.setValue(20)
            self.step_progress_bar.setFormat("Authenticating with Microsoft... (%p%)")
            self.step_progress_bar.setVisible(True)
            self.update_ui_state()

        accounts = load_accounts() or {}
        profiles = accounts.get("profiles") or {}
        current_profile = accounts.get("active_profile")
        force_new = getattr(self, "_force_new_login", False) if preserve_force_new is None else bool(preserve_force_new)
        self._auth_was_force_new = force_new
        cached_pid = selected_win_acc.get("cached_profile_id") if isinstance(selected_win_acc, dict) else None

        # Keep active_profile on disk untouched until auth succeeds so closing/failing mid-login never logs the user out
        if cached_pid:
            self.previous_active_profile = current_profile
            target_pid = cached_pid
        elif force_new or self._pending_login_hint or not current_profile or current_profile not in profiles:
            self.previous_active_profile = current_profile if (current_profile and current_profile in profiles) else None
            target_pid = str(_uuid.uuid4())
        else:
            self.previous_active_profile = current_profile
            target_pid = current_profile

        self._pending_auth_profile = target_pid
        self._auth_cancelled_profile = target_pid

        use_wam = sys.platform == "win32" and not force_popup
        self._wam_fallback_eligible = use_wam

        auth_head = "--internal-ms-auth" if getattr(sys, "frozen", False) else get_script_path("ms_accounts.py")
        cmd_args = [auth_head, "--json", "--profile", target_pid]
        if use_wam:
            cmd_args.extend(["--wam", "--hwnd", str(int(self.winId()))])
            if self._pending_login_hint:
                cmd_args.extend(["--login-hint", self._pending_login_hint])
            wam_id = (selected_win_acc.get("wam_account_id") or selected_win_acc.get("cid")) if isinstance(selected_win_acc, dict) else None
            if wam_id:
                cmd_args.extend(["--wam-id", str(wam_id)])
        else:
            cmd_args.extend(["--popup", "--ipc-popup"])
            if self._pending_login_hint:
                cmd_args.extend(["--login-hint", self._pending_login_hint])

        self.process = QProcess()
        self.process.setProgram(get_python_cli_executable())
        self.process.setArguments(cmd_args)
        self._force_new_login = False
        self.process.readyReadStandardOutput.connect(self.handle_auth_stdout)
        self.process.readyReadStandardError.connect(self.handle_stderr)
        self.process.finished.connect(self.auth_finished)
        self.process.start()

    def do_login(self):
        self.clear_progress_error()
        self.task_progress_bar.setVisible(False)
        self.step_progress_bar.setVisible(True)
        self.step_progress_bar.setRange(0, 100)
        self.step_progress_bar.setValue(5)
        self.step_progress_bar.setFormat("Starting sign-in... (%p%)")
        self.status_label.setText("Starting sign-in...")
        QApplication.processEvents()
        self._begin_auth_flow(from_launch=False)

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
                if self._is_launch_or_auth_busy():
                    QApplication.processEvents()
                    return
                self.step_progress_bar.setVisible(True)
                self.step_progress_bar.setRange(0, 100)
                self.step_progress_bar.setValue(percent)
                self.step_progress_bar.setFormat(f"{msg} ({percent}%)")
                self.status_label.setText(msg)
                QApplication.processEvents()

            self.banner_widget = ProfileBannerWidget(auth_data, self, progress_callback=banner_progress)
            if not getattr(self, "_banner_collapsed", False):
                h = self.banner_widget.sizeHint()
                if h.isValid() and h.height() > 0:
                    self.banner_widget.setMinimumHeight(h.height())
                    if hasattr(self, "top_banner_wrapper"):
                        self.top_banner_wrapper.setMinimumHeight(h.height())
                    self._banner_last_h = h.height()
                elif getattr(self, "_banner_last_h", 0):
                    self.banner_widget.setMinimumHeight(self._banner_last_h)
                    if hasattr(self, "top_banner_wrapper"):
                        self.top_banner_wrapper.setMinimumHeight(self._banner_last_h)
            elif hasattr(self, "top_banner_wrapper"):
                self.top_banner_wrapper.setMinimumHeight(0)
            self.banner_container.addWidget(self.banner_widget)
            if getattr(self, "_banner_collapsed", False):
                self._game_perf_paused = True
            if getattr(self, "_game_perf_paused", False) and hasattr(self.banner_widget, "pause_for_game"):
                self.banner_widget.pause_for_game()
            
            # Hide the progress bar after it's done loading, unless a launch pipeline, shortcut launch, or authentication is active
            if (
                not self._is_launch_or_auth_busy()
                and (not getattr(self, 'task_progress_bar', None) or not self.task_progress_bar.isVisible())
            ):
                self.step_progress_bar.setRange(0, 100)
                self.step_progress_bar.setValue(100)
                self.step_progress_bar.setFormat("Ready! (100%)")
                self.status_label.setText("Ready to launch.")
                QTimer.singleShot(600, lambda: self.step_progress_bar.setVisible(False) if not self._is_launch_or_auth_busy() else None)
        else:
            if not self._is_launch_or_auth_busy():
                self.step_progress_bar.setVisible(False)
                self.status_label.setText("Ready to launch.")

        self._sync_collapse_buttons_ui()
        self.update_ui_state()
        self.update_window_size()

    def _refresh_auth_buttons(self):
        """Show Login when no valid account is signed in; otherwise show Switch/Logout."""
        if not all(hasattr(self, n) for n in ("login_btn", "logout_btn", "switch_account_btn")):
            return
        try:
            auth = get_auth_cache()
            logged_in = bool(isinstance(auth, dict) and auth.get("mc_access_token"))
            if not logged_in:
                accounts = load_accounts() or {}
                active = accounts.get("active_profile")
                pdata = (accounts.get("profiles") or {}).get(active)
                logged_in = bool(isinstance(pdata, dict) and pdata.get("mc_access_token"))
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
        """Stop the profile banner from being squashed below its natural size when expanded."""
        if getattr(self, "_banner_collapsed", False):
            if hasattr(self, "top_banner_wrapper"):
                self.top_banner_wrapper.setMinimumHeight(0)
            return
        bw = getattr(self, "banner_widget", None)
        if not bw:
            if hasattr(self, "top_banner_wrapper"):
                self.top_banner_wrapper.setMinimumHeight(0)
            return
        try:
            if bw.isVisible() or not bw.isHidden():
                h = bw.sizeHint()
                if h.isValid() and h.height() > 0:
                    if h.height() != bw.minimumHeight():
                        bw.setMinimumHeight(h.height())
                    if hasattr(self, "top_banner_wrapper"):
                        self.top_banner_wrapper.setMinimumHeight(h.height())
                    self._banner_last_h = h.height()
        except RuntimeError:
            self.banner_widget = None

    def _save_ui_collapse_prefs(self):
        try:
            cfg = load_launcher_config()
            cfg["ui_collapse_left"] = bool(getattr(self, "_left_collapsed", False))
            cfg["ui_collapse_right"] = bool(getattr(self, "_right_collapsed", False))
            cfg["ui_collapse_banner"] = bool(getattr(self, "_banner_collapsed", False))
            save_launcher_config(cfg)
        except Exception:
            pass

    def _sync_collapse_buttons_ui(self):
        left_c = bool(getattr(self, "_left_collapsed", False))
        right_c = bool(getattr(self, "_right_collapsed", False))
        banner_c = bool(getattr(self, "_banner_collapsed", False))
        if hasattr(self, "toggle_left_panel_btn"):
            self.toggle_left_panel_btn.blockSignals(True)
            self.toggle_left_panel_btn.setChecked(not left_c)
            self.toggle_left_panel_btn.setText("▶ Show Versions" if left_c else "◀ Hide Versions")
            self.toggle_left_panel_btn.blockSignals(False)
        if hasattr(self, "toggle_right_panel_btn"):
            self.toggle_right_panel_btn.blockSignals(True)
            self.toggle_right_panel_btn.setChecked(not right_c)
            self.toggle_right_panel_btn.setText("◀ Show Loaders" if right_c else "Hide Loaders ▶")
            self.toggle_right_panel_btn.blockSignals(False)
        if hasattr(self, "toggle_banner_btn"):
            self.toggle_banner_btn.blockSignals(True)
            self.toggle_banner_btn.setChecked(not banner_c)
            if banner_c:
                uname = (self.auth_data or {}).get("mc_username") if isinstance(getattr(self, "auth_data", None), dict) else ""
                tag = f" ({uname})" if uname else ""
                self.toggle_banner_btn.setText(f"▼ Show Profile && 3D Banner{tag}")
            else:
                self.toggle_banner_btn.setText("▲ Hide Profile && 3D Banner")
            self.toggle_banner_btn.blockSignals(False)

    def _on_splitter_moved(self, _pos: int = 0, _index: int = 0):
        if not hasattr(self, "main_splitter"):
            return
        sizes = self.main_splitter.sizes()
        if len(sizes) != 3:
            return
        left_c = (sizes[0] == 0) or (not self.left_versions_panel.isVisible())
        right_c = (sizes[2] == 0) or (not self.right_loader_panel.isVisible())
        if sizes[0] > 0:
            self._saved_left_w = sizes[0]
        if sizes[2] > 0:
            self._saved_right_w = sizes[2]
        changed = (left_c != getattr(self, "_left_collapsed", False)) or (right_c != getattr(self, "_right_collapsed", False))
        self._left_collapsed = left_c
        self._right_collapsed = right_c
        self.left_versions_panel.setMinimumWidth(0 if left_c else 220)
        self.right_loader_panel.setMinimumWidth(0 if right_c else 220)
        if changed:
            self._sync_collapse_buttons_ui()
            self._save_ui_collapse_prefs()
            self.update_window_size(shrink_to_fit=True)

    def set_left_panel_collapsed(self, collapsed: bool, persist: bool = True, update_size: bool = True):
        collapsed = bool(collapsed)
        sizes = self.main_splitter.sizes() if hasattr(self, "main_splitter") else [235, 650, 245]
        if collapsed and len(sizes) == 3 and sizes[0] > 0:
            self._saved_left_w = sizes[0]
        self._left_collapsed = collapsed
        self.left_versions_panel.setMinimumWidth(0 if collapsed else 220)
        self.left_versions_panel.setVisible(not collapsed)
        if hasattr(self, "main_splitter"):
            cur = self.main_splitter.sizes()
            rw = cur[2] if len(cur) == 3 and not getattr(self, "_right_collapsed", False) else 0
            lw = 0 if collapsed else max(220, getattr(self, "_saved_left_w", 235))
            cw = max(650, cur[1] if len(cur) == 3 else 650)
            self.main_splitter.setSizes([lw, cw, rw])
        self._sync_collapse_buttons_ui()
        if persist:
            self._save_ui_collapse_prefs()
        if update_size:
            self.update_window_size(shrink_to_fit=collapsed)

    def set_right_panel_collapsed(self, collapsed: bool, persist: bool = True, update_size: bool = True):
        collapsed = bool(collapsed)
        sizes = self.main_splitter.sizes() if hasattr(self, "main_splitter") else [235, 650, 245]
        if collapsed and len(sizes) == 3 and sizes[2] > 0:
            self._saved_right_w = sizes[2]
        self._right_collapsed = collapsed
        self.right_loader_panel.setMinimumWidth(0 if collapsed else 220)
        self.right_loader_panel.setVisible(not collapsed)
        if hasattr(self, "main_splitter"):
            cur = self.main_splitter.sizes()
            lw = cur[0] if len(cur) == 3 and not getattr(self, "_left_collapsed", False) else 0
            rw = 0 if collapsed else max(220, getattr(self, "_saved_right_w", 245))
            cw = max(650, cur[1] if len(cur) == 3 else 650)
            self.main_splitter.setSizes([lw, cw, rw])
        self._sync_collapse_buttons_ui()
        if persist:
            self._save_ui_collapse_prefs()
        if update_size:
            self.update_window_size(shrink_to_fit=collapsed)

    def set_banner_collapsed(self, collapsed: bool, persist: bool = True, update_size: bool = True):
        collapsed = bool(collapsed)
        self._banner_collapsed = collapsed
        if hasattr(self, "top_banner_wrapper"):
            if collapsed:
                self.top_banner_wrapper.setMinimumHeight(0)
            self.top_banner_wrapper.setVisible(not collapsed)
        if not collapsed:
            self._lock_banner_minimum()
        self._sync_collapse_buttons_ui()
        self._evaluate_game_performance_mode()
        if persist:
            self._save_ui_collapse_prefs()
        if update_size:
            self.update_window_size(shrink_to_fit=collapsed)

    def _flush_versions_browser_if_dirty(self):
        if not getattr(self, "_versions_browser_dirty", False):
            return
        self._versions_browser_dirty = False
        browser_prof = getattr(self, "_browser_profile_name", None) or self.get_active_profile_name()
        self._refresh_installed_info(profile_name=browser_prof)
        if getattr(self, "_active_loader_id", "none") == "none" and hasattr(self, "mc_versions_list"):
            self._populate_mc_versions_for_none()
        elif hasattr(self, "mc_versions_list") and getattr(self, "_all_mc_versions", None):
            self._filter_mc_versions_panel()
            if hasattr(self, "loader_versions_list") and getattr(self, "_all_loader_versions", None):
                self._filter_loader_versions_panel()

    def _on_main_tab_changed(self, idx: int):
        if hasattr(self, "profiles_tab") and hasattr(self, "main_tabs") and self.main_tabs.widget(idx) is self.profiles_tab:
            self.profiles_tab.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
            self._profiles_tab_dirty = False
            self.profiles_tab.refresh_profiles(self.get_active_profile_name())
            self.profiles_tab.sync_dev_mode_tab()
            self._flush_versions_browser_if_dirty()
        elif hasattr(self, "profiles_tab"):
            self.profiles_tab.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.update_window_size()

    def _apply_profile_preferences(self, profile_name: str = None):
        """Apply preferred mod loader & version stored on a profile when switching profiles."""
        try:
            cfg = load_launcher_config()
            pname = profile_name or cfg.get("active_profile", "Default")
            dirs = (cfg.get("profiles") or {}).get(pname) or {}
            if not isinstance(dirs, dict):
                return
            pref_loader = dirs.get("preferred_loader")
            if pref_loader and (pref_loader == "none" or pref_loader in self._loader_map):
                if getattr(self, "_active_loader_id", "none") != pref_loader:
                    self.select_loader(pref_loader)
            pref_ver = dirs.get("preferred_version")
            if pref_ver and hasattr(self, "version_combo"):
                for i in range(self.version_combo.count()):
                    if self.version_combo.itemData(i) == pref_ver:
                        self.version_combo.setCurrentIndex(i)
                        break
        except Exception:
            pass

    def _compute_layout_minimums(self):
        """Return (min_w, min_h, hint) reflecting active tab & top banner collapse state."""
        on_profiles_tab = bool(hasattr(self, "main_tabs") and self.main_tabs.currentIndex() == 1)
        if hasattr(self, "profiles_tab"):
            pol = QSizePolicy.Policy.Preferred if on_profiles_tab else QSizePolicy.Policy.Ignored
            self.profiles_tab.setSizePolicy(pol, pol)

        for w_name in ("top_banner_wrapper", "center_panel", "play_tab_widget", "profiles_tab", "main_tabs", "central_widget"):
            w = getattr(self, w_name, None)
            if w is not None:
                w.updateGeometry()
        if hasattr(self, "center_layout"):
            self.center_layout.invalidate()
            self.center_layout.activate()
        self.layout.invalidate()
        self.layout.activate()
        mhint = self.central_widget.minimumSizeHint()

        if on_profiles_tab:
            min_w = max(860, mhint.width())
            min_h = max(600, min(mhint.height(), 780))
            return min_w, min_h, mhint

        play_hint = self.play_tab_widget.minimumSizeHint() if hasattr(self, "play_tab_widget") else mhint
        banner_collapsed = bool(getattr(self, "_banner_collapsed", False))
        base_min_h = 340 if banner_collapsed else 560
        min_w = max(695, play_hint.width(), mhint.width())
        if banner_collapsed and not self.results_text.isVisible():
            min_h = max(base_min_h, min(mhint.height(), 460))
        else:
            # Never clamp below actual expanded profile + 3D banner + locator bar minimum height
            min_h = max(base_min_h, mhint.height(), play_hint.height() + 30)
        return min_w, min_h, play_hint

    def update_window_size(self, shrink_to_fit: bool = False):
        # Use SetDefaultConstraint so manual dynamic setMinimumSize values aren't pinned to stale expanded hints.
        self.layout.setSizeConstraint(QVBoxLayout.SizeConstraint.SetDefaultConstraint)
        self.layout.setContentsMargins(16, 12, 16, 12)
        if not hasattr(self, 'dummy_width_widget'):
            return  # UI still being constructed
        console_shown = self.results_text.isVisible()
        self.results_text.setMinimumHeight(90 if console_shown else 0)
        self.setMaximumHeight(16777215)
        self._lock_banner_minimum()
        min_w, min_h, mhint = self._compute_layout_minimums()
        self.setMinimumSize(min_w, min_h)
        if shrink_to_fit:
            self._pending_shrink_fit = True
            target_w = min_w
            target_h = max(min_h, mhint.height())
            self.resize(target_w, target_h)
        else:
            self.resize(max(self.width(), min_w), max(self.height(), mhint.height()))

        # Re-measure once banner/tabs settle while preserving collapsed dimensions
        if not getattr(self, "_min_remeasure_pending", False):
            self._min_remeasure_pending = True

            def _again():
                self._min_remeasure_pending = False
                do_shrink = bool(getattr(self, "_pending_shrink_fit", False))
                self._pending_shrink_fit = False
                try:
                    self._lock_banner_minimum()
                    mw, mh, h = self._compute_layout_minimums()
                    self.setMinimumSize(mw, mh)
                    if do_shrink:
                        self.resize(mw, max(mh, h.height()))
                    else:
                        self.resize(max(self.width(), mw), max(self.height(), h.height()))
                except RuntimeError:
                    pass
            QTimer.singleShot(400, _again)
            QTimer.singleShot(1500, _again)

    def load_versions(self, force_manifest_refresh: bool = False, defer_versions_browser: bool = False):
        old_selected = self.version_combo.currentData()
        if old_selected is None and self.remember_version and self.last_played_version:
            old_selected = self.last_played_version

        self.version_combo.blockSignals(True)
        self.version_combo.clear()

        if not force_manifest_refresh and getattr(self, "_available_versions_cache", None):
            versions = list(self._available_versions_cache)
        else:
            versions = list(get_available_versions() or [])
            self._available_versions_cache = versions
        self.is_offline = not bool(versions)

        active_prof = self.get_active_profile_name()
        installed_info = self._refresh_installed_info(profile_name=active_prof)
        by_id = installed_info.get("by_id") or {}

        # Version dropdown contains active profile's downloaded versions ONLY (Latest channels live in Profiles -> Versions)
        loader_names = {item["id"]: item["name"] for item in self._loaders}
        loader_names["none"] = "Vanilla"

        type_lookup = {v_id: (v_type, v_date) for v_id, v_type, v_date in versions}
        config = load_launcher_config()
        hide_date = config.get("hide_release_date", False)

        # Put modded profiles first, then downloaded vanilla versions ordered cleanly
        mojang_order = {v_id: i for i, (v_id, _, _) in enumerate(versions)}
        sorted_ids = sorted(
            by_id.keys(),
            key=lambda vid: (
                0 if by_id[vid][0] != "none" else 1,
                mojang_order.get(by_id[vid][1], 99999),
                vid,
            ),
        )

        count = 0
        for v_id in sorted_ids:
            lid, mc_ver, _build = by_id[v_id]
            ldr_label = loader_names.get(lid, "Vanilla")
            if lid == "none" and v_id in type_lookup:
                v_type, v_date = type_lookup[v_id]
                t_disp = v_type.replace("_", " ").title()
                if hide_date or not v_date:
                    text = f"{v_id} ({t_disp})"
                else:
                    text = f"{v_id} ({t_disp} - {v_date})"
            elif lid == "none":
                text = f"{v_id} (Vanilla)"
            else:
                text = f"{v_id} ({ldr_label})"
            if self.is_offline:
                text += " (Offline)"
            text += " [Downloaded]"

            self.version_combo.addItem(text, v_id)
            idx = self.version_combo.count() - 1
            self.version_combo.setItemData(idx, True, Qt.ItemDataRole.UserRole + 1)
            count += 1

        if count == 0:
            self.version_combo.addItem("No versions downloaded yet — install one in Profiles -> Versions", None)
        else:
            idx_to_select = 0
            if old_selected:
                for i in range(self.version_combo.count()):
                    if self.version_combo.itemData(i) == old_selected:
                        idx_to_select = i
                        break
            self.version_combo.setCurrentIndex(idx_to_select)

        self.version_combo.blockSignals(False)

        mode_msg = (
            f"[System] Offline mode. Loaded {count} downloaded version(s)."
            if self.is_offline
            else f"[System] Loaded {count} downloaded version(s)."
        )
        self.log(mode_msg)

        # Refresh Versions & Loaders browser panels (defer if caller is switching profiles on Play tab)
        on_profiles_tab = bool(
            hasattr(self, "main_tabs")
            and hasattr(self, "profiles_tab")
            and self.main_tabs.currentWidget() is self.profiles_tab
        )
        if defer_versions_browser and not on_profiles_tab:
            self._versions_browser_dirty = True
        else:
            self._versions_browser_dirty = False
            browser_prof = getattr(self, "_browser_profile_name", None) or active_prof
            if browser_prof != active_prof:
                self._refresh_installed_info(profile_name=browser_prof)
            if getattr(self, "_active_loader_id", "none") == "none" and hasattr(self, "mc_versions_list"):
                self._populate_mc_versions_for_none()
            elif hasattr(self, "mc_versions_list") and getattr(self, "_all_mc_versions", None):
                self._filter_mc_versions_panel()
                if hasattr(self, "loader_versions_list") and getattr(self, "_all_loader_versions", None):
                    self._filter_loader_versions_panel()

        # Ensure UI updates to reflect the initially selected version
        self.on_version_changed(self.version_combo.currentIndex())
        self.update_ui_state()
        if not getattr(self, "_defer_profile_card_refresh", False):
            if hasattr(self, "profile_cards_container"):
                self.refresh_profile_cards()
            if hasattr(self, "play_install_queue_tree"):
                self.refresh_play_install_queue()

    def get_active_profile_name(self) -> str:
        cfg = load_launcher_config()
        active = str(cfg.get("active_profile") or "Default")
        profiles = cfg.get("profiles") or {"Default": {}}
        if active not in profiles and profiles:
            return next(iter(profiles.keys()))
        return active

    def set_active_profile(self, profile_name: str):
        if not profile_name:
            return
        cfg = load_launcher_config()
        profiles = cfg.get("profiles") or {"Default": {}}
        if profile_name not in profiles:
            return
        if cfg.get("active_profile") != profile_name:
            cfg["active_profile"] = profile_name
            save_launcher_config(cfg)
        ensure_profile_links(profile_name)
        on_profiles_tab = bool(
            hasattr(self, "main_tabs")
            and hasattr(self, "profiles_tab")
            and self.main_tabs.currentWidget() is self.profiles_tab
        )
        self._defer_profile_card_refresh = True
        try:
            self.load_versions(defer_versions_browser=True)
            self.setup_quick_play()
            self._apply_profile_preferences(profile_name)
        finally:
            self._defer_profile_card_refresh = False
        self.refresh_profile_cards()
        self.refresh_play_install_queue()
        if hasattr(self, "profiles_tab"):
            if on_profiles_tab:
                self._profiles_tab_dirty = False
                self.profiles_tab.refresh_profiles(profile_name)
            else:
                self._profiles_tab_dirty = True

    def reload_profiles_combo(self):
        if not getattr(self, "_defer_profile_card_refresh", False):
            if hasattr(self, "profile_cards_container"):
                self.refresh_profile_cards()
            if hasattr(self, "play_install_queue_tree"):
                self.refresh_play_install_queue()

    def on_profile_changed(self):
        pname = self.get_active_profile_name()
        ensure_profile_links(pname)
        on_profiles_tab = bool(
            hasattr(self, "main_tabs")
            and hasattr(self, "profiles_tab")
            and self.main_tabs.currentWidget() is self.profiles_tab
        )
        self._defer_profile_card_refresh = True
        try:
            self.load_versions(defer_versions_browser=True)
            self.setup_quick_play()
            self._apply_profile_preferences(pname)
        finally:
            self._defer_profile_card_refresh = False
        self.refresh_profile_cards()
        self.refresh_play_install_queue()
        if hasattr(self, "profiles_tab"):
            if on_profiles_tab:
                self._profiles_tab_dirty = False
                self.profiles_tab.refresh_profiles(pname)
            else:
                self._profiles_tab_dirty = True

    def open_add_profile_dialog(self):
        dlg = AddProfileDialog(self, self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.created_profile_name:
            self._defer_profile_card_refresh = True
            try:
                self.load_versions(defer_versions_browser=True)
                self.setup_quick_play()
                self._apply_profile_preferences(dlg.created_profile_name)
            finally:
                self._defer_profile_card_refresh = False
            self.refresh_profile_cards()
            self.refresh_play_install_queue()
            if hasattr(self, "profiles_tab"):
                self.profiles_tab.refresh_profiles(dlg.created_profile_name)

    def _activate_profile_from_card(self, profile_name: str):
        if profile_name:
            self.set_active_profile(profile_name)

    def quick_play_profile_target(self, profile_name: str, qp_type: str = None, qp_id: str = None, version_id: str = None):
        """Switch to profile_name (if needed), select optional Quick Play world/server or version, and launch."""
        cfg = load_launcher_config()
        if profile_name and profile_name in (cfg.get("profiles") or {}):
            if cfg.get("active_profile") != profile_name:
                self.set_active_profile(profile_name)

        if version_id and hasattr(self, "version_combo"):
            idx = self.version_combo.findData(version_id)
            if idx < 0:
                is_dl = version_id in get_downloaded_versions(profile_name=profile_name)
                lbl = f"{version_id} [Downloaded]" if is_dl else version_id
                self.version_combo.insertItem(0, lbl, version_id)
                self.version_combo.setItemData(0, is_dl, Qt.ItemDataRole.UserRole + 1)
                idx = 0
            self.version_combo.setCurrentIndex(idx)

        if not self.version_combo.currentData():
            QMessageBox.information(
                self,
                "No Version Selected",
                f"Profile '{profile_name}' has no version selected yet.\n"
                "Open Profiles & Directories -> Versions to select or install one.",
            )
            if hasattr(self, "main_tabs"):
                self.main_tabs.setCurrentIndex(0)
            return

        if qp_type and qp_id and hasattr(self, "quick_play_combo"):
            matched_idx = -1
            for i in range(self.quick_play_combo.count()):
                d = self.quick_play_combo.itemData(i)
                if isinstance(d, dict) and d.get("type") == qp_type and str(d.get("id")) == str(qp_id):
                    matched_idx = i
                    break
            if matched_idx < 0:
                qp_obj = {"type": qp_type, "id": str(qp_id), "display": f"{qp_type.title()}: {qp_id}"}
                self.quick_play_combo.addItem(qp_obj["display"], qp_obj)
                matched_idx = self.quick_play_combo.count() - 1
            self.quick_play_combo.setCurrentIndex(matched_idx)
        elif qp_type is None and hasattr(self, "quick_play_combo"):
            self.quick_play_combo.setCurrentIndex(0)

        if hasattr(self, "main_tabs"):
            self.main_tabs.setCurrentIndex(0)

        if not self._is_launch_or_auth_busy():
            self.start_launch_sequence()

    def refresh_profile_cards(self):
        """Rebuild clean clickable Profile Preview Cards (versions, worlds, mods, links & queued count — no dropdown/QP clutter)."""
        if not hasattr(self, "profile_cards_layout") or not hasattr(self, "profile_cards_container"):
            return
        while self.profile_cards_layout.count():
            item = self.profile_cards_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self.profile_preview_cards = {}

        cfg = load_launcher_config()
        profiles = cfg.get("profiles") or {"Default": {}}
        active_name = cfg.get("active_profile", "Default")
        pending_q = get_pending_install_queue()
        is_dark = get_effective_is_dark()

        for pname in profiles.keys():
            stats = get_profile_stats(pname)
            links = inspect_profile_directory_links(pname)
            linked_labels = [row["label"] for row in links if row.get("status") not in ("isolated", None)]
            q_cnt = sum(1 for q in pending_q if q.get("profile") == pname)

            n_vers = len(stats.get("versions") or [])
            n_worlds = len(stats.get("worlds") or [])
            n_mods = len(stats.get("active_mods") or [])
            pref_ldr = stats.get("preferred_loader") or ""
            ldr_tag = f" [{pref_ldr}]" if pref_ldr and pref_ldr != "none" else ""
            link_tag = f" • 🔗{len(linked_labels)}" if linked_labels else ""
            q_tag = f" • ⬇{q_cnt}" if q_cnt else ""

            is_active = (pname == active_name)
            if is_dark:
                bg = "#1e2a38" if is_active else "#252526"
                border = "#3b82f6" if is_active else "#3c3c3c"
                title_fg = "#ffffff" if is_active else "#dcdcdc"
                sub_fg = "#a8c7fa" if is_active else "#a0a0a0"
            else:
                bg = "#e8f1fc" if is_active else "#f3f4f6"
                border = "#2563eb" if is_active else "#cbd5e1"
                title_fg = "#0f172a"
                sub_fg = "#1d4ed8" if is_active else "#475569"

            card = QFrame(self.profile_cards_container)
            card.setObjectName("profilePreviewCard")
            card.setFixedHeight(44)
            card.setMinimumWidth(175)
            card.setMaximumWidth(250)
            card.setStyleSheet(
                f"QFrame#profilePreviewCard {{ background: {bg}; border: 1.5px solid {border}; border-radius: 6px; }}"
            )
            tip_lines = [
                f"Profile: {pname}" + (" (Active)" if is_active else ""),
                f"Downloaded versions: {n_vers}",
                f"Worlds: {n_worlds}",
                f"Active mods: {n_mods} (Disabled: {len(stats.get('disabled_mods') or [])})",
                f"Queued installs: {q_cnt}",
                f"Base dir: {stats.get('base_dir')}",
            ]
            if linked_labels:
                tip_lines.append("Links / Shared: " + ", ".join(linked_labels))
            card.setToolTip("\n".join(tip_lines))

            cv = QVBoxLayout(card)
            cv.setContentsMargins(7, 3, 7, 3)
            cv.setSpacing(1)

            title_btn = QPushButton(f"{'● ' if is_active else ''}{pname}{ldr_tag}")
            title_btn.setFlat(True)
            title_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            title_btn.setStyleSheet(
                f"QPushButton {{ text-align: left; border: none; padding: 0px; font-weight: bold; color: {title_fg}; background: transparent; }}"
            )
            title_btn.clicked.connect(lambda _c=False, p=pname: self._activate_profile_from_card(p))
            cv.addWidget(title_btn)

            stats_lbl = QLabel(f"{n_vers} ver • {n_worlds} worlds • {n_mods} mods{link_tag}{q_tag}")
            stats_lbl.setStyleSheet(f"color: {sub_fg}; font-size: 11px; border: none; background: transparent;")
            cv.addWidget(stats_lbl)

            card.setProperty("stats_text", stats_lbl.text())
            self.profile_cards_layout.addWidget(card)
            self.profile_preview_cards[pname] = card

        self.profile_cards_layout.addStretch()

    def refresh_play_install_queue(self):
        """Populate the read-only Play tab queue with 'Just Install' and 'Launch & Install' checkboxes per item."""
        if not hasattr(self, "play_install_queue_tree"):
            return
        t = self.play_install_queue_tree
        t.blockSignals(True)
        try:
            t.clear()
            items = get_pending_install_queue()
            if not items:
                empty_it = QTreeWidgetItem(["No versions queued yet — add/modify items in Profiles & Directories -> Versions.", "", ""])
                empty_it.setFlags(Qt.ItemFlag.ItemIsEnabled)
                empty_it.setData(0, Qt.ItemDataRole.UserRole, None)
                t.addTopLevelItem(empty_it)
                self.update_ui_state()
                return
            for item in items:
                lbl = str(item.get("label") or f"{item.get('profile', 'Default')}: {item.get('mc_version', '')}")
                row = QTreeWidgetItem([lbl, "", ""])
                row.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                    | Qt.ItemFlag.ItemIsUserCheckable
                )
                row.setData(0, Qt.ItemDataRole.UserRole, item)
                is_launch = (str(item.get("mode") or "install").lower() == "launch")
                row.setCheckState(1, Qt.CheckState.Unchecked if is_launch else Qt.CheckState.Checked)
                row.setCheckState(2, Qt.CheckState.Checked if is_launch else Qt.CheckState.Unchecked)
                t.addTopLevelItem(row)
        finally:
            t.blockSignals(False)
        if t.topLevelItemCount() > 0 and t.currentItem() is None:
            t.setCurrentItem(t.topLevelItem(0))
        self.update_ui_state()

    def _on_play_queue_item_changed(self, item: QTreeWidgetItem, column: int):
        if item is None or item.data(0, Qt.ItemDataRole.UserRole) is None:
            return
        t = self.play_install_queue_tree
        if column == 1 and item.checkState(1) == Qt.CheckState.Checked:
            t.blockSignals(True)
            item.setCheckState(2, Qt.CheckState.Unchecked)
            t.blockSignals(False)
        elif column == 2 and item.checkState(2) == Qt.CheckState.Checked:
            t.blockSignals(True)
            item.setCheckState(1, Qt.CheckState.Unchecked)
            t.blockSignals(False)

    def _collect_play_queue_actions(self) -> tuple[List[Dict], List[Dict]]:
        just_install: List[Dict] = []
        launch_install: List[Dict] = []
        if not hasattr(self, "play_install_queue_tree"):
            return just_install, launch_install
        t = self.play_install_queue_tree
        for i in range(t.topLevelItemCount()):
            row = t.topLevelItem(i)
            d = row.data(0, Qt.ItemDataRole.UserRole) if row else None
            if not isinstance(d, dict):
                continue
            item_copy = dict(d)
            if row.checkState(2) == Qt.CheckState.Checked:
                item_copy["mode"] = "launch"
                launch_install.append(item_copy)
            elif row.checkState(1) == Qt.CheckState.Checked:
                item_copy["mode"] = "install"
                just_install.append(item_copy)
        return just_install, launch_install

    def _play_tab_run_queue(self):
        just_inst, launch_inst = self._collect_play_queue_actions()
        combined = just_inst + launch_inst
        if not combined:
            QMessageBox.information(self, "Nothing Checked", "Check 'Just Install' or 'Launch & Install' on one or more queued rows first.")
            return
        launch_target = launch_inst[0] if launch_inst else None
        self.start_batch_install(
            combined,
            target_profile=str((launch_target or combined[0]).get("profile") or self.get_active_profile_name()),
            launch_last=bool(launch_inst),
        )

    def _play_tab_remove_queued(self):
        if not hasattr(self, "play_install_queue_tree"):
            return
        t = self.play_install_queue_tree
        rem_ids = set()
        for row in t.selectedItems():
            d = row.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(d, dict) and d.get("id"):
                rem_ids.add(str(d["id"]))
        if not rem_ids and t.currentItem():
            d = t.currentItem().data(0, Qt.ItemDataRole.UserRole)
            if isinstance(d, dict) and d.get("id"):
                rem_ids.add(str(d["id"]))
        if not rem_ids:
            return
        remove_pending_install_items(rem_ids)
        self.refresh_play_install_queue()
        if hasattr(self, "profiles_tab"):
            self.profiles_tab.refresh_versions_queue()
        self.refresh_profile_cards()

    def start_batch_install(
        self,
        items: List[Dict],
        target_profile: Optional[str] = None,
        launch_last: bool = False,
        quick_play: Optional[dict] = None,
    ):
        """Install 1 or many queued Vanilla/Mod-Loader items onto their target profiles (including Java, Client, Libraries & Assets) and optionally launch."""
        if not items or self._is_mod_loader_installing():
            return
        if launch_last and target_profile:
            self.set_active_profile(target_profile)

        self.set_progress_visible(True)
        self.step_progress_bar.setRange(0, 100)
        self.step_progress_bar.setValue(0)
        self.step_progress_bar.setFormat(f"Preparing {len(items)} install(s)... (%p%)")
        self.task_progress_bar.setRange(0, 100)
        self.task_progress_bar.setValue(0)
        self.task_progress_bar.setFormat("Current Task: %p%")
        if hasattr(self, "loader_install_status"):
            self.loader_install_status.setText(f"Starting install ({len(items)} item(s))...")
        self.status_label.setText(f"Installing {len(items)} item(s)...")
        self.log(f"[InstallQueue] Starting install ({len(items)} item(s))...")

        worker = _BatchInstallWorker(items, dev_mode=getattr(self, "dev_mode", False), parent=self)
        self._batch_install_worker = worker

        def _on_step_prog(cur_s: int, tot_s: int, _step_name: str, task_pct: float, status_str: str):
            base_pct = ((cur_s - 1) / max(1, tot_s)) * 100.0
            added_pct = (max(0.0, min(100.0, task_pct)) / 100.0) * (100.0 / max(1, tot_s))
            self.step_progress_bar.setValue(int(round(base_pct + added_pct)))
            self.step_progress_bar.setFormat(f"Step {cur_s}/{tot_s} - %p%")
            self.task_progress_bar.setRange(0, 100)
            self.task_progress_bar.setValue(int(round(task_pct)))
            self.task_progress_bar.setFormat("Current Task: %p%")
            if status_str:
                self.status_label.setText(status_str)
                if hasattr(self, "loader_install_status"):
                    self.loader_install_status.setText(status_str)

        def _on_item_done(qid: str, ok: bool, installed_vid: str, msg: str):
            if ok:
                self.log(f"[InstallQueue] Completed: {installed_vid} ({msg})")
                if qid:
                    remove_pending_install_items({qid})
            else:
                self.log(f"[Error] Batch item failed: {msg}")
            self.load_versions()
            if hasattr(self, "profiles_tab"):
                self.profiles_tab.refresh_profiles(self.profiles_tab._selected_profile_name())
                self.profiles_tab.refresh_versions_queue()
            self.refresh_play_install_queue()

        def _on_all_done(ok_count: int, fail_count: int, last_prof: str, last_ver: str):
            self._batch_install_worker = None
            self.step_progress_bar.setValue(100 if ok_count else 0)
            self.task_progress_bar.setValue(100 if ok_count else 0)
            self._update_mod_loader_buttons()
            self.update_ui_state()
            self.load_versions()
            if hasattr(self, "profiles_tab"):
                self.profiles_tab.refresh_profiles(self.profiles_tab._selected_profile_name())
                self.profiles_tab.refresh_versions_queue()
            self.refresh_play_install_queue()
            summary = f"Installed {ok_count} item(s)" + (f", {fail_count} failed" if fail_count else "")
            if hasattr(self, "loader_install_status"):
                self.loader_install_status.setText(summary)
            if launch_last and ok_count > 0 and last_ver:
                qp_t = (quick_play or {}).get("type") if isinstance(quick_play, dict) else None
                qp_i = str((quick_play or {}).get("id")) if isinstance(quick_play, dict) and (quick_play or {}).get("id") else None
                self.quick_play_profile_target(last_prof or self.get_active_profile_name(), qp_t, qp_i, version_id=last_ver)
                return
            self.set_progress_visible(False)
            self.status_label.setText(summary + ".")

        worker.step_progress.connect(_on_step_prog)
        worker.log_sig.connect(self.log)
        worker.item_finished.connect(_on_item_done)
        worker.all_finished.connect(_on_all_done)
        self._update_mod_loader_buttons()
        self.update_ui_state()
        self._track_and_start_loader_worker(worker)

    def open_directories_dialog(self):
        if hasattr(self, "main_tabs") and hasattr(self, "profiles_tab"):
            self.profiles_tab.refresh_profiles()
            self.main_tabs.setCurrentWidget(self.profiles_tab)
            return
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
        installed = self._refresh_installed_info()
        downloaded_ids = set((installed.get("by_id") or {}).keys())
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
        dialog.downloaded_cb.setText(f"Show only downloaded versions ({len(downloaded_ids)})")

        # Disable "Show only downloaded" if none exist
        if not downloaded_ids:
            self.show_downloaded_only = False
            dialog.downloaded_cb.setChecked(False)
            dialog.downloaded_cb.setEnabled(False)
            dialog.downloaded_cb.setToolTip("No versions have been downloaded yet.")
            
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
            if hasattr(self, "profiles_tab"):
                self.profiles_tab.sync_dev_mode_tab()
            if getattr(self, "_active_loader_id", "none") != "none":
                self._reload_mc_versions_panel(refresh=False)
                self._filter_loader_versions_panel()
            if not self.running_instances and not self.is_pipeline_running:
                self.status_label.setText("Ready to launch.")

    def setup_quick_play(self):
        prev_qp = None
        if hasattr(self, 'quick_play_combo') and self.quick_play_combo:
            prev_qp = self.quick_play_combo.currentData()
        else:
            self.quick_play_combo = QComboBox()
            self.quick_play_combo.setMinimumHeight(35)
            self.action_layout.insertWidget(0, self.quick_play_combo, stretch=2)

        prev_key = (
            (prev_qp.get("type"), str(prev_qp.get("id")))
            if isinstance(prev_qp, dict)
            else None
        )

        self.quick_play_combo.blockSignals(True)
        self.quick_play_combo.clear()
        self.quick_play_combo.addItem("No Quick Play", None)
        try:
            for qp in get_quick_play_options(get_dir("base"), include_realms=False):
                self.quick_play_combo.addItem(qp['display'], qp)
                if prev_key and (qp.get("type"), str(qp.get("id"))) == prev_key:
                    self.quick_play_combo.setCurrentIndex(self.quick_play_combo.count() - 1)
        except Exception as e:
            print("Failed to load quick play options:", e)
        self.quick_play_combo.blockSignals(False)
        self.update_ui_state()
        if not getattr(self, "_defer_profile_card_refresh", False) and hasattr(self, "profile_cards_container"):
            self.refresh_profile_cards()

        # Load Realms asynchronously so window startup & instance-close refresh stay instant
        self._realms_req_seq = getattr(self, "_realms_req_seq", 0) + 1
        req_id = self._realms_req_seq

        class _RealmsThread(QtCore.QThread):
            loaded = QtCore.pyqtSignal(int, list)
            def __init__(self_thread, r_id: int, parent=None):
                super().__init__(parent)
                self_thread.r_id = r_id
            def run(self_thread):
                try:
                    from utils import fetch_realms_options
                    self_thread.loaded.emit(self_thread.r_id, list(fetch_realms_options() or []))
                except Exception:
                    self_thread.loaded.emit(self_thread.r_id, [])

        def _on_realms_loaded(r_id: int, realms_list: list):
            if r_id != getattr(self, "_realms_req_seq", 0):
                return
            if not hasattr(self, 'quick_play_combo') or not self.quick_play_combo:
                return
            existing_keys = set()
            for i in range(self.quick_play_combo.count()):
                d = self.quick_play_combo.itemData(i)
                if isinstance(d, dict):
                    existing_keys.add((d.get("type"), str(d.get("id"))))
            for qp in realms_list:
                key = (qp.get("type"), str(qp.get("id")))
                if key in existing_keys:
                    continue
                self.quick_play_combo.addItem(qp['display'], qp)
                idx = self.quick_play_combo.count() - 1
                if qp.get('expired'):
                    item = self.quick_play_combo.model().item(idx)
                    if item:
                        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                elif prev_key and key == prev_key:
                    self.quick_play_combo.setCurrentIndex(idx)

        self._realms_thread = _RealmsThread(req_id, self)
        self._realms_thread.loaded.connect(_on_realms_loaded)
        self._track_and_start_loader_worker(self._realms_thread)

    def _format_log_line(self, line: str) -> str:
        cleaned = re.sub(r'\x1b\[[0-9;?]*[A-Za-z]', '', str(line or ""))
        if getattr(self, 'show_access_token', False):
            return cleaned
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
            _append_plain_log_line(self.results_text, self._format_log_line(raw_line))
        scrollbar = self.results_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        if getattr(self, 'log_window', None) and self.log_window.isVisible():
            self.log_window.refresh()

    def log(self, message):
        clean_msg = str(message or "").rstrip("\r\n").strip()
        if not hasattr(self, '_raw_log_lines'):
            self._raw_log_lines = []
        self._raw_log_lines.append(clean_msg)
        formatted = self._format_log_line(clean_msg)
        _append_plain_log_line(self.results_text, formatted)
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
        raw_sel = self.version_combo.currentData() or getattr(self, "_selected_left_mc", None)
        if not raw_sel:
            self._shortcut_launch_pending = False
            return
        pname = self.get_active_profile_name()
        qp_data = self.quick_play_combo.currentData() if hasattr(self, 'quick_play_combo') else None

        # Automatically resolve dynamic Latest channels ("Latest Release", "Latest Fabric", etc.) & install if missing
        if isinstance(raw_sel, str) and (raw_sel in ("latest", "latest-release", "latest-snapshot") or raw_sel.startswith("latest-loader:")):
            res = resolve_dynamic_version_target(raw_sel, profile_name=pname, available_versions=getattr(self, "_available_versions_cache", None))
            if res.get("kind") == "loader" and not res.get("already_downloaded"):
                mc_v = res.get("mc_version")
                if not mc_v:
                    self._shortcut_launch_pending = False
                    QMessageBox.warning(self, "Version Resolution Failed", f"Could not resolve a compatible Minecraft version for {raw_sel}.")
                    return
                self._selected_left_mc = None
                self.start_batch_install(
                    [{
                        "id": make_install_queue_id(pname, res.get("loader_id", "none"), mc_v, str((res.get("build_entry") or {}).get("version") or "__latest__")),
                        "profile": pname,
                        "loader_id": res.get("loader_id", "none"),
                        "mc_version": mc_v,
                        "build_entry": res.get("build_entry"),
                        "label": f"{pname}: {raw_sel}",
                    }],
                    target_profile=pname,
                    launch_last=True,
                    quick_play=qp_data,
                )
                return
            resolved_ver = res.get("version_id")
            if not resolved_ver:
                self._shortcut_launch_pending = False
                QMessageBox.warning(self, "Version Resolution Failed", f"Could not resolve {raw_sel}.")
                return
            self.selected_version = resolved_ver
        else:
            self.selected_version = str(raw_sel)

        self._selected_left_mc = None

        self.clear_progress_error()
        self.check_opengl_backend_prompt()

        self.selected_profile_name = pname
        self.selected_quick_play = qp_data
        self.is_pipeline_running = True
        self._shortcut_launch_pending = False
        self.status_label.setText(f"Starting launch sequence for Minecraft {self.selected_version}...")
        self.step_progress_bar.setRange(0, 100)
        self.step_progress_bar.setValue(0)
        self.step_progress_bar.setFormat("Starting launch sequence...")
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
        if isinstance(cached_auth, dict) and cached_auth.get("mc_access_token"):
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

        # Step 1: Authentication (tries native Windows sign-in first on Windows, then linked-accounts fallback on close/failure)
        self._begin_auth_flow(from_launch=True)

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

            if obj.get("status") == "pending_oauth_popup":
                url = obj.get("auth_url")
                hint = obj.get("login_hint") or getattr(self, "_pending_login_hint", None)
                self.log("[System] Waiting for user to complete Microsoft login in popup window...")
                self.open_browser(url, login_hint=hint, mode="oauth_popup")
            elif obj.get("status") == "pending_browser_login":
                code = obj.get("user_code") or ""
                url = obj.get("verification_uri") or "https://www.microsoft.com/link"
                self.log(f"[System] Opened default web browser for sign-in ({url}). Code: {code}")
                if code:
                    try:
                        from PyQt6.QtGui import QGuiApplication
                        cb = QGuiApplication.clipboard()
                        if cb:
                            cb.setText(code)
                    except Exception:
                        pass
                    self.status_label.setText(f"Finish signing in in your browser (Code copied: {code})")
                    self.step_progress_bar.setFormat(f"Waiting for browser sign-in... Code: {code}")
                else:
                    self.status_label.setText("Finish signing in in your default web browser...")
                    self.step_progress_bar.setFormat("Waiting for browser sign-in...")
            elif obj.get("status") == "pending_login":
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
                self.open_browser(url, login_hint=hint, user_code=code, mode="device_code")
            elif "mc_username" in obj:
                if getattr(self, "_pending_login_hint", None) and not obj.get("msa_email"):
                    obj["msa_email"] = self._pending_login_hint
                self.auth_data = obj
                self._login_succeeded = True
                target_pid = getattr(self, "_pending_auth_profile", None)
                if target_pid:
                    save_auth_cache_for_profile(target_pid, obj)
                else:
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
        target_pid = getattr(self, "_pending_auth_profile", None)
        if target_pid:
            save_auth_cache_for_profile(target_pid, obj)
        else:
            save_auth_cache(obj)
        self._pending_auth_profile = None
        self.previous_active_profile = None
        self._auth_cancelled_profile = None
        self.logout_btn.setVisible(True)
        self.switch_account_btn.setVisible(True)
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

        if exitCode != 0 or not getattr(self, "_login_succeeded", False) or not self.auth_data:
            self._revert_cancelled_auth_profile()

            if getattr(self, "_wam_fallback_eligible", False) and sys.platform == "win32":
                self._wam_fallback_eligible = False
                from_launch = getattr(self, "_auth_from_launch", False)
                preserve_force = getattr(self, "_auth_was_force_new", False)
                self.log("[System] Windows sign-in did not complete; checking Windows-linked accounts...")
                self.status_label.setText("Select an account or another sign-in method...")
                proceed, selected_win_acc = self._prompt_windows_linked_accounts(
                    header_text=(
                        "Windows sign-in did not complete or was closed.\n"
                        "Would you like to log in with one of these Windows-linked accounts?"
                    )
                )
                if proceed:
                    self._begin_auth_flow(
                        from_launch=from_launch,
                        selected_win_acc=selected_win_acc,
                        force_popup=True,
                        preserve_force_new=preserve_force,
                    )
                    return

            self.is_authenticating = False
            self.step_progress_bar.setVisible(False)
            self.step_progress_bar.setRange(0, 100)
            self.is_pipeline_running = False
            self.setWindowTitle("PyMcLauncher")
            self.log("[Error] Authentication failed or cancelled.")
            self.status_label.setText("Authentication Failed.")
            self.show_progress_error("Authentication", "Login Failed or Cancelled")

            self.auth_data = get_auth_cache()  # Reload reverted profile's auth data
            self.update_profile_banner()  # Show previous account's banner
            self._refresh_auth_buttons()
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

        profile_name = getattr(self, 'selected_profile_name', None) or self.get_active_profile_name()
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
        self._evaluate_game_performance_mode()
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
        self._evaluate_game_performance_mode()
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
        self._evaluate_game_performance_mode()
        try:
            sync_profile_mirror_links_back(inst.get("profile"))
        except Exception:
            pass
        # Refresh Quick Play worlds/servers cleanly after a Minecraft instance closes
        self.setup_quick_play()
        self.refresh_profile_cards()

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
        self._shortcut_launch_pending = False
        self.clear_progress_error()
        self._evaluate_game_performance_mode()
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

        if getattr(self, "_pending_banner_refresh", False) and not getattr(self, "_game_perf_paused", False):
            self._pending_banner_refresh = False
            prev_status = self.status_label.text() if hasattr(self, "status_label") else ""
            self.update_profile_banner()
            if prev_status and prev_status not in ("Loading profile and textures...", "Preparing shortcut launch...", "Profile ready"):
                self.status_label.setText(prev_status)

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
    def open_browser(self, url, login_hint: Optional[str] = None, user_code: Optional[str] = None, mode: str = "device_code"):
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
                    (
                        getattr(self.parent_launcher, 'is_just_logging_in', False)
                        or getattr(self.parent_launcher, 'is_authenticating', False)
                    )
                    and not getattr(self.parent_launcher, '_login_succeeded', False)
                    and not getattr(self.parent_launcher, '_browser_auth_done', False)
                ):
                    proc = getattr(self.parent_launcher, 'process', None)
                    if proc:
                        try:
                            if getattr(self.parent_launcher, '_browser_mode', '') == 'oauth_popup':
                                proc.write(b'{"error": "cancelled"}\n')
                                proc.closeWriteChannel()
                        except Exception:
                            pass
                        proc.kill()
                        try:
                            proc.waitForFinished(1000)
                        except Exception:
                            pass
                super().closeEvent(event)

        self._browser_login_hint = (login_hint or "").strip() or None
        self._browser_user_code = (user_code or "").strip() or None
        self._browser_mode = mode or "device_code"
        self._browser_start_url = url
        self._browser_auth_done = False
        self._initial_load_finished = False
        self.browser_window = LoginBrowserWindow(self)
        if user_code:
            self.browser_window.setWindowTitle(f"Microsoft Login - Code: {user_code} (Copied to Clipboard)")
        else:
            self.browser_window.setWindowTitle("Microsoft Login - Minecraft Launcher")
        self.browser_window.resize(600, 780)
        self.browser_window.setMinimumSize(520, 660)
        
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

        browser_helper_bar = QWidget()
        browser_helper_bar.setStyleSheet("background-color: #252526; border-bottom: 1px solid #3c3c3c;")
        helper_layout = QHBoxLayout(browser_helper_bar)
        helper_layout.setContentsMargins(12, 6, 12, 6)
        helper_layout.setSpacing(8)
        default_helper_text = (
            "Using a phone or iCloud passkey? Click 'Open in Default Browser'."
            if os.name != "nt"
            else "Trouble with passkey or sign-in? You can switch to your default browser."
        )
        self.browser_helper_label = QLabel(default_helper_text)
        self.browser_helper_label.setWordWrap(True)
        self.browser_helper_label.setStyleSheet("color: #d4d4d4; font-size: 12px;")
        helper_layout.addWidget(self.browser_helper_label, stretch=1)
        self.browser_use_pwd_btn = QPushButton("Use Password")
        self.browser_use_pwd_btn.setVisible(False)
        self.browser_use_pwd_btn.setStyleSheet(
            "background-color: #4a4a4d; color: white; font-weight: bold; "
            "border-radius: 4px; padding: 5px 12px; font-size: 12px;"
        )
        self.browser_use_pwd_btn.clicked.connect(self._abort_webauthn_for_password)
        helper_layout.addWidget(self.browser_use_pwd_btn)
        open_ext_btn = QPushButton("Open in Default Browser")
        open_ext_btn.setStyleSheet(
            "background-color: #0078D7; color: white; font-weight: bold; "
            "border-radius: 4px; padding: 5px 12px; font-size: 12px;"
        )
        open_ext_btn.clicked.connect(self._switch_to_default_browser_login)
        helper_layout.addWidget(open_ext_btn)
        main_layout.addWidget(browser_helper_bar)

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
        active_id = get_active_profile_id()

        if not hasattr(self, '_profile_cache'):
            self._profile_cache = {}
        if not self._browser_login_hint:
            # Off-the-record profile so prior sessions cannot silently log in the wrong user
            if "__ephemeral__" not in self._profile_cache:
                self._profile_cache["__ephemeral__"] = QWebEngineProfile()
            self.web_profile = self._profile_cache["__ephemeral__"]
            try:
                self.web_profile.cookieStore().deleteAllCookies()
            except Exception:
                pass
        else:
            profile_path = os.path.abspath(f".cache/{active_id}/browser_cache")
            os.makedirs(profile_path, exist_ok=True)
            if active_id not in self._profile_cache:
                p = QWebEngineProfile(active_id)
                p.setPersistentStoragePath(profile_path)
                p.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
                self._profile_cache[active_id] = p
            self.web_profile = self._profile_cache[active_id]
        
        from PyQt6.QtWebEngineCore import QWebEngineScript
        from ms_accounts import build_webauthn_js_hook
        script = QWebEngineScript()
        script.setSourceCode(build_webauthn_js_hook(auto_fallback_ms=3500 if os.name != "nt" else 0))
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
                    btn = getattr(self.parent_gui, "browser_use_pwd_btn", None)
                    if btn:
                        btn.setVisible(True)
                    if os.name == "nt":
                        self.parent_gui._set_webauthn_loading(js_active=True)
                    else:
                        self.parent_gui._set_browser_helper_notice(
                            "Passkey / security key check active. If no prompt opens, click 'Use Password' above or 'Open in Default Browser'.",
                            warning=False,
                        )
                    return
                if message == "AGY_WEBAUTHN_SUCCESS":
                    btn = getattr(self.parent_gui, "browser_use_pwd_btn", None)
                    if btn:
                        btn.setVisible(False)
                    if os.name == "nt":
                        self.parent_gui._webauthn_completed = True
                        self.parent_gui._set_webauthn_loading(js_active=True)
                        _safe_widget_delay(self.parent_gui, 6000, self.parent_gui._clear_stuck_webauthn_loading)
                    return
                if message in ("AGY_WEBAUTHN_AUTO_FALLBACK", "AGY_WEBAUTHN_END", "AGY_WEBAUTHN_CANCEL"):
                    btn = getattr(self.parent_gui, "browser_use_pwd_btn", None)
                    if btn:
                        btn.setVisible(False)
                    self.parent_gui._webauthn_completed = False
                    self.parent_gui._set_webauthn_loading(js_active=False)
                    if os.name != "nt":
                        self.parent_gui._set_browser_helper_notice(
                            "Passkey couldn't open in the embedded window. Choose 'Other ways to sign in' / Password below, or click 'Open in Default Browser'.",
                            warning=True,
                        )
                    return
                super().javaScriptConsoleMessage(level, message, lineNumber, sourceID)
                
        self.web_page = LoginWebPage(self.web_profile, self)
        try:
            from ms_accounts import attach_webauthn_ux_handler
            attach_webauthn_ux_handler(
                self.web_page,
                parent_window=self.browser_window,
                on_switch_browser=self._switch_to_default_browser_login,
            )
        except Exception as e:
            self.log(f"[System] Optional Qt WebAuthn dialog setup skipped: {e}")
        
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

        if os.name == "nt":
            # Poll for native Windows Security (WebAuthn / Passkey / Security Key) modal dialog
            self._webauthn_poll_timer = QTimer(self.browser_window)
            self._webauthn_poll_timer.setInterval(120)
            self._webauthn_poll_timer.timeout.connect(self._poll_windows_webauthn_dialog)
            self._webauthn_poll_timer.start()
        else:
            self._webauthn_poll_timer = None

        self.browser_window.setCentralWidget(self.browser_central)
        self.loading_widget.show()
        self.loading_widget.raise_()
        _safe_widget_delay(self.browser_window, 8000, self._clear_stuck_initial_loading)
        self.browser_window.show()

    def _abort_webauthn_for_password(self):
        btn = getattr(self, "browser_use_pwd_btn", None)
        if btn:
            try:
                btn.setVisible(False)
            except RuntimeError:
                pass
        page = getattr(self, "web_page", None)
        if page:
            try:
                page.runJavaScript(
                    "window.__agy_webauthn_ux_active = false;"
                    "window.__agy_abort_webauthn && window.__agy_abort_webauthn('Switched to password sign-in.');"
                )
            except RuntimeError:
                pass

    def _set_browser_helper_notice(self, text: str, warning: bool = False):
        lbl = getattr(self, "browser_helper_label", None)
        if not lbl:
            return
        try:
            lbl.setText(text)
            if warning:
                lbl.setStyleSheet("color: #ffd479; font-size: 12px; font-weight: bold;")
            else:
                lbl.setStyleSheet("color: #d4d4d4; font-size: 12px;")
        except RuntimeError:
            pass

    def _clear_stuck_initial_loading(self):
        if getattr(self, "_browser_page_loading", False) and not getattr(self, "_initial_load_finished", False):
            self._browser_page_loading = False
            self._set_webauthn_loading()

    def _clear_stuck_webauthn_loading(self):
        if getattr(self, "_webauthn_completed", False) and not getattr(self, "_browser_auth_done", False):
            self._webauthn_completed = False
            self._webauthn_js_active = False
            self._set_webauthn_loading()

    def _switch_to_default_browser_login(self):
        if getattr(self, "_browser_auth_done", False):
            return
        self._browser_auth_done = True
        mode = getattr(self, "_browser_mode", "")
        proc = getattr(self, "process", None)
        if mode == "oauth_popup" and proc:
            self.log("[System] Switching Microsoft sign-in to default web browser...")
            self.status_label.setText("Opening your default web browser for sign-in...")
            self.step_progress_bar.setFormat("Opening default web browser...")
            try:
                proc.write(b'{"switch_to_browser": true}\n')
                proc.closeWriteChannel()
            except Exception as e:
                self.log(f"[Error] Could not switch popup to browser mode: {e}")
            self._cleanup_browser()
            return

        # Fallback when already in device_code mode
        target_url = getattr(self, "_browser_start_url", None) or "https://www.microsoft.com/link"
        try:
            from PyQt6.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl(target_url))
        except Exception:
            pass
        self._cleanup_browser()

    def _on_browser_load_started(self):
        if getattr(self, "_initial_load_finished", False):
            return
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
        self._initial_load_finished = False
        self._browser_start_url = None
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
            self.loading_widget = None
            self.browser_helper_label = None
            
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
        self._initial_load_finished = True
        self._browser_page_loading = False
        self._set_webauthn_loading()
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

                        var consentBtn = document.querySelector('#acceptButton, [data-testid="appConsentPrimaryButton"]');
                        if (consentBtn && consentBtn.offsetParent !== null) {{
                            consentBtn.click();
                            return;
                        }}

                        var isKmsiPage = Boolean(document.querySelector('#KmsiDescription, #idDiv_Kmsi, input[name="DontShowAgain"]'));
                        if (isKmsiPage) {{
                            var yesBtn = document.querySelector('#idSIButton9, input[type=submit], button[type=submit]');
                            if (yesBtn && yesBtn.offsetParent !== null && yesBtn.value !== 'Cancel' && yesBtn.innerText !== 'Cancel') {{
                                yesBtn.click();
                            }}
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
        import urllib.parse
        url_str = url.toString()
        parsed = urllib.parse.urlparse(url_str)
        is_desktop_cb = (
            parsed.netloc.lower() == "login.live.com"
            and parsed.path.rstrip("/") == "/oauth20_desktop.srf"
        )
        qs_params = urllib.parse.parse_qs(parsed.query)
        if not qs_params and parsed.fragment:
            qs_params = urllib.parse.parse_qs(parsed.fragment)
        has_oauth_result = is_desktop_cb and ("code" in qs_params or "error" in qs_params)

        if has_oauth_result or "login.live.com/undefined" in url_str:
            if getattr(self, "_browser_auth_done", False):
                return
            self._browser_auth_done = True
            if getattr(self, "_browser_mode", "") == "oauth_popup" and getattr(self, "process", None):
                try:
                    payload = (json.dumps({"redirect_url": url_str}) + "\n").encode("utf-8")
                    self.process.write(payload)
                    self.process.closeWriteChannel()
                except Exception:
                    pass
            if self.browser_window:
                self.browser_window.hide()
            self.step_progress_bar.setVisible(True)
            self.step_progress_bar.setRange(0, 100)
            self.step_progress_bar.setValue(40)
            self.step_progress_bar.setFormat("Completing Microsoft Sign-In... (%p%)")
            self.status_label.setText("Completing sign-in and fetching Minecraft profile...")
            QApplication.processEvents()

if __name__ == '__main__':
    if '--internal-ms-auth' in sys.argv[1:]:
        sys.argv = [sys.argv[0]] + [a for a in sys.argv[1:] if a != '--internal-ms-auth']
        try:
            import ms_accounts
        except ImportError:
            from backend import ms_accounts
        sys.exit(ms_accounts.main() or 0)

    if '--internal-update' in sys.argv[1:]:
        rem_args = [a for a in sys.argv[1:] if a != '--internal-update']
        try:
            import launcher_update
        except ImportError:
            from backend import launcher_update
        sys.exit(launcher_update.run_cli(rem_args) or 0)

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
    app.setApplicationName("PyMcLauncher")
    app.setApplicationDisplayName("PyMcLauncher")
    app.setOrganizationName("cloudyhair6")
    try:
        import launcher_update as _lu_meta
        app.setApplicationVersion(getattr(_lu_meta, "DEFAULT_LAUNCHER_VERSION", "v3.1"))
    except Exception:
        pass
    apply_launcher_theme()
    try:
        app.setWindowIcon(get_pymclauncher_icon())
    except Exception:
        pass
    
    window = LauncherGUI(startup_args=cli_args)
    window.show()
    sys.exit(app.exec())
