import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("previewctl", Path(__file__).with_name("previewctl.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PreviewSecurityTests(unittest.TestCase):
    def controller(self):
        controller = module.Controller.__new__(module.Controller)
        controller.config = {"auth0_domain": "preview.eu.auth0.com", "planetscale_token": "control-plane-only"}
        return controller

    def test_application_environments_exclude_control_and_migration_credentials(self):
        controller = self.controller()
        state = {
            "id": "pr-12",
            "web_client": {"client_id": "web-id", "client_secret": "web-secret"},
            "rpc_client": {"client_id": "rpc-id", "client_secret": "rpc-secret"},
            "auth_secret": "session-secret", "signing_key": "signing-secret",
            "db_runtime": {"database_url": "runtime-branch-url"},
            "db_migrator": {"database_url": "migration-admin-url"},
            "origin": "https://pr-12.preview.example.com", "rpc_origin": "https://pr-12-api.preview.example.com",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            controller.environments(state, root)
            web, rpc = (root / "web.env").read_text(), (root / "rpc.env").read_text()
            self.assertNotIn("runtime-branch-url", web)
            self.assertIn("RPC_HOST=http://pr-12-rpc:4444", web)
            self.assertIn("runtime-branch-url", rpc)
            for text in (web, rpc):
                self.assertNotIn("migration-admin-url", text)
                self.assertNotIn("control-plane-only", text)
                self.assertNotIn("WORKSPACE_SERVICE_ACCOUNT", text)
            self.assertIn("UNSAFE_DISABLE_AUTHORIZATION=false", rpc)
            self.assertEqual((root / "rpc.env").stat().st_mode & 0o777, 0o600)

    def test_main_and_path_traversal_cannot_be_destroyed(self):
        controller = self.controller()
        for preview in ("main", "../main", "pr-1/../../main", "pr-1;id"):
            with self.subTest(preview=preview), self.assertRaises(ValueError):
                controller.destroy(preview)

    def test_wrong_commit_fails_before_provisioning(self):
        controller = self.controller()
        with patch.object(controller, "expected_commit", return_value="a" * 40), patch.object(controller, "provision") as provision:
            with self.assertRaises(ValueError):
                controller.deploy("main", "b" * 40)
            provision.assert_not_called()

    def test_environment_file_rejects_line_injection(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                module.write_env(Path(directory) / "rpc.env", {"ALLOWED_ORIGINS": "example.com\nUNSAFE_DISABLE_AUTHORIZATION=true"})

    def test_cleanup_refuses_mismatched_resource_ownership(self):
        controller = self.controller()
        with tempfile.TemporaryDirectory() as directory:
            controller.states = Path(directory)
            (controller.states / "pr-12.json").write_text(json.dumps({"id": "pr-12", "branch": "main"}))
            with self.assertRaises(ValueError):
                controller.destroy("pr-12")


if __name__ == "__main__":
    unittest.main()
