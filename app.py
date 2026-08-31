from pathlib import Path

import pandas as pd
import streamlit as st

from build_history import build_window, window_for
from charts import (
    all_merit_orders,
    battery_fleet_charge,
    depth_chart,
    depth_series,
    merit_order_chart,
    monotonicity_check,
    offer_percentile,
    offer_percentile_chart,
    revenue_by_fuel,
    stack_chart,
    unit_envelope,
)
from constants import (
    DEEP_MW,
    DEMO_DATES,
    FUEL_ORDER,
    HISTORY_DAYS,
    SHALLOW_MW,
)
from data_pull import build_live, stack_units

# streamlit needs to be told how to hash a dataframe before it can cache on one
FRAME_HASH = {
    pd.DataFrame: lambda d: (
        d.shape,
        tuple(d.columns),
        int(pd.util.hash_pandas_object(d, index=True).sum()),
    )
}

st.set_page_config(page_title="GB Power Fundamentals Dashboard", layout="wide")

PRICE_COLS = ["settlementPeriod", "price", "time"]  # shape of an empty price frame


# one market index price per period with a clock time
def prep_price(mid, settlement_date):
    if mid.empty or "dataProvider" not in mid.columns:
        return pd.DataFrame(columns=PRICE_COLS)

    # two providers publish so pick one
    price = mid[mid["dataProvider"] == "APXMIDP"].copy()
    if price.empty:
        return pd.DataFrame(columns=PRICE_COLS)

    price["time"] = pd.to_datetime(settlement_date) + pd.to_timedelta(
        (price["settlementPeriod"] - 1) * 30, unit="m"
    )
    price = price.drop_duplicates("settlementPeriod", keep="last")
    return price.sort_values("time")


@st.cache_data
def load_day(settlement_date):  # read a cached day off disk

    paths = {
        "bmu": Path(f"cache/bmu_{settlement_date}.parquet"),
        "mid": Path(f"cache/MID_{settlement_date}.parquet"),
        "bod": Path(f"cache/bid_off_{settlement_date}.parquet"),
    }
    missing = [str(p) for p in paths.values() if not p.exists()]
    if missing:
        return None, missing

    bmu = pd.read_parquet(paths["bmu"])
    mid = pd.read_parquet(paths["mid"])
    bod = pd.read_parquet(paths["bod"])
    return (bmu, prep_price(mid, settlement_date), bod), []


@st.cache_data(ttl=180, show_spinner="Pulling live data from Elexon")
def load_live(nonce=0):  # pull today up to the current period, nonce forces a refresh
    settlement_date, current_period, bmu, mid, bod = build_live()
    return settlement_date, current_period, bmu, prep_price(mid, settlement_date), bod


@st.cache_data(hash_funcs=FRAME_HASH)
def build_views(bmu):  # everything the generation and battery tabs need
    generation = stack_units(bmu)
    generation["fuelType"] = pd.Categorical(
        generation["fuelType"], categories=FUEL_ORDER, ordered=True
    )

    stack = (
        generation.groupby(["settlementPeriod", "fuelType"], observed=True)["avg_mw"]
        .sum()
        .reset_index()
        .sort_values(["settlementPeriod", "fuelType"])
    )

    batteries = generation[generation["fuelType"] == "BATTERY"].copy()
    # a unit that never posted a mel would drag the fleet share down for no reason
    active_units = batteries.loc[
        batteries["mel_min"] > 0, "nationalGridBmUnit"
    ].unique()
    bat = batteries[batteries["nationalGridBmUnit"].isin(active_units)].copy()
    bat["mil_abs"] = bat["mil_min"].abs()

    caps = bat.drop_duplicates("nationalGridBmUnit")
    # fleet nameplate is the denominator
    max_generation = caps["generationCapacity"].sum()
    max_charge = caps["demandCapacity"].abs().sum()

    # mel is what the fleet can export for half an hour so it stands in for charge
    sog = (
        bat.groupby("settlementPeriod")["mel_min"].sum() / max_generation
    ).reset_index(name="export_availability")
    soc = (bat.groupby("settlementPeriod")["mil_abs"].sum() / max_charge).reset_index(
        name="import_availability"
    )
    battery_fleet = sog.merge(soc, on="settlementPeriod", how="left")

    n_inactive = batteries["nationalGridBmUnit"].nunique() - len(active_units)
    mw_inactive = (
        batteries.drop_duplicates("nationalGridBmUnit")["generationCapacity"].sum()
        - max_generation
    )

    battery_units = sorted(batteries["nationalGridBmUnit"].unique())

    return generation, stack, battery_fleet, n_inactive, mw_inactive, battery_units


