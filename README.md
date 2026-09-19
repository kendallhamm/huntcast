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
over the search period, using the scoring formula described below.

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
+142 yph - is worth 3.0 points. The consequence is that **no activity
weight in this model is hand-tuned**: each is whatever a study measured,
so the relative ordering of rut vs. dawn/dusk vs. solunar is auditable
rather than a matter of taste.

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

### The six terms

| # | Term | Basis |
|---|---|---|
| 1 | **Daily activity** | Dawn/dusk (±60 min) + solunar Major (±60 min) / Minor (±30 min), overlap-weighted per hour at the measured yph above. **Measured.** |
| 2 | **Rut phase** | 14-day bands around a user-supplied peak breeding date, at the measured yph above. **Measured** (effect size); **user-supplied** (timing). |
| 3 | **Cold** | Degrees below this location's own recent normal *for that hour of day*, ramping to 16 yph at 15°F below normal. **Judgment call, bounded.** |
| 4 | **Rain/wind penalty** | Rain to -8 yph at 100% chance; wind to -8 yph, engaging only above 15 mph and maxing at 40 mph. **Judgment call, bounded.** |
| 5 | **Pressure** | 5 yph (or 2.5) for a falling 24-hour trend. The 29.8-30.3 inHg "sweet spot" band is still reported in the window text but carries **zero** weight. **Folklore, near-token.** |
| 6 | **Dawn/dusk damping** | Terms 3-5 are evaluated hour by hour and multiplied by `1 - 0.5 × (fraction of the hour inside a sunrise/sunset halo)`. **Structure from two studies; size a judgment call.** |

The weather terms (3-5) could not be calibrated the same way, because no
located study reports weather effects as a movement-rate change - a
fresh literature search through 2026 did not find one either. They are
bounded judgment calls, and the bounds are chosen to preserve evidential
ordering:

- The ceiling for the best-supported weather term is **one dawn's worth**
  of contribution (48 yph over ~2 of 6 hours ≈ 16 yph averaged), so
  weather can match but never dominate the best-established daily signal.
- **Cold gets that full ceiling** - temperature is the only weather
  variable with repeated support (5 of the 8 significant models in Webb
  et al. 2010).
- **Rain and wind each get half of it**, at the same tier as each other,
  because they have the same evidential standing: each was significant
  in exactly 1 of those 8 models. An earlier version gave rain the full
  ceiling on the strength of a Penn State storm analysis; the second
  citation audit found that source's two years disagree on *direction*
  (see Sources), so it supports neither the size nor the sign of a rain
  penalty. The penalty's direction is a judgment call.
- **Falling pressure gets half the Penn State null-result spread** (~±10
  yph → 5 yph). It keeps a token weight because Webb et al.'s day-over-day
  analysis of weather *changes* attributed 3 of 10 significant models to
  pressure.
- **The static pressure band gets nothing.** No located study tests a
  static pressure level; the closest thing to a test is Webb et al.'s
  within-day result, where pressure was the one variable of five with no
  linear trend. The constant stays in the code at 0 so it is a single
  number to raise if evidence appears.
- **Every weather term is damped by half inside dawn/dusk halos.**
  Goethlich 2019 and Webb et al. 2010 independently found weather effects
  concentrate in non-peak hours; Hunsaker et al. 2025 found no weather
  effect at all on rut-period movement. The sources say "least likely"
  and "less pronounced", not "absent", so the damping is 0.5 rather than
  1.0.
- **Sanity bound on the whole block:** Webb et al. 2010's weather
  parameter estimates never exceeded ~29 m/h (~32 yph), and the authors
  attribute even that partly to collar error. The most this block can
  move a window is about +21 / -16 yph, inside that bound.

### Why cold is measured against a local normal

The previous version of this model gave a bonus for dropping below a flat
45°F. That doesn't generalize down the East Coast - 38°F means something
very different in Maine than in Georgia, and deer respond to change from
what they're acclimated to. The app requests 7 days of *past* hourly
observations alongside the forecast and builds a per-hour-of-day normal
from them, so a daytime window is compared against daytime history rather
than a day/night average.

