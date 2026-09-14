# Solunar Feeding Times

A standalone Streamlit app that forecasts solunar "feeding times" -
major/minor hunting & fishing activity windows - for any postal/zip
code worldwide, and ranks the best 6-hour hunting windows using a
scoring model calibrated against published GPS-collar research.

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
> nothing (Neary et al. 2025). This app still computes and displays them,
> because that's what it's for, but the **window ranking weights them at
> their measured size rather than their traditional one**, so they barely
> move the results. What does move them is rut phase and dawn/dusk.

Computed locally with the [`ephem`](https://pypi.org/project/ephem/)
astronomy library (PyEphem) - no external solunar service, no network
call for the math itself.

On top of the daily feeding times, the app cross-references rut phase and
an hourly weather forecast to rank the best ~6-hour "hunting windows"
over the search period. This ranking is pure arithmetic - **no LLM is
used anywhere in this app**, on purpose, since it's public-facing and
window selection needs to be reproducible with no API cost or key
exposure for other people's usage.

## How the scoring works

### One currency: yards per hour

Every term in the model is expressed in the unit the underlying research
measured it in - **yards per hour (yph) of excess daytime buck
movement** - and a window's score is the *mean* excess yph across its
hours, converted to points by one shared constant:

```
score = k × mean over the window's hours of (excess yph in that hour)
k = 3.0 / 142 ≈ 0.0211
```

`k` is anchored so peak rut - the largest effect in the source data,
+142 yph - is worth 3.0 points. The consequence is that **no weight in
this model is hand-tuned**: each is whatever a study measured, so the
relative ordering of rut vs. dawn/dusk vs. solunar is auditable rather
than a matter of taste.

Averaging rather than summing is what makes effects of different
*durations* comparable. Rut elevates movement across every hour of a
window; dawn/dusk elevates roughly two hours of six. At equal yph the
all-hours effect genuinely is worth 3× the two-hour one, and that only
falls out correctly if both are averaged over the same window.

### The measured weight ladder

All effect sizes below are from **Neary et al. 2025**, against a **269
yph season mean**:

| Effect | Measured | Points at that rate |
|---|---|---|
| Peak rut | +142 yph | +3.00 |
| Early rut | +104 yph | +2.20 |
| Late rut | +78 yph | +1.65 |
| **Within 1 hr of sunrise/sunset** | **+48 yph** (317 vs. 269) | **+1.01** |
| Post-rut | +9 yph | +0.19 |
| Pre-rut | +4 yph | +0.08 |
| **Solunar Major** (moon overhead/underfoot) | **+3 yph** | **+0.06** |
| **Solunar Minor** (moonrise/moonset) | **-0.1 yph** | **0.00** |
| Outside the rut | -40 yph | -0.85 |

A term's actual contribution to a window also depends on how many of the
window's hours it covers - a dawn band covers ~2 of 6, so it adds ~0.34.

### The five terms

| # | Term | Basis |
|---|---|---|
| 1 | **Daily activity** | Dawn/dusk (±60 min) + solunar Major (±60 min) / Minor (±30 min), overlap-weighted per hour at the measured yph above. **Measured.** |
| 2 | **Rut phase** | 14-day bands around a user-supplied peak breeding date, at the measured yph above. **Measured** (effect size); **user-supplied** (timing). |
| 3 | **Cold** | Degrees below this location's own recent normal *for that hour of day*, ramping to 16 yph at 15°F below normal. **Judgment call, bounded.** |
| 4 | **Rain/wind penalty** | Rain to -16 yph at 100% chance; wind to -8 yph, engaging only above 15 mph and maxing at 40 mph. **Judgment call, bounded.** |
| 5 | **Pressure** | 5 yph for the 29.8-30.3 inHg band, plus 5 yph (or 2.5) for a falling 24-hour trend. **Folklore, deliberately near-token.** |

The weather terms (3-5) could not be calibrated the same way, because no
located study reports weather effects as a movement-rate change. They are
bounded judgment calls, and the bounds are chosen to preserve evidential
ordering:

- The ceiling for the best-supported weather terms is **one dawn's worth**
  of contribution (48 yph over ~2 of 6 hours ≈ 16 yph averaged), so
  weather can match but never dominate the best-established daily signal.
- **Cold gets that full ceiling** - temperature is the only weather
  variable with repeated support (Webb et al. 2010).
- **Wind gets half of it** - weak and inconsistent in the fine-scale GPS
  literature.
- **Pressure gets half the Penn State null-result spread** (~±10 yph →
  5 yph), keeping the entire pressure block below the cold term, which
  matches the fact that temperature has repeated support and pressure has
  none.

### Why cold is measured against a local normal

The previous version of this model gave a bonus for dropping below a flat
45°F. That doesn't generalize down the East Coast - 38°F means something
very different in Maine than in Georgia, and deer respond to change from
what they're acclimated to. The app now requests 7 days of *past* hourly
observations alongside the forecast and builds a per-hour-of-day normal
from them, so a daytime window is compared against daytime history rather
than a day/night average.

The same past-days fetch fixed a real bug: the 24-hour pressure lookback
previously had no history to reach into, so **every window in the first
24 hours of the forecast silently scored no pressure trend at all.**

## Sources

Every citation below was verified against the published source — abstract
or full text retrieved and read, bibliographic details confirmed against
Crossref, and every URL checked to resolve. Each entry states exactly
what this project takes from it, and flags where a source *disagrees*
with the model.

### Primary — supplies the weights

**Neary, N., B. Strickland, L. Resop, S. Demarais, and W. McKinley.
2025.** *Lunar Legends: Does the Moon Influence Buck Activity?*
Mississippi State University Extension Publication 4068.
<https://extension.msstate.edu/publications/lunar-legends-does-the-moon-influence-buck-activity>

- **Study design:** 48 GPS-collared bucks, central Mississippi, 15-minute
  fixes, September–February, 2 years. Crucially, each buck is compared
  against *his own* usual movement at the same time of day within a
  ±1-week window, which nets out rut phase and individual personality.
- **What this project takes from it:** every activity weight in the
  model. The 269 yph daytime season mean and 34% bedded baseline; the
  dawn/dusk figure (317 yph and 21% bedded within 1 hour of sunrise or
  sunset, i.e. **+48 yph**); the solunar results (**major +3 yph, minor
  -0.1 yph**, plus null results for moon phase, phase × position
  combinations, perigee/apogee, and moon × dawn/dusk combinations); and
  the full rut-phase ladder (**pre +4, early +104, peak +142, late +78,
  post +9, no-rut -40 yph**). Also the 14-day phase spacing (Mississippi
  pre-rut Nov 27 / early Dec 11 / peak Dec 25 / late Jan 8 / post Jan 22)
  and the peak-rut baseline of 409 yph / 22% bedded.
- **Caveats carried into the app:** these are **buck** movement rates,
  daytime only, from a single Mississippi population.

### Supporting — shapes the structure

**Webb, S.L., K.L. Gee, B.K. Strickland, S. Demarais, and R.W. DeYoung.
2010.** *Measuring Fine-Scale White-Tailed Deer Movements and
Environmental Influences Using GPS Collars.* International Journal of
Ecology 2010:1–12, article 459610. DOI 10.1155/2010/459610.
<https://doi.org/10.1155/2010/459610>

- **Study design:** 17 female and 15 male white-tailed deer, Oklahoma,
  7 years, 3 seasons, 15-minute relocation attempts. Five weather
  variables: air temperature, wind speed, pressure, relative humidity,
  total precipitation.
- **What this project takes from it:**
  - The weather result, verbatim: *"We found general linear trends in
    movements related to 4 of the 5 weather variables in only 8 of 80
    (10%) models. Temperature influenced movements in 5 of 8 cases and
    rain, relative humidity and wind speed each in 1 case."* This is why
    temperature is the only weather variable given the full ceiling.
    Note the arithmetic: 5 + 1 + 1 + 1 = 8, so **pressure is the one
    variable of five with no linear trend at all** — the single
    strongest justification for downweighting the pressure terms. The
    paper adds that *"parameter estimates of the 8 significant models do
    not provide useful biological"* interpretation.
  - The independent null for **moon phase** on daily, nocturnal and
    diurnal movement, corroborating Neary et al.
  - The conclusion that *"routine crepuscular movements... appear to be
    the most important factors influencing movements"*, and that
    *"hourly and daily variation in weather events have minimal impact"* —
    the basis for dawn/dusk outweighing solunar, and for the weather
    block being capped below the activity block.
  - **Partial disagreement, reported honestly:** in a separate
    day-over-day analysis, weather changes affected movements in 10 of
    80 models (12.5%), and **pressure accounted for 3 of those 10** —
    more than precipitation. So pressure is not wholly inert, which is
    why its weight is small rather than zero.
  - An independent corroboration of the rut effect's *direction*: *"Male
    total daily movements were 20% greater during rut (7,363 m ± 364)
    than postrut (6,156 m ± 260)."*

**Penn State Deer-Forest Study.** *Spidey Sense.*
<https://www.deer.psu.edu/spidey-sense/>

- **What it is:** a research-project blog post reporting the study's own
  GPS data. **Not peer-reviewed** — flagged as such because this project
  leans on it for the pressure downweighting.
- **Study design:** 30 storm events, 52,279 GPS locations, 4–8 collared
  adult females, 2016–2017.
- **What this project takes from it:** the finding of **"no statistical
  or biological significance of oncoming winter storms on behavior or
  movement"**, and the hourly movement rates behind it (before ~102–105,
  during ~98–113, after ~94–111, control ~102–111 yards/hour). The
  roughly ±10 yph spread across those conditions is the upper bound the
  pressure terms are pinned to — at half that value, since it bounds an
  effect the study could not detect at all. Also the finding that deer
  moved **less during** storms, which is the basis for the precipitation
  penalty's direction.

