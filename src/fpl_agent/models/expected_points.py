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

from dataclasses import dataclass

from fpl_agent.api.models import Element, Position
from fpl_agent.models.fixtures import FixtureRun
from fpl_agent.models.minutes import MinutesModel

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

#: Defenders and holding midfielders can earn 2 points for defensive
#: contributions. Season-to-date rate is used as the estimator.
DEFCON_POINTS = 2


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


def project(
    element: Element,
    run: FixtureRun | None,
    minutes_model: MinutesModel,
    *,
    event: int,
    prior_weight: float = 270.0,
    weight: float = 1.0,
) -> ExpectedPoints:
    """Project one player for one gameweek.

    ``weight`` blends the in-house model against FPL's ``ep_next``: 1.0 trusts
    the model completely, 0.0 defers entirely to FPL. See :func:`model_weight`.
    """
    prob = minutes_model.probability(element)

    if run is None:
        return ExpectedPoints(element.id, event, 0.0, 0.0, "blank gameweek")
    if prob.p_appear <= 0.0:
        return ExpectedPoints(element.id, event, 0.0, 0.0, prob.reason)

    position = element.position

    # --- appearance -------------------------------------------------------
    xp = prob.p_start * 2.0 + max(0.0, prob.p_appear - prob.p_start) * 1.0

    # --- attacking returns ------------------------------------------------
    goals_p90 = _per_90(
        element.goals_scored, element.minutes, prior=0.05, prior_weight=prior_weight
    )
    assists_p90 = _per_90(element.assists, element.minutes, prior=0.05, prior_weight=prior_weight)

    attacking = (
        goals_p90 * GOAL_POINTS[position] + assists_p90 * ASSIST_POINTS
    ) * run.attack_multiplier
    xp += attacking * (prob.expected_minutes / 90.0)

    # --- clean sheets -----------------------------------------------------
    cs_points = CLEAN_SHEET_POINTS[position]
    if cs_points:
        cs_rate = min(0.75, BASE_CLEAN_SHEET_RATE * run.defence_multiplier)
        # A clean sheet only pays out if the player completes 60 minutes.
        xp += cs_rate * cs_points * prob.p_start

    # --- defensive contributions -----------------------------------------
    if position in (Position.DEF, Position.MID):
        defcon_p90 = _per_90(
            element.defensive_contribution, element.minutes, prior=0.0, prior_weight=prior_weight
        )
        xp += min(defcon_p90, 1.0) * DEFCON_POINTS * prob.p_start

    # --- bonus ------------------------------------------------------------
    bonus_p90 = _per_90(element.bonus, element.minutes, prior=0.1, prior_weight=prior_weight)
    xp += bonus_p90 * prob.p_start

    # --- blend toward FPL's own projection when our sample is thin ---------
    reason = prob.reason
    if weight < 1.0:
        market = _market_expectation(element)
        if market is not None:
            # ep_next is quoted for the next gameweek only; for later events in
            # the horizon, re-scale it by that gameweek's attacking matchup.
            adjusted = market * run.attack_multiplier if run.attack_multiplier else market
            xp = weight * xp + (1.0 - weight) * adjusted
            reason = f"{reason}; blended {(1 - weight):.0%} toward FPL ep_next={market:.1f}"

    return ExpectedPoints(element.id, event, round(xp, 3), prob.p_start, reason)


def project_squad(
    elements: list[Element],
    runs: dict[tuple[int, int], FixtureRun],
    minutes_model: MinutesModel,
    events: list[int],
    *,
    prior_weight: float = 270.0,
    gameweeks_played: int | None = None,
) -> dict[tuple[int, int], ExpectedPoints]:
    """Project every player across every gameweek in ``events``.

    ``gameweeks_played`` drives the blend toward FPL's own projection. It
    defaults to the minutes model's value, which is almost always what you want.
    """
    played = minutes_model.gameweeks_played if gameweeks_played is None else gameweeks_played
    weight = model_weight(played)

    projections: dict[tuple[int, int], ExpectedPoints] = {}
    for element in elements:
        for event in events:
            run = runs.get((element.team, event))
            projections[(element.id, event)] = project(
                element,
                run,
                minutes_model,
                event=event,
                prior_weight=prior_weight,
                weight=weight,
            )
    return projections
