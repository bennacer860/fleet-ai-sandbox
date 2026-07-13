#!/usr/bin/env python3
"""Analyze Polymarket wallet trade data fetched by the collect-trades skill.

Produces:
  1. Crypto profitability ranking (from positions realizedPnl)
  2. Market-type-per-crypto profitability
  3. BTC deep dive: per-market-type dataset stats
  4. Strategy-inference metrics (entry timing, laddering, hedging, sizing)
  5. matplotlib charts + a markdown report

Usage:
    python analyze_trades.py \
        --trades data/<label>_<start>_<end>.csv \
        --positions data/<label>_<start>_<end>_positions.csv \
        --outdir analysis/<label>

Only --trades and --positions are required. --closed is optional (extra P&L
cross-check). Output goes to --outdir (default: analysis/<label>).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# matplotlib cache must be writable
os.environ.setdefault("MPLCONFIGDIR", "/tmp/mplconfig")
Path(os.environ["MPLCONFIGDIR"]).mkdir(parents=True, exist_ok=True)

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

CRYPTO_TOKENS = [
    ("bitcoin", "BTC"), ("btc", "BTC"),
    ("ethereum", "ETH"), ("eth", "ETH"),
    ("solana", "SOL"), ("sol", "SOL"),
    ("ripple", "XRP"), ("xrp", "XRP"),
]
DURATION_SECONDS = {"5min": 300, "15min": 900, "1hour": 3600, "hourly": 3600}


def parse_slug(slug: str) -> tuple[str, str]:
    """Return (crypto, market_type) parsed from an event slug."""
    s = (slug or "").lower()
    crypto = "OTHER"
    word_crypto = False
    for token, name in CRYPTO_TOKENS:
        if s.startswith(token + "-") or s.startswith(token + "_"):
            crypto = name
            word_crypto = token in ("bitcoin", "ethereum", "solana", "ripple")
            break

    # Check longer tokens first: "15min" contains "5min" as a substring.
    if "15min" in s or "-15m-" in s or "updown-15m" in s:
        dur = "15min"
    elif "30min" in s or "-30m-" in s or "updown-30m" in s:
        dur = "30min"
    elif "5min" in s or "-5m-" in s or "updown-5m" in s:
        dur = "5min"
    elif "1hour" in s or "-1h-" in s or "1-hour" in s:
        dur = "1hour"
    elif word_crypto and "-up-or-down-" in s:
        dur = "hourly"
    else:
        dur = "other"
    return crypto, dur


def _fmt_money(x: float) -> str:
    return f"${x:,.2f}"


def load_positions(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["pnl"] = pd.to_numeric(df["pnl"], errors="coerce")
    for col in ("buy_cost", "buy_shares", "sell_shares", "net_shares"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    parsed = df["event_slug"].apply(parse_slug)
    df["crypto"] = parsed.apply(lambda t: t[0])
    df["market_type"] = parsed.apply(lambda t: t[1])
    df["is_win"] = df["winner"].astype(str).str.lower() == "true"
    df["is_loss"] = df["winner"].astype(str).str.lower() == "false"
    return df


def load_trades(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    for col in ("price", "size", "usdc_value", "timestamp", "expiry_ts"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    parsed = df["event_slug"].apply(parse_slug)
    df["crypto"] = parsed.apply(lambda t: t[0])
    df["market_type"] = parsed.apply(lambda t: t[1])
    df["dur_s"] = df["market_type"].map(DURATION_SECONDS)
    # entry offset = seconds after market open (market_start = expiry - duration)
    df["market_start"] = df["expiry_ts"] - df["dur_s"]
    df["entry_offset_s"] = df["timestamp"] - df["market_start"]
    df["ts"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    return df


def crypto_profitability(pos: pd.DataFrame) -> pd.DataFrame:
    g = pos.groupby("crypto").agg(
        positions=("pnl", "size"),
        resolved=("pnl", "count"),
        pnl=("pnl", "sum"),
        buy_cost=("buy_cost", "sum"),
        wins=("is_win", "sum"),
        losses=("is_loss", "sum"),
    )
    g["roi_pct"] = 100 * g["pnl"] / g["buy_cost"].replace(0, pd.NA)
    g["win_rate_pct"] = 100 * g["wins"] / (g["wins"] + g["losses"]).replace(0, pd.NA)
    return g.sort_values("pnl", ascending=False)


def market_type_profitability(pos: pd.DataFrame) -> pd.DataFrame:
    g = pos.groupby(["crypto", "market_type"]).agg(
        positions=("pnl", "size"),
        pnl=("pnl", "sum"),
        buy_cost=("buy_cost", "sum"),
        wins=("is_win", "sum"),
        losses=("is_loss", "sum"),
    )
    g["roi_pct"] = 100 * g["pnl"] / g["buy_cost"].replace(0, pd.NA)
    g["win_rate_pct"] = 100 * g["wins"] / (g["wins"] + g["losses"]).replace(0, pd.NA)
    return g.sort_values("pnl", ascending=False)


def profit_decomposition(pos: pd.DataFrame, crypto: str | None = None) -> dict:
    """Explain the profit mechanism via exact per-share economics.

    Every winning share redeems at $1. So, exactly:
        redeemed_$ = total_buy_cost + total_pnl
        share_win_rate = redeemed_$ / total_shares_bought
        avg_price = total_buy_cost / total_shares_bought
        edge_per_share = share_win_rate - avg_price  (= total_pnl / total_shares)

    A hedger who buys both sides pays ~`2 * avg_price` per Up+Down pair; below
    $1.00 means the paired book is bought under fair value (edge source). We also
    report how balanced the book is (hedged_share_pct) and whether the net-long
    side tends to win (directional tilt).
    """
    df = pos if crypto is None else pos[pos["crypto"] == crypto]
    if df.empty:
        return {}

    total_shares = float(df["buy_shares"].sum())
    total_cost = float(df["buy_cost"].sum())
    total_pnl = float(df["pnl"].sum())
    if total_shares <= 0:
        return {}

    avg_price = total_cost / total_shares
    redeemed = total_cost + total_pnl
    share_win_rate = redeemed / total_shares
    edge_per_share = total_pnl / total_shares
    pair_cost = 2.0 * avg_price  # share-weighted Up+Down cost

    # Balance of the book (how hedged) + directional tilt
    grp = df.groupby(["condition_id", "outcome"])["buy_shares"].sum()
    shares = grp.unstack(fill_value=0.0)
    if shares.shape[1] > 2:
        top2 = shares.sum().sort_values(ascending=False).head(2).index
        shares = shares[top2]
    paired_shares = 2.0 * shares.min(axis=1).sum()
    hedged_share_pct = 100.0 * paired_shares / total_shares
    two_sided = int((shares.gt(0).sum(axis=1) >= 2).sum())

    heavier_side = shares.idxmax(axis=1)
    winning = df[df["is_win"]].groupby("condition_id")["outcome"].first()
    common = heavier_side.index.intersection(winning.index)
    net_long_winner_pct = (
        100.0 * (heavier_side.loc[common] == winning.loc[common]).mean()
        if len(common) else float("nan")
    )

    return {
        "markets": int(shares.shape[0]),
        "markets_two_sided": two_sided,
        "total_shares": total_shares,
        "total_buy_cost": total_cost,
        "avg_price_per_share": avg_price,
        "pair_cost_up_plus_down": pair_cost,
        "share_win_rate_pct": 100.0 * share_win_rate,
        "edge_per_share": edge_per_share,
        "total_pnl": total_pnl,
        "roi_pct": 100.0 * total_pnl / total_cost if total_cost else float("nan"),
        "hedged_share_pct": hedged_share_pct,
        "net_long_winner_pct": net_long_winner_pct,
    }


def btc_market_type_stats(trades: pd.DataFrame) -> pd.DataFrame:
    btc = trades[trades["crypto"] == "BTC"]
    rows = []
    for mt, grp in btc.groupby("market_type"):
        n_markets = grp["condition_id"].nunique()
        n_trades = len(grp)
        rows.append({
            "market_type": mt,
            "markets": n_markets,
            "trades": n_trades,
            "avg_trades_per_market": n_trades / n_markets if n_markets else 0,
            "avg_size": grp["size"].mean(),
            "median_size": grp["size"].median(),
            "min_size": grp["size"].min(),
            "max_size": grp["size"].max(),
            "avg_price": grp["price"].mean(),
            "min_price": grp["price"].min(),
            "max_price": grp["price"].max(),
            "total_volume_usdc": grp["usdc_value"].sum(),
        })
    return pd.DataFrame(rows).sort_values("trades", ascending=False)


def btc_strategy_metrics(trades: pd.DataFrame, pos: pd.DataFrame) -> dict:
    btc_t = trades[trades["crypto"] == "BTC"]
    btc_p = pos[pos["crypto"] == "BTC"]
    m: dict = {}

    sides = btc_t["side"].value_counts().to_dict()
    m["side_counts"] = sides
    m["buy_pct"] = 100 * sides.get("BUY", 0) / max(len(btc_t), 1)

    # Both-outcome hedging: markets with both Up and Down bought
    outcomes_per_market = btc_t.groupby("condition_id")["outcome"].nunique()
    m["markets_total"] = int(outcomes_per_market.size)
    m["markets_both_sides"] = int((outcomes_per_market >= 2).sum())
    m["both_sides_pct"] = 100 * m["markets_both_sides"] / max(m["markets_total"], 1)

    # Entry timing (seconds after market open)
    off = btc_t["entry_offset_s"].dropna()
    off = off[(off > -60) & (off < 7200)]
    if len(off):
        m["entry_offset_median_s"] = float(off.median())
        m["entry_offset_p25_s"] = float(off.quantile(0.25))
        m["entry_offset_p75_s"] = float(off.quantile(0.75))

    # Price laddering: distinct prices + price span per (market, outcome)
    grp = btc_t.groupby(["condition_id", "outcome"])
    m["avg_fills_per_leg"] = float(grp.size().mean())
    m["avg_distinct_prices_per_leg"] = float(grp["price"].nunique().mean())
    m["avg_price_span_per_leg"] = float((grp["price"].max() - grp["price"].min()).mean())

    # Sizing per leg (shares)
    m["avg_shares_per_leg"] = float(btc_p["buy_shares"].mean())
    m["median_shares_per_leg"] = float(btc_p["buy_shares"].median())

    # Hold to expiry vs sell
    total_buy = btc_p["buy_shares"].sum()
    total_sell = btc_p["sell_shares"].sum()
    m["sell_to_buy_ratio_pct"] = 100 * total_sell / max(total_buy, 1)

    # Win rate + pnl
    wins = int(btc_p["is_win"].sum())
    losses = int(btc_p["is_loss"].sum())
    m["win_rate_pct"] = 100 * wins / max(wins + losses, 1)
    m["total_pnl"] = float(btc_p["pnl"].sum())
    m["total_buy_cost"] = float(btc_p["buy_cost"].sum())
    m["roi_pct"] = 100 * m["total_pnl"] / max(m["total_buy_cost"], 1)

    # Avg entry price for winning vs losing legs
    winners = btc_p[btc_p["is_win"]]
    losers = btc_p[btc_p["is_loss"]]
    m["avg_buy_price_winners"] = float(
        (winners["buy_cost"].sum() / winners["buy_shares"].sum())
    ) if winners["buy_shares"].sum() else None
    m["avg_buy_price_losers"] = float(
        (losers["buy_cost"].sum() / losers["buy_shares"].sum())
    ) if losers["buy_shares"].sum() else None

    # Per-market win/loss rate (does the whole Up+Down book profit?)
    mkt_pnl = btc_p.groupby("condition_id")["pnl"].sum()
    n_mkt = len(mkt_pnl)
    if n_mkt:
        m["market_profit_pct"] = 100 * float((mkt_pnl > 0).mean())
        m["market_loss_pct"] = 100 * float((mkt_pnl < 0).mean())
        m["avg_market_win"] = float(mkt_pnl[mkt_pnl > 0].mean())
        m["avg_market_loss"] = float(mkt_pnl[mkt_pnl < 0].mean())
    return m


def _winning_outcome(pos: pd.DataFrame) -> pd.Series:
    """Winning outcome per condition_id (from the winner flag)."""
    w = pos[pos["is_win"]]
    return w.groupby("condition_id")["outcome"].first()


def accumulation_metrics(
    trades: pd.DataFrame, pos: pd.DataFrame
) -> tuple[dict, pd.Series]:
    """Does the algo lean toward the winner, and does the lean develop over time?

    Tests the gabagool22 signatures: 'first trade on loser' and a lean that
    grows across the window. A pure symmetric ladder shows ~50% throughout and
    ends balanced.
    """
    btc_t = trades[trades["crypto"] == "BTC"].copy()
    btc_p = pos[pos["crypto"] == "BTC"]
    win = _winning_outcome(btc_p)
    if win.empty:
        return {}, pd.Series(dtype=float)

    btc_t = btc_t[btc_t["condition_id"].isin(win.index)]
    btc_t = btc_t.sort_values(["condition_id", "timestamp"])
    btc_t["win_out"] = btc_t["condition_id"].map(win)
    btc_t["on_winner"] = btc_t["outcome"] == btc_t["win_out"]
    btc_t["k"] = btc_t.groupby("condition_id").cumcount()

    lean_by_trade = (
        btc_t.groupby("k")["on_winner"].mean().mul(100).head(25)
    )

    swin = btc_t.assign(s=btc_t["size"].where(btc_t["on_winner"], 0.0)) \
        .groupby("condition_id")["s"].sum()
    slos = btc_t.assign(s=btc_t["size"].where(~btc_t["on_winner"], 0.0)) \
        .groupby("condition_id")["s"].sum()
    net = swin - slos
    n = len(net)
    m = {
        "first_fill_on_winner_pct": float(lean_by_trade.iloc[0]) if len(lean_by_trade) else float("nan"),
        "markets_balanced_pct": 100 * float((net == 0).mean()),
        "markets_winner_heavy_pct": 100 * float((net > 0).mean()),
        "markets_loser_heavy_pct": 100 * float((net < 0).mean()),
        "avg_net_winner_shares": float(net.mean()),
        "avg_total_shares_per_market": float((swin + slos).mean()),
    }
    return m, lean_by_trade


def direction_and_calibration(
    trades: pd.DataFrame, pos: pd.DataFrame
) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    """One-sidedness, direction accuracy, and entry-price calibration (BTC).

    Answers: does it pick one direction? how often is the direction right? and is
    the entry price just the market's implied probability (calibrated)?
    """
    btc_t = trades[trades["crypto"] == "BTC"]
    btc_p = pos[pos["crypto"] == "BTC"]
    buy = btc_t[btc_t["side"] == "BUY"]
    mpnl = btc_p.groupby("condition_id")["pnl"].sum()
    m: dict = {}
    if buy.empty or mpnl.empty:
        return m, pd.DataFrame(), pd.DataFrame()

    sides = buy.groupby("condition_id")["outcome"].nunique()
    one_sided = set(sides[sides == 1].index)
    m["one_sided_pct"] = 100 * float((sides == 1).mean())
    m["both_sided_pct"] = 100 * float((sides >= 2).mean())

    # Direction accuracy on one-sided markets (unbiased: use realized pnl sign)
    os_pnl = mpnl[mpnl.index.isin(one_sided)]
    if len(os_pnl):
        m["direction_correct_pct"] = 100 * float((os_pnl > 0).mean())
        m["direction_wrong_pct"] = 100 * float((os_pnl < 0).mean())

    # First-entry per market
    first = buy.sort_values("timestamp").groupby("condition_id").head(1).set_index("condition_id")
    first = first.join(mpnl.rename("mpnl")).dropna(subset=["mpnl"])
    first["won"] = first["mpnl"] > 0
    corr, wrong = first[first["won"]], first[~first["won"]]
    if len(corr):
        m["entry_price_when_correct"] = float(corr["price"].mean())
        m["entry_time_when_correct_s"] = float(corr["entry_offset_s"].median())
    if len(wrong):
        m["entry_price_when_wrong"] = float(wrong["price"].mean())
        m["entry_time_when_wrong_s"] = float(wrong["entry_offset_s"].median())
    m["first_entry_on_favorite_pct"] = 100 * float((first["price"] > 0.5).mean())

    # Calibration: first-entry price bucket -> actual win rate
    first["pb"] = pd.cut(first["price"], np.arange(0, 1.01, 0.1))
    cal = first.groupby("pb", observed=True).agg(
        markets=("won", "size"),
        entry_price=("price", "mean"),
        actual_winrate_pct=("won", lambda s: 100 * s.mean()),
    ).round(3)
    return m, cal, first.reset_index()


def coverage_and_timing(trades: pd.DataFrame) -> dict:
    """Does it enter every BTC 5-min market, and when? (selectivity + timing)."""
    t5 = trades[(trades["crypto"] == "BTC") & (trades["market_type"] == "5min")]
    buy = t5[t5["side"] == "BUY"]
    m: dict = {}
    if t5.empty:
        return m
    traded = t5["condition_id"].nunique()
    starts = t5.groupby("condition_id")["expiry_ts"].first() - 300
    total = int((starts.max() - starts.min()) / 300) + 1 if len(starts) else 0
    m["btc5_markets_traded"] = int(traded)
    m["btc5_windows_available"] = int(total)
    if total:
        m["btc5_coverage_pct"] = 100 * traded / total
    first = buy.sort_values("timestamp").groupby("condition_id").head(1)
    off = first["entry_offset_s"].dropna()
    off = off[(off >= 0) & (off < 400)]
    if len(off):
        m["first_entry_median_s"] = float(off.median())
        m["first_entry_p10_s"] = float(off.quantile(0.10))
        m["first_entry_p90_s"] = float(off.quantile(0.90))
        m["entries_first_30s_pct"] = 100 * float((off < 30).mean())
        m["entries_after_150s_pct"] = 100 * float((off > 150).mean())
    return m


def sell_and_three_leg(
    trades: pd.DataFrame, pos: pd.DataFrame
) -> tuple[dict, pd.DataFrame]:
    """Sell behavior + 3-leg play (favorite round-trip -> lottery) prevalence,
    P&L by strategy class, and whether the lottery legs are net +EV.

    Runs on ALL cryptos (the round-trip/lottery pattern spans durations)."""
    m: dict = {}
    vol = trades.groupby("side")["size"].sum()
    m["sell_pct_of_bought"] = 100 * float(vol.get("SELL", 0) / max(vol.get("BUY", 0), 1))
    sells = trades[trades["side"] == "SELL"]
    if len(sells):
        m["sell_price_median"] = float(sells["price"].median())
        m["markets_with_sells_pct"] = 100 * float(
            trades[trades["side"] == "SELL"]["condition_id"].nunique()
            / max(trades["condition_id"].nunique(), 1)
        )

    # Classify each market: directional / roundtrip_only / 3-leg
    leg = trades.groupby(["condition_id", "outcome", "side"]).agg(
        sh=("size", "sum"), usd=("usdc_value", "sum")
    ).reset_index()
    mpnl = pos.groupby("condition_id")["pnl"].sum()
    cls: dict = {}
    for cid, d in leg.groupby("condition_id"):
        outs = d["outcome"].unique()
        rt_fav = None
        for oc in outs:
            b = d[(d["outcome"] == oc) & (d["side"] == "BUY")]
            s = d[(d["outcome"] == oc) & (d["side"] == "SELL")]
            if len(b) and len(s) and b["sh"].sum() and b["usd"].sum() / b["sh"].sum() > 0.5:
                rt_fav = oc
        if rt_fav is None:
            cls[cid] = "directional"
            continue
        lottery = False
        for oc in outs:
            if oc == rt_fav:
                continue
            b = d[(d["outcome"] == oc) & (d["side"] == "BUY")]
            if len(b) and b["sh"].sum() and b["usd"].sum() / b["sh"].sum() < 0.15:
                lottery = True
        cls[cid] = "3-leg" if lottery else "roundtrip_only"

    cdf = pd.DataFrame({"pnl": mpnl})
    cdf["cls"] = cdf.index.map(cls).fillna("directional")
    by_class = cdf.groupby("cls").agg(
        markets=("pnl", "size"),
        total_pnl=("pnl", "sum"),
        avg_pnl=("pnl", "mean"),
        win_rate_pct=("pnl", lambda s: 100 * (s > 0).mean()),
    ).round(2)

    # Lottery-leg EV
    pp = pos.copy()
    pp["avg_buy"] = pp["buy_cost"] / pp["buy_shares"].replace(0, np.nan)
    lot = pp[(pp["buy_shares"] > 0) & (pp["avg_buy"] < 0.15)]
    if len(lot):
        m["lottery_legs"] = int(len(lot))
        m["lottery_cost"] = float(lot["buy_cost"].sum())
        m["lottery_pnl"] = float(lot["pnl"].sum())
        m["lottery_avg_price"] = float(lot["buy_cost"].sum() / lot["buy_shares"].sum())
        if lot["buy_cost"].sum():
            m["lottery_roi_pct"] = 100 * float(lot["pnl"].sum() / lot["buy_cost"].sum())
    return m, by_class


def choppiness_table(trades: pd.DataFrame, pos: pd.DataFrame) -> pd.DataFrame:
    """P&L and per-share edge by within-market price choppiness quartile."""
    btc_t = trades[trades["crypto"] == "BTC"]
    btc_p = pos[pos["crypto"] == "BTC"]

    def flips(sub):
        pr = sub.sort_values("timestamp")["price"].values
        if len(pr) < 3:
            return np.nan
        d = np.diff(pr)
        return int((np.sign(d[:-1]) != np.sign(d[1:])).sum())

    fl = btc_t.groupby("condition_id").apply(flips, include_groups=False)
    mpnl = btc_p.groupby("condition_id")["pnl"].sum()
    msh = btc_p.groupby("condition_id")["buy_shares"].sum()
    d = pd.DataFrame({"flips": fl, "pnl": mpnl, "shares": msh}).dropna()
    d = d[d["shares"] > 0]
    if len(d) < 8:
        return pd.DataFrame()
    d["edge_per_share"] = d["pnl"] / d["shares"]
    q = pd.qcut(d["flips"].rank(method="first"), 4,
                labels=["calm", "q2", "q3", "choppy"])
    return d.groupby(q, observed=True).agg(
        markets=("pnl", "size"),
        avg_flips=("flips", "mean"),
        avg_pnl=("pnl", "mean"),
        edge_per_share=("edge_per_share", "mean"),
        total_pnl=("pnl", "sum"),
    ).round(4)


def make_charts(
    pos: pd.DataFrame,
    trades: pd.DataFrame,
    outdir: Path,
    lean_by_trade: pd.Series | None = None,
    chop: pd.DataFrame | None = None,
) -> list[tuple[str, str]]:
    """Generate all charts. Returns list of (filename, caption)."""
    charts: list[tuple[str, str]] = []
    plt.rcParams["figure.autolayout"] = True

    def save(fig, name: str, caption: str) -> None:
        p = outdir / name
        fig.savefig(p, dpi=110)
        plt.close(fig)
        charts.append((p.name, caption))

    # 1. PnL by crypto
    cp = pos.groupby("crypto")["pnl"].sum().sort_values()
    if len(cp):
        fig, ax = plt.subplots(figsize=(7, 4))
        cp.plot.barh(ax=ax, color=["#c0392b" if v < 0 else "#27ae60" for v in cp])
        ax.set_title("P&L by crypto")
        ax.set_xlabel("realized P&L ($)")
        save(fig, "pnl_by_crypto.png", "Realized P&L by crypto.")

    # 2. PnL by crypto x market type
    mt = pos.groupby(["crypto", "market_type"])["pnl"].sum().sort_values()
    if len(mt):
        labels = [f"{c}-{m}" for c, m in mt.index]
        fig, ax = plt.subplots(figsize=(8, max(4, 0.4 * len(mt))))
        ax.barh(labels, mt.values,
                color=["#c0392b" if v < 0 else "#27ae60" for v in mt.values])
        ax.set_title("P&L by crypto x market type")
        ax.set_xlabel("realized P&L ($)")
        save(fig, "pnl_by_crypto_market_type.png",
             "Realized P&L for every crypto x market-type segment.")

    # 3. BTC PnL by market type
    btc = pos[pos["crypto"] == "BTC"]
    mp = btc.groupby("market_type")["pnl"].sum().sort_values()
    if len(mp):
        fig, ax = plt.subplots(figsize=(7, 4))
        mp.plot.barh(ax=ax, color=["#c0392b" if v < 0 else "#27ae60" for v in mp])
        ax.set_title("BTC P&L by market type")
        ax.set_xlabel("realized P&L ($)")
        save(fig, "btc_pnl_by_market_type.png", "BTC realized P&L by market duration.")

    btc_t = trades[trades["crypto"] == "BTC"]

    # 4. Entry price distribution
    if len(btc_t):
        fig, ax = plt.subplots(figsize=(7, 4))
        btc_t["price"].plot.hist(bins=50, ax=ax, color="#2980b9")
        ax.set_title("BTC entry price distribution")
        ax.set_xlabel("price")
        save(fig, "btc_price_hist.png",
             "Distribution of prices paid per fill (laddering signature).")

    # 5. Trade size distribution (clipped at p99)
    if len(btc_t):
        clip = btc_t["size"].quantile(0.99)
        fig, ax = plt.subplots(figsize=(7, 4))
        btc_t["size"].clip(upper=clip).plot.hist(bins=50, ax=ax, color="#8e44ad")
        ax.set_title("BTC trade size distribution (clipped p99)")
        ax.set_xlabel("shares")
        save(fig, "btc_size_hist.png", "Per-fill size distribution (sizing pattern).")

    # 6. Entry offset (seconds after market open)
    off = btc_t["entry_offset_s"].dropna()
    off = off[(off >= 0) & (off < 1200)]
    if len(off):
        fig, ax = plt.subplots(figsize=(7, 4))
        off.plot.hist(bins=60, ax=ax, color="#e67e22")
        ax.set_title("BTC entry timing (seconds after market open)")
        ax.set_xlabel("seconds after open")
        save(fig, "btc_entry_timing.png",
             "When in the window entries occur (0 = market open).")

    # 7. Trades by hour of day (UTC)
    if len(btc_t):
        by_hour = btc_t["ts"].dt.hour.value_counts().sort_index()
        fig, ax = plt.subplots(figsize=(7, 4))
        by_hour.plot.bar(ax=ax, color="#16a085")
        ax.set_title("BTC trades by hour (UTC)")
        ax.set_xlabel("hour")
        save(fig, "btc_trades_by_hour.png", "Activity by hour of day (UTC).")

    # 8. Cumulative P&L
    if len(btc) and "event_slug" in btc.columns:
        tmp = btc.sort_values("event_slug").copy()
        tmp["cum_pnl"] = tmp["pnl"].cumsum()
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.plot(range(len(tmp)), tmp["cum_pnl"], color="#2c3e50")
        ax.set_title("BTC cumulative P&L (ordered by market)")
        ax.set_xlabel("market index")
        ax.set_ylabel("cumulative P&L ($)")
        save(fig, "btc_cumulative_pnl.png", "Cumulative P&L across markets (consistency).")

    # 9. Profit theory: per-market Up+Down pair cost vs $1 fair value (BTC)
    if len(btc):
        grp = btc.groupby(["condition_id", "outcome"]).agg(
            s=("buy_shares", "sum"), c=("buy_cost", "sum")
        ).reset_index()
        shares = grp.pivot(index="condition_id", columns="outcome", values="s")
        cost = grp.pivot(index="condition_id", columns="outcome", values="c")
        if shares.shape[1] >= 2:
            top2 = shares.sum().sort_values(ascending=False).head(2).index
            price = cost[top2].divide(shares[top2])
            pair_cost = price.sum(axis=1, skipna=False).dropna()
            pair_cost = pair_cost[(pair_cost > 0.5) & (pair_cost < 1.5)]
            if len(pair_cost):
                fig, ax = plt.subplots(figsize=(7, 4))
                pair_cost.plot.hist(bins=60, ax=ax, color="#2980b9")
                ax.axvline(1.0, color="#c0392b", linestyle="--", label="$1.00 fair value")
                ax.set_title("BTC Up+Down pair cost per market")
                ax.set_xlabel("combined entry price ($)")
                ax.legend()
                save(fig, "btc_pair_cost_hist.png",
                     "Combined Up+Down entry cost per market; left of the red line "
                     "= hedge bought under fair value.")

    # 10. Profit theory: share win rate vs avg price paid (edge, BTC)
    if len(btc):
        total_shares = btc["buy_shares"].sum()
        total_cost = btc["buy_cost"].sum()
        total_pnl = btc["pnl"].sum()
        if total_shares > 0:
            avg_price = total_cost / total_shares
            win_rate = (total_cost + total_pnl) / total_shares
            fig, ax = plt.subplots(figsize=(6, 4))
            bars = ax.bar(
                ["avg price/share", "share win rate"],
                [avg_price, win_rate],
                color=["#8e44ad", "#27ae60"],
            )
            ax.set_ylim(0, max(avg_price, win_rate) * 1.15)
            ax.set_title("BTC edge: share win rate vs avg price")
            ax.set_ylabel("$ per share")
            for b, v in zip(bars, [avg_price, win_rate]):
                ax.text(b.get_x() + b.get_width() / 2, v, f"${v:.4f}",
                        ha="center", va="bottom")
            save(fig, "btc_edge_bar.png",
                 "Edge source: winning-share rate above avg price paid = profit/share.")

    # 11. Winner-side lean by trade index (does it develop a direction?)
    if lean_by_trade is not None and len(lean_by_trade):
        fig, ax = plt.subplots(figsize=(7, 4))
        ax.plot(lean_by_trade.index + 1, lean_by_trade.values, "o-", color="#2980b9")
        ax.axhline(50, color="gray", ls="--", label="50% (no lean)")
        ax.set_ylim(30, 85)
        ax.set_xlabel("fill number within market")
        ax.set_ylabel("% of fills on eventual winner")
        ax.set_title("Winner-side lean by trade index\n(flat ~50% = symmetric ladder; rising = directional)")
        ax.legend()
        save(fig, "btc_winner_lean_by_trade.png",
             "Whether the book tilts toward the winner as the window progresses.")

    # 12. Per-share edge by choppiness quartile
    if chop is not None and len(chop):
        fig, ax = plt.subplots(figsize=(7, 4))
        ax.bar(chop.index.astype(str), chop["edge_per_share"], color="#8e44ad")
        ax.set_ylabel("edge per share ($)")
        ax.set_xlabel("within-market choppiness (price direction flips)")
        ax.set_title("Per-share edge by choppiness\n(controls for position size)")
        save(fig, "btc_choppiness_edge.png",
             "Per-share edge vs market choppiness; calmer markets = better fills.")

    # ---- BTC behavior charts (entry, calibration, price paths) ----
    btc = pos[pos["crypto"] == "BTC"]
    btc_t2 = trades[trades["crypto"] == "BTC"]
    if len(btc) and len(btc_t2):
        mpnl = btc.groupby("condition_id")["pnl"].sum()
        buy = btc_t2[btc_t2["side"] == "BUY"].copy()
        buy["mpnl"] = buy["condition_id"].map(mpnl)
        first = buy.sort_values("timestamp").groupby("condition_id").head(1).copy()
        first["mpnl"] = first["condition_id"].map(mpnl)
        first = first.dropna(subset=["mpnl"])
        first["won"] = first["mpnl"] > 0

        # 13. Entry-price calibration
        if len(first):
            first["pb"] = pd.cut(first["price"], np.arange(0, 1.01, 0.1))
            cal = first.groupby("pb", observed=True).agg(
                ep=("price", "mean"), wr=("won", "mean")).dropna()
            if len(cal):
                fig, ax = plt.subplots(figsize=(6.5, 6))
                ax.plot([0, 1], [0, 1], "--", color="gray",
                        label="entry price = win probability (fair)")
                ax.plot(cal["ep"], cal["wr"], "o-", color="#8e44ad",
                        label="actual win rate")
                ax.set_xlabel("first-entry price (market implied probability)")
                ax.set_ylabel("actual win rate")
                ax.set_xlim(0, 1); ax.set_ylim(0, 1)
                ax.set_title("Entry price IS the market's probability (calibration)\n"
                             "above the line = edge")
                ax.legend(loc="upper left", fontsize=8)
                save(fig, "btc_entry_calibration.png",
                     "First-entry price vs actual win rate; on the diagonal = "
                     "entering at fair market probability.")

            # 14. Entry-price distribution (profit vs loss)
            fig, ax = plt.subplots(figsize=(8, 4.5))
            ax.hist(first[first["won"]]["price"], bins=25, alpha=0.6,
                    color="#27ae60", label="profitable")
            ax.hist(first[~first["won"]]["price"], bins=25, alpha=0.6,
                    color="#c0392b", label="losing")
            ax.set_xlabel("first-entry price"); ax.set_ylabel("markets")
            ax.set_title("First-entry price distribution (profitable vs losing)")
            ax.legend()
            save(fig, "btc_entry_price_hist.png",
                 "Where entries land: favorites (high) vs longshots (low).")

            # 15. Entry price vs time (profit vs loss)
            fig, ax = plt.subplots(figsize=(8, 4.5))
            for won, color, lab in [(True, "#27ae60", "profitable"),
                                    (False, "#c0392b", "losing")]:
                d = first[first["won"] == won]
                ax.scatter(d["entry_offset_s"], d["price"], s=7, alpha=0.25,
                           color=color, label=lab)
            ax.axhline(0.5, color="gray", ls=":", lw=0.8)
            ax.set_xlim(0, 300); ax.set_ylim(0, 1)
            ax.set_xlabel("first-entry time (s after open)")
            ax.set_ylabel("first-entry price")
            ax.set_title("Entry price & timing: profitable vs losing markets")
            ax.legend(loc="center right", fontsize=8)
            save(fig, "btc_entry_price_vs_time.png",
                 "Profitable = early favorites; losing = late longshots.")

        # 16 & 17. Fill-price density over the window (5min), profit vs loss
        b5 = btc_t2[(btc_t2["market_type"] == "5min") & (btc_t2["side"] == "BUY")].copy()
        b5["mpnl"] = b5["condition_id"].map(mpnl)
        b5 = b5[(b5["entry_offset_s"] >= 0) & (b5["entry_offset_s"] <= 300)].dropna(subset=["mpnl"])
        for won, fname, cmap, lab in [
            (True, "btc5_price_path_profit.png", "Greens", "PROFITABLE"),
            (False, "btc5_price_path_loss.png", "Reds", "LOSING"),
        ]:
            d = b5[b5["mpnl"] > 0] if won else b5[b5["mpnl"] < 0]
            if len(d) < 50:
                continue
            fig, ax = plt.subplots(figsize=(8, 4.5))
            hb = ax.hexbin(d["entry_offset_s"], d["price"], C=d["size"],
                           reduce_C_function=np.sum, gridsize=40, cmap=cmap,
                           mincnt=1, extent=[0, 300, 0, 1])
            bb = pd.cut(d["entry_offset_s"], np.arange(0, 301, 15))
            med = d.groupby(bb, observed=True).apply(
                lambda x: np.average(x["price"], weights=x["size"]), include_groups=False)
            ax.plot([iv.mid for iv in med.index], med.values, color="black", lw=2,
                    label="size-weighted avg price")
            ax.axhline(0.5, color="white", ls=":", lw=1)
            ax.set_xlim(0, 300); ax.set_ylim(0, 1)
            ax.set_xlabel("seconds after market open"); ax.set_ylabel("fill price")
            ax.set_title(f"Fill price over window — {lab} BTC 5-min markets "
                         f"(n={d['condition_id'].nunique()})")
            ax.legend(loc="upper left", fontsize=8)
            fig.colorbar(hb, label="shares")
            save(fig, fname,
                 f"{lab.title()} markets: fill price path through the 5-min window.")

    return charts


def _decomp_lines(title: str, d: dict) -> list[str]:
    if not d:
        return [f"### {title}\n", "_No data._\n"]
    out = [f"### {title}\n"]
    out.append(f"- markets: {d['markets']:,} ({d['markets_two_sided']:,} two-sided)")
    out.append(f"- shares bought: {d['total_shares']:,.0f} for {_fmt_money(d['total_buy_cost'])}")
    out.append(f"- **avg price/share**: ${d['avg_price_per_share']:.4f}")
    out.append(
        f"- **Up+Down pair cost (share-weighted)**: ${d['pair_cost_up_plus_down']:.4f} "
        f"({'below' if d['pair_cost_up_plus_down'] < 1 else 'above'} $1.00 fair value)"
    )
    out.append(f"- **share win rate**: {d['share_win_rate_pct']:.2f}%")
    out.append(
        f"- **edge/share**: ${d['edge_per_share']:.4f} "
        f"(= win rate − avg price)"
    )
    out.append(f"- total P&L: {_fmt_money(d['total_pnl'])}  (ROI {d['roi_pct']:.2f}%)")
    out.append(f"- hedged shares: {d['hedged_share_pct']:.1f}% of book")
    out.append(
        f"- net-long side won: {d['net_long_winner_pct']:.1f}% of two-sided markets"
    )
    out.append("")
    return out


def write_report(
    label: str,
    crypto_tbl: pd.DataFrame,
    mkt_tbl: pd.DataFrame,
    btc_stats: pd.DataFrame,
    metrics: dict,
    decomp_overall: dict,
    decomp_btc: dict,
    accum: dict,
    chop: pd.DataFrame,
    dir_metrics: dict,
    calibration: pd.DataFrame,
    coverage: dict,
    sell_metrics: dict,
    class_pnl: pd.DataFrame,
    charts: list[tuple[str, str]],
    outdir: Path,
) -> Path:
    lines: list[str] = []
    lines.append(f"# Trade Analysis — {label}\n")

    lines.append("## 1. Crypto P&L (all)\n")
    lines.append(crypto_tbl.round(2).to_markdown())
    lines.append("")

    lines.append("## 2. Crypto × market-type P&L (all)\n")
    lines.append(mkt_tbl.round(2).to_markdown())
    lines.append("")

    lines.append("## 3. BTC dataset — per market type\n")
    lines.append(btc_stats.round(4).to_markdown(index=False))
    lines.append("")

    lines.append("## 4. BTC strategy-inference metrics\n")
    for k, v in metrics.items():
        if isinstance(v, float):
            lines.append(f"- **{k}**: {v:,.4f}")
        else:
            lines.append(f"- **{k}**: {v}")
    lines.append("")

    lines.append("## 5. Profit theory — how they make money\n")
    lines.append(
        "Every winning share redeems at $1, so the exact edge is "
        "`share_win_rate − avg_price_per_share`. Compare the share-weighted "
        "**Up+Down pair cost** to $1.00 (below = hedge bought under fair value) "
        "and check **net-long side won %** for a directional tilt.\n"
    )
    lines += _decomp_lines("All cryptos", decomp_overall)
    lines += _decomp_lines("BTC only", decomp_btc)

    lines.append("## 6. Accumulation & direction (BTC)\n")
    lines.append(
        "Does the algo lean toward the winner, and does the lean develop over "
        "the window? Flat ~50% + mostly-balanced books = a symmetric ladder; a "
        "rising lean = active directional quote management (gabagool22-style).\n"
    )
    if accum:
        for k, v in accum.items():
            lines.append(f"- **{k}**: {v:,.4f}" if isinstance(v, float) else f"- **{k}**: {v}")
    lines.append("")

    lines.append("## 7. Choppiness vs edge (BTC)\n")
    lines.append(
        "P&L and per-share edge by within-market price choppiness. Per-share "
        "edge controls for position size.\n"
    )
    if chop is not None and len(chop):
        lines.append(chop.to_markdown())
    lines.append("")

    lines.append("## 8. Direction & entry-price calibration (BTC)\n")
    lines.append(
        "Does it pick one direction, how often is it right, and is the entry "
        "price just the market's implied probability? Direction accuracy uses "
        "realized-P&L sign on one-sided markets (unbiased).\n"
    )
    for k, v in dir_metrics.items():
        lines.append(f"- **{k}**: {v:,.4f}" if isinstance(v, float) else f"- **{k}**: {v}")
    if calibration is not None and len(calibration):
        lines.append("\nEntry-price calibration (entry price → actual win rate):\n")
        lines.append(calibration.to_markdown())
    lines.append("")

    lines.append("## 9. Market coverage & entry timing (BTC 5-min)\n")
    lines.append(
        "Does it enter every 5-min market, and when? Low coverage + mid-window "
        "entry = selective, momentum-confirmation entry (waits for the move).\n"
    )
    for k, v in coverage.items():
        lines.append(f"- **{k}**: {v:,.4f}" if isinstance(v, float) else f"- **{k}**: {v}")
    lines.append("")

    lines.append("## 10. Sell behavior & 3-leg play (all cryptos)\n")
    lines.append(
        "Does it sell? And does it run the 3-leg play (buy favorite → sell for "
        "profit → buy the other side as a lottery)? `lottery_roi_pct` shows "
        "whether the longshot legs are net +EV.\n"
    )
    for k, v in sell_metrics.items():
        lines.append(f"- **{k}**: {v:,.4f}" if isinstance(v, float) else f"- **{k}**: {v}")
    if class_pnl is not None and len(class_pnl):
        lines.append("\nP&L by strategy class (note: 3-leg vs roundtrip-only is "
                     "outcome-dependent — the lottery leg is only added after a "
                     "profitable sell):\n")
        lines.append(class_pnl.to_markdown())
    lines.append("")

    lines.append("## 11. Charts\n")
    for name, caption in charts:
        title = name.replace(".png", "").replace("_", " ")
        lines.append(f"### {title}\n")
        lines.append(f"![{caption}]({name})\n")
        lines.append(f"*{caption}*\n")

    report = outdir / "REPORT.md"
    report.write_text("\n".join(lines) + "\n")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trades", required=True)
    ap.add_argument("--positions", required=True)
    ap.add_argument("--closed", default=None)
    ap.add_argument("--outdir", default=None)
    args = ap.parse_args()

    trades_path = Path(args.trades)
    label = trades_path.stem
    outdir = Path(args.outdir) if args.outdir else Path("analysis") / label
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"Loading positions: {args.positions}")
    pos = load_positions(args.positions)
    print(f"Loading trades: {args.trades}")
    trades = load_trades(args.trades)

    crypto_tbl = crypto_profitability(pos)
    mkt_tbl = market_type_profitability(pos)
    btc_stats = btc_market_type_stats(trades)
    metrics = btc_strategy_metrics(trades, pos)
    decomp_overall = profit_decomposition(pos)
    decomp_btc = profit_decomposition(pos, crypto="BTC")
    accum, lean_by_trade = accumulation_metrics(trades, pos)
    chop = choppiness_table(trades, pos)
    dir_metrics, calibration, _first = direction_and_calibration(trades, pos)
    coverage = coverage_and_timing(trades)
    sell_metrics, class_pnl = sell_and_three_leg(trades, pos)
    charts = make_charts(pos, trades, outdir, lean_by_trade=lean_by_trade, chop=chop)
    report = write_report(
        label, crypto_tbl, mkt_tbl, btc_stats, metrics,
        decomp_overall, decomp_btc, accum, chop,
        dir_metrics, calibration, coverage, sell_metrics, class_pnl,
        charts, outdir,
    )

    # Console summary
    pd.set_option("display.max_rows", None)
    pd.set_option("display.width", 200)
    print("\n=== CRYPTO P&L (ALL) ===")
    print(crypto_tbl.round(2).to_string())
    print("\n=== CRYPTO x MARKET-TYPE P&L (ALL) ===")
    print(mkt_tbl.round(2).to_string())
    print("\n=== BTC DATASET PER MARKET TYPE ===")
    print(btc_stats.round(4).to_string(index=False))
    print("\n=== BTC STRATEGY METRICS ===")
    for k, v in metrics.items():
        print(f"  {k}: {v}")
    print("\n=== PROFIT THEORY: HEDGED vs DIRECTIONAL (ALL) ===")
    for k, v in decomp_overall.items():
        print(f"  {k}: {v}")
    print("\n=== PROFIT THEORY: HEDGED vs DIRECTIONAL (BTC) ===")
    for k, v in decomp_btc.items():
        print(f"  {k}: {v}")
    print("\n=== ACCUMULATION & DIRECTION (BTC) ===")
    for k, v in accum.items():
        print(f"  {k}: {v}")
    print("\n=== CHOPPINESS vs EDGE (BTC) ===")
    if len(chop):
        print(chop.to_string())
    print("\n=== DIRECTION & CALIBRATION (BTC) ===")
    for k, v in dir_metrics.items():
        print(f"  {k}: {v}")
    if len(calibration):
        print(calibration.to_string())
    print("\n=== COVERAGE & ENTRY TIMING (BTC 5-min) ===")
    for k, v in coverage.items():
        print(f"  {k}: {v}")
    print("\n=== SELL & 3-LEG (all cryptos) ===")
    for k, v in sell_metrics.items():
        print(f"  {k}: {v}")
    if len(class_pnl):
        print(class_pnl.to_string())
    print(f"\nReport: {report}")
    print(f"Charts ({len(charts)}): {', '.join(n for n, _ in charts)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
