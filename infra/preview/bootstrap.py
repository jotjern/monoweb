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
source = Path(sys.argv[1])
volume_id = sys.argv[2]
def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)

# Cinder exposes a stable serial. Never format a disk that already has a filesystem.
serial = volume_id.replace('-', '')[:20]
for _ in range(60):
    disks = json.loads(run('lsblk', '-J', '-o', 'PATH,SERIAL,FSTYPE', capture_output=True).stdout)['blockdevices']
    disk = next((d for d in disks if d.get('serial') and d['serial'].replace('-', '').startswith(serial)), None)
    if disk: break
    time.sleep(2)
else: raise RuntimeError('Persistent Cinder data volume not found')
if not disk['fstype']: run('mkfs.ext4', '-L', 'monoweb-preview-data', disk['path'])
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
admin = json.loads((root / 'secrets/coolify-admin.json').read_text())
if not Path('/data/coolify/source/.env').exists():
    url = 'https://raw.githubusercontent.com/coollabsio/coolify/f55efc859dfcbbf79274c4907b3df89f9767c11d/scripts/install.sh'
    installer = source / 'install-coolify.sh'
    urllib.request.urlretrieve(url, installer)
    env = dict(os.environ, ROOT_USERNAME=admin['username'], ROOT_USER_EMAIL=admin['email'], ROOT_USER_PASSWORD=admin['password'], AUTOUPDATE='false')
    with open('/var/log/coolify-bootstrap.log', 'w') as log:
        run('bash', str(installer), '4.4.6', env=env, stdout=log, stderr=subprocess.STDOUT)
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
    except (URLError, HTTPError): time.sleep(2)
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
for name in ('reconcile.service', 'reconcile.timer'):
    shutil.copy2(source / name, '/etc/systemd/system/monoweb-preview-' + name)
run('systemctl', 'daemon-reload')
# Enable after the first verified migration, or automatically on a replacement server.
run('systemctl', 'enable', '--now', 'monoweb-preview-reconcile.timer')
print('Coolify and branch reconciliation configured; state is on the retained data volume.')
