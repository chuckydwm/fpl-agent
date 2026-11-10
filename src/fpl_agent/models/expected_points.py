"""Expected points per player, per gameweek.

    xP = P(appear) * appearance_points
       + P(start)  * fixture_adjusted_attacking_return
       + P(start)  * clean_sheet_expectation
       + bonus/DefCon expectation

Every term is scaled by the minutes model rather than assuming 90 minutes. The
per-90 rates come from season-to-date returns, which is weak in the opening
gameweeks — see ``prior_weight`` for the shrinkage that keeps early-season
projections from chasing a single haul.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from fpl_agent.api.models import Element, Position
from fpl_agent.models.fixtures import FixtureRun
from fpl_agent.models.minutes import MinutesModel, StartProbability

#: FPL points per goal and per clean sheet, by position.
GOAL_POINTS: dict[Position, int] = {
    Position.GKP: 10,
    Position.DEF: 6,
    Position.MID: 5,
    Position.FWD: 4,
}
CLEAN_SHEET_POINTS: dict[Position, int] = {
    Position.GKP: 4,
    Position.DEF: 4,
    Position.MID: 1,
    Position.FWD: 0,
}
ASSIST_POINTS = 3

#: League-average clean-sheet rate, used as the base before fixture adjustment.
BASE_CLEAN_SHEET_RATE = 0.28

#: 2 points for reaching a per-match count of defensive actions: 10 for a
#: defender, 12 for a midfielder or forward. FPL's ``defensive_contribution``
#: is that count (Groß: 5.8 per 90), not the points or a probability.
DEFCON_POINTS = 2
DEFCON_THRESHOLD: dict[Position, int] = {Position.DEF: 10, Position.MID: 12, Position.FWD: 12}


def defcon_chance(per_90: float, threshold: int) -> float:
    """P(at least ``threshold`` actions in a match), Poisson with mean ``per_90``.

    shortcut: a Poisson tail on the season rate; real counts are more spread
    out, so this slightly understates both the hit and the miss weeks. Fine
    until per-match history (element-summary) feeds the model.
    """
    term = math.exp(-per_90)
    below = 0.0
    for k in range(threshold):
        below += term
        term *= per_90 / (k + 1)
    return max(0.0, 1.0 - below)


#: Gameweeks of settled data before the in-house model is trusted on its own.
#: Below this, projections are blended toward FPL's published ``ep_next``.
CONFIDENCE_GAMEWEEKS = 4


def model_weight(gameweeks_played: int) -> float:
    """How far to trust our own rates versus FPL's published projection.

    With no settled gameweeks there is no attacking evidence at all, and a
    pure-rates model degenerates: every forward projects near zero, clean-sheet
    points dominate, and the optimiser fills the XI with cheap defenders and
    captains a centre-back. Blending toward ``ep_next`` keeps early-season
    output sane and decays to zero influence once real data exists.
    """
    return min(1.0, max(0.0, gameweeks_played / CONFIDENCE_GAMEWEEKS))


@dataclass(frozen=True, slots=True)
class ExpectedPoints:
    element_id: int
    event: int
    xp: float
    p_start: float
    reason: str
    #: Fixtures in the gameweek: 0 blank, 2 double.
    fixtures: int = 1
    #: Per-fixture minutes outlook, kept for prediction capture and scoring.
    p_appear: float = 0.0
    p_60: float = 0.0
    #: Expected minutes over the whole gameweek (both games of a double).
    expected_minutes: float = 0.0

    def __float__(self) -> float:
        return self.xp


def _per_90(total: float, minutes: int, *, prior: float, prior_weight: float) -> float:
    """Shrink a per-90 rate toward ``prior`` when the sample is small.

    With 90 minutes played, one goal implies a 1.0 per-90 rate, which is
    nonsense. ``prior_weight`` is expressed in minutes: it is the number of
    minutes of "imaginary" prior-rate evidence blended in.
    """
    if minutes <= 0:
        return prior
    observed_weight = minutes / 90.0
    prior_w = prior_weight / 90.0
    return (total + prior * prior_w) / (observed_weight + prior_w)


def _market_expectation(element: Element) -> float | None:
    """FPL's own expected points for the next round, if published."""
    if not element.ep_next:
        return None
    try:
        return float(element.ep_next)
    except ValueError:
        return None


def _one_fixture(
    element: Element, run: FixtureRun, prob: StartProbability, prior_weight: float
) -> float:
    """Expected points from one fixture, given the player's minutes outlook."""
    position = element.position

    # --- appearance: 1 for playing, 2 for reaching 60 minutes --------------
    xp = prob.p_60 * 2.0 + max(0.0, prob.p_appear - prob.p_60) * 1.0

    # --- attacking returns, scaled by expected minutes ----------------------
    goals_p90 = _per_90(
        element.goals_scored, element.minutes, prior=0.05, prior_weight=prior_weight
    )
    assists_p90 = _per_90(element.assists, element.minutes, prior=0.05, prior_weight=prior_weight)
    attacking = (
        goals_p90 * GOAL_POINTS[position] + assists_p90 * ASSIST_POINTS
    ) * run.attack_multiplier
    xp += attacking * (prob.expected_minutes / 90.0)

    # --- clean sheets: only paid after 60 minutes ---------------------------
    cs_points = CLEAN_SHEET_POINTS[position]
    if cs_points:
        cs_rate = min(0.75, BASE_CLEAN_SHEET_RATE * run.defence_multiplier)
        xp += cs_rate * cs_points * prob.p_60

    # --- defensive contributions -----------------------------------------
    threshold = DEFCON_THRESHOLD.get(position)
    if threshold is not None:
        defcon_p90 = _per_90(
            element.defensive_contribution, element.minutes, prior=0.0, prior_weight=prior_weight
        )
        xp += defcon_chance(defcon_p90, threshold) * DEFCON_POINTS * prob.p_start

    # --- bonus ------------------------------------------------------------
    bonus_p90 = _per_90(element.bonus, element.minutes, prior=0.1, prior_weight=prior_weight)
    xp += bonus_p90 * prob.p_start
    return xp


