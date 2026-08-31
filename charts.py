import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from constants import FUEL_ORDER

FUEL_COLOURS = {  # one colour per fuel shared by every chart
    "NUCLEAR": "#A97BD6",
    "WIND": "#3FBF74",
    "NPSHYD": "#3BA9BF",
    "BIOMASS": "#B5834F",
    "CCGT": "#F0993D",
    "COAL": "#8A8A8A",
    "OCGT": "#E05B4A",
    "PS": "#5FA8E8",
    "BATTERY": "#F2C94C",
    "INTERCONNECTOR": "#D470A0",
    "OTHER": "#7A7A7A",
    "UNKNOWN": "#4F4F4F",
}

INK = "#E8E8E8"  # dark theme palette
MUTED = "#8C8C8C"
SUBTLE = "#B0B0B0"
GRID = "rgba(255,255,255,0.08)"
ZERO = "rgba(255,255,255,0.25)"

EXPORT_COLOUR = "#E05B4A"
IMPORT_COLOUR = "#5FA8E8"

SUB_SIZE = 15  # font size for the grey subtitle line


def _window_label(history):  # dates of the reference window for the subtitle
    if "window_start" not in history.columns:
        return "30 days"
    return f"{history['window_start'].iloc[0]} to {history['window_end'].iloc[0]}"


def _sub(text):  # grey second line under a chart title
    if not text:
        return ""
    return f"<br><span style='font-size:{SUB_SIZE}px;color:{SUBTLE}'>{text}</span>"


def _dark_layout(fig):  # shared styling so every figure matches
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=INK),
        title_font=dict(size=20),
    )
    fig.update_xaxes(gridcolor=GRID, zerolinecolor=ZERO)
    fig.update_yaxes(gridcolor=GRID, zerolinecolor=ZERO)
    return fig


def _period_times(df, date):  # settlement period number to clock time
    return pd.to_datetime(date) + pd.to_timedelta(
        (df["settlementPeriod"] - 1) * 30, unit="m"
    )


def stack_chart(stack, date, prices):
    stack = stack.copy()
    stack["time"] = _period_times(stack, date)
    fig = px.bar(
        stack,
        x="time",
        y="avg_mw",
        color="fuelType",
        category_orders={"fuelType": FUEL_ORDER},
        color_discrete_map=FUEL_COLOURS,
        labels={"avg_mw": "MW", "time": "Time", "fuelType": "Fuel"},
    )

    # half hour in milliseconds so bars touch
    fig.update_traces(width=30 * 60 * 1000, selector=dict(type="bar"))

    fig.add_trace(
        go.Scatter(
            x=prices["time"],
            y=prices["price"],
            name="Market Index Price",
            line=dict(color=INK, width=2),
            yaxis="y2",  # price on its own axis on the right
        )
    )

    fig.update_layout(
        title=dict(text=f"Declared Position by Fuel Type: {date}"),
        bargap=0,
        height=800,
        hovermode="x unified",
        yaxis=dict(title="MW"),
        yaxis2=dict(title="£/MWh", overlaying="y", side="right", showgrid=False),
        legend=dict(orientation="h", yanchor="bottom", y=-0.22, x=0),
        margin=dict(t=100),
    )
    fig.update_xaxes(tickformat="%H:%M", dtick=7200000, title="")
    fig.add_hline(y=0, line_width=1, line_color=ZERO)

    return _dark_layout(fig)


