#!/usr/bin/env python3
"""
Vercel serverless handler (flat layout).

Usage:
    GET  /api/spin?uid=<UID>&pass=<PASSWORD>[&payload=<hex>]
    POST /api/spin  { "uid": "...", "pass": "..." }
Returns only the gained item(s) as JSON.

Visiting the base URL (no query string) returns the plain text "Running".
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

# --- Make output_pb2.py (same directory as this file) importable -------- #
try:
    _HERE = os.path.dirname(os.path.abspath(__file__))
    if _HERE and _HERE not in sys.path:
        sys.path.insert(0, _HERE)
except NameError:
    # __file__ is not always defined inside serverless runtimes
    pass

import aiohttp

_PB2_IMPORT_ERROR = None
try:
    import output_pb2
except Exception as e:
    output_pb2 = None
    _PB2_IMPORT_ERROR = str(e)


# ------------------------------------------------------------------ #
#  CONSTANTS
# ------------------------------------------------------------------ #
EXTERNAL_API_URL = "https://divan-jwt-gen.vercel.app/guest"
RELEASE_VERSION  = "OB55"
DEFAULT_URL      = "https://client.ind.freefiremobile.com"
NARUTO_PAYLOAD   = "D120B9DAAC2C87872B8C115DFD74A832"

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
            if shift > 63:            # guard against runaway shifts
                break
        if 100000000 <= value <= 999999999:
            if not any(x["id"] == value for x in items):
                items.append({"id": value, "name": RARE_ITEMS_DB.get(value)})
    return items


def parse_gacha_response(data: bytes):
    items = []

    # 1) gzip transparent
    try:
        if data.startswith(b"\x1f\x8b"):
            data = zlib.decompress(data, 16 + zlib.MAX_WBITS)
    except Exception:
        pass

    # 2) protobuf
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

    # 3) plain text fallback
    if not items:
        try:
            for num in re.findall(r"\d{9}", data.decode("utf-8", errors="ignore")):
                iid = int(num)
                if 100000000 <= iid <= 999999999 and not any(x["id"] == iid for x in items):
                    items.append({"id": iid, "name": RARE_ITEMS_DB.get(iid)})
        except Exception:
            pass

    # 4) raw byte scan
    if not items:
        items = _extract_ids_from_bytes(data)

    return items


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
        return {"success": False,
                "error": f"protobuf_load_failed: {_PB2_IMPORT_ERROR}"}

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
        return {
            "success": True,
            "uid": uid,
            "region": region,
            "items": items,          # <-- ONLY the gained item(s)
        }


# ------------------------------------------------------------------ #
#  VERCEL HANDLER
# ------------------------------------------------------------------ #
def _first(params: dict, *keys):
    """Safely fetch the first value for any of the given keys."""
    for k in keys:
        v = params.get(k)
        if isinstance(v, list) and v:
            return v[0]
        if isinstance(v, str):
            return v
    return None


class handler(BaseHTTPRequestHandler):

    # ---------- helpers ---------- #
    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_text(self, code: int, text: str):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    # ---------- core ---------- #
    def _run(self, params: dict):
        uid = _first(params, "uid")
        pwd = _first(params, "pass", "password")
        payload_hex = _first(params, "payload")

        if not uid or not pwd:
            self._send_json(
                400,
                {"success": False,
                 "error": "missing_params: uid & pass required"},
            )
            return

        try:
            result = asyncio.run(spin(uid, pwd, payload_hex))
        except Exception as e:
            result = {"success": False, "error": f"internal_error: {e}"}

        self._send_json(200 if result.get("success") else 502, result)

    # ---------- HTTP verbs ---------- #
    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query, keep_blank_values=True)

        # No query string  ->  home page
        if not query:
            self._send_text(200, "Running")
            return

        self._run(query)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("JSON body must be an object")
            params = {k: [str(v)] for k, v in data.items()}
        except Exception as e:
            self._send_json(
                400,
                {"success": False, "error": f"invalid_json_body: {e}"},
            )
            return
        self._run(params)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()
