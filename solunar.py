"""
Standalone Streamlit app: solunar "feeding times" forecast (hunting/fishing
major/minor activity windows) for any postal/zip code worldwide.

Fully self-contained - no dependency on, or import of, anything outside
this folder. No API keys, no .env, no secrets: geocoding is a public
Zippopotam.us lookup, timezone resolution and the hourly weather forecast
are public Open-Meteo lookups, and the solunar math itself runs locally
via the `ephem` astronomy library - no external solunar service involved.

On top of the per-day feeding times, rut phase and the hourly forecast
(temperature, precipitation chance, wind, barometric pressure) are
cross-referenced against the solunar events to rank the best 6-hour
"hunting windows" - see the scoring section below for the formula.

Scoring weights are not hand-tuned. Every activity term is expressed in
the unit the underlying GPS-collar research measured it in (yards per
hour of daytime movement) and converted to points by one shared
constant, so the relative weighting of rut phase vs. dawn/dusk vs.
solunar periods reflects measured effect sizes rather than folklore. See
README.md for the full citation list and what each source contributes.

Run: streamlit run solunar.py
"""

import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import altair as alt
import ephem
import pandas as pd
import requests
import streamlit as st

# ---------------------------------------------------------------------------
# Geocoding (Zippopotam.us - free, no key)
# ---------------------------------------------------------------------------

ZIP_LOOKUP_URL = "https://api.zippopotam.us/{country}/{postcode}"

# Countries Zippopotam.us actually has postal-code data for - (code, name)
# pairs, transcribed from the "Countries Supported" table at
# https://www.zippopotam.us/ (fetched 2026-07-28). Deliberately not a
# generic ISO-3166 list: Zippopotam doesn't cover every country (most of
# Africa and much of Asia have no postal-code data there), and a code
# missing from this list will just 404 against the API. Re-check that
# page if a country you expect is missing - Zippopotam adds countries
# over time.
SUPPORTED_COUNTRIES = [
    ("AD", "Andorra"), ("AR", "Argentina"), ("AS", "American Samoa"),
    ("AT", "Austria"), ("AU", "Australia"), ("BD", "Bangladesh"),
    ("BE", "Belgium"), ("BG", "Bulgaria"), ("BR", "Brazil"),
    ("CA", "Canada"), ("CH", "Switzerland"), ("CZ", "Czech Republic"),
    ("DE", "Germany"), ("DK", "Denmark"), ("DO", "Dominican Republic"),
    ("ES", "Spain"), ("FI", "Finland"), ("FO", "Faroe Islands"),
    ("FR", "France"), ("GB", "Great Britain"), ("GF", "French Guyana"),
    ("GG", "Guernsey"), ("GL", "Greenland"), ("GP", "Guadeloupe"),
    ("GT", "Guatemala"), ("GU", "Guam"), ("GY", "Guyana"),
    ("HR", "Croatia"), ("HU", "Hungary"), ("IM", "Isle of Man"),
    ("IN", "India"), ("IS", "Iceland"), ("IT", "Italy"),
    ("JE", "Jersey"), ("JP", "Japan"), ("LI", "Liechtenstein"),
    ("LK", "Sri Lanka"), ("LT", "Lithuania"), ("LU", "Luxembourg"),
    ("MC", "Monaco"), ("MD", "Moldova"), ("MH", "Marshall Islands"),
    ("MK", "Macedonia"), ("MP", "Northern Mariana Islands"),
    ("MQ", "Martinique"), ("MX", "Mexico"), ("MY", "Malaysia"),
    ("NL", "Netherlands"), ("NO", "Norway"), ("NZ", "New Zealand"),
    ("PH", "Philippines"), ("PK", "Pakistan"), ("PL", "Poland"),
    ("PM", "Saint Pierre and Miquelon"), ("PR", "Puerto Rico"),
    ("PT", "Portugal"), ("RE", "French Reunion"), ("RU", "Russia"),
    ("SE", "Sweden"), ("SI", "Slovenia"),
    ("SJ", "Svalbard & Jan Mayen Islands"), ("SK", "Slovak Republic"),
    ("SM", "San Marino"), ("TH", "Thailand"), ("TR", "Turkey"),
    ("US", "United States"), ("VA", "Vatican"), ("VI", "Virgin Islands"),
    ("YT", "Mayotte"), ("ZA", "South Africa"),
]


@st.cache_data(ttl=3600, show_spinner=False)
def lookup_location(postcode, country_code):
    """Postal code -> {'lat', 'lon', 'place', 'state'}, or None if not
    found or the lookup fails."""
    try:
        resp = requests.get(
            ZIP_LOOKUP_URL.format(country=country_code.strip().lower(), postcode=postcode.strip()),
            timeout=8,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        places = data.get("places") or []
        if not places:
            return None
        place = places[0]
        return {
            "lat": float(place["latitude"]),
            "lon": float(place["longitude"]),
            "place": place.get("place name"),
            "state": place.get("state abbreviation") or country_code.strip().upper(),
        }
    except (requests.RequestException, KeyError, ValueError):
        return None


@st.cache_data(ttl=3600, show_spinner=False)
def lookup_timezone(lat, lon):
    """Resolve the IANA timezone name for (lat, lon) via Open-Meteo's
    timezone="auto" (free, no key) - the actual forecast data is
    discarded, only the resolved zone name is used, so the solunar times
    below land on that location's real local clock instead of always
    whatever timezone this app happens to be running in. Falls back to
    "UTC" on any failure."""
    try:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "daily": "sunrise", "timezone": "auto", "forecast_days": 1,
            },
            timeout=8,
        )
        resp.raise_for_status()
        return resp.json().get("timezone") or "UTC"
    except requests.RequestException:
        return "UTC"


# ---------------------------------------------------------------------------
# Solunar calculation
#
# Solunar theory (John Alden Knight, 1926) predicts four daily windows of
# elevated wildlife feeding activity based on the moon's position:
#   - 2 Major periods (~2 hrs each), centered on moon transit (directly
#     overhead) and antitransit ("underfoot")
#   - 2 Minor periods (~1 hr each), centered on moonrise and moonset
#
# Computed locally with `ephem` (PyEphem) - no external API, no network
# call. ephem's Observer class has next_transit()/next_antitransit()/
# next_rising()/next_setting(), which map directly onto those four events.
# ---------------------------------------------------------------------------

MAJOR_HALF_WINDOW = timedelta(minutes=60)
MINOR_HALF_WINDOW = timedelta(minutes=30)
UTC = ZoneInfo("UTC")


def _observer(lat, lon):
    obs = ephem.Observer()
    obs.lat = str(lat)
    obs.lon = str(lon)
    obs.elevation = 0
    return obs


def _to_local(ephem_date, tz):
    """Convert an ephem.Date (always UTC) to a naive datetime in `tz`."""
    return ephem_date.datetime().replace(tzinfo=UTC).astimezone(tz).replace(tzinfo=None)


def _format_time(dt):
    hour_12 = dt.strftime("%I").lstrip("0") or "12"
    return f"{hour_12}:{dt.strftime('%M %p')}"


def _day_label(dt, idx):
    return "Today" if idx == 0 else f"{dt.strftime('%a')} {dt.month}/{dt.day}"


def fetch_solunar(lat, lon, tz, days):
    """Return a list of `days` entries, one per local calendar day
    starting today:
      {'label': str, 'periods': [{'kind': 'Major'|'Minor'|'Sunrise'|'Sunset',
                                   'start': dt, 'end': dt}, ...]}
    'periods' is sorted chronologically, with Sunrise/Sunset interleaved
    among the Major/Minor windows so it's obvious whether a feeding
    period falls near sunrise/sunset. Sunrise/Sunset are single points in
    time (start == end). Times are naive datetimes in `tz`.

    Each day is computed independently, searching forward from that
    day's own local midnight, rather than chaining from the previous
    day's last event - so a missing/failed event on one day can't
    cascade into every later day being wrong. Because the moon's cycle
    (~24h50m) runs longer than a calendar day, an event found searching
    from day N's midnight can itself land just after day N+1's midnight -
    in which case it's dropped here (day N+1's own search independently
    finds the same event and correctly attributes it there), otherwise
    the same event would show up twice.
    """
    now_local = datetime.now(tz).replace(tzinfo=None)
    today_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

    results = []
    for i in range(days):
        day_start_local = today_local + timedelta(days=i)
        moon = ephem.Moon()
        sun = ephem.Sun()
        obs = _observer(lat, lon)
        obs.date = day_start_local.replace(tzinfo=tz).astimezone(UTC).replace(tzinfo=None)

        periods = []
        for kind, body, finder, half_window in (
            ("Major", moon, obs.next_transit, MAJOR_HALF_WINDOW),
            ("Major", moon, obs.next_antitransit, MAJOR_HALF_WINDOW),
            ("Minor", moon, obs.next_rising, MINOR_HALF_WINDOW),
            ("Minor", moon, obs.next_setting, MINOR_HALF_WINDOW),
            ("Sunrise", sun, obs.next_rising, timedelta(0)),
            ("Sunset", sun, obs.next_setting, timedelta(0)),
        ):
            try:
                event_local = _to_local(finder(body), tz)
            except (ephem.CircumpolarError, ValueError):
                # Not expected at most latitudes, but don't let one
                # failed event blank out the rest of the day.
                continue
            if event_local.date() != day_start_local.date():
                # Belongs to the next day's search instead - see docstring.
                continue
            periods.append({
                "kind": kind,
                "start": event_local - half_window,
                "end": event_local + half_window,
            })

        periods.sort(key=lambda p: p["start"])
        results.append({"label": _day_label(day_start_local, i), "periods": periods})

    return results


_KIND_EMOJI = {"Major": ":full_moon:", "Minor": ":waxing_gibbous_moon:", "Sunrise": ":sunrise:", "Sunset": ":city_sunset:"}


