#!/usr/bin/env python3
"""Verify the default branch's required status checks still name real jobs.

Branch protection matches required status checks by the exact string a
check-run posts under. Renaming a required job therefore leaves pull requests
blocked forever waiting for the old context, with no diagnostic that connects
the pending check to the job rename. The list itself lives only in GitHub's
branch protection UI (`.github/scripts/daily-pr-sync.sh` reads it from the API
at runtime rather than hardcoding it), which means nothing in the repository
would otherwise explain that drift.

This asserts the other half: that each context recorded in
`.github/WORKFLOW_SECURITY.md` still resolves to a job that actually posts it.
Keeping the expected mapping here and cross-checking it against that document
means renaming a job fails `meta-lint` in the same pull request, with the
branch-protection update named as the fix.

Stdlib only, like check_action_pins.py: the meta-lint job intentionally has no
Python environment set up.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
SECURITY_DOC = ROOT / ".github" / "WORKFLOW_SECURITY.md"

# workflow file -> the check-run name branch protection requires from it.
# Must stay in sync with the default-branch ruleset; see WORKFLOW_SECURITY.md.
REQUIRED_CONTEXTS = {
    "tests.yaml": "tests-finished",
    "hacs.yaml": "HACS Action",
    "hassfest.yaml": "validate",
}

JOB_ID_RE = re.compile(r"^  ([A-Za-z_][A-Za-z0-9_-]*):\s*$")
JOB_NAME_RE = re.compile(r"""^    name:\s*["']?(.+?)["']?\s*$""")
JOB_STRATEGY_RE = re.compile(r"^    strategy:\s*$")


def posted_check_names(path: Path) -> dict[str, bool]:
    """Map each job's displayed check-run name to whether it is matrixed.

    A job's check-run name is its `name:` when present and its job id
    otherwise. A matrixed job posts one check-run per leg, with the matrix
    values interpolated into that name, so no fixed string it produces can
    ever be a stable required context.
    """
    names: dict[str, bool] = {}
    current: str | None = None
    matrixed = False
    in_jobs = False

    for line in path.read_text().splitlines():
        if not in_jobs:
            # `on:` holds two-space keys of its own (`pull_request:`,
            # `workflow_dispatch:`) that look exactly like job ids.
            in_jobs = line.rstrip() == "jobs:"
            continue
        job_id = JOB_ID_RE.match(line)
        if job_id:
            if current is not None:
                names[current] = matrixed
            current = job_id.group(1)
            matrixed = False
            continue
        if current is None:
            continue
        if JOB_STRATEGY_RE.match(line):
            matrixed = True
            continue
        job_name = JOB_NAME_RE.match(line)
        if job_name:
            current = job_name.group(1)

    if current is not None:
        names[current] = matrixed
    return names


def main() -> int:
    # `--list` prints the expected contexts, one per line, so
    # .github/scripts/daily-pr-sync.sh can reconcile them against the list it
    # already fetches from the branch-protection API. That is the only copy
    # that decides whether a pull request is actually blocked, and it is the
    # one copy nothing here can read: meta-lint has no token. Without that
    # reconciliation this script only checks two transcriptions against each
    # other and would stay green forever after an edit in the protection UI.
    if len(sys.argv) > 1 and sys.argv[1] == "--list":
        for context in sorted(REQUIRED_CONTEXTS.values()):
            print(context)
        return 0

    errors: list[str] = []
    doc = SECURITY_DOC.read_text()

    # Both directions: a context recorded in the document but absent from the
    # map would otherwise never be checked against a real job at all.
    documented = set(re.findall(r"^- `([^`]+)` —", doc, re.MULTILINE))
    undocumented_extras = documented - set(REQUIRED_CONTEXTS.values())
    if undocumented_extras:
        errors.append(
            f"{SECURITY_DOC.relative_to(ROOT)} lists required status checks "
            f"missing from REQUIRED_CONTEXTS: {', '.join(sorted(undocumented_extras))}."
        )

    for workflow_name, context in sorted(REQUIRED_CONTEXTS.items()):
        path = WORKFLOWS / workflow_name
        if not path.is_file():
            errors.append(f"{workflow_name} is missing but posts required context")
            continue

        names = posted_check_names(path)
        if context not in names:
            errors.append(
                f"{workflow_name}: no job posts the required status check "
                f"{context!r} (found: {', '.join(sorted(names)) or 'none'}). "
                f"Rename it back, or update both this list and the default "
                f"branch's required-checks list in the same pull request."
            )
            continue
        if names[context]:
            errors.append(
                f"{workflow_name}: required status check {context!r} is a "
                f"matrixed job, so it posts one interpolated name per leg and "
                f"never that fixed string."
            )
        if f"`{context}`" not in doc:
            errors.append(
                f"{SECURITY_DOC.relative_to(ROOT)} does not document required "
                f"status check {context!r}."
            )

    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1

    print(
        f"Verified {len(REQUIRED_CONTEXTS)} required status check contexts "
        f"still resolve to real jobs."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
