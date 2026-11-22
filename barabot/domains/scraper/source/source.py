from typing_extensions import Protocol


class Source(Protocol):
    def set_listener(self):
        ...
