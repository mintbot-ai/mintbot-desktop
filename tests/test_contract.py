import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_import_works_with_debian_cryptodome_namespace():
    import subprocess
    import sys
    result = subprocess.run([sys.executable, '-c', """
import builtins
original = builtins.__import__
def debian_import(name, *args, **kwargs):
    if name == 'Crypto' or name.startswith('Crypto.'):
        raise ModuleNotFoundError('Debian provides Cryptodome, not Crypto')
    return original(name, *args, **kwargs)
builtins.__import__ = debian_import
from mintbot_desktop.app import create_app
"""], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_manifest_declares_private_panel_and_shared_session():
    path = ROOT / 'agent-extension.json'
    assert path.exists(), 'AXP manifest missing'
    m = json.loads(path.read_text())
    assert m['spec_version'] == '0.5'
    assert m['identity']['publisher'] == 'mintbot.ai'
    assert m['identity']['name'] == 'mintbot-desktop'
    assert m['identity']['display_name'] == 'Mintbot Desktop'
    assert m['provides']['panel_views'] == [dict(name='desktop', title='Agent desktop', icon='monitor', service='desktop-web', path='/desktop/', capabilities=['pin', 'fullscreen'])]
    service = m['provides']['services'][0]
    assert service['endpoint'] == 'http://127.0.0.1:6081'
    assert service['auth'] == {'scheme': 'bearer', 'token_file': 'state:token'}
    assert m['permissions']['root'] is True
    assert set(m['permissions']['network_ingress']) == {'127.0.0.1:6081/tcp', '127.0.0.1:5913/tcp', '127.0.0.1:9223/tcp'}
    target = m['targets'][0]
    assert target['component_map']['services']['command'] == ['bin/mintbot-desktop', 'serve']
    assert set(target['lifecycle']) == {'install', 'upgrade', 'health', 'uninstall', 'enable', 'disable'}
