# Shapes

A **shape** is a small JSON document describing how to ask one app what it is
doing. Shapes are data, not code: adding support for a new app should be a
JSON file and a pull request, never a patch to `jtop.py`.

Drop `*.json` files in either of these directories:

    ~/.config/jtop/shapes/
    /etc/jtop/shapes/

A file-based shape **replaces** a built-in of the same name, so you can
override one app without vendoring the whole table.

## Quickest path

Run discovery against your own box first. It tells you which containers
matched a shape, which ports actually answered, and which have nothing:

    jtop --discover

It writes nothing. When the report looks right, save it:

    jtop --discover=~/.config/jtop/config.json

To see what is already known:

    jtop --list-shapes

## File format

One shape per file, named by the file (`jellyfin.json` → shape `jellyfin`):

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

Or several in one file, keyed by name:

```json
{
  "uptime-kuma": { "kind": "docker_health", "match": ["uptime-kuma"] },
  "gitea":       { "kind": "http_json", "port": 3000, "path": "/api/v1/version",
                   "match": ["gitea"], "template": "v{v}",
                   "fields": { "v": "version" } }
}
```

### Common keys

| Key | Meaning |
|---|---|
| `kind` | **Required.** Which parser to use (see below). |
| `match` | Image-name substrings, used by `--discover` to spot the app. |
| `port` | The project's **upstream default**. See "Ports" below. |
| `pinned_port` | `true` = use `port` verbatim, never auto-detect. |
| `path` | URL path for the HTTP kinds. |
| `host` | `"lan"` to use the host's LAN IP instead of loopback. |
| `key` | Set if the app needs an API key. |
| `key_file` | `[path under app_config_root, regex with one capture group]`. |
| `key_header` | Header to send the key in (default `X-Api-Key`). |

### Ports

The `port` in a shape is the project's documented default. jtop prefers the
port **the container actually publishes**: if the container publishes your
shape's port it uses that, if it publishes exactly one port it trusts that,
and only otherwise falls back to the default.

This matters more than it sounds. SABnzbd and SearXNG both default to `8080`
— blindly probing a default can report one app's state under another's name.
If you need a literal port, set `pinned_port: true`.

## Kinds

Prefer the generic kinds. A new `kind` means a change to `jtop.py`, so if you
find yourself wanting one, say so in the PR — it usually means the shape
language is missing something.

### `http_json`

Fetch JSON, pull fields out, render a template. Covers most modern apps.

```json
{
  "kind": "http_json", "port": 8989, "path": "/api/v3/queue",
  "key": "sonarr",
  "template": "queue {n}: {title} ({state})",
  "fields": {
    "n": "totalRecords",
    "title": "records[].title",
    "state": "records[].status"
  }
}
```

Field paths are dotted, with list support:

| Path | Means |
|---|---|
| `queue.totalRecords` | nested object key |
| `records[].title` | **first** element of a list |
| `items[2].name` | third element |
| `[].Id` | first element of a top-level list |

A missing path yields nothing and blanks that placeholder — a shape for an
app version you do not run must degrade quietly, never raise.

Optional transforms: `count` (list → length), `humanise` (bytes → `4.2GB`),
`suffix` (append, e.g. `/s`), `map` (substitute values, e.g.
`{"True": "healthy"}`).

### `http`

Liveness by status code only. `label` is the text shown.

```json
{ "kind": "http", "port": 8888, "path": "/", "label": "search up" }
```

### `docker_health`

The container's own `HEALTHCHECK` verdict. No port, no key, no API knowledge.

This is already the automatic fallback for any container whose shape returns
nothing, so declare it explicitly only when you want the healthcheck *instead*
of a probe rather than as a backstop.

### `log_tail`

Last line matching `pattern` from `docker logs`.

```json
{ "kind": "log_tail", "match": ["unpackerr"],
  "pattern": "(Extracted|Extracting|Queue|error)" }
```

**Use this only when an app has no API.** Three limitations, in order of
how often they bite:

- Log text is not a stable interface — wording changes in point releases.
- A log line is history, not state. The newest match may be hours old, so
  jtop prefixes the age (`4h10m ago …`).
- It requires access to the Docker socket.

If the app has a status endpoint, use `http_json`.

### `command`

Run a command, extract with a regex. For apps that speak a protocol rather
than HTTP.

```json
{
  "kind": "command", "port": 6379, "match": ["redis"],
  "argv": ["redis-cli", "-h", "{host}", "-p", "{port}", "info", "clients"],
  "pattern": "connected_clients:(\\d+)",
  "template": "{1} clients"
}
```

`argv` is a list and is never passed through a shell. Only `{host}` and
`{port}` are substituted. `{1}`, `{2}` … are regex groups in `template`.

## Rules for contributed shapes

1. **Use upstream default ports.** Not whatever your orchestrator assigned.
2. **No secrets, no hostnames, no personal paths.** `key_file` paths are
   relative to `app_config_root`, which every user sets themselves.
3. **Prefer an API over a log.** If you must use `log_tail`, say why in the
   PR.
4. **Keep the output short.** It shares one terminal row with the process
   tree — aim for under 60 characters.
5. **Test with `--discover`** and paste the matching line into the PR.
6. **Fail quietly.** Someone running a different version of the app should
   see a blank cell, not a traceback.
