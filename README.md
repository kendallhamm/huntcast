# Huntcaster

We all have limited time in our day- and even more limited time for
discretionary activities such as hunting! But what if there was a way to
guesstimate the best time windows for deer movement? This could allow
you to put yourself in the stand during periods that would be more
likely to result in a successful harvest.

This project seeks to answer just that question. It doesn't promise
success, or even hint that it may happen. But, if you're like me, the
illusion of control will make you feel like it made a difference. Good
luck and happy hunting. -KH

A standalone Streamlit app that ranks the best 6-hour deer hunting
windows for any postal/zip code worldwide, using a scoring model
calibrated against published GPS-collar research - combining rut phase,
weather, and solunar major/minor "feeding time" windows.

Fully self-contained: no `.env`, no API keys, no dependency on any other
project. Geocoding and timezone resolution use free, keyless public
APIs, and the solunar math itself runs locally.

## What it predicts

Solunar theory (John Alden Knight, 1926) predicts four daily windows of
elevated wildlife feeding activity based on the moon's position:

- **2 Major periods** (~2 hrs each) - centered on moon transit (directly
  overhead) and antitransit ("underfoot")
- **2 Minor periods** (~1 hr each) - centered on moonrise and moonset

Sunrise and Sunset are also shown, interleaved chronologically with the
Major/Minor windows.

> **Read this before trusting the solunar windows.** The GPS-collar
> literature does not support solunar theory. Measured against a buck's
> own usual movement at the same time of day, major periods come out at
> **+3 yards/hour** and minor periods at **-0.1 yards/hour** against a
> 269 yards/hour season mean - statistically indistinguishable from
> nothing (Neary et al. 2025). This app computes and displays them
> because hunters ask for them, but the **window ranking weights them at
> their measured size rather than their traditional one**, so they barely
> move the results. What does move them is rut phase and dawn/dusk.

