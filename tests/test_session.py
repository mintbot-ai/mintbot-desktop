import asyncio
import importlib.util
import os
import socket
import stat
import subprocess
import time

import pytest


def session_module():
    assert importlib.util.find_spec('mintbot_desktop.session'), 'session runner missing'
    from mintbot_desktop import session
    return session


def test_safe_config_and_browser_commands(tmp_path):
    s = session_module()
    cfg = s.Config(state=tmp_path)
    assert (cfg.display, cfg.web_port, cfg.vnc_port, cfg.cdp_port) == (13, 6081, 5913, 9223)
    argv = s.vnc_command(cfg)
    assert argv[1] == ':13'
    for key, value in {'-localhost':'yes', '-interface':'127.0.0.1', '-SecurityTypes':'VncAuth', '-rfbport':'5913', '-nolisten':'tcp'}.items():
        assert argv[argv.index(key)+1] == value
    assert str(tmp_path / 'Xauthority') in argv
    browser = s.browser_command(cfg, {'https_proxy':'http://127.42.1.2:3128'})
    assert '--remote-debugging-address=127.0.0.1' in browser
    assert '--remote-debugging-port=9223' in browser
    assert f'--user-data-dir={tmp_path}/chromium' in browser
    assert '--proxy-server=http://127.42.1.2:3128' in browser
    assert '--disable-quic' in browser
    # The manifest declares egress to any host, so the host sets no proxy: Chromium goes direct.
    assert not any(a.startswith('--proxy-server=') for a in s.browser_command(cfg, {}))
    with pytest.raises(ValueError):
        s.Config(state=tmp_path, display=12)
    with pytest.raises(ValueError):
        s.Config(state=tmp_path, web_port=6080)


def test_prepare_is_idempotent_and_private(tmp_path):
    s = session_module()
    cfg = s.Config(state=tmp_path / 'state', display=193, web_port=16081, vnc_port=15913, cdp_port=19223)
    s.prepare(cfg)
    first = {p.name: p.read_bytes() for p in cfg.state.iterdir() if p.is_file()}
    s.prepare(cfg)
    assert first == {p.name: p.read_bytes() for p in cfg.state.iterdir() if p.is_file()}
    for p in cfg.state.iterdir():
        assert stat.S_IMODE(p.stat().st_mode) & 0o077 == 0
    env = s.gui_environment(cfg)
    assert env['DISPLAY'] == ':193'
    assert env['XAUTHORITY'] == str(cfg.state / 'Xauthority')
    assert env['HOME'] == str(cfg.state)
    assert len(first['token']) >= 32
    assert len(first['vnc-password']) == 8


def test_prepare_rejects_symlink_credentials_and_repairs_modes(tmp_path):
    s = session_module()
    cfg = s.Config(state=tmp_path / 'state')
    s.prepare(cfg)
    for name in ['vnc-password', 'Xauthority']:
        (cfg.state / name).chmod(0o644)
    s.prepare(cfg)
    assert (cfg.state / 'vnc-password').stat().st_mode & 0o077 == 0
    assert (cfg.state / 'Xauthority').stat().st_mode & 0o077 == 0
    (cfg.state / 'Xauthority').unlink()
    (cfg.state / 'Xauthority').symlink_to(tmp_path / 'outside')
    with pytest.raises(ValueError):
        s.prepare(cfg)


def test_xauth_failure_is_retryable(tmp_path, monkeypatch):
    s = session_module()
    cfg = s.Config(state=tmp_path / 'state')
    original = s.subprocess.run

    def fail_xauth(args, **kwargs):
        if args[0] == 'xauth':
            raise subprocess.CalledProcessError(1, args)
        return original(args, **kwargs)

    monkeypatch.setattr(s.subprocess, 'run', fail_xauth)
    with pytest.raises(subprocess.CalledProcessError):
        s.prepare(cfg)
    monkeypatch.setattr(s.subprocess, 'run', original)
    s.prepare(cfg)
    assert (cfg.state / 'Xauthority').stat().st_size > 0


def test_supervisor_cleans_children_when_start_fails(tmp_path):
    s = session_module()
    cfg = s.Config(state=tmp_path)
    child = subprocess.Popen(['sleep', '60'], start_new_session=True)
    with pytest.raises(RuntimeError):
        with s.Children() as children:
            children.processes.append(child)
            raise RuntimeError('later startup failure')
    assert child.poll() is not None


def test_vnc_secret_fits_the_eight_character_cap_with_a_wide_alphabet():
    s = session_module()
    secret = s.vnc_secret()
    assert len(secret) == 8
    assert set(secret) <= set(s.VNC_SECRET_ALPHABET)
    # Lookalike characters stay out so a user can type the secret from a screen.
    assert not set('0O1lI') & set(s.VNC_SECRET_ALPHABET)
    assert s.vnc_secret() != secret


def test_port_available_reports_listeners_but_not_time_wait_leftovers():
    s = session_module()
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert s.port_available(port) is False
    assert s.port_available(port) is True
    # A finished connection leaves the server side in TIME_WAIT; that must not block a restart.
    with socket.socket() as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(('127.0.0.1', 0))
        server.listen()
        port = server.getsockname()[1]
        with socket.create_connection(('127.0.0.1', port)) as client:
            accepted, _ = server.accept()
            accepted.close()
    assert s.port_available(port) is True


def test_children_forget_exited_processes_on_restart():
    s = session_module()
    with s.Children() as children:
        for _ in range(5):
            children.start(['true']).wait()
        survivor = children.start(['sleep', '60'])
        assert children.processes == [survivor]
    assert survivor.poll() is not None


