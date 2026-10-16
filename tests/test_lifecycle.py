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
        self.playwright = False
        self.fail = None

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        if self.fail and self.fail in args:
            return subprocess.CompletedProcess(args, 1, '', 'simulated failure')
        if args[0] == 'dpkg-query':
            return subprocess.CompletedProcess(args, 0 if self.installed else 1, 'installed\n' * (len(args)-3) if self.installed else '', '')
        if args[:2] == ['apt-get', 'install']:
            self.installed = True
        if args[1:3] == ['-m', 'venv']:
            python = Path(args[3]) / 'bin' / 'python'
            python.parent.mkdir(parents=True)
            python.touch()
        if args[1:4] == ['-m', 'pip', 'install']:
            self.playwright = True
        if args[1:] == ['-c', 'import playwright']:
            return subprocess.CompletedProcess(args, 0 if self.playwright else 1, '', '')
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
    # The browser driver venv converges the same way: built once, then probed.
    pip_installs = [c for c in commands.calls if c[1:4] == ['-m', 'pip', 'install']]
    assert len(pip_installs) == 1 and pip_installs[0][0] == str(tmp_path/'prefix'/'venv'/'bin'/'python')
    assert any('playwright' in part for part in pip_installs[0])
    assert sum(c[1:3] == ['-m', 'venv'] for c in commands.calls) == 1
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


def test_driver_install_failure_does_not_report_success(tmp_path):
    commands = FakeCommands()
    commands.fail = 'install'
    commands.installed = True  # APT is satisfied; only pip fails
    hooks = lifecycle(tmp_path, commands)
    with pytest.raises(subprocess.CalledProcessError):
        hooks.install()
    assert not (tmp_path/'prefix'/'bin'/'mintbot-desktop').exists()


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


@pytest.mark.parametrize('host', ['mintbot', 'mintbot/2026.10', 'other/1', ''])
def test_disable_never_controls_a_host_service(tmp_path, host):
    commands = FakeCommands()
    hooks = lifecycle(tmp_path, commands)
    hooks.env['AXP_HOST'] = host
    hooks.install()
    before = list(commands.calls)
    hooks.disable()
    hooks.disable()
    assert commands.calls == before
    assert (tmp_path/'state'/'token').exists()


def test_standalone_install_reports_foreground_workflow_and_enable_runs_it(tmp_path, capsys):
    commands = FakeCommands()
    hooks = lifecycle(tmp_path, commands)
    hooks.env.pop('AXP_HOST')
    hooks.install()
    launcher = str(hooks.prefix / 'bin' / 'mintbot-desktop')
    assert launcher in capsys.readouterr().out
    before = len(commands.calls)
    hooks.enable()
    assert commands.calls[before:] == [[launcher, 'serve']]
    commands.fail = 'serve'
    with pytest.raises(subprocess.CalledProcessError):
        hooks.enable()


@pytest.mark.parametrize('host', ['mintbot', 'mintbot/2026.10', 'other/1'])
def test_host_enable_leaves_activation_to_host(tmp_path, host):
    commands = FakeCommands()
    hooks = lifecycle(tmp_path, commands)
    hooks.env['AXP_HOST'] = host
    hooks.install()
    before = list(commands.calls)
    hooks.enable()
    assert commands.calls == before


