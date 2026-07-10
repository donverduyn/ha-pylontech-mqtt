"""
Tests for .devcontainer/lib/cli.sh, the generic engine behind the
claude/opencode/kilo shell function shims.

Three layers, tested separately:

- _devcontainer_args_has_flag: "does this arg list already contain an
  override flag" -- pure arg-scanning, no CLI knowledge. See
  test_args_has_flag_*.

- _devcontainer_cli_shim_run: "run a binary, injecting a default flag
  after a matched trigger unless the caller already overrode it, plus any
  unconditional prefix flags" -- the actual generic mechanic, fully
  decoupled from claude/opencode/kilo. Exercised directly against
  synthetic CLI names and flags in test_shim_run_*, so this stays provably
  reusable rather than just "happens to work for the three CLIs it was
  extracted from."

- _devcontainer_define_cli_shim: generates a same-named shell function
  from that mechanic via eval (see that function's own comment for why).
  The real per-CLI wiring -- which binary, which trigger, which flags --
  is deliberately not in this lib file at all; it's plain data supplied at
  postCreate.sh's own call site (its "Devcontainer AI CLI home-config
  defaults" step). test_claude_*/test_opencode_*/test_kilo_* extract
  those exact calls out of postCreate.sh and run them for real, so this
  still exercises the live wiring -- not a hand-copied duplicate that
  could drift from what actually ships.
"""

import stat
import subprocess
from pathlib import Path

_ROOT = Path(__file__).parent.parent
_LIB = _ROOT / ".devcontainer" / "lib" / "cli.sh"
_POSTCREATE = _ROOT / ".devcontainer" / "postCreate.sh"

_REAL_CLAUDE = "/home/vscode/.local/bin/claude"
_REAL_OPENCODE = "/usr/local/bin/opencode"
_REAL_KILO = "/home/vscode/.local/share/pnpm/bin/kilo"


def _extract_shim_calls() -> str:
    """Pull the _devcontainer_define_cli_shim lines straight out of
    postCreate.sh -- the only CLI-specific data that exists anywhere (see
    that script and lib/cli.sh)."""
    lines = [
        line
        for line in _POSTCREATE.read_text().splitlines()
        if line.startswith("_devcontainer_define_cli_shim ")
    ]
    assert lines, (
        "fixture assumption broken: postCreate.sh no longer calls "
        "_devcontainer_define_cli_shim the way this test expects"
    )
    return "\n".join(lines)


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

    combined = _LIB.read_text() + "\n" + _extract_shim_calls()
    patched = (
        combined.replace(_REAL_CLAUDE, str(fake_claude))
        .replace(_REAL_OPENCODE, str(fake_opencode))
        .replace(_REAL_KILO, str(fake_kilo))
    )
    assert patched != combined, (
        "fixture assumption broken: postCreate.sh's hardcoded real-binary "
        "paths no longer match what this test expects to replace"
    )

    shimmed = tmp_path / "cli.sh"
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


def _has_flag(*args: str) -> bool:
    result = subprocess.run(
        ["bash", "-c", f'. "{_LIB}"; _devcontainer_args_has_flag "$@"', "--", *args],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _shim_run(tmp_path: Path, config: tuple[str, ...], *args: str) -> str:
    fake_bin = tmp_path / "fake-widget"
    _write_fake_bin(fake_bin)
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'. "{_LIB}"; _devcontainer_cli_shim_run "$@"',
            "--",
            str(fake_bin),
            *config,
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_args_has_flag_matches_short_form():
    assert _has_flag("-g", "--global", "install", "-g")


def test_args_has_flag_matches_long_form():
    assert _has_flag("-g", "--global", "install", "--global")


def test_args_has_flag_matches_long_form_with_value():
    assert _has_flag("-s", "--scope", "add", "--scope=local")


def test_args_has_flag_no_match():
    assert not _has_flag("-g", "--global", "install", "foo")


def test_shim_run_injects_default_after_single_word_trigger(tmp_path):
    out = _shim_run(
        tmp_path, ("login", "-v", "--verbose", "--verbose", ""), "login", "bob"
    )
    assert out == "fake-widget called with: login --verbose bob"


def test_shim_run_keeps_explicit_override_over_single_word_trigger(tmp_path):
    out = _shim_run(
        tmp_path, ("login", "-v", "--verbose", "--verbose", ""), "login", "bob", "-v"
    )
    assert out == "fake-widget called with: login bob -v"


def test_shim_run_matches_multi_word_trigger_alternative(tmp_path):
    out = _shim_run(
        tmp_path,
        ("account create|account add", "-f", "--force", "--force", ""),
        "account",
        "add",
        "bob",
    )
    assert out == "fake-widget called with: account add --force bob"


def test_shim_run_applies_unconditional_prefix_regardless_of_trigger(tmp_path):
    out = _shim_run(
        tmp_path, ("login", "-v", "--verbose", "--verbose", "--quiet always"), "status"
    )
    assert out == "fake-widget called with: --quiet always status"


def test_shim_run_no_trigger_match_is_plain_passthrough(tmp_path):
    out = _shim_run(tmp_path, ("login", "-v", "--verbose", "--verbose", ""), "status")
    assert out == "fake-widget called with: status"


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