# ---------------------------------------------------------------------------
# Activity scoring
#
# Cross-references the solunar events above against rut phase and an
# hourly weather forecast to surface the best ~6-hour "hunting window"
# per search. Window selection and ranking are deterministic arithmetic,
# so the same inputs always produce the same ranking.
#
# WEIGHTING METHOD. Every term below is expressed in the unit the
# underlying research measured - yards per hour (yph) of excess daytime
# buck movement - and a window's score is the MEAN excess yph across its
# hours, converted to points by one shared constant (POINTS_PER_YPH). A
# term's weight is therefore not a hand-tuned number; it is whatever the
# study measured, which makes the relative ordering of rut / dawn-dusk /
# solunar auditable rather than a matter of taste.
#
# Taking the mean (rather than a sum) is what makes terms with different
# durations commensurable. Rut elevates movement across every hour of a
# window; dawn/dusk elevates roughly two of six. At the same yph, the
# all-hours effect is correctly worth 3x the two-hour one - which only
# falls out if both are averaged over the same window. Summing the
# short-duration terms while averaging the long-duration ones silently
# inflates the short ones by a factor of WINDOW_HOURS.
#
# The effect sizes come from Neary, N., B. Strickland, L. Resop,
# S. Demarais, and W. McKinley. 2025. "Lunar Legends: Does the Moon
# Influence Buck Activity?" Mississippi State University Extension
# Publication 4068. That study GPS-collared 48 bucks in central
# Mississippi at 15-minute fixes from September through February over two
# years, and reported every effect as a change in daytime yards travelled
# per hour against a season mean of 269 yph - which is exactly the shape
# of number this scoring model needs.
#
# The *weather* terms below could not be calibrated the same way, because
# no study located reports weather effects as a movement-rate delta.
# Those terms are explicitly labelled as judgment calls and are bounded
# by what the weather literature does support (see each constant).
# ---------------------------------------------------------------------------

WINDOW_HOURS = 6

# Peak rut (+142 yph) was the largest effect Neary et al. 2025 measured.
# Anchor it at 3.0 points; every other activity term scales from there.
POINTS_PER_YPH = 3.0 / 142.0

# Dawn/dusk. Neary et al. 2025: within one hour of sunrise or sunset
# bucks averaged 317 yph against the 269 yph season mean, i.e. +48 yph,
# and were bedded 21% of the time vs. 34% overall. This is the most
# consistently replicated daily-timing effect in the literature - Webb et
# al. 2010 reach the same conclusion from an independent 7-year Oklahoma
# data set, concluding that routine crepuscular movement, not weather or
# moon, is the dominant driver of fine-scale deer movement. Scored over a
# +/-1 hour halo around each event, matching how Neary et al. defined it.
CREPUSCULAR_YPH = 48.0
CREPUSCULAR_HALF_WINDOW = timedelta(minutes=60)

# Solunar periods. Neary et al. 2025 tested these directly, comparing each
# buck against his *own* usual movement at the same time of day (which
# nets out rut phase and individual personality): major periods (moon
# overhead/underfoot) came out at +3 yph, minor periods (moonrise/moonset)
# at -0.1 yph. Both are indistinguishable from zero next to a 269 yph
# baseline, and moon phase, phase x position combinations, perigee/apogee
# and dawn-dusk x moon combinations were all null too. Webb et al. 2010
# independently found no moon-phase effect on daily, nocturnal or diurnal
# movement.
#
# Two other studies test solunar directly and disagree with Neary et al.
# AND with each other, which is the real reason these weights are near
# zero - the effect does not replicate, in either direction:
#   - Sullivan et al. 2016 (38 bucks, South Carolina): near a new/full
#     moon, MINOR-period activity probability rose (moonrise 0.384 ->
#     0.564, moonset 0.403 -> 0.591) while MAJOR-period activity FELL
#     (overhead 0.540 -> 0.413, underfoot 0.516 -> 0.305).
#   - Swartout and Ditchkoff 2025 (22 bucks, high-fenced Alabama): the
#     opposite sign on majors - top-rated days gave 3.02x and 2.83x
#     activity odds during moon underfoot/overhead, but only 0.30x and
#     0.37x at moonrise/moonset.
# Swartout and Ditchkoff is the strongest pro-solunar result located, and
# it is not reflected here only because it reports odds of being "active"
# rather than a movement rate, so converting it into this model's yph
# currency would mean inventing a conversion. If solunar should weigh
# more, MAJOR_YPH is the one constant to raise - see README.
#
# What all three do agree on: majors matter more than minors, and minors
# are neutral-to-negative. Hence MINOR_YPH = 0.
#
# Net effect: solunar windows are still computed and displayed (they are
# what this app is for), but they barely move the ranking, because that
# is what the measurements support.
MAJOR_YPH = 3.0
MINOR_YPH = 0.0

# Rut phase. The single largest effect on fall deer movement, and the one
# the previous version of this model was missing entirely. Values are the
# daytime movement deltas against the season mean reported by Neary et
# al. 2025 (their "All data" series), keyed by day offset from peak
# breeding. Neary et al. space their phases 14 days apart (Mississippi
# pre-rut Nov 27 / early Dec 11 / peak Dec 25 / late Jan 8 / post Jan 22),
# so each phase is treated as a 14-day band centered on its named day.
#
# Caveat carried into the UI: these are *buck* movement rates, and the
# phase offsets are relative to a peak breeding date the user supplies,
# because peak rut date is regionally idiosyncratic rather than a clean
# function of latitude (Pennsylvania peaks mid-November per the PA Game
# Commission's fetal-aging data, southwest Wisconsin Oct 23-Nov 12 per
# Hunsaker et al. 2025 despite being *further north*, and central
# Mississippi Dec 25 per Neary et al. 2025). See
# RUT_PEAK_DEFAULT_MONTH_DAY below.
RUT_PHASE_YPH = [
    ("Pre-rut", -35, -21, 4.0),
    ("Early rut", -21, -7, 104.0),
    ("Peak rut", -7, 7, 142.0),
    ("Late rut", 7, 21, 78.0),
    ("Post-rut", 21, 35, 9.0),
]
# Neary et al. 2025's "No Rut" series measured -40 yph against the season
# mean - but that mean is pulled up by the rut days themselves, so scoring
# every non-rut day against it makes ordinary daytime movement look like a
# penalty. This app ranks windows within a several-day forecast, not
# against the whole season, so "no measured rut elevation" is scored as
# neutral (0) rather than as a deficit: nothing is subtracted for a day
# simply because it falls outside the rut bands above. The -40 figure is
# still true of the underlying data and is cited in the docs, it's just
# not what gets scored.
NO_RUT_YPH = 0.0

# Default peak breeding date offered in the UI. November 15 is the
# best-supported anchor for the East Coast band this app is tuned for:
# the Pennsylvania Game Commission aged fetuses from 6,000+ road-killed
# does (2000-2007) and reports peak breeding by adult does in
# mid-November, and the Penn State Deer-Forest Study, working from the
# same data set, reports half of does bred by November 13. (A more
# precise "November 13-17" window circulates in the hunting press but
# could not be traced to any Penn State primary source, so it is not
# relied on.) Users elsewhere should override it - local wildlife agency
# conception data beats any formula this app could apply.
RUT_PEAK_DEFAULT_MONTH_DAY = (11, 15)

# --- Weather terms -------------------------------------------------------
#
# None of these are calibrated to a measured movement-rate delta, because
# no located study publishes weather effects as a movement-rate change
# (a literature search through 2026 did not turn one up). They are
# bounded judgment calls, expressed in the same yph currency as
# everything above so the bounds are legible, and sized by evidential
# standing: a term's ceiling scales with how often the fine-scale GPS
# literature actually detected it.
#
# The bound for the best-supported weather term is: at full strength,
# contribute no more to a window than a single dawn or dusk does.
# Dawn/dusk is +48 yph over roughly 2 of a window's 6 hours, i.e. ~16 yph
# averaged across the window - so 16 yph is the ceiling for a weather
# term that applies to *every* hour.
#
# A second, independent sanity bound: Webb et al. 2010's weather
# parameter estimates never exceeded ~29 m/h (~32 yph), and the authors
# attribute even that partly to collar error. The largest combined
# weather effect this block can produce (about +21 / -16 yph) sits
# inside it.
WEATHER_BOUND_YPH = CREPUSCULAR_YPH * 2 / WINDOW_HOURS

# Cold. Temperature is the one weather variable with consistent support:
# Webb et al. 2010 found general linear trends for weather in only 8 of
# 80 models (10%), and temperature accounted for 5 of those 8 - rain,
# relative humidity and wind speed took 1 each, which leaves barometric
# pressure as the only one of their five weather variables with no linear
# trend at all. Cold therefore gets the full bound.
#
# Scored as a departure below the location's own recent normal for that
# hour of day rather than against a fixed degree threshold: a 38F morning
# means something very different in Maine than in Georgia, and deer
# respond to change from what they are acclimated to. The normal is built
# from the trailing PAST_DAYS_LOOKBACK days of observations for the same
# hour of day, so a daytime window is compared against daytime history.
# COLD_ANOMALY_SCALE degrees below normal earns the full effect.
COLD_ANOMALY_SCALE = 15.0
COLD_MAX_YPH = WEATHER_BOUND_YPH

