#!/bin/sh
# Runs in a disposable helper with the preview-only PlanetScale token, never in an app.
set -eu
set -a; . /secrets/planetscale.env; set +a
id=$1
case "$id" in main|pr-[0-9]*) ;; *) exit 1 ;; esac
branch="coolify-$id"
ps() { pscale "$@" --org "$PLANETSCALE_ORG" --format json; }
branches=$(ps branch list "$PLANETSCALE_DATABASE")
if ! printf '%s' "$branches" | jq -e --arg name "$branch" 'any(.[]; .name == $name)' >/dev/null; then
  rm -f "/state/$id.migrator.json" "/state/$id.runtime.json"
  ps branch create "$PLANETSCALE_DATABASE" "$branch" --region eu-central --cluster-size PS_DEV --wait >/dev/null
fi
for kind in migrator runtime; do
  file="/state/$id.$kind.json"
  if [ ! -s "$file" ]; then
    roles=postgres
    [ "$kind" = migrator ] || roles=pg_read_all_data,pg_write_all_data
    umask 077
    ps role create "$PLANETSCALE_DATABASE" "$branch" "preview_$kind" --inherited-roles "$roles" > "$file.tmp"
    mv "$file.tmp" "$file"
  fi
done
# Each native Coolify preview has its own volume, containing only its runtime bindings.
jq -r '"DATABASE_URL=" + (.database_url | @sh)' "/state/$id.runtime.json" > /output/context.env
printf 'NEXT_PUBLIC_ORIGIN=%s\nWEB_PUBLIC_ORIGIN=%s\nALLOWED_ORIGINS=%s\nNEXT_PUBLIC_RPC_HOST=%s\nRPC_HOST=%s\n' "$2" "$2" "$2" "$3" "$3" >> /output/context.env
cp /hooks/runtime.mjs /output/runtime.mjs
chown 1001:1001 /output/context.env
chmod 400 /output/context.env
chmod 444 /output/runtime.mjs
jq -r '"MIGRATION_DATABASE_URL=" + (.database_url | @sh)' "/state/$id.migrator.json"
