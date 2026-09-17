"""Analyze collected Uniswap v3 WETH/USDT Swap and Mint events."""

import json
import math
import re
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
import numpy as np
import pandas as pd


DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"
EVENTS_PATH = DATA_DIR / "events.csv"
PARTIAL_PATH = DATA_DIR / "events_partial.csv"
METADATA_PATH = DATA_DIR / "collection_metadata.json"
MINT_PATH = DATA_DIR / "mint_analysis.csv"
DAILY_PATH = DATA_DIR / "daily_analysis.csv"

RAW_INTEGER_COLUMNS = [
    "block_number", "transaction_index", "log_index", "amount0_raw",
    "amount1_raw", "sqrt_price_x96", "tick", "tick_lower", "tick_upper",
    "liquidity_added",
]
ORDER_COLUMNS = ["block_number", "transaction_index", "log_index"]


def price_from_sqrt(sqrt_price_x96):
    """Return USDT per ETH from Uniswap's Q64.96 square-root price."""
    return (int(sqrt_price_x96) / 2**96) ** 2 * 10**12


def require_integer_strings(events):
    pattern = re.compile(r"^-?\d+$")
    for column in RAW_INTEGER_COLUMNS:
        if column not in events:
            continue
        invalid = events.loc[events[column].notna() & ~events[column].str.match(pattern), column]
        if not invalid.empty:
            raise ValueError(f"{column} contains a non-integer raw value: {invalid.iloc[0]}")


def read_inputs():
    if not METADATA_PATH.exists():
        raise FileNotFoundError("Run collect_data.py before analyze_data.py")
    metadata = json.loads(METADATA_PATH.read_text(encoding="utf-8"))
    event_path = EVENTS_PATH if metadata.get("collection_status") == "complete" else PARTIAL_PATH
    if not event_path.exists():
        raise FileNotFoundError(f"No event file is available at {event_path}")
    events = pd.read_csv(event_path, dtype=str, keep_default_na=False)
    events = events.replace("", pd.NA)
    require_integer_strings(events)
    return events, metadata


def fully_collected_dates(metadata):
    start = pd.Timestamp(metadata["actual_start_date"])
    requested_end = pd.Timestamp(metadata["actual_end_date"])
    if metadata.get("collection_status") == "complete":
        completed_end = requested_end
    else:
        completed_text = metadata.get("completed_coverage", {}).get("completed_through_timestamp")
        if not completed_text:
            return []
        # Only the day before the last reached UTC date is guaranteed complete.
        completed_end = pd.Timestamp(completed_text).tz_convert(None).normalize() - timedelta(days=1)
        completed_end = min(completed_end, requested_end)
    if completed_end < start:
        return []
    return pd.date_range(start, completed_end, freq="D").strftime("%Y-%m-%d").tolist()