# Precipitation and wind. Held to HALF the bound, and at the same tier as
# each other, because they have the same evidential standing: each was
# significant in exactly 1 of Webb et al. 2010's 8 significant models.
# An earlier version gave rain the full bound on the strength of the
# Penn State Deer-Forest Study's storm analysis "finding deer moved less
# during storms" - re-reading that source, its two years point in
# opposite directions (2016: 102 yph outside storms vs 113 during; 2017:
# 111 vs 98) and it concludes there was no significant effect, so it
# supports neither the size nor the direction of a rain penalty. The
# penalty's *direction* is a judgment call. Wind additionally doesn't
# engage at all below the threshold.
PRECIP_MAX_PENALTY_YPH = WEATHER_BOUND_YPH / 2
WIND_PENALTY_THRESHOLD_MPH = 15.0
WIND_PENALTY_FULL_MPH = 40.0
WIND_MAX_PENALTY_YPH = WEATHER_BOUND_YPH / 2

# Weather damping inside dawn/dusk halos. Two studies independently found
# that weather effects concentrate in NON-peak hours: Goethlich 2019
# (116 collared deer, South Carolina) was "most likely to see a
# significant relationship between abiotic factors and activity during
# daytime and nighttime and least likely to see an effect in the morning
# and evening", and Webb et al. 2010's weather effects surfaced at
# 0100-0200 and 1300 - "hours of limited movements" - not at dawn or
# dusk. Hunsaker et al. 2025 (188 bucks, Wisconsin) found no weather
# effect at all on rut-period movement. So every weather term above is
# multiplied by (1 - DAMPING x crepuscular coverage) hour by hour: an
# hour fully inside a sunrise/sunset halo carries half weight, an hour
# outside carries full weight. The size (0.5) is a judgment call - the
# sources say "least likely" and "less pronounced", not "absent" - and
# is the one constant to change if you read them more strongly.
WEATHER_CREPUSCULAR_DAMPING = 0.5

# Barometric pressure. Downweighted hard, and split by what the evidence
# actually distinguishes: a CHANGE in pressure vs. a static LEVEL.
#
# Falling pressure has a sliver of support - Webb et al. 2010's separate
# day-over-day analysis (weather *changes*, 10 of 80 models significant)
# attributed 3 of those 10 to pressure, and Goethlich 2019 found pressure
# affected activity in some seasons and times of day. Against that, Webb
# et al.'s within-day analysis found pressure was the only one of five
# weather variables with no linear trend, and the Penn State Deer-Forest
# Study found "no statistical or biological significance" of oncoming
# storms on collared deer (before/during/after/control rates all within
# ~94-113 yph, with the two years disagreeing on direction). Small and
# unproven rather than disproven, so the falling-pressure term is kept
# at token weight: half the ~10 yph spread across the Penn State
# conditions, which is itself an upper bound on an effect that study
# could not detect. The pressure trend is also genuinely informative to
# display.
#
# The 29.8-30.3 inHg "sweet spot" band traces to a hunting-magazine rule
# of thumb; no located study tests a static pressure level, and Webb et
# al.'s within-day null is the closest thing to a test. Its weight is
# therefore 0 - it stays in the code as a single constant to raise if
# evidence ever appears, and the band is still reported in the window
# text for hunters who track it.
HPA_PER_INHG = 33.8639

PRESSURE_DROP_LOOKBACK_HOURS = 24
PRESSURE_DROP_MINOR_THRESHOLD_IN = 0.2
PRESSURE_DROP_MINOR_YPH = 2.5
PRESSURE_DROP_THRESHOLD_IN = 0.4
PRESSURE_DROP_YPH = 5.0

PRESSURE_BAND_LOW_IN = 29.8
PRESSURE_BAND_HIGH_IN = 30.3
PRESSURE_BAND_YPH = 0.0

# How many top-scoring, non-overlapping windows to search for; the UI
# text list only shows the top 3 of these, but the score bar chart plots
# all of them.
CANDIDATE_WINDOW_COUNT = 15

# Open-Meteo's hourly forecast rejects forecast_days > 16 (HTTP 400), but
# the "Days" slider below goes up to 30 so solunar-only (moon/sun) times
# can still be shown further out via ephem, which has no such cap.
# Clamping here keeps the weather/ranking feature working for the first
# 16 days instead of failing outright the moment someone picks >16.
OPEN_METEO_MAX_FORECAST_DAYS = 16

# Days of *past* hourly data to request alongside the forecast. Serves two
# purposes: it supplies the per-hour temperature normals the cold-anomaly
# term is measured against, and it gives the 24-hour pressure lookback
# real history for windows early in the forecast (which previously scored
# no pressure trend at all, since nothing preceded them).
PAST_DAYS_LOOKBACK = 7


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_hourly_weather(lat, lon, days):
    """Hourly temp/precip-chance/wind speed+direction/barometric pressure
    for `days` days at (lat, lon), via Open-Meteo (free, no key) - same
    service and timezone="auto" resolution as lookup_timezone() above, so
    these hours line up with the days_data fetch_solunar() produces for
    the same location. Returns a list of {'dt', 'temp_f', 'precip_chance',
    'wind_mph', 'wind_dir_deg', 'pressure_inhg'} dicts (naive local
    datetimes), one per hour, or None on failure. Open-Meteo only reports
    pressure in hPa (no unit param like temperature/wind have), so it's
    converted to inches of mercury here to match how a barometer is
    normally read.

    Includes PAST_DAYS_LOOKBACK days of history before today as well as
    the forecast, so the cold-anomaly baseline and the 24-hour pressure
    lookback have something to measure against."""
    try:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "hourly": "temperature_2m,precipitation_probability,wind_speed_10m,wind_direction_10m,pressure_msl",
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
                "timezone": "auto",
                "forecast_days": min(days, OPEN_METEO_MAX_FORECAST_DAYS),
                "past_days": PAST_DAYS_LOOKBACK,
            },
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
        hourly = data.get("hourly", {})
        times = hourly.get("time") or []
        temps = hourly.get("temperature_2m") or []
        precips = hourly.get("precipitation_probability") or []
        winds = hourly.get("wind_speed_10m") or []
        wind_dirs = hourly.get("wind_direction_10m") or []
        pressures = hourly.get("pressure_msl") or []
        return [
            {
                "dt": datetime.fromisoformat(t),
                "temp_f": temps[i] if i < len(temps) else None,
                "precip_chance": precips[i] if i < len(precips) else None,
                "wind_mph": winds[i] if i < len(winds) else None,
                "wind_dir_deg": wind_dirs[i] if i < len(wind_dirs) else None,
                "pressure_inhg": (pressures[i] / HPA_PER_INHG) if i < len(pressures) and pressures[i] is not None else None,
            }
            for i, t in enumerate(times)
        ]
    except (requests.RequestException, ValueError, KeyError):
        return None


COMPASS_POINTS = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]


def _compass_direction(degrees):
    """Convert a wind-direction-from bearing in degrees to a 16-point
    compass label (e.g. 270 -> "W")."""
    return COMPASS_POINTS[round(degrees / 22.5) % 16]


def _circular_mean_deg(degrees, weights=None):
    """Average compass bearings correctly - a naive mean of e.g. [350,
    10] gives 180 (due south) instead of 0 (due north), since bearings
    wrap at 360. Averaged as unit vectors instead, optionally weighted
    (by wind speed, so calmer/noisier hours don't skew the direction as
    much as hours where the wind is actually blowing meaningfully).
    Returns None for an empty input or if the vectors cancel out
    exactly."""
    if not degrees:
        return None
    if weights is None:
        weights = [1.0] * len(degrees)
    sin_sum = sum(w * math.sin(math.radians(d)) for d, w in zip(degrees, weights))
    cos_sum = sum(w * math.cos(math.radians(d)) for d, w in zip(degrees, weights))
    if sin_sum == 0 and cos_sum == 0:
        return None
    return math.degrees(math.atan2(sin_sum, cos_sum)) % 360


def _summarize_weather(samples):
    """Average temp/wind speed/wind direction, peak precip chance
    across a set of hourly samples - None if there's no weather data at
    all (rather than a dict of Nones), so callers can tell "no data"
    apart from "data says calm and dry."."""
    if not samples:
        return None
    temps = [s["temp_f"] for s in samples if s["temp_f"] is not None]
    precips = [s["precip_chance"] for s in samples if s["precip_chance"] is not None]
    winds = [s["wind_mph"] for s in samples if s["wind_mph"] is not None]
    pressures = [s["pressure_inhg"] for s in samples if s.get("pressure_inhg") is not None]
    wind_dir_pairs = [
        (s["wind_dir_deg"], s["wind_mph"]) for s in samples
        if s.get("wind_dir_deg") is not None and s.get("wind_mph") is not None
    ]
    return {
        "temp_avg": round(sum(temps) / len(temps)) if temps else None,
        "precip_max": max(precips) if precips else None,
        "wind_avg": round(sum(winds) / len(winds), 1) if winds else None,
        "pressure_avg": round(sum(pressures) / len(pressures), 2) if pressures else None,
        "wind_dir_deg": (
            _circular_mean_deg([d for d, _ in wind_dir_pairs], [w for _, w in wind_dir_pairs])
            if wind_dir_pairs else None
        ),
    }


def rut_phase(day, peak_date):
    """(phase label, measured yph delta) for calendar date `day`, based
    on its offset from `peak_date`. See RUT_PHASE_YPH for provenance."""
    offset = (day - peak_date).days
    for label, start_offset, end_offset, yph in RUT_PHASE_YPH:
        if start_offset <= offset < end_offset:
            return label, yph
    return "Outside rut", NO_RUT_YPH


def _hourly_temp_normals(samples, now_local):
    """Mean temperature per hour-of-day (0-23) across the *trailing*
    samples, i.e. the PAST_DAYS_LOOKBACK days of observations that
    precede `now_local`. This is what the cold term measures a window
    against, so "cold" means colder than this location has actually been
    lately at this time of day, rather than colder than a fixed degree
    threshold that can't mean the same thing in Maine and Georgia.

    Bucketing by hour-of-day matters: a flat multi-day mean folds nights
    in with days, which would make every daytime window look warm than
    normal and every night window look cold. Falls back to the full
    sample set if no history came back, so the term still works (against
    a weaker baseline) when Open-Meteo returns forecast hours only."""
    def buckets_from(pool):
        out = {}
        for s in pool:
            if s["temp_f"] is not None:
                out.setdefault(s["dt"].hour, []).append(s["temp_f"])
        return out

    buckets = buckets_from([s for s in samples if s["dt"] < now_local])
    if not buckets:
        buckets = buckets_from(samples)
    return {hour: sum(temps) / len(temps) for hour, temps in buckets.items()}


