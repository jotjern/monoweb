#!/usr/bin/env python3
"""Trusted host controller. Application builds never receive control-plane credentials."""
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(os.environ.get("PREVIEW_ROOT", "/data/monoweb-previews"))
SAFE_ENV = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root", "LANG": "C.UTF-8"}
PREVIEW_ID = re.compile(r"^(main|pr-[1-9][0-9]*)$")
SHA = re.compile(r"^[0-9a-f]{40}$")

def run(args, *, cwd=None, capture=False, env=None):
    return subprocess.run(args, cwd=cwd, env=env or SAFE_ENV, check=True, text=True,
                          stdout=subprocess.PIPE if capture else None).stdout

def private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(value, f, indent=2)
    temporary.replace(path)

def http(method, url, data=None, token=None):
    headers = {"Content-Type": "application/json", "User-Agent": "monoweb-preview-controller"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, method=method, headers=headers,
        data=json.dumps(data).encode() if data is not None else None)
    for attempt in range(8):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                body = response.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as error:
            if error.code == 429 and attempt < 7:
                try:
                    delay = float(error.headers.get("Retry-After", "0"))
                    if delay <= 0:
                        delay = float(error.headers.get("X-RateLimit-Reset", "0")) - time.time() + 1
                except ValueError:
                    delay = 0
                time.sleep(min(60, max(2 ** attempt, delay)))
                continue
            if method == "DELETE" and error.code == 404:
                return None
            details = json.loads(error.read() or b"{}")
            raise RuntimeError(f"{method} {url}: HTTP {error.code}: {details.get('message', details.get('error', 'request failed'))}; fields: {', '.join(details.get('errors', {}))}") from None

