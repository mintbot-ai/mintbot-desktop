"""Publishing on two channels: `release.sh` on a tree it is handed, with the key
it is named and behind the promote gate; `release-dev.sh` end to end; the dev
key directory; the workflow that publishes every push to main.

The scripts run for real against a throwaway git clone of the runtime tree,
with fake `axp`, `gh`, `git fetch` and promote gate on PATH that only log: no
test touches the signing key, GitHub, Beaver or a remote.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
RELEASE = REPO / "scripts" / "release.sh"
RELEASE_DEV = REPO / "scripts" / "release-dev.sh"
STABLE_KEYDIR = REPO / "agent-extension-keys.json"
DEV_KEYDIR = REPO / "agent-extension-keys.dev.json"
DEV_KEY_ID = "mintbot.ai-mintbot-desktop-dev-2026"
WORKFLOW = REPO / ".github" / "workflows" / "dev-build.yml"
GIT_IDENTITY = ["-c", "user.name=test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false"]
PROMOTE_FROM = "9.9.9-dev.20260101000000"

_FAKE_AXP = """#!/bin/bash
echo "axp $*" >> "${FAKE_LOG}"
"""

# GitHub as the scripts see it: the latest release, the release list (which
# grows by every created release) and a log of every call.
_FAKE_GH = """#!/bin/bash
echo "gh $*" >> "${FAKE_LOG}"
case "$1 $2" in
  "release view") [ -n "${FAKE_LATEST:-}" ] || exit 1; echo "${FAKE_LATEST}" ;;
  "release list") cat "${FAKE_RELEASES}" ;;
  "release create")
    echo "$3" >> "${FAKE_RELEASES}"
    for asset in "$@"; do
      case "${asset}" in *agent-extension.json) cp "${asset}" "${FAKE_RELEASES}.manifest.json" ;; esac
    done ;;
