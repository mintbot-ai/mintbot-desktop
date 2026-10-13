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


class Lifecycle:
    unit = 'mintbot-ext-mintbot.ai-mintbot-desktop-svc-desktop-web.service'

    def __init__(self, *, source, env=None, run=subprocess.run):
        self.source = Path(source)
        self.env = dict(os.environ if env is None else env)
        self.run = run
        self.prefix = Path(self.env.get('AXP_PREFIX', '/opt/mintbot-desktop')).resolve()
        self.state = Path(self.env.get('AXP_STATE_DIR', '/var/lib/axp/mintbot.ai/mintbot-desktop')).resolve()
        self.hermes = Path(self.env.get('AXP_HERMES_HOME') or self.env.get('HERMES_HOME') or Path.home() / '.hermes').resolve()
        if len(self.state.parts) < 4 or len(self.prefix.parts) < 3 or self.state == self.prefix:
            raise ValueError('Unsafe install/state paths')

    def checked(self, argv):
        result = self.run(argv, env={**os.environ, **self.env, 'DEBIAN_FRONTEND': 'noninteractive'}, capture_output=True, text=True)
        result.check_returncode()
        return result

    def install(self):
        if os.geteuid() != 0:
            raise PermissionError('System GUI dependencies require root')
        probe = self.run(['dpkg-query', '-W', '-f=${db:Status-Status}\n', *PACKAGES], capture_output=True, text=True)
        if probe.returncode or probe.stdout.splitlines() != ['installed'] * len(PACKAGES):
            self.checked(['apt-get', 'update', '-o', 'DPkg::Lock::Timeout=120'])
            self.checked(['apt-get', 'install', '-y', '--no-install-recommends', '-o', 'DPkg::Lock::Timeout=120', *PACKAGES])
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
        self.checked(['systemctl', 'start', self.unit])

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


def main():
    hook = sys.argv[1]
    instance = Lifecycle(source=Path(__file__).resolve().parents[1])
    getattr(instance, 'install' if hook == 'upgrade' else hook)()


if __name__ == '__main__':
    main()
