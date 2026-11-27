#!/usr/bin/env python3
"""Tests for the NipoVPN dashboard.

    python3 tests/test_dashboard.py
    python3 tests/test_dashboard.py --nipovpn ../nipovpn --binary ../nipovpn/build/core/nipovpn

Needs: python3 with PyYAML. Uses zbarimg (zbar-tools), when installed, to
check the QR codes decode, a nipovpn checkout (--nipovpn, default ../nipovpn),
when there is one, to compare shared links with its scripts/config2url.py, and
a nipovpn binary (--binary) to test against a real server.
"""

import argparse
import base64
import copy
import http.client
import importlib.util
import json
import os
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(ROOT, "src")
# nipovpn's sample config.yaml, the config the tests manage.
SAMPLE_CONFIG = os.path.join(ROOT, "tests", "fixtures", "config.yaml")
NIPOVPN_SRC = os.environ.get("NIPOVPN_SRC") or os.path.join(os.path.dirname(ROOT), "nipovpn")
sys.path.insert(0, DASHBOARD)

import agentconfig  # noqa: E402
import qr  # noqa: E402
import serverconfig  # noqa: E402
import settings as server_settings  # noqa: E402

spec = importlib.util.spec_from_file_location("dashboard_main", os.path.join(DASHBOARD, "nipovpn-dashboard.py"))
dashboard_main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dashboard_main)


def load_config2url():
    """nipovpn's scripts/config2url.py, or None without a nipovpn checkout."""
    path = os.path.join(NIPOVPN_SRC, "scripts", "config2url.py")
    if not os.path.exists(path):
        return None
    spec = importlib.util.spec_from_file_location("config2url", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


config2url = load_config2url()


def decode_url(url):
    import base64
    data = url[len("nipovpn://"):]
    return json.loads(base64.b64decode(data + "=" * (-len(data) % 4)).decode("utf-8"))

BINARY = None
PASSWORD = "correct horse battery"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sample_text():
    with open(SAMPLE_CONFIG) as f:
        return f.read()


class QrTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("zbarimg"), "zbarimg is not installed")
    def test_codes_decode(self):
        tmp = tempfile.mkdtemp()
        try:
            for length in (1, 20, 80, 150, 400, 900, 1400, 2200):
                text = "nipovpn://" + ("abcXYZ0189+/=" * 200)[:length]
                code = qr.QrCode(text.encode())
                path = os.path.join(tmp, f"{length}.pbm")
                with open(path, "w") as f:
                    f.write(code.to_pbm())
                out = subprocess.run(["zbarimg", "-q", "--raw", path], capture_output=True, text=True)
                self.assertEqual(out.stdout.rstrip("\n"), text, f"version {code.version} did not decode")
        finally:
            shutil.rmtree(tmp)

    def test_svg(self):
        svg = qr.svg("nipovpn://abc")
        self.assertTrue(svg.startswith("<svg") and "<path" in svg)


