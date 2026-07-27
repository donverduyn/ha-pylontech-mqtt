from pathlib import Path

ROOT = Path(__file__).parents[1]
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
WORKFLOWS = ROOT / ".github" / "workflows"
AUTOMATION_SECRET_CONSUMERS = {
    "dependabot-auto-merge.yaml",
    "dependency-updates.yaml",
    "min-ha-version-update.yaml",
}


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

    assert "  push:\n    branches:\n      - master\n" in trigger
    assert "  pull_request:" not in trigger

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


def test_tests_concurrency_cancels_only_stale_pr_runs() -> None:
    text = TESTS_WORKFLOW.read_text()

    assert (
        "group: ${{ github.workflow }}-"
        "${{ github.event.pull_request.number || github.run_id }}" in text
    )
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in text


def test_tests_downstream_jobs_override_transitive_skip_and_fail_closed() -> None:
    text = TESTS_WORKFLOW.read_text()

    assert "if: \"!cancelled() && needs.codeql-analyze.result == 'success'\"" in text
    assert (
        "if: \"!cancelled() && needs.pytest.result == 'success' && "
        "needs.docker-build.result == 'success'\"" in text
    )
    assert 'if [ "$r" != "success" ]; then' in text
    assert '&& [ "$r" != "skipped" ]' not in text


def test_skip_ci_is_limited_to_documentation_only_non_release_prs() -> None:
    detector = DETECT_RELEASE_WORKFLOW.read_text()

    assert "skip_ci_allowed:" in detector
    assert "*.md|LICENSE|LICENSE.*)" in detector
    assert 'git diff --name-only --no-renames -z "$BASE_SHA" HEAD' in detector
    assert 'done < "$changed_paths"' in detector
    assert "< <(git diff" not in detector

    for caller_path in (TESTS_WORKFLOW, HACS_WORKFLOW, HASSFEST_WORKFLOW):
        caller = caller_path.read_text()
        assert "needs.detect-release.outputs.skip_ci_allowed == 'true'" in caller
        assert "skip_ci_allowed" in caller


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
