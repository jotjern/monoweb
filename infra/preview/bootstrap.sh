#!/bin/bash
# Terraform invokes this on a new/replacement VPS. Coolify retains its own configuration on /data.
set -euo pipefail
cloud-init status --wait >/dev/null || true
source_dir=$1
volume_id=$2
disk="/dev/disk/by-id/virtio-${volume_id:0:20}"
if ! blkid "$disk" >/dev/null 2>&1; then mkfs.ext4 -L preview-data "$disk"; fi
uuid=$(blkid -s UUID -o value "$disk")
grep -q "UUID=$uuid" /etc/fstab || echo "UUID=$uuid /data ext4 defaults,nofail 0 2" >> /etc/fstab
mkdir -p /data
mountpoint -q /data || mount /data
install -d -m 700 /data/monoweb-preview-db /data/monoweb-previews/secrets /opt/monoweb-previews/hooks
install -m 644 "$source_dir"/{build.sh,database.sh,runtime.mjs} /opt/monoweb-previews/hooks/
install -m 600 "$source_dir/recovery/planetscale.env" /data/monoweb-previews/secrets/planetscale.env
# Configure the official installer; no application deployment controller runs on the host.
install -d /data/coolify/source /data/coolify/postgres /data/coolify/redis
install -m 644 "$source_dir/coolify-compose.custom.yml" /data/coolify/source/docker-compose.custom.yml
printf 'PasswordAuthentication no\nPermitRootLogin prohibit-password\n' > /etc/ssh/sshd_config.d/90-preview.conf
sshd -t && systemctl reload ssh
if ! docker inspect coolify >/dev/null 2>&1; then
  if [ -s /data/coolify/source/.env ]; then
    docker volume create coolify-db >/dev/null
    install -d -m 700 /root/.ssh
    for key in /data/coolify/ssh/keys/id.*@host.docker.internal; do
      ssh-keygen -y -f "$key" >> /root/.ssh/authorized_keys
    done
    chmod 600 /root/.ssh/authorized_keys
  fi
  set -a; source "$source_dir/recovery/coolify-admin.env"; set +a
  curl -fsSL https://raw.githubusercontent.com/coollabsio/coolify/f55efc859dfcbbf79274c4907b3df89f9767c11d/scripts/install.sh -o /tmp/install-coolify.sh
  AUTOUPDATE=false bash /tmp/install-coolify.sh 4.4.6 > /var/log/coolify-bootstrap.log 2>&1
fi
if ! command -v pscale >/dev/null; then
  curl -fsSL https://github.com/planetscale/cli/releases/download/v0.321.0/pscale_0.321.0_linux_amd64.tar.gz | tar xz -C /usr/local/bin pscale
fi
rm -rf "$source_dir/recovery"
