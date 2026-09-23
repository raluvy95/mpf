# Contributing

Keep changes focused and include regression coverage for behavior changes.

## Development Setup

MPF targets Linux and Python 3.14. Create a source environment with:

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
```

Before submitting a change, run:

```bash
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy mpf.py mpf_core
```

AppImage changes should also pass a Linux build and smoke test:

```bash
APPIMAGETOOL=/path/to/appimagetool bash scripts/build_appimage.sh
./dist/mpf-0.1.0-x86_64.AppImage --help
```

## Commits and Releases

Use Conventional Commits: `type: short imperative description`, optionally
with a scope. Use `feat:` for features, `fix:` for fixes, and `docs:`, `test:`,
`refactor:`, `perf:`, `ci:`, or `chore:` where appropriate.

Mark breaking changes with `!` and a `BREAKING CHANGE:` footer. Releases follow
Semantic Versioning. Push a `vMAJOR.MINOR.PATCH` tag to build and publish a
GitHub Release; its title matches the tag and its notes are generated from
commits since the previous release.

## Repository Hygiene

Do not commit virtual environments, XDG cache data, generated thumbnails,
local playback state, logs, or local build output. Update user-facing docs and
the changelog when behavior, requirements, controls, or packaging changes.
