#!/usr/bin/env python3
"""Trusted host controller. Application builds never receive control-plane credentials."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request

ROOT = Path(os.environ.get("PREVIEW_ROOT", "/opt/monoweb-preview"))
SAFE_ENV = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root", "LANG": "C.UTF-8"}
PREVIEW_ID = re.compile(r"^(main|poc|pr-[1-9][0-9]*)$")
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

def write_env(path, values):
    # Docker --env-file parses literal values; reject line injection.
    if any("\n" in str(v) or "\r" in str(v) for v in values.values()):
        raise ValueError("Invalid multiline environment value")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("".join(f"{k}={v}\n" for k, v in values.items()))

def http(method, url, data=None, token=None):
    headers = {"Content-Type": "application/json", "User-Agent": "monoweb-preview-controller"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, method=method, headers=headers,
        data=json.dumps(data).encode() if data is not None else None)
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            body = response.read()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as error:
        if method == "DELETE" and error.code == 404:
            return None
        details = json.loads(error.read() or b"{}")
        raise RuntimeError(f"{method} {url}: HTTP {error.code}: {details.get('message', details.get('error', 'request failed'))}") from None

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

    def provision(self, preview):
        path = self.state_path(preview)
        if path.exists():
            state = json.loads(path.read_text())
        else:
            active = [p for p in self.states.glob("pr-*.json")]
            if preview.startswith("pr-") and len(active) >= self.config.get("max_pr_previews", 2):
                raise RuntimeError("Preview capacity reached; remove an unused PR preview first")
            state = {"id": preview, "branch": "preview-main" if preview == "main" else preview,
                "origin": f"https://{preview}.{self.config['base_domain']}",
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

    def environments(self, state, stack):
        domain = self.config["auth0_domain"]
        common = {"DOPPLER_ENVIRONMENT": "dev", "OTEL_SDK_DISABLED": "1", "AUTH0_ISSUER": "https://" + domain,
                  "AUTH0_AUDIENCES": state["rpc_origin"]}
        web = {**common, "AUTH0_CLIENT_ID": state["web_client"]["client_id"],
            "AUTH0_CLIENT_SECRET": state["web_client"]["client_secret"], "AUTH_SECRET": state["auth_secret"],
            "SIGNING_KEY": state["signing_key"], "RPC_HOST": "http://rpc:4444", "NEXT_PUBLIC_ORIGIN": state["origin"],
            "NEXT_PUBLIC_RPC_HOST": state["rpc_origin"], "AWS_CLOUDFRONT_URL": state["origin"],
            "NEXT_PUBLIC_TURNSTILE_SITE_KEY": "1x00000000000000000000AA"}
        rpc = {**common, "AUTH0_CLIENT_ID": state["rpc_client"]["client_id"],
            "AUTH0_CLIENT_SECRET": state["rpc_client"]["client_secret"], "AUTH0_MGMT_TENANT": domain,
            "AUTH0_WEB_CLIENT_ID": state["web_client"]["client_id"], "AUTH0_WEB_CLIENT_SECRET": state["web_client"]["client_secret"],
            "DATABASE_URL": state["db_runtime"]["database_url"], "WEB_PUBLIC_ORIGIN": state["origin"],
            "ALLOWED_ORIGINS": state["origin"], "UNSAFE_DISABLE_AUTHORIZATION": "false",
            "AWS_REGION": "eu-north-1", "AWS_S3_BUCKET": "preview-uploads-unconfigured",
            "AWS_EC2_METADATA_DISABLED": "true", "STRIPE_SECRET_KEY": "sk_test_preview_payments_unconfigured",
            "STRIPE_WEBHOOK_IDENTIFIER": "dev", "TURNSTILE_SECRET_KEY": "1x0000000000000000000000000000000AA"}
        write_env(stack / "web.env", web)
        write_env(stack / "rpc.env", rpc)

    def source(self, preview, sha):
        repo = ROOT / "source.git"
        if not repo.exists():
            run(["git", "clone", "--bare", "https://github.com/" + self.config["repository"] + ".git", str(repo)])
        run(["git", "--git-dir", str(repo), "fetch", "--depth=1", "origin", sha])
        build = ROOT / "builds" / preview / sha
        build.mkdir(parents=True, exist_ok=True)
        archive = build / "source.tar"
        with archive.open("wb") as f:
            subprocess.run(["git", "--git-dir", str(repo), "archive", sha], env=SAFE_ENV, stdout=f, check=True)
        with tarfile.open(archive) as tar:
            tar.extractall(build, filter="data")
        archive.unlink()
        overlay = build / "infra/preview"
        overlay.mkdir(parents=True, exist_ok=True)
        for name in ("Dockerfile.web", "Dockerfile.rpc", "Dockerfile.tools", "seed.ts"):
            shutil.copyfile(self.directory / name, overlay / name)
        ignore = build / ".dockerignore"
        ignore.write_text(ignore.read_text().replace("\ninfra\n", "\ninfra/*\n!infra/preview/\n") + "\n**/.env*\n.preview-state\n")
        config = build / "apps/web/next.config.ts"
        text = config.read_text()
        if "PREVIEW_STANDALONE" not in text:
            text = 'import path from "node:path"\n' + text.replace("const config: NextConfig = {", 'const config: NextConfig = {\n  output: "standalone",\n  outputFileTracingRoot: path.resolve(process.cwd(), "../.."),')
            config.write_text(text)
        return build

    def compose(self, state, stack, tag):
        services = {}
        for name, port, memory in [("web", 3000, "1g"), ("rpc", 4444, "768m")]:
            services[name] = {"image": f"monoweb-preview-{name}:{tag}", "restart": "unless-stopped",
                "env_file": [str(stack / (name + ".env"))], "mem_limit": memory, "cpus": 1.5,
                "security_opt": ["no-new-privileges:true"], "cap_drop": ["ALL"],
                "networks": {"default": {}, "preview-edge": {"aliases": [state["id"] + "-" + name]}},
                "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "2"}},
                "healthcheck": {"test": ["CMD", "node", "-e", f"fetch('http://127.0.0.1:{port}{'/health' if name == 'rpc' else '/api/auth/session'}').then(r=>process.exit(r.status<500?0:1)).catch(()=>process.exit(1))"], "interval": "15s", "timeout": "10s", "retries": 6, "start_period": "40s"}}
        # Separate application networks; edge network is only for the reverse proxy.
        services["rpc"]["networks"] = {"default": {}, "preview-edge": {"aliases": [state["id"] + "-rpc"]}}
        manifest = {"services": services, "networks": {"preview-edge": {"external": True}}}
        (stack / "compose.json").write_text(json.dumps(manifest, indent=2))

    def deploy(self, preview, sha):
        if not SHA.fullmatch(sha):
            raise ValueError("Deployment requires a full commit SHA")
        expected = self.expected_commit(preview)
        if expected and expected != sha:
            raise ValueError("Refusing an outdated or mismatched deployment commit")
        print(f"Provisioning {preview}", flush=True)
        state = self.provision(preview)
        path = self.state_path(preview)
        build = self.source(preview, sha)
        stack = ROOT / "stacks" / preview
        stack.mkdir(parents=True, exist_ok=True, mode=0o700)
        tag = preview + "-" + sha[:12]
        tools = "monoweb-preview-tools:" + tag
        run(["docker", "build", "--progress=plain", "-f", str(self.directory / "Dockerfile.tools"), "-t", tools, "."], cwd=build)
        migration_file = stack / "migration.env"
        write_env(migration_file, {"DATABASE_URL": state["db_migrator"]["database_url"], "PREVIEW_ID": preview,
            "PREVIEW_ADMIN_ID": state["admin"]["user_id"], "PREVIEW_ORIGIN": state["origin"]})
        try:
            args = ["docker", "run", "--rm", "--env-file", str(migration_file), tools]
            run([*args, "pnpm", "-F", "@dotkomonline/db", "exec", "prisma", "migrate", "deploy"])
            run(["docker", "run", "--rm", "--env-file", str(migration_file), "-v",
                 f"{build}/packages/db/generated:/app/packages/db/generated", tools,
                 "pnpm", "-F", "@dotkomonline/db", "generate"])
            run([*args, "pnpm", "-F", "@dotkomonline/db", "exec", "tsx", "/app/infra/preview/seed.ts"])
        finally:
            migration_file.unlink(missing_ok=True)
        run(["docker", "build", "--progress=plain", "-f", str(self.directory / "Dockerfile.rpc"), "-t", "monoweb-preview-rpc:" + tag, "."], cwd=build)
        run(["docker", "build", "--progress=plain", "-f", str(self.directory / "Dockerfile.web"), "-t", "monoweb-preview-web:" + tag,
            "--build-arg", "NEXT_PUBLIC_ORIGIN=" + state["origin"], "--build-arg", "NEXT_PUBLIC_RPC_HOST=" + state["rpc_origin"], "."], cwd=build)
        if expected and self.expected_commit(preview) != sha:
            raise ValueError("Branch changed during build; refusing stale deployment")
        self.environments(state, stack)
        self.compose(state, stack, tag)
        with contextlib.suppress(subprocess.CalledProcessError):
            run(["docker", "network", "create", "preview-edge"], capture=True)
        run(["docker", "compose", "-p", "preview-" + preview, "-f", str(stack / "compose.json"), "up", "-d", "--wait", "--wait-timeout", "180", "--remove-orphans"])
        route = Path("/etc/caddy/previews") / (preview + ".caddy")
        route.parent.mkdir(exist_ok=True)
        route.write_text(f"{state['origin']} {{\n header X-Robots-Tag \"noindex, nofollow\"\n reverse_proxy {preview}-web:3000\n}}\n{state['rpc_origin']} {{\n header X-Robots-Tag \"noindex, nofollow\"\n reverse_proxy {preview}-rpc:4444\n}}\n")
        run(["docker", "exec", "preview-caddy", "caddy", "validate", "--config", "/etc/caddy/Caddyfile"])
        run(["docker", "exec", "preview-caddy", "caddy", "reload", "--config", "/etc/caddy/Caddyfile"])
        state.update(status="running", sha=sha, deployed_at=int(time.time()))
        private_json(path, state)
        print("Preview ready: " + state["origin"], flush=True)

    def destroy(self, preview):
        if not preview.startswith("pr-") and preview != "poc":
            raise ValueError("The persistent main preview cannot be destroyed by automation")
        path = self.state_path(preview)
        if not path.exists():
            return
        state = json.loads(path.read_text())
        if state["id"] != preview or state["branch"] != preview:
            raise ValueError("Resource ownership mismatch")
        stack = ROOT / "stacks" / preview
        if (stack / "compose.json").exists():
            run(["docker", "compose", "-p", "preview-" + preview, "-f", str(stack / "compose.json"), "down", "--remove-orphans"])
        (Path("/etc/caddy/previews") / (preview + ".caddy")).unlink(missing_ok=True)
        run(["docker", "exec", "preview-caddy", "caddy", "reload", "--config", "/etc/caddy/Caddyfile"])
        if "admin" in state:
            self.auth("DELETE", "/users/" + state["admin"]["user_id"])
        if "connection" in state:
            self.auth("DELETE", "/connections/" + state["connection"]["id"])
        for key in ("web_client", "rpc_client"):
            if key in state:
                self.auth("DELETE", "/clients/" + state[key]["client_id"])
        if "api" in state:
            self.auth("DELETE", "/resource-servers/" + state["api"]["id"])
        branches = self.pscale("branch", "list", self.config["planetscale_database"])
        if any(b["name"] == preview for b in branches):
            self.pscale("branch", "delete", self.config["planetscale_database"], preview, "--force")
        shutil.rmtree(stack, ignore_errors=True)
        shutil.rmtree(ROOT / "builds" / preview, ignore_errors=True)
        # Delete only images belonging to this preview, never shared images or volumes.
        images = run(["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}"], capture=True).splitlines()
        for image in images:
            if re.fullmatch(r"monoweb-preview-(web|rpc|tools):" + re.escape(preview) + r"-[0-9a-f]{12}", image):
                run(["docker", "image", "rm", image])
        path.unlink()
        print("Removed preview " + preview)

    def reconcile(self):
        for path in self.states.glob("pr-*.json"):
            preview = path.stem
            pr = http("GET", f"https://api.github.com/repos/{self.config['repository']}/pulls/{preview.removeprefix('pr-')}")
            if pr["state"] == "closed":
                self.destroy(preview)

def main():
    os.umask(0o077)
    args = sys.argv[1:]
    if args == ["ssh"]:
        args = shlex.split(os.environ.get("SSH_ORIGINAL_COMMAND", ""))
        if not args or args[0] not in ("deploy", "destroy", "list") or (len(args) > 1 and args[1] == "poc"):
            raise ValueError("Allowed commands: deploy main|pr-N SHA, destroy pr-N, list")
    ROOT.mkdir(exist_ok=True)
    with (ROOT / "controller.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        controller = Controller()
        if len(args) == 3 and args[0] == "deploy":
            controller.deploy(args[1], args[2])
        elif len(args) == 2 and args[0] == "destroy":
            controller.destroy(args[1])
        elif args == ["reconcile"]:
            controller.reconcile()
        elif args == ["list"]:
            for path in controller.states.glob("*.json"):
                s = json.loads(path.read_text())
                print(json.dumps({k: s.get(k) for k in ("id", "status", "origin", "branch", "sha")}))
        else:
            raise ValueError("Usage: previewctl deploy ID SHA | destroy pr-N | list | reconcile")

if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Preview failed: {error}", file=sys.stderr)
        sys.exit(1)
