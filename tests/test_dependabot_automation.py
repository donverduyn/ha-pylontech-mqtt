from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
PR_KIND = ROOT / ".github" / "scripts" / "dependabot-pr-kind.sh"
SUPERSESSION_CHECK = ROOT / ".github" / "scripts" / "dependabot-supersession-check.sh"
PHACC_PINNED_CHECK = ROOT / ".github" / "scripts" / "dependabot-phacc-pinned-check.sh"
DAILY_SYNC = ROOT / ".github" / "scripts" / "daily-pr-sync.sh"
WORKFLOWS = ROOT / ".github" / "workflows"
CREATE_OR_UPDATE_PR_ACTION = (
    ROOT / ".github" / "actions" / "create-or-update-pr" / "action.yaml"
)
AUTO_MERGE_WORKFLOW = ROOT / ".github" / "workflows" / "dependabot-auto-merge.yaml"

STALE_WORKFLOW = ROOT / ".github" / "workflows" / "close-stale-automation-prs.yaml"

FAKE_GH = r"""#!/bin/bash
set -euo pipefail
printf '%s\n' "$*" >>"$GH_LOG"

if [ "$1" = "api" ]; then
  case "$2" in
    */protection/required_status_checks/contexts)
      if [ -n "${GH_CONTEXTS_ERROR:-}" ]; then
        # Mirrors real `gh api` behaviour on an error status: the response
        # body goes to stdout, the diagnostic to stderr, exit code non-zero.
        printf '%s\n' "$GH_CONTEXTS_ERROR"
        echo "gh: Resource not accessible by integration (HTTP 403)" >&2
        exit 1
      fi
      printenv GH_REQUIRED_CONTEXTS || printf '["ci"]\n'
      ;;
    */commits/*/check-runs*)
      # Trailing glob: the real call carries a ?per_page=100 query so that
      # --paginate walks every page (see daily-pr-sync.sh).
      sha="${2##*/commits/}"
      sha="${sha%%/check-runs*}"
      checkruns_var="GH_CHECKRUNS_$sha"
      raw="$(printenv "$checkruns_var" || printf '[]')"
      # GH_CHECKRUNS_* fixtures stay written as the array the old, un-paginated
      # `--jq '[.check_runs[] | ...]'` produced. Real `gh api --paginate --jq
      # '.check_runs[]'` emits one object per line across all pages instead,
      # for `jq -s` to slurp back, so reshape here rather than rewriting every
      # fixture. A value that is not exactly one JSON array (the concatenated
      # "[...][...]" malformed-output fixture) is passed through untouched --
      # that is the input under test.
      if jq -e -s 'length == 1 and (.[0] | type) == "array"' \
        >/dev/null 2>&1 <<<"$raw"; then
        jq -c '.[]' <<<"$raw"
      else
        printf '%s\n' "$raw"
      fi
      ;;
    *)
      echo "unexpected gh api path: $2" >&2
      exit 1
      ;;
  esac
  exit 0
fi

case "$1 $2" in
  "pr list")
    printf '%s\n' "$GH_PRS"
    ;;
  "pr update-branch")
    echo "updated"
    ;;
  "pr view")
    case "$*" in
      *headRefOid*)
        sha_var="GH_HEAD_SHA_$3"
        printenv "$sha_var" || echo "sha$3"
        ;;
      *)
        state_var="GH_MERGE_STATE_$3"
        printenv "$state_var" || echo "CLEAN"
        ;;
    esac
    ;;
  "pr merge")
    ;;
  "pr close")
    ;;
  "pr diff")
    files_var="GH_CHANGED_FILES_$3"
    printenv "$files_var" || printf ''
    ;;
  "repo view")
    printenv GH_DEFAULT_BRANCH || echo "master"
    ;;
  *)
    echo "unexpected gh command: $*" >&2
    exit 1
    ;;
esac
"""


def _install_fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)

    log = tmp_path / "gh.log"
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("GH_LOG", str(log))
    monkeypatch.setenv("GH_TOKEN", "test-token")
    monkeypatch.setenv("REPO", "owner/repo")

    # Point dependabot-phacc-pinned-check.sh at a fixture rather than the
    # repository's own generated exclude-patterns block. That block is
    # regenerated from whatever homeassistant/phacc pin is locked at the time,
    # so leaving these tests reading it would make unrelated cases (which
    # happen to use real package names like "requests") start closing PRs the
    # next time `make update-deps` runs.
    dependabot_yml = _write_exclude_block(
        tmp_path, ["exactly-pinned-package", "pytest"]
    )
    monkeypatch.setenv("DEPENDABOT_YML", str(dependabot_yml))
    return log


