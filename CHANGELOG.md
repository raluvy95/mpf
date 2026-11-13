# Changelog

## [1.2.0](https://github.com/raluvy95/mpf/compare/v1.1.0...v1.2.0) (2026-11-13)


### Features

* add a settings screen and more visualizer styles ([252333b](https://github.com/raluvy95/mpf/commit/252333bb064a8987ce0a123e0de6c83e4748536e))
* add MPRIS support for lock screen and media keys ([e4dec48](https://github.com/raluvy95/mpf/commit/e4dec48296611e1d0d68862576487967203cc6ae))
* colorize the help screen ([229092d](https://github.com/raluvy95/mpf/commit/229092d8514f6c099b1fabec54a004b3d6fe3118))
* draw keybinds as boxed keys in the help screen and footer ([44b7816](https://github.com/raluvy95/mpf/commit/44b7816381ed84e0f4b68851d943db96fd9ce4ef))
* let the cursor move within text prompts ([ef1c6c3](https://github.com/raluvy95/mpf/commit/ef1c6c3ab0cffb9e6e1ec4958d745a852a769b85))
* list playlists with the system yt-dlp and stop bundling it ([0c2fa8f](https://github.com/raluvy95/mpf/commit/0c2fa8fa7acc1b3f1d4d0cfab9dd05e5b033e5b2))
* make shuffle a reversible toggle ([fc702fe](https://github.com/raluvy95/mpf/commit/fc702feeb396083da0fbcf18f1c2c7e060bc7cf7))
* parse artist and title from YouTube metadata ([02f8651](https://github.com/raluvy95/mpf/commit/02f86514ad9c2023fe9fc0f1db2614fcaeea23f7))
* **visualizer:** cover 20 Hz-20 kHz and keep typical music level ([0a65ee7](https://github.com/raluvy95/mpf/commit/0a65ee7531e1cfe60054c94a2e268453e920ba7f))
* **visualizer:** follow the music instead of flattening it ([7fd1bca](https://github.com/raluvy95/mpf/commit/7fd1bcaa61f6cf4e3c504b56a029d94ab75d669f))
* **visualizer:** port cava's spectrum algorithm ([1e336a0](https://github.com/raluvy95/mpf/commit/1e336a01dfa47877f9554a1fa445303a4177d119))


### Bug Fixes

* cap cache, state, and config file sizes ([7706a86](https://github.com/raluvy95/mpf/commit/7706a868ba49cbbf3bd8f9a84035a5f35bec3b3e))
* keep colors in the help screen ([6ac3225](https://github.com/raluvy95/mpf/commit/6ac3225dc6f38692124f720e1a0a96b78aa2830d))
* let V toggle Vim mode while the help is open ([fc93ffe](https://github.com/raluvy95/mpf/commit/fc93ffeb97c30f40c4f176c439e336a674631220))
* make the help title readable on any terminal theme ([f8743f9](https://github.com/raluvy95/mpf/commit/f8743f91bea3e037157c0cc11e3e4e094467ad53))
* redact stream URLs from mpv errors and harden the log ([7314511](https://github.com/raluvy95/mpf/commit/73145113d79646b2ff397a758247ac7bb42de64e))
* redact URLs of any scheme and sanitize tracebacks in the log ([963e9ed](https://github.com/raluvy95/mpf/commit/963e9ed6bf9397865f2fcd2531dec0e0cbe42e89))
* retry failed YouTube tracks before skipping them ([2ba73e5](https://github.com/raluvy95/mpf/commit/2ba73e5a385c7303bb5aec94dd35a5317f6daca0))
* show an error under the open prompt for invalid URLs ([5735743](https://github.com/raluvy95/mpf/commit/5735743ebe23f622c6f37bf9a0ab454f8ae8c325))
* small hardening for mpv, yt-dlp, thumbnails, and parec ([286742f](https://github.com/raluvy95/mpf/commit/286742f2c57dd971c7eb0016836fd0445afcaad9))
* strip control characters from track text ([caa42e4](https://github.com/raluvy95/mpf/commit/caa42e479697bfde3d959d3f5ceac66704ebf4e4))
* use local thumbnail for MPRIS art ([734b44d](https://github.com/raluvy95/mpf/commit/734b44ddc45f134f42988976edb4f2660e375ec6))
* **visualizer:** read raw PCM from pw-record and rebuild the analyzer ([310b235](https://github.com/raluvy95/mpf/commit/310b235a8ec313489d43fc96ec026f5941ad692d))


### Performance Improvements

* speed up fuzzy matching and fix a crash on İ in titles ([496f31e](https://github.com/raluvy95/mpf/commit/496f31e4ee7ee4cb80e13f3cb4cc3a219129e565))


### Documentation

* add a screenshot to the README ([5a176d7](https://github.com/raluvy95/mpf/commit/5a176d777fc4a0e5507b414a0b6f37ec860de596))
* record MPRIS support in TODO ([89a8223](https://github.com/raluvy95/mpf/commit/89a8223b6f422c379d5122252b83874b5823ee63))
* record shuffle toggle in TODO ([d84ea18](https://github.com/raluvy95/mpf/commit/d84ea18074d496226f5dad2fdb034a0f54f86d4f))
* record test fix, AppImage check, and MPRIS shuffle note ([6ceb765](https://github.com/raluvy95/mpf/commit/6ceb7656c5becd02033b382f2c937fd6362b2108))
* record visualizer rewrite in TODO ([be7005e](https://github.com/raluvy95/mpf/commit/be7005e6162fdb68561d320bc83fd4ad1e3fd151))
* remove TODO.md and update agent instructions ([50293f0](https://github.com/raluvy95/mpf/commit/50293f05b79b9215dad857094fe1d9f4066b2b45))

## [1.1.0](https://github.com/raluvy95/mpf/compare/v1.0.3...v1.1.0) (2026-10-24)


### Features

* add progress bar seeking and freeze the visualizer on pause ([9420eb1](https://github.com/raluvy95/mpf/commit/9420eb10e3bc28926f97a510c0076d9a2bd3a521))
* simplify the dashboard layout and fix idle buffering state ([6ec79db](https://github.com/raluvy95/mpf/commit/6ec79db7c04b29e93e1ef25b42d4f843e04c7d1f))


### Bug Fixes

* keep search separate from the main playlist ([d42e0b1](https://github.com/raluvy95/mpf/commit/d42e0b17b030976c02bf7e28ba1fe3244a3bd5e4))
* preserve invalid config, unblock UI I/O, and harden release pipeline ([da939da](https://github.com/raluvy95/mpf/commit/da939da71f645999540669ed80e8e071394f0dfe))


### Performance Improvements

* cut playback CPU by about 60% and speed up startup ([d748e59](https://github.com/raluvy95/mpf/commit/d748e594f303b8779823e0a045306dd117c35ab9))


### Documentation

* base next version on the latest existing tag ([4002a10](https://github.com/raluvy95/mpf/commit/4002a10adaece671c09bc055c71bc583be9431c4))

## [1.0.3](https://github.com/raluvy95/mpf/compare/v1.0.2...v1.0.3) (2026-09-23)


### Bug Fixes

* isolate system mpv from AppImage libraries ([c254c8c](https://github.com/raluvy95/mpf/commit/c254c8cd387ee6f0680e2e7ccf9c128de399105b))


### Documentation

* document manual GitHub push policy ([1e47095](https://github.com/raluvy95/mpf/commit/1e47095f897bdd1e15783a49b1f4326cbbdfd5a4))
* require pulling before commits ([3ca794b](https://github.com/raluvy95/mpf/commit/3ca794b2e280658277dde1216429dd4a30cef8a2))

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
