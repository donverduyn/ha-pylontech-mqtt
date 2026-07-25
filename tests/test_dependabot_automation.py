from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
PR_KIND = ROOT / ".github" / "scripts" / "dependabot-pr-kind.sh"
SUPERSESSION_CHECK = ROOT / ".github" / "scripts" / "dependabot-supersession-check.sh"
DAILY_SYNC = ROOT / ".github" / "scripts" / "daily-pr-sync.sh"
AUTO_MERGE_WORKFLOW = ROOT / ".github" / "workflows" / "dependabot-auto-merge.yaml"

STALE_WORKFLOW = ROOT / ".github" / "workflows" / "close-stale-automation-prs.yaml"

FAKE_GH = r"""#!/bin/bash
set -euo pipefail
printf '%s\n' "$*" >>"$GH_LOG"

if [ "$1" = "api" ]; then
  case "$2" in
    */protection/required_status_checks/contexts)
      printenv GH_REQUIRED_CONTEXTS || printf '[]\n'
      ;;
    */commits/*/check-runs)
      sha="${2##*/commits/}"
      sha="${sha%/check-runs}"
      checkruns_var="GH_CHECKRUNS_$sha"
      printenv "$checkruns_var" || printf '[]\n'
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


def test_stale_cleanup_labels_but_never_closes_dependabot() -> None:
    text = STALE_WORKFLOW.read_text()

    assert "age_days" in text
    assert 'if [ "$age_days" -lt 7 ]; then' in text
    assert "createdAt,updatedAt" not in text
    assert "updated_hours_ago" not in text
    assert "automation-needs-attention" in text

    needs_attention_policy = text.index('if [ "$needs_attention" = "true" ]; then')
    leave_open = text.index("continue", needs_attention_policy)
    close_own_automation = text.index('gh pr close "$number"', leave_open)
    assert needs_attention_policy < leave_open < close_own_automation


def test_stale_cleanup_leaves_min_ha_version_open_but_recycles_dependency_updates() -> (
    None
):
    text = STALE_WORKFLOW.read_text()

    # min-ha-version-update never auto-merges (a maintainer decision, not a
    # mechanical bump — see min-ha-version-update.yaml), so it must join
    # Dependabot's needs_attention/labelled-and-left-open path rather than
    # the is_recyclable/closed-and-recreated path that dependency-updates
    # (which does auto-merge) uses.
    needs_attention_block = text[
        text.index("needs_attention=false") : text.index("is_recyclable=false")
    ]
    assert "automation/min-ha-version-update" in needs_attention_block

    recyclable_block = text[
        text.index("is_recyclable=false") : text.index(
            'if [ "$needs_attention" != "true" ]'
        )
    ]
    assert "automation/dependency-updates" in recyclable_block
    assert "automation/min-ha-version-update" not in recyclable_block


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
    squash_calls = [call for call in calls if call.endswith("--squash")]
    assert squash_calls == [
        "pr merge https://example.test/pr/3 --repo owner/repo --squash",
        "pr merge https://example.test/pr/4 --repo owner/repo --squash",
    ]

    # A human PR's own auto-merge choice is never touched, and it never
    # gets merged by this workflow.
    assert "pr merge 5 --repo owner/repo --disable-auto" not in calls
    assert not any("pr/5 --repo owner/repo --squash" in call for call in calls)


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
    squash_calls = {call for call in calls if call.endswith("--squash")}
    assert squash_calls == {
        "pr merge https://example.test/pr/1 --repo owner/repo --squash",
        "pr merge https://example.test/pr/2 --repo owner/repo --squash",
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
    assert not any(call.endswith("--squash") for call in calls)


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
    assert "pr merge https://example.test/pr/1 --repo owner/repo --squash" in calls


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
    assert not any(call.endswith("--squash") for call in calls)


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
    assert not any(call.endswith("--squash") for call in calls)
