"""AXP lifecycle, with an injectable command runner for side-effect-free tests."""
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request

from .attach import SCRAPERS
from .browse import PLAYWRIGHT_REQUIREMENT
from .session import Config, prepare

# How long the health hook waits for a session that is still coming up. The
# host gives a health hook 60 seconds; a gateway that is silent for 25 is down.
HEALTH_WAIT_S = 25.0
HEALTH_RETRY_S = 0.5

# Session: X server (standalone: the package's own desktop; scraping: the
# computer's own, attach.py), window manager, desktop icons (idesk: the
# browser icon), browser, gateway. Agent tooling: window and clipboard
# helpers (wmctrl, xclip), screenshots (scrot, maim), the accessibility bus
# Hermes's computer_use toolset speaks to (at-spi2-core), and a virtualenv
# for the Playwright browser driver.
PACKAGES = ['tigervnc-standalone-server', 'tigervnc-scraping-server', 'tigervnc-tools', 'openbox', 'idesk', 'novnc',
            'xauth', 'x11-utils', 'xdotool', 'scrot', 'maim', 'wmctrl', 'xclip', 'at-spi2-core',
            'chromium', 'python3-aiohttp', 'python3-pycryptodome', 'python3-venv', 'python3-pip']

# Where APT is not this package's to run (not root, or not Debian: a
# customer's own Linux computer), the programs behind those packages must be
# there already. What a session cannot do without, each with the Debian
# package to name; one X server suffices: the standalone one for a desktop
# of the package's own, the scraping one for the computer's own desktop.
REQUIRED_PROGRAMS = {'tigervncpasswd': 'tigervnc-tools', 'xauth': 'xauth', 'xdpyinfo': 'x11-utils', 'xprop': 'x11-utils'}
X_SERVERS = {'Xtigervnc': 'tigervnc-standalone-server', **{name: 'tigervnc-scraping-server' for name in SCRAPERS}}
NOVNC = Path('/usr/share/novnc/core/rfb.js')
OPTIONAL_PROGRAMS = {'openbox': 'openbox', 'idesk': 'idesk', 'chromium': 'chromium', 'xdotool': 'xdotool',
                     'wmctrl': 'wmctrl', 'xclip': 'xclip', 'maim': 'maim'}
OWNER = '<!-- Managed by mintbot.ai/mintbot-desktop -->'

# IronRDP's browser client (Devolutions, MIT OR Apache-2.0), which shows a
# Wayland desktop's own RDP server in the panel (app.py, rdp.py). Prebuilt
# on npm: the web component, and the RDP engine with its WebAssembly inside
# the script. Pinned by version and by the registry's integrity digest; the
# install hook fetches them into the prefix (install_viewer), the way it
# fetches the Playwright driver with pip.
NPM_REGISTRY = 'https://registry.npmjs.org'
VIEWER_DIRECTORY = 'ironrdp'
VIEWER_PACKAGES = (
    ('@devolutions/iron-remote-desktop', '0.11.0', 'iron-remote-desktop.js',
     'sha512-ZH8xfabDA+6rAXkW2V3XgRdj0LHOsHoXAzars/H22DmKsHM39LdMfheKFUR3qLaFDM8mjf6DTFngzjgr+Byx0Q=='),
    ('@devolutions/iron-remote-desktop-rdp', '0.7.0', 'iron-remote-desktop-rdp.js',
     'sha512-CclAh4OS9aBoPJT0l7bih7ETOHPnB9KjT0drB0a6r5+Qh95pTEuQCx7uFN/XA2AlkFlrqx+DBAMDGYXmgsjHAQ=='),
)
VIEWER_NOTICE = '''IronRDP browser client, by Devolutions (https://github.com/Devolutions/IronRDP).
Licensed under MIT OR Apache-2.0. Fetched prebuilt from the npm registry by
the mintbot Desktop install hook; the packages and versions are named in
mintbot_desktop/lifecycle.py.
'''


def tarball_url(name, version):
    """Where the npm registry serves a package's tarball (`@scope/name` keeps its scope in the path)."""
    bare = name.rpartition('/')[2]
    return f'{NPM_REGISTRY}/{name}/-/{bare}-{version}.tgz'


