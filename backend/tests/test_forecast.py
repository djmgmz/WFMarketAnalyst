"""Forecasts: features without look-ahead, targets, walk-forward testing and fallbacks."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from wfm import api
from wfm.db import connect, init_db

from helpers import _seed_history, pd_timedelta, pd_timestamp


def test_forecast_features_never_look_ahead(conn):
    from wfm import forecast

    _seed_history(conn, trend_items=1, noise_items=0, days=40)
    before = forecast._dataset(conn)
    # Change only the future.
    conn.execute("UPDATE price_stats SET median = median * 3 WHERE bucket >= '2026-07-31'")
    after = forecast._dataset(conn)
    day = pd_timestamp("2026-07-25")
    feature_cols = forecast.NUMERIC + ["band"]
    assert before.loc[before["date"] == day, feature_cols].equals(after.loc[after["date"] == day, feature_cols])
    # Only the target moves.
    assert not before.loc[before["date"] == day, "y_7"].equals(after.loc[after["date"] == day, "y_7"])


def test_forecast_target_is_the_average_over_the_next_h_days(conn):
    import math

    from wfm import forecast

    _seed_history(conn, trend_items=1, noise_items=0, days=40)
    data = forecast._dataset(conn).set_index("date")
    day = pd_timestamp("2026-07-20")
    week = [math.log(data.loc[day + pd_timedelta(k), "price"]) for k in range(1, 8)]
    assert data.loc[day, "y_7"] == pytest.approx(sum(week) / 7 - math.log(data.loc[day, "price"]))
    tomorrow = data.loc[day + pd_timedelta(1), "price"]
    # h = 1 is tomorrow's price.
    assert data.loc[day, "y_1"] == pytest.approx(math.log(tomorrow / data.loc[day, "price"]))

    # A day without trades carries the last price forward; a week without any trades has no target.
    conn.execute("DELETE FROM price_stats WHERE bucket LIKE '2026-07-27%'")
    data = forecast._dataset(conn).set_index("date")
    assert not math.isnan(data.loc[day, "y_7"]) and math.isnan(data.loc[pd_timestamp("2026-07-26"), "y_1"])
    conn.execute("DELETE FROM price_stats WHERE bucket >= '2026-07-21' AND bucket < '2026-07-28'")
    assert math.isnan(forecast._dataset(conn).set_index("date").loc[day, "y_7"])


def test_forecast_split_never_trains_on_labels_from_the_test_window(conn):
    import pandas as pd

    from wfm import forecast

    _seed_history(conn, trend_items=2, noise_items=0, days=60)
    data = forecast._dataset(conn)
    cutoff = data["date"].max() - pd.Timedelta(days=forecast.TEST_DAYS)
    for h in forecast.HORIZONS:
        train_rows, test_rows = forecast._split(data[data[f"y_{h}"].notna()], cutoff, h)
        assert (train_rows["date"] + pd.Timedelta(days=h)).max() <= cutoff < test_rows["date"].min()


def test_thin_or_faked_series_get_the_baseline_and_never_reach_predicted_movers(conn):
    from wfm import forecast

    _seed_history(conn)
    # trend_0 stops trading most days (6 of the last 28), and trend_1 gets a faked 99,999p day.
    conn.execute("""DELETE FROM price_stats WHERE item_id = 'trend_0' AND bucket >= '2026-08-11'
                    AND CAST(substr(bucket, 9, 2) AS INTEGER) % 5 != 0""")
    conn.execute("UPDATE price_stats SET median = 99999, volume = 400 WHERE item_id = 'trend_1' AND bucket LIKE '2026-08-07%'")
    conn.commit()
    from wfm import prices
    prices.flag_suspect(conn)

    forecast.train(conn)
    methods = dict(conn.execute("SELECT item_id, method FROM forecasts WHERE horizon = 7"))
    assert methods["trend_0"] == "baseline" and methods["trend_1"] == methods["trend_2"] == "model"
    week = forecast.forecast_for(conn, "trend_1", None)[1]
    # The faked day did not leak into the forecast.
    assert week["base_price"] < 1000 and week["high"] < 1000
    assert "trend_0" not in [m["item_id"] for m in forecast.predicted_movers(conn, 7, 50)["gainers"]]


def test_forecast_uses_the_model_only_where_it_beats_no_change(conn):
    from wfm import forecast

    _seed_history(conn)
    report = forecast.train(conn)
    week = report["horizons"]["7"]
    assert week["status"] == "tested"
    assert week["segments"]["category"]["Arcane"]["skill"] > 0.3  # the trend is learnable
    assert week["uses_model"] == {"category": {"Arcane": True, "Mod": False}, "band": {"100–500p": True, "under 20p": False}}
    assert week["segments"]["category"]["Mod"]["skill"] <= 0  # never claims skill on a random walk

    methods = {(r["item_id"].split("_")[0], r["method"])
               for r in conn.execute("SELECT item_id, method FROM forecasts WHERE horizon = 7")}
    assert methods == {("trend", "model"), ("noise", "baseline")}
    trend = forecast.forecast_for(conn, "trend_0", None)
    assert [f["horizon"] for f in trend] == [1, 7]
    week_ahead = trend[1]
    assert week_ahead["low"] <= week_ahead["predicted"] <= week_ahead["high"]
    # Follows the trend: about 2.4% over days 1-7 on average.
    assert week_ahead["predicted"] > week_ahead["base_price"] * 1.015
    noise = forecast.forecast_for(conn, "noise_0", None)[1]
    assert noise["predicted"] == noise["base_price"] and noise["low"] < noise["base_price"] < noise["high"]
    assert forecast.latest_report(conn)["made_at"] == report["made_at"]
    assert [m["item_id"] for m in forecast.predicted_movers(conn, 7, 3)["gainers"]][:1] != []


def test_forecast_falls_back_on_small_segments_and_too_little_data(conn):
    from wfm import forecast

    _seed_history(conn, trend_items=3, noise_items=0, days=60)  # 3 series x 7 test days < 30 rows
    report = forecast.train(conn)
    assert report["horizons"]["7"]["uses_model"]["category"].get("Arcane") is False
    assert {r[0] for r in conn.execute("SELECT method FROM forecasts")} == {"baseline"}

    empty = connect(":memory:")
    init_db(empty)
    with pytest.raises(ValueError):
        forecast.train(empty)  # nothing to learn from


def test_forecast_job_and_endpoints(conn, monkeypatch):
    from wfm import jobs

    _seed_history(conn)
    spec = jobs.JOBS["train-forecast"]
    assert (spec.kind, spec.command) == ("progress", "python -m wfm forecast")

    def override():
        yield conn

    api.app.dependency_overrides[api.get_db] = override
    try:
        client = TestClient(api.app)
        assert client.get("/api/forecast/report").json() is None  # never trained
        from wfm import forecast
        forecast.train(conn)
        body = client.get("/api/items/trend_0/forecast").json()
        assert [f["horizon"] for f in body] == [1, 7] and body[1]["method"] == "model"
        movers = client.get("/api/forecast/movers", params={"horizon": 7}).json()
        assert movers["gainers"] and movers["gainers"][0]["item"]["slug"].startswith("trend_")
        assert client.get("/api/forecast/report").json()["horizons"]["7"]["status"] == "tested"
        assert client.get("/api/forecast/movers", params={"horizon": 3}).status_code == 422
        # Ranked by platinum gained by default, or by percentage; moves under 1p are left out.
        for by in ("gain", "change"):
            rises = client.get("/api/forecast/movers", params={"horizon": 7, "limit": 50, "by": by}).json()["gainers"]
            assert rises and all(r["gain"] >= 1 for r in rises)
            assert [r[by] for r in rises] == sorted((r[by] for r in rises), reverse=True)
        assert client.get("/api/forecast/movers", params={"by": "volume"}).status_code == 422

        found = client.get("/api/forecast/search", params={"q": "trend", "limit": 3}).json()
        assert found["total"] == 8 and [f["item"]["name"] for f in found["forecasts"]] == ["Trend 0", "Trend 1", "Trend 2"]
        assert found["forecasts"][0]["method"] == "model" and found["forecasts"][0]["rank"] is None
        noise = client.get("/api/forecast/search", params={"q": "Noise 3", "horizon": 1}).json()["forecasts"]
        assert [(f["method"], f["change"]) for f in noise] == [("baseline", 0.0)]
        assert client.get("/api/forecast/search", params={"q": "nothing like this"}).json() == {"total": 0, "forecasts": []}
        assert client.get("/api/forecast/search", params={"q": "trend", "horizon": 3}).status_code == 422
    finally:
        api.app.dependency_overrides.clear()
