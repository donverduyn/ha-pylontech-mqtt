from pathlib import Path

ROOT = Path(__file__).parents[1]
TESTS_WORKFLOW = ROOT / ".github" / "workflows" / "tests.yaml"
AUTORELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "autorelease.yaml"
DETECT_RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "detect-release.yaml"
HACS_WORKFLOW = ROOT / ".github" / "workflows" / "hacs.yaml"
HASSFEST_WORKFLOW = ROOT / ".github" / "workflows" / "hassfest.yaml"
BUILD_IMAGE_ACTION = (
    ROOT / ".github" / "actions" / "build-sidecar-image" / "action.yaml"
)
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
