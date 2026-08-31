import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
from tqdm import tqdm

from charts import all_merit_orders, depth_series
from constants import CLOCK_CHANGE, DEEP_MW, SHALLOW_MW
from data_pull import (
    build_bid_offer,
    build_bmu,
    classify,
    get_bm_desc,
    stack_units,
)

HISTORY_DIR = Path("cache/history")  # one small file per day
DEPTH_OUT = Path("cache/depth_history.parquet")
SOC_OUT = Path("cache/soc_history.parquet")

DAY_WORKERS = 4  # days built at the same time

DEFAULT_START = "2025-12-07"  # 30 days behind the readme screenshots
DEFAULT_END = "2026-01-05"


def fleet_availability(bmu):  # share of battery nameplate that could export or import
    gen = stack_units(bmu)
    batteries = gen[gen["fuelType"] == "BATTERY"].copy()
    if batteries.empty:
        return pd.DataFrame()

    # units that showed up that day
    active = batteries.loc[batteries["mel_min"] > 0, "nationalGridBmUnit"].unique()
    bat = batteries[batteries["nationalGridBmUnit"].isin(active)].copy()
    if bat.empty:
        return pd.DataFrame()

    bat["mil_abs"] = bat["mil_min"].abs()
    caps = bat.drop_duplicates("nationalGridBmUnit")
    max_generation = caps["generationCapacity"].sum()
    max_charge = caps["demandCapacity"].abs().sum()

    if not max_generation or not max_charge:
        return pd.DataFrame()

    sog = (
        bat.groupby("settlementPeriod")["mel_min"].sum() / max_generation
    ).reset_index(name="export_availability")
    soc = (bat.groupby("settlementPeriod")["mil_abs"].sum() / max_charge).reset_index(
        name="import_availability"
    )
    out = sog.merge(soc, on="settlementPeriod", how="left")
    out["n_active"] = len(active)
    return out


def one_day(settlement_date, bm_desc):
    d_path = HISTORY_DIR / f"depth_{settlement_date}.parquet"
    s_path = HISTORY_DIR / f"soc_{settlement_date}.parquet"

    if d_path.exists() and s_path.exists():  # already built so skip the pull
        return pd.read_parquet(d_path), pd.read_parquet(s_path)

    bmu = classify(build_bmu(settlement_date, bm_desc))
    bod = build_bid_offer(settlement_date)

    if bmu.empty:
        return pd.DataFrame(), pd.DataFrame()

    depths = pd.DataFrame()
    if not bod.empty:
        stacks = all_merit_orders(bod, bmu)
        depths = depth_series(bod, bmu, SHALLOW_MW, DEEP_MW, stacks=stacks)
        depths["date"] = settlement_date
        depths.to_parquet(d_path)

    soc = fleet_availability(bmu)
    if not soc.empty:
        soc["date"] = settlement_date
        soc.to_parquet(s_path)

    return depths, soc


def summarise_depth(frames):  # quartiles of turn up price per settlement period
    hist = pd.concat(frames, ignore_index=True)
    return (
        hist.groupby("settlementPeriod")
        .agg(
            deep_p25=("deep", lambda s: s.quantile(0.25)),
            deep_median=("deep", "median"),
            deep_p75=("deep", lambda s: s.quantile(0.75)),
            shallow_median=("shallow", "median"),
            n_days=("date", "nunique"),
        )
        .reset_index()
    )


def summarise_soc(frames):  # quartiles of fleet availability per settlement period
    hist = pd.concat(frames, ignore_index=True)
    return (
        hist.groupby("settlementPeriod")
        .agg(
            export_p25=("export_availability", lambda s: s.quantile(0.25)),
            export_median=("export_availability", "median"),
            export_p75=("export_availability", lambda s: s.quantile(0.75)),
            import_median=("import_availability", "median"),
            n_days=("date", "nunique"),
        )
        .reset_index()
    )


def build_window(start, end, day_workers=DAY_WORKERS, bm_desc=None):

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)

    # clock change days have 46 or 50 periods so the index would not line up
    dates = [
        d
        for d in pd.date_range(start, end).strftime("%Y-%m-%d")
        if d not in CLOCK_CHANGE
    ]
    if not dates:
        return pd.DataFrame(), pd.DataFrame()

    bm_desc = (
        bm_desc
        if bm_desc is not None
        else get_bm_desc().drop_duplicates("nationalGridBmUnit")
    )

    def safe_day(d):  # one bad day should not kill the whole window
        try:
            return one_day(d, bm_desc)
        except Exception as exc:
            print(f"{d}: skipped ({exc})")
            return pd.DataFrame(), pd.DataFrame()

    depth_frames, soc_frames = [], []
    with ThreadPoolExecutor(max_workers=day_workers) as ex:
        futures = [ex.submit(safe_day, d) for d in dates]
        for fut in tqdm(as_completed(futures), total=len(dates), desc="Days"):
            depths, soc = fut.result()
            if not depths.empty:
                depth_frames.append(depths)
            if not soc.empty:
                soc_frames.append(soc)

    depth_out = summarise_depth(depth_frames) if depth_frames else pd.DataFrame()
    soc_out = summarise_soc(soc_frames) if soc_frames else pd.DataFrame()

    # columns rather than attrs since attrs does not survive a parquet round trip
    for frame in (depth_out, soc_out):
        if not frame.empty:
            frame["window_start"] = dates[0]
            frame["window_end"] = dates[-1]

    return depth_out, soc_out


# trailing window so the day is never inside it
def window_for(settlement_date, days=30):
    end = pd.Timestamp(settlement_date) - pd.Timedelta(days=1)
    return end - pd.Timedelta(days=days - 1), end


def main(start=None, end=None):
    if start is None:
        start, end = DEFAULT_START, DEFAULT_END

    depth_out, soc_out = build_window(pd.Timestamp(start), pd.Timestamp(end or start))

    if not depth_out.empty:
        depth_out.to_parquet(DEPTH_OUT)
        print(
            f"\n{DEPTH_OUT}: median {DEEP_MW:,} MW turn-up across the window "
            f"£{depth_out['deep_median'].median():,.0f}/MWh."
        )

    if not soc_out.empty:
        soc_out.to_parquet(SOC_OUT)
        print(
            f"{SOC_OUT}: median fleet export availability "
            f"{soc_out['export_median'].median():.0%}."
        )

    if depth_out.empty and soc_out.empty:
        print("nothing built")


if __name__ == "__main__":
    main(*sys.argv[1:])
