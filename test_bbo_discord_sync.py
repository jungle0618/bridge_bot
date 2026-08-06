from bbo_discord_sync import Hand, group_stable_hands


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