Computed locally with the [`ephem`](https://pypi.org/project/ephem/)
astronomy library (PyEphem) - no external solunar service, no network
call for the math itself.

On top of the daily feeding times, the app cross-references rut phase and
an hourly weather forecast to rank the best ~6-hour "hunting windows"
over the search period, using the scoring formula described below.

## Two modes

The first thing the app asks is which formula should rank your windows.

**1. Kendall's formula** (the default) is the model described below:
every weight traced to a GPS-collar study, nothing hand-tuned.

**2. Build your own formula** exposes 16 dials — the effect sizes, and
the thresholds that decide where each effect switches on — with the
research behind each one printed next to it: a one-line summary of what
was measured, and a **"Why this number"** expander holding the relevant
finding and its citation. Dials are in **yph**, the same unit the
studies report, so what you set is directly comparable to what was
measured. The rut ladder is a single dial that scales all five phases
together, because the ladder's *shape* is what Neary et al. establish
and its overall *size* is the realistic thing to disagree about.

Switching modes never loses your forecast — the location, dates and
weather are cached, and only the scoring re-runs — and **Reset to
Kendall's** puts every dial back. In custom mode the scoring notes at
the bottom of the page re-print themselves from your dial positions, so
the page never describes a formula other than the one that produced the
ranking.

### Saving a formula

Custom dial positions last for the browser session; reloading starts you
back at Kendall's formula. To keep one, use **Save this formula**, which
sits directly under the formula comparison (or, before you've run a
forecast, at the foot of the page): pressing **Put this formula in the URL** writes
your dials into the page's own address, so you can bookmark it or send
it to someone. Opening that link puts every dial back and starts in
custom mode.

Nothing is stored anywhere — the link *is* the formula. There's no
account, no file, no server-side state, which keeps the app as
self-contained as the rest of it.

It is **opt-in on purpose**. The address bar is left completely alone
until you press the button, so a query string nobody asked for doesn't
clutter every visit. Once you have opted in, the link rewrites itself on
every change, so it can never describe a formula other than the one on
screen, and **Take it back out of the URL** clears it again.

Only dials that differ from Kendall's are written, so a one-dial tweak
makes a short link (`?f=1&cold_max_yph=45`). Reading a link is
forgiving: unknown parameters are ignored, an unparseable value is
skipped, and an out-of-range one is clamped and snapped onto its dial's
step, so a hand-edited link degrades into the nearest sane formula
rather than failing. The `f=1` version marker is the one hard gate — if
the dial set ever changes, old links stop being read instead of quietly
decoding to something different.

### Comparing the two

Once you've actually moved a dial, a **Your formula vs. Kendall's**
section appears under the forecast charts: both top 3s side by side,
and for each window, where the *other* formula places it — "Kendall's
formula ranks this #88 of 154". A one-line verdict says whether the two
agree on all three, some, or none.

The comparison is by **rank, not points**. Scaling every dial up
multiplies all your scores without reordering anything, so the two
formulas' point totals aren't on a shared scale and showing them side
by side would invite a comparison that means nothing. Where a window
places is what survives rescaling. Ranks are out of every legal,
un-elapsed 6-hour window in the forecast — not just the non-overlapping
picks — because the two formulas select different non-overlapping sets,
so a window in your top 3 may be absent from Kendall's picks entirely
while still having a well-defined rank.

## How the scoring works

This section describes **Kendall's formula** — the default, and the
starting position of every dial in custom mode.

Every term in the model is expressed in the unit the underlying
GPS-collar research measured it in — **yards per hour (yph) of excess
daytime buck movement** — and a window's score is the *mean* excess yph
across its six hours, scaled so the largest measured effect in the
source data (peak rut, +142 yph) is worth 3.0 points.

The consequence is that **no activity weight in this model is
hand-tuned**: each is whatever a study measured, so the relative
ordering of rut vs. dawn/dusk vs. solunar is auditable rather than a
matter of taste. (That claim is about Kendall's formula. A custom
formula is hand-tuned by definition — that is the point of it — which is
why the app labels it as yours and keeps the measured value visible
beside every dial you've moved.)

| Effect | Measured | Points at that rate |
|---|---|---|
| Peak rut | +142 yph | +3.00 |
| Early rut | +104 yph | +2.20 |
| Late rut | +78 yph | +1.65 |
| **Within 1 hr of sunrise/sunset** | **+48 yph** (317 vs. 269) | **+1.01**, scaled by rut phase |
| Post-rut | +9 yph | +0.19 |
| Pre-rut | +4 yph | +0.08 |
| **Solunar Major** (moon overhead/underfoot) | **+3 yph** | **+0.06** |
| **Solunar Minor** (moonrise/moonset) | **-0.1 yph** | **0.00** |
| Outside the rut | -40 yph measured, scored as 0 | 0.00 |

All of the above are from **Neary et al. 2025**, measured against a 269
yph season mean. A term's actual contribution to a window also depends
on how many of the window's hours it covers — a dawn band covers ~2 of
6, so it adds ~0.34.

Two things the table doesn't show. The rut figures are **anchor points,
not plateaus**: each lands exactly on its named day and the days in
between are interpolated, so no single day's slip in the user-supplied
peak date can drop a score off a cliff. And the terms are **added
together**, which is an assumption the source doesn't test - see
[litreview.md](litreview.md#adding-the-terms-is-itself-an-assumption).

| # | Term | Basis |
|---|---|---|
| 1 | **Daily activity** | Dawn/dusk (±60 min) + solunar Major (±60 min) / Minor (±30 min), overlap-weighted per hour at the measured yph above. Dawn/dusk is also scaled by a **measured rut-phase factor** - 1.15 outside the rut, 0.13 at peak - because Neary et al. measured the dawn premium separately by phase and it nearly vanishes at peak rut. **Measured** (Neary et al. 2025). |
| 2 | **Rut phase** | Five measured levels anchored 14 days apart around a user-supplied peak breeding date and linearly interpolated between, so the ladder is continuous in day offset. **Measured** effect size (Neary et al. 2025); **user-supplied** timing. |
| 3 | **Cold** | Degrees below this location's own recent normal *for that hour of day*, ramping to 16 yph at 15°F below. **Judgment call, bounded** (Webb et al. 2010). |
| 4 | **Rain/wind penalty** | Rain to -8 yph at 100% chance; wind to -8 yph, engaging above 15 mph and maxing at 40. **Judgment call, bounded** (Webb et al. 2010; Penn State Deer-Forest Study). |
| 5 | **Pressure** | **Zero weight, both halves.** The falling 24-hour trend and the 29.9-30.3 inHg "sweet spot" band are both reported in the window text and neither moves the ranking. **Three nulls** (Webb et al. 2010; Penn State Deer-Forest Study; Hellickson et al., Texas). |
| 6 | **Dawn/dusk damping** | Terms 3-5 are evaluated hour by hour and multiplied by `1 - 0.5 × (fraction of the hour inside a sunrise/sunset halo)`. **Structure from two studies** (Goethlich 2019; Webb et al. 2010); size a judgment call. |

The weather terms (3-5) could not be calibrated the same way, because no
located study reports weather effects as a movement-rate change. They
are bounded judgment calls, ceilinged at **one dawn's worth of
contribution** so weather can match but never dominate the
best-established daily signal — cold gets that full ceiling, rain and
wind half of it each, and both halves of the pressure term nothing at
all.

> **Where the rest of this went.** The full derivation of every
> constant, the annotated bibliography behind it, the county and region
> rut-date sources, the audit notes, and the model's known gaps and
> limitations now live in **[litreview.md](litreview.md)**. That is the
> document to read — or to argue with — before changing a weight.

## Setup

```
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS/Linux
pip install -r requirements.txt
```

## Run

```
streamlit run solunar.py
```

Streamlit will print a local URL (defaults to `http://localhost:8501`)
to open in your browser.

## Using it

1. **Which formula** - **Kendall's formula** (the researched default) or
   **Build your own formula** (16 dials, each with the research behind
   it). See [Two modes](#two-modes). You can switch at any point without
   losing the forecast, so the quickest way in is to leave it on
   Kendall's, get a forecast, and only then start moving dials to see
   what changes.
2. **Country** - dropdown of every country
   [Zippopotam.us](https://www.zippopotam.us/) has postal-code data for
   (69 countries, transcribed from its "Countries Supported" table).
   Not every country in the world is listed - a country missing here has
   no postal-code coverage on Zippopotam and would just fail the lookup.
3. **Postal / zip code** - the postal code to look up within that
   country.
4. **Days** - slider from 1 to 16 days of forecast (defaults to 7). The
   cap is Open-Meteo's hourly forecast horizon: past it there is no
   weather to score, and a window straddling the edge got its weather
   term weighted as if there were. See
   [litreview.md](litreview.md#model-mechanics--the-formulas-and-constants).
5. **Peak breeding (rut) date** - defaults to November 15. Override it
   with your state wildlife agency's conception data if they publish it;
   local data beats any formula this app could apply.
   - Expand **"I don't know my peak rut date - help me find it by state"**,
     directly under the date box, to look it up. **North Carolina** (100 counties) and **Georgia**
     (159 counties) carry their full county tables in-app: pick your
     state, then your county, and it shows that county's date with a
     **"Use <date>, <year> as my peak rut date"** button that fills the
     field in for you. **New York** (north/south) and **Texas** (16 TPWD
     ecoregions) are split by region rather than county, because that is
     the scale their sources work at. **Illinois** and **Pennsylvania** show a
     single statewide date with no picker, because their sources publish no
     sub-state breakdown. **Louisiana**, **Mississippi** and
     **South Carolina** link out to their agency maps instead — those are
     contour maps whose bands cross county lines, so there is no honest
     single date per county to show. The source PDF is always linked, but you never have to
     open it.
6. Click **Get hunting forecast** to fetch the location, resolve its local
   timezone, and compute/display the forecast, one card per day. The
   forecast then stays on screen: moving a weight dial — or changing the
   peak rut date — re-scores it in place rather than making you look the
   location up again. Only country, postal code and days need the button,
   because only those change what gets fetched.

## How it works

- **Geocoding**: postal code + country -> latitude/longitude via the
  free [Zippopotam.us](https://api.zippopotam.us/) API. No key required.
- **Timezone**: latitude/longitude -> IANA timezone name via
  [Open-Meteo](https://open-meteo.com/)'s `timezone=auto` parameter.
- **Solunar math**: `ephem`'s `Observer` class computes moon
  transit/antitransit/rising/setting and sun rising/setting for each
  day, searched independently per calendar day so one failed/missing
  event can't cascade into every later day being wrong.
- **Hunting-window ranking**: hourly temperature/precipitation
  chance/wind/barometric pressure for the search period *plus the
  preceding 7 days* ([Open-Meteo](https://open-meteo.com/)'s hourly
  forecast with `past_days=7`) is flattened into an hour-by-hour timeline
  alongside the solunar events and rut phase, then every possible 6-hour
  window is scored as described above. The top 3 non-overlapping windows
  are shown as text, and all candidates are plotted as a bar chart
  stacked by Rut Phase / Daily Activity / Weather. Bonuses stack above
  the zero line and penalties hang below it, so the bar's height is not
  the score; a diamond on each bar marks the **net** total the ranking
  actually uses, and the top 3 carry their net value as a label.

The geocoding, timezone, and hourly weather lookups are all cached
in-memory (`st.cache_data`, 1 hour TTL) so re-running a search for the
same location doesn't refetch immediately.

## Dependencies

**Standard library** (no install needed):

| Module | Used for |
|---|---|
| `datetime` | today's date, per-day search windows, rut phase offsets, formatting event times |
| `math` | averaging wind bearings as unit vectors (a naive mean of 350° and 10° gives due south) |
| `zoneinfo` | resolving/using the looked-up location's IANA timezone (`tzdata`, below, supplies the actual zone data) |

**Installed separately** (pinned in `requirements.txt`; `pip install -r requirements.txt` gets all of them):

| Package | Import name | Used for |
|---|---|---|
| `streamlit==1.63.0` | `streamlit` | the whole UI - form widgets, layout, caching (`st.cache_data`) |
| `requests==2.34.2` | `requests` | HTTP client for the Zippopotam and Open-Meteo lookups |
| `ephem==4.2.1` | `ephem` | moon/sun transit math (the actual solunar calculation) |
| `pandas==2.3.3` | `pandas` | the dataframes backing both charts |
| `altair==6.2.2` | `altair` | the stacked score-breakdown chart and the hourly overview chart |
| `tzdata==2025.2` | *(no direct import)* | IANA timezone database backing `zoneinfo`, which Windows doesn't ship with its own copy of |

### External services

Both are free, public, and require no API key or account:

| Service | Used for | Notes |
|---|---|---|
| Zippopotam.us (`api.zippopotam.us`) | postal code -> latitude/longitude | public REST, no key |
| Open-Meteo (`api.open-meteo.com`) | latitude/longitude -> IANA timezone name (via `timezone=auto`), plus the hourly forecast and 7 days of past hourly observations (temperature, precipitation chance, wind speed/direction, `pressure_msl`) that drive hunting-window ranking | public REST, no key |

## Files

```
huntcaster/
  solunar.py          <- the whole app (UI + geocoding + solunar math + scoring + charts)
  requirements.txt    <- streamlit, requests, ephem, pandas, altair, tzdata
  .gitignore          <- standard Python gitignore
  README.md           <- this file
  litreview.md        <- literature review, weight derivations, sources, known gaps
```
