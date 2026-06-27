"""Cosine similarity. Inline numpy-free impl — slop doesn't need the dep."""
from __future__ import annotations

import math


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        # Different embedding dimensions = definitively not similar.
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def best_match(query: list[float], candidates: list[tuple[int, str, list[float]]]) -> tuple[int | None, float]:
    """Return (msg_id, score) of the closest known item, or (None, 0.0) when empty."""
    if not candidates:
        return None, 0.0
    best_id = None
    best_score = -1.0
    for mid, _txt, vec in candidates:
        s = cosine(query, vec)
        if s > best_score:
            best_score = s
            best_id = mid
    return best_id, best_score
