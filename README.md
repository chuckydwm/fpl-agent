# fpl-agent

An open-source Fantasy Premier League analyst. It projects expected points, solves
for the best transfers over a multi-gameweek horizon, and tells you who to captain
and how to order your bench.

**It recommends. You execute.** There is no authenticated code path in this
repository — no login, no session cookie, no write endpoint. Every request it makes
is to a public API, so it cannot change your team even by accident.

---

## Why another FPL optimiser

Most open-source projections compute `per-90 rate × assumed minutes` and treat
minutes as a constant. Rotation is then the largest single source of error: a 6.0 xP
projection on a player who starts on the bench isn't slightly wrong, it's entirely
wrong.

Here, minutes are a **probability**, modelled in
[`models/minutes.py`](src/fpl_agent/models/minutes.py) and multiplied through every
term of the projection. That module also exposes an override hook, which is the
designed seam for press-conference and community signal:

```python
minutes_model.add_override(427, 0.15, "Amorim confirmed he is rested for the cup")
```

Scrapers produce *minutes priors*. They never touch expected points directly.

## Install

Requires Python 3.11+.

```bash
git clone https://github.com/chuckydwm/fpl-agent
cd fpl-agent
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env      # then set FPL_ENTRY_ID and FPL_TIMEZONE
```

Your entry ID is the number in your team URL:
`https://fantasy.premierleague.com/entry/<THIS>/event/1`

## Use

```bash
fpl deadline              # next deadline, in your timezone, with time remaining
fpl squad                 # your current picks, including bench order
fpl plan --horizon 3      # recommended transfers, XI, captain, bench order
fpl backtest --start 1    # replay past gameweeks, score against baselines
fpl now                   # current time, UTC and local
```

`fpl plan` prints a gameweek-by-gameweek plan with expected points and any hits
it thinks are worth taking. Nothing is applied — copy the moves into the FPL site
yourself before the deadline.

## Architecture

```
src/fpl_agent/
├── api/          public FPL client — retries, disk cache, stale fallback
├── models/       fixtures · minutes · expected points
├── optimiser/    PuLP integer program over a rolling horizon
├── agent/        report rendering
└── cli.py        typer entrypoint
```

### The optimiser

Maximise over horizon `H`:

```
Σ_g  decay^g · [ Σ_p  xP[p,g] · (start[p,g] + captain[p,g])  −  4 · hits[g] ]
```

subject to: squad size 15 · position quotas 2/5/5/3 · budget · ≤3 per club ·
legal formation (1 GK, 3–5 DEF, 2–5 MID, 1–3 FWD) · one captain from the XI ·
transfer continuity `squad[p,g] − squad[p,g−1] = buy − sell` · free-transfer
rollover capped at 5.

`decay` (default 0.85) discounts later gameweeks so the plan doesn't sacrifice a
good current gameweek for a speculative future one.

### Bench ordering

FPL substitutes in slot order, so sub 1 should be your likeliest returner. The
optimiser sorts the bench by expected points behind the reserve keeper. This is
the cheapest points you will ever recover, and it locks at the deadline along with
everything else.

## Backtesting

```bash
fpl backtest --start 1 --end 10 --limit 300
python examples/demo_backtest.py     # synthetic season, runs without live data
```

### Lookahead bias is the whole problem

`bootstrap-static` reports season-to-date totals **as of now**. Feed those into a
projection for gameweek 5 and you are predicting gameweek 5 with knowledge of
gameweek 38 — the backtest scores brilliantly and means nothing.

So the replay never touches it. `/element-summary/{id}/` returns a per-gameweek
row per player including *the price they were that week*; summing rounds `1..g-1`
reconstructs what was genuinely knowable before gameweek `g`. Today's injury flags
and `ep_next` are explicitly stripped, because they cannot be reconstructed
historically and would leak the present into the past. Five tests in
`tests/test_backtest.py::TestPointInTimeSnapshots` exist purely to keep that true.

### What it measures

Total points hides the interesting failures, so they are separated: hits taken and
what they cost, points stranded on the bench, auto-substitutions triggered, and
captain efficiency — the share of the achievable armband haul captured, measured
against *the best pick from the XI you actually fielded*, not the highest scorer in
the game.

Auto-subs are simulated properly: bench order is respected, a keeper can only be
replaced by a keeper, and a substitution that would break formation legality is
refused. This is why bench ordering is worth getting right.

### Current honest status

The 2026/27 season has no settled gameweeks yet, so **the backtest cannot run on
live data** — `fpl backtest` says so and exits cleanly. Against the synthetic season
in `examples/`:

| Strategy | Pts | Per GW |
|---|---:|---:|
| template | 639 | 53.2 |
| optimiser | 381 | 31.8 |
| hold | 263 | 21.9 |

The optimiser clearly finds signal — it beats doing nothing by **+9.9/GW**, and
that is asserted as a regression test. It loses to the template, but that result is
an artifact of the fixture: synthetic ownership is a far cleaner read on player
quality than a dozen noisy gameweeks, so `template` is close to an oracle there.
It is reported rather than tuned away, because tuning a benchmark until your model
wins is the exact failure a backtest exists to prevent.

**Do not read the template row as a verdict on the live game.** A real answer needs
real settled gameweeks.

## Known gaps

These are real and worth knowing before you trust the tool with a −4:

| Gap | Impact | Where |
|---|---|---|
| Double gameweeks collapse to the first fixture | DGW teams undervalued | `models/fixtures.py` |
| Sell-on tax ignored (sells at purchase price) | Overstates available funds | `optimiser/squad.py` |
| Chips not modelled | Wildcard expressible via a high `free_transfers` | `optimiser/squad.py` |
| No in-horizon price-change forecast | Minor budget drift | — |
| Early-season rates are noisy | Mitigated by shrinkage, not solved | `models/expected_points.py` |

## Design rules

1. **No credentials, ever.** The read path is entirely public. If a feature needs
   auth, it does not belong here.
2. **Never infer the current time from a rendered clock.** Use `datetime.now(timezone.utc)`.
   A skewed browser clock once produced a confidently wrong "4 hours to the deadline"
   four hours *after* it passed. `fpl now` exists to make the real time cheap to check.
3. **Timezones are not cosmetic.** A Friday 18:30 UK deadline is Saturday 01:30 in
   UTC+8. Group fixtures by the *manager's* local date.
4. **Provisional ≠ final.** `finished` flips before `data_checked`. Only report
   points once `Event.is_settled` is true.
5. **Sources over vibes.** Where two sources disagree, surface both.

## Social signals (optional)

```bash
pip install -e ".[social]"
```

Reddit uses the **official API** via PRAW — register a script app at
[reddit.com/prefs/apps](https://www.reddit.com/prefs/apps). Read-only access needs
no password.

X/Twitter is deliberately **not** included: scraping it violates their terms and the
endpoints are actively defended. If you want that signal, budget for the paid API
tier and add it as a `minutes` override provider.

## Testing

```bash
pytest -q          # unit tests against a synthetic 6-club league
ruff check src tests
mypy src
```

The test fixtures build a miniature but legal FPL universe so the club-limit and
formation constraints actually bind.

## Licence

MIT.
