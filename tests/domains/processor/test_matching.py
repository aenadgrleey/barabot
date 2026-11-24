"""Question building and answer reading, against real SDK response models."""
import asyncio
from pathlib import Path

import pytest
from typesafe_sdk import Image, SystemOneResponse

from barabot.domains.processor.matching import (
    IMAGES_HEADER,
    JEV_IMAGES,
    MAX_QUESTIONS,
    Decision,
    JevDecider,
    Subscription,
    build_questions,
    is_transient,
)

RENT = Subscription(1, "сдаёт жильё", "ищет жильё, продаёт что-то или пишет о другом", "квартира с балконом")
RENT_2 = Subscription(2, "сдаёт жильё", "ищет жильё, продаёт что-то или пишет о другом", "квартира у метро")
OUTAGE = Subscription(3, "сообщает об отключении", None, "вода на Žižkov")


def dump(question) -> dict:
    return question.model_dump(mode="json", exclude_none=True, exclude_unset=True)


def test_identical_author_roles_are_asked_once():
    questions, author_key = build_questions([RENT, RENT_2, OUTAGE])
    assert list(questions) == ["author_0", "sub_1", "sub_2", "author_1", "sub_3"]
    assert author_key == {1: "author_0", 2: "author_0", 3: "author_1"}


def test_author_not_becomes_the_false_description_only_when_given():
    questions, _ = build_questions([RENT, OUTAGE])
    assert dump(questions["author_0"]) == {
        "instructions": "The message author сдаёт жильё",
        "criteria": {"false": "ищет жильё, продаёт что-то или пишет о другом"},
    }
    assert dump(questions["author_1"]) == {"instructions": "The message author сообщает об отключении"}


def test_subscription_question_is_the_query_with_three_options():
    questions, _ = build_questions([RENT])
    assert dump(questions["sub_1"]) == {
        "instructions": "The message fits this request: квартира с балконом",
        "criteria": {"fits": None, "does_not_fit": None, "unclear": "not enough information to decide"},
    }


def test_too_many_questions_are_refused_before_the_request():
    subs = [Subscription(i, f"роль {i}", None, "q") for i in range(MAX_QUESTIONS // 2 + 1)]
    with pytest.raises(ValueError, match="Jev limit"):
        build_questions(subs)


@pytest.mark.parametrize(
    ("author_p", "choice", "matched", "needs_photos"),
    [
        (0.9, "fits", True, False),
        (0.9, "unclear", False, True),
        (0.9, "does_not_fit", False, False),
        (0.3, "fits", False, False),  # topic fits, wrong role
        (0.3, "unclear", False, False),  # wrong role: photos would not help
    ],
)
def test_decision_rules(author_p, choice, matched, needs_photos):
    d = Decision(1, author_p, choice, 0.9, 1)
    assert (d.matched, d.needs_photos) == (matched, needs_photos)


class FakeClient:
    def __init__(self):
        self.requests = []

    async def system_one(self, *, state, questions, model, extra_headers):
        self.requests.append({"state": state, "questions": questions, "model": model, "headers": extra_headers})
        answers = {"author_0": {"type": "noul", "noul": 0.97}}
        for name in questions:
            if name.startswith("sub_"):
                answers[name] = {
                    "type": "choice",
                    "choice": "fits" if name == "sub_1" else "unclear",
                    "confidence": 0.8,
                    "probabilities": {"fits": 0.8, "does_not_fit": 0.1, "unclear": 0.1},
                }
        return SystemOneResponse.model_validate(
            {"model": "jev-1.14.0", "answers": answers, "usage": {"input_tokens": 500, "output_tokens": 0}}
        )

    async def aclose(self):
        pass


def test_text_round_sends_text_only_and_reads_answers():
    client = FakeClient()
    decisions = asyncio.run(JevDecider(client).decide("Сдаю 2+kk", [RENT, RENT_2]))
    assert decisions == [
        Decision(1, 0.97, "fits", 0.8, 1),
        Decision(2, 0.97, "unclear", 0.8, 1),
    ]
    request = client.requests[0]
    assert request["state"] == {"text": "Сдаю 2+kk"}
    assert request["headers"] is None


def test_photo_round_sends_images_with_the_opt_in_header(tmp_path: Path):
    client = FakeClient()
    photo = tmp_path / "1-x.jpg"
    photo.write_bytes(b"jpeg")
    decisions = asyncio.run(JevDecider(client).decide("Сдаю", [RENT_2], [photo]))
    assert decisions == [Decision(2, 0.97, "unclear", 0.8, 2)]
    request = client.requests[0]
    assert request["headers"] == IMAGES_HEADER
    assert [type(p) for p in request["state"]["photos"]] == [Image]


def test_photos_beyond_the_jev_limit_are_not_sent(tmp_path: Path):
    client = FakeClient()
    photos = []
    for i in range(JEV_IMAGES.max_images + 2):
        photos.append(tmp_path / f"{i}-x.jpg")
        photos[-1].write_bytes(b"jpeg")
    asyncio.run(JevDecider(client).decide("Сдаю", [RENT], photos))
    assert len(client.requests[0]["state"]["photos"]) == JEV_IMAGES.max_images


def test_no_subscriptions_no_request():
    client = FakeClient()
    assert asyncio.run(JevDecider(client).decide("Сдаю", [])) == []
    assert client.requests == []


def test_only_transient_errors_are_retried():
    import httpx2
    from typesafe_sdk import TypeSafeAuthenticationError, TypeSafeRateLimitError

    assert is_transient(TypeSafeRateLimitError(429, None, httpx2.Headers()))
    assert not is_transient(TypeSafeAuthenticationError(401, None, httpx2.Headers()))
    assert not is_transient(ValueError("bug"))
