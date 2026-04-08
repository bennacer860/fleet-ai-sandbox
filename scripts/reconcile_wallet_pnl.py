#!/usr/bin/env python3
"""Reconcile wallet-level PnL with trade CSV diagnostics.

This script helps reconcile differences between:
1) Website-level "profit over period" metrics (NAV-based), and
2) Trade-window calculations from exported CSV rows.

Inputs:
- Trade CSV exported by fetch_wallet_trades.py
- Optional account-level period metrics (start/end NAV, deposits/withdrawals, rewards, fees)

Core formulas:
- net_deposits = deposits - withdrawals
- account_profit_ex_cashflows = end_nav - start_nav - net_deposits
- trading_profit_ex_rewards = account_profit_ex_cashflows - rewards + fees
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class TradeDiagnostics:
    total_rows: int
    buy_rows: int
    sell_rows: int
    gross_buy_usdc: float
    gross_sell_usdc: float
    gross_notional_usdc: float
    mirrored_rows: int
    mirrored_notional_usdc: float
    unmatched_sell_usdc: float
    matched_sell_usdc: float


@dataclass
class NavReconciliation:
    start_nav: float
    end_nav: float
    deposits: float
    withdrawals: float
    rewards: float
    fees: float
    net_deposits: float
    account_profit_ex_cashflows: float
    trading_profit_ex_rewards: float


def load_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _f(row: dict[str, str], key: str) -> float:
    try:
        return float(row.get(key, "") or 0.0)
    except ValueError:
        return 0.0


def compute_trade_diagnostics(rows: list[dict[str, str]]) -> TradeDiagnostics:
    buy_rows = 0
    sell_rows = 0
    gross_buy_usdc = 0.0
    gross_sell_usdc = 0.0

    # Mirror detection: same tx + same leg + same timestamp + same price + same size, opposite sides.
    mirror_bucket: dict[tuple[str, ...], dict[str, int | float]] = defaultdict(
        lambda: {"BUY": 0, "SELL": 0, "notional": 0.0}
    )

    # Unmatched sell notional approximation within the window.
    # Tracks quantity inventory by (condition_id, outcome) from CSV rows only.
    window_inventory_qty: dict[tuple[str, str], float] = defaultdict(float)
    unmatched_sell_usdc = 0.0
    matched_sell_usdc = 0.0

    # Process in timestamp order for inventory matching.
    rows_sorted = sorted(rows, key=lambda r: int(r.get("timestamp", "0") or 0))

    for row in rows_sorted:
        side = (row.get("side") or "").upper()
        usdc = _f(row, "usdc_value")
        size = _f(row, "size")

        if side == "BUY":
            buy_rows += 1
            gross_buy_usdc += usdc
        elif side == "SELL":
            sell_rows += 1
            gross_sell_usdc += usdc

        mirror_key = (
            row.get("transaction_hash", "") or "",
            row.get("condition_id", "") or "",
            row.get("asset", "") or "",
            row.get("outcome", "") or "",
            row.get("price", "") or "",
            row.get("size", "") or "",
            row.get("timestamp", "") or "",
        )
        if side in ("BUY", "SELL"):
            mirror_bucket[mirror_key][side] = int(mirror_bucket[mirror_key][side]) + 1
            mirror_bucket[mirror_key]["notional"] = usdc

        inv_key = (
            row.get("condition_id", "") or "",
            (row.get("outcome", "") or "").strip(),
        )
        if side == "BUY":
            window_inventory_qty[inv_key] += size
        elif side == "SELL":
            have = window_inventory_qty[inv_key]
            if have <= 0:
                unmatched_sell_usdc += usdc
            else:
                matched_sell_usdc += usdc
                window_inventory_qty[inv_key] = max(0.0, have - size)

    mirrored_rows = 0
    mirrored_notional_usdc = 0.0
    for item in mirror_bucket.values():
        mirrored_pairs = min(int(item["BUY"]), int(item["SELL"]))
        if mirrored_pairs > 0:
            mirrored_rows += 2 * mirrored_pairs
            mirrored_notional_usdc += 2.0 * mirrored_pairs * float(item["notional"])

    return TradeDiagnostics(
        total_rows=len(rows),
        buy_rows=buy_rows,
        sell_rows=sell_rows,
        gross_buy_usdc=gross_buy_usdc,
        gross_sell_usdc=gross_sell_usdc,
        gross_notional_usdc=gross_buy_usdc + gross_sell_usdc,
        mirrored_rows=mirrored_rows,
        mirrored_notional_usdc=mirrored_notional_usdc,
        unmatched_sell_usdc=unmatched_sell_usdc,
        matched_sell_usdc=matched_sell_usdc,
    )


def compute_nav_reconciliation(
    start_nav: float,
    end_nav: float,
    deposits: float,
    withdrawals: float,
    rewards: float,
    fees: float,
) -> NavReconciliation:
    net_deposits = deposits - withdrawals
    account_profit_ex_cashflows = end_nav - start_nav - net_deposits
    trading_profit_ex_rewards = account_profit_ex_cashflows - rewards + fees
    return NavReconciliation(
        start_nav=start_nav,
        end_nav=end_nav,
        deposits=deposits,
        withdrawals=withdrawals,
        rewards=rewards,
        fees=fees,
        net_deposits=net_deposits,
        account_profit_ex_cashflows=account_profit_ex_cashflows,
        trading_profit_ex_rewards=trading_profit_ex_rewards,
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Reconcile website PnL with trade CSV diagnostics.")
    p.add_argument("--trades-csv", required=True, help="Path to wallet trade CSV.")

    p.add_argument("--start-nav", type=float, default=None, help="Account NAV at period start.")
    p.add_argument("--end-nav", type=float, default=None, help="Account NAV at period end.")
    p.add_argument("--deposits", type=float, default=0.0, help="Total deposits during period.")
    p.add_argument("--withdrawals", type=float, default=0.0, help="Total withdrawals during period.")
    p.add_argument("--rewards", type=float, default=0.0, help="Rewards/rebates credited during period.")
    p.add_argument("--fees", type=float, default=0.0, help="Fees paid during period.")

    p.add_argument(
        "--website-profit",
        type=float,
        default=None,
        help="Displayed website profit for the same period (for difference check).",
    )
    p.add_argument(
        "--output-json",
        default="",
        help="Optional output JSON path for machine-readable report.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    csv_path = Path(args.trades_csv).expanduser().resolve()
    rows = load_rows(csv_path)
    diag = compute_trade_diagnostics(rows)

    print("=== Trade Diagnostics ===")
    print(f"CSV: {csv_path}")
    print(f"Rows: {diag.total_rows} (BUY={diag.buy_rows}, SELL={diag.sell_rows})")
    print(f"Gross buy USDC:   {diag.gross_buy_usdc:,.4f}")
    print(f"Gross sell USDC:  {diag.gross_sell_usdc:,.4f}")
    print(f"Gross notional:   {diag.gross_notional_usdc:,.4f}")
    print(f"Mirrored rows:    {diag.mirrored_rows}")
    print(f"Mirrored notion.: {diag.mirrored_notional_usdc:,.4f}")
    print(f"Matched sells:    {diag.matched_sell_usdc:,.4f}")
    print(f"Unmatched sells:  {diag.unmatched_sell_usdc:,.4f}")
    if diag.gross_notional_usdc > 0:
        pct = 100.0 * diag.unmatched_sell_usdc / diag.gross_notional_usdc
        print(f"Unmatched sells / gross: {pct:.2f}%")

    nav = None
    if args.start_nav is not None and args.end_nav is not None:
        nav = compute_nav_reconciliation(
            start_nav=args.start_nav,
            end_nav=args.end_nav,
            deposits=args.deposits,
            withdrawals=args.withdrawals,
            rewards=args.rewards,
            fees=args.fees,
        )
        print("\n=== NAV Reconciliation ===")
        print(f"Start NAV: {nav.start_nav:,.4f}")
        print(f"End NAV:   {nav.end_nav:,.4f}")
        print(f"Deposits:  {nav.deposits:,.4f}")
        print(f"Withdraws: {nav.withdrawals:,.4f}")
        print(f"Rewards:   {nav.rewards:,.4f}")
        print(f"Fees:      {nav.fees:,.4f}")
        print(f"Net deposits:                 {nav.net_deposits:,.4f}")
        print(f"Account profit ex cashflows: {nav.account_profit_ex_cashflows:,.4f}")
        print(f"Trading profit ex rewards:   {nav.trading_profit_ex_rewards:,.4f}")

        if args.website_profit is not None:
            delta = nav.account_profit_ex_cashflows - args.website_profit
            print(f"Website profit: {args.website_profit:,.4f}")
            print(f"Difference (recon - website): {delta:,.4f}")
    elif args.website_profit is not None:
        print("\nWebsite profit was provided, but NAV inputs are missing.")
        print("Provide --start-nav and --end-nav (and optional cashflow fields) for reconciliation.")

    if args.output_json:
        payload: dict[str, object] = {"trade_diagnostics": asdict(diag)}
        if nav is not None:
            payload["nav_reconciliation"] = asdict(nav)
        if args.website_profit is not None:
            payload["website_profit"] = args.website_profit
        out_path = Path(args.output_json).expanduser().resolve()
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nWrote JSON report: {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
