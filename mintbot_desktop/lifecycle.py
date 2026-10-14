"""AXP lifecycle, with an injectable command runner for side-effect-free tests."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import urllib.request

from .session import Config, prepare

PACKAGES = ['tigervnc-standalone-server', 'tigervnc-tools', 'openbox', 'novnc',
            'xauth', 'x11-utils', 'xdotool', 'scrot', 'chromium', 'python3-aiohttp', 'python3-pycryptodome']
OWNER = '<!-- Managed by mintbot.ai/mintbot-desktop -->'

# A machine's own APT sources (provider mirrors, third-party repositories) are
# not this package's to depend on: one unreachable or broken source fails the
# whole `apt-get update`, so dependencies are fetched through a private source
# list naming Debian's own mirrors instead. The host's source lists and index
# cache stay untouched.
DEBIAN_SOURCES = '''Types: deb
URIs: http://deb.debian.org/debian/
Suites: {codename} {codename}-updates
Components: main

Types: deb
URIs: http://deb.debian.org/debian-security/
Suites: {codename}-security
Components: main
'''


def debian_codename(os_release=Path('/etc/os-release')):
    """The running Debian release's codename (``trixie``); other distributions are refused."""
    fields = {}
    for line in os_release.read_text().splitlines():
        key, sep, value = line.partition('=')
        if sep:
            fields[key.strip()] = value.strip().strip('"')
    if fields.get('ID') != 'debian' or not fields.get('VERSION_CODENAME'):
        raise RuntimeError('mintbot Desktop installs its dependencies from Debian mirrors only; this is not a Debian release')
    return fields['VERSION_CODENAME']


def failure_summary(error):
    """One readable line plus the command's last output lines: the host relays only a short tail."""
    output = (error.stderr or error.stdout or '').strip()
    tail = '\n'.join(output.splitlines()[-8:])
    summary = f'{shlex.join([str(part) for part in error.cmd[:2]])} exited {error.returncode}'
    return f'{summary}:\n{tail}' if tail else summary


