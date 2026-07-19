#!/usr/bin/env python3
"""Refresh dependency constraints, pins, and generated lock files.

Normal rebuilds and CI installs consume checked-in constraints, pins, and lock
files. This script is the explicit operation that contacts upstream registries,
updates them, and regenerates lock files in one visible git diff.
"""

from __future__ import annotations

import argparse
import calendar
import datetime
import functools
import hashlib
import html.parser
import json
import re
import subprocess
import urllib.request
from collections import OrderedDict
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
DEVCONTAINER_DIR = ROOT / ".devcontainer"
DEVCONTAINER_JSON = DEVCONTAINER_DIR / "devcontainer.json"
DEVCONTAINER_LOCK_FILE = DEVCONTAINER_DIR / "devcontainer-lock.json"
TOOL_VERSIONS_FILE = DEVCONTAINER_DIR / "tool-versions.env"

HACS_JSON = ROOT / "hacs.json"
REQUIREMENTS_DEV_MIN_TXT = ROOT / "requirements_dev_min.txt"
REQUIREMENTS_DEV_MIN_LOCK = ROOT / "requirements_dev_min.lock.txt"
REQUIREMENTS_DEV_LOCK = ROOT / "requirements_dev.lock.txt"
REQUIREMENTS_RUNTIME_TXT = ROOT / "requirements_runtime.txt"
TESTS_WORKFLOW = ROOT / ".github" / "workflows" / "tests.yaml"
DEPENDABOT_YML = ROOT / ".github" / "dependabot.yml"

# Decided 2026-07-09: trail the current HA release by ~2 release cycles
# before dropping support for anything older. See requirements_dev_min.txt
# for the mechanism this drives.
MIN_HA_VERSION_MONTHS_BEHIND = 6
# How many pytest-homeassistant-custom-component releases to walk forward
# (never backward) past the months-behind target looking for one whose
# dependency set actually resolves, before giving up and failing loudly.
MIN_HA_VERSION_MAX_ATTEMPTS = 12

NPM_VERSION_PINS: OrderedDict[str, str] = OrderedDict(
    [
        ("CLAUDE_CODE_VERSION", "@anthropic-ai/claude-code"),
        ("CODEX_VERSION", "@openai/codex"),
        ("COPILOT_CLI_VERSION", "@github/copilot"),
        ("KILO_VERSION", "@kilocode/cli"),
        ("OPENCODE_VERSION", "opencode-ai"),
        ("PNPM_VERSION", "pnpm"),
    ]
)

# Every npm-distributed tool is consumed through an npm-compatible selector, so
# pin only its major release line and let rebuilds resolve the newest compatible
# minor/patch.
# This deliberately uses "MAJOR.x" instead of caret ranges: npm's ^0.144.0
# would allow patch releases only, while 0.x also allows Codex minor releases.
NPM_MAJOR_RANGE_KEYS = frozenset(NPM_VERSION_PINS)

GITHUB_RELEASE_PINS: OrderedDict[str, str] = OrderedDict(
    [
        ("GITHUB_CLI_VERSION", "cli/cli"),
        ("NVM_VERSION", "nvm-sh/nvm"),
        ("RIPGREP_VERSION", "BurntSushi/ripgrep"),
    ]
)

# Unlike GITHUB_RELEASE_PINS above (consumed only as devcontainer feature
# version *options*, which install and verify their own binary), these are
# installed by a direct pinned curl download in both postCreate.sh and
# tests.yaml's meta-lint job — so besides the version, this script also has
# to pin and refresh the exact sha256 of the release asset those two
# checksum-verify against. {version} in the asset template is substituted
# with the resolved version (no "v" prefix, matching this repo's other
# pins) before building the download URL.
GITHUB_BINARY_PINS: OrderedDict[str, tuple[str, str]] = OrderedDict(
    [
        ("ACTIONLINT", ("rhysd/actionlint", "actionlint_{version}_linux_amd64.tar.gz")),
        ("HADOLINT", ("hadolint/hadolint", "hadolint-linux-x86_64")),
    ]
)

