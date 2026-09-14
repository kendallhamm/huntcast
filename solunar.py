"""
Standalone Streamlit app: solunar "feeding times" forecast (hunting/fishing
major/minor activity windows) for any postal/zip code worldwide.

Fully self-contained - no dependency on, or import of, anything outside
this folder. No API keys, no .env, no secrets: geocoding is a public
Zippopotam.us lookup, timezone resolution is a public Open-Meteo lookup,
and the solunar math itself runs locally via the `ephem` astronomy
library - no external solunar service involved.

Run: streamlit run solunar.py
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import ephem
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

# Hunting-camp folklore: deer move more in the ~24 hours before a rain
# system moves in (falling pressure ahead of the front), not during the
# rain itself. A window gets a flat bonus when it's still reasonably dry
# (its own average precip chance under PRE_RAIN_PRECIP_THRESHOLD) but
# precipitation chance climbs to/above that threshold within
# PRE_RAIN_LOOKAHEAD_HOURS after it ends.
PRE_RAIN_LOOKAHEAD_HOURS = 24
PRE_RAIN_PRECIP_THRESHOLD = 50  # percent
PRE_RAIN_BONUS = 1.5

# How many top-scoring, non-overlapping windows to search for; the UI
# only shows the top 3 of these, same as the no-LLM fallback on the
# Slack-bot side.
CANDIDATE_WINDOW_COUNT = 5


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_hourly_weather(lat, lon, days):
    """Hourly temp/precip-chance/wind for `days` days at (lat, lon), via
    Open-Meteo (free, no key) - same service and timezone="auto"
    resolution as lookup_timezone() above, so these hours line up with
    the days_data fetch_solunar() produces for the same location.
    Returns a list of {'dt', 'temp_f', 'precip_chance', 'wind_mph'}
    dicts (naive local datetimes), one per hour, or None on failure."""
    try:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "hourly": "temperature_2m,precipitation_probability,wind_speed_10m",
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
                "timezone": "auto",
                "forecast_days": days,
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
        return [
            {
                "dt": datetime.fromisoformat(t),
                "temp_f": temps[i] if i < len(temps) else None,
                "precip_chance": precips[i] if i < len(precips) else None,
                "wind_mph": winds[i] if i < len(winds) else None,
            }
            for i, t in enumerate(times)
        ]
    except (requests.RequestException, ValueError, KeyError):
        return None


def _summarize_weather(samples):
    """Average temp/wind, peak precip chance across a set of hourly
    samples - None if there's no weather data at all (rather than a
    dict of Nones), so callers can tell "no data" apart from "data says
    calm and dry."."""
    if not samples:
        return None
    temps = [s["temp_f"] for s in samples if s["temp_f"] is not None]
    precips = [s["precip_chance"] for s in samples if s["precip_chance"] is not None]
    winds = [s["wind_mph"] for s in samples if s["wind_mph"] is not None]
    return {
        "temp_avg": round(sum(temps) / len(temps)) if temps else None,
        "precip_max": max(precips) if precips else None,
        "wind_avg": round(sum(winds) / len(winds), 1) if winds else None,
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


def _upcoming_rain_chance(timeline, start_idx, window_hours):
    """Max precipitation chance in the PRE_RAIN_LOOKAHEAD_HOURS right
    after timeline[start_idx:start_idx+window_hours] ends, or None if
    there's no weather data in that lookahead span. Shared by
    score_window() (the "before the rain" bonus) and format_window() (so
    the displayed text can reference the same signal that earned the
    bonus)."""
    lookahead = timeline[start_idx + window_hours: start_idx + window_hours + PRE_RAIN_LOOKAHEAD_HOURS]
    precips = [h["weather"]["precip_max"] for h in lookahead if h["weather"] and h["weather"]["precip_max"] is not None]
    return max(precips) if precips else None


def score_window(timeline, start_idx, window_hours=WINDOW_HOURS):
    """Combined goodness score for timeline[start_idx:start_idx+window_hours]:
    summed solunar activity, a cold-weather bonus, a "rain is coming"
    bonus, and a precipitation/wind penalty. Returns None if the window
    would run past the end of the timeline, or if it has neither solunar
    activity nor any weather data at all."""
    hours = timeline[start_idx:start_idx + window_hours]
    if len(hours) < window_hours:
        return None
    if not any(h["events"] for h in hours) and not any(h["weather"] for h in hours):
        return None

    score = sum(h["activity"] for h in hours)
    temps = [h["weather"]["temp_avg"] for h in hours if h["weather"] and h["weather"]["temp_avg"] is not None]
    precips = [h["weather"]["precip_max"] for h in hours if h["weather"] and h["weather"]["precip_max"] is not None]
    winds = [h["weather"]["wind_avg"] for h in hours if h["weather"] and h["weather"]["wind_avg"] is not None]

    if temps:
        avg_temp = sum(temps) / len(temps)
        score += min(COLD_BONUS_CAP, max(0.0, (COLD_BASELINE_F - avg_temp) / COLD_BONUS_SCALE))

    avg_precip = (sum(precips) / len(precips)) if precips else None
    if avg_precip is not None:
        score -= avg_precip / 50.0

    if winds:
        avg_wind = sum(winds) / len(winds)
        score -= max(0.0, avg_wind - 10) / 10.0

    upcoming_precip = _upcoming_rain_chance(timeline, start_idx, window_hours)
    if (
        upcoming_precip is not None
        and upcoming_precip >= PRE_RAIN_PRECIP_THRESHOLD
        and (avg_precip is None or avg_precip < PRE_RAIN_PRECIP_THRESHOLD)
    ):
        score += PRE_RAIN_BONUS

    return score


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
    avg_precip = None
    if weathers:
        temps = [w["temp_avg"] for w in weathers if w["temp_avg"] is not None]
        precips = [w["precip_max"] for w in weathers if w["precip_max"] is not None]
        winds = [w["wind_avg"] for w in weathers if w["wind_avg"] is not None]
        if temps:
            bits.append(f"avg {round(sum(temps) / len(temps))}°F")
        if precips:
            avg_precip = sum(precips) / len(precips)
            bits.append(f"precip up to {max(precips):.0f}%")
        if winds:
            bits.append(f"wind {round(sum(winds) / len(winds), 1)} mph avg")

    upcoming_precip = _upcoming_rain_chance(timeline, start_idx, window_hours)
    if (
        upcoming_precip is not None
        and upcoming_precip >= PRE_RAIN_PRECIP_THRESHOLD
        and (avg_precip is None or avg_precip < PRE_RAIN_PRECIP_THRESHOLD)
    ):
        bits.append(f"rain likely within {PRE_RAIN_LOOKAHEAD_HOURS}h - pre-front movement bump expected")

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
                        "temperature, wind, and rain timing - pure scoring, no AI."
                    )
                    today_date = now_local.date()
                    for i, (_score, start_idx) in enumerate(candidates[:3], start=1):
                        st.write(f"**#{i}** - {format_window(timeline, start_idx, today_date)}")
                    st.divider()
            else:
                st.caption(
                    ":warning: Couldn't fetch the weather forecast, so hunting-window "
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
