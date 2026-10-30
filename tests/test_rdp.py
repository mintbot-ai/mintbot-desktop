"""A Wayland desktop shown through its RDP server: rdcleanpath, rdp, the gateway's proxy, the viewer install.

The RDP server here is a fake that speaks just enough X.224 and TLS for
the proxy's handshake; the real one (freerdp-shadow, standing in for
GNOME Remote Desktop) is in tests/test_rdp_e2e.py.
"""
import asyncio
import hashlib
import socket
import ssl
import subprocess

import aiohttp
from aiohttp import web
import pytest

from mintbot_desktop import rdcleanpath, rdp
from mintbot_desktop.app import create_app

# TPKT + X.224 Connection Request + RDP_NEG_REQ asking for TLS or CredSSP.
X224_REQUEST = bytes.fromhex('03000027' '22e00000000000') + b'Cookie: mstshash=a\r\n' + bytes.fromhex('0100080003000000')


# A TPKT-framed PDU of 7 bytes and a fast-path update of 4 (one-byte length), then one with a two-byte length.
TPKT_ONE = b'\x03\x00\x00\x07abc'
FASTPATH_ONE = b'\x00\x04xy'
FASTPATH_LONG = b'\x00\x81\x03' + bytes(0x103 - 3)


def test_the_servers_stream_is_cut_into_pdus_for_the_browser():
    """IronRDP wants one PDU per WebSocket message; TCP hands the proxy
    whatever it has. Partial PDUs wait, coalesced ones are separated, and
    a stream that is not RDP goes through as it is."""
    assert rdcleanpath.rdp_pdu_length(b'') is None and rdcleanpath.rdp_pdu_length(b'\x03\x00\x00') is None
    assert rdcleanpath.rdp_pdu_length(TPKT_ONE) == 7 and rdcleanpath.rdp_pdu_length(FASTPATH_ONE) == 4
    assert rdcleanpath.rdp_pdu_length(b'\x00\x81') is None and rdcleanpath.rdp_pdu_length(FASTPATH_LONG) == 0x103
    assert rdcleanpath.rdp_pdu_length(b'echo:') == 0
    buffer = bytearray(TPKT_ONE + FASTPATH_ONE + FASTPATH_LONG + TPKT_ONE[:5])
    assert rdcleanpath.split_pdus(buffer) == [TPKT_ONE, FASTPATH_ONE, FASTPATH_LONG]
    assert bytes(buffer) == TPKT_ONE[:5]
    buffer.extend(TPKT_ONE[5:])
    assert rdcleanpath.split_pdus(buffer) == [TPKT_ONE] and not buffer
    buffer = bytearray(b'echo:hello')
    assert rdcleanpath.split_pdus(buffer) == [b'echo:hello'] and not buffer


def x224_confirm(protocol=rdcleanpath.PROTOCOL_SSL, failure=None):
    """TPKT + X.224 Connection Confirm + RDP_NEG_RSP (or RDP_NEG_FAILURE)."""
    body = bytes.fromhex('0ed00000123400')
    if failure is not None:
        body += bytes([3, 0, 8, 0]) + failure.to_bytes(4, 'little')
    elif protocol is not None:
        body += bytes([2, 0, 8, 0]) + protocol.to_bytes(4, 'little')
    return b'\x03\x00' + (4 + len(body)).to_bytes(2, 'big') + body


def test_the_request_pdu_round_trips_and_is_what_the_der_definition_says():
    pdu = rdcleanpath.Pdu(destination='127.0.0.1:3389', proxy_auth='token', x224_connection_pdu=X224_REQUEST)
    encoded = pdu.encode()
    # SEQUENCE { [0] INTEGER 3390, [2] UTF8String, [3] UTF8String, [6] OCTET STRING }
    assert encoded[:2] == bytes([0x30, len(encoded) - 2])
    assert encoded[2:10] == bytes.fromhex('a004' '02020d3e' 'a2') + bytes([len('127.0.0.1:3389') + 2])
    assert bytes([0x0c, 14]) + b'127.0.0.1:3389' in encoded
    assert bytes([0xa6, len(X224_REQUEST) + 2, 0x04, len(X224_REQUEST)]) + X224_REQUEST in encoded
    assert rdcleanpath.Pdu.decode(encoded) == pdu
    assert rdcleanpath.pdu_length(encoded[:1]) is None and rdcleanpath.pdu_length(encoded[:2]) == len(encoded)


