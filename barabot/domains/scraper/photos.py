"""Download listing photos so processing can show them to its decider.

Only the scraper holds a Telegram connection, so photos are fetched at
ingest and stored under the media directory; processing reads the files
later.

Which sizes are worth downloading depends on the model that will look at
them. The scraper does not know that model: the composition root
(`barabot.__main__`) builds the PhotoDownloader with the decider's
ImageSpec, next to the decider itself.
"""

from __future__ import annotations

import logging
from pathlib import Path

from telethon.tl import types

from barabot.core.images import ImageSpec

log = logging.getLogger(__name__)


class PhotoDownloader:
    """Downloads the smallest Telegram size that is good enough for `spec`."""

    def __init__(self, spec: ImageSpec, media_dir: Path) -> None:
        self._spec = spec
        self._media_dir = media_dir

    def pick_size(
        self, photo: types.Photo
    ) -> types.PhotoSize | types.PhotoCachedSize | types.PhotoSizeProgressive | None:
        """The Telegram photo size to download, or None to skip the photo."""
        # Stripped, path and empty sizes carry no dimensions and are never candidates.
        sized = [
            s
            for s in photo.sizes
            if isinstance(s, (types.PhotoSize, types.PhotoCachedSize, types.PhotoSizeProgressive)) and s.w and s.h
        ]
        index = self._spec.pick([(s.w, s.h) for s in sized])
        return None if index is None else sized[index]

    async def download(self, client, message) -> dict | None:
        """Fetch the photo of `message` once; return what to add to its media descriptor."""
        media = message.media
        if not isinstance(media, types.MessageMediaPhoto) or not isinstance(media.photo, types.Photo):
            return None
        size = self.pick_size(media.photo)
        if size is None:
            log.info("Photo %s has no size within the image spec", media.photo.id)
            return None
        name = f"{media.photo.id}-{size.type}.jpg"
        path = self._media_dir / name
        if not path.exists():
            self._media_dir.mkdir(parents=True, exist_ok=True)
            part = self._media_dir / f"{name}.part"
            saved = await client.download_media(message, file=str(part), thumb=size)
            if not saved:
                return None
            Path(saved).replace(path)
        return {"file": name, "w": size.w, "h": size.h}