def unit_envelope(day, unit_id, prices, date, highlight=None, note=None):
    u = day[day["nationalGridBmUnit"] == unit_id].sort_values("settlementPeriod").copy()
    u = u.merge(
        prices[["settlementPeriod", "price"]], on="settlementPeriod", how="left"
    )
    u["time"] = _period_times(u, date)

    # declared position valued at the market index
    u["cashflow"] = u["mwh"] * u["price"]
    spent = u.loc[u["mwh"] < 0, "cashflow"].sum()  # negative mwh is charging
    made = u.loc[u["mwh"] > 0, "cashflow"].sum()
    margin = made + spent

    fig = go.Figure()

    fig.add_trace(
        go.Scatter(
            x=u["time"],
            y=u["mel_max"],
            name="MEL, Declared Capability",
            line=dict(color=EXPORT_COLOUR, width=1.5, dash="dot"),
        )
    )
    fig.add_trace(
        go.Scatter(
            x=u["time"],
            y=u["avg_mw"],
            name="FPN, Declared Output",
            line=dict(color=INK, width=2.5),
            fill="tonexty",
            fillcolor="rgba(224,91,74,0.20)",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=u["time"],
            y=u["mil_min"],
            name="MIL, Maximum Import",
            line=dict(color=IMPORT_COLOUR, width=1.5, dash="dot"),
        )
    )
    fig.add_trace(
        go.Scatter(
            x=prices["time"],
            y=prices["price"],
            name="Market Index Price",
            line=dict(color=MUTED, width=1.5),
            yaxis="y2",
        )
    )

    fig.update_layout(
        title=dict(text=f"{unit_id}: Declared Output Against Capability {date}"),
        height=600,
        hovermode="x unified",
        yaxis=dict(title="MW", zeroline=True, zerolinecolor=ZERO),
        yaxis2=dict(title="£/MWh", overlaying="y", side="right", showgrid=False),
        xaxis=dict(title="", tickformat="%H:%M", dtick=7200000),
        legend=dict(orientation="h", yanchor="bottom", y=-0.22, x=0),
        margin=dict(t=110),
    )

    if highlight:
        fig.add_vrect(
            x0=highlight[0],
            x1=highlight[1],
            fillcolor=EXPORT_COLOUR,
            opacity=0.14,
            line_width=0,
        )

    if note and highlight:
        start = pd.to_datetime(highlight[0])
        end = pd.to_datetime(highlight[1])
        fig.add_annotation(
            x=start + (end - start) / 2,
            y=u["mel_max"].max() * 1.08,
            text=note,
            showarrow=False,
            font=dict(size=13, color=EXPORT_COLOUR),
        )
    _dark_layout(fig)
    return fig, spent, made, margin


def battery_fleet_charge(  # aggregate mel and mil as a state of charge proxy
    battery_fleet, prices, date, n_inactive=None, mw_inactive=None, history=None
):
    battery_fleet = battery_fleet.copy()
    battery_fleet["time"] = _period_times(battery_fleet, date)

    fig = go.Figure()

    if history is not None and not history.empty:
        h = history.copy()
        h["time"] = _period_times(h, date)
        fig.add_trace(
            go.Scatter(
                x=h["time"],
                y=h["export_p75"],
                # invisible upper edge for the band to fill to
                line=dict(color=MUTED, width=0),
                showlegend=False,
                hoverinfo="skip",
            )
        )
        fig.add_trace(
            go.Scatter(
                x=h["time"],
                y=h["export_p25"],
                name="Export Availability, Usual Range",
                line=dict(color=MUTED, width=0),
                fill="tonexty",
                fillcolor="rgba(140,140,140,0.20)",
                hoverinfo="skip",
            )
        )
        fig.add_trace(
            go.Scatter(
                x=h["time"],
                y=h["export_median"],
                name="Export Availability, Usual Level",
                line=dict(color=MUTED, width=1.5, dash="dash"),
            )
        )

    fig.add_trace(
        go.Scatter(
            x=battery_fleet["time"],
            y=battery_fleet["export_availability"],
            name="Export Availability, Share of Active Fleet",
            line=dict(color=EXPORT_COLOUR, width=2.5),
        )
    )
    fig.add_trace(
        go.Scatter(
            x=battery_fleet["time"],
            y=battery_fleet["import_availability"],
            name="Import Availability, Share of Active Fleet",
            line=dict(color=IMPORT_COLOUR, width=2.5, dash="dash"),
        )
    )
    fig.add_trace(
        go.Scatter(
            x=prices["time"],
            y=prices["price"],
            name="Market Index Price",
            line=dict(color=MUTED, width=1.5),
            yaxis="y2",
        )
    )

    parts = []
    if history is not None and not history.empty:
        parts.append(f"Grey band is the interquartile range: {_window_label(history)}.")
    if n_inactive:
        parts.append(
            f"{n_inactive} registered units ({mw_inactive:,.0f} MW) "
            "posted no MEL and are excluded."
        )
    subtitle = "<br>".join(parts)

    fig.update_layout(
        title=dict(
            text=f"GB Battery Fleet Declared Availability: {date}" + _sub(subtitle)
        ),
        height=600,
        hovermode="x unified",
        yaxis=dict(
            title="Share of Fleet Nameplate",
            range=[0, 1],
            tickformat=".0%",
            zeroline=True,
            zerolinecolor=ZERO,
        ),
        yaxis2=dict(
            title="£/MWh",
            overlaying="y",
            side="right",
            showgrid=False,
            rangemode="tozero",
        ),
        xaxis=dict(title="", tickformat="%H:%M", dtick=7200000),
        legend=dict(orientation="h", yanchor="bottom", y=-0.22, x=0),
        margin=dict(t=130),
    )
    return _dark_layout(fig)


