#!/usr/bin/env python3
"""Unprivileged HTTPS deployment backend; Caddy supplies TLS termination."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time
import uuid

DIRECTORY = Path(os.environ.get("PREVIEW_API_STATE", "/home/preview-deploy/.local/state/preview-api"))
TOKEN_FILE = Path(os.environ.get("PREVIEW_API_TOKEN_FILE", "/home/preview-deploy/.config/preview-api-token"))
PREVIEW = re.compile(r"^(main|pr-[1-9][0-9]*)$")
SHA = re.compile(r"^[0-9a-f]{40}$")
JOB = re.compile(r"^[0-9a-f]{32}$")
LOCK = threading.Lock()
ACTIVE = set()

def save(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value))
    temporary.replace(path)

def execute(job, command):
    path = DIRECTORY / (job + ".json")
    try:
        with (DIRECTORY / (job + ".log")).open("w") as log:
            environment = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "HOME": "/home/preview-deploy", "PREVIEW_COMMAND": command}
            result = subprocess.run(["sudo", "-n", "/opt/monoweb-preview/controller/previewctl.py", "dispatch"],
                env=environment, stdout=log, stderr=subprocess.STDOUT)
        save(path, {"job": job, "status": "success" if result.returncode == 0 else "failure", "finished_at": int(time.time())})
    except Exception:
        save(path, {"job": job, "status": "failure"})
    finally:
        with LOCK:
            ACTIVE.discard(job)

class Handler(BaseHTTPRequestHandler):
    server_version = "PreviewDeploy"
    def log_message(self, *_):
        pass  # Never log authentication headers or request bodies.

    def respond(self, status, body):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def authenticated(self):
        expected = "Bearer " + TOKEN_FILE.read_text().strip()
        if not secrets.compare_digest(self.headers.get("Authorization", "").encode(), expected.encode()):
            self.respond(401, {"error": "Unauthorized"})
            return False
        return True

    def do_GET(self):
        if not self.authenticated():
            return
        job = self.path.removeprefix("/jobs/")
        if not self.path.startswith("/jobs/") or not JOB.fullmatch(job):
            return self.respond(404, {"error": "Not found"})
        path = DIRECTORY / (job + ".json")
        if not path.exists():
            return self.respond(404, {"error": "Not found"})
        result = json.loads(path.read_text())
        if result["status"] != "running":
            log = DIRECTORY / (job + ".log")
            result["log_tail"] = "\n".join(log.read_text(errors="replace").splitlines()[-80:])[-16000:] if log.exists() else ""
        self.respond(200, result)

    def do_POST(self):
        if not self.authenticated():
            return
        if self.path != "/jobs":
            return self.respond(404, {"error": "Not found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError()
            body = json.loads(self.rfile.read(length))
            action, preview = body["action"], body["preview"]
            if not PREVIEW.fullmatch(preview):
                raise ValueError()
            if action == "deploy" and SHA.fullmatch(body.get("sha", "")):
                command = f"deploy {preview} {body['sha']}"
            elif action == "destroy" and preview.startswith("pr-"):
                command = f"destroy {preview}"
            else:
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            return self.respond(400, {"error": "Invalid preview operation"})
        with LOCK:
            if len(ACTIVE) >= 6:
                return self.respond(429, {"error": "Deployment queue full"})
            job = uuid.uuid4().hex
            save(DIRECTORY / (job + ".json"), {"job": job, "status": "running", "started_at": int(time.time())})
            ACTIVE.add(job)
        threading.Thread(target=execute, args=(job, command), daemon=True).start()
        self.respond(202, {"job": job, "status": "running"})

if __name__ == "__main__":
    os.umask(0o077)
    DIRECTORY.mkdir(parents=True, exist_ok=True, mode=0o700)
    for path in DIRECTORY.glob("*.json"):
        status = json.loads(path.read_text())
        if status.get("status") == "running":
            save(path, {"job": path.stem, "status": "failure", "reason": "Deployment service restarted"})
    ThreadingHTTPServer.daemon_threads = True
    server = ThreadingHTTPServer((os.environ.get("PREVIEW_API_BIND", "172.18.0.1"), 9090), Handler)
    server.serve_forever()
