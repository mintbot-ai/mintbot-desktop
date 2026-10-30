"""Private gateway. Only the host reverse proxy knows the service token.

The view shows one of two kinds of desktop. One over RFB: the package's own
Xtigervnc, or an X11 session scraped by X0tigervnc (attach.py); noVNC in the
browser, the gateway authenticating to the VNC server and bridging the
WebSocket to it. The other over RDP: a Wayland session's own remote
desktop server (rdp.py); IronRDP's browser client, the gateway being its
RDCleanPath proxy (rdcleanpath.py) that opens TCP and TLS to the server.
"""
import asyncio
import hmac
from pathlib import Path
import ssl
import sys

from aiohttp import web, WSMsgType
from Cryptodome.Cipher import DES

from . import rdcleanpath


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


async def relay(ws, reader, writer, pending=b'', split=None):
    """Carry bytes both ways until either side ends.

    `pending` is what the browser sent ahead. With `split` (rdcleanpath.split_pdus)
    the server's stream goes to the browser one PDU per message, as IronRDP
    expects; without it, as it comes.
    """
    async def upstream():
        buffer = bytearray()
        while data := await reader.read(65536):
            if split is None:
                await ws.send_bytes(data)
                continue
            buffer.extend(data)
            for pdu in split(buffer):
                await ws.send_bytes(pdu)

    async def downstream():
        if pending:
            writer.write(pending)
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


async def bridge(ws, reader, writer):
    """noVNC's side of RFB: a handshake without authentication (the gateway did that upstream), then the relay."""
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
    await relay(ws, reader, writer, bytes(buffer))


async def receive_pdu(ws):
    """The client's RDCleanPath request, however many messages it came in; what followed it is returned too."""
    buffer = bytearray()
    while True:
        length = rdcleanpath.pdu_length(bytes(buffer))
        if length is not None and len(buffer) >= length:
            return rdcleanpath.Pdu.decode(bytes(buffer[:length])), bytes(buffer[length:])
        message = await ws.receive()
        if message.type != WSMsgType.BINARY:
            raise ValueError('Binary RDCleanPath required')
        buffer.extend(message.data)


async def proxy(ws, target):
    """The RDCleanPath proxy: open the RDP server for the client's request, answer, then relay.

    The client names a destination; this proxy only ever opens the one
    server the session was started for and ignores the name. A server that
    cannot be opened is reported to the client in its own terms.
    """
    async with asyncio.timeout(15):
        request, pending = await receive_pdu(ws)
    try:
        opened = await rdcleanpath.open_connection(target, request)
    except rdcleanpath.ProxyError as error:
        print(f'mintbot-desktop: RDP: {error}', file=sys.stderr, flush=True)
        await ws.send_bytes(error.pdu.encode())
        return
    try:
        await ws.send_bytes(opened.response.encode())
        await relay(ws, opened.reader, opened.writer, pending, split=rdcleanpath.split_pdus)
    finally:
        opened.writer.close()
        try:
            await opened.writer.wait_closed()
        except (OSError, ssl.SSLError):
            # A TLS peer that goes on talking after our close_notify is not our problem.
            pass


HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Agent desktop</title>
<style>html,body,#screen{margin:0;width:100%;height:100%;overflow:hidden;background:#171717}#status{position:fixed;top:0;color:white;background:#222;font:14px sans-serif;z-index:2}</style></head>
<body><div id="status">Connecting…</div><div id="screen"></div><script type="module" src="./app.js"></script></body></html>"""
JS = """import RFB from './novnc/core/rfb.js';
const url = new URL('./rfb', window.location.href);
url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
const screen = document.getElementById('screen');
const status = document.getElementById('status');
// The host restarts the session's service after an update or a crash; the
// view connects again by itself, every few seconds for as long as it takes,
// instead of asking for a reload. noVNC takes its canvas away on disconnect.
const RECONNECT_MS = 3000;
function connect() {
  const rfb = new RFB(screen, url.href);
  rfb.scaleViewport = true;
  // The package's own desktop takes the size of the panel; a computer's own
  // desktop keeps the size of its screen and is scaled to fit.
  rfb.resizeSession = __RESIZE_SESSION__;
  rfb.addEventListener('connect', () => { status.hidden = true; });
  rfb.addEventListener('disconnect', () => {
    status.hidden = false;
    status.textContent = 'Desktop disconnected. Reconnecting…';
    setTimeout(connect, RECONNECT_MS);
  });
}
connect();
"""
RDP_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Agent desktop</title>
<style>html,body,#screen{margin:0;width:100%;height:100%;overflow:hidden;background:#171717}#status{position:fixed;top:0;color:white;background:#222;font:14px sans-serif;z-index:2}iron-remote-desktop{display:block;width:100%;height:100%}</style></head>
<body><div id="status">Connecting…</div><div id="screen"></div><script type="module" src="./rdp.js"></script></body></html>"""
RDP_JS = """import './ironrdp/iron-remote-desktop.js';
import * as Rdp from './ironrdp/iron-remote-desktop-rdp.js';
// The gateway is the RDCleanPath proxy: IronRDP opens this WebSocket and
// the gateway opens the RDP server on the computer for it.
const proxy = new URL('./rdp', window.location.href);
proxy.protocol = proxy.protocol === 'https:' ? 'wss:' : 'ws:';
const screen = document.getElementById('screen');
const status = document.getElementById('status');
const RECONNECT_MS = 3000;
const engine = Rdp.init('WARN');
function describe(error) {
  if (error && typeof error.backtrace === 'function') return error.backtrace();
  return String(error);
}
async function connect() {
  status.hidden = false;
  try {
    await engine;
    // Who to log in to the computer's RDP server as; the gateway knows.
    const login = await (await fetch('./rdp.json', {cache: 'no-store'})).json();
    const element = document.createElement('iron-remote-desktop');
    element.setAttribute('scale', 'fit');
    element.setAttribute('verbose', 'false');
    element.module = Rdp.Backend;
    const session = new Promise((resolve, reject) => {
      element.addEventListener('ready', (event) => {
        const ui = event.detail.irgUserInteraction;
        const config = ui.configBuilder().withDestination(login.destination).withProxyAddress(proxy.href)
          .withAuthToken('mintbot-desktop').withUsername(login.username).withPassword(login.password).build();
        ui.connect(config).then((started) => { status.hidden = true; resolve(started.run()); }, reject);
      }, {once: true});
    });
    screen.replaceChildren(element);
    const end = await session;
    status.textContent = 'Desktop disconnected (' + end.reason() + '). Reconnecting…';
  } catch (error) {
    status.textContent = 'Desktop unavailable (' + describe(error) + '). Reconnecting…';
  }
  status.hidden = false;
  setTimeout(connect, RECONNECT_MS);
}
connect();
"""


def create_app(*, token, password, novnc=Path('/usr/share/novnc'), rfb_port=5913, resize_session=True,
               rdp=None, ironrdp=Path('/opt/mintbot-desktop/ironrdp')):
    """The gateway.

    Over RFB by default; `resize_session` is off when the session shows a
    computer's own X11 desktop (attach.py). With `rdp` (an rdp.Target) the
    view is IronRDP's, served from `ironrdp` (lifecycle.install_viewer),
    and the gateway proxies RDCleanPath to that server.
    """
    if not token or not password:
        raise ValueError('Credentials must not be empty')
    script_text = JS.replace('__RESIZE_SESSION__', 'true' if resize_session else 'false')

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
        return web.Response(text=RDP_HTML if rdp else HTML, content_type='text/html')

    async def script(request):
        return web.Response(text=script_text, content_type='text/javascript')

    async def rdp_script(request):
        return web.Response(text=RDP_JS, content_type='text/javascript')

    async def health(request):
        return web.json_response({'status': 'ok'})

    def assets(directory):
        async def asset(request):
            base = Path(directory).resolve()
            path = (base / request.match_info['path']).resolve()
            if not path.is_relative_to(base) or not path.is_file():
                raise web.HTTPNotFound()
            return web.FileResponse(path)
        return asset

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

    def rdp_routes(server):
        target = rdcleanpath.Target(server.host, server.port, server.fingerprint)

        async def login(request):
            return web.json_response({'destination': server.destination, 'username': server.username, 'password': server.password})

        async def rdp_websocket(request):
            ws = web.WebSocketResponse(max_msg_size=16 * 1024 * 1024, heartbeat=30, timeout=2)
            try:
                await ws.prepare(request)
                sockets.add(ws)
                await proxy(ws, target)
            except (ConnectionError, ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError):
                pass
            finally:
                sockets.discard(ws)
                await ws.close()
            return ws

        app.router.add_get('/desktop/rdp', rdp_websocket)
        app.router.add_get('/desktop/rdp.js', rdp_script)
        app.router.add_get('/desktop/rdp.json', login)
        app.router.add_get('/desktop/ironrdp/{path:.*}', assets(ironrdp))

    app.router.add_get('/health', health)
    app.router.add_get('/desktop/', index)
    if rdp:
        rdp_routes(rdp)
    else:
        app.router.add_get('/desktop/rfb', websocket)
        app.router.add_get('/desktop/app.js', script)
        app.router.add_get('/desktop/novnc/{path:.*}', assets(novnc))
    return app
