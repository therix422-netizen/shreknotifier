#!/usr/bin/env python3
# ================================================================
# SHREK NOTIFIER — SERVER HOPPER + BOT STATUS MONITOR
# pip install websockets aiohttp requests
# python server_hopper_backend.py
# ================================================================

import asyncio, json, time, datetime, threading, os, hashlib, base64, hmac
try:
    from Crypto.Cipher import ChaCha20_Poly1305
except ImportError:
    from Cryptodome.Cipher import ChaCha20_Poly1305
from collections import deque
from urllib.parse import urlparse, parse_qs
import aiohttp
import websockets
from websockets.asyncio.server import serve as ws_serve
import requests as req_lib

# ================================================================
# CONFIG
# ================================================================
PORT            = int(os.environ.get("PORT", 3001))  # Railway sets PORT automatically

# ── AUTH + CHACHA20-POLY1305 ─────────────────────────────────────
# Set in Railway environment variables:
#   AUTH_KEYS  = comma-separated valid keys  e.g. shreknotifiiifier23242!
#   ENC_SECRET = encryption secret (shared with Lua clients)
_raw_keys  = os.environ.get("AUTH_KEYS",  "shreknotifiiifier23242!")
AUTH_KEYS  = set(k.strip() for k in _raw_keys.split(",") if k.strip())
ENC_SECRET = os.environ.get("ENC_SECRET", "xK9#m5P2$vL7nQ4@32wR8")

def _hkdf32(ikm: bytes, salt: bytes = b"", info: bytes = b"") -> bytes:
    """HKDF-SHA256, extract+expand, 32 bytes output"""
    if not salt: salt = bytes(32)
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"", hashlib.sha256).digest()

def derive_key(auth_key: str) -> bytes:
    """Derive per-client encryption key from shared secret + auth key"""
    return _hkdf32(
        ikm  = ENC_SECRET.encode(),
        salt = auth_key.encode(),
        info = b"shrek-chacha20-v1"
    )

def encrypt_payload(data: str, auth_key: str) -> str:
    """ChaCha20-Poly1305 encrypt JSON string -> base64(nonce+tag+ct)"""
    key    = derive_key(auth_key)
    nonce  = os.urandom(12)
    cipher = ChaCha20_Poly1305.new(key=key, nonce=nonce)
    ct, tag = cipher.encrypt_and_digest(data.encode())
    return base64.b64encode(nonce + tag + ct).decode()
PLACE_ID        = "109983668079237"
MIN_P           = 6
MAX_P           = 8
PAGE_LIMIT      = 100
SCAN_DELAY      = 0.05       # seconds between roblox API pages
COOLDOWN        = 90         # seconds before a used server can be reused

# Bot status monitor
BOT_API_URL     = "https://status.therixyt4.workers.dev/bots"
BOT_API_URL     = "https://status.therixyt4.workers.dev/bots"
TOTAL_BOTS      = 600        # total number of bots you own
STATUS_INTERVAL = 300        # check every 5 minutes

# ================================================================
# SERVER HOPPER STATE
# ================================================================
def now():
    return time.time()

queue    = deque()   # available server IDs
in_use   = set()     # servers currently held by a client
used_ts  = {}        # server_id -> timestamp when last used
seen     = set()     # already processed server IDs
waiters  = []        # (future, who, current_job) — clients waiting
clients  = {}        # who -> server_id they are on right now
viewer_keys = {}      # ws -> auth_key, viewers that receive found events (ChaCha20 encrypted)
found_seen = {}       # "job_id:name" -> timestamp, dedup so only 1 webhook per find (10min TTL)

# ── GIVE / RELEASE ───────────────────────────────────────────────
def give(who, current_job=None):
    attempts = 0
    while queue and attempts < 1000:
        attempts += 1
        sid = queue.popleft()
        if sid in in_use:                            continue
        if current_job and sid == current_job:       continue
        if now() - used_ts.get(sid, 0) < COOLDOWN:  continue
        in_use.add(sid)
        clients[who] = sid
        used_ts[sid] = now()
        print(f"[GIVE] {sid[:8]} -> {who[:14]} | q={len(queue)} used={len(in_use)} clients={len(clients)}")
        return sid
    return None

def free(who):
    sid = clients.pop(who, None)
    if sid:
        in_use.discard(sid)
        print(f"[FREE] {sid[:8]} from {who[:14]} | used={len(in_use)}")

def drain_waiters():
    remaining = []
    for fut, who, cj in waiters:
        if fut.done():
            continue
        sid = give(who, cj)
        if sid:
            try: fut.set_result(sid)
            except: pass
        else:
            remaining.append((fut, who, cj))
    waiters.clear()
    waiters.extend(remaining)

