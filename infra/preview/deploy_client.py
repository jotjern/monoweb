#!/usr/bin/env python3
import json
import os
import sys
import time
import urllib.request

url = os.environ["PREVIEW_DEPLOY_URL"].rstrip("/")
if not url.startswith("https://"):
    raise ValueError("Deployment API must use HTTPS")
token = os.environ["PREVIEW_DEPLOY_TOKEN"]

def request(path, body=None):
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    req = urllib.request.Request(url + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
    with urllib.request.urlopen(req, timeout=45) as response:
        return json.load(response)

action, preview = sys.argv[1:3]
body = {"action": action, "preview": preview}
if action == "deploy":
    body["sha"] = sys.argv[3]
job = request("/jobs", body)["job"]
print(f"Queued {action} {preview}: {job}", flush=True)
for _ in range(170):
    result = request("/jobs/" + job)
    if result["status"] != "running":
        print(result.get("log_tail", ""), flush=True)
        if result["status"] != "success":
            raise SystemExit("Preview operation failed")
        print("Preview operation succeeded", flush=True)
        break
    time.sleep(15)
else:
    raise SystemExit("Preview deployment timed out; inspect the host job log")
