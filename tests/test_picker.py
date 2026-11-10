from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from dom.picker import last_pick, pick

T0 = datetime(2025, 4, 1, 12, 0, tzinfo=UTC)


def ids(result):
    return {a["id"] for a in result["artworks"]}


def test_picker_cooldown(ingested, conn):
    rng = random.Random(1)
    all_ids = {r[0] for r in conn.execute("SELECT id FROM artworks")}

    first = pick(conn, 2, cooldown_days=5, now=T0, rng=rng)
    assert len(first["artworks"]) == 2 and not first["all_on_cooldown"]

    # a day later only the remaining artwork is eligible
    second = pick(conn, 2, cooldown_days=5, now=T0 + timedelta(days=1), rng=rng)
    assert ids(second) == all_ids - ids(first)
    assert second["eligible"] == 1

    # everything is on cooldown now, so the whole collection is the pool again
    third = pick(conn, 3, cooldown_days=5, now=T0 + timedelta(days=2), rng=rng)
    assert third["all_on_cooldown"] is True
    assert ids(third) == all_ids

    assert conn.execute("SELECT COUNT(*) FROM picks").fetchone()[0] == 6
    assert ids(last_pick(conn)) == ids(third)


def test_cooldown_expires(ingested, conn):
    rng = random.Random(2)
    a = pick(conn, 3, cooldown_days=5, now=T0, rng=rng)
    b = pick(conn, 1, cooldown_days=5, now=T0 + timedelta(days=4), rng=rng)
    assert b["all_on_cooldown"] is True
    c = pick(conn, 3, cooldown_days=5, now=T0 + timedelta(days=5, minutes=1), rng=rng)
    # batch a has expired; only the artwork from b (picked on day 4) is still excluded
    assert c["eligible"] == 2
    assert ids(c) == ids(a) - ids(b)


def test_pick_empty_collection(conn):
    r = pick(conn, 3, cooldown_days=5)
    assert r["artworks"] == [] and r["all_on_cooldown"] is False
