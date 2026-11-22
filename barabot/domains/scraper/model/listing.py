from typing import Protocol


class ListingSource(Protocol): ...


class TgListingSource(ListingSource):
    chat_id: int
    msg_id: int
    owner_id: int

class ListingContent:
    content: str
    image_urls: [HttpUrl]
