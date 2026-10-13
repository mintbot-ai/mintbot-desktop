import hashlib
import json
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]


def test_artifact_is_deterministic_and_contains_only_runtime(tmp_path):
    script = ROOT / 'scripts' / 'build-artifact.sh'
    assert script.exists(), 'artifact builder missing'
    for directory in ['a', 'b']:
        result = subprocess.run(['bash', str(script), '0.1.0', str(tmp_path/directory)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    left = tmp_path/'a'/'mintbot-desktop-0.1.0.tar.gz'
    right = tmp_path/'b'/'mintbot-desktop-0.1.0.tar.gz'
    assert left.read_bytes() == right.read_bytes()
    with tarfile.open(left) as tar:
        names = tar.getnames()
        assert 'mintbot_desktop/app.py' in names
        assert 'skills/mintbot-desktop/SKILL.md' in names
        for member in tar.getmembers():
            assert member.uid == member.gid == member.mtime == 0
        assert not any('.git' in n or '__pycache__' in n or n.endswith('.key') or n.startswith('tests/') or n == 'agent-extension.json' for n in names)


def test_release_fails_closed_without_private_key(tmp_path):
    script = ROOT/'scripts'/'release.sh'
    assert script.exists(), 'signed release script missing'
    result = subprocess.run(['bash', str(script), '0.1.0'], env={'PATH':'/usr/bin:/bin'}, capture_output=True, text=True)
    assert result.returncode != 0
    assert 'AXP_SIGNING_KEY' in result.stderr


def test_checked_in_signature_and_artifact_digest(tmp_path):
    import shutil
    manifest = json.loads((ROOT/'agent-extension.json').read_text())
    if not manifest.get('signature'):
        raise AssertionError('Release manifest must be signed before committing')
    version = manifest['identity']['version']
    result = subprocess.run(['bash', str(ROOT/'scripts/build-artifact.sh'), version, str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    digest = hashlib.sha256((tmp_path/f'mintbot-desktop-{version}.tar.gz').read_bytes()).hexdigest()
    assert digest == manifest['release']['artifact']['sha256']
    assert digest == manifest['targets'][0]['delivery']['sha256']
    if shutil.which('axp'):
        result = subprocess.run(['axp', 'verify', str(ROOT/'agent-extension.json'), '--keydir', str(ROOT/'agent-extension-keys.json')], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


def test_signing_directory_scopes_desktop_key():
    path = ROOT/'agent-extension-keys.json'
    assert path.exists(), 'public signing directory missing'
    keys = json.loads(path.read_text())
    assert keys['publisher'] == 'mintbot.ai'
    manifest = json.loads((ROOT/'agent-extension.json').read_text())
    entry = next(k for k in keys['keys'] if k['key_id'] == manifest['signing']['key_id'])
    assert entry['extensions'] == ['mintbot-desktop']
    assert entry['public_key'] == manifest['signing']['public_key']
