#!/usr/bin/env python3
"""Functional test for MasterDNS Panel — no root, no systemd, no network.

Starts the panel on a throwaway port with throwaway data dirs, then drives
it over HTTP exactly like a browser would: login, create a user, check the
generated links and the generated server_config.toml, check the public
share page + subscription endpoint, then disable the user and confirm the
tunnel domain disappears from the server config (that is the revocation).
"""
import base64
import html
import http.client
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.parse
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "panel", "app.py")
PORT = 18099


class DomainTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="mdv-domain-test-")
        self.addCleanup(tmp.cleanup)
        with mock.patch.dict(os.environ, MDV_HOME=os.path.join(tmp.name, "panel"),
                             MDV_SERVER_DIR=os.path.join(tmp.name, "server"),
                             MDV_RESTART_CMD="true"):
            spec = importlib.util.spec_from_file_location("panel_app", APP)
            self.app = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.app)
        self.app.cmd_init(mock.Mock(base_domain="example.com", ns_host="ns.example.com",
                                    server_ip="127.0.0.1", encryption_method=5,
                                    admin_password="test-pass-123"))
        server = self.app.ThreadingHTTPServer(("127.0.0.1", 0), self.app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        self.port = server.server_port
        self.app.SESSIONS["test-session"] = {"csrf": "test-csrf", "expires": time.time() + 60}

    def post(self, path, **fields):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(conn.close)
        conn.request("POST", path, urllib.parse.urlencode(dict(csrf="test-csrf", **fields)),
                     {"Content-Type": "application/x-www-form-urlencoded",
                      "Cookie": "mdv_session=test-session"})
        response = conn.getresponse()
        response.read()
        return response.status

    def test_custom_domains(self):
        with open(self.app.SERVER_CONFIG, "rb") as fh:
            original = fh.read()
        bad = ['alice.example.com"', "-alice.example.com", "alice..example.com",
               "alice_.example.com", "localhost", "a" * 64 + ".com",
               ".".join(["a" * 63] * 4), "alice-.example.com"]
        for domain in bad:
            with self.subTest(domain=domain):
                self.assertEqual(self.post("/users/create", name="Alice", custom_domain=domain), 400)
                with open(self.app.SERVER_CONFIG, "rb") as fh:
                    config = fh.read()
                tomllib.loads(config.decode())
                self.assertEqual(config, original)
                self.assertNotIn(domain, config.decode())
                self.assertEqual(self.app.load_users(), [])
        for domain in ("Alice.Example.COM.", ""):
            with self.subTest(valid=domain):
                self.assertEqual(self.post("/users/create", name="Alice", custom_domain=domain), 303)
        with open(self.app.SERVER_CONFIG, "rb") as fh:
            config = tomllib.load(fh)
        self.assertIn("alice.example.com", config["DOMAIN"])

    def test_settings_domains(self):
        for field in ("base_domain", "ns_host"):
            with self.subTest(field=field):
                settings = self.app.load_settings()
                with open(self.app.SERVER_CONFIG, "rb") as fh:
                    original = fh.read()
                self.assertEqual(self.post("/settings", **{field: 'bad.example.com"'}), 400)
                self.assertEqual(self.app.load_settings(), settings)
                with open(self.app.SERVER_CONFIG, "rb") as fh:
                    self.assertEqual(fh.read(), original)
        self.assertEqual(self.post("/settings", base_domain="New.Example.COM.",
                                   ns_host="NS.Example.COM."), 303)
        self.assertEqual(self.app.load_settings()["base_domain"], "new.example.com")
        self.assertEqual(self.post("/settings", base_domain="", ns_host=""), 303)
        self.assertEqual(self.app.load_settings()["base_domain"], "new.example.com")
        with open(self.app.SERVER_CONFIG, "rb") as fh:
            self.assertEqual(tomllib.load(fh)["DOMAIN"], ["setup.new.example.com"])

    def test_domain_boundaries(self):
        domain = ".".join(["a" * 63] * 3 + ["b" * 61])
        self.assertEqual(len(domain), 253)
        self.assertEqual(self.app.normalize_domain(domain), domain)
        for invalid in ("", "example.com..", domain + "b"):
            with self.subTest(domain=invalid), self.assertRaises(ValueError):
                self.app.normalize_domain(invalid)

    def test_init_rejects_invalid_domains_without_writing(self):
        for field in ("base_domain", "ns_host"):
            args = dict(base_domain="example.com", ns_host="ns.example.com",
                        server_ip="", encryption_method=5, admin_password="password")
            args[field] = 'bad.example.com"'
            settings = self.app.load_settings()
            with open(self.app.SERVER_CONFIG, "rb") as fh:
                original = fh.read()
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.app.cmd_init(mock.Mock(**args))
            self.assertEqual(self.app.load_settings(), settings)
            with open(self.app.SERVER_CONFIG, "rb") as fh:
                self.assertEqual(fh.read(), original)


class ExpiryTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("panel_app", APP)
        self.app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app)
        tmp = tempfile.TemporaryDirectory(prefix="mdv-expiry-test-")
        self.addCleanup(tmp.cleanup)
        paths = mock.patch.multiple(
            self.app, SERVER_DIR=tmp.name,
            SERVER_CONFIG=os.path.join(tmp.name, "server_config.toml"),
            SERVER_KEY_FILE=os.path.join(tmp.name, "encrypt_key.txt"),
            SETTINGS_FILE=os.path.join(tmp.name, "settings.json"),
            USERS_FILE=os.path.join(tmp.name, "users.json"), RESTART_CMD="true")
        paths.start()
        self.addCleanup(paths.stop)
        self.app.save_settings({"base_domain": "example.com", "key": "a" * 32,
                                "encryption_method": 5})
        self.app.save_users([{"slug": "expires", "expiry": "2030-01-01"}])
        clock = mock.patch.object(self.app.time, "strftime", return_value="2030-01-01")
        self.clock = clock.start()
        self.addCleanup(clock.stop)
        self.assertTrue(self.app.sync_server(restart=True))

    def test_expiry_removes_domain_and_restarts(self):
        with open(self.app.SERVER_CONFIG) as fh:
            self.assertIn("expires.example.com", fh.read())
        self.clock.return_value = "2030-01-02"
        with mock.patch.object(self.app.subprocess, "run") as restart:
            restart.return_value.returncode = 0
            self.assertTrue(self.app.sync_if_stale())
            restart.assert_called_once_with("true", shell=True, capture_output=True,
                                            text=True, timeout=30)
        with open(self.app.SERVER_CONFIG) as fh:
            config = fh.read()
        self.assertNotIn("expires.example.com", config)
        self.assertIn('DOMAIN = ["setup.example.com"]', config)

    def test_unchanged_config_does_not_sync_or_restart(self):
        with mock.patch.object(self.app, "sync_server") as sync, \
                mock.patch.object(self.app.subprocess, "run") as restart:
            self.assertFalse(self.app.sync_if_stale())
            sync.assert_not_called()
            restart.assert_not_called()

    def test_non_restarting_sync_does_not_hide_expiry(self):
        self.assertTrue(self.app.sync_server(restart=True))
        self.clock.return_value = "2030-01-02"
        self.assertTrue(self.app.sync_server(restart=False))
        with mock.patch.object(self.app.subprocess, "run") as restart:
            restart.return_value.returncode = 0
            self.assertTrue(self.app.sync_if_stale())
            restart.assert_called_once()

    def test_empty_restart_command_applies_config_once(self):
        self.app.RESTART_CMD = ""
        self.clock.return_value = "2030-01-02"
        with mock.patch.object(self.app.subprocess, "run") as restart:
            self.assertTrue(self.app.sync_if_stale())
            self.assertFalse(self.app.sync_if_stale())
            restart.assert_not_called()

    def test_missing_config_is_synced(self):
        os.remove(self.app.SERVER_CONFIG)
        with mock.patch.object(self.app.subprocess, "run") as restart:
            restart.return_value.returncode = 0
            self.assertTrue(self.app.sync_if_stale())
            restart.assert_called_once()
        self.assertTrue(os.path.exists(self.app.SERVER_CONFIG))

    def test_startup_preserves_pre_sync_applied_config(self):
        with open(self.app.SERVER_CONFIG) as fh:
            active_config = fh.read()
        self.clock.return_value = "2030-01-02"
        with mock.patch.object(self.app, "ThreadingHTTPServer"), \
                mock.patch.object(self.app.threading, "Thread"):
            self.app.cmd_serve(mock.Mock(host="127.0.0.1", port=0))
        self.assertEqual(self.app.APPLIED_CONFIG, active_config)
        with mock.patch.object(self.app.subprocess, "run") as restart:
            restart.return_value.returncode = 0
            self.assertTrue(self.app.sync_if_stale())
            restart.assert_called_once()

    def test_failed_restart_is_retried(self):
        self.clock.return_value = "2030-01-02"
        with mock.patch.object(self.app.subprocess, "run") as restart:
            restart.return_value.returncode = 1
            restart.return_value.stderr = "restart failed"
            self.assertFalse(self.app.sync_if_stale())
            restart.return_value.returncode = 0
            self.assertTrue(self.app.sync_if_stale())
            self.assertEqual(restart.call_count, 2)

    def test_sync_lock_covers_load_check_and_restart(self):
        def assert_locked():
            acquired = []

            def probe():
                held = self.app.SYNC_LOCK.acquire(blocking=False)
                acquired.append(held)
                if held:
                    self.app.SYNC_LOCK.release()

            thread = threading.Thread(target=probe)
            thread.start()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(acquired, [False])

        load_settings, load_users = self.app.load_settings, self.app.load_users

        def settings():
            assert_locked()
            return load_settings()

        def users():
            assert_locked()
            return load_users()

        def restart(*args, **kwargs):
            assert_locked()
            return mock.Mock(returncode=0)

        self.clock.return_value = "2030-01-02"
        with mock.patch.object(self.app, "load_settings", side_effect=settings), \
                mock.patch.object(self.app, "load_users", side_effect=users), \
                mock.patch.object(self.app.subprocess, "run", side_effect=restart):
            self.assertTrue(self.app.sync_if_stale())

    def test_watcher_continues_after_error(self):
        with mock.patch.object(self.app, "sync_if_stale",
                               side_effect=[OSError("read failed"), False]) as sync, \
                mock.patch.object(self.app.time, "sleep",
                                  side_effect=[None, None, KeyboardInterrupt]) as sleep:
            with self.assertRaises(KeyboardInterrupt):
                self.app.watch_expiry()
            self.assertEqual(sync.call_count, 2)
            sleep.assert_called_with(self.app.EXPIRY_CHECK_INTERVAL)
        self.assertIn("read failed", self.app.LAST_SYNC["message"])


