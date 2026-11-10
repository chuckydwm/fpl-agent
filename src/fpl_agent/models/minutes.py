"""Start-probability modelling.

This is the module that decides whether the whole agent is any good.

Most open-source FPL projections compute ``per-90 rate x assumed minutes`` and
treat minutes as a constant. Rotation is then the single largest source of
error: a 6.0 xP projection on a player who starts on the bench is not slightly
wrong, it is entirely wrong. So minutes are modelled as explicit probabilities
and multiplied through the projection rather than assumed.

Three probabilities are kept apart, because FPL pays them differently:
appearing (1 point), reaching 60 minutes (2 points and any clean sheet) and
starting (which drives the minutes behind attacking returns). Each comes from
the player's recent matches, most recent weighted highest, with his typical
minutes when he starts and when he comes on. A player losing his place (the
Cherki case: 84, 90, 0, 45, 45, 35) is caught by the recent weighting rather
than averaged away.

**Sparse history.** With fewer than :data:`MIN_MATCHES` team matches, or none
supplied, the season-average fallback applies: start chance from average
minutes per settled gameweek, 60-minute chance at :data:`SIXTY_GIVEN_START`
of it, and fixed minutes per start and per cameo. With some history, the
recent rates are shrunk toward that fallback by :data:`PRIOR_MATCHES`.

Overrides (``add_override``) win over everything. Nothing turns news text into
an override automatically: unverified snippets are shown, never scored.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from fpl_agent.api.models import Element

#: Roughly the number of minutes that earns the 2-point appearance bonus.
FULL_APPEARANCE = 60
#: Team matches needed before recent form is used at all.
MIN_MATCHES = 3
#: Recent matches considered.
WINDOW = 6
#: Weight of the newest match relative to the one before it.
RECENCY = 0.75
#: Pseudo-matches of the season-average fallback mixed into recent rates.
PRIOR_MATCHES = 2.0
#: Fallback: share of starts that reach 60 minutes.
SIXTY_GIVEN_START = 0.85
#: Fallback minutes for a start and for a cameo.
START_MINUTES = 82.0
CAMEO_MINUTES = 18.0


@dataclass(frozen=True, slots=True)
class Appearance:
    """One team match from the player's point of view (0 minutes if unused)."""

    event: int
    minutes: int
    started: bool


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
    minutes_if_start: float = START_MINUTES
    minutes_if_cameo: float = CAMEO_MINUTES

    def __post_init__(self) -> None:
        if self.p_60 < 0:
            object.__setattr__(self, "p_60", self.p_start)

    @property
    def expected_minutes(self) -> float:
        cameo = max(0.0, self.p_appear - self.p_start)
        return self.p_start * self.minutes_if_start + cameo * self.minutes_if_cameo

    def scaled(self, chance: float, reason: str) -> StartProbability:
        """The same outlook, times an availability chance."""
        return StartProbability(
            self.element_id,
            _clamp(self.p_start * chance),
            _clamp(self.p_appear * chance),
            reason,
            p_60=_clamp(self.p_60 * chance),
            minutes_if_start=self.minutes_if_start,
            minutes_if_cameo=self.minutes_if_cameo,
        )


