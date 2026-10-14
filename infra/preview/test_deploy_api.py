import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

spec = importlib.util.spec_from_file_location("deploy_api", Path(__file__).with_name("deploy_api.py"))
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)

class DeploymentApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        token = root / "token"
        token.write_text("test-api-token")
        self.paths = patch.multiple(api, DIRECTORY=root, TOKEN_FILE=token, ACTIVE=set())
        self.paths.start()
        self.server = api.ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.paths.stop()
        self.directory.cleanup()

    def request(self, body=None, token="test-api-token", path="/jobs"):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(self.url + path, headers=headers,
            data=json.dumps(body).encode() if body is not None else None)
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.code, json.load(response)

    def test_unauthenticated_requests_cannot_queue_or_read_jobs(self):
        body = {"action": "deploy", "preview": "main", "sha": "a" * 40}
        self.assertEqual(self.request(body, token=None)[0], 401)
        self.assertEqual(self.request(token="wrong", path="/jobs/" + "a" * 32)[0], 401)
        self.assertEqual(list(Path(self.directory.name).glob("*.json")), [])

    def test_only_scoped_preview_operations_are_accepted(self):
        for body in (
            {"action": "destroy", "preview": "main"},
            {"action": "deploy", "preview": "pr-1; id", "sha": "a" * 40},
            {"action": "deploy", "preview": "pr-1", "sha": "main"},
            {"action": "shell", "preview": "main"},
        ):
            with self.subTest(body=body):
                self.assertEqual(self.request(body)[0], 400)

    def test_valid_request_queues_only_the_validated_command(self):
        called = threading.Event()
        commands = []
        def execute(job, command):
            commands.append(command)
            api.save(api.DIRECTORY / (job + ".json"), {"job": job, "status": "success"})
            called.set()
        with patch.object(api, "execute", execute):
            code, job = self.request({"action": "deploy", "preview": "pr-12", "sha": "a" * 40})
            self.assertEqual(code, 202)
            self.assertTrue(called.wait(5))
            self.assertEqual(commands, ["deploy pr-12 " + "a" * 40])
            self.assertEqual(self.request(path="/jobs/" + job["job"])[1]["status"], "success")

    def test_job_path_cannot_traverse_files(self):
        self.assertEqual(self.request(path="/jobs/../../token")[0], 404)

if __name__ == "__main__":
    unittest.main()
