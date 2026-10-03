#!/usr/bin/env python3
"""Replay and validate a personal trading style from an OKX unified bill export.

This is a bill-level research experiment.  It does not pretend that the export
contains a reliable open/close flag: zero-realized-PnL orders are treated as
entry candidates and the opposite-side realized-PnL order closes the candidate
episode.  The resulting episodes are useful labels for style research, while
the policy comparison only uses features known before entry.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import re
import statistics
import zipfile
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Sequence


UTC8 = timezone(timedelta(hours=8))
TRADE_TYPES = {"买入", "卖出"}
FUNDING_TYPES = {"资金费收入", "资金费支出"}
LIQUIDATION_TYPES = {"强平卖出", "强平惩罚费", "穿仓代偿"}
NET_EVENT_TYPES = FUNDING_TYPES | LIQUIDATION_TYPES


def decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value or "0").strip())
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.00000001")))


def parse_time(value: str) -> datetime:
    return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC8)


def format_time(value: datetime | None) -> str:
    return value.astimezone(UTC8).strftime("%Y-%m-%d %H:%M:%S") if value else ""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class BillRow:
    timestamp: datetime
    bill_type: str
    symbol: str
    trade_type: str
    order_id: str
    quantity: Decimal
    price: Decimal
    realized_pnl: Decimal
    fee: Decimal
    account_change: Decimal
    position_change: Decimal = Decimal("0")

    @property
    def net(self) -> Decimal:
        return self.realized_pnl + self.fee


@dataclass
class OrderGroup:
    order_id: str
    symbol: str
    timestamp: datetime
    side: str
    quantity: Decimal = Decimal("0")
    price_quantity: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    fee: Decimal = Decimal("0")
    row_count: int = 0
    last_timestamp: datetime | None = None

    @property
    def average_price(self) -> Decimal:
        return self.price_quantity / self.quantity if self.quantity else Decimal("0")

    @property
    def net_pnl(self) -> Decimal:
        return self.realized_pnl + self.fee


@dataclass(frozen=True)
class BillEvent:
    timestamp: datetime
    symbol: str
    event_type: str
    realized_pnl: Decimal
    fee: Decimal
    quantity: Decimal
    account_change: Decimal = Decimal("0")
    position_change: Decimal = Decimal("0")

    @property
    def net_pnl(self) -> Decimal:
        reported = self.realized_pnl + self.fee
        return reported if reported != 0 else (self.position_change if self.position_change != 0 else self.account_change)


@dataclass
class Episode:
    symbol: str
    direction: str
    entry_time: datetime
    exit_time: datetime
    entry_price: Decimal
    exit_price: Decimal
    entry_quantity: Decimal
    exit_quantity: Decimal
    entry_orders: int
    add_count: int
    hold_minutes: float
    realized_pnl: Decimal
    fees: Decimal
    funding_pnl: Decimal
    liquidation_pnl: Decimal
    net_pnl: Decimal
    close_reason: str
    prior_6h_net: Decimal
    prior_24h_net: Decimal
    inference: str = "zero_realized_entry_approximation"


def _clean_row(raw: dict[str, str]) -> dict[str, str]:
    return {str(key).lstrip("\ufeff"): (value or "").strip() for key, value in raw.items()}


def read_ledger(path: Path) -> tuple[list[BillRow], dict[str, object]]:
    """Read the UTF-8-BOM OKX CSV inside a unified bill ZIP."""
    rows: list[BillRow] = []
    account_line = ""
    csv_name = ""
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"expected one CSV in {path}, found {len(names)}")
        csv_name = names[0]
        with archive.open(csv_name) as raw:
            stream = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
            account_line = next(stream, "").rstrip("\n")
            header_line = next(stream, "")
            header = [item.lstrip("\ufeff").strip() for item in next(csv.reader([header_line]))]
            for raw_row in csv.DictReader(stream, fieldnames=header):
                row = _clean_row(raw_row)
                if not row.get("时间") or not row.get("账单类型"):
                    continue
                try:
                    rows.append(
                        BillRow(
                            timestamp=parse_time(row["时间"]),
                            bill_type=row.get("账单类型", ""),
                            symbol=row.get("交易品种", ""),
                            trade_type=row.get("交易类型", ""),
                            order_id=row.get("关联订单id", ""),
                            quantity=decimal(row.get("数量")),
                            price=decimal(row.get("成交价")),
                            realized_pnl=decimal(row.get("收益")),
                            fee=decimal(row.get("手续费")),
                            account_change=decimal(row.get("交易账户余额变动")),
                            position_change=decimal(row.get("仓位余额变动")),
                        )
                    )
                except (KeyError, ValueError):
                    continue
    rows.sort(key=lambda item: item.timestamp)
    timestamps = [row.timestamp for row in rows]
    # Keep the source hash and time window for reproducibility without writing
    # the account's personally identifying user id into committed results.
    safe_account_line = re.sub(r"用户ID\s*:\s*[^,，\r\n]+", "用户ID:<redacted>", account_line)
    manifest = {
        "source_file": path.name,
        "source_sha256": sha256(path),
        "csv_member": csv_name,
        "account_metadata": safe_account_line,
        "row_count": len(rows),
        "first_timestamp": format_time(min(timestamps) if timestamps else None),
        "last_timestamp": format_time(max(timestamps) if timestamps else None),
        "timezone": "UTC+8",
    }
    return rows, manifest


def aggregate_order_groups(rows: Iterable[BillRow]) -> list[OrderGroup]:
    groups: dict[tuple[str, str], OrderGroup] = {}
    for row in rows:
        if row.bill_type != "永续合约" or row.trade_type not in TRADE_TYPES:
            continue
        key = (row.order_id, row.symbol)
        group = groups.setdefault(
            key,
            OrderGroup(row.order_id, row.symbol, row.timestamp, row.trade_type),
        )
        group.timestamp = min(group.timestamp, row.timestamp)
        group.last_timestamp = max(group.last_timestamp or row.timestamp, row.timestamp)
        group.quantity += abs(row.quantity)
        group.price_quantity += abs(row.quantity) * row.price
        group.realized_pnl += row.realized_pnl
        group.fee += row.fee
        group.row_count += 1
    return sorted(groups.values(), key=lambda item: (item.timestamp, item.order_id))


def collect_events(rows: Iterable[BillRow]) -> list[BillEvent]:
    return sorted(
        [
            BillEvent(row.timestamp, row.symbol, row.trade_type, row.realized_pnl, row.fee, row.quantity, row.account_change, row.position_change)
            for row in rows
            if row.bill_type == "永续合约" and row.trade_type in NET_EVENT_TYPES
        ],
        key=lambda item: item.timestamp,
    )


def _prior_symbol_net(
    orders: Sequence[OrderGroup], events: Sequence[BillEvent], symbol: str, timestamp: datetime, hours: int
) -> Decimal:
    start = timestamp - timedelta(hours=hours)
    total = sum(
        (group.net_pnl for group in orders if group.symbol == symbol and start <= group.timestamp < timestamp),
        Decimal("0"),
    )
    total += sum(
        (event.net_pnl for event in events if event.symbol == symbol and start <= event.timestamp < timestamp),
        Decimal("0"),
    )
    return total


def _weighted_price(groups: Sequence[OrderGroup], use_entry: bool) -> Decimal:
    selected = groups if use_entry else groups
    quantity = sum((group.quantity for group in selected), Decimal("0"))
    value = sum((group.price_quantity for group in selected), Decimal("0"))
    return value / quantity if quantity else Decimal("0")


def _make_episode(
    state: dict[str, object],
    close_time: datetime,
    close_reason: str,
    close_groups: Sequence[OrderGroup],
    close_events: Sequence[BillEvent],
    orders: Sequence[OrderGroup],
    events: Sequence[BillEvent],
) -> Episode:
    entry_groups = state["entry_groups"]  # type: ignore[assignment]
    assert isinstance(entry_groups, list)
    all_groups = entry_groups + list(close_groups)
    entry_time = state["entry_time"]
    assert isinstance(entry_time, datetime)
    symbol = str(state["symbol"])
    direction = str(state["direction"])
    relevant_events = [event for event in events if event.symbol == symbol and entry_time <= event.timestamp <= close_time]
    relevant_events.extend(event for event in close_events if event not in relevant_events)
    realized = sum((group.realized_pnl for group in all_groups), Decimal("0"))
    fees = sum((group.fee for group in all_groups), Decimal("0"))
    funding = sum((event.net_pnl for event in relevant_events if event.event_type in FUNDING_TYPES), Decimal("0"))
    liquidation = sum((event.net_pnl for event in relevant_events if event.event_type in LIQUIDATION_TYPES), Decimal("0"))
    entry_quantity = sum((group.quantity for group in entry_groups), Decimal("0"))
    exit_quantity = sum((group.quantity for group in close_groups), Decimal("0"))
    hold_minutes = (close_time - entry_time).total_seconds() / 60.0
    return Episode(
        symbol=symbol,
        direction=direction,
        entry_time=entry_time,
        exit_time=close_time,
        entry_price=_weighted_price(entry_groups, True),
        exit_price=_weighted_price(close_groups, False),
        entry_quantity=entry_quantity,
        exit_quantity=exit_quantity,
        entry_orders=len(entry_groups),
        add_count=max(0, len(entry_groups) - 1),
        hold_minutes=hold_minutes,
        realized_pnl=realized,
        fees=fees,
        funding_pnl=funding,
        liquidation_pnl=liquidation,
        net_pnl=realized + fees + funding + liquidation,
        close_reason=close_reason,
        prior_6h_net=_prior_symbol_net(orders, events, symbol, entry_time, 6),
        prior_24h_net=_prior_symbol_net(orders, events, symbol, entry_time, 24),
    )


def infer_episodes(orders: Sequence[OrderGroup], events: Sequence[BillEvent]) -> list[Episode]:
    """Infer closed style episodes without treating the export as exact positions."""
    by_symbol: dict[str, list[OrderGroup]] = defaultdict(list)
    for order in orders:
        by_symbol[order.symbol].append(order)
    by_event: dict[str, list[BillEvent]] = defaultdict(list)
    for event in events:
        by_event[event.symbol].append(event)
    episodes: list[Episode] = []
    for symbol, symbol_orders in by_symbol.items():
        timeline: list[tuple[datetime, int, object]] = []
        for order in symbol_orders:
            timeline.append((order.timestamp, 1, order))
        for event in by_event.get(symbol, []):
            timeline.append((event.timestamp, 0, event))
        timeline.sort(key=lambda item: (item[0], item[1]))
        state: dict[str, object] | None = None
        for timestamp, _, item in timeline:
            if isinstance(item, BillEvent):
                if state is not None:
                    if item.event_type in FUNDING_TYPES:
                        state.setdefault("events", []).append(item)  # type: ignore[union-attr]
                    elif item.event_type in LIQUIDATION_TYPES:
                        episodes.append(
                            _make_episode(state, timestamp, "liquidation", [], [item], symbol_orders, by_event[symbol])
                        )
                        state = None
                continue
            order = item
            assert isinstance(order, OrderGroup)
            if state is None:
                if order.realized_pnl == 0:
                    state = {
                        "symbol": symbol,
                        "direction": "short" if order.side == "卖出" else "long",
                        "entry_time": order.timestamp,
                        "entry_groups": [order],
                        "events": [],
                    }
                continue
            direction = str(state["direction"])
            entry_side = "卖出" if direction == "short" else "买入"
            opposite_side = "买入" if entry_side == "卖出" else "卖出"
            entry_groups = state["entry_groups"]
            assert isinstance(entry_groups, list)
            if order.side == entry_side and order.realized_pnl == 0:
                entry_groups.append(order)
                continue
            if order.side == opposite_side and order.realized_pnl != 0:
                episodes.append(
                    _make_episode(state, order.last_timestamp or order.timestamp, "realized_exit", [order], state.get("events", []), symbol_orders, by_event[symbol])  # type: ignore[arg-type]
                )
                state = None
                continue
            if order.side == opposite_side and order.realized_pnl == 0:
                # A zero-realized opposite-side order is ambiguous in net or hedge mode.
                # End the current candidate and start a new candidate from this order.
                state = {
                    "symbol": symbol,
                    "direction": "short" if order.side == "卖出" else "long",
                    "entry_time": order.timestamp,
                    "entry_groups": [order],
                    "events": [],
                }
                continue
            # Same-side realized PnL cannot be assigned safely to this candidate.
            state = None
    return sorted(episodes, key=lambda item: (item.entry_time, item.symbol))


def _profit_factor(values: Sequence[Decimal]) -> float:
    gross_win = sum((value for value in values if value > 0), Decimal("0"))
    gross_loss = -sum((value for value in values if value < 0), Decimal("0"))
    return money(gross_win / gross_loss) if gross_loss else (999999999.0 if gross_win else 0.0)


def _max_drawdown(values: Sequence[Decimal]) -> Decimal:
    cumulative = Decimal("0")
    peak = Decimal("0")
    drawdown = Decimal("0")
    for value in values:
        cumulative += value
        peak = max(peak, cumulative)
        drawdown = min(drawdown, cumulative - peak)
    return drawdown


def evaluate(episodes: Sequence[Episode]) -> dict[str, object]:
    ordered = sorted(episodes, key=lambda item: (item.exit_time, item.symbol, item.entry_time))
    values = [episode.net_pnl for episode in ordered]
    return {
        "episodes": len(episodes),
        "wins": sum(value > 0 for value in values),
        "losses": sum(value < 0 for value in values),
        "win_rate": round(sum(value > 0 for value in values) / len(values), 6) if values else 0.0,
        "net_pnl": money(sum(values, Decimal("0"))),
        "average_net_pnl": money(sum(values, Decimal("0")) / len(values)) if values else 0.0,
        "median_net_pnl": money(Decimal(str(statistics.median(values)))) if values else 0.0,
        "profit_factor": _profit_factor(values),
        "max_drawdown": money(_max_drawdown(values)),
    }


def policy_matches(episode: Episode, policy: dict[str, object]) -> bool:
    if policy.get("direction", "any") != "any" and episode.direction != policy["direction"]:
        return False
    if policy.get("require_prior_6h_nonnegative", False) and episode.prior_6h_net < 0:
        return False
    if policy.get("require_prior_24h_nonnegative", False) and episode.prior_24h_net < 0:
        return False
    return True


def split_labels(episodes: Sequence[Episode], config: dict[str, object]) -> dict[int, str]:
    # Allocate by exit time so an episode's realized outcome never crosses a
    # train/validation boundary while remaining in the earlier split.
    ordered = sorted(episodes, key=lambda item: (item.exit_time, item.symbol, item.entry_time))
    split = config.get("walk_forward", {})
    train_ratio = float(split.get("train_ratio", 0.6))
    validation_ratio = float(split.get("validation_ratio", 0.2))
    train_end = max(1, int(len(ordered) * train_ratio)) if ordered else 0
    validation_end = max(train_end, int(len(ordered) * (train_ratio + validation_ratio)))
    labels: dict[int, str] = {}
    for index, episode in enumerate(ordered):
        labels[id(episode)] = "train" if index < train_end else "validation" if index < validation_end else "test"
    return labels


def evaluate_policies(episodes: Sequence[Episode], config: dict[str, object]) -> list[dict[str, object]]:
    policies = config.get("candidate_policies", [])
    if not isinstance(policies, list):
        raise ValueError("candidate_policies must be a list")
    labels = split_labels(episodes, config)
    output: list[dict[str, object]] = []
    for policy in policies:
        if not isinstance(policy, dict) or "name" not in policy:
            continue
        selected = [episode for episode in episodes if policy_matches(episode, policy)]
        for split in ("all", "train", "validation", "test"):
            scoped = selected if split == "all" else [episode for episode in selected if labels[id(episode)] == split]
            metrics = evaluate(sorted(scoped, key=lambda item: item.exit_time))
            output.append({"policy": policy["name"], "split": split, **metrics})
    return output


def choose_policy(evaluations: Sequence[dict[str, object]], config: dict[str, object]) -> str:
    minimum = int(config.get("walk_forward", {}).get("minimum_train_episodes", 5))
    candidates = [
        row for row in evaluations
        if row["split"] == "train"
        and int(row["episodes"]) >= minimum
        and next(
            (bool(policy.get("selection_eligible", True))
             for policy in config.get("candidate_policies", [])
             if isinstance(policy, dict) and policy.get("name") == row["policy"]),
            True,
        )
    ]
    if not candidates:
        return "all_closed_episodes"
    best = max(candidates, key=lambda row: (float(row["net_pnl"]), float(row["profit_factor"])))
    return str(best["policy"])


def write_csv(path: Path, rows: Sequence[dict[str, object]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def order_rows(groups: Sequence[OrderGroup]) -> list[dict[str, object]]:
    return [
        {
            "order_id": group.order_id,
            "symbol": group.symbol,
            "timestamp": format_time(group.timestamp),
            "last_timestamp": format_time(group.last_timestamp or group.timestamp),
            "side": group.side,
            "quantity": money(group.quantity),
            "average_price": money(group.average_price),
            "realized_pnl": money(group.realized_pnl),
            "fee": money(group.fee),
            "net_pnl": money(group.net_pnl),
            "row_count": group.row_count,
        }
        for group in groups
    ]


def episode_row(episode: Episode, policy: str = "") -> dict[str, object]:
    row = {
        "symbol": episode.symbol,
        "direction": episode.direction,
        "entry_time": format_time(episode.entry_time),
        "exit_time": format_time(episode.exit_time),
        "entry_price": money(episode.entry_price),
        "exit_price": money(episode.exit_price),
        "entry_quantity": money(episode.entry_quantity),
        "exit_quantity": money(episode.exit_quantity),
        "entry_orders": episode.entry_orders,
        "add_count": episode.add_count,
        "hold_minutes": round(episode.hold_minutes, 3),
        "realized_pnl": money(episode.realized_pnl),
        "fees": money(episode.fees),
        "funding_pnl": money(episode.funding_pnl),
        "liquidation_pnl": money(episode.liquidation_pnl),
        "net_pnl": money(episode.net_pnl),
        "close_reason": episode.close_reason,
        "prior_6h_net": money(episode.prior_6h_net),
        "prior_24h_net": money(episode.prior_24h_net),
        "inference": episode.inference,
    }
    if policy:
        row["policy"] = policy
    return row


def build_style_metrics(rows: Sequence[BillRow], groups: Sequence[OrderGroup], events: Sequence[BillEvent], episodes: Sequence[Episode], manifest: dict[str, object]) -> dict[str, object]:
    perpetual = [row for row in rows if row.bill_type == "永续合约"]
    trades = [row for row in perpetual if row.trade_type in TRADE_TYPES]
    funding = [row for row in perpetual if row.trade_type in FUNDING_TYPES]
    liquidation = [row for row in perpetual if row.trade_type in LIQUIDATION_TYPES]
    margin_add = [row for row in perpetual if row.trade_type == "手动追加保证金"]
    margin_remove = [row for row in perpetual if row.trade_type == "手动减少保证金"]
    symbols = Counter(row.symbol for row in trades)
    side = Counter(row.trade_type for row in trades)
    episode_direction = Counter(episode.direction for episode in episodes)
    actual_strategy_net = sum(
        (
            row.net if row.net != 0 else (row.position_change if row.position_change != 0 else row.account_change)
            for row in perpetual
            if row.trade_type in TRADE_TYPES | FUNDING_TYPES | LIQUIDATION_TYPES
        ),
        Decimal("0"),
    )
    episode_net = sum((episode.net_pnl for episode in episodes), Decimal("0"))
    actual_trade_realized = sum((row.realized_pnl for row in trades), Decimal("0"))
    episode_trade_realized = sum((episode.realized_pnl for episode in episodes), Decimal("0"))
    return {
        "source": manifest,
        "rows": {"all": len(rows), "perpetual": len(perpetual), "perpetual_trade_rows": len(trades)},
        "orders": {"groups": len(groups), "side": dict(side), "symbols": len(symbols), "top_symbols_by_rows": symbols.most_common(20)},
        "episodes": {"closed": len(episodes), "direction": dict(episode_direction), "all_closed": evaluate(episodes)},
        "pnl": {
            "trade_realized_pnl": money(sum((row.realized_pnl for row in trades), Decimal("0"))),
            "trade_fees": money(sum((row.fee for row in trades), Decimal("0"))),
            "funding_net": money(sum((row.net for row in funding), Decimal("0"))),
            "liquidation_net": money(sum((row.net if row.net != 0 else (row.position_change if row.position_change != 0 else row.account_change) for row in liquidation), Decimal("0"))),
            "position_change": money(sum((row.position_change for row in perpetual), Decimal("0"))),
            "account_change": money(sum((row.account_change for row in perpetual), Decimal("0"))),
            "perpetual_net_excluding_transfers": money(actual_strategy_net),
        },
        "episode_coverage": {
            "inferred_episode_net": money(episode_net),
            "unassigned_strategy_net": money(actual_strategy_net - episode_net),
            "inferred_episode_realized_pnl": money(episode_trade_realized),
            "unassigned_realized_pnl": money(actual_trade_realized - episode_trade_realized),
            "note": "Unassigned values are expected because the bill export does not expose exact open/close state; they are a diagnostic, not missing cash.",
        },
        "margin": {
            "add_events": len(margin_add),
            "add_amount": money(sum((row.quantity for row in margin_add), Decimal("0"))),
            "remove_events": len(margin_remove),
            "remove_amount": money(sum((row.quantity for row in margin_remove), Decimal("0"))),
        },
        "limitations": [
            "OKX unified bills do not expose a reliable per-fill open/close flag in this export.",
            "Episodes use zero realized PnL as an entry candidate and are approximate labels.",
            "The policy comparison uses only entry-time direction and prior-symbol PnL; it is not an OHLC price backtest.",
            "Transfers and manual margin movements are excluded from strategy PnL.",
        ],
    }


def run(ledger: Path, config_path: Path, output_dir: Path) -> dict[str, object]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    rows, manifest = read_ledger(ledger)
    groups = aggregate_order_groups(rows)
    events = collect_events(rows)
    episodes = infer_episodes(groups, events)
    evaluations = evaluate_policies(episodes, config)
    selected_policy = choose_policy(evaluations, config)
    # Policy selection is based on train-split metrics only.  The direct lookup
    # below keeps the episode filter independent from post-entry outcomes.
    selected_spec = next((policy for policy in config.get("candidate_policies", []) if policy.get("name") == selected_policy), {"name": selected_policy})
    selected_episodes = [episode for episode in episodes if policy_matches(episode, selected_spec)]
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "source_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(output_dir / "order_groups.csv", order_rows(groups), ["order_id", "symbol", "timestamp", "last_timestamp", "side", "quantity", "average_price", "realized_pnl", "fee", "net_pnl", "row_count"])
    episode_fields = list(episode_row(episodes[0]).keys()) if episodes else list(episode_row(Episode("", "", datetime.now(UTC8), datetime.now(UTC8), Decimal(0), Decimal(0), Decimal(0), Decimal(0), 0, 0, 0, Decimal(0), Decimal(0), Decimal(0), Decimal(0), Decimal(0), "", Decimal(0), Decimal(0))).keys())
    write_csv(output_dir / "episodes.csv", [episode_row(episode) for episode in episodes], episode_fields)
    write_csv(output_dir / "policy_evaluation.csv", evaluations, ["policy", "split", "episodes", "wins", "losses", "win_rate", "net_pnl", "average_net_pnl", "median_net_pnl", "profit_factor", "max_drawdown"])
    selected_rows = [episode_row(episode, selected_policy) for episode in sorted(selected_episodes, key=lambda item: item.entry_time)]
    write_csv(output_dir / "counterfactual_trades.csv", selected_rows, list(selected_rows[0].keys()) if selected_rows else episode_fields + ["policy"])
    equity_rows: list[dict[str, object]] = []
    cumulative = Decimal("0")
    peak = Decimal("0")
    for episode in sorted(selected_episodes, key=lambda item: item.exit_time):
        cumulative += episode.net_pnl
        peak = max(peak, cumulative)
        equity_rows.append({"timestamp": format_time(episode.exit_time), "symbol": episode.symbol, "net_pnl": money(episode.net_pnl), "cumulative_net_pnl": money(cumulative), "drawdown": money(cumulative - peak), "policy": selected_policy})
    write_csv(output_dir / "equity.csv", equity_rows, ["timestamp", "symbol", "net_pnl", "cumulative_net_pnl", "drawdown", "policy"])
    metrics = build_style_metrics(rows, groups, events, episodes, manifest)
    (output_dir / "style_metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    report = {
        "strategy_id": config.get("strategy_id"),
        "version": config.get("version"),
        "selected_policy": selected_policy,
        "selected_policy_metrics": evaluate(selected_episodes),
        "selected_policy_walk_forward": [row for row in evaluations if row["policy"] == selected_policy],
        "policy_evaluation_file": "policy_evaluation.csv",
        "style_metrics_file": "style_metrics.json",
        "limitations": metrics["limitations"],
    }
    (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True, help="OKX unified bill ZIP export")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "config/strategy.json")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parents[1] / "results")
    args = parser.parse_args()
    report = run(args.ledger, args.config, args.output_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
