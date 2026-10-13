"""Private gateway. Only the host reverse proxy knows the service token."""
import asyncio
import hmac
from pathlib import Path

from aiohttp import web, WSMsgType
from Cryptodome.Cipher import DES


async def rfb_connect(password, port):
    """Authenticate upstream before admitting a browser; never expose secrets."""
    reader, writer = await asyncio.open_connection('127.0.0.1', port)
    try:
        if await reader.readexactly(12) != b'RFB 003.008\n':
            raise ValueError('RFB 3.8 required')
        writer.write(b'RFB 003.008\n')
        count = (await reader.readexactly(1))[0]
        types = await reader.readexactly(count)
        if 2 not in types:
            raise ValueError('VncAuth required')
        writer.write(b'\x02')
        challenge = await reader.readexactly(16)
        key = bytes(int(f'{b:08b}'[::-1], 2) for b in password.encode('ascii')[:8].ljust(8, b'\0'))
        writer.write(DES.new(key, DES.MODE_ECB).encrypt(challenge))
        await writer.drain()
        if await reader.readexactly(4) != b'\0\0\0\0':
            raise ValueError('VNC authentication failed')
        return reader, writer
    except BaseException:
        writer.close()
        await writer.wait_closed()
        raise


async def bridge(ws, reader, writer):
    buffer = bytearray()

    async def receive_exactly(count):
        while len(buffer) < count:
            message = await ws.receive()
            if message.type != WSMsgType.BINARY:
                raise ValueError('Binary RFB required')
            buffer.extend(message.data)
        result = bytes(buffer[:count])
        del buffer[:count]
        return result

    async with asyncio.timeout(15):
        await ws.send_bytes(b'RFB 003.008\n')
        if await receive_exactly(12) != b'RFB 003.008\n':
            raise ValueError('RFB 3.8 required')
        await ws.send_bytes(b'\x01\x01')
        if await receive_exactly(1) != b'\x01':
            raise ValueError('Invalid security selection')
        await ws.send_bytes(b'\0\0\0\0')

    async def upstream():
        while data := await reader.read(65536):
            await ws.send_bytes(data)

    async def downstream():
        if buffer:
            writer.write(buffer)
            await writer.drain()
        async for message in ws:
            if message.type != WSMsgType.BINARY:
                break
            writer.write(message.data)
            await writer.drain()

    tasks = [asyncio.create_task(upstream()), asyncio.create_task(downstream())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Agent desktop</title>
<style>html,body,#screen{margin:0;width:100%;height:100%;overflow:hidden;background:#171717}#status{position:fixed;top:0;color:white;background:#222;font:14px sans-serif;z-index:2}</style></head>
<body><div id="status">Connecting…</div><div id="screen"></div><script type="module" src="./app.js"></script></body></html>"""
JS = """import RFB from './novnc/core/rfb.js';
const url = new URL('./rfb', window.location.href);
url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
const rfb = new RFB(document.getElementById('screen'), url.href);
rfb.scaleViewport = true;
rfb.resizeSession = true;
rfb.addEventListener('connect', () => document.getElementById('status').hidden = true);
rfb.addEventListener('disconnect', () => {
  const status = document.getElementById('status');
  status.hidden = false; status.textContent = 'Desktop disconnected. Reload to reconnect.';
});
"""


def create_app(*, token, password, novnc=Path('/usr/share/novnc'), rfb_port=5913):
    if not token or not password:
        raise ValueError('Credentials must not be empty')

    @web.middleware
    async def authenticate(request, handler):
        expected = 'Bearer ' + token
        if not hmac.compare_digest(request.headers.get('Authorization', '').encode(), expected.encode()):
            raise web.HTTPUnauthorized()
        response = await handler(request)
        response.headers['Cache-Control'] = 'no-store'
        # ES modules run inside the panel's opaque-origin sandbox.
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    app = web.Application(middlewares=[authenticate])
    sockets = set()

    async def shutdown(application):
        await asyncio.gather(*(ws.close(code=1001, message=b'Desktop stopping') for ws in list(sockets)))

    app.on_shutdown.append(shutdown)

    async def index(request):
        return web.Response(text=HTML, content_type='text/html')

    async def script(request):
        return web.Response(text=JS, content_type='text/javascript')

    async def health(request):
        return web.json_response({'status': 'ok'})

    async def asset(request):
        base = Path(novnc).resolve()
        path = (base / request.match_info['path']).resolve()
        if not path.is_relative_to(base) or not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    async def websocket(request):
        try:
            async with asyncio.timeout(10):
                reader, writer = await rfb_connect(password, rfb_port)
        except (OSError, ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError):
            raise web.HTTPBadGateway(text='Desktop unavailable') from None
        ws = web.WebSocketResponse(max_msg_size=4 * 1024 * 1024, heartbeat=30, timeout=2)
        try:
            await ws.prepare(request)
            sockets.add(ws)
            await bridge(ws, reader, writer)
        except (ConnectionError, ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError):
            pass
        finally:
            sockets.discard(ws)
            writer.close()
            await writer.wait_closed()
            await ws.close()
        return ws

    app.router.add_get('/desktop/rfb', websocket)
    app.router.add_get('/health', health)
    app.router.add_get('/desktop/', index)
    app.router.add_get('/desktop/app.js', script)
    app.router.add_get('/desktop/novnc/{path:.*}', asset)
    return app