# Per-kind scoring halo and measured effect. Sunrise/Sunset are stored as
# instantaneous events for display, but Neary et al. 2025 measured the
# crepuscular effect over a +/-1 hour band around each, so that is the
# band scored here.
_SCORE_HALF_WINDOW = {
    "Major": MAJOR_HALF_WINDOW,
    "Minor": MINOR_HALF_WINDOW,
    "Sunrise": CREPUSCULAR_HALF_WINDOW,
    "Sunset": CREPUSCULAR_HALF_WINDOW,
}
_KIND_YPH = {
    "Major": MAJOR_YPH,
    "Minor": MINOR_YPH,
    "Sunrise": CREPUSCULAR_YPH,
    "Sunset": CREPUSCULAR_YPH,
}


def build_hourly_timeline(days_data, samples, tz, rut_peak_date):
    """Flatten fetch_solunar()'s days_data and fetch_hourly_weather()'s
    samples into one hour-by-hour timeline covering the whole forecast,
    so the sliding window search below can start at ANY hour instead of
    a fixed per-day grid. Both inputs share the same "local midnight
    today" anchor (both derive "today" from the same `tz`), so hours
    line up without extra conversion.

    Returns a list of dicts, one per hour in chronological order starting
    at local midnight today:
      'dt'            - the hour's start
      'weather'       - summarized hourly weather, or None
      'activity'      - solunar + crepuscular points for this hour
      'crepuscular'   - fraction (0-1) of this hour inside a sunrise or
                        sunset halo; damps the weather terms
      'rut'           - rut-phase points for this hour's date
      'rut_label'     - the phase name behind 'rut'
      'temp_anomaly'  - degrees F below this hour-of-day's recent normal
                        (positive = colder than normal), or None
      'pressure_drop' - inHg fall over the preceding
                        PRESSURE_DROP_LOOKBACK_HOURS (positive =
                        falling), or None
      'events'        - the solunar/sun events touching this hour

    Per-hour activity is overlap-weighted: a period spanning parts of two
    hours contributes proportionally to each rather than double-counting
    or arbitrarily picking one. Weights are the measured yph effects
    converted by POINTS_PER_YPH, so an hour fully inside a Major period
    is worth MAJOR_YPH * POINTS_PER_YPH.

    `samples` may (and normally does) extend PAST_DAYS_LOOKBACK days
    before today; those hours never become timeline entries, but they do
    feed the temperature normals and the pressure lookback."""
    now_local = datetime.now(tz).replace(tzinfo=None)
    today_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

    samples_by_hour = {s["dt"].replace(minute=0, second=0, microsecond=0): s for s in samples}
    temp_normals = _hourly_temp_normals(samples, now_local)
    all_periods = [p for day in days_data for p in day["periods"]]

    timeline = []
    for h in range(len(days_data) * 24):
        hour_start = today_local + timedelta(hours=h)
        hour_end = hour_start + timedelta(hours=1)

        activity = 0.0
        crepuscular = 0.0
        events = []
        for p in all_periods:
            # Both point events (Sunrise/Sunset) and windowed ones
            # (Major/Minor) are scored from their center outward, so the
            # displayed period stays independent of the scored halo.
            center = p["start"] + (p["end"] - p["start"]) / 2
            half = _SCORE_HALF_WINDOW.get(p["kind"], timedelta(0))
            overlap_hours = (
                min(center + half, hour_end) - max(center - half, hour_start)
            ).total_seconds() / 3600
            if overlap_hours > 0:
                activity += _KIND_YPH.get(p["kind"], 0.0) * POINTS_PER_YPH * overlap_hours
                if p["kind"] in ("Sunrise", "Sunset"):
                    crepuscular += overlap_hours
                events.append(p)
        # Fraction of this hour inside a dawn/dusk halo (the two halos
        # can only overlap at extreme latitudes, hence the clamp). This
        # is what the weather terms are damped by - see
        # WEATHER_CREPUSCULAR_DAMPING.
        crepuscular = min(1.0, crepuscular)

        rut_label, rut_yph = rut_phase(hour_start.date(), rut_peak_date)

        sample = samples_by_hour.get(hour_start)
        normal = temp_normals.get(hour_start.hour)
        temp_anomaly = None
        if sample and sample["temp_f"] is not None and normal is not None:
            temp_anomaly = normal - sample["temp_f"]

        timeline.append({
            "dt": hour_start,
            "weather": _summarize_weather([sample]) if sample else None,
            "activity": activity,
            "crepuscular": crepuscular,
            "rut": rut_yph * POINTS_PER_YPH,
            "rut_label": rut_label,
            "temp_anomaly": temp_anomaly,
            "pressure_drop": _pressure_drop_in(samples_by_hour, hour_start),
            "events": events,
        })

    return timeline


def _pressure_drop_in(samples_by_hour, hour_start):
    """Pressure fall (inHg) over the PRESSURE_DROP_LOOKBACK_HOURS before
    `hour_start` - positive means falling pressure, negative means
    rising. None if there's no pressure reading at either end.

    Keyed by datetime rather than timeline index so it can reach into the
    PAST_DAYS_LOOKBACK days of history that precede the timeline; the
    previous index-based version silently scored no pressure trend for
    every window in the first 24 hours of the forecast."""
    before = samples_by_hour.get(hour_start - timedelta(hours=PRESSURE_DROP_LOOKBACK_HOURS))
    now = samples_by_hour.get(hour_start)
    if not before or not now:
        return None
    if before["pressure_inhg"] is None or now["pressure_inhg"] is None:
        return None
    return before["pressure_inhg"] - now["pressure_inhg"]


def _pressure_drop_yph(pressure_drop):
    """Tiered falling-pressure effect in yph for a pressure_drop (inHg,
    from _pressure_drop_in()) - 0.0 if None or below the minor
    threshold. Shared by _hour_weather_yph() (scored per hour) and
    format_window() (which reports the window-start reading) so the
    tiers behind the displayed label are the ones actually scored."""
    if pressure_drop is None:
        return 0.0
    if pressure_drop >= PRESSURE_DROP_THRESHOLD_IN:
        return PRESSURE_DROP_YPH
    if pressure_drop >= PRESSURE_DROP_MINOR_THRESHOLD_IN:
        return PRESSURE_DROP_MINOR_YPH
    return 0.0


CATEGORY_ACTIVITY = "Daily Activity"
CATEGORY_RUT = "Rut Phase"
CATEGORY_WEATHER = "Weather"


def _hour_weather_yph(hour):
    """Net weather effect for one timeline hour, in yph, or None if the
    hour has no weather data at all. Cold bonus and pressure bonuses,
    minus the rain and wind penalties, then damped by how much of the
    hour sits inside a dawn/dusk halo (see WEATHER_CREPUSCULAR_DAMPING).

    Scored per hour rather than from window averages so that the
    damping can be applied to exactly the hours it belongs to, and so
    that the weather term is the same "mean over the window's hours"
    shape as the activity and rut terms."""
    weather = hour["weather"]
    if not weather:
        return None

    yph = 0.0
    if hour["temp_anomaly"] is not None:
        yph += COLD_MAX_YPH * min(1.0, max(0.0, hour["temp_anomaly"] / COLD_ANOMALY_SCALE))

    if weather["precip_max"] is not None:
        yph -= PRECIP_MAX_PENALTY_YPH * weather["precip_max"] / 100.0

    if weather["wind_avg"] is not None:
        wind_span = WIND_PENALTY_FULL_MPH - WIND_PENALTY_THRESHOLD_MPH
        yph -= WIND_MAX_PENALTY_YPH * min(
            1.0, max(0.0, (weather["wind_avg"] - WIND_PENALTY_THRESHOLD_MPH) / wind_span)
        )

    pressure = weather["pressure_avg"]
    if pressure is not None and PRESSURE_BAND_LOW_IN <= pressure <= PRESSURE_BAND_HIGH_IN:
        yph += PRESSURE_BAND_YPH

    yph += _pressure_drop_yph(hour["pressure_drop"])

    return yph * (1.0 - WEATHER_CREPUSCULAR_DAMPING * hour["crepuscular"])


def score_breakdown(timeline, start_idx, window_hours=WINDOW_HOURS):
    """Same validity rules as score_window(), but returns the individual
    named terms that sum to the total score, so callers (the stacked
    score bar chart) can show where a window's score actually comes
    from. Returns None if the window would run past the end of the
    timeline, or if it has neither solunar activity nor any weather data
    at all. Otherwise a dict:
      CATEGORY_ACTIVITY - dawn/dusk + solunar
      CATEGORY_RUT      - rut phase
      CATEGORY_WEATHER  - cold-anomaly bonus + pressure bonuses, net of
                          the precipitation/wind penalty, damped at
                          dawn/dusk (can be negative)
      'total'           - sum of the three above

    Every term is the window's MEAN excess movement rate in yph times
    POINTS_PER_YPH - see the WEIGHTING METHOD note above for why the mean
    is what makes a two-hour effect and an all-day effect comparable.
    The weather mean is taken over the hours that have weather data, so
    a partially-covered window is not diluted by its blank hours.
    """
    hours = timeline[start_idx:start_idx + window_hours]
    if len(hours) < window_hours:
        return None
    if not any(h["events"] for h in hours) and not any(h["weather"] for h in hours):
        return None

    activity = sum(h["activity"] for h in hours) / len(hours)
    rut = sum(h["rut"] for h in hours) / len(hours)

    hourly_weather = [w for w in (_hour_weather_yph(h) for h in hours) if w is not None]
    weather_yph = sum(hourly_weather) / len(hourly_weather) if hourly_weather else 0.0
    weather = weather_yph * POINTS_PER_YPH

    return {
        CATEGORY_ACTIVITY: activity,
        CATEGORY_RUT: rut,
        CATEGORY_WEATHER: weather,
        "total": activity + rut + weather,
    }


