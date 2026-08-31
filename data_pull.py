import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import requests
from tqdm import tqdm

from constants import (
    ADMIN_CABLES,
    ADMIN_FUEL,
    DEMO_DATES,
    EXCLUDE_INTERCONNECTOR_OWNERS,
    GEN_CODES,
    INTERCONNECTOR_ADMIN_PARTIES,
    INTERCONNECTOR_CODE,
    STACK_CODES,
)

BASE = "https://data.elexon.co.uk/bmrs/api/v1"  # elexon insights api
TIMEOUT = 30  # seconds before a request gives up
WORKERS = 8  # threads used per dataset pull

DESC_COLS = [  # columns kept from the bm unit reference data
    "leadPartyName",
    "fpnFlag",
    "fuelType",
    "bmUnitType",
    "demandCapacity",
    "generationCapacity",
    "gspGroupName",
    "productionOrConsumptionFlag",
]


def now_london():
    return pd.Timestamp.now(tz="Europe/London")  # periods run on uk local time


def current_settlement_period(ts=None):
    ts = ts or now_london()
    return int((ts.hour * 60 + ts.minute) / 30) + 1  # 48 half hour periods a day


def live_date():
    return now_london().strftime("%Y-%m-%d")


def live_periods(lookahead=2):
    # a bit ahead of now
    return range(1, min(current_settlement_period() + lookahead + 1, 49))


def _concat(frames):
    frames = [f for f in frames if f is not None and not f.empty]  # drop failed pulls
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _safe(fn, *args):  # retry a request a couple of times before giving up
    for attempt in range(3):
        try:
            return fn(*args)
        except requests.RequestException:
            if attempt == 2:
                return pd.DataFrame()
            time.sleep(2**attempt)  # wait longer each retry
    return pd.DataFrame()


# one call per period at once
def _parallel(fn, settlement_date, periods, extra=None, workers=WORKERS):
    args = [(settlement_date, p) + ((extra,) if extra else ()) for p in periods]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return _concat(list(ex.map(lambda a: _safe(fn, *a), args)))


def get_bm_desc():
    url = f"{BASE}/reference/bmunits/all"  # static list of every bm unit
    r = requests.get(url, headers={"accept": "application/json"}, timeout=TIMEOUT)
    r.raise_for_status()
    return pd.DataFrame(r.json())