@pytest.mark.parametrize(
    "body",
    [
        "Bumps the docker group in /docker with 1 update: python.\n",
        (
            "Bumps the github-actions group with 2 updates:\n\n"
            "Updates `actions/checkout` from 4.0.0 to 4.1.0\n"
        ),
        "Bumps the security-updates group with 1 update: urllib3.\n",
        # The "in <directory>" clause can land after "with N updates"
        # instead of before it -- seen on a real github-actions group PR.
        (
            "Bumps the github-actions group with 2 updates in the / "
            "directory: [github/codeql-action/init]"
            "(https://github.com/github/codeql-action) and "
            "[github/codeql-action/analyze]"
            "(https://github.com/github/codeql-action).\n"
        ),
    ],
)
def test_dependabot_pr_kind_detects_generated_group_bodies(body: str) -> None:
    result = subprocess.run(
        [PR_KIND, body],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout == "group\n"


@pytest.mark.parametrize(
    "body",
    [
        "Bumps urllib3 from 2.0.0 to 2.0.1.\n",
        "Updates `urllib3` from 2.0.0 to 2.0.1\n",
        "",
        "Bumps something in a format we do not recognize.\n",
    ],
)
def test_dependabot_pr_kind_fails_closed_for_single_or_unknown_bodies(
    body: str,
) -> None:
    result = subprocess.run(
        [PR_KIND, body],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout == "single\n"


def _write_exclude_block(tmp_path: Path, names: list[str]) -> Path:
    path = tmp_path / "dependabot.yml"
    generated = "\n".join(f'        - "{name}"' for name in names)
    path.write_text(
        "    groups:\n"
        "      security-updates:\n"
        "        exclude-patterns:\n"
        "        # <dependabot-exclude-generated>\n"
        f"{generated}\n"
        "        # </dependabot-exclude-generated>\n"
    )
    return path


def _run_phacc_pinned_check(title: str, body: str, dependabot_yml: Path | None) -> str:
    env = dict(os.environ)
    if dependabot_yml is not None:
        env["DEPENDABOT_YML"] = str(dependabot_yml)
    result = subprocess.run(
        [PHACC_PINNED_CHECK, title, body],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout.strip()


def test_phacc_pinned_check_reads_the_real_generated_block() -> None:
    # The one end-to-end assertion of the contract between
    # update_dependabot_exclude_list() (which writes the block) and this
    # script (which reads it back): marker spelling, indentation, `- "name"`
    # syntax and double-quoting, none of which anything else checks. Every
    # other test here writes the fixture by hand in that same format, so they
    # could only ever agree with each other. Uses the repository's real
    # dependabot.yml and a name taken from it at runtime rather than a
    # hardcoded one, so it does not need updating when the list is
    # regenerated.
    dependabot_yml = ROOT / ".github" / "dependabot.yml"
    block = dependabot_yml.read_text().split("# <dependabot-exclude-generated>", 1)[1]
    block = block.split("# </dependabot-exclude-generated>", 1)[0]
    names = re.findall(r'-\s*"([^"]+)"', block)
    assert names, "the generated exclude-patterns block is empty"

    assert (
        _run_phacc_pinned_check(f"bump {names[0]} from 1.0.0 to 1.0.1", "", None)
        == "blocked"
    )


def test_phacc_pinned_check_blocks_a_package_pinned_exactly_by_phacc(
    tmp_path: Path,
) -> None:
    # The live shape of this: five open PRs (#139/#142/#143/#146/#147) sat
    # failing `pytest (min)` for a week each with
    # "Because pytest-homeassistant-custom-component==0.13.308 depends on
    # pytest==9.0.0 and you require pytest==9.0.3 ... unsatisfiable". No
    # rebase can resolve that, so waiting on checks only burns run budget.
    dependabot_yml = _write_exclude_block(tmp_path, ["pytest", "pyjwt"])

    assert (
        _run_phacc_pinned_check(
            "chore(deps-dev): bump pytest from 9.0.0 to 9.0.3", "", dependabot_yml
        )
        == "blocked"
    )
    # The generated body section is read too, not just the title -- a grouped
    # PR's title carries no dependency name at all.
    assert (
        _run_phacc_pinned_check(
            "chore(deps): bump the security-updates group",
            "Bumps the security-updates group with 1 update: pyjwt.\n\n"
            "Updates `pyjwt` from 2.10.1 to 2.13.0\n",
            dependabot_yml,
        )
        == "blocked"
    )


def test_phacc_pinned_check_normalizes_names_the_way_pypi_does(
    tmp_path: Path,
) -> None:
    # update_dependabot_exclude_list() writes already-normalised names, but
    # Dependabot renders whatever the PR's own manifest spells.
    dependabot_yml = _write_exclude_block(tmp_path, ["paho-mqtt", "pytest-asyncio"])

    for rendered in ("paho.mqtt", "paho_mqtt", "Paho-MQTT"):
        assert (
            _run_phacc_pinned_check(
                f"bump {rendered} from 1.0 to 2.0", "", dependabot_yml
            )
            == "blocked"
        ), rendered


@pytest.mark.parametrize(
    ("title", "body"),
    [
        # Not in the generated list at all -- an ordinary, mergeable bump.
        ("chore(deps): bump actions/checkout from 4.0.0 to 7.0.1", ""),
        # Nothing parseable to compare: fails open rather than closing a PR
        # this cannot classify, since closing is not reversible here.
        ("chore(deps): bump the docker group", "Bumps something unrecognized.\n"),
        ("", ""),
    ],
)
def test_phacc_pinned_check_fails_open_for_anything_it_cannot_confirm(
    tmp_path: Path, title: str, body: str
) -> None:
    dependabot_yml = _write_exclude_block(tmp_path, ["pytest"])

    assert _run_phacc_pinned_check(title, body, dependabot_yml) == "ok"


def test_phacc_pinned_check_reads_only_the_generated_block(tmp_path: Path) -> None:
    # A hand-written exclusion outside the markers is a grouping preference,
    # not a statement that the package is exactly pinned, so it must not cause
    # a close.
    path = tmp_path / "dependabot.yml"
    path.write_text(
        "        exclude-patterns:\n"
        '        - "hand-written-exclusion"\n'
        "        # <dependabot-exclude-generated>\n"
        '        - "pytest"\n'
        "        # </dependabot-exclude-generated>\n"
    )

    assert _run_phacc_pinned_check("bump pytest from 1 to 2", "", path) == "blocked"
    assert (
        _run_phacc_pinned_check("bump hand-written-exclusion from 1 to 2", "", path)
        == "ok"
    )


def test_phacc_pinned_check_fails_loudly_without_a_readable_generated_list(
    tmp_path: Path,
) -> None:
    # Distinct from an unclassifiable PR, which fails open: this is the
    # repository's own file no longer matching the format
    # update_dependabot_exclude_list() writes. Returning "ok" there would
    # disable the check permanently and silently, bringing back exactly the
    # week-of-red-PRs symptom it exists to prevent.
    markerless = tmp_path / "dependabot.yml"
    markerless.write_text("version: 2\nupdates: []\n")

    for target in (tmp_path / "nowhere" / "dependabot.yml", markerless):
        result = subprocess.run(
            [PHACC_PINNED_CHECK, "bump pytest from 1 to 2", ""],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "DEPENDABOT_YML": str(target)},
        )
        assert result.returncode != 0, target
        assert "::error::" in result.stderr, target
        assert result.stdout.strip() == "", target


def test_daily_sync_keeps_running_but_stays_red_on_an_unreadable_exclude_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An unparseable generated block must not merge PRs past the check, and
    # must not take down the whole maintenance pass either -- rebases still
    # need to happen. The script's ::error:: is what keeps the run red.
    log = _install_fake_gh(tmp_path, monkeypatch)
    monkeypatch.setenv("MAX_WAIT_SECONDS", "0")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("DEPENDABOT_YML", str(tmp_path / "nowhere.yml"))
    monkeypatch.setenv(
        "GH_PRS",
        json.dumps(
            [
                {
                    "number": 1,
                    "author": {"login": "app/dependabot"},
                    "url": "https://example.test/pr/1",
                    "title": "chore(deps-dev): bump pytest from 9.0.0 to 9.0.3",
                    "body": "Bumps pytest from 9.0.0 to 9.0.3.",
                    "createdAt": "2026-01-01T00:00:00Z",
                    "autoMergeRequest": None,
                    "isDraft": False,
                },
            ]
        ),
    )

    subprocess.run([DAILY_SYNC], check=True)

    calls = log.read_text().splitlines()
    assert "pr update-branch 1 --repo owner/repo --rebase" in calls
    assert not any(call.startswith("pr close 1 ") for call in calls)


def _run_supersession_check(
    title: str, body: str, changed_files: list[str], cwd: Path
) -> str:
    result = subprocess.run(
        [SUPERSESSION_CHECK, title, body],
        input="\n".join(changed_files),
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_supersession_check_detects_a_pip_pr_already_superseded_on_master(
    tmp_path: Path,
) -> None:
    (tmp_path / "requirements_dev.lock.txt").write_text(
        "homeassistant==2026.7.2\nrequests==2.34.2\n"
    )

    result = _run_supersession_check(
        "chore(deps-dev): bump requests from 2.32.5 to 2.33.0",
        "Bumps requests from 2.32.5 to 2.33.0.",
        ["requirements_dev.lock.txt"],
        tmp_path,
    )

    assert result == "superseded"


def test_supersession_check_treats_a_pip_pr_still_ahead_of_master_as_current(
    tmp_path: Path,
) -> None:
    (tmp_path / "requirements_dev.lock.txt").write_text("requests==2.34.2\n")

    result = _run_supersession_check(
        "chore(deps-dev): bump requests from 2.32.5 to 2.35.0",
        "Bumps requests from 2.32.5 to 2.35.0.",
        ["requirements_dev.lock.txt"],
        tmp_path,
    )

    assert result == "current"


def test_supersession_check_is_superseded_if_any_one_of_several_files_meets_it(
    tmp_path: Path,
) -> None:
    # A package can be pinned independently across more than one lockfile
    # (e.g. requirements_dev.lock.txt vs requirements_dev_min.lock.txt),
    # each leg free to move on its own schedule. PR #145 (orjson 3.11.3 ->
    # 3.11.6) hit exactly this: by the time it was checked, the dev leg
    # had already moved past the proposal to 3.11.9 via homeassistant's
    # own exact transitive pin, while the min leg was still sitting at the
    # pre-PR 3.11.3 -- genuinely still an upgrade from that leg's own
    # point of view. One superseded leg is enough: the PR's single
    # version number was never going to apply across every leg at once.
    (tmp_path / "requirements_dev.lock.txt").write_text("orjson==3.11.9\n")
    (tmp_path / "requirements_dev_min.lock.txt").write_text("orjson==3.11.3\n")

    result = _run_supersession_check(
        "chore(deps-dev): bump orjson from 3.11.3 to 3.11.6",
        "Bumps orjson from 3.11.3 to 3.11.6.",
        ["requirements_dev.lock.txt", "requirements_dev_min.lock.txt"],
        tmp_path,
    )

    assert result == "superseded"


def test_supersession_check_detects_a_docker_pr_already_superseded_on_master(
    tmp_path: Path,
) -> None:
    docker_dir = tmp_path / "docker"
    docker_dir.mkdir()
    (docker_dir / "Dockerfile").write_text("FROM python:3.14.6-slim\n")

    result = _run_supersession_check(
        "chore(deps): bump python from 3.13.14-slim to 3.14.6-slim"
        " in /docker in the docker group across 1 directory",
        "Bumps the docker group with 1 update in the /docker directory: python.",
        ["docker/Dockerfile"],
        tmp_path,
    )

    assert result == "superseded"


def test_supersession_check_detects_a_github_action_pr_already_superseded(
    tmp_path: Path,
) -> None:
    workflows_dir = tmp_path / ".github" / "workflows"
    workflows_dir.mkdir(parents=True)
    (workflows_dir / "tests.yaml").write_text(
        '- uses: "actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5" # v4.3.1\n'
    )

    result = _run_supersession_check(
        "chore(deps): bump actions/checkout from 4.0.0 to 4.3.1",
        "Bumps actions/checkout from 4.0.0 to 4.3.1.",
        [".github/workflows/tests.yaml"],
        tmp_path,
    )

    assert result == "superseded"


def test_supersession_check_is_unknown_for_a_sha_only_action_with_no_tagged_release(
    tmp_path: Path,
) -> None:
    # home-assistant/actions/hassfest has no tagged releases -- Dependabot's
    # own body is SHA-to-SHA with no version anywhere, so there's no basis
    # to ever call this superseded. Must stay "unknown", never "current"
    # (which would misleadingly imply we checked and it's still needed).
    workflows_dir = tmp_path / ".github" / "workflows"
    workflows_dir.mkdir(parents=True)
    (workflows_dir / "tests.yaml").write_text(
        '- uses: "home-assistant/actions/hassfest'
        '@e3fb68ebda13d88a0d695082f471ba2c83d025fb"\n'
    )

    result = _run_supersession_check(
        "chore(deps): bump home-assistant/actions/hassfest from"
        " f4ca6f671bd429efb108c0f2fa0ae8af0215986c"
        " to e3fb68ebda13d88a0d695082f471ba2c83d025fb",
        "Bumps [home-assistant/actions/hassfest](x) from"
        " f4ca6f671bd429efb108c0f2fa0ae8af0215986c"
        " to e3fb68ebda13d88a0d695082f471ba2c83d025fb.",
        [".github/workflows/tests.yaml"],
        tmp_path,
    )

    assert result == "unknown"


def test_supersession_check_is_unknown_when_the_package_is_not_found_in_any_file(
    tmp_path: Path,
) -> None:
    (tmp_path / "requirements_dev.lock.txt").write_text("homeassistant==2026.7.2\n")

    result = _run_supersession_check(
        "chore(deps): bump nonexistent-pkg from 1.0.0 to 2.0.0",
        "Bumps nonexistent-pkg from 1.0.0 to 2.0.0.",
        ["requirements_dev.lock.txt"],
        tmp_path,
    )

    assert result == "unknown"


def test_supersession_check_requires_every_pair_in_a_group_to_be_superseded(
    tmp_path: Path,
) -> None:
    workflows_dir = tmp_path / ".github" / "workflows"
    workflows_dir.mkdir(parents=True)
    (workflows_dir / "tests.yaml").write_text(
        '- uses: "github/codeql-action/init'
        '@e4fba868fa4b1b91e1fdab776edc8cfbe6e9fb81" # v4.37.3\n'
        '- uses: "github/codeql-action/analyze'
        '@e4fba868fa4b1b91e1fdab776edc8cfbe6e9fb81" # v4.37.3\n'
    )
    body = (
        "Bumps the github-actions group with 2 updates.\n\n"
        "Updates `github/codeql-action/init` from 4.37.1 to 4.37.3\n"
        "Updates `github/codeql-action/analyze` from 4.37.1 to 5.0.0\n"
    )

    result = _run_supersession_check(
        "chore(deps): bump the github-actions group across 1 directory with 2 updates",
        body,
        [".github/workflows/tests.yaml"],
        tmp_path,
    )

    # codeql-action/analyze proposes 5.0.0, which master's 4.37.3 hasn't
    # reached -- the group as a whole is not superseded even though one of
    # its two members already is.
    assert result == "current"


def test_auto_merge_workflow_is_daily_schedule_only_and_runs_the_sync_script() -> None:
    text = AUTO_MERGE_WORKFLOW.read_text()

    assert "push:" not in text
    assert "workflow_run:" not in text
    assert "pull_request" not in text
    assert 'cron: "22 8 * * *"' in text
    assert "cancel-in-progress: false" in text
    assert "sync-open-prs:" in text
    assert "run: .github/scripts/daily-pr-sync.sh" in text


def test_stale_cleanup_never_overlaps_a_maintenance_run() -> None:
    # PR Auto-merge can be mid-rebase/mid-merge on a Dependabot PR when the
    # sweep fires. Cron spacing cannot order two independently delayed
    # schedules, so the shared concurrency group is what enforces it -- for
    # every trigger of either workflow, not just the scheduled ones.
    auto_merge_text = AUTO_MERGE_WORKFLOW.read_text()
    assert 'cron: "22 8 * * *"' in auto_merge_text
    assert "timeout-minutes: 55" in auto_merge_text

    stale_text = STALE_WORKFLOW.read_text()
    for text in (auto_merge_text, stale_text):
        assert "group: dependency-pr-maintenance" in text
        assert "cancel-in-progress: false" in text
        assert "queue: max" in text


def test_stale_cleanup_runs_on_a_plain_weekly_cron() -> None:
    # Previously triggered by workflow_run on PR Auto-merge's completion, which
    # made three separate things load-bearing for the sweep happening at all:
    # Monday's PR Auto-merge cron firing, that run being the scheduled one
    # rather than a dispatch, and its concluding success. Any one miss cost a
    # full week. The concurrency group above already guarantees the ordering
    # workflow_run was there for, so the trigger only added failure modes.
    text = STALE_WORKFLOW.read_text()

    assert 'cron: "30 9 * * 1"' in text
    assert "workflow_run:" not in text
    # "Monday" must be encoded once, in the cron -- not also as a date +%u
    # comparison against a source run's start time. Checked against the shell
    # body rather than the whole file, which explains the change in prose.
    shell_body = text.split("        run: |", 1)[1]
    assert "SOURCE_RUN_STARTED_AT" not in shell_body
    assert "+%u" not in shell_body


def test_automation_pr_push_leases_against_a_freshly_fetched_ref() -> None:
    # daily-pr-sync.sh rebases every open PR, including the automation/*
    # branches the two producers push. A bare --force-with-lease leases against
    # the remote-tracking ref actions/checkout recorded minutes earlier, so
    # that rebase broke the lease and failed the producer outright -- on
    # Mondays their crons are five and eight minutes from PR Auto-merge's.
    # Fixed at the push rather than by serializing the workflows: serializing
    # would not even close it, since gh pr update-branch applies the rebase
    # asynchronously and it can land after the lock releases.
    text = CREATE_OR_UPDATE_PR_ACTION.read_text()

    assert 'git fetch --no-tags origin "$BRANCH"' in text
    assert 'lease="$BRANCH:$(git rev-parse FETCH_HEAD)"' in text
    # Empty <expect> is git's "this ref must not exist yet", the correct lease
    # the first time a branch is published.
    assert 'lease="$BRANCH:"' in text
    assert 'git push --force-with-lease="$lease" origin "$BRANCH"' in text
    assert "git push --force-with-lease origin" not in text


def test_producers_are_not_serialized_behind_the_maintenance_run() -> None:
    # They push disjoint branches and do not interact, so with the lease race
    # fixed above there is nothing left to serialize -- joining the
    # maintenance group would only delay the weekly refresh by up to the
    # 55-minute ceiling of a run it has no conflict with.
    for workflow_name in ("dependency-updates.yaml", "min-ha-version-update.yaml"):
        text = (WORKFLOWS / workflow_name).read_text()
        assert "group: dependency-pr-maintenance" not in text, workflow_name


def test_auto_merged_dependency_pr_token_cannot_write_workflow_files() -> None:
    # dependency-updates.yaml is the only automation PR that auto-merges, so
    # it is the only token that could land a change without a human looking at
    # it. `make update-deps` writes no file under .github/workflows/ -- only
    # --min-ha-version-only does, via update_tests_workflow_min_python().
    dependency_updates = (WORKFLOWS / "dependency-updates.yaml").read_text()
    min_ha_version = (WORKFLOWS / "min-ha-version-update.yaml").read_text()

    # Matched as an indented mapping key, not a substring: both files also
    # mention the grant in prose explaining its presence or absence.
    grant = "\n          permission-workflows: write\n"
    assert "--auto --squash" in dependency_updates
    assert grant not in dependency_updates
    assert grant in min_ha_version
    assert "--auto" not in min_ha_version


def test_stale_cleanup_closes_every_recyclable_pr_with_no_leave_open_bucket() -> None:
    text = STALE_WORKFLOW.read_text()

    assert "age_days" in text
    assert 'if [ "$age_days" -lt 7 ]; then' in text
    assert "createdAt,updatedAt" not in text
    assert "updated_hours_ago" not in text
    assert "automation-needs-attention" not in text
    # No leave-open bucket survives: every recyclable PR is closed, none
    # are merely commented-on-and-left-open.
    assert "needs_attention" not in text
    assert "gh pr comment" not in text

    is_recyclable = text.index("is_recyclable=false")
    not_recyclable_guard = text.index(
        'if [ "$is_recyclable" != "true" ]', is_recyclable
    )
    stale_guard = text.index('if [ "$age_days" -lt 7 ]; then', not_recyclable_guard)
    close_dependabot = text.index('if [ "$is_dependabot" = "true" ]; then', stale_guard)
    close_min_ha = text.index(
        'elif [ "$head_ref" = "automation/min-ha-version-update" ]; then',
        close_dependabot,
    )
    close_pr = text.index('gh pr close "$number"', close_min_ha)
    assert (
        is_recyclable
        < not_recyclable_guard
        < stale_guard
        < close_dependabot
        < close_min_ha
        < close_pr
    )


def test_stale_cleanup_treats_all_three_pr_kinds_as_recyclable() -> None:
    text = STALE_WORKFLOW.read_text()

    # dependency-updates auto-merges on its own (stale means CI is blocked),
    # min-ha-version-update is recreated weekly regardless of whether a
    # previous instance is open (closing costs nothing), and most Dependabot
    # PRs here can never resolve as opened (see dependabot.yml) — all three
    # are closed after 7 days rather than left open, except a confirmed
    # major-version Dependabot bump (see the exemption test below).
    recyclable_block = text[
        text.index("is_recyclable=false") : text.index(
            'if [ "$is_recyclable" != "true" ]'
        )
    ]
    assert "automation/dependency-updates" in recyclable_block
    assert "automation/min-ha-version-update" in recyclable_block
    assert "is_dependabot" in recyclable_block


def test_stale_cleanup_exempts_confirmed_major_dependabot_bumps() -> None:
    # Closing a PR only blocks Dependabot from recreating that *exact*
    # proposed version -- not the dependency, and not any newer release
    # under the same major. A dependency with a sparse release cadence could
    # have its one "a major bump is pending" signal closed here and then
    # never resurface, since nothing would trigger Dependabot to re-propose
    # it. This doesn't trade away cleanup: daily-pr-sync.sh already runs a
    # supersession check against every individual/major Dependabot PR daily
    # and closes it for real once the base branch resolves the update some
    # other way -- this job's age-based sweep was never the only thing
    # capable of closing a genuinely-resolved major.
    text = STALE_WORKFLOW.read_text()

    assert "actions/checkout@" in text
    assert ".github/scripts/dependabot-bump-kind.sh" in text

    recyclable_block = text[
        text.index("is_recyclable=false") : text.index(
            'if [ "$is_recyclable" != "true" ]'
        )
    ]
    assert "is_major_bump" in recyclable_block
    assert '[ "$is_major_bump" != "true" ]' in recyclable_block


def test_daily_sync_rebases_every_open_pr_and_merges_each_eligible_group_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = _install_fake_gh(tmp_path, monkeypatch)
    # No real waiting in tests: any check state that isn't immediately
    # terminal (pass/fail) should resolve to "timeout" on the first poll.
    monkeypatch.setenv("MAX_WAIT_SECONDS", "0")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "0")

    group_body = (
        "Bumps the docker group with 1 update: python.\n\n"
        "Updates `python` from 3.13 to 3.14\n"
    )
    monkeypatch.setenv(
        "GH_PRS",
        json.dumps(
            [
                {
                    "number": 1,
                    "author": {"login": "app/dependabot"},
                    "url": "https://example.test/pr/1",
                    "title": "chore(deps): bump the docker group",
                    "body": group_body,
                    "createdAt": "2026-01-01T00:00:00Z",
                    "autoMergeRequest": None,
                    "isDraft": False,
                },
                {
                    "number": 2,
                    "author": {"login": "app/dependabot"},
                    "url": "https://example.test/pr/2",
                    "title": "chore(deps): bump urllib3 from 1.0.0 to 2.0.0",
                    "body": "Bumps urllib3 from 1.0.0 to 2.0.0.",
                    "createdAt": "2026-01-02T00:00:00Z",
                    "autoMergeRequest": None,
                    "isDraft": False,
                },
                {
                    "number": 3,
                    "author": {"login": "app/dependabot"},
                    "url": "https://example.test/pr/3",
                    "title": "chore(deps): bump the actions group",
                    "body": group_body,
                    "createdAt": "2026-01-03T00:00:00Z",
                    "autoMergeRequest": {"enabledAt": "2026-01-03T00:05:00Z"},
                    "isDraft": False,
                },
                {
                    "number": 4,
                    "author": {"login": "app/dependabot"},
                    "url": "https://example.test/pr/4",
                    "title": "chore(deps): bump another group",
                    "body": group_body,
                    "createdAt": "2026-01-04T00:00:00Z",
                    "autoMergeRequest": None,
                    "isDraft": False,
                },
                {
                    "number": 5,
                    "author": {"login": "octocat"},
                    "url": "https://example.test/pr/5",
                    "title": "Add a new sensor",
                    "body": "A human-authored feature PR.",
                    "createdAt": "2026-01-05T00:00:00Z",
                    "autoMergeRequest": {"enabledAt": "2026-01-05T00:05:00Z"},
                    "isDraft": False,
                },
                {
                    "number": 6,
                    "author": {"login": "octocat"},
                    "url": "https://example.test/pr/6",
                    "title": "WIP: draft feature",
                    "body": "Not ready yet.",
                    "createdAt": "2026-01-06T00:00:00Z",
                    "autoMergeRequest": None,
                    "isDraft": True,
                },
            ]
        ),
    )
    # Branch protection requires one context, "ci" -- fetched dynamically by
    # the script, not hardcoded, so tests exercise that same lookup.
    monkeypatch.setenv("GH_REQUIRED_CONTEXTS", '["ci"]')
    # PR #1: required check already failed on its (default "sha1") head
    # commit -- rebase only.
    monkeypatch.setenv(
        "GH_CHECKRUNS_sha1",
        '[{"name":"ci","status":"completed","conclusion":"failure"}]',
    )
    # PR #3 and #4 both pass on their own head commits -- both should merge,
    # in order, in this one run.
    monkeypatch.setenv(
        "GH_CHECKRUNS_sha3",
        '[{"name":"ci","status":"completed","conclusion":"success"}]',
    )
    monkeypatch.setenv(
        "GH_CHECKRUNS_sha4",
        '[{"name":"ci","status":"completed","conclusion":"success"}]',
    )
    # PR #2 is a single-dependency update and is never checked at all.

    subprocess.run([DAILY_SYNC], check=True)

    calls = log.read_text().splitlines()

    # Every non-draft PR gets rebased, Dependabot and human alike, including
    # ones that can never be merged automatically. #2 (an individual major
    # bump, not superseded here since no changed-files fixture was given)
    # is checked for supersession but left alone -- see the dedicated
    # supersession tests below for the close-when-superseded path, and the
    # dedicated eligibility test below for individual non-major PRs (like
    # the real PR #149) that now *are* merge-eligible.
    for number in (1, 2, 3, 4, 5):
        assert f"pr update-branch {number} --repo owner/repo --rebase" in calls
    assert "pr diff 2 --repo owner/repo --name-only" in calls
    assert not any(call.startswith("pr close 2 ") for call in calls)

    # The draft PR is skipped entirely.
    assert not any("update-branch 6" in call for call in calls)
    assert not any("/pr/6" in call for call in calls)

    # #3's own (Dependabot-set) auto-merge is reset before re-evaluation.
    assert "pr merge 3 --repo owner/repo --disable-auto" in calls

    # Both eligible, passing grouped PRs are merged for real (not gh's async
    # --auto) -- #3 before #4, matching processing order -- while the
    # failing (#1), individual (#2), and human (#5) PRs are never merged.
    squash_calls = [call for call in calls if call.endswith("--squash --delete-branch")]
    assert squash_calls == [
        "pr merge https://example.test/pr/3 --repo owner/repo --squash --delete-branch",
        "pr merge https://example.test/pr/4 --repo owner/repo --squash --delete-branch",
    ]

    # A human PR's own auto-merge choice is never touched, and it never
    # gets merged by this workflow.
    assert "pr merge 5 --repo owner/repo --disable-auto" not in calls
    assert not any(
        "pr/5 --repo owner/repo --squash --delete-branch" in call for call in calls
    )


def test_daily_sync_merges_individual_non_major_prs_not_just_grouped_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Real PR #149 (home-assistant/actions/hassfest, SHA-only, no tagged
    # version -- bump_kind "unknown") was fully green and mergeable but
    # never got merged, because eligibility used to require pr_kind ==
    # "group". Grouping is Dependabot's own packaging choice, not a safety
    # signal -- only a real major-version bump should require a human.
    log = _install_fake_gh(tmp_path, monkeypatch)
    monkeypatch.setenv("MAX_WAIT_SECONDS", "0")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("GH_REQUIRED_CONTEXTS", '["ci"]')

    monkeypatch.setenv(
        "GH_PRS",
        json.dumps(
            [
                {
                    "number": 1,
                    "author": {"login": "app/dependabot"},
                    "url": "https://example.test/pr/1",
                    "title": (
                        "chore(deps): bump home-assistant/actions/hassfest"
                        " from f4ca6f6 to e3fb68e"
                    ),
                    "body": (
                        "Bumps [home-assistant/actions/hassfest](x)"
                        " from f4ca6f671bd429efb108c0f2fa0ae8af0215986c"
                        " to e3fb68ebda13d88a0d695082f471ba2c83d025fb."
                    ),
                    "createdAt": "2026-01-01T00:00:00Z",
                    "autoMergeRequest": None,
                    "isDraft": False,
                },
                {
                    "number": 2,
                    "author": {"login": "app/dependabot"},
                    "url": "https://example.test/pr/2",
                    "title": "chore(deps-dev): bump requests from 2.32.5 to 2.33.0",
                    "body": "Bumps requests from 2.32.5 to 2.33.0.",
                    "createdAt": "2026-01-02T00:00:00Z",
                    "autoMergeRequest": None,
                    "isDraft": False,
                },
            ]
        ),
    )
    checkrun_pass = '[{"name":"ci","status":"completed","conclusion":"success"}]'
    monkeypatch.setenv("GH_CHECKRUNS_sha1", checkrun_pass)
    monkeypatch.setenv("GH_CHECKRUNS_sha2", checkrun_pass)

    subprocess.run([DAILY_SYNC], check=True)

    calls = log.read_text().splitlines()
    squash_calls = {call for call in calls if call.endswith("--squash --delete-branch")}
    assert squash_calls == {
        "pr merge https://example.test/pr/1 --repo owner/repo --squash --delete-branch",
        "pr merge https://example.test/pr/2 --repo owner/repo --squash --delete-branch",
    }


def test_daily_sync_closes_an_individual_pr_confirmed_superseded_on_master(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = _install_fake_gh(tmp_path, monkeypatch)
    monkeypatch.setenv("MAX_WAIT_SECONDS", "0")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "0")

    # requirements_dev.lock.txt already pins requests past what PR #1
    # proposes -- dependabot-supersession-check.sh reads it straight from
    # the working tree daily-pr-sync.sh runs in, exactly as it would from
    # a real checkout.
    (tmp_path / "requirements_dev.lock.txt").write_text("requests==2.34.2\n")

    monkeypatch.setenv(
        "GH_PRS",
        json.dumps(
            [
                {
                    "number": 1,
                    "author": {"login": "app/dependabot"},
                    "url": "https://example.test/pr/1",
                    "title": "chore(deps-dev): bump requests from 2.32.5 to 2.33.0",
                    "body": "Bumps requests from 2.32.5 to 2.33.0.",
                    "createdAt": "2026-01-01T00:00:00Z",
                    "autoMergeRequest": None,
                    "isDraft": False,
                },
            ]
        ),
    )
    monkeypatch.setenv("GH_CHANGED_FILES_1", "requirements_dev.lock.txt")

    subprocess.run([DAILY_SYNC], check=True, cwd=tmp_path)

    calls = log.read_text().splitlines()
    assert "pr update-branch 1 --repo owner/repo --rebase" in calls
    assert "pr diff 1 --repo owner/repo --name-only" in calls
    assert any(call.startswith("pr close 1 ") for call in calls)
    assert not any(call.endswith("--squash --delete-branch") for call in calls)


def test_daily_sync_closes_a_pr_bumping_a_package_phacc_pins_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # _install_fake_gh's fixture list contains "pytest". Such a PR must be
    # closed on sight: it cannot resolve at any version, so rebasing it and
    # then waiting MAX_WAIT_SECONDS for checks that will certainly fail only
    # spends the run budget that later, mergeable PRs need.
    log = _install_fake_gh(tmp_path, monkeypatch)
    monkeypatch.setenv("MAX_WAIT_SECONDS", "0")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "0")

    monkeypatch.setenv(
        "GH_PRS",
        json.dumps(
            [
                {
                    "number": 1,
                    "author": {"login": "app/dependabot"},
                    "url": "https://example.test/pr/1",
                    "title": "chore(deps-dev): bump pytest from 9.0.0 to 9.0.3",
                    "body": "Bumps pytest from 9.0.0 to 9.0.3.",
                    "createdAt": "2026-01-01T00:00:00Z",
                    "autoMergeRequest": None,
                    "isDraft": False,
                },
            ]
        ),
    )

    subprocess.run([DAILY_SYNC], check=True)

    calls = log.read_text().splitlines()
    # Still rebased first: every open PR is, regardless of what happens next.
    assert "pr update-branch 1 --repo owner/repo --rebase" in calls
    assert any(call.startswith("pr close 1 ") for call in calls)
    # Closed before the supersession check, which costs an extra API call and
    # could only ever reach the same "do not merge" conclusion.
    assert not any(call.startswith("pr diff 1 ") for call in calls)
    assert not any(call.endswith("--squash --delete-branch") for call in calls)
    # The close comment must name the real fix path, not just refuse.
    close_call = next(call for call in calls if call.startswith("pr close 1 "))
    assert "scripts/update_dependencies.py" in close_call


def test_daily_sync_waits_for_the_rebase_to_land_before_reading_head_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `gh pr update-branch` returns once GitHub accepts the request; the
    # rebase lands asynchronously. A BEHIND read straight afterwards means the
    # PR still points at its pre-rebase commit -- whose required checks are
    # already green from before the rebase, so trusting it would merge a
    # commit never tested against the current base.
    log = _install_fake_gh(tmp_path, monkeypatch)
    monkeypatch.setenv("MAX_WAIT_SECONDS", "0")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("REBASE_SETTLE_SECONDS", "0")
    monkeypatch.setenv("REBASE_SETTLE_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("GH_REQUIRED_CONTEXTS", '["ci"]')
    monkeypatch.setenv(
        "GH_CHECKRUNS_sha1",
        '[{"name":"ci","status":"completed","conclusion":"success"}]',
    )
    # GitHub is still computing mergeability -- indistinguishable from
    # "the rebase has not landed", and equally not a basis for merging.
    monkeypatch.setenv("GH_MERGE_STATE_1", "UNKNOWN")

    monkeypatch.setenv(
        "GH_PRS",
        json.dumps(
            [
                {
                    "number": 1,
                    "author": {"login": "app/dependabot"},
                    "url": "https://example.test/pr/1",
                    "title": "chore(deps): bump the docker group",
                    "body": (
                        "Bumps the docker group with 1 update: python.\n\n"
                        "Updates `python` from 3.13 to 3.14\n"
                    ),
                    "createdAt": "2026-01-01T00:00:00Z",
                    "autoMergeRequest": None,
                    "isDraft": False,
                },
            ]
        ),
    )

    subprocess.run([DAILY_SYNC], check=True)

    calls = log.read_text().splitlines()
    assert "pr update-branch 1 --repo owner/repo --rebase" in calls
    assert not any(call.endswith("--squash --delete-branch") for call in calls)
    # Never even asked which commit to gate on: the branch state was not a
    # basis for merging in the first place.
    assert not any("headRefOid" in call for call in calls)


def test_daily_sync_reads_every_page_of_check_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # This endpoint returns 30 per page by default, and one commit here
    # already carries ~17 across tests.yaml/hacs.yaml/hassfest.yaml. Two
    # re-runs of Tests push the required contexts off page one, where they
    # would match nothing, classify as "pending", and leave every eligible PR
    # spinning to MAX_WAIT_SECONDS with no error anywhere. Asserted against
    # the call the fake gh actually recorded rather than the script's source
    # text, so reflowing the invocation doesn't fail this.
    log = _install_fake_gh(tmp_path, monkeypatch)
    monkeypatch.setenv("MAX_WAIT_SECONDS", "0")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("GH_REQUIRED_CONTEXTS", '["ci"]')
    monkeypatch.setenv(
        "GH_CHECKRUNS_sha1",
        '[{"name":"ci","status":"completed","conclusion":"success"}]',
    )
    monkeypatch.setenv(
        "GH_PRS",
        json.dumps(
            [
                {
                    "number": 1,
                    "author": {"login": "app/dependabot"},
                    "url": "https://example.test/pr/1",
                    "title": "chore(deps): bump the docker group",
                    "body": (
                        "Bumps the docker group with 1 update: python.\n\n"
                        "Updates `python` from 3.13 to 3.14\n"
                    ),
                    "createdAt": "2026-01-01T00:00:00Z",
                    "autoMergeRequest": None,
                    "isDraft": False,
                },
            ]
        ),
    )

    subprocess.run([DAILY_SYNC], check=True)

    calls = log.read_text().splitlines()
    assert any(
        "check-runs?per_page=100" in call and "--paginate" in call for call in calls
    )


def test_daily_sync_merges_when_a_required_context_has_duplicate_check_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Observed for real on PR #148: GitHub reported two check-run entries
    # named "tests-finished" for the same commit (both successful), which
    # made a naive "entry count == required-context count" comparison never
    # equal, looping until timeout even though every required context had
    # actually passed. Classification must be per-name ("does this required
    # context have at least one successful run"), not per-entry-count.
    log = _install_fake_gh(tmp_path, monkeypatch)
    monkeypatch.setenv("MAX_WAIT_SECONDS", "0")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "0")

    group_body = (
        "Bumps the docker group with 1 update: python.\n\n"
        "Updates `python` from 3.13 to 3.14\n"
    )
    monkeypatch.setenv(
        "GH_PRS",
        json.dumps(
            [
                {
                    "number": 1,
                    "author": {"login": "app/dependabot"},
                    "url": "https://example.test/pr/1",
                    "title": "chore(deps): bump the docker group",
                    "body": group_body,
                    "createdAt": "2026-01-01T00:00:00Z",
                    "autoMergeRequest": None,
                    "isDraft": False,
                },
            ]
        ),
    )
    monkeypatch.setenv(
        "GH_REQUIRED_CONTEXTS", '["HACS Action","validate","tests-finished"]'
    )
    monkeypatch.setenv(
        "GH_CHECKRUNS_sha1",
        json.dumps(
            [
                {
                    "name": "tests-finished",
                    "status": "completed",
                    "conclusion": "success",
                },
                {
                    "name": "tests-finished",
                    "status": "completed",
                    "conclusion": "success",
                },
                {"name": "HACS Action", "status": "completed", "conclusion": "success"},
                {"name": "validate", "status": "completed", "conclusion": "success"},
            ]
        ),
    )

    subprocess.run([DAILY_SYNC], check=True)

    calls = log.read_text().splitlines()
    assert "pr update-branch 1 --repo owner/repo --rebase" in calls
    assert (
        "pr merge https://example.test/pr/1 --repo owner/repo --squash --delete-branch"
        in calls
    )


def test_daily_sync_never_merges_on_malformed_check_runs_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Observed for real: `gh api .../check-runs` returned two concatenated
    # JSON arrays back to back ("[...][...]") instead of one, which failed
    # `jq -n --argjson`. That failure was silently swallowed by the `!`
    # negation guarding the classification result and misread as "no
    # pending entries found", i.e. a false "pass" that went on to attempt
    # (and only failed to actually merge because branch protection
    # separately rejected it) a merge nothing had verified was safe.
    # Malformed check-run output must never resolve to anything but
    # "not yet complete".
    log = _install_fake_gh(tmp_path, monkeypatch)
    monkeypatch.setenv("MAX_WAIT_SECONDS", "0")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "0")

    group_body = (
        "Bumps the docker group with 1 update: python.\n\n"
        "Updates `python` from 3.13 to 3.14\n"
    )
    monkeypatch.setenv(
        "GH_PRS",
        json.dumps(
            [
                {
                    "number": 1,
                    "author": {"login": "app/dependabot"},
                    "url": "https://example.test/pr/1",
                    "title": "chore(deps): bump the docker group",
                    "body": group_body,
                    "createdAt": "2026-01-01T00:00:00Z",
                    "autoMergeRequest": None,
                    "isDraft": False,
                },
            ]
        ),
    )
    monkeypatch.setenv("GH_REQUIRED_CONTEXTS", '["ci"]')
    one_pass = '[{"name":"ci","status":"completed","conclusion":"success"}]'
    monkeypatch.setenv("GH_CHECKRUNS_sha1", one_pass + one_pass)

    subprocess.run([DAILY_SYNC], check=True)

    calls = log.read_text().splitlines()
    assert "pr update-branch 1 --repo owner/repo --rebase" in calls
    assert not any(call.endswith("--squash --delete-branch") for call in calls)