class Controller:
    def __init__(self):
        self.config = json.loads((ROOT / "secrets/controller.json").read_text())
        self.directory = Path(__file__).resolve().parent
        self.states = ROOT / "states"
        self.states.mkdir(exist_ok=True, mode=0o700)
        self.auth_token = None

    def state_path(self, preview):
        if not PREVIEW_ID.fullmatch(preview):
            raise ValueError("Expected main, poc, or pr-N")
        return self.states / f"{preview}.json"

    def auth(self, method, path, data=None):
        domain = self.config["auth0_domain"]
        if self.auth_token is None:
            result = http("POST", f"https://{domain}/oauth/token", {
                "grant_type": "client_credentials", "client_id": self.config["auth0_client_id"],
                "client_secret": self.config["auth0_client_secret"], "audience": f"https://{domain}/api/v2/",
            })
            self.auth_token = result["access_token"]
        return http(method, f"https://{domain}/api/v2{path}", data, self.auth_token)

    def pscale(self, *args):
        environment = {**SAFE_ENV,
            "PLANETSCALE_SERVICE_TOKEN_ID": self.config["planetscale_token_id"],
            "PLANETSCALE_SERVICE_TOKEN": self.config["planetscale_token"],
        }
        process = subprocess.run(["pscale", *args, "--org", self.config["planetscale_org"], "--format", "json"],
            env=environment, text=True, capture_output=True)
        try:
            result = json.loads(process.stdout)
        except json.JSONDecodeError:
            raise RuntimeError(f"PlanetScale {args[0]} {args[1]} failed (exit {process.returncode})") from None
        if process.returncode or (isinstance(result, dict) and result.get("status") in ("error", "action_required")):
            raise RuntimeError(f"PlanetScale {args[0]} {args[1]}: {result.get('error', 'request failed')}")
        return result

    def expected_commit(self, preview):
        base = f"https://api.github.com/repos/{self.config['repository']}"
        if preview == "main":
            return http("GET", base + "/commits/main")["sha"]
        if preview == "poc":
            return None  # Only accepted through the administrator CLI, never the SSH dispatcher.
        pr = http("GET", base + "/pulls/" + preview.removeprefix("pr-"))
        if pr["state"] != "open" or pr["head"]["repo"]["full_name"] != self.config["repository"]:
            raise ValueError("Preview requires an open PR from this repository")
        return pr["head"]["sha"]

    def isolate_connection(self, state):
        # Auth0 may automatically enable existing tenant connections for new
        # applications. Remove only this preview client's inherited associations.
        connections = self.auth("GET", "/connections?fields=id,name&include_fields=true&per_page=100")
        client_id = state["web_client"]["client_id"]
        for connection in connections:
            if connection["id"] == state["connection"]["id"]:
                continue
            result = self.auth("GET", "/connections/" + connection["id"] + "/clients")
            if any(client.get("client_id") == client_id for client in result["clients"]):
                self.auth("PATCH", "/connections/" + connection["id"] + "/clients", [{"client_id": client_id, "status": False}])

    def provision(self, preview):
        path = self.state_path(preview)
        label = preview if preview == "main" else preview + "-web"
        origin = f"https://{label}.{self.config['base_domain']}"
        if path.exists():
            state = json.loads(path.read_text())
            if state["origin"] != origin:
                settings = {
                    "callbacks": [origin + "/api/auth/callback/auth0", origin + "/api/auth/link-identity/callback"],
                    "allowed_logout_urls": [origin, origin + "/innstillinger/bruker"], "web_origins": [origin],
                }
                if "web_client" in state:
                    self.auth("PATCH", "/clients/" + state["web_client"]["client_id"], settings)
                    state["web_client"].update(settings)
                state["origin"] = origin
                private_json(path, state)
        else:
            active = [p for p in self.states.glob("pr-*.json")]
            if preview.startswith("pr-") and len(active) >= self.config.get("max_pr_previews", 2):
                raise RuntimeError("Preview capacity reached; remove an unused PR preview first")
            state = {"id": preview, "branch": "coolify-" + preview,
                "origin": origin,
                "rpc_origin": f"https://{preview}-api.{self.config['base_domain']}",
                "auth_secret": secrets.token_hex(32), "signing_key": secrets.token_hex(32),
                "status": "provisioning"}
            private_json(path, state)
        def save():
            private_json(path, state)
        database = self.config["planetscale_database"]
        branches = self.pscale("branch", "list", database)
        if not any(b["name"] == state["branch"] for b in branches):
            self.pscale("branch", "create", database, state["branch"], "--region", "eu-central", "--cluster-size", "PS_DEV")
        for _ in range(90):
            branch = self.pscale("branch", "show", database, state["branch"])
            if branch.get("ready"):
                break
            time.sleep(5)
        else:
            raise RuntimeError("PlanetScale branch did not become ready")
        state["branch_id"] = branch["id"]
        save()
        for kind, inherited in [("migrator", "postgres"), ("runtime", "pg_read_all_data,pg_write_all_data")]:
            if "db_" + kind not in state:
                state["db_" + kind] = self.pscale("role", "create", database, state["branch"],
                    "preview_" + kind, "--inherited-roles", inherited)
                save()
        if "web_client" not in state:
            state["web_client"] = self.auth("POST", "/clients", {
                "name": f"Monoweb preview {preview} web", "app_type": "regular_web", "is_first_party": True,
                "callbacks": [state["origin"] + "/api/auth/callback/auth0", state["origin"] + "/api/auth/link-identity/callback"],
                "allowed_logout_urls": [state["origin"], state["origin"] + "/innstillinger/bruker"],
                "web_origins": [state["origin"]], "grant_types": ["authorization_code", "refresh_token", "client_credentials"],
                "jwt_configuration": {"alg": "RS256"},
            })
            save()
        if "rpc_client" not in state:
            state["rpc_client"] = self.auth("POST", "/clients", {
                "name": f"Monoweb preview {preview} rpc", "app_type": "non_interactive",
                "grant_types": ["client_credentials"], "jwt_configuration": {"alg": "RS256"},
            })
            save()
        if "api" not in state:
            state["api"] = self.auth("POST", "/resource-servers", {
                "name": f"Monoweb preview {preview}", "identifier": state["rpc_origin"],
                "signing_alg": "RS256", "token_dialect": "access_token_authz", "allow_offline_access": True,
                "scopes": [{"value": "read:calendar_feed", "description": "Read preview calendar"}],
            })
            save()
        if "rpc_grant" not in state:
            state["rpc_grant"] = self.auth("POST", "/client-grants", {
                "client_id": state["rpc_client"]["client_id"], "audience": f"https://{self.config['auth0_domain']}/api/v2/",
                "scope": ["read:users", "update:users", "read:user_idp_tokens"],
            })
            save()
        if "connection" not in state:
            state["connection"] = self.auth("POST", "/connections", {
                "name": "Monoweb-Preview-" + preview, "strategy": "auth0",
                "enabled_clients": [state["web_client"]["client_id"], self.config["auth0_client_id"]],
                "options": {"passwordPolicy": "good", "disable_signup": True, "requires_username": False},
            })
            save()
        self.auth("PATCH", "/connections/" + state["connection"]["id"] + "/clients", [
            {"client_id": client_id, "status": True}
            for client_id in [state["web_client"]["client_id"], self.config["auth0_client_id"]]
        ])
        self.isolate_connection(state)
        if "admin" not in state:
            password = secrets.token_urlsafe(24) + "aA1!"
            state["admin"] = self.auth("POST", "/users", {
                "connection": state["connection"]["name"], "email": f"admin.{preview}@preview.example.com",
                "name": "Preview Administrator", "password": password,
                "email_verified": True, "verify_email": False,
            })
            state["admin_password"] = password
            save()
        return state

    def coolify(self, method, path, data=None):
        return http(method, self.config['coolify_url'] + '/api/v1' + path, data, self.config['coolify_token'])

    def configuration(self, state):
        # JSON configuration on stdout, also supported by Doppler secrets download.
        from string import Template
        context = {
            'WEB_CLIENT_ID': state['web_client']['client_id'], 'WEB_CLIENT_SECRET': state['web_client']['client_secret'],
            'RPC_CLIENT_ID': state['rpc_client']['client_id'], 'RPC_CLIENT_SECRET': state['rpc_client']['client_secret'],
            'AUTH0_DOMAIN': self.config['auth0_domain'], 'ORIGIN': state['origin'], 'RPC_ORIGIN': state['rpc_origin'],
            'DATABASE_URL': state['db_runtime']['database_url'], 'AUTH_SECRET': state['auth_secret'],
            'SIGNING_KEY': state['signing_key'],
        }
        values = {}
        for application in ('web', 'rpc'):
            command = [part.replace('{application}', application) for part in self.config['environment_command']]
            source = json.loads(run(command, capture=True, env={**SAFE_ENV, **self.config.get('environment_source_credentials', {})}))
            for key, value in source.items():
                if not re.fullmatch(r'[A-Z][A-Z0-9_]*', key) or not isinstance(value, str):
                    raise ValueError('Expected a JSON object of environment names and string values')
                name = application.upper() + '_' + key if key in ('AUTH0_CLIENT_ID', 'AUTH0_CLIENT_SECRET') else key
                rendered = Template(value).substitute(context)
                if name in values and values[name] != rendered:
                    raise ValueError('Conflicting shared configuration: ' + name)
                values[name] = rendered
        return values

    def deploy(self, preview, sha, branch=None):
        if not SHA.fullmatch(sha) or self.expected_commit(preview) != sha:
            raise ValueError('Refusing mismatched source commit')
        if branch is None and preview.startswith('pr-'):
            branch = http('GET', f'https://api.github.com/repos/{self.config["repository"]}/pulls/{preview[3:]}')['head']['ref']
        state = self.provision(preview)
        values = self.configuration(state)
        domains = [{'name': 'web', 'domain': state['origin'] + ':3000'}, {'name': 'rpc', 'domain': state['rpc_origin'] + ':4444'}]
        # Run the independently installed build hook in a helper sharing Coolify's checkout.
        build_command = "sh -c 'docker run --rm --volumes-from \"$HOSTNAME\" --env-file /artifacts/build-time.env -v /opt/monoweb-previews/controller:/control:ro -w \"$PWD\" coollabsio/coolify-helper:1.0.17 sh /control/build.sh'"
        definition = {
            'name': 'monoweb-' + preview, 'git_repository': 'https://github.com/' + self.config['repository'],
            'git_branch': branch or 'main', 'git_commit_sha': sha, 'build_pack': 'dockercompose',
            'docker_compose_location': '/infra/preview/compose.yaml', 'docker_compose_raw': (self.directory / 'compose.yaml').read_text(),
            'docker_compose_domains': domains, 'docker_compose_custom_build_command': build_command,
            'is_auto_deploy_enabled': False, 'is_preview_deployments_enabled': False, 'instant_deploy': False,
            'use_build_secrets': True, 'is_preserve_repository_enabled': True,
        }
        if 'coolify_uuid' not in state:
            app = self.coolify('POST', '/applications/public', {**definition, 'project_uuid': self.config['coolify_project_uuid'],
                'server_uuid': self.config['coolify_server_uuid'], 'environment_name': 'preview', 'ports_exposes': '3000'})
            state['coolify_uuid'] = app['uuid']
            private_json(self.state_path(preview), state)
        else:
            # The update endpoint uses the saved Compose definition supplied at creation.
            update = {k:v for k,v in definition.items() if k not in ('docker_compose_raw', 'docker_compose_domains')}
            update['git_repository'] = self.config['repository']
            self.coolify('PATCH', '/applications/' + state['coolify_uuid'], update)
        entries = [{'key': k, 'value': v, 'is_runtime': True, 'is_buildtime': True, 'is_literal': True} for k,v in values.items()]
        entries.extend({'key': k, 'value': v, 'is_runtime': False, 'is_buildtime': True, 'is_literal': True} for k,v in {
            'MIGRATION_DATABASE_URL': state['db_migrator']['database_url'], 'PREVIEW_ADMIN_ID': state['admin']['user_id'],
            'PREVIEW_ADMIN_EMAIL': state['admin']['email']}.items())
        self.coolify('PATCH', '/applications/' + state['coolify_uuid'] + '/envs/bulk', {'data': entries})
        deployment = self.coolify('POST', '/deploy', {'uuid': state['coolify_uuid'], 'force': True})
        state.update(sha=sha, deployment=deployment, status='queued')
        private_json(self.state_path(preview), state)
        print('Queued in Coolify: ' + state['origin'], flush=True)

    def destroy(self, preview):
        if not re.fullmatch(r'pr-[1-9][0-9]*', preview):
            raise ValueError('Only PR previews can be deleted')
        state = json.loads(self.state_path(preview).read_text())
        if state['id'] != preview or state['branch'] != 'coolify-' + preview:
            raise ValueError('Resource ownership mismatch')
        if 'coolify_uuid' in state:
            self.coolify('DELETE', '/applications/' + state['coolify_uuid'])
        if 'admin' in state: self.auth('DELETE', '/users/' + state['admin']['user_id'])
        if 'connection' in state: self.auth('DELETE', '/connections/' + state['connection']['id'])
        for key in ('web_client', 'rpc_client'):
            if key in state: self.auth('DELETE', '/clients/' + state[key]['client_id'])
        if 'api' in state: self.auth('DELETE', '/resource-servers/' + state['api']['id'])
        self.pscale('branch', 'delete', self.config['planetscale_database'], state['branch'], '--force')
        self.state_path(preview).unlink()

    def reconcile(self):
        base = f'https://api.github.com/repos/{self.config["repository"]}'
        # One list request, rather than polling every PR. GitHub's public unauthenticated quota is sufficient at 5 minutes.
        prs = http('GET', base + '/pulls?state=open&per_page=100')
        desired = {'main': (http('GET', base + '/commits/main')['sha'], 'main')}
        for pr in prs:
            if pr['head']['repo'] and pr['head']['repo']['full_name'] == self.config['repository']:
                desired['pr-' + str(pr['number'])] = (pr['head']['sha'], pr['head']['ref'])
        # Clean first to release capacity. Never remove main.
        for path in self.states.glob('pr-*.json'):
            if path.stem not in desired: self.destroy(path.stem)
        for preview, (sha, branch) in desired.items():
            path = self.state_path(preview)
            state = json.loads(path.read_text()) if path.exists() else {}
            if state.get('sha') != sha or 'coolify_uuid' not in state:
                self.deploy(preview, sha, branch)


def main():
    os.umask(0o077)
    ROOT.mkdir(exist_ok=True)
    with (ROOT / 'controller.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        controller = Controller()
        if sys.argv[1:] == ['reconcile']: controller.reconcile()
        elif len(sys.argv) == 4 and sys.argv[1] == 'deploy': controller.deploy(sys.argv[2], sys.argv[3])
        elif len(sys.argv) == 3 and sys.argv[1] == 'destroy': controller.destroy(sys.argv[2])
        else: raise ValueError('Usage: previewctl.py reconcile | deploy ID SHA | destroy pr-N')

if __name__ == '__main__':
    main()