@st.cache_data(hash_funcs=FRAME_HASH, show_spinner="Building the offer stacks")
def build_stacks(bod, bmu):  # slowest step so cache it
    return all_merit_orders(bod, bmu)


@st.cache_data(hash_funcs=FRAME_HASH)
def check_ordering(bod):
    return monotonicity_check(bod)


@st.cache_data(hash_funcs=FRAME_HASH)
def build_depths(_stacks, bod, bmu):
    return depth_series(bod, bmu, SHALLOW_MW, DEEP_MW, stacks=_stacks)


@st.cache_data(hash_funcs=FRAME_HASH)
def unit_percentile(_stacks, bod, bmu, unit_id):
    return offer_percentile(bod, bmu, unit_id, stacks=_stacks)


@st.cache_data
def load_committed_history():  # the bands shipped with the repo
    depth_path = Path("cache/depth_history.parquet")
    soc_path = Path("cache/soc_history.parquet")
    return (
        pd.read_parquet(depth_path) if depth_path.exists() else None,
        pd.read_parquet(soc_path) if soc_path.exists() else None,
    )


# build bands around the chosen day instead
@st.cache_data(show_spinner="Building reference bands")
def load_rolling_history(settlement_date, days=HISTORY_DAYS):

    start, end = window_for(settlement_date, days)
    depth, soc = build_window(start, end)
    return (
        depth if not depth.empty else None,
        soc if not soc.empty else None,
    )


def fmt(v, prefix="£"):  # money or n/a for a missing value
    return f"{prefix}{v:,.0f}" if pd.notna(v) else "n/a"


st.title("GB Power Fundamentals Dashboard")

st.sidebar.header("Data Source")
mode = st.sidebar.radio("Mode", ["Cached day", "Live"])

if "live_nonce" not in st.session_state:
    st.session_state.live_nonce = 0

if mode == "Live":
    if st.sidebar.button("Refresh"):  # bump the nonce to bust the cache
        st.session_state.live_nonce += 1
    date, live_period, bmu, price, bod = load_live(st.session_state.live_nonce)
    st.sidebar.caption(
        f"{date}, period {live_period}. "
        f"Updated {pd.Timestamp.now(tz='Europe/London').strftime('%H:%M')}."
    )
    default_period = live_period
else:
    date = st.sidebar.selectbox("Date", DEMO_DATES)
    day, missing = load_day(date)
    if day is None:
        st.error(
            f"No parquet cache for {date}. Run `python data_pull.py` to build it, "
            "or switch to Live in the sidebar."
        )
        st.caption("Missing: " + ", ".join(missing))
        st.stop()
    bmu, price, bod = day
    live_period = None
    default_period = 20  # mid morning

if bod.empty or bmu.empty:
    st.warning(f"No data published yet for {date}.")
    st.stop()

generation, stack, battery_fleet, n_inactive, mw_inactive, battery_units = build_views(
    bmu
)

stacks = build_stacks(bod, bmu)
violations = check_ordering(bod)

st.sidebar.header("Reference Bands")
band_mode = st.sidebar.radio(
    "Window",
    ["Committed", f"{HISTORY_DAYS} days before this date"],
    help=(
        "Committed uses the bands shipped with the repo. The rolling option "
        "builds the window around the selected day."
    ),
)

if band_mode == "Committed":
    depth_history, soc_history = load_committed_history()
else:
    depth_history, soc_history = load_rolling_history(date)

# live mode has fewer than 48
available_periods = sorted(bod["settlementPeriod"].unique())
if default_period not in available_periods and available_periods:
    default_period = available_periods[-1]

median_label = "vs periods so far" if mode == "Live" else "vs day median"

gen_tab, bat_tab = st.tabs(["Generation", "Batteries"])