def test_the_response_and_error_pdus_round_trip():
    chain = [b'\x30\x03\x02\x01\x01', b'cert2']
    response = rdcleanpath.Pdu(x224_connection_pdu=x224_confirm(), server_cert_chain=chain, server_addr='127.0.0.1')
    decoded = rdcleanpath.Pdu.decode(response.encode())
    assert decoded == response and decoded.server_cert_chain == chain
    error = rdcleanpath.error_pdu(rdcleanpath.TLS_HANDSHAKE_FAILED, tls_alert_code=40)
    decoded = rdcleanpath.Pdu.decode(error.encode())
    assert decoded.error == rdcleanpath.Error(105, None, None, 40)
    # Long form lengths: a payload past 127 bytes.
    big = rdcleanpath.Pdu(x224_connection_pdu=bytes(300)).encode()
    assert big[1] == 0x82 and rdcleanpath.pdu_length(big[:4]) == len(big)
    assert rdcleanpath.Pdu.decode(big).x224_connection_pdu == bytes(300)


@pytest.mark.parametrize('bad', [b'', b'\x30\x00', b'\x30\x03\xa0\x01\x05', b'\x04\x02\x0d\x3e',
                                 bytes.fromhex('3006a00402020d3f'), bytes.fromhex('3008a00402020d3ea800')])
def test_what_is_not_a_pdu_is_refused(bad):
    with pytest.raises(rdcleanpath.DecodeError):
        rdcleanpath.Pdu.decode(bad)


def test_the_confirm_names_the_security_protocol():
    assert rdcleanpath.selected_protocol(x224_confirm(rdcleanpath.PROTOCOL_SSL)) == 1
    assert rdcleanpath.selected_protocol(x224_confirm(rdcleanpath.PROTOCOL_HYBRID)) == 2
    assert rdcleanpath.selected_protocol(x224_confirm(None)) == rdcleanpath.PROTOCOL_RDP
    with pytest.raises(rdcleanpath.NegotiationFailure) as failure:
        rdcleanpath.selected_protocol(x224_confirm(failure=5))
    assert failure.value.code == 5
    with pytest.raises(ValueError):
        rdcleanpath.selected_protocol(b'\x03\x00\x00\x0b\x06\xe0\x00\x00\x00\x00\x00')
    assert rdcleanpath.tpkt_length(X224_REQUEST[:4]) == len(X224_REQUEST)


@pytest.fixture(scope='module')
def certificate(tmp_path_factory):
    """A self-signed certificate and key, as GNOME Remote Desktop makes for itself."""
    directory = tmp_path_factory.mktemp('tls')
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2', '-subj', '/CN=stand-in',
                    '-keyout', str(directory / 'key.pem'), '-out', str(directory / 'cert.pem')], check=True, capture_output=True)
    der = subprocess.run(['openssl', 'x509', '-in', str(directory / 'cert.pem'), '-outform', 'DER'], check=True, capture_output=True).stdout
    return directory / 'cert.pem', directory / 'key.pem', der


class FakeRdpServer:
    """Answers the X.224 request with a confirm, upgrades to TLS when it selected one, then echoes."""

    def __init__(self, certificate, confirm=None, echo=True):
        self.certificate, self.confirm, self.echo = certificate, confirm if confirm is not None else x224_confirm(), echo
        self.requests = []
        self.server = None

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.handle, '127.0.0.1', 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()

    async def handle(self, reader, writer):
        try:
            header = await reader.readexactly(4)
            request = header + await reader.readexactly(rdcleanpath.tpkt_length(header) - 4)
            self.requests.append(request)
            writer.write(self.confirm)
            await writer.drain()
            try:
                protocol = rdcleanpath.selected_protocol(self.confirm)
            except rdcleanpath.NegotiationFailure:
                return
            if protocol != rdcleanpath.PROTOCOL_RDP:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(self.certificate[0], self.certificate[1])
                await writer.start_tls(context)
            while self.echo and (data := await reader.read(65536)):
                if data == b'two':
                    # Two PDUs in one write, as TCP coalesces them: a TPKT and a fast-path update.
                    writer.write(TPKT_ONE + FASTPATH_ONE)
                else:
                    writer.write(b'echo:' + data)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
            pass
        finally:
            writer.close()


