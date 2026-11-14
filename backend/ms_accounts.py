#!/usr/bin/env python3
"""
Microsoft & Minecraft Java Account Authentication Manager (ms_accounts.py)

Can be used both as a standalone tool or as a module for PyMcLauncher.
Only requires utils.py (or can even run standalone without it).

Supported Sign-in Methods:
  1. Windows Security Popup (--wam / --windows-popup) [Windows 10/11 only]:
     Uses native Windows Web Account Manager (WAM) broker via MSAL.
     Sign in with Windows Hello, PIN, or pre-linked Windows accounts without typing passwords.
  2. Built-in Popup Window (--popup) [Cross-platform, default on macOS/Linux]:
     Opens a built-in Python Microsoft sign-in popup window (OAuth2 desktop flow, no device code).
  3. Default Web Browser (--browser) [Cross-platform]:
     Opens your default web browser (Chrome, Edge, Safari, Firefox) to microsoft.com/link with the code pre-filled.
  4. Device Code Link (--device / --device-code) [Cross-platform]:
     Generates a one-time device code link (https://www.microsoft.com/link?otc=...) and copies the code to clipboard.

Usage:
  python3 ms_accounts.py
  python3 ms_accounts.py --wam
  python3 ms_accounts.py --popup
  python3 ms_accounts.py --browser
  python3 ms_accounts.py --device
  python3 ms_accounts.py --list-accounts
  python3 ms_accounts.py --json --profile default_account
"""

import argparse
import base64
import hashlib
import json
import os
import secrets
import shutil
import stat
import struct
import subprocess
import sys
import time
import urllib.parse
import uuid
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

import requests

# Enable UTF-8 output on Windows without replacing/closing sys.stdout.buffer
if sys.platform == "win32":
    os.environ["PYTHONIOENCODING"] = "utf-8"
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

# Safe imports for utils.py if present (standalone capability)
_this_dir = Path(__file__).resolve().parent
_possible_roots = [_this_dir, _this_dir.parent]
for _r in _possible_roots:
    if str(_r) not in sys.path:
        sys.path.insert(0, str(_r))

try:
    from utils import get_windows_linked_microsoft_accounts, calculate_locator_bar_color
except ImportError:
    try:
        from backend.utils import get_windows_linked_microsoft_accounts, calculate_locator_bar_color
    except ImportError:
        get_windows_linked_microsoft_accounts = None
        calculate_locator_bar_color = None

# Constants matching Minecraft official launcher and Titles.MinecraftNintendoSwitch
NINTENDO_SWITCH_TITLE_ID = "00000000441cc96b"
AZURE_APP_CLIENT_ID = "00000000402b5328"  # Minecraft Official Launcher Client ID
LIVE_AUTH_URL = "https://login.live.com/oauth20_authorize.srf"
LIVE_DESKTOP_REDIRECT = "https://login.live.com/oauth20_desktop.srf"
LIVE_TOKEN_URL = "https://login.live.com/oauth20_token.srf"
LIVE_DEVICE_CODE_URL = "https://login.live.com/oauth20_connect.srf"
LIVE_SCOPE = "service::user.auth.xboxlive.com::MBI_SSL"

XBL_USER_AUTH_URL = "https://user.auth.xboxlive.com/user/authenticate"
XBL_DEVICE_AUTH_URL = "https://device.auth.xboxlive.com/device/authenticate"
XBL_TITLE_AUTH_URL = "https://title.auth.xboxlive.com/title/authenticate"
XBL_SISU_AUTHORIZE_URL = "https://sisu.xboxlive.com/authorize"
XBL_XSTS_AUTH_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"

MC_LOGIN_WITH_XBOX_URL = "https://api.minecraftservices.com/authentication/login_with_xbox"
MC_LAUNCHER_LOGIN_URL = "https://api.minecraftservices.com/launcher/login"
MC_PROFILE_URL = "https://api.minecraftservices.com/minecraft/profile"

XBL_RELYING_PARTY = "http://xboxlive.com"
MC_XSTS_RELYING_PARTY = "rp://api.minecraftservices.com/"

XBOX_LIVE_ERRORS = {
    2148916233: "This Microsoft account does not have an Xbox profile. Please create one on xbox.com first.",
    2148916235: "Xbox Live is not available in your country or region.",
    2148916236: "This account requires adult verification on xbox.com (South Korea / specific regions).",
    2148916237: "Your account has reached its limit for playtime. Your account has been blocked from logging in.",
    2148916238: "This account is a child account and must be added to a Family Safety group by a parent before playing.",
}


# --- Cryptographic Helpers for Xbox Live Proof-of-Possession Signatures ---
_P256_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_P256_A = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFC
_P256_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
_P256_GX = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
_P256_GY = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5
_P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


def _p256_add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2:
        if (y1 + y2) % _P256_P == 0:
            return None
        m = (3 * x1 * x1 + _P256_A) * pow(2 * y1, _P256_P - 2, _P256_P) % _P256_P
    else:
        m = (y2 - y1) * pow(x2 - x1, _P256_P - 2, _P256_P) % _P256_P
    x3 = (m * m - x1 - x2) % _P256_P
    y3 = (m * (x1 - x3) - y1) % _P256_P
    return (x3, y3)


def _p256_mul(k: int, pt):
    curr = pt
    res = None
    while k > 0:
        if k & 1:
            res = _p256_add(res, curr)
        curr = _p256_add(curr, curr)
        k >>= 1
    return res


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


class PurePythonXboxSigner:
    """Signs requests destined for Xbox Live endpoints using ECDSA P-256."""

    def __init__(self):
        self.d = secrets.randbelow(_P256_N - 1) + 1
        px, py = _p256_mul(self.d, (_P256_GX, _P256_GY))
        self.x = px.to_bytes(32, "big")
        self.y = py.to_bytes(32, "big")
        self.jwk = {
            "crv": "P-256",
            "kty": "EC",
            "x": _b64url(self.x),
            "y": _b64url(self.y),
            "alg": "ES256",
            "use": "sig",
        }

    def sign_bytes(self, data: bytes) -> bytes:
        e = int.from_bytes(hashlib.sha256(data).digest(), "big")
        while True:
            k = secrets.randbelow(_P256_N - 1) + 1
            pt = _p256_mul(k, (_P256_GX, _P256_GY))
            if pt is None:
                continue
            r = pt[0] % _P256_N
            if r == 0:
                continue
            s = (pow(k, _P256_N - 2, _P256_N) * (e + self.d * r)) % _P256_N
            if s == 0:
                continue
            if s > _P256_N // 2:
                s = _P256_N - s
            return r.to_bytes(32, "big") + s.to_bytes(32, "big")

    def sign_xbox_request(self, url: str, auth_token: str, payload_str: str) -> str:
        parsed = urllib.parse.urlsplit(url)
        path_and_query = parsed.path + ("?" + parsed.query if parsed.query else "")
        windows_filetime = int((time.time() + 11644473600) * 10000000)
        ts_bytes = struct.pack(">Q", windows_filetime)

        msg = bytearray()
        msg.extend(b"\x00\x00\x00\x01\x00")
        msg.extend(ts_bytes)
        msg.extend(b"\x00POST\x00")
        msg.extend(path_and_query.encode("ascii") + b"\x00")
        msg.extend(auth_token.encode("ascii") + b"\x00" if auth_token else b"\x00")
        msg.extend(payload_str.encode("utf-8") + b"\x00" if payload_str else b"\x00")

        raw_sig = self.sign_bytes(bytes(msg))
        out = bytearray(b"\x00\x00\x00\x01")
        out.extend(ts_bytes)
        out.extend(raw_sig)
        return base64.b64encode(out).decode("ascii")


