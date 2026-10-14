# Coolify previews

Coolify handles Git events, builds, native PR previews, containers, health checks, logs, HTTPS, restarts, and preview removal. Main stays deployed. Only trusted PRs are enabled.

`compose.yaml` reuses the normal web/RPC Dockerfiles. The RPC Dockerfile exposes its existing dependency stage so the build hook can run Prisma and `pnpm -F @dotkomonline/db apply-fixtures`. There are no preview Dockerfiles or separate fixture dataset. The existing Dotkom fixture user is linked to the shared staging test login.

The small custom integration covers PlanetScale, which Coolify does not branch itself:

- `database.sh` uses the official `pscale` CLI to ensure one database branch and separate migration/runtime roles per native preview. Credentials stay on the retained disk.
- `build.sh` runs that hook, migrations, existing fixtures on an empty database, and the normal Compose build. Its preview ID, volume, and URLs come from Coolify's generated Compose configuration.
- `runtime.mjs` loads that preview's bindings from its own Coolify-managed volume before Node starts. Application containers receive only their own database password. The PlanetScale service token stays in the provisioning helper.
- A short GitHub workflow deletes the PlanetScale branch when its PR closes. Coolify removes the containers, network, and preview volume. No custom controller, polling timer, deployment API, or per-PR Auth0 provisioning runs.

Auth0 clients and the synthetic test account are shared in the existing staging tenant. Each PlanetScale branch has independent fixtures and credentials. Preview Auth0 callbacks are allowed on the preview hostname pattern. Auth0 tokens are intentionally shared within staging; database credentials remain separate.

## Configuration

Coolify receives the existing working configuration through a private standard JSON export, supplied as `environment_json_file` to `coolify/`. Terraform imports it without maintaining another list of app variables in deployment code. Database URLs are excluded: the branch hook supplies those. `NODE_OPTIONS` is runtime-only; test identity bindings are build-only.

Later, supply a Doppler JSON export at the same boundary, or switch the values to Coolify's native Doppler secret references. No Doppler environment has been created. Keep preview configuration synthetic/test-only; email, Google Workspace, uploads, payments, and telemetry remain unconfigured/disabled as in the POC.

## Infrastructure and recovery

`terraform/` owns the OpenStack VPS, private network, router, security groups, SSH keypair, retained data volume, and additive ingress NIC/HTTP forwarders. It references the Minecraft VM and its existing public NIC without owning their lifecycle. Its game services and ports are untouched.

`bootstrap.sh` mounts the retained disk and uses Coolify's official pinned installer. Coolify's supported Compose override puts its PostgreSQL/Redis data on `/data`; its own keys, configuration, certificates, and branch credentials also survive VPS replacement. The data volume has `prevent_destroy`. Terraform state and bootstrap secrets are kept off the VPS, outside Git.

`coolify/` declares the application and imported configuration using the community Coolify Terraform provider. Keep its separate state outside Git too. This separation lets infrastructure be recovered before connecting to the Coolify API.

```sh
source '/Users/jotjern/Downloads/app-cred-Codex v2-openrc.sh'
cd infra/preview/terraform
terraform init -backend-config='path=/private/path/infrastructure.tfstate'
terraform apply -var-file=/private/path/preview.tfvars.json

cd ../coolify
terraform init -backend-config='path=/private/path/coolify.tfstate'
# COOLIFY_ENDPOINT and COOLIFY_TOKEN authenticate the provider.
terraform apply -var-file=/private/path/coolify.tfvars.json
```

The working machine keeps both state files, input files, and recovery credentials under `~/.config/monoweb-preview/`. Existing resources were imported rather than recreated. Restore these state files before applying to the existing infrastructure. OpenStack API access requires NTNU networking; an SSH SOCKS proxy via the ingress host also works.

Replacing the VPS reattaches `/data` and reinstalls Coolify with its retained configuration. Coolify can then redeploy the Git applications. Retained storage does not replace an off-site backup: keep Coolify's native database backups off the host before intentional storage changes. Initial recovery snapshots exist locally; scheduled external backup storage is a follow-up.

## Access

Dashboard: `https://coolify.129.241.100.225.sslip.io`.
Main: `https://main.129.241.100.225.sslip.io`.
Native preview URLs follow Coolify's default template, such as `https://2.main.129.241.100.225.sslip.io` and `https://2.main-api.129.241.100.225.sslip.io`.
Dashboard credentials and the shared staging test login are stored privately in `~/.config/monoweb-preview/`.

Repository pushes and PR events reach Coolify through its standard signed GitHub repository webhook. The cleanup workflow uses a PlanetScale service token scoped to this preview database. Builds and runtime are visible in Coolify; no deployment credentials are needed by application CI.
