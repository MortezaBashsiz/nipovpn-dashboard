#!/usr/bin/env python3
"""NipoVPN dashboard: a web page to manage the users of a nipovpn server.

    nipovpn-dashboard --config /etc/nipovpn-dashboard/config.yaml
    nipovpn-dashboard hash-password            # prints a passwordHash for the config
    nipovpn-dashboard make-cert --host 1.2.3.4 # creates a self-signed TLS certificate

It runs beside the server and never talks to it directly: it edits the
server.users block of the server's config and asks the server to reload,
reads the usage file the server writes and follows the server's log.

Needs: python3 (3.8 or newer) with PyYAML.
"""

import argparse
import getpass
import hashlib
import hmac
import http.server
import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import traceback
from urllib.parse import parse_qs, unquote, urlsplit

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: apt install python3-yaml (or pip install pyyaml)")

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
sys.dont_write_bytecode = True  # installed under /usr/lib; keep it free of __pycache__

import agentconfig  # noqa: E402
import monitor  # noqa: E402
import qr  # noqa: E402
import serverconfig  # noqa: E402
import settings as server_settings  # noqa: E402

DEFAULT_CONFIG = "/etc/nipovpn-dashboard/config.yaml"
STATIC_DIR = os.path.join(HERE, "static")
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/theme.js": ("theme.js", "text/javascript; charset=utf-8"),
    "/logo.png": ("logo.png", "image/png"),
    "/favicon.ico": ("logo.png", "image/png"),
}
COOKIE = "nipovpn_dashboard"
API_HEADER = "X-Nipovpn-Dashboard"
MAX_BODY = 64 * 1024
HASH_SCHEME = "pbkdf2-sha256"
HASH_ITERATIONS = 200000

SETTINGS_DEFAULTS = {
    "listenIp": "127.0.0.1",
    "listenPort": 8090,
    "passwordHash": "",
    "nipovpnConfig": "/etc/nipovpn/config.yaml",
    "reloadCommand": [],
    "restartCommand": [],
    "logFile": "",
    "usageFile": "",
    "publicAddress": "",
    "publicPort": 0,
    "allowedHosts": [],
    "sessionHours": 12,
    "tlsEnable": False,
    "tlsCertFile": "",
    "tlsKeyFile": "",
}
DEFAULT_CERT_DIR = "/etc/nipovpn-dashboard"
# Used when restartCommand is empty and systemd is there: the unit of the nipovpn Debian package.
DEFAULT_RESTART_COMMAND = ["systemctl", "restart", "nipovpn-server"]


# ---------------------------------------------------------------- passwords


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, HASH_ITERATIONS)
    return f"{HASH_SCHEME}${HASH_ITERATIONS}${salt.hex()}${digest.hex()}"


def parse_password_hash(text):
    parts = str(text).split("$")
    if len(parts) != 4 or parts[0] != HASH_SCHEME or not parts[1].isdigit():
        return None
    try:
        iterations, salt, digest = int(parts[1]), bytes.fromhex(parts[2]), bytes.fromhex(parts[3])
    except ValueError:
        return None
    if iterations < 1000 or not salt or len(digest) != 32:
        return None
    return iterations, salt, digest


def verify_password(password, stored):
    parsed = parse_password_hash(stored)
    if not parsed:
        return False
    iterations, salt, digest = parsed
    got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(got, digest)


# ---------------------------------------------------------------- settings


class SettingsError(Exception):
    pass


