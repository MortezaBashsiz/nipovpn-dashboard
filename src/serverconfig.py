"""Reading and editing the users of a nipovpn server config file.

Only the server.users block of the file is rewritten. Every other line,
comments included, is kept exactly as it was.
"""

import json
import os
import re
import secrets
import shutil
import tempfile

import yaml

USER_NAME_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")
MIN_TOKEN_LENGTH = 32
SAMPLE_TOKEN = "af445adb-2434-4975-9445-2c1b2231"

_SIZE_RE = re.compile(r"^ *([0-9]+(?:\.[0-9]*)?) *([A-Za-z]+) *$")
_UNITS = {
    "b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12,
    "kib": 1024.0, "mib": 1024.0 ** 2, "gib": 1024.0 ** 3, "tib": 1024.0 ** 4,
}
_KNOWN_KEYS = ("name", "token", "enabled", "limit")


class ConfigError(Exception):
    pass


def parse_byte_size(text):
    """Bytes in a size such as 2GB, 500MB or 1.5GiB, as the server reads it; None if invalid."""
    m = _SIZE_RE.match(str(text))
    if not m or m.group(2).lower() not in _UNITS:
        return None
    value = float(m.group(1)) * _UNITS[m.group(2).lower()]
    if value < 1 or value > 1.8e19:
        return None
    return int(value)


def new_token():
    """A token like `openssl rand -hex 16`."""
    return secrets.token_hex(16)


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e.strerror}") from e
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} is not valid YAML: {e}") from e
    if not isinstance(doc, dict):
        raise ConfigError(f"{path} is not a nipovpn config")
    return text, doc


def users_of(doc):
    """The users of a parsed config as dicts with name, token, enabled, limit (and any other keys)."""
    server = doc.get("server") or {}
    users = server.get("users")
    if users is None:
        return None
    result = []
    for entry in users if isinstance(users, list) else []:
        if not isinstance(entry, dict):
            continue
        user = dict(entry)
        user["name"] = str(entry.get("name", ""))
        user["token"] = str(entry.get("token", ""))
        user["enabled"] = entry.get("enabled", True) is not False
        user["limit"] = "" if entry.get("limit") in (None, "") else str(entry.get("limit"))
        result.append(user)
    return result


def validate_users(users):
    """Raises ConfigError with the same rules the server applies to server.users."""
    if not users:
        raise ConfigError("the server needs at least one user")
    names, tokens = {}, {}
    for user in users:
        name, token = user["name"], user["token"]
        if not USER_NAME_RE.match(name):
            raise ConfigError(f"user name {name!r} must be 1-64 characters of A-Z a-z 0-9 . _ @ -")
        if len(token.encode("utf-8")) < MIN_TOKEN_LENGTH:
            raise ConfigError(f"token of user {name} must be at least {MIN_TOKEN_LENGTH} bytes")
        if name in names:
            raise ConfigError(f"user name {name} is used more than once")
        if token in tokens:
            raise ConfigError(f"users {tokens[token]} and {name} have the same token")
        if user.get("limit") and parse_byte_size(user["limit"]) is None:
            raise ConfigError(f"limit of {name} must be a size with a unit, such as 2GB, 500MB or 1.5GiB")
        names[name] = True
        tokens[token] = name


def _yaml_scalar(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False, default=str)


def _render_users(users, indent):
    pad = " " * indent
    lines = [f"{pad}users:"]
    for user in users:
        first = True
        keys = [k for k in _KNOWN_KEYS if k != "limit" or user.get("limit")]
        keys += [k for k in user if k not in _KNOWN_KEYS]
        for key in keys:
            prefix = f"{pad}  - " if first else f"{pad}    "
            lines.append(f"{prefix}{key}: {_yaml_scalar(user[key])}")
            first = False
    return lines


def _indent(line):
    return len(line) - len(line.lstrip(" "))


def _is_blank(line):
    return line.strip() == ""


def _is_comment(line):
    return line.lstrip(" ").startswith("#")


