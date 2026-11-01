"""Forecast module: each item's price tomorrow and its next-7-day average, with an 80% range.

A forecast for horizon h is the average latest price over the next h days, so h = 1 is
tomorrow's price and h = 7 is next week's average (a single day's median is mostly noise).

Interface::

    train(conn, job=NO_REPORT)                 build the dataset, test the model against
                                               baselines, store fresh forecasts and the report;
                                               returns the report
    forecast_for(conn, item_id, rank)          stored forecasts for one item (one per horizon)
    predicted_movers(conn, horizon, limit, by="gain")
                                               biggest predicted rises and falls (model forecasts
                                               only), by platinum ("gain") or percentage ("change")
    search(conn, query, horizon, limit)        forecasts for items whose name matches, by name
    latest_report(conn)                        the most recent evaluation report, or None

Behind the interface:

* Dataset: one row per (item, rank, day) from the daily price history, gaps
  filled forward. Features use only data up to that day; targets are the log
  change from that day to the average price over the next 1 and 7 days (only
  where those days had at least one trade).
* Model: one global LightGBM model per horizon trained across all items on
  relative moves (absolute-error objective: the median move), plus quantile
  models (10% / 90%) for the range. Item traits
  (category, rarity, ranks, price level, volatility, volume) are features.
* Testing: walk-forward. The last TEST_DAYS days are held out; nothing trained
  on may look past the cutoff. Accuracy is measured on log error, so a 5p item
  and a 3,000p item count equally, and compared with the naive "no change" baseline.
* Per-segment fallback: for every category and price band the model is used
  only if it beats the naive baseline there on at least MIN_TEST_ROWS test rows.
  Otherwise forecasts for that segment come from the naive baseline, with a
  range from the segment's past moves. Every forecast records its method.
* Clean, liquid data: suspect (likely faked) trade days are left out of the
  dataset (prices.flag_suspect), and a series with fewer than LIQUID_MIN_DAYS
  trading days in the last LIQUID_WINDOW_DAYS gets the baseline forecast too, so
  thin markets never reach Predicted movers.
"""

from __future__ import annotations

import json
import math
import sqlite3
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd

from .db import utc_now
from .jobs import NO_REPORT, Reporter
from .prices import LIQUID_MIN_DAYS, LIQUID_WINDOW_DAYS, UNRANKED

HORIZONS = (1, 7)
TEST_DAYS = 14
MIN_TEST_ROWS = 30
MIN_TRAIN_ROWS = 200
QUANTILES = {"low": 0.1, "high": 0.9}
STALE_DAYS = 7  # series whose last trade day is older than this get no forecast
MIN_MOVE_PLAT = 1.0  # predicted movers: smaller moves are below the smallest price step
MOVER_RANKINGS = ("gain", "change")  # platinum gained or lost, percentage change

BAND_EDGES = [0, 20, 100, 500, math.inf]
BAND_LABELS = ["under 20p", "20–100p", "100–500p", "500p+"]
SEGMENTS = ("category", "band")

NUMERIC = [
    "ret_1", "ret_3", "ret_7", "ret_14",
    "mean_gap_7", "mean_gap_14", "mean_gap_28", "vol_7", "vol_14", "vol_28",
    "log_volume", "volume_7", "volume_28", "range_pos", "ma_gap",
    "listing_premium", "spread", "log_supply", "level", "days_since_trade", "dow",
    "rank", "max_rank", "is_max_rank", "ducats", "vaulted", "req_mastery_rank",
]
CATEGORICAL = ["category", "rarity", "band"]
FEATURES = NUMERIC + CATEGORICAL

_LGB_PARAMS = dict(n_estimators=300, learning_rate=0.05, num_leaves=15, min_child_samples=20,
                   subsample=0.8, subsample_freq=1, colsample_bytree=0.8, random_state=0, verbose=-1)


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------