def _create_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


class JsonCacheStore:
    def __init__(self, file_path: Path):
        self.file_path = file_path

    def get_cached(self) -> dict:
        if self.file_path.exists():
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def set_cached_partial(self, data: dict):
        current = self.get_cached()
        current.update(data)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2)


def _check_iso_valid(iso_str: str) -> bool:
    if not iso_str:
        return False
    try:
        clean = iso_str.replace("Z", "+00:00")
        if "." in clean:
            main_part, frac_part = clean.split(".", 1)
            tz_split = frac_part.split("+")
            frac_digits = tz_split[0]
            tz = "+" + tz_split[1] if len(tz_split) > 1 else ""
            clean = f"{main_part}.{frac_digits[:6]}{tz}"
        exp = datetime.fromisoformat(clean)
        return exp > datetime.now(timezone.utc)
    except Exception:
        return False


_QT_PROFILE_KEEPALIVE = []


class _QtPopupUnavailable(RuntimeError):
    """Raised when PyQt6 / PyQt6-WebEngine cannot be loaded for the standalone popup."""


class _SwitchToBrowserRequested(RuntimeError):
    """Raised when the user opts to continue Microsoft sign-in in their default browser."""


def attach_webauthn_ux_handler(
    page,
    parent_window=None,
    on_switch_browser=None,
    on_state_update=None,
):
    """Attach a Qt 6.7+ WebAuthn / Passkey UX dialog handler to a QWebEnginePage.

    Handles SelectAccount, CollectPin, FinishTokenCollection, RequestFailed,
    Cancelled, and Completed states so hardware security keys and local passkey
    prompts never hang with no UI handler.
    """
    if page is None or not hasattr(page, "webAuthUxRequested"):
        return None

    from PyQt6.QtCore import Qt
    from PyQt6.QtWebEngineCore import QWebEngineWebAuthUxRequest
    from PyQt6.QtWidgets import (
        QDialog,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QListWidget,
        QPushButton,
        QVBoxLayout,
        QWidget,
    )

    state_enum = QWebEngineWebAuthUxRequest.WebAuthUxState
    state_values = {
        "not_started": getattr(state_enum, "NotStarted", 0),
        "select_account": getattr(state_enum, "SelectAccount", 1),
        "collect_pin": getattr(state_enum, "CollectPin", 2),
        "finish_token": getattr(state_enum, "FinishTokenCollection", 3),
        "failed": getattr(state_enum, "RequestFailed", 4),
        "cancelled": getattr(state_enum, "Cancelled", 5),
        "completed": getattr(state_enum, "Completed", 6),
    }

    pin_reason_map = {
        0: "Create a PIN for your security key.",
        1: "Change the PIN on your security key.",
        2: "Enter the PIN for your security key.",
    }
    pin_error_map = {
        1: "Security key user verification is locked.",
        2: "Incorrect PIN. Please try again.",
        3: "PIN is too short.",
        4: "PIN contains invalid characters.",
        5: "New PIN cannot be the same as the current PIN.",
    }
    failure_reason_map = {
        0: "The passkey / security key request timed out.",
        1: "This security key is not registered with your Microsoft account.",
        2: "This security key is already registered.",
        3: "Too many wrong PIN attempts. Remove and reinsert your security key.",
        4: "Security key PIN is blocked due to too many failed attempts.",
        5: "The security key was removed before PIN entry finished.",
        6: "This security key does not support resident keys (passkeys).",
        7: "This security key does not support user verification.",
        8: "This security key does not support large blobs.",
        9: "No compatible credential algorithm found on this key.",
        10: "Security key credential storage is full.",
        11: "Passkey verification was denied or cancelled.",
        12: "Passkey verification was cancelled.",
    }

    def _enum_int(val, default: int = -1) -> int:
        if val is None:
            return default
        raw = getattr(val, "value", val)
        try:
            return int(raw)
        except Exception:
            return default

    controller = {"request": None, "dialog": None, "closing": False}

    class _WebAuthUxDialog(QDialog):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setWindowTitle("Security Key / Passkey Verification")
            self.setMinimumWidth(390)
            self.setModal(False)
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)

            root = QVBoxLayout(self)
            root.setContentsMargins(16, 16, 16, 16)
            root.setSpacing(10)

            self.title_lbl = QLabel("Passkey Verification")
            self.title_lbl.setStyleSheet("font-size: 15px; font-weight: bold;")
            self.title_lbl.setWordWrap(True)
            root.addWidget(self.title_lbl)

            self.desc_lbl = QLabel("")
            self.desc_lbl.setWordWrap(True)
            root.addWidget(self.desc_lbl)

            self.err_lbl = QLabel("")
            self.err_lbl.setWordWrap(True)
            self.err_lbl.setStyleSheet("color: #d9534f; font-weight: bold;")
            self.err_lbl.setVisible(False)
            root.addWidget(self.err_lbl)

            self.account_list = QListWidget()
            self.account_list.setVisible(False)
            self.account_list.itemDoubleClicked.connect(lambda _item: self._submit_account())
            root.addWidget(self.account_list)

            self.pin_edit = QLineEdit()
            self.pin_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.pin_edit.setPlaceholderText("Enter security key PIN")
            self.pin_edit.setVisible(False)
            self.pin_edit.returnPressed.connect(self._submit_pin)
            root.addWidget(self.pin_edit)

            btn_row = QWidget()
            btn_layout = QHBoxLayout(btn_row)
            btn_layout.setContentsMargins(0, 4, 0, 0)
            btn_layout.setSpacing(8)

            self.browser_btn = QPushButton("Open in Default Browser")
            self.browser_btn.setVisible(callable(on_switch_browser))
            self.browser_btn.clicked.connect(self._switch_to_browser)
            btn_layout.addWidget(self.browser_btn)

            btn_layout.addStretch(1)

            self.retry_btn = QPushButton("Try Again")
            self.retry_btn.setVisible(False)
            self.retry_btn.clicked.connect(self._retry_request)
            btn_layout.addWidget(self.retry_btn)

            self.primary_btn = QPushButton("Continue")
            self.primary_btn.setVisible(False)
            self.primary_btn.clicked.connect(self._handle_primary)
            btn_layout.addWidget(self.primary_btn)

            self.cancel_btn = QPushButton("Cancel")
            self.cancel_btn.clicked.connect(self._cancel_request)
            btn_layout.addWidget(self.cancel_btn)

            root.addWidget(btn_row)

        def _active_req(self):
            return controller.get("request")

        def _submit_account(self):
            req = self._active_req()
            item = self.account_list.currentItem()
            if req and item:
                try:
                    req.setSelectedAccount(item.text())
                except RuntimeError:
                    pass

        def _submit_pin(self):
            req = self._active_req()
            pin = self.pin_edit.text()
            if req and pin:
                self.pin_edit.clear()
                try:
                    req.setPin(pin)
                except RuntimeError:
                    pass

        def _handle_primary(self):
            if self.account_list.isVisible():
                self._submit_account()
            elif self.pin_edit.isVisible():
                self._submit_pin()

        def _retry_request(self):
            req = self._active_req()
            if req:
                try:
                    req.retry()
                except RuntimeError:
                    pass

        def _cancel_request(self):
            req = self._active_req()
            controller["request"] = None
            controller["closing"] = True
            if req:
                try:
                    req.cancel()
                except RuntimeError:
                    pass
            self.close()

        def _switch_to_browser(self):
            self._cancel_request()
            if callable(on_switch_browser):
                on_switch_browser()

        def closeEvent(self, event):
            if not controller.get("closing"):
                req = self._active_req()
                controller["request"] = None
                if req:
                    try:
                        req.cancel()
                    except RuntimeError:
                        pass
            super().closeEvent(event)

    def _close_dialog():
        dlg = controller.get("dialog")
        controller["request"] = None
        if dlg is not None:
            controller["closing"] = True
            try:
                dlg.close()
                dlg.deleteLater()
            except RuntimeError:
                pass
            controller["dialog"] = None
            controller["closing"] = False

    def _ensure_dialog():
        dlg = controller.get("dialog")
        if dlg is None:
            dlg = _WebAuthUxDialog(parent_window)
            controller["dialog"] = dlg
        return dlg

    def _sync_state(state):
        req = controller.get("request")
        if req is None:
            return
        try:
            rp = req.relyingPartyId() or "Microsoft"
        except RuntimeError:
            _close_dialog()
            return

        if callable(on_state_update):
            try:
                on_state_update(state, req)
            except Exception:
                pass

        if state in (state_values["cancelled"], state_values["completed"]):
            _close_dialog()
            return
        if state == state_values["not_started"]:
            return

        dlg = _ensure_dialog()
        controller["closing"] = False
        dlg.err_lbl.setVisible(False)
        dlg.account_list.setVisible(False)
        dlg.pin_edit.setVisible(False)
        dlg.primary_btn.setVisible(False)
        dlg.retry_btn.setVisible(False)

        if state == state_values["select_account"]:
            dlg.title_lbl.setText("Choose a Passkey Account")
            dlg.desc_lbl.setText(f"Select which account to use with {rp}:")
            dlg.account_list.clear()
            for name in req.userNames():
                dlg.account_list.addItem(name)
            if dlg.account_list.count() > 0:
                dlg.account_list.setCurrentRow(0)
            dlg.account_list.setVisible(True)
            dlg.primary_btn.setText("Continue")
            dlg.primary_btn.setVisible(True)
        elif state == state_values["collect_pin"]:
            pin_req = req.pinRequest()
            reason_code = _enum_int(getattr(pin_req, "reason", None), 2)
            err_code = _enum_int(getattr(pin_req, "error", None), 0)
            min_len = getattr(pin_req, "minPinLength", 0) or 0
            attempts = getattr(pin_req, "remainingAttempts", 0) or 0
            dlg.title_lbl.setText("Security Key PIN Required")
            hint = pin_reason_map.get(reason_code, "Enter the PIN for your security key.")
            if min_len > 0:
                hint += f" (Minimum {min_len} characters)"
            dlg.desc_lbl.setText(hint)
            if err_code > 0:
                err_txt = pin_error_map.get(err_code, "PIN error. Please try again.")
                if attempts > 0:
                    err_txt += f" ({attempts} attempts remaining)"
                dlg.err_lbl.setText(err_txt)
                dlg.err_lbl.setVisible(True)
            dlg.pin_edit.setVisible(True)
            dlg.primary_btn.setText("Submit PIN")
            dlg.primary_btn.setVisible(True)
            dlg.pin_edit.setFocus()
        elif state == state_values["finish_token"]:
            dlg.title_lbl.setText("Complete Security Key / Passkey Check")
            dlg.desc_lbl.setText(
                f"Touch your security key or complete verification for {rp}.\n"
                "If your passkey is on your phone or Chrome/Safari, click 'Open in Default Browser'."
            )
        elif state == state_values["failed"]:
            fail_code = _enum_int(req.requestFailureReason(), -1)
            msg = failure_reason_map.get(fail_code, "Passkey or security key verification failed.")
            dlg.title_lbl.setText("Passkey Verification Failed")
            dlg.desc_lbl.setText(
                f"{msg}\n\nYou can try again, use another sign-in method on the page, "
                "or open sign-in in your default browser."
            )
            dlg.retry_btn.setVisible(True)

        dlg.adjustSize()
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _on_ux_requested(request):
        _close_dialog()
        controller["request"] = request
        request.stateChanged.connect(_sync_state)
        _sync_state(request.state())

    page.webAuthUxRequested.connect(_on_ux_requested)
    page.destroyed.connect(_close_dialog)
    page._webauthn_ux_controller = controller
    return controller


