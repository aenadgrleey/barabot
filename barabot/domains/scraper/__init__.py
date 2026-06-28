"""Scraper domain — Telegram-side ingest.

Will own the MTProto client and push events into the processor. Today
only a stub service is wired; the real listener lands in a follow-up
commit.
"""
from barabot.domains.scraper.service import ScraperService

__all__ = ["ScraperService"]