# ── SCANNER ──────────────────────────────────────────────────────
cursors = {"Asc": None, "Desc": None}

async def scan_page(sess, order):
    try:
        params = {"sortOrder": order, "limit": PAGE_LIMIT, "excludeFullGames": "true"}
        if cursors[order]:
            params["cursor"] = cursors[order]

        async with sess.get(
            f"https://games.roblox.com/v1/games/{PLACE_ID}/servers/Public",
            params=params,
            headers={"Accept": "application/json"},
            timeout=aiohttp.ClientTimeout(total=8)
        ) as r:
            if r.status == 429:
                await asyncio.sleep(5); return
            if r.status != 200:
                await asyncio.sleep(1); return
            data = await r.json()

        cursors[order] = data.get("nextPageCursor")

        added = 0
        for sv in data.get("data", []):
            sid     = str(sv.get("id", ""))
            playing = int(sv.get("playing", 0) or 0)
            maxp    = int(sv.get("maxPlayers", 0) or 0)

            if not sid:                                                        continue
            if sid in seen:                                                    continue
            if sid in in_use:                                                  continue
            if sv.get("vipServerId") or sv.get("privateServerId"):             continue
            if not maxp or playing >= maxp:                                    continue
            if playing < MIN_P or (MAX_P > 0 and playing > MAX_P):            continue
            if (maxp - playing) < 1:                                           continue
            if str(sv.get("serverType", "public")).lower() != "public":        continue

            seen.add(sid)
            queue.append(sid)
            added += 1

        if added > 0:
            print(f"[SCAN-{order[0]}] +{added} | q={len(queue)} clients={len(clients)}")
            drain_waiters()

        await asyncio.sleep(SCAN_DELAY)

    except asyncio.TimeoutError:
        await asyncio.sleep(1)
    except Exception as e:
        print(f"[SCAN ERR] {e}")
        await asyncio.sleep(1)

async def scanner():
    print(f"[SCAN] Starting for place {PLACE_ID}")
    async with aiohttp.ClientSession() as sess:
        while True:
            try:
                await asyncio.gather(
                    scan_page(sess, "Asc"),
                    scan_page(sess, "Desc"),
                    return_exceptions=True
                )
                if len(queue) > 3000 and not waiters and len(clients) < 5:
                    await asyncio.sleep(2)
            except Exception as e:
                print(f"[SCANNER ERR] {e}")
                await asyncio.sleep(2)

async def cleanup():
    while True:
        await asyncio.sleep(30)
        n = now()
        expired = [s for s, t in used_ts.items() if n - t > COOLDOWN]
        for s in expired:
            del used_ts[s]
            seen.discard(s)
        print(f"[CLEAN] recycled={len(expired)} q={len(queue)} used={len(in_use)} clients={len(clients)} waiters={len(waiters)}")


# ================================================================
# Webhooks are handled entirely by the Lua script
# ================================================================