def train(conn: sqlite3.Connection, job: Reporter = NO_REPORT) -> dict[str, Any]:
    """Test the model, then store forecasts for every item with recent history.

    Raises ValueError when there is no price history to learn from.
    """
    steps = 2 + len(HORIZONS) * (1 + len(QUANTILES)) * 2
    job.set_total(steps)
    job.working_on("building the dataset")
    data = _dataset(conn)
    if data.empty:
        raise ValueError("No price history to learn from: backfill a category first")
    job.advance()

    last_date = data["date"].max()
    cutoff = last_date - pd.Timedelta(days=TEST_DAYS)
    report: dict[str, Any] = {
        "made_at": utc_now(), "last_date": last_date.strftime("%Y-%m-%d"),
        "cutoff": cutoff.strftime("%Y-%m-%d"), "test_days": TEST_DAYS,
        "series": int(data.groupby(["item_id", "rank"]).ngroups), "horizons": {},
    }
    latest = _latest_rows(data, last_date)
    forecasts: list[dict[str, Any]] = []

    for h in HORIZONS:
        target = f"y_{h}"
        labelled = data[data[target].notna()]
        train_rows, test_rows = _split(labelled, cutoff, h)

        job.working_on(f"testing the {h}-day model")
        evaluation = _evaluate(train_rows, test_rows, target, h, job)
        report["horizons"][str(h)] = evaluation

        job.working_on(f"training the {h}-day model on all data")
        models = _fit_all(labelled, target, job) if len(labelled) >= MIN_TRAIN_ROWS else None
        forecasts += _forecast(latest, models, evaluation["uses_model"], labelled, target, h)

        if h == max(HORIZONS) and models:
            gain = models["point"].booster_.feature_importance(importance_type="gain")
            total = float(gain.sum()) or 1.0
            ranked = sorted(zip(FEATURES, gain), key=lambda kv: -kv[1])[:12]
            report["feature_importance"] = [{"feature": f, "share": round(float(g) / total, 4)} for f, g in ranked]

    job.working_on("saving forecasts")
    report["forecasts"] = len(forecasts)
    _store(conn, forecasts, report)
    job.advance()
    return report