REQUEST = rdcleanpath.Pdu(destination='ignored:1', proxy_auth='mintbot-desktop', x224_connection_pdu=X224_REQUEST)


async def test_the_proxy_opens_tcp_forwards_x224_and_upgrades_to_tls(certificate):
    async with FakeRdpServer(certificate) as server:
        target = rdcleanpath.Target('127.0.0.1', server.port, hashlib.sha256(certificate[2]).hexdigest())
        opened = await rdcleanpath.open_connection(target, REQUEST)
        try:
            assert server.requests == [X224_REQUEST]
            assert opened.response.x224_connection_pdu == x224_confirm()
            assert opened.response.server_cert_chain == [certificate[2]] and opened.response.server_addr == '127.0.0.1'
            # Plain bytes in, inside TLS to the server, plain bytes back.
            opened.writer.write(b'hello')
            await opened.writer.drain()
            assert await opened.reader.readexactly(10) == b'echo:hello'
        finally:
            opened.writer.close()


async def test_the_proxy_reports_what_went_wrong_in_the_clients_terms(certificate):
    with pytest.raises(rdcleanpath.ProxyError) as error:
        await rdcleanpath.open_connection(rdcleanpath.Target('127.0.0.1', 1), REQUEST)
    assert error.value.pdu.error.error_code == rdcleanpath.TARGET_UNREACHABLE
    async with FakeRdpServer(certificate) as server:
        with pytest.raises(rdcleanpath.ProxyError, match='other than the one it is known by') as error:
            await rdcleanpath.open_connection(rdcleanpath.Target('127.0.0.1', server.port, 'ff' * 32), REQUEST)
        assert error.value.pdu.error.error_code == rdcleanpath.TLS_HANDSHAKE_FAILED
    async with FakeRdpServer(certificate, confirm=x224_confirm(failure=2)) as server:
        with pytest.raises(rdcleanpath.ProxyError, match='negotiation failure 2') as error:
            await rdcleanpath.open_connection(rdcleanpath.Target('127.0.0.1', server.port), REQUEST)
        assert error.value.pdu.error.error_code == rdcleanpath.NEGOTIATION_ERROR
    with pytest.raises(rdcleanpath.ProxyError) as error:
        await rdcleanpath.open_connection(rdcleanpath.Target('127.0.0.1', 1), rdcleanpath.Pdu(destination='x'))
    assert error.value.pdu.error.error_code == rdcleanpath.GENERAL_ERROR


async def test_standard_rdp_security_is_relayed_without_tls(certificate):
    async with FakeRdpServer(certificate, confirm=x224_confirm(None)) as server:
        opened = await rdcleanpath.open_connection(rdcleanpath.Target('127.0.0.1', server.port), REQUEST)
        try:
            assert opened.response.server_cert_chain == [] and opened.certificate is None
            opened.writer.write(b'plain')
            await opened.writer.drain()
            assert await opened.reader.readexactly(10) == b'echo:plain'
        finally:
            opened.writer.close()


async def gateway(tmp_path, target, viewer=None):
    viewer = viewer or tmp_path / 'ironrdp'
    viewer.mkdir(exist_ok=True)
    (viewer / 'iron-remote-desktop.js').write_text('// component')
    (viewer / 'iron-remote-desktop-rdp.js').write_text('// engine')
    app = create_app(token='secret-service-token', password='unused-vnc', rdp=target, ironrdp=viewer)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    return runner, f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'