def load_settings(path):
    try:
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
    except OSError as e:
        raise SettingsError(f"cannot read {path}: {e.strerror}")
    except yaml.YAMLError as e:
        raise SettingsError(f"{path} is not valid YAML: {e}")
    if not isinstance(doc, dict):
        raise SettingsError(f"{path} must be a map of settings")
    unknown = sorted(set(doc) - set(SETTINGS_DEFAULTS))
    if unknown:
        raise SettingsError(f"unknown settings in {path}: {', '.join(unknown)}")
    s = dict(SETTINGS_DEFAULTS)
    s.update({k: v for k, v in doc.items() if v is not None})

    try:
        ipaddress.ip_address(str(s["listenIp"]))
    except ValueError:
        raise SettingsError(f"listenIp {s['listenIp']!r} is not an IP address")
    for key in ("listenPort", "publicPort", "sessionHours"):
        if not isinstance(s[key], int) or isinstance(s[key], bool):
            raise SettingsError(f"{key} must be a number")
    if not 1 <= s["listenPort"] <= 65535 or not 0 <= s["publicPort"] <= 65535:
        raise SettingsError("ports must be between 1 and 65535")
    if not s["passwordHash"]:
        raise SettingsError("passwordHash is required; create one with `nipovpn-dashboard hash-password`")
    if not parse_password_hash(s["passwordHash"]):
        raise SettingsError("passwordHash is not valid; create one with `nipovpn-dashboard hash-password`")
    for key, example in (("reloadCommand", "[systemctl, reload, nipovpn-server]"),
                         ("restartCommand", "[systemctl, restart, nipovpn-server]")):
        if isinstance(s[key], str):
            s[key] = s[key].split()
        if not isinstance(s[key], list) or not all(isinstance(a, str) for a in s[key]):
            raise SettingsError(f"{key} must be a list of strings, e.g. {example}")
    if not s["restartCommand"] and shutil.which("systemctl"):
        s["restartCommand"] = list(DEFAULT_RESTART_COMMAND)
    if not isinstance(s["tlsEnable"], bool):
        raise SettingsError("tlsEnable must be true or false")
    if s["tlsEnable"]:
        for key in ("tlsCertFile", "tlsKeyFile"):
            if not s[key]:
                raise SettingsError(f"tlsEnable is true, so {key} is required; "
                                    "create a certificate with `nipovpn-dashboard make-cert`")
        s["sslContext"] = make_ssl_context(s["tlsCertFile"], s["tlsKeyFile"])
    if not isinstance(s["allowedHosts"], list):
        raise SettingsError("allowedHosts must be a list of host names")
    s["allowedHosts"] = [str(h).lower() for h in s["allowedHosts"]]
    return s


# ---------------------------------------------------------------- TLS


def make_ssl_context(cert_file, key_file):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    for path in (cert_file, key_file):
        try:
            with open(path, "rb"):
                pass
        except OSError as e:
            raise SettingsError(f"cannot read the TLS file {path}: {e.strerror}")
    try:
        context.load_cert_chain(cert_file, key_file)
    except (ssl.SSLError, OSError) as e:
        raise SettingsError(f"cannot load the TLS certificate {cert_file} and key {key_file}: {e}")
    return context


class CertificateWatcher:
    """Loads a renewed certificate into the running server (checked every minute and on SIGHUP)."""

    def __init__(self, context, cert_file, key_file):
        self.context, self.cert_file, self.key_file = context, cert_file, key_file
        self.stamp = self._stamp()
        self.lock = threading.Lock()

    def _stamp(self):
        try:
            return tuple(os.stat(p).st_mtime_ns for p in (self.cert_file, self.key_file))
        except OSError:
            return None

    def check(self, force=False):
        with self.lock:
            stamp = self._stamp()
            if stamp is None or (stamp == self.stamp and not force):
                return
            try:
                self.context.load_cert_chain(self.cert_file, self.key_file)
                print(f"loaded the TLS certificate {self.cert_file}", flush=True)
            except (ssl.SSLError, OSError) as e:
                print(f"warning: cannot load the renewed TLS certificate, keeping the old one: {e}", flush=True)
            self.stamp = stamp


