import asyncio

import aiohttp
from aiohttp import web
from Cryptodome.Cipher import DES
import pytest

from mintbot_desktop.app import create_app


@pytest.mark.asyncio
async def test_authenticated_websocket_hides_upstream_password_and_bridges_rfb(tmp_path):
    from mintbot_desktop import app as module
    assert hasattr(module, 'rfb_connect'), 'authenticated RFB transport missing'
    authenticated = asyncio.Event()
    password = 'abcDEF12'
    challenge = bytes(range(16))

    async def vnc(reader, writer):
        try:
            writer.write(b'RFB 003.008\n')
            assert await reader.readexactly(12) == b'RFB 003.008\n'
            writer.write(b'\x01\x02')
            assert await reader.readexactly(1) == b'\x02'
            writer.write(challenge)
            response = await reader.readexactly(16)
            key = bytes(int(f'{b:08b}'[::-1], 2) for b in password.encode())
            assert response == DES.new(key, DES.MODE_ECB).encrypt(challenge)
            authenticated.set()
            writer.write(b'\0\0\0\0')
            assert await reader.readexactly(1) == b'\x01'
            writer.write(b'server-frame')
            assert await reader.readexactly(5) == b'input'
            writer.write(b'input-received')
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    upstream = await asyncio.start_server(vnc, '127.0.0.1', 0)
    port = upstream.sockets[0].getsockname()[1]
    runner = web.AppRunner(create_app(token='service-token', password=password, novnc=tmp_path, rfb_port=port))
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    url = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/desktop/rfb'
    try:
        async with aiohttp.ClientSession() as client:
            with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
                await client.ws_connect(url)
            assert exc.value.status == 401
            async with client.ws_connect(url, headers={'Authorization': 'Bearer service-token', 'Origin': 'null'}) as ws:
                assert (await ws.receive()).data == b'RFB 003.008\n'
                # Fragmented binary input is legal WebSocket RFB transport.
                await ws.send_bytes(b'RFB 003.')
                await ws.send_bytes(b'008\n')
                assert (await ws.receive()).data == b'\x01\x01'
                await ws.send_bytes(b'\x01')
                assert (await ws.receive()).data == b'\0\0\0\0'
                await ws.send_bytes(b'\x01')
                assert (await ws.receive()).data == b'server-frame'
                await ws.send_bytes(b'input')
                assert (await ws.receive()).data == b'input-received'
            assert authenticated.is_set()
    finally:
        await runner.cleanup()
        upstream.close()
        await upstream.wait_closed()


@pytest.mark.asyncio
async def test_shutdown_closes_live_websocket_promptly(tmp_path, monkeypatch):
    from mintbot_desktop import app as module
    closed = asyncio.Event()

    async def peer(reader, writer):
        try:
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
            closed.set()

    upstream = await asyncio.start_server(peer, '127.0.0.1', 0)
    port = upstream.sockets[0].getsockname()[1]

    async def connected(password, unused):
        return await asyncio.open_connection('127.0.0.1', port)

    monkeypatch.setattr(module, 'rfb_connect', connected)
    runner = web.AppRunner(create_app(token='token', password='password'), shutdown_timeout=.1)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    try:
        async with aiohttp.ClientSession() as client:
            ws = await client.ws_connect(f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/desktop/rfb', headers={'Authorization':'Bearer token'})
            assert (await ws.receive()).data == b'RFB 003.008\n'
            # Application must close the connection proactively on shutdown,
            # not rely on the server's forced handler cancellation timeout.
            await runner.shutdown()
            message = await ws.receive(timeout=.5)
            assert message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)
            await ws.close()
    finally:
        await runner.cleanup()
        upstream.close()
        await upstream.wait_closed()
        await asyncio.wait_for(closed.wait(), 2)


@pytest.mark.asyncio
async def test_unreachable_vnc_returns_502_before_ws_upgrade(tmp_path):
    runner = web.AppRunner(create_app(token='token', password='password', novnc=tmp_path, rfb_port=1))
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    try:
        async with aiohttp.ClientSession() as client:
            async with client.get(f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/desktop/rfb', headers={'Authorization':'Bearer token'}) as response:
                assert response.status == 502
                assert 'password' not in await response.text()
    finally:
        await runner.cleanup()
