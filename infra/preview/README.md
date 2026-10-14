# Branch previews on OpenStack

This proof of concept hosts a persistent `main` preview and up to two trusted internal PR previews on a dedicated VPS. Each preview has a separate PlanetScale Postgres branch, migration/runtime roles, Auth0 web client, API audience, database login connection, test administrator, and session/calendar signing keys.

## Running infrastructure

- Repository: `jotjern/monoweb`.
- Preview VPS: `monoweb-preview-poc`, Ubuntu 24.04, 4 vCPUs, 8 GiB RAM, 80 GiB boot volume, private address `192.168.73.66`.
- Public ingress: `129.241.100.225` on `court-smp-26-3`. Two systemd socket proxies forward TCP 80/443 to the VPS over an additional private interface (`192.168.73.254`). Minecraft still uses 25565 and its original network interface.
- Caddy on the preview VPS terminates HTTPS and routes hostnames to Docker containers.
- Main preview: `https://main.129.241.100.225.sslip.io`, API at `https://main-api.129.241.100.225.sslip.io`.
- PR N: `https://pr-N.129.241.100.225.sslip.io`, API at `https://pr-N-api.129.241.100.225.sslip.io`.
- PlanetScale: `jotjernshaugen/monoweb-preview-test`. The main preview uses `preview-main`; PR N uses `pr-N`. The default database branch `main` is not used by the application.
- Auth0 tenant: `onlineweb-staging.eu.auth0.com`. Preview connections and applications are separately named `Monoweb-Preview-*` / `Monoweb preview *`.

The Minecraft host only forwards web TCP connections and acts as a restricted SSH jump host. No application containers or application secrets run there. Its floating IP and game processes remain assigned as before.

## Deployment lifecycle

`.github/workflows/preview.yml` requests deploys on pushes to `main`, internal PR opens/updates/reopens, and manual dispatch. It requests teardown when an internal PR closes. Fork PRs are excluded both by the workflow and the host controller.

The deployment job runs no repository code on the GitHub runner. A separate credential-free job runs controller boundary tests first. Deployment uses a restricted SSH key through `monoweb-preview-jump` to `preview-deploy` on the VPS. The forced command accepts only `deploy main|pr-N SHA`, `destroy pr-N`, and `list`. It cannot open an interactive shell, copy arbitrary files, or delete the persistent main preview. Before building and again before starting containers, the controller checks the requested SHA against GitHub's current main/PR head.

The host controller serializes operations using a lock, provisions or reuses resources, fetches the exact source commit, builds a tools image, runs Prisma migrations/generation and an idempotent synthetic seed, builds the app images, starts a separate Compose project, waits for health, and reloads Caddy. Docker restart policies recover crashed processes and restart stacks after a host reboot. An hourly systemd timer removes previews for closed PRs if a GitHub cleanup event was missed.

Builds run on the preview VPS for this proof of concept. Application build processes receive no PlanetScale service token or Auth0 provisioning credential. Frontend builds use non-secret placeholders for server configuration; actual server secrets are supplied at container startup. Per-preview public URLs are embedded at build time.

## Credentials

`/opt/monoweb-preview/secrets/controller.json` is root-readable only and follows `controller.example.json`. The PlanetScale service token is scoped to the preview database with `read_branch`, `create_branch`, `delete_branch`, `connect_branch`, and `delete_branch_password`. It has no production-branch connection or deletion permissions. The dedicated Auth0 provisioner can manage preview resources in the staging tenant; it is not a production tenant credential.

Per-preview state lives in root-only `/opt/monoweb-preview/states/ID.json`. It records resource IDs, database credentials, client secrets, the synthetic admin password, and session/signing keys. Application `.env` files are mode 0600 under `/opt/monoweb-preview/stacks/ID/`. Only the backend receives its branch's runtime database credential. The migration role inherits `postgres`; the runtime role inherits `pg_read_all_data,pg_write_all_data`. Migration credentials are passed only to short-lived tools containers.

GitHub stores only `PREVIEW_DEPLOY_SSH_KEY` and `PREVIEW_SSH_KNOWN_HOSTS`, plus non-secret host/domain repository variables. The jump key permits forwarding only to the preview VPS's SSH port. The VPS key invokes only the forced controller command via a narrowly specified sudo rule.

Auth0 Management API user permissions are tenant-wide even though login connections and API audiences are per-preview. Treat all deployed application code as trusted. This POC separates previews from production but is not a sandbox for hostile code: Docker containers share a kernel and the Caddy edge network.

## Access and operations

Administrator SSH:

```sh
ssh -J court -i ~/.ssh/monoweb-preview-admin ubuntu@192.168.73.66
sudo /opt/monoweb-preview/controller/previewctl.py list
sudo docker ps
sudo journalctl -u monoweb-preview-build-main
```

Test account emails are `admin.main@preview.example.com` and `admin.pr-N@preview.example.com`. Retrieve their generated passwords from the corresponding root-only state file; never commit them. The login connection disables public signup. Authorization remains enabled, and the seed assigns the test administrator a real Dotkom group membership.

The controller and Dockerfiles are installed under `/opt/monoweb-preview/controller`. Application deployments intentionally do not replace that trusted controller; infrastructure changes must be installed by an administrator. Install `reconcile.service` and `reconcile.timer` as `monoweb-preview-reconcile.*`. Caddy's fixed Compose manifest is `edge.compose.json`.

## POC limits

- Synthetic event/article/group data only; no production database or user import.
- Upload storage and Stripe payments are unconfigured and will not work. No AWS credentials are supplied. Email delivery, Workspace synchronization, Google Sheets writes, and external telemetry are disabled/unconfigured.
- Turnstile uses its documented test keys; this is a preview environment, not production bot protection.
- Deployments can briefly interrupt a preview. Schema migrations are applied in place; incompatible migration edits may require destroying/recreating that PR preview. There is no automatic database schema merge or production deployment.
- Capacity is limited to main plus two PRs. Builds are serialized. Image/build-cache retention should be revisited before wider use; teardown removes the closed preview's application images and source checkout.
- The temporary `sslip.io` hostnames can be replaced with a dedicated wildcard DNS domain by updating the controller config and GitHub repository variable, then rebuilding/reprovisioning callback URLs.
- Preview state and credentials need a secure backup/rotation procedure before this becomes long-lived infrastructure.

Run controller boundary tests with `python3 -m unittest discover -s infra/preview -p 'test_*.py'`.
