"""The server settings the dashboard can change, with the rules the server applies.

Each field names its place in config.yaml, its type and limits, and what a change
means: restart (the server only applies it on restart) or agents (agents must
use the same value, so existing agent configs stop working).
"""

import ipaddress
import os
import re

import serverconfig

SECTIONS = [
    {"id": "link", "title": "Agent ↔ server link",
     "description": "How agents and this server talk. Every agent must use the same values: "
                    "after a change, send every user a new agent config."},
    {"id": "server", "title": "Server",
     "description": "Where the server listens and how it handles connections."},
    {"id": "tls", "title": "TLS certificate",
     "description": "The certificate the server presents when TLS is on. The files are read for "
                    "every new connection, so a renewed certificate needs no reload."},
    {"id": "usage", "title": "Usage tracking",
     "description": "Per-user usage and the monthly reset."},
    {"id": "log", "title": "Logging",
     "description": "The dashboard reads the log for activity and events; keep the level at INFO or above."},
    {"id": "agent", "title": "Agent template",
     "description": "The agent block of the server config. The dashboard builds shared agent configs "
                    "and nipovpn:// links from it; the server itself does not use it."},
]

HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
PATH_RE = re.compile(r"^[^\0\n\r]{1,1024}$")
ENDPOINT_RE = re.compile(r"^[A-Za-z0-9._~!$&'()*+,;=:@/-]{1,200}$")
METHOD_RE = re.compile(r"^[A-Z]{1,20}$")


def _f(id, section, label, type, default, help="", **kw):
    field = {"id": id, "path": tuple(id.split(".")), "section": section, "label": label,
             "type": type, "default": default, "help": help}
    field.update(kw)
    return field


FIELDS = [
    _f("general.tunnelEnable", "link", "Tunnel mode", "bool", False,
       "On: a TCP/TLS tunnel between agent and server. Off: the agent polls the server over HTTP.",
       agents=True),
    _f("general.tlsEnable", "link", "TLS", "bool", False,
       "TLS between agent and server. Needs a certificate (below).", agents=True, restart=True),
    _f("general.aesEnable", "link", "AES-256-GCM encryption", "bool", True,
       "Encrypts payloads. Off: requests are only signed, so traffic is readable on the path unless TLS is on.",
       agents=True),
    _f("general.timeout", "link", "I/O timeout", "int", 10,
       "Seconds before a stalled connection is closed. 0 means no timeout.", min=0, max=65535, unit="s"),

    _f("server.listenIp", "server", "Listen address", "ip", "127.0.0.1",
       "IPv4 address the server listens on; 0.0.0.0 for all.", restart=True),
    _f("server.listenPort", "server", "Listen port", "int", 8080,
       "Agents connect to this port; existing agent configs keep the old one.", min=1, max=65535,
       restart=True, agents=True),
    _f("server.threads", "server", "Threads", "int", 2, "Worker threads.", min=1, max=1024, restart=True),
    _f("server.bufferSize", "server", "Buffer size", "int", 65536,
       "Most bytes read from a destination at a time in polling mode.", min=1, max=65536, unit="bytes",
       moved=True),

    _f("server.tlsCertFile", "tls", "Certificate file", "path", "",
       "PEM certificate (a chain is allowed).", moved=True, file=True),
    _f("server.tlsKeyFile", "tls", "Private key file", "path", "", "PEM private key.", moved=True, file=True),

    _f("server.usage.resetDay", "usage", "Reset day", "int", 1,
       "Day of the month (1-28) on which everyone's usage resets.", min=1, max=28),
    _f("server.usage.file", "usage", "Usage file", "path", "/var/lib/nipovpn/usage.yaml",
       "Where usage is kept.", restart=True),

    _f("log.logLevel", "log", "Log level", "enum", "INFO",
       "INFO logs connections; TRACE adds every request; DEBUG adds everything.",
       options=["INFO", "TRACE", "DEBUG"]),
    _f("log.logFile", "log", "Log file", "path", "/var/log/nipovpn/nipovpn.log",
       "The directory must exist and be writable by the server."),

    _f("agent.protocol", "agent", "Local protocol", "enum", "socks5",
       "What applications use to connect to the agent.", options=["socks5", "http"], moved=True),
    _f("agent.listenIp", "agent", "Agent listen address", "ip", "127.0.0.1",
       "127.0.0.1 for the device itself; 0.0.0.0 to share the agent on the local network."),
    _f("agent.listenPort", "agent", "Agent listen port", "int", 8080, "", min=1, max=65535),
    _f("agent.threads", "agent", "Agent threads", "int", 2, "", min=1, max=1024),
    _f("agent.fakeUrls", "agent", "Fake hosts", "list", [],
       "Hosts used at random for the Host header and TLS SNI. One per line.", moved=True, item="host"),
    _f("agent.methods", "agent", "HTTP methods", "list", [],
       "Used at random for requests to the server. One per line.", moved=True, item="method"),
    _f("agent.endPoints", "agent", "URL paths", "list", [],
       "Used at random for requests to the server. One per line.", moved=True, item="endpoint"),
    _f("agent.pullTimeout", "agent", "Poll interval", "int", 1,
       "Base interval between polls in polling mode.", min=0, max=65535, unit="ms", moved=True),
    _f("agent.connectionReuse", "agent", "Reuse connections", "bool", False,
       "Send requests over one kept-alive connection instead of a new one each time.", moved=True),
    _f("agent.httpVersion", "agent", "HTTP version", "enum", "1.1", "", options=["1.1", "1.0"]),
    _f("agent.userAgent", "agent", "User agent", "str", "NipoAgent",
       "The User-Agent header the agent sends.", max=500),
    _f("agent.tlsVerifyPeer", "agent", "Verify server certificate", "bool", False,
       "Needs a certificate the agent trusts (a CA file or a public CA).", moved=True),
    _f("agent.tlsCaFile", "agent", "CA file", "path", "", "CA file used to verify the server certificate.",
       moved=True, optional=True),
]
BY_ID = {f["id"]: f for f in FIELDS}