def make_cert(host, directory, days):
    """Creates a self-signed certificate for host with openssl; returns (cert, key) paths."""
    try:
        ipaddress.ip_address(host)
        san = f"IP:{host}"
    except ValueError:
        if not re.match(r"^[A-Za-z0-9.-]{1,253}$", host):
            sys.exit(f"{host!r} is not an IP address or host name")
        san = f"DNS:{host}"
    cert, key = os.path.join(directory, "dashboard.crt"), os.path.join(directory, "dashboard.key")
    for path in (cert, key):
        if os.path.exists(path):
            sys.exit(f"{path} already exists; remove it first to create a new one")
    os.makedirs(directory, exist_ok=True)
    old_umask = os.umask(0o077)
    try:
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                        "-nodes", "-days", str(days), "-subj", f"/CN={host}",
                        "-addext", f"subjectAltName={san},DNS:localhost,IP:127.0.0.1",
                        "-keyout", key, "-out", cert], check=True, capture_output=True, text=True)
    except FileNotFoundError:
        sys.exit("openssl is needed: apt install openssl")
    except subprocess.CalledProcessError as e:
        sys.exit(f"openssl failed: {e.stderr.strip()}")
    finally:
        os.umask(old_umask)
    os.chmod(cert, 0o644)
    return cert, key