def forecast_for(conn: sqlite3.Connection, item_id: str, rank: int | None) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT horizon, made_at, base_date, base_price, predicted, low, high, method
           FROM forecasts WHERE item_id = ? AND rank = ? ORDER BY horizon""",
        (item_id, UNRANKED if rank is None else rank))
    return [dict(r) for r in rows]


def predicted_movers(conn: sqlite3.Connection, horizon: int, limit: int,
                     by: str = "gain") -> dict[str, list[dict[str, Any]]]:
    """Largest predicted moves among model forecasts (baseline forecasts predict no change).

    Ranked `by` platinum gained or lost ("gain") or by percentage ("change"). Moves under
    MIN_MOVE_PLAT are left out: prices are whole platinum, so they could not be traded on.
    """
    if by not in MOVER_RANKINGS:
        raise ValueError(f"by must be one of {MOVER_RANKINGS}")
    rows = _forecast_rows(conn, "f.horizon = ? AND f.method = 'model' AND f.base_price > 0 AND ABS(f.predicted - f.base_price) >= ?",
                          (horizon, MIN_MOVE_PLAT))
    gainers = sorted((r for r in rows if r["gain"] > 0), key=lambda r: -r[by])
    losers = sorted((r for r in rows if r["gain"] < 0), key=lambda r: r[by])
    return {"gainers": gainers[:limit], "losers": losers[:limit]}


def search(conn: sqlite3.Connection, query: str, horizon: int, limit: int) -> dict[str, Any]:
    """Forecasts for items whose name contains `query`, by name: {"total", "forecasts"}."""
    rows = _forecast_rows(conn, "f.horizon = ? AND i.name LIKE ? AND f.base_price > 0",
                          (horizon, f"%{query}%"), order="i.name COLLATE NOCASE, f.rank")
    return {"total": len(rows), "forecasts": rows[:limit]}


def latest_report(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute("SELECT report FROM forecast_runs ORDER BY id DESC LIMIT 1").fetchone()
    return json.loads(row[0]) if row else None


def _forecast_rows(conn: sqlite3.Connection, where: str, args: tuple[Any, ...],
                   order: str = "f.item_id") -> list[dict[str, Any]]:
    rows = [dict(r) for r in conn.execute(
        f"""SELECT f.item_id, f.rank, f.base_price, f.predicted, f.low, f.high, f.method,
                   f.predicted / f.base_price - 1 AS change, f.predicted - f.base_price AS gain
            FROM forecasts f JOIN items i ON i.id = f.item_id WHERE {where} ORDER BY {order}""", args)]
    for r in rows:
        r["rank"] = None if r["rank"] == UNRANKED else r["rank"]
        r["change"] = round(r["change"], 4)
        r["gain"] = round(r["gain"], 2)
    return rows


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------

def _frame(conn: sqlite3.Connection, sql: str, numeric: tuple[str, ...] = ()) -> pd.DataFrame:
    cur = conn.execute(sql)
    frame = pd.DataFrame([tuple(r) for r in cur.fetchall()], columns=[c[0] for c in cur.description])
    # A column that is entirely NULL would otherwise be typed object and break the maths.
    for col in numeric:
        frame[col] = pd.to_numeric(frame[col], errors="coerce").astype(float)
    return frame


def _dataset(conn: sqlite3.Connection) -> pd.DataFrame:
    # Suspect (likely faked) trade days are left out: they count as days without trades. Only the
    # default subtype is read, the one the item's latest price refers to.
    closed = _frame(conn, """SELECT ps.item_id, ps.mod_rank AS rank, ps.bucket, ps.median, ps.volume,
                                    ps.moving_avg, ps.donch_top, ps.donch_bot
                             FROM price_stats ps
                             JOIN default_subtypes d ON d.item_id = ps.item_id AND d.subtype = ps.subtype
                             WHERE ps.source = 'closed' AND ps.granularity = 'day' AND ps.suspect = 0""",
                    numeric=("median", "volume", "moving_avg", "donch_top", "donch_bot"))
    if closed.empty:
        return pd.DataFrame()
    closed["date"] = pd.to_datetime(closed["bucket"].str[:10])

    live = _frame(conn, """SELECT ps.item_id, ps.mod_rank AS rank, ps.bucket, ps.order_type, ps.median,
                                  ps.min_price, ps.volume
                           FROM price_stats ps
                           JOIN default_subtypes d ON d.item_id = ps.item_id AND d.subtype = ps.subtype
                           WHERE ps.source = 'live' AND ps.granularity = 'day'
                             AND ps.order_type IN ('sell', 'buy')""",
                  numeric=("median", "min_price", "volume"))
    live_by_series: dict[tuple[str, int], pd.DataFrame] = {}
    if not live.empty:
        live["date"] = pd.to_datetime(live["bucket"].str[:10])
        wide = live.pivot_table(index=["item_id", "rank", "date"], columns="order_type",
                                values=["min_price", "median", "volume"], aggfunc="last")
        wide.columns = [f"{value}_{side}" for value, side in wide.columns]
        live_by_series = {key: g.droplevel([0, 1]) for key, g in wide.groupby(level=[0, 1])}

    items = _frame(conn, """SELECT id AS item_id, category, rarity, max_rank, ducats, vaulted, req_mastery_rank
                            FROM items""").set_index("item_id")

    frames = []
    for (item_id, rank), g in closed.groupby(["item_id", "rank"], sort=False):
        if item_id not in items.index:
            continue
        series = _series(g, live_by_series.get((item_id, rank)))
        if series.empty:
            continue
        series["item_id"], series["rank"] = item_id, rank
        frames.append(series)
    if not frames:
        return pd.DataFrame()

    data = pd.concat(frames, ignore_index=True).join(items, on="item_id")
    data["is_max_rank"] = ((data["rank"] >= 0) & (data["rank"] == data["max_rank"])).astype(int)
    for col in ("max_rank", "ducats", "vaulted", "req_mastery_rank"):
        data[col] = pd.to_numeric(data[col], errors="coerce")
    for col in ("category", "rarity"):
        data[col] = data[col].fillna("unknown").astype("category")
    data["band"] = pd.Categorical(data["band"], categories=BAND_LABELS)
    return data


def _series(g: pd.DataFrame, live: pd.DataFrame | None) -> pd.DataFrame:
    """Features and targets for one (item, rank) daily series; day t uses days <= t only."""
    s = g.set_index("date").sort_index()
    s = s[~s.index.duplicated(keep="last")]
    days = pd.date_range(s.index.min(), s.index.max(), freq="D")
    s = s.reindex(days)
    if live is not None:
        s = s.join(live.reindex(days).ffill(limit=3))

    traded = s["median"].gt(0)
    price = s["median"].where(traded).ffill()
    logp = np.log(price)
    daily = logp.diff()
    out = pd.DataFrame(index=days)
    out["price"] = price

    position = np.arange(len(days), dtype=float)
    last_trade = pd.Series(np.where(traded, position, np.nan), index=days).ffill()
    out["days_since_trade"] = position - last_trade.to_numpy()
    for k in (1, 3, 7, 14):
        out[f"ret_{k}"] = logp - logp.shift(k)
    for k in (7, 14, 28):
        out[f"mean_gap_{k}"] = logp - np.log(price.rolling(k, min_periods=max(3, k // 2)).mean())
        out[f"vol_{k}"] = daily.rolling(k, min_periods=max(3, k // 2)).std()

    log_volume = np.log1p(s["volume"].fillna(0))
    out["log_volume"] = log_volume
    out["volume_7"] = log_volume.rolling(7, min_periods=3).mean()
    out["volume_28"] = log_volume.rolling(28, min_periods=7).mean()
    width = (s["donch_top"] - s["donch_bot"]).ffill()
    out["range_pos"] = ((price - s["donch_bot"].ffill()) / width.where(width > 0)).clip(-1, 2)
    out["ma_gap"] = logp - np.log(s["moving_avg"].ffill().where(lambda v: v > 0))

    if "min_price_sell" in s:
        # The cheapest listing, the asking price the item chart shows (prices.history).
        sell = s["min_price_sell"].where(lambda v: v > 0)
        out["listing_premium"] = np.log(sell) - logp
        out["spread"] = np.log(sell / s.get("median_buy", pd.Series(np.nan, index=days)).where(lambda v: v > 0))
        out["log_supply"] = np.log1p(s.get("volume_sell", pd.Series(np.nan, index=days)))
    else:
        out["listing_premium"] = out["spread"] = out["log_supply"] = np.nan

    median_28 = price.rolling(28, min_periods=7).median()
    out["level"] = np.log(median_28)
    out["band"] = pd.cut(median_28, bins=BAND_EDGES, labels=BAND_LABELS, right=False).astype(object)
    out["dow"] = days.day_of_week
    # Genuine trading days in prices.liquid_series' window; thinner series get no model forecast.
    out["trade_days"] = traded.astype(float).rolling(LIQUID_WINDOW_DAYS, min_periods=1).sum()

    for h in HORIZONS:
        # The average log price over the next h days vs today (h = 1: tomorrow's price). Averaging
        # cancels most single-day noise. Untraded days carry the last price forward; a window with
        # no trades at all, or one that runs past the data, has no target.
        ahead = range(1, h + 1)
        traded_ahead = pd.concat([traded.shift(-k, fill_value=False) for k in ahead], axis=1).any(axis=1)
        out[f"y_{h}"] = (sum(logp.shift(-k) for k in ahead) / h - logp).where(traded_ahead)

    out = out[out["ret_7"].notna() & out["price"].gt(0) & out["band"].notna()]
    out.insert(0, "date", out.index)
    return out.reset_index(drop=True)


def _latest_rows(data: pd.DataFrame, last_date: pd.Timestamp) -> pd.DataFrame:
    """Each series' newest day, if it is recent enough to forecast from."""
    newest = data.loc[data.groupby(["item_id", "rank"])["date"].idxmax()]
    return newest[newest["date"] >= last_date - pd.Timedelta(days=STALE_DAYS)]


