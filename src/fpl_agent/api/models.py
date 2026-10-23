"""Typed views over the FPL JSON API.

The upstream API is untyped, undocumented and changes between seasons, so every
model here sets ``extra="ignore"``: unknown fields are dropped rather than
raising. Fields the agent genuinely depends on are required; everything else is
optional with a sensible default, so a mid-season shape change degrades a
feature instead of crashing the run.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Position(StrEnum):
    GKP = "GKP"
    DEF = "DEF"
    MID = "MID"
    FWD = "FWD"


#: FPL squad composition: exactly this many of each position in the 15.
SQUAD_QUOTA: dict[Position, int] = {
    Position.GKP: 2,
    Position.DEF: 5,
    Position.MID: 5,
    Position.FWD: 3,
}

#: Legal starting-XI bounds per position (1 GK, 3-5 DEF, 2-5 MID, 1-3 FWD).
FORMATION_BOUNDS: dict[Position, tuple[int, int]] = {
    Position.GKP: (1, 1),
    Position.DEF: (3, 5),
    Position.MID: (2, 5),
    Position.FWD: (1, 3),
}

MAX_PER_CLUB = 3
SQUAD_SIZE = 15
XI_SIZE = 11
MAX_FREE_TRANSFERS = 5
HIT_COST = 4

_ELEMENT_TYPE_TO_POSITION = {1: Position.GKP, 2: Position.DEF, 3: Position.MID, 4: Position.FWD}


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class Team(_Base):
    id: int
    name: str
    short_name: str
    strength_attack_home: int = 1000
    strength_attack_away: int = 1000
    strength_defence_home: int = 1000
    strength_defence_away: int = 1000


class Element(_Base):
    """A player. ``now_cost`` is in tenths of a million (e.g. 155 -> 15.5)."""

    id: int
    web_name: str
    team: int
    element_type: int
    now_cost: int

    # Availability. ``status`` is one of a/d/i/s/u (available, doubtful,
    # injured, suspended, unavailable).
    status: str = "a"
    chance_of_playing_next_round: int | None = None
    # The flag text as shown in the game ("Knee injury - 75% chance of
    # playing"). Empty when unflagged. The deadline reminder quotes it.
    news: str = ""

    # Season-to-date form and returns.
    minutes: int = 0
    total_points: int = 0
    event_points: int = 0
    goals_scored: int = 0
    assists: int = 0
    clean_sheets: int = 0
    bonus: int = 0
    bps: int = 0
    saves: int = 0
    defensive_contribution: int = 0

    form: str = "0.0"
    points_per_game: str = "0.0"
    selected_by_percent: str = "0.0"
    ep_next: str | None = None

    @property
    def position(self) -> Position:
        return _ELEMENT_TYPE_TO_POSITION[self.element_type]

    @property
    def price(self) -> float:
        """Price in millions."""
        return self.now_cost / 10.0

    @property
    def ownership(self) -> float:
        try:
            return float(self.selected_by_percent)
        except ValueError:
            return 0.0

    @property
    def form_value(self) -> float:
        try:
            return float(self.form)
        except ValueError:
            return 0.0

    @property
    def points_per_game_value(self) -> float:
        try:
            return float(self.points_per_game)
        except ValueError:
            return 0.0

    @property
    def is_available(self) -> bool:
        """False for injured, suspended or otherwise ruled-out players."""
        return self.status == "a"


class Event(_Base):
    """A gameweek."""

    id: int
    name: str
    deadline_time: datetime
    finished: bool = False
    data_checked: bool = False
    is_previous: bool = False
    is_current: bool = False
    is_next: bool = False
    average_entry_score: int = 0
    most_captained: int | None = None

    @property
    def is_settled(self) -> bool:
        """True only once FPL has finalised bonus and stats for the gameweek.

        ``finished`` flips before ``data_checked``; reporting points between the
        two risks presenting provisional numbers as final.
        """
        return self.finished and self.data_checked

    def seconds_until_deadline(self, now: datetime | None = None) -> float:
        now = now or datetime.now(UTC)
        return (self.deadline_time - now).total_seconds()


class Fixture(_Base):
    id: int
    event: int | None = None
    team_h: int
    team_a: int
    team_h_difficulty: int = 3
    team_a_difficulty: int = 3
    kickoff_time: datetime | None = None
    finished: bool = False

    def opponent_of(self, team_id: int) -> int | None:
        if team_id == self.team_h:
            return self.team_a
        if team_id == self.team_a:
            return self.team_h
        return None

    def difficulty_for(self, team_id: int) -> int | None:
        if team_id == self.team_h:
            return self.team_h_difficulty
        if team_id == self.team_a:
            return self.team_a_difficulty
        return None

    def is_home_for(self, team_id: int) -> bool:
        return team_id == self.team_h


class Pick(_Base):
    """One of the 15 slots in a manager's squad for a gameweek.

    ``position`` is the slot index 1-15: 1-11 are the XI and 12-15 are the
    bench *in substitution order*, which is why bench ordering is recoverable
    from this endpoint.
    """

    element: int
    position: int
    multiplier: int
    is_captain: bool = False
    is_vice_captain: bool = False

    @property
    def is_starter(self) -> bool:
        return self.position <= XI_SIZE

    @property
    def bench_order(self) -> int | None:
        """1-based substitution priority, or None for starters and the bench GK."""
        if self.is_starter:
            return None
        # Slot 12 is always the reserve keeper; 13/14/15 are outfield subs 1-3.
        return None if self.position == 12 else self.position - 12


class EntryHistoryRow(_Base):
    event: int
    points: int
    total_points: int
    rank: int | None = None
    overall_rank: int | None = None
    bank: int = 0
    value: int = 1000
    event_transfers: int = 0
    event_transfers_cost: int = 0
    points_on_bench: int = 0


class Entry(_Base):
    id: int
    name: str
    player_first_name: str = ""
    player_last_name: str = ""
    summary_overall_points: int = 0
    summary_overall_rank: int | None = None
    current_event: int | None = None
    last_deadline_bank: int = 0
    last_deadline_value: int = 1000

    @property
    def bank(self) -> float:
        return self.last_deadline_bank / 10.0

    @property
    def squad_value(self) -> float:
        return self.last_deadline_value / 10.0


class Bootstrap(_Base):
    """The ``/bootstrap-static/`` payload: the spine of everything else."""

    events: list[Event] = Field(default_factory=list)
    teams: list[Team] = Field(default_factory=list)
    elements: list[Element] = Field(default_factory=list)
    total_players: int = 0

    def element_by_id(self) -> dict[int, Element]:
        return {e.id: e for e in self.elements}

    def team_by_id(self) -> dict[int, Team]:
        return {t.id: t for t in self.teams}

    @property
    def next_event(self) -> Event | None:
        return next((e for e in self.events if e.is_next), None)

    @property
    def current_event(self) -> Event | None:
        return next((e for e in self.events if e.is_current), None)

    @property
    def last_settled_event(self) -> Event | None:
        """Most recent gameweek whose points are final."""
        settled = [e for e in self.events if e.is_settled]
        return max(settled, key=lambda e: e.id) if settled else None
