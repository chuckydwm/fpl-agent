# FPL advice sources (vetted list)

Research for the FPL agent, compiled 2026-11-23 (2026/27 season, Gameweek 11 finishing, midweek Gameweek 12 next). Updated the same day with a checked pass over a ChatGPT research run: only claims we re-read on the cited page were added. This is a reading list for people and for the agent's research. It does not change what the agent fetches: the news watcher still reads `FPL_TELEGRAM_CHANNELS` and `FPL_NEWS_FEEDS` (see `src/fpl_agent/ops/channelswatch.py`).

**How to read this file**

- Every figure has the page it came from and the date shown on that page. **UNVERIFIED** means we couldn't confirm it.
- Subscriber counts are third-party snapshots (vidIQ, SponsorRadar, Feedspot, us.youtubers.me), not live YouTube numbers. Feedspot's list is dated 2 July 2026, so it is about five months old.
- "Top-ranked" was judged by audience size, by published FPL results (the Fantasy Football Scout career Hall of Fame, which rates managers 2015/16 to 2025/26), and by being cited by other FPL sources. Pro Pundit bios on Fantasy Football Scout are self-reported and undated.
- Recent advice is paraphrased, not quoted. Most YouTube, The Athletic and Premier League pages were blocked for the cloud researcher (proxy 403 or "site blocked"). Where a cell says "not retrieved", read the source directly before relying on it.
- Other people's tips are claims, not instructions. The analyst still cites its own data for verdicts.

## FPL-specific sources

### Seed sources

| Source | URL | Type | Platform | Why it ranks (evidence) | Recent suggestions |
|---|---|---|---|---|---|
| Let's Talk FPL (Andy) | https://www.youtube.com/@LetsTalkFPL | FPL-specific | YouTube, podcast | 516K subscribers ([SponsorRadar](https://sponsorradar.com/channels/letstalkfpl), page updated 2026-11-20, figure undated). 494K on [Feedspot](https://videos.feedspot.com/fantasy_premier_league_youtube_channels/) (2026-07-02). Rank record: none found. | Not retrieved for 2026/27. The latest episode we could read was from 2025/26. |
| FPL Raptor | https://www.youtube.com/@FPLRaptor | FPL-specific | YouTube, podcast | 189K subscribers and 1,161 videos ([vidIQ](https://vidiq.com/youtube-stats/channel/UC54QLWzsMifTRjNQ02z5pCw), as of 2026-11-22). Rank history on [fpl.team/managers/1587](https://fpl.team/managers/1587), best 10,198 in 2022/23: **UNVERIFIED** (the page doesn't confirm it's his account). | Not retrieved. |
| FPL Mate | https://www.youtube.com/@FPLMate | FPL-specific | YouTube | 247K subscribers and 1,669 videos ([us.youtubers.me](https://us.youtubers.me/fpl-mate/youtuber-stats), latest row 2026-04-15). Same 247K on Feedspot (2026-07-02). Rank record: none found. | Not retrieved. |
| The Athletic FPL | https://www.nytimes.com/athletic/football/fantasy-premier-league/ | FPL-specific | Website (paywalled) | Publication of The Athletic (New York Times). Audience figure: **UNVERIFIED**. | Not retrieved (page blocked for the researcher). |
| The Scout (official FPL) | https://fantasy.premierleague.com/en/the-scout | FPL-specific | Website (official) | The game's own advice column. No audience figure applies. | 2026-11-18: Free Hit looks better than Bench Boost in GW12, with Haaland facing his toughest fixture of the season against Arsenal. Suggested transfers are Bruno Fernandes plus Mbeumo or Cunha ([article](https://www.premierleague.com/en/news/4764341/plan-to-use-an-fpl-chip-in-gw12-which-one-will-be-best)). |

### New sources

