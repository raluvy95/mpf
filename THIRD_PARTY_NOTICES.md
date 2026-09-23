# Third-Party Notices

MPF relies on the following third-party projects when installed from source:

- [Blessed](https://github.com/jquast/blessed)
- [msgpack-python](https://github.com/msgpack/msgpack-python)
- [NumPy](https://numpy.org/)
- [python-mpv-jsonipc](https://github.com/iwalton3/python-mpv-jsonipc)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [zstandard](https://github.com/indygreg/python-zstandard)

Development and AppImage builds additionally use PyInstaller, pytest, Ruff,
mypy, and appimagetool. MPF displays Nerd Font glyph code points but does not
bundle or redistribute a font.

Each dependency is distributed under its own license. Source and binary
distributions must retain the notices and satisfy the licenses shipped by
their installed dependency versions. `mpv` is a separate system dependency;
consult its distribution package for its license notices.
