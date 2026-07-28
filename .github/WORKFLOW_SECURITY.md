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
file, so nothing can read the authoritative copy at lint time. What CI does
verify, via `meta-lint`'s `.github/scripts/check_required_contexts.py` step, is
that each context named above still resolves to a real, non-matrixed job that
actually posts it, and that this document still records it. If any of these
three jobs is ever renamed (its `name:` key, or its job `id` for the two
without one), that step fails — update branch protection's required-checks
list, `REQUIRED_CONTEXTS` in that script, and the list above together in the
same PR. Without it, a rename silently stops the check being enforced instead
of failing (GitHub does not block a PR on a required-context string that no run
ever posts under — see
[status checks docs](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets-and-branch-protection-rules/about-protected-branches)).

`.github/scripts/daily-pr-sync.sh` reads this same list from the branch
protection API at runtime rather than hardcoding it, which is why the
automation App needs `Administration: read` (see `AUTOMATION_APP.md`). The
script refuses to run at all if that read fails: an empty context list is
indistinguishable from "nothing is required" and would merge PRs unverified.

### Settings this design depends on

Two settings are load-bearing, not cosmetic, because `tests.yaml` no longer
runs on master pushes:

- **Require branches to be up to date before merging** (`strict`). The merged
  tree only equals the tree `tests.yaml` passed on because a PR must be rebased
  onto current master before it can merge. With this off, `autorelease.yaml`
  would reuse a Tests run for a head that was never tested against the base it
  is being released from.
- **Do not allow bypassing the above settings** (`enforce_admins`), together
  with the required checks themselves. These are what block direct pushes to
  master; every master commit must arrive through a PR that ran the suite.

Turning either off means master can change without any run of `tests.yaml`
ever having seen the result.

The repository currently has one collaborator, who is also the only Code Owner.
Do not enable non-author or Code Owner approval requirements until a second
trusted reviewer is added: with admin enforcement enabled, either requirement
would make every pull request unmergeable. When a second maintainer is added,
enable one non-author approval, stale-approval dismissal, and Code Owner review
together. `.github/CODEOWNERS` assigns workflow and dependency automation
changes to the maintainer in the meantime.

The `skip-ci` label skips the entire suite on any non-release pull request.
Because a skipped job reports a `skipped` conclusion that branch protection
treats as satisfied, the label is a complete CI bypass: a pull request carrying
it merges without pytest, lint, meta-lint, e2e, the Docker build, or CodeQL
having run, whatever it changes.

It was previously restricted to pull requests whose changed paths were all
Markdown or license files, which is what kept that bypass narrow. That
restriction was removed deliberately; applying the label is now a maintainer
judgement call on each pull request, with no mechanical backstop. Treat it as
equivalent to merging unreviewed and untested, because that is what it is.

The one remaining restriction is that `skip-ci` still fails a release pull
request (one that bumps `manifest.json`'s version). That is not a policy
preference: `autorelease.yaml`'s `require-pr-tests` job demands a real
successful `tests-finished` before publishing, so a skipped suite on a version
bump does not merge faster — it fails the release.

Where repository workflow-execution protections are available, restrict
`workflow_dispatch` execution to maintainers.

## Releases

Enable immutable GitHub Releases in repository settings. The release workflow
also fails when a Git tag, GitHub Release, or versioned GHCR tag already exists;
it never updates a published release identity in place.
