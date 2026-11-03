"""
Pure-Python Microsoft / Xbox Live / Minecraft Java Authentication.
Replaces Node.js / prismarine-auth with zero external dependencies beyond `requests`,
while remaining 100% compatible with existing `.cache/<profile>/minecraft-auth-cache/` files.
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

# Constants matching prismarine-auth (Titles.MinecraftNintendoSwitch & Endpoints)
NINTENDO_SWITCH_TITLE_ID = "00000000441cc96b"
AZURE_APP_CLIENT_ID = "00000000402b5328"
LIVE_SCOPE = "service::user.auth.xboxlive.com::MBI_SSL"

LIVE_DEVICE_CODE_URL = "https://login.live.com/oauth20_connect.srf"
LIVE_TOKEN_URL = "https://login.live.com/oauth20_token.srf"

XBL_DEVICE_AUTH_URL = "https://device.auth.xboxlive.com/device/authenticate"
XBL_TITLE_AUTH_URL = "https://title.auth.xboxlive.com/title/authenticate"
XBL_USER_AUTH_URL = "https://user.auth.xboxlive.com/user/authenticate"
XBL_XSTS_AUTH_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"

MC_XSTS_RELYING_PARTY = "rp://api.minecraftservices.com/"
XBL_RELYING_PARTY = "http://xboxlive.com"

MC_LOGIN_WITH_XBOX_URL = "https://api.minecraftservices.com/authentication/login_with_xbox"
MC_PROFILE_URL = "https://api.minecraftservices.com/minecraft/profile"
MC_ENTITLEMENTS_URL = "https://api.minecraftservices.com/entitlements/mcstore"

XBOX_LIVE_ERRORS = {
    2148916227: "Your account was banned by Xbox for violating one or more Community Standards for Xbox and is unable to be used.",
    2148916229: "Your account is currently restricted and your guardian has not given you permission to play online. Login to https://account.microsoft.com/family/ and have your guardian change your permissions.",
    2148916233: "Your account currently does not have an Xbox profile. Please create one at https://signup.live.com/signup",
    2148916234: "Your account has not accepted Xbox's Terms of Service. Please login and accept them.",
    2148916235: "Your account resides in a region that Xbox has not authorized use from. Xbox has blocked your attempt at logging in.",
    2148916236: "Your account requires proof of age. Please login to https://login.live.com/login.srf and provide proof of age.",
    2148916237: "Your account has reached its limit for playtime. Your account has been blocked from logging in.",
    2148916238: "The account date of birth is under 18 years and cannot proceed unless the account is added to a family by an adult.",
}


# ---------------------------------------------------------------------------
# Pure-Python NIST P-256 (secp256r1) ECDSA for Xbox Live Request Signing
# ---------------------------------------------------------------------------
_P256_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_P256_A = -3 % _P256_P
_P256_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
_P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_P256_GX = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
_P256_GY = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5


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
        lam = (3 * x1 * x1 + _P256_A) * pow(2 * y1, -1, _P256_P) % _P256_P
    else:
        lam = (y2 - y1) * pow(x2 - x1, -1, _P256_P) % _P256_P
    x3 = (lam * lam - x1 - x2) % _P256_P
    y3 = (lam * (x1 - x3) - y1) % _P256_P
    return (x3, y3)


def _p256_mul(k: int, pt):
    res = None
    addend = pt
    while k:
        if k & 1:
            res = _p256_add(res, addend)
        addend = _p256_add(addend, addend)
        k >>= 1
    return res


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class P256Signer:
    """Pure-Python P-256 ECDSA keypair and Xbox Live HTTP request signer."""

    def __init__(self):
        self.d = secrets.randbelow(_P256_N - 1) + 1
        qx, qy = _p256_mul(self.d, (_P256_GX, _P256_GY))
        self.jwk = {
            "kty": "EC",
            "crv": "P-256",
            "x": _b64url(qx.to_bytes(32, "big")),
            "y": _b64url(qy.to_bytes(32, "big")),
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
            s = (pow(k, -1, _P256_N) * (e + r * self.d)) % _P256_N
            if s != 0:
                return r.to_bytes(32, "big") + s.to_bytes(32, "big")

    def sign_xbox_request(self, url: str, auth_token: str, payload_str: str) -> str:
        # Windows epoch timestamp in 100-nanosecond intervals since 1601-01-01
        win_ts = (int(time.time()) + 11644473600) * 10000000
        path_only = urllib.parse.urlsplit(url).path

        buf = bytearray()
        buf.extend(struct.pack(">iB", 1, 0))
        buf.extend(struct.pack(">QB", win_ts, 0))
        buf.extend(b"POST\x00")
        buf.extend(path_only.encode("utf-8") + b"\x00")
        buf.extend(auth_token.encode("utf-8") + b"\x00")
        buf.extend(payload_str.encode("utf-8") + b"\x00")

        sig = self.sign_bytes(bytes(buf))
        header = struct.pack(">iQ", 1, win_ts) + sig
        return base64.b64encode(header).decode("ascii")


# ---------------------------------------------------------------------------
# Cache Helpers (compatible with prismarine-auth FileCache)
# ---------------------------------------------------------------------------
def _create_hash(text: str) -> str:
    return hashlib.sha1((text or "").encode("latin1")).hexdigest()[:6]


class FileCache:
    def __init__(self, file_path: Path):
        self.file_path = file_path

    def get_cached(self) -> dict:
        if not self.file_path.exists():
            return {}
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def set_cached_partial(self, data: dict):
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        cur = self.get_cached()
        cur.update(data)
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump(cur, f, indent=2)


def _check_iso_valid(iso_str: str) -> bool:
    if not iso_str:
        return False
    try:
        s = iso_str.strip()
        if s.endswith("Z"):
            s = s[:-1]
        if "." in s:
            main_part, frac = s.split(".", 1)
            frac = (frac + "000000")[:6]
            s = f"{main_part}.{frac}"
        dt = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        remaining_ms = (dt.timestamp() - time.time()) * 1000.0
        return remaining_ms > 60000
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Token Managers (Live, Xbox, Minecraft Java)
# ---------------------------------------------------------------------------
class PurePythonAuthFlow:
    def __init__(self, profile_name: str, cache_dir: Path, is_json: bool = False):
        self.profile_name = profile_name
        self.cache_dir = cache_dir
        self.is_json = is_json
        self.signer = P256Signer()
        self.session = requests.Session()

        p_hash = _create_hash(profile_name)
        self.live_cache = FileCache(cache_dir / f"{p_hash}_live-cache.json")
        self.xbl_cache = FileCache(cache_dir / f"{p_hash}_xbl-cache.json")
        self.mca_cache = FileCache(cache_dir / f"{p_hash}_mca-cache.json")

    # --- Microsoft Live OAuth2 ---
    def _get_live_cached_tokens(self):
        token = self.live_cache.get_cached().get("token")
        if not token or not isinstance(token, dict):
            return None
        obtained_on = float(token.get("obtainedOn", 0))
        expires_in = float(token.get("expires_in", 0))
        now_ms = time.time() * 1000.0
        # prismarine-auth stored obtainedOn in ms; check both ms and s just in case
        expiry_ms = obtained_on + (expires_in * 1000.0 if expires_in < 10000000 else expires_in)
        valid = (expiry_ms - now_ms) > 60000
        return {
            "valid": valid,
            "access_token": token.get("access_token"),
            "refresh_token": token.get("refresh_token"),
        }

    def _save_live_token(self, token_data: dict):
        saved = dict(token_data)
        saved["obtainedOn"] = int(time.time() * 1000)
        self.live_cache.set_cached_partial({"token": saved})

    def _refresh_live_token(self, refresh_token: str) -> str:
        resp = self.session.post(
            LIVE_TOKEN_URL,
            data={
                "scope": LIVE_SCOPE,
                "client_id": NINTENDO_SWITCH_TITLE_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if "access_token" not in data:
            raise RuntimeError(f"Failed to refresh Microsoft token: {data}")
        self._save_live_token(data)
        return data["access_token"]

    def _device_code_live_auth(self) -> str:
        resp = self.session.post(
            LIVE_DEVICE_CODE_URL,
            data={
                "scope": LIVE_SCOPE,
                "client_id": NINTENDO_SWITCH_TITLE_ID,
                "response_type": "device_code",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        resp.raise_for_status()
        res = resp.json()

        cookies = {}
        for k, v in resp.cookies.items():
            cookies[k] = v

        code = res.get("user_code") or res.get("userCode")
        ver_uri = res.get("verification_uri") or res.get("verificationUri") or "https://www.microsoft.com/link"
        direct_url = f"https://microsoft.com/link?otc={code}"
        msg = f"To sign in, use a web browser to open the page {ver_uri} and use the code {code} or visit {direct_url}"

        if self.is_json:
            print(
                json.dumps(
                    {
                        "status": "pending_login",
                        "verification_uri": direct_url,
                        "user_code": code,
                        "message": msg,
                    }
                ),
                flush=True,
            )
        else:
            print("\nOpening your web browser...", flush=True)
            print(f"---> [COPIED] Authentication Code: {code}", flush=True)
            print("---> The code has been copied to your clipboard. Paste it in your browser!", flush=True)
            print(f"\n{msg}", flush=True)
            if os.name == "nt":
                try:
                    subprocess.run(f"echo {code} | clip", shell=True)
                    os.startfile(direct_url)
                except Exception:
                    pass

        interval = int(res.get("interval", 5))
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
                    continue
                raise RuntimeError(
                    f"Failed to acquire authorization code ({poll_data.get('error')}): {poll_data.get('error_description')}"
                )
            if "access_token" in poll_data:
                self._save_live_token(poll_data)
                return poll_data["access_token"]

        raise RuntimeError("Authentication timed out waiting for user login.")

    def get_msa_token(self, force_refresh: bool = False) -> str:
        cached = self._get_live_cached_tokens()
        if cached:
            if cached["valid"] and cached["access_token"] and not force_refresh:
                return cached["access_token"]
            if cached["refresh_token"]:
                try:
                    return self._refresh_live_token(cached["refresh_token"])
                except Exception:
                    pass
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

    def _get_xbl_user_token(self, msa_token: str) -> str:
        payload = {
            "RelyingParty": "http://auth.xboxlive.com",
            "TokenType": "JWT",
            "Properties": {
                "AuthMethod": "RPS",
                "SiteName": "user.auth.xboxlive.com",
                "RpsTicket": f"t={msa_token}",
            },
        }
        body = json.dumps(payload, separators=(",", ":"))
        sig = self.signer.sign_xbox_request(XBL_USER_AUTH_URL, "", body)
        headers = self._xbl_base_headers(sig, contract_version="2")
        headers["signature"] = sig
        resp = self.session.post(XBL_USER_AUTH_URL, headers=headers, data=body, timeout=15)
        resp.raise_for_status()
        ret = resp.json()
        self.xbl_cache.set_cached_partial({"userToken": ret})
        return ret["Token"]

    def _get_xbl_device_token(self) -> str:
        ns = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")
        dev_id = str(uuid.uuid3(ns, str(int(time.time() * 1000))))
        ser_id = str(uuid.uuid3(ns, str(int(time.time() * 1000) + 1)))
        payload = {
            "Properties": {
                "AuthMethod": "ProofOfPossession",
                "Id": f"{{{dev_id}}}",
                "DeviceType": "Nintendo",
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
        payload = {
            "Properties": {
                "AuthMethod": "RPS",
                "DeviceToken": device_token,
                "RpsTicket": f"t={msa_token}",
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
        resp.raise_for_status()
        ret = resp.json()
        self.xbl_cache.set_cached_partial({"titleToken": ret})
        return ret["Token"]

    def _get_xsts_token(self, user_token: str, device_token: str, title_token: str, relying_party: str) -> dict:
        payload = {
            "RelyingParty": relying_party,
            "TokenType": "JWT",
            "Properties": {
                "UserTokens": [user_token],
                "DeviceToken": device_token,
                "TitleToken": title_token,
                "ProofKey": self.signer.jwk,
                "SandboxId": "RETAIL",
            },
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

        # Obtain fresh tokens signed with our current P-256 session key
        for attempt in range(2):
            try:
                msa_token = self.get_msa_token(force_refresh=(attempt > 0 or force_refresh))
                ut = self._get_xbl_user_token(msa_token)
                dt = self._get_xbl_device_token()
                tt = self._get_xbl_title_token(msa_token, dt)
                return self._get_xsts_token(ut, dt, tt, relying_party)
            except Exception:
                if attempt == 1:
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
                    resp = self.session.post(
                        MC_LOGIN_WITH_XBOX_URL,
                        headers={
                            "Content-Type": "application/json",
                            "User-Agent": "MinecraftLauncher/2.2.10675",
                        },
                        json={"identityToken": f"XBL3.0 x={xsts['userHash']};{xsts['XSTSToken']}"},
                        timeout=15,
                    )
                    resp.raise_for_status()
                    mc_data = resp.json()
                    mc_data_to_save = dict(mc_data)
                    mc_data_to_save["obtainedOn"] = int(time.time() * 1000)
                    self.mca_cache.set_cached_partial({"mca": mc_data_to_save})
                    mc_token = mc_data["access_token"]
                    break
                except Exception:
                    if attempt == 1:
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
    clean_hex = mc_uuid_hex.replace("-", "")
    most = int(clean_hex[:16], 16)
    least = int(clean_hex[16:32], 16)
    hilo = most ^ least
    hash_val = ((hilo >> 32) ^ hilo) & 0xFFFFFFFF
    if hash_val & 0x80000000:
        hash_val -= 0x100000000
    raw_color = hash_val & 0xFFFFFF
    return f"#{raw_color:06X}"


def main():
    parser = argparse.ArgumentParser(description="Fetch Minecraft and Xbox usernames (pure Python).")
    parser.add_argument("--logout", action="store_true", help="Log out by clearing the authentication cache.")
    parser.add_argument("--json", action="store_true", help="Output the result in JSON format.")
    parser.add_argument("--profile", default="default_account", help="The profile name to authenticate as.")
    args = parser.parse_args()

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
        print("Initializing Microsoft Authentication...")

    try:
        authflow = PurePythonAuthFlow(args.profile, cache_dir, is_json=args.json)
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
                prof_data = prof_res.json()
                users = prof_data.get("profileUsers") or []
                if users and "settings" in users[0]:
                    for s in users[0]["settings"]:
                        sid = s.get("id")
                        val = s.get("value", "")
                        if sid == "Gamertag":
                            gamertag = val
                        elif sid == "GameDisplayPicRaw":
                            gamerpic = val
                        elif sid == "Gamerscore":
                            gamerscore = val
        except Exception:
            pass

        presence_state = "Unknown"
        try:
            pres_res = authflow.session.get(
                "https://userpresence.xboxlive.com/users/me",
                headers={
                    "x-xbl-contract-version": "3",
                    "Authorization": xbl_token,
                    "Accept": "application/json",
                },
                timeout=10,
            )
            if pres_res.ok:
                pres_data = pres_res.json()
                if pres_data and pres_data.get("state"):
                    presence_state = pres_data["state"]
        except Exception:
            pass

        owns_minecraft = True
        try:
            ent_res = authflow.session.get(
                MC_ENTITLEMENTS_URL,
                headers={"Authorization": f"Bearer {mc_token}"},
                timeout=10,
            )
            if ent_res.ok:
                ent_data = ent_res.json()
                if ent_data and "items" in ent_data:
                    owns_minecraft = len(ent_data["items"]) > 0
                else:
                    owns_minecraft = False
        except Exception:
            pass

        launch_client_id = str(uuid.uuid4())
        hex_color = _calculate_locator_bar_color(mc_profile["id"])

        if args.json:
            print(
                json.dumps(
                    {
                        "xbox_gamertag": gamertag,
                        "xbox_gamerpic_url": gamerpic,
                        "xbox_gamerscore": gamerscore,
                        "xbox_xuid": xsts.get("userXUID"),
                        "mc_username": mc_profile["name"],
                        "mc_uuid": mc_profile["id"],
                        "mc_access_token": mc_token,
                        "launch_client_id": launch_client_id,
                        "azure_client_id": AZURE_APP_CLIENT_ID,
                        "locator_bar_color": hex_color,
                        "xbox_presence_state": presence_state,
                        "owns_minecraft": owns_minecraft,
                    }
                ),
                flush=True,
            )
        else:
            print("\n=============================================")
            print(f"🎮 Your Xbox Gamertag is: {gamertag}")
            if gamerpic:
                print(f"🖼️ Your Xbox Gamerpic URL is: {gamerpic}")
            print(f"🎮 Your Xbox Gamerscore is: {gamerscore}")
            print(f"🆔 Your Xbox XUID is: {xsts.get('userXUID')}")
            print(f"✨ Your Minecraft Java Username is: {mc_profile['name']}")
            print(f"🔑 Your Java UUID is: {mc_profile['id']}")
            print(f"🔐 Your Java Access Token is: {mc_token}")
            print(f"🛠️ Launch Client ID (--clientId): {launch_client_id}")
            print(f"🌐 Azure App Client ID: {AZURE_APP_CLIENT_ID}")
            print(f"🎨 Your Locator Bar Color is: {hex_color}")
            print(f"🎮 Owns Minecraft: {owns_minecraft}")
            print("=============================================\n")

    except Exception as err:
        if args.json:
            print(json.dumps({"error": "Authentication failed", "details": str(err)}), flush=True)
        else:
            print("\nFailed to authenticate:", file=sys.stderr)
            print(str(err), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