@dataclass
class MinutesModel:
    """Minutes outlook from recent matches, with a season-average fallback.

    Parameters
    ----------
    gameweeks_played:
        Used to convert season-total minutes into a per-gameweek average. Pass
        the number of *settled* gameweeks, not the current gameweek id.
    recent:
        ``element_id -> appearances``, oldest first, holding only matches
        played before the gameweek being projected. A backtest must build
        this from history strictly before the round (see
        ``HistoryStore.recent_appearances``) or it leaks the future.
    overrides:
        ``element_id -> (p_start, reason)``. Anything placed here wins over the
        statistical baseline. This is the seam for confirmed team news; it is
        never filled automatically from unverified snippets.
    """

    gameweeks_played: int
    recent: dict[int, Sequence[Appearance]] = field(default_factory=dict)
    overrides: dict[int, tuple[float, str]] = field(default_factory=dict)

    def probability(self, element: Element, *, next_round: bool = True) -> StartProbability:
        """``next_round`` is True for the coming gameweek, the only one the
        game's availability flags describe. For later gameweeks a flagged
        player is assumed back to his usual minutes, unless marked
        unavailable ('u'); the reason says so."""
        if element.id in self.overrides:
            p_start, reason = self.overrides[element.id]
            p_start = _clamp(p_start)
            return StartProbability(
                element.id,
                p_start,
                _clamp(p_start + 0.08),
                reason,
                p_60=p_start * SIXTY_GIVEN_START,
            )

        if not next_round:
            if element.status == "u":
                return StartProbability(element.id, 0.0, 0.0, "unavailable")
            base = self._baseline(element)
            if element.status != "a" or element.chance_of_playing_next_round not in (None, 100):
                return base.scaled(1.0, f"{base.reason}; assumed back after the coming gameweek")
            return base

        # Hard availability signals from the API come first.
        if element.status in {"i", "s", "u"}:
            label = {"i": "injured", "s": "suspended", "u": "unavailable"}[element.status]
            return StartProbability(element.id, 0.0, 0.0, label)

        base = self._baseline(element)
        # FPL leaves 100 behind when a flag is cleared; that is a fit player,
        # not a flagged one (188 players carried it on 2026-11-10, Haaland too).
        if element.chance_of_playing_next_round not in (None, 100):
            chance = element.chance_of_playing_next_round / 100.0
            # "Chance of playing" is availability; his usual role still applies.
            return base.scaled(
                chance,
                f"FPL flags {element.chance_of_playing_next_round}% chance of playing; "
                f"{base.reason}",
            )
        return base

    def _baseline(self, element: Element) -> StartProbability:
        """Minutes outlook from history alone, ignoring availability flags."""
        fallback = self._season_average(element)
        matches = list(self.recent.get(element.id, ()))[-WINDOW:]
        if len(matches) < MIN_MATCHES:
            return fallback
        return _from_matches(element.id, matches, fallback)

    def _season_average(self, element: Element) -> StartProbability:
        if self.gameweeks_played <= 0:
            # Preseason: no minutes history exists yet, so refuse to pretend.
            return StartProbability(
                element.id,
                0.5,
                0.6,
                "no minutes history yet (preseason)",
                p_60=0.5 * SIXTY_GIVEN_START,
            )
        avg = element.minutes / self.gameweeks_played
        p_start = _clamp((avg - 20.0) / 55.0)
        p_appear = max(_clamp((avg - 5.0) / 55.0), p_start)
        return StartProbability(
            element.id,
            p_start,
            p_appear,
            f"season average {avg:.0f} min/GW over {self.gameweeks_played} GW",
            p_60=p_start * SIXTY_GIVEN_START,
        )

    def add_override(self, element_id: int, p_start: float, reason: str) -> None:
        self.overrides[element_id] = (_clamp(p_start), reason)


def _from_matches(
    element_id: int, matches: list[Appearance], prior: StartProbability
) -> StartProbability:
    """Recency-weighted rates over the last matches, shrunk toward ``prior``."""
    weights = [RECENCY ** (len(matches) - 1 - i) for i in range(len(matches))]
    total = sum(weights)

    def rate(hit: list[bool], prior_rate: float) -> float:
        observed = sum(w for w, h in zip(weights, hit, strict=True) if h)
        return (observed + PRIOR_MATCHES * prior_rate) / (total + PRIOR_MATCHES)

    started = [m.started for m in matches]
    p_start = rate(started, prior.p_start)
    p_appear = max(rate([m.minutes > 0 for m in matches], prior.p_appear), p_start)
    p_60 = min(rate([m.minutes >= FULL_APPEARANCE for m in matches], prior.p_60), p_appear)

    start_minutes = [m.minutes for m in matches if m.started]
    cameo_minutes = [m.minutes for m in matches if not m.started and m.minutes > 0]
    minutes_if_start = (
        sum(start_minutes) / len(start_minutes) if start_minutes else prior.minutes_if_start
    )
    minutes_if_cameo = (
        sum(cameo_minutes) / len(cameo_minutes) if cameo_minutes else prior.minutes_if_cameo
    )
    shown = "/".join(str(m.minutes) for m in matches[-3:])
    return StartProbability(
        element_id,
        _clamp(p_start),
        _clamp(p_appear),
        f"last {len(matches)} matches (latest {shown}'), {sum(started)} starts",
        p_60=_clamp(p_60),
        minutes_if_start=minutes_if_start,
        minutes_if_cameo=minutes_if_cameo,
    )


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))