| Source | URL | Type | Platform | Why it ranks (evidence) | Recent suggestions |
|---|---|---|---|---|---|
| FPL Focal (Oscar) | https://www.youtube.com/channel/UC72QokPHXQ9r98ROfNZmaDw | FPL-specific | YouTube, X | 250K subscribers (Feedspot, 2026-07-02). [FFS Pro Pundit](https://www.fantasyfootballscout.co.uk/the-ffs-pro-pundits) bio: briefly world #1 in 2021/22, 3.4K finish in 2024/25 (self-reported). | Not retrieved. |
| FPL Harry | https://www.youtube.com/channel/UCcPWnCj5AKC19HaySZjb25g | FPL-specific | YouTube, X | 227K subscribers (Feedspot, 2026-07-02). Pro Pundit bio: seven top-15K finishes, five in a row in the top 10K (self-reported). | Not retrieved. |
| Fantasy Football Hub | https://www.fantasyfootballhub.co.uk | FPL-specific | Website, YouTube | 107K YouTube subscribers (Feedspot, 2026-07-02). Hosts Ben Crellin's fixture calendar. | 2026-11-19: Haaland as the safe GW11 captain against Fulham ([article](https://www.fantasyfootballhub.co.uk/articles/fpl-captain-gameweek-11-article-26-27)). |
| Fantasy Football Scout | https://www.fantasyfootballscout.co.uk | FPL-specific | Website, YouTube, Scoutcast, members | 101K YouTube subscribers (Feedspot, 2026-07-02). Runs the [career Hall of Fame](https://www.fantasyfootballscout.co.uk/fpl/hall-of-fame/career). Cited by [FPL Feed](https://fplfeed.substack.com/p/some-fpl-heavy-hitters) (2025-08-20). The agent already reads its RSS feed. | 2026-11-21: the GW12 hub warns of rotation ahead of the midweek round, with picks still marked "coming soon" ([hub](https://www.fantasyfootballscout.co.uk/fpl-gameweek-12-tips-best-players-predicted-line-ups-team-news-more-4)). 2026-11-20: GW11 Scout Picks were built on an Arsenal triple-up, with the players behind a sign-up wall ([picks](https://www.fantasyfootballscout.co.uk/2026/11/20/fpl-gameweek-11-scout-picks-arsenal-triple-up-2)). 2026-11-09: predicts blank gameweeks 30 and 33 and doubles in 32, 35 and 36, with caveats ([article](https://www.fantasyfootballscout.co.uk/2026/11/09/when-are-the-fpl-blank-and-double-gameweeks-in-2026-27)). |
| Ben Crellin | https://twitter.com/BenCrellin | FPL-specific | X, fixture sheets via the Hub | 6th in the FFS career Hall of Fame, rating 98.41 (HoF page, watchlists updated 2026-11-20). His calendar is at https://www.fantasyfootballhub.co.uk/crellin-fpl-calendar. Follower count: **UNVERIFIED**. | His 2026/27 blank and double predictions are in the Hub's [guide](https://www.fantasyfootballhub.co.uk/fpl-blank-double-gameweek-guide) (2026-06-19). No newer post found. |
| BigMan Bakar (Abubakar Siddiq) | https://twitter.com/BigManBakar | FPL-specific | X, Fantasy Football Hub | 12th in the FFS career Hall of Fame, rating 97.85. Career-best world rank of 84 (FPL Feed, 2025-08-20). | Not retrieved. |
| The FPL Wire (Lateriser, Zophar) | https://www.fplwire.com · https://www.youtube.com/channel/UCtIPFexB6PLKNNl0XH3SKKw | FPL-specific | Website, YouTube, podcast ([Apple](https://podcasts.apple.com/podcast/id1530847697)) | 42.8K subscribers (Feedspot, 2026-07-02). Zophar: eight top-10K finishes, best 17th. Lateriser: two top-200 finishes. Both per FPL Feed, 2025-08-20. | GW12: hold anyone above 20% ownership until the latest team news ([team news](https://www.fplwire.com/fpl-team-news/)). A value-picks page was published 2026-11-22 ([value picks](https://www.fplwire.com/fpl-value-picks/)). |
| FPL General (Mark McGettigan) | https://twitter.com/FPLGeneral | FPL-specific | X, podcast, YouTube | Three top-500 finishes (FPL Feed, 2025-08-20, and his Pro Pundit bio). Audience figure: **UNVERIFIED**. | Not retrieved. |
| Fantasy Football Fix | https://www.fantasyfootballfix.com | FPL-specific | Website, YouTube | 36.2K YouTube subscribers (Feedspot, 2026-07-02). No rank evidence found: we list it for its tools and tips. | 2026-11-19: GW11 top five of Haaland, Palmer, Groß, Evanilson and Vušković ([post](https://www.fantasyfootballfix.com/blog-index/fpl-gw11-top-5-players-2026-27/)). 2026-11-15: most bought were Nicolas Jackson, Thierno Barry and Evanilson; most sold were João Pedro, Gvardiol and Guéhi ([post](https://www.fantasyfootballfix.com/blog-index/fpl-gw11-transfer-tips-2026-27/)). |