def replace_users_block(text, users):
    """The config text with its server.users block replaced by users."""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)

    server = next((i for i, line in enumerate(lines) if re.match(r"^server\s*:\s*(#.*)?$", line)), None)
    if server is None:
        raise ConfigError("the config has no top-level server: block")

    end = len(lines)
    for i in range(server + 1, len(lines)):
        line = lines[i]
        if not _is_blank(line) and not _is_comment(line) and _indent(line) == 0:
            end = i
            break

    child = next((_indent(lines[i]) for i in range(server + 1, end)
                  if not _is_blank(lines[i]) and not _is_comment(lines[i])), None)
    if not child:
        raise ConfigError("the server: block of the config is empty")

    start = next((i for i in range(server + 1, end)
                  if _indent(lines[i]) == child and re.match(r"^users\s*:", lines[i][child:])), None)

    if start is not None:
        stop = start + 1
        while stop < end:
            line = lines[stop]
            if _is_blank(line) or _indent(line) > child:
                stop += 1
            elif _indent(line) == child and line[child:].startswith("- "):
                stop += 1
            else:
                break
        while stop > start + 1 and _is_blank(lines[stop - 1]):
            stop -= 1
    else:
        start = end
        while start > server + 1 and (_is_blank(lines[start - 1]) or _indent(lines[start - 1]) == 0):
            start -= 1
        stop = start

    new_lines = lines[:start] + _render_users(users, child) + lines[stop:]
    return newline.join(new_lines)


def _check_rewrite(old_doc, new_text, users):
    """Makes sure the rewrite changed server.users and nothing else."""
    try:
        new_doc = yaml.safe_load(new_text) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"the rewritten config is not valid YAML ({e}); edit the file by hand") from e
    expected = json.loads(json.dumps(old_doc, default=str))
    expected.setdefault("server", {})
    expected["server"]["users"] = [
        {k: v for k, v in u.items() if k != "limit" or v} for u in users
    ]
    got = json.loads(json.dumps(new_doc, default=str))
    if got != expected:
        raise ConfigError("the layout of the config is not supported for editing here; "
                          "edit server.users by hand")


def _write_atomically(path, text):
    directory = os.path.dirname(os.path.abspath(path))
    st = os.stat(path)
    try:
        fd, tmp = tempfile.mkstemp(prefix=".config.", suffix=".tmp", dir=directory)
    except OSError:
        fd = tmp = None
    if fd is not None:
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, st.st_mode & 0o7777)
            try:
                os.chown(tmp, st.st_uid, st.st_gid)
            except OSError:
                pass
            os.replace(tmp, path)
            return
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    # The directory is not writable or the file is a mount point (e.g. a Docker
    # bind mount of a single file): write the file in place.
    try:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        raise ConfigError(f"cannot write {path}: {e.strerror}") from e


def save_users(path, users):
    """Writes users as server.users of the config at path; keeps the previous file as <path>.bak."""
    validate_users(users)
    text, doc = load(path)
    if users_of(doc) is None:
        raise ConfigError("the config has no server.users list; add one by hand first")
    new_text = replace_users_block(text, users)
    _check_rewrite(doc, new_text, users)
    if new_text == text:
        return False
    try:
        shutil.copy2(path, path + ".bak")
    except OSError:
        pass
    _write_atomically(path, new_text)
    return True


# ---------------------------------------------------------------- single settings


def _key_re(key):
    return re.compile(r"^" + re.escape(key) + r"\s*:(\s|$)")


def _value_end(lines, i, end):
    """The line after the value of the key at line i (its nested lines included)."""
    k = _indent(lines[i])
    stop = i + 1
    while stop < end:
        line = lines[stop]
        if _is_blank(line) or _indent(line) > k:
            stop += 1
        elif _indent(line) == k and line[k:].startswith("- "):
            stop += 1
        else:
            break
    while stop > i + 1 and (_is_blank(lines[stop - 1]) or (_is_comment(lines[stop - 1]) and k == 0)):
        stop -= 1
    return stop


