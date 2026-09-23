# 🎵 MPF

### YouTube playlists, right in your terminal.

MPF is a small Linux music player for public YouTube playlists and videos. It
combines `mpv`, `yt-dlp`, fuzzy search, Kitty thumbnails, and a colorful audio
visualizer in a Blessed TUI.

> 🤖 Built with some AI help — expect rough edges and please report bugs.

## ✨ Features

- 🎶 Play public YouTube playlists and videos with `mpv`.
- 🔎 Fuzzy queue search, shuffle, repeat, seeking, and volume controls.
- 💾 Cached playlists with background refresh and offline fallback.
- 🌈 Four visualizer styles: waterfall, bars, braille, and waveform.
- 🖱️ Mouse navigation, keyboard controls, and an in-app `?` help screen.
- 🧭 Optional Vim navigation with `j`/`k` browsing and `h`/`l` seeking.
- 🗂️ XDG-friendly configuration, cache, thumbnail, and log paths.
- 📦 Linux x86-64 AppImage builds through GitHub Actions.

The dashboard looks best with a Nerd Font. MPF still works without one, but
some icons may show up as empty boxes.

## 🧰 Requirements

- Linux
- Python 3.14 for source installations
- `mpv` installed and available on `PATH`
- A public `youtube.com` or `youtu.be` playlist or video URL

Optional extras:

- 🖼️ Kitty enables thumbnail previews.
- 📊 `pw-record` (PipeWire) or `parec` (PulseAudio) enables the visualizer.

MPF starts without the optional tools. Playback remains available when
thumbnails or the visualizer are unavailable.

## 🚀 Install from source

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python mpf.py 'https://www.youtube.com/playlist?list=PLAYLIST_ID'
```

Run MPF without a URL to load your saved default playlist, or press `o` to
open a different one.

## 📦 AppImage

CI builds an AppImage for every push. Non-release builds are available as
temporary workflow artifacts. Release tags such as `v1.0.0` create a GitHub
Release with the versioned AppImage attached and notes generated from commits
since the previous release.

To build locally on Linux, install `appimagetool`, create the development
environment above, then run:

```bash
APPIMAGETOOL=/path/to/appimagetool bash scripts/build_appimage.sh
./dist/mpf-0.1.0-x86_64.AppImage --help
```

Set `MPF_VERSION`, `PYTHON_BIN`, or `OUTPUT` to override the build defaults.

## ⌨️ Command line

```text
mpf.py [--no-auto-play] [--config PATH] [--no-visualizer] [--no-preview]
       [--vim | --no-vim] [--version] [url]
```

Run `mpf.py --help` for the full option list.

## ⚙️ Configuration and data

Configuration defaults to `$XDG_CONFIG_HOME/mpf/config.json` or
`~/.config/mpf/config.json`:

```json
{
  "default_playlist": "https://www.youtube.com/playlist?list=PLAYLIST_ID",
  "visualizer_style": "waterfall",
  "show_visualizer": true,
  "vim_mode": true
}
```

Loading a URL saves it as the default playlist. The URL argument takes
precedence. Cache files, thumbnails, playback state, and logs use
`$XDG_CACHE_HOME/mpf` or `~/.cache/mpf`. Use `--config PATH` to select another
configuration file.

## 🎮 Controls

Press `?` in MPF for the full in-app reference.

| Control | Action |
| --- | --- |
| `q` | Quit |
| `Space` | Pause or resume |
| `n` / `p` | Next or previous track |
| `s` | Shuffle queue |
| `r` | Cycle repeat mode |
| `[` / `]` | Seek 30 seconds |
| `+` / `-` | Change volume |
| `m` | Mute |
| `/` or `f` | Fuzzy search |
| `o` | Load a YouTube URL |
| `v` | Toggle visualizer |
| `a` | Cycle visualizer style |
| `t` | Toggle thumbnail preview |
| `V` | Toggle Vim navigation mode |
| Mouse wheel, click, or Enter | Select and play a track |

Vim mode uses `j`/`k` to browse and `h`/`l` to seek 5 seconds. Standard mode
uses the arrow keys instead. The two navigation modes are intentionally
exclusive.

## 🩹 Troubleshooting

- **MPV startup failed** — install `mpv` and check that it is on `PATH`.
- **Unable to retrieve URL** — make sure the URL is public, then update
  `yt-dlp`.
- **No visualizer** — install PipeWire's `pw-record` or PulseAudio's `parec`.
- **No thumbnail** — run under Kitty and check access to YouTube thumbnail
  hosts.
- **Missing dashboard icons** — select a Nerd Font in your terminal.
- **Reset local state** — remove `$XDG_CACHE_HOME/mpf` or
  `~/.cache/mpf`.

Logs live in the MPF cache directory and may have extra details when something
goes sideways.

## 🧪 Development

```bash
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy mpf.py mpf_core
```

More details are in [CONTRIBUTING.md](CONTRIBUTING.md),
[CHANGELOG.md](CHANGELOG.md),
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), and [LICENSE](LICENSE).