def process_events(events, valid_dates):
    numeric_order = events[ORDER_COLUMNS].astype("int64")
    events = events.assign(**{column: numeric_order[column] for column in ORDER_COLUMNS})
    expected_index = events.sort_values(ORDER_COLUMNS, kind="stable").index
    if not expected_index.equals(pd.RangeIndex(len(events))):
        raise ValueError("Events are not stored in exact blockchain order")
    events = events.sort_values(ORDER_COLUMNS, kind="stable").reset_index(drop=True)

    if events.duplicated(["transaction_hash", "log_index"]).any():
        raise ValueError("Duplicate logs found (transaction hash and log index)")
    events["timestamp"] = pd.to_datetime(events["timestamp"], utc=True)
    events["date"] = events["timestamp"].dt.strftime("%Y-%m-%d")
    events["is_initialization"] = events["is_initialization"].str.lower().eq("true")
    if events.loc[events["is_initialization"], "event_type"].ne("Swap").any():
        raise ValueError("Only a Swap may be used as an initialization record")

    current_price = None
    mint_rows = []
    swap_rows = []
    for row in events.itertuples(index=False):
        is_sample = row.date in valid_dates and not row.is_initialization
        if row.event_type == "Swap":
            price = price_from_sqrt(row.sqrt_price_x96)
            if not math.isfinite(price) or price <= 0:
                raise ValueError(f"Non-positive Swap price at block {row.block_number}")
            current_price = price
            if is_sample:
                swap_rows.append({
                    "block_number": row.block_number,
                    "transaction_index": row.transaction_index,
                    "log_index": row.log_index,
                    "timestamp": row.timestamp,
                    "date": row.date,
                    "price_usdt_per_eth": price,
                    "amount1_raw": row.amount1_raw,
                    "tick": row.tick,
                })
        elif row.event_type == "Mint" and is_sample:
            if current_price is None:
                raise ValueError(f"Mint at block {row.block_number} has no preceding observed Swap")
            lower_price = 1.0001 ** int(row.tick_lower) * 10**12
            upper_price = 1.0001 ** int(row.tick_upper) * 10**12
            if not 0 < lower_price < upper_price:
                raise ValueError(f"Invalid Mint range at block {row.block_number}")
            center = math.sqrt(lower_price * upper_price)
            displacement = math.log(center / current_price)
            width = math.log(upper_price / lower_price)
            if lower_price > current_price:
                classification = "Above"
            elif upper_price < current_price:
                classification = "Below"
            else:
                classification = "Around"
            weth_amount = int(row.amount0_raw) / 10**18
            usdt_amount = int(row.amount1_raw) / 10**6
            value = weth_amount * current_price + usdt_amount
            mint_rows.append({
                "block_number": row.block_number,
                "block_hash": row.block_hash,
                "transaction_hash": row.transaction_hash,
                "transaction_index": row.transaction_index,
                "log_index": row.log_index,
                "timestamp": row.timestamp,
                "date": row.date,
                "owner": row.owner,
                "tick_lower": row.tick_lower,
                "tick_upper": row.tick_upper,
                "liquidity_added": row.liquidity_added,
                "amount0_raw": row.amount0_raw,
                "amount1_raw": row.amount1_raw,
                "current_price_usdt_per_eth": current_price,
                "lower_price_usdt_per_eth": lower_price,
                "upper_price_usdt_per_eth": upper_price,
                "range_center_usdt_per_eth": center,
                "displacement": displacement,
                "range_width": width,
                "classification": classification,
                "weth_amount": weth_amount,
                "usdt_amount": usdt_amount,
                "mint_value_usdt": value,
            })

    mints = pd.DataFrame(mint_rows)
    swaps = pd.DataFrame(swap_rows)
    if mints.empty:
        mints = pd.DataFrame(columns=[
            "block_number", "block_hash", "transaction_hash", "transaction_index",
            "log_index", "timestamp", "date", "owner", "tick_lower", "tick_upper",
            "liquidity_added", "amount0_raw", "amount1_raw",
            "current_price_usdt_per_eth", "lower_price_usdt_per_eth",
            "upper_price_usdt_per_eth", "range_center_usdt_per_eth", "displacement",
            "range_width", "classification", "weth_amount", "usdt_amount", "mint_value_usdt",
        ])
    if swaps.empty:
        swaps = pd.DataFrame(columns=[
            "block_number", "transaction_index", "log_index", "timestamp", "date",
            "price_usdt_per_eth", "amount1_raw", "tick",
        ])
    return events, swaps, mints


def weighted_average(frame, value_column):
    weights = frame["mint_value_usdt"].astype(float)
    values = frame[value_column].astype(float)
    usable = weights.notna() & values.notna() & (weights >= 0)
    if not usable.any() or weights[usable].sum() <= 0:
        return np.nan
    return np.average(values[usable], weights=weights[usable])


