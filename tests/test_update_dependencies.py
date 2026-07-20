"""Focused tests for dependency version-policy helpers and generated values."""

import re
from collections import OrderedDict
from pathlib import Path

import pytest

from scripts import update_dependencies

_ROOT = Path(__file__).parent.parent
_MOVED_NPM_FEATURES = (
    "ghcr.io/devcontainers-extra/features/claude-code:2",
    "ghcr.io/devcontainers-extra/features/opencode:1",
    "ghcr.io/devcontainers/features/copilot-cli:1",
)


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("0.144.1", "0.x"),
        ("7.4.5", "7.x"),
        ("11.10.0", "11.x"),
        ("12.0.0-beta.1", "12.x"),
        ("12.0.0+build.2", "12.x"),
    ],
)
def test_npm_major_range_allows_minor_and_patch_releases(version, expected):
    assert update_dependencies.npm_major_range(version) == expected


def test_npm_major_range_rejects_non_concrete_versions():
    with pytest.raises(SystemExit, match="not a valid npm version"):
        update_dependencies.npm_major_range("0.x")


def test_generated_npm_ranges_match_their_devcontainer_consumers():
    values = update_dependencies.read_env(_ROOT / ".devcontainer" / "tool-versions.env")

    assert (
        frozenset(update_dependencies.NPM_VERSION_PINS)
        == update_dependencies.NPM_MAJOR_RANGE_KEYS
    )

    assert {key: values[key] for key in update_dependencies.NPM_MAJOR_RANGE_KEYS} == {
        "CLAUDE_CODE_VERSION": "2.x",
        "CODEX_VERSION": "0.x",
        "COPILOT_CLI_VERSION": "1.x",
        "KILO_VERSION": "7.x",
        "OPENCODE_VERSION": "1.x",
        "PNPM_VERSION": "11.x",
    }

    devcontainer = (_ROOT / ".devcontainer" / "devcontainer.json").read_text()
    assert '"pnpmVersion": "11.x"' in devcontainer

    lock = (_ROOT / ".devcontainer" / "devcontainer-lock.json").read_text()
    for feature in _MOVED_NPM_FEATURES:
        assert feature not in update_dependencies.FEATURE_OPTION_REFS
        assert feature not in devcontainer
        assert feature not in lock

    post_create = (_ROOT / ".devcontainer" / "postCreate.sh").read_text()
    assert '"@anthropic-ai/claude-code@$CLAUDE_CODE_VERSION"' in post_create
    assert '"@openai/codex@$CODEX_VERSION"' in post_create
    assert '"@github/copilot@$COPILOT_CLI_VERSION"' in post_create
    assert '"@kilocode/cli@$KILO_VERSION"' in post_create
    assert '"opencode-ai@$OPENCODE_VERSION"' in post_create


def test_refresh_tool_versions_ranges_every_npm_package(monkeypatch):
    npm_versions = {
        "@anthropic-ai/claude-code": "2.1.205",
        "@openai/codex": "0.200.3",
        "@github/copilot": "1.2.3",
        "@kilocode/cli": "7.9.1",
        "opencode-ai": "1.18.0",
        "pnpm": "11.12.0",
    }
    monkeypatch.setattr(
        update_dependencies, "latest_npm_version", npm_versions.__getitem__
    )
    monkeypatch.setattr(update_dependencies, "GITHUB_RELEASE_PINS", OrderedDict())
    monkeypatch.setattr(update_dependencies, "GITHUB_BINARY_PINS", OrderedDict())

    def _latest_node_version(_major: int) -> str:
        return "22.23.1"

    monkeypatch.setattr(
        update_dependencies, "latest_node_version", _latest_node_version
    )
    monkeypatch.setattr(
        update_dependencies, "latest_docker_cli_version", lambda: "29.6.1"
    )
    monkeypatch.setattr(update_dependencies, "latest_uv_version", lambda: "0.11.28")

    values = update_dependencies.refresh_tool_versions(
        OrderedDict([("NODE_VERSION", "22.23.1")])
    )

    assert values["CLAUDE_CODE_VERSION"] == "2.x"
    assert values["CODEX_VERSION"] == "0.x"
    assert values["COPILOT_CLI_VERSION"] == "1.x"
    assert values["KILO_VERSION"] == "7.x"
    assert values["OPENCODE_VERSION"] == "1.x"
    assert values["PNPM_VERSION"] == "11.x"


