#!/bin/sh
# Executed by Coolify's build helper, in its checkout. No control-plane credentials here.
set -eu
# Reuse the RPC Dockerfile's dependency stage, including older branches predating its name.
sed -e 's/AS installer/AS dependencies/' -e 's/# Install for production/FROM dependencies AS installer/' apps/rpc/Dockerfile > /tmp/preview-dependencies.Dockerfile
# Once the repository has the named stage, use it directly.
if grep -q 'AS dependencies' apps/rpc/Dockerfile; then cp apps/rpc/Dockerfile /tmp/preview-dependencies.Dockerfile; fi
docker build -f /tmp/preview-dependencies.Dockerfile --target dependencies -t "preview-tools:$COOLIFY_RESOURCE_UUID" .
container=$(docker create -e "DATABASE_URL=$MIGRATION_DATABASE_URL" -e "PREVIEW_ADMIN_ID=$PREVIEW_ADMIN_ID" -e "PREVIEW_ADMIN_EMAIL=$PREVIEW_ADMIN_EMAIL" "preview-tools:$COOLIFY_RESOURCE_UUID" sh -ec '
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
trap 'docker rm -f "$container" >/dev/null 2>&1 || true' EXIT
docker start -a "$container"
mkdir -p packages/db/generated
docker cp "$container:/app/packages/db/generated/." packages/db/generated/
docker compose --env-file /artifacts/build-time.env -f infra/preview/compose.yaml build web rpc
