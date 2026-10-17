# jtop

A workload-aware system monitor for a headless server's physical console.

`htop` tells you a process is using 13.6 GB. `jtop` tells you it is **ollama,
holding mistral-small at Q4_K_M with 11 minutes left on its TTL**, that the
9.1 GB of VRAM next to it belongs to that same container, and that the 27 GB
your tools are calling "used" is actually ZFS ARC and is evictable.

Zero dependencies — Python 3.9+ standard library only.

```
 jtop  demo-host [DEMO]                                         load 0.42 0.51 0.48   up 3d 23:00   22:35:57
  0 [||||||||||                              24%]        8 [|||||||||||||||||||                     45%]
  1 [||||||||                                19%]        9 [||||||                                  13%]
  2 [|||||||||||||                           30%]       10 [|||||||||||||||||||                     45%]
  3 [|||||||||                               21%]       11 [||||||||                                19%]
  4 [||||||||||||||||                        36%]       12 [||||||||||||||||||||||||||              61%]
  5 [|||||                                   13%]       13 [||||                                     8%]
  6 [||||||||||||||                          32%]       14 [|||||||                                 16%]
  7 [||||||||||||||||||||||                  52%]       15 [||||                                     9%]
── 16 cores ─────────────────────────────────────────────────────────────────────────────────────────────────
 CPU [|||||||||||||                        27.7% 16c]  Mem [|||||||||||||||41GB/64GB]  a20GB r21GB c9GB f14GB
 RTX 4070 [|||||||||||||||||| 84%] [||||||||||||||||||||||||||9GB/12GB] 72°C 172/240W 1860/10501MHz fan48% P2
 GTX 1080 [|||                 13%] [||||                        1GB/11GB] 42°C 31/250W 1480/5505MHz fan0% P2
 NAME                          CPU%       MEM    READ/s   WRITE/s   VRAM  PROCS  DETAIL
 + radarr                      32.1   165.0MB           976.6KB/s             2  queue 0
 + searxng                     27.0   129.0MB           976.6KB/s             3  search up (HTTP 200)
 + prowlarr                    26.1   147.0MB                                 2  11/12 indexers enabled  1 FA
 + sabnzbd                     22.3    98.0MB   1.9MB/s                       3  Downloading  2 jobs  41.2MB/
 + overseerr                   19.7   192.0MB             8.6MB/s             3  v1.33.2  update available
 + open-webui                  19.3   639.0MB                                 1  healthy
 + bazarr                       9.7   193.0MB                                 2  4 ep / 1 film wanted  links
 + redis                        8.0    15.0MB  42.9MB/s                       2
 CONSOLE  (kernel ring buffer, newest last)
 22:35:47 nvidia 0000:01:00.0: GPU at 42C, fan ramping to 48%
 22:35:53 zfs: ARC target reduced to 24G under memory pressure
q quit  enter expand  c/m/n sort  a/z all/none  / filter  l hide con  +/- 1.0s  h help
```