The same past-days fetch fixed a real bug: the 24-hour pressure lookback
previously had no history to reach into, so **every window in the first
24 hours of the forecast silently scored no pressure trend at all.**

## Sources

Every citation below has been through two independent verification
passes: bibliographic details confirmed, every URL checked to resolve,
and every number attributed to a source re-read against the abstract or
full text where accessible. Each entry states exactly what this project
takes from it, flags where a source *disagrees* with the model, and notes
any figure that could only be checked against an abstract rather than
the full text. Corrections from the second pass (September 2026) are
marked **Audit note**.

### Primary — supplies the weights

**Neary, N., B. Strickland, L. Resop, S. Demarais, and W. McKinley.
2025.** *Lunar Legends: Does the Moon Influence Buck Activity?*
Mississippi State University Extension Publication 4068.
<https://extension.msstate.edu/publications/lunar-legends-does-the-moon-influence-buck-activity>
(PDF: <https://extension.msstate.edu/sites/default/files/publications/P4068_Lunar_web.pdf>)

- **Study design:** 48 GPS-collared bucks, central Mississippi, 15-minute
  fixes, September–February, 2 years. The analysis controls for each
  buck's own movement pattern, which nets out rut phase and individual
  personality when testing the moon.
- **What this project takes from it:** every activity weight in the
  model. The 269 yph daytime season mean and 34% bedded baseline; the
  dawn/dusk figure (317 yph and 21% bedded within 1 hour of sunrise or
  sunset, i.e. **+48 yph**); the solunar results (**major +3 yph, minor
  -0.1 yph**, plus near-zero results for moon phase, phase × position
  combinations, perigee **+3** / apogee **-4** yph, and moon × dawn/dusk
  combinations); and the full rut-phase ladder (**pre +4, early +104,
  peak +142, late +78, post +9, no-rut -40 yph**). Also the 14-day phase
  spacing (Mississippi pre-rut Nov 27 / early Dec 11 / peak Dec 25 / late
  Jan 8 / post Jan 22) and the peak-rut baseline of 409 yph / 22% bedded.
- **Audit note:** every number above was re-confirmed against the
  publication PDF. An earlier README described the method as comparing
  each buck to himself "within a ±1-week window"; that specific wording
  could not be found in the publication and has been softened to what
  it does say.
- **Caveats carried into the app:** these are **buck** movement rates,
  daytime only, from a single Mississippi population.

### Supporting — shapes the structure

**Webb, S.L., K.L. Gee, B.K. Strickland, S. Demarais, and R.W. DeYoung.
2010.** *Measuring Fine-Scale White-Tailed Deer Movements and
Environmental Influences Using GPS Collars.* International Journal of
Ecology 2010:1–12, article 459610. DOI 10.1155/2010/459610.
<https://doi.org/10.1155/2010/459610>

- **Study design:** 17 female and 15 male white-tailed deer, Oklahoma
  (Noble Foundation), 7 years, 3 seasons, 15-minute relocation attempts.
  Five weather variables: air temperature, wind speed, pressure, relative
  humidity, total precipitation.
- **What this project takes from it:**
  - The weather result, verbatim: *"We found general linear trends in
    movements related to 4 of the 5 weather variables in only 8 of 80
    (10%) models. Temperature influenced movements in 5 of 8 cases and
    rain, relative humidity and wind speed each in 1 case."* This is why
    temperature is the only weather variable given the full ceiling, and
    why rain and wind share a tier. Note the arithmetic: 5 + 1 + 1 + 1 =
    8, so **pressure is the one variable of five with no linear trend at
    all** — the strongest justification for zeroing the static pressure
    band. The paper adds that *"parameter estimates of the 8 significant
    models do not provide useful biological"* interpretation.
  - The size of those estimates: weather parameter estimates were
    **≤29 m/h** (~32 yph), which the authors attribute partly to collar
    error and path tortuosity. Used as the sanity ceiling on the whole
    weather block.
  - The independent null for **moon phase**: *"Moon phase had no effect
    on daily, nocturnal, and diurnal deer movements"*, corroborating
    Neary et al.
  - The conclusion that *"routine crepuscular movements... appear to be
    the most important factors influencing movements"*, and that
    *"hourly and daily variation in weather events have minimal impact"* —
    the basis for dawn/dusk outweighing solunar, and for the weather
    block being capped below the activity block.
  - The timing of the weather effects that did appear — at 0100–0200 and
    1300, *"hours of limited movements"* — which, together with Goethlich
    2019, is the basis for damping weather inside dawn/dusk halos.
  - **Partial disagreement, reported honestly:** in a separate
    day-over-day analysis, weather changes affected movements in 10 of
    80 models (12.5%), and **pressure accounted for 3 of those 10** —
    more than precipitation. So pressure is not wholly inert, which is
    why the *falling-pressure* term is small rather than zero.
    **Audit note:** the 10-of-80 / 3-of-10 tally lives in the full-text
    tables, which are paywalled; the second audit could confirm only that
    pressure effects appeared in three season/time-specific instances
    (females spring 0100h, summer 0200h; males winter 1300h). Consistent,
    but treat the exact tally as full-text-only.
  - An independent corroboration of the rut effect's *direction*: *"Male
    total daily movements were 20% greater during rut (7,363 m ± 364)
    than postrut (6,156 m ± 260)."*

**Penn State Deer-Forest Study.** *Spidey Sense.*
<https://www.deer.psu.edu/spidey-sense/>

- **What it is:** a research-project blog post reporting the study's own
  GPS data. **Not peer-reviewed** — flagged as such because this project
  leans on it for the pressure downweighting.
- **Study design:** 30 storm events, 52,279 GPS locations, 4 collared
  adult females in 2016 and 8 in 2017. The storm record itself spans
  January–April 2015–2017; the collared-deer data is 2016–2017.
- **What this project takes from it:** the finding of **"no statistical
  or biological significance of oncoming winter storms on behavior or
  movement"**, and the hourly movement rates behind it (before ~102–105,
  during ~98–113, after ~94–111, control ~102–111 yards/hour). The
  roughly ±10 yph spread across those conditions is the upper bound the
  falling-pressure term is pinned to — at half that value, since it
  bounds an effect the study could not detect at all.
