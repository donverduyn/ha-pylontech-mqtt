"""
Tests for .devcontainer/lib/agent-cli-shims.sh -- the claude/opencode/kilo
shell function shims sourced from ~/.bashrc by postCreate.sh (see that
script's "Devcontainer AI CLI home-config defaults" step and this file's
own header comment for why the shims live here instead of as heredoc text
in postCreate.sh).

Each real CLI's install path is hardcoded in the source file (their actual
locations inside the container). These tests substitute those paths for
disposable fake binaries in a temp copy of the file, rather than writing to
/usr/local/bin or ~/.local/bin, so they need no container-specific install
and can't clobber a real CLI -- same reasoning as
test_devcontainer_seed_host_config.py's dir-mount test copying
devcontainer.json before editing it.
"""

import stat
import subprocess
from pathlib import Path

_ROOT = Path(__file__).parent.parent
_LIB = _ROOT / ".devcontainer" / "lib" / "agent-cli-shims.sh"

_REAL_CLAUDE = "/home/vscode/.local/bin/claude"
_REAL_OPENCODE = "/usr/local/bin/opencode"
_REAL_KILO = "/home/vscode/.local/share/pnpm/bin/kilo"


def _write_fake_bin(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/bash\necho "$(basename "$0") called with: $*"\n')
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _shimmed_lib(tmp_path: Path) -> Path:
    fake_claude = tmp_path / "fake" / "claude"
    fake_opencode = tmp_path / "fake" / "opencode"
    fake_kilo = tmp_path / "fake" / "kilo"
    for fake_bin in (fake_claude, fake_opencode, fake_kilo):
        _write_fake_bin(fake_bin)

    text = _LIB.read_text()
    patched = (
        text.replace(_REAL_CLAUDE, str(fake_claude))
        .replace(_REAL_OPENCODE, str(fake_opencode))
        .replace(_REAL_KILO, str(fake_kilo))
    )
    assert patched != text, (
        "fixture assumption broken: agent-cli-shims.sh's hardcoded real-"
        "binary paths no longer match what this test expects to replace"
    )

    shimmed = tmp_path / "agent-cli-shims.sh"
    shimmed.write_text(patched)
    return shimmed


def _run(tmp_path: Path, *args: str) -> str:
    lib = _shimmed_lib(tmp_path)
    result = subprocess.run(
        ["bash", "-c", f'. "{lib}"; "$@"', "--", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_claude_mcp_add_json_gets_default_scope(tmp_path):
    out = _run(tmp_path, "claude", "mcp", "add-json", "foo", "bar")
    assert (
        out == "claude called with: --permission-mode auto mcp "
        "add-json --scope user foo bar"
    )


def test_claude_mcp_add_keeps_explicit_scope(tmp_path):
    out = _run(tmp_path, "claude", "mcp", "add", "foo", "--scope", "local")
    assert out == "claude called with: --permission-mode auto mcp add foo --scope local"


def test_claude_non_mcp_command_only_gets_permission_mode(tmp_path):
    out = _run(tmp_path, "claude", "--version")
    assert out == "claude called with: --permission-mode auto --version"


def test_opencode_plugin_install_gets_default_global(tmp_path):
    out = _run(tmp_path, "opencode", "plugin", "install", "foo")
    assert out == "opencode called with: plugin --global install foo"


def test_opencode_plugin_install_keeps_explicit_global(tmp_path):
    out = _run(tmp_path, "opencode", "plugin", "install", "foo", "--global")
    assert out == "opencode called with: plugin install foo --global"


def test_opencode_non_plugin_command_passes_through(tmp_path):
    out = _run(tmp_path, "opencode", "--version")
    assert out == "opencode called with: --version"


def test_kilo_plugin_install_gets_default_global(tmp_path):
    out = _run(tmp_path, "kilo", "plugin", "install", "bar")
    assert out == "kilo called with: plugin --global install bar"


def test_kilo_plugin_install_keeps_explicit_global_short_flag(tmp_path):
    out = _run(tmp_path, "kilo", "plugin", "install", "bar", "-g")
    assert out == "kilo called with: plugin install bar -g"