async def test_the_gateway_serves_the_rdp_view_and_proxies_rdcleanpath(tmp_path, certificate):
    async with FakeRdpServer(certificate) as server:
        target = rdp.Target('127.0.0.1', server.port, 'mintbot', 's3cret', hashlib.sha256(certificate[2]).hexdigest(), 'gnome-remote-desktop')
        runner, url = await gateway(tmp_path, target)
        headers = {'Authorization': 'Bearer secret-service-token'}
        try:
            async with aiohttp.ClientSession() as client:
                for path in ['/desktop/', '/desktop/rdp.js', '/desktop/rdp.json', '/desktop/ironrdp/iron-remote-desktop.js', '/desktop/rdp']:
                    async with client.get(url + path) as r:
                        assert r.status == 401, path
                # The RFB routes are not there in this mode, and the other way round.
                async with client.get(url + '/desktop/app.js', headers=headers) as r:
                    assert r.status == 404
                async with client.get(url + '/desktop/', headers=headers) as r:
                    text = await r.text()
                    assert 'src="./rdp.js"' in text and 's3cret' not in text
                async with client.get(url + '/desktop/rdp.js', headers=headers) as r:
                    text = await r.text()
                    assert "import './ironrdp/iron-remote-desktop.js'" in text
                    assert "new URL('./rdp', window.location.href)" in text and "fetch('./rdp.json'" in text
                    assert 'element.module = Rdp.Backend' in text and 'setTimeout(connect, RECONNECT_MS)' in text
                    assert r.headers['Access-Control-Allow-Origin'] == '*'
                async with client.get(url + '/desktop/rdp.json', headers=headers) as r:
                    assert await r.json() == {'destination': f'127.0.0.1:{server.port}', 'username': 'mintbot', 'password': 's3cret'}
                    assert r.headers['Cache-Control'] == 'no-store'
                async with client.get(url + '/desktop/ironrdp/iron-remote-desktop-rdp.js', headers=headers) as r:
                    assert r.status == 200 and await r.text() == '// engine'
                (tmp_path / 'ironrdp' / 'leak').symlink_to('/etc/passwd')
                async with client.get(url + '/desktop/ironrdp/leak', headers=headers) as r:
                    assert r.status == 404
                async with client.ws_connect(url + '/desktop/rdp', headers=headers) as ws:
                    encoded = REQUEST.encode()
                    # The request may arrive in pieces; the proxy waits for the whole of it.
                    await ws.send_bytes(encoded[:5])
                    await ws.send_bytes(encoded[5:] + b'after')
                    message = await ws.receive(timeout=10)
                    assert message.type == aiohttp.WSMsgType.BINARY
                    response = rdcleanpath.Pdu.decode(message.data)
                    assert response.error is None and response.x224_connection_pdu == x224_confirm()
                    assert response.server_cert_chain == [certificate[2]]
                    # What followed the request went through, and so does everything after.
                    message = await ws.receive(timeout=10)
                    assert message.data == b'echo:after'
                    await ws.send_bytes(b'more')
                    message = await ws.receive(timeout=10)
                    assert message.data == b'echo:more'
                    # Two PDUs the server wrote at once reach the browser as two messages.
                    await ws.send_bytes(b'two')
                    first, second = (await ws.receive(timeout=10)).data, (await ws.receive(timeout=10)).data
                    assert (first, second) == (TPKT_ONE, FASTPATH_ONE)
                assert server.requests == [X224_REQUEST]
        finally:
            await runner.cleanup()


async def test_the_gateway_tells_the_client_when_the_server_is_gone(tmp_path, capsys):
    target = rdp.Target('127.0.0.1', 1, 'mintbot', 's3cret', None, 'given')
    runner, url = await gateway(tmp_path, target)
    try:
        async with aiohttp.ClientSession() as client:
            async with client.ws_connect(url + '/desktop/rdp', headers={'Authorization': 'Bearer secret-service-token'}) as ws:
                await ws.send_bytes(REQUEST.encode())
                message = await ws.receive(timeout=10)
                assert rdcleanpath.Pdu.decode(message.data).error.error_code == rdcleanpath.TARGET_UNREACHABLE
                closing = await ws.receive(timeout=10)
                assert closing.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)
    finally:
        await runner.cleanup()
    assert 'RDP: the RDP server at 127.0.0.1:1 is unreachable' in capsys.readouterr().err


