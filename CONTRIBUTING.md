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

A new `kind` changes `jtop.py`, so it needs a reason the shape language could
not express it. Say what you tried first. There are 13 kinds; five are generic
(`http_json`, `http`, `docker_health`, `log_tail`, `command`) and cover most
apps.

If the app simply has no API, that is `log_tail` — a last resort, with the
reasons documented in
[shapes/README.md](shapes/README.md#log_tail).

## Style

* Standard library only. No dependencies, ever — this has to run on a
  console with no package manager available.
* Python 3.9 compatible.
* A failing probe must return `None`, never raise. Running a different
  version of an app should give a blank cell, not a traceback.
* Keep DETAIL output under ~60 characters; it shares a row with the tree.
* Comments describe what the code does and the invariants it holds — not the
  bug that prompted the change. That belongs in the commit message.

## Testing a UI change

jtop is a curses app, so "it looked fine" is not evidence. Render it
headlessly in a pty and assert on the resulting cells. At minimum check
80 columns — the width of a real Linux console — and confirm no row reaches
the final column, which makes some terminals wrap and corrupt the next line.

`--demo` gives you deterministic data to test layout against, and must stay
free of host access: no `/proc`, no `nvidia-smi`, no `docker`, no network.
