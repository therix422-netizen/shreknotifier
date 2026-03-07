#!/usr/bin/env python3
# ================================================================
# SHREK NOTIFIER — RAILWAY RELAY (Autojoiner only, no hopper)
# pip install websockets
# ================================================================

import asyncio, json, os, base64, hashlib
from urllib.parse import urlparse, parse_qs

PORT       = int(os.environ.get("PORT", 8080))
AUTH_KEY   = os.environ.get("AUTH_KEY", "shreknotifiiifier23242!")
ENC_SECRET = os.environ.get("ENC_SECRET", "xK9#m5P2$vL7nQ4@32wR8")

try:
    from websockets.asyncio.server import serve as _ws_serve
    _NEW_WS = True
except ImportError:
    import websockets.server as ws_server
    _NEW_WS = False

# ── ENCRYPTION ───────────────────────────────────────────────────
def _derive_key(auth_key: str) -> bytes:
    return hashlib.sha256((ENC_SECRET + auth_key).encode()).digest()

def encrypt_payload(data: str, auth_key: str) -> str:
    key = _derive_key(auth_key)
    ct  = bytes(data.encode()[i] ^ key[i % len(key)] for i in range(len(data.encode())))
    return base64.b64encode(ct).decode()

def xor_decrypt(data: str, key: str) -> str:
    """Decrypt XOR+base64 encoded string from Lua client."""
    raw = base64.b64decode(data.encode()).decode('latin-1')
    out = []
    for i, c in enumerate(raw):
        out.append(chr(ord(c) ^ ord(key[i % len(key)])))
    return ''.join(out)

# ── STATE ────────────────────────────────────────────────────────
# bots: ws -> who   (tracker bots that send found_batch)
# viewers: ws -> who (autojoiners that receive found_batch)
bots    = {}
viewers = {}

# ── HANDLER ──────────────────────────────────────────────────────
async def handle(ws, path=None):
    if path is None:
        try:    path = ws.request.path
        except: 
            try:    path = ws.path
            except: path = "/"

    qp      = parse_qs(urlparse(path).query)
    who     = qp.get("who", ["?"])[0]
    key     = qp.get("key", [""])[0]
    is_view = qp.get("viewer", ["0"])[0] == "1"

    # Auth check — accept both plain and XOR encrypted key
    key_valid = False
    if key == AUTH_KEY:
        key_valid = True
    else:
        try:
            decrypted = xor_decrypt(key, ENC_SECRET)
            key_valid = decrypted == AUTH_KEY
        except Exception:
            key_valid = False
    if not key_valid:
        print(f"[AUTH] Rejected {who[:20]} — invalid key")
        try: await ws.send(json.dumps({"type": "error", "msg": "Invalid key"}))
        except: pass
        try: await ws.close()
        except: pass
        return

    if is_view:
        # Autojoiner viewer
        viewers[ws] = who
        print(f"[+] Viewer: {who[:20]} (total: {len(viewers)})")
        try:
            await ws.send(json.dumps({"type": "viewer_ok"}))
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                    if msg.get("type") == "ping":
                        await ws.send(json.dumps({"type": "pong"}))
                except: pass
        except: pass
        finally:
            viewers.pop(ws, None)
            print(f"[-] Viewer: {who[:20]} (total: {len(viewers)})")
    else:
        # Tracker bot
        bots[ws] = who
        print(f"[+] Bot: {who[:20]} (total: {len(bots)})")
        try:
            await ws.send(json.dumps({"type": "hello"}))
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                    t   = msg.get("type", "")

                    if t == "found_batch":
                        # Broadcast to all viewers (encrypted)
                        job_id = msg.get("job_id", "")
                        items  = msg.get("items", [])
                        if job_id and items and viewers:
                            payload = json.dumps({
                                "type":   "found",
                                "job_id": job_id,
                                "items":  items,
                            })
                            encrypted = encrypt_payload(payload, AUTH_KEY)
                            dead = set()
                            for v in list(viewers):
                                try:
                                    await v.send(json.dumps({"type": "enc", "d": encrypted}))
                                except:
                                    dead.add(v)
                            for d in dead:
                                viewers.pop(d, None)
                            if viewers:
                                names = ", ".join(i.get("name","?") for i in items[:3])
                                print(f"[RELAY] {who[:14]} → {len(viewers)} viewers | {names}")

                    elif t == "ping":
                        await ws.send(json.dumps({"type": "pong"}))

                except Exception as e:
                    print(f"[MSG ERR] {e}")
        except: pass
        finally:
            bots.pop(ws, None)
            print(f"[-] Bot: {who[:20]} (total: {len(bots)})")

# ── MAIN ─────────────────────────────────────────────────────────
async def main():
    print("=" * 50)
    print("  SHREK NOTIFIER — RAILWAY RELAY")
    print(f"  Port    : {PORT}")
    print(f"  Auth    : {AUTH_KEY[:8]}...")
    print("=" * 50)

    if _NEW_WS:
        from websockets.asyncio.server import serve as _ws_serve
        async with _ws_serve(handle, "0.0.0.0", PORT):
            print(f"✅ Relay running on ws://0.0.0.0:{PORT}")
            await asyncio.Future()
    else:
        async with ws_server.serve(handle, "0.0.0.0", PORT):
            print(f"✅ Relay running on ws://0.0.0.0:{PORT}")
            await asyncio.Future()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Stopped")
