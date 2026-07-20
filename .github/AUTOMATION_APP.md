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
  - Contents: read and write
  - Pull requests: read and write
  - Workflows: read and write

The Workflows permission is required because the minimum-version updater may
change `.github/workflows/tests.yaml`, and App-authenticated PR branch updates
must trigger the normal pull-request checks. Install the App only on this
repository.

## Actions configuration

Configure these under **Settings → Secrets and variables → Actions**:

- Repository variable `AUTOMATION_APP_CLIENT_ID`: the App's Client ID
- Repository secret `AUTOMATION_APP_PRIVATE_KEY`: the complete contents of a
  private key PEM generated in the GitHub App settings

Do not commit the private key or put it in `secrets.env`. The workflows use
`actions/create-github-app-token` to mint a repository-scoped, short-lived
installation token and explicitly limit its permissions.

## Verification and migration

Run **Dependency Updates** manually after merging this configuration. Confirm
that token creation succeeds, any generated commit and pull request are
attributed to `ha-pylontech-mqtt-automation[bot]`, and the normal
pull-request checks run.

After a successful App-authenticated run:

1. Delete the obsolete `UPDATE_PR_TOKEN` repository secret.
2. Delete any local `secrets.env` copy containing the old token.
3. Revoke the old personal access token in the owning GitHub account.
