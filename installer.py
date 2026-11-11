"""
Standalone bootstrap installer for PyMcLauncher.

Usage:
  Place this script in the directory where you want to install PyMcLauncher and run:
    python installer.py

Features:
- Fetches available download options from the latest release of cloudyhair6/PyMcLauncher.
- Presents a graphical checkbox menu to choose options (supports PyQt6, PyQt5, or native Tkinter fallback).
- Downloads all selected files with concurrent downloads and a progress bar.
- Generates updater_config.json so the launcher and built-in auto-updater work seamlessly.
- Self-deletion: If this installer script itself is NOT listed in the selected download options,
  it automatically removes itself from disk after a successful installation.
"""
import base64
import concurrent.futures
import json
import os
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

OWNER = "cloudyhair6"
REPO = "PyMcLauncher"
USER_AGENT = "Python-Latest-Version-Detector"
CONFIG_FILE = "updater_config.json"


# --- GitHub API & Network Helpers ---
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


def download_raw_url(url, extra_headers=None):
    handlers = [NoAuthRedirectHandler]
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    except Exception:
        pass
    opener = urllib.request.build_opener(*handlers)

    for use_auth in (True, False):
        headers = get_auth_headers(allow_token=use_auth)
        if extra_headers:
            headers.update(extra_headers)
        if not use_auth and "Authorization" in headers:
            del headers["Authorization"]

        req = urllib.request.Request(url, headers=headers)
        try:
            with opener.open(req, timeout=15) as resp:
                if resp.status == 200:
                    return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 401 and use_auth:
                continue
            break
        except ssl.SSLError:
            try:
                unverified_ctx = ssl._create_unverified_context()
                fallback_opener = urllib.request.build_opener(
                    NoAuthRedirectHandler, urllib.request.HTTPSHandler(context=unverified_ctx)
                )
                with fallback_opener.open(req, timeout=15) as resp:
                    if resp.status == 200:
                        return resp.read()
            except Exception:
                break
        except Exception:
            break
    return None


def get_latest_version():
    url = f"https://api.github.com/repos/{OWNER}/{REPO}/releases/latest"
    content = download_raw_url(url)
    if content:
        try:
            return json.loads(content.decode("utf-8")).get("tag_name")
        except Exception:
            pass

    # Fallback to redirect URL from web
    try:
        req = urllib.request.Request(f"https://github.com/{OWNER}/{REPO}/releases/latest", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as resp:
            final_url = resp.geturl()
            if "/releases/tag/" in final_url:
                tag = final_url.split("/releases/tag/")[-1].split("?")[0].strip("/")
                if tag:
                    return urllib.parse.unquote(tag)
    except Exception:
        pass
    return None


def fetch_download_options(tag):
    for ref in (tag, tag.lstrip("v") if tag.startswith("v") else f"v{tag}", "main"):
        for path in ("download_options.json", "updater/download_options.json"):
            api_url = f"https://api.github.com/repos/{OWNER}/{REPO}/contents/{path}?ref={ref}"
            content = download_raw_url(api_url, extra_headers={"Accept": "application/vnd.github.v3.raw"})
            if content:
                try:
                    return json.loads(content.decode("utf-8"))
                except Exception:
                    pass
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
            if data:
                with open(local_fp, "wb") as f:
                    f.write(data)
                # Ensure shell and command scripts have execution permissions on macOS / Linux
                if os.name != "nt" and (fp.endswith(".command") or fp.endswith(".sh") or fp.endswith(".py")):
                    try:
                        os.chmod(local_fp, 0o755)
                    except Exception:
                        pass
        except Exception as e:
            print(f"Error downloading {fp}: {e}")
        finally:
            with lock:
                completed += 1
                if progress_cb:
                    progress_cb(completed, total, fp)

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(task, f) for f in files]
        concurrent.futures.wait(futures)