FEATURE_OPTION_REFS: dict[str, OrderedDict[str, str | bool]] = {
    "ghcr.io/devcontainers-extra/features/ripgrep:1": OrderedDict(
        [("version", "RIPGREP_VERSION")]
    ),
    "ghcr.io/devcontainers/features/github-cli:1": OrderedDict(
        [("version", "GITHUB_CLI_VERSION")]
    ),
    "ghcr.io/devcontainers-extra/features/uv:1": OrderedDict(
        [("version", "UV_VERSION")]
    ),
    "ghcr.io/devcontainers/features/node:2": OrderedDict(
        [
            ("version", "NODE_VERSION"),
            ("pnpmVersion", "PNPM_VERSION"),
            ("nvmVersion", "NVM_VERSION"),
        ]
    ),
    "ghcr.io/devcontainers/features/docker-outside-of-docker:1": OrderedDict(
        [
            ("version", "DOCKER_CLI_VERSION"),
            ("enableNonRootDocker", True),
            ("moby", False),
        ]
    ),
}

# requirements_dev_min.lock.txt is deliberately excluded here: unlike the
# other two (whose floors are hardcoded full patches that never drift), its
# homeassistant pin is frozen ~months behind current, so its *actual*
# Python floor drifts independently — see min_lock_python_floor(), which
# resolves it fresh from that pin instead of a hardcoded bare minor.
PYTHON_LOCKS: tuple[tuple[str, str, str], ...] = (
    ("requirements_dev.txt", "requirements_dev.lock.txt", "3.14.6"),
    ("requirements_runtime.txt", "requirements_runtime.lock.txt", "3.13.14"),
)


def run_text(args: list[str]) -> str:
    try:
        return subprocess.check_output(
            args, cwd=ROOT, text=True, stderr=subprocess.STDOUT
        )
    except FileNotFoundError as err:
        raise SystemExit(f"missing required command: {args[0]}") from err
    except subprocess.CalledProcessError as err:
        raise SystemExit(
            f"command failed ({' '.join(args)}):\n{err.output.strip()}"
        ) from err


def run(args: list[str]) -> None:
    try:
        subprocess.run(args, cwd=ROOT, check=True)
    except FileNotFoundError as err:
        raise SystemExit(f"missing required command: {args[0]}") from err
    except subprocess.CalledProcessError as err:
        raise SystemExit(
            f"command failed ({' '.join(args)}): exit {err.returncode}"
        ) from err


@functools.cache
def fetch_json(url: str) -> Any:
    # Cached for the script's lifetime: collect_dependabot_ignore_names()
    # calls exact_pins() on the same (package, version) pairs that
    # min_lock_python_floor()/homeassistant_pin_for_phacc() just resolved
    # moments earlier in the same run (refresh_python_locks() and
    # refresh_min_ha_version() both hit this), so without caching every
    # ordinary run makes 2 redundant blocking PyPI requests. Every caller
    # only reads the returned dict, never mutates it, so sharing the same
    # cached object across calls is safe.
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "ha-pylontech-dependency-updater"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def latest_npm_version(package: str) -> str:
    return run_text(["npm", "view", package, "version"]).strip()


def npm_major_range(version: str) -> str:
    match = re.fullmatch(r"(\d+)\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", version)
    if not match:
        raise SystemExit(f"not a valid npm version: {version!r}")
    return f"{match.group(1)}.x"


def latest_github_release_version(repository: str) -> str:
    data = cast(
        dict[str, Any],
        fetch_json(f"https://api.github.com/repos/{repository}/releases/latest"),
    )
    tag = str(data["tag_name"])
    return tag.removeprefix("v")


def sha256_of_url(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "ha-pylontech-dependency-updater"},
    )
    digest = hashlib.sha256()
    with urllib.request.urlopen(request, timeout=120) as response:
        for chunk in iter(lambda: response.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def semver_key(version: str) -> tuple[int, int, int, tuple[str, ...]]:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:[-+.]([0-9A-Za-z.-]+))?", version)
    if not match:
        return (-1, -1, -1, (version,))
    suffix = tuple((match.group(4) or "").split("."))
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)), suffix)


def latest_node_version(major: int) -> str:
    data = cast(list[dict[str, Any]], fetch_json("https://nodejs.org/dist/index.json"))
    versions = [
        str(item["version"]).removeprefix("v")
        for item in data
        if str(item["version"]).removeprefix("v").startswith(f"{major}.")
    ]
    if not versions:
        raise SystemExit(f"could not find latest Node.js {major}.x version")
    return max(versions, key=semver_key)


class DockerIndexParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        for name, value in attrs:
            if name == "href" and value:
                self.hrefs.append(value)