class ServerConfigTest(unittest.TestCase):
    def test_byte_sizes(self):
        self.assertEqual(serverconfig.parse_byte_size("1GB"), 10 ** 9)
        self.assertEqual(serverconfig.parse_byte_size("1.5GiB"), int(1.5 * 1024 ** 3))
        self.assertEqual(serverconfig.parse_byte_size(" 500 mb "), 500 * 10 ** 6)
        for bad in ("", "5", "GB", "1 XB", "-1GB", "0B"):
            self.assertIsNone(serverconfig.parse_byte_size(bad), bad)

    def test_rewrite_keeps_everything_else(self):
        text = sample_text()
        doc = yaml.safe_load(text)
        users = serverconfig.users_of(doc)
        users[0]["enabled"] = False
        users.append({"name": "carol@home", "token": serverconfig.new_token(), "enabled": True, "limit": "5GB"})
        new = serverconfig.replace_users_block(text, users)
        serverconfig._check_rewrite(doc, new, users)

        def outside_users(t):
            lines = t.splitlines()
            start = next(i for i, l in enumerate(lines) if l.strip() == "users:")
            end = next(i for i in range(start + 1, len(lines))
                       if lines[i].strip() and not lines[i].startswith("    "))
            return lines[:start] + lines[end:]

        self.assertEqual(outside_users(new), outside_users(text))
        self.assertEqual(serverconfig.users_of(yaml.safe_load(new)), users)

    def test_rewrite_layouts(self):
        token = "t" * 32
        cases = [
            "server:\n  a: 1\n  users:\n  - name: x\n    token: %s\n  b: 2\nagent:\n  c: 3\n",
            "server:\n  users: [{name: x, token: %s}]\n  b: 2\n",
            "server:\n    users:\n        -   name: x\n            token: %s\n# end\nagent: {}\n",
            "server:\r\n  users:\r\n    - name: x\r\n      token: %s\r\n",
        ]
        for case in cases:
            text = case % token
            doc = yaml.safe_load(text)
            users = serverconfig.users_of(doc) + [{"name": "y", "token": "u" * 32, "enabled": True, "limit": ""}]
            new = serverconfig.replace_users_block(text, users)
            serverconfig._check_rewrite(doc, new, users)

    def test_validation(self):
        good = {"name": "a", "token": "t" * 32, "enabled": True, "limit": ""}
        serverconfig.validate_users([good])
        bad = [
            [],
            [dict(good, name="bad name")],
            [dict(good, token="short")],
            [good, dict(good, token="u" * 32)],
            [good, dict(good, name="b")],
            [dict(good, limit="lots")],
        ]
        for users in bad:
            with self.assertRaises(serverconfig.ConfigError, msg=str(users)):
                serverconfig.validate_users(users)

    def test_save_keeps_backup(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "config.yaml")
            with open(path, "w") as f:
                f.write(sample_text())
            os.chmod(path, 0o640)
            users = serverconfig.users_of(yaml.safe_load(sample_text()))[:1]
            self.assertTrue(serverconfig.save_users(path, users))
            with open(path + ".bak") as f:
                self.assertEqual(f.read(), sample_text())
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o640)
            self.assertEqual(len(serverconfig.users_of(serverconfig.load(path)[1])), 1)
            self.assertFalse(serverconfig.save_users(path, users))
        finally:
            shutil.rmtree(tmp)


