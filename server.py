#!/usr/bin/env python3
"""
KeyMirror Server
Captures PC keyboard input and streams it to any browser on the local network.

Usage:
    pip install -r requirements.txt
    python server.py [--port 8765]

Then open http://<PC_IP>:8765 on your phone.
"""

import asyncio
import argparse
import io
import os
import socket
import sys
import logging
import qrcode
import qrcode.image.svg
from pynput import keyboard
from aiohttp import web

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("keymirror")

# --- Shared state ---
clients: set[web.WebSocketResponse] = set()
key_queue: asyncio.Queue[str] = asyncio.Queue()
server_url: str = ""

# The event loop reference — set by main(), used by on_press (pynput thread)
asyncio_loop: asyncio.AbstractEventLoop | None = None

# --- Character filtering ---
SPECIAL_DISPLAY = {
    keyboard.Key.space: " ",
    keyboard.Key.enter: "\n",
    keyboard.Key.tab: "    ",
    keyboard.Key.backspace: "\b",
    keyboard.Key.left: "\x00LEFT\x00",
    keyboard.Key.right: "\x00RIGHT\x00",
    keyboard.Key.up: "\x00UP\x00",
    keyboard.Key.down: "\x00DOWN\x00",
}

# Dead keys / combining marks for accented characters
COMBINING_ACCENTS = {
    "´": "áéíóúÁÉÍÓÚñÑýÝ",  # acute
    "`": "àèìòùÀÈÌÒÙ",       # grave
    "^": "âêîôûÂÊÎÔÛ",       # circumflex
    "~": "ãõÃÕñÑ",           # tilde
}

# Mapping: accent + letter -> composed character
COMPOSED_CHARS = {
    # Acute (´)
    "´a": "á", "´e": "é", "´i": "í", "´o": "ó", "´u": "ú",
    "´A": "Á", "´E": "É", "´I": "Í", "´O": "Ó", "´U": "Ú",
    "´n": "ñ", "´N": "Ñ", "´y": "ý", "´Y": "Ý",
    # Grave (`)
    "`a": "à", "`e": "è", "`i": "ì", "`o": "ò", "`u": "ù",
    "`A": "À", "`E": "È", "`I": "Ì", "`O": "Ò", "`U": "Ù",
    # Circumflex (^)
    "^a": "â", "^e": "ê", "^i": "î", "^o": "ô", "^u": "û",
    "^A": "Â", "^E": "Ê", "^I": "Î", "^O": "Ô", "^U": "Û",
    # Tilde (~)
    "~a": "ã", "~o": "õ", "~A": "Ã", "~O": "Õ",
    "~n": "ñ", "~N": "Ñ",
}

# Buffer for dead keys
_dead_key_buffer: str = ""


def key_to_char(key) -> str | None:
    """Convert pynput key to a displayable character, or None if filtered."""
    global _dead_key_buffer

    if key in SPECIAL_DISPLAY:
        return SPECIAL_DISPLAY[key]

    char = getattr(key, "char", None)
    if char is not None and len(char) == 1:
        # Check if we have a pending dead key
        if _dead_key_buffer:
            combo = _dead_key_buffer + char
            composed = COMPOSED_CHARS.get(combo)
            if composed:
                _dead_key_buffer = ""
                return composed
            else:
                # Not a valid combination, output dead key + current char
                _dead_key_buffer = ""
                return combo

        # Check if this key is a dead key (combining accent)
        if char in COMBINING_ACCENTS:
            _dead_key_buffer = char
            return None  # Don't output anything yet

        if char.isprintable():
            return char

    return None


# --- pynput callbacks (run in pynput's own thread) ---
def on_press(key):
    char = key_to_char(key)
    if char is not None and asyncio_loop is not None:
        try:
            logger.debug(f"Key captured: {repr(char)}")
            asyncio_loop.call_soon_threadsafe(key_queue.put_nowait, char)
        except Exception as e:
            logger.error(f"Error queuing key: {e}")


def on_release(key):
    pass


# --- HTTP handler ---
async def handle_index(request):
    return web.FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))


