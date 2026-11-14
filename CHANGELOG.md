# Changelog

## [1.3.0](https://github.com/raluvy95/mpf/compare/v1.2.0...v1.3.0) (2026-11-14)


### Features

* adapt read-ahead to stalls and prefetch the next track ([7df4a00](https://github.com/raluvy95/mpf/commit/7df4a000accb3f250d9f0f41d1a059b709405fce))
* add --force-cache to fetch a playlist again and replace its cache ([6fec1ab](https://github.com/raluvy95/mpf/commit/6fec1abf13406419adeddb08d9a688727793abab))
* add a background mode with a headless player controlled over a unix socket ([470d1ce](https://github.com/raluvy95/mpf/commit/470d1ced71600dd6ac630280c8fd95f426c40bed))
* add an open dialog view setting (list or thumbnails) ([f1fd9e5](https://github.com/raluvy95/mpf/commit/f1fd9e506c71794eb0e2bc789b9568b07de52b34))
* add control subcommands (pause, next, volume, seek, status) ([cbaf2bc](https://github.com/raluvy95/mpf/commit/cbaf2bcc7b6f25b6a819737f47e2e172a2b90111))
* add the client/daemon wire protocol ([696a75f](https://github.com/raluvy95/mpf/commit/696a75fee66f64fb2a9563668cbef56846e5c376))
* colour the control commands in --help like the rest of it ([3bfff33](https://github.com/raluvy95/mpf/commit/3bfff33ca798df606d39d0afb76543eb177a1c88))
* navigate settings with arrows and change rows with Enter or Space ([68836de](https://github.com/raluvy95/mpf/commit/68836dea22bd1c7e2cafddec17a098d7421f4798))
* redesign the open dialog as a full screen with saved playlists ([5e13e19](https://github.com/raluvy95/mpf/commit/5e13e1971ba6925269a592f5e9ce9b84a441080b))
* replace the quit-time hand-off with a resident playback daemon ([bbc3170](https://github.com/raluvy95/mpf/commit/bbc317017290c9a1758e6dab168b828684218a83))
* run the TUI as a client of the playback daemon ([e6308d1](https://github.com/raluvy95/mpf/commit/e6308d1159b08a2aad376690be8e7871c2f17843))
* save playlist titles and list the saved playlists ([83c8d4b](https://github.com/raluvy95/mpf/commit/83c8d4b662bec487f71afb0549192ab287ad6ad3))
* serve the playback engine over the daemon socket ([e03d52d](https://github.com/raluvy95/mpf/commit/e03d52d3812b8419b5ee792c7f70c0e20ec6306c))
* show saved playlists as a thumbnail grid in the open dialog ([69c6b4a](https://github.com/raluvy95/mpf/commit/69c6b4a1644d8d0f294e2337cdb2388dd41bdf7f))
* take the playing mpv back from the background player when the TUI returns ([b5fd9d3](https://github.com/raluvy95/mpf/commit/b5fd9d37681accfc9f0678c227f4fc0118aeb7d4))


### Bug Fixes

* blank the margin columns in the open dialog ([9c57e8e](https://github.com/raluvy95/mpf/commit/9c57e8e43b66b1b306337674a5ae20fa6dd83df8))
* hand the playing mpv to the background player instead of restarting it ([22f1736](https://github.com/raluvy95/mpf/commit/22f1736e56a119eee36f5b72744fa205a67a3d6c))
* keep "|" and "-" apart in the seek help so fonts do not join them ([a27dbd5](https://github.com/raluvy95/mpf/commit/a27dbd5f9d0866ea6af5fa20f99f2670ff9f54d6))
* keep help and settings drawn from column 2 ([ca62f8c](https://github.com/raluvy95/mpf/commit/ca62f8ce301eda79013386e6d2b19dd46db339da))
* keep the visualizer's autosens ramp from restarting every track ([4f1c4b8](https://github.com/raluvy95/mpf/commit/4f1c4b8e062faa4fe1d3256a5e58acd6ff097f6c))
* look up missing playlist titles when the open dialog opens ([27aea3a](https://github.com/raluvy95/mpf/commit/27aea3af0151a537fb2cd1d3e27ccba7594f27cb))
* never let a thumbnail download hold up quitting ([672239a](https://github.com/raluvy95/mpf/commit/672239a2cf579e5d61599c1beb200032bf0c62fd))
* never let extractor output choose the URL mpv opens ([f5b8cf6](https://github.com/raluvy95/mpf/commit/f5b8cf6841ff5a6f03de0058eb6fa67094b143df))
* scroll the help and settings screens on short terminals ([54d2c76](https://github.com/raluvy95/mpf/commit/54d2c767d28c5cba0636885c7f4c71d2f4044b16))
* tell the user to run mpf.py quit when an older background player is running ([4e11cad](https://github.com/raluvy95/mpf/commit/4e11cad238bec6f10c569fe6c3b90410a893eb7c))
* validate and clean saved playlists read from the cache ([e9e7bed](https://github.com/raluvy95/mpf/commit/e9e7bed5e679d094e6053512cabc27c2f4509f66))


### Performance Improvements

* draw thumbnail grid tiles as they arrive instead of redrawing all ([5d9037a](https://github.com/raluvy95/mpf/commit/5d9037a5d3a1fa5d16afcdbc44df8aafa03e9048))
* make the control commands start fast ([d25c2f4](https://github.com/raluvy95/mpf/commit/d25c2f4adeaaff1788e44d3f13d2ffcec6e01fce))


### Documentation

* describe the redesigned open dialog and the open_view setting ([c9096a8](https://github.com/raluvy95/mpf/commit/c9096a83f7e6b5dabe1a92a86d75d5a025d79893))
* design a client/daemon split so quitting the TUI is instant ([458e35a](https://github.com/raluvy95/mpf/commit/458e35ada27794ce16455df99bea78f828a4bac5))
* keep the daemon a lightweight layer between mpv and the clients ([4baf2fd](https://github.com/raluvy95/mpf/commit/4baf2fdfdaa9060415bb6d0a4fd02337d855c897))
* require a commit for every change and undo via git ([98cb7ff](https://github.com/raluvy95/mpf/commit/98cb7ff1851021e08822db22aff7d3b879106177))

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