# ---------------------------------------------------------------- the dashboard


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class Dashboard:
    def __init__(self, settings):
        self.settings = settings
        self.config_path = settings["nipovpnConfig"]
        self.edit_lock = threading.Lock()
        self.sessions = {}
        self.sessions_lock = threading.Lock()
        self.failures = []
        self.started = time.time()

        _, doc = serverconfig.load(self.config_path)
        self.log = monitor.LogTail(self._log_path(doc))
        self.log.poll()
        self.usage = monitor.UsageFile(self._usage_path(doc))
        self.usage.poll()

        self.stop = threading.Event()
        threading.Thread(target=self._watch, daemon=True).start()

    def _log_path(self, doc):
        return (self.settings["logFile"] or (doc.get("log") or {}).get("logFile")
                or "/var/log/nipovpn/nipovpn.log")

    def _usage_path(self, doc):
        # The server only changes its usage file on restart, so follow the one it runs with.
        usage = ((doc.get("server") or {}).get("usage") or {})
        with self.log.lock:
            running = self.log.running.get("server.usage.file")
        return (self.settings["usageFile"] or running or usage.get("file")
                or "/var/lib/nipovpn/usage.yaml")

    def _sync_paths(self, doc):
        self.log.set_path(self._log_path(doc))
        path = self._usage_path(doc)
        if path != self.usage.path:
            self.usage = monitor.UsageFile(path)

    def _watch(self):
        while not self.stop.wait(5):
            try:
                self._sync_paths(serverconfig.load(self.config_path)[1])
                self.log.poll()
                self.usage.poll()
            except Exception:
                traceback.print_exc()

    # -- sessions

    def login(self, password):
        now = time.time()
        with self.sessions_lock:
            self.failures = [t for t in self.failures if t > now - 60]
            if len(self.failures) >= 5:
                raise ApiError(429, "too many wrong passwords; wait a minute")
        if not verify_password(password, self.settings["passwordHash"]):
            with self.sessions_lock:
                self.failures.append(now)
            time.sleep(0.5)
            raise ApiError(401, "wrong password")
        token = secrets.token_urlsafe(32)
        with self.sessions_lock:
            self.sessions = {k: v for k, v in self.sessions.items() if v > now}
            if len(self.sessions) >= 32:
                oldest = min(self.sessions, key=self.sessions.get)
                del self.sessions[oldest]
            self.sessions[token] = now + self.settings["sessionHours"] * 3600
        return token

    def logout(self, token):
        with self.sessions_lock:
            self.sessions.pop(token, None)

    def authenticated(self, token):
        if not token:
            return False
        with self.sessions_lock:
            expires = self.sessions.get(token)
            if expires is None:
                return False
            if expires < time.time():
                del self.sessions[token]
                return False
            return True

    # -- reading state

    def _server_doc(self):
        try:
            return serverconfig.load(self.config_path)[1]
        except serverconfig.ConfigError as e:
            raise ApiError(500, str(e))

    def _users(self, doc):
        users = serverconfig.users_of(doc)
        if users is None:
            raise ApiError(409, "the server config has no server.users list; add one by hand first")
        return users

    def overview(self):
        doc = self._server_doc()
        server = doc.get("server") or {}
        general = doc.get("general") or {}
        users = serverconfig.users_of(doc)
        self.log.poll()
        self._sync_paths(doc)
        self.usage.poll()

        pid = monitor.find_server_process(self.config_path)
        with self.log.lock:
            running = dict(self.log.running)
        port = int(running["server.listenPort"]) if running.get("server.listenPort", "").isdigit() \
            else server.get("listenPort")
        problems = [p for p in (self.log.error, self.usage.error) if p]
        if not os.access(self.config_path, os.W_OK):
            problems.append(f"{self.config_path} is not writable; user changes cannot be saved")
        if users is None:
            problems.append("the server config has no server.users list; add one by hand to manage users here")

        result_users = []
        with self.usage.lock, self.log.lock:
            used, rate = dict(self.usage.used), dict(self.usage.rate)
            for user in users or []:
                name = user["name"]
                limit_bytes = serverconfig.parse_byte_size(user["limit"]) if user["limit"] else 0
                u = used.get(name, 0)
                result_users.append({
                    "name": name,
                    "token": user["token"],
                    "enabled": user["enabled"],
                    "limit": user["limit"],
                    "limitBytes": limit_bytes or 0,
                    "used": u,
                    "rate": rate.get(name, 0),
                    "blocked": bool(limit_bytes) and u >= limit_bytes,
                    "lastSeen": self.log.last_seen.get(name),
                    "lastSource": self.log.last_src.get(name),
                    "connectsLastHour": len(self.log.connects.get(name, ())),
                    "sampleToken": user["token"] == serverconfig.SAMPLE_TOKEN,
                })
            names = {u["name"] for u in result_users}
            orphans = sorted(n for n in used if n not in names)
            events = list(self.log.events)
            period = self.usage.period

        return {
            "now": time.time(),
            "server": {
                "running": pid is not None,
                "pid": pid,
                "uptime": monitor.process_uptime(pid) if pid else None,
                "connections": monitor.established_connections(pid, port) if pid and isinstance(port, int) else None,
                "listenPort": port,
                "configFile": self.config_path,
                "logFile": self.log.path,
                "usageFile": self.usage.path,
                "tlsEnable": bool(general.get("tlsEnable", False)),
                "tunnelEnable": bool(general.get("tunnelEnable", False)),
                "aesEnable": bool(general.get("aesEnable", True)),
                "period": period,
                "resetDay": ((server.get("usage") or {}).get("resetDay")) or 1,
            },
            "users": result_users,
            "pendingRestart": self._pending_restart(doc, running) if pid else [],
            "restartAvailable": bool(self.settings["restartCommand"]),
            "orphans": orphans,
            "events": events,
            "problems": problems,
            "managed": users is not None,
        }

    # -- changing users

    def _find(self, users, name):
        for user in users:
            if user["name"] == name:
                return user
        raise ApiError(404, f"no user named {name}")

    def _apply(self, users, what):
        try:
            changed = serverconfig.save_users(self.config_path, users)
        except serverconfig.ConfigError as e:
            raise ApiError(400, str(e))
        result = self.reload() if changed else {"ok": True, "message": "nothing changed"}
        result["change"] = what
        return result

    @staticmethod
    def _clean_limit(value):
        value = "" if value is None else str(value).strip()
        if value and serverconfig.parse_byte_size(value) is None:
            raise ApiError(400, "limit must be a size with a unit, such as 2GB, 500MB or 1.5GiB")
        return value

    def create_user(self, body):
        with self.edit_lock:
            users = self._users(self._server_doc())
            name = str(body.get("name", "")).strip()
            if any(u["name"] == name for u in users):
                raise ApiError(409, f"a user named {name} already exists")
            token = str(body.get("token") or "").strip() or serverconfig.new_token()
            user = {"name": name, "token": token, "enabled": body.get("enabled", True) is not False,
                    "limit": self._clean_limit(body.get("limit"))}
            return self._apply(users + [user], f"added user {name}")

    def update_user(self, name, body):
        with self.edit_lock:
            users = self._users(self._server_doc())
            user = self._find(users, name)
            notes = []
            if "enabled" in body:
                user["enabled"] = body["enabled"] is True
                notes.append("enabled" if user["enabled"] else "disabled")
            if "limit" in body:
                user["limit"] = self._clean_limit(body["limit"])
                notes.append(f"limit {user['limit']}" if user["limit"] else "no limit")
            if body.get("regenerateToken"):
                user["token"] = serverconfig.new_token()
                notes.append("new token")
            elif body.get("token"):
                user["token"] = str(body["token"]).strip()
                notes.append("new token")
            return self._apply(users, f"user {name}: " + (", ".join(notes) or "no change"))

    def delete_user(self, name):
        with self.edit_lock:
            users = self._users(self._server_doc())
            self._find(users, name)
            return self._apply([u for u in users if u["name"] != name], f"deleted user {name}")

    def set_usage(self, name, body):
        try:
            used = int(body.get("used", 0))
        except (TypeError, ValueError):
            raise ApiError(400, "used must be a number of bytes")
        if used < 0:
            raise ApiError(400, "used must not be negative")
        self._find(self._users(self._server_doc()), name)
        try:
            self.usage.set_used(name, used)
        except RuntimeError as e:
            raise ApiError(409, str(e))
        return {"ok": True, "message": f"usage of {name} set; the server applies it within 30 seconds"}

    # -- server settings

    @staticmethod
    def _pending_restart(doc, running):
        return [{"id": f["id"], "label": f["label"], "running": f["running"], "value": f["value"]}
                for f in server_settings.describe(doc, running) if f.get("pending")]

    def settings_view(self):
        doc = self._server_doc()
        with self.log.lock:
            running = dict(self.log.running)
        return {
            "sections": server_settings.SECTIONS,
            "fields": server_settings.describe(doc, running),
            "restartAvailable": bool(self.settings["restartCommand"]),
            "serverRunning": monitor.find_server_process(self.config_path) is not None,
            "configFile": self.config_path,
        }

    def save_settings(self, body):
        requested = body.get("changes")
        if not isinstance(requested, dict) or not requested:
            raise ApiError(400, "send the changed settings as changes: {id: value}")
        with self.edit_lock:
            try:
                changed, warnings = server_settings.save(self.config_path, requested)
            except ValueError as e:
                raise ApiError(400, str(e))
            except serverconfig.ConfigError as e:
                raise ApiError(400, str(e))
            if not changed:
                return {"ok": True, "message": "nothing changed", "changed": [], "warnings": warnings}
            result = self.reload()
        labels = [server_settings.BY_ID[i]["label"] for i in changed]
        result["change"] = "changed " + ", ".join(labels)
        result["changed"] = changed
        result["warnings"] = warnings
        result["restartNeeded"] = [server_settings.BY_ID[i]["label"] for i in changed
                                   if server_settings.BY_ID[i].get("restart")]
        return result

    def restart(self):
        command = self.settings["restartCommand"]
        if not command:
            raise ApiError(409, "no restartCommand in the dashboard settings and no systemctl; "
                                "restart the server yourself")
        seq = self.log.mark()
        try:
            proc = subprocess.run(command, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            return {"ok": False, "message": f"the restart command failed: {e}"}
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[:500]
            return {"ok": False, "message": f"the restart command failed (exit {proc.returncode}): {detail}"}
        event = self.log.wait_for(r"^Config initialized", seq, 15)
        if event is None:
            return {"ok": False, "message": "restart requested, but the server did not log a start within "
                                            "15 seconds; check the server"}
        return {"ok": True, "message": "server restarted"}

    def reload(self):
        seq = self.log.mark()
        command = self.settings["reloadCommand"]
        if command:
            try:
                proc = subprocess.run(command, capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired) as e:
                return {"ok": False, "message": f"config saved, but the reload command failed: {e}"}
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout).strip()[:500]
                return {"ok": False, "message": f"config saved, but the reload command failed "
                                                f"(exit {proc.returncode}): {detail}"}
        else:
            pid = monitor.find_server_process(self.config_path)
            if pid is None:
                return {"ok": False, "message": "config saved, but no running nipovpn server was found "
                                                "to reload; it applies when the server starts"}
            try:
                os.kill(pid, signal.SIGHUP)
            except OSError as e:
                return {"ok": False, "message": f"config saved, but the server could not be "
                                                f"signalled: {e.strerror}"}
        event = self.log.wait_for(r"\[Runner reload\] (config reloaded|config not reloaded)", seq, 6)
        if event is None:
            return {"ok": True, "message": "config saved and reload requested; "
                                           "the server log did not confirm it yet"}
        message = re.sub(r"^\[Runner reload\] ", "", event["message"])
        return {"ok": "config not reloaded" not in message, "message": message}

    def share(self, name, query):
        doc = self._server_doc()
        user = self._find(self._users(doc), name)
        agent = doc.get("agent") or {}
        server = doc.get("server") or {}
        address = (query.get("address") or self.settings["publicAddress"] or agent.get("serverIp") or "").strip()
        try:
            port = int(query.get("port") or self.settings["publicPort"] or server.get("listenPort") or 443)
        except ValueError:
            raise ApiError(400, "port must be a number")
        if not 1 <= port <= 65535:
            raise ApiError(400, "port must be between 1 and 65535")
        if not address or not re.match(r"^[A-Za-z0-9.:\[\]_-]{1,253}$", address):
            raise ApiError(400, "enter the address agents use to reach this server")
        cfg = agentconfig.agent_config(doc, user, address, port)
        url = agentconfig.share_url(cfg, query.get("profile") or name)
        try:
            svg = qr.svg(url)
        except ValueError:
            svg = ""
        return {
            "address": address,
            "port": port,
            "yaml": agentconfig.agent_yaml(cfg, name),
            "url": url,
            "qrSvg": svg,
            "defaultAddress": bool(not query.get("address")),
            "aesEnable": cfg["general"]["aesEnable"],
        }


