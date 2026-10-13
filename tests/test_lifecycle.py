import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


class FakeCommands:
    def __init__(self):
        self.calls = []
        self.installed = False
        self.fail = None

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        if self.fail and self.fail in args:
            return subprocess.CompletedProcess(args, 1, '', 'simulated failure')
        if args[0] == 'dpkg-query':
            return subprocess.CompletedProcess(args, 0 if self.installed else 1, 'installed\n' * (len(args)-3) if self.installed else '', '')
        if args[:2] == ['apt-get', 'install']:
            self.installed = True
        return subprocess.CompletedProcess(args, 0, '', '')


def lifecycle(tmp_path, run):
    assert importlib.util.find_spec('mintbot_desktop.lifecycle'), 'lifecycle missing'
    from mintbot_desktop.lifecycle import Lifecycle
    return Lifecycle(source=ROOT, env={'AXP_PREFIX':str(tmp_path/'prefix'), 'AXP_STATE_DIR':str(tmp_path/'state'), 'HERMES_HOME':str(tmp_path/'hermes'), 'AXP_HOST':'mintbot'}, run=run)


def test_install_idempotently_converges_without_starting_host_service(tmp_path):
    commands = FakeCommands()
    hooks = lifecycle(tmp_path, commands)
    hooks.install()
    first = (tmp_path/'state'/'token').read_bytes()
    hooks.install()
    assert (tmp_path/'state'/'token').read_bytes() == first
    assert sum(c[:2] == ['apt-get','install'] for c in commands.calls) == 1
    assert not any(c[0] == 'systemctl' for c in commands.calls)
    launcher = tmp_path/'prefix'/'bin'/'mintbot-desktop'
    assert launcher.is_file()
    assert (tmp_path/'hermes'/'skills'/'mintbot-desktop'/'SKILL.md').is_file()
    result = subprocess.run([str(launcher), 'env'], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['DISPLAY'] == ':13'
    assert json.loads(result.stdout)['XAUTHORITY'] == str(tmp_path/'state'/'Xauthority')


def test_apt_failure_does_not_report_success_or_start_services(tmp_path):
    commands = FakeCommands()
    commands.fail = 'update'
    hooks = lifecycle(tmp_path, commands)
    with pytest.raises(subprocess.CalledProcessError):
        hooks.install()
    assert not (tmp_path/'prefix'/'bin'/'mintbot-desktop').exists()
    assert not any(c[0] == 'systemctl' for c in commands.calls)


def test_disable_preserves_state_and_failed_stop_prevents_purge(tmp_path):
    commands = FakeCommands()
    hooks = lifecycle(tmp_path, commands)
    hooks.install()
    hooks.disable()
    assert commands.calls[-1] == ['systemctl', 'stop', hooks.unit]
    assert (tmp_path/'state'/'token').exists()
    hooks.enable()
    assert commands.calls[-1] == ['systemctl', 'start', hooks.unit]
    commands.fail = 'stop'
    hooks.env['AXP_PURGE'] = '1'
    with pytest.raises(subprocess.CalledProcessError):
        hooks.uninstall()
    assert (tmp_path/'state'/'token').exists()


def test_uninstall_retains_state_unless_explicit_purge(tmp_path):
    commands = FakeCommands()
    hooks = lifecycle(tmp_path, commands)
    hooks.install()
    hooks.uninstall()
    assert (tmp_path/'state'/'token').exists()
    assert not (tmp_path/'hermes'/'skills'/'mintbot-desktop').exists()
    hooks.env['AXP_PURGE'] = '1'
    hooks.uninstall()
    assert not (tmp_path/'state').exists()