class SettingsTest(unittest.TestCase):
    def test_set_values_keeps_comments(self):
        text = sample_text()
        doc = yaml.safe_load(text)
        changes = {
            ("general", "timeout"): 30,
            ("log", "logLevel"): "TRACE",
            ("server", "usage", "resetDay"): 15,
            ("agent", "fakeUrls"): ["a.example", "b.example"],
            ("agent", "userAgent"): "Agent # with hash",
            ("server", "missing", "deep"): True,
        }
        new = serverconfig.set_values(text, changes)
        serverconfig._check_values(doc, new, changes)
        comments = lambda t: [l for l in t.splitlines() if l.lstrip().startswith("#")]
        self.assertEqual(comments(new), comments(text))
        self.assertIn("  timeout: 30\n", new)

    def test_set_values_layouts(self):
        cases = [
            ("general:\n  timeout: 10 # seconds\n", {("general", "timeout"): 5}, "  timeout: 5 # seconds"),
            ("general: {timeout: 10}\nlog:\n  logLevel: INFO\n", {("log", "logLevel"): "DEBUG"}, '  logLevel: "DEBUG"'),
            ("server:\n    listenPort: 1\n", {("server", "threads"): 4}, "    threads: 4"),
            ("log:\n  logLevel: INFO\n", {("general", "timeout"): 3}, "general:\n  timeout: 3"),
            ("agent:\n  methods: [GET]\n", {("agent", "methods"): ["PUT"]}, '  methods:\n    - "PUT"'),
        ]
        for text, changes, expect in cases:
            new = serverconfig.set_values(text, changes)
            serverconfig._check_values(yaml.safe_load(text), new, changes)
            self.assertIn(expect, new)

    def test_plan(self):
        doc = yaml.safe_load(sample_text())
        changes, changed, _ = server_settings.plan(doc, {"general.timeout": "20", "server.threads": 2})
        self.assertEqual(changes, {("general", "timeout"): 20})
        self.assertEqual(changed, ["general.timeout"])
        bad = [
            {"general.timeout": -1}, {"server.listenPort": 70000}, {"server.listenIp": "nope"},
            {"log.logLevel": "LOUD"}, {"agent.fakeUrls": []}, {"agent.methods": ["get it"]},
            {"general.tlsEnable": "yes"}, {"nope.nope": 1}, {"server.usage.resetDay": 29},
            {"general.tlsEnable": True, "server.tlsCertFile": ""},
            {"general.traffic": "all"}, {"general.traffic": "udp", "agent.protocol": "http"},
        ]
        for request in bad:
            with self.assertRaises(ValueError, msg=str(request)):
                server_settings.plan(doc, request)

    def test_traffic_setting(self):
        doc = yaml.safe_load(sample_text())
        doc["general"].pop("traffic", None)
        self.assertEqual(server_settings.current(doc)["general.traffic"], "both")
        changes, _, _ = server_settings.plan(doc, {"general.traffic": "tcp"})
        self.assertEqual(changes, {("general", "traffic"): "tcp"})
        changes, _, _ = server_settings.plan(doc, {"general.traffic": "udp", "agent.protocol": "socks5"})
        self.assertEqual(changes[("general", "traffic")], "udp")

        # A config from before the setting existed gets the key added to general.
        text = "general:\n  timeout: 10\nlog:\n  logLevel: INFO\n"
        new = serverconfig.set_values(text, {("general", "traffic"): "tcp"})
        serverconfig._check_values(yaml.safe_load(text), new, {("general", "traffic"): "tcp"})
        self.assertEqual(yaml.safe_load(new)["general"]["traffic"], "tcp")

    def test_moved_settings_are_read_from_general(self):
        doc = {"general": {"bufferSize": 1024}, "server": {}, "agent": {}}
        self.assertEqual(server_settings.current(doc)["server.bufferSize"], 1024)


class AgentConfigTest(unittest.TestCase):
    def test_link_matches_config2url(self):
        if config2url is None:
            self.skipTest(f"no nipovpn checkout at {NIPOVPN_SRC}; pass --nipovpn")
        doc = yaml.safe_load(sample_text())
        user = serverconfig.users_of(doc)[1]
        cfg = agentconfig.agent_config(doc, user, "203.0.113.7", 443)
        ours = agentconfig.share_url(cfg, "nokhod")
        theirs = config2url.encode(config2url.build_payload(cfg, "nokhod"))
        decoded = config2url.decode(ours)["config"]
        expected = config2url.decode(theirs)["config"]
        self.assertEqual(decoded["aesEnable"], doc["general"]["aesEnable"])
        self.assertEqual(decoded["traffic"], doc["general"].get("traffic", "both"))
        # Fields the dashboard writes that the checked-out config2url.py may not
        # have yet (it lags behind on the default branch).
        newer = {k for k in ("aesEnable", "traffic") if k not in expected}
        if newer:
            decoded_without = {k: v for k, v in decoded.items() if k not in newer}
            self.assertEqual(decoded_without, expected)
        else:
            self.assertEqual(ours, theirs)
        self.assertEqual(decoded["token"], user["token"])
        self.assertEqual(decoded["serverIp"], "203.0.113.7")

    def test_agent_yaml_is_a_valid_agent_config(self):
        doc = yaml.safe_load(sample_text())
        user = serverconfig.users_of(doc)[0]
        cfg = yaml.safe_load(agentconfig.agent_yaml(agentconfig.agent_config(doc, user, "vpn.example", 8443), "adas"))
        self.assertEqual(cfg["agent"]["token"], user["token"])
        self.assertEqual(cfg["agent"]["serverIp"], "vpn.example")
        self.assertEqual(cfg["agent"]["serverPort"], 8443)
        self.assertEqual(cfg["general"]["tlsEnable"], doc["general"]["tlsEnable"])

    def test_agent_config_and_link_carry_traffic(self):
        doc = yaml.safe_load(sample_text())
        user = serverconfig.users_of(doc)[0]
        for traffic in ("tcp", "udp", "both"):
            doc["general"]["traffic"] = traffic
            cfg = agentconfig.agent_config(doc, user, "vpn.example", 443)
            self.assertEqual(yaml.safe_load(agentconfig.agent_yaml(cfg, "adas"))["general"]["traffic"], traffic)
            url = agentconfig.share_url(cfg, "adas")
            payload = json.loads(base64.b64decode(url[len("nipovpn://"):]).decode("utf-8"))
            self.assertEqual(payload["config"]["traffic"], traffic)
        doc["general"].pop("traffic")
        cfg = agentconfig.agent_config(doc, user, "vpn.example", 443)
        self.assertEqual(cfg["general"]["traffic"], "both")