# ---------------------------------------------------------------- HTTP


def host_allowed(host_header, allowed):
    host = (host_header or "").strip().lower()
    if host.startswith("["):
        host = host[1:host.find("]")] if "]" in host else ""
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    if not host:
        return False
    if host == "localhost" or host in allowed:
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


class Handler(http.server.BaseHTTPRequestHandler):
    timeout = 60  # seconds a client may stay silent, including during the TLS handshake
    server_version = "nipovpn-dashboard"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    dashboard: Dashboard = None

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.address_string()} {fmt % args}\n")

    # -- responses

    def _headers(self, status, content_type, length, extra=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                         "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        if self.dashboard.settings["tlsEnable"]:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()

    def _json(self, status, payload, extra=()):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(body), extra)
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status, message):
        # The request body may not have been read; do not reuse the connection.
        self.close_connection = True
        self._json(status, {"ok": False, "message": message}, [("Connection", "close")])

    def _secure(self):
        return "; Secure" if self.dashboard.settings["tlsEnable"] else ""

    def _cookie(self):
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                return value
        return None

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ApiError(400, "bad Content-Length")
        if length > MAX_BODY:
            raise ApiError(413, "request too large")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        if "application/json" not in self.headers.get("Content-Type", ""):
            raise ApiError(415, "send JSON")
        try:
            body = json.loads(raw)
        except ValueError:
            raise ApiError(400, "the request is not valid JSON")
        if not isinstance(body, dict):
            raise ApiError(400, "the request must be a JSON object")
        return body

    # -- routing

    def do_GET(self):
        self._handle("GET")

    def do_HEAD(self):
        self._handle("HEAD")

    def do_POST(self):
        self._handle("POST")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_DELETE(self):
        self._handle("DELETE")

    def _handle(self, method):
        d = self.dashboard
        try:
            if not host_allowed(self.headers.get("Host"), d.settings["allowedHosts"]):
                raise ApiError(421, "unknown host name; add it to allowedHosts in the dashboard config")
            url = urlsplit(self.path)
            path = url.path

            if not path.startswith("/api/"):
                if method not in ("GET", "HEAD") or path not in STATIC_FILES:
                    raise ApiError(404, "not found")
                name, content_type = STATIC_FILES[path]
                with open(os.path.join(STATIC_DIR, name), "rb") as f:
                    body = f.read()
                self._headers(200, content_type, len(body))
                if method != "HEAD":
                    self.wfile.write(body)
                return

            if self.headers.get(API_HEADER) != "1":
                raise ApiError(403, f"missing {API_HEADER} header")
            body = self._body() if method in ("POST", "PATCH") else {}

            if path == "/api/login" and method == "POST":
                token = d.login(str(body.get("password", "")))
                max_age = d.settings["sessionHours"] * 3600
                self._json(200, {"ok": True}, [("Set-Cookie", f"{COOKIE}={token}; Path=/; HttpOnly; "
                                                              f"SameSite=Strict; Max-Age={max_age}{self._secure()}")])
                return

            session = self._cookie()
            if path == "/api/logout" and method == "POST":
                d.logout(session)
                self._json(200, {"ok": True}, [("Set-Cookie", f"{COOKIE}=; Path=/; HttpOnly; "
                                                              f"SameSite=Strict; Max-Age=0{self._secure()}")])
                return
            if not d.authenticated(session):
                raise ApiError(401, "log in first")

            parts = [unquote(p) for p in path[len("/api/"):].split("/")]
            if parts == ["overview"] and method == "GET":
                return self._json(200, d.overview())
            if parts == ["reload"] and method == "POST":
                return self._json(200, d.reload())
            if parts == ["restart"] and method == "POST":
                return self._json(200, d.restart())
            if parts == ["settings"] and method == "GET":
                return self._json(200, d.settings_view())
            if parts == ["settings"] and method == "PATCH":
                return self._json(200, d.save_settings(body))
            if parts == ["users"] and method == "POST":
                return self._json(200, d.create_user(body))
            if len(parts) == 2 and parts[0] == "users":
                if method == "PATCH":
                    return self._json(200, d.update_user(parts[1], body))
                if method == "DELETE":
                    return self._json(200, d.delete_user(parts[1]))
            if len(parts) == 3 and parts[0] == "users":
                if parts[2] == "usage" and method == "POST":
                    return self._json(200, d.set_usage(parts[1], body))
                if parts[2] == "share" and method == "GET":
                    query = {k: v[0] for k, v in parse_qs(url.query).items()}
                    return self._json(200, d.share(parts[1], query))
            raise ApiError(404, "not found")
        except ApiError as e:
            self._error(e.status, e.message)
        except Exception:
            traceback.print_exc()
            self._error(500, "internal error; see the dashboard's output")


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        error = sys.exc_info()[1]
        if isinstance(error, (ssl.SSLError, ConnectionError, socket.timeout, TimeoutError)):
            # Failed TLS handshakes, plain HTTP to the TLS port, dropped clients: one line, no traceback.
            sys.stderr.write(f"{client_address[0]} connection error: {error}\n")
            return
        super().handle_error(request, client_address)

    def __init__(self, address, handler):
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, handler)