def test_supervised_restarts_a_dying_child_then_gives_up():
    from mintbot_desktop import __main__ as main
    with session_module().Children() as children:
        child = main.Supervised(children, 'probe', ['true'], os.environ.copy())
        seen = {child.process.pid}
        for _ in range(main.RESTART_LIMIT):
            child.process.wait()
            child.check()
            assert child.process.pid not in seen
            seen.add(child.process.pid)
        child.process.wait()
        with pytest.raises(RuntimeError, match='keeps exiting'):
            child.check()


async def test_wait_for_display_probes_only_once_the_socket_exists(tmp_path):
    """A probe before the socket exists falls back to TCP, which the host's
    address filter drops silently: every service start waited out a TCP
    connect timeout. The probe must not run until the socket is there."""
    from mintbot_desktop import __main__ as main
    x = subprocess.Popen(['sleep', '60'])
    try:
        marker = tmp_path / 'probed'
        probe = ('sh', '-c', f'touch {marker}; test -S {tmp_path}/.X11-unix/X14 || test -e {tmp_path}/.X11-unix/X14')
        task = asyncio.ensure_future(main.wait_for_display(x, os.environ.copy(), asyncio.Event(), 14, tmp=tmp_path, probe=probe))
        await asyncio.sleep(.5)
        assert not task.done() and not marker.exists(), 'probed before the X socket existed'
        (tmp_path / '.X11-unix').mkdir()
        (tmp_path / '.X11-unix' / 'X14').touch()
        await asyncio.wait_for(task, 5)
        assert marker.exists()
    finally:
        x.kill()
        x.wait()


async def test_wait_for_display_bounds_a_hung_probe(tmp_path):
    from mintbot_desktop import __main__ as main
    (tmp_path / '.X11-unix').mkdir()
    (tmp_path / '.X11-unix' / 'X14').touch()
    x = subprocess.Popen(['sleep', '60'])
    try:
        started = time.monotonic()
        with pytest.raises(RuntimeError, match='readiness timed out'):
            await main.wait_for_display(x, os.environ.copy(), asyncio.Event(), 14, tmp=tmp_path,
                                        probe=('sleep', '60'), timeout=.6, probe_timeout=.2)
        assert time.monotonic() - started < 3
        assert not subprocess.run(['pgrep', '-f', '^sleep 60$'], capture_output=True).stdout.count(b'\n') > 1
    finally:
        x.kill()
        x.wait()


def test_dead_x_server_leftovers_do_not_block_a_restart(tmp_path):
    s = session_module()
    assert s.display_in_use(13, tmp_path) is False
    finished = subprocess.Popen(['true'])
    finished.wait()
    (tmp_path / '.X11-unix').mkdir()
    (tmp_path / '.X11-unix' / 'X13').touch()
    (tmp_path / '.X13-lock').write_text(f'{finished.pid:>10}\n')
    # Xvnc replaces a dead server's lock and socket itself; refusing them left
    # the service failing on every restart after a single unclean exit.
    assert s.display_in_use(13, tmp_path) is False
    for owner in (f'{os.getpid():>10}\n', 'not-a-pid\n', '0\n'):
        (tmp_path / '.X13-lock').write_text(owner)
        assert s.display_in_use(13, tmp_path) is True


def test_child_named_finds_only_our_own_child():
    s = session_module()
    child = subprocess.Popen(['sleep', '60'])
    try:
        assert s.child_named('sleep') == child.pid
        assert s.child_named('no-such-program') is None
    finally:
        child.kill()
        child.wait()
    assert s.child_named('sleep') is None


def test_open_browser_run_by_hand_becomes_chromium(tmp_path):
    """From the terminal, the openbox menu or an idesk that forks the right way
    round, the launcher simply becomes Chromium."""
    s = session_module()
    cfg = s.Config(state=tmp_path)
    calls = []
    s.open_browser(cfg, run=lambda argv, **kwargs: calls.append(('run', argv)),
                   execve=lambda *args: calls.append(('execve', args)), orphan=lambda name: None)
    assert [call[0] for call in calls] == ['execve']
    file, argv, env = calls[0][1]
    assert file == 'chromium' and argv == s.browser_command(cfg, env) and env['DISPLAY'] == ':13'


def test_open_browser_run_as_the_supervised_idesk_becomes_idesk_again(tmp_path):
    """idesk's `pid = fork() != 0` makes the supervised idesk exec the icon's
    command and leaves the idesk drawing the icons as its orphan child. The
    launcher then starts the browser detached, retires that orphan and execs
    idesk: the supervisor keeps one live idesk instead of a browser that
    exits at once, followed by three idesks refusing to start beside the
    orphan and the whole session given up on."""
    s = session_module()
    cfg = s.Config(state=tmp_path)
    orphan = os.posix_spawnp('sleep', ['sleep', '60'], os.environ)
    calls = []
    s.open_browser(cfg, run=lambda argv, **kwargs: calls.append(('run', argv, kwargs['env'])),
                   execve=lambda *args: calls.append(('execve', args)),
                   orphan=lambda name: orphan if name == 'idesk' else None)
    assert [call[0] for call in calls] == ['run', 'execve']
    assert calls[0][1] == ['setsid', '--fork', *s.browser_command(cfg, calls[0][2])]
    assert calls[1][1][:2] == ('idesk', ['idesk'])
    assert calls[1][1][2]['DISPLAY'] == ':13'
    # The orphan was signalled and reaped before idesk took the process over.
    with pytest.raises(ChildProcessError):
        os.waitpid(orphan, os.WNOHANG)