def score_window(timeline, start_idx, window_hours=WINDOW_HOURS):
    """Combined goodness score for timeline[start_idx:start_idx+window_hours]
    - the 'total' from score_breakdown(), or None under the same
    conditions score_breakdown() returns None."""
    breakdown = score_breakdown(timeline, start_idx, window_hours)
    return breakdown["total"] if breakdown else None


def find_candidate_windows(timeline, now_local, top_n=CANDIDATE_WINDOW_COUNT, window_hours=WINDOW_HOURS):
    """Slide a `window_hours`-wide window across EVERY possible starting
    hour in `timeline` and return the `top_n` best-scoring,
    non-overlapping windows, highest score first. A window that has
    already fully elapsed (its end is at or before `now_local`) is never
    a candidate. Non-overlap is enforced greedily (best score first,
    skip anything sharing an hour with an already-picked window) so two
    windows that are really "the same" opportunity shifted by an hour
    don't crowd out genuine variety."""
    scored = []
    for start_idx in range(len(timeline) - window_hours + 1):
        window_end = timeline[start_idx]["dt"] + timedelta(hours=window_hours)
        if window_end <= now_local:
            continue
        s = score_window(timeline, start_idx, window_hours)
        if s is not None:
            scored.append((s, start_idx))
    scored.sort(key=lambda pair: pair[0], reverse=True)

    picked = []
    used_hours = set()
    for score, start_idx in scored:
        window_range = range(start_idx, start_idx + window_hours)
        if used_hours.intersection(window_range):
            continue
        picked.append((score, start_idx))
        used_hours.update(window_range)
        if len(picked) >= top_n:
            break
    return picked


def _label_for_date(d, today_date):
    return "Today" if d == today_date else f"{d.strftime('%a')} {d.month}/{d.day}"


def _chart_theme():
    """(surface, ink) colors for chart chrome, following whichever
    Streamlit theme is active. The score chart draws its inter-segment
    gaps and the ring around the net-score diamond in the *surface*
    color so they vanish into the page in both light and dark mode;
    hard-coding white would draw visible white borders on a dark
    theme. Falls back to Streamlit's light-theme defaults on any
    Streamlit version without `st.context.theme`."""
    surface, ink = "#ffffff", "#31333f"
    try:
        theme = st.context.theme
        surface = theme.backgroundColor or surface
        ink = theme.textColor or ink
    except Exception:
        pass
    return surface, ink


def format_window(timeline, start_idx, today_date, window_hours=WINDOW_HOURS):
    """One line describing timeline[start_idx:start_idx+window_hours],
    for direct display in the UI."""
    hours = timeline[start_idx:start_idx + window_hours]
    start_dt = hours[0]["dt"]
    end_dt = hours[-1]["dt"] + timedelta(hours=1)

    seen = set()
    events = []
    for h in hours:
        for e in h["events"]:
            key = (e["kind"], e["start"])
            if key not in seen:
                seen.add(key)
                events.append(e)
    events.sort(key=lambda e: e["start"])

    parts = []
    for e in events:
        if e["start"] == e["end"]:
            parts.append(f"{e['kind']} {_format_time(e['start'])}")
        else:
            parts.append(f"{e['kind']} {_format_time(e['start'])}-{_format_time(e['end'])}")
    events_text = ", ".join(parts) if parts else "no major/minor feeding activity"

    rut_label = hours[0]["rut_label"]

    weathers = [h["weather"] for h in hours if h["weather"]]
    bits = []
    if weathers:
        temps = [w["temp_avg"] for w in weathers if w["temp_avg"] is not None]
        precips = [w["precip_max"] for w in weathers if w["precip_max"] is not None]
        winds = [w["wind_avg"] for w in weathers if w["wind_avg"] is not None]
        pressures = [w["pressure_avg"] for w in weathers if w["pressure_avg"] is not None]
        wind_dir_pairs = [
            (w["wind_dir_deg"], w["wind_avg"]) for w in weathers
            if w.get("wind_dir_deg") is not None and w.get("wind_avg") is not None
        ]
        if temps:
            temp_bit = f"avg {round(sum(temps) / len(temps))}°F"
            anomalies = [h["temp_anomaly"] for h in hours if h["temp_anomaly"] is not None]
            if anomalies:
                avg_anomaly = sum(anomalies) / len(anomalies)
                if abs(avg_anomaly) >= 3:
                    direction = "below" if avg_anomaly > 0 else "above"
                    temp_bit += f" ({abs(avg_anomaly):.0f}° {direction} normal)"
            bits.append(temp_bit)
        if precips:
            bits.append(f"precip up to {max(precips):.0f}%")
        if winds:
            wind_bit = f"wind {round(sum(winds) / len(winds), 1)} mph avg"
            mean_dir = _circular_mean_deg(
                [d for d, _ in wind_dir_pairs], [w for _, w in wind_dir_pairs]
            ) if wind_dir_pairs else None
            if mean_dir is not None:
                wind_bit += f" from {_compass_direction(mean_dir)}"
            bits.append(wind_bit)
        if pressures:
            avg_pressure = sum(pressures) / len(pressures)
            pressure_bit = f"pressure {avg_pressure:.2f} inHg"
            if PRESSURE_BAND_LOW_IN <= avg_pressure <= PRESSURE_BAND_HIGH_IN:
                # Informational only: PRESSURE_BAND_YPH is 0, so this
                # band does not move the score. See the constant.
                pressure_bit += " (traditional 'sweet spot' band)"
            bits.append(pressure_bit)

    pressure_drop = hours[0]["pressure_drop"]
    if pressure_drop is not None and pressure_drop >= PRESSURE_DROP_MINOR_THRESHOLD_IN:
        tier_label = "front approaching" if pressure_drop >= PRESSURE_DROP_THRESHOLD_IN else "pressure easing"
        bits.append(f"falling {pressure_drop:.2f} in/{PRESSURE_DROP_LOOKBACK_HOURS}h - {tier_label}")

    weather_text = ", ".join(bits) if bits else "weather data unavailable"

    day_label = _label_for_date(start_dt.date(), today_date)
    return (
        f"{day_label} {_format_time(start_dt)} - {_format_time(end_dt)}: "
        f"{events_text} | {rut_label} | {weather_text}"
    )


# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------

st.set_page_config(page_title="Solunar Feeding Times", page_icon=":deer:")
st.title(":deer: Solunar Feeding Times")
st.caption(
    "Major/minor solunar feeding-time windows for hunting & fishing, computed "
    "locally from moon transit/rise/set times - no API key, no external "
    "solunar service. Enter a postal code to get a forecast for that location."
)

def _default_rut_peak():
    """RUT_PEAK_DEFAULT_MONTH_DAY in whichever season the user is most
    likely to mean: the upcoming/current fall if it's July or later,
    otherwise the rut that just passed."""
    today = date.today()
    month, day = RUT_PEAK_DEFAULT_MONTH_DAY
    year = today.year if today.month >= 7 else today.year - 1
    return date(year, month, day)


_by_name = sorted(SUPPORTED_COUNTRIES, key=lambda pair: pair[1])
_name_to_code = {name: code for code, name in _by_name}
_country_names = list(_name_to_code.keys())

with st.form("location_form"):
    col1, col2 = st.columns([2, 1])
    with col1:
        country_name = st.selectbox(
            "Country", _country_names, index=_country_names.index("United States"),
        )
    with col2:
        postcode = st.text_input("Postal / zip code")
    days = st.slider("Days", min_value=1, max_value=30, value=7)
    rut_peak = st.date_input(
        "Peak breeding (rut) date for your area",
        value=_default_rut_peak(),
        help=(
            "Rut phase is the strongest driver of fall deer movement in the "
            "research this app's scoring is calibrated against, but peak rut "
            "date is regionally specific and not a clean function of "
            "latitude. The default (Nov 15) follows the Pennsylvania Game "
            "Commission's fetal-aging data (peak breeding mid-November, half "
            "of does bred by Nov 13). If your state wildlife agency publishes "
            "conception dates for your area, use those."
        ),
    )
    submitted = st.form_submit_button("Get feeding times", type="primary")