def serve(settings):
    dashboard = Dashboard(settings)
    handler = type("BoundHandler", (Handler,), {"dashboard": dashboard})
    httpd = Server((settings["listenIp"], settings["listenPort"]), handler)
    watcher = None
    if settings["tlsEnable"]:
        # The handshake runs in the request's thread, so a slow client cannot block others.
        httpd.socket = settings["sslContext"].wrap_socket(httpd.socket, server_side=True,
                                                          do_handshake_on_connect=False)
        watcher = CertificateWatcher(settings["sslContext"], settings["tlsCertFile"], settings["tlsKeyFile"])

        def watch_certificate():
            while not dashboard.stop.wait(60):
                watcher.check()

        threading.Thread(target=watch_certificate, daemon=True).start()

    def shutdown(*_):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, lambda *_: watcher and threading.Thread(
            target=watcher.check, kwargs={"force": True}, daemon=True).start())

    ip = ipaddress.ip_address(settings["listenIp"])
    scheme = "https" if settings["tlsEnable"] else "http"
    host = f"[{settings['listenIp']}]" if ip.version == 6 else settings["listenIp"]
    print(f"nipovpn dashboard on {scheme}://{host}:{settings['listenPort']}/ "
          f"managing {settings['nipovpnConfig']}", flush=True)
    if not ip.is_loopback and not settings["tlsEnable"]:
        print("warning: the dashboard serves plain HTTP; listening on a non-loopback address exposes "
              "the password and user tokens to the network. Turn on tlsEnable, or keep listenIp "
              "127.0.0.1 and use an SSH tunnel.", flush=True)
    httpd.serve_forever()
    dashboard.stop.set()
    httpd.server_close()