def test_update_tests_workflow_min_python_patches_both_matrix_entries(
    tmp_path, monkeypatch
):
    tests_workflow = tmp_path / "tests.yaml"
    tests_workflow.write_text(
        "  pytest:\n"
        "    strategy:\n"
        "      matrix:\n"
        "        include:\n"
        "          - ha-version: min\n"
        "            lockfile: requirements_dev_min.lock.txt\n"
        '            python-version: "3.13"\n'
        "  e2e:\n"
        "    strategy:\n"
        "      matrix:\n"
        "        include:\n"
        "          - ha-version: min\n"
        "            lockfile: requirements_dev_min.lock.txt\n"
        '            python-version: "3.13"\n'
    )
    monkeypatch.setattr(update_dependencies, "TESTS_WORKFLOW", tests_workflow)

    update_dependencies.update_tests_workflow_min_python("3.14")

    assert tests_workflow.read_text().count('python-version: "3.14"') == 2
    assert 'python-version: "3.13"' not in tests_workflow.read_text()


def test_update_tests_workflow_min_python_requires_exactly_two_matches(
    tmp_path, monkeypatch
):
    tests_workflow = tmp_path / "tests.yaml"
    tests_workflow.write_text(
        "  pytest:\n"
        "    strategy:\n"
        "      matrix:\n"
        "        include:\n"
        "          - ha-version: min\n"
        "            lockfile: requirements_dev_min.lock.txt\n"
        '            python-version: "3.13"\n'
    )
    monkeypatch.setattr(update_dependencies, "TESTS_WORKFLOW", tests_workflow)

    with pytest.raises(SystemExit, match="expected exactly 2"):
        update_dependencies.update_tests_workflow_min_python("3.14")


def test_tests_workflow_min_python_version_is_identical_across_both_matrices():
    """Regression guard for the bug where only pytest's "ha-version: min"
    matrix entry was ever regenerated (count=1 on a pattern matching both
    it and e2e's identical block) -- meta-lint's actionlint has no way to
    catch this kind of semantic drift, since both spellings are valid YAML
    on their own.
    """
    text = (_ROOT / ".github" / "workflows" / "tests.yaml").read_text()
    matches = re.findall(
        r"- ha-version: min\n\s*lockfile: requirements_dev_min\.lock\.txt\n"
        r'\s*python-version: "([^"]+)"',
        text,
    )
    assert len(matches) == 2, (
        "expected exactly 2 'ha-version: min' matrix entries in tests.yaml "
        f"(pytest's and e2e's), found {len(matches)}"
    )
    assert matches[0] == matches[1], (
        "pytest's and e2e's ha-version: min python-version have drifted "
        f"apart: {matches[0]!r} vs {matches[1]!r} -- see "
        "update_tests_workflow_min_python in scripts/update_dependencies.py"
    )


def test_exact_pins_extracts_only_unconditional_exact_pins(monkeypatch):
    requires_dist = [
        "aiohttp==3.14.1",
        "pyjwt[crypto]==2.12.1",
        # Range, not exact -- not something we can never independently bump.
        "awesomeversion>=25.8.0",
        # Marker-gated -- conditional, so not a pin our Linux-only CI
        # resolution is actually bound by.
        'pywin32==308; sys_platform == "win32"',
    ]

    def _fetch_json(_url: str) -> dict[str, object]:
        return {"info": {"requires_dist": requires_dist}}

    monkeypatch.setattr(update_dependencies, "fetch_json", _fetch_json)

    pins = update_dependencies.exact_pins("homeassistant", "2026.7.2")

    assert pins == {"aiohttp": "3.14.1", "pyjwt": "2.12.1"}


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("PyJWT", "pyjwt"),
        ("pyjwt", "pyjwt"),
        ("aiohttp_cors", "aiohttp-cors"),
        ("atomicwrites.homeassistant", "atomicwrites-homeassistant"),
    ],
)
def test_normalize_pypi_name_collapses_pep503_equivalent_spellings(name, expected):
    assert update_dependencies.normalize_pypi_name(name) == expected


