"""
Tests for .devcontainer/utils/json.sh, the pure-bash JSONC-to-JSON
converter seedHostConfig.sh depends on to read devcontainer.json's "mounts"
array on the host -- before anything else (jq, python, @devcontainers/cli)
could ever be installed there, since this runs via initializeCommand,
strictly before the devcontainer exists (see seedHostConfig.sh).

Invoked via subprocess exactly as seedHostConfig.sh invokes it, so these
exercise the actual script, not a reimplementation of its logic.
"""

import json
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
_SCRIPT = _ROOT / ".devcontainer" / "utils" / "json.sh"
_DEVCONTAINER_JSON = _ROOT / ".devcontainer" / "devcontainer.json"


def _convert(tmp_path: Path, jsonc_text: str) -> str:
    src = tmp_path / "input.jsonc"
    src.write_text(jsonc_text)
    result = subprocess.run(
        ["bash", str(_SCRIPT), str(src)],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def test_real_devcontainer_json_parses_as_strict_json(tmp_path):
    real = _DEVCONTAINER_JSON.read_text()
    config = json.loads(_convert(tmp_path, real))
    assert isinstance(config["mounts"], list)
    assert len(config["mounts"]) > 0


def test_real_devcontainer_json_excludes_disabled_history_mount(tmp_path):
    # devcontainer.json keeps a commented-out example mount for VS Code
    # Local History (target=/home/vscode/.local-history-sync). It must not
    # show up as if it were a live mount -- that's exactly the class of bug
    # a naive `grep -v '^\s*//'` line-comment strip couldn't be trusted to
    # avoid (see seedHostConfig.sh for the full story).
    real = _DEVCONTAINER_JSON.read_text()
    config = json.loads(_convert(tmp_path, real))
    matches = [m for m in config["mounts"] if ".local-history-sync" in m]
    assert matches == []


def _declared_dir_mount_relpaths(config: dict) -> set[str]:
    # Mirrors seedHostConfig.sh's is_declared_dir_mount(): a directory
    # mount is marked by a trailing "/" on its target, not merely by
    # appearing in the array at all -- a target without one (e.g. a
    # hypothetical bare-file bind mount) must NOT be treated as a
    # directory.
    prefix = "/home/vscode/"
    relpaths = set()
    for m in config["mounts"]:
        parts = dict(kv.split("=", 1) for kv in m.split(",") if "=" in kv)
        target = parts.get("target", "")
        is_bind = parts.get("type") == "bind"
        if is_bind and target.startswith(prefix) and target.endswith("/"):
            relpaths.add(target[len(prefix) : -1])
    return relpaths


def test_real_devcontainer_json_declares_every_config_files_dir_entry(tmp_path):
    # Cross-check against config-files.txt: every entry that isn't
    # .claude.json (the one deliberate bare-file exception) must have its
    # own directory bind mount declared, or seedHostConfig.sh's
    # is_declared_dir_mount() would silently mis-seed it as a file the
    # first time that tool is used on a fresh host.
    real = _DEVCONTAINER_JSON.read_text()
    config = json.loads(_convert(tmp_path, real))
    mount_targets = _declared_dir_mount_relpaths(config)

    config_files = _ROOT / ".devcontainer" / "config-files.txt"
    relpaths = [
        line.strip()
        for line in config_files.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert relpaths, "config-files.txt should list at least one path"
    for relpath in relpaths:
        if relpath == ".claude.json":
            continue
        assert relpath in mount_targets, (
            f"{relpath} is in config-files.txt but has no matching directory "
            "bind mount (with a trailing slash on its target) in "
            "devcontainer.json's mounts array"
        )


def test_file_target_bind_mount_without_trailing_slash_is_not_a_directory(tmp_path):
    # The regression this whole trailing-slash convention exists to close:
    # without an explicit marker, any type=bind target under /home/vscode/
    # would be indistinguishable from a real directory mount. A target with
    # no trailing slash (however unlikely today) must never be treated as
    # one.
    jsonc = (
        '{"mounts": ['
        '"source=/host/real-dir,target=/home/vscode/.claude/,type=bind",'
        '"source=/host/some-file,target=/home/vscode/.claude.json,type=bind"'
        "]}"
    )
    config = json.loads(_convert(tmp_path, jsonc))
    relpaths = _declared_dir_mount_relpaths(config)
    assert relpaths == {".claude"}


@pytest.mark.parametrize(
    ("jsonc", "expected"),
    [
        pytest.param('{"a": 1}', {"a": 1}, id="plain_json_passthrough"),
        pytest.param(
            '{\n  // comment\n  "a": 1\n}', {"a": 1}, id="line_comment_stripped"
        ),
        pytest.param('{"a": 1,}', {"a": 1}, id="trailing_comma_object"),
        pytest.param('{"a": [1, 2,]}', {"a": [1, 2]}, id="trailing_comma_array"),
        pytest.param(
            '{"a": "has // not a comment"}',
            {"a": "has // not a comment"},
            id="slashes_inside_string_preserved",
        ),
        pytest.param(
            '{"a": "trailing, comma, inside a string"}',
            {"a": "trailing, comma, inside a string"},
            id="comma_inside_string_preserved",
        ),
        pytest.param(
            '{"a": "bracket ] and brace } inside a string"}',
            {"a": "bracket ] and brace } inside a string"},
            id="brackets_inside_string_preserved",
        ),
        pytest.param(
            '{/* block\ncomment spanning\nlines */ "a": 1}',
            {"a": 1},
            id="multiline_block_comment_stripped",
        ),
        pytest.param(
            '{"a": "fake /* block */ inside a string"}',
            {"a": "fake /* block */ inside a string"},
            id="fake_block_comment_inside_string_preserved",
        ),
        pytest.param(
            r'{"a": "he said \"hi\""}',
            {"a": 'he said "hi"'},
            id="escaped_quote",
        ),
        pytest.param(
            r'{"a": "back\\slash"}',
            {"a": "back\\slash"},
            id="escaped_backslash",
        ),
        pytest.param(
            r'{"a": "esc\"aped \\\" quote and a /* fake block */ inside a string"}',
            {"a": 'esc"aped \\" quote and a /* fake block */ inside a string'},
            id="combined_escapes_and_fake_block_comment",
        ),
        pytest.param(
            '{"a": "url http://example.com/path//double//slash"}',
            {"a": "url http://example.com/path//double//slash"},
            id="url_like_double_slashes_preserved",
        ),
    ],
)
def test_conversion(tmp_path, jsonc, expected):
    assert json.loads(_convert(tmp_path, jsonc)) == expected