- **Audit note — this source no longer backs the rain penalty.** An
  earlier README cited it for "deer moved less during storms" as the
  basis for the precipitation penalty's direction. Re-read by year, the
  data says the opposite in 2016 (102 yph outside storms vs. **113
  during**, 111 after) and the claimed direction only in 2017 (111 vs.
  **98** during, 94 after). Two years, two signs, and a stated null
  result do not establish a direction. The rain penalty's sign is now
  labelled a judgment call and its ceiling has been halved.

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
- **Audit note on the "November 13–17" figure.** A widely repeated claim
  attributes "peak conception November 13–17, half of does bred by
  November 13" to the Penn State Deer-Forest Study. The second audit
  found a genuine Penn State primary source for the *second half*: the
  Deer-Forest Study's own posts
  (<https://www.deer.psu.edu/the-rut-is-half-over/>,
  <https://www.deer.psu.edu/how-to-predict-the-rut/>) state that "by
  November 13th half the females in Pennsylvania become pregnant",
  working from the same Game Commission fetal data set. No Penn State
  source states a **13–17** range; every accessible page gives the single
  date. So the app cites "mid-November, half bred by Nov 13" and does not
  use the five-day window. An earlier version of the app's code comments
  also asserted a Game Commission "November 10–20" range that appears
  nowhere on the Commission's page; that has been removed.

**Hunsaker, M.A., M.L.J. Gilbertson, D.J. Storm, and W.C. Turner. 2025.**
*The Breeding Season and Movement Ecology of Male White-Tailed Deer in
Southwest Wisconsin.* Ecology and Evolution 15(7):e71589. DOI
10.1002/ece3.71589.
<https://pmc.ncbi.nlm.nih.gov/articles/PMC12240682/>

- **Study design:** 188 collared male deer, southwest Wisconsin (Dane,
  Iowa, Grant counties), 15 October–1 December, 2017–2020, hourly GPS
  fixes; changepoint analysis on movement rate plus fawn-conception
  backdating.
- **What this project takes from it:** the changepoint-derived peak
  breeding window of **October 23–November 12**, plus the paper's framing
  that variation in how studies define the breeding season *"created
  uncertainty about whether regional differences in deer breeding ecology
  stem from ecological factors or methodological inconsistencies."* Used
  to show peak rut date is *not* a clean function of latitude — Wisconsin
  (~43°N) peaks roughly two weeks *earlier* than Pennsylvania (~41°N)
  despite being further north, and central Mississippi (~33°N) peaks
  December 25. That is why the app **asks** for a peak rut date instead of
  computing one from latitude.
- **Added in the second pass:** the paper found **"no significant effect
  of weather, year, hunting seasons, or the timing of opening firearm
  weekend on movement metrics"** during the rut. That is a third,
  larger-sample data point for keeping the weather block small, and a
  caution against building an opening-day or day-of-week hunting-pressure
  proxy without regional evidence. Also: 2-year-old bucks had the highest
  movement rates of any age class.

### Dissenting — solunar studies that disagree

The three studies below test solunar theory directly and reach three
different answers. That lack of replication, not any single null result,
is the reason solunar carries near-zero weight here. A search for a
meta-analysis or systematic review of solunar-theory tests across
species found none; the closest things are single-species primary
studies and trade-press skepticism.

**Sullivan, J.D., S.S. Ditchkoff, B.A. Collier, C.R. Ruth, and J.B.
Raglin. 2016.** *Movement with the moon: white-tailed deer activity and
solunar events.* Journal of the Southeastern Association of Fish and
Wildlife Agencies 3:225–232.
<https://seafwa.org/journal/2016/movement-moon-white-tailed-deer-activity-and-solunar-events>

- **Study design:** 38 adult male white-tailed deer, GPS locations every
  30 minutes, August–December 2010–2012, at Brosnan Forest, Dorchester
  County, South Carolina. Logistic regression on activity *odds*, not a
  movement rate.
- **What this project takes from it:** a **split** result. On days near a
  new or full moon, activity probability during **minor** periods *rose*
  — moonrise 0.384 → 0.564, moonset 0.403 → 0.591 — while during
  **major** periods it *fell*: moon overhead 0.540 → 0.413 and underfoot
  0.516 → 0.305; far from a new or full moon the pattern reversed. The
  paper opens by stating deer activity patterns "are predominately
  crepuscular", and concludes that solunar events have "some association
  with deer activity. However, the relationships between lunar events and
  lunar phase expressed in solunar charts **may be misleading**."
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
- **Audit note:** both audits could reach only the abstract; the full
  text is paywalled. The odds ratios above appear in the abstract, so
  they are confirmed, but nothing beyond it has been checked. An
  Auburn-hosted PDF with a similar filename is a *different*, earlier
  Swartout paper — do not confuse the two.
- **Why the weights weren't changed:** the result is reported as odds of
  being "active", not as a movement rate, so there is no non-invented way
  to convert it into the yph currency every other weight uses.
  Additionally it disagrees with Sullivan et al. on the *direction* of
  the major-period effect, and a high-fenced, winter-only, 22-animal
  sample is the weakest basis for generalizing of the three. It does
  corroborate one thing the model already encodes: **major periods
  matter more than minor ones, and minor periods may be neutral or
  negative** — which is why `MINOR_YPH` is 0.0.

### Consulted — informs structure or known gaps

**Goethlich, J. 2019.** *Effects of Abiotic Factors on White-tailed Deer
Activity in South Carolina.* M.S. thesis, Auburn University (chair:
S.S. Ditchkoff). <https://etd.auburn.edu/handle/10415/7077>

- **Study design:** 116 GPS-collared adult white-tailed deer, 2009–2018,
  South Carolina; activity classified from interfix step length and
  turning angles; each abiotic factor modelled separately by logistic
  regression. **Audit note:** the thesis PDF could not be text-extracted
  by either audit, so the 116 / 2009–2018 figures rest on indexed
  abstract text; nothing found contradicts them.
- **What this project takes from it — now implemented:** the thesis
  abstract's conclusion, verbatim: *"responses to abiotic factors were
  typically less pronounced than circadian fluctuations in activity, and
  occurred most often during non-peak times of activity."* A summary of
  the same work adds: the authors were *"most likely to see a significant
  relationship between abiotic factors and activity during daytime and
  nighttime and least likely to see an effect in the morning and
  evening."* The first half supports capping weather below the
  daily-rhythm terms. The second half is why every weather term is now
  **damped by half inside the dawn/dusk halos** (term 6). **Webb et al.
  2010 found the same thing independently** (weather effects at 0100–0200
  and 1300, "hours of limited movements"), so this is two studies, not
  one — which is the bar this project set for a structural change.
- **Disagreement, reported honestly:** this thesis found that weather
  condition, temperature, wind speed, **barometric pressure**, moon
  phase, moon position and nocturnal brightness all "affected activity in
  some seasons and times of day." So neither pressure nor moon is
  universally inert — another reason both are downweighted rather than
  removed.
- **Not independent of Sullivan et al. 2016:** both studies come from the
  Auburn Deer Lab and both used deer at Brosnan Forest, South Carolina.
  Treat them as one study site, not two replications.

**Little, A.R., S.L. Webb, S. Demarais, K.L. Gee, S.K. Riffell, and J.A.
Gaskamp. 2016.** *Hunting intensity alters movement behaviour of
white-tailed deer.* Basic and Applied Ecology 17:360–369. DOI
10.1016/j.baae.2015.12.003. <https://doi.org/10.1016/j.baae.2015.12.003>

- **Study design:** 37 adult (≥2.5 yr) **male** deer, Love County,
  southern Oklahoma (Noble Foundation); GPS locations at 30-minute
  intervals under manipulated hunting-risk treatments (control / low /
  high) over 36 days.
- **What this project takes from it:** confirmation that **hunting
  pressure is a real movement driver** — movement was greater under risk
  treatments than controls, and it does report movement as a **rate
  (m/h)**, so a hunting-pressure term *could* be expressed in this
  model's currency. Not implemented, because the app has no data source
  for local hunting pressure, and Hunsaker et al. 2025 found no
  opening-weekend effect in Wisconsin, so a calendar proxy is not safe to
  assume. Listed as a known gap below.
- **Audit note (second pass):** an earlier README said this paper found
  "low hunting pressure produced no biologically significant change in
  *female* movement." The study animals were all male; that sentence was
  a sex misattribution and has been removed.
- **Correction note (first pass) — this is why the citation audit
  mattered.** An earlier version of this code cited "Little et al. 2016,
  *Effects of Weather on Habitat Selection and Movement of White-tailed
  Deer*" in support of the falling-pressure bonus. **That title does not
  exist.** The author/year are real but were attached to a fabricated
  title, and the actual Little et al. 2016 is the hunting-intensity paper
  above, which says nothing about barometric pressure. The false citation
  has been removed.

### Searched for and not found

The second research pass looked specifically for, and did not find:

- Any study reporting weather effects on deer movement as a **rate**
  (distance per time) with a usable sample size. The weather terms
  therefore remain judgment calls.
- Any newer or rate-based **solunar** test that could adjudicate between
  Neary et al. 2025 and the two conflicting odds-based studies.
- Any **meta-analysis or systematic review** of solunar-theory tests in
  any species.
- A peer-reviewed source for the "Saturday -22%, Sunday -34% daytime
  movement" hunting-pressure figures that circulate in hunting media
  (attributed to unpublished Auburn data). Not used.

## Known gaps and limitations

- **Hunting pressure is not modeled.** Little et al. 2016 establishes it
  matters and even reports a rate; the app has no data source for it,
  and Hunsaker et al. 2025 argues against a naive calendar proxy.
- **The dawn/dusk damping factor (0.5) is a judgment call.** Its
  *existence* rests on two independent studies; its *size* does not. If
  you read Goethlich and Webb as "weather does nothing at dawn and
  dusk", `WEATHER_CREPUSCULAR_DAMPING` is the single constant to raise
  toward 1.0.
- **The rain penalty's direction is unsupported.** The best storm data
  set available (Penn State) points both ways across its two years.
  Suppression during rain is the conventional assumption and is kept,
  at half the cold ceiling, but no located study establishes it.
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
```
