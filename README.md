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

### Lookahead bias, and what this replay can and cannot claim

`bootstrap-static` reports season-to-date totals **as of now**. Feed those into a
projection for gameweek 5 and you are predicting gameweek 5 with knowledge of
gameweek 38 — the backtest scores brilliantly and means nothing.

So player statistics never come from it. `/element-summary/{id}/` returns one row
per player per *fixture*, including the price that week; summing the rows from
rounds `1..g-1` rebuilds each player's record as it stood before gameweek `g`.
A double gameweek's two fixtures are added together (points, minutes, goals,
assists, clean sheets, bonus, BPS, defensive contributions); price is a snapshot,
never summed. Today's injury flags and `ep_next` are stripped.

**That is a reconstruction, not a lookahead-free test.** The public API keeps no
dated copy of several inputs, so these are today's values:

- club, position and name (a mid-season transfer is backdated)
- ownership — which makes the `template` benchmark a hindsight benchmark
- team strength ratings
- which gameweek each fixture sits in (postponed games appear in their new round)
- the price of a player with no history row at or before a round

A transfer costs the round's first-fixture price, which matched all eight of a
real entry's transfers this season. It is public before the deadline, so reading
it is not lookahead, though a change between deadline and kickoff would be included.
No double gameweek has occurred yet in 2026/27, so double-gameweek handling is
covered by synthetic tests only.

Every report prints this list, and marks any strategy that reads one of these
inputs with `†`. A replay backed by dated pre-deadline snapshots would need a
snapshot source this project does not have.

### Transfer accounting

The replay, not the strategy, does the bookkeeping. Transfers are the change in
squad (a strategy whose reported transfers disagree with its squad change is
rejected), hits follow FPL's rule, and bank and free transfers move with them.
The opening squad is a free build — no hits, no free transfers used — and the
following gameweek starts with one free transfer, whichever gameweek the replay
started at. The history a strategy sees is counted from the earlier gameweeks in
the data, and fixture coverage spans the strategy's own planning horizon.

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

Synthetic season in `examples/` (`python examples/demo_backtest.py`, 300 players,
12 gameweeks, seed 20262027):

| Strategy | Pts | Per GW |
|---|---:|---:|
| template † | 395 | 32.9 |
| optimiser | 381 | 31.8 |
| hold | 263 | 21.9 |

The optimiser finds signal — it beats doing nothing by **+9.8/GW**, and that is
asserted as a regression test. The template edges it by 14 points (+1.2/GW), a
margin too small to read much into over twelve gameweeks.

An earlier version of this table showed the template on **639**. That number was
an accounting bug: the template's transfers were discarded, so it never paid for
its squad, kept a £100.0m bank every week and fielded a £138.5m squad. With the
squad paid for, it scores 395. Neither result is tuned.

Live 2026/27, GW1–8 (`fpl backtest --start 1 --end 8`, run 31 Oct 2026):

| Strategy | Most-owned 150 (`--limit 150`) | All players |
|---|---:|---:|
| template † | 627 | 607 |
| optimiser | 437 | 256 |
| hold | 422 | 99 |
| manager average | 461 | 461 |

No strategy a manager could actually have followed beats the average here. Read
these as a snapshot, not a stable benchmark:

- **The pool moves the result.** `--limit` chooses players by today's ownership, so
  it hands every strategy hindsight, not only the template, and the numbers shift
  as ownership changes from one day to the next.
- **The opening build has almost no information.** The model has no prior-season
  data, so the GW1 squad is close to a blind pick. Across all players that build
  scores 99 if it is never changed. Real managers start with far more knowledge.

## Known gaps

These are real and worth knowing before you trust the tool with a −4:

| Gap | Impact | Where |
|---|---|---|
| Double gameweeks collapse to the first fixture in projections (backtest scoring sums both) | DGW teams undervalued | `models/fixtures.py` |
| Backtest club, ownership, team strengths and fixture rounds are today's values | Replays are approximate; template is a hindsight benchmark | `backtest/history.py` |
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
