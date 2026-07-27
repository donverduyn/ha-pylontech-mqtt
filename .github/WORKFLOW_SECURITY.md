# Workflow security settings

The workflow files enforce least-privilege tokens and protected publication
jobs, but several controls live in GitHub repository settings and cannot be
declared in YAML.

## Environments

Create these Actions environments:

- `automation`: allow deployment only from the default branch. Store
  `AUTOMATION_APP_PRIVATE_KEY` here, not as a repository secret.
- `release`: allow deployment only from the default branch. Add a required
  reviewer if releases should require an explicit human approval; otherwise
  keep the branch restriction so branch-dispatched workflow revisions cannot
  publish.

## Actions execution policy

In **Settings → Actions → General**, require external actions to use full-length
commit SHAs and allow only selected actions. Allow GitHub-owned actions plus
these repository patterns:

- `aquasecurity/trivy-action@*`
- `aquasecurity/setup-trivy@*`
- `astral-sh/setup-uv@*`
- `hacs/action@*`
- `home-assistant/actions/*@*`

Do not enable the blanket "verified creators" allowance. The repository's
`check_action_pins.py` remains a readable CI diagnostic, while the repository
setting prevents a non-pinned action from beginning execution before that
diagnostic runs.

## Default-branch ruleset

Required status checks are configured by exact job name, not workflow name —
GitHub matches on the job's displayed check-run name, which is the job `id`
unless overridden by a `name:` key. As of 2026-07-25 the three required
contexts are:

- `tests-finished` — the summary job in `tests.yaml`; deliberately the only
  context from that workflow (see its own comment for why: matrixed jobs
  like `pytest (current)` never post a stable name a skip-ci PR could
  satisfy).
- `HACS Action` — `hacs.yaml`'s `validate` job, named via `name: "HACS
  Action"`.
- `validate` — `hassfest.yaml`'s job, no `name:` override.

This list lives only in GitHub's branch protection UI, not in any workflow
file — nothing in CI verifies it stays in sync. If any of these three jobs
is ever renamed (its `name:` key, or its job `id` for the two without one),
update branch protection's required-checks list in the same PR, or that
check silently stops being enforced (GitHub does not block a PR on a
required-context string that no run ever posts under — see
[status checks docs](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets-and-branch-protection-rules/about-protected-branches)).

The repository currently has one collaborator, who is also the only Code Owner.
Do not enable non-author or Code Owner approval requirements until a second
trusted reviewer is added: with admin enforcement enabled, either requirement
would make every pull request unmergeable. When a second maintainer is added,
enable one non-author approval, stale-approval dismissal, and Code Owner review
together. `.github/CODEOWNERS` assigns workflow and dependency automation
changes to the maintainer in the meantime.

The `skip-ci` label is limited in `detect-release.yaml` to non-release pull
requests whose changed paths are all Markdown or license files. Any source,
workflow, action, script, dependency, or configuration change with that label
runs the full suite and fails each required summary check until the label is
removed.

Where repository workflow-execution protections are available, restrict
`workflow_dispatch` execution to maintainers.

## Releases

Enable immutable GitHub Releases in repository settings. The release workflow
also fails when a Git tag, GitHub Release, or versioned GHCR tag already exists;
it never updates a published release identity in place.