def _copy_to_clipboard(text: str) -> bool:
    """Copy text to the OS clipboard cleanly on Windows, macOS, and Linux."""
    clean = (text or "").strip()
    if not clean:
        return False
    try:
        if sys.platform == "darwin":
            res = subprocess.run(["pbcopy"], input=clean.encode("utf-8"), check=False)
            return res.returncode == 0
        if os.name == "nt":
            res = subprocess.run(["clip"], input=clean.encode("utf-16-le"), check=False)
            if res.returncode == 0:
                return True
            subprocess.run(f"echo {clean}| clip", shell=True, check=False)
            return True
        for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
            try:
                res = subprocess.run(cmd, input=clean.encode("utf-8"), check=False)
                if res.returncode == 0:
                    return True
            except FileNotFoundError:
                continue
    except Exception:
        pass
    return False


def _open_system_browser(url: str) -> bool:
    """Open URL in the user's default system web browser cleanly."""
    try:
        if sys.platform == "darwin":
            res = subprocess.run(["open", url], check=False)
            if res.returncode == 0:
                return True
        elif os.name == "nt":
            os.startfile(url)
            return True
    except Exception:
        pass
    try:
        return bool(webbrowser.open(url, new=1, autoraise=True))
    except Exception:
        return False


def _open_qt_oauth_popup(auth_url: str, redirect_prefix: str = LIVE_DESKTOP_REDIRECT) -> str:
    """Open a built-in Qt WebEngine popup window for direct Microsoft OAuth login."""
    try:
        from PyQt6.QtCore import QEventLoop, QTimer, QUrl
        from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
        from PyQt6.QtWebEngineWidgets import QWebEngineView
        from PyQt6.QtWidgets import (
            QApplication,
            QHBoxLayout,
            QLabel,
            QMainWindow,
            QPushButton,
            QVBoxLayout,
            QWidget,
        )
    except Exception as exc:
        raise _QtPopupUnavailable(str(exc)) from exc

    existing_app = QApplication.instance()
    owns_app = existing_app is None
    app = existing_app or QApplication([sys.argv[0] or "ms_accounts.py"])
    loop = None if owns_app else QEventLoop()

    if not _QT_PROFILE_KEEPALIVE:
        _QT_PROFILE_KEEPALIVE.append(QWebEngineProfile(app))
    profile = _QT_PROFILE_KEEPALIVE[0]
    try:
        profile.cookieStore().deleteAllCookies()
    except Exception:
        pass
    page = QWebEnginePage(profile)
    view = QWebEngineView()
    view.setPage(page)

    state = {"url": "", "done": False, "switch_browser": False}

    class _PopupWindow(QMainWindow):
        def closeEvent(self, event):
            if not state["done"]:
                state["done"] = True
                _teardown()
            super().closeEvent(event)

    win = _PopupWindow()
    win.setWindowTitle("Microsoft Sign-In - PyMcLauncher")
    win.resize(580, 740)

    central = QWidget()
    root_layout = QVBoxLayout(central)
    root_layout.setContentsMargins(0, 0, 0, 0)
    root_layout.setSpacing(0)

    helper_bar = QWidget()
    helper_bar.setStyleSheet("background-color: #252526; border-bottom: 1px solid #3c3c3c;")
    helper_layout = QHBoxLayout(helper_bar)
    helper_layout.setContentsMargins(10, 6, 10, 6)
    helper_lbl = QLabel(
        "Using a phone/iCloud passkey? Click 'Open in Default Browser'."
        if sys.platform != "win32"
        else "Trouble with Passkey or Sign-In?"
    )
    helper_lbl.setStyleSheet("color: #d4d4d4; font-size: 12px;")
    helper_layout.addWidget(helper_lbl, stretch=1)

    switch_btn = QPushButton("Open in Default Browser")
    switch_btn.setStyleSheet(
        "background-color: #0078D7; color: white; font-weight: bold; "
        "border-radius: 4px; padding: 4px 10px; font-size: 12px;"
    )
    helper_layout.addWidget(switch_btn)

    root_layout.addWidget(helper_bar)
    root_layout.addWidget(view, stretch=1)
    win.setCentralWidget(central)

    def _quit_loop():
        if owns_app:
            app.quit()
        elif loop is not None:
            loop.quit()

    def _teardown():
        try:
            win.hide()
        except Exception:
            pass
        try:
            view.setPage(None)
            page.deleteLater()
            view.deleteLater()
        except Exception:
            pass
        QTimer.singleShot(50, _quit_loop)

    def _trigger_switch_browser():
        if state["done"]:
            return
        state["switch_browser"] = True
        state["done"] = True
        QTimer.singleShot(0, _teardown)

    switch_btn.clicked.connect(_trigger_switch_browser)

    attach_webauthn_ux_handler(
        page,
        parent_window=win,
        on_switch_browser=_trigger_switch_browser,
    )

    def _handle_url(qurl):
        if state["done"]:
            return
        url_str = qurl.toString()
        parsed = urllib.parse.urlparse(url_str)
        target_parsed = urllib.parse.urlparse(redirect_prefix)
        if parsed.netloc.lower() == target_parsed.netloc.lower() and parsed.path.rstrip("/") == target_parsed.path.rstrip("/"):
            params = urllib.parse.parse_qs(parsed.query)
            if not params and parsed.fragment:
                params = urllib.parse.parse_qs(parsed.fragment)
            if "code" in params or "error" in params:
                state["url"] = url_str
                state["done"] = True
                QTimer.singleShot(0, _teardown)

    view.urlChanged.connect(_handle_url)
    win.show()
    win.raise_()
    win.activateWindow()
    view.setUrl(QUrl(auth_url))

    if owns_app:
        app.exec()
    else:
        loop.exec()

    if state["switch_browser"]:
        raise _SwitchToBrowserRequested()
    if not state["url"]:
        raise RuntimeError("Popup sign-in was cancelled.")
    return state["url"]