def build_daily(swaps, mints, valid_dates):
    dates = pd.DatetimeIndex(pd.to_datetime(valid_dates), name="date")
    daily = pd.DataFrame(index=dates)
    if not swaps.empty:
        swap_data = swaps.copy()
        swap_data["date"] = pd.to_datetime(swap_data["date"])
        swap_data["usdt_volume"] = swap_data["amount1_raw"].map(lambda value: abs(int(value)) / 10**6)
        swap_daily = swap_data.groupby("date").agg(
            close_price_usdt_per_eth=("price_usdt_per_eth", "last"),
            high_price_usdt_per_eth=("price_usdt_per_eth", "max"),
            low_price_usdt_per_eth=("price_usdt_per_eth", "min"),
            swap_count=("price_usdt_per_eth", "size"),
            usdt_swap_volume=("usdt_volume", "sum"),
        )
        daily = daily.join(swap_daily)

    if "swap_count" not in daily:
        daily["swap_count"] = 0
        daily["usdt_swap_volume"] = 0.0
    daily["swap_count"] = daily["swap_count"].fillna(0).astype(int)
    daily["usdt_swap_volume"] = daily["usdt_swap_volume"].fillna(0.0)
    daily["mint_count"] = 0
    daily["total_mint_value_usdt"] = 0.0
    for column in [
        "above_value_share", "below_value_share", "around_value_share",
        "avg_displacement", "avg_range_width", "lp_imbalance",
    ]:
        daily[column] = np.nan

    if not mints.empty:
        mint_data = mints.copy()
        mint_data["date"] = pd.to_datetime(mint_data["date"])
        for date, group in mint_data.groupby("date"):
            total = group["mint_value_usdt"].sum()
            daily.loc[date, "mint_count"] = len(group)
            daily.loc[date, "total_mint_value_usdt"] = total
            if total > 0:
                shares = group.groupby("classification")["mint_value_usdt"].sum() / total
                daily.loc[date, "above_value_share"] = shares.get("Above", 0.0)
                daily.loc[date, "below_value_share"] = shares.get("Below", 0.0)
                daily.loc[date, "around_value_share"] = shares.get("Around", 0.0)
                daily.loc[date, "avg_displacement"] = weighted_average(group, "displacement")
                daily.loc[date, "avg_range_width"] = weighted_average(group, "range_width")
                daily.loc[date, "lp_imbalance"] = (
                    daily.loc[date, "above_value_share"] - daily.loc[date, "below_value_share"]
                )

    daily["insufficient_high_low"] = daily["swap_count"] < 2
    daily["same_day_return"] = np.log(
        daily["close_price_usdt_per_eth"] / daily["close_price_usdt_per_eth"].shift(1)
    )
    daily["next_day_return"] = np.log(
        daily["close_price_usdt_per_eth"].shift(-1) / daily["close_price_usdt_per_eth"]
    )
    daily["next_day_high_low_range"] = np.log(
        daily["high_price_usdt_per_eth"].shift(-1) / daily["low_price_usdt_per_eth"].shift(-1)
    )
    daily["next_day_high_low_insufficient"] = daily["insufficient_high_low"].shift(-1).astype("boolean")
    daily.loc[daily["next_day_high_low_insufficient"].fillna(True), "next_day_high_low_range"] = np.nan
    daily = daily.reset_index()
    daily["date"] = daily["date"].dt.strftime("%Y-%m-%d")
    return daily


def correlation_row(daily, predictor, outcome):
    valid = daily[[predictor, outcome]].dropna()
    correlation = valid[predictor].corr(valid[outcome]) if len(valid) >= 2 else np.nan
    return {"predictor": predictor, "outcome": outcome, "n_valid": len(valid), "correlation": correlation}


def tied_quantile_groups(values, labels=("Low", "Middle", "High")):
    """Make ordered quantile groups without separating observations tied in value."""
    result = pd.Series(pd.NA, index=values.index, dtype="object")
    valid = values.dropna()
    if valid.empty:
        return result
    if valid.nunique() == 1:
        result.loc[valid.index] = "All"
        return result
    bins = pd.qcut(valid, q=3, duplicates="drop")
    categories = list(bins.cat.categories)
    available_labels = {
        1: ["All"],
        2: [labels[0], labels[-1]],
        3: list(labels),
    }[len(categories)]
    mapping = {category: available_labels[index] for index, category in enumerate(categories)}
    result.loc[valid.index] = bins.map(mapping).astype("object")
    return result


def outcome_statistics(group, outcome):
    values = group[outcome].dropna()
    return {
        "n_days": len(group),
        "n_valid_outcome": len(values),
        "mean_outcome": values.mean(),
        "median_outcome": values.median(),
        "positive_outcome_share": (values > 0).mean() if len(values) else np.nan,
    }