def revenue_by_fuel(generation, price):  # value each fuel at the market index price
    price = price.loc[price["price"].notna(), ["settlementPeriod", "price"]]
    price = price.astype({"settlementPeriod": "int64", "price": "float64"})
    price = price.drop_duplicates("settlementPeriod", keep="last")

    n_total = generation["settlementPeriod"].nunique()

    rev = generation.merge(price, on="settlementPeriod", how="inner")
    n_priced = rev["settlementPeriod"].nunique()

    empty = pd.DataFrame(columns=["mwh", "profit", "revenue", "cost", "captured_price"])
    if rev.empty:
        empty.attrs = {"n_priced": 0, "n_total": n_total}
        return empty

    exported = rev["avg_mw"].clip(lower=0)  # split selling from buying
    imported = rev["avg_mw"].clip(upper=0)

    rev["mwh"] = exported * 0.5
    rev["revenue"] = rev["mwh"] * rev["price"]
    rev["cost"] = imported * 0.5 * rev["price"]
    rev["profit"] = rev["revenue"] + rev["cost"]

    summary = rev.groupby("fuelType", observed=True).agg(
        mwh=("mwh", "sum"),
        profit=("profit", "sum"),
        revenue=("revenue", "sum"),
        cost=("cost", "sum"),
    )

    summary["captured_price"] = summary["revenue"] / summary["mwh"].where(
        summary["mwh"] > 0
    )

    summary = summary[summary["mwh"] > 0]  # drop fuels that never exported
    summary.attrs = {"n_priced": n_priced, "n_total": n_total}
    return summary


# bsc section q says offer prices must not fall as the pair number moves outward
def monotonicity_check(bod):
    empty = pd.DataFrame(columns=["settlementPeriod", "nationalGridBmUnit"])
    if bod.empty or "pairId" not in bod.columns:
        return empty

    offers = bod.loc[(bod["pairId"] > 0) & bod["offer"].notna()]
    if offers.empty:
        return empty

    flags = (
        offers.sort_values(["settlementPeriod", "nationalGridBmUnit", "pairId"])
        .groupby(["settlementPeriod", "nationalGridBmUnit"])["offer"]
        .apply(lambda s: bool((s.diff() < 0).any()))
    )
    hits = flags[flags]
    if hits.empty:
        return empty
    return hits.reset_index()[["settlementPeriod", "nationalGridBmUnit"]]


def merit_order(bod, settlement_period, bmu):

    bod = bod[bod["settlementPeriod"] == settlement_period].copy()

    offers = (
        bod[bod["pairId"] > 0].sort_values(["nationalGridBmUnit", "levelFrom"]).copy()
    )

    # levelFrom is a mark on a number line not a band edge so difference the marks
    offers["vols"] = offers.groupby("nationalGridBmUnit")["levelFrom"].diff()
    # first band measured from the fpn
    offers["vols"] = offers["vols"].fillna(offers["levelFrom"])
    offers = offers[offers["offer"] < 2000]  # drop refusal priced bands

    offers = offers.merge(
        bmu[
            [
                "nationalGridBmUnit",
                "settlementPeriod",
                "mel_min",
                "avg_mw",
                "fuelType",
            ]
        ],
        on=["nationalGridBmUnit", "settlementPeriod"],
        how="left",
    )

    # headroom above the declared position
    offers["room"] = (offers["mel_min"] - offers["avg_mw"]).clip(lower=0)
    # a unit can price volume it has no room to deliver so cap each band
    offers["useable"] = (
        (offers["room"] - (offers["levelFrom"] - offers["vols"]))
        .clip(lower=0)
        .fillna(0)
    )
    offers["vols"] = offers[["vols", "useable"]].min(axis=1)
    offers = offers[offers["vols"] > 0].copy()

    offers["fuelType"] = offers["fuelType"].fillna("UNKNOWN")
    if offers.empty:
        return offers

    offers = offers.sort_values("offer").reset_index(drop=True)
    offers["cum_mw"] = offers["vols"].cumsum()  # x position in the stack
    offers["left"] = offers["cum_mw"] - offers["vols"]
    offers["centre"] = offers["left"] + offers["vols"] / 2

    return offers


