"""Processor domain — embed + dedup + outbox pipeline.

Will consume items from the scraper, compute embeddings, decide
NEW/DUP, and write outbox events for the bot domain. Today only a
stub service is wired; the real pipeline lands in a follow-up commit.
"""
from barabot.domains.processor.service import ProcessorService

__all__ = ["ProcessorService"]