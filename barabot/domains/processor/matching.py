"""Decide which subscriptions a message matches, with TypeSafe Jev.

Every question is about the message alone, never about the subscriber:

- one `noul` per unique author role, AUTHOR_PROMPT. Its optional "false"
  description is the subscription's `author_not`, the mirror role; without
  it the model confuses roles that share a topic, such as landlord and
  tenant. Identical roles of different subscriptions are asked once.
- one `choice` per subscription, QUERY_PROMPT, with FITS / DOES_NOT_FIT /
  UNCLEAR.

Prompt templates are English; the subscription texts inside them stay in
the subscriber's language. English and Russian templates scored the same.

A subscription matches when its author role is at least AUTHOR_MIN and its
query answer is "fits". "unclear" with a passing role means the text was
not enough; the caller may ask again with photos.

The wording comes from the barabot-jev-spike bench in agent-home
.experiments: on 25 messages x 14 subscriptions it made no errors, and the
"false" description was what separated roles.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from barabot.core.images import ImageSpec
from barabot.domains.processor.budget import Budget
from typesafe_sdk import (
    AsyncTypeSafeClient,
    Choice,
    Image,
    Noul,
    TypeSafeAPIConnectionError,
    TypeSafeBadRequestError,
    TypeSafeInternalServerError,
    TypeSafeRateLimitError,
    TypeSafeUnprocessableEntityError,
)

log = logging.getLogger(__name__)

MODEL = "jev-latest"
IMAGES_HEADER = {"TypeSafe-Danger-Zone-Images": "true"}
MAX_QUESTIONS = 400  # Jev limit per request

# Prompts. "{author}" and "{query}" are the subscriber's own words.
AUTHOR_PROMPT = "The message author {author}"
QUERY_PROMPT = "The message fits this request: {query}"

# Options of the query choice, and the description of the unclear one.
FITS = "fits"
DOES_NOT_FIT = "does_not_fit"
UNCLEAR = "unclear"
UNCLEAR_DESCRIPTION = "not enough information to decide"

AUTHOR_MIN = 0.6  # author noul probability needed for a match

# Jev's hard image limits (docs.typesafe.ai/images), and what we choose to send.
# 800 px is the size Telegram keeps as "x"; it measured ~1300 input tokens and
# was enough to read a printed ad. Both choices are open to experiment.
# barabot.__main__ hands this spec to the scraper's photo downloader, so the
# scraper fetches what this decider wants without knowing about it.
JEV_IMAGES = ImageSpec(
    min_side=280,
    max_side=1568,
    min_pixels=78_400,
    max_pixels=2_458_624,
    target_long_side=800,
    max_images=4,
)

TRANSIENT_ERRORS = (TypeSafeRateLimitError, TypeSafeInternalServerError, TypeSafeAPIConnectionError)
REJECTED_IMAGES = (TypeSafeBadRequestError, TypeSafeUnprocessableEntityError)


@dataclass(frozen=True)
class Subscription:
    id: int
    author: str
    author_not: str | None
    query: str


@dataclass(frozen=True)
class Decision:
    subscription_id: int
    author_p: float
    choice: str  # FITS | DOES_NOT_FIT | UNCLEAR
    confidence: float
    round: int  # 1: text only, 2: text and photos

    @property
    def matched(self) -> bool:
        return self.author_p >= AUTHOR_MIN and self.choice == FITS

    @property
    def needs_photos(self) -> bool:
        return self.author_p >= AUTHOR_MIN and self.choice == UNCLEAR


class Decider(Protocol):
    async def decide(
        self, text: str, subscriptions: Sequence[Subscription], photos: Sequence[Path] = ()
    ) -> list[Decision]: ...


def build_questions(subscriptions: Sequence[Subscription]) -> tuple[dict, dict[int, str]]:
    """Jev questions for one request, and the author question each subscription uses."""
    questions: dict = {}
    role_keys: dict[tuple[str, str | None], str] = {}
    author_key: dict[int, str] = {}
    for sub in subscriptions:
        role = (sub.author, sub.author_not)
        if role not in role_keys:
            role_keys[role] = f"author_{len(role_keys)}"
            criteria = {"false": sub.author_not} if sub.author_not else None
            questions[role_keys[role]] = Noul(instructions=AUTHOR_PROMPT.format(author=sub.author), criteria=criteria)
        author_key[sub.id] = role_keys[role]
        questions[f"sub_{sub.id}"] = Choice(
            instructions=QUERY_PROMPT.format(query=sub.query),
            criteria={FITS: None, DOES_NOT_FIT: None, UNCLEAR: UNCLEAR_DESCRIPTION},
        )
    if len(questions) > MAX_QUESTIONS:
        raise ValueError(f"{len(questions)} questions exceed the Jev limit of {MAX_QUESTIONS}")
    return questions, author_key


def read_decisions(answers, subscriptions: Sequence[Subscription], author_key: dict[int, str], round: int) -> list[Decision]:
    decisions = []
    for sub in subscriptions:
        choice = answers.choices[f"sub_{sub.id}"]
        decisions.append(
            Decision(
                subscription_id=sub.id,
                author_p=answers.nouls[author_key[sub.id]].noul,
                choice=choice.choice,
                confidence=choice.confidence,
                round=round,
            )
        )
    return decisions


class JevDecider:
    """One request per call: text only, or text with photos (at most JEV_IMAGES.max_images)."""

    def __init__(self, client: AsyncTypeSafeClient, model: str = MODEL, budget: Budget | None = None) -> None:
        self._client = client
        self._model = model
        self._budget = budget

    async def decide(
        self, text: str, subscriptions: Sequence[Subscription], photos: Sequence[Path] = ()
    ) -> list[Decision]:
        if not subscriptions:
            return []
        questions, author_key = build_questions(subscriptions)
        state: dict = {"text": text}
        headers = None
        photos = list(photos)
        if len(photos) > JEV_IMAGES.max_images:
            # Counted in docs/tech-debt.md: how often albums exceed the limit.
            log.info("Sending %d of %d photos to Jev", JEV_IMAGES.max_images, len(photos))
            photos = photos[: JEV_IMAGES.max_images]
        if photos:
            state["photos"] = [Image(path) for path in photos]
            headers = IMAGES_HEADER
        round = 2 if photos else 1
        if self._budget is not None:
            await self._budget.check_async()  # raises BudgetExhausted
        answers = await self._client.system_one(
            state=state, questions=questions, model=self._model, extra_headers=headers
        )
        usage = answers.usage
        log.info(
            "Jev round %d: %d questions, %d photos, %d input tokens",
            round, len(questions), len(photos), usage.input_tokens,
        )
        if self._budget is not None:
            await self._budget.record_async(
                round=round,
                questions=len(questions),
                photos=len(photos),
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            )
        return read_decisions(answers, subscriptions, author_key, round=round)

    async def aclose(self) -> None:
        await self._client.aclose()


def is_transient(exc: BaseException) -> bool:
    return isinstance(exc, TRANSIENT_ERRORS)
