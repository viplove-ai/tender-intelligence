import pytest

from src.guarantee import calculate_performance_guarantee


def test_additional_pg_for_bid_more_than_twenty_percent_below():
    result = calculate_performance_guarantee(
        66_282_553, -26.97, components={"Civil": 54_765_000, "E&M": 11_517_553},
    )
    assert result["tendered_amount"] == pytest.approx(48_406_148.4559)
    assert result["normal_pg"] == pytest.approx(3_314_127.65)
    assert result["additional_pg"] == pytest.approx(4_619_893.9441)
    assert result["total_pg_percent"] == pytest.approx(11.97)
    assert result["total_pg"] == pytest.approx(7_934_021.5941)
    assert sum(result["component_pg"].values()) == pytest.approx(result["total_pg"])


@pytest.mark.parametrize("bid_percent", [-20, -10, 0, 12])
def test_only_normal_pg_applies_until_twenty_percent_below(bid_percent):
    result = calculate_performance_guarantee(10_000_000, bid_percent)
    assert result["additional_pg"] == 0
    assert result["total_pg_percent"] == 5
    assert result["total_pg"] == 500_000
