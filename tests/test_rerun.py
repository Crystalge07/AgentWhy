import pytest

from whyagent.rerun import decide, fisher_one_sided


@pytest.mark.parametrize("a,b,p", [(0, 5, 0.004), (0, 4, 0.024), (1, 5, 0.024)])
def test_fisher_matches_prd(a, b, p):
    assert round(fisher_one_sided(a, 5, b, 5), 3) == p


def test_decide():
    assert decide(0, 5, 5, 5)[0] == "VALIDATED"
    assert decide(4, 5, 5, 5)[0] == "NOT_NEEDED"
    assert decide(1, 5, 3, 5)[0] == "PROMISING"
    assert decide(2, 5, 1, 5)[0] == "INEFFECTIVE"