class PurePythonAuthFlow:
    """Manages the authentication chain from Microsoft -> Xbox Live -> XSTS -> Minecraft."""

    def __init__(
        self,
        profile: str,
        cache_dir: Path,
        is_json: bool = False,
        login_hint: str = None,
        auth_method: str = "auto",
        wam_id: str = None,
        hwnd: int = 0,
        ipc_popup: bool = False,
    ):
        self.profile = profile
        self.cache_dir = cache_dir
        self.is_json = is_json
        self.login_hint = login_hint
        self.auth_method = auth_method.lower() if auth_method else "auto"
        self.wam_id = wam_id
        self.hwnd = hwnd
        self.ipc_popup = ipc_popup

        self.signer = PurePythonXboxSigner()
        self.session = requests.Session()

        self.live_cache = JsonCacheStore(cache_dir / "live-tokens.json")
        self.xbl_cache = JsonCacheStore(cache_dir / "xbl-tokens.json")
        self.mca_cache = JsonCacheStore(cache_dir / "mca-tokens.json")

        self._last_msa_was_interactive = False
        self.obtained_wam_account_id = None
        self.obtained_msa_email = self.login_hint

    # --- Live Token Management ---
    def _get_live_cached_tokens(self):
        c = self.live_cache.get_cached()
        token = c.get("token")
        if not token:
            return None
        obtained = float(token.get("obtainedOn", 0))
        expires_in = float(token.get("expires_in", 0)) * 1000.0
        valid = (obtained + expires_in - time.time() * 1000.0) > 60000
        return {
            "valid": valid,
            "access_token": token.get("access_token"),
            "refresh_token": token.get("refresh_token"),
            "client_id": token.get("client_id") or NINTENDO_SWITCH_TITLE_ID,
            "auth_method": token.get("auth_method"),
        }

    def _save_live_token(self, token_data: dict, client_id: str = None, auth_method: str = None):
        existing = self.live_cache.get_cached().get("token") or {}
        d = dict(token_data)
        d["obtainedOn"] = int(time.time() * 1000)
        d["client_id"] = client_id or existing.get("client_id") or NINTENDO_SWITCH_TITLE_ID
        if auth_method:
            d["auth_method"] = auth_method
        elif existing.get("auth_method"):
            d["auth_method"] = existing["auth_method"]
        elif self.auth_method and self.auth_method != "auto":
            d["auth_method"] = self.auth_method
        self.live_cache.set_cached_partial({"token": d})

    def _refresh_live_token(self, refresh_token: str, client_id: str = None) -> str:
        existing = self.live_cache.get_cached().get("token") or {}
        cid = client_id or existing.get("client_id") or NINTENDO_SWITCH_TITLE_ID
        body = {
            "client_id": cid,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": LIVE_SCOPE,
        }
        resp = self.session.post(LIVE_TOKEN_URL, data=body, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        self._save_live_token(data, client_id=cid, auth_method=existing.get("auth_method"))
        return data["access_token"]

    def _popup_live_oauth(self) -> str:
        """Sign in directly inside a Python Qt WebEngine popup window (OAuth2 desktop code flow)."""
        params = {
            "client_id": AZURE_APP_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": LIVE_DESKTOP_REDIRECT,
            "scope": LIVE_SCOPE,
        }
        if self.login_hint:
            params["login_hint"] = self.login_hint
        else:
            params["prompt"] = "select_account"
        auth_url = f"{LIVE_AUTH_URL}?{urllib.parse.urlencode(params)}"

        if self.ipc_popup:
            pending_payload = {
                "status": "pending_oauth_popup",
                "auth_url": auth_url,
                "redirect_prefix": LIVE_DESKTOP_REDIRECT,
            }
            if self.login_hint:
                pending_payload["login_hint"] = self.login_hint
            print(json.dumps(pending_payload), flush=True)
            raw_line = sys.stdin.readline()
            if not raw_line:
                raise RuntimeError("Popup sign-in was cancelled.")
            raw_line = raw_line.strip()
            try:
                msg = json.loads(raw_line)
            except json.JSONDecodeError:
                msg = {"redirect_url": raw_line}
            if isinstance(msg, dict) and msg.get("switch_to_browser"):
                return self._device_code_live_auth(open_browser=True)
            if isinstance(msg, dict) and msg.get("error"):
                raise RuntimeError(f"Popup sign-in failed: {msg.get('error')}")
            redirect_url = msg.get("redirect_url", "") if isinstance(msg, dict) else str(msg)
        else:
            if not self.is_json:
                print("\nOpening built-in Microsoft Sign-In popup window...", flush=True)
            try:
                redirect_url = _open_qt_oauth_popup(auth_url, LIVE_DESKTOP_REDIRECT)
            except _SwitchToBrowserRequested:
                if not self.is_json:
                    print("[*] Switching sign-in to default web browser...")
                return self._device_code_live_auth(open_browser=True)
            except _QtPopupUnavailable as e:
                if not self.is_json:
                    print(f"[!] Built-in popup unavailable ({e}). Falling back to default web browser...")
                return self._device_code_live_auth(open_browser=True)

        parsed = urllib.parse.urlparse(redirect_url)
        qs = urllib.parse.parse_qs(parsed.query)
        if not qs and parsed.fragment:
            qs = urllib.parse.parse_qs(parsed.fragment)
        if "error" in qs:
            err = qs.get("error", ["unknown_error"])[0]
            desc = qs.get("error_description", [""])[0]
            raise RuntimeError(f"Microsoft sign-in failed ({err}): {desc}" if desc else f"Microsoft sign-in failed: {err}")

        code = (qs.get("code") or [None])[0]
        if not code:
            raise RuntimeError("No authorization code was returned from Microsoft sign-in.")

        token_resp = self.session.post(
            LIVE_TOKEN_URL,
            data={
                "client_id": AZURE_APP_CLIENT_ID,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": LIVE_DESKTOP_REDIRECT,
                "scope": LIVE_SCOPE,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        token_resp.raise_for_status()
        token_data = token_resp.json()
        if "access_token" not in token_data:
            raise RuntimeError(f"Failed to exchange authorization code: {token_data}")
        self._save_live_token(token_data, client_id=AZURE_APP_CLIENT_ID, auth_method="popup")
        return token_data["access_token"]

    def _device_code_live_auth(self, open_browser: bool = False) -> str:
        body = {
            "client_id": NINTENDO_SWITCH_TITLE_ID,
            "scope": LIVE_SCOPE,
            "response_type": "device_code",
        }
        resp = self.session.post(LIVE_DEVICE_CODE_URL, data=body, timeout=15)
        resp.raise_for_status()
        res = resp.json()
        cookies = resp.cookies

        base_url = res.get("verification_uri", "https://www.microsoft.com/link")
        code = res["user_code"]
        prefill_url = f"{base_url}{'&' if '?' in base_url else '?'}otc={urllib.parse.quote(code)}"
        msg = f"To sign in, open {prefill_url} (or {base_url} and enter code {code}) to authenticate."

        copied = _copy_to_clipboard(code)
        if open_browser:
            _open_system_browser(prefill_url)

        if self.is_json:
            pending_payload = {
                "status": "pending_browser_login" if open_browser else "pending_login",
                "verification_uri": prefill_url,
                "portal_uri": base_url,
                "user_code": code,
                "message": msg,
            }
            if self.login_hint:
                pending_payload["login_hint"] = self.login_hint
            print(json.dumps(pending_payload), flush=True)
        else:
            if open_browser:
                print("\nOpened your default web browser to Microsoft sign-in:", flush=True)
                print(f"  {prefill_url}", flush=True)
            else:
                print("\nOpen the following link in any browser:", flush=True)
                print(f"  {prefill_url}", flush=True)
            copy_note = " (copied to clipboard)" if copied else ""
            print(f"  Code: {code}{copy_note}", flush=True)
            print("Waiting for you to complete sign-in...", flush=True)

        interval = min(int(res.get("interval", 5)), 2)
        expires_in = int(res.get("expires_in", 900))
        deadline = time.time() + expires_in - 2

        while time.time() < deadline:
            time.sleep(interval)
            poll_resp = self.session.post(
                f"{LIVE_TOKEN_URL}?client_id={NINTENDO_SWITCH_TITLE_ID}",
                data={
                    "client_id": NINTENDO_SWITCH_TITLE_ID,
                    "device_code": res["device_code"],
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                cookies=cookies,
                timeout=15,
            )
            poll_data = poll_resp.json()
            if "error" in poll_data:
                if poll_data["error"] in ("authorization_pending", "slow_down"):
                    if poll_data["error"] == "slow_down":
                        interval = min(interval + 2, 5)
                    continue
                raise RuntimeError(
                    f"Failed to acquire authorization code ({poll_data.get('error')}): {poll_data.get('error_description')}"
                )
            if "access_token" in poll_data:
                self._save_live_token(
                    poll_data,
                    client_id=NINTENDO_SWITCH_TITLE_ID,
                    auth_method=("browser" if open_browser else "device"),
                )
                return poll_data["access_token"]

        raise RuntimeError("Authentication timed out waiting for user login.")

    def _msal_broker_token(self) -> str:
        """Sign in via Windows WebAccountManager broker popup using MSAL."""
        import msal
        import ctypes

        cache_file = self.cache_dir.parent.parent / "msal_cache.bin"
        token_cache = msal.SerializableTokenCache()
        try:
            if cache_file.exists():
                token_cache.deserialize(cache_file.read_text(encoding="utf-8"))
        except Exception:
            pass

        def _persist():
            try:
                if token_cache.has_state_changed:
                    cache_file.parent.mkdir(parents=True, exist_ok=True)
                    cache_file.write_text(token_cache.serialize(), encoding="utf-8")
            except Exception:
                pass

        app = msal.PublicClientApplication(
            AZURE_APP_CLIENT_ID,
            authority="https://login.microsoftonline.com/consumers",
            enable_broker_on_windows=True,
            token_cache=token_cache,
        )
        scopes = ["XboxLive.signin"]
        hint = (self.login_hint or "").strip().lower()
        res = None
        if hint or self.wam_id:
            try:
                for acct in app.get_accounts():
                    acct_user = str(acct.get("username", "")).lower()
                    acct_id = str(acct.get("home_account_id", "") or "")
                    if (hint and acct_user == hint) or (self.wam_id and acct_id == str(self.wam_id)):
                        res = app.acquire_token_silent(scopes, account=acct)
                        if res and "access_token" in res:
                            break
                        res = None
            except Exception:
                res = None

        if not res:
            hwnd = self.hwnd or 0
            if not hwnd:
                try:
                    hwnd = ctypes.windll.kernel32.GetConsoleWindow() or ctypes.windll.user32.GetForegroundWindow()
                except Exception:
                    hwnd = 0
            self._last_msa_was_interactive = True
            res = app.acquire_token_interactive(
                scopes,
                parent_window_handle=hwnd or app.CONSOLE_WINDOW_HANDLE,
                login_hint=self.login_hint or None,
                prompt=None if self.login_hint else "select_account",
            )

        _persist()
        if not res or "access_token" not in res:
            raise RuntimeError(
                f"Windows sign-in failed: {(res or {}).get('error_description') or (res or {}).get('error') or 'cancelled'}"
            )
        claims = res.get("id_token_claims") or {}
        email = claims.get("preferred_username") or claims.get("email") or self.login_hint
        if email:
            self.obtained_msa_email = email
        acct_info = res.get("account") if isinstance(res.get("account"), dict) else {}
        wam_acc_id = acct_info.get("home_account_id") or claims.get("oid") or claims.get("sub")
        if wam_acc_id:
            self.obtained_wam_account_id = str(wam_acc_id)
        try:
            self.live_cache.set_cached_partial({
                "token": {
                    "auth_method": "wam",
                    "client_id": AZURE_APP_CLIENT_ID,
                    "obtainedOn": int(time.time() * 1000),
                    "expires_in": int(res.get("expires_in", 3600)),
                    "wam_account_id": self.obtained_wam_account_id,
                    "msa_email": self.obtained_msa_email,
                }
            })
        except Exception:
            pass
        return res["access_token"]

    def get_msa_token(self, force_refresh: bool = False) -> str:
        self._last_msa_was_interactive = False
        if self.auth_method == "wam" and sys.platform == "win32":
            return self._msal_broker_token()

        cached = self._get_live_cached_tokens()
        if cached:
            if cached["valid"] and cached["access_token"] and not force_refresh:
                return cached["access_token"]
            if cached["refresh_token"]:
                try:
                    return self._refresh_live_token(cached["refresh_token"], cached.get("client_id"))
                except Exception:
                    pass

        self._last_msa_was_interactive = True
        if self.auth_method == "popup":
            return self._popup_live_oauth()
        if self.auth_method == "browser":
            return self._device_code_live_auth(open_browser=True)
        return self._device_code_live_auth(open_browser=False)

    # --- Xbox Live Tokens ---
    def _xbl_base_headers(self, signature: str, contract_version: str = "1") -> dict:
        return {
            "Cache-Control": "no-store, must-revalidate, no-cache",
            "x-xbl-contract-version": contract_version,
            "Signature": signature,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @staticmethod
    def _rps_ticket_candidates(msa_token: str) -> list[str]:
        raw = msa_token[2:] if msa_token.startswith(("t=", "d=")) else msa_token
        cands = []
        for c in (msa_token, f"t={raw}", f"d={raw}", raw):
            if c and c not in cands:
                cands.append(c)
        return cands

    def _get_xbl_user_token(self, msa_token: str) -> str:
        last_resp = None
        for rps_ticket in self._rps_ticket_candidates(msa_token):
            payload = {
                "RelyingParty": "http://auth.xboxlive.com",
                "TokenType": "JWT",
                "Properties": {
                    "AuthMethod": "RPS",
                    "SiteName": "user.auth.xboxlive.com",
                    "RpsTicket": rps_ticket,
                },
            }
            body = json.dumps(payload, separators=(",", ":"))
            sig = self.signer.sign_xbox_request(XBL_USER_AUTH_URL, "", body)
            headers = self._xbl_base_headers(sig, contract_version="2")
            headers["signature"] = sig
            resp = self.session.post(XBL_USER_AUTH_URL, headers=headers, data=body, timeout=15)
            last_resp = resp
            if resp.ok:
                ret = resp.json()
                self.xbl_cache.set_cached_partial({"userToken": ret})
                return ret["Token"]
        last_resp.raise_for_status()

    def _get_xbl_device_token(self, device_type: str = "Nintendo") -> str:
        ns = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")
        dev_id = str(uuid.uuid3(ns, str(int(time.time() * 1000))))
        ser_id = str(uuid.uuid3(ns, str(int(time.time() * 1000) + 1)))
        payload = {
            "Properties": {
                "AuthMethod": "ProofOfPossession",
                "Id": f"{{{dev_id}}}",
                "DeviceType": device_type,
                "SerialNumber": f"{{{ser_id}}}",
                "Version": "0.0.0",
                "ProofKey": self.signer.jwk,
            },
            "RelyingParty": "http://auth.xboxlive.com",
            "TokenType": "JWT",
        }
        body = json.dumps(payload, separators=(",", ":"))
        sig = self.signer.sign_xbox_request(XBL_DEVICE_AUTH_URL, "", body)
        headers = self._xbl_base_headers(sig, contract_version="1")
        resp = self.session.post(XBL_DEVICE_AUTH_URL, headers=headers, data=body, timeout=15)
        resp.raise_for_status()
        ret = resp.json()
        self.xbl_cache.set_cached_partial({"deviceToken": ret})
        return ret["Token"]

    def _get_xbl_title_token(self, msa_token: str, device_token: str) -> str:
        last_resp = None
        for rps_ticket in self._rps_ticket_candidates(msa_token):
            payload = {
                "Properties": {
                    "AuthMethod": "RPS",
                    "DeviceToken": device_token,
                    "RpsTicket": rps_ticket,
                    "SiteName": "user.auth.xboxlive.com",
                    "ProofKey": self.signer.jwk,
                },
                "RelyingParty": "http://auth.xboxlive.com",
                "TokenType": "JWT",
            }
            body = json.dumps(payload, separators=(",", ":"))
            sig = self.signer.sign_xbox_request(XBL_TITLE_AUTH_URL, "", body)
            headers = self._xbl_base_headers(sig, contract_version="1")
            resp = self.session.post(XBL_TITLE_AUTH_URL, headers=headers, data=body, timeout=15)
            last_resp = resp
            if resp.ok:
                ret = resp.json()
                self.xbl_cache.set_cached_partial({"titleToken": ret})
                return ret["Token"]
        last_resp.raise_for_status()

    def _get_xsts_token(
        self,
        user_token: str,
        device_token: str | None,
        title_token: str | None,
        relying_party: str,
    ) -> dict:
        props = {
            "UserTokens": [user_token],
            "SandboxId": "RETAIL",
        }
        if device_token:
            props["DeviceToken"] = device_token
        if title_token:
            props["TitleToken"] = title_token
        if device_token or title_token:
            props["ProofKey"] = self.signer.jwk

        payload = {
            "RelyingParty": relying_party,
            "TokenType": "JWT",
            "Properties": props,
        }
        body = json.dumps(payload, separators=(",", ":"))
        sig = self.signer.sign_xbox_request(XBL_XSTS_AUTH_URL, "", body)
        headers = self._xbl_base_headers(sig, contract_version="1")
        resp = self.session.post(XBL_XSTS_AUTH_URL, headers=headers, data=body, timeout=15)
        ret = resp.json()
        if not resp.ok:
            xerr = ret.get("XErr")
            if xerr in XBOX_LIVE_ERRORS:
                raise RuntimeError(XBOX_LIVE_ERRORS[xerr])
            raise RuntimeError(f"Xbox Live XSTS authentication failed (XErr: {xerr}): {ret}")

        xui = ret["DisplayClaims"]["xui"][0]
        xsts = {
            "userXUID": xui.get("xid") or None,
            "userHash": xui["uhs"],
            "XSTSToken": ret["Token"],
            "expiresOn": ret["NotAfter"],
        }
        self.xbl_cache.set_cached_partial({_create_hash(relying_party): xsts})
        return xsts

    def get_xbox_token(self, relying_party: str = XBL_RELYING_PARTY, force_refresh: bool = False) -> dict:
        cached_xbl = self.xbl_cache.get_cached()
        rp_hash = _create_hash(relying_party)

        if not force_refresh and rp_hash in cached_xbl:
            cached_xsts = cached_xbl[rp_hash]
            if isinstance(cached_xsts, dict) and _check_iso_valid(cached_xsts.get("expiresOn")):
                return cached_xsts

        # Fast path for MSAL tokens (Windows broker only on Windows)
        if self.auth_method == "wam" and sys.platform == "win32":
            msal_tok = self._msal_broker_token()
            ut = self._get_xbl_user_token("d=" + msal_tok)
            return self._get_xsts_token(ut, None, None, relying_party)

        # Popup OAuth (00000000402b5328) or Device/Browser flow (00000000441cc96b)
        for attempt in range(2):
            try:
                msa_token = self.get_msa_token(force_refresh=(attempt > 0 or force_refresh))
                cached_tok = self.live_cache.get_cached().get("token") or {}
                is_desktop_oauth = (
                    self.auth_method == "popup"
                    or cached_tok.get("auth_method") == "popup"
                    or cached_tok.get("client_id") == AZURE_APP_CLIENT_ID
                )
                ut = self._get_xbl_user_token(msa_token)
                if is_desktop_oauth:
                    try:
                        return self._get_xsts_token(ut, None, None, relying_party)
                    except Exception as xsts_err:
                        if any(msg in str(xsts_err) for msg in XBOX_LIVE_ERRORS.values()):
                            raise
                        dt = self._get_xbl_device_token("Win32")
                        return self._get_xsts_token(ut, dt, None, relying_party)

                dt = self._get_xbl_device_token("Nintendo")
                tt = None
                try:
                    tt = self._get_xbl_title_token(msa_token, dt)
                except Exception:
                    tt = None
                return self._get_xsts_token(ut, dt, tt, relying_party)
            except Exception:
                if attempt == 1 or getattr(self, "_last_msa_was_interactive", False):
                    raise

    # --- Minecraft Java Token & Profile ---
    def get_minecraft_java_token(self) -> tuple[str, dict]:
        cached_mca = self.mca_cache.get_cached().get("mca")
        mc_token = None
        if isinstance(cached_mca, dict):
            obtained_on = float(cached_mca.get("obtainedOn", 0))
            expires_in = float(cached_mca.get("expires_in", 0))
            if (obtained_on + expires_in * 1000.0 - time.time() * 1000.0) > 60000:
                mc_token = cached_mca.get("access_token")

        if not mc_token:
            for attempt in range(2):
                try:
                    xsts = self.get_xbox_token(MC_XSTS_RELYING_PARTY, force_refresh=(attempt > 0))
                    xbl_auth_str = f"XBL3.0 x={xsts['userHash']};{xsts['XSTSToken']}"
                    req_headers = {
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "User-Agent": "MinecraftLauncher/2.2.10675",
                    }
                    resp = self.session.post(
                        MC_LOGIN_WITH_XBOX_URL,
                        headers=req_headers,
                        json={"identityToken": xbl_auth_str},
                        timeout=15,
                    )
                    if not resp.ok:
                        alt_resp = self.session.post(
                            MC_LAUNCHER_LOGIN_URL,
                            headers=req_headers,
                            json={"xtoken": xbl_auth_str, "platform": "PC_LAUNCHER"},
                            timeout=15,
                        )
                        if alt_resp.ok:
                            resp = alt_resp
                    resp.raise_for_status()
                    mc_data = resp.json()
                    mc_data_to_save = dict(mc_data)
                    mc_data_to_save["obtainedOn"] = int(time.time() * 1000)
                    self.mca_cache.set_cached_partial({"mca": mc_data_to_save})
                    mc_token = mc_data["access_token"]
                    break
                except Exception:
                    if attempt == 1 or getattr(self, "_last_msa_was_interactive", False):
                        raise

        # Fetch Minecraft Java profile
        prof_resp = self.session.get(
            MC_PROFILE_URL,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "MinecraftLauncher/2.2.10675",
                "Authorization": f"Bearer {mc_token}",
            },
            timeout=15,
        )
        prof_resp.raise_for_status()
        return mc_token, prof_resp.json()


def _calculate_locator_bar_color(mc_uuid_hex: str) -> str:
    """Calculate vanilla Minecraft Locator Bar color for player UUID."""
    if calculate_locator_bar_color:
        try:
            return calculate_locator_bar_color(mc_uuid_hex)
        except Exception:
            pass

    import colorsys
    clean_hex = mc_uuid_hex.replace("-", "").strip()
    if len(clean_hex) < 32:
        return "#FFFFFF"
    most = int(clean_hex[:16], 16)
    least = int(clean_hex[16:32], 16)
    hilo = most ^ least
    hash_val = ((hilo >> 32) ^ hilo) & 0xFFFFFFFF
    raw_color = hash_val & 0xFFFFFF
    r = ((raw_color >> 16) & 0xFF) / 255.0
    g = ((raw_color >> 8) & 0xFF) / 255.0
    b = (raw_color & 0xFF) / 255.0
    h, s, _ = colorsys.rgb_to_hsv(r, g, b)
    nr, ng, nb = colorsys.hsv_to_rgb(h, s, 0.9)
    return f"#{int(round(nr * 255)):02X}{int(round(ng * 255)):02X}{int(round(nb * 255)):02X}"


def main():
    parser = argparse.ArgumentParser(description="Microsoft & Minecraft Java Account Authentication Manager.")
    parser.add_argument("--logout", action="store_true", help="Log out by clearing authentication cache.")
    parser.add_argument("--json", action="store_true", help="Output result in JSON format.")
    parser.add_argument("--profile", default="default_account", help="Profile name to authenticate as.")
    parser.add_argument("--login-hint", default=None, help="Optional Microsoft account email to pre-select.")

    # Login Method Flags
    parser.add_argument(
        "--method",
        choices=["auto", "wam", "popup", "browser", "device"],
        default="auto",
        help="Sign-in method: wam (Windows Security popup), popup (built-in Qt window), browser (default browser), device (device code link).",
    )
    parser.add_argument("--wam", "--windows-popup", dest="wam_flag", action="store_true", help="Use Windows Security PIN/Hello popup (Windows only).")
    parser.add_argument("--popup", dest="popup_flag", action="store_true", help="Use built-in Python Microsoft sign-in popup window.")
    parser.add_argument("--ipc-popup", dest="ipc_popup", action="store_true", help="Delegate popup window display to parent GUI via JSON/stdin.")
    parser.add_argument("--browser", dest="browser_flag", action="store_true", help="Open default web browser to pre-filled microsoft.com/link.")
    parser.add_argument("--device", "--device-code", dest="device_flag", action="store_true", help="Use device code flow (microsoft.com/link).")

    parser.add_argument("--wam-id", default=None, help="Optional Windows WebAccount ID.")
    parser.add_argument("--hwnd", type=int, default=0, help="Optional parent window handle.")
    parser.add_argument("--list-accounts", action="store_true", help="List all Microsoft accounts linked to Windows and exit.")
    args = parser.parse_args()

    # --list-accounts action
    if args.list_accounts:
        if get_windows_linked_microsoft_accounts:
            accs = get_windows_linked_microsoft_accounts()
            if args.json:
                print(json.dumps(accs, indent=2))
            else:
                print("\nMicrosoft Accounts Linked with Windows:")
                if not accs:
                    print("  (No linked Microsoft accounts detected)")
                for i, a in enumerate(accs, 1):
                    name = a.get("mc_username") or a.get("display_name") or a.get("xbox_gamertag") or ""
                    named_str = f" - {name}" if name else ""
                    print(f"  [{i}] {a.get('email')}{named_str} ({a.get('source', 'Windows')})")
                print()
        else:
            print("Account discovery requires utils.py.")
        return

    _pkg_dir = Path(__file__).resolve().parent
    _root_dir = _pkg_dir.parent if _pkg_dir.name == "backend" else _pkg_dir
    cache_dir = _root_dir / ".cache" / args.profile / "minecraft-auth-cache"

    if args.logout:
        if cache_dir.exists():
            def on_rm_error(func, path, exc_info):
                try:
                    os.chmod(path, stat.S_IWRITE)
                    func(path)
                except Exception:
                    pass

            try:
                shutil.rmtree(cache_dir, onerror=on_rm_error)
                if not args.json:
                    print("✨ Successfully logged out (authentication cache cleared).")
                else:
                    print('{"status": "logged_out"}')
            except Exception as e:
                if not args.json:
                    print(f"Error clearing cache: {e}")
                else:
                    print(json.dumps({"error": "Failed to clear cache", "details": str(e)}))
        else:
            if not args.json:
                print("You are already logged out (no cache found).")
            else:
                print('{"status": "already_logged_out"}')
        return

    # Determine auth method
    method = args.method
    explicit_wam = bool(args.wam_flag or args.method == "wam")
    if args.wam_flag:
        method = "wam"
    elif args.popup_flag:
        method = "popup"
    elif args.browser_flag:
        method = "browser"
    elif args.device_flag:
        method = "device"
    elif method == "auto":
        # Interactive CLI prompt if run directly in a terminal
        if not args.json and sys.stdin.isatty():
            print("\nPyMcLauncher Microsoft Sign-In")
            print("================================")
            print("Select sign-in method:")
            if sys.platform == "win32":
                print("  [1] Windows Security Popup (PIN / Windows Hello)")
                print("  [2] Popup Window (Built-in Microsoft sign-in window)")
                print("  [3] Default Web Browser (Opens your default browser)")
                try:
                    choice = input("\nEnter choice [1-3] (Default: 1): ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    return
                if choice == "2":
                    method = "popup"
                elif choice == "3":
                    method = "browser"
                else:
                    method = "wam"
            else:
                print("  [1] Popup Window (Built-in Microsoft sign-in window)")
                print("  [2] Default Web Browser (Opens your default browser)")
                print("  [3] Device Code Link (Manual link & code)")
                try:
                    choice = input("\nEnter choice [1-3] (Default: 1): ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    return
                if choice == "2":
                    method = "browser"
                elif choice == "3":
                    method = "device"
                else:
                    method = "popup"
        else:
            saved_tok = JsonCacheStore(cache_dir / "live-tokens.json").get_cached().get("token")
            saved_method = saved_tok.get("auth_method") if isinstance(saved_tok, dict) else None
            if saved_method in ("wam", "popup", "browser", "device") and (saved_method != "wam" or sys.platform == "win32"):
                method = saved_method
            else:
                method = "wam" if sys.platform == "win32" else "popup"

    # WAM is Windows-only
    if sys.platform != "win32" and method == "wam":
        if explicit_wam:
            msg = "Windows Security Popup (--wam) is only available on Windows."
            if args.json:
                print(json.dumps({"error": "Unsupported platform", "details": msg}))
            else:
                print(f"\n❌ {msg}")
            sys.exit(1)
        method = "popup"

    if not args.json:
        method_names = {
            "wam": "Windows Security native popup",
            "popup": "Built-in Python popup window",
            "browser": "Default web browser",
            "device": "Device code link",
        }
        print(f"Initializing Microsoft Authentication ({method_names.get(method, method)})...")

    try:
        authflow = PurePythonAuthFlow(
            args.profile,
            cache_dir,
            is_json=args.json,
            login_hint=args.login_hint,
            auth_method=method,
            wam_id=args.wam_id,
            hwnd=args.hwnd,
            ipc_popup=args.ipc_popup,
        )
        mc_token, mc_profile = authflow.get_minecraft_java_token()

        # Fetch Xbox Gamertag, Gamerpic, and Gamerscore
        xsts = authflow.get_xbox_token(XBL_RELYING_PARTY)
        xbl_token = f"XBL3.0 x={xsts['userHash']};{xsts['XSTSToken']}"

        gamertag = "Unknown Gamertag"
        gamerpic = ""
        gamerscore = "0"
        try:
            prof_res = authflow.session.get(
                "https://profile.xboxlive.com/users/me/profile/settings?settings=Gamertag,GameDisplayPicRaw,Gamerscore",
                headers={
                    "x-xbl-contract-version": "2",
                    "Authorization": xbl_token,
                },
                timeout=10,
            )
            if prof_res.ok:
                settings_list = prof_res.json().get("profileUsers", [{}])[0].get("settings", [])
                for s in settings_list:
                    sid = s.get("id")
                    val = s.get("value")
                    if sid == "Gamertag":
                        gamertag = val
                    elif sid == "GameDisplayPicRaw":
                        gamerpic = val
                    elif sid == "Gamerscore":
                        gamerscore = val
        except Exception:
            pass

        loc_color = _calculate_locator_bar_color(mc_profile.get("id", ""))

        if args.json:
            out_payload = {
                "mc_username": mc_profile.get("name"),
                "mc_uuid": mc_profile.get("id"),
                "mc_access_token": mc_token,
                "xbox_gamertag": gamertag,
                "xbox_gamerpic": gamerpic,
                "xbox_gamerscore": gamerscore,
                "locator_bar_color": loc_color,
            }
            if authflow.obtained_msa_email:
                out_payload["msa_email"] = authflow.obtained_msa_email
            if authflow.obtained_wam_account_id:
                out_payload["wam_account_id"] = authflow.obtained_wam_account_id
            print(json.dumps(out_payload))
        else:
            print("\n✨ Authentication Successful!")
            print(f"  Minecraft Username: {mc_profile.get('name')}")
            print(f"  Minecraft UUID:     {mc_profile.get('id')}")
            print(f"  Locator Bar Color:  {loc_color}")
            print(f"  Xbox Gamertag:      {gamertag}")
            print(f"  Xbox Gamerscore:    {gamerscore}")
            if authflow.obtained_msa_email:
                print(f"  Microsoft Account:  {authflow.obtained_msa_email}")

    except Exception as e:
        if args.json:
            print(json.dumps({"error": "Authentication failed", "details": str(e)}))
        else:
            print(f"\n❌ Authentication failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
