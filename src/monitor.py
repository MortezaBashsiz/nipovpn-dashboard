"""What the dashboard knows about a running server, without talking to it.

- usage: the usage file the server writes every 30 seconds
- activity: the server log (every tunnel/CONNECT line names its user)
- process: /proc (pid, uptime, established connections on the listen port)
"""

import collections
import os
import re
import threading
import time

import yaml

_LINE_RE = re.compile(r"^(\d{4}-\d\d-\d\d_\d\d:\d\d:\d\d) \[(?i:server)\] \[(\w+)\] (.*)$")
_USER_RE = re.compile(r"\[SRC ([^\]]*)\] \[USER ([A-Za-z0-9._@-]{1,64})\]")
_EVENT_RE = re.compile(r"\[Runner|\[Usage\]|\[WARNING\]|Config initialized")
_SECTION_RE = re.compile(r"^ (General|Log|Server|Agent|Users) :$")
_DUMP_KEY_RE = re.compile(r"^   (\w+): ?(.*)$")
_TRACKING_RE = re.compile(r"^\[Usage\] tracking usage in (.+), period from ")
_RUNNING_KEYS = {"general.tlsEnable", "server.listenIp", "server.listenPort", "server.threads"}
_SEED_BYTES = 4 * 1024 * 1024
_READ_LIMIT = 8 * 1024 * 1024


def _parse_time(stamp):
    try:
        return time.mktime(time.strptime(stamp, "%Y-%m-%d_%H:%M:%S"))
    except ValueError:
        return None


class LogTail:
    def __init__(self, path):
        self.path = path
        self.offset = None
        self.inode = None
        self.lock = threading.Lock()
        self.last_seen = {}
        self.last_src = {}
        self.connects = collections.defaultdict(collections.deque)
        self.events = collections.deque(maxlen=200)
        self.seq = 0
        self.error = None
        # What the running server started with, from the config it logs at startup.
        self.running = {}
        self.started_at = None
        self._dump = None

    def set_path(self, path):
        with self.lock:
            if path != self.path:
                self.path, self.offset, self.inode = path, None, None

    def poll(self):
        with self.lock:
            return self._poll()

    def _poll(self):
        try:
            st = os.stat(self.path)
        except OSError as e:
            self.error = f"cannot read the server log {self.path}: {e.strerror}"
            return
        self.error = None
        if self.offset is None or st.st_ino != self.inode or st.st_size < self.offset:
            self.offset = max(0, st.st_size - _SEED_BYTES) if self.offset is None else 0
            self.inode = st.st_ino
        if st.st_size == self.offset:
            return
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                data = f.read(_READ_LIMIT)
        except OSError as e:
            self.error = f"cannot read the server log {self.path}: {e.strerror}"
            return
        end = data.rfind(b"\n")
        if end < 0:
            if len(data) >= _READ_LIMIT:
                self.offset += len(data)
            return
        self.offset += end + 1
        for raw in data[:end].split(b"\n"):
            self._line(raw.decode("utf-8", "replace"))
        cutoff = time.time() - 3600
        for times in self.connects.values():
            while times and times[0] < cutoff:
                times.popleft()

    def _dump_line(self, line):
        section = _SECTION_RE.match(line)
        if section:
            self._dump = section.group(1).lower()
            return
        kv = _DUMP_KEY_RE.match(line)
        if kv and f"{self._dump}.{kv.group(1)}" in _RUNNING_KEYS:
            self.running[f"{self._dump}.{kv.group(1)}"] = kv.group(2).strip()

    def _line(self, line):
        m = _LINE_RE.match(line)
        if not m:
            if self._dump is not None:
                self._dump_line(line)
            return
        stamp, level, message = m.groups()
        self._dump = None
        when = _parse_time(stamp)
        if when is None:
            return
        if message.strip() == "":
            self._dump = ""
            return
        if message.startswith("Config initialized"):
            self.running = {}
            self.started_at = when
        tracking = _TRACKING_RE.match(message)
        if tracking:
            self.running["server.usage.file"] = tracking.group(1)
        u = _USER_RE.search(message)
        if u and "[CONNECT]" in message:
            src, user = u.groups()
            self.last_seen[user] = when
            self.last_src[user] = src.rsplit(":", 1)[0] if ":" in src else src
            times = self.connects[user]
            times.append(when)
            if len(times) > 100000:
                times.popleft()
        elif _EVENT_RE.search(message) or level == "ERROR" and "[USER" not in message:
            if message.startswith("\nConfig :") or message.startswith("Config :"):
                return
            self.seq += 1
            self.events.append({"seq": self.seq, "time": when, "level": level,
                                "message": message.strip()[:500]})

    def mark(self):
        """The sequence number of the latest event, after reading what is new in the log."""
        with self.lock:
            self._poll()
            return self.seq

    def wait_for(self, pattern, after_seq, timeout):
        """The first event matching pattern newer than after_seq, or None after timeout seconds."""
        regex = re.compile(pattern)
        deadline = time.time() + timeout
        while True:
            with self.lock:
                self._poll()
                for event in self.events:
                    if event["seq"] > after_seq and regex.search(event["message"]):
                        return event
            if time.time() >= deadline:
                return None
            time.sleep(0.1)


