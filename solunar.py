"""
Standalone Streamlit app: solunar "feeding times" forecast (hunting/fishing
major/minor activity windows) for any postal/zip code worldwide.

Fully self-contained - no dependency on, or import of, anything outside
this folder. No API keys, no .env, no secrets: geocoding is a public
Zippopotam.us lookup, timezone resolution and the hourly weather forecast
are public Open-Meteo lookups, and the solunar math itself runs locally
via the `ephem` astronomy library - no external solunar service involved.

On top of the per-day feeding times, the hourly forecast (temperature,
precipitation chance, wind, barometric pressure) is cross-referenced
against the solunar events to rank the best 6-hour "hunting windows" -
see the scoring section below, which is pure arithmetic with no LLM
anywhere in it.

Run: streamlit run solunar.py
"""

import math
from datetime import datetime, timedelta
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
# Weather + hunting-window scoring (Open-Meteo hourly forecast)
#
# Cross-references the solunar events above against an hourly weather
# forecast to surface the best ~6-hour "hunting window" per search - the
# same deterministic scoring math as this app's private companion (a
# Slack bot that also offers an LLM-narrated version for personal use).
# This app is public-facing, so on purpose there is no LLM call anywhere
# in this section: window selection and ranking are pure arithmetic, so
# behavior is fully reproducible and there's no API cost or key exposure
# risk from other people's usage.
# ---------------------------------------------------------------------------

WINDOW_HOURS = 6

# Per-hour solunar "activity" weights - a Major period counts for twice
# a Minor period, and an hour containing a sunrise/sunset gets a flat
# bonus on top of whatever else is happening that hour, since animals
# are already moving around either event.
MAJOR_WEIGHT = 3.0
MINOR_WEIGHT = 1.5
SUN_EVENT_BONUS = 1.0

# Deer move more in cold weather - a window's average temperature earns
# a bonus the further it runs below COLD_BASELINE_F, growing by +1.0 per
# COLD_BONUS_SCALE degrees colder, capped at COLD_BONUS_CAP so a brutal
# cold snap doesn't swamp the solunar signal entirely.
COLD_BASELINE_F = 45.0
COLD_BONUS_SCALE = 15.0
COLD_BONUS_CAP = 2.0

# Barometric pressure. Falling pressure ahead of an approaching front is
# a well-documented driver of increased deer movement (e.g. tracking-collar
# studies such as Little et al. 2016, "Effects of Weather on Habitat
# Selection and Movement of White-tailed Deer"); rather than inferring
# "a front is coming" from a precipitation-chance forecast, this reads
# actual barometric pressure (hourly `pressure_msl`, mean-sea-level so
# it's comparable across elevations) from Open-Meteo and scores two
# effects directly. Both award flat bonuses at fixed thresholds rather
# than scaling continuously, and every threshold is a hunting-camp rule
# of thumb expressed in inches of mercury (inHg), since that's how a
# barometer is normally read:
#   - a "falling" bonus, tiered by how much pressure has dropped over the
#     PRESSURE_DROP_LOOKBACK_HOURS before a window starts: a drop of at
#     least PRESSURE_DROP_THRESHOLD_IN earns PRESSURE_DROP_BONUS, a
#     smaller-but-still-notable drop of at least
#     PRESSURE_DROP_MINOR_THRESHOLD_IN earns PRESSURE_DROP_MINOR_BONUS.
#     A bigger drop always earns at least as much as a smaller one - it's
#     a threshold ladder, not a band - and only the higher tier applies
#     once its threshold is met (no stacking of both bonuses), and
#   - a "in the sweet-spot band" bonus, when the window's own average
#     pressure falls between PRESSURE_BAND_LOW_IN and
#     PRESSURE_BAND_HIGH_IN.
HPA_PER_INHG = 33.8639

PRESSURE_DROP_LOOKBACK_HOURS = 24
PRESSURE_DROP_MINOR_THRESHOLD_IN = 0.2
PRESSURE_DROP_MINOR_BONUS = 1.0
PRESSURE_DROP_THRESHOLD_IN = 0.4
PRESSURE_DROP_BONUS = 1.5