class PasswordTest(unittest.TestCase):
    def test_hash(self):
        h = dashboard_main.hash_password("secret pass")
        self.assertTrue(dashboard_main.verify_password("secret pass", h))
        self.assertFalse(dashboard_main.verify_password("secret pas", h))
        self.assertFalse(dashboard_main.verify_password("x", "nonsense"))

    def test_host_check(self):
        allowed = dashboard_main.host_allowed
        for host in ("127.0.0.1:8090", "localhost:8090", "[::1]:8090", "10.0.0.5", "vpn.example"):
            self.assertTrue(allowed(host, ["vpn.example"]), host)
        for host in ("evil.example", "evil.example:8090", "", "127.0.0.1.evil.example"):
            self.assertFalse(allowed(host, []), host)

    def test_restart_command_default(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "dashboard.yaml")
            with open(path, "w") as f:
                yaml.safe_dump({"passwordHash": dashboard_main.hash_password("x")}, f)
            with mock.patch.object(dashboard_main.shutil, "which", return_value="/usr/bin/systemctl"):
                self.assertEqual(dashboard_main.load_settings(path)["restartCommand"],
                                 ["systemctl", "restart", "nipovpn-server"])
            with mock.patch.object(dashboard_main.shutil, "which", return_value=None):
                self.assertEqual(dashboard_main.load_settings(path)["restartCommand"], [])
        finally:
            shutil.rmtree(tmp)


