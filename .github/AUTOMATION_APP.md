# Repository automation GitHub App

The dependency, minimum-version, and pull-request maintenance workflows
authenticate as the private **HA Pylontech MQTT Automation** GitHub App. This
replaces the long-lived personal access token previously stored as
`UPDATE_PR_TOKEN`.

## App registration

Use these GitHub App settings:

- GitHub App name: `ha-pylontech-mqtt-automation`
- Display name: `HA Pylontech MQTT Automation`
- Homepage URL: `https://github.com/donverduyn/ha-pylontech-mqtt`
- Callback URL, setup URL, OAuth user authorization, device flow, and
  webhooks: disabled or blank
- Repository permissions:
  - Administration: read
  - Contents: read and write
  - Pull requests: read and write
  - Workflows: read and write

The Workflows permission is required because the minimum-version updater may
change `.github/workflows/tests.yaml`, and because PR Auto-merge merges
Dependabot PRs that bump action pins in those same files. App-authenticated PR
branch updates must trigger the normal pull-request checks. Install the App
only on this repository.

Each workflow requests a narrower token than the App itself holds, so this
grant does not reach every run: `dependency-updates.yaml` deliberately omits
`permission-workflows: write`, because `make update-deps` writes no file under
`.github/workflows/` and its pull request is the one that auto-merges. See that
workflow's own comment on the step.

The Administration permission is **read-only** and is required by
`.github/scripts/daily-pr-sync.sh`, which reads the default branch's required
status check contexts from branch protection before merging anything. That
endpoint returns 403 for the other three permissions; `gh api` writes an error
response's body to stdout while exiting non-zero, so a missing grant produced a
variable holding two concatenated JSON documents rather than a clean failure —
which made the merge path either fail open (attempting merges seconds after a
rebase) or never merge at all. The script now refuses to run without this read,
so granting it is mandatory, not optional.

Changing an installed App's permissions requires the account that installed it
to approve the new request before tokens carry it. Until that approval lands,
`actions/create-github-app-token` fails outright when it asks for
`permission-administration: read`, and **PR Auto-merge cannot run at all** —
including its rebase and stale-close passes.

## Actions configuration

Configure the App Client ID as repository variable
`AUTOMATION_APP_CLIENT_ID`. Create an Actions environment named
`automation`, restrict its deployment branches to the default branch, and
store the complete private-key PEM as environment secret
`AUTOMATION_APP_PRIVATE_KEY`.

Do not also keep the private key as a repository secret: a repository secret
would bypass the environment's branch restriction. Do not commit it or put it
in `secrets.env`. The workflows mint repository-scoped, short-lived
installation tokens and expose them to git only in the final publication step.

## Verification and migration

Run **Dependency Updates** manually after merging this configuration. Confirm
that token creation succeeds, any generated commit and pull request are
attributed to `ha-pylontech-mqtt-automation[bot]`, and the normal
pull-request checks run.

After a successful App-authenticated run:

1. Delete the obsolete `UPDATE_PR_TOKEN` repository secret.
2. Delete any local `secrets.env` copy containing the old token.
3. Revoke the old personal access token in the owning GitHub account.