class UsageFile:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.mtime = None
        self.period = ""
        self.used = {}
        self.rate = {}
        self.sampled_at = None
        self.error = None

    def poll(self):
        with self.lock:
            try:
                st = os.stat(self.path)
            except FileNotFoundError:
                self.error = None
                self.used, self.period = {}, ""
                return
            except OSError as e:
                self.error = f"cannot read the usage file {self.path}: {e.strerror}"
                return
            if st.st_mtime == self.mtime:
                return
            try:
                with open(self.path, encoding="utf-8") as f:
                    doc = yaml.safe_load(f) or {}
            except (OSError, yaml.YAMLError) as e:
                self.error = f"cannot read the usage file {self.path}: {e}"
                return
            self.error = None
            used = {}
            for name, entry in (doc.get("users") or {}).items():
                try:
                    used[str(name)] = int((entry or {}).get("used", 0))
                except (TypeError, ValueError):
                    continue
            now = time.time()
            if self.sampled_at is not None and str(doc.get("period", "")) == self.period:
                elapsed = max(now - self.sampled_at, 1.0)
                self.rate = {n: max(0, (v - self.used.get(n, v))) / elapsed for n, v in used.items()}
            else:
                self.rate = {}
            self.period = str(doc.get("period", ""))
            self.used, self.mtime, self.sampled_at = used, st.st_mtime, now

    def set_used(self, name, value):
        """Changes the usage of one user; the server applies it within 30 seconds."""
        with self.lock:
            try:
                with open(self.path, encoding="utf-8") as f:
                    text = f.read()
                doc = yaml.safe_load(text) or {}
            except FileNotFoundError:
                raise RuntimeError("the server has not written its usage file yet; try again in 30 seconds")
            except (OSError, yaml.YAMLError) as e:
                raise RuntimeError(f"cannot read the usage file {self.path}: {e}")
            users = doc.get("users") or {}
            pattern = re.compile(r"^(  " + re.escape(name) + r": \{used: )\d+(\})", re.M)
            if name in users and pattern.search(text):
                text = pattern.sub(lambda m: f"{m.group(1)}{int(value)}{m.group(2)}", text, count=1)
            else:
                text = text.rstrip("\n") + f"\n  {name}: {{used: {int(value)}}}\n"
                if "users:" not in text:
                    raise RuntimeError("the usage file has no users list")
            tmp = self.path + ".dashboard.tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(text)
                os.replace(tmp, self.path)
            except OSError as e:
                raise RuntimeError(f"cannot write the usage file {self.path}: {e.strerror}")
            self.used[name] = int(value)


def _proc_cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
    except OSError:
        return None


def find_server_process(config_path):
    """The pid of the nipovpn server using config_path (or the only nipovpn server), or None."""
    if not os.path.isdir("/proc"):
        return None
    want = os.path.realpath(config_path)
    candidates = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        args = _proc_cmdline(entry)
        if not args or os.path.basename(args[0]) != "nipovpn":
            continue
        if len(args) >= 3 and args[1] == "server":
            candidates.append((int(entry), args[2]))
    for pid, path in candidates:
        if os.path.realpath(path) == want:
            return pid
    return candidates[0][0] if len(candidates) == 1 else None


def process_uptime(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        start_ticks = int(fields[19])
        with open("/proc/uptime") as f:
            uptime = float(f.read().split()[0])
        return max(0.0, uptime - start_ticks / os.sysconf("SC_CLK_TCK"))
    except (OSError, ValueError, IndexError):
        return None


def established_connections(pid, port):
    """Established TCP connections on the server's listen port, seen in its network namespace."""
    total, found = 0, False
    for name in ("tcp", "tcp6"):
        path = f"/proc/{pid}/net/{name}" if pid else f"/proc/net/{name}"
        try:
            with open(path) as f:
                next(f)
                for line in f:
                    parts = line.split()
                    if len(parts) > 3 and parts[3] == "01" and int(parts[1].rsplit(":", 1)[1], 16) == port:
                        total += 1
            found = True
        except (OSError, ValueError, StopIteration):
            continue
    return total if found else None