# Brainrot images
# ── WS HANDLER ───────────────────────────────────────────────────
async def handle(ws, path):
    qp      = parse_qs(urlparse(path).query)
    who     = qp.get("who", ["?"])[0]
    is_view = qp.get("viewer", ["0"])[0] == "1"
    print(f"[+] {who[:20]} connected viewer={is_view} | total={len(clients)+1}")

    # ── AUTH CHECK ───────────────────────────────────────────────
    auth_key = qp.get("key", [""])[0]
    if auth_key not in AUTH_KEYS:
        print(f"[AUTH] Rejected {who[:20]} — invalid key: {auth_key[:16]}")
        await ws.send(json.dumps({"type": "error", "msg": "Invalid auth key"}))
        await ws.close()
        return

    # Viewer mode - just receive found events, no hopping
    if is_view:
        viewer_keys[ws] = auth_key
        await ws.send(json.dumps({"type": "viewer_ok", "msg": "Connected - waiting for brainrot finds..."}))
        try:
            async for raw in ws:
                pass  # viewers don't send anything
        except Exception:
            pass
        finally:
            viewer_keys.pop(ws, None)
            print(f"[-] viewer {who[:20]} left")
        return

    current_job = None

    async def send_next():
        nonlocal current_job
        free(who)
        sid = give(who, current_job)
        if sid:
            await ws.send(json.dumps({"type": "next", "id": sid}))
            return
        fut = asyncio.get_event_loop().create_future()
        waiters.append((fut, who, current_job))
        print(f"[WAIT] {who[:14]} | waiters={len(waiters)}")
        try:
            sid = await asyncio.wait_for(fut, timeout=5.0)
            await ws.send(json.dumps({"type": "next", "id": sid}))
        except asyncio.TimeoutError:
            sid = give(who, current_job)
            if sid:
                await ws.send(json.dumps({"type": "next", "id": sid}))
            else:
                await ws.send(json.dumps({"type": "timeout"}))
        finally:
            for i, (f, w, _) in enumerate(waiters):
                if f is fut:
                    waiters.pop(i); break

    await send_next()

    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
                t   = msg.get("type", "")

                if t == "next":
                    current_job = msg.get("currentJob") or current_job
                    await send_next()

                elif t == "joined":
                    new_sid = msg.get("id", "")
                    old_sid = clients.get(who)
                    if old_sid and old_sid != new_sid:
                        in_use.discard(old_sid)
                    clients[who] = new_sid
                    current_job  = new_sid
                    in_use.add(new_sid)
                    print(f"[JOINED] {who[:14]} on {new_sid[:8]}")

                elif t == "release":
                    sid = msg.get("id", "")
                    in_use.discard(sid)
                    if clients.get(who) == sid:
                        clients.pop(who, None)
                    if msg.get("blocked"):
                        used_ts[sid] = now() + 9999999
                        print(f"[BLOCK] {sid[:8]} 773-blocked")
                    else:
                        print(f"[REL] {sid[:8]} by {who[:14]}")
                    drain_waiters()
                    await send_next()

                elif t in ("found", "found_batch"):
                    job_id  = msg.get("job_id", "?")
                    player  = msg.get("player", who)
                    items   = msg.get("items") or ([{
                        "name":  msg.get("name","?"),
                        "gen":   msg.get("gen","?"),
                        "value": msg.get("value", 0),
                    }] if t == "found" else [])

                    # Expire old entries (10 min)
                    now_ts = now()
                    expired = [k for k, t2 in found_seen.items() if now_ts - t2 > 600]
                    for k in expired:
                        del found_seen[k]

                    # Dedup: keep only items not seen yet for this server
                    new_items = []
                    for item in items:
                        key = f"{job_id}:{item.get('name','?').lower()}"
                        if key not in found_seen:
                            found_seen[key] = now_ts
                            new_items.append(item)

                    if not new_items:
                        await ws.send(json.dumps({"type": "found_ack", "first": False}))
                    else:
                        names = ", ".join(i["name"] for i in new_items)
                        print(f"[FOUND] {who[:14]} | {names}")
                        # Reply to bot - it sends the Discord webhook
                        await ws.send(json.dumps({"type": "found_ack", "first": True, "items": new_items, "job_id": job_id}))
                        # Broadcast to viewers
                        entry = {"type": "found", "player": player, "job_id": job_id, "items": new_items,
                                 "time": datetime.datetime.utcnow().isoformat() + "Z"}
                        dead = set()
                        for v, v_key in list(viewer_keys.items()):
                            try:
                                encrypted = encrypt_payload(json.dumps(entry), v_key)
                                await v.send(json.dumps({"type": "enc", "d": encrypted}))
                            except:
                                dead.add(v)
                        for d in dead: viewer_keys.pop(d, None)

                elif t == "viewer":
                    # Client wants to receive found events
                    viewers.add(ws)
                    await ws.send(json.dumps({"type": "viewer_ok", "msg": "Now receiving brainrot finds"}))

                elif t == "ping":
                    await ws.send(json.dumps({"type": "pong"}))

            except Exception as e:
                print(f"[MSG ERR] {e}")
    except Exception:
        pass
    finally:
        free(who)
        drain_waiters()
        viewer_keys.pop(ws, None)
        print(f"[-] {who[:20]} left | total={len(clients)}")

# ── BOT STATUS MONITOR ───────────────────────────────────────────
BOT_STATUS_WEBHOOK = os.environ.get("BOT_STATUS_WEBHOOK", "https://ptb.discord.com/api/webhooks/1477758597961089258/GDZXg7MBzeabPNrMTSHzKNNPa9iFnT16xgEnO4brL6J8BnpUFxCBnw9dX7Sa98F9Tm8Z")
BOT_STATUS_API     = "https://status.therixyt4.workers.dev/bots"
TOTAL_BOTS         = int(os.environ.get("TOTAL_BOTS", "600"))
STATUS_INTERVAL    = int(os.environ.get("STATUS_INTERVAL", "60"))
_last_bot_count    = None
_posting_lock      = False
MSG_ID_FILE        = "/tmp/status_msg_id.txt"

def _load_msg_id():
    try:
        return open(MSG_ID_FILE).read().strip()
    except:
        return None