with gen_tab:
    st.plotly_chart(stack_chart(stack, date, price), use_container_width=True)

    exports = generation.loc[generation["avg_mw"] > 0]  # ignore charging and imports
    total_mwh = exports["avg_mw"].sum() * 0.5
    peak_mw = exports.groupby("settlementPeriod")["avg_mw"].sum().max()

    col_1, col_2 = st.columns(2)
    col_1.metric("Total Exported", f"{total_mwh:,.0f} MWh")
    col_2.metric("Peak Export", f"{peak_mw:,.0f} MW")

    summary = revenue_by_fuel(generation, price)
    st.caption(
        f"Valued across {summary.attrs['n_priced']} of "
        f"{summary.attrs['n_total']} periods shown."
    )
    st.dataframe(
        summary,
        column_config={
            "mwh": st.column_config.NumberColumn("MWh", format="%,.0f"),
            "revenue": st.column_config.NumberColumn("Revenue", format="£%,.0f"),
            "cost": st.column_config.NumberColumn("Cost", format="£%,.0f"),
            "profit": st.column_config.NumberColumn("Profit", format="£%,.0f"),
            "captured_price": st.column_config.NumberColumn(
                "Captured Price", format="£%,.2f/MWh"
            ),
        },
        use_container_width=True,
    )
    st.caption("Excludes balancing and ancillary revenue.")

    st.subheader("Cost of Turn-Up")

    depths = build_depths(stacks, bod, bmu)
    history = depth_history

    current_period = st.selectbox(
        "Select Period",
        options=available_periods,
        index=available_periods.index(default_period) if available_periods else 0,
    )

    row_match = depths.loc[depths["settlementPeriod"] == current_period]
    price_match = price.loc[price["settlementPeriod"] == current_period, "price"]
    p = price_match.iloc[0] if len(price_match) else float("nan")

    # fall back to the day median
    ref, ref_label = depths["deep"].median(), median_label
    if history is not None:
        hit = history.loc[history["settlementPeriod"] == current_period, "deep_median"]
        if len(hit):
            ref = hit.iloc[0]
            ref_label = f"vs usual for period {current_period}"

    if len(row_match):
        row = row_match.iloc[0]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric(f"{SHALLOW_MW} MW Turn-Up", fmt(row["shallow"]))
        c2.metric(
            f"{DEEP_MW:,} MW Turn-Up",
            fmt(row["deep"]),
            delta=(
                f"{fmt(row['deep'] - ref)} {ref_label}"
                if pd.notna(row["deep"]) and pd.notna(ref)
                else None
            ),
        )
        c3.metric(f"{SHALLOW_MW} to {DEEP_MW:,} MW Spread", fmt(row["steepness"]))
        c4.metric("Market Index", fmt(p))

    if not depths.empty:
        st.plotly_chart(
            depth_chart(
                depths,
                price,
                date,
                SHALLOW_MW,
                DEEP_MW,
                current_period,
                history=history,
            ),
            use_container_width=True,
        )

    if pd.notna(p):
        st.plotly_chart(
            merit_order_chart(date, current_period, bod, bmu, p),
            use_container_width=True,
        )
        if not violations.empty:
            n_units = violations["nationalGridBmUnit"].nunique()
            st.warning(
                f"{len(violations)} unit-periods across {n_units} units post offer "
                "prices that fall as the pair number moves outward."
            )
    else:
        st.info(f"No market index price published yet for period {current_period}.")

with bat_tab:
    # interconnector legs are not assets so leave them out of the picker
    unit_options = sorted(
        generation.loc[
            generation["fuelType"] != "INTERCONNECTOR", "nationalGridBmUnit"
        ].unique()
    )
    default_ix = unit_options.index("THURB-3") if "THURB-3" in unit_options else 0
    current_bm_unit = st.selectbox(
        "Select BM Unit", options=unit_options, index=default_ix
    )
    unit_env, spent, made, margin = unit_envelope(
        generation, current_bm_unit, price, date
    )

    st.plotly_chart(unit_env, use_container_width=True)
    st.caption(
        "Declared position valued at the market index price. Excludes balancing "
        "and ancillary revenue."
    )
    col1, col2, col3 = st.columns(3)
    col1.metric("Net Value", f"£{margin:,.0f}")
    col2.metric("Value of Exports", f"£{made:,.0f}")
    col3.metric("Value of Imports", f"£{abs(spent):,.0f}")

    st.subheader("Position in the Battery Offer Stack")

    if current_bm_unit not in battery_units:
        st.info(
            f"{current_bm_unit} is not classified as storage, so there is no battery "
            "fleet to compare it against. Select a battery unit for this panel."
        )
    else:
        pct = unit_percentile(stacks, bod, bmu, current_bm_unit)

        if pct.empty:
            st.info(
                "No battery unit posted offer volume with headroom behind it on "
                f"{date}, so there is no fleet distribution to place "
                f"{current_bm_unit} against."
            )
        else:
            m1, m2, m3 = st.columns(3)
            m1.metric("Median Position", f"{pct['percentile'].median():.0%}")
            m2.metric("Median Offer", fmt(pct["my_offer"].median()))
            m3.metric("Fleet Median Offer", fmt(pct["fleet_median"].median()))

            st.plotly_chart(
                offer_percentile_chart(pct, current_bm_unit, date),
                use_container_width=True,
            )

    st.plotly_chart(
        battery_fleet_charge(
            battery_fleet,
            price,
            date,
            n_inactive,
            mw_inactive,
            history=soc_history,
        ),
        use_container_width=True,
    )
