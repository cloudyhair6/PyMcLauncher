#!/usr/bin/env python3
"""
Microsoft & Minecraft Java Account Authentication Manager (ms_accounts.py)

Can be used both as a standalone tool or as a module for PyMcLauncher.
Only requires utils.py (or can even run standalone without it).

Supported Sign-in Methods:
  1. Windows Security Popup (--wam / --windows-popup) [Windows 10/11 only]:
     Uses native Windows Web Account Manager (WAM) broker via MSAL.
     Sign in with Windows Hello, PIN, or pre-linked Windows accounts without typing passwords.
  2. Web Browser (--browser):
     Opens your default web browser (Chrome, Edge, Firefox, etc.) for standard Microsoft OAuth.
  3. Device Code Link (--device / --device-code) [Cross-platform, default on macOS/Linux]:
     Generates a one-time code, copies it to clipboard, and opens https://www.microsoft.com/link.

Usage:
  python3 ms_accounts.py
  python3 ms_accounts.py --device
  python3 ms_accounts.py --wam
  python3 ms_accounts.py --browser
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
    ):
        self.profile = profile
        self.cache_dir = cache_dir
        self.is_json = is_json
        self.login_hint = login_hint
        self.auth_method = auth_method.lower() if auth_method else "auto"
        self.wam_id = wam_id
        self.hwnd = hwnd

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
        }

    def _save_live_token(self, token_data: dict):
        d = dict(token_data)
        d["obtainedOn"] = int(time.time() * 1000)
        self.live_cache.set_cached_partial({"token": d})

    def _refresh_live_token(self, refresh_token: str) -> str:
        body = {
            "client_id": NINTENDO_SWITCH_TITLE_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": LIVE_SCOPE,
        }
        resp = self.session.post(LIVE_TOKEN_URL, data=body, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        self._save_live_token(data)
        return data["access_token"]

    def _device_code_live_auth(self) -> str:
        body = {
            "client_id": NINTENDO_SWITCH_TITLE_ID,
            "scope": LIVE_SCOPE,
            "response_type": "code",
        }
        resp = self.session.post(LIVE_DEVICE_CODE_URL, data=body, timeout=15)
        resp.raise_for_status()
        res = resp.json()
        cookies = resp.cookies

        direct_url = res.get("verification_uri", "https://www.microsoft.com/link")
        code = res["user_code"]
        msg = f"To sign in, use a web browser to open the page {direct_url} and enter the code {code} to authenticate."

        if self.is_json:
            pending_payload = {
                "status": "pending_login",
                "verification_uri": direct_url,
                "user_code": code,
                "message": msg,
            }
            if self.login_hint:
                pending_payload["login_hint"] = self.login_hint
            print(json.dumps(pending_payload), flush=True)
        else:
            print("\nOpening your web browser...", flush=True)
            print(f"---> [COPIED] Authentication Code: {code}", flush=True)
            print("---> The code has been copied to your clipboard. Paste it into the page!", flush=True)
            print(f"\n{msg}", flush=True)
            try:
                if sys.platform == "darwin":
                    subprocess.run(["pbcopy"], input=code.strip().encode("utf-8"), check=False)
                    subprocess.run(["open", direct_url], check=False)
                elif os.name == "nt":
                    subprocess.run(f"echo {code} | clip", shell=True)
                    os.startfile(direct_url)
                else:
                    import webbrowser
                    try:
                        subprocess.run(["xclip", "-selection", "clipboard"], input=code.strip().encode("utf-8"), check=False)
                    except Exception:
                        pass
                    webbrowser.open(direct_url)
            except Exception:
                pass

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
                self._save_live_token(poll_data)
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
        try:
            for acct in app.get_accounts():
                if not hint or str(acct.get("username", "")).lower() == hint:
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
        return res["access_token"]

    def _msal_browser_token(self) -> str:
        """Sign in via the default web browser (OAuth loopback) using MSAL."""
        import msal

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
            enable_broker_on_windows=False,
            token_cache=token_cache,
        )
        scopes = ["XboxLive.signin"]
        hint = (self.login_hint or "").strip().lower()
        res = None
        try:
            for acct in app.get_accounts():
                if not hint or str(acct.get("username", "")).lower() == hint:
                    res = app.acquire_token_silent(scopes, account=acct)
                    if res and "access_token" in res:
                        break
                    res = None
        except Exception:
            res = None

        if not res:
            if not self.is_json:
                print("\nOpening your default web browser for Microsoft sign-in...")
            self._last_msa_was_interactive = True
            res = app.acquire_token_interactive(
                scopes,
                login_hint=self.login_hint or None,
                prompt="select_account" if not self.login_hint else None,
            )

        _persist()
        if not res or "access_token" not in res:
            raise RuntimeError(
                f"Browser sign-in failed: {(res or {}).get('error_description') or (res or {}).get('error') or 'cancelled'}"
            )
        claims = res.get("id_token_claims") or {}
        email = claims.get("preferred_username") or claims.get("email") or self.login_hint
        if email:
            self.obtained_msa_email = email
        return res["access_token"]

    def get_msa_token(self, force_refresh: bool = False) -> str:
        self._last_msa_was_interactive = False
        cached = self._get_live_cached_tokens()
        if cached:
            raw_tok_entry = self.live_cache.get_cached().get("token") or {}
            has_extra = bool(raw_tok_entry.get("extra_tokens"))
            if cached["valid"] and cached["access_token"] and not force_refresh and (self.auth_method != "wam" or has_extra):
                return cached["access_token"]
            if cached["refresh_token"]:
                try:
                    return self._refresh_live_token(cached["refresh_token"])
                except Exception:
                    pass

        self._last_msa_was_interactive = True
        if self.auth_method == "wam" and sys.platform == "win32":
            return self._msal_broker_token()
        return self._device_code_live_auth()

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

        # Standard / device-code flow
        for attempt in range(2):
            try:
                msa_token = self.get_msa_token(force_refresh=(attempt > 0 or force_refresh))
                ut = self._get_xbl_user_token(msa_token)
                dt = self._get_xbl_device_token()
                return self._get_xsts_token(ut, dt, None, relying_party)
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
        choices=["auto", "wam", "browser", "device"],
        default="auto",
        help="Sign-in method: wam (Windows Security popup), browser (web browser OAuth), device (device code link).",
    )
    parser.add_argument("--wam", "--windows-popup", dest="wam_flag", action="store_true", help="Use Windows Security PIN/Hello popup.")
    parser.add_argument("--browser", dest="browser_flag", action="store_true", help="Open default web browser for OAuth login.")
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

    # Determine auth method
    method = args.method
    if args.wam_flag:
        method = "wam"
    elif args.browser_flag or args.device_flag:
        method = "device"
    elif method == "auto":
        # Interactive CLI prompt if run directly in a terminal
        if not args.json and sys.stdin.isatty():
            print("\nPyMcLauncher Microsoft Sign-In")
            print("================================")
            print("Select sign-in method:")
            if sys.platform == "win32":
                print("  [1] Windows Security Popup (PIN / Windows Hello)")
                print("  [2] Web Browser / Device Link (Opens default browser to microsoft.com/link)")
                try:
                    choice = input("\nEnter choice [1-2] (Default: 1): ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    return
                if choice == "2":
                    method = "device"
                else:
                    method = "wam"
            else:
                print("  [1] Web Browser / Device Link (Opens default browser to microsoft.com/link)")
                try:
                    choice = input("\nEnter choice [1] (Default: 1): ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    return
                method = "device"
        else:
            method = "wam" if sys.platform == "win32" else "device"

    # WAM is Windows-only; on macOS/Linux gracefully fall back to device code flow
    if sys.platform != "win32" and method == "wam":
        method = "device"

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

    if not args.json:
        method_names = {
            "wam": "Windows Security native popup",
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
