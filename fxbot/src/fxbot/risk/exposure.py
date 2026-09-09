"""Portfolio exposure checks (§8.4). Pure.

Checked in order: ``MAX_POSITIONS`` -> ``SYMBOL_ALREADY_OPEN`` -> ``CLUSTER_LIMIT`` ->
``TOTAL_RISK_CAP``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from fxbot.config.schema import RiskParams
from fxbot.core.enums import RejectReason, Side
from fxbot.core.models import Position, SizedOrder, SymbolSpec
from fxbot.risk.sizing import position_risk_amount


def usd_direction(symbol_spec: SymbolSpec, side: Side) -> int:
    """Return ``+1`` long USD, ``-1`` short USD, ``0`` for symbols with no USD leg.

    A position is long USD when the base currency is USD and the side is BUY, or when the
    profit currency is USD and the side is SELL; mirrored for short USD. Long EURUSD and
    long GBPUSD are both **short USD** -- one trade wearing two tickets, which is exactly
    what the cluster cap exists to stop.

    Args:
        symbol_spec: The symbol specification.
        side: The position side.

    Returns:
        ``+1``, ``-1`` or ``0``.
    """
    if symbol_spec.currency_base == "USD":
        return 1 if side is Side.BUY else -1
    if symbol_spec.currency_profit == "USD":
        return -1 if side is Side.BUY else 1
    return 0


def cluster_of(symbol: str, clusters: Mapping[str, Sequence[str]]) -> str | None:
    """Return the configured cluster ``symbol`` belongs to, or None."""
    for name, members in clusters.items():
        if symbol in members:
            return name
    return None


def check_exposure(
    candidate: SizedOrder,
    open_positions: Sequence[Position],
    specs: Mapping[str, SymbolSpec],
    equity: float,
    p: RiskParams,
    commission_per_lot_round_turn: float = 0.0,
) -> RejectReason:
    """Return the first exposure rule ``candidate`` breaks, or ``NONE``.

    Args:
        candidate: The order the governor is about to approve.
        open_positions: Every position the bot currently holds.
        specs: ``symbol -> SymbolSpec``; needed for tick values and currencies.
        equity: Current account equity.
        p: Risk parameters.
        commission_per_lot_round_turn: Round-turn commission per lot. Defaults to 0.0 only
            so the signature §8.4 fixes stays callable as written; the governor always
            passes the real number, because the commission term in ``TOTAL_RISK_CAP`` is
            **not optional** -- omitting it makes this layer and ``sizing.py`` disagree
            about what "0.5% risk" means.

    Returns:
        The reject reason, or :attr:`~fxbot.core.enums.RejectReason.NONE` if all pass.
    """
    if len(open_positions) >= p.max_open_positions:
        return RejectReason.MAX_POSITIONS

    # Looks unreachable, since §10.2 step 9 only iterates symbols without a position. It
    # is defence in depth against a stale positions list after a partial reconciliation,
    # and it is cheap. Tested directly rather than deleted (§8.4).
    same_symbol = [pos for pos in open_positions if pos.symbol == candidate.symbol]
    if len(same_symbol) >= p.max_positions_per_symbol:
        return RejectReason.SYMBOL_ALREADY_OPEN

    candidate_spec = specs.get(candidate.symbol)
    if candidate_spec is None:
        # An unknown symbol spec is uncertainty, and uncertainty halts entries (§0.7).
        return RejectReason.TOTAL_RISK_CAP

    cluster = cluster_of(candidate.symbol, p.clusters)
    if cluster is not None:
        candidate_dir = usd_direction(candidate_spec, candidate.side)
        if candidate_dir != 0:
            same_direction = 0
            for pos in open_positions:
                spec = specs.get(pos.symbol)
                if spec is None:
                    continue
                if cluster_of(pos.symbol, p.clusters) != cluster:
                    continue
                if usd_direction(spec, pos.side) == candidate_dir:
                    same_direction += 1
            if same_direction >= p.max_positions_per_cluster:
                return RejectReason.CLUSTER_LIMIT

    if equity <= 0.0:
        return RejectReason.TOTAL_RISK_CAP

    open_risk = 0.0
    for pos in open_positions:
        spec = specs.get(pos.symbol)
        if spec is None:
            return RejectReason.TOTAL_RISK_CAP
        open_risk += position_risk_amount(
            pos.volume, pos.entry_price, pos.stop_loss, spec, commission_per_lot_round_turn
        )
    total_pct = 100.0 * (open_risk + candidate.risk_amount) / equity
    if total_pct > p.total_open_risk_pct:
        return RejectReason.TOTAL_RISK_CAP

    return RejectReason.NONE