def verify_integrity(data, integrity):
    """`data` is what the registry's Subresource Integrity string (`sha512-<base64>`) names, or ValueError."""
    algorithm, _, digest = integrity.partition('-')
    if base64.b64encode(hashlib.new(algorithm, data).digest()).decode() != digest:
        raise ValueError(f'the download does not match its pinned {algorithm} digest')


def fetch_url(url, timeout=120):
    """The body at `url`, through whatever proxy the environment names (the host's egress proxy)."""
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read()


def fetch_viewer(directory, packages=VIEWER_PACKAGES, fetch=fetch_url):
    """IronRDP's browser client scripts in `directory`.

    Each package is fetched from the npm registry once per pinned version,
    checked against the pinned integrity digest, and only its script taken
    from the tarball. A fetch that fails leaves what was there; the next
    install or upgrade tries again.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name, version, filename, integrity in packages:
        script = directory / filename
        stamp = directory / f'{filename}.version'
        if script.is_file() and stamp.is_file() and stamp.read_text().strip() == version:
            continue
        data = fetch(tarball_url(name, version))
        verify_integrity(data, integrity)
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as tarball:
            member = tarball.extractfile(f'package/{filename}')
            if member is None:
                raise RuntimeError(f'{name} {version} ships no {filename}')
            body = member.read()
        staging = script.with_suffix('.tmp')
        staging.write_bytes(body)
        os.replace(staging, script)
        stamp.write_text(version + '\n')
    (directory / 'NOTICE').write_text(VIEWER_NOTICE)

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


def os_release_fields(os_release=Path('/etc/os-release')):
    fields = {}
    for line in os_release.read_text().splitlines():
        key, sep, value = line.partition('=')
        if sep:
            fields[key.strip()] = value.strip().strip('"')
    return fields


def is_debian(os_release=Path('/etc/os-release')):
    return os_release_fields(os_release).get('ID') == 'debian'


def debian_codename(os_release=Path('/etc/os-release')):
    """The running Debian release's codename (``trixie``); other distributions are refused."""
    fields = os_release_fields(os_release)
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

    def __init__(self, *, source, env=None, run=subprocess.run, os_release=Path('/etc/os-release'),
                 euid=None, which=shutil.which, present=None, fetch=fetch_url, viewer_packages=VIEWER_PACKAGES):
        self.source = Path(source)
        self.env = dict(os.environ if env is None else env)
        self.run = run
        self.fetch = fetch
        self.viewer_packages = viewer_packages
        self.os_release = Path(os_release)
        self.euid = os.geteuid() if euid is None else euid
        self.which = which
        self.present = (lambda path: Path(path).is_file()) if present is None else present
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

    def install_viewer(self):
        """IronRDP's browser client in the prefix, for a Wayland desktop's RDP server (VIEWER_PACKAGES)."""
        fetch_viewer(self.prefix / VIEWER_DIRECTORY, self.viewer_packages, self.fetch)

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

    def install_packages(self):
        """The Debian packages, through APT, on a Debian host where this runs as root."""
        probe = self.run(['dpkg-query', '-W', '-f=${db:Status-Status}\n', *PACKAGES], capture_output=True, text=True)
        if probe.returncode or probe.stdout.splitlines() != ['installed'] * len(PACKAGES):
            options = self.apt_options()
            self.checked(['apt-get', 'update', *options])
            self.checked(['apt-get', 'install', '-y', '--no-install-recommends', *options, *PACKAGES])

    def check_programs(self):
        """Without APT (not root, or not Debian), what the session needs must be there already.

        A customer's own Linux computer runs the agent unprivileged, and
        Ubuntu's packages are not Debian's (its Chromium is a snap): the
        hook installs nothing there and says what is missing instead, by
        Debian package name. Missing optional programs cost a feature each,
        not the install.
        """
        missing = {program: package for program, package in REQUIRED_PROGRAMS.items() if not self.which(program)}
        if not any(self.which(name) for name in X_SERVERS):
            missing['Xtigervnc or X0tigervnc'] = ' or '.join(dict.fromkeys(X_SERVERS.values()))
        if not self.present(NOVNC):
            missing['noVNC'] = 'novnc'
        if self.run(['/usr/bin/python3', '-c', 'import aiohttp, Cryptodome'], capture_output=True, text=True).returncode:
            missing['python3 aiohttp and pycryptodome'] = 'python3-aiohttp python3-pycryptodome'
        if missing:
            raise RuntimeError('APT is not run here (not root on Debian); install these first: '
                               + ' '.join(dict.fromkeys(missing.values())) + ' (missing: ' + ', '.join(missing) + ')')
        absent = [package for program, package in OPTIONAL_PROGRAMS.items() if not self.which(program)]
        if absent:
            print('mintbot-desktop: not installed, each costs a feature (the own desktop, its icon, the browser, '
                  'window/clipboard/screenshot helpers): ' + ' '.join(absent), file=sys.stderr)

    def install(self):
        if not self.env.get('AXP_HOST') and self.prefix.exists() and any(self.prefix.iterdir()):
            self.validate_record()
        if self.euid == 0 and is_debian(self.os_release):
            self.install_packages()
        else:
            self.check_programs()
        skill = self.hermes / 'skills' / 'mintbot-desktop' / 'SKILL.md'
        if skill.exists() and OWNER not in skill.read_text():
            raise RuntimeError('Refusing to overwrite an unrelated Hermes skill')
        prepare(Config(state=self.state))
        # Record ownership before populating the prefix so a failed download is retryable.
        record = self.state / 'install.json'
        record.write_text(json.dumps({'hermes_home': str(self.hermes), 'prefix': str(self.prefix)}))
        record.chmod(0o600)
        self.install_browser_driver()
        self.install_viewer()
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

    def health(self, wait=None, sleep=time.sleep, clock=time.monotonic):
        """Exit 0 once the session's gateway answers; the last failure's reason otherwise.

        The host runs this hook the moment it has restarted the service
        (right after an update, on a panel check), and the gateway listens
        only once the X server and the window manager are up, a second or
        two later. A session that is still coming up is not unhealthy, so
        the probe is repeated for `wait` seconds (well inside the host's
        own hook timeout) before the answer is no.
        """
        wait = HEALTH_WAIT_S if wait is None else wait
        settings = json.loads((self.state / 'session.json').read_text())
        token = (self.state / 'token').read_text()
        request = urllib.request.Request(f'http://127.0.0.1:{settings["web_port"]}/health', headers={'Authorization': 'Bearer ' + token})
        # Never send loopback health credentials via inherited AXP proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = clock() + wait
        while True:
            try:
                with opener.open(request, timeout=5) as response:
                    if response.status != 200 or json.load(response).get('status') != 'ok':
                        raise RuntimeError(f'the desktop gateway answered {response.status} on port {settings["web_port"]}, not ok')
                return
            except (OSError, ValueError, RuntimeError) as error:
                if clock() >= deadline:
                    raise RuntimeError(f'the desktop gateway on port {settings["web_port"]} did not answer within {wait:g}s: {error}') from error
            sleep(HEALTH_RETRY_S)


def main(argv=None):
    hook = (sys.argv if argv is None else argv)[1]
    # A failed health hook's reason is the panel's "detail" for the red dot,
    # and the host reads that from stdout; the other hooks' failures are
    # relayed as errors, from either stream.
    report = sys.stdout if hook == 'health' else sys.stderr
    try:
        instance = Lifecycle(source=Path(__file__).resolve().parents[1])
        getattr(instance, 'install' if hook == 'upgrade' else hook)()
    except subprocess.CalledProcessError as error:
        # A bare traceback hides the failed command's own message; the host
        # shows the user only the end of this output.
        print(f'mintbot-desktop: {failure_summary(error)}', file=report)
        sys.exit(1)
    except (OSError, ValueError, RuntimeError) as error:
        print(f'mintbot-desktop: {error}', file=report)
        sys.exit(1)


if __name__ == '__main__':
    main()
