"""FPL money, in integer tenths of a million (``now_cost`` units).

Every financial calculation in the project goes through these units: floats
for £m only appear at the edges, for display.
"""

from __future__ import annotations


def selling_price(purchase: int, now: int) -> int:
    """What FPL pays for a player, in tenths: a fall is taken in full, a rise
    is split and the half rounded down (bought 5.4, now 5.5 -> sells at 5.4;
    bought 5.4, now 5.7 -> 5.5)."""
    return now if now <= purchase else purchase + (now - purchase) // 2


def fmt_money(tenths: int) -> str:
    """``£5.5m`` from tenths."""
    return f"£{tenths / 10:.1f}m"