class Dashboard:
    """A dashboard process on a free port with its own copy of a server config."""

    def __init__(self, tmp, server_cfg, reload_command=None, log_file=None, extra=None):
        self.tmp = tmp
        self.config_path = os.path.join(tmp, "config.yaml")
        self.port = free_port()
        settings = {
            "listenIp": "127.0.0.1",
            "listenPort": self.port,
            "passwordHash": dashboard_main.hash_password(PASSWORD),
            "nipovpnConfig": self.config_path,
            "reloadCommand": reload_command if reload_command is not None else ["true"],
            "publicAddress": "203.0.113.7",
            # Never restart a real nipovpn-server through the systemctl default.
            "restartCommand": ["false"],
        }
        if log_file:
            settings["logFile"] = log_file
        settings.update(extra or {})
        self.tls = bool(settings.get("tlsEnable"))
        self.cafile = settings.get("tlsCertFile")
        self.settings_path = os.path.join(tmp, "dashboard.yaml")
        with open(self.settings_path, "w") as f:
            yaml.safe_dump(settings, f)
        if not os.path.exists(self.config_path):
            with open(self.config_path, "w") as f:
                f.write(server_cfg)
        self.out = open(os.path.join(tmp, "dashboard.out"), "w")
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(DASHBOARD, "nipovpn-dashboard.py"), "--config", self.settings_path],
            stdout=self.out, stderr=subprocess.STDOUT)
        deadline = time.time() + 10
        while time.time() < deadline:
            if self.proc.poll() is not None:
                with open(self.out.name) as f:
                    raise RuntimeError("dashboard exited: " + f.read())
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.05)
        self.cookie = None

    def request(self, method, path, body=None, headers=None, api_header=True, host=None):
        if self.tls:
            context = ssl.create_default_context(cafile=self.cafile)
            conn = http.client.HTTPSConnection("127.0.0.1", self.port, timeout=30, context=context)
        else:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        if api_header:
            h["X-Nipovpn-Dashboard"] = "1"
        if self.cookie:
            h["Cookie"] = self.cookie
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        conn.request(method, path, body=data, headers=h)
        res = conn.getresponse()
        raw = res.read()
        cookie = res.getheader("Set-Cookie")
        if cookie:
            self.cookie = cookie.split(";", 1)[0]
        conn.close()
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = raw
        return res.status, payload, res

    def login(self):
        status, body, _ = self.request("POST", "/api/login", {"password": PASSWORD})
        assert status == 200, body

    def stop(self):
        self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.out.close()


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        cfg = sample_text()
        doc = yaml.safe_load(cfg)
        usage = os.path.join(self.tmp, "usage.yaml")
        log = os.path.join(self.tmp, "server.log")
        cfg = cfg.replace('file: "/var/lib/nipovpn/usage.yaml"', f'file: "{usage}"')
        cfg = cfg.replace('logFile: "/var/log/nipovpn/nipovpn.log"', f'logFile: "{log}"')
        with open(usage, "w") as f:
            f.write('period: "2026-11-01"\nusers:\n  adas: {used: 5000}  # 5.00 KB, no limit\n'
                    '  nokhod: {used: 2000000000}  # 2.00 GB of 1.00 GB (200%), blocked\n')
        with open(log, "w") as f:
            f.write("2026-11-23_10:00:00 [SERVER] [INFO] [abc] [CONNECT] [SRC 198.51.100.4:5555] "
                    "[USER adas] [DST example.com:443]\n")
        self.assertIn("adas", [u["name"] for u in serverconfig.users_of(doc)])
        self.d = Dashboard(self.tmp, cfg)

    def tearDown(self):
        self.d.stop()
        shutil.rmtree(self.tmp)

    def test_static_and_security_headers(self):
        status, body, res = self.d.request("GET", "/", api_header=False)
        self.assertEqual(status, 200)
        self.assertIn(b"NipoVPN", body)
        self.assertIn("script-src 'self'", res.getheader("Content-Security-Policy"))
        self.assertEqual(res.getheader("X-Frame-Options"), "DENY")
        for path in ("/app.js", "/app.css"):
            self.assertEqual(self.d.request("GET", path, api_header=False)[0], 200)
        self.assertEqual(self.d.request("GET", "/../dashboard.yaml", api_header=False)[0], 404)

    def test_auth(self):
        self.assertEqual(self.d.request("GET", "/api/overview")[0], 401)
        self.assertEqual(self.d.request("POST", "/api/login", {"password": "nope"})[0], 401)
        self.assertEqual(self.d.request("POST", "/api/login", {"password": PASSWORD}, api_header=False)[0], 403)
        self.assertEqual(self.d.request("POST", "/api/login", {"password": PASSWORD},
                                        host="evil.example:8090")[0], 421)
        self.d.login()
        self.assertIn("HttpOnly", self.d.request("POST", "/api/login", {"password": PASSWORD})[2]
                      .getheader("Set-Cookie"))
        self.assertEqual(self.d.request("GET", "/api/overview")[0], 200)
        self.assertEqual(self.d.request("GET", "/api/overview", api_header=False)[0], 403)
        self.d.request("POST", "/api/logout")
        self.assertEqual(self.d.request("GET", "/api/overview")[0], 401)

    def test_login_throttle(self):
        for _ in range(5):
            self.d.request("POST", "/api/login", {"password": "wrong"})
        self.assertEqual(self.d.request("POST", "/api/login", {"password": PASSWORD})[0], 429)

    def test_overview(self):
        self.d.login()
        status, body, _ = self.d.request("GET", "/api/overview")
        self.assertEqual(status, 200)
        users = {u["name"]: u for u in body["users"]}
        self.assertEqual(users["adas"]["used"], 5000)
        self.assertEqual(users["adas"]["lastSource"], "198.51.100.4")
        self.assertEqual(users["adas"]["connectsLastHour"] >= 0, True)
        self.assertTrue(users["nokhod"]["blocked"])
        self.assertEqual(body["server"]["period"], "2026-11-01")

    def test_user_lifecycle(self):
        self.d.login()
        status, body, _ = self.d.request("POST", "/api/users", {"name": "carol", "limit": "2GB"})
        self.assertEqual(status, 200, body)
        users = {u["name"]: u for u in serverconfig.users_of(serverconfig.load(self.d.config_path)[1])}
        self.assertEqual(users["carol"]["limit"], "2GB")
        self.assertEqual(len(users["carol"]["token"]), 32)
        token = users["carol"]["token"]

        self.assertEqual(self.d.request("POST", "/api/users", {"name": "carol"})[0], 409)
        self.assertEqual(self.d.request("POST", "/api/users", {"name": "bad name"})[0], 400)
        self.assertEqual(self.d.request("POST", "/api/users", {"name": "x", "token": "short"})[0], 400)

        status, body, _ = self.d.request("PATCH", "/api/users/carol",
                                         {"enabled": False, "limit": "", "regenerateToken": True})
        self.assertEqual(status, 200, body)
        carol = {u["name"]: u for u in serverconfig.users_of(serverconfig.load(self.d.config_path)[1])}["carol"]
        self.assertFalse(carol["enabled"])
        self.assertEqual(carol["limit"], "")
        self.assertNotEqual(carol["token"], token)

        status, body, _ = self.d.request("GET", "/api/users/carol/share?address=vpn.example&port=8443")
        self.assertEqual(status, 200, body)
        self.assertEqual(decode_url(body["url"])["config"]["token"], carol["token"])
        self.assertIn("serverIp: vpn.example", body["yaml"])
        self.assertTrue(body["qrSvg"].startswith("<svg"))
        self.assertEqual(self.d.request("GET", "/api/users/carol/share?address=bad%20host")[0], 400)

        self.assertEqual(self.d.request("DELETE", "/api/users/carol")[0], 200)
        names = [u["name"] for u in serverconfig.users_of(serverconfig.load(self.d.config_path)[1])]
        self.assertNotIn("carol", names)
        self.assertEqual(self.d.request("DELETE", "/api/users/carol")[0], 404)

        with open(self.d.config_path) as f:
            self.assertIn("# This block is to define agent directives", f.read())

    def test_settings(self):
        self.d.login()
        status, body, _ = self.d.request("GET", "/api/settings")
        self.assertEqual(status, 200, body)
        fields = {f["id"]: f for f in body["fields"]}
        self.assertEqual(fields["server.listenPort"]["value"], 443)
        self.assertTrue(fields["server.listenPort"]["restart"])

        status, body, _ = self.d.request("PATCH", "/api/settings",
                                         {"changes": {"general.timeout": 25, "server.threads": 4,
                                                      "agent.methods": "GET\nPOST"}})
        self.assertEqual(status, 200, body)
        self.assertEqual(sorted(body["changed"]), ["agent.methods", "general.timeout", "server.threads"])
        self.assertEqual(body["restartNeeded"], ["Threads"])
        _, doc = serverconfig.load(self.d.config_path)
        self.assertEqual(doc["general"]["timeout"], 25)
        self.assertEqual(doc["server"]["threads"], 4)
        self.assertEqual(doc["agent"]["methods"], ["GET", "POST"])
        with open(self.d.config_path) as f:
            self.assertIn("# timeout: unsigned short", f.read())

        self.assertEqual(self.d.request("PATCH", "/api/settings", {"changes": {"server.listenPort": 0}})[0], 400)
        status, body, _ = self.d.request("POST", "/api/restart")
        self.assertEqual(status, 200, body)
        self.assertFalse(body["ok"])
        self.assertIn("restart command failed", body["message"])

        status, body, _ = self.d.request("PATCH", "/api/settings",
                                         {"changes": {"agent.serverIp": "vpn.example.com",
                                                      "agent.methods": ["PUT", "DELETE"]}})
        self.assertEqual(status, 200, body)
        _, doc = serverconfig.load(self.d.config_path)
        self.assertEqual(doc["agent"]["serverIp"], "vpn.example.com")
        self.assertEqual(doc["agent"]["methods"], ["PUT", "DELETE"])
        self.assertEqual(self.d.request("PATCH", "/api/settings",
                                        {"changes": {"agent.serverIp": "bad host"}})[0], 400)
        self.assertEqual(self.d.request("PATCH", "/api/settings",
                                        {"changes": {"agent.methods": []}})[0], 400)

    def test_set_usage(self):
        self.d.login()
        status, body, _ = self.d.request("POST", "/api/users/nokhod/usage", {"used": 0})
        self.assertEqual(status, 200, body)
        with open(os.path.join(self.tmp, "usage.yaml")) as f:
            doc = yaml.safe_load(f)
        self.assertEqual(doc["users"]["nokhod"]["used"], 0)
        self.assertEqual(doc["users"]["adas"]["used"], 5000)
        self.assertEqual(doc["period"], "2026-11-01")
        self.assertEqual(self.d.request("POST", "/api/users/nokhod/usage", {"used": -1})[0], 400)