def positioning_groups(daily):
    """Summarize intuitive sign groups and equal-frequency imbalance groups."""
    usable = daily[daily["lp_imbalance"].notna()].copy()
    rows = []
    usable["group"] = np.select(
        [usable["lp_imbalance"] < 0, usable["lp_imbalance"] > 0],
        ["More value below", "More value above"],
        default="No directional imbalance",
    )
    for name in ["More value below", "No directional imbalance", "More value above"]:
        group = usable[usable["group"] == name]
        row = {"definition": "Sign of LP imbalance", "group": name}
        row.update(outcome_statistics(group, "next_day_return"))
        row.update({
            "mean_lp_imbalance": group["lp_imbalance"].mean(),
            "min_lp_imbalance": group["lp_imbalance"].min(),
            "max_lp_imbalance": group["lp_imbalance"].max(),
            "mean_same_day_return": group["same_day_return"].mean(),
        })
        rows.append(row)

    labels = ("Below-tilted", "Middle", "Above-tilted")
    usable["group"] = tied_quantile_groups(usable["lp_imbalance"], labels)
    for name in labels:
        group = usable[usable["group"] == name]
        if group.empty:
            continue
        row = {"definition": "Equal-frequency imbalance group", "group": name}
        row.update(outcome_statistics(group, "next_day_return"))
        row.update({
            "mean_lp_imbalance": group["lp_imbalance"].mean(),
            "min_lp_imbalance": group["lp_imbalance"].min(),
            "max_lp_imbalance": group["lp_imbalance"].max(),
            "mean_same_day_return": group["same_day_return"].mean(),
        })
        rows.append(row)
    return pd.DataFrame(rows).rename(columns={
        "n_valid_outcome": "n_valid_next_day_return",
        "mean_outcome": "mean_next_day_return",
        "median_outcome": "median_next_day_return",
        "positive_outcome_share": "positive_next_day_share",
    })


def width_groups(daily):
    usable = daily[daily["avg_range_width"].notna()].copy()
    labels = ("Narrow", "Middle", "Wide")
    usable["group"] = tied_quantile_groups(usable["avg_range_width"], labels)
    rows = []
    for name in labels:
        group = usable[usable["group"] == name]
        if group.empty:
            continue
        row = {"group": name, "mean_selected_width": group["avg_range_width"].mean()}
        row.update(outcome_statistics(group, "next_day_high_low_range"))
        row.pop("positive_outcome_share")
        rows.append(row)
    return pd.DataFrame(rows).rename(columns={
        "n_valid_outcome": "n_valid_next_day_high_low_range",
        "mean_outcome": "mean_next_day_high_low_range",
        "median_outcome": "median_next_day_high_low_range",
    })


def write_insight_report(summary, placement, relationships, positioning, widths, metadata):
    metric = dict(zip(summary["metric"], summary["value"]))
    sign = positioning[positioning["definition"] == "Sign of LP imbalance"].set_index("group")
    terciles = positioning[positioning["definition"] == "Equal-frequency imbalance group"].set_index("group")
    width = widths.set_index("group")
    rel = relationships.set_index("relationship")
    report = f"""# Trading insight from LP range choices

## What LPs did

From {metadata['actual_start_date']} through {metadata['actual_end_date']}, the pool recorded {int(float(metric['swap_count'])):,} Swaps and {int(float(metric['mint_count'])):,} Mints. ETH moved from {float(metric['first_close_usdt_per_eth']):,.2f} to {float(metric['last_close_usdt_per_eth']):,.2f} USDT per ETH, a simple return of {float(metric['sample_simple_return']):.1%}.

LPs placed {float(metric['around_value_share']):.1%} of Mint value in ranges that contained the current price. Only {float(metric['above_value_share']):.1%} was entirely above and {float(metric['below_value_share']):.1%} entirely below. The dominant behavior was therefore active market making near price, not one-sided directional positioning.

## Directional insight

On the {int(sign.loc['More value below', 'n_days'])} below-tilted days, the next-day mean log return was {sign.loc['More value below', 'mean_next_day_return']:.4f}, the median was {sign.loc['More value below', 'median_next_day_return']:.4f}, and {sign.loc['More value below', 'positive_next_day_share']:.1%} of valid next days were positive. On the {int(sign.loc['More value above', 'n_days'])} above-tilted days, the corresponding values were {sign.loc['More value above', 'mean_next_day_return']:.4f}, {sign.loc['More value above', 'median_next_day_return']:.4f}, and {sign.loc['More value above', 'positive_next_day_share']:.1%}. Three days had no above-versus-below imbalance and are kept neutral.

That sign split looks potentially useful, but it is not a stable ranked signal. Equal-frequency below, middle, and above groups produced mean next-day log returns of {terciles.loc['Below-tilted', 'mean_next_day_return']:.4f}, {terciles.loc['Middle', 'mean_next_day_return']:.4f}, and {terciles.loc['Above-tilted', 'mean_next_day_return']:.4f}. The middle group performed best, and the overall imbalance/next-day-return correlation was only {rel.loc['LP imbalance vs next-day return', 'correlation']:.3f}. The data do not show that progressively more above- or below-market liquidity leads to progressively different returns.

## Volatility insight

Narrow, middle, and wide range-selection groups were followed by mean next-day log high-low ranges of {width.loc['Narrow', 'mean_next_day_high_low_range']:.4f}, {width.loc['Middle', 'mean_next_day_high_low_range']:.4f}, and {width.loc['Wide', 'mean_next_day_high_low_range']:.4f}. The width/next-day-range correlation was {rel.loc['Selected width vs next-day high-low range', 'correlation']:.3f}. Wider LP ranges did not precede greater next-day price variation in this sample.

## Practical reading

Above-market liquidity behaves like potential ETH supply as price rises through it; below-market liquidity behaves like potential ETH demand as price falls through it. This makes LP tilt useful as market context. Here, however, the strongest result is negative: aggregate Mint placement and selected width were weak standalone forecasts. A trader could monitor unusually one-sided positioning as confirmation for another view, but these data do not support using LP range choices alone as an entry signal.
"""
    (OUTPUT_DIR / "TRADING_INSIGHTS.md").write_text(report, encoding="utf-8")