PRESSURE_BAND_LOW_IN = 29.8
PRESSURE_BAND_HIGH_IN = 30.3
PRESSURE_BAND_BONUS = 1.0

# How many top-scoring, non-overlapping windows to search for; the UI
# text list only shows the top 3 of these (same as the no-LLM fallback
# on the Slack-bot side), but the score bar chart plots all of them.
CANDIDATE_WINDOW_COUNT = 15

# Open-Meteo's hourly forecast rejects forecast_days > 16 (HTTP 400), but
# the "Days" slider below goes up to 30 so solunar-only (moon/sun) times
# can still be shown further out via ephem, which has no such cap.
# Clamping here keeps the weather/ranking feature working for the first
# 16 days instead of failing outright the moment someone picks >16.
OPEN_METEO_MAX_FORECAST_DAYS = 16


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
    normally read."""
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


def build_hourly_timeline(days_data, samples, tz):
    """Flatten fetch_solunar()'s days_data and fetch_hourly_weather()'s
    samples into one hour-by-hour timeline covering the whole forecast,
    so the sliding window search below can start at ANY hour instead of
    a fixed per-day grid. Both inputs share the same "local midnight
    today" anchor (both derive "today" from the same `tz`), so hours
    line up without extra conversion.

    Returns a list of {'dt', 'weather', 'activity', 'events'} dicts, one
    per hour, in chronological order starting at local midnight today.
    Per-hour activity is overlap-weighted: a Major period spanning parts
    of two hours contributes proportionally to each rather than
    double-counting or arbitrarily picking one."""
    now_local = datetime.now(tz).replace(tzinfo=None)
    today_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

    samples_by_hour = {s["dt"].replace(minute=0, second=0, microsecond=0): s for s in samples}
    all_periods = [p for day in days_data for p in day["periods"]]

    timeline = []
    for h in range(len(days_data) * 24):
        hour_start = today_local + timedelta(hours=h)
        hour_end = hour_start + timedelta(hours=1)

        activity = 0.0
        events = []
        for p in all_periods:
            if p["start"] == p["end"]:
                if hour_start <= p["start"] < hour_end:
                    activity += SUN_EVENT_BONUS
                    events.append(p)
            else:
                overlap_hours = (min(p["end"], hour_end) - max(p["start"], hour_start)).total_seconds() / 3600
                if overlap_hours > 0:
                    weight = MAJOR_WEIGHT if p["kind"] == "Major" else MINOR_WEIGHT
                    activity += weight * overlap_hours
                    events.append(p)

        sample = samples_by_hour.get(hour_start)
        timeline.append({
            "dt": hour_start,
            "weather": _summarize_weather([sample]) if sample else None,
            "activity": activity,
            "events": events,
        })

    return timeline


def _pressure_drop_in(timeline, start_idx):
    """Pressure fall (inHg) over the PRESSURE_DROP_LOOKBACK_HOURS before
    a window starts - positive means falling pressure, negative means
    rising. None if there isn't enough history or pressure data at
    either end. Fed to _pressure_drop_bonus() by both score_breakdown()
    and format_window(), so the displayed text always references the
    same signal that earned the bonus."""
    before_idx = start_idx - PRESSURE_DROP_LOOKBACK_HOURS
    if before_idx < 0:
        return None
    before = timeline[before_idx]["weather"]
    now = timeline[start_idx]["weather"]
    if not before or not now or before["pressure_avg"] is None or now["pressure_avg"] is None:
        return None
    return before["pressure_avg"] - now["pressure_avg"]


def _pressure_drop_bonus(pressure_drop):
    """Tiered falling-pressure bonus for a pressure_drop (inHg, from
    _pressure_drop_in()) - 0.0 if None or below the minor threshold.
    Shared by score_breakdown() and format_window() so the displayed
    text always matches what was actually scored."""
    if pressure_drop is None:
        return 0.0
    if pressure_drop >= PRESSURE_DROP_THRESHOLD_IN:
        return PRESSURE_DROP_BONUS
    if pressure_drop >= PRESSURE_DROP_MINOR_THRESHOLD_IN:
        return PRESSURE_DROP_MINOR_BONUS
    return 0.0


CATEGORY_ACTIVITY = "Feeding Window"
CATEGORY_WEATHER = "Weather"
CATEGORY_PRESSURE_DROP = "Pressure Drop"


def score_breakdown(timeline, start_idx, window_hours=WINDOW_HOURS):
    """Same validity rules as score_window(), but returns the individual
    named terms that sum to the total score, so callers (the stacked
    score bar chart) can show where a window's score actually comes
    from. Returns None if the window would run past the end of the
    timeline, or if it has neither solunar activity nor any weather data
    at all. Otherwise a dict:
      CATEGORY_ACTIVITY      - summed solunar activity (Major/Minor/Sun)
      CATEGORY_WEATHER       - cold-weather bonus + pressure sweet-spot
                               bonus, net of the precipitation/wind
                               penalty (can be negative)
      CATEGORY_PRESSURE_DROP - tiered falling-pressure bonus
      'total'                 - sum of the three above
    """
    hours = timeline[start_idx:start_idx + window_hours]
    if len(hours) < window_hours:
        return None
    if not any(h["events"] for h in hours) and not any(h["weather"] for h in hours):
        return None

    activity = sum(h["activity"] for h in hours)
    temps = [h["weather"]["temp_avg"] for h in hours if h["weather"] and h["weather"]["temp_avg"] is not None]
    precips = [h["weather"]["precip_max"] for h in hours if h["weather"] and h["weather"]["precip_max"] is not None]
    winds = [h["weather"]["wind_avg"] for h in hours if h["weather"] and h["weather"]["wind_avg"] is not None]
    pressures = [h["weather"]["pressure_avg"] for h in hours if h["weather"] and h["weather"]["pressure_avg"] is not None]

    weather = 0.0
    if temps:
        avg_temp = sum(temps) / len(temps)
        weather += min(COLD_BONUS_CAP, max(0.0, (COLD_BASELINE_F - avg_temp) / COLD_BONUS_SCALE))

    avg_precip = (sum(precips) / len(precips)) if precips else None
    if avg_precip is not None:
        weather -= avg_precip / 50.0

    if winds:
        avg_wind = sum(winds) / len(winds)
        weather -= max(0.0, avg_wind - 10) / 10.0

    if pressures:
        avg_pressure = sum(pressures) / len(pressures)
        if PRESSURE_BAND_LOW_IN <= avg_pressure <= PRESSURE_BAND_HIGH_IN:
            weather += PRESSURE_BAND_BONUS

    pressure_drop = _pressure_drop_bonus(_pressure_drop_in(timeline, start_idx))

    return {
        CATEGORY_ACTIVITY: activity,
        CATEGORY_WEATHER: weather,
        CATEGORY_PRESSURE_DROP: pressure_drop,
        "total": activity + weather + pressure_drop,
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


def format_window(timeline, start_idx, today_date, window_hours=WINDOW_HOURS):
    """One line describing timeline[start_idx:start_idx+window_hours],
    for direct display in the UI - no LLM narration, just the scored
    facts."""
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
            bits.append(f"avg {round(sum(temps) / len(temps))}°F")
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
                pressure_bit += " (sweet spot)"
            bits.append(pressure_bit)

    pressure_drop = _pressure_drop_in(timeline, start_idx)
    if pressure_drop is not None and pressure_drop >= PRESSURE_DROP_MINOR_THRESHOLD_IN:
        tier_label = "front approaching" if pressure_drop >= PRESSURE_DROP_THRESHOLD_IN else "pressure easing"
        bits.append(f"falling {pressure_drop:.2f} in/{PRESSURE_DROP_LOOKBACK_HOURS}h - {tier_label}")

    weather_text = ", ".join(bits) if bits else "weather data unavailable"

    day_label = _label_for_date(start_dt.date(), today_date)
    return f"{day_label} {_format_time(start_dt)} - {_format_time(end_dt)}: {events_text} | {weather_text}"


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
                timeline = build_hourly_timeline(days_data, weather_samples, tz)
                now_local = datetime.now(tz).replace(tzinfo=None)
                candidates = find_candidate_windows(timeline, now_local)
                if candidates:
                    st.subheader(":dart: Best Hunting Windows")
                    st.caption(
                        "Ranks every possible 6-hour window by solunar activity, "
                        "temperature, wind, and barometric pressure."
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

                    # Each bar is stacked by score SOURCE (feeding window /
                    # weather / pressure drop) rather than plotted as one
                    # solid color, so it's visible at a glance where a
                    # window's score is actually coming from. Colors are
                    # fixed per category (never re-cycled) and match the
                    # order they're stacked in.
                    category_order = [CATEGORY_ACTIVITY, CATEGORY_WEATHER, CATEGORY_PRESSURE_DROP]
                    category_colors = ["#2a78d6", "#eb6834", "#1baf7a"]

                    window_labels = []
                    breakdown_rows = []
                    for i, (_score, start_idx) in ranked_by_time:
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
                            })

                    score_breakdown_df = pd.DataFrame(breakdown_rows)
                    score_chart = alt.Chart(score_breakdown_df).mark_bar().encode(
                        x=alt.X("Window:N", sort=window_labels, title=None,
                                axis=alt.Axis(labelAngle=-40)),
                        y=alt.Y("Score:Q", title="Score"),
                        color=alt.Color(
                            "Category:N",
                            scale=alt.Scale(domain=category_order, range=category_colors),
                            legend=alt.Legend(title="Score source"),
                        ),
                        order=alt.Order("CategoryRank:Q"),
                        tooltip=[
                            alt.Tooltip("Window:N"),
                            alt.Tooltip("Category:N"),
                            alt.Tooltip("Score:Q", format=".2f"),
                        ],
                    ).properties(height=320)
                    st.altair_chart(score_chart, width="stretch")
                    st.caption(
                        "Each bar is stacked by where its score comes from: solunar "
                        "**Feeding Window** activity, the combined **Weather** effect "
                        "(cold bonus + pressure sweet-spot bonus, net of the rain/wind "
                        "penalty - can pull a bar down), and the **Pressure Drop** bonus."
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
                        y=alt.Y("activity:Q", title="Solunar Activity"),
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
    "Major periods (~2 hrs, moon overhead/underfoot) are typically best; "
    "Minor periods (~1 hr, moonrise/moonset) are secondary. Sunrise/Sunset "
    "are shown alongside them so you can spot when a feeding period "
    "overlaps or nearly overlaps one."
)

with st.expander(":straight_ruler: How the hunting-window score is calculated"):
    st.markdown(
        f"Each candidate is a rolling **{WINDOW_HOURS}-hour** window. Its score is the "
        "sum of five independent terms (pure arithmetic - no AI involved):"
    )
    st.latex(r"\text{score} = \text{activity} + \text{cold} - \text{penalty} + \text{sweetspot} + \text{dropping}")

    st.markdown("**1. Solunar activity** - every hour $h$ in the window contributes:")
    st.latex(
        r"\text{activity} = \sum_{h}\Big("
        r"w_{\text{major}}\, o_h^{\text{major}}"
        r" + w_{\text{minor}}\, o_h^{\text{minor}}"
        r" + b_{\text{sun}}\, \mathbb{1}[\text{sunrise/sunset in } h]"
        r"\Big)"
    )
    st.markdown(
        f"$o_h$ is the fraction of hour $h$ a Major/Minor period overlaps; "
        f"$w_{{\\text{{major}}}} = {MAJOR_WEIGHT}$, "
        f"$w_{{\\text{{minor}}}} = {MINOR_WEIGHT}$, "
        f"$b_{{\\text{{sun}}}} = {SUN_EVENT_BONUS}$."
    )

    st.markdown("**2. Cold-weather bonus** - deer move more in cold weather:")
    st.latex(
        r"\text{cold} = \min\!\Big(C_{\text{cap}},\ "
        r"\max\big(0,\ \tfrac{T_{\text{base}} - \bar T}{S}\big)\Big)"
    )
    st.markdown(
        f"$\\bar T$ is the window's average temperature; "
        f"$T_{{\\text{{base}}}} = {COLD_BASELINE_F:.0f}^\\circ F$, "
        f"$S = {COLD_BONUS_SCALE:.0f}^\\circ F$, "
        f"$C_{{\\text{{cap}}}} = {COLD_BONUS_CAP:.1f}$."
    )

    st.markdown("**3. Rain/wind penalty** - subtracted from the score:")
    st.latex(r"\text{penalty} = \frac{\bar p}{50} + \frac{\max(0,\ \bar v - 10)}{10}")
    st.markdown(
        r"$\bar p$ is average precipitation chance (%) and $\bar v$ is average wind speed (mph)."
    )

    st.markdown(
        "**4. Pressure sweet-spot bonus** - a flat bonus (not scaled) when the window's own "
        "average barometric pressure falls in a hunting-camp \"sweet spot\" band, read "
        "directly from hourly mean-sea-level pressure data:"
    )
    st.latex(
        r"\text{sweetspot} = \begin{cases}"
        r"B_{\text{band}} & P_{\text{low}} \le \bar P \le P_{\text{high}} \\"
        r"0 & \text{otherwise}"
        r"\end{cases}"
    )
    st.markdown(
        f"$\\bar P$ is the window's average sea-level pressure (inHg); "
        f"$P_{{\\text{{low}}}} = {PRESSURE_BAND_LOW_IN}$, "
        f"$P_{{\\text{{high}}}} = {PRESSURE_BAND_HIGH_IN}$, "
        f"$B_{{\\text{{band}}}} = {PRESSURE_BAND_BONUS:.1f}$."
    )

    st.markdown(
        "**5. Falling-pressure bonus** - a two-tier flat bonus based on how much pressure "
        "has dropped over the lookback window before the window starts - a bigger drop earns "
        "at least as much as a smaller one, since this reads the actual pressure trend "
        "instead of inferring \"a storm is coming\" from a precipitation forecast:"
    )
    st.latex(
        r"\text{dropping} = \begin{cases}"
        r"B_{\text{drop}} & \Delta P \ge \Delta P_{\text{min}} \\"
        r"B_{\text{drop,minor}} & \Delta P_{\text{minor}} \le \Delta P < \Delta P_{\text{min}} \\"
        r"0 & \text{otherwise}"
        r"\end{cases}"
    )
    st.markdown(
        f"$\\Delta P$ is the pressure fall (inHg) over the {PRESSURE_DROP_LOOKBACK_HOURS} hours "
        f"before the window starts; "
        f"$\\Delta P_{{\\text{{minor}}}} = {PRESSURE_DROP_MINOR_THRESHOLD_IN}$, "
        f"$B_{{\\text{{drop,minor}}}} = {PRESSURE_DROP_MINOR_BONUS:.1f}$, "
        f"$\\Delta P_{{\\text{{min}}}} = {PRESSURE_DROP_THRESHOLD_IN}$, "
        f"$B_{{\\text{{drop}}}} = {PRESSURE_DROP_BONUS:.1f}$."
    )

    st.markdown(
        f"The top {CANDIDATE_WINDOW_COUNT} highest-scoring windows are found by sliding "
        f"this {WINDOW_HOURS}-hour window across every possible starting hour in the "
        "forecast, keeping only non-overlapping windows (best score wins any overlap) so "
        "the results represent genuinely different opportunities rather than the same "
        "window shifted by an hour."
    )

    st.markdown("**How this maps to the score chart colors**")
    st.markdown(
        f"The stacked bars group these five terms into three sources, so each bar shows "
        f"at a glance where its score came from:\n\n"
        f"| Chart color | Terms it contains | Can it be negative? |\n"
        f"|---|---|---|\n"
        f"| **{CATEGORY_ACTIVITY}** | 1 (solunar activity) | No |\n"
        f"| **{CATEGORY_WEATHER}** | 2 + 4 - 3 (cold bonus + sweet-spot bonus, "
        f"net of the rain/wind penalty) | Yes - a wet, windy window pulls its bar below zero |\n"
        f"| **{CATEGORY_PRESSURE_DROP}** | 5 (falling-pressure bonus) | No |\n\n"
        f"The three stacked segments always sum to the window's total score."
    )
