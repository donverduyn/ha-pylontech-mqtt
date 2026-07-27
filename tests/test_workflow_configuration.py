from pathlib import Path

ROOT = Path(__file__).parents[1]
TESTS_WORKFLOW = ROOT / ".github" / "workflows" / "tests.yaml"
WORKFLOWS = ROOT / ".github" / "workflows"
AUTOMATION_SECRET_CONSUMERS = {
    "dependabot-auto-merge.yaml",
    "dependency-updates.yaml",
    "min-ha-version-update.yaml",
}


def test_tests_push_trigger_is_limited_to_master() -> None:
    text = TESTS_WORKFLOW.read_text()
    trigger = text[text.index("on:") : text.index("permissions:")]

    assert "  push:\n    branches:\n      - master\n" in trigger
    assert "  pull_request:\n" in trigger


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