**Pennsylvania Game Commission** — *When is the rut?*
<https://www.pa.gov/agencies/pgc/wildlife/discover-pa-wildlife/white-tailed-deer/when-is-the-rut>

- **Method:** fetal measurements from **over 6,000 road-killed does,
  2000–2007**, back-calculated to conception dates.
- **What this project takes from it:** the **November 15** default peak
  rut date. The Commission reports that **peak breeding by adult does
  occurs in mid-November**, with nine of ten does bred between
  mid-October and mid-December (doe fawns breed later, peaking late
  November into early December). November 15 is the midpoint of that
  mid-November peak. The Commission separately concludes that deer
  follow their natural breeding schedule rather than lunar cycles.
- **Verification note:** a widely repeated figure attributes a more
  precise "**peak conception November 13–17**, half of does bred by
  November 13" to the Penn State Deer-Forest Study. That figure could
  **not** be confirmed on a Penn State primary source, only in secondary
  hunting-press articles, so this project cites the Game Commission's
  directly verified "mid-November" instead and does not rely on the
  narrower claim.

**Hunsaker, M.A., M.L.J. Gilbertson, D.J. Storm, and W.C. Turner. 2025.**
*The Breeding Season and Movement Ecology of Male White-Tailed Deer in
Southwest Wisconsin.* Ecology and Evolution 15(7):e71589. DOI
10.1002/ece3.71589.
<https://pmc.ncbi.nlm.nih.gov/articles/PMC12240682/>