def test_locked_pin_reads_the_exact_version_from_a_pip_compile_style_line(tmp_path):
    lock_file = tmp_path / "requirements.lock.txt"
    lock_file.write_text(
        "attrs==26.1.0 \\\n"
        "    --hash=sha256:deadbeef\n"
        "homeassistant==2026.7.2 \\\n"
        "    --hash=sha256:deadbeef\n"
    )

    assert update_dependencies.locked_pin(lock_file, "homeassistant") == "2026.7.2"


def test_locked_pin_raises_when_the_package_is_not_pinned(tmp_path):
    lock_file = tmp_path / "requirements.lock.txt"
    lock_file.write_text("attrs==26.1.0 \\\n")

    with pytest.raises(SystemExit, match="could not find a homeassistant== pin"):
        update_dependencies.locked_pin(lock_file, "homeassistant")


def test_collect_dependabot_exclude_names_unions_and_normalizes_both_ha_legs(
    monkeypatch,
):
    monkeypatch.setattr(update_dependencies, "REQUIREMENTS_DEV_LOCK", "dev-lock")
    monkeypatch.setattr(update_dependencies, "REQUIREMENTS_DEV_MIN_LOCK", "min-lock")

    pinned_versions = {
        ("dev-lock", "homeassistant"): "2026.7.2",
        ("dev-lock", "pytest-homeassistant-custom-component"): "0.13.346",
        ("min-lock", "homeassistant"): "2026.1.0",
        ("min-lock", "pytest-homeassistant-custom-component"): "0.13.305",
    }

    def _locked_pin(lock_file: Path, name: str) -> str:
        return pinned_versions[(str(lock_file), name)]

    monkeypatch.setattr(update_dependencies, "locked_pin", _locked_pin)

    exact_pin_results = {
        ("homeassistant", "2026.7.2"): {
            "PyJWT": "2.12.1",
            "aiohttp": "3.14.1",
            "paho-mqtt": "2.1.0",
        },
        ("homeassistant", "2026.1.0"): {"pyjwt": "2.10.1"},
        ("pytest-homeassistant-custom-component", "0.13.346"): {"pytest": "9.0.3"},
        ("pytest-homeassistant-custom-component", "0.13.305"): {"pytest": "9.0.0"},
    }

    def _exact_pins(package: str, version: str) -> dict[str, str]:
        return exact_pin_results[(package, version)]

    monkeypatch.setattr(update_dependencies, "exact_pins", _exact_pins)

    names = update_dependencies.collect_dependabot_exclude_names()

    # "PyJWT" and "pyjwt" from the two legs collapse into one normalized
    # entry; the two pytest pins from either phacc leg collapse likewise.
    assert names == ["aiohttp", "paho-mqtt", "pyjwt", "pytest"]


def test_update_dependabot_exclude_list_rewrites_only_group_exclusions(
    tmp_path, monkeypatch
):
    dependabot_yml = tmp_path / "dependabot.yml"
    dependabot_yml.write_text(
        "        exclude-patterns:\n"
        "        # <dependabot-exclude-generated>\n"
        '        - "stale"\n'
        "        # </dependabot-exclude-generated>\n"
    )
    monkeypatch.setattr(update_dependencies, "DEPENDABOT_YML", dependabot_yml)
    monkeypatch.setattr(
        update_dependencies,
        "collect_dependabot_exclude_names",
        lambda: ["aiohttp", "pyjwt"],
    )

    update_dependencies.update_dependabot_exclude_list()

    text = dependabot_yml.read_text()
    assert '        - "aiohttp"\n        - "pyjwt"\n' in text
    assert "ignore:" not in text
    assert "stale" not in text