def merit_order_chart(settlement_date, settlement_period, bod, bmu, price):
    offers = merit_order(bod, settlement_period, bmu)

    fig = go.Figure()

    if offers.empty:
        fig.update_layout(
            title=dict(
                text=f"Period {settlement_period} Balancing Offer Stack: "
                f"{settlement_date}"
                + _sub("No bid-offer data published for this period yet.")
            ),
            height=600,
            margin=dict(t=110),
        )
        return _dark_layout(fig)

    for fuel in offers["fuelType"].unique():
        f = offers[offers["fuelType"] == fuel]
        fig.add_trace(
            go.Bar(
                x=f["centre"],
                y=f["offer"],
                width=f["vols"],
                name=fuel,
                marker=dict(
                    color=FUEL_COLOURS.get(fuel, FUEL_COLOURS["UNKNOWN"]),
                    line=dict(width=0),
                ),
                customdata=f[["nationalGridBmUnit", "vols"]],
                hovertemplate=(
                    "%{customdata[0]}<br>%{customdata[1]:,.0f} MW "
                    "at £%{y:,.2f}<extra></extra>"
                ),
            )
        )

    fig.add_hline(
        y=price,
        line_dash="dash",
        line_color="#E05B4A",
        annotation_text=f"Market Index £{price:,.0f}",
        annotation_position="top left",
        annotation_font=dict(size=15, color="#E05B4A"),
    )

    depth_at_price = offers.loc[offers["offer"] <= price, "vols"].sum()
    total_depth = offers["vols"].sum()

    subtitle = (
        f"{total_depth:,.0f} MW of turn-up priced below £2,000, "
        f"{depth_at_price:,.0f} MW of it at or below the market index. "
    )

    x_top = min(offers["cum_mw"].max() * 1.02, 5000)  # cut the flat expensive tail
    visible = offers[offers["left"] < x_top]

    y_top = max(visible["offer"].max() * 1.08, price * 1.5, 50)
    y_bottom = min(0, visible["offer"].min() * 1.05, price * 1.1)

    fig.update_layout(
        title=dict(
            text=f"Period {settlement_period} Balancing Offer Stack: {settlement_date}"
            + _sub(subtitle)
        ),
        height=600,
        bargap=0,
        barmode="overlay",
        showlegend=True,
        xaxis=dict(title="Cumulative Turn-Up Available (MW)", range=[0, x_top]),
        yaxis=dict(title="Offer Price (£/MWh)", range=[y_bottom, y_top]),
        legend=dict(orientation="h", yanchor="bottom", y=-0.22, x=0),
        margin=dict(t=110),
    )

    return _dark_layout(fig)


def price_at(offers, depth_mw):  # marginal offer price at a given depth
    hit = offers.loc[offers["cum_mw"] >= depth_mw, "offer"]
    return hit.iloc[0] if len(hit) else float("nan")


def all_merit_orders(bod, bmu):  # one stack per settlement period
    return {
        p: merit_order(bod, p, bmu) for p in sorted(bod["settlementPeriod"].unique())
    }


