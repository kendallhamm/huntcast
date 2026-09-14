# Solunar Feeding Times

A standalone Streamlit app that forecasts solunar "feeding times" -
major/minor hunting & fishing activity windows - for any postal/zip
code worldwide.

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
Major/Minor windows, so it's easy to spot when a feeding period overlaps
or nearly overlaps one.

Computed locally with the [`ephem`](https://pypi.org/project/ephem/)
astronomy library (PyEphem) - no external solunar service, no network
call for the math itself.

On top of the daily feeding times, the app also cross-references an
hourly weather forecast to rank the best ~6-hour "hunting windows"
over the search period - rewarding strong solunar activity, colder
temperatures (deer move more in the cold), barometric pressure sitting
in a 29.8-30.3 inHg "sweet spot" band, and falling pressure ahead of an
approaching front, while penalizing high precipitation chance and wind.
Both pressure terms are read directly from hourly mean-sea-level
pressure data rather than inferred from a precipitation forecast. This
ranking is pure arithmetic - **no LLM is used anywhere in this app**, on
purpose, since it's public-facing and window selection needs to be
reproducible with no API cost or key exposure for other people's usage.

The app shows the ranked windows three ways: the top 3 as text, a
stacked bar chart of all candidates colored by *where* each score came
from (feeding window / weather / pressure drop), and an hourly overview
chart. The **"How the hunting-window score is calculated"** expander at
the bottom of the page documents every term of the formula, including
how the five scoring terms map onto the three chart colors.

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
4. Click **Get feeding times** to fetch the location, resolve its local
   timezone, and compute/display the forecast, one card per day.

## How it works

- **Geocoding**: postal code + country -> latitude/longitude via the
  free [Zippopotam.us](https://api.zippopotam.us/) API. No key required.
- **Timezone**: latitude/longitude -> IANA timezone name via
  [Open-Meteo](https://open-meteo.com/)'s `timezone=auto` parameter (the
  same free, keyless weather API - only the resolved timezone name is
  used here, not the weather data), so displayed times are correct local
  time for the looked-up location, not wherever the app happens to be
  running.
- **Solunar math**: `ephem`'s `Observer` class computes moon
  transit/antitransit/rising/setting and sun rising/setting for each
  day, searched independently per calendar day so one failed/missing
  event can't cascade into every later day being wrong.
- **Hunting-window ranking**: hourly temperature/precipitation
  chance/wind/barometric pressure for the search period
  ([Open-Meteo](https://open-meteo.com/)'s hourly forecast, same
  free/keyless service as the timezone lookup) is flattened into an
  hour-by-hour timeline alongside the solunar events, then every
  possible 6-hour window is scored on five terms:

  | # | Term | Effect |
  |---|---|---|
  | 1 | Solunar activity | Major periods weight 3x, Minor 1.5x, a sunrise/sunset in the window adds a flat +1.0 |
  | 2 | Cold-weather bonus | Grows the further average temp runs below 45°F, capped at +2.0 |
  | 3 | Rain/wind penalty | Subtracts for average precipitation chance and for wind above 10 mph |
  | 4 | Pressure sweet spot | Flat +1.0 when average pressure is between 29.8 and 30.3 inHg |
  | 5 | Falling pressure | Flat +1.5 for a drop of 0.4 inHg or more over the prior 24 hours; flat +1.0 for a drop of 0.2-0.4 inHg |

  Pressure is read as mean-sea-level pressure (`pressure_msl`) so it's
  comparable across elevations, and converted from hPa to inHg since
  that's how a barometer is normally read. The top 3 non-overlapping
  windows are shown as text, and all candidates are plotted as a
  stacked bar chart whose colors split each score into feeding-window
  activity, weather, and pressure drop. No AI narration anywhere - just
  the ranked facts. The in-app expander documents the full formula.

The geocoding, timezone, and hourly weather lookups are all cached
in-memory (`st.cache_data`, 1 hour TTL) so re-running a search for the
same location doesn't refetch immediately.

## Dependencies

**Standard library** (no install needed):

| Module | Used for |
|---|---|
| `datetime` | today's date, per-day search windows, formatting event times |
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
| Open-Meteo (`api.open-meteo.com`) | latitude/longitude -> IANA timezone name (via `timezone=auto`), plus the hourly forecast (temperature, precipitation chance, wind speed/direction, `pressure_msl`) that drives hunting-window ranking | public REST, no key |

## Files

```
solunar/
  solunar.py          <- the whole app (UI + geocoding + solunar math + weather scoring + charts)
  requirements.txt    <- streamlit, requests, ephem, pandas, altair, tzdata
  .gitignore          <- standard Python gitignore
  README.md           <- this file
```
