import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
REQUIRED_CONTEXTS_SCRIPT = ROOT / ".github" / "scripts" / "check_required_contexts.py"
TESTS_WORKFLOW = ROOT / ".github" / "workflows" / "tests.yaml"
AUTORELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "autorelease.yaml"
DETECT_RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "detect-release.yaml"
HACS_WORKFLOW = ROOT / ".github" / "workflows" / "hacs.yaml"
HASSFEST_WORKFLOW = ROOT / ".github" / "workflows" / "hassfest.yaml"
MASTER_BASELINE_WORKFLOW = ROOT / ".github" / "workflows" / "master-baseline.yaml"
BUILD_IMAGE_ACTION = (
    ROOT / ".github" / "actions" / "build-sidecar-image" / "action.yaml"
)
APP_TOKEN_ACTION = ROOT / ".github" / "actions" / "automation-app-token" / "action.yml"
REFUSE_SKIP_CI_ACTION = (
    ROOT / ".github" / "actions" / "refuse-skip-ci-on-release" / "action.yml"
)
WORKFLOWS = ROOT / ".github" / "workflows"
AUTOMATION_SECRET_CONSUMERS = {
    "dependabot-auto-merge.yaml",
    "dependency-updates.yaml",
    "min-ha-version-update.yaml",
}
# All three pull-request-triggered workflows share one key. Written once here
# rather than in each test, so changing it is a one-line edit.
PR_CONCURRENCY_GROUP = (
    "group: ${{ github.workflow }}-"
    "${{ github.event.pull_request.number || github.run_id }}"
)
PR_CONCURRENCY_CANCEL = "cancel-in-progress: ${{ github.event_name == 'pull_request' }}"


def test_tests_runs_automatically_only_for_pull_requests() -> None:
    text = TESTS_WORKFLOW.read_text()
    trigger = text[text.index("on:") : text.index("permissions:")]

    assert "\n  push:" not in trigger
    assert "  pull_request:\n" in trigger


def test_autorelease_reuses_pr_tests_instead_of_waiting_for_push_tests() -> None:
    text = AUTORELEASE_WORKFLOW.read_text()

    assert "require-pr-tests:" in text
    assert '--event pull_request --commit "$pr_head_sha"' in text
    assert "--event push" not in text
    assert "needs.require-pr-tests.result == 'success'" in text


def test_autorelease_requires_the_reused_run_to_have_passed_tests_finished() -> None:
    # A Tests run whose jobs were all skipped also concludes "success", so
    # watching the run is not on its own proof the suite ran.
    text = AUTORELEASE_WORKFLOW.read_text()

    assert 'select(.name == "tests-finished")' in text
    assert 'if [ "$summary" != "success" ]; then' in text
    assert ".base.ref == $ENV.DEFAULT_BRANCH" in text


def test_master_baseline_seeds_default_branch_caches_and_code_scanning() -> None:
    # Caches written by a PR run are scoped to that PR's merge ref, so only a
    # default-branch run produces entries other PRs can restore; CodeQL alerts
    # are likewise tracked per ref. tests.yaml no longer runs on master, so
    # this workflow is the only remaining producer of both.
    text = MASTER_BASELINE_WORKFLOW.read_text()
    trigger = text[text.index("on:") : text.index("permissions:")]

    # Reached by autorelease.yaml calling it, not by a `push:` trigger of its
    # own: both wanted the same event, and two triggers listed two runs against
    # every merge. A called workflow's jobs run inside the caller's run, so the
    # default-branch execution these caches and scans require is preserved
    # while a merge produces one run. The call is asserted below, since without
    # it this workflow would silently stop running on master altogether.
    assert "  workflow_call:\n" in trigger
    assert "  push:" not in trigger
    assert "  pull_request:" not in trigger
    autorelease_text = AUTORELEASE_WORKFLOW.read_text()
    assert "uses: ./.github/workflows/master-baseline.yaml" in autorelease_text

    # The three cache producers plus the default-branch scan.
    assert "./.github/actions/setup-python-env" in text
    assert "./.github/actions/build-sidecar-image" in text
    assert "path: .mypy_cache" in text
    assert "github/codeql-action/analyze@" in text

    # Cache keys must stay byte-identical to tests.yaml's or the entries are
    # not interchangeable and seeding them accomplishes nothing.
    mypy_key = (
        "key: mypy-${{ runner.os }}-"
        "${{ hashFiles('requirements_dev.lock.txt') }}-${{ github.sha }}"
    )
    assert mypy_key in text
    assert mypy_key in TESTS_WORKFLOW.read_text()

    # Not a gate: master commits are already merged, so no job here may post a
    # required status check's name.
    assert "\n  tests-finished:" not in text

    # Nor may it gate the release. Now that these jobs run inside
    # autorelease.yaml's run, a failure here -- a newly disclosed advisory
    # failing Trivy, a CodeQL hiccup -- must not stop a release that
    # require-pr-tests has already cleared, so no release job may take
    # `baseline` as a dependency.
    needs = re.findall(r"^\s*needs:.*$", autorelease_text, re.MULTILINE)
    assert needs, "autorelease.yaml should still declare job dependencies"
    assert not [line for line in needs if "baseline" in line], (
        f"no autorelease job may depend on baseline; found: {needs}"
    )


