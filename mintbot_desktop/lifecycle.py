"""AXP lifecycle, with an injectable command runner for side-effect-free tests."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import urllib.request

from .browse import PLAYWRIGHT_REQUIREMENT
from .session import Config, prepare

# Session: X server, window manager, browser, gateway. Agent tooling: window
# and clipboard helpers (wmctrl, xclip), screenshots (scrot, maim), the
# accessibility bus Hermes's computer_use toolset speaks to (at-spi2-core),
# and a virtualenv for the Playwright browser driver.
PACKAGES = ['tigervnc-standalone-server', 'tigervnc-tools', 'openbox', 'novnc',
            'xauth', 'x11-utils', 'xdotool', 'scrot', 'maim', 'wmctrl', 'xclip', 'at-spi2-core',
            'chromium', 'python3-aiohttp', 'python3-pycryptodome', 'python3-venv', 'python3-pip']
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

    def __init__(self, *, source, env=None, run=subprocess.run, os_release=Path('/etc/os-release')):
        self.source = Path(source)
        self.env = dict(os.environ if env is None else env)
        self.run = run
        self.os_release = Path(os_release)
        self.prefix = Path(self.env.get('AXP_PREFIX', '/opt/mintbot-desktop'))
        self.state = Path(self.env.get('AXP_STATE_DIR', '/var/lib/axp/mintbot.ai/mintbot-desktop'))
        self.cache = Path(self.env.get('AXP_CACHE_DIR', '/var/cache/mintbot-desktop')).resolve()
        self.hermes = Path(self.env.get('AXP_HERMES_HOME') or self.env.get('HERMES_HOME') or Path.home() / '.hermes').resolve()
        self.validate_paths()

    def validate_paths(self):
        """Only dedicated, canonical directories may become deletion targets."""
        protected = {Path('/usr/local'), Path('/var/lib'), Path('/var/cache'),
                     Path.home().resolve(), self.hermes, self.source.resolve()}
        for path, depth in ((self.prefix, 3), (self.state, 4)):
            if (not path.is_absolute() or path != path.resolve() or len(path.parts) < depth
                    or any(target == path or path in target.parents for target in protected)):
                raise ValueError('Unsafe install/state paths')
        if (self.prefix == self.state or self.prefix in self.state.parents
                or self.state in self.prefix.parents or self.prefix == self.cache
                or self.prefix in self.cache.parents):
            raise ValueError('Unsafe overlapping install/state/cache paths')

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

    def install_browser_driver(self):
        """Playwright in the extension's own virtualenv, for `mintbot-desktop browse`.

        It only ever attaches to the session's Chromium over CDP, so no
        browser download: the pip package is the whole driver.
        """
        venv = self.prefix / 'venv'
        python = venv / 'bin' / 'python'
        if python.exists() and self.run([str(python), '-c', 'import playwright'], capture_output=True, text=True).returncode == 0:
            return
        if not python.exists():
            self.checked(['/usr/bin/python3', '-m', 'venv', str(venv)])
        self.checked([str(python), '-m', 'pip', 'install', '--quiet', '--disable-pip-version-check',
                      '--cache-dir', str(self.cache / 'pip'), PLAYWRIGHT_REQUIREMENT])

    def validate_record(self):
        record = self.state / 'install.json'
        try:
            data = json.loads(record.read_text())
            if (record.is_symlink() or not isinstance(data, dict)
                    or data.get('prefix') != str(self.prefix)
                    or data.get('hermes_home') != str(self.hermes)):
                raise ValueError('mismatched paths')
        except (OSError, ValueError) as error:
            raise ValueError('Unsafe or missing installation record; refusing cleanup/adoption') from error

    def install(self):
        if not self.env.get('AXP_HOST') and self.prefix.exists() and any(self.prefix.iterdir()):
            self.validate_record()
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
        # Record ownership before populating the prefix so a failed download is retryable.
        record = self.state / 'install.json'
        record.write_text(json.dumps({'hermes_home': str(self.hermes), 'prefix': str(self.prefix)}))
        record.chmod(0o600)
        self.install_browser_driver()
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
        # A host owns the declared command service; a hand-install owns no daemon.
        if not self.env.get('AXP_HOST'):
            print(f'Installed. Start in the foreground: {shlex.quote(str(launcher))} serve')
            print('Or run ./enable.sh in the foreground. Stop with Ctrl-C/SIGTERM before disable/uninstall.')

    def enable(self):
        if not self.env.get('AXP_HOST'):
            # Inherit the terminal: no detached child or service-manager dependency.
            self.run([str(self.prefix / 'bin' / 'mintbot-desktop'), 'serve'],
                     env={**os.environ, **self.env}).check_returncode()

    @contextmanager
    def stopped(self):
        """Hold the supervisor's existing lock; never signal somebody else's process."""
        self.validate_paths()
        path = self.state / 'session.lock'
        if path.is_symlink():
            raise ValueError('Unsafe session lock')
        if not path.exists():
            yield
            return
        with path.open('r') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('Stop the foreground runner or its owning service before disable/uninstall') from None
            yield

    def disable(self):
        with self.stopped():
            pass

    def uninstall(self):
        with self.stopped():
            if self.state.exists() or (not self.env.get('AXP_HOST') and self.prefix.exists()):
                self.validate_record()
                skill = self.hermes / 'skills' / 'mintbot-desktop' / 'SKILL.md'
                if skill != skill.resolve():
                    raise ValueError('Unsafe managed skill path')
                if skill.exists() and OWNER in skill.read_text():
                    shutil.rmtree(skill.parent)
            if not self.env.get('AXP_HOST') and self.prefix.exists():
                shutil.rmtree(self.prefix)
            if self.env.get('AXP_PURGE') == '1' and self.state.exists():
                shutil.rmtree(self.state)
        # APT dependencies are shared system packages and deliberately remain.

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