# ---------------------------------------------------------------------------
# Models and testing
# ---------------------------------------------------------------------------

def _split(labelled: pd.DataFrame, cutoff: pd.Timestamp, h: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Walk-forward split: test on the days after the cutoff.

    Training uses only rows whose label (the price h days later) is known by the cutoff, so no
    training label reaches into the test window.
    """
    return labelled[labelled["date"] + pd.Timedelta(days=h) <= cutoff], labelled[labelled["date"] > cutoff]


def _fit(rows: pd.DataFrame, target: str, alpha: float | None = None) -> lgb.LGBMRegressor:
    # The point model minimises absolute error (it predicts the median move) because accuracy
    # is judged on absolute error; most days a price does not move, so mean and median differ a lot.
    objective = {"objective": "quantile", "alpha": alpha} if alpha is not None else {"objective": "regression_l1"}
    model = lgb.LGBMRegressor(**objective, **_LGB_PARAMS)
    model.fit(rows[FEATURES], rows[target])
    return model


def _fit_all(rows: pd.DataFrame, target: str, job: Reporter) -> dict[str, lgb.LGBMRegressor]:
    models = {"point": _fit(rows, target)}
    job.advance()
    for name, alpha in QUANTILES.items():
        models[name] = _fit(rows, target, alpha)
        job.advance()
    return models


def _predict(models: dict[str, lgb.LGBMRegressor], rows: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame({name: m.predict(rows[FEATURES]) for name, m in models.items()}, index=rows.index)
    # Quantile models can cross; keep low <= point <= high.
    ordered = np.sort(out[["low", "point", "high"]].to_numpy(), axis=1)
    out["low"], out["point"], out["high"] = ordered[:, 0], ordered[:, 1], ordered[:, 2]
    return out


def _metrics(rows: pd.DataFrame, pred: pd.DataFrame | None, target: str, h: int) -> dict[str, Any]:
    """Model vs baselines on held-out rows; skill uses log error, so every price level counts."""
    actual = rows[target].to_numpy()
    base = rows["price"].to_numpy()
    naive_error = np.abs(actual)
    # Last week's daily drift continued: averaged over days 1..h that is (h + 1) / 2 days of drift.
    trend = (h + 1) / 2 * rows["ret_7"].to_numpy() / 7
    result: dict[str, Any] = {
        "n": int(len(rows)),
        "naive_mae_plat": round(float(np.mean(np.abs(base * np.expm1(actual)))), 2),
        "trend_skill": _skill(np.abs(trend - actual), naive_error),
    }
    if pred is None:
        return result
    point = pred["point"].to_numpy()
    moved = actual != 0
    result.update({
        "model_mae_plat": round(float(np.mean(np.abs(base * (np.exp(point) - np.exp(actual))))), 2),
        "model_mape": round(float(np.mean(np.abs(np.exp(point - actual) - 1))), 4),
        "skill": _skill(np.abs(point - actual), naive_error),
        "direction": round(float(np.mean(np.sign(point[moved]) == np.sign(actual[moved]))), 4) if moved.any() else None,
        "coverage": round(float(np.mean((actual >= pred["low"].to_numpy()) & (actual <= pred["high"].to_numpy()))), 4),
    })
    return result


def _skill(model_error: np.ndarray, naive_error: np.ndarray) -> float | None:
    """1 - model error / naive error: above 0 means better than assuming no change."""
    naive = float(np.mean(naive_error)) if len(naive_error) else 0.0
    return round(1 - float(np.mean(model_error)) / naive, 4) if naive > 0 else None


def _evaluate(train_rows: pd.DataFrame, test_rows: pd.DataFrame, target: str, h: int,
              job: Reporter) -> dict[str, Any]:
    enough = len(train_rows) >= MIN_TRAIN_ROWS and len(test_rows) > 0
    models = _fit_all(train_rows, target, job) if enough else None
    if not enough:
        for _ in range(1 + len(QUANTILES)):
            job.advance()
    pred = _predict(models, test_rows) if models else None

    evaluation: dict[str, Any] = {
        "status": "tested" if models else "not enough data",
        "train_rows": int(len(train_rows)), "test_rows": int(len(test_rows)),
        "overall": _metrics(test_rows, pred, target, h), "segments": {}, "uses_model": {},
    }
    for segment in SEGMENTS:
        evaluation["segments"][segment] = {}
        evaluation["uses_model"][segment] = {}
        for value, rows in test_rows.groupby(segment, observed=True):
            m = _metrics(rows, pred.loc[rows.index] if pred is not None else None, target, h)
            use = bool(pred is not None and m["n"] >= MIN_TEST_ROWS and (m.get("skill") or 0) > 0)
            evaluation["segments"][segment][str(value)] = {**m, "uses_model": use}
            evaluation["uses_model"][segment][str(value)] = use
    return evaluation


# ---------------------------------------------------------------------------
# Forecasting and storage
# ---------------------------------------------------------------------------

def _forecast(latest: pd.DataFrame, models: dict[str, lgb.LGBMRegressor] | None, uses_model: dict[str, dict[str, bool]],
              labelled: pd.DataFrame, target: str, h: int) -> list[dict[str, Any]]:
    """One forecast per recent series, from the model where it has earned it, else the baseline.

    The model is used where it beat the baseline for the series' segments and the series trades
    often enough (prices.LIQUID_MIN_DAYS).
    """
    if latest.empty:
        return []
    pred = _predict(models, latest) if models else None
    spread = _baseline_ranges(labelled, target)
    made_at = utc_now()
    rows = []
    for idx, r in latest.iterrows():
        use = (pred is not None and r["trade_days"] >= LIQUID_MIN_DAYS
               and all(uses_model.get(seg, {}).get(str(r[seg]), False) for seg in SEGMENTS))
        if use:
            low, point, high = pred.at[idx, "low"], pred.at[idx, "point"], pred.at[idx, "high"]
        else:
            low, high = spread.get(str(r["band"]), spread["all"])
            point = 0.0
        base = float(r["price"])
        rows.append({
            "item_id": r["item_id"], "rank": int(r["rank"]), "horizon": h, "made_at": made_at,
            "base_date": r["date"].strftime("%Y-%m-%d"), "base_price": round(base, 2),
            "predicted": round(base * math.exp(point), 2), "low": round(base * math.exp(low), 2),
            "high": round(base * math.exp(high), 2), "method": "model" if use else "baseline",
        })
    return rows


def _baseline_ranges(labelled: pd.DataFrame, target: str) -> dict[str, tuple[float, float]]:
    """10%-90% of past moves per price band (all items when a band has too few rows)."""
    def q(values: pd.Series) -> tuple[float, float]:
        return float(values.quantile(QUANTILES["low"])), float(values.quantile(QUANTILES["high"]))

    ranges = {"all": q(labelled[target]) if len(labelled) else (0.0, 0.0)}
    for band, rows in labelled.groupby("band", observed=True):
        if len(rows) >= MIN_TEST_ROWS:
            ranges[str(band)] = q(rows[target])
    return ranges


def _store(conn: sqlite3.Connection, forecasts: list[dict[str, Any]], report: dict[str, Any]) -> None:
    """Replace all forecasts and add the report in one transaction."""
    conn.execute("DELETE FROM forecasts")
    if forecasts:
        cols = list(forecasts[0])
        conn.executemany(f"INSERT INTO forecasts ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})",
                         forecasts)
    conn.execute("INSERT INTO forecast_runs (made_at, report) VALUES (?, ?)", (report["made_at"], json.dumps(report)))
    conn.commit()