if submitted:
    if not postcode.strip():
        st.error("Enter a postal/zip code.")
    else:
        country_code = _name_to_code[country_name]
        with st.spinner("Looking up location..."):
            loc = lookup_location(postcode, country_code)

        if loc is None:
            st.error(f"Couldn't find a location for {country_name} postal code '{postcode}'.")
        else:
            with st.spinner("Resolving timezone..."):
                tz_name = lookup_timezone(loc["lat"], loc["lon"])
            try:
                tz = ZoneInfo(tz_name)
            except Exception:
                tz = UTC

            place_label = f"{loc['place']}, {loc['state']}" if loc.get("place") else f"({loc['lat']:.4f}, {loc['lon']:.4f})"
            st.subheader(f":round_pushpin: {place_label}")
            st.caption(f"Timezone: {tz_name}")

            days_data = fetch_solunar(loc["lat"], loc["lon"], tz, days)

            with st.spinner("Fetching weather forecast..."):
                weather_samples = fetch_hourly_weather(loc["lat"], loc["lon"], days)

            if weather_samples:
                timeline = build_hourly_timeline(days_data, weather_samples, tz, rut_peak)
                now_local = datetime.now(tz).replace(tzinfo=None)
                candidates = find_candidate_windows(timeline, now_local)
                if candidates:
                    st.subheader(":dart: Best Hunting Windows")
                    st.caption(
                        "Ranks every possible 6-hour window by rut phase, dawn/dusk "
                        "timing, solunar activity, temperature relative to local "
                        "normal, wind, and barometric pressure - weighted by "
                        "measured effect sizes from GPS-collar research (see the "
                        "scoring notes at the bottom of the page)."
                    )
                    today_date = now_local.date()
                    top_candidates = candidates[:3]
                    for i, (_score, start_idx) in enumerate(top_candidates, start=1):
                        st.write(f"**#{i}** - {format_window(timeline, start_idx, today_date)}")

                    # Ranked by score (candidates is score-sorted) but charted in
                    # chronological order, so the x-axis reads left-to-right as time
                    # rather than jumping around by rank.
                    ranked_by_time = sorted(
                        enumerate(candidates, start=1),
                        key=lambda pair: timeline[pair[1][1]]["dt"],
                    )

                    # Each bar is stacked by score SOURCE rather than plotted
                    # as one solid color, so it's visible at a glance where a
                    # window's score is actually coming from. Colors are fixed
                    # per category (never re-cycled) and match the order
                    # they're stacked in.
                    #
                    # Stacking is zero-based: positive terms pile up above the
                    # axis and negative ones hang below it. That is the right
                    # way to show *composition*, but it means the bar's visual
                    # span is NOT the window's net score - a window with a big
                    # weather penalty below the axis and a dawn bonus above it
                    # looks taller than its total. So the net is drawn
                    # explicitly on top of every bar: a thin connector from
                    # zero to the true total, ending in a diamond, and a
                    # direct value label on the top-3 ranked windows only (a
                    # number on all 15 would be noise; the rest are in the
                    # tooltip).
                    category_order = [CATEGORY_RUT, CATEGORY_ACTIVITY, CATEGORY_WEATHER]
                    category_colors = ["#1baf7a", "#2a78d6", "#eb6834"]
                    surface, ink = _chart_theme()

                    window_labels = []
                    breakdown_rows = []
                    net_rows = []
                    for i, (score, start_idx) in ranked_by_time:
                        label = (
                            f"#{i} {_label_for_date(timeline[start_idx]['dt'].date(), today_date)} "
                            f"{_format_time(timeline[start_idx]['dt'])}"
                        )
                        window_labels.append(label)
                        breakdown = score_breakdown(timeline, start_idx)
                        for rank, category in enumerate(category_order):
                            breakdown_rows.append({
                                "Window": label,
                                "Category": category,
                                "CategoryRank": rank,
                                "Score": round(breakdown[category], 2),
                                "Net": round(breakdown["total"], 2),
                            })
                        net_rows.append({
                            "Window": label,
                            "Rank": i,
                            "Net": round(breakdown["total"], 2),
                            "Zero": 0.0,
                            "NetLabel": f"{breakdown['total']:+.2f}" if i <= 3 else "",
                        })

                    score_breakdown_df = pd.DataFrame(breakdown_rows)
                    net_df = pd.DataFrame(net_rows)

                    x_axis = alt.X(
                        "Window:N", sort=window_labels, title=None,
                        axis=alt.Axis(labelAngle=-40),
                    )
                    # A stroke in the surface color puts a 2px gap between
                    # touching segments (and between the bar and the axis),
                    # so neighbouring colors read as separate without a
                    # drawn border.
                    bars = alt.Chart(score_breakdown_df).mark_bar(
                        stroke=surface, strokeWidth=2,
                    ).encode(
                        x=x_axis,
                        y=alt.Y("Score:Q", title="Score (points)", stack="zero"),
                        color=alt.Color(
                            "Category:N",
                            scale=alt.Scale(domain=category_order, range=category_colors),
                            legend=alt.Legend(title="Score source"),
                        ),
                        order=alt.Order("CategoryRank:Q"),
                        tooltip=[
                            alt.Tooltip("Window:N"),
                            alt.Tooltip("Category:N"),
                            alt.Tooltip("Score:Q", format="+.2f", title="This term"),
                            alt.Tooltip("Net:Q", format="+.2f", title="Net score"),
                        ],
                    )
                    zero_line = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(
                        color=ink, opacity=0.45, strokeWidth=1,
                    ).encode(y="y:Q")
                    net_connector = alt.Chart(net_df).mark_rule(
                        color=ink, opacity=0.75, strokeWidth=1.5,
                    ).encode(x=x_axis, y="Zero:Q", y2="Net:Q")
                    net_marker = alt.Chart(net_df).mark_point(
                        shape="diamond", size=120, filled=True,
                        color=ink, stroke=surface, strokeWidth=2,
                    ).encode(
                        x=x_axis,
                        y=alt.Y("Net:Q"),
                        tooltip=[
                            alt.Tooltip("Window:N"),
                            alt.Tooltip("Net:Q", format="+.2f", title="Net score"),
                            alt.Tooltip("Rank:Q", title="Rank"),
                        ],
                    )
                    def _net_label_layers(dy, keep):
                        """Net-score label above (dy<0) or below (dy>0) the
                        diamond, for rows matching `keep`. Drawn twice: a
                        fat surface-colored copy first as a halo, then the
                        ink copy, so the label stays legible when it lands
                        on a colored segment."""
                        base = alt.Chart(net_df).encode(
                            x=x_axis, y="Net:Q", text="NetLabel:N",
                        ).transform_filter(keep)
                        halo = base.mark_text(
                            fontWeight="bold", fontSize=12, dy=dy,
                            color=surface, stroke=surface, strokeWidth=4, opacity=0.9,
                        )
                        text = base.mark_text(
                            fontWeight="bold", fontSize=12, dy=dy, color=ink,
                        )
                        return [halo, text]

                    net_labels = (
                        _net_label_layers(-13, alt.datum.Net >= 0)
                        + _net_label_layers(15, alt.datum.Net < 0)
                    )
                    score_chart = alt.layer(
                        bars, zero_line, net_connector, net_marker, *net_labels,
                    ).properties(height=340)
                    st.altair_chart(score_chart, width="stretch")
                    st.caption(
                        "Each bar is stacked by where its score comes from: **Rut "
                        "Phase** (the largest measured effect, so it sets the level "
                        "for a whole day rather than separating windows within it), "
                        "**Daily Activity** (dawn/dusk plus solunar major/minor), and "
                        "the combined **Weather** effect. Bonuses stack above the "
                        "zero line and penalties hang below it, so the bar's height "
                        "alone is not the score - the **diamond is the net score** "
                        "(bonuses minus penalties), and the top 3 windows carry their "
                        "net value as a label. Hover any segment or diamond for exact "
                        "numbers."
                    )

                    st.subheader(":chart_with_upwards_trend: Forecast Overview")
                    st.caption(
                        "Hourly solunar activity (area) vs. temperature (line) across "
                        "the whole forecast."
                    )
                    timeline_df = pd.DataFrame({
                        "dt": [h["dt"] for h in timeline],
                        "activity": [h["activity"] for h in timeline],
                        "temp_f": [h["weather"]["temp_avg"] if h["weather"] else None for h in timeline],
                    })
                    base = alt.Chart(timeline_df).encode(x=alt.X("dt:T", title="Date / Time"))
                    activity_area = base.mark_area(opacity=0.35, color="#4C78A8").encode(
                        y=alt.Y("activity:Q", title="Activity Points"),
                    )
                    temp_line = base.mark_line(color="#E45756", strokeWidth=2).encode(
                        y=alt.Y("temp_f:Q", title="Temp (°F)"),
                    )
                    overview_chart = alt.layer(activity_area, temp_line).resolve_scale(y="independent")
                    st.altair_chart(overview_chart, width="stretch")

                    st.divider()
            else:
                st.warning(
                    "Couldn't fetch the weather forecast, so hunting-window "
                    "ranking is unavailable right now - feeding times are still shown below."
                )

            for day in days_data:
                with st.container(border=True):
                    st.markdown(f"**{day['label']}**")
                    if not day["periods"]:
                        st.caption("No data available for this day.")
                        continue
                    for p in day["periods"]:
                        emoji = _KIND_EMOJI.get(p["kind"], "")
                        if p["start"] == p["end"]:
                            st.write(f"{emoji} **{p['kind']}** - {_format_time(p['start'])}")
                        else:
                            st.write(f"{emoji} **{p['kind']}** - {_format_time(p['start'])} - {_format_time(p['end'])}")

st.divider()
st.caption(
    "Solunar theory holds that Major periods (~2 hrs, moon overhead/underfoot) "
    "are best and Minor periods (~1 hr, moonrise/moonset) secondary. GPS-collar "
    "research does not bear that out - the measured effect of both is close to "
    "zero, while dawn and dusk are consistently the strongest daily signal. "
    "They're shown here because they're what this app computes, but the window "
    "ranking above weights them at their measured size, not their traditional "
    "one. See the scoring notes below."
)

