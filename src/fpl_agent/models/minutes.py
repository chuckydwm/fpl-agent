"""Start-probability modelling.

This is the module that decides whether the whole agent is any good.

Most open-source FPL projections compute ``per-90 rate x assumed minutes`` and
treat minutes as a constant. Rotation is then the single largest source of
error: a 6.0 xP projection on a player who starts on the bench is not slightly
wrong, it is entirely wrong. So minutes are modelled as an explicit probability
and multiplied through the projection rather than assumed.

The baseline below is deliberately simple and uses only data the public API
gives us. It is designed to be *replaced*: any richer signal (press-conference
scrapes, r/FantasyPL consensus, predicted-lineup feeds) should land here as an
override, not be bolted onto expected points downstream.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fpl_agent.api.models import Element

#: Roughly the number of minutes that earns the 2-point appearance bonus.
FULL_APPEARANCE = 60


@dataclass(frozen=True, slots=True)
class StartProbability:
    """Probability a player starts, with the reasoning kept attached."""

    element_id: int
    p_start: float
    p_appear: float
    reason: str
    #: Probability of reaching 60 minutes (the 2-point appearance and the
    #: clean sheet). Defaults to p_start for callers that give no evidence.
    p_60: float = -1.0

    def __post_init__(self) -> None:
        if self.p_60 < 0:
            object.__setattr__(self, "p_60", self.p_start)

    @property
    def expected_minutes(self) -> float:
        starter_minutes = 82.0
        cameo_minutes = 18.0
        cameo = max(0.0, self.p_appear - self.p_start)
        return self.p_start * starter_minutes + cameo * cameo_minutes


@dataclass
class MinutesModel:
    """Baseline minutes model plus a hook for external overrides.

    Parameters
    ----------
    gameweeks_played:
        Used to convert season-total minutes into a per-gameweek average. Pass
        the number of *settled* gameweeks, not the current gameweek id.
    overrides:
        ``element_id -> (p_start, reason)``. Anything placed here wins over the
        statistical baseline. This is the seam for social/press-conference
        signal: scrapers produce overrides, they never touch expected points.
    """

    gameweeks_played: int
    overrides: dict[int, tuple[float, str]] = field(default_factory=dict)

    def probability(self, element: Element, *, next_round: bool = True) -> StartProbability:
        """``next_round`` is True for the coming gameweek, the only one the
        game's availability flags describe. For later gameweeks a flagged
        player is assumed back to his usual minutes, unless marked
        unavailable ('u'); the reason says so."""
        if element.id in self.overrides:
            p_start, reason = self.overrides[element.id]
            p_start = _clamp(p_start)
            return StartProbability(element.id, p_start, _clamp(p_start + 0.08), reason)

        if not next_round:
            if element.status == "u":
                return StartProbability(element.id, 0.0, 0.0, "unavailable")
            flagged = element.status != "a" or element.chance_of_playing_next_round not in (
                None,
                100,
            )
            base = self._baseline(element)
            if flagged:
                return StartProbability(
                    element.id,
                    base.p_start,
                    base.p_appear,
                    f"{base.reason}; assumed back after the coming gameweek",
                )
            return base

        # Hard availability signals from the API come first.
        if element.status in {"i", "s", "u"}:
            label = {"i": "injured", "s": "suspended", "u": "unavailable"}[element.status]
            return StartProbability(element.id, 0.0, 0.0, label)

        # FPL leaves 100 behind when a flag is cleared; that is a fit player,
        # not a flagged one (188 players carried it on 2026-11-10, Haaland too).
        if element.chance_of_playing_next_round not in (None, 100):
            chance = element.chance_of_playing_next_round / 100.0
            # "Chance of playing" is availability, not a guarantee of starting.
            return StartProbability(
                element.id,
                _clamp(chance * 0.85),
                _clamp(chance),
                f"FPL flags {element.chance_of_playing_next_round}% chance of playing",
            )

        return self._baseline(element)

    def _baseline(self, element: Element) -> StartProbability:
        """Minutes outlook from history alone, ignoring availability flags."""
        if self.gameweeks_played <= 0:
            # Preseason: no minutes history exists yet, so refuse to pretend.
            return StartProbability(element.id, 0.5, 0.6, "no minutes history yet (preseason)")

        avg = element.minutes / self.gameweeks_played
        p_start = _clamp((avg - 20.0) / 55.0)
        p_appear = _clamp((avg - 5.0) / 55.0)
        return StartProbability(
            element.id,
            p_start,
            max(p_appear, p_start),
            f"averaging {avg:.0f} min/GW over {self.gameweeks_played} GW",
        )

    def add_override(self, element_id: int, p_start: float, reason: str) -> None:
        self.overrides[element_id] = (_clamp(p_start), reason)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))
