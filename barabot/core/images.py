"""What images a decider accepts, shared by the scraper and processing.

The decider (processing) owns the numbers; the scraper only follows them
when it picks which Telegram photo size to download. Neither side knows
which model is behind the decider.

The goal is the cheapest image that is good enough, not the largest one
allowed: image tokens grow with pixels. `target_long_side` and
`max_images` are the knobs to experiment with.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class ImageSpec:
    min_side: int  # hard limits: an image outside them is rejected
    max_side: int
    min_pixels: int
    max_pixels: int
    target_long_side: int  # enough to read an ad; prefer the smallest size that reaches it
    max_images: int  # photos sent per request

    def accepts(self, w: int, h: int) -> bool:
        return (
            self.min_side <= min(w, h)
            and max(w, h) <= self.max_side
            and self.min_pixels <= w * h <= self.max_pixels
        )

    def pick(self, sizes: Sequence[tuple[int, int]]) -> int | None:
        """Index of the size to use: the smallest accepted one that reaches the
        target, or the largest accepted one if none does. None if none is accepted."""
        accepted = [(w * h, i) for i, (w, h) in enumerate(sizes) if self.accepts(w, h)]
        if not accepted:
            return None
        enough = [(area, i) for area, i in accepted if max(sizes[i]) >= self.target_long_side]
        return min(enough)[1] if enough else max(accepted)[1]
