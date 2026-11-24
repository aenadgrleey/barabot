import pytest

from barabot.core.images import ImageSpec

SPEC = ImageSpec(min_side=280, max_side=1568, min_pixels=78_400, max_pixels=2_458_624, target_long_side=800, max_images=4)

TELEGRAM = [(90, 67), (320, 240), (800, 600), (1280, 960)]  # s, m, x, y


def test_prefers_the_smallest_size_that_is_good_enough():
    assert SPEC.pick(TELEGRAM) == 2  # x: y is allowed too, but bigger than needed


def test_takes_the_largest_accepted_size_when_none_reaches_the_target():
    assert SPEC.pick([(320, 300), (640, 480)]) == 1


def test_a_lower_target_picks_a_smaller_size():
    cheap = ImageSpec(**{**SPEC.__dict__, "target_long_side": 300})
    assert cheap.pick([(320, 290), (800, 600)]) == 0


@pytest.mark.parametrize(
    ("w", "h", "ok"),
    [(800, 600, True), (320, 240, False), (2000, 1500, False), (1568, 280, True), (1568, 279, False)],
)
def test_hard_limits(w, h, ok):
    assert SPEC.accepts(w, h) is ok


def test_nothing_accepted():
    assert SPEC.pick([(90, 67), (2560, 1920)]) is None