def main():
    parser = argparse.ArgumentParser(description="NipoVPN dashboard")
    parser.add_argument("command", nargs="?", choices=["serve", "hash-password", "make-cert"], default="serve")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help=f"dashboard settings (default: {DEFAULT_CONFIG})")
    parser.add_argument("--host", help="make-cert: the IP address or host name you open the dashboard with")
    parser.add_argument("--dir", default=DEFAULT_CERT_DIR,
                        help=f"make-cert: where to write dashboard.crt and dashboard.key (default: {DEFAULT_CERT_DIR})")
    parser.add_argument("--days", type=int, default=3650, help="make-cert: validity in days (default: 3650)")
    args = parser.parse_args()

    if args.command == "make-cert":
        if not args.host:
            parser.error("make-cert needs --host, the IP address or host name you open the dashboard with")
        cert, key = make_cert(args.host, args.dir, args.days)
        print(f"created {cert} and {key}. Put this in {args.config}:\n"
              f"tlsEnable: true\ntlsCertFile: \"{cert}\"\ntlsKeyFile: \"{key}\"\n"
              f"Browsers warn about a self-signed certificate; check its fingerprint once:\n"
              f"  openssl x509 -in {cert} -noout -fingerprint -sha256")
        return

    if args.command == "hash-password":
        if sys.stdin.isatty():
            password = getpass.getpass("Dashboard password: ")
            if password != getpass.getpass("Again: "):
                sys.exit("the passwords do not match")
        else:
            password = sys.stdin.readline().rstrip("\n")
        if len(password) < 8:
            sys.exit("use a password of at least 8 characters")
        print(hash_password(password))
        return

    try:
        settings = load_settings(args.config)
        serve(settings)
    except (SettingsError, serverconfig.ConfigError) as e:
        sys.exit(f"nipovpn-dashboard: {e}")
    except OSError as e:
        sys.exit(f"nipovpn-dashboard: cannot listen: {e.strerror}")


if __name__ == "__main__":
    main()
