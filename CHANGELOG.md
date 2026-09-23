# Changelog

## [1.0.2](https://github.com/raluvy95/mpf/compare/v1.0.1...v1.0.2) (2026-09-23)


### Bug Fixes

* publish AppImage releases ([b331607](https://github.com/raluvy95/mpf/commit/b33160794c919b2e4f1093a3ffc70482b7326fb0))

## [1.0.1](https://github.com/raluvy95/mpf/compare/v1.0.0...v1.0.1) (2026-09-23)


### Documentation

* refresh README tone and presentation ([3fcafc2](https://github.com/raluvy95/mpf/commit/3fcafc2186733eed6fce337ead91013894019929))

## Changelog

All notable changes are documented here. This project follows Semantic
Versioning and uses Conventional Commits.

## Unreleased

### Added

- Initial Blessed TUI for public YouTube playlist and video playback through
  mpv and yt-dlp.
- Fuzzy queue search, shuffle and repeat modes, mouse navigation, seeking,
  volume and mute controls, thumbnail previews, and four spectrum styles.
- Persistent playback state, visualizer preferences, default playlist, and
  configurable Vim navigation mode.
- In-app `?` help overlay and expanded CLI options for configuration, playback,
  preview, visualizer, and Vim-mode overrides.
- System sleep inhibition while audio is playing.
- Linux x86-64 AppImage packaging and GitHub Actions test/build workflows.
- Nerd Font dashboard icons and compact dashboard labels.
- Source installation, contribution, licensing, and third-party documentation.

### Changed

- Redesigned the player dashboard with progress, artwork, spectrum, queue, and
  responsive command sections.
- Moved configuration, caches, thumbnails, playback state, and logs to XDG
  locations.
- Made Vim and arrow-key navigation modes exclusive.
- Removed the embedded personal playlist default.

### Fixed

- Preserved active track identity during stale playlist refreshes and retained
  cached queues during network outages.
- Made cache writes atomic and safe across concurrent updates.
- Stabilized thumbnail cleanup, optional process shutdown, resume seeking,
  unavailable-track handling, and sleep-inhibitor release.
- Isolated tests from persisted local Vim preferences.

### Performance

- Cached fuzzy-search results until the queue or query changes.
- Vectorized spectrum smoothing and capped visualizer redraws.
- Avoided unnecessary static dashboard renders and thumbnail downloads.