# --- GNOME Remote Desktop through grdctl -------------------------------

STATUS = '''RDP:
\tStatus: {status}
\tPort: 3389
\tTLS certificate: /home/owner/.local/share/gnome-remote-desktop/rdp-tls.crt
\tTLS fingerprint: {fingerprint}
\tTLS key: /home/owner/.local/share/gnome-remote-desktop/rdp-tls.key
\tView-only: {view_only}
{credentials}VNC:
\tStatus: disabled
'''
FINGERPRINT = ':'.join(f'{b:02X}' for b in bytes(range(32)))


class FakeGrdctl:
    """grdctl and systemctl --user as rdp.Gnome drives them, over a sharing state they change."""

    def __init__(self, enabled=False, username='', password='', view_only='no', listening=False):
        self.enabled, self.username, self.password, self.view_only = enabled, username, password, view_only
        self.listening = listening
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs.get('env', {})))
        if argv[:2] == ['grdctl', 'status']:
            credentials = f'\tUsername: {self.username}\n\tPassword: {self.password}\n' if '--show-credentials' in argv and self.username else ''
            text = STATUS.format(status='enabled' if self.enabled else 'disabled', fingerprint=FINGERPRINT,
                                 view_only=self.view_only, credentials=credentials)
            return subprocess.CompletedProcess(argv, 0, text, '')
        if argv[:3] == ['grdctl', 'rdp', 'set-credentials']:
            self.username, self.password = argv[3], argv[4]
        elif argv[1:] == ['rdp', 'disable-view-only']:
            self.view_only = 'no'
        elif argv[1:] == ['rdp', 'enable']:
            self.enabled = True
        elif argv[:3] == ['systemctl', '--user', 'start']:
            self.listening = True
        return subprocess.CompletedProcess(argv, 0, '', '')


def gnome(commands, environment=None, **kwargs):
    log = kwargs.pop('log', lambda message: None)
    return rdp.Gnome(environment or {'WAYLAND_DISPLAY': 'wayland-0', 'XDG_RUNTIME_DIR': '/run/user/1000'},
                     run=commands, log=log, sleep=lambda s: None, clock=kwargs.pop('clock', None) or Clock(), **kwargs)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        self.now += 1.0
        return self.now


def test_sharing_that_is_off_is_turned_on_with_credentials_of_the_packages_own(monkeypatch):
    commands = FakeGrdctl()
    messages = []
    monkeypatch.setattr(rdp, 'listening', lambda host, port, timeout=1.0: commands.listening)
    monkeypatch.delenv('DBUS_SESSION_BUS_ADDRESS', raising=False)
    target = gnome(commands, log=messages.append, password=lambda: 'generated-pw').target()
    assert target == rdp.Target('127.0.0.1', 3389, 'mintbot', 'generated-pw', bytes(range(32)).hex(), 'gnome-remote-desktop')
    argvs = [argv for argv, _ in commands.calls]
    assert argvs[:5] == [['grdctl', 'status', '--show-credentials'], ['grdctl', 'rdp', 'set-credentials', 'mintbot', 'generated-pw'],
                         ['grdctl', 'rdp', 'disable-view-only'], ['grdctl', 'rdp', 'enable'], ['grdctl', 'status', '--show-credentials']]
    assert ['systemctl', '--user', 'start', 'gnome-remote-desktop.service'] in argvs
    assert ['systemctl', '--user', 'enable', 'gnome-remote-desktop.service'] in argvs
    assert messages == ['turning GNOME Remote Desktop sharing on for the panel']
    # grdctl ran in the owner's session: its bus, by the runtime directory when nothing names it.
    env = commands.calls[0][1]
    assert env['WAYLAND_DISPLAY'] == 'wayland-0' and env['DBUS_SESSION_BUS_ADDRESS'] == 'unix:path=/run/user/1000/bus'