| FourFourTwo FPL (Buy, Hold or Sell) | https://www.fourfourtwo.com/competition/buy-hold-or-sell-fpl-expert-reveals-gw11-transfers-cheat-sheet-amid-controversial-chelsea-verdict | FPL-specific | Football magazine website | Long-running football magazine with a named FPL columnist. No audience or rank evidence for the columnist: **UNVERIFIED**. | 2026-11-20: buy Van Hecke or Vušković, hold Mbeumo and Schade, and hold off on Tavernier until his suspension risk clears. |

### Projection and model sites

These sites publish model projections, not a manager's track record. We found no audience or rank evidence for any of them, so treat each one as a single data input and compare several before acting.

| Source | URL | Type | Platform | Evidence | Recent model output |
|---|---|---|---|---|---|
| FPL Copilot | https://fplcopilot.com/blog/fpl-tips | FPL-specific | Website | None found: **UNVERIFIED**. | 2026-11-20: captain Bruno Fernandes, differential Muharemović, value pick Calvert-Lewin. |
| FPL Apex | https://fplapex.com/gw/gw12.html | FPL-specific | Website | None found: **UNVERIFIED**. | 2026-11-23: GW12 captain order is Bruno Fernandes, then Calvert-Lewin, with Mbeumo fourth and Nicolas Jackson sixth. |
| LineupOptimizer | https://lineupoptimizer.app/fpl-tips | FPL-specific | Website | None found: **UNVERIFIED**. | 2026-11-23: top GW12 captain is De Cuyper, just ahead of Bruno Fernandes and Muharemović. |
| Statz | https://statz.ai/fpl/gameweek/12 | FPL-specific | Website | None found: **UNVERIFIED**. | 2026-11-23: top captain Bruno Fernandes (5.7 projected points). Tavernier and Haaland are among its transfer targets, though Haaland is also among the most sold. |

Checked and left out:

- **FPL Alpha, FPL Demon and FPL Picks.** Their pages were current, but we couldn't read any specific picks from them.
- **Finn Sollie.** Ten top-10K finishes, but we found no public channel.
- **Official FPL Podcast.** No audience or rank data found.
- **Andy North, Az, Mark Sutherns, FPL BlackBox and others on Feedspot.** We found audience figures but no rank evidence.
- **r/FantasyPL.** Not checked yet.

## Football pundits and insight sources (not FPL-specific)

