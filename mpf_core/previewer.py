"""Kitty icat real pixel image rendering with Adwaita audio icon fallback."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from typing import Optional, Tuple

from mpf_core.models import Track
from mpf_core.paths import THUMBS_CACHE_DIR

DEFAULT_AUDIO_ICON_PATH = "/usr/share/icons/Adwaita/scalable/mimetypes/audio-x-generic.svg"
THUMBNAIL_MAX_AGE_SECONDS = 30 * 24 * 3600
THUMBNAIL_CACHE_MAX_BYTES = 256 * 1024 * 1024
THUMBNAIL_DOWNLOAD_MAX_BYTES = 10 * 1024 * 1024

logger = logging.getLogger(__name__)


class KittyPreviewer:
    """Renders real pixel images using Kitty icat graphics protocol."""

    def __init__(
        self,
        thumbs_dir: str = THUMBS_CACHE_DIR,
        fallback_icon: str = DEFAULT_AUDIO_ICON_PATH,
        max_age_seconds: float = THUMBNAIL_MAX_AGE_SECONDS,
        max_cache_bytes: int = THUMBNAIL_CACHE_MAX_BYTES,
    ) -> None:
        self.thumbs_dir = thumbs_dir
        self.fallback_icon = fallback_icon
        self.has_kitty = shutil.which("kitty") is not None
        self._current_rendered: Optional[Tuple[str, int, int, int, int]] = None
        self.max_age_seconds = max_age_seconds
        self.max_cache_bytes = max_cache_bytes
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mpf-thumbnail")
        self._pending: dict[str, Future[None]] = {}
        self._lock = threading.Lock()
        self.generation = 0
        os.makedirs(self.thumbs_dir, exist_ok=True)
        self.expire_cache()

    def _thumbnail_path(self, track: Track) -> str:
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", track.id)[:128] or "thumbnail"
        return os.path.join(self.thumbs_dir, f"{safe_id}.jpg")

    def get_cached_image_path(self, track: Optional[Track]) -> str:
        """Return an existing thumbnail without performing network I/O."""
        if track:
            local_path = self._thumbnail_path(track)
            try:
                if os.path.getsize(local_path) > 0:
                    if time.time() - os.path.getmtime(local_path) > 3600:
                        os.utime(local_path, None)
                    return local_path
            except OSError:
                pass
        return self.fallback_icon if os.path.exists(self.fallback_icon) else ""

    def get_local_image_path(self, track: Optional[Track]) -> str:
        """Compatibility wrapper that downloads synchronously outside rendering."""
        if not track:
            return self.get_cached_image_path(None)
        self._download(track)
        return self.get_cached_image_path(track)

    def request(self, track: Optional[Track]) -> None:
        """Schedule a thumbnail download, deduplicating concurrent requests."""
        if not track:
            return
        try:
            if os.path.getsize(self._thumbnail_path(track)) > 0:
                return
        except OSError:
            pass
        key = track.id
        with self._lock:
            if key in self._pending:
                return
            future = self._executor.submit(self._download, track)
            self._pending[key] = future
        future.add_done_callback(partial(self._download_finished, key))

    def _download_finished(self, track_id: str, _future: Future[None]) -> None:
        with self._lock:
            self._pending.pop(track_id, None)
            self.generation += 1

    def _download(self, track: Track) -> None:
        local_path = self._thumbnail_path(track)
        try:
            if os.path.getsize(local_path) > 0:
                return
        except OSError:
            pass

        fd, tmp_path = tempfile.mkstemp(prefix=".thumb-", dir=self.thumbs_dir)
        try:
            with urllib.request.urlopen(track.thumbnail, timeout=10) as response, os.fdopen(fd, "wb") as output:
                content_type = response.headers.get_content_type()
                if not content_type.startswith("image/"):
                    raise ValueError(f"unexpected content type {content_type}")
                data = response.read(THUMBNAIL_DOWNLOAD_MAX_BYTES + 1)
                if len(data) > THUMBNAIL_DOWNLOAD_MAX_BYTES:
                    raise ValueError("thumbnail exceeds download size limit")
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(tmp_path, local_path)
            self.expire_cache()
        except (OSError, ValueError) as err:
            try:
                os.close(fd)
            except OSError:
                pass
            logger.warning("Unable to cache thumbnail for %s: %s", track.id, err)
        finally:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass

    def expire_cache(self) -> None:
        """Remove expired thumbnails, then oldest files until under the size cap."""
        now = time.time()
        files = []
        try:
            entries = list(os.scandir(self.thumbs_dir))
        except OSError as err:
            logger.warning("Unable to scan thumbnail cache: %s", err)
            return
        for entry in entries:
            if not entry.is_file() or not entry.name.endswith(".jpg"):
                continue
            try:
                stat = entry.stat()
                if now - stat.st_mtime > self.max_age_seconds:
                    os.unlink(entry.path)
                else:
                    files.append((stat.st_mtime, stat.st_size, entry.path))
            except OSError as err:
                logger.debug("Unable to inspect thumbnail %s: %s", entry.path, err)
        total_size = sum(size for _, size, _ in files)
        for _mtime, size, path in sorted(files):
            if total_size <= self.max_cache_bytes:
                break
            try:
                os.unlink(path)
                total_size -= size
            except OSError as err:
                logger.debug("Unable to expire thumbnail %s: %s", path, err)

    def render(self, track: Optional[Track], x: int, y: int, width: int, height: int) -> None:
        """Render image in terminal cell rect using kitty icat."""
        if not self.has_kitty or width <= 0 or height <= 0:
            return

        img_path = self.get_cached_image_path(track)
        if not img_path or not os.path.exists(img_path):
            self.clear()
            return

        state_key = (img_path, x, y, width, height)
        if self._current_rendered == state_key:
            return

        if self._current_rendered:
            self.clear()
        self._current_rendered = state_key
        try:
            cmd = [
                "kitty",
                "+kitten",
                "icat",
                "--silent",
                "--scale-up",
                f"--place={width}x{height}@{x}x{y}",
                img_path,
            ]
            result = subprocess.run(cmd, stdout=sys.stdout, stderr=subprocess.DEVNULL, check=False)
            if result.returncode:
                logger.warning("Kitty thumbnail renderer exited with status %d", result.returncode)
        except OSError as err:
            logger.warning("Unable to render Kitty thumbnail: %s", err)

    def close(self) -> None:
        """Stop accepting work and cancel thumbnail downloads not yet started."""
        self._executor.shutdown(wait=False, cancel_futures=True)

    def clear(self) -> None:
        """Clear Kitty graphics from terminal screen."""
        self._current_rendered = None
        sys.stdout.write("\x1b_Ga=d,d=a\x1b\\")
        sys.stdout.flush()