def _child_indent(lines, start, stop):
    for i in range(start + 1, stop):
        if not _is_blank(lines[i]) and not _is_comment(lines[i]):
            return _indent(lines[i])
    return None


def _split_comment(rest):
    """Splits the text after "key:" into the value and a trailing "# comment" (with its spacing)."""
    quote = None
    i = 0
    while i < len(rest):
        c = rest[i]
        if quote == '"' and c == "\\":
            i += 2
            continue
        if quote:
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c == "#" and (i == 0 or rest[i - 1] in " \t"):
            j = i
            while j > 0 and rest[j - 1] in " \t":
                j -= 1
            return rest[:j], rest[j:]
        i += 1
    return rest, ""


def _render_value(key, value, indent):
    pad = " " * indent
    if isinstance(value, list):
        if not value:
            return [f"{pad}{key}: []"]
        return [f"{pad}{key}:"] + [f"{pad}  - {_yaml_scalar(v)}" for v in value]
    return [f"{pad}{key}: {_yaml_scalar(value)}"]


def _set_value(lines, path, value):
    start, stop, depth = -1, len(lines), 0
    while depth < len(path):
        key = path[depth]
        indent = 0 if depth == 0 else _child_indent(lines, start, stop)
        found = None
        if indent is not None:
            regex = _key_re(key)
            for i in range(start + 1, stop):
                if _indent(lines[i]) == indent and not _is_comment(lines[i]) and regex.match(lines[i][indent:]):
                    found = i
                    break
        if found is None:
            break
        start, stop = found, _value_end(lines, found, stop)
        depth += 1

    if depth == len(path):
        i, key = start, path[-1]
        indent = _indent(lines[i])
        if stop == i + 1 and not isinstance(value, list):
            m = re.match(r"^(\s*" + re.escape(key) + r"\s*:)(.*)$", lines[i])
            _, comment = _split_comment(m.group(2))
            lines[i] = f"{m.group(1)} {_yaml_scalar(value)}{comment}"
        else:
            lines[i:stop] = _render_value(key, value, indent)
        return

    # Insert the missing keys at the end of the deepest block that exists.
    if depth == 0:
        insert, indent = len(lines), 0
        while insert > 0 and _is_blank(lines[insert - 1]):
            insert -= 1
    else:
        insert = stop
        child = _child_indent(lines, start, stop)
        indent = child if child is not None else _indent(lines[start]) + 2
    new = []
    for key in path[depth:-1]:
        new.append(" " * indent + f"{key}:")
        indent += 2
    new += _render_value(path[-1], value, indent)
    lines[insert:insert] = new


def set_values(text, changes):
    """The config text with each (path tuple -> value) of changes set; comments are kept."""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)
    for path, value in changes.items():
        _set_value(lines, path, value)
    return newline.join(lines)


def _check_values(old_doc, new_text, changes):
    """Makes sure the edit set changes and nothing else."""
    try:
        new_doc = yaml.safe_load(new_text) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"the edited config is not valid YAML ({e}); edit the file by hand") from e
    expected = json.loads(json.dumps(old_doc, default=str))
    for path, value in changes.items():
        node = expected
        for key in path[:-1]:
            if not isinstance(node.get(key), dict):
                node[key] = {}
            node = node[key]
        node[path[-1]] = value
    if json.loads(json.dumps(new_doc, default=str)) != expected:
        raise ConfigError("the layout of the config is not supported for editing here; edit it by hand")


def _commit(path, text, new_text):
    if new_text == text:
        return False
    try:
        shutil.copy2(path, path + ".bak")
    except OSError:
        pass
    _write_atomically(path, new_text)
    return True


def save_values(path, changes):
    """Sets each (path tuple -> value) of changes in the config at path; keeps the previous file as <path>.bak."""
    text, doc = load(path)
    new_text = set_values(text, changes)
    _check_values(doc, new_text, changes)
    return _commit(path, text, new_text)