def test_automation_app_token_can_read_branch_protection() -> None:
    # daily-pr-sync.sh reads the default branch's required status check
    # contexts before merging anything, and refuses to run if it cannot (see
    # test_dependabot_automation.py, which exercises that refusal). The
    # endpoint 403s without this read-only grant.
    assert "permission-administration: read" in APP_TOKEN_ACTION.read_text()


def test_automation_private_key_consumers_use_protected_environment() -> None:
    secret_reference = "secrets.AUTOMATION_APP_PRIVATE_KEY"
    consumers = {
        workflow.name
        for workflow in WORKFLOWS.glob("*.yaml")
        if secret_reference in workflow.read_text()
    }

    assert consumers == AUTOMATION_SECRET_CONSUMERS
    for workflow_name in consumers:
        workflow = (WORKFLOWS / workflow_name).read_text()
        assert "    environment: automation\n" in workflow


def test_pr_workflows_cancel_only_stale_pr_runs() -> None:
    # Every non-PR trigger on hacs.yaml/hassfest.yaml resolves to
    # refs/heads/master, so their previous github.ref group key let a manual
    # dispatch cancel the in-flight weekly run -- the one run that exists to
    # catch drift in the floating validator images hacs/action and
    # home-assistant/actions/hassfest delegate to. All three now use
    # tests.yaml's key, which gives each non-PR run a group of its own.
    for workflow_path in (TESTS_WORKFLOW, HACS_WORKFLOW, HASSFEST_WORKFLOW):
        text = workflow_path.read_text()
        assert PR_CONCURRENCY_GROUP in text, workflow_path.name
        assert PR_CONCURRENCY_CANCEL in text, workflow_path.name


def test_meta_lint_verifies_required_status_check_contexts_resolve() -> None:
    # Branch protection matches by the exact string a check-run posts under.
    # Renaming one of these jobs leaves the PR blocked waiting for the old
    # context; this checker identifies the rename as the cause.
    assert (
        "run: python3 .github/scripts/check_required_contexts.py"
        in TESTS_WORKFLOW.read_text()
    )

    result = subprocess.run(
        [sys.executable, str(REQUIRED_CONTEXTS_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_tests_downstream_jobs_override_transitive_skip_and_fail_closed() -> None:
    text = TESTS_WORKFLOW.read_text()

    assert "if: \"!cancelled() && needs.codeql-analyze.result == 'success'\"" in text
    assert (
        "if: \"!cancelled() && needs.pytest.result == 'success' && "
        "needs.docker-build.result == 'success'\"" in text
    )
    assert 'if [ "$r" != "success" ]; then' in text
    assert '&& [ "$r" != "skipped" ]' not in text


def test_skip_ci_bypasses_ci_entirely_on_any_non_release_pr() -> None:
    # The documentation-only restriction was removed deliberately: skip-ci is
    # now a complete CI bypass on any non-release PR, since a skipped job
    # reports a conclusion branch protection treats as satisfied.
    # Asserted against the executable body, not the whole file: the headers
    # explain in prose why the old skip_ci_allowed output was dropped.
    detector = DETECT_RELEASE_WORKFLOW.read_text().split("\njobs:", 1)[1]

    assert "skip_ci_allowed" not in detector
    assert "*.md|LICENSE|LICENSE.*)" not in detector

    for caller_path in (TESTS_WORKFLOW, HACS_WORKFLOW, HASSFEST_WORKFLOW):
        caller = caller_path.read_text()
        assert "skip_ci_allowed" not in caller, caller_path.name
        assert (
            "!cancelled() && !(needs.detect-release.outputs.skip_ci == 'true' "
            "&& needs.detect-release.outputs.is_release == 'false')" in caller
        ), caller_path.name


def test_skip_ci_is_still_refused_on_a_release_pr() -> None:
    # Not a policy preference: autorelease.yaml's require-pr-tests demands a
    # real successful tests-finished before publishing, so skip-ci on a
    # version bump fails the release rather than speeding anything up.
    assert (
        "inputs.skip_ci == 'true' && inputs.is_release == 'true'"
        in REFUSE_SKIP_CI_ACTION.read_text()
    )

    # tests.yaml repeats the check inline; its summary job has no checkout, so
    # it cannot resolve the local composite action.
    tests = TESTS_WORKFLOW.read_text()
    assert "needs.detect-release.outputs.skip_ci == 'true' &&" in tests
    assert "needs.detect-release.outputs.is_release == 'true'" in tests

    for caller_path in (HACS_WORKFLOW, HASSFEST_WORKFLOW):
        assert (
            "uses: ./.github/actions/refuse-skip-ci-on-release"
            in caller_path.read_text()
        ), caller_path.name


def test_cached_sidecar_is_loaded_smoke_tested_and_rescanned() -> None:
    text = BUILD_IMAGE_ACTION.read_text()

    assert "- name: Load the cached sidecar image" in text
    assert "if: steps.image-cache.outputs.cache-hit == 'true'" in text
    assert "run: docker load -i /tmp/pylon2mqtt-ci.tar.gz" in text

    smoke = text.split("    - name: Smoke test the built image", 1)[1].split(
        "    - name: Gate on fixable high or critical vulnerabilities", 1
    )[0]
    scan = text.split(
        "    - name: Gate on fixable high or critical vulnerabilities", 1
    )[1].split("    - name: Save the image", 1)[0]
    assert "cache-hit" not in smoke
    assert "cache-hit" not in scan
