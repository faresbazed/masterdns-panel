#!/usr/bin/env python3
"""Functional test for MasterDNS Panel — no root, no systemd, no network.

Starts the panel on a throwaway port with throwaway data dirs, then drives
it over HTTP exactly like a browser would: login, create a user, check the
generated links and the generated server_config.toml, check the public
share page + subscription endpoint, then disable the user and confirm the
tunnel domain disappears from the server config (that is the revocation).
"""
import base64
import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "panel", "app.py")
PORT = 18099


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

        print("PANEL TEST: all assertions passed")
    finally:
        proc.terminate()
        proc.wait(timeout=5)


if __name__ == "__main__":
    main()