*(That is real output from `jtop --demo`, which runs entirely on synthetic
data — see [Demo mode](#demo-mode).)*

## Install

One file, no packaging:

```
curl -O https://raw.githubusercontent.com/JabawokJayUK/jtop/main/jtop.py
chmod +x jtop.py
./jtop.py
```

Then let it work out what you are running:

```
./jtop.py --discover
```

That prints a report — which containers matched, which ports answered, what
it could not reach — and a config you can save. It writes nothing unless you
ask:

```
./jtop.py --discover=~/.config/jtop/config.json
```

Run it as root (or with working `sudo -n`) for the CONSOLE box, which needs
the kernel ring buffer.

## Why it exists

Four things conventional monitors get wrong on a container host:

**Memory.** On ZFS, ARC shows up as "used" and panics you into buying RAM you
do not need. jtop splits memory into apps / ARC / cache / free, because ARC
gives memory back under pressure and the others do not.

**VRAM.** `nvidia-smi` reports VRAM per PID. On a container host a PID is
meaningless — you want to know which *app* is holding 9 GB. jtop resolves
each GPU process to its owning container.

**What the app is doing.** CPU and RSS tell you a container is busy, not
whether the queue is stuck, the indexer is failing, or the model you think is
loaded actually is. That is the DETAIL column.

**The physical console.** On a real console, `printk` writes into the same
cells curses is managing. Its internal screen model never learns those cells
changed, so kernel messages leave stale text that no redraw clears. jtop
docks a CONSOLE box above the footer, tails the ring buffer itself, and forces
a full repaint whenever a message arrives — so the messages land *inside* a
region it owns. `--hush` additionally stops the kernel writing to the console
while jtop runs, and restores the previous loglevel on exit.

## The DETAIL column: kinds and shapes

This is the part worth understanding, and the part designed for contribution.

- A **kind** is a parser, written in Python: `http_json`, `http`,
  `docker_health`, `log_tail`, `command`, plus a few app-specific ones. There
  are only a handful and they rarely change.
- A **shape** is *data* describing one app — which kind, which port, which
  field to show. Shapes are JSON.

Adding support for a new app is a JSON file and a pull request, not a patch
to the source:

```json
{
  "kind": "http_json",
  "port": 8096,
  "path": "/Sessions",
  "match": ["jellyfin"],
  "template": "{n} session(s)",
  "fields": { "n": "[].Id" }
}
```

Drop that in `~/.config/jtop/shapes/jellyfin.json` and it loads. A file shape
replaces a built-in of the same name, so overriding one app does not mean
vendoring the whole table. Full format: **[shapes/README.md](shapes/README.md)**.

Containers with no shape are not blank: jtop falls back to the container's own
Docker `HEALTHCHECK` verdict, which needs no per-app knowledge at all. On a
typical stack that covers over half of them for free.

### Ports are detected, not assumed

A shape's `port` is the project's documented default. jtop prefers the port
**the container actually publishes** — because orchestrators reassign ports
(TrueNAS SCALE hands out 30000+), and because a default can belong to a
*different* app. SABnzbd and SearXNG both default to 8080; probing blindly
would report one app's state under the other's name.

### Keys

Some apps need an API key. `app_config_root` points at the parent of your app
config trees, and each shape says where its key lives:

```json
"key_file": ["sonarr/config/config.xml", "<ApiKey>([a-f0-9]+)</ApiKey>"]
```

`app_config_root` is **empty by default** — a fresh install reads no app
config until you point it somewhere. Keys are used only to talk to the host
you configured, and are never rendered or logged.

## Demo mode

```
./jtop.py --demo
```

Every sampler is replaced with a synthetic one: invented hosts, two invented
GPUs, invented containers and messages. Useful for trying the UI on a machine
with no GPU or containers, for reproducible screenshots, and as a layout test
fixture.

It genuinely touches nothing. Verified with a PEP 578 audit hook counting
file opens, process spawns and socket connects:

| 10s run, 40×150 | file opens | process spawns | socket connects |
|---|---|---|---|
| normal | 3443 | 4 | 11 |
| `--demo` | **0** | **0** | **0** |

(Only paths that could identify a host are counted — `/proc`, cgroups, the
app config root, `/dev/kmsg`. Terminfo and Python's own imports are ignored.)

## Keys

```
up/down,PgUp/PgDn,Home/End  move      enter/space  expand/collapse
c/m/n  sort cpu/mem/name              a  expand all     z  collapse all
/      filter by name                 esc  clear filter
+/-    refresh interval               l  console box   [ / ]  box size
^L     force full repaint             q  quit          h  help
```

The `/` filter matches container names *and* the processes inside them, so
`/python` finds every container running python.

## Options

```
<secs>          refresh interval (default 2)
--demo          run on synthetic data (no /proc, nvidia-smi, docker, network)
--discover      report what is running and print a matching config
--discover=FILE write that config to FILE
--list-shapes   list known shapes and the kind each uses
--dump-config   print the effective config as JSON
--config FILE   use FILE instead of the search path
--hush          stop the kernel printing to the console while jtop runs
```

Config search order, first match wins: `$JTOP_CONFIG`,
`~/.config/jtop/config.json`, `/etc/jtop/config.json`.

## Requirements

- Linux with cgroup v2 (container tree)
- Python 3.9+ — standard library only
- `nvidia-smi` for the GPU pane (absent = pane hidden)
- `docker` for container names and healthchecks
- root or `sudo -n` for the CONSOLE box

Everything degrades: no GPU, no ZFS, no Docker, no root — the panels that
cannot work disappear rather than erroring.

## Licence

MIT — see [LICENSE](LICENSE).
