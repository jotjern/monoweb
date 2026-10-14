#!/bin/sh
# Executed by Coolify's build helper, in its checkout. No control-plane credentials here.
set -eu
set -a
. /artifacts/build-time.env
set +a
tools="preview-tools:${PWD##*/}"
compose() { docker compose --project-directory "$PWD" --env-file /artifacts/build-time.env -f infra/preview/compose.yaml "$@"; }
config=$(compose config --format json)
pr=$(printf '%s' "$config" | jq -r '.services.rpc.labels["coolify.pullRequestId"] // "0"')
id=main; [ "$pr" = 0 ] || id="pr-$pr"
volume=$(printf '%s' "$config" | jq -r '.volumes["preview-context"].name')
web=$(printf '%s' "$config" | jq -r '.services.web.environment.COOLIFY_URL | split(",")[0]')
rpc=$(printf '%s' "$config" | jq -r '.services.rpc.environment.COOLIFY_URL | split(",")[0]')
docker run --rm -v /opt/monoweb-previews/hooks:/hooks:ro -v /data/monoweb-preview-db:/state -v /data/monoweb-previews/secrets/planetscale.env:/secrets/planetscale.env:ro -v /usr/local/bin/pscale:/usr/local/bin/pscale:ro -v "$volume:/output" coollabsio/coolify-helper:1.0.17 sh /hooks/database.sh "$id" "$web" "$rpc" > /artifacts/preview-db.env
docker run --rm -v "$volume:/context:ro" coollabsio/coolify-helper:1.0.17 cat /context/context.env >> /artifacts/build-time.env
set -a; . /artifacts/build-time.env; . /artifacts/preview-db.env; set +a
# Reuse the RPC Dockerfile's dependency stage, including older branches predating its name.
sed -e 's/AS installer/AS dependencies/' -e 's/# Install for production/FROM dependencies AS installer/' apps/rpc/Dockerfile > /tmp/preview-dependencies.Dockerfile
# Once the repository has the named stage, use it directly.
if grep -q 'AS dependencies' apps/rpc/Dockerfile; then cp apps/rpc/Dockerfile /tmp/preview-dependencies.Dockerfile; fi
docker build -f /tmp/preview-dependencies.Dockerfile --target dependencies -t "$tools" .
container=$(docker create -e "DATABASE_URL=$MIGRATION_DATABASE_URL" -e "PREVIEW_ADMIN_ID=$PREVIEW_ADMIN_ID" -e "PREVIEW_ADMIN_EMAIL=$PREVIEW_ADMIN_EMAIL" "$tools" sh -ec '
  pnpm -F @dotkomonline/db exec prisma migrate deploy
  pnpm -F @dotkomonline/db generate
  cat > /app/packages/db/.preview-init.mts <<"JS"
import { createPrisma } from "./src/index.ts"
import { execFileSync } from "node:child_process"
const db = createPrisma(process.env.DATABASE_URL!)
if (await db.user.count() === 0) execFileSync("pnpm", ["apply-fixtures"], { stdio: "inherit" })
// Bind the existing Dotkom fixture member to the preview login; preserve its roles/data.
const member = await db.groupMembership.findFirstOrThrow({ where: { groupId: "dotkom" } })
await db.user.update({ where: { id: member.userId }, data: { id: process.env.PREVIEW_ADMIN_ID, email: process.env.PREVIEW_ADMIN_EMAIL } })
await db.$disconnect()
JS
  pnpm -F @dotkomonline/db exec tsx .preview-init.mts
')
trap 'docker rm -f "$container" >/dev/null 2>&1 || true; docker image rm "$tools" >/dev/null 2>&1 || true' EXIT
docker start -a "$container"
mkdir -p packages/db/generated
docker cp "$container:/app/packages/db/generated/." packages/db/generated/
compose build --build-arg "NEXT_PUBLIC_ORIGIN=$web" --build-arg "NEXT_PUBLIC_RPC_HOST=$rpc" web rpc