# turn up price at two fixed depths
def depth_series(bod, bmu, depth_shallow=500, depth_deep=1500, stacks=None):
    stacks = stacks if stacks is not None else all_merit_orders(bod, bmu)

    rows = []
    for p, o in stacks.items():
        if o.empty:
            continue
        rows.append(
            {
                "settlementPeriod": p,
                "shallow": price_at(o, depth_shallow),
                "deep": price_at(o, depth_deep),
            }
        )

    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(
            columns=["settlementPeriod", "shallow", "deep", "steepness"]
        )
    df["steepness"] = df["deep"] - df["shallow"]  # how fast turn up gets dearer
    return df


def depth_chart(
    depths, prices, date, shallow_mw, deep_mw, current_period=None, history=None
):

    d = depths.copy()
    d["time"] = _period_times(d, date)

    fig = go.Figure()

    if history is not None and not history.empty:
        h = history.copy()
        h["time"] = _period_times(h, date)
        fig.add_trace(
            go.Scatter(
                x=h["time"],
                y=h["deep_p75"],
                name="Upper quartile",
                line=dict(color=MUTED, width=0),
                showlegend=False,
                hoverinfo="skip",
            )
        )
        fig.add_trace(
            go.Scatter(
                x=h["time"],
                y=h["deep_p25"],
                name=f"{deep_mw:,} MW, Usual Range",
                line=dict(color=MUTED, width=0),
                fill="tonexty",
                fillcolor="rgba(140,140,140,0.20)",
                hoverinfo="skip",
            )
        )
        fig.add_trace(
            go.Scatter(
                x=h["time"],
                y=h["deep_median"],
                name=f"{deep_mw:,} MW, Usual Level",
                line=dict(color=MUTED, width=1.5, dash="dash"),
            )
        )

    fig.add_trace(
        go.Scatter(
            x=d["time"],
            y=d["shallow"],
            name=f"{shallow_mw:,} MW Turn-Up",
            line=dict(color=IMPORT_COLOUR, width=2),
        )
    )
    fig.add_trace(
        go.Scatter(
            x=d["time"],
            y=d["deep"],
            name=f"{deep_mw:,} MW Turn-Up",
            line=dict(color=EXPORT_COLOUR, width=2),
            fill="tonexty",
            fillcolor="rgba(224,91,74,0.25)",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=prices["time"],
            y=prices["price"],
            name="Market Index",
            line=dict(color=MUTED, width=1.5, dash="dot"),
        )
    )

    if current_period is not None:
        marker = pd.to_datetime(date) + pd.to_timedelta(
            (current_period - 1) * 30, unit="m"
        )

        fig.add_vline(x=marker, line_width=1, line_dash="dot", line_color=SUBTLE)

    median_steep = d["steepness"].median()
    subtitle = (
        f"Shaded red band is the price spread between {shallow_mw:,} and "
        f"{deep_mw:,} MW of turn-up. Median spread £{median_steep:,.0f}/MWh."
    )
    if history is not None and not history.empty:
        subtitle += (
            f"<br>Grey band is the interquartile range for the same period: "
            f"{_window_label(history)}."
        )
    n_missing = d["deep"].isna().sum()
    if n_missing:
        subtitle += f" {n_missing} periods had a stack thinner than {deep_mw:,} MW."
    fig.update_layout(
        title=dict(text=f"Marginal Cost of Balancing Turn-Up: {date}" + _sub(subtitle)),
        height=520,
        hovermode="x unified",
        yaxis=dict(title="£/MWh", rangemode="tozero"),
        xaxis=dict(title="", tickformat="%H:%M", dtick=7200000),
        legend=dict(orientation="h", yanchor="bottom", y=-0.26, x=0),
        margin=dict(t=140, l=70),
    )

    return _dark_layout(fig)


def offer_percentile(bod, bmu, unit_id, stacks=None):
    stacks = stacks if stacks is not None else all_merit_orders(bod, bmu)

    posted = set(  # periods where the unit submitted anything at all
        bod.loc[
            (bod["nationalGridBmUnit"] == unit_id) & (bod["pairId"] > 0),
            "settlementPeriod",
        ]
    )

    rows = []
    for p, o in stacks.items():
        if o.empty:
            continue
        bat = o[o["fuelType"] == "BATTERY"]
        if bat.empty:
            continue

        mine = bat.loc[bat["nationalGridBmUnit"] == unit_id, "offer"]
        cheapest = mine.min() if len(mine) else float("nan")

        # fleet spread that period
        q25, median, q75 = _weighted_quantiles(bat, [0.25, 0.5, 0.75])
        rows.append(
            {
                "settlementPeriod": p,
                "percentile": (
                    bat.loc[bat["offer"] < cheapest, "vols"].sum() / bat["vols"].sum()
                    if pd.notna(cheapest)
                    else float("nan")
                ),
                "my_offer": cheapest,
                "fleet_q25": q25,
                "fleet_median": median,
                "fleet_q75": q75,
                "n_units": bat["nationalGridBmUnit"].nunique(),
                # absent means it posted nothing or had no headroom
                "no_post": p not in posted,
            }
        )

    return pd.DataFrame(rows)