- **Study design:** 188 collared male deer, southwest Wisconsin (Dane,
  Iowa, Grant counties), 15 October–1 December, 2017–2020.
- **What this project takes from it:** the changepoint-derived peak
  breeding window of **October 23–November 12**, plus the paper's framing
  that variation in how studies define the breeding season *"created
  uncertainty about whether regional differences in deer breeding ecology
  stem from ecological factors or methodological inconsistencies."* Used
  for one specific purpose: to show peak rut date is *not* a clean
  function of latitude — Wisconsin (~43°N) peaks roughly two weeks
  *earlier* than Pennsylvania (~41°N) despite being further north, and
  central Mississippi (~33°N) peaks December 25. That is why the app
  **asks** for a peak rut date instead of computing one from latitude.

### Dissenting — solunar studies that disagree

The three studies below test solunar theory directly and reach three
different answers. That lack of replication, not any single null result,
is the reason solunar carries near-zero weight here.

**Sullivan, J.D., S.S. Ditchkoff, B.A. Collier, C.R. Ruth, and J.B.
Raglin. 2016.** *Movement with the moon: white-tailed deer activity and
solunar events.* Journal of the Southeastern Association of Fish and
Wildlife Agencies 3:225–232.
<https://seafwa.org/journal/2016/movement-moon-white-tailed-deer-activity-and-solunar-events>

