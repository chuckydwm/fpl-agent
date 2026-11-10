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
fpl plan --horizon 3      # recommended transfers, XI, captain, vice, bench order
fpl plan --snapshot squad.json   # plan from a squad you confirmed yourself
fpl backtest --start 1    # replay past gameweeks, score against baselines
fpl predictions evaluate --event 11   # how the dated predictions scored
fpl now                   # current time, UTC and local
```

`fpl plan` prints, before the plan itself:

- where the data came from and how old it is (bold red if a failed request fell
  back to an old copy; the CLI accepts copies up to a day old, labelled);
- the **squad state** it plans from: source gameweek, time, any later transfers
  it counted, free transfers left, and the fields it cannot be sure of;
- the policy that flagged players are not bought;
- the plan **against holding the same squad** over the same horizon, after hits,
  with the move to make now kept apart from later, contingent ones. A gain under
  2 points is labelled marginal.

Nothing is applied — copy the moves into the FPL site yourself before the deadline.

## Architecture

```
src/fpl_agent/
├── api/          public FPL client — retries, disk cache, recorded freshness
├── models/       fixtures · minutes · expected points
├── optimiser/    PuLP integer program over a rolling horizon, independently validated
├── agent/        squad state · planner (with hold comparison) · predictions · report
├── backtest/     point-in-time history, replay, strategies, metrics
├── ops/          launchd jobs: briefs, recaps, news, watchdog
├── pricing.py    money in integer tenths; FPL's selling-price rule
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
rollover capped at 5. A tie-break of 0.01 per transfer keeps a move worth
nothing (a bench swap) from being recommended.

`decay` (default 0.85) discounts later gameweeks so the plan doesn't sacrifice a
good current gameweek for a speculative future one.

**Money** is integer tenths of a million throughout. Each owned player sells at
FPL's selling price (purchase price plus half any rise, rounded down; a fall in
full); a player bought inside the horizon sells back at what was paid, so
selling, rebuying and selling again is priced correctly.

**The answer is checked, not trusted.** Only an optimal result, or a
time-limited run's integer-feasible incumbent, is accepted; the latter is
labelled "feasible, not proven optimal". Every value must be within 1e-6 of an
integer (nothing is rounded), and `validate_plan` recomputes every rule from the
extracted squads — quotas, clubs, formation, captain and vice, banned and locked
players, continuity, transfer limits, free transfers, hits and the bank each
week. A breach raises `SolverError` and no plan is returned.

The **vice-captain** is the best projected starter other than the captain.

### The squad state

The public API gives the picks, bank and free transfers of the last *locked*
gameweek, plus a transfer list that can run past it. The planner never mixes
the two silently: `agent/squad_state.py` starts from the locked picks and bank,
applies later public transfers in time order (squad, bank, free transfers,
hits already taken), leaves Free Hit transfers out (that squad reverts) and
makes a Wildcard week unlimited. Selling prices come from the latest
non-Free-Hit purchase or the first-squad price; any it cannot find are named as
estimates, and affordability is then reported as an estimate too.

FPL may hide transfers and chips for the coming gameweek until its deadline, so
those are listed as uncertain. A **squad snapshot** you confirm yourself
(`FPL_SQUAD_SNAPSHOT`, default `<config dir>/fpl-agent/squad.json`, or
`fpl plan --snapshot`) settles that: when it is for the coming gameweek and newer
than the last deadline it replaces the public state entirely; when it is
malformed the plan stops with the reason. The format is in the module docstring.

### Minutes

Appearing (1 point), reaching 60 minutes (2 points and any clean sheet) and
starting are separate probabilities, taken from the player's last six matches
with the most recent weighted highest, and his own minutes when he starts and
when he comes on. With fewer than three matches the season-average fallback is
used and named. An injury flag scales his usual role for the coming gameweek
only; later gameweeks assume he is back (except players marked unavailable),
and the projection says so. News text never becomes a number automatically.

Doubles are projected per fixture and summed; a blank is zero. FPL's own
`ep_next` is a whole-gameweek figure, so it is blended once per gameweek,
unadjusted, while early-season data is thin.

### Freshness

Each fetch records whether it came from the network, a fresh cache entry, or an
old copy served after a failure. Deadline briefs, the model line in them,
settlement decisions and prediction scoring use live data only; a failed fetch
is a failed run, never "no flags" or "nothing new". The CLI may use copies up to
a day old and says so in bold.

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

Money is integer tenths, and each holding remembers its purchase price, so a
sale pays FPL's selling price on the reconstructed deadline prices. The
match-level minutes a strategy sees stop before the round it decides
(`HistoryStore.recent_appearances`). The armband follows FPL's rule: the vice
is doubled only if the captain plays no minutes across the whole gameweek.

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
12 gameweeks, seed 20262027), recomputed 10 Nov 2026 after the audit fixes on
this branch (before → after):

