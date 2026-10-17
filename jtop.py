#!/usr/bin/env python3
"""
jtop - workload-aware system monitor (Linux + NVIDIA + Docker/cgroup v2)

Built for a headless server's physical console: ZFS-aware memory accounting,
GPU VRAM attributed to the owning container, and a DETAIL column that asks
each app what it is actually doing.

Zero dependencies: Python 3.9+ stdlib only (curses, threading, subprocess).

Panels:
  * per-core CPU meters
  * one combined line: CPU total + memory split into apps / ZFS ARC / cache /
    free, so reclaimable ARC is not counted as used
  * GPU: one line per card - util bar, VRAM bar, temp, power, clocks, fan,
    P-state.  Per-process VRAM is attributed to the owning *container* in the
    tree's VRAM column, not a bare PID
  * container tree (cgroup v2), expandable to the processes inside
  * CONSOLE box: kernel/console messages, inside a region curses owns, so
    printk output no longer leaves stale text on the physical console
  * DETAIL column: what each app is actually doing (ollama's resident
    model, Plex sessions + hwaccel, *arr queues, SAB speed)

Nothing host-specific is hardcoded below the configuration block: paths,
cgroup roots and ports live in DEFAULT_CONFIG and can be overridden from a
JSON file. Run --dump-config to get a starting point.

The DETAIL column separates KINDS from SHAPES. A kind is a parser, in code:
http_json, http, docker_health, log_tail, command, plus a few app-specific
ones. A shape is data - which kind, which port, which field - so adding an
app is a JSON file in shapes/, not a patch to this module. Containers with
no shape still show their docker HEALTHCHECK verdict. Run --discover to
write a config for what is actually running here.

Keys:
  up/down or j/k   move             enter/space/left/right  expand/collapse
  PgUp/PgDn        page             Home/End                first/last
  c/m/n            sort by cpu/mem/name
  a / z            expand all / collapse all
  /                filter by name   esc  clear filter, else quit
  +/-              refresh -/+ 0.5s (0.5s-10s)
  l                CONSOLE box      [ / ]  box 1-20 rows
  ^L               full repaint     h or ?  help      q  quit

Options:
  <secs>          refresh interval (default 2)
  --demo          run on synthetic data: no /proc, no nvidia-smi, no docker,
                  no network. For screenshots and for trying the UI on a
                  machine with no GPU or containers.
  --dump-config   print the effective config as JSON and exit. Redirect it to
                  ~/.config/jtop/config.json, edit, and jtop picks it up.
  --config FILE   use FILE instead of $JTOP_CONFIG / ~/.config/jtop/config.json
                  / /etc/jtop/config.json (first found wins)
  --discover      probe the running containers, report which shapes match and
                  which ports/keys actually answer, and print a config you
                  can save. Writes nothing by itself.
  --discover=FILE as above, but write the config to FILE.
  --list-shapes   list known shapes and the kind each uses.
  --hush          while running, stop the kernel printing to the physical
                  console (console_loglevel -> 3). Messages still appear in
                  the CONSOLE box. Restored on a clean exit; a SIGKILL
                  leaves it lowered.
  -h, --help      print this text and exit.
"""

import calendar
import curses
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE_SZ = os.sysconf("SC_PAGE_SIZE")

# --------------------------------------------------------------------------
# configuration
#
# Everything host- or stack-specific lives here, so the only edit needed to
# run this on another box is this block (or a config file - see load_config).
# Nothing below this section hardcodes a port, path or app name.
# --------------------------------------------------------------------------
DEFAULT_CONFIG = {
    # where to look for container cgroups (cgroup v2)
    "cgroup_roots": ["/sys/fs/cgroup/docker", "/sys/fs/cgroup/system.slice"],
    # ZFS ARC stats; absent on non-ZFS hosts, in which case the memory split
    # simply shows no ARC segment
    "arcstats": "/proc/spl/kstat/zfs/arcstats",
    # Root of per-app config trees, used ONLY to find API keys for probes of
    # apps on this host. EMPTY BY DEFAULT: nothing reads app config until you
    # point this somewhere, so a fresh install never touches your secrets.
    # TrueNAS SCALE users want "/mnt/.ix-apps/app_mounts"; a plain Docker host
    # is usually the parent of your compose bind mounts, e.g. "/srv/appdata".
    "app_config_root": "",
    # host the probes talk to. Keep this loopback unless an app binds only to
    # a LAN address ("lan" resolves the primary LAN IP at runtime).
    "probe_host": "127.0.0.1",
    # per-probe timeout and how often the DETAIL column refreshes
    "probe_timeout": 2.5,
    "detail_interval": 5.0,

    # Shapes are DATA describing one app each (see BUILTIN_SHAPES). Drop
    # *.json files in any of these directories to add apps or override a
    # built-in of the same name - no code changes, no fork.
    "shape_dirs": ["~/.config/jtop/shapes", "/etc/jtop/shapes"],

    # Which rows get a DETAIL probe. "auto" probes every container whose
    # name matches a shape, which is what you want on a normal host. Or give
    # an explicit list of names (or inline shape dicts) to pin the order.
    "detail": "auto",

    # When a shape yields nothing - or no shape exists for a container - fall
    # back to the container's own docker HEALTHCHECK verdict. Costs one
    # `docker ps` for every container and needs no per-app knowledge.
    "health_fallback": True,
}

def config_paths():
    """Search order for a config file, first match wins.

    Built per call, NOT as a module constant: --config works by setting
    JTOP_CONFIG and re-loading, so reading the environment once at import
    time would make the flag silently do nothing.
    """
    return (os.environ.get("JTOP_CONFIG") or "",
            os.path.expanduser("~/.config/jtop/config.json"),
            "/etc/jtop/config.json")


def load_config():
    """DEFAULT_CONFIG, shallow-merged with the first config file found.

    Top-level keys are replaced wholesale, so a file supplying "probes" or
    "api_keys" fully overrides the built-in list rather than adding to it -
    that is what you want when running on a different stack. A broken or
    unreadable file does not take the UI down: the defaults stand and the
    reason is reported in the CONSOLE box (and on stderr for --config).
    """
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))   # deep copy
    for path in config_paths():
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path) as f:
                loaded = json.load(f)
            if not isinstance(loaded, dict):
                raise ValueError("top level must be an object")
            cfg.update(loaded)
            cfg["_loaded_from"] = path
        except (OSError, ValueError) as e:
            cfg["_config_error"] = "%s: %s" % (path, str(e)[:60])
        break
    return cfg


CONFIG = load_config()
CG_ROOTS = tuple(CONFIG["cgroup_roots"])
ARCSTATS = CONFIG["arcstats"]
IX = CONFIG["app_config_root"]


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def read(path, default=""):
    try:
        with open(path, "r", errors="replace") as f:
            return f.read()
    except (OSError, IOError):
        return default


def human0(n, unit="B"):
    """Bytes -> very compact human string, no decimals (for tight lines)."""
    if n is None:
        return "-"
    n = float(n)
    for suf in ("", "K", "M", "G", "T"):
        if abs(n) < 1024.0:
            return "%.0f%s" % (n, suf + unit if suf else unit)
        n /= 1024.0
    return "%.0fP%s" % (n, unit)


def short_gpu(name):
    """'NVIDIA GeForce RTX 3080' -> 'RTX 3080'."""
    n = (name or "GPU").strip()
    for drop in ("NVIDIA ", "GeForce ", "Quadro ", "Tesla "):
        if n.startswith(drop):
            n = n[len(drop):]
    return n


def human(n, unit="B"):
    """Bytes -> compact human string."""
    if n is None:
        return "-"
    n = float(n)
    for suf in ("", "K", "M", "G", "T"):
        if abs(n) < 1024.0:
            if suf == "":
                return "%d%s" % (n, unit)
            return "%.1f%s" % (n, suf + unit)
        n /= 1024.0
    return "%.1fP%s" % (n, unit)


def hms(secs):
    secs = int(secs)
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return "%dd %02d:%02d" % (d, h, m)
    return "%02d:%02d:%02d" % (h, m, s)


def cpu_time_str(secs):
    """htop-style TIME+ column."""
    m, s = divmod(secs, 60)
    h, m = divmod(int(m), 60)
    if h:
        return "%dh%02d:%04.1f" % (h, m, s)
    return "%d:%04.1f" % (m, s)


def prettify(name):
    """ix-firecrawl-playwright-service-1 -> firecrawl-playwright-service
       ix-plex-plex-1                    -> plex"""
    n = name
    if n.startswith("ix-"):
        n = n[3:]
    n = re.sub(r"-\d+$", "", n)
    parts = n.split("-")
    # compose-style names repeat the stack name: myapp-web-myapp-web
    half = len(parts) // 2
    if len(parts) % 2 == 0 and parts[:half] == parts[half:]:
        parts = parts[:half]
    out = []
    for p in parts:
        if not out or out[-1] != p:
            out.append(p)
    return "-".join(out)