@pytest.mark.parametrize('hook', ['disable', 'uninstall'])
@pytest.mark.parametrize('host', ['', 'mintbot/2026.10'])
def test_live_session_refuses_disable_or_uninstall_without_signalling(tmp_path, hook, host):
    import fcntl
    commands = FakeCommands()
    hooks = lifecycle(tmp_path, commands)
    hooks.env['AXP_HOST'] = host
    hooks.install()
    hooks.env['AXP_PURGE'] = '1'
    before = list(commands.calls)
    with (hooks.state / 'session.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match='Stop.*foreground.*service'):
            getattr(hooks, hook)()
        assert (hooks.state / 'token').exists()
        assert (hooks.prefix / 'bin' / 'mintbot-desktop').exists()
    assert commands.calls == before
    getattr(hooks, hook)()  # stale lock file is not a running session


def test_standalone_uninstall_removes_prefix_preserves_state_and_is_repeatable(tmp_path):
    hooks = lifecycle(tmp_path, FakeCommands())
    hooks.env.pop('AXP_HOST')
    hooks.install()
    token = (hooks.state / 'token').read_bytes()
    hooks.uninstall()
    assert not hooks.prefix.exists()
    assert (hooks.state / 'token').read_bytes() == token
    hooks.uninstall()
    hooks.install()
    assert (hooks.state / 'token').read_bytes() == token
    hooks.env['AXP_PURGE'] = '1'
    hooks.uninstall()
    hooks.uninstall()
    assert not hooks.state.exists()


def test_host_uninstall_leaves_prefix_cleanup_to_host(tmp_path):
    hooks = lifecycle(tmp_path, FakeCommands())
    hooks.install()
    hooks.uninstall()
    assert (hooks.prefix / 'bin' / 'mintbot-desktop').exists()


@pytest.mark.parametrize('prefix', ['/', '/opt', '/usr/local', '/var/lib', '/home', '/root', '', '.', 'relative/prefix'])
def test_unsafe_prefix_is_refused_before_any_command(tmp_path, prefix):
    from mintbot_desktop.lifecycle import Lifecycle
    commands = FakeCommands()
    with pytest.raises(ValueError, match='Unsafe'):
        Lifecycle(source=ROOT, env={'AXP_PREFIX': prefix, 'AXP_STATE_DIR': str(tmp_path/'state')}, run=commands)
    assert not commands.calls


@pytest.mark.parametrize('nested', ['prefix-in-state', 'state-in-prefix', 'prefix-is-home', 'prefix-is-source', 'symlink'])
def test_prefix_aliases_and_overlaps_are_refused(tmp_path, nested):
    from mintbot_desktop.lifecycle import Lifecycle
    prefix, state = tmp_path/'prefix', tmp_path/'state'
    home = tmp_path/'hermes'
    if nested == 'prefix-in-state':
        prefix = state/'prefix'
    elif nested == 'state-in-prefix':
        state = prefix/'state'
    elif nested == 'prefix-is-home':
        prefix = home
    elif nested == 'prefix-is-source':
        prefix = ROOT
    else:
        target = tmp_path/'target'
        target.mkdir()
        prefix.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match='Unsafe'):
        Lifecycle(source=ROOT, env={'AXP_PREFIX':str(prefix), 'AXP_STATE_DIR':str(state),
                                  'HERMES_HOME':str(home)}, run=FakeCommands())


@pytest.mark.parametrize('record', ['missing', 'wrong-prefix', 'malformed', 'symlink'])
def test_uninstall_refuses_unrecognized_installation_before_deleting_anything(tmp_path, record):
    hooks = lifecycle(tmp_path, FakeCommands())
    hooks.env.pop('AXP_HOST')
    hooks.install()
    hooks.env['AXP_PURGE'] = '1'
    path = hooks.state/'install.json'
    if record == 'missing':
        path.unlink()
    elif record == 'wrong-prefix':
        path.write_text(json.dumps({'prefix':str(tmp_path/'other'), 'hermes_home':str(hooks.hermes)}))
    elif record == 'malformed':
        path.write_text('{}')
    else:
        other = tmp_path/'record.json'
        path.rename(other)
        path.symlink_to(other)
    with pytest.raises(ValueError, match='installation record'):
        hooks.uninstall()
    assert (hooks.prefix/'bin'/'mintbot-desktop').exists()
    assert (hooks.state/'token').exists()
    assert (hooks.hermes/'skills'/'mintbot-desktop'/'SKILL.md').exists()


def test_standalone_install_refuses_to_claim_nonempty_unowned_prefix(tmp_path):
    commands = FakeCommands()
    hooks = lifecycle(tmp_path, commands)
    hooks.env.pop('AXP_HOST')
    hooks.prefix.mkdir()
    unrelated = hooks.prefix/'unrelated'
    unrelated.write_text('keep')
    with pytest.raises(ValueError, match='installation record'):
        hooks.install()
    assert unrelated.read_text() == 'keep'
    assert commands.calls == []


def test_standalone_retry_after_driver_failure_preserves_ownership(tmp_path):
    commands = FakeCommands()
    hooks = lifecycle(tmp_path, commands)
    hooks.env.pop('AXP_HOST')
    commands.installed = True
    commands.fail = 'install'
    with pytest.raises(subprocess.CalledProcessError):
        hooks.install()
    commands.fail = None
    hooks.install()
    assert (hooks.prefix/'bin'/'mintbot-desktop').exists()
    hooks.uninstall()
    assert not hooks.prefix.exists()


@pytest.mark.parametrize('target', ['prefix', 'state', 'skill', 'lock'])
def test_uninstall_rechecks_symlinks_before_cleanup(tmp_path, target):
    hooks = lifecycle(tmp_path, FakeCommands())
    hooks.env.pop('AXP_HOST')
    hooks.install()
    hooks.env['AXP_PURGE'] = '1'
    paths = {'prefix': hooks.prefix, 'state': hooks.state,
             'skill': hooks.hermes/'skills'/'mintbot-desktop',
             'lock': hooks.state/'session.lock'}
    path = paths[target]
    if target == 'lock':
        path.touch()
    outside = tmp_path/'outside'
    path.rename(outside)
    path.symlink_to(outside, target_is_directory=target != 'lock')
    with pytest.raises(ValueError, match='Unsafe'):
        hooks.uninstall()
    assert outside.exists()
    assert (hooks.state/'token').exists()
    assert (hooks.prefix/'bin'/'mintbot-desktop').exists()


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
