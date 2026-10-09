import sys
import json
import os
from pathlib import Path

# Suppress harmless Chromium hardware acceleration warnings
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-logging"

from PyQt6 import QtCore
from PyQt6.QtCore import Qt, QUrl, QUrlQuery, QProcess, QTimer, QRect, QEvent, QTime
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QGridLayout, 
                             QLabel, QProgressBar, QTextEdit, QHBoxLayout, QPushButton, QMessageBox, 
                             QStackedWidget, QComboBox, QDialog, QCheckBox, QStyledItemDelegate, QStyle, QFileDialog, QGroupBox, QRadioButton,
                             QListWidget, QListWidgetItem)
from PyQt6.QtGui import QFont, QIcon, QPalette, QColor, QImage, QPainter, QPixmap, QMouseEvent, QCursor
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEngineProfile
from utils import (
    get_dir,get_available_versions, get_downloaded_versions,  
                   delete_version_files, load_launcher_config, 
                   save_launcher_config, get_auth_cache, save_auth_cache, clear_auth_cache, get_active_profile_id, load_accounts, save_accounts, 
                   has_any_downloads, delete_all_downloads, fetch_player_textures)




from PyQt6.QtWidgets import QFileDialog, QFormLayout, QLineEdit

from PyQt6.QtWidgets import QInputDialog, QMessageBox

class DirectoriesDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Custom Directories")
        self.setFixedSize(500, 420)
        
        self.config = load_launcher_config()
        self.profiles = self.config.get("profiles", {
            "Default": {
                "base": "minecraft_downloads",
                "assets": "minecraft_downloads/assets",
                "libraries": "minecraft_downloads/libraries",
                "versions": "minecraft_downloads/versions",
                "java": "minecraft_downloads/java",
                "saves": "minecraft_downloads/saves",
                "indexes": "minecraft_downloads/indexes"
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
        for key in ["assets", "libraries", "versions", "java", "saves", "indexes"]:
            row_layout = QHBoxLayout()
            inp = QLineEdit()
            btn = QPushButton("Browse...")
            btn.clicked.connect(lambda checked=False, k=key, i=inp: self.browse_dir(k, i))
            row_layout.addWidget(inp)
            row_layout.addWidget(btn)
            self.inputs[key] = inp
            self.form_layout.addRow(f"{key.capitalize()}:", row_layout)
            
        layout.addLayout(self.form_layout)
        
        
        # Hide Release Date
        self.hide_date_cb = QCheckBox("Hide release date in version list")
        self.hide_date_cb.setChecked(hide_release_date)
        layout.addWidget(self.hide_date_cb)
        
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
        new_base = new_base.replace('\\\\', '/')
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

class VersionOptionsDialog(QDialog):
    def __init__(self, current_filters, show_downloaded_only, skip_verification, show_console, remember_version, hide_release_date, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Version Options")
        self.setFixedSize(300, 350)
        self.setStyleSheet(parent.styleSheet())
        layout = QVBoxLayout(self)
        
        self.filters = current_filters
        self.show_downloaded_only = show_downloaded_only
        self.skip_verification = skip_verification
        self.show_console = show_console
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
        
        # Remember Version
        self.remember_cb = QCheckBox("Remember last played version")
        self.remember_cb.setChecked(remember_version)
        layout.addWidget(self.remember_cb)
        
        btn_layout = QHBoxLayout()
        
        # Delete All Downloads button
        if has_any_downloads():
            self.delete_all_btn = QPushButton("Delete All Downloads")
            self.delete_all_btn.setStyleSheet("color: red; font-weight: bold;")
            self.delete_all_btn.clicked.connect(self.on_delete_all_clicked)
            btn_layout.addWidget(self.delete_all_btn)
            
        ok_btn = QPushButton("Save")
        ok_btn.clicked.connect(self.accept)
        btn_layout.addStretch()
        btn_layout.addWidget(ok_btn)
        layout.addLayout(btn_layout)
        
    def on_delete_all_clicked(self):
        reply = QMessageBox.question(self, 'Confirm Delete All', 
                                     "Are you sure you want to delete ALL downloaded Minecraft versions, assets, and libraries?\n\nThis will free up space but you will need to re-download everything to play.\n\n(This will NOT delete your saves, worlds, or configurations)",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                     QMessageBox.StandardButton.No)
        
        if reply == QMessageBox.StandardButton.Yes:
            self.parent().log("")
            self.parent().log("=== Starting Deletion ===")
            self.parent().step_progress_bar.setFormat("Deleting All Downloads...")
            
            def delete_progress(percent, status):
                self.parent().task_progress_bar.setValue(int(percent))
                self.parent().status_label.setText(status)
                self.parent().log(status)
                QApplication.processEvents()
                
            if delete_all_downloads(progress_callback=delete_progress):
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

    def get_remember_version(self):
        return self.remember_cb.isChecked()

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
            window.skinViewer.playerObject.backEquipment = 'cape';
            window.skinViewer.playerObject.rotation.x = 0;
            window.skinViewer.playerObject.rotation.y = 0;
            window.skinViewer.playerObject.position.y = 0;
        """
    elif anim_type == "walk":
        inner = """
            window.skinViewer.animation = new skinview3d.WalkingAnimation();
            window.skinViewer.playerObject.backEquipment = 'cape';
            window.skinViewer.playerObject.rotation.x = 0;
            window.skinViewer.playerObject.rotation.y = 0;
            window.skinViewer.playerObject.position.y = 0;
        """
    elif anim_type == "swim":
        inner = """
            window.skinViewer.playerObject.backEquipment = 'cape';
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
                if ("{c_path}") {{ window.skinViewer.loadCape("{c_path}", {{ backEquipment: "{back_eq}" }}); }} else {{ window.skinViewer.loadCape(null); }}
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
            self.login_btn.setVisible(True)
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
                        self.login_btn.setVisible(True)
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
                        self.login_btn.setVisible(True)
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
    def __init__(self, color_hex, parent=None):
        super().__init__(parent)
        self.setFixedHeight(60)
        self.setMouseTracking(True)
        self.dot_x = 0
        self.hovering = False
        self.mouse_y = 0  # Global mouse Y relative to the bar
        self.color = QColor(color_hex)
        
        self.track_timer = QTimer(self)
        self.track_timer.timeout.connect(self._track_mouse)
        self.track_timer.start(16)
        
        self.anim_timer = QTimer(self)
        self.anim_timer.timeout.connect(self.update)
        self.anim_timer.start(50)

        import os
        import json
        bg_path = os.path.join("files", "locator_bar", "locator_bar_background.png")
        dot_path = os.path.join("files", "locator_bar", "locator_bar_dot", "default_0.png")
        arrow_up_path = os.path.join("files", "locator_bar", "locator_bar_arrow_up.png")
        arrow_down_path = os.path.join("files", "locator_bar", "locator_bar_arrow_down.png")

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
            except: pass

        def load_arrow_meta(path):
            frames = [{"index": 0, "time": 10}, {"index": 1, "time": 4}]
            frame_height = 5
            if os.path.exists(path + ".mcmeta"):
                try:
                    with open(path + ".mcmeta", "r") as f:
                        meta = json.load(f)
                        anim = meta.get("animation", {})
                        if "frames" in anim: frames = anim["frames"]
                        if "height" in anim: frame_height = anim["height"]
                except: pass
            # time is in ticks, 1 tick = 50ms
            total_duration = sum([f.get("time", 1) * 50 for f in frames])
            return frames, frame_height, total_duration

        self.arrow_up_meta = load_arrow_meta(arrow_up_path)
        self.arrow_down_meta = load_arrow_meta(arrow_down_path)

        # Helper to load and tint images
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
                                int(pixel.red() * self.color.red() / 255),
                                int(pixel.green() * self.color.green() / 255),
                                int(pixel.blue() * self.color.blue() / 255),
                                pixel.alpha()
                            )
                            raw_img.setPixelColor(x, y, tinted)
            scale_factor = 3
            return QPixmap.fromImage(raw_img).scaled(
                raw_img.width() * scale_factor, raw_img.height() * scale_factor,
                Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation)

        self.dot_pixmap = load_tinted_pixmap(dot_path)
        self.arrow_up_pixmap = load_tinted_pixmap(arrow_up_path, is_arrow=True)
        self.arrow_down_pixmap = load_tinted_pixmap(arrow_down_path, is_arrow=True)

    def _track_mouse(self):
        global_pos = QCursor.pos()
        widget = QApplication.widgetAt(global_pos)
        
        old_dot_x, old_mouse_y, old_hover = self.dot_x, self.mouse_y, self.hovering
        
        if widget and widget.window() == self.window():
            local_pos = self.mapFromGlobal(global_pos)
            self.dot_x = local_pos.x()
            self.mouse_y = local_pos.y()
            self.hovering = (0 <= self.mouse_y <= self.height()) and (0 <= self.dot_x <= self.width())
        else:
            self.dot_x = -100
            self.mouse_y = -100
            self.hovering = False
            
        if self.dot_x != old_dot_x or self.mouse_y != old_mouse_y or self.hovering != old_hover:
            self.update()
        
    def paintEvent(self, event):
        painter = QPainter(self)
        if not self.bg_pixmap.isNull():
            bg_scale = 3
            scaled_bg = self.bg_pixmap.scaled(
                self.bg_pixmap.width() * bg_scale, 
                self.bg_pixmap.height() * bg_scale,
                Qt.AspectRatioMode.IgnoreAspectRatio, 
                Qt.TransformationMode.FastTransformation
            )
            
            h = scaled_bg.height()
            y_offset = (self.height() - h) // 2
            
            left_cap_w = 2 * bg_scale
            right_cap_w = 2 * bg_scale
            
            # Left cap
            painter.drawPixmap(0, y_offset, scaled_bg.copy(0, 0, left_cap_w, h))
            
            # Center (stretch just a single 1-pixel wide column to create a perfectly smooth, uniform bar)
            center_src_x = 5 * bg_scale
            center_src_w = 1 * bg_scale
            center_rect = QRect(left_cap_w, y_offset, self.width() - left_cap_w - right_cap_w, h)
            center_pixmap = scaled_bg.copy(center_src_x, 0, center_src_w, h)
            # Stretch the 1-pixel column across the center to eliminate tiling patterns
            painter.drawPixmap(center_rect, center_pixmap)
            
            # Right cap
            painter.drawPixmap(self.width() - right_cap_w, y_offset, scaled_bg.copy(scaled_bg.width() - right_cap_w, 0, right_cap_w, h))
            
        dot_center_x = self.dot_x
        if (0 <= self.dot_x <= self.width()):
            # Always draw dot if mouse X is within bounds
            if not self.dot_pixmap.isNull():
                dot_w = self.dot_pixmap.width()
                dot_h = self.dot_pixmap.height()
                draw_x = max(0, min(self.width() - dot_w, int(self.dot_x - dot_w / 2)))
                draw_y = (self.height() - dot_h) // 2
                painter.drawPixmap(draw_x, draw_y, self.dot_pixmap)
                dot_center_x = draw_x + dot_w / 2.0

            # Draw animated arrow if mouse is outside vertically
            if not self.hovering:
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
                    return frame_idx, frame_height * 3

                if self.mouse_y < 0 and hasattr(self, 'arrow_up_pixmap') and not self.arrow_up_pixmap.isNull():
                    frame_idx, arr_h = get_arrow_frame(self.arrow_up_meta, t)
                    arr_w = self.arrow_up_pixmap.width()
                    src_rect = QRect(0, frame_idx * arr_h, arr_w, arr_h)
                    arrow_draw_x = int(dot_center_x - arr_w / 2.0)
                    arrow_draw_y = y_offset - arr_h - 2
                    painter.drawPixmap(arrow_draw_x, arrow_draw_y, self.arrow_up_pixmap, src_rect.x(), src_rect.y(), src_rect.width(), src_rect.height())
                elif self.mouse_y > self.height() and hasattr(self, 'arrow_down_pixmap') and not self.arrow_down_pixmap.isNull():
                    frame_idx, arr_h = get_arrow_frame(self.arrow_down_meta, t)
                    arr_w = self.arrow_down_pixmap.width()
                    src_rect = QRect(0, frame_idx * arr_h, arr_w, arr_h)
                    arrow_draw_x = int(dot_center_x - arr_w / 2.0)
                    arrow_draw_y = y_offset + h + 2
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
        self.setMinimumHeight(44)
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

        left_layout.addSpacing(-30)
        
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
        mc_logo_path = "files/minecraft-logo-svg-vector.svg"
        if os.path.exists(mc_logo_path):
            mc_logo = QLabel()
            mc_logo_pix = QPixmap(mc_logo_path).scaled(32, 32, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            mc_logo.setPixmap(mc_logo_pix)
            mc_name_layout.addWidget(mc_logo)

        java_layout.addLayout(mc_name_layout)

        locator_color = auth_data.get('locator_bar_color', '#FFFFFF')
        color_label = QLabel(f"Your locator bar color is: {locator_color}")
        color_label.setFont(QFont("Segoe UI", 9))
        java_layout.addWidget(color_label, alignment=Qt.AlignmentFlag.AlignCenter)

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
        if os.path.exists("files/icons8-xbox-24.svg"):
            xbox_logo = QLabel()
            xbox_logo_pix = QPixmap("files/icons8-xbox-24.svg").scaled(18, 18, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
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
            gamerscore_icon_path = "files/xbox-gamerscore.svg"
            if os.path.exists(gamerscore_icon_path):
                gs_logo = QLabel()
                gs_logo_pix = QPixmap(gamerscore_icon_path).scaled(14, 14, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
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
            pixmap = QPixmap(textures['gamerpic']).scaled(50, 50, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            pic_label.setPixmap(pixmap)
            right_layout.addWidget(pic_label)

        self.right_widget.setVisible(self.options.get("show_xbox_profile", True))
        top_layout.addWidget(self.right_widget)
        main_layout.addLayout(top_layout)
        
        # 5. Bottom: Interactive Locator Bar spanning full width
        self.locator_bar = LocatorBarWidget(locator_color)
        self.locator_bar.setVisible(self.options.get("show_locator_bar", True))
        main_layout.addWidget(self.locator_bar)


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
                if ("{c_data}") {{ window.skinViewer.loadCape("{c_data}", {{ backEquipment: "{back_eq}" }}); }} else {{ window.skinViewer.loadCape(null); }}
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
            os.makedirs(".cache", exist_ok=True)
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
            cache_dir = f".cache/{pid}/minecraft-auth-cache"
            if os.path.exists(cache_dir):
                try: shutil.rmtree(cache_dir)
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


class LauncherGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PyMcLauncher")

        import socket
        try:
            socket.create_connection(("api.minecraftservices.com", 443), timeout=2)
            self.is_offline = False
        except OSError:
            self.is_offline = True
        
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
        prof_layout.addWidget(QLabel("Profile:"))
        prof_layout.addWidget(self.profile_combo, stretch=1)
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
        self.remember_version = config.get("remember_version", False)
        self.last_played_version = config.get("last_played_version", None)

        # Update Banner
        self.step_progress_bar = QProgressBar()
        self.step_progress_bar.setRange(0, 100)
        self.step_progress_bar.setValue(0)
        self.step_progress_bar.setVisible(False)
        self.layout.addWidget(self.step_progress_bar)

        self.task_progress_bar = QProgressBar()
        self.task_progress_bar.setRange(0, 100)
        self.task_progress_bar.setValue(0)
        self.task_progress_bar.setFormat("Current Task: %p%")
        self.task_progress_bar.setVisible(False)
        self.layout.addWidget(self.task_progress_bar)
        
        self.update_profile_banner()

        # Console Output
        self.results_text = QTextEdit()
        self.results_text.setReadOnly(True)
        self.results_text.setVisible(self.show_console)
        self.layout.addWidget(self.results_text)

        # Status Label
        self.status_label = QLabel("Ready to launch.")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.status_label.setMinimumWidth(610) # Forces layout sizeHint width to 650 (610 + 20*2 margins)
        self.layout.addWidget(self.status_label)
        
        self.warning_label = QLabel("Warning: This specific version will not automatically update.")
        self.warning_label.setStyleSheet("color: #cc8800; font-style: italic;")
        self.warning_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.warning_label.setVisible(False)
        self.layout.addWidget(self.warning_label)

        # Play Button and Delete Button Layout
        self.action_layout = QHBoxLayout()
        self.play_button = QPushButton("PLAY MINECRAFT")
        self.play_button.clicked.connect(self.start_launch_sequence)
        
        self.delete_button = QPushButton("Delete")
        self.delete_button.clicked.connect(self.delete_selected_version)
        self.delete_button.setVisible(False)
        
        self.setup_quick_play()
        self.action_layout.addWidget(self.play_button, stretch=4)
        self.action_layout.addWidget(self.delete_button, stretch=1)
        self.layout.addLayout(self.action_layout)
        
        # Dummy widget to enforce minimum layout width of 610 (plus 40 margins = 650 window width)
        self.dummy_width_widget = QWidget()
        self.dummy_width_widget.setFixedSize(610, 0)
        self.layout.addWidget(self.dummy_width_widget)

        self.browser_window = None
        self.process = None
        self.auth_data = None
        self.is_just_logging_in = False
        
        # Pipeline State
        self.pipeline_steps = []
        self.current_step = 0
        self.selected_version = "1.21.1"
        
        self.version_filters = config.get("version_filters", ["release"])
        self.show_downloaded_only = config.get("show_downloaded_only", False)
        self.skip_verification = config.get("skip_verification", True)
        
        self.load_versions()

        # Start a silent background refresh to update skin/presence
        if is_logged_in and not is_expired:
            self.refresh_process = QProcess()
            self.refresh_process.setProgram(sys.executable)
            self.refresh_process.setArguments(["get_mc_username.py", "--json", "--profile", get_active_profile_id()])
            self.refresh_process.readyReadStandardOutput.connect(self.handle_refresh_stdout)
            self.refresh_process.start()

        # Run startup garbage collection after UI is shown
        QTimer.singleShot(100, self.run_startup_garbage_collection)

    def run_startup_garbage_collection(self):
        try:
            from garbage_collect import perform_garbage_collection
            perform_garbage_collection(log_callback=self.log)
        except Exception as e:
            self.log(f"Error during startup garbage collection: {e}")

    def closeEvent(self, event):
        if getattr(self, 'process', None) and self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.kill()
        if getattr(self, 'refresh_process', None) and self.refresh_process.state() != QProcess.ProcessState.NotRunning:
            self.refresh_process.kill()
        event.accept()

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
            return
        is_downloaded = self.version_combo.itemData(index, Qt.ItemDataRole.UserRole + 1)
        self.delete_button.setVisible(bool(is_downloaded))
        if hasattr(self, 'warning_label'):
            self.warning_label.setVisible(self.remember_version)
        
    def delete_selected_version(self):
        version_id = self.version_combo.currentData()
        if not version_id: return
        
        reply = QMessageBox.question(self, 'Confirm Delete', 
                                     f"Are you sure you want to delete Minecraft {version_id}?",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                     QMessageBox.StandardButton.No)
        
        if reply == QMessageBox.StandardButton.Yes:
            self.log(f"[System] Deleting version {version_id}...")
            if delete_version_files(version_id, log_callback=self.log):
                self.log(f"[System] Successfully deleted Minecraft {version_id}.")
                self.load_versions()
            else:
                self.log(f"[Error] Failed to delete Minecraft {version_id}.")

    def logout(self):
        reply = QMessageBox.question(self, 'Confirm Logout', 
                                     "Are you sure you want to logout? You will need to sign in with Microsoft again.",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self.set_progress_visible(True)
            self.step_progress_bar.setFormat("Logging out...")
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
            self.step_progress_bar.setFormat("")
            self.step_progress_bar.setValue(0)
            self.task_progress_bar.setFormat("")
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
                    self.banner_widget.setVisible(False)
            self.set_progress_visible(False)
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
                    self.banner_widget.setVisible(False)
            
            if dialog.wants_new_account:
                # Clear banner for new account before logging in
                while self.banner_container.count():
                    item = self.banner_container.takeAt(0)
                    if item.widget():
                        item.widget().setParent(None)
                        item.widget().deleteLater()
                self._force_new_login = True
                self.do_login()

    def do_login(self):
        self.log("Starting Microsoft authentication...")
        self.is_just_logging_in = True
        self._login_succeeded = False

        self.status_label.setText("Authenticating...")
        self.play_button.setEnabled(False)
        self.delete_button.setEnabled(False)
        self.login_btn.setEnabled(False)
        
        self.step_progress_bar.setRange(0, 0)
        self.step_progress_bar.setVisible(True)

        current_profile = get_active_profile_id()
        force = getattr(self, "_force_new_login", False)
        if force:
            self.previous_active_profile = current_profile
            self._auth_cancelled_profile = get_active_profile_id(True)
        else:
            self.previous_active_profile = None
            self._auth_cancelled_profile = current_profile

        self.process = QProcess()
        self.process.setProgram(sys.executable)
        self.process.setArguments(["get_mc_username.py", "--json", "--profile", self._auth_cancelled_profile])
        self._force_new_login = False
        self.process.readyReadStandardOutput.connect(self.handle_auth_stdout)
        self.process.readyReadStandardError.connect(self.handle_stderr)
        self.process.finished.connect(self.auth_finished)
        self.process.start()

    def update_profile_banner(self):
        # Clear existing banner
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
                self.step_progress_bar.setFormat(msg)
                QApplication.processEvents()

            self.banner_widget = ProfileBannerWidget(auth_data, self, progress_callback=banner_progress)
            self.banner_container.addWidget(self.banner_widget)
            
            # Hide the progress bar after it's done loading, unless a download pipeline started
            if not getattr(self, 'task_progress_bar', None) or not self.task_progress_bar.isVisible():
                self.step_progress_bar.setVisible(False)

        self.update_window_size()

    def update_window_size(self):
        # We exclusively use SetFixedSize constraint so the window flawlessly shrink-wraps its contents
        # without any arbitrary gaps or cutoff content. The dummy_width_widget guarantees width=650.
        
        if self.show_console:
            self.setMinimumHeight(0)
            self.setMaximumHeight(16777215)
            # Give the console a comfortable minimum height so it's readable
            if hasattr(self, 'results_text'):
                self.results_text.setMinimumHeight(300)
            self.layout.setSizeConstraint(QVBoxLayout.SizeConstraint.SetFixedSize)
            self.layout.setContentsMargins(20, 20, 20, 35)
        else:
            self.setMinimumHeight(0)
            self.setMaximumHeight(16777215)
            if hasattr(self, 'results_text'):
                self.results_text.setMinimumHeight(0)
            self.layout.setSizeConstraint(QVBoxLayout.SizeConstraint.SetFixedSize)
            self.layout.setContentsMargins(20, 20, 20, 35)
            
        # Force layout evaluation and explicitly resize to apply constraints properly
        self.layout.activate()
        QApplication.processEvents()
        self.adjustSize()
        self.resize(self.sizeHint())

    def load_versions(self):
        old_selected = self.version_combo.currentData()
        if old_selected is None and self.remember_version and self.last_played_version:
            old_selected = self.last_played_version

        self.version_combo.clear()
        QApplication.processEvents()

        versions = get_available_versions()
        downloaded = get_downloaded_versions()

        if self.show_downloaded_only and not downloaded:
            self.show_downloaded_only = False
            config = load_launcher_config()
            config["show_downloaded_only"] = False
            save_launcher_config(config)

        count = 0
        if not versions:
            self.is_offline = True
            for v_id in downloaded:
                self.version_combo.addItem(f"{v_id} (Offline)", v_id)
                idx = self.version_combo.count() - 1
                self.version_combo.setItemData(idx, True, Qt.ItemDataRole.UserRole + 1)
                count += 1
            if hasattr(self, 'play_button'):
                self.play_button.setText("PLAY MINECRAFT OFFLINE")
            if count > 0:
                self.results_text.append(f"[System] Offline mode. Loaded {count} downloaded versions.")
            else:
                self.results_text.append("[System] Offline mode. No downloaded versions available to play.")
        else:
            self.is_offline = False
            if hasattr(self, 'play_button'):
                auth_data = get_auth_cache()
                if auth_data and auth_data.get("owns_minecraft") is False:
                    self.play_button.setText("PLAY MINECRAFT TRIAL")
                else:
                    self.play_button.setText("PLAY MINECRAFT")
            config = load_launcher_config()
            hide_date = config.get("hide_release_date", False)
            for v_id, v_type, v_date in versions:
                if v_type not in self.version_filters:
                    continue
                is_downloaded = v_id in downloaded
                if self.show_downloaded_only and not is_downloaded:
                    continue

                type_display = v_type.replace('_', ' ').title()
                if hide_date:
                    text = f"{v_id} ({type_display})"
                else:
                    text = f"{v_id} ({type_display} - {v_date})"
                    
                if is_downloaded:
                    text += " [Downloaded]"
                    
                self.version_combo.addItem(text, v_id)

                count += 1
            self.results_text.append(f"[System] Loaded {count} versions.")

        if count > 0:
            idx_to_select = 0
            if old_selected:
                for i in range(self.version_combo.count()):
                    # .itemData(i) actually returns True if role is UserRole+1, wait, data is v_id which is default role (UserRole).
                    # Actually, version_combo.itemData(i) gets the UserRole data, which is v_id
                    if self.version_combo.itemData(i) == old_selected:
                        idx_to_select = i
                        break
            self.version_combo.setCurrentIndex(idx_to_select)

        # Ensure UI updates to reflect the initially selected version
        self.on_version_changed(self.version_combo.currentIndex())

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
        downloaded = get_downloaded_versions()
        dialog = VersionOptionsDialog(self.version_filters, self.show_downloaded_only, self.skip_verification, self.show_console, self.remember_version, hide_release_date, self)

        
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
            
            self.remember_version = dialog.get_remember_version()
            self.hide_release_date = dialog.get_hide_release_date()

            self.results_text.setVisible(self.show_console)

            # Save to config
            config = load_launcher_config()
            config["version_filters"] = self.version_filters
            config["show_downloaded_only"] = self.show_downloaded_only
            config["skip_verification"] = self.skip_verification
            config["show_console"] = self.show_console
            config["remember_version"] = self.remember_version
            config["hide_release_date"] = self.hide_release_date
            save_launcher_config(config)
            
            self.load_versions()

            
            self.update_profile_banner()
            self.load_versions()
            
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
            for qp in get_quick_play_options(get_dir("base")):
                self.quick_play_combo.addItem(qp['display'], qp)
        except Exception as e:
            print("Failed to load quick play options:", e)
            
        # Insert it before the play button
        self.action_layout.insertWidget(0, self.quick_play_combo, stretch=2)

    def log(self, message):
        self.results_text.append(message.strip())
        scrollbar = self.results_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def start_launch_sequence(self):
        self.selected_version = self.version_combo.currentData()
        self.selected_quick_play = self.quick_play_combo.currentData() if hasattr(self, 'quick_play_combo') else None
        self.play_button.setEnabled(False)
        self.version_combo.setEnabled(False)
        if hasattr(self, 'quick_play_combo'): self.quick_play_combo.setEnabled(False)
        self.results_text.clear()
        
        self.log(f"=== Starting Launch Sequence for Minecraft {self.selected_version} ===")
        self.setWindowTitle(f"Minecraft - {self.selected_version} - Downloading...")
        
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
                config_path = utils.get_dir("versions") / self.selected_version / "command_config.json"
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
        self.set_progress_visible(True)
        self.status_label.setText("[1/6] Authenticating with Microsoft...")
        self.step_progress_bar.setRange(0, 100)
        self.step_progress_bar.setValue(0)
        self.step_progress_bar.setFormat("Step 1/6 - Authentication")
        self.task_progress_bar.setRange(0, 0)
        
        self.is_just_logging_in = False
        self.process = QProcess()
        self.process.setProgram(sys.executable)
        self.process.setArguments(["get_mc_username.py", "--json", "--profile", get_active_profile_id(getattr(self, "_force_new_login", False))])
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
                self.log("[System] Waiting for user to complete Microsoft login in browser...")
                url = obj.get("verification_uri")
                self.open_browser(url)
            elif "mc_username" in obj:
                self.auth_data = obj
                self._login_succeeded = True
                self._cleanup_browser()

                # Defer the file saving/moving logic so the Qt Event Loop can actually process the deleteLater calls
                from PyQt6.QtCore import QTimer
                QTimer.singleShot(1000, lambda auth_obj=obj: self._finalize_auth(auth_obj))
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
                self.check_game_started(line)
                # Standard Python logging uses stderr, so we don't want to prefix everything with 'ERROR: '
                self.log(line)

    def auth_finished(self, exitCode, exitStatus):
        self.step_progress_bar.setVisible(False)
        self.step_progress_bar.setRange(0, 100)

        self._cleanup_browser()

        self.play_button.setEnabled(True)
        self.login_btn.setEnabled(True)
        if self.version_combo.currentText() != "No versions available":
            self.delete_button.setEnabled(True)
            
        if exitCode != 0 or not self.auth_data:
            self.log("[Error] Authentication failed or cancelled.")
            self.status_label.setText("Authentication Failed.")
            
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
            self.reset_ui()
            self.update_profile_banner() # Show previous account's banner
            is_logged_in = bool(self.auth_data)
            self.logout_btn.setVisible(is_logged_in)
            self.login_btn.setVisible(not is_logged_in)
            return
            
        if self.is_just_logging_in:
            self.log("[System] Login complete. Ready to play.")
            self.status_label.setText("Ready to launch.")
        else:
            self.start_download_pipeline()
        
    def start_download_pipeline(self):
        self.set_progress_visible(True)
        self.log("")
        self.log("=== Authentication Complete. Proceeding to Downloads ===")
        
        is_downloaded = self.selected_version in get_downloaded_versions()
        if is_downloaded and (getattr(self, 'skip_verification', True) or getattr(self, 'is_offline', False)):
            self.log("")
            self.log(f"[System] Minecraft {self.selected_version} is already fully downloaded. Skipping verification.")
            self.pipeline_steps = []
        else:
            self.pipeline_steps = [
                ("Java", ["download_java.py", "--version", self.selected_version, "--json"]),
                ("Game Client", ["download_minecraft.py", "--version", self.selected_version, "--json"]),
                ("Libraries", ["download_libraries.py", "--version", self.selected_version, "--json"]),
                ("Assets", ["download_assets.py", "--version", self.selected_version, "--json"])
            ]
            
        launch_args = [
            "command_executor.py",
            "--version", self.selected_version,
            "--username", self.auth_data['mc_username'],
            "--uuid", self.auth_data['mc_uuid'],
            "--accessToken", self.auth_data['mc_access_token'],
            "--xuid", self.auth_data['xbox_xuid'],
            "--clientId", self.auth_data['launch_client_id']
        ]
        if getattr(self, "is_offline", False):
            launch_args.append("--offline")
            
        if hasattr(self, 'selected_quick_play') and self.selected_quick_play:
            launch_args.append("--quickPlayType")
            launch_args.append(self.selected_quick_play['type'])
            launch_args.append("--quickPlayId")
            launch_args.append(self.selected_quick_play['id'])
            
        self.pipeline_steps.append(("Launching Game...", launch_args))
        self.current_step = 0
        self.run_next_step()

    def run_next_step(self):
        if self.current_step >= len(self.pipeline_steps):
            self.log("")
            self.log("=== Game Closed! ===")
            self.status_label.setText("Game closed.")
            self.step_progress_bar.setRange(0, 100)
            self.step_progress_bar.setValue(100)
            self.task_progress_bar.setValue(100)
            self.reset_ui()
            return

        total_steps = len(self.pipeline_steps) + 1
        current_display_step = self.current_step + 2

        percent = int(((current_display_step - 1) / total_steps) * 100)
        self.step_progress_bar.setValue(percent)
        self.step_progress_bar.setFormat(f"Step {current_display_step}/{total_steps} - %p%")
        
        status_msg, args = self.pipeline_steps[self.current_step]
        
        if "command_executor.py" in args:
            self.task_progress_bar.setRange(0, 0)
            self.status_label.setText(f"[{current_display_step}/{total_steps}] {status_msg}")
        else:
            self.task_progress_bar.setRange(0, 100)
            self.task_progress_bar.setValue(0)
            self.status_label.setText(f"[{current_display_step}/{total_steps}] Verifying {status_msg}...")
        self.log("")
        self.log(f"> Running: {' '.join(args)}")
        
        self.process = QProcess()
        self.process.setProgram(sys.executable)
        self.process.setArguments(args)
        
        self.process.readyReadStandardOutput.connect(self.handle_pipeline_stdout)
        self.process.readyReadStandardError.connect(self.handle_stderr)
        self.process.finished.connect(self.pipeline_step_finished)
        
        self.process.start()

    def check_game_started(self, line):
        if self.current_step >= len(self.pipeline_steps): return
        
        status_msg, args = self.pipeline_steps[self.current_step]
        if "command_executor.py" in args:
            if "LWJGL" in line or "Setting user" in line or "OpenAL initialized" in line:
                if self.task_progress_bar.maximum() == 0:
                    total_steps = len(self.pipeline_steps) + 1
                    self.step_progress_bar.setValue(100)
                    self.step_progress_bar.setFormat(f"Step {total_steps}/{total_steps} - 100%")
                    self.task_progress_bar.setRange(0, 100)
                    self.task_progress_bar.setValue(100)
                    self.task_progress_bar.setFormat("Game Running!")
                    self.setWindowTitle(f"Minecraft - {self.selected_version} - Playing")
                    
                    if self.remember_version:
                        config = load_launcher_config()
                        config["last_played_version"] = self.selected_version
                        save_launcher_config(config)
                        self.last_played_version = self.selected_version
                    
                    self.log("[SUCCESS] Minecraft launched successfully!")
                    self.status_label.setText(f"[{total_steps}/{total_steps}] Minecraft is now running.")

    def handle_pipeline_stdout(self):
        data = self.process.readAllStandardOutput().data().decode('utf-8')
        for line in data.splitlines():
            line = line.strip()
            if not line: continue
            
            self.check_game_started(line)
            
            
            try:
                obj = json.loads(line)
                if isinstance(obj, dict) and obj.get("type") == "progress":
                    task_percent = float(obj["progress"])
                    self.task_progress_bar.setValue(int(task_percent))
                    if "status" in obj:
                        self.status_label.setText(obj["status"])
                        
                    # Smoothly update the top step progress bar
                    total_steps = len(self.pipeline_steps) + 1
                    current_display_step = self.current_step + 2
                    base_percent = ((current_display_step - 1) / total_steps) * 100
                    added_percent = (task_percent / 100.0) * (100.0 / total_steps)
                    self.step_progress_bar.setValue(int(base_percent + added_percent))
                else:
                    self.log(line)
            except json.JSONDecodeError:
                self.log(line)
                
                # Dynamic status updates based on script text
                if self.current_step < len(self.pipeline_steps):
                    status_msg, args = self.pipeline_steps[self.current_step]
                    if "command_executor.py" not in args:
                        total_steps = len(self.pipeline_steps) + 1
                        current_display_step = self.current_step + 1
                        lower_line = line.lower()
                        if "verifying" in lower_line:
                            self.status_label.setText(f"[{current_display_step}/{total_steps}] Verifying {status_msg}...")
                        elif "downloading" in lower_line or "fetching" in lower_line:
                            self.status_label.setText(f"[{current_display_step}/{total_steps}] Downloading {status_msg}...")
                        elif "extracting" in lower_line:
                            self.status_label.setText(f"[{current_display_step}/{total_steps}] Extracting {status_msg}...")
                
    def pipeline_step_finished(self, exitCode, exitStatus):
        if exitCode != 0:
            self.log("")
            self.log(f"[Error] Step failed with code {exitCode}. Aborting launch.")
            self.status_label.setText("Launch Failed.")
            self.reset_ui()
            return
            
        if self.current_step < len(self.pipeline_steps):
            status_msg, args = self.pipeline_steps[self.current_step]
            if "download_assets.py" in args:
                # Re-load versions so the [Downloaded] tag updates
                self.load_versions()
            
        self.current_step += 1
        self.run_next_step()

    def reset_ui(self):
        self.setWindowTitle("PyMcLauncher")
        self.play_button.setEnabled(True)
        self.version_combo.setEnabled(True)
        if hasattr(self, 'quick_play_combo'): self.quick_play_combo.setEnabled(True)
        self.step_progress_bar.setRange(0, 100)
        self.step_progress_bar.setValue(100)
        self.step_progress_bar.setFormat("%p%")
        self.task_progress_bar.setRange(0, 100)
        self.task_progress_bar.setValue(100)
        self.set_progress_visible(False)

    # --- Browser Window Code (from old GUI) ---
    def open_browser(self, url):
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
                
                if getattr(self.parent_launcher, 'is_just_logging_in', False) and not getattr(self.parent_launcher, '_login_succeeded', False):
                    if getattr(self.parent_launcher, 'process', None):
                        self.parent_launcher.process.kill()
                super().closeEvent(event)

        self.browser_window = LoginBrowserWindow(self)
        self.browser_window.setWindowTitle("Microsoft Login - Minecraft Launcher")
        self.browser_window.setFixedSize(600, 750)
        
        self.browser_central = QWidget()
        self.browser_layout = QGridLayout(self.browser_central)
        self.browser_layout.setContentsMargins(0, 0, 0, 0)

        self.loading_widget = QWidget()
        self.loading_widget.setStyleSheet("background-color: rgba(43, 43, 43, 180);")
        loading_layout = QVBoxLayout(self.loading_widget)
        loading_label = QLabel("Loading Secure Login...")
        loading_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        loading_label.setStyleSheet("color: white; font-size: 18px; font-weight: bold;")
        loading_layout.addWidget(loading_label)
        
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
            if (!navigator.credentials) return;
            if (navigator.credentials.is_hooked) return;
            navigator.credentials.is_hooked = true;
            const originalGet = navigator.credentials.get;
            navigator.credentials.get = function(options) {
                console.log("AGY_WEBAUTHN_START");
                return originalGet.call(this, options).then(res => {
                    console.log("AGY_WEBAUTHN_END");
                    return res;
                }).catch(err => {
                    console.log("AGY_WEBAUTHN_END");
                    throw err;
                });
            };
        })();
        """)
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setName("webauthn_hook")
        
        # Only insert if it doesn't exist
        if not any(s.name() == "webauthn_hook" for s in self.web_profile.scripts().toList()):
            self.web_profile.scripts().insert(script)
            
        class LoginWebPage(QWebEnginePage):
            def __init__(self, profile, parent_gui):
                super().__init__(profile)
                self.parent_gui = parent_gui
                
            def javaScriptConsoleMessage(self, level, message, lineNumber, sourceID):
                if message == "AGY_WEBAUTHN_START":
                    if hasattr(self.parent_gui, 'loading_widget') and self.parent_gui.loading_widget:
                        self.parent_gui.loading_widget.show()
                    return
                if message == "AGY_WEBAUTHN_END":
                    if hasattr(self.parent_gui, 'loading_widget') and self.parent_gui.loading_widget:
                        self.parent_gui.loading_widget.hide()
                    return
                super().javaScriptConsoleMessage(level, message, lineNumber, sourceID)
                
        self.web_page = LoginWebPage(self.web_profile, self)
        
        self.browser = QWebEngineView()
        self.browser.setPage(self.web_page)
        
        self.browser_layout.addWidget(self.browser, 0, 0)
        self.browser_layout.addWidget(self.loading_widget, 0, 0)

        self.browser.loadStarted.connect(self.loading_widget.show)
        self.browser.load(QUrl(url))
        self.browser.loadFinished.connect(self.on_browser_load_finished)
        self.browser.urlChanged.connect(self.on_browser_url_changed)

        self.browser_window.setCentralWidget(self.browser_central)
        self.browser_window.show()
    def _cleanup_browser(self):
        if getattr(self, '_is_cleaning_up', False):
            return
        self._is_cleaning_up = True
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
        if hasattr(self, 'loading_widget') and self.loading_widget:
            self.loading_widget.hide()
        if ok:
            self.browser.page().runJavaScript("""
                setTimeout(function() {
                    var btn = document.querySelector('#idSIButton9, input[type=submit], button[type=submit]');
                    if(btn && btn.value !== 'Cancel' && btn.innerText !== 'Cancel') {
                        btn.click();
                    }
                }, 500);
            """)

    def on_browser_url_changed(self, url):
        if "login.live.com/undefined" in url.toString() or "oauth20_desktop.srf" in url.toString():
            if self.browser_window:
                self.browser_window.hide()

if __name__ == '__main__':
    os.environ["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
    app = QApplication(sys.argv)
    
    # Force the use of the native Windows UX theme engine (comctl32.dll)
    app.setStyle("windowsvista")
    
    window = LauncherGUI()
    window.show()
    sys.exit(app.exec())
