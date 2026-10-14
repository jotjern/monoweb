# Coolify branch previews

Coolify owns checkout, builds, application containers, health checks, HTTPS, logs, and restarts.
A host-side reconciler polls the public fork every five minutes and connects the external resources Coolify does not provision:

- `main` remains available; each open internal PR gets a separate Coolify Compose application.
- Each application gets its own PlanetScale Postgres development branch and runtime/migration roles.
- Each preview gets its own staging Auth0 clients, API audience, connection, and test login.
- Closed PRs lose their Coolify application, PlanetScale branch, and Auth0 resources. Fork PRs never deploy.
- Two concurrent PR previews are allowed by default. Builds are serialized by Coolify on this 4 CPU / 8 GiB host.

There are no preview Dockerfiles, separate fixtures, deployment HTTP service, or GitHub deployment secrets.
`compose.yaml` uses the existing web/RPC Dockerfiles. `build.sh` uses the RPC dependency stage for Prisma and the existing `pnpm -F @dotkomonline/db apply-fixtures` command, only on an empty database. It binds the existing Dotkom fixture member to the preview's Auth0 login. The compatibility stage insertion allows the persistent preview to build older commits too.

## Configuration source

The private `controller.json` contains provider credentials and an `environment_command` returning a JSON object of string values. Initially it reads private `web.json` / `rpc.json` configuration files exported from the working POC, rather than constructing app environments in Python. Only per-preview resource bindings use `${ORIGIN}`, `${RPC_ORIGIN}`, `${DATABASE_URL}`, `${WEB_CLIENT_ID}`, `${WEB_CLIENT_SECRET}`, `${RPC_CLIENT_ID}`, `${RPC_CLIENT_SECRET}`, `${AUTH0_DOMAIN}`, `${AUTH_SECRET}`, and `${SIGNING_KEY}` placeholders.

Coolify stores the rendered application configuration. The migration credential is build-only. PlanetScale/Auth0 provisioning tokens stay outside application builds and containers. Shared keys must agree; only the two conflicting web/RPC Auth0 keys are namespaced in Compose.

Later, replace `environment_command` with `doppler secrets download --project monoweb-{application} --config <preview-config> --no-file --format json`, supplying its service token to the trusted reconciler. Alternatively use Coolify's native Doppler secret-manager integration and `{{vault.KEY}}` references. No Doppler configuration has been created by this change. Changes to the configuration source require an explicit redeployment of existing applications.

The POC baseline uses Turnstile test keys and disables email, Google Workspace, RIF, telemetry, real uploads, and payments. Do not replace it with an unfiltered production export. The application RPC clients still have staging-tenant user-management access; use a dedicated preview Auth0 tenant before expanding access beyond trusted branches.

## Terraform and recovery

`terraform/` owns the private network, router, security groups, keypair, 4 CPU / 8 GiB VPS, retained data volume, and the additive Minecraft ingress NIC/forwarders. It references the Minecraft VM and its existing public NIC without managing their lifecycle. The two available floating IPs are already allocated, so HTTP/HTTPS share Minecraft's IP; game ports/services are untouched.

Terraform uploads the trusted controller independently of application deployments. Updates to server integration code are applied through Terraform, not executed from a PR. The Coolify installer and release are pinned; auto-updates are disabled. The 40 GiB data volume has `prevent_destroy` and holds Coolify's database, SSH keys, and provisioning state; deleting/replacing the boot VM retains it.

Keep Terraform state, the OpenStack application credential, SSH private keys, and a private recovery directory **outside Git**. The recovery directory contains `controller.json`, `web.json`, `rpc.json`, `coolify-admin.json`, and optionally `states/*.json`. Never overwrite a newer live state with an older recovery snapshot. The working machine stores these under `~/.config/monoweb-preview/` with restricted permissions.

```sh
source '/Users/jotjern/Downloads/app-cred-Codex v2-openrc.sh'
cd infra/preview/terraform
terraform init -backend-config='path=/private/path/preview.tfstate'
terraform plan -var-file=/private/path/preview.tfvars
terraform apply -var-file=/private/path/preview.tfvars
```

See `terraform.tfvars.example`. OpenStack API access requires NTNU network access; an SSH SOCKS proxy through the ingress host also works (`HTTPS_PROXY=socks5h://127.0.0.1:11080`). Existing POC resources were imported into Terraform rather than recreated. Restore the external state file to operate on those resources; do not apply an empty state against the existing deployment.

For a deleted VPS, refresh/plan/apply recreates it, reattaches the retained volume, mounts `/data`, reinstalls the pinned Coolify release if needed, restores the trusted reconciler, and resumes previews. The fixed private address keeps the ingress configuration stable. A retained disk is not an off-site backup: copy `/data/monoweb-previews` and Coolify's normal database backups off the host before intentional storage changes. Initial recovery copies exist locally; external scheduled backup storage remains a follow-up.

## Operations

- Dashboard: `https://coolify.129.241.100.225.sslip.io` (credentials in the private local credential file).
- Persistent preview: `https://main.129.241.100.225.sslip.io`.
- PR preview: `https://pr-N-web.129.241.100.225.sslip.io`; API: `https://pr-N-api.129.241.100.225.sslip.io`.
- Do not use `pr-N.<ip>.sslip.io`: numeric labels can resolve to a different IP.
- `journalctl -u monoweb-preview-reconcile.service` shows provisioning; Coolify shows builds and runtime logs.
- To reconcile now: `sudo systemctl start monoweb-preview-reconcile.service`.
- Test logins are recorded privately in `/data/monoweb-previews/states/<preview>.json`.

Validation: `python3 -m unittest discover -s infra/preview -p 'test_*.py'`, `sh -n infra/preview/build.sh`, and `terraform -chdir=infra/preview/terraform validate`. Live migration checks cover HTTPS, fixture content, Auth0 login, branch isolation, and Minecraft availability.
