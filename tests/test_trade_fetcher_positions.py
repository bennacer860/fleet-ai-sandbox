"""Tests for wallet trade position P&L aggregation and resolution matching."""

from __future__ import annotations

import csv
from pathlib import Path

from src.trade_fetcher import (
    _parse_gamma_winning_info,
    _token_is_winner,
    compute_and_write_positions_csv,
)


def _trade(**kwargs):
    base = {
        "condition_id": "cid1",
        "event_slug": "btc-15min-up-or-down-2026-06-11-00:00",
        "outcome": "Up",
        "asset": "token-up",
        "side": "BUY",
        "size": 100.0,
        "usdc_value": 40.0,
    }
    base.update(kwargs)
    return base


def test_parse_gamma_winning_info_resolved():
    market = {
        "clobTokenIds": '["token-up","token-down"]',
        "outcomes": '["Up","Down"]',
        "outcomePrices": '["0.01","0.99"]',
    }
    info = _parse_gamma_winning_info(market)
    assert info["resolved"] is True
    assert info["winning_token"] == "token-down"
    assert info["winning_outcome"] == "Down"


def test_parse_gamma_winning_info_unresolved():
    market = {
        "clobTokenIds": '["token-up","token-down"]',
        "outcomes": '["Up","Down"]',
        "outcomePrices": '["0.55","0.45"]',
    }
    info = _parse_gamma_winning_info(market)
    assert info["resolved"] is False
    assert info["winning_token"] is None


def test_token_is_winner_prefers_token_id():
    resolution = {
        "resolved": True,
        "winning_token": "token-down",
        "winning_outcome": "Down",
    }
    assert _token_is_winner("Up", "token-up", resolution) is False
    assert _token_is_winner("Down", "token-down", resolution) is True


def test_positions_split_by_outcome_and_pnl(tmp_path: Path):
    """Up and Down on the same market must not be merged."""
    trades = [
        _trade(outcome="Up", asset="token-up", side="BUY", size=100, usdc_value=80),
        _trade(outcome="Down", asset="token-down", side="BUY", size=100, usdc_value=20),
    ]
    outcomes = {
        "cid1": {
            "resolved": True,
            "winning_token": "token-down",
            "winning_outcome": "Down",
        }
    }
    out = tmp_path / "positions.csv"
    n, resolved_n = compute_and_write_positions_csv(trades, outcomes, str(out))
    assert n == 2
    assert resolved_n == 2

    rows = {r["outcome"]: r for r in csv.DictReader(out.open())}
    # Down wins: settlement $1 on 100 shares → pnl = 0 - 20 + 100 = 80
    assert rows["Down"]["winner"] == "True"
    assert float(rows["Down"]["pnl"]) == 80.0
    # Up loses: settlement $0 → pnl = 0 - 80 + 0 = -80
    assert rows["Up"]["winner"] == "False"
    assert float(rows["Up"]["pnl"]) == -80.0
    # Combined portfolio P&L for this hedged market is $0
    assert float(rows["Up"]["pnl"]) + float(rows["Down"]["pnl"]) == 0.0


def test_positions_prefer_closed_pnl(tmp_path: Path):
    trades = [
        _trade(outcome="Down", asset="token-down", side="BUY", size=100, usdc_value=10),
    ]
    outcomes = {}
    closed = {
        ("cid1", "token-down"): {"realized_pnl": 42.5, "winner": True, "cur_price": 1.0},
    }
    out = tmp_path / "positions.csv"
    n, resolved_n = compute_and_write_positions_csv(
        trades, outcomes, str(out), closed_pnl=closed
    )
    assert n == 1 and resolved_n == 1
    row = next(csv.DictReader(out.open()))
    assert float(row["pnl"]) == 42.5
    assert row["winner"] == "True"


def test_positions_unresolved_leave_pnl_blank(tmp_path: Path):
    trades = [_trade()]
    outcomes = {"cid1": {"resolved": False, "winning_token": None, "winning_outcome": None}}
    out = tmp_path / "positions.csv"
    n, resolved_n = compute_and_write_positions_csv(trades, outcomes, str(out))
    assert n == 1
    assert resolved_n == 0
    row = next(csv.DictReader(out.open()))
    assert row["pnl"] == ""
    assert row["resolved"] == "False"