| Strategy | Pts | Per GW |
|---|---:|---:|
| optimiser | 381 → 431 | 35.9 |
| template † | 395 → 422 | 35.2 |
| hold | 263 → 378 | 31.5 |

Every strategy moved, not only the optimiser: the vice now takes the armband
when the captain does not play, minutes come from recent matches, and the
opening build changed. Nothing was tuned to make the optimiser win.

The unit test that asserts the optimiser beats holding on a smaller synthetic
league (`TestSignalDetection`) is not reliable: in GW1 every candidate squad
projects the same, so the opening build is the solver's arbitrary pick among
ties, and the result follows it. Across eight seeds the margin ranged from
−48 to +31 points. Treat "the optimiser finds signal" as unproven.

An earlier version of the synthetic table showed the template on **639**. That
number was an accounting bug: the template's transfers were discarded, so it
never paid for its squad. With the squad paid for, it scored 395.

Live 2026/27, GW1–8 (`fpl backtest --start 1 --end 8`, run 10 Nov 2026 after the
audit fixes; the earlier 10 Nov figures in brackets):

| Strategy | Most-owned 150 (`--limit 150`) | All players |
|---|---:|---:|
| template † | 625 (620) | 616 (616) |
| optimiser | 346 (390) | 280 (282) |
| hold | 372 (420) | 204 (186) |
| manager average | 461 | 461 |

No strategy a manager could actually have followed beats the average here, and
the fixes did not change that. Read these as a snapshot, not a stable benchmark:

- **The pool moves the result.** `--limit` chooses players by today's ownership, so
  it hands every strategy hindsight, not only the template, and the numbers shift
  as ownership changes from one day to the next.
- **The opening build has almost no information.** The model has no prior-season
  data, so the GW1 squad is close to a blind pick, and "hold" is that pick kept.
  Real managers start with far more knowledge.

Whether the projections are any good is measured, not asserted: the deadline job
records each gameweek's predictions, and `fpl predictions evaluate --event N`
scores the last pre-deadline capture against what happened (points and minutes
error, Brier scores and calibration for starting and for 60 minutes).

## Known gaps

These are real and worth knowing before you trust the tool with a −4:

| Gap | Impact | Where |
|---|---|---|
| Backtest club, ownership, team strengths and fixture rounds are today's values | Replays are approximate; template is a hindsight benchmark | `backtest/history.py` |
| Chips are not planned: an active Wildcard or Free Hit is handled, choosing when to play one is not | No chip advice | `agent/squad_state.py` |
| FPL may hide coming-week transfers and chips before the deadline | A public state can miss them; use a snapshot | `agent/squad_state.py` |
| Selling prices for players with no purchase record are estimated | Affordability then an estimate (stated) | `agent/squad_state.py` |
| No in-horizon price-change forecast | Minor budget drift | — |
| Bench points are not in the objective; the vice is chosen after solving | Bench and vice are ordered, not optimised for autosub value | `optimiser/squad.py` |
| Both games of a double use the same minutes outlook | Rotation between the two games is not modelled | `models/expected_points.py` |
| Flags apply to the coming week only; later weeks assume a return | A long injury looks short beyond the coming week | `models/minutes.py` |
| The 2-point "marginal" threshold is a floor, not calibrated | Use `fpl predictions evaluate` before trusting it | `agent/planner.py` |
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

## News sources

The deadline brief and the recap quote news that names your players, read from
public Telegram channels (`FPL_TELEGRAM_CHANNELS`, via the `t.me/s/` web preview)
and RSS feeds (`FPL_NEWS_FEEDS`, Fantasy Football Scout and BBC Sport by default).
No logins. Every line carries its source, because a shared surname can match the
wrong player. X/Twitter is deliberately not read: scraping it violates their terms.

## Operations

The launchd jobs in `launchd/` keep their state under the user state directory
(`~/Library/Application Support/fpl-agent/` on macOS):

| File | Written by | What it holds |
|---|---|---|
| `settlewatch.json` | settlement watcher | `last_handled` and, per pending gameweek, `synced` / `delivered` / `archived`, attempts and last error. A file with only `last_handled` (the old format) is read as done up to it |
| `predictions.jsonl` | deadline job, `fpl predictions capture` | dated projections, one line per player per capture |
| `news.jsonl` | news watcher | posts from the channels and feeds |
| `telegram.jsonl` | every alert | what was sent, when, and whether it succeeded; used to avoid sending twice |

Settled gameweeks are handled oldest first; an undelivered recap stays pending
and holds newer ones back, and the LMS sync and the LMS copy are retried
separately. State is written atomically and each job takes a lock, so an
overlapping run does nothing. **Delivery is at least once, not exactly once**:
a timeout after Telegram accepted a message looks like a failure, and the retry
can deliver it twice.

The LMS side (`/api/sports/fpl/sync`, `/desk`, `/api/sports/notes`) is called
over HTTP and is not exercised by this repo's tests, which mock it.

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