def req(method, path, body=None, cookie=None):
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if cookie:
        headers["Cookie"] = cookie
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read().decode("utf-8", "replace")
    out = (resp.status, dict(resp.getheaders()), data)
    conn.close()
    return out


def main():
    tmp = tempfile.mkdtemp(prefix="mdv-panel-test-")
    env = dict(os.environ,
               MDV_HOME=os.path.join(tmp, "panel"),
               MDV_SERVER_DIR=os.path.join(tmp, "server"),
               MDV_RESTART_CMD="true")
    subprocess.run([sys.executable, APP, "init", "--base-domain", "example.com",
                    "--ns-host", "ns.example.com", "--admin-password", "test-pass-123",
                    "--server-ip", "203.0.113.10"], env=env, check=True, capture_output=True)
    proc = subprocess.Popen([sys.executable, APP, "serve", "--port", str(PORT)],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(50):
            try:
                if req("GET", "/login")[0] == 200:
                    break
            except OSError:
                time.sleep(0.2)
        else:
            raise AssertionError("panel did not come up")

        status, _, body = req("POST", "/login", "password=wrong")
        assert "Wrong password" in body, "bad password was accepted"
        status, headers, _ = req("POST", "/login", "password=test-pass-123")
        assert status == 303, f"login status {status}"
        cookie = headers["Set-Cookie"].split(";")[0]

        status, _, admin = req("GET", "/admin", cookie=cookie)
        assert status == 200
        csrf = re.search(r"name=csrf value='([^']+)'", admin).group(1)

        form = urllib.parse.urlencode({
            "csrf": csrf, "name": "Test User", "presets": "Google",
            "custom_resolvers": "10.150.2.73",
        })
        status, _, _ = req("POST", "/users/create", form, cookie)
        assert status == 303, f"create status {status}"

        status, _, admin = req("GET", "/admin", cookie=cookie)
        assert "test-user.example.com" in admin, "user domain missing from admin page"

        link = re.search(r"masterdns://test-user\.example\.com\?[^'< ]+", admin).group(0)
        parsed = urllib.parse.urlparse(link.replace("&amp;", "&"))
        params = urllib.parse.parse_qs(parsed.query)
        assert params["encryption"] == ["aes-256-gcm"], params
        assert "8.8.8.8" in params["resolvers"][0] and "10.150.2.73" in params["resolvers"][0], params
        assert len(params["key"][0]) == 32, "key must be 32 chars for method 5"

        json_link = re.search(r"masterdns://([A-Za-z0-9_-]{20,})#", admin).group(1)
        cfg = json.loads(base64.urlsafe_b64decode(json_link + "=" * (-len(json_link) % 4)))
        assert cfg["DOMAINS"] == ["test-user.example.com"], cfg
        assert cfg["DATA_ENCRYPTION_METHOD"] == 5 and cfg["ENCRYPTION_KEY"] == params["key"][0], cfg
        assert "10.150.2.73" in cfg["RESOLVERS"], cfg

        server_cfg = open(os.path.join(tmp, "server", "server_config.toml")).read()
        assert 'DOMAIN = ["test-user.example.com"]' in server_cfg, "server config missing user domain"
        assert "DATA_ENCRYPTION_METHOD = 5" in server_cfg
        key_file = open(os.path.join(tmp, "server", "encrypt_key.txt")).read().strip()
        assert key_file == params["key"][0], "key file does not match issued links"

        token = re.search(r"/sub/([A-Za-z0-9_-]+)", admin).group(1)
        status, _, sub = req("GET", f"/sub/{token}")
        assert status == 200 and base64.b64decode(sub).decode().strip() == link.replace("&amp;", "&"), \
            "subscription body mismatch"
        status, _, share = req("GET", f"/u/{token}")
        assert status == 200 and "test-user.example.com" in share, "public share page broken"

        uid = re.search(r"name=id value='([0-9a-f]+)'", admin).group(1)
        form = urllib.parse.urlencode({"csrf": csrf, "id": uid, "op": "toggle"})
        status, _, _ = req("POST", "/users/action", form, cookie)
        assert status == 303
        server_cfg = open(os.path.join(tmp, "server", "server_config.toml")).read()
        assert "test-user.example.com" not in server_cfg, "disabled user still in server config"
        status, _, _ = req("GET", f"/sub/{token}")
        assert status == 404, "disabled user's subscription still active"

        # A user must never be given the nameserver's own host or another
        # user's domain: either would put a duplicate in DOMAIN and ask for an
        # NS record that breaks delegation for everyone.
        def server_domains():
            cfg = open(os.path.join(tmp, "server", "server_config.toml")).read()
            return re.search(r'^DOMAIN = \[(.*)\]$', cfg, re.M).group(1)

        form = urllib.parse.urlencode({"csrf": csrf, "name": "NS", "presets": "Google"})
        status, _, _ = req("POST", "/users/create", form, cookie)
        assert status == 303, f"create status {status}"
        assert server_domains() == '"ns-2.example.com"', \
            f"user named NS got the nameserver host: DOMAIN = [{server_domains()}]"

        for taken in ("ns.example.com", "NS.Example.com.", "test-user.example.com"):
            form = urllib.parse.urlencode({"csrf": csrf, "name": "Clash", "presets": "Google",
                                           "custom_domain": taken})
            status, _, body = req("POST", "/users/create", form, cookie)
            assert status == 400 and "already in use" in body, \
                f"custom domain {taken!r} should be refused, got {status}"
        status, _, admin = req("GET", "/admin", cookie=cookie)
        assert "Clash" not in admin, "refused user was still created"

        form = urllib.parse.urlencode({"csrf": csrf, "name": "Custom", "presets": "Google",
                                       "custom_domain": "alice.example.com"})
        assert req("POST", "/users/create", form, cookie)[0] == 303
        form = urllib.parse.urlencode({"csrf": csrf, "name": "Alice", "presets": "Google"})
        assert req("POST", "/users/create", form, cookie)[0] == 303
        assert server_domains() == '"ns-2.example.com", "alice.example.com", "alice-2.example.com"', \
            f"auto slug collided with another user's custom domain: DOMAIN = [{server_domains()}]"

        # A trailing dot on the stored nameserver host must not let it through.
        settings_form = {"csrf": csrf, "base_domain": "example.com", "server_ip": "203.0.113.10",
                         "encryption_method": "5"}
        form = urllib.parse.urlencode(dict(settings_form, ns_host="ns.example.com."))
        assert req("POST", "/settings", form, cookie)[0] == 303
        form = urllib.parse.urlencode({"csrf": csrf, "name": "Clash", "presets": "Google",
                                       "custom_domain": "ns.example.com"})
        status, _, body = req("POST", "/users/create", form, cookie)
        assert status == 400 and "already in use" in body, \
            f"custom domain matching a dotted nameserver host should be refused, got {status}"
        form = urllib.parse.urlencode(dict(settings_form, ns_host="ns.example.com"))
        assert req("POST", "/settings", form, cookie)[0] == 303

        # Re-saving a user's resolvers unchanged must keep every resolver,
        # including a lone IP from a preset whose partner was not chosen.
        form = urllib.parse.urlencode({
            "csrf": csrf, "name": "Partial", "custom_resolvers": "8.8.8.8 10.0.0.1",
        })
        status, _, _ = req("POST", "/users/create", form, cookie)
        assert status == 303, f"create status {status}"
        status, _, admin = req("GET", "/admin", cookie=cookie)
        edit = re.search(r"<form method=post action=/users/update>.*?</form>",
                         admin.split("<h2 style='margin-top:0'>Partial</h2>", 1)[1], re.S).group(0)
        fields = [("csrf", csrf), ("id", re.search(r"name=id value='([0-9a-f]+)'", edit).group(1))]
        fields += [("presets", html.unescape(v)) for v in
                   re.findall(r"name=presets value='([^']+)' checked", edit)]
        fields.append(("custom_resolvers", html.unescape(
            re.search(r"name=custom_resolvers value='([^']*)'", edit).group(1))))
        status, _, _ = req("POST", "/users/update", urllib.parse.urlencode(fields), cookie)
        assert status == 303, f"update status {status}"
        users = json.load(open(os.path.join(tmp, "panel", "users.json")))
        partial = next(u for u in users if u["name"] == "Partial")
        assert partial["resolvers"] == ["8.8.8.8", "10.0.0.1"], \
            f"re-saving resolvers changed them: {partial['resolvers']}"

        # Danger zone: the admin page must carry the uninstall form, and the
        # endpoint must refuse cleanly — wrong confirmation word first…
        assert "action=/uninstall" in admin and "name=confirm" in admin, \
            "danger-zone uninstall form missing from admin page"
        form = urllib.parse.urlencode({"csrf": csrf, "confirm": "yes"})
        status, _, body = req("POST", "/uninstall", form, cookie)
        assert status == 400 and "not confirmed" in body.lower(), \
            f"uninstall with wrong confirmation should be refused, got {status}"
        # …then a correct confirmation in this throwaway env, where no
        # uninstall.sh was installed next to the panel: it must fail with
        # the clear "not found" page and remove nothing.
        form = urllib.parse.urlencode({"csrf": csrf, "confirm": "UNINSTALL"})
        status, _, body = req("POST", "/uninstall", form, cookie)
        assert status == 500 and "Uninstaller not found" in body, \
            f"uninstall without the script should fail cleanly, got {status}"
        assert os.path.exists(os.path.join(tmp, "panel", "settings.json")), \
            "refused uninstall removed panel data"

        print("PANEL TEST: all assertions passed")
    finally:
        proc.terminate()
        proc.wait(timeout=5)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    if not result.wasSuccessful():
        sys.exit(1)
    main()
