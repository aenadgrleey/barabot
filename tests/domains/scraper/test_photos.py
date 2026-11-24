import asyncio
from pathlib import Path
from types import SimpleNamespace

from telethon.tl import types

from barabot.domains.processor.matching import JEV_IMAGES as SPEC
from barabot.domains.scraper.photos import PhotoDownloader


def size(type_, w, h):
    return types.PhotoSize(type=type_, w=w, h=h, size=1)


def photo(*sizes, photo_id=777):
    return types.Photo(id=photo_id, access_hash=1, file_reference=b"", date=None, sizes=list(sizes), dc_id=2)


def downloader(media_dir=Path("unused")):
    return PhotoDownloader(SPEC, media_dir)


def test_picks_the_smallest_size_that_is_good_enough():
    p = photo(size("s", 90, 67), size("m", 320, 240), size("x", 800, 600), size("y", 1280, 960))
    assert downloader().pick_size(p).type == "x"


def test_no_size_fits_when_the_photo_is_a_thin_strip():
    assert downloader().pick_size(photo(size("m", 320, 100), size("x", 800, 250))) is None


def test_stripped_sizes_without_dimensions_are_skipped():
    p = photo(types.PhotoStrippedSize(type="i", bytes=b"x"), size("x", 800, 600))
    assert downloader().pick_size(p).type == "x"


class FakeClient:
    def __init__(self):
        self.downloads = []

    async def download_media(self, message, file, thumb):
        self.downloads.append(thumb.type)
        Path(file).write_bytes(b"jpeg")
        return file


def message(p):
    return SimpleNamespace(media=types.MessageMediaPhoto(photo=p))


def test_downloads_once_and_describes_the_file(tmp_path):
    client = FakeClient()
    msg = message(photo(size("x", 800, 600)))
    first = asyncio.run(downloader(tmp_path).download(client, msg))
    second = asyncio.run(downloader(tmp_path).download(client, msg))
    assert first == second == {"file": "777-x.jpg", "w": 800, "h": 600}
    assert client.downloads == ["x"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["777-x.jpg"]


def test_non_photo_media_is_ignored(tmp_path):
    msg = SimpleNamespace(media=types.MessageMediaWebPage(webpage=types.WebPageEmpty(id=1)))
    assert asyncio.run(downloader(tmp_path).download(FakeClient(), msg)) is None