# --------------------------------------------------------------------------
# samplers
# --------------------------------------------------------------------------
class CpuSampler:
    """Per-core and aggregate CPU utilisation from /proc/stat deltas."""

    def __init__(self):
        self.prev = {}
        self.total = 0.0
        self.cores = []
        self.ncpu = os.cpu_count() or 1

    def sample(self):
        cur = {}
        for line in read("/proc/stat").splitlines():
            if not line.startswith("cpu"):
                break
            f = line.split()
            key = f[0]
            vals = [int(x) for x in f[1:]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            tot = sum(vals[:8]) if len(vals) >= 8 else sum(vals)
            cur[key] = (tot, idle)

        cores = []
        total = 0.0
        for key in sorted(cur, key=lambda k: (k != "cpu", int(k[3:] or -1))):
            tot, idle = cur[key]
            ptot, pidle = self.prev.get(key, (tot, idle))
            dt, di = tot - ptot, idle - pidle
            pct = 0.0 if dt <= 0 else max(0.0, min(100.0, 100.0 * (dt - di) / dt))
            if key == "cpu":
                total = pct
            else:
                cores.append(pct)
        self.prev = cur
        self.total = total
        self.cores = cores
        return total, cores


class MemSampler:
    """Memory split into apps / ZFS ARC / page cache / free."""

    def sample(self):
        mi = {}
        for line in read("/proc/meminfo").splitlines():
            k, _, v = line.partition(":")
            mi[k] = int(v.split()[0]) * 1024

        total = mi.get("MemTotal", 0)
        free = mi.get("MemFree", 0)
        cached = mi.get("Cached", 0) + mi.get("SReclaimable", 0) - mi.get("Shmem", 0)
        buffers = mi.get("Buffers", 0)

        arc = 0
        for line in read(ARCSTATS).splitlines():
            f = line.split()
            if len(f) == 3 and f[0] == "size":
                arc = int(f[2])
                break

        used = total - free - cached - buffers
        apps = max(0, used - arc)
        return {
            "total": total, "free": free, "cached": max(0, cached + buffers),
            "arc": arc, "apps": apps, "used": used,
            "swap_total": mi.get("SwapTotal", 0),
            "swap_used": mi.get("SwapTotal", 0) - mi.get("SwapFree", 0),
        }


class GpuSampler(threading.Thread):
    """nvidia-smi in a background thread so a slow call never stalls the UI."""

    GPU_FIELDS = ("utilization.gpu,utilization.memory,memory.used,memory.total,"
                  "temperature.gpu,power.draw,power.limit,clocks.sm,clocks.mem,"
                  "fan.speed,pstate,name")

    def __init__(self, interval=2.0):
        super().__init__(daemon=True)
        self.interval = interval
        self.lock = threading.Lock()
        self.gpus = []
        self.procs = []
        self.error = None
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def _run_smi(self, query, extra=None):
        cmd = ["nvidia-smi", "--query-%s=%s" % (query, extra),
               "--format=csv,noheader,nounits"]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        if out.returncode != 0:
            raise RuntimeError((out.stderr or "nvidia-smi failed").strip())
        return [l for l in out.stdout.splitlines() if l.strip()]

    def poll(self):
        try:
            gpus = []
            for line in self._run_smi("gpu", self.GPU_FIELDS):
                f = [x.strip() for x in line.split(",")]

                def num(i, cast=float):
                    try:
                        return cast(f[i])
                    except (ValueError, IndexError):
                        return None
                gpus.append({
                    "util": num(0, int), "memutil": num(1, int),
                    "mem_used": (num(2, float) or 0) * 1024 * 1024,
                    "mem_total": (num(3, float) or 0) * 1024 * 1024,
                    "temp": num(4, int), "power": num(5), "power_cap": num(6),
                    "sm_clk": num(7, int), "mem_clk": num(8, int),
                    "fan": num(9, int), "pstate": f[10] if len(f) > 10 else "-",
                    "name": f[11] if len(f) > 11 else "GPU",
                })
            procs = []
            for line in self._run_smi("compute-apps", "pid,used_memory"):
                f = [x.strip() for x in line.split(",")]
                try:
                    procs.append((int(f[0]), int(f[1]) * 1024 * 1024))
                except (ValueError, IndexError):
                    pass
            with self.lock:
                self.gpus, self.procs, self.error = gpus, procs, None
        except FileNotFoundError:
            with self.lock:
                self.error = "nvidia-smi not found"
        except Exception as e:  # noqa: BLE001 - surface anything to the UI
            with self.lock:
                self.error = str(e)[:60]

    def run(self):
        while not self._stop.is_set():
            self.poll()
            self._stop.wait(self.interval)

    def snapshot(self):
        with self.lock:
            return list(self.gpus), dict(self.procs), self.error


class DockerNames(threading.Thread):
    """container id -> display name.

    Three tiers, so this works whether or not the user can reach the docker
    socket (an unprivileged console login cannot):
      1. `docker ps` / `sudo -n docker ps` - exact compose names.
      2. /proc/<pid>/mountinfo - world readable, and TrueNAS bind-mounts
         .ix-apps/app_mounts/<app>, which names most ix- apps.
      3. the container init process's comm (postgres, redis-server, node).
    Tiers 2-3 need a pid per container, supplied by the caller.
    """

    APP_RE = re.compile(r"app_(?:mounts|configs)/([A-Za-z0-9_.-]+)")
    VOL_RE = re.compile(r"volumes/(?:ix-)?([A-Za-z0-9][A-Za-z0-9.-]*)_[A-Za-z0-9]")

    def __init__(self, interval=10.0):
        super().__init__(daemon=True)
        self.interval = interval
        self.lock = threading.Lock()
        self.names = {}
        self.error = None
        self.degraded = False
        self._pids = {}
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def set_pids(self, pids):
        """pids: {cid: representative pid} for the fallback tiers."""
        with self.lock:
            self._pids = dict(pids)

    def _from_docker(self):
        for cmd in (["docker", "ps", "--no-trunc", "--format", "{{.ID}} {{.Names}}"],
                    ["sudo", "-n", "docker", "ps", "--no-trunc",
                     "--format", "{{.ID}} {{.Names}}"]):
            try:
                out = subprocess.run(cmd, capture_output=True, text=True,
                                     timeout=10)
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                continue
            except Exception:  # noqa: BLE001
                continue
            if out.returncode != 0:
                continue
            names = {}
            for line in out.stdout.splitlines():
                cid, _, nm = line.strip().partition(" ")
                if cid and nm:
                    names[cid] = nm
            if names:
                return names
        return None

    def _from_proc(self, pids):
        names = {}
        for cid, pid in pids.items():
            label = None
            mi = read("/proc/%d/mountinfo" % pid)
            if mi:
                hits = self.APP_RE.findall(mi)
                if hits:
                    label = max(set(hits), key=hits.count)
                else:
                    # named docker volumes carry the compose stack name
                    vols = [v for v in self.VOL_RE.findall(mi) if len(v) < 40]
                    if vols:
                        stack = max(set(vols), key=vols.count)
                        comm = read("/proc/%d/comm" % pid).strip()
                        label = ("%s/%s" % (stack, comm)) if comm else stack
            if not label:
                comm = read("/proc/%d/comm" % pid).strip()
                if comm:
                    label = "%s [%s]" % (comm, cid[:8])
            names[cid] = label or cid[:12]
        return names

    def poll(self):
        exact = self._from_docker()
        if exact:
            with self.lock:
                self.names, self.error, self.degraded = exact, None, False
            return
        with self.lock:
            pids = dict(self._pids)
        guessed = self._from_proc(pids) if pids else {}
        with self.lock:
            self.names = guessed
            self.degraded = True
            self.error = ("no docker socket - names inferred from /proc"
                          if guessed else
                          "no docker access (showing short container ids)")

    def run(self):
        while not self._stop.is_set():
            self.poll()
            # while degraded, names depend on a pid map the UI supplies after
            # its first sample - retry soon instead of waiting a full cycle
            with self.lock:
                quick = self.degraded and not self.names
            self._stop.wait(1.0 if quick else self.interval)

    def snapshot(self):
        with self.lock:
            return dict(self.names), self.error


class ProcSampler:
    """All processes, their cgroup, CPU delta and RSS."""

    def __init__(self):
        self.prev = {}
        self.prev_t = time.monotonic()

    def sample(self):
        now = time.monotonic()
        dt = max(1e-6, now - self.prev_t)
        cur, procs = {}, {}

        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            stat = read("/proc/%d/stat" % pid)
            if not stat:
                continue
            rp = stat.rfind(")")
            lp = stat.find("(")
            if rp < 0 or lp < 0:
                continue
            comm = stat[lp + 1:rp]
            f = stat[rp + 2:].split()
            try:
                utime, stime = int(f[11]), int(f[12])
                threads = int(f[17])
                state = f[0]
            except (IndexError, ValueError):
                continue
            jiffies = utime + stime
            cur[pid] = jiffies
            prev = self.prev.get(pid)
            pct = 0.0 if prev is None else max(
                0.0, 100.0 * (jiffies - prev) / CLK_TCK / dt)

            rss = 0
            statm = read("/proc/%d/statm" % pid).split()
            if len(statm) > 1:
                try:
                    rss = int(statm[1]) * PAGE_SZ
                except ValueError:
                    pass

            cg = read("/proc/%d/cgroup" % pid).strip()
            cid = None
            m = re.search(r"/docker[-/]([0-9a-f]{12,64})", cg)
            if m:
                cid = m.group(1).replace(".scope", "")

            procs[pid] = {
                "pid": pid, "comm": comm, "cpu": pct, "rss": rss,
                "cid": cid, "threads": threads, "state": state,
                "time": jiffies / CLK_TCK,
            }

        self.prev = cur
        self.prev_t = now
        return procs


class CgroupSampler:
    """Container-level CPU / memory / IO straight from cgroup v2."""

    def __init__(self):
        self.prev = {}
        self.prev_t = time.monotonic()

    @staticmethod
    def _dirs():
        found = {}
        for root in CG_ROOTS:
            if not os.path.isdir(root):
                continue
            try:
                for e in os.scandir(root):
                    if not e.is_dir():
                        continue
                    m = re.fullmatch(r"(?:docker-)?([0-9a-f]{12,64})(?:\.scope)?",
                                     e.name)
                    if m:
                        found[m.group(1)] = e.path
            except OSError:
                continue
        return found

    def sample(self):
        now = time.monotonic()
        dt = max(1e-6, now - self.prev_t)
        out, cur = {}, {}

        for cid, path in self._dirs().items():
            usec = 0
            for line in read(os.path.join(path, "cpu.stat")).splitlines():
                if line.startswith("usage_usec"):
                    usec = int(line.split()[1])
                    break
            mem = read(os.path.join(path, "memory.current"), "0").strip()
            mem = int(mem) if mem.isdigit() else 0

            rb = wb = 0
            for line in read(os.path.join(path, "io.stat")).splitlines():
                for tok in line.split()[1:]:
                    k, _, v = tok.partition("=")
                    if k == "rbytes":
                        rb += int(v)
                    elif k == "wbytes":
                        wb += int(v)

            cur[cid] = (usec, rb, wb)
            p = self.prev.get(cid)
            if p:
                cpu = max(0.0, (usec - p[0]) / 1e6 / dt * 100.0)
                rd = max(0, rb - p[1]) / dt
                wr = max(0, wb - p[2]) / dt
            else:
                cpu = rd = wr = 0.0
            out[cid] = {"cid": cid, "cpu": cpu, "mem": mem,
                        "rd": rd, "wr": wr, "cputime": usec / 1e6}

        self.prev = cur
        self.prev_t = now
        return out


# --------------------------------------------------------------------------
# DETAIL column: kinds (code) vs shapes (data)
#
# A "kind" is a small reusable PARSER, written here in Python and reviewed:
# docker_health, http_json, log_tail, command, and so on. There are only a
# handful and they rarely change.
#
# A "shape" is DATA describing one app: which kind to use, which port and
# path, which field or pattern to pull out. Shapes live in JSON - embedded in
# BUILTIN_SHAPES below so the single-file install keeps working, and loadable
# from a shapes/ directory, which overrides a built-in of the same name.
#
# See CONTRIBUTING.md before adding a kind.
# --------------------------------------------------------------------------

def dig(obj, path):
    """Pull a value out of nested JSON with a dotted path.

    Supports list indices and a [] "first element" shorthand, so a shape can
    say "records[].title" without needing code:
        queue.totalRecords      -> obj["queue"]["totalRecords"]
        records[].title         -> obj["records"][0]["title"]
        items[2].name           -> obj["items"][2]["name"]
    Returns None for any missing link rather than raising, since a shape
    describing an app version you do not run must degrade to a blank cell.
    """
    cur = obj
    if not path:
        return cur
    for part in str(path).split("."):
        if cur is None:
            return None
        m = re.match(r"^([^\[]*)\[(\d*)\]$", part)
        idx = None
        if m:
            part, idx = m.group(1), (int(m.group(2)) if m.group(2) else 0)
        if part:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(part)
        if idx is not None:
            if not isinstance(cur, (list, tuple)) or len(cur) <= idx:
                return None
            cur = cur[idx]
    return cur


def render_template(tmpl, ctx):
    """Fill "{queue} queued, {title}" from a dict, leaving unknowns blank.

    Deliberately not str.format: a shape is third-party data and must never
    be able to raise (or reach dunder attributes) from a missing key.
    """
    def sub(m):
        key = m.group(1)
        fmt = None
        if ":" in key:
            key, fmt = key.split(":", 1)
        v = ctx.get(key)
        if v is None:
            return ""
        if fmt:
            try:
                return format(v, fmt)
            except (ValueError, TypeError):
                return str(v)
        return str(v)
    return re.sub(r"\{([^}]+)\}", sub, tmpl)


# Built-in shapes. Keys are the app name; "match" lists image-name substrings
# used by --discover. Ports are each project's UPSTREAM default.
BUILTIN_SHAPES = {
    "ollama": {
        "kind": "ollama", "port": 11434, "match": ["ollama"],
    },
    "plex": {
        "kind": "plex", "port": 32400, "key": "plex",
        "match": ["plexinc/pms", "plex-media-server", "linuxserver/plex"],
        "key_file": ["plex/config/Library/Application Support/"
                     "Plex Media Server/Preferences.xml",
                     "PlexOnlineToken=\"([^\"]+)\""],
    },
    "sonarr": {
        "kind": "arr", "port": 8989, "key": "sonarr", "match": ["sonarr"],
        "key_file": ["sonarr/config/config.xml",
                     "<ApiKey>([a-f0-9]+)</ApiKey>"],
    },
    "radarr": {
        "kind": "arr", "port": 7878, "key": "radarr", "match": ["radarr"],
        "key_file": ["radarr/config/config.xml",
                     "<ApiKey>([a-f0-9]+)</ApiKey>"],
    },
    "lidarr": {
        "kind": "arr", "port": 8686, "key": "lidarr", "match": ["lidarr"],
        "key_file": ["lidarr/config/config.xml",
                     "<ApiKey>([a-f0-9]+)</ApiKey>"],
    },
    "prowlarr": {
        "kind": "prowlarr", "port": 9696, "key": "prowlarr",
        "match": ["prowlarr"],
        "key_file": ["prowlarr/config/config.xml",
                     "<ApiKey>([a-f0-9]+)</ApiKey>"],
    },
    "bazarr": {
        "kind": "bazarr", "port": 6767, "key": "bazarr", "match": ["bazarr"],
        "key_file": ["bazarr/config/config/config.yaml",
                     "(?m)^\\s*apikey:\\s*(\\S+)"],
    },
    "sabnzbd": {
        "kind": "sabnzbd", "port": 8080, "key": "sabnzbd",
        "match": ["sabnzbd"],
        "key_file": ["sabnzbd/config/sabnzbd.ini",
                     "(?m)^api_key\\s*=\\s*(\\S+)"],
    },
    "overseerr": {
        "kind": "http_json", "port": 5055, "path": "/api/v1/status",
        "match": ["overseerr", "jellyseerr"],
        "template": "v{version}{upd}",
        "fields": {"version": "version", "upd": "updateAvailable"},
        "map": {"upd": {"True": "  update available", "False": ""}},
    },
    "qbittorrent": {
        "kind": "http_json", "port": 8080,
        "path": "/api/v2/transfer/info", "match": ["qbittorrent"],
        "template": "{state}  down {dl} up {ul}",
        "fields": {"state": "connection_status",
                   "dl": "dl_info_speed", "ul": "up_info_speed"},
        "humanise": ["dl", "ul"], "suffix": {"dl": "/s", "ul": "/s"},
    },
    "transmission": {
        "kind": "http_json", "port": 9091,
        "path": "/transmission/rpc", "match": ["transmission"],
        "template": "{n} torrents",
        "fields": {"n": "arguments.torrents[].id"},
    },
    "jellyfin": {
        "kind": "http_json", "port": 8096, "key": "jellyfin",
        "path": "/Sessions", "match": ["jellyfin"],
        "template": "{n} session(s)", "fields": {"n": "[].Id"},
    },
    "open-webui": {
        "kind": "http_json", "port": 8080, "path": "/health",
        "match": ["open-webui"], "template": "{status}",
        "fields": {"status": "status"},
        "map": {"status": {"True": "healthy", "False": "unhealthy"}},
    },
    "searxng": {
        "kind": "http", "port": 8888, "path": "/", "match": ["searxng"],
        "label": "search up",
    },
    "postgres": {
        "kind": "log_tail", "match": ["postgres", "postgis"],
        "pattern": "(database system is ready|checkpoint|autovacuum|FATAL.*)",
    },
    "redis": {
        "kind": "command", "match": ["redis"],
        "argv": ["redis-cli", "-h", "{host}", "-p", "{port}", "info",
                 "clients"],
        "port": 6379, "pattern": "connected_clients:(\\d+)",
        "template": "{1} clients",
    },
    "unpackerr": {
        "kind": "log_tail", "match": ["unpackerr"],
        "pattern": "(Extracted|Extracting|Queue|error)",
    },
    "recyclarr": {
        "kind": "log_tail", "match": ["recyclarr"],
    },
    "honcho-api": {
        "kind": "honcho", "port": 8000, "host": "lan",
        "match": ["plastic-labs/honcho"],
    },
    "system (host)": {"kind": "host"},
}


def load_shapes(cfg):
    """BUILTIN_SHAPES overlaid with every *.json in the shapes directories.

    A file shape fully replaces a built-in of the same name, so overriding
    one app does not mean vendoring the whole table. Contributors drop a file
    in shapes/ and never touch this module.
    """
    shapes = json.loads(json.dumps(BUILTIN_SHAPES))
    errors = []
    for d in cfg.get("shape_dirs") or []:
        d = os.path.expanduser(d)
        if not os.path.isdir(d):
            continue
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for fn in names:
            if not fn.endswith(".json"):
                continue
            path = os.path.join(d, fn)
            try:
                with open(path) as f:
                    doc = json.load(f)
            except (OSError, ValueError) as e:
                errors.append("%s: %s" % (fn, str(e)[:48]))
                continue
            # a file may hold one shape (named by filename) or a dict of them
            if "kind" in doc:
                shapes[doc.get("name") or fn[:-5]] = doc
            elif isinstance(doc, dict):
                shapes.update(doc)
            else:
                errors.append("%s: expected an object" % fn)
    return shapes, errors


def running_containers():
    """[(name, image, [ports])] for running containers, or [] if no docker."""
    for cmd in (["docker", "ps", "--format",
                 "{{.Names}}\t{{.Image}}\t{{.Ports}}"],
                ["sudo", "-n", "docker", "ps", "--format",
                 "{{.Names}}\t{{.Image}}\t{{.Ports}}"]):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=6)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
        if r.returncode != 0:
            continue
        out = []
        for line in r.stdout.splitlines():
            f = line.split("\t")
            if len(f) < 2:
                continue
            ports = sorted({int(m) for m in
                            re.findall(r":(\d+)->", f[2] if len(f) > 2 else "")})
            out.append((f[0], f[1], ports))
        return out
    return []