with st.expander(":straight_ruler: How the hunting-window score is calculated"):
    st.markdown(
        f"Each candidate is a rolling **{WINDOW_HOURS}-hour** window, scored by the "
        "formula below."
    )

    st.markdown("### Where the weights come from")
    st.markdown(
        "The weights are **not** hand-tuned. Each one is expressed in the unit the "
        "underlying GPS-collar research measured it in - yards per hour (yph) of excess "
        "daytime buck movement - and a window's score is the **mean** excess yph across "
        "its hours, converted to points by one shared constant:"
    )
    st.latex(
        r"\text{score} = k \cdot \frac{1}{H}\sum_{h=1}^{H} \text{(excess yph in hour } h)"
        r", \qquad k = \frac{3.0}{142} \approx " + f"{POINTS_PER_YPH:.4f}"
    )
    st.markdown(
        "Averaging rather than summing is what makes effects of different *durations* "
        "comparable. Rut elevates movement across every hour of a window; dawn/dusk "
        "elevates roughly two of six. At equal yph the all-hours effect really is worth "
        "3x the two-hour one, and that only falls out if both are averaged over the same "
        "window."
    )
    st.markdown(
        "$k$ is anchored so that peak rut - the largest effect in the source data, "
        "+142 yph - is worth 3.0 points. Effect sizes are from **Neary et al. (2025)**, "
        "which GPS-collared 48 bucks in central Mississippi at 15-minute fixes from "
        "September through February over two years and reported every result as a change "
        "in daytime yards-per-hour against a **269 yph season mean**:\n\n"
        "| Effect | Measured | Points |\n"
        "|---|---|---|\n"
        f"| Peak rut | +142 yph | {142 * POINTS_PER_YPH:.2f} |\n"
        f"| Early rut | +104 yph | {104 * POINTS_PER_YPH:.2f} |\n"
        f"| Late rut | +78 yph | {78 * POINTS_PER_YPH:.2f} |\n"
        f"| **Within 1 hr of sunrise/sunset** | **+48 yph** (317 vs. 269) | "
        f"**{CREPUSCULAR_YPH * POINTS_PER_YPH:.2f}** |\n"
        f"| Post-rut | +9 yph | {9 * POINTS_PER_YPH:.2f} |\n"
        f"| Pre-rut | +4 yph | {4 * POINTS_PER_YPH:.2f} |\n"
        f"| **Solunar Major** (moon overhead/underfoot) | **+3 yph** | "
        f"**{MAJOR_YPH * POINTS_PER_YPH:.2f}** |\n"
        f"| **Solunar Minor** (moonrise/moonset) | **-0.1 yph** | "
        f"**{MINOR_YPH * POINTS_PER_YPH:.2f}** |\n"
        f"| Outside the rut | -40 yph measured, scored as {NO_RUT_YPH * POINTS_PER_YPH:.2f} | "
        f"{NO_RUT_YPH * POINTS_PER_YPH:.2f} |\n\n"
        "The rightmost column is the effect at its own rate; what a term actually "
        "contributes to a window also depends on how many of the window's hours it "
        "covers (a dawn band covers ~2 of 6, so it adds ~"
        f"{CREPUSCULAR_YPH * 2 / WINDOW_HOURS * POINTS_PER_YPH:.2f}).\n\n"
        "**Outside the rut is floored at zero, not scored at its measured -40 yph.** "
        "That -40 is a delta against a season mean that the rut days themselves pull "
        "up, so scoring every non-rut day against it would subtract points from most "
        "of the season just for not being the rut. This app ranks windows within a "
        "short forecast rather than against the whole season, so a day with no "
        "measured rut elevation is neutral, not a deficit.\n\n"
        "That ordering is the single most important thing on this page: **rut phase and "
        "dawn/dusk dominate; the solunar periods this app is named after are, measured "
        "against a buck's own usual movement at the same time of day, indistinguishable "
        "from zero.** They're still computed and displayed, but they barely move the "
        "ranking, because that's what the measurements support."
    )

    st.markdown("### The score")
    st.latex(
        r"\text{score} = \underbrace{\text{activity}}_{\text{dawn/dusk} + \text{solunar}}"
        r" + \underbrace{\text{rut}}_{\text{per day}}"
        r" + \underbrace{(\text{cold} + \text{pressure} - \text{penalty}) \cdot d}_{\text{weather}}"
    )
    st.markdown(
        "$d$ is the dawn/dusk damping factor applied to every weather term - see step 6."
    )

    st.markdown("**1. Daily activity** - every hour $h$ in the window contributes:")
    st.latex(
        r"\text{activity} = \frac{k}{H} \sum_{h} \sum_{e}\ y_e \cdot o_{h,e}"
    )
    st.markdown(
        f"$y_e$ is event type $e$'s measured yph effect and $o_{{h,e}}$ the fraction of "
        f"hour $h$ that event's band covers. Bands: Major +/-60 min, Minor +/-30 min, "
        f"sunrise/sunset +/-60 min (matching how Neary et al. defined \"within an hour of\" "
        f"dawn and dusk)."
    )

    st.markdown(
        "**2. Rut phase** - a per-day level that applies to every hour of the window. "
        "Phases are 14-day bands around the peak breeding date you enter:"
    )
    st.markdown(
        "| Phase | Days from peak |\n|---|---|\n"
        + "\n".join(
            f"| {label} | {start:+d} to {end:+d} |"
            for label, start, end, _ in RUT_PHASE_YPH
        )
        + "\n| Outside rut | beyond +/-35 (scored as 0, not a penalty) |\n\n"
        "**Peak rut date is regionally specific and not a clean function of latitude**, "
        "which is why this app asks rather than computes it: Pennsylvania peaks "
        "mid-November with half of does bred by Nov 13 (PA Game Commission fetal "
        "aging; Penn State Deer-Forest Study), southwest Wisconsin Oct 23-Nov 12 "
        "(Hunsaker et al. 2025) *despite being further north*, and central Mississippi "
        "Dec 25 (Neary et al. 2025). If your state wildlife agency publishes conception "
        "data, use it. Note also that these are **buck** movement rates."
    )

    st.markdown(
        "**3. Cold** - measured as a departure below this location's own recent normal "
        "**for that hour of day**, not against a fixed degree threshold (38°F means "
        "something very different in Maine than in Georgia, and deer respond to change "
        "from what they're acclimated to):"
    )
    st.latex(
        r"\text{cold}_h = Y_{\text{cold}} \cdot "
        r"\text{clamp}\big(\tfrac{\Delta T_h}{S},\ 0,\ 1\big)"
    )
    st.markdown(
        f"$\\Delta T$ is how many °F below normal an hour runs, where \"normal\" is "
        f"the mean of the last {PAST_DAYS_LOOKBACK} days of observations at the same hour "
        f"of day; $S = {COLD_ANOMALY_SCALE:.0f}$°F and "
        f"$Y_{{\\text{{cold}}}} = {COLD_MAX_YPH:.0f}$ yph (max "
        f"{COLD_MAX_YPH * POINTS_PER_YPH:.2f} points). Like every weather term it is "
        f"evaluated hour by hour and averaged over the window.\n\n"
        f"Temperature is the one weather variable with consistent support - Webb et al. "
        f"(2010) found weather mattered in only 8 of 80 models (10%), and temperature "
        f"accounted for 5 of those 8, more than any other variable. No study publishes a "
        f"movement-rate effect size for it, so $Y_{{\\text{{cold}}}}$ is a bounded "
        f"judgment call: it's set so that a full cold anomaly, applied across every hour, "
        f"contributes about as much to a window as a single dawn or dusk does."
    )

    st.markdown("**4. Rain/wind penalty** - subtracted:")
    st.latex(
        r"\text{penalty}_h = Y_{\text{rain}} \tfrac{p_h}{100}"
        r" + Y_{\text{wind}}\,\text{clamp}\big(\tfrac{v_h - v_0}{v_1 - v_0},\ 0,\ 1\big)"
    )
    st.markdown(
        f"$p_h$ is the hour's precipitation chance (%) and $v_h$ its wind speed (mph); "
        f"$Y_{{\\text{{rain}}}} = {PRECIP_MAX_PENALTY_YPH:.0f}$ yph, "
        f"$Y_{{\\text{{wind}}}} = {WIND_MAX_PENALTY_YPH:.0f}$ yph, "
        f"$v_0 = {WIND_PENALTY_THRESHOLD_MPH:.0f}$, $v_1 = {WIND_PENALTY_FULL_MPH:.0f}$ mph.\n\n"
        f"Rain and wind sit at the same tier - half the cold ceiling - because they have "
        f"the same evidential standing: each was significant in exactly 1 of Webb et al. "
        f"(2010)'s 8 significant weather models. An earlier version gave rain the full "
        f"ceiling on the strength of the Penn State Deer-Forest Study's storm analysis "
        f"(30 storms, 52,279 GPS locations) \"finding deer moved less during storms\"; on "
        f"re-reading, its two years point in opposite directions (2016: 102 yph outside "
        f"storms vs 113 during; 2017: 111 vs 98) and it concludes there was no significant "
        f"effect, so it supports neither the size nor the direction of a rain penalty. "
        f"The direction is a judgment call. Wind doesn't engage at all below "
        f"{WIND_PENALTY_THRESHOLD_MPH:.0f} mph."
    )

    st.markdown(
        "**5. Pressure** - deliberately downweighted to near-token size, and split by "
        "what the evidence can actually distinguish: a *change* in pressure (a two-tier "
        "bonus for a falling 24-hour trend) versus a static *level* (the traditional "
        "\"sweet spot\" band, which now carries **zero** weight):"
    )
    st.latex(
        r"\text{pressure}_h = \underbrace{Y_{\text{band}}\,"
        r"\mathbb{1}[P_{\text{low}} \le P_h \le P_{\text{high}}]}_{\text{band}}"
        r" + \underbrace{\begin{cases}"
        r"Y_{\text{drop}} & \Delta P_h \ge \Delta P_{\text{min}} \\"
        r"Y_{\text{drop,minor}} & \Delta P_{\text{minor}} \le \Delta P_h < \Delta P_{\text{min}} \\"
        r"0 & \text{otherwise}"
        r"\end{cases}}_{\text{falling}}"
    )
    st.markdown(
        f"$P_h$ is the hour's sea-level pressure (inHg) and $\\Delta P_h$ the fall over "
        f"the {PRESSURE_DROP_LOOKBACK_HOURS} hours before it; "
        f"$P_{{\\text{{low}}}} = {PRESSURE_BAND_LOW_IN}$, "
        f"$P_{{\\text{{high}}}} = {PRESSURE_BAND_HIGH_IN}$, "
        f"$Y_{{\\text{{band}}}} = {PRESSURE_BAND_YPH:.0f}$ yph, "
        f"$\\Delta P_{{\\text{{minor}}}} = {PRESSURE_DROP_MINOR_THRESHOLD_IN}$, "
        f"$Y_{{\\text{{drop,minor}}}} = {PRESSURE_DROP_MINOR_YPH:.0f}$ yph, "
        f"$\\Delta P_{{\\text{{min}}}} = {PRESSURE_DROP_THRESHOLD_IN}$, "
        f"$Y_{{\\text{{drop}}}} = {PRESSURE_DROP_YPH:.0f}$ yph "
        f"(so at most {(PRESSURE_BAND_YPH + PRESSURE_DROP_YPH) * POINTS_PER_YPH:.2f} "
        f"points).\n\n"
        "**Why so small?** The Penn State Deer-Forest Study found *no statistical or "
        "biological significance* of oncoming storms on collared deer - their before / "
        "during / after / control hourly movement rates all sit within roughly 94-113 yph "
        "of each other, and the two study years disagree on direction. Webb et al. (2010) "
        "found pressure was the only one of five weather variables with no within-day "
        "linear trend at all. Falling pressure keeps a token weight because a separate "
        "Webb et al. analysis of day-over-day weather *changes* attributed 3 of 10 "
        "significant models to pressure, and Goethlich (2019) found pressure affected "
        "activity in some seasons and times of day - small-and-unproven rather than "
        "disproven. Its size is half the roughly +/-10 yph spread across the Penn State "
        "conditions, which is itself an upper bound on an effect that study could not "
        "detect.\n\n"
        "The 29.8-30.3 inHg \"sweet spot\" band, by contrast, traces to a hunting-magazine "
        "rule of thumb, and no located study tests a static pressure *level* at all. It "
        "is still reported in the window text for hunters who track it, but it no longer "
        "moves the score."
    )

    st.markdown(
        "**6. Dawn/dusk damping** - every weather term above is evaluated per hour and "
        "then scaled down inside the sunrise/sunset halos:"
    )
    st.latex(
        r"\text{weather} = \frac{k}{H}\sum_h (\text{cold}_h + \text{pressure}_h - "
        r"\text{penalty}_h)\,(1 - D \cdot c_h)"
    )
    st.markdown(
        f"$c_h$ is the fraction of hour $h$ inside a dawn or dusk halo (0-1) and "
        f"$D = {WEATHER_CREPUSCULAR_DAMPING}$, so an hour fully at dawn carries half the "
        f"weather weight of a midday hour.\n\n"
        "Two studies independently found that weather effects concentrate in *non-peak* "
        "hours: Goethlich (2019; 116 collared deer, South Carolina) was \"most likely to "
        "see a significant relationship between abiotic factors and activity during "
        "daytime and nighttime and least likely to see an effect in the morning and "
        "evening\", and Webb et al. (2010)'s weather effects surfaced at 0100-0200 and "
        "1300 - \"hours of limited movements\" - not at dawn or dusk. Hunsaker et al. "
        "(2025; 188 bucks, Wisconsin) found no weather effect at all on rut-period "
        "movement. The size of $D$ is a judgment call: the sources say \"least likely\" "
        "and \"less pronounced\", not \"absent\".\n\n"
        "A final sanity check on the whole weather block: Webb et al. (2010)'s weather "
        "parameter estimates never exceeded ~29 m/h (~32 yph), and the authors attribute "
        "even that partly to collar error. The most this block can move a window is "
        f"about +{COLD_MAX_YPH + PRESSURE_BAND_YPH + PRESSURE_DROP_YPH:.0f} / "
        f"-{PRECIP_MAX_PENALTY_YPH + WIND_MAX_PENALTY_YPH:.0f} yph, inside that bound."
    )

    st.markdown("### Window search and chart colors")
    st.markdown(
        f"The top {CANDIDATE_WINDOW_COUNT} highest-scoring windows are found by sliding "
        f"this {WINDOW_HOURS}-hour window across every possible starting hour in the "
        "forecast, keeping only non-overlapping windows (best score wins any overlap) so "
        "the results represent genuinely different opportunities rather than the same "
        "window shifted by an hour.\n\n"
        f"| Chart color | Terms it contains | Can it be negative? |\n"
        f"|---|---|---|\n"
        f"| **{CATEGORY_RUT}** | 2 | No - floored at zero outside the rut |\n"
        f"| **{CATEGORY_ACTIVITY}** | 1 (dawn/dusk + solunar) | No |\n"
        f"| **{CATEGORY_WEATHER}** | (3 + 5 - 4) x 6 | Yes - a wet, windy window pulls its "
        f"bar below zero |\n\n"
        f"Bonuses stack above the zero line and penalties hang below it, so a bar's "
        f"height is not its score; the diamond on each bar marks the net total (the three "
        f"segments summed), which is what the ranking uses."
    )

    st.markdown("### Sources")
    st.caption(
        "Every citation below was verified against the published source. README.md "
        "carries the full write-up, including what each source contributes and where "
        "sources disagree with this model."
    )
    st.markdown(
        "- **Neary, N., B. Strickland, L. Resop, S. Demarais, and W. McKinley. 2025.** "
        "*Lunar Legends: Does the Moon Influence Buck Activity?* Mississippi State "
        "University Extension Publication 4068. — supplies **every activity weight in "
        "this model**: the 269 yph season baseline, the +48 yph dawn/dusk effect, the "
        "+3 / -0.1 yph solunar major/minor effects, and the full rut-phase yph ladder. "
        "48 GPS-collared bucks, central Mississippi, 15-min fixes, Sept-Feb, 2 years.\n"
        "- **Webb, S.L., K.L. Gee, B.K. Strickland, S. Demarais, and R.W. DeYoung. 2010.** "
        "*Measuring Fine-Scale White-Tailed Deer Movements and Environmental Influences "
        "Using GPS Collars.* International Journal of Ecology 2010:1-12. — 32 deer, 7 "
        "years of 15-minute fixes in Oklahoma. Source for *\"general linear trends in "
        "movements related to 4 of the 5 weather variables in only 8 of 80 (10%) models... "
        "Temperature influenced movements in 5 of 8 cases\"* - note that leaves "
        "**pressure as the one variable of five with no linear trend**. Also the null "
        "moon-phase result and the finding that crepuscular movement is the dominant "
        "driver.\n"
        "- **Penn State Deer-Forest Study.** *Spidey Sense* (deer.psu.edu) — 30 storm "
        "events, 52,279 GPS locations, 4-8 collared does, 2016-17; source for the null "
        "storm/pressure result (\"no statistical or biological significance\") and the "
        "~94-113 yph before/during/after/control spread the falling-pressure term is "
        "pinned under. Its two years disagree on whether deer moved more or less during "
        "storms, which is why it no longer backs the rain penalty. (Research-project blog, "
        "not peer-reviewed.)\n"
        "- **Pennsylvania Game Commission.** *When is the rut?* — fetal measurements from "
        "6,000+ road-killed does, 2000-2007; peak breeding by adult does in "
        "**mid-November**, which is where the Nov 15 default comes from. The Penn State "
        "Deer-Forest Study, from the same data, puts half of does bred by Nov 13.\n"
        "- **Sullivan, J.D., S.S. Ditchkoff, B.A. Collier, C.R. Ruth, and J.B. Raglin. "
        "2016.** *Movement with the moon: white-tailed deer activity and solunar events.* "
        "Journal of the SEAFWA 3:225-232. — 38 bucks, Brosnan Forest, South Carolina. Near a new/full "
        "moon, **minor**-period activity rose (0.384->0.564 at moonrise) while "
        "**major**-period activity *fell* (0.540->0.413 overhead). Concluded solunar "
        "charts \"may be misleading\".\n"
        "- **Swartout, T.J., and S.S. Ditchkoff. 2025.** *Are Solunar Charts as "
        "Predictable as They Claim?* Southeastern Naturalist 24(2):137-150. — 22 bucks, "
        "high-fenced Alabama property. The strongest pro-solunar result found: top-rated "
        "days gave **3.02x / 2.83x** activity odds during moon underfoot/overhead, but "
        "only 0.30x / 0.37x at moonrise/moonset. Reported as odds, not movement rate, so "
        "it could not be converted into this model's units - see README.\n"
        "- **Hunsaker, M.A., M.L.J. Gilbertson, D.J. Storm, and W.C. Turner. 2025.** *The "
        "Breeding Season and Movement Ecology of Male White-Tailed Deer in Southwest "
        "Wisconsin.* Ecology and Evolution 15(7):e71589. — 188 collared males; source for "
        "the Oct 23-Nov 12 Wisconsin peak-breeding window used to show that rut timing "
        "isn't a simple latitude function, and for the null result that weather, hunting "
        "season and opening firearm weekend had no significant effect on rut-period "
        "movement.\n"
        "- **Little, A.R., S.L. Webb, S. Demarais, K.L. Gee, S.K. Riffell, and J.A. "
        "Gaskamp. 2016.** *Hunting intensity alters movement behaviour of white-tailed "
        "deer.* Basic and Applied Ecology 17:360-369. — 37 adult bucks, Oklahoma; "
        "hunting pressure as a real movement driver; a known gap this model does not "
        "attempt (see README).\n"
        "- **Goethlich, J. 2019.** *Effects of Abiotic Factors on White-tailed Deer "
        "Activity in South Carolina.* M.S. thesis, Auburn University. — 116 collared "
        "deer, 2009-2018: responses to abiotic factors were *\"typically less pronounced "
        "than circadian fluctuations in activity, and occurred most often during non-peak "
        "times of activity.\"* The basis, with Webb et al. 2010, for damping the weather "
        "terms at dawn and dusk (step 6)."
    )