@unittest.skipUnless(shutil.which("openssl"), "openssl is not installed")
class TlsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        out = subprocess.run([sys.executable, os.path.join(DASHBOARD, "nipovpn-dashboard.py"), "make-cert",
                              "--host", "127.0.0.1", "--dir", self.tmp], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.cert = os.path.join(self.tmp, "dashboard.crt")
        self.key = os.path.join(self.tmp, "dashboard.key")
        self.assertEqual(os.stat(self.key).st_mode & 0o777, 0o600)
        self.d = Dashboard(self.tmp, sample_text(), extra={
            "tlsEnable": True, "tlsCertFile": self.cert, "tlsKeyFile": self.key})

    def tearDown(self):
        self.d.stop()
        shutil.rmtree(self.tmp)

    def test_https(self):
        status, body, res = self.d.request("POST", "/api/login", {"password": PASSWORD})
        self.assertEqual(status, 200, body)
        self.assertIn("; Secure", res.getheader("Set-Cookie"))
        self.assertIn("max-age", res.getheader("Strict-Transport-Security"))
        self.assertEqual(self.d.request("GET", "/api/overview")[0], 200)

    def test_plain_http_is_refused(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.d.port, timeout=5)
        with self.assertRaises((http.client.HTTPException, ConnectionError, OSError)):
            conn.request("GET", "/")
            conn.getresponse()
        # The dashboard still serves HTTPS afterwards.
        self.assertEqual(self.d.request("GET", "/", api_header=False)[0], 200)

    def test_renewed_certificate_is_loaded_on_sighup(self):
        def fingerprint():
            context = ssl.create_default_context()
            context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
            with socket.create_connection(("127.0.0.1", self.d.port)) as raw:
                with context.wrap_socket(raw) as tls:
                    return tls.getpeercert(binary_form=True)

        before = fingerprint()
        os.unlink(self.cert)
        os.unlink(self.key)
        subprocess.run([sys.executable, os.path.join(DASHBOARD, "nipovpn-dashboard.py"), "make-cert",
                        "--host", "127.0.0.1", "--dir", self.tmp], check=True, capture_output=True)
        self.d.proc.send_signal(signal.SIGHUP)
        deadline = time.time() + 5
        while time.time() < deadline and fingerprint() == before:
            time.sleep(0.1)
        self.assertNotEqual(fingerprint(), before)

    def test_bad_certificate_is_reported_at_startup(self):
        settings = os.path.join(self.tmp, "bad.yaml")
        with open(settings, "w") as f:
            yaml.safe_dump({"passwordHash": dashboard_main.hash_password(PASSWORD), "tlsEnable": True,
                            "tlsCertFile": os.path.join(self.tmp, "missing.crt"), "tlsKeyFile": self.key,
                            "nipovpnConfig": self.d.config_path}, f)
        out = subprocess.run([sys.executable, os.path.join(DASHBOARD, "nipovpn-dashboard.py"), "--config", settings],
                             capture_output=True, text=True, timeout=10)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("missing.crt", out.stderr)


class RealServerTest(unittest.TestCase):
    """The dashboard driving a real nipovpn server: changes reach it through a reload."""

    def setUp(self):
        if not os.environ.get("NIPOVPN_BINARY"):
            self.skipTest("pass --binary to test against a real server")
        self.tmp = tempfile.mkdtemp()
        doc = yaml.safe_load(sample_text())
        self.log = os.path.join(self.tmp, "server.log")
        doc["general"].update(tlsEnable=False, aesEnable=True, tunnelEnable=False)
        doc["log"]["logFile"] = self.log
        doc["server"].update(listenIp="127.0.0.1", listenPort=free_port(),
                             usage={"file": os.path.join(self.tmp, "usage.yaml"), "resetDay": 1},
                             users=[{"name": "alice", "token": serverconfig.new_token(), "enabled": True}])
        open(self.log, "w").close()
        config_path = os.path.join(self.tmp, "config.yaml")
        with open(config_path, "w") as f:
            yaml.safe_dump(doc, f, sort_keys=False)
        self.server_out = open(os.path.join(self.tmp, "server.out"), "w")
        self.server = subprocess.Popen([os.environ["NIPOVPN_BINARY"], "server", config_path],
                                       stdout=self.server_out, stderr=subprocess.STDOUT)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", doc["server"]["listenPort"]), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.05)
        self.d = Dashboard(self.tmp, None, reload_command=[])

    def tearDown(self):
        self.d.stop()
        self.server.send_signal(signal.SIGTERM)
        self.server.wait(timeout=10)
        self.server_out.close()
        shutil.rmtree(self.tmp)

    def test_changes_reload_the_server(self):
        self.d.login()
        status, body, _ = self.d.request("GET", "/api/overview")
        self.assertTrue(body["server"]["running"], body)
        self.assertEqual(body["server"]["pid"], self.server.pid)

        status, body, _ = self.d.request("POST", "/api/users", {"name": "bob", "limit": "1GB"})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"], body)
        self.assertIn("config reloaded; 2 of 2 users enabled", body["message"])

        status, body, _ = self.d.request("PATCH", "/api/users/bob", {"enabled": False})
        self.assertIn("access revoked for: bob", body["message"])

        status, body, _ = self.d.request("GET", "/api/overview")
        self.assertTrue(any("config reloaded" in e["message"] for e in body["events"]), body["events"])

        status, body, _ = self.d.request("PATCH", "/api/settings",
                                         {"changes": {"general.timeout": 20, "server.threads": 3}})
        self.assertTrue(body["ok"], body)
        self.assertIn("server.threads changed but needs a restart", body["message"])
        status, body, _ = self.d.request("GET", "/api/overview")
        self.assertEqual([p["id"] for p in body["pendingRestart"]], ["server.threads"])
        self.assertEqual(body["pendingRestart"][0]["running"], "2")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", help="nipovpn binary, to also test against a real server")
    parser.add_argument("--nipovpn", help="a nipovpn checkout (default: ../nipovpn)")
    args, rest = parser.parse_known_args()
    if args.nipovpn:
        global NIPOVPN_SRC, config2url
        NIPOVPN_SRC = os.path.abspath(args.nipovpn)
        config2url = load_config2url()
    if args.binary:
        os.environ["NIPOVPN_BINARY"] = os.path.abspath(args.binary)
    unittest.main(argv=[sys.argv[0]] + rest, verbosity=2)


if __name__ == "__main__":
    main()
