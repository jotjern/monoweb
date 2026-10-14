#!/usr/bin/env python3
"""Idempotent host setup, invoked by Terraform. Secrets arrive through a private recovery directory."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request

os.umask(0o077)
if shutil.which('cloud-init'):
    subprocess.run(['cloud-init', 'status', '--wait'], check=False, stdout=subprocess.DEVNULL)
source = Path(sys.argv[1])
volume_id = sys.argv[2]
def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)

# Cinder exposes a stable serial. Never format a disk that already has a filesystem.
serial = volume_id.replace('-', '')[:20]
for _ in range(60):
    disks = json.loads(run('lsblk', '-J', '-o', 'PATH,SERIAL,FSTYPE', capture_output=True).stdout)['blockdevices']
    disk = next((d for d in disks if d.get('serial') and len(d['serial']) >= 8 and volume_id.replace('-', '').startswith(d['serial'].replace('-', ''))), None)
    if disk: break
    time.sleep(2)
else: raise RuntimeError('Persistent Cinder data volume not found')
if not disk['fstype']: run('mkfs.ext4', '-L', 'preview-data', disk['path'])
uuid = run('blkid', '-s', 'UUID', '-o', 'value', disk['path'], capture_output=True).stdout.strip()
fstab = Path('/etc/fstab')
if f'UUID={uuid}' not in fstab.read_text():
    with fstab.open('a') as f: f.write(f'\nUUID={uuid} /data ext4 defaults,nofail 0 2\n')
if not os.path.ismount('/data'):
    # Also migrates the running POC's Coolify installation onto the retained volume.
    names = run('docker', 'ps', '--format', '{{.Names}}', capture_output=True).stdout.splitlines() if shutil.which('docker') else []
    coolify = [n for n in names if n.startswith('coolify')]
    if coolify: run('docker', 'stop', *coolify)
    Path('/mnt/preview-data').mkdir(exist_ok=True)
    run('mount', disk['path'], '/mnt/preview-data')
    if Path('/data').exists(): run('cp', '-a', '/data/.', '/mnt/preview-data/')
    run('umount', '/mnt/preview-data')
    Path('/data').mkdir(exist_ok=True)
    run('mount', '/data')
    if coolify: run('docker', 'start', *coolify)

root = Path('/data/monoweb-previews')
for name in ('secrets', 'states'): (root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
old = Path('/opt/monoweb-previews')
for name in ('secrets', 'states'):
    if (old / name).exists():
        for p in (old / name).iterdir():
            if not (root / name / p.name).exists(): shutil.copy2(p, root / name / p.name)
for p in (source / 'recovery').rglob('*'):
    if p.is_file():
        target = root / ('states' if p.parent.name == 'states' else 'secrets') / p.name
        if not target.exists() or p.name in ('web.json', 'rpc.json'): shutil.copy2(p, target)
        target.chmod(0o600)
controller = Path('/opt/monoweb-previews/controller')
controller.mkdir(parents=True, exist_ok=True)
for name in ('previewctl.py', 'build.sh', 'compose.yaml'): shutil.copy2(source / name, controller / name)
# Coolify uses SSH keys for localhost; keep password authentication disabled.
Path('/etc/ssh/sshd_config.d/90-preview.conf').write_text('PasswordAuthentication no\nPermitRootLogin prohibit-password\n')
run('sshd', '-t'); run('systemctl', 'reload', 'ssh')
# Coolify supports a custom Compose override. Persist its actual database/Redis data,
# which otherwise live in Docker named volumes on the disposable boot disk.
custom_compose = Path('/data/coolify/source/docker-compose.custom.yml')
if Path('/data/coolify/source/.env').exists() and not custom_compose.exists():
    names = run('docker', 'ps', '--format', '{{.Names}}', capture_output=True).stdout.splitlines()
    active = [n for n in ('coolify', 'coolify-db', 'coolify-redis') if n in names]
    if active: run('docker', 'stop', *active)
    for volume, destination in [('coolify-db', '/data/coolify/postgres'), ('coolify-redis', '/data/coolify/redis')]:
        target = Path(destination)
        if not target.exists():
            target.mkdir()
            mountpoint = run('docker', 'volume', 'inspect', volume, '--format', '{{.Mountpoint}}', capture_output=True).stdout.strip()
            run('cp', '-a', mountpoint + '/.', destination + '/')
    custom_compose.write_text("""services:
  postgres:
    volumes:
      - /data/coolify/postgres:/var/lib/postgresql/data
  redis:
    volumes:
      - /data/coolify/redis:/data