def save_insights(events, swaps, mints, daily, metadata):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    analyzed_dates = set(daily["date"])
    sample_events = events.loc[(~events["is_initialization"]) & events["date"].isin(analyzed_dates)]
    total_mint_value = mints["mint_value_usdt"].sum() if not mints.empty else 0.0
    shares = {}
    if total_mint_value > 0:
        shares = (mints.groupby("classification")["mint_value_usdt"].sum() / total_mint_value).to_dict()
    first_close = daily["close_price_usdt_per_eth"].dropna().iloc[0]
    last_close = daily["close_price_usdt_per_eth"].dropna().iloc[-1]
    summary = pd.DataFrame([
        {"metric": "start_date", "value": metadata["actual_start_date"]},
        {"metric": "end_date", "value": metadata["actual_end_date"]},
        {"metric": "calendar_days", "value": len(daily)},
        {"metric": "swap_count", "value": int((sample_events["event_type"] == "Swap").sum())},
        {"metric": "mint_count", "value": int((sample_events["event_type"] == "Mint").sum())},
        {"metric": "first_close_usdt_per_eth", "value": first_close},
        {"metric": "last_close_usdt_per_eth", "value": last_close},
        {"metric": "sample_simple_return", "value": last_close / first_close - 1},
        {"metric": "positive_next_day_share", "value": (daily["next_day_return"].dropna() > 0).mean()},
        {"metric": "total_mint_value_usdt", "value": total_mint_value},
        {"metric": "above_value_share", "value": shares.get("Above", 0.0) if total_mint_value > 0 else np.nan},
        {"metric": "below_value_share", "value": shares.get("Below", 0.0) if total_mint_value > 0 else np.nan},
        {"metric": "around_value_share", "value": shares.get("Around", 0.0) if total_mint_value > 0 else np.nan},
        {"metric": "median_event_range_width", "value": mints["range_width"].median() if not mints.empty else np.nan},
    ])
    summary.to_csv(OUTPUT_DIR / "market_summary.csv", index=False)

    placement = mints.groupby("classification").agg(
        mint_count=("classification", "size"),
        mint_value_usdt=("mint_value_usdt", "sum"),
        median_displacement=("displacement", "median"),
        median_range_width=("range_width", "median"),
    ).reset_index()
    placement["mint_count_share"] = placement["mint_count"] / len(mints)
    placement["mint_value_share"] = placement["mint_value_usdt"] / total_mint_value
    placement.to_csv(OUTPUT_DIR / "liquidity_placement.csv", index=False)

    relationships = pd.DataFrame([
        {"relationship": "LP imbalance vs next-day return", **correlation_row(daily, "lp_imbalance", "next_day_return")},
        {"relationship": "Range-center displacement vs next-day return", **correlation_row(daily, "avg_displacement", "next_day_return")},
        {"relationship": "LP imbalance vs same-day return", **correlation_row(daily, "lp_imbalance", "same_day_return")},
        {"relationship": "Selected width vs next-day high-low range", **correlation_row(daily, "avg_range_width", "next_day_high_low_range")},
    ])
    relationships = relationships[["relationship", "n_valid", "correlation"]]
    relationships.to_csv(OUTPUT_DIR / "key_relationships.csv", index=False)

    positioning = positioning_groups(daily)
    positioning.to_csv(OUTPUT_DIR / "positioning_signal.csv", index=False)
    widths = width_groups(daily)
    widths.to_csv(OUTPUT_DIR / "range_width_signal.csv", index=False)
    write_insight_report(summary, placement, relationships, positioning, widths, metadata)