def project(
    element: Element,
    runs: Sequence[FixtureRun] | None,
    minutes_model: MinutesModel,
    *,
    event: int,
    next_round: bool = True,
    market: float | None = None,
    prior_weight: float = 270.0,
    weight: float = 1.0,
) -> ExpectedPoints:
    """Project one player for one gameweek: every fixture, summed.

    ``runs`` is the team's fixtures that gameweek (two in a double, none in a
    blank). ``next_round`` says whether this is the coming gameweek, the only
    one the game's availability flags speak for. ``market`` is FPL's own
    projection for the whole gameweek (already summed over its fixtures), and
    ``weight`` blends toward it: 1.0 trusts the model, 0.0 defers to FPL.
    """
    if not runs:
        return ExpectedPoints(element.id, event, 0.0, 0.0, "blank gameweek", fixtures=0)

    prob = minutes_model.probability(element, next_round=next_round)
    if prob.p_appear <= 0.0:
        return ExpectedPoints(element.id, event, 0.0, 0.0, prob.reason, fixtures=len(runs))
    minutes_outlook = {
        "p_appear": round(prob.p_appear, 4),
        "p_60": round(prob.p_60, 4),
        "expected_minutes": round(prob.expected_minutes * len(runs), 1),
    }

    xp = sum(_one_fixture(element, run, prob, prior_weight) for run in runs)
    reason = prob.reason if len(runs) == 1 else f"{prob.reason}; {len(runs)} fixtures"

    # --- blend toward FPL's own projection when our sample is thin ---------
    if weight < 1.0 and market is not None:
        xp = weight * xp + (1.0 - weight) * market
        reason = f"{reason}; blended {(1 - weight):.0%} toward FPL's {market:.1f}"

    return ExpectedPoints(
        element.id, event, round(xp, 3), prob.p_start, reason, fixtures=len(runs), **minutes_outlook
    )


def market_for(
    element: Element, fixtures_by_event: dict[int, int], events: list[int]
) -> dict[int, float | None]:
    """FPL's ``ep_next`` mapped onto each gameweek of the horizon.

    ``ep_next`` is already the whole coming gameweek, doubles included, so the
    first gameweek takes it as is: no per-fixture split and no matchup
    adjustment (FPL priced the matchup in). A later gameweek takes the same
    per-fixture rate times its own fixture count. It is left out (None) where
    it says nothing: a blank coming gameweek, or a player flagged now, whose
    ``ep_next`` reflects an absence that later gameweeks are assumed not to
    have.
    """
    first = min(events)
    ep = _market_expectation(element)
    flagged = element.status != "a" or element.chance_of_playing_next_round not in (None, 100)
    n_next = fixtures_by_event.get(first, 0)
    out: dict[int, float | None] = {}
    for event in events:
        if event == first:
            out[event] = ep
        elif ep is None or flagged or not n_next:
            out[event] = None
        else:
            out[event] = ep * fixtures_by_event.get(event, 0) / n_next
    return out


def project_squad(
    elements: list[Element],
    runs: dict[tuple[int, int], tuple[FixtureRun, ...]],
    minutes_model: MinutesModel,
    events: list[int],
    *,
    prior_weight: float = 270.0,
    gameweeks_played: int | None = None,
) -> dict[tuple[int, int], ExpectedPoints]:
    """Project every player across every gameweek in ``events``.

    The first gameweek is the coming one: availability flags apply to it only.
    Later gameweeks assume a flagged player is back to his usual minutes,
    except one marked unavailable ('u', e.g. left the club). That is an
    assumption, stated in each projection's reason.

    ``gameweeks_played`` drives the blend toward FPL's own projection. It
    defaults to the minutes model's value, which is almost always what you want.
    """
    played = minutes_model.gameweeks_played if gameweeks_played is None else gameweeks_played
    weight = model_weight(played)
    first = min(events) if events else 0

    projections: dict[tuple[int, int], ExpectedPoints] = {}
    for element in elements:
        counts = {event: len(runs.get((element.team, event), ())) for event in events}
        market = market_for(element, counts, events) if events else {}
        for event in events:
            projections[(element.id, event)] = project(
                element,
                runs.get((element.team, event), ()),
                minutes_model,
                event=event,
                next_round=event == first,
                market=market.get(event),
                prior_weight=prior_weight,
                weight=weight,
            )
    return projections
