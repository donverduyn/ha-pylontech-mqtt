"""
Tests for .devcontainer/seedHostConfig.sh's dir-vs-file seeding decision.

Two regressions this guards:

1. Every relpath in config-files.txt other than .claude.json is meant to
   become a directory bind mount (see devcontainer.json's "mounts").
   Deciding that from whether the host happens to already have something
   at that path breaks the first time a tool (gh, kilo, opencode, ...) is
   used on a fresh host -- nothing exists there yet, so a naive "not a
   directory" check falls through to seeding a bogus `{}` JSON *file*
   where a directory belongs, which the real directory bind mount then
   can't attach to correctly. seedHostConfig.sh instead falls back to
   devcontainer.json's own mount declarations (via lib/json.sh)
   for exactly this case.

2. This script must never create anything on the real host -- only ever
   read from it. A bare-file entry (.claude.json) with nothing on the
   host still gets a default `{}` placeholder, but only inside the
   project-scoped staging mirror ($sync_dir), never at the real host
   path -- postCreate.sh (see test_devcontainer_sync_config_in.py) only
   ever copies from that staging mirror into the container, never
   invents content of its own, so something has to already be staged
   for it to copy.

3. is_declared_dir_mount()'s field parsing must not depend on target=
   sitting immediately before type=bind in a mount spec -- Docker mount
   strings don't guarantee field order, so a spec with an extra field
   wedged between them (e.g. consistency=cached) must still be recognized
   as a directory mount, not silently fall through to the bare-file case.

These tests exercise the real script and the real devcontainer.json/
config-files.txt, not a reimplementation of the logic.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
_DEVCONTAINER_DIR = _ROOT / ".devcontainer"
_SCRIPT = _DEVCONTAINER_DIR / "seedHostConfig.sh"
_CONFIG_FILES = _DEVCONTAINER_DIR / "config-files.txt"


def _relpaths() -> list[str]:
    return [
        line.strip()
        for line in _CONFIG_FILES.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _staged_path(fake_home: Path, project_dir: Path, relpath: str) -> Path:
    # Mirrors seedHostConfig.sh's own sync_dir="$home/.devcontainer-agent-sync$PWD".
    sync_dir = fake_home / ".devcontainer-agent-sync"
    return sync_dir / str(project_dir).lstrip("/") / relpath


def _run_seed(fake_home: Path, project_dir: Path) -> None:
    subprocess.run(
        ["bash", str(_SCRIPT)],
        cwd=project_dir,
        env={**os.environ, "HOME": str(fake_home)},
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def fake_host(tmp_path: Path):
    fake_home = tmp_path / "home"
    project_dir = tmp_path / "proj"
    fake_home.mkdir()
    project_dir.mkdir()
    return fake_home, project_dir


@pytest.mark.parametrize("relpath", _relpaths())
def test_never_used_tool_seeds_correctly(fake_host, relpath):
    fake_home, project_dir = fake_host
    _run_seed(fake_home, project_dir)
    staged = _staged_path(fake_home, project_dir, relpath)

    if relpath == ".claude.json":
        assert staged.is_file(), f"{relpath} should seed as a file"
        assert staged.read_text() == "{}"
    else:
        assert staged.is_dir(), (
            f"{relpath} should seed as an empty directory even though the "
            "host has never had this tool installed -- a file placeholder "
            "here would break its real directory bind mount"
        )
        assert not staged.is_file()


def test_never_used_bare_file_entry_does_not_touch_real_host(fake_host):
    fake_home, project_dir = fake_host
    real_path = fake_home / ".claude.json"
    assert not real_path.exists()

    _run_seed(fake_home, project_dir)

    assert not real_path.exists(), (
        "seedHostConfig.sh must only ever read from the real host, never "
        "write to it -- it must not invent a placeholder at the real "
        "~/.claude.json just because nothing was there to seed from; the "
        "{} placeholder belongs only in the project-scoped staging mirror"
    )


def test_existing_directory_content_is_copied(fake_host):
    fake_home, project_dir = fake_host
    real_dir = fake_home / ".config" / "gh"
    real_dir.mkdir(parents=True)
    (real_dir / "hosts.yml").write_text("real login data")

    _run_seed(fake_home, project_dir)

    staged = _staged_path(fake_home, project_dir, ".config/gh")
    assert staged.is_dir()
    assert (staged / "hosts.yml").read_text() == "real login data"
    assert (staged / ".devcontainer-freshly-seeded").exists()


def test_existing_claude_json_content_is_copied(fake_host):
    fake_home, project_dir = fake_host
    (fake_home / ".claude.json").write_text('{"already": "logged in"}')

    _run_seed(fake_home, project_dir)

    staged = _staged_path(fake_home, project_dir, ".claude.json")
    assert staged.is_file()
    assert staged.read_text() == '{"already": "logged in"}'


def test_already_seeded_path_is_left_alone(fake_host):
    fake_home, project_dir = fake_host
    _run_seed(fake_home, project_dir)  # first run seeds .config/gh as an empty dir

    staged = _staged_path(fake_home, project_dir, ".config/gh")
    (staged / "marker").write_text("from a prior container run")

    _run_seed(fake_home, project_dir)  # second run must not touch it again

    assert (staged / "marker").read_text() == "from a prior container run"


def test_dir_mount_detected_despite_extra_field_between_target_and_type(
    fake_host, tmp_path
):
    fake_home, project_dir = fake_host

    devcontainer_copy = tmp_path / "devcontainer_copy"
    shutil.copytree(_DEVCONTAINER_DIR, devcontainer_copy)

    devcontainer_json = devcontainer_copy / "devcontainer.json"
    original = devcontainer_json.read_text()
    target_field = "target=/home/vscode/.config/gh/,type=bind"
    assert target_field in original, (
        "fixture assumption broken: devcontainer.json's .config/gh mount "
        "spec no longer matches this test's expected format"
    )
    modified = original.replace(
        target_field,
        "target=/home/vscode/.config/gh/,consistency=cached,type=bind",
    )
    assert modified != original
    devcontainer_json.write_text(modified)

    subprocess.run(
        ["bash", str(devcontainer_copy / "seedHostConfig.sh")],
        cwd=project_dir,
        env={**os.environ, "HOME": str(fake_home)},
        capture_output=True,
        text=True,
        check=True,
    )

    staged = _staged_path(fake_home, project_dir, ".config/gh")
    assert staged.is_dir(), (
        "a mount spec with a field (e.g. consistency=cached) between "
        "target= and type=bind must still be recognized as a directory "
        "bind mount, not mis-seeded as a bare file"
    )
    assert not staged.is_file()