""")
    run('docker', 'compose', '--env-file', '/data/coolify/source/.env', '-f', '/data/coolify/source/docker-compose.yml', '-f', '/data/coolify/source/docker-compose.prod.yml', '-f', str(custom_compose), 'up', '-d', env=dict(os.environ, LATEST_IMAGE='4.4.6'))
admin = json.loads((root / 'secrets/coolify-admin.json').read_text())
coolify_exists = shutil.which('docker') and subprocess.run(['docker', 'inspect', 'coolify'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
if not coolify_exists:
    # A retained database uses its retained SSH key. Prevent the installer replacing that key.
    if Path('/data/coolify/postgres').exists():
        run('docker', 'volume', 'create', 'coolify-db', stdout=subprocess.DEVNULL)
        for key in Path('/data/coolify/ssh/keys').glob('id.*@host.docker.internal'):
            public = run('ssh-keygen', '-y', '-f', str(key), capture_output=True).stdout.strip()
            authorized = Path('/root/.ssh/authorized_keys')
            authorized.parent.mkdir(mode=0o700, exist_ok=True)
            if not authorized.exists() or public not in authorized.read_text():
                with authorized.open('a') as f: f.write(public + '\n')
            authorized.chmod(0o600)
    for path in (root / 'states').glob('*.json'):
        state = json.loads(path.read_text()); state.pop('sha', None); path.write_text(json.dumps(state))
    url = 'https://raw.githubusercontent.com/coollabsio/coolify/f55efc859dfcbbf79274c4907b3df89f9767c11d/scripts/install.sh'
    installer = source / 'install-coolify.sh'
    urllib.request.urlretrieve(url, installer)
    env = dict(os.environ, ROOT_USERNAME=admin['username'], ROOT_USER_EMAIL=admin['email'], ROOT_USER_PASSWORD=admin['password'], AUTOUPDATE='false')
    with open('/var/log/coolify-bootstrap.log', 'w') as log:
        run('bash', str(installer), '4.4.6', env=env, stdout=log, stderr=subprocess.STDOUT)
if not custom_compose.exists():
    # The first install creates the database volumes; a second pass adds persistent bindings.
    os.execv(sys.executable, [sys.executable, __file__, *sys.argv[1:]])
# Use the official CLI package on the host; applications never receive its token.
if not shutil.which('pscale'):
    run('sh', '-c', 'curl -fsSL https://github.com/planetscale/cli/releases/download/v0.321.0/pscale_0.321.0_linux_amd64.tar.gz | tar xz -C /usr/local/bin pscale')
config_path = root / 'secrets/controller.json'
config = json.loads(config_path.read_text())
config['coolify_url'] = 'http://127.0.0.1:8000'
config['environment_command'] = ['cat', str(root / 'secrets/{application}.json')]
# Bootstrap a local API token through Coolify's own Laravel models. Persist it on the data volume.
from urllib.error import HTTPError, URLError
for _ in range(90):
    try:
        urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3); break
    except OSError: time.sleep(2)
php = r'''App\Models\InstanceSettings::get()->update(["is_api_enabled"=>true]); session(["currentTeam"=>App\Models\Team::findOrFail(0)]); $u=App\Models\User::findOrFail(0); $u->tokens()->where("name","preview-reconciler")->delete(); echo $u->createToken("preview-reconciler",["read","read:sensitive","write","write:sensitive","deploy"])->plainTextToken;'''
config['coolify_token'] = run('docker', 'exec', 'coolify', 'php', 'artisan', 'tinker', '--execute=' + php, capture_output=True).stdout.strip()
if '|' not in config['coolify_token'] or '\n' in config['coolify_token']: raise RuntimeError('Coolify token bootstrap failed')
sys.path.insert(0, str(controller))
from previewctl import http
api = lambda method, path, data=None: http(method, config['coolify_url'] + '/api/v1' + path, data, config['coolify_token'])
config['coolify_server_uuid'] = api('GET', '/servers')[0]['uuid']
projects = api('GET', '/projects')
project = next((p for p in projects if p['name'] == 'Monoweb previews'), None) or api('POST', '/projects', {'name': 'Monoweb previews'})
config['coolify_project_uuid'] = project['uuid']
if not any(e['name']=='preview' for e in api('GET', '/projects/' + project['uuid'])['environments']):
    api('POST', '/projects/' + project['uuid'] + '/environments', {'name': 'preview'})
config_path.write_text(json.dumps(config, indent=2))
dynamic = Path('/data/coolify/proxy/dynamic')
dynamic.mkdir(parents=True, exist_ok=True)
(dynamic / 'monoweb-control.yaml').write_text("""http:
  routers:
    monoweb-control:
      rule: Host(`coolify.""" + config['base_domain'] + """`)
      entryPoints: [https]
      tls:
        certResolver: letsencrypt
      service: monoweb-control
  services:
    monoweb-control:
      loadBalancer:
        servers:
          - url: http://coolify:8080
""")
# A fresh server can start its proxy immediately. During migration the old edge stays until validation.
old_edge = subprocess.run(['docker', 'inspect', 'preview-caddy'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
if not old_edge: api('POST', '/servers/' + config['coolify_server_uuid'] + '/proxy/restart')
for name in ('reconcile.service', 'reconcile.timer'):
    shutil.copy2(source / name, '/etc/systemd/system/monoweb-preview-' + name)
run('systemctl', 'daemon-reload')
# Enable after the first verified migration, or automatically on a replacement server.
run('systemctl', 'enable', '--now', 'monoweb-preview-reconcile.timer')
print('Coolify and branch reconciliation configured; state is on the retained data volume.')