class Lifecycle:
    unit = 'mintbot-ext-mintbot.ai-mintbot-desktop-svc-desktop-web.service'

    def __init__(self, *, source, env=None, run=subprocess.run, os_release=Path('/etc/os-release')):
        self.source = Path(source)
        self.env = dict(os.environ if env is None else env)
        self.run = run
        self.os_release = Path(os_release)
        self.prefix = Path(self.env.get('AXP_PREFIX', '/opt/mintbot-desktop')).resolve()
        self.state = Path(self.env.get('AXP_STATE_DIR', '/var/lib/axp/mintbot.ai/mintbot-desktop')).resolve()
        self.cache = Path(self.env.get('AXP_CACHE_DIR', '/var/cache/mintbot-desktop')).resolve()
        self.hermes = Path(self.env.get('AXP_HERMES_HOME') or self.env.get('HERMES_HOME') or Path.home() / '.hermes').resolve()
        if len(self.state.parts) < 4 or len(self.prefix.parts) < 3 or self.state == self.prefix:
            raise ValueError('Unsafe install/state paths')

    def checked(self, argv):
        result = self.run(argv, env={**os.environ, **self.env, 'DEBIAN_FRONTEND': 'noninteractive'}, capture_output=True, text=True)
        result.check_returncode()
        return result

    def apt_options(self):
        """APT options confining both update and install to the declared Debian mirrors.

        A private index directory keeps `apt-get update` from pruning the
        host's own index files, which it does for every source it no longer
        sees. Package downloads still land in the shared archive cache.
        """
        apt = self.cache / 'apt'
        (apt / 'sources.list.d').mkdir(parents=True, exist_ok=True)
        (apt / 'lists' / 'partial').mkdir(parents=True, exist_ok=True)
        (apt / 'debian.sources').write_text(DEBIAN_SOURCES.format(codename=debian_codename(self.os_release)))
        return ['-o', 'DPkg::Lock::Timeout=120',
                '-o', f'Dir::Etc::SourceList={apt / "debian.sources"}',
                '-o', f'Dir::Etc::SourceParts={apt / "sources.list.d"}',
                '-o', f'Dir::State::Lists={apt / "lists"}']

    def install(self):
        if os.geteuid() != 0:
            raise PermissionError('System GUI dependencies require root')
        probe = self.run(['dpkg-query', '-W', '-f=${db:Status-Status}\n', *PACKAGES], capture_output=True, text=True)
        if probe.returncode or probe.stdout.splitlines() != ['installed'] * len(PACKAGES):
            options = self.apt_options()
            self.checked(['apt-get', 'update', *options])
            self.checked(['apt-get', 'install', '-y', '--no-install-recommends', *options, *PACKAGES])
        skill = self.hermes / 'skills' / 'mintbot-desktop' / 'SKILL.md'
        if skill.exists() and OWNER not in skill.read_text():
            raise RuntimeError('Refusing to overwrite an unrelated Hermes skill')
        prepare(Config(state=self.state))
        lib = self.prefix / 'lib'
        lib.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.source / 'mintbot_desktop', lib / 'mintbot_desktop', dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        bindir = self.prefix / 'bin'
        bindir.mkdir(exist_ok=True)
        launcher = bindir / 'mintbot-desktop'
        launcher.write_text('#!/bin/sh\nset -eu\nexport PYTHONPATH=' + shlex.quote(str(lib)) + '\nexport AXP_STATE_DIR=' + shlex.quote(str(self.state)) + '\nexec /usr/bin/python3 -m mintbot_desktop "$@"\n')
        launcher.chmod(0o755)
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text((self.source / 'skills' / 'mintbot-desktop' / 'SKILL.md').read_text().replace('@LAUNCHER@', shlex.quote(str(launcher))).replace('@STATE@', shlex.quote(str(self.state))))
        record = self.state / 'install.json'
        record.write_text(json.dumps({'hermes_home': str(self.hermes), 'prefix': str(self.prefix)}))
        record.chmod(0o600)
        # The host creates/starts the declared command unit AFTER install.
        # No detached child, standalone systemd unit or global service here.

    def enable(self):
        # Activation hooks run before host runtime reconciliation. Disable
        # removes the managed unit, so only the host can recreate/start it.
        pass

    def disable(self):
        loaded = self.checked(['systemctl', 'show', '--property=LoadState', '--value', self.unit])
        if loaded.stdout.strip() != 'not-found':
            self.checked(['systemctl', 'stop', self.unit])

    def uninstall(self):
        self.disable()  # A failure must never proceed to delete live state.
        record = self.state / 'install.json'
        if record.exists():
            home = Path(json.loads(record.read_text())['hermes_home'])
            skill = home / 'skills' / 'mintbot-desktop' / 'SKILL.md'
            if skill.exists() and OWNER in skill.read_text():
                shutil.rmtree(skill.parent)
        if self.env.get('AXP_PURGE') == '1' and self.state.exists():
            shutil.rmtree(self.state)
        # Host removes its managed unit and install prefix. APT dependencies
        # are shared system packages and deliberately remain installed.

    def health(self):
        settings = json.loads((self.state / 'session.json').read_text())
        token = (self.state / 'token').read_text()
        request = urllib.request.Request(f'http://127.0.0.1:{settings["web_port"]}/health', headers={'Authorization': 'Bearer ' + token})
        # Never send loopback health credentials via inherited AXP proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=5) as response:
            if response.status != 200 or json.load(response).get('status') != 'ok':
                raise RuntimeError('Desktop health failed')


def main(argv=None):
    hook = (sys.argv if argv is None else argv)[1]
    try:
        instance = Lifecycle(source=Path(__file__).resolve().parents[1])
        getattr(instance, 'install' if hook == 'upgrade' else hook)()
    except subprocess.CalledProcessError as error:
        # A bare traceback hides the failed command's own message; the host
        # shows the user only the end of this output.
        print(f'mintbot-desktop: {failure_summary(error)}', file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError, RuntimeError) as error:
        print(f'mintbot-desktop: {error}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
