#!/usr/bin/env python3
"""Refresh dependency pins and generated lock files.

Normal rebuilds and CI installs consume checked-in pins and lock files. This
script is the explicit operation that contacts upstream registries, updates the
pins, and regenerates lock files in one visible git diff.
"""

from __future__ import annotations

import argparse
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

GITHUB_RELEASE_PINS: OrderedDict[str, str] = OrderedDict(
    [
        ("GITHUB_CLI_VERSION", "cli/cli"),
        ("NVM_VERSION", "nvm-sh/nvm"),
        ("RIPGREP_VERSION", "BurntSushi/ripgrep"),
    ]
)

FEATURE_OPTION_REFS: dict[str, OrderedDict[str, str | bool]] = {
    "ghcr.io/devcontainers-extra/features/ripgrep:1": OrderedDict(
        [("version", "RIPGREP_VERSION")]
    ),
    "ghcr.io/devcontainers/features/github-cli:1": OrderedDict(
        [("version", "GITHUB_CLI_VERSION")]
    ),
    "ghcr.io/devcontainers-extra/features/claude-code:2": OrderedDict(
        [("version", "CLAUDE_CODE_VERSION")]
    ),
    "ghcr.io/devcontainers-extra/features/opencode:1": OrderedDict(
        [("version", "OPENCODE_VERSION")]
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
    "ghcr.io/devcontainers/features/copilot-cli:1": OrderedDict(
        [("version", "COPILOT_CLI_VERSION")]
    ),
    "ghcr.io/devcontainers/features/docker-outside-of-docker:1": OrderedDict(
        [
            ("version", "DOCKER_CLI_VERSION"),
            ("enableNonRootDocker", True),
            ("moby", False),
        ]
    ),
}

PYTHON_LOCKS: tuple[tuple[str, str, str], ...] = (
    ("requirements_dev.txt", "requirements_dev.lock.txt", "3.14.6"),
    ("requirements_dev_min.txt", "requirements_dev_min.lock.txt", "3.13"),
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


def fetch_json(url: str) -> Any:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "ha-pylontech-dependency-updater"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def latest_npm_version(package: str) -> str:
    return run_text(["npm", "view", package, "version"]).strip()


def latest_github_release_version(repository: str) -> str:
    data = cast(
        dict[str, Any],
        fetch_json(f"https://api.github.com/repos/{repository}/releases/latest"),
    )
    tag = str(data["tag_name"])
    return tag.removeprefix("v")


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
        "# Rebuilds consume these exact pins; run `make update-deps` to refresh them.",
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
        values[env_key] = latest_npm_version(package)
        print(f"npm {package} -> {values[env_key]}")

    for env_key, repository in GITHUB_RELEASE_PINS.items():
        values[env_key] = latest_github_release_version(repository)
        print(f"github {repository} -> {values[env_key]}")

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--devcontainer-only",
        action="store_true",
        help="refresh only devcontainer feature/tool pins",
    )
    parser.add_argument(
        "--python-locks-only",
        action="store_true",
        help="refresh only Python requirement lock files",
    )
    args = parser.parse_args()
    if args.devcontainer_only and args.python_locks_only:
        parser.error("choose at most one of --devcontainer-only/--python-locks-only")
    return args


def main() -> int:
    if not DEVCONTAINER_DIR.is_dir():
        raise SystemExit("run from a checkout containing .devcontainer")

    args = parse_args()
    if not args.python_locks_only:
        refresh_devcontainer_dependencies()
    if not args.devcontainer_only:
        refresh_python_locks()
    print("updated dependency pins and generated lock files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