def latest_docker_cli_version() -> str:
    request = urllib.request.Request(
        "https://download.docker.com/linux/static/stable/x86_64/",
        headers={"User-Agent": "ha-pylontech-dependency-updater"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        html = response.read().decode()
    parser = DockerIndexParser()
    parser.feed(html)
    versions: list[str] = []
    for href in parser.hrefs:
        match = re.fullmatch(r"docker-(\d+\.\d+\.\d+)\.tgz", href)
        if match:
            versions.append(match.group(1))
    if not versions:
        raise SystemExit("could not find Docker CLI versions in static download index")
    return max(versions, key=semver_key)


def latest_uv_version() -> str:
    data = cast(dict[str, Any], fetch_json("https://pypi.org/pypi/uv/json"))
    info = cast(dict[str, Any], data["info"])
    return str(info["version"])


def read_env(path: Path) -> OrderedDict[str, str]:
    values: OrderedDict[str, str] = OrderedDict()
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, value = stripped.partition("=")
        if not sep:
            raise SystemExit(f"invalid line in {path}: {line}")
        values[key] = value
    return values


def write_env(path: Path, values: OrderedDict[str, str]) -> None:
    lines = [
        "# Generated by scripts/update_dependencies.py.",
        "# Rebuilds consume these constraints/pins; run "
        "`make update-deps` to refresh them.",
    ]
    for key in sorted(values):
        lines.append(f"{key}={values[key]}")
    path.write_text("\n".join(lines) + "\n")


def feature_digest(ref: str) -> str:
    output = run_text(["docker", "buildx", "imagetools", "inspect", ref])
    match = re.search(r"^Digest:\s+(sha256:[0-9a-f]+)$", output, re.MULTILINE)
    if not match:
        raise SystemExit(f"could not find digest for feature {ref}")
    return match.group(1)


def feature_metadata(ref: str) -> dict[str, Any]:
    manifest = cast(
        dict[str, Any], json.loads(run_text(["docker", "manifest", "inspect", ref]))
    )
    annotations = cast(dict[str, Any], manifest.get("annotations", {}))
    raw = annotations.get("dev.containers.metadata")
    if not raw:
        raise SystemExit(f"feature {ref} did not expose dev.containers.metadata")
    return cast(dict[str, Any], json.loads(str(raw)))


def devcontainer_feature_order() -> list[str]:
    text = DEVCONTAINER_JSON.read_text()
    start = text.find('  "features": {')
    if start == -1:
        raise SystemExit(f"could not find features block in {DEVCONTAINER_JSON}")
    depth = 0
    in_string = False
    escape = False
    end = None
    for index in range(text.find("{", start), len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index
                break
    if end is None:
        raise SystemExit(f"could not find end of features block in {DEVCONTAINER_JSON}")
    block = text[start:end]
    return re.findall(r'^    "([^"]+)":', block, re.MULTILINE)


def refresh_feature_lock() -> None:
    lock = cast(
        OrderedDict[str, Any],
        json.loads(DEVCONTAINER_LOCK_FILE.read_text(), object_pairs_hook=OrderedDict),
    )
    features = lock.get("features")
    if not isinstance(features, OrderedDict):
        raise SystemExit(f"{DEVCONTAINER_LOCK_FILE} does not contain a features object")
    feature_entries = cast(OrderedDict[str, OrderedDict[str, str]], features)

    feature_order = devcontainer_feature_order()
    reordered: OrderedDict[str, OrderedDict[str, str]] = OrderedDict()
    for ref in feature_order:
        if ref in feature_entries:
            reordered[ref] = feature_entries[ref]
    for ref, entry in feature_entries.items():
        if ref not in reordered:
            reordered[ref] = entry
    lock["features"] = reordered

    for ref, entry in reordered.items():
        metadata = feature_metadata(ref)
        digest = feature_digest(ref)
        entry["version"] = str(metadata["version"])
        entry["resolved"] = f"{ref.split(':', 1)[0]}@{digest}"
        entry["integrity"] = digest
        print(f"feature {ref} -> {entry['version']} {digest}")

    DEVCONTAINER_LOCK_FILE.write_text(json.dumps(lock, indent=2) + "\n")


def refresh_tool_versions(existing: OrderedDict[str, str]) -> OrderedDict[str, str]:
    values = OrderedDict(existing)

    for env_key, package in NPM_VERSION_PINS.items():
        latest = latest_npm_version(package)
        values[env_key] = (
            npm_major_range(latest) if env_key in NPM_MAJOR_RANGE_KEYS else latest
        )
        print(f"npm {package} -> {values[env_key]}")

    for env_key, repository in GITHUB_RELEASE_PINS.items():
        values[env_key] = latest_github_release_version(repository)
        print(f"github {repository} -> {values[env_key]}")

    for env_key, (repository, asset_template) in GITHUB_BINARY_PINS.items():
        version = latest_github_release_version(repository)
        asset = asset_template.format(version=version)
        url = f"https://github.com/{repository}/releases/download/v{version}/{asset}"
        sha256 = sha256_of_url(url)
        values[f"{env_key}_VERSION"] = version
        values[f"{env_key}_SHA256"] = sha256
        print(f"github binary {repository} {asset} -> {version} {sha256}")

    node_major = int(values.get("NODE_VERSION", "22").split(".", 1)[0])
    values["NODE_VERSION"] = latest_node_version(node_major)
    print(f"node {node_major}.x -> {values['NODE_VERSION']}")

    values["DOCKER_CLI_VERSION"] = latest_docker_cli_version()
    print(f"docker cli -> {values['DOCKER_CLI_VERSION']}")

    values["UV_VERSION"] = latest_uv_version()
    print(f"uv -> {values['UV_VERSION']}")

    return values


def render_feature_options(
    options: OrderedDict[str, str | bool], values: OrderedDict[str, str]
) -> str:
    lines = ["{"]
    rendered: list[tuple[str, str | bool]] = []
    for option_name, env_key_or_value in options.items():
        if isinstance(env_key_or_value, bool):
            rendered.append((option_name, env_key_or_value))
        else:
            rendered.append((option_name, values[env_key_or_value]))
    for index, (name, value) in enumerate(rendered):
        comma = "," if index < len(rendered) - 1 else ""
        if isinstance(value, bool):
            rendered_value = "true" if value else "false"
        else:
            rendered_value = json.dumps(value)
        lines.append(f"      {json.dumps(name)}: {rendered_value}{comma}")
    lines.append("    }")
    return "\n".join(lines)


def replace_feature_block(
    text: str,
    ref: str,
    options: OrderedDict[str, str | bool],
    values: OrderedDict[str, str],
) -> str:
    replacement = f"    {json.dumps(ref)}: {render_feature_options(options, values)},"

    single_line = re.compile(
        rf"^    {re.escape(json.dumps(ref))}: \{{\}},?", re.MULTILINE
    )
    if single_line.search(text):
        return single_line.sub(replacement, text, count=1)

    pattern = re.compile(
        rf"^    {re.escape(json.dumps(ref))}: \{{\n(?:^      .*\n)*^    \}},?",
        re.MULTILINE,
    )
    if pattern.search(text):
        return pattern.sub(replacement, text, count=1)

    raise SystemExit(f"could not find feature block for {ref} in {DEVCONTAINER_JSON}")


def update_devcontainer_json(values: OrderedDict[str, str]) -> None:
    text = DEVCONTAINER_JSON.read_text()
    for ref, options in FEATURE_OPTION_REFS.items():
        text = replace_feature_block(text, ref, options, values)
    text = text.replace(
        "// Node, pnpm, and nvm are exact pins here. CLI versions that should\n"
        "    // move are updated by .devcontainer/updateToolVersions.sh, then\n"
        "    // consumed as exact pins by feature options here and by\n"
        "    // postCreate.sh's pnpm install step.\n",
        "// Node, pnpm, and nvm are exact pins here. CLI versions that should\n"
        "    // move are updated by `make update-deps`, then consumed as exact\n"
        "    // pins by feature options here and by postCreate.sh's pnpm\n"
        "    // install step.\n",
    )
    text = text.replace(
        '// Pinned major version: the feature defaults to whatever "lts" resolves\n'
        "    // to at build time, which drifts across major versions over a\n"
        "    // container's lifetime. CLI versions that should move are updated by\n"
        "    // .devcontainer/updateToolVersions.sh, then consumed as exact pins by\n"
        "    // feature options here and by postCreate.sh's pnpm install step.\n",
        "// Node, pnpm, and nvm are exact pins here. CLI versions that should\n"
        "    // move are updated by `make update-deps`, then consumed as exact\n"
        "    // pins by feature options here and by postCreate.sh's pnpm\n"
        "    // install step.\n",
    )
    text = text.replace(
        "// container's lifetime. The npm-installed AI CLIs below (Codex,\n"
        "    // Kilocode) are deliberately left unpinned instead — those want latest\n"
        '    // by design, and floating is the whole point of an "agent" package.\n',
        "// Node, pnpm, and nvm are exact pins here. CLI versions that should\n"
        "    // move are updated by `make update-deps`, then consumed as exact\n"
        "    // pins by feature options here and by postCreate.sh's pnpm\n"
        "    // install step.\n",
    )
    text = re.sub(r",(\n  \},\n  \"containerEnv\")", r"\1", text, count=1)
    DEVCONTAINER_JSON.write_text(text)


def refresh_devcontainer_dependencies() -> None:
    values = refresh_tool_versions(read_env(TOOL_VERSIONS_FILE))
    write_env(TOOL_VERSIONS_FILE, values)
    update_devcontainer_json(values)
    refresh_feature_lock()


def compile_python_lock(requirements: str, output: str, python_version: str) -> None:
    print(f"uv pip compile {requirements} -> {output} (Python {python_version})")
    run(
        [
            "uv",
            "pip",
            "compile",
            "--quiet",
            # Without --upgrade, uv reuses the existing lockfile's pins for
            # any package whose constraint is still satisfiable, even when
            # a newer compatible version exists, so unconstrained packages
            # can sit stale indefinitely despite this compiling weekly.
            # Confirmed by recompiling requirements_dev.lock.txt against
            # its own committed content as a baseline: boto3, mypy, ruff,
            # grpcio, filelock, tzdata, and virtualenv all had newer
            # compatible versions available that a prior run without
            # --upgrade had left on the table. (Note this does *not* apply
            # to every stale-looking package — some, like pyjwt, are
            # pinned exactly by homeassistant's own requires_dist and stay
            # put either way; see dependabot.yml's ignore list for those.)
            "--upgrade",
            requirements,
            "--generate-hashes",
            "--python-version",
            python_version,
            "-o",
            output,
        ]
    )


def refresh_python_locks() -> None:
    for requirements, output, python_version in PYTHON_LOCKS:
        compile_python_lock(requirements, output, python_version)
    compile_python_lock(
        "requirements_dev_min.txt",
        "requirements_dev_min.lock.txt",
        min_lock_python_floor(),
    )
    # Both locks are current on disk at this point (the "min" leg's own
    # pinned phacc/homeassistant identity is untouched here — only
    # refresh_min_ha_version() changes that — but its lock content is still
    # freshly recompiled above), so this is a valid point to also refresh
    # dependabot.yml's generated ignore list from both.
    update_dependabot_ignore_list()


def months_ago(months: int) -> str:
    today = datetime.date.today()
    total_months = today.year * 12 + (today.month - 1) - months
    year, month0 = divmod(total_months, 12)
    day = min(today.day, calendar.monthrange(year, month0 + 1)[1])
    return datetime.date(year, month0 + 1, day).isoformat()


def phacc_releases_sorted() -> list[tuple[str, str]]:
    data = fetch_json(
        "https://pypi.org/pypi/pytest-homeassistant-custom-component/json"
    )
    releases = cast(dict[str, list[dict[str, Any]]], data["releases"])
    items: list[tuple[str, str]] = []
    for version, files in releases.items():
        if not files or any(f.get("yanked") for f in files):
            continue
        items.append((version, str(files[0]["upload_time"])[:10]))
    items.sort(key=lambda item: item[1])
    return items


def homeassistant_pin_for_phacc(version: str) -> str:
    data = fetch_json(
        f"https://pypi.org/pypi/pytest-homeassistant-custom-component/{version}/json"
    )
    info = cast(dict[str, Any], data["info"])
    for requirement in cast(list[str], info["requires_dist"] or []):
        if requirement.startswith("homeassistant=="):
            return requirement.split("==", 1)[1].split(";")[0].strip()
    raise SystemExit(
        f"pytest-homeassistant-custom-component=={version} has no exact "
        "homeassistant== pin"
    )


def exact_pins(package: str, version: str) -> dict[str, str]:
    """Every dependency `package==version` pins exactly via an unconditional
    `==` entry in its own requires_dist.

    Generalizes homeassistant_pin_for_phacc's single-name lookup to every
    requirement — this drives dependabot.yml's generated ignore: list (see
    collect_dependabot_ignore_names): a package pinned this way can never be
    bumped independently by Dependabot, since our own
    dependency-updates.yaml/min-ha-version-update.yaml (which respect the
    same upstream pin) can't apply an independent bump either.
    """
    data = fetch_json(f"https://pypi.org/pypi/{package}/{version}/json")
    info = cast(dict[str, Any], data["info"])
    pins: dict[str, str] = {}
    for requirement in cast(list[str], info["requires_dist"] or []):
        if ";" in requirement:
            # Environment-marker-gated (e.g. `pywin32==...; sys_platform ==
            # "win32"`) — conditional, so not a pin our Linux-only CI
            # resolution is actually bound by.
            continue
        match = re.match(
            r"^([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?\s*==\s*([A-Za-z0-9_.\-]+)$",
            requirement.strip(),
        )
        if match:
            pins[match.group(1)] = match.group(2)
    return pins


def normalize_pypi_name(name: str) -> str:
    """PEP 503 normalization — pip/PyPI treat these spellings as one project
    (e.g. "PyJWT", "pyjwt", and "py_jwt" would all collapse together)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def homeassistant_requires_python(version: str) -> str:
    """Return the full >=X.Y.Z floor, e.g. "3.13.2" (not just "3.13").

    `uv pip compile --python-version` treats a bare "3.13" as the *lowest*
    3.13 patch, which then conflicts with any release whose actual floor is
    a later patch (e.g. homeassistant requiring >=3.13.2) — the full floor
    is needed here even though tests.yaml's matrix only wants major.minor
    (see major_minor_python below), because astral-sh/setup-uv resolves a
    bare "3.13" there to the *latest* matching patch, which always satisfies
    any 3.13.x floor.
    """
    data = fetch_json(f"https://pypi.org/pypi/homeassistant/{version}/json")
    info = cast(dict[str, Any], data["info"])
    requires_python = str(info["requires_python"] or "")
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", requires_python)
    if not match:
        raise SystemExit(
            f"homeassistant=={version} has no parseable requires_python "
            f"({requires_python!r})"
        )
    major, minor, patch = match.group(1), match.group(2), match.group(3) or "0"
    return f"{major}.{minor}.{patch}"


def current_min_phacc_pin() -> str:
    match = re.search(
        r"pytest-homeassistant-custom-component==(\S+)",
        REQUIREMENTS_DEV_MIN_TXT.read_text(),
    )
    if not match:
        raise SystemExit(
            "could not find a pytest-homeassistant-custom-component== pin in "
            f"{REQUIREMENTS_DEV_MIN_TXT}"
        )
    return match.group(1)


def min_lock_python_floor() -> str:
    """The min lock's *currently pinned* homeassistant release's own Python
    floor — resolved fresh rather than hardcoded, since that pin is frozen
    ~months behind current (see MIN_HA_VERSION_MONTHS_BEHIND) and each
    min-ha-version bump can move its floor to a later patch.
    """
    phacc_version = current_min_phacc_pin()
    ha_version = homeassistant_pin_for_phacc(phacc_version)
    return homeassistant_requires_python(ha_version)


def major_minor_python(version: str) -> str:
    match = re.match(r"(\d+)\.(\d+)", version)
    if not match:
        raise SystemExit(f"not a major.minor(.patch) Python version: {version!r}")
    return f"{match.group(1)}.{match.group(2)}"


def write_min_requirements_pin(phacc_version: str) -> None:
    text = REQUIREMENTS_DEV_MIN_TXT.read_text()
    new_text = re.sub(
        r"pytest-homeassistant-custom-component==\S+",
        f"pytest-homeassistant-custom-component=={phacc_version}",
        text,
        count=1,
    )
    if new_text == text:
        raise SystemExit(
            f"could not find a pytest-homeassistant-custom-component== pin in "
            f"{REQUIREMENTS_DEV_MIN_TXT}"
        )
    REQUIREMENTS_DEV_MIN_TXT.write_text(new_text)


def try_compile_min_lock(phacc_version: str, python_version: str) -> bool:
    write_min_requirements_pin(phacc_version)
    try:
        subprocess.run(
            [
                "uv",
                "pip",
                "compile",
                "--quiet",
                # See compile_python_lock's identical --upgrade comment —
                # same reasoning applies here: without it, a transitive
                # dependency's version could stay frozen at whatever a
                # previous attempt happened to pick, even when a newer
                # version compatible with *this* candidate HA pin exists.
                "--upgrade",
                str(REQUIREMENTS_DEV_MIN_TXT),
                "--generate-hashes",
                "--python-version",
                python_version,
                "-o",
                str(REQUIREMENTS_DEV_MIN_LOCK),
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        return True
    except subprocess.CalledProcessError as err:
        output = err.stderr or err.stdout or str(err)
        reason = output.strip().splitlines()[-1] if output.strip() else str(err)
        print(f"  {phacc_version} did not resolve: {reason}")
        return False


def update_hacs_json(ha_version: str) -> None:
    text = HACS_JSON.read_text()
    new_text = re.sub(
        r'"homeassistant":\s*"[^"]+"', f'"homeassistant": "{ha_version}"', text, count=1
    )
    if new_text == text:
        raise SystemExit(f'could not find a "homeassistant" field in {HACS_JSON}')
    HACS_JSON.write_text(new_text)


def update_tests_workflow_min_python(python_version: str) -> None:
    text = TESTS_WORKFLOW.read_text()
    pattern = re.compile(
        r"(- ha-version: min\n\s*lockfile: requirements_dev_min\.lock\.txt\n"
        r"\s*python-version: )\"[^\"]+\""
    )
    # No count= limit: tests.yaml has two identical "ha-version: min" matrix
    # blocks today (pytest's and e2e's), and both must track the same
    # floor. Asserting exactly 2 rather than just count != 0 catches this
    # drifting silently in either direction -- a stale copy left behind by
    # a hardcoded count=1 (the original bug here: only pytest's matrix ever
    # got patched, so e2e's silently ran the old interpreter against the
    # new lockfile once a min-ha-version bump crossed a Python minor
    # boundary), or a third leg added to the matrix later without this
    # function being updated to match.
    new_text, count = pattern.subn(lambda m: f'{m.group(1)}"{python_version}"', text)
    if count != 2:
        raise SystemExit(
            f"expected exactly 2 ha-version: min matrix entries in "
            f"{TESTS_WORKFLOW}, found {count}"
        )
    TESTS_WORKFLOW.write_text(new_text)


def locked_pin(lock_file: Path, name: str) -> str:
    match = re.search(rf"(?m)^{re.escape(name)}==(\S+)", lock_file.read_text())
    if not match:
        raise SystemExit(f"could not find a {name}== pin in {lock_file}")
    return match.group(1)


def runtime_package_names() -> set[str]:
    """Every package this project actually ships to users (see
    requirements_runtime.txt's own header comment: "The actual runtime
    footprint shipped to users"). Excluded from the generated dependabot
    ignore list even when also exactly pinned by homeassistant/phacc's
    dev-only dependency tree — e.g. paho-mqtt is both a runtime dependency
    here *and* one of homeassistant's own exact pins, so without this
    exclusion collect_dependabot_ignore_names() would silently suppress
    Dependabot's version *and* security alerts for a package that ships to
    every user, not a can-never-resolve dev-scope one.
    """
    names: set[str] = set()
    for line in REQUIREMENTS_RUNTIME_TXT.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.match(r"^([A-Za-z0-9_.\-]+)", line)
        if match:
            names.add(normalize_pypi_name(match.group(1)))
    return names


def collect_dependabot_ignore_names() -> list[str]:
    """Every package name exactly pinned (== in requires_dist) by whichever
    homeassistant or pytest-homeassistant-custom-component releases are
    *currently* locked, across both the "current" and "min" HA legs, minus
    anything runtime_package_names() says actually ships to users.

    Read fresh from both lock files rather than passed in, so this produces
    the same result regardless of which of refresh_python_locks() /
    refresh_min_ha_version() just ran — each only ever refreshes one leg's
    lock files, but dependabot.yml's ignore: list needs the union of both,
    or a Dependabot PR against the *other* leg's still-exact-pinned
    homeassistant/phacc release could slip through unignored.
    """
    sources = [
        ("homeassistant", locked_pin(REQUIREMENTS_DEV_LOCK, "homeassistant")),
        ("homeassistant", locked_pin(REQUIREMENTS_DEV_MIN_LOCK, "homeassistant")),
        (
            "pytest-homeassistant-custom-component",
            locked_pin(REQUIREMENTS_DEV_LOCK, "pytest-homeassistant-custom-component"),
        ),
        (
            "pytest-homeassistant-custom-component",
            locked_pin(
                REQUIREMENTS_DEV_MIN_LOCK, "pytest-homeassistant-custom-component"
            ),
        ),
    ]
    names: set[str] = set()
    for package, version in sources:
        for name in exact_pins(package, version):
            names.add(normalize_pypi_name(name))
    return sorted(names - runtime_package_names())


_DEPENDABOT_EXCLUDE_BEGIN = "        # <dependabot-exclude-generated>\n"
_DEPENDABOT_EXCLUDE_END = "        # </dependabot-exclude-generated>"
_DEPENDABOT_IGNORE_BEGIN = "      # <dependabot-ignore-generated>\n"
_DEPENDABOT_IGNORE_END = "      # </dependabot-ignore-generated>"


def _replace_between_markers(
    text: str, begin_marker: str, end_marker: str, body: str, *, label: str
) -> str:
    begin = text.find(begin_marker)
    end = text.find(end_marker)
    if begin == -1 or end == -1 or end < begin:
        raise SystemExit(f"could not find the {label} markers in {DEPENDABOT_YML}")
    begin += len(begin_marker)
    return text[:begin] + body + "\n" + text[end:]


def update_dependabot_ignore_list() -> None:
    """Rewrite dependabot.yml's ignore: list, and its mirror under the
    security-updates group's exclude-patterns, from
    collect_dependabot_ignore_names() — between two independent marker
    pairs, since the two blocks use different YAML shapes
    (dependency-name: mappings vs. bare pattern strings).

    A hand-maintained version of this list only ever grew by someone
    noticing a failed Dependabot PR after the fact (see
    close-stale-automation-prs.yaml's CI-failure-signature detection) — this
    computes the same "can never resolve" fact directly from what's exactly
    pinned right now, so it can't drift stale between homeassistant/phacc
    version bumps the way a hand-maintained list did, and both blocks are
    generated from the same source so they can't drift apart from each
    other either.
    """
    names = collect_dependabot_ignore_names()
    text = DEPENDABOT_YML.read_text()
    text = _replace_between_markers(
        text,
        _DEPENDABOT_EXCLUDE_BEGIN,
        _DEPENDABOT_EXCLUDE_END,
        "\n".join(f'        - "{name}"' for name in names),
        label="dependabot-exclude-generated",
    )
    text = _replace_between_markers(
        text,
        _DEPENDABOT_IGNORE_BEGIN,
        _DEPENDABOT_IGNORE_END,
        "\n".join(f'      - dependency-name: "{name}"' for name in names),
        label="dependabot-ignore-generated",
    )
    DEPENDABOT_YML.write_text(text)


def refresh_min_ha_version() -> None:
    cutoff = months_ago(MIN_HA_VERSION_MONTHS_BEHIND)
    releases = phacc_releases_sorted()
    candidate_indices = [i for i, (_, day) in enumerate(releases) if day <= cutoff]
    if not candidate_indices:
        raise SystemExit(
            f"no pytest-homeassistant-custom-component release found on/before {cutoff}"
        )
    start = candidate_indices[-1]

    attempts = releases[start : start + MIN_HA_VERSION_MAX_ATTEMPTS]
    for phacc_version, _ in attempts:
        ha_version = homeassistant_pin_for_phacc(phacc_version)
        python_floor = homeassistant_requires_python(ha_version)
        print(
            f"trying pytest-homeassistant-custom-component=={phacc_version} "
            f"(homeassistant=={ha_version}, python>={python_floor})"
        )
        if try_compile_min_lock(phacc_version, python_floor):
            update_hacs_json(ha_version)
            update_tests_workflow_min_python(major_minor_python(python_floor))
            update_dependabot_ignore_list()
            print(
                f"minimum supported HA version -> {ha_version} "
                f"(pytest-homeassistant-custom-component=={phacc_version})"
            )
            return

    raise SystemExit(
        f"none of {len(attempts)} pytest-homeassistant-custom-component releases from "
        f"{attempts[0][0]} onward resolved cleanly — needs manual investigation"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--devcontainer-only",
        action="store_true",
        help="refresh only devcontainer feature/tool constraints and pins",
    )
    group.add_argument(
        "--python-locks-only",
        action="store_true",
        help="refresh only Python requirement lock files",
    )
    group.add_argument(
        "--min-ha-version-only",
        action="store_true",
        help=(
            "refresh only the minimum supported HA version (hacs.json, "
            "requirements_dev_min.txt/.lock.txt, tests.yaml's min python-version) "
            "— not part of the default run; has its own schedule/PR since it's "
            "never auto-merged"
        ),
    )
    return parser.parse_args()


def main() -> int:
    if not DEVCONTAINER_DIR.is_dir():
        raise SystemExit("run from a checkout containing .devcontainer")

    args = parse_args()
    if args.min_ha_version_only:
        refresh_min_ha_version()
        print("updated minimum supported HA version")
        return 0

    if not args.python_locks_only:
        refresh_devcontainer_dependencies()
    if not args.devcontainer_only:
        refresh_python_locks()
    print("updated dependency constraints, pins, and generated lock files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