def _save_msg_id(mid):
    try:
        open(MSG_ID_FILE, "w").write(str(mid))
    except:
        pass

def _power_color(pct):
    if pct >= 80: return 0x00ff88
    if pct >= 50: return 0xffcc00
    if pct >= 20: return 0xff8800
    return 0xff3333

def _power_label(pct):
    if pct >= 80: return "🟢 High"
    if pct >= 50: return "🟡 Moderate"
    if pct >= 20: return "🟠 Low"
    return "🔴 Critical"

async def bot_status_loop():
    global _last_bot_count, _posting_lock
    if not BOT_STATUS_WEBHOOK:
        print("[STATUS] No BOT_STATUS_WEBHOOK set, skipping monitor")
        return
    await asyncio.sleep(10)
    print(f"[STATUS] Monitor started — checking every {STATUS_INTERVAL}s")
    msg_id = _load_msg_id()

    # On startup, try to find existing status message in channel
    if not msg_id:
        try:
            async with aiohttp.ClientSession() as sess:
                # Get webhook info to find channel_id and token
                wh_parts = BOT_STATUS_WEBHOOK.rstrip("/").split("/")
                wh_id, wh_token = wh_parts[-2], wh_parts[-1]
                async with sess.get(
                    f"https://discord.com/api/webhooks/{wh_id}/{wh_token}/messages/@original",
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        msg_id = data.get("id")
                        if msg_id:
                            _save_msg_id(msg_id)
                            print(f"[STATUS] Recovered msg_id={msg_id}")
        except Exception:
            pass
    while True:
        try:
            async with aiohttp.ClientSession() as sess:
                # Count active bots directly from connected clients (no external API needed)
                bots  = [{"player": k} for k in list(clients.keys())]
                count = len(bots)
                pct   = round((count / max(TOTAL_BOTS, 1)) * 100, 1)
                trend = ""
                if _last_bot_count is not None:
                    if count > _last_bot_count: trend = " ↑"
                    elif count < _last_bot_count: trend = " ↓"
                    else: trend = " →"
                _last_bot_count = count

                now_str = datetime.datetime.utcnow().strftime("%d.%m.%Y %H:%M UTC")
                embed = {
                    "title": "🤖 Bot Status",
                    "color": _power_color(pct),
                    "fields": [
                        {"name": f"{pct}% power{trend}", "value": f"**{_power_label(pct)}**", "inline": False},
                        {"name": f"Active Bots ({count}/{TOTAL_BOTS})", "value": f"**{count}** bots online", "inline": False},
                    ],
                    "footer": {"text": f"Last updated • {now_str}"},
                    "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
                }
                payload = {"embeds": [embed]}

                if msg_id:
                    # Try editing existing message
                    async with sess.patch(
                        f"{BOT_STATUS_WEBHOOK}/messages/{msg_id}",
                        json=payload, timeout=aiohttp.ClientTimeout(total=10)
                    ) as r:
                        if r.status in (200, 204):
                            print(f"[STATUS] Edited — {count}/{TOTAL_BOTS} bots ({pct}%)")
                        else:
                            # Message gone, clear ID and post new next iteration
                            print(f"[STATUS] Edit failed ({r.status}), will post new")
                            msg_id = None
                            _save_msg_id("")
                else:
                    # Post new message once
                    async with sess.post(
                        f"{BOT_STATUS_WEBHOOK}?wait=true",
                        json=payload, timeout=aiohttp.ClientTimeout(total=10)
                    ) as r:
                        resp = await r.json()
                        msg_id = resp.get("id")
                        if msg_id:
                            _save_msg_id(msg_id)
                            print(f"[STATUS] Posted new — {count}/{TOTAL_BOTS} bots ({pct}%)")
        except Exception as e:
            print(f"[STATUS ERR] {e}")
        await asyncio.sleep(STATUS_INTERVAL)

async def main():
    print("=" * 55)
    print("  SHREK NOTIFIER — HOPPER + BOT MONITOR")
    print(f"  WS Port    : {PORT}")
    print(f"  Place ID   : {PLACE_ID}")
    print(f"  Players    : {MIN_P}-{MAX_P}")
    print(f"  Total bots : {TOTAL_BOTS}")
    print(f"  Status API : {BOT_API_URL}")
    print("=" * 55 + "\n")

    # Start async tasks
    asyncio.create_task(scanner())
    asyncio.create_task(cleanup())
    asyncio.create_task(bot_status_loop())

    async with ws_serve(handle, "0.0.0.0", PORT):
        print(f"✅  WS server running on ws://0.0.0.0:{PORT}\n")
        await asyncio.Future()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Stopped")
