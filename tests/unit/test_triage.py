import pytest

from mdm.triage import AUTO_MATCH, NON_MATCH, REVIEW, decide


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (15.0, AUTO_MATCH),
        (12.0, AUTO_MATCH),  # boundary: >= upper is auto_match
        (11.999, REVIEW),
        (5.0, REVIEW),
        (2.0, REVIEW),  # boundary: >= lower is review
        (1.999, NON_MATCH),
        (-10.0, NON_MATCH),
    ],
)
def test_decide(score, expected):
    assert decide(score, upper=12.0, lower=2.0) == expected


def test_decide_upper_equals_lower_collapses_review_band():
    assert decide(5.0, upper=5.0, lower=5.0) == AUTO_MATCH
    assert decide(4.999, upper=5.0, lower=5.0) == NON_MATCH