- **Study design:** 38 adult male white-tailed deer, GPS locations every
  30 minutes, August–December 2010–2012, at Brosnan Forest, Dorchester,
  South Carolina.
- **What this project takes from it:** a **split** result. On days near a
  new or full moon, activity probability during **minor** periods *rose*
  — moonrise 0.384 → 0.564, moonset 0.403 → 0.591 — while during
  **major** periods it *fell*: moon overhead 0.540 → 0.413 and underfoot
  0.516 → 0.305. The paper opens by stating deer activity patterns "are
  predominately crepuscular", and concludes that solunar events have
  "some association with deer activity. However, the relationships
  between lunar events and lunar phase expressed in solunar charts **may
  be misleading**."
- **Correction note:** an earlier draft of this README claimed this paper
  was the reason the Major weight is left positive. That was wrong — this
  paper found major-period activity *decreasing* on high-rated days. The
  positive Major weight comes from Neary et al. alone.

**Swartout, T.J., and S.S. Ditchkoff. 2025.** *Are Solunar Charts as
Predictable as They Claim? Comparing Solunar Ratings to Activity Patterns
of Male White-Tailed Deer.* Southeastern Naturalist 24(2):137–150. DOI
10.1656/058.024.0206. <https://doi.org/10.1656/058.024.0206>

- **Study design:** 22 GPS-collared adult male deer on a **high-fenced**
  property in Alabama, 15-minute fixes, December–February 2009–2011;
  activity defined as >51.78 m moved between fixes; solunar day ratings
  1–4.
- **What this project takes from it:** the **strongest pro-solunar
  result** found anywhere in this review, reported here because it cuts
  against the model's weighting. On top-rated days, activity was **3.02×
  and 2.83×** more likely during moon underfoot and overhead (major)
  periods — but only **0.30× and 0.37×** during moonrise and moonset
  (minor), i.e. *less* likely. The authors' own conclusion: *"This study
  supports prior findings that solunar charts show inconsistencies in
  predictions of Deer activity."*
- **Why the weights weren't changed:** the result is reported as odds of
  being "active", not as a movement rate, so there is no non-invented way
  to convert it into the yph currency every other weight uses.
  Additionally it disagrees with Sullivan et al. on the *direction* of
  the major-period effect, and a high-fenced, winter-only, 22-animal
  sample is the weakest basis for generalizing of the three. It does
  corroborate one thing the model already encodes: **major periods
  matter more than minor ones, and minor periods may be neutral or
  negative** — which is why `MINOR_YPH` is 0.0.

### Consulted — informs known gaps, not implemented

**Little, A.R., S.L. Webb, S. Demarais, K.L. Gee, S.K. Riffell, and J.A.
Gaskamp. 2016.** *Hunting intensity alters movement behaviour of
white-tailed deer.* Basic and Applied Ecology 17:360–369. DOI
10.1016/j.baae.2015.12.003. <https://doi.org/10.1016/j.baae.2015.12.003>

- **Study design:** southern Oklahoma; GPS locations at 30-minute
  intervals under manipulated hunting-risk treatments.
- **What this project takes from it:** confirmation that **hunting
  pressure is a real movement driver** — movement rate was greater under
  risk treatments than controls, while low hunting pressure produced no
  biologically significant change in female movement. Not implemented,
  because the app has no data source for local hunting pressure and a
  weekend/opening-day proxy would behave very differently on public vs.
  private land. Listed as a known gap below.
- **Correction note — this is why the citation audit mattered.** An
  earlier version of this code cited "Little et al. 2016, *Effects of
  Weather on Habitat Selection and Movement of White-tailed Deer*" in
  support of the falling-pressure bonus. **That title does not exist.**
  The author/year are real but were attached to a fabricated title, and
  the actual Little et al. 2016 is the hunting-intensity paper above,
  which says nothing about barometric pressure. The false citation has
  been removed.

**Goethlich, J. 2019.** *Effects of Abiotic Factors on White-tailed Deer
Activity in South Carolina.* M.S. thesis, Auburn University (chair:
S.S. Ditchkoff). <https://etd.auburn.edu/handle/10415/7077>

