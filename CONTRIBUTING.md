# Contributing

## Adding an app

Most contributions should be a **shape**: a small JSON file describing how to
ask one app what it is doing. No Python required. See
[shapes/README.md](shapes/README.md) for the format and the rules.

Please include in the PR:

1. The shape file, in `shapes/`.
2. The matching line from `jtop --discover` on your host, so there is
   evidence it works against a real instance.
3. The app version you tested against.

Use the project's **upstream default port**, never whatever your orchestrator
assigned. jtop prefers the port a container actually publishes, so an
upstream default is the right thing to write down.

## Adding a kind

A new `kind` is a change to `jtop.py` and needs more justification: it means
the shape language could not express something. Say what you tried first. If
the answer is "the app has no API", that is `log_tail` — see below.

## Logs are a last resort

`log_tail` exists for apps with no API at all. It is a fallback, not a
shortcut:

* Log wording is not a stable interface. It changes in point releases and
  nobody treats that as breaking.
* A log line is *history, not state*. The newest line may be hours old, which
  is why jtop always prefixes the age.
* It needs the Docker socket, so it is more privileged than an HTTP probe.

If the app has a status endpoint, use `http_json`.

## Style

* Standard library only. No dependencies, ever — this has to run on a
  console with no package manager available.
* Python 3.9 compatible.
* A failing probe must return `None`, never raise. Someone running a
  different version of an app should see a blank cell, not a traceback.
* Keep DETAIL output under ~60 characters; it shares a row with the tree.

## Testing a UI change

jtop is a curses app, so "it looked fine" is not evidence. Render it
headlessly in a pty and assert on the resulting cells. At minimum check
80 columns — the width of a real Linux console — and confirm no row reaches
the final column, which makes some terminals wrap and corrupt the next line.

`--demo` gives you deterministic data to test layout against, and must stay
free of host access: no `/proc`, no `nvidia-smi`, no `docker`, no network.