@pytest.mark.parametrize(
    ("env_name", "env_value"),
    [
        # The real production failure: the automation App lacked
        # "Administration: read", so this endpoint 403'd. `gh api` writes an
        # error response's body to stdout while exiting non-zero, so the old
        # `2>/dev/null || echo '[]'` fallback yielded one variable holding two
        # concatenated JSON documents -- which read either as "nothing is
        # required" (merging unverified) or broke every downstream jq call
        # (merging nothing, after burning the run budget on timeouts).
        ("GH_CONTEXTS_ERROR", '{"message":"Resource not accessible by integration"}'),
        # An empty list is indistinguishable from "no checks are required",
        # which would merge every PR without verifying anything.
        ("GH_REQUIRED_CONTEXTS", "[]"),
        # Anything that is not a JSON array cannot be classified at all.
        ("GH_REQUIRED_CONTEXTS", "not json"),
    ],
)
def test_daily_sync_refuses_to_run_without_a_usable_required_context_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_name: str, env_value: str
) -> None:
    log = _install_fake_gh(tmp_path, monkeypatch)
    monkeypatch.setenv("MAX_WAIT_SECONDS", "0")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "0")
    monkeypatch.setenv(env_name, env_value)

    monkeypatch.setenv(
        "GH_PRS",
        json.dumps(
            [
                {
                    "number": 1,
                    "author": {"login": "app/dependabot"},
                    "url": "https://example.test/pr/1",
                    "title": "chore(deps): bump the docker group",
                    "body": "Bumps the docker group with 1 update: python.\n",
                    "createdAt": "2026-01-01T00:00:00Z",
                    "autoMergeRequest": None,
                    "isDraft": False,
                },
            ]
        ),
    )
    monkeypatch.setenv(
        "GH_CHECKRUNS_sha1",
        '[{"name":"ci","status":"completed","conclusion":"success"}]',
    )

    result = subprocess.run([DAILY_SYNC], capture_output=True, text=True)

    # Fails closed and loudly, before touching a single PR.
    assert result.returncode != 0
    assert "Administration: read" in result.stdout
    calls = log.read_text().splitlines()
    assert not any(call.startswith("pr list") for call in calls)
    assert not any("--squash" in call for call in calls)