esac
"""

# The throwaway clone has no remote: fetch is the one git call that needs one.
_FAKE_GIT = """#!/bin/bash
[ "$3" = "fetch" ] && exit 0
exec __REAL__ "$@"
"""

# The mintbot repository's promote gate, as release.sh calls it: logs its
# arguments and answers what FAKE_GATE_EXIT says.
_FAKE_GATE = """import os, sys
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("promote-check " + " ".join(sys.argv[1:]) + "\\n")
sys.exit(int(os.environ.get("FAKE_GATE_EXIT", "0")))
"""


def _run(script: Path, *args: str, env: dict, cwd: Path = REPO) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(script), *args], env=env, cwd=str(cwd), capture_output=True, text=True, timeout=120)


def _log(env: dict) -> str:
    path = Path(env["FAKE_LOG"])
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _env(tmp_path: Path) -> dict:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    return {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path), "FAKE_LOG": str(tmp_path / "fake.log")}


@pytest.fixture
def publisher(tmp_path: Path) -> dict:
    env = _env(tmp_path)
    bin_dir = tmp_path / "bin"
    for name, script in (("axp", _FAKE_AXP), ("gh", _FAKE_GH),
                         ("git", _FAKE_GIT.replace("__REAL__", shutil.which("git") or "git"))):
        (bin_dir / name).write_text(script, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    gate = tmp_path / "promote_check.py"
    gate.write_text(_FAKE_GATE, encoding="utf-8")
    releases = tmp_path / "releases"
    releases.write_text("v0.1.10-dev.20261020000000\nv0.1.9\nv0.1.10-dev.20261021000000\nv0.1.8\n", encoding="utf-8")
    (tmp_path / "scratch").mkdir()
    (tmp_path / "publisher.key").write_text("not a key\n", encoding="utf-8")
    env.update(AXP_SIGNING_KEY=str(tmp_path / "publisher.key"), AXP_PROMOTE_CHECK=str(gate),
               AXP_PROMOTE_FROM=PROMOTE_FROM, FAKE_LATEST="v0.1.9", FAKE_RELEASES=str(releases),
               TMPDIR=str(tmp_path / "scratch"))
    return env


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """The runtime tree as a fresh git repository whose origin/main is its HEAD."""
    clone = tmp_path / "clone"
    shutil.copytree(REPO, clone, ignore=shutil.ignore_patterns(
        ".git", ".venv", ".test-tmp", "dist", "__pycache__", "tests", "*.pyc", "*.egg-info", ".pytest_cache"))
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "tree"],
                 ["update-ref", "refs/remotes/origin/main", "HEAD"]):
        subprocess.run(["git", "-C", str(clone), *GIT_IDENTITY, *args], check=True, capture_output=True)
    return clone


def _git(clone: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(clone), *args], check=True, capture_output=True, text=True).stdout


def _keys(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["keys"]


def _dev_signing() -> dict:
    dev = next(key for key in _keys(DEV_KEYDIR) if key["key_id"] == DEV_KEY_ID)
    return {"public_key": dev["public_key"], "key_id": DEV_KEY_ID, "next_key": None}


def _manifest(clone: Path) -> dict:
    return json.loads((clone / "agent-extension.json").read_text(encoding="utf-8"))


def _init_py(root: Path) -> str:
    return (root / "mintbot_desktop" / "__init__.py").read_text(encoding="utf-8")


def test_release_scripts_parse_and_are_executable():
    for script in (RELEASE, RELEASE_DEV):
        assert subprocess.run(["bash", "-n", str(script)], capture_output=True).returncode == 0
        text = script.read_text(encoding="utf-8")
        assert text.startswith("#!/usr/bin/env bash\n") and "set -euo pipefail" in text
        assert script.stat().st_mode & 0o111, f"{script.name} is not executable"


def test_release_signs_the_tree_it_is_handed_behind_the_gate(publisher, clone):
    head = _git(clone, "rev-parse", "HEAD").strip()
    result = _run(RELEASE, "9.9.9", env={**publisher, "RELEASE_TREE": str(clone)})
    assert result.returncode == 0, result.stdout + result.stderr
    assert '__version__ = "9.9.9"' in _init_py(clone)
    assert '__version__ = "9.9.9"' not in _init_py(REPO)
    assert (clone / "dist" / "mintbot-desktop-9.9.9.tar.gz").is_file()
    assert not (REPO / "dist" / "mintbot-desktop-9.9.9.tar.gz").exists()
    log = _log(publisher)
    manifest = clone / "agent-extension.json"
    gate = f"promote-check --package mintbot-desktop --version {PROMOTE_FROM} --commit {head}\n"
    assert log.startswith(gate), "the evidence is checked before anything is built or signed"
    channel = _manifest(clone)["release"]["channel"]
    assert channel != "dev"
    assert (f"axp release {manifest} --version 9.9.9 --artifact {clone}/dist/mintbot-desktop-9.9.9.tar.gz "
            f"--channel {channel} ") in log, "without AXP_CHANNEL the manifest names the channel"
    assert _manifest(clone)["signing"]["key_id"] == "mintbot.ai-mintbot-desktop-2026", "without AXP_SIGNING_KEY_ID the manifest's key signs"
    assert f"axp validate {manifest}" in log
    assert f"axp verify {manifest} --keydir {clone}/agent-extension-keys.json" in log
    assert "next:" not in result.stdout, "the commit and tag hints are for a release cut in the checkout"


def test_release_signs_with_the_key_directory_entry_it_is_named(publisher, clone):
    env = {**publisher, "RELEASE_TREE": str(clone), "AXP_SIGNING_KEY_ID": DEV_KEY_ID,
           "AXP_KEYDIR": str(clone / "agent-extension-keys.dev.json")}
    result = _run(RELEASE, "9.9.9", env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _manifest(clone)["signing"] == _dev_signing()
    assert (f"axp verify {clone}/agent-extension.json --keydir {clone}/agent-extension-keys.dev.json"
            in _log(publisher)), "the named key is checked against the directory it came from"


def test_release_refuses_a_key_the_directory_does_not_list(publisher, clone):
    env = {**publisher, "RELEASE_TREE": str(clone), "AXP_SIGNING_KEY_ID": DEV_KEY_ID}
    result = _run(RELEASE, "9.9.9", env=env)
    assert result.returncode == 1
    assert f"{DEV_KEY_ID} is not listed in {clone}/agent-extension-keys.json" in result.stderr
    assert '__version__ = "9.9.9"' not in _init_py(clone)
    assert "axp " not in _log(publisher), "nothing is built or signed"


def test_release_refuses_to_promote_without_naming_the_dev_build(publisher, clone):
    env = {k: v for k, v in publisher.items() if k != "AXP_PROMOTE_FROM"}
    result = _run(RELEASE, "9.9.9", env={**env, "RELEASE_TREE": str(clone)})
    assert result.returncode == 1 and "AXP_PROMOTE_FROM" in result.stderr
    assert _log(publisher) == "", "nothing is checked, built or signed"


def test_release_refuses_a_dev_build_of_another_version(publisher, clone):
    env = {**publisher, "RELEASE_TREE": str(clone), "AXP_PROMOTE_FROM": "9.9.8-dev.20260101000000"}
    result = _run(RELEASE, "9.9.9", env=env)
    assert result.returncode == 1 and "9.9.8-dev.20260101000000 is not a dev build of 9.9.9" in result.stderr
    assert _log(publisher) == ""


def test_release_refuses_a_dirty_checkout(publisher, clone):
    (clone / "scratch.txt").write_text("not committed\n", encoding="utf-8")
    result = _run(RELEASE, "9.9.9", env={**publisher, "RELEASE_TREE": str(clone)})
    assert result.returncode == 1 and "uncommitted changes" in result.stderr
    assert _log(publisher) == ""


def test_release_refuses_when_the_gate_refuses(publisher, clone):
    result = _run(RELEASE, "9.9.9", env={**publisher, "RELEASE_TREE": str(clone), "FAKE_GATE_EXIT": "1"})
    assert result.returncode == 1
    log = _log(publisher)
    assert log.startswith("promote-check ") and "axp " not in log, "a refused gate builds and signs nothing"
    assert '__version__ = "9.9.9"' not in _init_py(clone)


def test_release_refuses_without_the_gate_script(publisher, clone, tmp_path):
    env = {**publisher, "RELEASE_TREE": str(clone), "AXP_PROMOTE_CHECK": str(tmp_path / "absent.py")}
    result = _run(RELEASE, "9.9.9", env=env)
    assert result.returncode == 1 and "the promote gate" in result.stderr and "is missing" in result.stderr
    assert _log(publisher) == ""


def test_release_fails_closed_without_axp_cli(publisher, clone, tmp_path):
    (tmp_path / "bin" / "axp").unlink()
    result = _run(RELEASE, "9.9.9", env={**publisher, "RELEASE_TREE": str(clone)})
    assert result.returncode == 1 and "axp CLI is required" in result.stderr
    assert _log(publisher) == ""


def test_the_dev_key_directory_lists_every_release_key_and_the_dev_keys():
    """The directory mintbot.dev serves to DEV agents, the same bytes as the copy
    in the mintbot repository (and in mintbot-mcp): every mintbot.ai release
    key, then the dev-build keys mintbot.ai never lists. This package's own
    agent-extension-keys.json is the scoped slice a release is checked against."""
    stable, dev = _keys(STABLE_KEYDIR), _keys(DEV_KEYDIR)
    assert json.loads(DEV_KEYDIR.read_text(encoding="utf-8"))["publisher"] == "mintbot.ai"
    for key in stable:
        assert key in dev, f"{key['key_id']} is missing from the dev directory"
    added = [key for key in dev if "-dev-" in key["key_id"]]
    assert added == dev[-2:], "dev-build keys come after every release key"
    assert [(key["key_id"], key["extensions"]) for key in added] == [
        ("mintbot.ai-mintbot-mcp-dev-2026", ["mintbot-mcp"]), (DEV_KEY_ID, ["mintbot-desktop"])]
    release_keys = {key["public_key"] for key in dev if "-dev-" not in key["key_id"]}
    assert not {key["public_key"] for key in added} & release_keys


def test_the_workflow_publishes_main_with_the_dev_key_alone():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "push:\n    branches: [main]" in text and "release:\n    types: [published]" in text
    assert "!github.event.release.prerelease" in text, "its own dev builds start no further build"
    assert re.findall(r"secrets\.\w+", text) == ["secrets.AXP_DEV_SIGNING_KEY", "secrets.BEAVER_DEV_BUILD_TOKEN"], \
        "the release key is never a secret; the dev-build token opens one DEV route"
    assert re.search(r"agent-extension@[0-9a-f]{40}\"", text), "the axp CLI is pinned to a commit"
    assert "permissions:\n  contents: write\n" in text and "scripts/release-dev.sh" in text


def test_the_workflow_reports_the_published_build_to_dev_beaver():
    """The build's completion queues DEV's AXP package tests: the last step
    posts the version release-dev.sh reported to CatOffice's dev-build route,
    with the token that opens exactly that route, and fails when refused."""
    text = WORKFLOW.read_text(encoding="utf-8")
    publish, report = text.split("- name: Queue the DEV Beaver package tests")
    assert "id: publish" in publish
    assert "steps.publish.outputs.version" in report and "steps.publish.outputs.commit" in report
    assert "https://cat.mintbot.dev/api/beaver/dev-build" in report
    assert "--arg package mintbot-desktop " in report
    assert "X-Dashboard-Token: ${DEV_BUILD_TOKEN}" in report and "secrets.BEAVER_DEV_BUILD_TOKEN" in report
    assert "--fail-with-body" in report, "a refused report fails the workflow"
    assert "- name:" not in report.split("run: |")[1], "the report is the last step"


def test_release_dev_tells_the_workflow_what_it_published(tmp_path, publisher, clone):
    output = tmp_path / "github-output"
    env = {k: v for k, v in publisher.items() if k != "AXP_PROMOTE_FROM"}
    result = _run(clone / "scripts" / "release-dev.sh", env={**env, "GITHUB_OUTPUT": str(output)}, cwd=clone)
    assert result.returncode == 0, result.stdout + result.stderr
    version = re.search(r"^published v(\S+): ", result.stdout, re.M).group(1)
    commit = _git(clone, "rev-parse", "HEAD").strip()
    assert output.read_text(encoding="utf-8") == f"version={version}\ncommit={commit}\n"


def test_release_dev_publishes_a_dev_build_and_keeps_the_newest_two(tmp_path, publisher, clone):
    commit = _git(clone, "rev-parse", "HEAD").strip()
    env = {k: v for k, v in publisher.items() if k != "AXP_PROMOTE_FROM"}
    result = _run(clone / "scripts" / "release-dev.sh", env=env, cwd=clone)
    assert result.returncode == 0, result.stdout + result.stderr
    log = _log(publisher)

    created = re.search(r"^gh release create v(0\.1\.10-dev\.\d{14}) --repo mintbot-ai/mintbot-desktop "
                        r"--target (\w+) --prerelease ", log, re.M)
    assert created, log
    version, target = created.groups()
    assert target == commit, "the tag goes on the commit that was built"
    assert re.search(rf"^axp release \S+/agent-extension\.json --version {re.escape(version)} .* --channel dev ", log, re.M)
    assert re.search(r"^axp verify \S+/agent-extension\.json --keydir \S+/agent-extension-keys\.dev\.json$", log, re.M)
    assert "promote-check" not in log, "a dev build is what the gate guards, never gated itself"
    uploaded = json.loads((tmp_path / "releases.manifest.json").read_text(encoding="utf-8"))
    assert uploaded["signing"] == _dev_signing(), "a dev build carries the dev key, never the release key"
    assert f"published v{version}: " in result.stdout

    deleted = re.findall(r"^gh release delete (\S+) ", log, re.M)
    assert deleted == ["v0.1.10-dev.20261020000000"], "only dev builds beyond the newest two go, never a release"
    assert _git(clone, "status", "--porcelain") == "" and not (clone / "dist").exists(), "the checkout stays untouched"
    assert not any((tmp_path / "scratch").iterdir()), "the scratch export is removed"


@pytest.mark.parametrize("latest", ["", "v0.1.10-dev.20261021000000"], ids=["none", "pre-release"])
def test_release_dev_refuses_without_a_stable_latest_release(publisher, clone, latest):
    env = {**publisher, "FAKE_LATEST": latest}
    result = _run(clone / "scripts" / "release-dev.sh", env=env, cwd=clone)
    assert result.returncode == 1 and "has no stable latest release" in result.stderr
    assert "axp release" not in _log(env) and "gh release create" not in _log(env)


def test_release_dev_refuses_a_commit_that_is_not_on_main(publisher, clone):
    _git(clone, *GIT_IDENTITY, "commit", "-q", "--allow-empty", "-m", "local only")
    result = _run(clone / "scripts" / "release-dev.sh", "HEAD", env=publisher, cwd=clone)
    assert result.returncode == 1 and "is not on origin/main" in result.stderr
    assert "gh release" not in _log(publisher)


def test_release_dev_refuses_without_signing_key_or_axp_cli(tmp_path):
    env = _env(tmp_path)
    result = _run(RELEASE_DEV, env=env)
    assert result.returncode == 1 and "AXP_SIGNING_KEY" in result.stderr
    result = _run(RELEASE_DEV, env={**env, "AXP_SIGNING_KEY": str(tmp_path / "absent.key")})
    assert result.returncode == 1 and "axp is required" in result.stderr