def test_sharing_the_owner_set_up_is_used_as_it_is(monkeypatch):
    commands = FakeGrdctl(enabled=True, username='owner', password='their-pw', view_only='yes', listening=True)
    messages = []
    monkeypatch.setattr(rdp, 'listening', lambda host, port, timeout=1.0: commands.listening)
    target = gnome(commands, log=messages.append).target()
    assert target.username == 'owner' and target.password == 'their-pw'
    assert [argv for argv, _ in commands.calls] == [['grdctl', 'status', '--show-credentials']]
    assert messages and 'view-only' in messages[0]


def test_a_server_that_does_not_come_up_or_a_grdctl_that_fails_is_a_reason(monkeypatch):
    commands = FakeGrdctl(enabled=True, username='owner', password='pw')
    monkeypatch.setattr(rdp, 'listening', lambda host, port, timeout=1.0: False)
    with pytest.raises(RuntimeError, match='nothing listens on port 3389'):
        gnome(commands).target()
    failing = lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, '', 'Failed to connect to bus')
    with pytest.raises(RuntimeError, match='grdctl status --show-credentials failed: Failed to connect to bus'):
        gnome(failing).target()


def test_a_listening_port_is_seen_in_the_kernels_tables_without_connecting(tmp_path):
    """The watch reads /proc/net/tcp: a connection would land in the owner's
    server log as an error every few seconds."""
    (tmp_path / 'tcp').write_text('  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n'
                                  '   0: 0100007F:0D3D 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 1 0 100 0 0 10 0\n'
                                  '   1: 0100007F:9C40 0100007F:0D3D 01 00000000:00000000 00:00000000 00000000  1000        0 2 0 100 0 0 10 0\n')
    (tmp_path / 'tcp6').write_text('  sl  local_address rem_address st\n'
                                   '   0: 00000000000000000000000000000000:1A0B 00000000000000000000000000000000:0000 0A 0 0 0 0 0 0 0\n')
    assert rdp.port_listened(3389, tmp_path) is True
    assert rdp.port_listened(6667, tmp_path) is True
    assert rdp.port_listened(40000, tmp_path) is False
    assert rdp.port_listened(3389, tmp_path / 'absent') is None
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        sock.listen()
        assert rdp.port_listened(sock.getsockname()[1]) is True


async def test_the_shared_desktop_ends_the_session_once_its_server_has_been_gone_a_while():
    from mintbot_desktop.__main__ import SharedDesktop
    clock = Clock()
    present = [True]
    desktop = SharedDesktop(rdp.Target('127.0.0.1', 3389, 'u', 'p'), clock=lambda: clock.now, listened=lambda port: present[0])
    for _ in range(3):
        clock.now += SharedDesktop.PROBE_INTERVAL
        await desktop.check()
    present[0] = False
    clock.now += SharedDesktop.PROBE_INTERVAL
    await desktop.check()  # gone, but not for long
    clock.now += SharedDesktop.GRACE
    with pytest.raises(RuntimeError, match='has been gone for 30s; giving up'):
        await desktop.check()


def test_grdctl_output_is_read_by_section_and_the_fingerprint_as_hex():
    sections = rdp.parse_status(STATUS.format(status='enabled', fingerprint=FINGERPRINT, view_only='no', credentials='\tUsername: u\n\tPassword: p:q\n'))
    assert sections['RDP']['Status'] == 'enabled' and sections['RDP']['Password'] == 'p:q' and sections['VNC'] == {'Status': 'disabled'}
    assert rdp.fingerprint_of(sections['RDP']) == bytes(range(32)).hex()
    assert rdp.fingerprint_of({'TLS fingerprint': 'not-a-digest'}) is None and rdp.fingerprint_of({}) is None
    assert rdp.given('[::1]:3390', {rdp.USERNAME_VARIABLE: 'u', rdp.PASSWORD_VARIABLE: 'p'}) == rdp.Target('::1', 3390, 'u', 'p', None, 'given')
