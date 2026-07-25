from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
PR_KIND = ROOT / ".github" / "scripts" / "dependabot-pr-kind.sh"
DAILY_SYNC = ROOT / ".github" / "scripts" / "daily-pr-sync.sh"
AUTO_MERGE_WORKFLOW = ROOT / ".github" / "workflows" / "dependabot-auto-merge.yaml"

STALE_WORKFLOW = ROOT / ".github" / "workflows" / "close-stale-automation-prs.yaml"

FAKE_GH = r"""#!/bin/bash
set -euo pipefail
printf '%s\n' "$*" >>"$GH_LOG"

case "$1 $2" in
  "pr list")
    printf '%s\n' "$GH_PRS"
    ;;
  "pr checks")
    checks_var="GH_CHECKS_$3"
    printenv "$checks_var" || printf '[]\n'
    ;;
  "pr update-branch")
    echo "updated"
    ;;
  "pr view")
    state_var="GH_MERGE_STATE_$3"
    printenv "$state_var" || echo "CLEAN"
    ;;
  "pr merge")
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
                    "title": "chore(deps): bump urllib3 from 2.0.0 to 2.0.1",
                    "body": "Bumps urllib3 from 2.0.0 to 2.0.1.",
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
    # PR #1: required checks already failed -- rebase only.
    monkeypatch.setenv("GH_CHECKS_1", '[{"bucket":"fail"}]')
    # PR #3 and #4 both pass -- both should merge, in order, in this one run.
    monkeypatch.setenv("GH_CHECKS_3", '[{"bucket":"pass"}]')
    monkeypatch.setenv("GH_CHECKS_4", '[{"bucket":"pass"}]')
    # PR #2 is a single-dependency update and is never checked at all.

    subprocess.run([DAILY_SYNC], check=True)

    calls = log.read_text().splitlines()

    # Every non-draft PR gets rebased, Dependabot and human alike, including
    # ones that can never be merged automatically.
    for number in (1, 2, 3, 4, 5):
        assert f"pr update-branch {number} --repo owner/repo --rebase" in calls

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
    # gets to look at GH_CHECKS_1 in that case.
    monkeypatch.setenv("GH_MERGE_STATE_1", "DIRTY")
    monkeypatch.setenv("GH_CHECKS_1", '[{"bucket":"pass"}]')

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
    monkeypatch.setenv("GH_CHECKS_1", '[{"bucket":"pass"}]')

    subprocess.run([DAILY_SYNC], check=True)

    calls = log.read_text().splitlines()
    assert "pr update-branch 1 --repo owner/repo --rebase" in calls
    assert not any(call.endswith("--squash") for call in calls)
