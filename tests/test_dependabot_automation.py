from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
PR_KIND = ROOT / ".github" / "scripts" / "dependabot-pr-kind.sh"
QUEUE_GROUP = ROOT / ".github" / "scripts" / "queue-dependabot-group.sh"
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
    echo "CLEAN"
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


def test_auto_merge_workflow_is_schedule_only_and_runs_the_group_queue() -> None:
    text = AUTO_MERGE_WORKFLOW.read_text()

    assert "push:" not in text
    assert "workflow_run:" not in text
    assert "pull_request" not in text
    assert "schedule:" in text
    assert "cancel-in-progress: false" in text
    assert "queue-dependabot-group:" in text
    assert "run: .github/scripts/queue-dependabot-group.sh" in text


def test_stale_cleanup_labels_but_never_closes_dependabot() -> None:
    text = STALE_WORKFLOW.read_text()

    assert "age_days" in text
    assert 'if [ "$age_days" -lt 7 ]; then' in text
    assert "createdAt,updatedAt" not in text
    assert "updated_hours_ago" not in text
    assert "automation-needs-attention" in text

    dependabot_policy = text.index('if [ "$is_dependabot" = "true" ]; then')
    leave_open = text.index("continue", dependabot_policy)
    close_own_automation = text.index('gh pr close "$number"', leave_open)
    assert dependabot_policy < leave_open < close_own_automation


def test_group_queue_skips_failed_and_single_prs_and_activates_only_one_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = _install_fake_gh(tmp_path, monkeypatch)
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
                },
                {
                    "number": 2,
                    "author": {"login": "app/dependabot"},
                    "url": "https://example.test/pr/2",
                    "title": "chore(deps): bump urllib3 from 2.0.0 to 2.0.1",
                    "body": "Bumps urllib3 from 2.0.0 to 2.0.1.",
                    "createdAt": "2026-01-02T00:00:00Z",
                    "autoMergeRequest": None,
                },
                {
                    "number": 3,
                    "author": {"login": "app/dependabot"},
                    "url": "https://example.test/pr/3",
                    "title": "chore(deps): bump the actions group",
                    "body": group_body,
                    "createdAt": "2026-01-03T00:00:00Z",
                    "autoMergeRequest": {"enabledAt": "2026-01-03T00:05:00Z"},
                },
                {
                    "number": 4,
                    "author": {"login": "app/dependabot"},
                    "url": "https://example.test/pr/4",
                    "title": "chore(deps): bump another group",
                    "body": group_body,
                    "createdAt": "2026-01-04T00:00:00Z",
                    "autoMergeRequest": None,
                },
            ]
        ),
    )
    monkeypatch.setenv("GH_CHECKS_1", '[{"bucket":"fail"}]')
    monkeypatch.setenv("GH_CHECKS_3", '[{"bucket":"pass"}]')

    subprocess.run([QUEUE_GROUP], check=True)

    calls = log.read_text().splitlines()
    assert "pr merge 3 --repo owner/repo --disable-auto" in calls
    assert "pr update-branch 3 --repo owner/repo --rebase" in calls
    auto_calls = [call for call in calls if " --auto " in call]
    assert auto_calls == [
        "pr merge https://example.test/pr/3 --repo owner/repo --auto --squash"
    ]
    assert not any("update-branch 1" in call for call in calls)
    assert not any("update-branch 2" in call for call in calls)
    assert not any("update-branch 4" in call for call in calls)
