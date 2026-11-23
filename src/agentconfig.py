"""The agent config.yaml and nipovpn:// link of one user of a server."""

import base64
import copy
import json

import yaml

AGENT_DEFAULTS = {
    "threads": 16,
    "listenIp": "127.0.0.1",
    "listenPort": 8080,
    "protocol": "socks5",
    "fakeUrls": ["google.com", "cloudflare.com"],
    "methods": ["GET", "POST"],
    "endPoints": ["api", "login"],
    "pullTimeout": 1,
    "connectionReuse": True,
    "tlsVerifyPeer": False,
    "tlsCaFile": "",
    "httpVersion": "1.1",
    "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
}


def agent_config(server_cfg, user, address, port):
    """An agent config for user, built from the server's config."""
    general = server_cfg.get("general") or {}
    agent = copy.deepcopy(server_cfg.get("agent") or {})
    moved = lambda key, default: agent.get(key, general.get(key, default))

    agent_block = {key: moved(key, default) for key, default in AGENT_DEFAULTS.items()}
    agent_block.update(serverIp=address, serverPort=int(port), token=user["token"])

    return {
        "general": {
            "aesEnable": bool(general.get("aesEnable", True)),
            "timeout": general.get("timeout", 10),
            "tunnelEnable": bool(general.get("tunnelEnable", False)),
            "tlsEnable": bool(general.get("tlsEnable", False)),
        },
        "log": {"logLevel": "INFO", "logFile": "/var/log/nipovpn/nipovpn.log"},
        # The agent does not use this block, but the config must have one.
        "server": {"listenIp": "0.0.0.0", "listenPort": int(port)},
        "agent": agent_block,
    }


def agent_yaml(cfg, user_name):
    header = (f"# NipoVPN agent config for user {user_name}.\n"
              "# Run it with: nipovpn agent /etc/nipovpn/config.yaml\n")
    return header + yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True)


def _str(value):
    return "" if value is None else str(value)


def _lines(value):
    if isinstance(value, (list, tuple)):
        return "\n".join(str(v) for v in value)
    return _str(value)


def share_url(cfg, name):
    """The nipovpn:// link of an agent config; the same encoding as scripts/config2url.py."""
    general, log, agent = cfg["general"], cfg["log"], cfg["agent"]
    server = cfg.get("server") or {}
    config = {
        "token": _str(agent.get("token")),
        "protocol": _str(agent.get("protocol")),
        "fakeUrls": _lines(agent.get("fakeUrls")),
        "methods": _lines(agent.get("methods")),
        "endPoints": _lines(agent.get("endPoints")),
        "timeout": _str(general.get("timeout")),
        "pullTimeout": _str(agent.get("pullTimeout")),
        "tunnelEnable": bool(general.get("tunnelEnable", False)),
        "connectionReuse": bool(agent.get("connectionReuse", False)),
        "tlsEnable": bool(general.get("tlsEnable", False)),
        "tlsVerifyPeer": bool(agent.get("tlsVerifyPeer", False)),
        "tlsCertFile": _str(server.get("tlsCertFile")),
        "tlsKeyFile": _str(server.get("tlsKeyFile")),
        "tlsCaFile": _str(agent.get("tlsCaFile")),
        "logLevel": _str(log.get("logLevel")),
        "listenIp": _str(agent.get("listenIp")),
        "listenPort": _str(agent.get("listenPort")),
        "serverIp": _str(agent.get("serverIp")),
        "serverPort": _str(agent.get("serverPort")),
        "httpVersion": _str(agent.get("httpVersion")),
        "userAgent": _str(agent.get("userAgent")),
    }
    text = json.dumps({"name": name, "config": config}, separators=(",", ":"), ensure_ascii=False)
    text = text.replace("/", "\\/")
    return "nipovpn://" + base64.b64encode(text.encode("utf-8")).decode("ascii")
