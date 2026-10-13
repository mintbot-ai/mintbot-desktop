import importlib.util
import os
import stat
import subprocess

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
