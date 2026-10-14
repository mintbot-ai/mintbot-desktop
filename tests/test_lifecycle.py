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


def lifecycle(tmp_path, run, os_release='ID=debian\nVERSION_CODENAME=trixie\n'):
    assert importlib.util.find_spec('mintbot_desktop.lifecycle'), 'lifecycle missing'
    from mintbot_desktop.lifecycle import Lifecycle
    (tmp_path/'os-release').write_text(os_release)
    env = {'AXP_PREFIX':str(tmp_path/'prefix'), 'AXP_STATE_DIR':str(tmp_path/'state'), 'AXP_CACHE_DIR':str(tmp_path/'cache'),
           'HERMES_HOME':str(tmp_path/'hermes'), 'AXP_HOST':'mintbot'}
    return Lifecycle(source=ROOT, env=env, run=run, os_release=tmp_path/'os-release')


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


def test_apt_uses_only_declared_debian_mirrors(tmp_path):
    """A machine's own APT sources (provider mirrors, third-party repositories)
    are not this package's to depend on; both apt-get calls must read a private
    source list naming Debian's own mirrors and a private index dir."""
    commands = FakeCommands()
    hooks = lifecycle(tmp_path, commands, os_release='PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nID=debian\nVERSION_CODENAME=trixie\n')
    hooks.install()
    apt_calls = [c for c in commands.calls if c[0] == 'apt-get']
    assert [c[1] for c in apt_calls] == ['update', 'install']
    sources = tmp_path/'cache'/'apt'/'debian.sources'
    text = sources.read_text()
    assert 'http://deb.debian.org/debian/' in text and 'http://deb.debian.org/debian-security/' in text
    assert 'Suites: trixie trixie-updates' in text and 'Suites: trixie-security' in text
    for call in apt_calls:
        options = [call[i+1] for i, part in enumerate(call) if part == '-o']
        assert f'Dir::Etc::SourceList={sources}' in options
        assert f'Dir::Etc::SourceParts={tmp_path/"cache"/"apt"/"sources.list.d"}' in options
        assert f'Dir::State::Lists={tmp_path/"cache"/"apt"/"lists"}' in options
    assert (tmp_path/'cache'/'apt'/'lists'/'partial').is_dir()
    assert not any((tmp_path/'cache'/'apt'/'sources.list.d').iterdir())


def test_non_debian_host_is_refused_before_apt_runs(tmp_path):
    commands = FakeCommands()
    hooks = lifecycle(tmp_path, commands, os_release='ID=ubuntu\nVERSION_CODENAME=noble\n')
    with pytest.raises(RuntimeError, match='Debian'):
        hooks.install()
    assert not any(c[0] == 'apt-get' for c in commands.calls)


def test_failure_summary_keeps_the_failed_commands_own_message():
    from mintbot_desktop.lifecycle import failure_summary
    error = subprocess.CalledProcessError(100, ['apt-get', 'update', '-o', 'x'], output='Hit:1 ...',
                                         stderr='W: first\nE: Failed to fetch http://mirror.hetzner.com/... 403 Forbidden\n')
    summary = failure_summary(error)
    assert summary.startswith('apt-get update exited 100:')
    assert summary.endswith('403 Forbidden')
    assert 'Traceback' not in summary


def test_main_reports_a_failed_command_without_a_traceback(tmp_path, capsys, monkeypatch):
    import mintbot_desktop.lifecycle as module

    class Failing(module.Lifecycle):
        def install(self):
            raise subprocess.CalledProcessError(100, ['apt-get', 'update'], stderr='E: mirror refused\n')

    monkeypatch.setattr(module, 'Lifecycle', Failing)
    with pytest.raises(SystemExit) as exit_info:
        module.main(['lifecycle', 'install'])
    assert exit_info.value.code == 1
    err = capsys.readouterr().err
    assert err.strip().splitlines() == ['mintbot-desktop: apt-get update exited 100:', 'E: mirror refused']


def test_disable_preserves_state_and_failed_stop_prevents_purge(tmp_path):
    commands = FakeCommands()
    hooks = lifecycle(tmp_path, commands)
    hooks.install()
    hooks.disable()
    assert commands.calls[-1] == ['systemctl', 'stop', hooks.unit]
    assert (tmp_path/'state'/'token').exists()
    before_enable = list(commands.calls)
    hooks.enable()
    assert commands.calls == before_enable  # host recreates/starts its unit later
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
