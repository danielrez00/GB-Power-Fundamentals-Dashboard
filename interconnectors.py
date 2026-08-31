import sys
from pathlib import Path

import pandas as pd
import requests

from constants import (
    ADMIN_CABLES,
    ADMIN_FUEL,
    DEMO_DATES,
    FUELINST_CABLES,
    INTERCONNECTOR_CODE,
)
from data_pull import BASE, TIMEOUT, classify, get_bm_desc

pd.set_option("display.width", 200)
pd.set_option("display.max_rows", 120)

MIN_RANGE_MW = 100  # below this a series is too flat to tell from any other

# columns classify adds
DERIVED = ["family", "cable", "cableCode", "interconnectorLeg", "interconnectorOwner"]


def interconnector_units(desc):  # every interconnector bm unit with its cable and leg
    inter = classify(desc.copy())
    inter = inter[inter["bmUnitType"] == INTERCONNECTOR_CODE].copy()
    if inter.empty:
        return inter

    suffix = (
        inter["nationalGridBmUnit"]
        .astype("string")
        .str.extract(r"[-_](.+)$", expand=False)
    )
    admin = inter["fuelType"] == ADMIN_FUEL
    inter["family"] = admin.map({True: "NESO admin", False: "User"})
    inter["cable"] = inter["cableCode"].where(~admin, suffix.map(ADMIN_CABLES))
    inter["cable"] = inter["cable"].fillna(suffix)
    return inter


def cable_table(inter):  # unit and party counts per cable
    return (
        inter.groupby(["family", "cable", "interconnectorLeg"], dropna=False)
        .agg(
            n_units=("nationalGridBmUnit", "nunique"),
            n_parties=("leadPartyName", "nunique"),
            gen_capacity_mw=("generationCapacity", "sum"),
            dem_capacity_mw=("demandCapacity", "sum"),
            example_unit=("nationalGridBmUnit", "first"),
        )
        .reset_index()
        .sort_values(["family", "cable", "interconnectorLeg"])
    )


# join the cached declarations onto the cable labels
def declared(inter, settlement_date):
    path = Path(f"cache/bmu_{settlement_date}.parquet")
    if not path.exists():
        return pd.DataFrame()

    keys = ["nationalGridBmUnit"] + [c for c in DERIVED if c in inter.columns]

    bmu = pd.read_parquet(path).drop(columns=DERIVED, errors="ignore")
    joined = bmu.merge(
        inter[keys].drop_duplicates("nationalGridBmUnit"),
        on="nationalGridBmUnit",
        how="inner",
    )
    return joined.assign(date=settlement_date)


# column names in the fuelinst response have changed before
def _pick(df, names, label):
    for name in names:
        if name in df.columns:
            return name
    raise KeyError(f"no {label} column in FUELINST response, saw {list(df.columns)}")