def schedule_self_deletion():
    """Schedules self-deletion after process exit using detached background process."""
    try:
        cur_file = os.path.abspath(__file__)
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            subprocess.Popen(
                f'cmd.exe /c timeout /t 1 >nul & del /f /q "{cur_file}"',
                shell=True,
                creationflags=flags,
            )
        else:
            # On macOS and Linux, spawn a detached shell to delete the installer after 1 second
            try:
                subprocess.Popen(
                    f'sleep 1 && rm -f "{cur_file}"',
                    shell=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except Exception:
                try:
                    os.remove(cur_file)
                except Exception:
                    pass
    except Exception as e:
        print(f"Self-deletion error: {e}")


# --- GUI Implementation (PyQt6 -> PyQt5 -> Tkinter Fallback) ---
def run_installer_gui():
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


def _run_qt_gui(QtModule):
    QtCore, QtWidgets, qt_ver = QtModule
    app = QtWidgets.QApplication(sys.argv)

    install_dir = os.path.abspath(os.path.dirname(__file__))

    try:
        if qt_ver == 6:
            from PyQt6.QtGui import QIcon
        else:
            from PyQt5.QtGui import QIcon
        inst_icon = QIcon()
        icon_svg = os.path.join(install_dir, "images", "icon", "PyMcLauncher.svg")
        icon_ico = os.path.join(install_dir, "images", "icon", "PyMcLauncher.ico")
        if os.path.exists(icon_svg):
            inst_icon.addFile(icon_svg)
        if os.path.exists(icon_ico):
            inst_icon.addFile(icon_ico)
        if not inst_icon.isNull():
            app.setWindowIcon(inst_icon)
    except Exception:
        pass

    # Splash / loading
    splash = QtWidgets.QProgressDialog("Checking latest release information...", None, 0, 0)
    splash.setWindowTitle("PyMcLauncher Installer")
    splash.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal if qt_ver == 6 else QtCore.Qt.ApplicationModal)
    splash.show()
    app.processEvents()

    latest_version = get_latest_version()
    if not latest_version:
        splash.close()
        QtWidgets.QMessageBox.critical(
            None, "Connection Error", "Could not fetch the latest release version from GitHub.\nPlease check your internet connection."
        )
        return

    splash.setLabelText("Fetching available download options...")
    app.processEvents()
    download_options = fetch_download_options(latest_version)
    splash.close()

    if not download_options:
        QtWidgets.QMessageBox.critical(
            None, "Error", f"download_options.json was not found in release {latest_version}."
        )
        return

    # Main Dialog Window
    win = QtWidgets.QWidget()
    win.setWindowTitle("PyMcLauncher Installer & Setup")
    win.resize(560, 440)
    layout = QtWidgets.QVBoxLayout(win)
    layout.setContentsMargins(20, 20, 20, 20)
    layout.setSpacing(12)

    title_lbl = QtWidgets.QLabel(f"PyMcLauncher Installation (Latest Version: {latest_version})")
    title_lbl.setStyleSheet("font-size: 16px; font-weight: bold;")
    layout.addWidget(title_lbl)

    info_lbl = QtWidgets.QLabel("Select what you want to download and install:")
    layout.addWidget(info_lbl)

    scroll = QtWidgets.QScrollArea()
    scroll.setWidgetResizable(True)
    scroll_content = QtWidgets.QWidget()
    scroll_layout = QtWidgets.QVBoxLayout(scroll_content)

    checkboxes = {}
    for opt_name in download_options.keys():
        cb = QtWidgets.QCheckBox(opt_name)
        cb.setChecked(True)
        scroll_layout.addWidget(cb)
        checkboxes[opt_name] = cb
    scroll_layout.addStretch(1)
    scroll.setWidget(scroll_content)
    layout.addWidget(scroll, stretch=1)

    status_lbl = QtWidgets.QLabel("Ready to install.")
    layout.addWidget(status_lbl)

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

    class DownloadWorker(QtCore.QThread):
        progress_sig = QtCore.pyqtSignal(int, int, str)
        finished_sig = QtCore.pyqtSignal(bool, str)

        def __init__(self, selected_opts):
            super().__init__()
            self.selected_opts = selected_opts

        def run(self):
            try:
                paths = []
                ignore_files = []
                for opt in self.selected_opts:
                    opt_data = download_options[opt]
                    if isinstance(opt_data, dict):
                        paths.extend(opt_data.get("Files", []))
                        ignore_files.extend(opt_data.get("Update Ignore Files", []))
                    elif isinstance(opt_data, list):
                        paths.extend(opt_data)

                self.progress_sig.emit(0, 1, "Fetching repository file tree...")
                tree = get_repo_tree(latest_version)
                files = resolve_files_to_download(paths, tree, ignore_files=ignore_files)

                if not files:
                    self.finished_sig.emit(False, "No matching files found in repository to download.")
                    return

                def on_prog(cur, total, fp):
                    self.progress_sig.emit(cur, total, f"Downloading: {fp}")

                perform_download_concurrently(files, latest_version, install_dir, on_prog)

                # Save updater_config.json
                cfg_path = os.path.join(install_dir, CONFIG_FILE)
                cfg_data = {
                    "current_version": latest_version,
                    "selected_options": self.selected_opts,
                    "install_dir": install_dir,
                }
                with open(cfg_path, "w", encoding="utf-8") as f:
                    json.dump(cfg_data, f, indent=4)

                self.finished_sig.emit(True, "")
            except Exception as e:
                self.finished_sig.emit(False, str(e))

    def on_install_clicked():
        selected = [name for name, cb in checkboxes.items() if cb.isChecked()]
        if not selected:
            QtWidgets.QMessageBox.warning(win, "No Options Selected", "Please select at least one option to install.")
            return

        install_btn.setEnabled(False)
        cancel_btn.setEnabled(False)
        for cb in checkboxes.values():
            cb.setEnabled(False)

        worker = DownloadWorker(selected)
        win._worker = worker

        def on_prog(cur, total, msg):
            pbar.setValue(int((cur / total) * 100) if total else 0)
            status_lbl.setText(msg[:60])

        def on_finish(success, err):
            if not success:
                QtWidgets.QMessageBox.critical(win, "Installation Failed", f"Installation failed: {err}")
                install_btn.setEnabled(True)
                cancel_btn.setEnabled(True)
                return

            pbar.setValue(100)
            status_lbl.setText("Installation completed successfully!")

            # Check if this script itself was in the download options
            this_script_name = os.path.basename(os.path.abspath(__file__)).lower()
            all_downloaded_names = []
            for opt in selected:
                opt_data = download_options[opt]
                f_list = opt_data.get("Files", []) if isinstance(opt_data, dict) else opt_data
                for f in f_list:
                    all_downloaded_names.append(os.path.basename(f).lower())

            should_delete_self = this_script_name not in all_downloaded_names

            del_msg = (
                "\n\nNote: Because this installer is not part of the downloaded files, it will now delete itself."
                if should_delete_self
                else ""
            )

            QtWidgets.QMessageBox.information(
                win,
                "Installation Complete",
                f"PyMcLauncher ({latest_version}) has been installed successfully!{del_msg}",
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
    root.geometry("540x440")
    root.resizable(False, False)

    # Bring window to front on macOS
    if sys.platform == "darwin":
        try:
            root.lift()
            root.attributes('-topmost', True)
            root.after_idle(root.attributes, '-topmost', False)
        except Exception:
            pass

    install_dir = os.path.abspath(os.path.dirname(__file__))

    icon_ico = os.path.join(install_dir, "images", "icon", "PyMcLauncher.ico")
    if os.path.exists(icon_ico):
        try:
            root.iconbitmap(icon_ico)
        except Exception:
            pass

    lbl_title = tk.Label(root, text="PyMcLauncher Installer", font=("Arial", 14, "bold"))
    lbl_title.pack(pady=10)

    lbl_status = tk.Label(root, text="Fetching latest version...", font=("Arial", 9))
    lbl_status.pack()

    pbar = ttk.Progressbar(root, mode="indeterminate")
    pbar.pack(fill=tk.X, padx=25, pady=8)
    pbar.start(10)

    frame_options = tk.LabelFrame(root, text="Available Download Options", padx=10, pady=10)
    frame_options.pack(fill=tk.BOTH, expand=True, padx=20, pady=5)

    check_vars = {}
    btn_install_ref = [None]

    def on_meta_success(ver, opts):
        pbar.stop()
        pbar.config(mode="determinate", value=0)
        lbl_status.config(text=f"Latest Version: {ver}. Select options to install:")

        for opt_name in opts.keys():
            var = tk.BooleanVar(value=True)
            check_vars[opt_name] = var
            chk = tk.Checkbutton(frame_options, text=opt_name, variable=var, anchor="w")
            chk.pack(fill=tk.X, pady=2)

        def do_install():
            selected = [name for name, var in check_vars.items() if var.get()]
            if not selected:
                messagebox.showwarning("Warning", "Please select at least one option to install.")
                return

            if btn_install_ref[0]:
                btn_install_ref[0].config(state="disabled")
            lbl_status.config(text="Fetching files...")

            def worker_task(selected_list, ver_str, opts_dict, dest_dir):
                try:
                    paths = []
                    ignore_files = []
                    for opt in selected_list:
                        opt_data = opts_dict.get(opt, {})
                        if isinstance(opt_data, dict):
                            paths.extend(opt_data.get("Files", []))
                            ignore_files.extend(opt_data.get("Update Ignore Files", []))
                        elif isinstance(opt_data, list):
                            paths.extend(opt_data)

                    tree = get_repo_tree(ver_str)
                    files = resolve_files_to_download(paths, tree, ignore_files=ignore_files)
                    if not files:
                        root.after(0, lambda: messagebox.showerror("Installation Error", "No matching files found in repository to download."))
                        return

                    def on_prog(cur, total, fp):
                        pct = int((cur / total) * 100) if total else 0
                        short_fp = os.path.basename(fp)
                        root.after(0, lambda p=pct, s=short_fp: (pbar.config(value=p), lbl_status.config(text=f"Downloading: {s}")))

                    perform_download_concurrently(files, ver_str, dest_dir, on_prog)

                    cfg_path = os.path.join(dest_dir, CONFIG_FILE)
                    cfg_data = {
                        "current_version": ver_str,
                        "selected_options": selected_list,
                        "install_dir": dest_dir,
                    }
                    with open(cfg_path, "w", encoding="utf-8") as f:
                        json.dump(cfg_data, f, indent=4)

                    this_script = os.path.basename(os.path.abspath(__file__)).lower()
                    all_names = [os.path.basename(f).lower() for f in files]
                    should_delete = this_script not in all_names

                    del_note = (
                        "\n\nNote: Because this installer is not part of the downloaded files, it will now delete itself."
                        if should_delete
                        else ""
                    )

                    def on_done():
                        messagebox.showinfo("Success", f"PyMcLauncher ({ver_str}) installed successfully!{del_note}")
                        root.destroy()
                        if should_delete:
                            schedule_self_deletion()
                        sys.exit(0)

                    root.after(0, on_done)
                except Exception as e:
                    err_msg = str(e)
                    root.after(0, lambda m=err_msg: messagebox.showerror("Installation Error", m))

            threading.Thread(
                target=worker_task,
                args=(list(selected), str(ver), dict(opts), str(install_dir)),
                daemon=True,
            ).start()

        btn = tk.Button(root, text="Download & Install", font=("Arial", 10, "bold"), command=do_install, pady=4)
        btn.pack(pady=10)
        btn_install_ref[0] = btn

    def on_meta_error(err_msg):
        pbar.stop()
        messagebox.showerror("Error", err_msg)
        root.destroy()

    def fetch_meta_thread():
        # Pure network I/O — zero Tkinter widget access from worker thread
        ver = get_latest_version()
        if not ver:
            root.after(0, lambda: on_meta_error("Could not fetch latest release version from GitHub.\nPlease check your internet connection."))
            return
        opts = fetch_download_options(ver)
        if not opts:
            root.after(0, lambda: on_meta_error(f"download_options.json was not found in release {ver}."))
            return
        root.after(0, lambda: on_meta_success(ver, opts))

    threading.Thread(target=fetch_meta_thread, daemon=True).start()
    root.mainloop()


def run_update_check(install_dir=None, relaunch_script=None):
    """Silently update the installation to the latest release."""
    if not install_dir:
        install_dir = os.path.dirname(os.path.abspath(__file__))
    install_dir = os.path.abspath(install_dir)
    cfg_file = os.path.join(install_dir, CONFIG_FILE)
    cfg = {}
    if os.path.exists(cfg_file):
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            pass

    selected_opts = cfg.get("selected_options", ["PC App only (comes with development soon will without)"])
    ver = get_latest_version()
    if not ver:
        return
    download_options = fetch_download_options(ver)
    if not download_options:
        return
    paths = []
    ignore_files = []
    for opt in selected_opts:
        if opt in download_options:
            opt_data = download_options[opt]
            if isinstance(opt_data, dict):
                paths.extend(opt_data.get("Files", []))
                ignore_files.extend(opt_data.get("Update Ignore Files", []))
            elif isinstance(opt_data, list):
                paths.extend(opt_data)

    tree = get_repo_tree(ver)
    files = resolve_files_to_download(paths, tree, ignore_files=ignore_files)
    if not files:
        return

    perform_download_concurrently(files, ver, install_dir)
    cfg["current_version"] = ver
    cfg["selected_options"] = selected_opts
    cfg["install_dir"] = install_dir
    with open(cfg_file, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4)

    if relaunch_script:
        candidate_dirs = [install_dir, os.path.dirname(install_dir), os.getcwd()]
        for cdir in candidate_dirs:
            sp = os.path.join(cdir, relaunch_script)
            if os.path.exists(sp):
                if os.name == "nt":
                    subprocess.Popen(["cmd.exe", "/c", sp], cwd=cdir, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", sp], cwd=cdir)
                else:
                    subprocess.Popen(["bash", sp], cwd=cdir)
                break


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--update-check", action="store_true", help="Perform silent update check and download")
    parser.add_argument("--relaunch", type=str, default=None, help="Script to relaunch")
    parser.add_argument("--install-dir", type=str, default=None, help="Target installation directory")
    args, _ = parser.parse_known_args()
    if args.update_check:
        run_update_check(args.install_dir, args.relaunch)
    else:
        run_installer_gui()
