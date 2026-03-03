#!/usr/bin/env python3
# SHREK NOTIFIER - SERVER HOPPER BACKEND

import asyncio, json, time, datetime, os, hashlib, base64
from collections import deque
from urllib.parse import urlparse, parse_qs
import aiohttp
import websockets
try:
    from websockets.asyncio.server import serve as _ws_serve
    _NEW_WS = True
except ImportError:
    import websockets.server as ws_server
    _NEW_WS = False

PORT       = int(os.environ.get("PORT", 3001))
AUTH_KEYS  = set(k.strip() for k in os.environ.get("AUTH_KEYS", "shreknotifiiifier23242!").split(",") if k.strip())
ENC_SECRET = os.environ.get("ENC_SECRET", "xK9#m5P2$vL7nQ4@32wR8")

def _derive_key(auth_key: str) -> bytes:
    return hashlib.sha256((ENC_SECRET + auth_key).encode()).digest()

def encrypt_payload(data: str, auth_key: str) -> str:
    key = _derive_key(auth_key)
    ct  = bytes(data.encode()[i] ^ key[i % len(key)] for i in range(len(data)))
    return base64.b64encode(ct).decode()

PLACE_ID        = "109983668079237"
MIN_P           = 6
MAX_P           = 8
PAGE_LIMIT      = 100
SCAN_DELAY      = 0.0       # seconds between roblox API pages
COOLDOWN        = 15         # seconds before a used server can be reused

# Bot status monitor


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
        pass  # [GIVE] suppressed for performance
        return sid
    return None

def free(who):
    sid = clients.pop(who, None)
    if sid:
        in_use.discard(sid)
        pass  # [FREE] suppressed

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
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=20)) as sess:
        while True:
            try:
                await asyncio.gather(
                    scan_page(sess, "Asc"),
                    scan_page(sess, "Desc"),
                    return_exceptions=True
                )
                if waiters: drain_waiters()
                if len(queue) > 8000 and not waiters:
                    await asyncio.sleep(0.5)
            except Exception as e:
                print(f"[SCANNER ERR] {e}")
                await asyncio.sleep(2)

async def cleanup():
    while True:
        await asyncio.sleep(15)
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
async def handle(ws, path=None):
    if path is None:
        # websockets >= 14 new API
        try:
            path = ws.request.path
        except AttributeError:
            try:
                path = ws.path
            except AttributeError:
                path = "/"
    qp      = parse_qs(urlparse(path).query)
    who     = qp.get("who", ["?"])[0]
    is_view = qp.get("viewer", ["0"])[0] == "1"
    pass  # connect log suppressed

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
            sid = await asyncio.wait_for(fut, timeout=2.0)
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
                    prefetch = min(int(msg.get("prefetch", 1)), 50)
                    for _ in range(prefetch):
                        await send_next()

                elif t == "joined":
                    new_sid = msg.get("id", "")
                    old_sid = clients.get(who)
                    if old_sid and old_sid != new_sid:
                        in_use.discard(old_sid)
                    clients[who] = new_sid
                    current_job  = new_sid
                    in_use.add(new_sid)
                    pass  # joined log suppressed

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

                    # Dedup for webhook (only first bot sends Discord webhook)
                    new_items = []
                    for item in items:
                        key = f"{job_id}:{item.get('name','?').lower()}"
                        if key not in found_seen:
                            found_seen[key] = now_ts
                            new_items.append(item)

                    # found_ack - webhooks handled by local Python now
                    if not new_items:
                        try: await ws.send(json.dumps({"type": "found_ack", "first": False}))
                        except: pass
                    else:
                        names = ", ".join(i["name"] for i in new_items)
                        print(f"[FOUND] {who[:14]} | {names}")
                        try: await ws.send(json.dumps({"type": "found_ack", "first": False}))  # webhooks via local py
                        except: pass

                    # Always broadcast ALL items to viewers (no dedup for viewers)
                    if items and viewer_keys:
                        entry = {"type": "found", "player": player, "job_id": job_id, "items": items,
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
                if "no close frame" not in str(e) and "keepalive" not in str(e):
                    print(f"[MSG ERR] {e}")
    except Exception:
        pass
    finally:
        free(who)
        drain_waiters()
        viewer_keys.pop(ws, None)
        pass  # disconnect log suppressed


async def main():
    print("=" * 55)
    print("  SHREK NOTIFIER — HOPPER BACKEND")
    print(f"  WS Port    : {PORT}")
    print(f"  Place ID   : {PLACE_ID}")
    print(f"  Players    : {MIN_P}-{MAX_P}")
    print("=" * 55 + "\n")

    # Start async tasks
    asyncio.create_task(scanner())
    asyncio.create_task(cleanup())

    if _NEW_WS:
        from websockets.asyncio.server import serve as _ws_serve
        async with _ws_serve(handle, "0.0.0.0", PORT):
            print(f"✅  WS server running on ws://0.0.0.0:{PORT}\n")
            await asyncio.Future()
    else:
        async with ws_server.serve(handle, "0.0.0.0", PORT):
            print(f"✅  WS server running on ws://0.0.0.0:{PORT}\n")
            await asyncio.Future()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Stopped")
