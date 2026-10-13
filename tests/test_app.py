import importlib.util
from pathlib import Path

import aiohttp
from aiohttp import web
import pytest


@pytest.mark.asyncio
async def test_http_auth_and_relative_assets(tmp_path):
    assert importlib.util.find_spec('mintbot_desktop'), 'desktop package missing'
    from mintbot_desktop.app import create_app
    assets = tmp_path / 'novnc'
    (assets / 'core').mkdir(parents=True)
    (assets / 'core' / 'rfb.js').write_text('// fixture')
    (assets / 'leak').symlink_to('/etc/passwd')
    app = create_app(token='secret-service-token', password='vnc-pass', novnc=assets)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    url = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
    try:
        async with aiohttp.ClientSession() as client:
            for path in ['/health', '/desktop/', '/desktop/novnc/core/rfb.js', '/desktop/rfb']:
                async with client.get(url + path) as r:
                    assert r.status == 401
            headers = {'Authorization': 'Bearer secret-service-token'}
            async with client.get(url + '/desktop/', headers=headers) as r:
                assert r.status == 200
                text = await r.text()
                assert 'src="./app.js"' in text
                assert 'vnc-pass' not in text and 'secret-service-token' not in text
            async with client.get(url + '/desktop/app.js', headers=headers) as r:
                text = await r.text()
                assert "'./novnc/core/rfb.js'" in text
                assert "new URL('./rfb', window.location.href)" in text
                assert r.headers['Access-Control-Allow-Origin'] == '*'
            async with client.get(url + '/desktop/novnc/core/rfb.js', headers=headers) as r:
                assert r.status == 200
            async with client.get(url + '/desktop/novnc/leak', headers=headers) as r:
                assert r.status == 404
    finally:
        await runner.cleanup()