def get_fpn_one_date(settlement_date, settlement_period, dataset):
    url = f"{BASE}/balancing/physical/all"  # pn mels and mils all live here
    r = requests.get(
        url,
        params={
            "dataset": dataset,
            "settlementDate": settlement_date,
            "settlementPeriod": settlement_period,
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return pd.DataFrame(r.json()["data"])


def build_period_output(settlement_date, dataset, periods=range(1, 49)):
    # name the output column
    col_name = {"MELS": "mel", "MILS": "mil", "PN": "mwh"}.get(dataset, "error")
    keys = ["nationalGridBmUnit", "settlementDate", "settlementPeriod"]

    fpn_df = _parallel(get_fpn_one_date, settlement_date, list(periods), dataset)

    if fpn_df.empty:
        return pd.DataFrame(
            columns=keys + [col_name, f"{col_name}_min", f"{col_name}_max"]
        )

    fpn_df[["timeFrom", "timeTo"]] = fpn_df[["timeFrom", "timeTo"]].apply(
        pd.to_datetime
    )
    # a declaration ramps from levelFrom to levelTo so energy is the trapezoid area
    fpn_df["time_taken"] = (
        fpn_df["timeTo"] - fpn_df["timeFrom"]
    ).dt.total_seconds() / 60
    fpn_df[col_name] = (
        (fpn_df["levelFrom"] + fpn_df["levelTo"]) / 2 * (fpn_df["time_taken"] / 60)
    )

    g = fpn_df.groupby(keys)  # one row per unit per period
    return pd.concat(
        [
            g[col_name].sum(),
            g[["levelFrom", "levelTo"]].min().min(axis=1).rename(f"{col_name}_min"),
            g[["levelFrom", "levelTo"]].max().max(axis=1).rename(f"{col_name}_max"),
        ],
        axis=1,
    ).reset_index()


def get_SEL(settlement_date, settlement_period):
    url = f"{BASE}/balancing/dynamic/all"
    r = requests.get(
        url,
        params={
            "settlementDate": settlement_date,
            "settlementPeriod": settlement_period,
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    sel_unfil = pd.DataFrame(r.json()["data"])
    if sel_unfil.empty or "dataset" not in sel_unfil.columns:
        return pd.DataFrame()
    # endpoint returns several datasets
    return sel_unfil[sel_unfil["dataset"] == "SEL"].rename(columns={"value": "sel"})


def get_bo(settlement_date, settlement_period):
    url = f"{BASE}/balancing/bid-offer/all"  # bid and offer prices per unit
    r = requests.get(
        url,
        params={
            "settlementDate": settlement_date,
            "settlementPeriod": settlement_period,
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return pd.DataFrame(r.json()["data"])


def build_day(settlement_date, bm_desc, DESC_COLS, periods=range(1, 49)):
    keys = ["nationalGridBmUnit", "settlementDate", "settlementPeriod"]

    # the four datasets are independent so fetch them at the same time
    with ThreadPoolExecutor(max_workers=4) as ex:
        f_sel = ex.submit(_parallel, get_SEL, settlement_date, list(periods))
        f_pn = ex.submit(build_period_output, settlement_date, "PN", periods)
        f_mel = ex.submit(build_period_output, settlement_date, "MELS", periods)
        f_mil = ex.submit(build_period_output, settlement_date, "MILS", periods)

        sel, fpn, mel, mil = (
            f_sel.result(),
            f_pn.result(),
            f_mel.result(),
            f_mil.result(),
        )

    if not sel.empty:
        # keep the last version published
        sel = sel.sort_values("time").drop_duplicates(
            ["nationalGridBmUnit", "settlementPeriod"], keep="last"
        )
    else:
        sel = pd.DataFrame(columns=["nationalGridBmUnit", "settlementPeriod", "sel"])

    desc = bm_desc[["nationalGridBmUnit"] + DESC_COLS].drop_duplicates(
        "nationalGridBmUnit"
    )

    bmu_data = (
        fpn.merge(mel, on=keys, how="left")
        .merge(mil, on=keys, how="left")
        .merge(
            sel[["nationalGridBmUnit", "settlementPeriod", "sel"]],
            on=["nationalGridBmUnit", "settlementPeriod"],
            how="left",
        )
        .merge(desc, on="nationalGridBmUnit", how="left")
    )

    # catches a duplicate key
    assert len(bmu_data) == len(fpn), "merge changed row count"
    return bmu_data


MANUAL_FUEL = {  # lead parties elexon leaves unlabelled
    "EDF Energy Nuclear Generation": "NUCLEAR",
    "Drax Pumped Storage Limited": "PS",
}

MANUAL_BATTERY_UNITS = [  # batteries the capacity ratio test misses
    "ARNKB-2",
    "WNCRB-1",
    "RCKFB-1",
    "RDFRB-1",
    "OVRHB-1",
    "TYLNB-1",
    "HIRWB-1",
    "MELKB-1",
    "AG-EDF01C",
    "AG-FLX00L",
]

BATTERY_PARTIES = [  # lead parties that only own storage
    "ZENOBE KILMARNOCK SOUTH LTD",
    "Zenobe Capenhurst Limited",
    "Field Newport Ltd",
    "Arenko Cleantech Limited",
    "Tollgate Energy Storage Ltd",
    "Tesla Motors Limited",
    "BESS Holdco 2 Limited",
]


# prefix ends in g for the generation leg and d for the demand leg
# the rest names the cable, neso pairs put it in the suffix
def label_interconnectors(df):
    inter = df["bmUnitType"] == INTERCONNECTOR_CODE  # rows to work on
    if not inter.any():
        return df

    # split returns a list column on arrow strings so extract instead
    # a few ids use an underscore not a hyphen
    ids = df.loc[inter, "nationalGridBmUnit"].astype("string")
    prefix = ids.str.extract(r"^([^-_]+)", expand=False)

    df.loc[inter, "cableCode"] = prefix.str.slice(0, -1)
    df.loc[inter, "interconnectorLeg"] = prefix.str.slice(-1).map(
        {"G": "import", "D": "export"}
    )
    df.loc[inter, "fuelType"] = "INTERCONNECTOR"

    # neso own pairs
    admin = inter & df["leadPartyName"].isin(INTERCONNECTOR_ADMIN_PARTIES)
    df.loc[admin, "fuelType"] = ADMIN_FUEL

    # owner pairs carry a cable name in the suffix
    suffix = ids.str.extract(r"[-_](.+)$", expand=False)
    df["interconnectorOwner"] = False
    df.loc[suffix.index, "interconnectorOwner"] = (
        suffix.isin(ADMIN_CABLES).fillna(False).to_numpy()
    )
    df.loc[admin, "interconnectorOwner"] = False
    return df


# generation types plus interconnector users
def stack_units(df):
    # admin pair would double count
    keep = df["bmUnitType"].isin(STACK_CODES) & (df["fuelType"] != ADMIN_FUEL)
    if EXCLUDE_INTERCONNECTOR_OWNERS and "interconnectorOwner" in df.columns:
        keep &= ~df["interconnectorOwner"].fillna(False).astype(bool)
    return df[keep].copy()


CAPACITY_COLS = ["generationCapacity", "demandCapacity"]  # registered mw either way


def classify(df):
    # the reference endpoint returns capacities as strings
    for col in CAPACITY_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # arrow rejects a string assignment into an all null column
    df["fuelType"] = df["fuelType"].astype(object)

    m = df["fuelType"].isna()
    df.loc[m, "fuelType"] = df.loc[m, "leadPartyName"].map(MANUAL_FUEL)

    # first so an interconnector leg never falls into the capacity tests
    df = label_interconnectors(df)

    gen_cap = df["generationCapacity"]
    # storage registers both ways about equally
    ratio = df["demandCapacity"].abs() / gen_cap.where(gen_cap > 0)
    unclassified = df["fuelType"].isna() | (df["fuelType"] == "OTHER")

    gen_mask = df["bmUnitType"].isin(GEN_CODES)
    df.loc[ratio.between(0.85, 1.2) & unclassified & gen_mask, "fuelType"] = "BATTERY"

    df.loc[
        df["nationalGridBmUnit"].isin(MANUAL_BATTERY_UNITS) & gen_mask, "fuelType"
    ] = "BATTERY"

    still_unlabelled = df["fuelType"].isna() | (df["fuelType"] == "OTHER")
    df.loc[
        still_unlabelled & gen_mask & df["leadPartyName"].isin(BATTERY_PARTIES),
        "fuelType",
    ] = "BATTERY"

    df["fuelType"] = df["fuelType"].fillna("UNKNOWN")  # shown on the charts not dropped
    return df


def cached(path, build):  # read the parquet if it exists otherwise build and save
    if path.exists():
        print(f"cache hit {path.name}")
        return pd.read_parquet(path)
    df = build()
    df.to_parquet(path)
    return df


def build_bid_offer(settlement_date, periods=range(1, 49)):
    return _parallel(get_bo, settlement_date, list(periods))


def get_mid(date, periods=range(1, 49)):
    # volume weighted price of short term trades
    url = f"{BASE}/balancing/pricing/market-index"
    r = requests.get(
        url,
        params={
            "from": date,
            "to": date,
            "settlementPeriodFrom": min(periods),
            "settlementPeriodTo": max(periods),
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return pd.DataFrame(r.json()["data"])


def build_bmu(settlement_date, bm_desc, periods=range(1, 49)):
    bmu_data = build_day(settlement_date, bm_desc, DESC_COLS, periods)
    bmu_data["avg_mw"] = bmu_data["mwh"] / 0.5  # back to average mw over the half hour
    for col in CAPACITY_COLS:
        bmu_data[col] = pd.to_numeric(bmu_data[col], errors="coerce")
    return bmu_data


def build_live(lookahead=2):
    settlement_date = live_date()
    periods = live_periods(lookahead)
    bm_desc = get_bm_desc().drop_duplicates("nationalGridBmUnit")

    bmu_data = classify(build_bmu(settlement_date, bm_desc, periods))
    mid = get_mid(settlement_date, periods)
    bod = build_bid_offer(settlement_date, periods)

    return settlement_date, current_settlement_period(), bmu_data, mid, bod


def main():
    Path("cache").mkdir(exist_ok=True)
    bm_desc = get_bm_desc().drop_duplicates("nationalGridBmUnit")

    for settlement_date in tqdm(DEMO_DATES, desc="Days"):
        p_bmu = Path(f"cache/bmu_{settlement_date}.parquet")
        p_mid = Path(f"cache/MID_{settlement_date}.parquet")
        p_bo = Path(f"cache/bid_off_{settlement_date}.parquet")

        cached(p_mid, lambda: get_mid(settlement_date))
        cached(p_bo, lambda: build_bid_offer(settlement_date))
        bmu_data = cached(p_bmu, lambda: classify(build_bmu(settlement_date, bm_desc)))

        # rough check on how much the classifier missed
        gen = bmu_data[bmu_data["bmUnitType"].isin(GEN_CODES)]
        total = gen["avg_mw"].abs().sum()
        if total:
            unk = gen.loc[gen["fuelType"] == "UNKNOWN", "avg_mw"].abs().sum()
            print(f"{settlement_date}: {unk / total:.1%} of generation MW unlabelled")


if __name__ == "__main__":
    main()
