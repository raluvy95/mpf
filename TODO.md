# MPF Progress

## Completed

- [x] Analyzed the project architecture, tests, Linux-only constraints, and build gaps.
- [x] Stabilized thumbnail-cache cleanup and fixed the nullable process handle.
  - Commit: `72cf005 fix: stabilize thumbnail cleanup and type checks`
- [x] Added Linux-only AppImage packaging with PyInstaller and appimagetool.
  - Local AppImage build and `--help` smoke test completed successfully.
  - Commit: `71a5191 feat: add Linux AppImage packaging`
- [x] Added GitHub Actions for Linux tests, Ruff, mypy, and AppImage builds.
  - Commit: `c4ac817 ci: add Linux tests and AppImage build`

- [x] Added player quality-of-life controls:
  - Configurable Vim mode with `V` toggle.
  - `?` help overlay.
  - Expanded CLI options: `--version`, `--config`, `--no-visualizer`, `--no-preview`, `--vim`, and `--no-vim`.
  - Custom config-file propagation through the player.
  - Regression tests for the new behavior.
  - Commits: `743935b feat: add player quality-of-life controls`, `aa52cbd fix: make vim navigation mode exclusive`
- [x] Completed the TUI visual redesign.
  - Commit: `1d907d0 feat: redesign player dashboard`
- [x] Cached fuzzy-search results and vectorized spectrum smoothing.
  - Commit: `0dcf3d3 perf: cache searches and vectorize spectrum smoothing`
- [x] Added Nerd Font dashboard icons and removed verbose dashboard labels.
  - Commit: `5626da5 feat: use Nerd Font icons in dashboard`
- [x] Ran the complete test, Ruff, and mypy checks after the final edits.
  - Result: 83 tests and 9 subtests passed; Ruff and mypy passed.
- [x] Published current installation, usage, contribution, licensing, release,
  and third-party documentation.
- [x] Added MPRIS support so the KDE lock screen, media keys, and `playerctl` can show and control MPF.
  - In-app D-Bus service (`dbus-fast`), since mpv has no built-in MPRIS and MPF owns the queue.
  - Publishes the cached local thumbnail as `file://` art; remote art did not show on the lock screen.
  - Commits: `e4dec48 feat: add MPRIS support for lock screen and media keys`, `734b44d fix: use local thumbnail for MPRIS art`
- [x] Fixed the render-signature test, which depended on the user's saved visualizer style.
- [x] Added `mpf_core.mpris` to the AppImage build and smoke-tested the frozen binary:
  `dbus-fast` bundles correctly and the binary registers on the session bus.
- [x] Made shuffle a reversible toggle instead of a destructive reorder; MPRIS exposes it as `Shuffle`.
  - Commit: `fc702fe feat: make shuffle a reversible toggle`

## Notes

- The AppImage bundles MPF and Python dependencies but continues to require `mpv` as a Linux system dependency.
- Project documentation reflects the current TUI, controls, packaging, and
  development workflow.
