from bbo_discord_sync import Hand, date_range_timestamps, group_stable_hands, query_url


def test_newest_30_minute_group_is_pending():
    hands = [Hand(100, "a"), Hand(200, "b"), Hand(1100, "c"), Hand(1200, "d"), Hand(1300, "e")]
    stable, pending = group_stable_hands(hands, now=1400, gap_seconds=300)
    assert [hand.url for hand in stable] == ["a", "b"]
    assert [hand.url for hand in pending] == ["c", "d", "e"]


def test_all_hands_stable_after_30_minutes():
    hands = [Hand(100, "a"), Hand(200, "b")]
    stable, pending = group_stable_hands(hands, now=600, gap_seconds=300)
    assert [hand.url for hand in stable] == ["a", "b"]
    assert pending == []


def test_date_range_includes_both_calendar_days():
    start, end = date_range_timestamps("2026-07-01", "2026-07-20", "Asia/Taipei")
    assert end - start == 20 * 86400
    assert "start_time=" in query_url("wei1011", start, end)


def test_date_range_rejects_reversed_dates():
    try:
        date_range_timestamps("2026-07-20", "2026-07-01", "Asia/Taipei")
    except ValueError as exc:
        assert "不早於" in str(exc)
    else:
        raise AssertionError("reversed date range should fail")