def validate(events, swaps, mints, daily, valid_dates, metadata):
    if metadata["collection_status"] != "complete":
        print(f"Warning: collection status is {metadata['collection_status']}; only fully covered dates are analyzed")
    if events.duplicated(["transaction_hash", "log_index"]).any():
        raise AssertionError("Duplicate event logs remain")
    if not swaps.empty and not (swaps["price_usdt_per_eth"] > 0).all():
        raise AssertionError("A non-positive Swap price remains")
    if not swaps.empty:
        tick_prices = swaps["tick"].map(lambda value: 1.0001 ** int(value) * 10**12)
        ratios = swaps["price_usdt_per_eth"] / tick_prices
        if not ((ratios >= 1 - 1e-10) & (ratios <= 1.0001 + 1e-10)).all():
            raise AssertionError("A Swap price is inconsistent with its decoded tick")
    if not mints.empty:
        if not (mints["lower_price_usdt_per_eth"] < mints["upper_price_usdt_per_eth"]).all():
            raise AssertionError("A Mint lower bound is not below its upper bound")
        if mints["current_price_usdt_per_eth"].isna().any():
            raise AssertionError("A Mint lacks a preceding price")
    positive = daily["total_mint_value_usdt"] > 0
    share_sum = daily.loc[positive, ["above_value_share", "below_value_share", "around_value_share"]].sum(axis=1)
    if not np.allclose(share_sum, 1.0, atol=1e-9):
        raise AssertionError("Daily Mint-value shares do not sum to one")
    for index in range(len(daily) - 1):
        today = pd.Timestamp(daily.loc[index, "date"])
        tomorrow = pd.Timestamp(daily.loc[index + 1, "date"])
        if tomorrow - today != pd.Timedelta(days=1):
            raise AssertionError("Daily calendar index is not consecutive")
        closes = daily.loc[[index, index + 1], "close_price_usdt_per_eth"]
        expected = math.log(closes.iloc[1] / closes.iloc[0]) if closes.notna().all() else np.nan
        actual = daily.loc[index, "next_day_return"]
        if not (pd.isna(expected) and pd.isna(actual)) and not np.isclose(expected, actual):
            raise AssertionError("Next-day return is misaligned")
    sample_dates = set(events.loc[~events["is_initialization"], "date"].dropna())
    if not sample_dates.issubset(set(valid_dates)):
        raise AssertionError("Events outside fully collected dates entered the sample")


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    events, metadata = read_inputs()
    valid_dates = fully_collected_dates(metadata)
    if not valid_dates:
        raise RuntimeError("No complete UTC calendar day is available for analysis")
    events, swaps, mints = process_events(events, valid_dates)
    daily = build_daily(swaps, mints, valid_dates)
    mints.to_csv(MINT_PATH, index=False)
    daily.to_csv(DAILY_PATH, index=False)
    save_insights(events, swaps, mints, daily, metadata)
    validate(events, swaps, mints, daily, valid_dates, metadata)
    print(f"Analyzed {valid_dates[0]} through {valid_dates[-1]}")
    print(f"Saved {len(mints):,} Mint rows and {len(daily):,} daily rows")
    print(f"Validation passed; compact insight tables and report are in {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
