"""
Tests for .devcontainer/lib/fs.sh's sync_config_in(), the container-side
half of getting a config path into place (the host-side half is
seedHostConfig.sh -- see test_devcontainer_seed_host_config.py).

The regression this guards: sync_config_in must be a pure copy from
$HOME/.agent-sync into the real container path, never inventing content of
its own. seedHostConfig.sh is solely responsible for deciding real-vs-
default content and staging it into .agent-sync; if nothing was staged
there, sync_config_in must do nothing, not fabricate a placeholder itself.

Sources the real lib file directly via a tiny bash wrapper rather than
running postCreate.sh as a whole -- that script's top-level body triggers
real installs/downloads/sudo calls unconditionally the moment it's
invoked, which has no place in a unit test. mountpoint -q naturally
reports "not mounted" for a plain tmp_path directory with no real bind
mount, so the "not mounted" branch is exercised without needing an actual
mount (which would require root/privileges this test has no business
using).
"""

import os
import subprocess
from pathlib import Path

_ROOT = Path(__file__).parent.parent
_LIB = _ROOT / ".devcontainer" / "lib" / "fs.sh"


def _sync_config_in(fake_home: Path, relpath: str) -> None:
    subprocess.run(
        ["bash", "-c", f'. "{_LIB}"; sync_config_in "$1"', "--", relpath],
        env={**os.environ, "HOME": str(fake_home)},
        capture_output=True,
        text=True,
        check=True,
    )


def test_copies_staged_file_into_target(tmp_path):
    fake_home = tmp_path
    staged = fake_home / ".agent-sync" / ".claude.json"
    staged.parent.mkdir(parents=True)
    staged.write_text('{"real": "content"}')

    _sync_config_in(fake_home, ".claude.json")

    target = fake_home / ".claude.json"
    assert target.is_file()
    assert target.read_text() == '{"real": "content"}'


def test_copies_staged_directory_into_target(tmp_path):
    fake_home = tmp_path
    staged = fake_home / ".agent-sync" / ".config" / "gh"
    staged.mkdir(parents=True)
    (staged / "hosts.yml").write_text("real login data")

    _sync_config_in(fake_home, ".config/gh")

    target = fake_home / ".config" / "gh"
    assert target.is_dir()
    assert (target / "hosts.yml").read_text() == "real login data"


def test_staged_content_overwrites_existing_target(tmp_path):
    fake_home = tmp_path
    target = fake_home / ".claude.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"stale": "image-baked default"}')

    staged = fake_home / ".agent-sync" / ".claude.json"
    staged.parent.mkdir(parents=True)
    staged.write_text('{"real": "synced content"}')

    _sync_config_in(fake_home, ".claude.json")

    assert target.read_text() == '{"real": "synced content"}'


def test_does_nothing_when_nothing_staged(tmp_path):
    fake_home = tmp_path
    target = fake_home / ".claude.json"

    _sync_config_in(fake_home, ".claude.json")

    assert not target.exists(), (
        "sync_config_in must never invent content when nothing was staged "
        "-- that's seedHostConfig.sh's job, not this function's"
    )


def test_does_nothing_when_nothing_staged_and_target_already_has_content(tmp_path):
    fake_home = tmp_path
    target = fake_home / ".claude.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"image-baked": "default"}')

    _sync_config_in(fake_home, ".claude.json")

    assert target.read_text() == '{"image-baked": "default"}', (
        "with nothing staged, sync_config_in must leave whatever's already "
        "at the target alone, not overwrite or fabricate anything"
    )