def discover(cfg, write=None):
    """Probe what is actually running and emit a config for it.

    Deliberately read-only unless you pass --discover=FILE: it prints a
    report plus the JSON, so you can see WHY each app matched and what it
    could not reach before trusting it. The published defaults list upstream
    ports, so on an orchestrator that assigns its own this is how you get a
    working config without editing a table by hand.
    """
    shapes, errs = load_shapes(cfg)
    for e in errs:
        print("shape error: %s" % e)
    containers = running_containers()
    if not containers:
        print("no running containers found (is docker reachable? try sudo)")
    det = AppDetails(config=cfg)
    probes, unmatched = [], []
    print("%-24s %-28s %s" % ("ROW", "MATCHED SHAPE", "RESULT"))
    print("-" * 78)
    for name, image, ports in sorted(containers):
        row = prettify(name)
        img = image.lower()
        hit = None
        # exact row-name match wins, then image-substring match
        if row in shapes:
            hit = row
        else:
            for sname, sh in shapes.items():
                if any(tok in img for tok in (sh.get("match") or [])):
                    hit = sname
                    break
        if not hit:
            unmatched.append((row, image, ports))
            print("%-24s %-28s %s" % (row[:24], "-", "docker health only"))
            continue
        sh = dict(shapes[hit])
        sh.pop("match", None)
        # port: believe the published mapping over the shape's default
        tried = []
        if sh.get("kind") in ("http_json", "http") or sh.get("port"):
            # Only probe ports THIS container publishes. Falling back to the
            # shape's default port when the container does not publish it
            # means probing whatever else happens to own that port: sabnzbd
            # and searxng both default to 8080, so a guess would happily
            # report searxng's answer as sabnzbd's state. Prefer the shape's
            # port when the container does publish it, else try its ports,
            # and only use the bare default if it publishes nothing at all.
            cands = ([p for p in ports if p == sh.get("port")] + ports)
            if not cands and sh.get("port"):
                cands = [sh["port"]]
            seen = set()
            cands = [c for c in cands if not (c in seen or seen.add(c))]
            for cand in cands[:6]:
                sh["port"] = cand
                tried.append(cand)
                try:
                    v = getattr(det, det.KINDS[sh["kind"]])(hit, sh)
                except Exception:  # noqa: BLE001
                    v = None
                if v:
                    note = "OK :%d  -> %s" % (cand, str(v)[:30])
                    break
            else:
                sh["port"] = sh.get("port") or (ports[0] if ports else None)
                need = "key missing" if sh.get("key") and not det._key(
                    hit, sh) else "no answer"
                note = "%s (tried %s)" % (
                    need, ",".join(str(t) for t in tried) or "-")
        else:
            try:
                v = getattr(det, det.KINDS[sh["kind"]])(hit, sh)
            except Exception:  # noqa: BLE001
                v = None
            note = ("OK  -> %s" % str(v)[:36]) if v else "no answer"
        entry = {"row": row, "kind": sh.get("kind")}
        for k in ("port", "path", "key", "template", "fields", "map",
                  "pattern", "argv", "label", "key_file", "host", "lines",
                  "humanise", "suffix", "count", "key_header"):
            if k in sh:
                entry[k] = sh[k]
        probes.append(entry)
        print("%-24s %-28s %s" % (row[:24], hit[:28], note))

    out = {"detail": probes}
    if cfg.get("app_config_root"):
        out["app_config_root"] = cfg["app_config_root"]
    else:
        guesses = [d for d in ("/mnt/.ix-apps/app_mounts", "/srv/appdata",
                               "/opt/appdata", "/var/lib/docker/volumes")
                   if os.path.isdir(d)]
        if guesses:
            out["_hint_app_config_root"] = guesses
            print("\nAPI keys: set app_config_root to find them. Candidates "
                  "on this host: %s" % ", ".join(guesses))
    if unmatched:
        print("\n%d container(s) have no shape. They still show their docker "
              "HEALTHCHECK verdict; to give them a DETAIL line, add a shape:"
              % len(unmatched))
        for row, image, ports in unmatched[:12]:
            print("    %-22s %s%s" % (row, image.split("@")[0][:40],
                                      "  ports %s" % ports if ports else ""))
        print("    see shapes/README.md for the format")
    blob = json.dumps(out, indent=2, sort_keys=True)
    # Config keys replace their built-in counterpart rather than merging, so
    # an empty probe list is a valid config meaning "no DETAIL column".
    # Never emit one from a failed scan.
    if not out.get("detail"):
        print("\nnothing discovered, so there is no config worth writing.")
        print("a config with an empty probe list would REPLACE the built-in")
        print("one and leave the DETAIL column blank.")
        if not containers:
            print("docker returned no containers: re-run with sudo, since")
            print("listing containers needs access to the docker socket.")
        if write:
            print("refusing to write %s" % write)
            return 1
        return 1
    if write:
        d = os.path.dirname(os.path.expanduser(write))
        if d:
            os.makedirs(d, exist_ok=True)
        with open(os.path.expanduser(write), "w") as f:
            f.write(blob + "\n")
        print("\nwrote %s" % write)
    else:
        print("\n--- config (save to ~/.config/jtop/config.json) ---")
        print(blob)
    return 0


def list_shapes(cfg):
    shapes, errs = load_shapes(cfg)
    for e in errs:
        print("shape error: %s" % e)
    print("%-22s %-14s %s" % ("SHAPE", "KIND", "PORT / NOTE"))
    for name in sorted(shapes):
        sh = shapes[name]
        note = str(sh.get("port") or "")
        if sh.get("key"):
            note += "  needs api key"
        if sh.get("kind") == "log_tail":
            note += "  log text, not an API"
        print("%-22s %-14s %s" % (name[:22], sh.get("kind", "?"), note))
    print("\n%d shapes.  Built-in: %d.  Add your own as JSON in: %s"
          % (len(shapes), len(BUILTIN_SHAPES),
             ", ".join(cfg.get("shape_dirs") or [])))
    return 0