# weighted by band volume so a unit posting five bands is not counted five times
def _weighted_quantiles(bat, qs):

    d = bat.sort_values("offer")
    cum = d["vols"].cumsum() / d["vols"].sum()
    return [d.loc[cum >= q, "offer"].iloc[0] for q in qs]


def offer_percentile_chart(pct, unit_id, date):
    periods = range(1, int(pct["settlementPeriod"].max()) + 1)
    d = pd.DataFrame({"settlementPeriod": periods}).merge(
        pct, on="settlementPeriod", how="left"
    )
    d["time"] = _period_times(d, date)

    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        row_heights=[0.58, 0.42],
        vertical_spacing=0.07,
    )

    fig.add_trace(
        go.Scatter(
            x=d["time"],
            y=d["fleet_q75"],
            name="Fleet Upper Quartile",
            line=dict(color=MUTED, width=1, dash="dot"),
            connectgaps=False,
            showlegend=False,
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=d["time"],
            y=d["fleet_q25"],
            name="Fleet Interquartile Range",
            line=dict(color=MUTED, width=1, dash="dot"),
            connectgaps=False,
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=d["time"],
            y=d["fleet_median"],
            name="Fleet Median Offer",
            line=dict(color=MUTED, width=1.5),
            connectgaps=False,
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=d["time"],
            y=d["my_offer"],
            name=f"{unit_id} Cheapest Offer",
            line=dict(color=EXPORT_COLOUR, width=2.5),
            connectgaps=False,  # leave a break where the unit is absent
        ),
        row=1,
        col=1,
    )

    fig.add_trace(
        go.Scatter(
            x=d["time"],
            y=d["percentile"],
            name="Position in Stack",
            line=dict(color=IMPORT_COLOUR, width=2.5),
            connectgaps=False,
            hovertemplate="%{y:.0%} of fleet volume cheaper<extra></extra>",
        ),
        row=2,
        col=1,
    )
    fig.add_hline(y=0.5, line_width=1, line_dash="dot", line_color=SUBTLE, row=2, col=1)

    median_pct = d["percentile"].median()

    subtitle = (
        f"Median position: {median_pct:.0%} of battery offer volume priced below it. "
        f"Fleet of {int(d['n_units'].median(skipna=True))} units posting offers."
    )

    absent = d[d["my_offer"].isna()]
    no_post = int(absent["no_post"].fillna(True).sum())
    no_room = len(absent) - no_post

    bits = []
    if no_post:
        bits.append(f"{no_post} periods where it posted no offer")
    if no_room:
        bits.append(f"{no_room} periods where it had no headroom above its FPN")
    if bits:
        subtitle += "<br>Gaps are " + " and ".join(bits) + "."

    fig.update_layout(
        title=dict(
            text=f"{unit_id}: Offer Price Against the Battery Fleet {date}"
            + _sub(subtitle)
        ),
        height=680,
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=-0.16, x=0),
        margin=dict(t=140),
    )
    fig.update_yaxes(
        title="£/MWh",
        range=[
            d[["fleet_q25", "my_offer"]].min().min() * 0.95,
            d["fleet_q75"].max() * 1.25,
        ],
        row=1,
        col=1,
    )
    fig.update_yaxes(
        title="Share of Fleet Volume Cheaper",
        range=[0, 1],
        tickformat=".0%",
        row=2,
        col=1,
    )
    fig.update_xaxes(tickformat="%H:%M", dtick=7200000, row=2, col=1)

    return _dark_layout(fig)
