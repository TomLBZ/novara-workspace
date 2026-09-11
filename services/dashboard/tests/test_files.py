#!/usr/bin/env python3
"""Unit tests for the ws-dashboard file browser backend (stdlib only).

Run: python3 services/dashboard/tests/test_files.py   (or tools/verify.sh, which calls it)
"""
from __future__ import annotations

import os
import pathlib
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from files import FileBrowser  # noqa: E402


def build_ws(root: pathlib.Path) -> None:
    (root / "projects" / "demo" / "src").mkdir(parents=True)
    (root / "projects" / "demo" / "readme.md").write_text("# demo\nhello\n")
    (root / "projects" / "demo" / "src" / "app.py").write_text("print('x')\n")
    (root / "projects" / "demo" / "blob.bin").write_bytes(b"\x00\x01\x02\x03")
    (root / "projects" / "demo" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    (root / "projects" / "demo" / "huge.bin").write_bytes(b"a" * 4096)
    (root / "config" / "keys").mkdir(parents=True)
    (root / "config" / "keys" / "id_ed25519").write_text("PRIVATE KEY\n")
    (root / "config.yaml").write_text("api_keys: {}\n")
    (root / ".env").write_text("SECRET=1\n")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n")
    outside = root.parent / "outside.txt"
    outside.write_text("not yours\n")
    (root / "projects" / "demo" / "escape").symlink_to(outside)


CFG = {"root": "projects", "admin_root": ".", "max_entries": 2000,
       "max_preview_bytes": 1024, "max_raw_bytes": 2048,
       "token_file": "config/dashboard-admin-token"}


class FileBrowserTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = pathlib.Path(self._tmp.name).resolve()
        build_ws(self.ws)
        self.fb = FileBrowser(self.ws, CFG)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- scopes ------------------------------------------------------------
    def test_default_scope_is_the_configured_root(self):
        self.assertEqual(self.fb.scope_for(None), "root")
        self.assertEqual(self.fb.describe("root")["browse_root"], "projects")

    def test_anonymous_sees_projects_only(self):
        payload, error, status = self.fb.listing("", "root")
        self.assertIsNone(error)
        self.assertEqual(status, 200)
        self.assertEqual([e["name"] for e in payload["entries"]], ["demo"])
        self.assertNotIn("config.yaml", [e["name"] for e in payload["entries"]])
        self.assertNotIn("config", [e["name"] for e in payload["entries"]])

    # -- traversal ---------------------------------------------------------
    def test_traversal_is_refused(self):
        for rel in ("..", "../config.yaml", "../../etc/passwd", "../.env"):
            payload, error, status = self.fb.listing(rel, "root")
            self.assertIsNone(payload, rel)
            self.assertEqual(status, 400, rel)
            self.assertIn("browse root", error)

    def test_absolute_path_is_treated_as_relative(self):
        payload, error, _ = self.fb.listing("/demo", "root")
        self.assertIsNone(error)
        self.assertEqual(payload["path"], "demo")

    def test_symlink_escape_is_refused(self):
        payload, error, _ = self.fb.preview("demo/escape", "root")
        self.assertIsNone(payload)
        self.assertIn("browse root", error)

    # -- deny list ---------------------------------------------------------
    def test_deny_list_blocks_secrets_even_when_unlocked(self):
        forbidden = ("config.yaml", ".env", "config/keys", "config/keys/id_ed25519",
                     ".git/config", "config/git-credentials")
        for rel in forbidden:
            payload, error, status = self.fb.preview(rel, "admin")
            self.assertIsNone(payload, rel)
            self.assertIn("deny list", error, rel)
            self.assertEqual(status, 400, rel)

    def test_deny_list_blocks_by_basename_globs(self):
        (self.ws / "projects" / "demo" / "server.pem").write_text("key\n")
        payload, error, _ = self.fb.preview("demo/server.pem", "admin")
        self.assertIsNone(payload)
        self.assertIn("deny list", error)

    def test_admin_scope_reaches_the_workspace_but_not_secrets(self):
        payload, error, _ = self.fb.listing("", "admin")
        self.assertIsNone(error)
        names = [e["name"] for e in payload["entries"]]
        self.assertIn("projects", names)
        self.assertIn("config.yaml", names)          # visible...
        row = next(e for e in payload["entries"] if e["name"] == "config.yaml")
        self.assertTrue(row["denied"])               # ...but marked unreadable
        blocked, error, _ = self.fb.preview("config.yaml", "admin")
        self.assertIsNone(blocked)

    # -- listing / preview -------------------------------------------------
    def test_listing_caps_entries(self):
        sb = FileBrowser(self.ws, {**CFG, "max_entries": 1})
        payload, _, _ = sb.listing("demo", "root")
        self.assertTrue(payload["truncated"])
        self.assertEqual(len(payload["entries"]), 1)

    def test_preview_truncates_and_flags_it(self):
        payload, error, _ = self.fb.preview("demo/huge.bin", "root", max_bytes=64)
        self.assertIsNone(error)
        self.assertEqual(payload["kind"], "text")
        self.assertTrue(payload["truncated"])
        self.assertEqual(len(payload["content"]), 64)

    def test_preview_detects_binary(self):
        payload, _, _ = self.fb.preview("demo/blob.bin", "root")
        self.assertEqual(payload["kind"], "binary")
        self.assertIsNone(payload["content"])

    def test_directory_preview_is_a_client_error(self):
        payload, error, status = self.fb.preview("demo", "root")
        self.assertIsNone(payload)
        self.assertEqual(status, 400)

    def test_raw_only_for_whitelisted_small_assets(self):
        body, ctype, error, status = self.fb.raw("demo/logo.png", "root")
        self.assertEqual(error, None)
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "image/png")
        _, _, error, status = self.fb.raw("demo/src/app.py", "root")
        self.assertEqual(status, 415)
        _, _, error, status = self.fb.raw("demo/huge.bin", "root")
        self.assertEqual(status, 415)                 # type wins before size

    # -- token -------------------------------------------------------------
    def test_token_is_generated_with_0600_and_verified(self):
        token, source = self.fb.ensure_token()
        self.assertEqual(source, "config/dashboard-admin-token")
        self.assertGreater(len(token), 20)
        mode = stat.S_IMODE(os.stat(self.ws / "config" / "dashboard-admin-token").st_mode)
        self.assertEqual(mode, 0o600)
        self.assertTrue(self.fb.check_token(token))
        self.assertFalse(self.fb.check_token("nope"))
        self.assertFalse(self.fb.check_token(None))
        self.assertEqual(self.fb.scope_for(token), "admin")

    def test_token_is_stable_then_rotates(self):
        first, _ = self.fb.ensure_token()
        self.assertEqual(self.fb.ensure_token()[0], first)
        second, _ = self.fb.rotate_token()
        self.assertNotEqual(first, second)
        self.assertFalse(self.fb.check_token(first))
        self.assertTrue(self.fb.check_token(second))

    def test_config_yaml_token_wins(self):
        (self.ws / "config.yaml").write_text("api_keys: {}\ndashboard:\n  admin_token: from-config\n")
        token, source = self.fb.ensure_token()
        self.assertEqual((token, source), ("from-config", "config.yaml"))
        self.assertTrue(self.fb.check_token("from-config"))

    # -- lockout -----------------------------------------------------------
    def test_lockout_after_repeated_failures(self):
        client = "10.0.0.9"
        for _ in range(4):
            self.fb.note_failure(client)
        self.assertEqual(self.fb.locked(client), 0.0)
        self.fb.note_failure(client)
        self.assertGreater(self.fb.locked(client), 0.0)
        self.fb.note_success(client)
        self.assertEqual(self.fb.locked(client), 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