def _get(doc, path):
    node = doc
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def current(doc):
    """The value of every field in a parsed config, with the server's defaults."""
    values = {}
    for f in FIELDS:
        value = _get(doc, f["path"])
        if value is None and f.get("moved"):
            value = _get(doc, ("general", f["path"][-1]))
        values[f["id"]] = f["default"] if value is None else value
    return values


def normalize(field, value):
    """value checked and converted to the field's type; raises ValueError with a message."""
    label, t = field["label"], field["type"]
    if t == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{label} must be on or off")
        return value
    if t == "int":
        if isinstance(value, bool):
            raise ValueError(f"{label} must be a whole number")
        try:
            number = int(str(value).strip())
        except ValueError:
            raise ValueError(f"{label} must be a whole number")
        if not field["min"] <= number <= field["max"]:
            raise ValueError(f"{label} must be between {field['min']} and {field['max']}")
        return number
    if t == "enum":
        if str(value) not in field["options"]:
            raise ValueError(f"{label} must be one of {', '.join(field['options'])}")
        return str(value)
    if t == "ip":
        text = str(value).strip()
        try:
            if ipaddress.ip_address(text).version != 4:
                raise ValueError
        except ValueError:
            raise ValueError(f"{label} must be an IPv4 address, such as 0.0.0.0 or 127.0.0.1")
        return text
    if t == "path":
        text = str(value).strip()
        if not text and (field.get("optional") or field["section"] == "tls"):
            return ""
        if not PATH_RE.match(text) or not text.startswith("/") and os.name != "nt":
            raise ValueError(f"{label} must be an absolute path")
        return text
    if t == "str":
        text = str(value)
        if not text.strip() or len(text) > field.get("max", 1000) or "\n" in text:
            raise ValueError(f"{label} must be one line of text")
        return text
    if t == "list":
        if isinstance(value, str):
            value = value.splitlines()
        if not isinstance(value, list):
            raise ValueError(f"{label} must be a list")
        items = [str(v).strip() for v in value if str(v).strip()]
        if not items:
            raise ValueError(f"{label} needs at least one entry")
        check = {"host": HOST_RE, "method": METHOD_RE, "endpoint": ENDPOINT_RE}[field["item"]]
        for item in items:
            if not check.match(item):
                raise ValueError(f"{label}: {item!r} is not valid")
        return items
    raise ValueError(f"unknown field type {t}")


def plan(doc, requested):
    """Checks the requested changes {id: value}; returns (changes {path: value}, changed ids, warnings)."""
    before = current(doc)
    after = dict(before)
    changed = []
    for id, value in requested.items():
        field = BY_ID.get(id)
        if field is None:
            raise ValueError(f"unknown setting {id}")
        value = normalize(field, value)
        if value != before[id]:
            after[id] = value
            changed.append(id)

    warnings = []
    if after["general.tlsEnable"]:
        for id in ("server.tlsCertFile", "server.tlsKeyFile"):
            if not after[id]:
                raise ValueError(f"TLS is on, so {BY_ID[id]['label'].lower()} is required")
            if id in changed or "general.tlsEnable" in changed:
                if not os.path.isfile(after[id]):
                    warnings.append(f"{after[id]} does not exist (from the dashboard's view)")
    if "log.logFile" in changed and not os.path.isdir(os.path.dirname(after["log.logFile"]) or "/"):
        warnings.append(f"the directory of {after['log.logFile']} does not exist; "
                        "the server cannot write its log there")
    if "log.logLevel" in changed and after["log.logLevel"] == "DEBUG":
        warnings.append("DEBUG logs a lot; use it only while troubleshooting")

    changes = {BY_ID[id]["path"]: after[id] for id in changed}
    return changes, changed, warnings


def describe(doc, running):
    """Every field with its value, for the settings page."""
    values = current(doc)
    result = []
    for f in FIELDS:
        item = {k: v for k, v in f.items() if k != "path"}
        item["value"] = values[f["id"]]
        if f.get("restart") and f["id"] in running:
            item["running"] = running[f["id"]]
            item["pending"] = not same(running[f["id"]], values[f["id"]])
        result.append(item)
    return result


def same(running_text, value):
    if isinstance(value, bool):
        return str(running_text).lower() == ("true" if value else "false")
    return str(running_text) == str(value)


def save(path, requested):
    """Applies the requested changes to the config at path; returns (changed ids, warnings)."""
    _, doc = serverconfig.load(path)
    changes, changed, warnings = plan(doc, requested)
    if changes:
        serverconfig.save_values(path, changes)
    return changed, warnings