- **Study design:** **116** GPS-collared adult white-tailed deer,
  2009–2018, South Carolina; activity classified from interfix step
  length and turning angles; each abiotic factor modelled separately by
  logistic regression.
- **What this project takes from it:** the thesis abstract's conclusion,
  verbatim: *"responses to abiotic factors were typically less pronounced
  than circadian fluctuations in activity, and occurred most often during
  non-peak times of activity."* The first half independently supports the
  core design decision here — weather is capped below the daily-rhythm
  terms. The second half implies weather should additionally be *damped*
  inside crepuscular windows. **Webb et al. 2010 found the same thing
  independently** (weather effects concentrated at 0100–0200 and 1300,
  i.e. "hours of limited movements"), so this is two studies, not one.
  Still not implemented: it would turn the additive model into an
  interaction, complicating both the math and the stacked score chart,
  and both studies agree the total weather effect is small anyway — which
  the low weather ceilings already encode. Listed as a known gap below.
- **Disagreement, reported honestly:** this thesis found that weather
  condition, temperature, wind speed, **barometric pressure**, moon
  phase and moon position all "affected activity in some seasons and
  times of day." So neither pressure nor moon is universally inert —
  another reason both are downweighted rather than removed.
- **Not independent of Sullivan et al. 2016:** both studies come from the
  Auburn Deer Lab and both used deer at Brosnan Forest, South Carolina.
  Treat them as one study site, not two replications.

## Known gaps and limitations

- **Hunting pressure is not modeled.** Little et al. 2016 establishes it
  matters; the app has no data source for it.
- **Weather is not damped at dawn/dusk**, though Goethlich 2019 *and*
  Webb et al. 2010 independently found weather effects concentrate in
  non-peak activity hours. This is the best-supported unimplemented
  refinement in the list.
- **Solunar weighting rests on one study's units.** Neary et al. 2025 is
  the only solunar test reporting results as a movement rate, so it alone
  sets the Major/Minor weights. Swartout & Ditchkoff 2025 found a
  substantially *larger* major-period effect (2.83–3.02× odds of
  activity) that could not be converted into those units without
  inventing a conversion. If you think solunar deserves more weight, that
  paper is the argument, and `MAJOR_YPH` is the single constant to change.
- **Rut weights are buck-specific**, from one Mississippi population,
  daytime only.
- **Rut phase barely discriminates *within* a short forecast.** It's a
  per-day level, so across a 7-day search it mostly shifts every window
  up or down together. It earns its place by making the absolute score
  meaningful ("is this week worth hunting at all?") and by discriminating
  across longer searches and phase boundaries.
- **The cold anomaly has a seasonal-drift artifact.** In autumn, a
  trailing 7-day baseline systematically makes the forecast look cold,
  because temperatures are genuinely declining. This inflates the cold
  term roughly equally across all windows, so it largely cancels out of
  the *ranking*, but absolute scores in fall run slightly high.
- **No diel curve.** The model gives a bonus at dawn/dusk but otherwise
  treats 2 AM and 2 PM identically, because the sources provide only two
  points (season mean and crepuscular mean), not a full activity curve.
- **Solunar windows are still displayed prominently** despite scoring
  near zero, because computing them is the app's original purpose.

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

1. **Country** - dropdown of every country
   [Zippopotam.us](https://www.zippopotam.us/) has postal-code data for
   (69 countries, transcribed from its "Countries Supported" table).
   Not every country in the world is listed - a country missing here has
   no postal-code coverage on Zippopotam and would just fail the lookup.
2. **Postal / zip code** - the postal code to look up within that
   country.
3. **Days** - slider from 1 to 30 days of forecast (defaults to 7).
4. **Peak breeding (rut) date** - defaults to November 15. Override it
   with your state wildlife agency's conception data if they publish it;
   local data beats any formula this app could apply.
5. Click **Get feeding times** to fetch the location, resolve its local
   timezone, and compute/display the forecast, one card per day.

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
  are shown as text, and all candidates are plotted as a stacked bar
  chart split into Rut Phase / Daily Activity / Weather.

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
```