class AppDetails(threading.Thread):
    """Runs the configured shapes and publishes the DETAIL strings.

    Which shapes run is decided by CONFIG["detail"]:
      "auto"  - every container in the tree whose name matches a shape
      [list]  - only these rows, in this order
    Everything is best-effort: each probe is short-timeout, wrapped, and a
    failure leaves the cell blank. Secrets read from app config are used only
    to talk to the configured host and are never rendered or logged.
    """

    def __init__(self, config=None, interval=None, host=None):
        super().__init__(daemon=True)
        self.cfg = config or CONFIG
        self.interval = interval or self.cfg.get("detail_interval", 5.0)
        self.host = host or self.cfg.get("probe_host", "127.0.0.1")
        self.timeout = self.cfg.get("probe_timeout", 2.5)
        self.shapes, self.shape_errors = load_shapes(self.cfg)
        self.lock = threading.Lock()
        self.details = {}
        self.health = {}          # row -> docker healthcheck state
        self.ports = {}           # row -> [published ports]
        self._keys = {}
        self._ws = None
        self._lan = None
        self._rows = []           # container names the UI is showing
        self._cids = {}           # row name -> container id, for log_tail
        self._stop = threading.Event()

    # ---------- wiring ----------
    def set_rows(self, names, cids=None):
        """The UI tells us which containers exist, so "auto" can match.

        cids maps row name -> container id; log_tail needs it to call
        `docker logs`.
        """
        with self.lock:
            self._rows = list(names)
            if cids:
                self._cids = dict(cids)

    def stop(self):
        self._stop.set()

    def lan_ip(self):
        if self._lan is None:
            self._lan = ""
            try:
                import socket
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    s.connect(("192.0.2.1", 9))      # never sends a packet
                    self._lan = s.getsockname()[0]
                finally:
                    s.close()
            except OSError:
                pass
        return self._lan

    def _host_for(self, sh):
        want = sh.get("host")
        if want == "lan":
            return self.lan_ip() or self.host
        return want or self.host

    def _base(self, sh):
        return "http://%s:%s" % (self._host_for(sh), self._port(sh))

    def _port(self, sh):
        """The port to probe for this shape.

        Resolution order: the shape's port if the container publishes it, else
        its sole published port, else the shape's port as a default. Upstream
        defaults collide across projects (sabnzbd and searxng both use 8080),
        so a published port is always preferred. pinned_port skips all of this.
        """
        want = sh.get("port")
        if sh.get("_pinned"):
            return want or 80
        with self.lock:
            pub = list(self.ports.get(sh.get("_row") or "", []))
        if want and want in pub:
            return want
        if len(pub) == 1:
            return pub[0]
        return want or 80

    def _get(self, url, headers=None, timeout=None):
        """GET and return the response body, or None if unreachable.

        Returns None rather than raising: an absent app is the normal case,
        and kinds are expected to be callable in isolation.
        """
        req = urllib.request.Request(url, headers=headers or {})
        try:
            with urllib.request.urlopen(req,
                                        timeout=timeout or self.timeout) as r:
                return r.read()
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def _json(self, url, headers=None, timeout=None):
        raw = self._get(url, headers, timeout)
        if raw is None:
            return None
        try:
            return json.loads(raw.decode("utf8", "replace"))
        except ValueError:
            return None

    def _key(self, name, sh):
        """API key for a shape, from its own key_file spec. Cached.

        Returns None when app_config_root is unset (the default) or the file
        is unreadable, in which case key-requiring shapes are skipped.
        """
        if name in self._keys:
            return self._keys[name]
        val = None
        spec = sh.get("key_file")
        root = self.cfg.get("app_config_root") or ""
        if spec and root:
            try:
                m = re.search(spec[1], read(os.path.join(root, spec[0])))
                if m:
                    val = m.group(1).strip("'\"")
            except (re.error, IndexError, TypeError):
                val = None
        self._keys[name] = val
        return val

    def _auth_headers(self, name, sh):
        key = self._key(name, sh)
        if not key:
            return None, False
        hdr = sh.get("key_header", "X-Api-Key")
        return {hdr: key}, True

    # ---------- kinds ----------
    def k_http_json(self, name, sh):
        """Generic JSON endpoint -> template. Covers most modern apps."""
        headers = None
        if sh.get("key"):
            headers, ok = self._auth_headers(name, sh)
            if not ok:
                return None
        d = self._json(self._base(sh) + sh.get("path", "/"), headers)
        if d is None:
            return None
        ctx = {}
        for label, path in (sh.get("fields") or {}).items():
            v = dig(d, path)
            # a bare "list length" request: fields ending [] with no key
            if v is None and str(path).endswith("[].id"):
                v = None
            ctx[label] = v
        for label in sh.get("count") or []:
            v = dig(d, (sh.get("fields") or {}).get(label, label))
            ctx[label] = len(v) if isinstance(v, (list, tuple)) else v
        for label in sh.get("humanise") or []:
            if isinstance(ctx.get(label), (int, float)):
                ctx[label] = human(ctx[label])
        for label, table in (sh.get("map") or {}).items():
            if label in ctx:
                ctx[label] = table.get(str(ctx[label]), ctx[label])
        for label, suf in (sh.get("suffix") or {}).items():
            if ctx.get(label) not in (None, ""):
                ctx[label] = "%s%s" % (ctx[label], suf)
        tmpl = sh.get("template")
        if not tmpl:
            vals = [str(v) for v in ctx.values() if v not in (None, "")]
            return "  ".join(vals) or None
        return render_template(tmpl, ctx).strip() or None

    def k_http(self, name, sh):
        """Liveness only, by status code."""
        url = self._base(sh) + sh.get("path", "/")
        code = None
        try:
            with urllib.request.urlopen(urllib.request.Request(url),
                                        timeout=self.timeout) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code          # a 4xx still proves it is listening
        except (urllib.error.URLError, OSError, ValueError):
            return None
        return "%s (HTTP %s)" % (sh.get("label", "up"), code) if code else None

    def k_docker_health(self, name, sh):
        """The container's own HEALTHCHECK verdict.

        The cheapest useful signal there is: no port, no key, no API
        knowledge. Populated in bulk by refresh_health().
        """
        with self.lock:
            return self.health.get(name)

    def k_log_tail(self, name, sh):
        """Last interesting line from the container's log.

        For apps with no API at all (unpackerr, recyclarr, bare postgres).
        Log text is NOT a stable interface, so this is a fallback: shapes
        using it should expect to be rewritten, and the line is shown with
        its age so a stale message cannot masquerade as current state.
        """
        cid = (sh.get("_cid") or "")[:64]
        if not cid:
            return None
        n = int(sh.get("lines", 40))
        try:
            out = subprocess.run(
                ["docker", "logs", "--tail", str(n), "--timestamps", cid],
                capture_output=True, text=True, timeout=self.timeout)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None
        pat = sh.get("pattern")
        best = None
        for line in (out.stdout + out.stderr).splitlines():
            if pat:
                try:
                    if not re.search(pat, line):
                        continue
                except re.error:
                    return None
            best = line
        if not best:
            return None
        age = ""
        # docker --timestamps emits RFC3339 with NANOSECONDS and a zone:
        # 2026-10-17T22:23:53.050058556Z. Match the whole thing, not just to
        # the second, or the fractional part leaks into the displayed text.
        m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)"
                     r"(\.\d+)?(Z|[+-]\d\d:?\d\d)?\s*", best)
        if m:
            try:
                t = calendar.timegm(
                    time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S"))
                off = m.group(3)
                if off and off not in ("Z", "+00:00", "-00:00"):
                    sign = 1 if off[0] == "+" else -1
                    hh, _, mm = off[1:].partition(":")
                    t -= sign * (int(hh) * 3600 + int(mm or 0) * 60)
                secs = max(0, time.time() - t)
                if secs >= 86400:
                    age = "%dd ago " % (secs // 86400)
                elif secs >= 3600:
                    age = "%dh%02dm ago " % (secs // 3600, (secs % 3600) // 60)
                elif secs >= 60:
                    age = "%dm ago " % (secs // 60)
                else:
                    age = "%ds ago " % secs
                best = best[m.end():]
            except (ValueError, OverflowError):
                pass
        # Many apps print their own timestamp as well as docker's, often
        # after a level tag ("[INFO] 2026/10/17 22:30:54 ..."). We already
        # show the age, so drop any date-time in the first 40 chars.
        best = re.sub(r"\[?\d{4}[-/]\d\d[-/]\d\d[T ]\d\d:\d\d:\d\d"
                      r"(\.\d+)?(Z|[+-]\d\d:?\d\d)?\]?\s*",
                      "", best[:40], count=1) + best[40:]
        # logfmt apps lead with time="..."; strip the key too, including the
        # empty quotes left behind once the timestamp itself has gone
        best = re.sub(r'^time=("[^"]*"|\S*)\s*', "", best)
        best = re.sub(r"\s+", " ", best).strip()
        return ("%s%s" % (age, best))[:160] or None

    def k_command(self, name, sh):
        """Run a command and pull a value out of its output.

        For apps that speak a protocol rather than HTTP (redis-cli, zpool).
        argv is substituted with {host}/{port} only - no shell, and the list
        form means a shape cannot inject arguments through quoting.
        """
        argv = sh.get("argv")
        if not isinstance(argv, list) or not argv:
            return None
        subst = {"host": self._host_for(sh), "port": str(sh.get("port") or "")}
        argv = [render_template(a, subst) for a in argv]
        try:
            out = subprocess.run(argv, capture_output=True, text=True,
                                 timeout=self.timeout)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None
        text = out.stdout or out.stderr
        pat = sh.get("pattern")
        if not pat:
            return text.strip().splitlines()[0][:120] if text.strip() else None
        try:
            m = re.search(pat, text)
        except re.error:
            return None
        if not m:
            return None
        ctx = {str(i + 1): g for i, g in enumerate(m.groups())}
        ctx["0"] = m.group(0)
        tmpl = sh.get("template")
        return (render_template(tmpl, ctx).strip() if tmpl
                else (m.group(1) if m.groups() else m.group(0)))

    # --- kinds that are genuinely app-specific logic, not just a shape ---
    def k_ollama(self, name, sh):
        d = self._json(self._base(sh) + "/api/ps")
        if not isinstance(d, dict):
            return None
        models = d.get("models") or []
        if not models:
            return "no model resident"
        out = []
        for m in sorted(models, key=lambda x: -(x.get("size_vram") or 0)):
            det = m.get("details") or {}
            nm = (m.get("name") or "?").replace(":latest", "")
            bits = [nm]
            if det.get("parameter_size"):
                bits.append(det["parameter_size"])
            if det.get("quantization_level"):
                bits.append(det["quantization_level"])
            ctx = m.get("context_length")
            if ctx:
                bits.append("%dk ctx" % (ctx // 1000) if ctx >= 1000
                            else "%d ctx" % ctx)
            ttl = ""
            exp = m.get("expires_at") or ""
            if exp:
                try:
                    # expires_at is UTC ("...Z"); timegm, NOT mktime, which
                    # would apply the local offset and break under BST
                    t = calendar.timegm(
                        time.strptime(exp[:19], "%Y-%m-%dT%H:%M:%S"))
                    left = t - time.time()
                    # a pinned model (keep_alive -1) reports an expiry years
                    # out; printing "ttl 15372286m" is noise, say so instead
                    if left > 86400:
                        ttl = " pinned"
                    elif left > 0:
                        ttl = (" ttl %dm" % (left // 60) if left >= 60
                               else " ttl %ds" % left)
                except (ValueError, OverflowError):
                    pass
            out.append(" ".join(bits) + ttl)
        return " | ".join(out)

    def k_plex(self, name, sh):
        tok = self._key(name, sh)
        if not tok:
            return None
        raw = self._get(self._base(sh) + "/status/sessions",
                        {"X-Plex-Token": tok})
        if not raw:
            return None
        root = ET.fromstring(raw)
        n = int(root.get("size") or 0)
        if not n:
            return "idle, no sessions"
        parts = []
        for v in list(root)[:2]:
            title = (v.get("grandparentTitle") or v.get("title") or "?")[:24]
            pl = v.find("Player")
            ts = v.find("TranscodeSession")
            state = pl.get("state") if pl is not None else ""
            seg = "%s%s" % (title, " [%s]" % state if state else "")
            if ts is not None:
                how = "transcode" if ts.get("videoDecision") == "transcode" \
                      else "direct"
                hw = []
                if ts.get("transcodeHwDecoding"):
                    hw.append("dec:" + ts.get("transcodeHwDecoding"))
                if ts.get("transcodeHwEncoding"):
                    hw.append("enc:" + ts.get("transcodeHwEncoding"))
                seg += " %s%s" % (how, " " + "/".join(hw) if hw else "")
                if ts.get("throttled") == "1":
                    seg += " throttled"
            parts.append(seg)
        extra = " +%d more" % (n - len(parts)) if n > len(parts) else ""
        return "%d session%s: %s%s" % (n, "" if n == 1 else "s",
                                       "; ".join(parts), extra)

    def k_arr(self, name, sh):
        headers, ok = self._auth_headers(name, sh)
        if not ok:
            return None
        base = self._base(sh)
        q = self._json(base + "/api/v3/queue?pageSize=1", headers)
        if not isinstance(q, dict):
            return None
        total = q.get("totalRecords") or 0
        try:
            health = len(self._json(base + "/api/v3/health", headers))
        except Exception:  # noqa: BLE001
            health = None
        s = "queue %d" % total
        recs = q.get("records") or []
        if recs:
            r = recs[0]
            t = (r.get("title") or "")[:28]
            if t:
                s += ": %s" % t
            if r.get("status"):
                s += " (%s)" % r["status"]
        if health:
            s += "  %d health" % health
        return s

    def k_sabnzbd(self, name, sh):
        key = self._key(name, sh)
        if not key:
            return None
        d = self._json("%s/api?mode=queue&output=json&apikey=%s"
                       % (self._base(sh), key))
        if not isinstance(d, dict):
            return None
        q = d.get("queue") or {}
        slots = q.get("noofslots", len(q.get("slots") or []))
        if not slots:
            return "idle"
        return "%s  %d job%s  %sB/s  %.1fGB left" % (
            q.get("status", "?"), slots, "" if slots == 1 else "s",
            q.get("speed") or "0", float(q.get("mbleft") or 0) / 1024)

    def k_prowlarr(self, name, sh):
        headers, ok = self._auth_headers(name, sh)
        if not ok:
            return None
        base = self._base(sh) + "/api/v1"
        idx = self._json(base + "/indexer", headers)
        if not isinstance(idx, list):
            return None
        total = len(idx)
        on = sum(1 for i in idx if i.get("enable"))
        s = "%d/%d indexers enabled" % (on, total)
        try:
            bad = self._json(base + "/indexerstatus", headers)
            if bad:
                names = {i.get("id"): i.get("name") for i in idx}
                who = [str(names.get(b.get("indexerId"), b.get("indexerId")))
                       for b in bad[:2]]
                s += "  %d FAILING: %s" % (len(bad), ", ".join(who))
        except Exception:  # noqa: BLE001
            pass
        try:
            n = len(self._json(base + "/health", headers))
            if n:
                s += "  %d health" % n
        except Exception:  # noqa: BLE001
            pass
        return s

    def k_bazarr(self, name, sh):
        key = self._key(name, sh)
        if not key:
            return None
        b = self._json(self._base(sh) + "/api/badges", {"X-API-KEY": key})
        if not isinstance(b, dict):
            return None
        s = "%d ep / %d film wanted" % (b.get("episodes", 0),
                                        b.get("movies", 0))
        if b.get("providers", 0):
            s += "  %d provider issues" % b["providers"]
        links = [n for n, k in (("sonarr", "sonarr_signalr"),
                                ("radarr", "radarr_signalr"))
                 if b.get(k) != "LIVE"]
        s += "  %s DOWN" % "+".join(links) if links else "  links live"
        return s

    def k_honcho(self, name, sh):
        base = self._base(sh)
        if not self._ws:
            req = urllib.request.Request(
                base + "/v3/workspaces/list", data=b"{}",
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                items = json.loads(r.read().decode()).get("items") or []
            if not items:
                return None
            self._ws = items[0]["id"]
        q = self._json("%s/v3/workspaces/%s/queue/status" % (base, self._ws))
        if not isinstance(q, dict):
            return None
        pend = q.get("pending_work_units") or 0
        prog = q.get("in_progress_work_units") or 0
        s = "ws %s: %d/%d done" % (self._ws, q.get("completed_work_units") or 0,
                                   q.get("total_work_units") or 0)
        if prog:
            s += "  %d running" % prog
        if pend:
            s += "  %d PENDING" % pend
        if not pend and not prog:
            s += "  idle"
        return s

    def k_host(self, name, sh):
        """ZFS pool health + ARC hit rate for the host row."""
        bits = []
        try:
            r = subprocess.run(["zpool", "list", "-Ho", "name,health,cap"],
                               capture_output=True, text=True, timeout=5)
            pools, bad = [], []
            for line in r.stdout.splitlines():
                f = line.split()
                if len(f) >= 3:
                    pools.append(f[0])
                    if f[1] != "ONLINE":
                        bad.append("%s %s" % (f[0], f[1]))
            if bad:
                bits.append("POOL " + ", ".join(bad))
            elif pools:
                bits.append("%d pools ONLINE" % len(pools))
        except Exception:  # noqa: BLE001
            pass
        st = {}
        for line in read(ARCSTATS).splitlines():
            f = line.split()
            if len(f) == 3 and f[0] in ("hits", "misses", "size"):
                st[f[0]] = int(f[2])
        tot = st.get("hits", 0) + st.get("misses", 0)
        if tot:
            bits.append("ARC %s %.1f%% hit" % (human(st.get("size", 0)),
                                               100.0 * st["hits"] / tot))
        return "  ".join(bits) or None

    KINDS = {
        # generic, data-driven: prefer these in new shapes
        "http_json": "k_http_json", "http": "k_http",
        "docker_health": "k_docker_health", "log_tail": "k_log_tail",
        "command": "k_command",
        # app-specific logic that a template cannot express
        "ollama": "k_ollama", "plex": "k_plex", "arr": "k_arr",
        "sabnzbd": "k_sabnzbd", "prowlarr": "k_prowlarr",
        "bazarr": "k_bazarr", "honcho": "k_honcho", "host": "k_host",
    }

    # ---------- driver ----------
    def refresh_health(self):
        """One docker call for every container's HEALTHCHECK verdict.

        Also harvests published ports from the same call, so a shape can be
        pinned to the port its own container actually exposes rather than
        the upstream default. This is what lets detail:"auto" work on an
        orchestrator that reassigns ports, with no config at all.
        """
        out = {}
        ports = {}
        for cmd in (["docker", "ps", "--format",
                     "{{.Names}}\t{{.Status}}\t{{.Ports}}"],
                    ["sudo", "-n", "docker", "ps", "--format",
                     "{{.Names}}\t{{.Status}}\t{{.Ports}}"]):
            try:
                r = subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=self.timeout)
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                continue
            if r.returncode != 0:
                continue
            for line in r.stdout.splitlines():
                f = line.split("\t")
                nm = f[0] if f else ""
                status = f[1] if len(f) > 1 else ""
                if not nm:
                    continue
                row = prettify(nm)
                pub = sorted({int(x) for x in
                              re.findall(r":(\d+)->",
                                         f[2] if len(f) > 2 else "")})
                if pub:
                    ports[row] = pub
                m = re.search(r"\((healthy|unhealthy|health: starting)\)",
                              status)
                if m:
                    out[prettify(nm)] = {"healthy": "healthy",
                                         "unhealthy": "UNHEALTHY",
                                         "health: starting": "starting"
                                         }[m.group(1)]
            break
        with self.lock:
            self.health = out
            if ports:
                self.ports = ports

    def planned(self):
        """[(row, shape)] to probe this cycle."""
        want = self.cfg.get("detail", "auto")
        with self.lock:
            rows = list(self._rows)
        out = []
        if want == "auto":
            for row in rows + ["system (host)"]:
                sh = self.shapes.get(row)
                if sh:
                    out.append((row, sh))
        else:
            for row in want or []:
                if isinstance(row, dict):          # inline shape
                    nm = row.get("row") or row.get("name")
                    if nm:
                        out.append((nm, row))
                elif row in self.shapes:
                    out.append((row, self.shapes[row]))
        return out

    def poll(self):
        self.refresh_health()
        out = {}
        fallback = self.cfg.get("health_fallback", True)
        for row, sh in self.planned():
            if self._stop.is_set():
                break
            meth = self.KINDS.get(sh.get("kind"))
            v = None
            if meth:
                # _row lets _port() consult this container's published ports;
                # _pinned marks a port the user set explicitly in config, which
                # must be used verbatim
                sh = dict(sh, _row=row,
                          _pinned=bool(sh.get("pinned_port")))
                if sh.get("kind") == "log_tail":
                    sh["_cid"] = self._cids.get(row, "")
                try:
                    v = getattr(self, meth)(row, sh)
                except Exception:  # noqa: BLE001 - a dead app must not break the UI
                    v = None
            if not v and fallback:
                # nothing from the shape: fall back to the container's own
                # healthcheck, which needs no per-app knowledge at all
                with self.lock:
                    v = self.health.get(row)
            if v:
                out[row] = v
        # containers with no shape at all still get their health verdict
        if fallback:
            with self.lock:
                health = dict(self.health)
                rows = set(self._rows)
            for row, verdict in health.items():
                if row in rows and row not in out:
                    out[row] = verdict
        with self.lock:
            self.details = out

    def run(self):
        while not self._stop.is_set():
            self.poll()
            self._stop.wait(self.interval)

    def snapshot(self):
        with self.lock:
            return dict(self.details)


# --------------------------------------------------------------------------
# demo mode
#
# --demo swaps every sampler for a synthetic one. Reads nothing: no /proc,
# no nvidia-smi, no docker, no network. Hosts, cards, models and messages
# below are invented. Deterministic for a given DEMO_SEED.
# --------------------------------------------------------------------------
DEMO_SEED = 20261017


class _Walk:
    """Deterministic bounded random walk, so demo frames move but replay the
    same way for a given seed."""

    def __init__(self, rnd, lo, hi, start=None, step=None):
        self.rnd, self.lo, self.hi = rnd, lo, hi
        self.v = start if start is not None else rnd.uniform(lo, hi)
        self.step = step if step is not None else (hi - lo) / 8.0

    def next(self):
        self.v += self.rnd.uniform(-self.step, self.step)
        self.v = max(self.lo, min(self.hi, self.v))
        return self.v


class DemoWorld:
    """One synthetic host, shared by all the demo samplers."""

    GB = 1024 ** 3
    MB = 1024 ** 2

    APPS = [
        # name, mem, nproc, vram, detail
        ("ollama", 13.5 * GB, 3, 9.1 * GB,
         "mistral-small 24B Q4_K_M 32k ctx ttl 11m | all-minilm 23M pinned"),
        ("plex", 420 * MB, 7, 1.2 * GB,
         "2 sessions: Blue Planet II [playing] transcode enc:nvenc; "
         "Arrival [paused] direct"),
        ("sonarr", 206 * MB, 2, 0, "queue 3: Some.Show.S02E04 (downloading)"),
        ("radarr", 165 * MB, 2, 0, "queue 0"),
        ("sabnzbd", 98 * MB, 3, 0, "Downloading  2 jobs  41.2MB/s  7.4GB left"),
        ("prowlarr", 147 * MB, 2, 0, "11/12 indexers enabled  1 FAILING: nzbgeek"),
        ("bazarr", 193 * MB, 2, 0, "4 ep / 1 film wanted  links live"),
        ("open-webui", 639 * MB, 1, 0, "healthy"),
        ("searxng", 129 * MB, 3, 0, "search up (HTTP 200)"),
        ("postgres", 512 * MB, 9, 0, None),
        ("redis", 15 * MB, 2, 0, None),
        ("overseerr", 192 * MB, 3, 0, "v1.33.2  update available"),
    ]
    KIDS = {
        "ollama": ["ollama", "llama-server", "llama-server"],
        "plex": ["Plex Media Serv", "Plex Transcoder", "plugin-host",
                 "Plex Tuner Serv", "EAE", "Plex Script Hos", "sh"],
        "postgres": ["postgres"] * 9,
        "redis": ["redis-server", "sh"],
    }
    CONSOLE = [
        (6, "nvidia 0000:01:00.0: GPU at 42C, fan ramping to 48%"),
        (6, "zfs: ARC target reduced to 24G under memory pressure"),
        (4, "r8169 0000:26:00.0 eth0: link up, 2500Mbps full-duplex"),
        (6, "docker0: port 7(veth9f2a1c) entered forwarding state"),
        (3, "EXT4-fs (sdb1): mounted filesystem read-only, errors=remount-ro"),
        (6, "ZFS: scrub of pool 'tank' completed, 0 errors"),
    ]

    def __init__(self):
        import random
        self.rnd = random.Random(DEMO_SEED)
        self.ncpu = 16
        self.cores = [_Walk(self.rnd, 0, 100,
                            start=self.rnd.choice([1, 2, 4, 18, 35]))
                      for _ in range(self.ncpu)]
        self.total_ram = 64 * self.GB
        self.arc = _Walk(self.rnd, 20 * self.GB, 28 * self.GB)
        self.gpu_util = [_Walk(self.rnd, 0, 98, start=61),
                         _Walk(self.rnd, 0, 40, start=4)]
        self.gpu_temp = [_Walk(self.rnd, 48, 74, start=62),
                         _Walk(self.rnd, 36, 58, start=43)]
        self.gpu_pwr = [_Walk(self.rnd, 70, 235, start=180),
                        _Walk(self.rnd, 20, 120, start=38)]
        self.t0 = time.time()
        # containers get stable fake ids and pids
        self.cids = {n: ("%012x" % (0xC0FFEE000 + i * 0x1111))
                      for i, (n, _, _, _, _) in enumerate(self.APPS)}
        self.pids = {n: 2000 + i * 37
                     for i, (n, _, _, _, _) in enumerate(self.APPS)}
        self.cpu = {n: _Walk(self.rnd, 0, 60,
                             start=self.rnd.uniform(0.1, 12))
                    for n, _, _, _, _ in self.APPS}


class DemoCpu:
    def __init__(self, world):
        self.w = world
        self.ncpu = world.ncpu
        self.total = 0.0
        self.cores = []

    def sample(self):
        self.cores = [c.next() for c in self.w.cores]
        self.total = sum(self.cores) / len(self.cores)
        return self.total, self.cores


class DemoMem:
    def __init__(self, world):
        self.w = world

    def sample(self):
        w = self.w
        arc = w.arc.next()
        apps = sum(a[1] for a in w.APPS) + 4 * w.GB
        cached = 9 * w.GB
        free = w.total_ram - arc - apps - cached
        return {"total": w.total_ram, "free": max(0, free), "cached": cached,
                "arc": arc, "apps": apps, "used": arc + apps,
                "swap_total": 8 * w.GB, "swap_used": 0}


class DemoProc:
    def __init__(self, world):
        self.w = world

    def sample(self):
        procs = {}
        w = self.w
        for name, mem, nproc, vram, _ in w.APPS:
            base = w.pids[name]
            kids = w.KIDS.get(name, [name])
            for j in range(nproc):
                comm = kids[j] if j < len(kids) else kids[-1]
                pid = base + j
                procs[pid] = {
                    "pid": pid, "comm": comm,
                    "cpu": w.cpu[name].v / max(1, nproc),
                    "rss": mem / max(1, nproc), "cid": w.cids[name],
                    "threads": 4, "state": "S",
                    "time": (time.time() - w.t0) * 0.3 + pid % 97,
                }
        for j, comm in enumerate(("systemd", "sshd", "zfs-arc", "kthreadd",
                                  "containerd", "dockerd")):
            procs[100 + j] = {"pid": 100 + j, "comm": comm, "cpu": 0.2,
                              "rss": 40 * self.w.MB, "cid": None,
                              "threads": 2, "state": "S", "time": 61.0 + j}
        return procs


class DemoCgroup:
    def __init__(self, world):
        self.w = world

    def sample(self):
        out = {}
        for name, mem, _, _, _ in self.w.APPS:
            cid = self.w.cids[name]
            cpu = self.w.cpu[name].next()
            out[cid] = {"cid": cid, "cpu": cpu, "mem": mem,
                        "rd": self.w.rnd.choice([0, 0, 0, 2e6, 4.5e7]),
                        "wr": self.w.rnd.choice([0, 0, 1e6, 9e6]),
                        "cputime": 3600.0}
        return out


class DemoGpu:
    """Two invented cards, so the multi-GPU layout is exercised."""

    CARDS = [("NVIDIA GeForce RTX 4070", 12 * DemoWorld.GB, 240),
             ("NVIDIA GeForce GTX 1080 Ti", 11 * DemoWorld.GB, 250)]

    def __init__(self, world):
        self.w = world

    def start(self):
        pass

    def stop(self):
        pass

    def snapshot(self):
        gpus = []
        for i, (name, vram, cap) in enumerate(self.CARDS):
            util = self.w.gpu_util[i].next()
            used = (9.1 if i == 0 else 1.2) * self.w.GB
            gpus.append({
                "util": int(util), "memutil": int(util * 0.7),
                "mem_used": used, "mem_total": vram,
                "temp": int(self.w.gpu_temp[i].next()),
                "power": self.w.gpu_pwr[i].next(), "power_cap": float(cap),
                "sm_clk": 1860 if i == 0 else 1480,
                "mem_clk": 10501 if i == 0 else 5505,
                "fan": 48 if i == 0 else 0,
                "pstate": "P2" if util > 10 else "P8", "name": name,
            })
        procs = {}
        for name, _, nproc, vram, _ in self.w.APPS:
            if vram:
                procs[self.w.pids[name]] = vram
        return gpus, procs, None


class DemoNames:
    def __init__(self, world):
        self.w = world

    def start(self):
        pass

    def stop(self):
        pass

    def set_pids(self, pids):
        pass

    def snapshot(self):
        return {cid: name for name, cid in self.w.cids.items()}, None


class DemoDetails:
    def __init__(self, world):
        self.w = world

    def start(self):
        pass

    def stop(self):
        pass

    def snapshot(self):
        d = {n: det for n, _, _, _, det in self.w.APPS if det}
        d["system (host)"] = "2 pools ONLINE  ARC %s 98.6%% hit" % human(
            self.w.arc.v)
        return d


class DemoConsole:
    """Replays invented kernel messages on a timer, which also exercises the
    new-message-forces-a-repaint path."""

    def __init__(self, world):
        self.w = world
        self.lines = []
        self.seq = 0
        self.error = None
        self._i = 0
        self._next = time.time() + 2.0

    def start(self):
        pass

    def stop(self):
        pass

    def _add(self, ts, sev, msg):
        self.lines.append((ts, sev, msg))
        self.seq += 1

    def snapshot(self, n):
        if time.time() >= self._next:
            sev, msg = self.w.CONSOLE[self._i % len(self.w.CONSOLE)]
            self._i += 1
            self._next = time.time() + 6.0
            self._add(time.strftime("%H:%M:%S"), sev, msg)
        return self.lines[-n:] if n > 0 else [], self.seq



class ConsoleTail(threading.Thread):
    """Kernel ring buffer tail for the CONSOLE box.

    The physical console is where the kernel prints too, and those lines land
    on top of a curses frame without curses knowing: the result is text that
    never clears until something redraws that exact cell. Rather than fight
    it, show the same messages inside the UI, in a box curses owns.

    `dmesg --follow` replays the existing buffer and then streams; a deque
    keeps only the tail. Tried unprivileged first, then `sudo -n`, because
    kernel.dmesg_restrict=1 is the TrueNAS default.
    """

    LINE_RE = re.compile(
        r"^(?P<fac>\w+)\s*:(?P<lvl>\w+)\s*:\s*"
        r"(?:\[\s*(?P<up>[\d.]+)\]|(?P<iso>\S+))\s*(?P<msg>.*)$")
    # syslog severity -> (short tag, colour slot)  0=emerg .. 7=debug
    SEVERITY = {"emerg": 0, "alert": 1, "crit": 2, "err": 3,
                "warn": 4, "notice": 5, "info": 6, "debug": 7}

    def __init__(self, maxlines=400):
        super().__init__(daemon=True)
        self.lock = threading.Lock()
        self.lines = []            # [(ts, severity, text)]
        self.maxlines = maxlines
        self.seq = 0               # bumps on every new line (unread marker)
        self.error = None
        self._stop = threading.Event()
        self._proc = None

    def stop(self):
        self._stop.set()
        p = self._proc
        if p and p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass

    def _spawn(self):
        base = ["dmesg", "--follow", "--decode", "--time-format=iso"]
        for cmd in (base, ["sudo", "-n"] + base):
            try:
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True,
                                     bufsize=1)
            except (FileNotFoundError, OSError) as e:
                self.error = str(e)[:50]
                continue
            # a permission failure exits immediately; give it a moment
            time.sleep(0.4)
            if p.poll() is None:
                return p
            try:
                p.wait(timeout=1)
            except Exception:  # noqa: BLE001
                pass
        return None

    def _add(self, ts, sev, msg):
        with self.lock:
            self.lines.append((ts, sev, msg))
            if len(self.lines) > self.maxlines:
                del self.lines[:len(self.lines) - self.maxlines]
            self.seq += 1

    def _parse(self, raw):
        raw = raw.rstrip("\n")
        if not raw:
            return
        m = self.LINE_RE.match(raw)
        if not m:
            # anything written straight to /dev/kmsg keeps its raw
            # "<prio>,<seq>,<usec>,-;" header; strip it so the message reads
            # as a message rather than as a row of numbers
            km = re.match(r"^(\d+),\d+,\d+,[^;]*;(.*)$", raw)
            if km:
                self._add("", min(7, int(km.group(1)) & 7), km.group(2))
            else:
                self._add("", 6, raw)
            return
        sev = self.SEVERITY.get(m.group("lvl"), 6)
        iso = m.group("iso")
        if iso and "T" in iso:
            ts = iso.split("T", 1)[1][:8]
        elif m.group("up"):
            ts = "+%ds" % float(m.group("up"))
        else:
            ts = ""
        self._add(ts, sev, m.group("msg"))

    def run(self):
        while not self._stop.is_set():
            p = self._spawn()
            if p is None:
                self.error = self.error or "no access to kernel log"
                self._add("", 4, "CONSOLE: %s (needs root or sudo)"
                          % self.error)
                return
            self._proc = p
            self.error = None
            try:
                for raw in p.stdout:
                    if self._stop.is_set():
                        break
                    self._parse(raw)
            except (OSError, ValueError):
                pass
            if self._stop.is_set():
                break
            self._stop.wait(2.0)       # dmesg died; retry

    def snapshot(self, n):
        with self.lock:
            return self.lines[-n:] if n > 0 else [], self.seq


def console_loglevel(value=None):
    """Read, or set, the kernel's console_loglevel (first field of printk).

    Returning it to the UI lets `--hush` stop the kernel scribbling over the
    physical console while jtop owns the screen.
    """
    cur = read("/proc/sys/kernel/printk", "").split()
    if not cur:
        return None
    old = int(cur[0])
    if value is None:
        return old
    fields = list(cur)
    fields[0] = str(value)
    try:
        with open("/proc/sys/kernel/printk", "w") as f:
            f.write(" ".join(fields[:4]) + "\n")
    except (OSError, IOError):
        try:
            subprocess.run(["sudo", "-n", "sysctl", "-q",
                            "kernel.printk=" + "\t".join(fields[:4])],
                           timeout=5, capture_output=True)
        except Exception:  # noqa: BLE001
            return None
    return old


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
(C_GREEN, C_YELLOW, C_RED, C_CYAN, C_BLUE, C_MAGENTA, C_HDR, C_DIM, C_SEL,
 C_KEY) = range(1, 11)

# Filled by init_colors(). Title bars must NEVER add A_BOLD: on an 8-colour
# TERM=linux console bold brightens the foreground, turning black into dark
# grey, which all but disappears against cyan. Use these instead of composing
# attributes at each call site.
HDR = 0   # title / column-header bars
KEY = 0   # emphasised key letters in the footer


def init_colors():
    global HDR, KEY
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    curses.init_pair(C_GREEN, curses.COLOR_GREEN, bg)
    curses.init_pair(C_YELLOW, curses.COLOR_YELLOW, bg)
    curses.init_pair(C_RED, curses.COLOR_RED, bg)
    curses.init_pair(C_CYAN, curses.COLOR_CYAN, bg)
    curses.init_pair(C_BLUE, curses.COLOR_BLUE, bg)
    curses.init_pair(C_MAGENTA, curses.COLOR_MAGENTA, bg)
    curses.init_pair(C_HDR, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(C_DIM, curses.COLOR_WHITE, bg)
    curses.init_pair(C_SEL, curses.COLOR_WHITE, curses.COLOR_BLUE)
    curses.init_pair(C_KEY, curses.COLOR_WHITE, bg)
    # black-on-cyan, deliberately without A_BOLD
    HDR = curses.color_pair(C_HDR)
    # htop-style: key letters sit outside the bar, bright on the normal bg
    KEY = curses.color_pair(C_KEY) | curses.A_BOLD


def load_colour(pct):
    if pct >= 90:
        return curses.color_pair(C_RED) | curses.A_BOLD
    if pct >= 60:
        return curses.color_pair(C_YELLOW)
    return curses.color_pair(C_GREEN)


class App:
    def __init__(self, scr, interval=2.0, demo=False):
        self.scr = scr
        self.interval = interval
        self.demo = demo
        if demo:
            # every sampler is synthetic: demo mode reads nothing from the host
            world = DemoWorld()
            self.cpu = DemoCpu(world)
            self.mem = DemoMem(world)
            self.proc = DemoProc(world)
            self.cg = DemoCgroup(world)
            self.gpu = DemoGpu(world)
            self.dock = DemoNames(world)
            self.appdet = DemoDetails(world)
            self.con = DemoConsole(world)
        else:
            self.cpu = CpuSampler()
            self.mem = MemSampler()
            self.proc = ProcSampler()
            self.cg = CgroupSampler()
            self.gpu = GpuSampler(interval=max(1.0, interval))
            self.dock = DockerNames()
            self.appdet = AppDetails(config=CONFIG,
                                     interval=max(3.0, interval * 2))
            self.con = ConsoleTail()
        self.expanded = set()
        self.sel = 0
        self.scroll = 0
        self.sort = "cpu"
        self.rows = []
        self.show_help = False
        self.filter = ""          # active name filter ("" = show all)
        self.editing = False      # True while typing in the / prompt
        self.ncpu = self.cpu.ncpu if demo else (os.cpu_count() or 1)
        self.show_console = True
        self.console_lines = 6    # body rows in the CONSOLE box
        self.hushed = False       # --hush lowered console_loglevel
        self._saved_loglevel = None
        self._con_seq = -1        # last drawn console seq, for full repaints
        self.force_repaint = True

    # ---------------- drawing primitives ----------------
    def addstr(self, y, x, text, attr=0):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        text = text[:max(0, w - x)]
        try:
            self.scr.addstr(y, x, text, attr)
        except curses.error:
            pass

    def bar(self, y, x, width, segments, label="", total=None):
        """htop-style bar.  segments = [(value, color_pair), ...]"""
        if width < 6:
            return
        inner = width - 2
        self.addstr(y, x, "[", curses.color_pair(C_DIM) | curses.A_BOLD)
        self.addstr(y, x + width - 1, "]", curses.color_pair(C_DIM) | curses.A_BOLD)
        total = total or sum(s[0] for s in segments) or 1
        pos = 0
        for val, cp in segments:
            n = int(round(inner * (val / total)))
            n = min(n, inner - pos)
            if n > 0:
                # cp None/0 is the unused tail: blank, not bars
                if cp:
                    self.addstr(y, x + 1 + pos, "|" * n, curses.color_pair(cp))
                else:
                    self.addstr(y, x + 1 + pos, " " * n)
                pos += n
        if pos < inner:
            self.addstr(y, x + 1 + pos, " " * (inner - pos))
        if label:
            lx = x + width - 1 - len(label)
            self.addstr(y, lx, label, curses.color_pair(C_DIM) | curses.A_BOLD)

    # ---------------- panels ----------------
    def draw_rule(self, y, w, label=""):
        """A thin horizontal rule with an optional label sat in it.

        This is a whole text row, because a cell is the smallest thing a
        console can paint - there is no sub-cell drawing. A real underline is
        not an option either: on the Linux framebuffer console the vt driver
        does not draw A_UNDERLINE as a line, it substitutes vc_ulcolor into
        the foreground (see build_attr() in drivers/tty/vt/vt.c), so it would
        silently recolour the row above instead of ruling it off.

        ACS_HLINE comes from the alternate character set, so it renders as a
        vertically-centred 1px-looking line rather than a solid band.
        """
        width = max(0, w - 1)
        if width < 4:
            return y + 1
        try:
            self.scr.hline(y, 0, curses.ACS_HLINE, width,
                           curses.color_pair(C_DIM))
        except curses.error:
            self.addstr(y, 0, "-" * width, curses.color_pair(C_DIM))
        if label and width > len(label) + 6:
            self.addstr(y, 2, " %s " % label,
                        curses.color_pair(C_CYAN) | curses.A_BOLD)
        return y + 1

    def draw_header(self, y, w):
        if self.demo:
            # demo mode must not read the host at all, header included
            up, load = 342000.0, ["0.42", "0.51", "0.48"]
        else:
            up = float(read("/proc/uptime", "0 0").split()[0])
            load = read("/proc/loadavg", "- - -").split()[:3]
        host = "demo-host" if self.demo else os.uname().nodename
        left = " jtop  %s %s" % (host, "[DEMO] " if self.demo else "")
        right = "load %s %s %s   up %s   %s " % (
            load[0], load[1], load[2], hms(up), time.strftime("%H:%M:%S"))
        # keep one cell spare: writing the final cell can wrap on some terminals
        width = max(0, w - 1)
        if len(left) + len(right) > width:
            right = right.lstrip()
        pad = max(1, width - len(left) - len(right))
        line = (left + " " * pad + right)[:width]
        self.addstr(y, 0, line.ljust(width), HDR)
        return y + 1

    def draw_cpu(self, y, w, total, cores):
        n = len(cores)
        if n == 0:
            return y
        ncols = 4 if w >= 160 else (2 if w >= 80 else 1)
        colw = w // ncols
        barw = max(10, colw - 10)
        rows = (n + ncols - 1) // ncols
        for r in range(rows):
            for c in range(ncols):
                i = c * rows + r
                if i >= n:
                    continue
                x = c * colw
                self.addstr(y + r, x, "%3d" % i,
                            curses.color_pair(C_DIM) | curses.A_BOLD)
                pct = cores[i]
                self.bar(y + r, x + 4, barw,
                         [(pct, C_GREEN if pct < 60 else
                           (C_YELLOW if pct < 90 else C_RED)),
                          (100 - pct, 0)],
                         label="%3.0f%%" % pct, total=100)
        y += rows
        return y

    def draw_cpumem(self, y, w, total, cores, m):
        """CPU total and memory on ONE line: two bars side by side.

        Split in half, labels inside the bars, and the apps/ARC/cache legend
        only when there is room for it (below ~80 columns it is dropped - the
        bar segment colours still carry the split).
        """
        avail = max(20, w - 1)
        half = avail // 2
        self.addstr(y, 0, " CPU ", curses.color_pair(C_CYAN) | curses.A_BOLD)
        cbar = max(8, half - 6)
        self.bar(y, 5, cbar,
                 [(total, C_GREEN if total < 60 else
                   (C_YELLOW if total < 90 else C_RED)), (100 - total, 0)],
                 label="%4.1f%% %dc" % (total, self.ncpu), total=100)

        mx = 5 + cbar + 1
        self.addstr(y, mx, " Mem ", curses.color_pair(C_CYAN) | curses.A_BOLD)
        region = max(0, avail - mx - 5)
        legend = [(" a%s" % human0(m["apps"]), C_GREEN),
                  (" r%s" % human0(m["arc"]), C_MAGENTA),
                  (" c%s" % human0(m["cached"]), C_BLUE),
                  (" f%s" % human0(m["free"]), C_DIM)]
        if m["swap_total"] and m["swap_used"] > 0:
            legend.append((" sw%s" % human0(m["swap_used"]), C_RED))
        lwidth = sum(len(t) for t, _ in legend)
        if region >= lwidth + 18:
            mbar = region - lwidth - 1
        else:
            legend, mbar = [], region
        if mbar >= 8:
            self.bar(y, mx + 5, mbar,
                     [(m["apps"], C_GREEN), (m["arc"], C_MAGENTA),
                      (m["cached"], C_BLUE), (m["free"], 0)],
                     label="%s/%s" % (human0(m["used"]), human0(m["total"])),
                     total=m["total"] or 1)
        x = mx + 5 + mbar + 1
        for txt, cp in legend:
            self.addstr(y, x, txt, curses.color_pair(cp) | curses.A_BOLD)
            x += len(txt)
        return y + 1

    def draw_gpu(self, y, w, gpus, err):
        """One line per GPU: util bar + VRAM bar + temp/power/clocks/fan/P-state.

        Everything is on a single row, so the stats block degrades in tiers as
        the terminal narrows (clocks and fan go first) rather than wrapping or
        being truncated mid-number. Per-process VRAM is NOT shown here - the
        tree's VRAM column already attributes it to the owning container,
        which is the same information in a place that can be sorted.
        """
        if err and not gpus:
            self.addstr(y, 0, " GPU ", curses.color_pair(C_CYAN) | curses.A_BOLD)
            self.addstr(y, 5, err, curses.color_pair(C_RED))
            return y + 1

        avail = max(20, w - 1)
        for i, g in enumerate(gpus):
            tag = short_gpu(g["name"])[:8] if len(gpus) > 1 else "GPU"
            lbl = " %-*s " % (max(3, len(tag)), tag)
            self.addstr(y, 0, lbl, curses.color_pair(C_CYAN) | curses.A_BOLD)
            x0 = len(lbl)

            temp, pwr = g["temp"], g["power"]
            stats_full, stats_mid, stats_min = [], [], []
            if temp is not None:
                t = ("%d\u00b0C" % temp,
                     C_RED if temp >= 80 else
                     (C_YELLOW if temp >= 70 else C_GREEN))
                stats_full.append(t)
                stats_mid.append(t)
                stats_min.append(t)
            if pwr is not None:
                pw = ("%.0f/%.0fW" % (pwr, g["power_cap"] or 0),
                      C_YELLOW if (g["power_cap"] and
                                   pwr > 0.9 * g["power_cap"]) else C_GREEN)
                stats_full.append(pw)
                stats_mid.append(pw)
                stats_min.append(pw)
            if g["sm_clk"] or g["mem_clk"]:
                stats_full.append(("%d/%dMHz" % (g["sm_clk"] or 0,
                                                 g["mem_clk"] or 0), C_DIM))
            if g["fan"] is not None:
                stats_full.append(("fan%d%%" % g["fan"], C_DIM))
            ps = str(g["pstate"] or "")
            if ps and ps != "-":
                stats_full.append((ps, C_DIM))
                stats_mid.append((ps, C_DIM))

            def width(bits):
                return sum(len(t) + 1 for t, _ in bits)

            stats = stats_full
            for cand in (stats_full, stats_mid, stats_min, []):
                stats = cand
                if avail - x0 - width(cand) >= 24:
                    break

            room = avail - x0 - width(stats)
            # util gets the smaller share; VRAM carries a longer label
            ubar = max(10, (room - 1) * 2 // 5)
            vbar = max(10, room - 1 - ubar)

            u = g["util"] or 0
            self.bar(y, x0, ubar,
                     [(u, C_GREEN if u < 60 else
                       (C_YELLOW if u < 90 else C_RED)), (100 - u, 0)],
                     label="%3d%%" % u, total=100)
            mu = g["mem_used"] / (g["mem_total"] or 1) * 100
            self.bar(y, x0 + ubar + 1, vbar,
                     [(g["mem_used"], C_MAGENTA if mu < 85 else C_RED),
                      (max(0, g["mem_total"] - g["mem_used"]), 0)],
                     label="%s/%s" % (human0(g["mem_used"]),
                                      human0(g["mem_total"])),
                     total=g["mem_total"] or 1)
            x = x0 + ubar + 1 + vbar
            for txt, cp in stats:
                self.addstr(y, x + 1, txt,
                            curses.color_pair(cp) | curses.A_BOLD)
                x += len(txt) + 1
            y += 1
        return y

    def draw_console(self, y, h, w):
        """Dedicated box for kernel/console messages.

        The physical console is shared with the kernel: printk writes land
        wherever the cursor happens to be and curses never learns that those
        cells changed, so stale text survives every partial repaint. Owning a
        box for them means the UI redraws that region itself every frame, and
        the whole screen is force-repainted whenever a new message arrives.
        Returns the first row the box occupies.
        """
        n = self.console_lines
        # never starve the tree: it keeps a floor of rows (header + 5) even if
        # that means shrinking the console box on a short console
        room = (h - 1) - y - 6
        n = max(1, min(n, room - 1))
        if room < 3:
            return h - 1
        top = h - 1 - n - 1              # header row + n body rows
        if top <= y:
            return h - 1
        lines, _ = self.con.snapshot(n)
        hdr = " CONSOLE  (kernel ring buffer, newest last)"
        if self.hushed:
            hdr += "  [console muted]"
        self.addstr(top, 0, hdr.ljust(max(0, w - 1))[:max(0, w - 1)], HDR)
        for i in range(n):
            row = top + 1 + i
            self.addstr(row, 0, " " * max(0, w - 1))
            if i >= len(lines):
                continue
            ts, sev, msg = lines[i]
            cp = (C_RED if sev <= 3 else
                  (C_YELLOW if sev == 4 else
                   (C_CYAN if sev == 5 else C_DIM)))
            attr = curses.color_pair(cp)
            if sev <= 3:
                attr |= curses.A_BOLD
            text = ("%-8s %s" % (ts, msg)) if ts else msg
            self.addstr(row, 1, text[:max(0, w - 2)], attr)
        return top

    # ---------------- tree ----------------
    def build_rows(self, cgs, procs, names, gproc):
        by_cid = {}
        for p in procs.values():
            if p["cid"]:
                by_cid.setdefault(p["cid"], []).append(p)

        # feed the name resolver a representative pid per container: the
        # lowest pid is the container's init, which holds the bind mounts.
        dock = getattr(self, "dock", None)
        if dock is not None:
            dock.set_pids({cid: min(p["pid"] for p in kids)
                           for cid, kids in by_cid.items() if kids})

        rows = []
        groups = []
        for cid, c in cgs.items():
            kids = by_cid.get(cid, [])
            vram = sum(gproc.get(p["pid"], 0) for p in kids)
            groups.append({
                "kind": "container", "cid": cid,
                "name": prettify(names.get(cid, cid[:12])),
                "cpu": c["cpu"], "mem": c["mem"], "rd": c["rd"], "wr": c["wr"],
                "vram": vram, "nproc": len(kids), "kids": kids,
                "cputime": c["cputime"],
            })

        host = [p for p in procs.values() if not p["cid"]]
        if host:
            groups.append({
                "kind": "host", "cid": "__host__", "name": "system (host)",
                "cpu": sum(p["cpu"] for p in host),
                "mem": sum(p["rss"] for p in host),
                "rd": 0.0, "wr": 0.0,
                "vram": sum(gproc.get(p["pid"], 0) for p in host),
                "nproc": len(host), "kids": host, "cputime": 0,
            })

        # tell the probe thread which containers exist so detail:"auto" can
        # match shapes by row name. Done BEFORE the filter: hiding a row in
        # the UI must not stop its probe, or clearing the filter would show a
        # column of blanks until the next poll.
        appdet = getattr(self, "appdet", None)
        if appdet is not None and hasattr(appdet, "set_rows"):
            appdet.set_rows([g["name"] for g in groups],
                            {g["name"]: g["cid"] for g in groups})

        key = {"cpu": lambda g: -g["cpu"], "mem": lambda g: -g["mem"],
               "name": lambda g: g["name"]}[self.sort]
        groups.sort(key=key)

        # name filter: match the container name, or any child process name so
        # '/python' finds the containers running python
        f = self.filter.lower()
        if f:
            groups = [g for g in groups
                      if f in g["name"].lower()
                      or any(f in p["comm"].lower() for p in g["kids"])]

        for g in groups:
            rows.append(g)
            if g["cid"] in self.expanded:
                pkey = {"cpu": lambda p: -p["cpu"], "mem": lambda p: -p["rss"],
                        "name": lambda p: p["comm"]}[self.sort]
                for p in sorted(g["kids"], key=pkey)[:60]:
                    rows.append({"kind": "proc", "parent": g["cid"], "p": p,
                                 "vram": gproc.get(p["pid"], 0)})
        return rows

    def draw_tree(self, y, bottom, w, rows, details):
        # PROCS is narrow and fixed; DETAIL takes every remaining column
        fixed = 1 + 26 + 1 + 7 + 1 + 9 + 1 + 9 + 1 + 9 + 1 + 6 + 2 + 5 + 2
        dwidth = max(0, w - fixed)
        hdr = (" %-26s %7s %9s %9s %9s %6s  %5s  %s" %
               ("NAME", "CPU%", "MEM", "READ/s", "WRITE/s", "VRAM", "PROCS",
                "DETAIL" if dwidth > 6 else ""))
        # one cell spare: writing the final column wraps on some terminals
        lw = max(0, w - 1)
        self.addstr(y, 0, hdr.ljust(lw)[:lw], HDR)
        y += 1
        avail = bottom - y
        if avail < 1:
            return

        if self.sel >= len(rows):
            self.sel = max(0, len(rows) - 1)
        if self.sel < self.scroll:
            self.scroll = self.sel
        elif self.sel >= self.scroll + avail:
            self.scroll = self.sel - avail + 1
        self.scroll = max(0, min(self.scroll, max(0, len(rows) - avail)))

        for i in range(avail):
            idx = self.scroll + i
            if idx >= len(rows):
                break
            r = rows[idx]
            selected = (idx == self.sel)
            base = curses.color_pair(C_SEL) if selected else 0

            if r["kind"] == "proc":
                p = r["p"]
                line = ("   %s %-22s %7.1f %9s %9s %9s %6s  %s" %
                        ("+" if r["vram"] else " ",
                         ("%d %s" % (p["pid"], p["comm"]))[:22],
                         p["cpu"], human(p["rss"]), "", "",
                         human(r["vram"]) if r["vram"] else "",
                         cpu_time_str(p["time"])))
                attr = base or curses.color_pair(C_DIM)
                self.addstr(y + i, 0, line.ljust(lw)[:lw], attr)
            else:
                mark = "-" if r["cid"] in self.expanded else "+"
                nm = "%s %s" % (mark, r["name"])
                line = (" %-26s %7.1f %9s %9s %9s %6s  %5d  " %
                        (nm[:26], r["cpu"], human(r["mem"]),
                         human(r["rd"]) + "/s" if r["rd"] > 1024 else "",
                         human(r["wr"]) + "/s" if r["wr"] > 1024 else "",
                         human(r["vram"]) if r["vram"] else "",
                         r["nproc"]))
                attr = base
                if not selected:
                    attr = curses.A_BOLD
                    if r["vram"]:
                        attr |= curses.color_pair(C_MAGENTA)
                    elif r["kind"] == "host":
                        attr |= curses.color_pair(C_DIM)
                    else:
                        attr |= curses.color_pair(C_CYAN)
                self.addstr(y + i, 0, line.ljust(lw)[:lw], attr)
                # detail text in its own colour so it reads as annotation
                det = details.get(r["name"])
                room = lw - len(line)
                if det and dwidth > 6 and room > 6:
                    self.addstr(y + i, len(line), det[:room],
                                base or curses.color_pair(C_GREEN))

    def draw_footer(self, h, w, note):
        if self.editing:
            prompt = "filter: %s_" % self.filter
            self.addstr(h - 1, 0, " " * w, HDR)
            self.addstr(h - 1, 0, prompt[:w], HDR)
            hint = "enter=apply  esc=clear"
            if w - len(hint) - 1 > len(prompt) + 2:
                self.addstr(h - 1, w - len(hint) - 1, hint, HDR)
            return
        keys = [("q", "quit"), ("enter", "expand"), ("c/m/n", "sort"),
                ("a/z", "all/none"), ("/", "filter"),
                ("l", "console" if not self.show_console else "hide con"),
                ("+/-", "%.1fs" % self.interval), ("h", "help")]
        if self.filter:
            note = "filter: %s (esc clears)" % self.filter
        # htop layout: key letter bright on the normal background, its label
        # on the cyan bar. No bold on the bar itself - see init_colors().
        self.addstr(h - 1, 0, " " * w, HDR)
        x = 0
        for k, v in keys:
            if x + len(k) + len(v) + 3 > w:
                break
            self.addstr(h - 1, x, k, KEY)
            x += len(k)
            lbl = " %s  " % v
            self.addstr(h - 1, x, lbl, HDR)
            x += len(lbl)
        if note:
            nx = w - len(note) - 1
            if nx > x + 1:
                self.addstr(h - 1, nx, note, HDR)

    def draw_help(self, h, w):
        lines = [
            "jtop - workload-aware system monitor",
            "",
            "  up/down or j/k                 move selection",
            "  PgUp/PgDn, Home/End            page, first, last",
            "  enter/space/left/right         expand or collapse a container",
            "  a / z                          expand all / collapse all",
            "  c / m / n                      sort by CPU / memory / name",
            "  /                              filter by name",
            "  esc                            clear the filter, else quit",
            "  + / -                          refresh -/+ 0.5s (0.5s-10s)",
            "  l                              show/hide the CONSOLE box",
            "  [ / ]                          CONSOLE box 1-20 rows",
            "  ctrl-L                         force a full repaint",
            "  h or ?                         this help",
            "  q                              quit",
            "",
            "Memory bar segments:",
            "  green=apps  magenta=ZFS ARC  blue=page cache  empty=free",
            "  ARC is reclaimable: it shrinks under memory pressure, so it is",
            "  shown separately from memory the kernel cannot hand back.",
            "",
            "VRAM comes from nvidia-smi, mapped to the owning container via",
            "/proc/<pid>/cgroup. The tree's VRAM column is sortable with m.",
            "",
            "CONSOLE box: the kernel prints to the same physical console that",
            "curses is drawing on. The box gives those messages a region the",
            "UI owns, and repaints the screen when one arrives. --hush stops",
            "the kernel writing to the console while jtop is up.",
            "",
            "press any key to return",
        ]
        bh, bw = len(lines) + 2, max(len(l) for l in lines) + 4
        top, left = max(0, (h - bh) // 2), max(0, (w - bw) // 2)
        for i in range(bh):
            self.addstr(top + i, left, " " * bw, curses.color_pair(C_SEL))
        for i, l in enumerate(lines):
            self.addstr(top + 1 + i, left + 2, l,
                        curses.color_pair(C_SEL) |
                        (curses.A_BOLD if i == 0 else 0))

    # ---------------- main loop ----------------
    def run(self):
        curses.curs_set(0)
        self.scr.nodelay(True)
        init_colors()
        self.gpu.start()
        # prime the pid map from a first sample so the /proc name fallback has
        # something to work with before the resolver's first poll
        first = self.proc.sample()
        self.cg.sample()
        self.cpu.sample()
        self.dock.set_pids({})
        by_cid = {}
        for p in first.values():
            if p["cid"]:
                by_cid.setdefault(p["cid"], []).append(p["pid"])
        self.dock.set_pids({c: min(v) for c, v in by_cid.items()})
        self.dock.start()
        self.appdet.start()
        self.con.start()
        if CONFIG.get("_config_error") and not self.demo:
            self.con._add("", 4, "jtop config: " + CONFIG["_config_error"])

        last = 0.0
        cached = None
        while True:
            now = time.monotonic()
            if cached is None or now - last >= self.interval:
                total, cores = self.cpu.sample()
                m = self.mem.sample()
                procs = self.proc.sample()
                cgs = self.cg.sample()
                gpus, gproc, gerr = self.gpu.snapshot()
                names, derr = self.dock.snapshot()
                details = self.appdet.snapshot()
                self.rows = self.build_rows(cgs, procs, names, gproc)
                cached = (total, cores, m, procs, gpus, gproc, gerr, names,
                          derr, details)
                last = now
            (total, cores, m, procs, gpus, gproc, gerr, names, derr,
             details) = cached

            self.scr.erase()
            h, w = self.scr.getmaxyx()
            y = self.draw_header(0, w)
            y = self.draw_cpu(y, w, total, cores)
            y = self.draw_rule(y, w, "%d cores" % self.ncpu)
            y = self.draw_cpumem(y, w, total, cores, m)
            y = self.draw_gpu(y, w, gpus, gerr)
            bottom = h - 1
            if self.show_console:
                bottom = self.draw_console(y, h, w)
            self.draw_tree(y, bottom, w, self.rows, details)
            self.draw_footer(h, w, derr or (gerr if gpus else "") or "")
            if self.show_help:
                self.draw_help(h, w)
            # A kernel printk writes straight to the physical console, so
            # curses' idea of those cells is wrong and a diff-based refresh
            # leaves the message stuck on screen. Whenever a new console line
            # arrives (or the user asks with ^L), repaint every cell.
            _, cseq = self.con.snapshot(1)
            if cseq != self._con_seq or self.force_repaint:
                self._con_seq = cseq
                self.force_repaint = False
                self.scr.clearok(True)
            self.scr.refresh()

            if self.handle_keys():
                return
            time.sleep(0.05)

    def handle_keys(self):
        page = max(1, self.scr.getmaxyx()[0] - 14 -
                   (self.console_lines + 1 if self.show_console else 0))
        while True:
            ch = self.scr.getch()
            if ch == -1:
                return False
            if self.show_help:
                self.show_help = False
                continue
            if self.editing:
                if ch in (curses.KEY_ENTER, 10, 13):
                    self.editing = False
                elif ch == 27:                      # esc: cancel + clear
                    self.editing = False
                    self.filter = ""
                elif ch in (curses.KEY_BACKSPACE, 127, 8):
                    self.filter = self.filter[:-1]
                elif 32 <= ch < 127:
                    self.filter += chr(ch)
                self.sel = 0
                self.scroll = 0
                continue
            if ch == ord("/"):
                self.editing = True
                continue
            if ch == 27 and self.filter:            # esc clears an applied filter
                self.filter = ""
                self.sel = 0
                continue
            if ch in (ord("q"), ord("Q"), 27):
                return True
            elif ch in (curses.KEY_DOWN, ord("j")):
                self.sel = min(self.sel + 1, max(0, len(self.rows) - 1))
            elif ch in (curses.KEY_UP, ord("k")):
                self.sel = max(0, self.sel - 1)
            elif ch == curses.KEY_NPAGE:
                self.sel = min(self.sel + page, max(0, len(self.rows) - 1))
            elif ch == curses.KEY_PPAGE:
                self.sel = max(0, self.sel - page)
            elif ch == curses.KEY_HOME:
                self.sel = 0
            elif ch == curses.KEY_END:
                self.sel = max(0, len(self.rows) - 1)
            elif ch in (curses.KEY_ENTER, 10, 13, ord(" "),
                        curses.KEY_RIGHT, curses.KEY_LEFT):
                if 0 <= self.sel < len(self.rows):
                    r = self.rows[self.sel]
                    cid = r["parent"] if r["kind"] == "proc" else r["cid"]
                    self.expanded.symmetric_difference_update({cid})
            elif ch == ord("a"):
                self.expanded = {r["cid"] for r in self.rows
                                 if r["kind"] != "proc"}
            elif ch == ord("z"):
                self.expanded.clear()
            elif ch in (ord("c"), ord("m"), ord("n")):
                self.sort = {ord("c"): "cpu", ord("m"): "mem",
                             ord("n"): "name"}[ch]
            elif ch in (ord("+"), ord("=")):
                self.interval = max(0.5, self.interval - 0.5)
            elif ch == ord("-"):
                self.interval = min(10.0, self.interval + 0.5)
            elif ch in (ord("h"), ord("?")):
                self.show_help = True
            elif ch in (ord("l"), ord("L")):
                self.show_console = not self.show_console
                self.force_repaint = True
            elif ch == ord("["):
                self.console_lines = max(1, self.console_lines - 1)
                self.force_repaint = True
            elif ch == ord("]"):
                self.console_lines = min(20, self.console_lines + 1)
                self.force_repaint = True
            elif ch == 12:                          # ctrl-L
                self.force_repaint = True
            elif ch == curses.KEY_RESIZE:
                self.scr.erase()
                self.force_repaint = True


def main():
    global CONFIG, CG_ROOTS, ARCSTATS, IX
    interval = 2.0
    hush = False
    demo = False
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a in ("-h", "--help"):
            print(__doc__)
            return 0
        if a == "--dump-config":
            cfg = {k: v for k, v in CONFIG.items()
                   if not k.startswith("_")}
            print(json.dumps(cfg, indent=2, sort_keys=True))
            return 0
        if a == "--config":
            if i + 1 >= len(args):
                print("--config needs a file path", file=sys.stderr)
                return 2
            os.environ["JTOP_CONFIG"] = args[i + 1]
            CONFIG = load_config()
            if CONFIG.get("_config_error"):
                print("config: " + CONFIG["_config_error"], file=sys.stderr)
                return 2
            CG_ROOTS = tuple(CONFIG["cgroup_roots"])
            ARCSTATS = CONFIG["arcstats"]
            IX = CONFIG["app_config_root"]
            continue
        if a == "--hush":
            hush = True
            continue
        if a == "--demo":
            demo = True
            continue
        if a == "--list-shapes":
            sys.exit(list_shapes(CONFIG))
        if a == "--discover" or a.startswith("--discover="):
            sys.exit(discover(CONFIG,
                              write=a.split("=", 1)[1] if "=" in a else None))
        if i and args[i - 1] == "--config":
            continue
        try:
            interval = max(0.5, float(a))
        except ValueError:
            pass

    saved = None
    if hush:
        # 3 = only err and worse reach the console; everything still lands in
        # the ring buffer, so the CONSOLE box keeps showing it all
        saved = console_loglevel(3)

    def boot(scr):
        app = App(scr, interval, demo=demo)
        app.hushed = bool(hush and saved is not None)
        try:
            app.run()
        finally:
            app.con.stop()
            app.gpu.stop()
            app.dock.stop()
            app.appdet.stop()

    try:
        curses.wrapper(boot)
    except KeyboardInterrupt:
        pass
    finally:
        if saved is not None:
            console_loglevel(saved)
    return 0


if __name__ == "__main__":
    sys.exit(main())