# metered flow per cable to check the declarations against
def fuelinst(settlement_date):
    start = pd.Timestamp(settlement_date, tz="Europe/London")
    end = start + pd.Timedelta(days=1)
    r = requests.get(
        f"{BASE}/datasets/FUELINST",
        params={
            "publishDateTimeFrom": start.tz_convert("UTC").strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "publishDateTimeTo": end.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        headers={"accept": "application/json"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    payload = r.json()
    raw = pd.DataFrame(payload["data"] if isinstance(payload, dict) else payload)
    if raw.empty:
        return pd.DataFrame()

    fuel_col = _pick(raw, ["fuelType", "fuel"], "fuel")
    mw_col = _pick(raw, ["generation", "quantity", "value"], "generation")
    time_col = _pick(raw, ["startTime", "publishTime", "measurementTime"], "time")

    # interconnector fuel codes only
    ints = raw[raw[fuel_col].astype("string").str.startswith("INT")].copy()
    if ints.empty:
        return pd.DataFrame()

    unmapped = sorted(set(ints[fuel_col]) - set(FUELINST_CABLES))
    if unmapped:
        print(f"  fuel codes with no cable name: {', '.join(unmapped)}")

    ints["cable"] = ints[fuel_col].map(FUELINST_CABLES).fillna(ints[fuel_col])
    ts = pd.to_datetime(ints[time_col], utc=True).dt.tz_convert("Europe/London")
    # fuelinst is five minutely
    ints["settlementPeriod"] = ts.dt.hour * 2 + ts.dt.minute // 30 + 1
    ints = ints[ts.dt.strftime("%Y-%m-%d") == settlement_date]
    ints[mw_col] = pd.to_numeric(ints[mw_col], errors="coerce")

    return (
        ints.groupby(["settlementPeriod", "cable"])[mw_col]
        .mean()
        .reset_index(name="outturn_mw")
        .assign(date=settlement_date)
    )


def report_structure(inter):
    print(
        f"{len(inter)} interconnector BM units, {inter['cable'].nunique()} cable keys, "
        f"{inter['leadPartyName'].nunique()} lead parties\n"
    )
    print("CABLE TABLE")
    print(cable_table(inter).to_string(index=False))

    owners = inter[inter["interconnectorOwner"].fillna(False).astype(bool)]
    if not owners.empty:
        print("\nOWNER UNITS")
        print(
            owners[["cable", "nationalGridBmUnit", "leadPartyName"]]
            .sort_values("nationalGridBmUnit")
            .to_string(index=False)
        )

    unreadable = inter[inter["interconnectorLeg"].isna()]
    if not unreadable.empty:
        print(
            f"\n{len(unreadable)} units with an unreadable leg: "
            + ", ".join(sorted(unreadable["nationalGridBmUnit"])[:10])
        )


def report_admin(joined):  # check the neso pairs really do declare nothing
    admin = joined[joined["family"] == "NESO admin"]["mwh"].abs().sum()
    users = joined[joined["family"] == "User"]["mwh"].abs().sum()
    total = admin + users
    print("\nNESO ADMINISTRATOR PAIRS")
    print(f"NESO  {admin:>12,.0f} MWh declared, absolute")
    print(f"Users {users:>12,.0f} MWh declared, absolute")
    if total:
        print(f"share {admin / total:>12.1%}")


def report_owners(joined):  # check owner units are not a restatement of the traders
    if "interconnectorOwner" not in joined.columns:
        return
    users = joined[joined["family"] == "User"]
    flag = users["interconnectorOwner"].fillna(False).astype(bool)
    owner, trader = users[flag], users[~flag]
    if owner.empty:
        return

    o = owner.groupby(["date", "settlementPeriod"])["mwh"].sum().mul(2)
    t = trader.groupby(["date", "settlementPeriod"])["mwh"].sum().mul(2)
    o, t = o.align(t, join="inner", fill_value=0.0)

    print("\nOWNER UNITS AGAINST TRADER UNITS")
    print(f"Owner mean   {o.mean():>9,.0f} MW")
    print(f"Trader mean  {t.mean():>9,.0f} MW")
    if o.std() and t.std():
        print(f"Correlation  {o.corr(t):>9.2f}")

    both = (
        users.assign(role=flag.map({True: "owner", False: "trader"}))
        .groupby(["cable", "role"])["mwh"]
        .mean()
        .mul(2)
        .unstack("role")
        .dropna()
        .round(0)
    )
    if not both.empty:
        print("\nMean MW by prefix, where both roles appear")
        print(both.to_string())


# rmse between each declared prefix and each metered cable, lowest is the match
def match_cables(joined, outturn):
    d = (
        joined[joined["family"] == "User"]
        .groupby(["date", "settlementPeriod", "cable"])["mwh"]
        .sum()
        .mul(2)
        .unstack("cable")
    )
    o = outturn.pivot_table(
        index=["date", "settlementPeriod"], columns="cable", values="outturn_mw"
    )
    d, o = d.align(o, join="inner", axis=0)

    matrix = pd.DataFrame(
        {
            prefix: {
                cable: ((d[prefix] - o[cable]) ** 2).mean() ** 0.5
                for cable in o.columns
            }
            for prefix in d.columns
        }
    ).T.round(0)
    return matrix, d, o


def report_matches(matrix, d, o):
    flat_prefix = [c for c in d.columns if (d[c].max() - d[c].min()) < MIN_RANGE_MW]
    flat_cable = [c for c in o.columns if (o[c].max() - o[c].min()) < MIN_RANGE_MW]
    live = matrix.drop(index=flat_prefix, columns=flat_cable, errors="ignore")
    if live.empty:
        print("\nNothing varies enough to match on these days.")
        return

    rows = []
    for prefix in live.index:
        row = live.loc[prefix].sort_values()
        rows.append(
            {
                "prefix": prefix,
                "cable": row.index[0],
                "rmse_mw": row.iloc[0],
                "runner_up": row.index[1] if len(row) > 1 else "",
                "runner_up_rmse": row.iloc[1] if len(row) > 1 else float("nan"),
                "range_mw": round(d[prefix].max() - d[prefix].min()),
            }
        )
    table = pd.DataFrame(rows).sort_values("rmse_mw")
    # how clear the win is
    table["margin"] = (table["runner_up_rmse"] / table["rmse_mw"]).round(1)

    print("\nBEST MATCH PER PREFIX")
    print(table.to_string(index=False))
    # margin is runner up over best, above about 3 the match is clear

    clashes = table["cable"].value_counts()
    clashes = clashes[clashes > 1]
    if len(clashes):
        print(f"Claimed twice: {', '.join(clashes.index)}. Add more days.")
    unmatched = sorted(set(o.columns) - set(table["cable"]))
    if unmatched:
        print(f"Cables no prefix matched: {', '.join(unmatched)}")
    if flat_prefix:
        print(f"Prefixes too flat to match: {', '.join(flat_prefix)}")
    if flat_cable:
        print(f"Cables too flat to match: {', '.join(flat_cable)}")


def report_coverage(d, o):  # declared against metered summed over every cable
    print("\nMEAN MW BY CABLE, positive into GB")
    print(o.mean().round(0).sort_values(ascending=False).to_string())
    print("\nMEAN MW BY PREFIX, positive into GB")
    print(d.mean().round(0).sort_values(ascending=False).to_string())

    gap = d.sum(axis=1) - o.sum(axis=1)
    print("\nCOVERAGE, all cables summed")
    print(f"Mean declared {d.sum(axis=1).mean():>9,.0f} MW")
    print(f"Mean outturn  {o.sum(axis=1).mean():>9,.0f} MW")
    print(f"Mean gap      {gap.mean():>9,.0f} MW")
    print(f"Worst gap     {gap.abs().max():>9,.0f} MW")
    # a gap near one cable typical flow means that cable is missing


def main(*dates):
    dates = list(dates) or DEMO_DATES

    inter = interconnector_units(get_bm_desc().drop_duplicates("nationalGridBmUnit"))
    if inter.empty:
        print("No interconnector BM units in the reference data.")
        return
    report_structure(inter)

    declared_parts, outturn_parts = [], []
    print()
    for date in dates:
        print(f"{date}:")
        dec = declared(inter, date)
        if dec.empty:
            print("  no cached bmu parquet, skipped")
            continue
        declared_parts.append(dec)
        out = fuelinst(date)
        if out.empty:
            print("  declared only, no FUELINST")
            continue
        outturn_parts.append(out)
        print(f"  {dec['cable'].nunique()} prefixes, {out['cable'].nunique()} cables")

    if not declared_parts:
        print("\nNothing to check. Run `python data_pull.py` first.")
        return

    joined = pd.concat(declared_parts, ignore_index=True)
    report_admin(joined)
    report_owners(joined)

    if not outturn_parts:
        print("\nNo FUELINST data, so prefixes cannot be matched to cables.")
        return

    matrix, d, o = match_cables(joined, pd.concat(outturn_parts, ignore_index=True))
    print(f"\nRMSE IN MW, {len(d)} settlement periods")
    print(matrix.to_string())
    report_matches(matrix, d, o)
    report_coverage(d, o)


if __name__ == "__main__":
    main(*sys.argv[1:])