| Source | URL | Type | Platform | Why it ranks (evidence) | Use for FPL / recent |
|---|---|---|---|---|---|
| Sky Sports Football | https://www.youtube.com/SkySportsFootball | Pundit | YouTube, TV | 908K subscribers ([SponsorRadar](https://sponsorradar.com/channels/skysportsfootball), 2026-11-22). Official channel of a major UK broadcaster. | Press-conference clips, team news, highlights. 2026-11-19: previewed Liverpool v Man Utd (22 Nov), Pochettino's first game in charge, with Arsenal five points clear ([talking points](https://www.skysports.com/football/news/13613208/mauricio-pochettino-begins-man-utd-tenure-with-testing-trip-to-liverpool-premier-league-talking-points)). The 6.02M figure ChatGPT gave is for a different channel (Sky Sports Premier League) and is **UNVERIFIED**. |
| BBC Sport / Match of the Day | https://www.youtube.com/@bbcsport | Pundit | YouTube, TV | 979K subscribers ([vidIQ](https://vidiq.com/youtube-stats/channel/UCW6-BQWFA70Dyyc7ZpZ9Xlg), 2026-11-22). Public broadcaster. The agent already reads the BBC Premier League RSS feed. | Highlights, post-match analysis. Recent: not retrieved. |
| Tifo Football (The Athletic) | https://www.youtube.com/@tifo | Pundit / tactics | YouTube | About 1.83M subscribers ([vidIQ](https://vidiq.com/youtube-stats/channel/@tifo/), 2026-11-22). | Tactical explainers: formations, pressing, set pieces. Recent: not retrieved. |
| The Rest Is Football (Goalhanger) | https://podnews.net/podcast/iaqh1 | Pundit | Podcast, YouTube, Netflix | #5 in the UK Q2 2026 podcast chart ([Edison via Podnews](https://podnews.net/press-release/top-25-podcasts-uk-q226), 2026-09-06). Subscriber count: **UNVERIFIED**. | Weekly form and tactics talk. Recent: not retrieved. |
| The Overlap (Gary Neville) | https://www.theoverlap.com | Pundit | YouTube, podcast | About 6M YouTube subscribers across its network, before acquiring channels worth another 3.7M ([report](https://the-business-of-youtube.beehiiv.com/p/from-podcast-to-platform-inside-the-overlap-s-multi-million-pound-land-grab), 2026-04-16). #20 in the UK Q2 2026 podcast chart (Podnews). | Analysis and opinion, not team news. Leans toward Manchester United. Recent: not retrieved. |
| Opta Analyst | https://theanalyst.com/competition/premier-league/stats | Insight / data | Website | Run by Opta / Stats Perform, the data provider. Audience: **UNVERIFIED**. | xG and playing-style analysis. Recent: not retrieved. |
| The IntelliGoal | https://theintelligoal.com/ | Insight / visuals | Website (free, AI-generated analysis) | Chucky's pick for match visuals (2026-11-23). Run by The Intelligo AI Inc. It covers the big five leagues and the Champions League, with lineups before kick-off, "decoded" match stories after full time and 3D match visuals. Audience: **UNVERIFIED**. | Post-match context on how a game was won, useful for judging whether a result or a haul was deserved. Not FPL-specific. Latest items read: 2026-11-22 (e.g. Liverpool 4-0 Man Utd). |
| Premier League injury page | https://www.premierleague.com/en/latest-player-injuries | Insight (official) | Website | Official source. The page said "last updated 21 Nov 2026" when read on 2026-11-23. | The main reference for availability across all 20 clubs. It has no press-conference quotes. |

Checked and left out: **premierinjuries.com (Ben Dinnery).** The cached page was last updated in 2021, and we couldn't confirm who runs it.

## Gaps and next steps

- **Seed sources.** We couldn't read 2026/27 advice from Let's Talk FPL, FPL Raptor, FPL Mate or The Athletic. YouTube and premierleague.com were blocked from the cloud, and the Mac's sandbox shell was blocked as well. Read these in a browser.
- **Feed URLs.** The YouTube channel IDs above come from stats-site URLs and haven't been checked on YouTube. Each channel's uploads would be at `https://www.youtube.com/feeds/videos.xml?channel_id=<id>`. Adding any of them to `FPL_NEWS_FEEDS` would change what the agent reads, so it is a separate decision.
- **Next audience pull.** Refresh Feedspot's figures (2026-07-02) against a newer snapshot.
