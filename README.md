# MPF

MPF is a Linux terminal player for public YouTube playlists and videos. It
combines mpv playback, yt-dlp extraction, fuzzy queue search, Kitty thumbnail
previews, and a PipeWire or PulseAudio spectrum display in a Blessed TUI.

## Features

- Persistent playlist, playback position, volume, repeat mode, visualizer, and
  Vim-mode preferences.
- Cached playlists with background refresh and offline fallback.
- Four spectrum styles: waterfall, bars, braille, and waveform.
- Mouse and keyboard queue navigation, fuzzy search, shuffle, repeat, seeking,
  volume controls, and an in-app help overlay.
- XDG-compliant configuration, cache, thumbnail, and log paths.
- Linux x86-64 AppImage packaging and GitHub Actions CI.

The dashboard uses Nerd Font icons. A Nerd Font is recommended; playback still
works without one, but unsupported icons may appear as empty boxes.

## Requirements

- Linux
- Python 3.14 for source installations
- `mpv` installed and available on `PATH`
- A public `youtube.com` or `youtu.be` playlist/video URL

Optional integrations:

- Kitty enables thumbnail previews.
- `pw-record` (PipeWire) or `parec` (PulseAudio) enables the spectrum display.

MPF starts without optional tools. Playback remains available when preview or
spectrum integration is unavailable.

## Source Installation

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python mpf.py 'https://www.youtube.com/playlist?list=PLAYLIST_ID'
```

Run without a URL to load the saved default or open one with `o`.

## AppImage

CI builds `dist/mpf-0.1.0-x86_64.AppImage` as an artifact. The AppImage bundles
MPF and its Python dependencies, but `mpv` remains a system dependency.

To build locally on Linux, install `appimagetool`, create the development
environment above, then run:

```bash
APPIMAGETOOL=/path/to/appimagetool bash scripts/build_appimage.sh
./dist/mpf-0.1.0-x86_64.AppImage --help
```

Set `MPF_VERSION`, `PYTHON_BIN`, or `OUTPUT` to override build defaults.

## Command Line

```text
mpf.py [--no-auto-play] [--config PATH] [--no-visualizer] [--no-preview]
       [--vim | --no-vim] [--version] [url]
```

Use `mpf.py --help` for descriptions of every option.

## Configuration and Data

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

Loading a URL saves it as the default. The URL argument takes precedence.
Cache files, thumbnails, playback state, and logs use `$XDG_CACHE_HOME/mpf` or
`~/.cache/mpf`. `--config PATH` selects another configuration file.

## Controls

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
| `v` | Toggle spectrum |
| `a` | Cycle spectrum style |
| `t` | Toggle thumbnail preview |
| `V` | Toggle Vim navigation mode |
| Mouse wheel, click, or Enter | Select and play a track |

Vim mode uses `j`/`k` to browse and `h`/`l` to seek 5 seconds. Standard mode
uses arrow keys instead. These navigation modes are intentionally exclusive.

## Troubleshooting

- **MPV startup failed**: install `mpv` and verify it is on `PATH`.
- **Unable to retrieve URL**: verify the URL is public, then update yt-dlp.
- **No spectrum**: install PipeWire's `pw-record` or PulseAudio's `parec`.
- **No thumbnail**: run under Kitty and verify access to YouTube thumbnail hosts.
- **Square or missing dashboard icons**: select a Nerd Font in the terminal.
- **Reset local state**: remove `$XDG_CACHE_HOME/mpf` or `~/.cache/mpf`.

Logs are written to the MPF cache directory and can provide additional error
details.

## Development

```bash
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy mpf.py mpf_core
```

See [CONTRIBUTING.md](CONTRIBUTING.md), [CHANGELOG.md](CHANGELOG.md),
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), and [LICENSE](LICENSE).