def test_daily_sync_skips_merge_when_branch_is_dirty_or_behind_after_rebase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = _install_fake_gh(tmp_path, monkeypatch)
    monkeypatch.setenv("MAX_WAIT_SECONDS", "0")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "0")

    group_body = (
        "Bumps the docker group with 1 update: python.\n\n"
        "Updates `python` from 3.13 to 3.14\n"
    )
    monkeypatch.setenv(
        "GH_PRS",
        json.dumps(
            [
                {
                    "number": 1,
                    "author": {"login": "app/dependabot"},
                    "url": "https://example.test/pr/1",
                    "title": "chore(deps): bump the docker group",
                    "body": group_body,
                    "createdAt": "2026-01-01T00:00:00Z",
                    "autoMergeRequest": None,
                    "isDraft": False,
                },
            ]
        ),
    )
    # Even though required checks would pass, a post-rebase merge state of
    # DIRTY must still block the merge -- the check-wait step never even
    # gets to look at GH_CHECKRUNS_sha1 in that case.
    monkeypatch.setenv("GH_REQUIRED_CONTEXTS", '["ci"]')
    monkeypatch.setenv("GH_MERGE_STATE_1", "DIRTY")
    monkeypatch.setenv(
        "GH_CHECKRUNS_sha1",
        '[{"name":"ci","status":"completed","conclusion":"success"}]',
    )

    subprocess.run([DAILY_SYNC], check=True)

    calls = log.read_text().splitlines()
    assert "pr update-branch 1 --repo owner/repo --rebase" in calls
    assert not any(call.endswith("--squash --delete-branch") for call in calls)


