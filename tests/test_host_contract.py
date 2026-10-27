"""Read-only integration with a neighboring host checkout; systemd is fake."""
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import types

import pytest


def test_manifest_through_actual_host_runtime_apply(tmp_path, monkeypatch):
    host = Path(os.environ.get('MINTBOT_HOST_REPO', '/root/projects/mintbot'))
    plugins = host/'agent_templates'/'_shared'/'plugins'/'mintbot'
    if not plugins.is_dir():
        pytest.skip('Set MINTBOT_HOST_REPO to test against the AXP host checkout')
    # Load only the actual runtime modules, never the live plugin entry point.
    package = types.ModuleType('desktop_host_contract')
    package.__path__ = [str(plugins)]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    rt = importlib.import_module('desktop_host_contract.axp_runtime')
    monkeypatch.setattr(rt.axp_sandbox, 'EXT_STATE_ROOT', tmp_path/'ext')
    monkeypatch.setattr(rt.axp_sandbox, 'available', lambda: True)
    monkeypatch.setattr(rt, 'UNIT_DIR', tmp_path/'units')
    monkeypatch.setattr(rt, 'RUNTIME_LIB_DIR', tmp_path/'lib')
    monkeypatch.setattr(rt.Systemd, 'available', lambda self: True)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path/'hermes'))
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        assert argv[0].endswith('systemctl')
        return subprocess.CompletedProcess(argv, 0, 'loaded', '')

    m = json.loads(Path('agent-extension.json').read_text())
    record = {**m['identity'], 'kind': 'agent-extension', 'ext_id':'mintbot.ai/mintbot-desktop',
              'runtime':'hermes', 'install_prefix':str(tmp_path/'prefix'),
              'provides':m['provides'], 'permissions':m['permissions'],
              'component_map':m['targets'][0]['component_map']}
    result = rt.apply(record, systemd=rt.Systemd(run=run))
    # Egress to any host (`*`): the host runs no proxy for this extension and
    # records the network half as not enforced, rather than a proxy that
    # admits everything. Root already switched the filesystem half off.
    assert result['proxy'] is None, result
    service = result['services'][0]
    assert service['mode'] == 'managed'
    assert service['enforcement'] == 'declared'
    assert any('any host' in note for note in service['notes'])
    assert not any('released' in note for note in service['notes'])
    assert service['unit'] == 'mintbot-ext-mintbot.ai-mintbot-desktop-svc-desktop-web.service'
    unit = (tmp_path/'units'/service['unit']).read_text()
    assert 'IPAddressDeny=' not in unit and 'IPAddressAllow=' not in unit
    assert '_PROXY=' not in unit
    assert 'PrivateTmp=' not in unit and 'ProtectSystem=' not in unit
    assert f'ExecStart={tmp_path}/prefix/bin/mintbot-desktop serve' in unit
    assert any('enable' in call and service['unit'] in call for call in calls)
    # One host-managed service, the gateway: the X server, the window manager,
    # the desktop icons and the browser are its children. The host's restart
    # policy covers the gateway alone; a browser the user closed stays
    # closed (`Once` in the supervisor), nothing of the host brings it back.
    assert len(result['services']) == 1
    assert m['targets'][0]['component_map']['services']['restart'] == 'on-failure'
    assert m['targets'][0]['component_map']['services']['command'] == ['bin/mintbot-desktop', 'serve']