async def handle_qr(request):
    img = qrcode.make(server_url, image_factory=qrcode.image.svg.SvgFillImage)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode()

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>KeyMirror — Scan to connect</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ background: #0d0d0d; display: flex; flex-direction: column; align-items: center;
       justify-content: center; height: 100vh; font-family: 'Courier New', monospace;
       color: #e0e0e0; gap: 20px; }}
.qr {{ background: #fff; padding: 16px; border-radius: 12px; }}
.qr svg {{ display: block; width: 240px; height: 240px; }}
p {{ color: #888; font-size: 14px; }}
</style>
</head>
<body>
<div class="qr">{svg}</div>
<p>{server_url}</p>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html")


# --- WebSocket handler ---
async def handle_ws(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    try:
        request.transport.set_tcp_nodelay(True)
        request.transport.set_tcp_keepalive(True)
    except Exception:
        pass

    addr = request.remote
    logger.info(f"Client connected: {addr}")
    clients.add(ws)
    client_count = len(clients)
    logger.info(f"Active clients: {client_count}")

    async def safe_send(data: str) -> bool:
        """Send data, returning False if the connection is dead."""
        if ws.closed:
            return False
        try:
            await ws.send_str(data)
            return True
        except (ConnectionResetError, ConnectionError, RuntimeError, AttributeError):
            return False

    try:
        if not await safe_send("\x00CLEAR\x00"):
            return ws
        logger.info("Sent CLEAR to browser")

        while not ws.closed:
            try:
                # Short timeout to detect dead connections quickly
                char = await asyncio.wait_for(key_queue.get(), timeout=5.0)
                if not await safe_send(char):
                    break
            except asyncio.TimeoutError:
                # Send keepalive to prevent idle-connection drops
                if not await safe_send("\x00PING\x00"):
                    break
                continue
    except Exception as e:
        logger.info(f"Client {addr} disconnected: {e}")
    finally:
        clients.discard(ws)
        logger.info(f"Client disconnected: {addr}. Active: {len(clients)}")

    return ws


# --- Server startup ---
async def main(port: int, host: str = "0.0.0.0"):
    global asyncio_loop
    asyncio_loop = asyncio.get_running_loop()

    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/ws", handle_ws)
    app.router.add_get("/qr", handle_qr)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()

    site = web.TCPSite(runner, host, port, reuse_address=True)
    await site.start()

    # Start keyboard listener in a daemon thread
    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.daemon = True
    listener.start()
    logger.info("Keyboard listener started")

    # Find IPs for display
    hostname = socket.gethostname()
    try:
        ips = socket.getaddrinfo(hostname, None, socket.AF_INET)
        ip_set = {addr[4][0] for addr in ips if not addr[4][0].startswith("127.")}

        def _ip_rank(ip):
            if ip.startswith("192.168."): return 0
            if ip.startswith("10."):      return 1
            return 2

        ip_list = sorted(ip_set, key=_ip_rank)
    except Exception:
        ip_list = []

    logger.info("=" * 50)
    logger.info("  KeyMirror Server")
    logger.info("=" * 50)

    global server_url
    if ip_list:
        server_url = f"http://{ip_list[0]}:{port}"
        for ip in ip_list:
            logger.info(f"  Open on phone: http://{ip}:{port}")
    else:
        server_url = f"http://localhost:{port}"
        logger.info(f"  Open on phone: http://localhost:{port}")

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(server_url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)

    logger.info(f"  WebSocket port: {port}")
    logger.info("  Start typing on your PC!")
    logger.info("  Press Ctrl+C to stop")
    logger.info("=" * 50)

    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        listener.stop()
        await runner.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="KeyMirror — Keyboard to Browser")
    parser.add_argument("--port", type=int, default=8765, help="Port (default: 8765)")
    parser.add_argument(
        "--host", type=str, default="0.0.0.0", help="Host (default: all)"
    )
    args = parser.parse_args()

    # Enable debug logging if --verbose flag is passed
    if "--verbose" in sys.argv:
        logging.getLogger("keymirror").setLevel(logging.DEBUG)

    asyncio.run(main(args.port, args.host))