def test_daily_sync_stops_merging_once_the_run_budget_is_spent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = _install_fake_gh(tmp_path, monkeypatch)
    monkeypatch.setenv("MAX_WAIT_SECONDS", "0")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "0")
    # An already-exhausted run budget must stop new merges even for a PR
    # that would otherwise pass every other check.
    monkeypatch.setenv("RUN_BUDGET_SECONDS", "0")

    group_body = (
        "Bumps the docker group with 1 update: python.\n\n"
        "Updates `python` from 3.13 to 3.14\n"
    )
    monkeypatch.setenv(
        "GH_PRS",
        json.dumps(
            [
                {
                    "number": 1,
                    "author": {"login": "app/dependabot"},
                    "url": "https://example.test/pr/1",
                    "title": "chore(deps): bump the docker group",
                    "body": group_body,
                    "createdAt": "2026-01-01T00:00:00Z",
                    "autoMergeRequest": None,
                    "isDraft": False,
                },
            ]
        ),
    )
    monkeypatch.setenv("GH_REQUIRED_CONTEXTS", '["ci"]')
    monkeypatch.setenv(
        "GH_CHECKRUNS_sha1",
        '[{"name":"ci","status":"completed","conclusion":"success"}]',
    )

    subprocess.run([DAILY_SYNC], check=True)

    calls = log.read_text().splitlines()
    assert "pr update-branch 1 --repo owner/repo --rebase" in calls
    assert not any(call.endswith("--squash --delete-branch") for call in calls)
