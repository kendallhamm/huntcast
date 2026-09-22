"""
Huntcaster - a standalone Streamlit app that ranks the best deer hunting
windows for any postal/zip code worldwide, by combining rut phase,
weather, and solunar major/minor periods.

Fully self-contained - no dependency on, or import of, anything outside
this folder. No API keys, no .env, no secrets: geocoding is a public
Zippopotam.us lookup, timezone resolution and the hourly weather forecast
are public Open-Meteo lookups, and the solunar math itself runs locally
via the `ephem` astronomy library - no external solunar service involved.

On top of the per-day solunar events, rut phase and the hourly forecast
(temperature, precipitation chance, wind, barometric pressure) are
cross-referenced against those events to rank the best 6-hour "hunting
windows" - see the scoring section below for the formula. Windows that
never touch legal shooting light are dropped before scoring.

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
from urllib.parse import urlencode
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

# Hunting at night is illegal everywhere in the US; legal shooting hours
# generally run from some margin before sunrise to the same margin after
# sunset (states vary on the exact margin - many use 30 minutes, some up
# to an hour). This app uses the more conservative 1-hour margin so it
# never recommends a window a stricter state would call illegal, and
# drops windows outright when they get no benefit from the doubt: a
# window is excluded only when it starts at/after that day's own
# sunset+margin AND finishes at/before that morning's sunrise-margin,
# i.e. it never touches legal light at all. A window that only partly
# overlaps the margin (e.g. starts just before legal dawn) is still
# scored and can still be recommended.
LEGAL_LIGHT_MARGIN = timedelta(hours=1)

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
# Net effect: solunar windows are computed and displayed because hunters
# ask for them, but they barely move the ranking, because that is what
# the measurements support.
MAJOR_YPH = 3.0
MINOR_YPH = 0.0

# Rut phase. The single largest effect on fall deer movement. Values are
# the daytime movement deltas against the season mean reported by Neary et
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
#
# Stored as (label, band index, measured yph): band index counts phases
# out from peak, so with the default RUT_BAND_DAYS = 14 the bands are
# pre-rut [-35,-21), early [-21,-7), peak [-7,7), late [7,21) and post
# [21,35) - exactly Neary et al.'s spacing. Keeping the index rather
# than baked-in day offsets is what lets the band width be a dial in
# custom mode; at the default it reproduces the published bands.
RUT_PHASE_YPH = [
    ("Pre-rut", -2, 4.0),
    ("Early rut", -1, 104.0),
    ("Peak rut", 0, 142.0),
    ("Late rut", 1, 78.0),
    ("Post-rut", 2, 9.0),
]
RUT_BAND_DAYS = 14
RUT_PEAK_YPH = 142.0
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

# --- North Carolina county-level peak conception dates --------------------
#
# Transcribed from the NC Wildlife Resources Commission "Estimated Peak
# Conception Dates" map (updated 2025), linked in RUT_DATE_HELP_STATES
# below. The map itself is a raster image, so each county's name/date
# pair was read off the map directly; the accompanying county sample
# sizes come from page 2 of the same PDF and are stored alongside because
# NCWRC explicitly warns that low-sample counties are less precise.
#
# Transcription check: all 100 county names match page 2's sample-size
# table exactly, and the sample counts here sum to 1,776, the total
# printed on that page.
#
# Conception date *is* peak breeding date, i.e. what this app calls peak
# rut, so these values feed the rut term directly with no adjustment.
#
# Graham County has no printed estimate (1 sample); it is stored as None
# and the UI falls back to its season zone's average.
# county -> ((month, day) | None, n_samples)
NC_COUNTY_PEAK_CONCEPTION = {
    "Alamance":        ((11, 12), 12),
    "Alexander":       ((11, 27), 5),
    "Alleghany":       ((11, 28), 10),
    "Anson":           ((11, 24), 12),
    "Ashe":            ((11, 18), 11),
    "Avery":           ((11, 26), 19),
    "Beaufort":        ((10, 28), 8),
    "Bertie":          ((11, 10), 23),
    "Bladen":          ((11, 1), 11),
    "Brunswick":       ((10, 17), 36),
    "Buncombe":        ((11, 30), 18),
    "Burke":           ((12, 7), 42),
    "Cabarrus":        ((12, 2), 6),
    "Caldwell":        ((12, 3), 18),
    "Camden":          ((11, 14), 6),
    "Carteret":        ((10, 17), 31),
    "Caswell":         ((11, 16), 5),
    "Catawba":         ((12, 5), 18),
    "Chatham":         ((11, 12), 13),
    "Cherokee":        ((12, 10), 13),
    "Chowan":          ((11, 8), 13),
    "Clay":            ((12, 10), 9),
    "Cleveland":       ((12, 3), 5),
    "Columbus":        ((10, 19), 19),
    "Craven":          ((11, 4), 38),
    "Cumberland":      ((11, 4), 6),
    "Currituck":       ((11, 14), 6),
    "Dare":            ((11, 20), 7),
    "Davidson":        ((12, 2), 15),
    "Davie":           ((11, 21), 7),
    "Duplin":          ((11, 11), 10),
    "Durham":          ((11, 13), 6),
    "Edgecombe":       ((11, 16), 8),
    "Forsyth":         ((11, 19), 5),
    "Franklin":        ((11, 11), 12),
    "Gaston":          ((11, 24), 17),
    "Gates":           ((11, 5), 25),
    "Graham":          (None, 1),
    "Granville":       ((11, 4), 5),
    "Greene":          ((11, 8), 15),
    "Guilford":        ((11, 13), 19),
    "Halifax":         ((11, 10), 6),
    "Harnett":         ((11, 9), 9),
    "Haywood":         ((12, 15), 10),
    "Henderson":       ((12, 11), 18),
    "Hertford":        ((11, 3), 15),
    "Hoke":            ((11, 3), 7),
    "Hyde":            ((10, 4), 311),
    "Iredell":         ((11, 20), 11),
    "Jackson":         ((12, 15), 5),
    "Johnston":        ((11, 9), 6),
    "Jones":           ((10, 30), 12),
    "Lee":             ((11, 19), 9),
    "Lenoir":          ((11, 2), 7),
    "Lincoln":         ((12, 7), 11),
    "Macon":           ((12, 19), 15),
    "Madison":         ((12, 4), 8),
    "Martin":          ((11, 11), 6),
    "McDowell":        ((12, 3), 14),
    "Mecklenburg":     ((11, 22), 11),
    "Mitchell":        ((11, 23), 16),
    "Montgomery":      ((11, 13), 105),
    "Moore":           ((11, 12), 22),
    "Nash":            ((11, 8), 8),
    "New Hanover":     ((11, 14), 6),
    "Northampton":     ((11, 9), 10),
    "Onslow":          ((11, 1), 65),
    "Orange":          ((11, 9), 7),
    "Pamlico":         ((11, 5), 10),
    "Pasquotank":      ((11, 10), 7),
    "Pender":          ((11, 5), 17),
    "Perquimans":      ((11, 8), 15),
    "Person":          ((11, 14), 9),
    "Pitt":            ((11, 7), 21),
    "Polk":            ((11, 30), 6),
    "Randolph":        ((11, 30), 11),
    "Richmond":        ((11, 15), 33),
    "Robeson":         ((11, 7), 18),
    "Rockingham":      ((11, 13), 20),
    "Rowan":           ((12, 1), 10),
    "Rutherford":      ((12, 12), 12),
    "Sampson":         ((11, 15), 7),
    "Scotland":        ((11, 3), 5),
    "Stanly":          ((11, 17), 45),
    "Stokes":          ((11, 14), 47),
    "Surry":           ((11, 20), 25),
    "Swain":           ((12, 14), 5),
    "Transylvania":    ((12, 9), 9),
    "Tyrrell":         ((10, 16), 6),
    "Union":           ((11, 18), 9),
    "Vance":           ((11, 5), 6),
    "Wake":            ((11, 8), 21),
    "Warren":          ((11, 5), 8),
    "Washington":      ((10, 24), 11),
    "Watauga":         ((11, 19), 10),
    "Wayne":           ((11, 8), 11),
    "Wilkes":          ((11, 30), 29),
    "Wilson":          ((11, 9), 11),
    "Yadkin":          ((11, 28), 12),
    "Yancey":          ((11, 27), 15),
}

# Season-zone averages printed in the map's legend, used as a fallback
# for any county with no county-level estimate.
NC_ZONE_AVERAGES = {
    "Western": (12, 5),
    "Northwestern": (11, 25),
    "Central": (11, 15),
    "Northeastern": (11, 8),
    "Southeastern": (10, 31),
}

# Only needed for counties with no printed date; read off the map's
# heavy zone boundaries. Keyed by NC county, and only ever consulted
# through NC's own entry in COUNTY_LOOKUPS.
NC_COUNTY_ZONE = {"Graham": "Western"}

# NCWRC's own precision caveat: estimates built on few samples are
# shakier. The map gives no threshold, so this is our own cutoff for
# when to show a caution line - deliberately low, to flag only the
# counties sitting at the bottom of the sample distribution.
NC_LOW_SAMPLE_CUTOFF = 5

# --- Georgia county-level peak movement dates ----------------------------
#
# Transcribed from Georgia DNR Wildlife Resources Division's "Peak Deer
# Movement in Georgia" map, linked in RUT_DATE_HELP_STATES below. Unlike
# the NC map this one is a real text layer, so the table was extracted
# rather than read off an image.
#
# Important difference from NC: Georgia publishes a *peak movement week*
# derived from Georgia DOT deer-vehicle-collision data, not a fetal-aged
# conception date. WRD's stated basis for treating it as a rut proxy is a
# UGA/WRD finding of "a strong correlation between peak deer-vehicle
# collision timeframes, deer conception dates and the hourly movement
# rates of deer tracked by GPS." So this is one inferential step further
# from conception than NC's numbers, and the UI says so.
#
# Every published range is exactly 7 days, so the stored peak is the
# midpoint (day 4) of the week; the source week is kept alongside it so
# the UI can show the range rather than implying single-day precision.
#
# Transcription checks: two independent parses (regex over the flowed
# text, and a positional parse off the word layer) agreed on all 159
# counties with zero date mismatches; all 159 ranges are 7 days long; and
# the 11 distinct week-starts found match the 11 buckets printed in the
# map's own legend.
#
# The map's legend defines an asterisk for counties with fewer than 100
# collisions, but no county in this edition carries one - verified in
# both the text layer and the rendered page - so no GA county is flagged
# low-confidence here.
# county -> ((month, day) week midpoint, "MM/DD-MM/DD" source week)
GA_COUNTY_PEAK_MOVEMENT = {
    "Appling":         ((11, 6), "11/03-11/09"),
    "Atkinson":        ((10, 23), "10/20-10/26"),
    "Bacon":           ((10, 30), "10/27-11/02"),
    "Baker":           ((11, 27), "11/24-11/30"),
    "Baldwin":         ((10, 30), "10/27-11/02"),
    "Banks":           ((11, 27), "11/24-11/30"),
    "Barrow":          ((11, 13), "11/10-11/16"),
    "Bartow":          ((11, 6), "11/03-11/09"),
    "Ben Hill":        ((10, 16), "10/13-10/19"),
    "Berrien":         ((11, 6), "11/03-11/09"),
    "Bibb":            ((11, 6), "11/03-11/09"),
    "Bleckley":        ((11, 6), "11/03-11/09"),
    "Brantley":        ((10, 23), "10/20-10/26"),
    "Brooks":          ((11, 20), "11/17-11/23"),
    "Bryan":           ((10, 23), "10/20-10/26"),
    "Bulloch":         ((10, 23), "10/20-10/26"),
    "Burke":           ((10, 23), "10/20-10/26"),
    "Butts":           ((11, 6), "11/03-11/09"),
    "Calhoun":         ((11, 27), "11/24-11/30"),
    "Camden":          ((10, 16), "10/13-10/19"),
    "Candler":         ((10, 16), "10/13-10/19"),
    "Carroll":         ((11, 13), "11/10-11/16"),
    "Catoosa":         ((11, 13), "11/10-11/16"),
    "Charlton":        ((10, 23), "10/20-10/26"),
    "Chatham":         ((10, 23), "10/20-10/26"),
    "Chattahoochee":   ((11, 13), "11/10-11/16"),
    "Chattooga":       ((11, 6), "11/03-11/09"),
    "Cherokee":        ((11, 13), "11/10-11/16"),
    "Clarke":          ((11, 13), "11/10-11/16"),
    "Clay":            ((10, 23), "10/20-10/26"),
    "Clayton":         ((11, 6), "11/03-11/09"),
    "Clinch":          ((10, 23), "10/20-10/26"),
    "Cobb":            ((11, 6), "11/03-11/09"),
    "Coffee":          ((10, 23), "10/20-10/26"),
    "Colquitt":        ((11, 20), "11/17-11/23"),
    "Columbia":        ((10, 23), "10/20-10/26"),
    "Cook":            ((11, 20), "11/17-11/23"),
    "Coweta":          ((11, 13), "11/10-11/16"),
    "Crawford":        ((11, 6), "11/03-11/09"),
    "Crisp":           ((11, 20), "11/17-11/23"),
    "Dade":            ((11, 13), "11/10-11/16"),
    "Dawson":          ((11, 20), "11/17-11/23"),
    "DeKalb":          ((11, 6), "11/03-11/09"),
    "Decatur":         ((12, 11), "12/08-12/14"),
    "Dodge":           ((11, 6), "11/03-11/09"),
    "Dooly":           ((11, 6), "11/03-11/09"),
    "Dougherty":       ((11, 27), "11/24-11/30"),
    "Douglas":         ((11, 13), "11/10-11/16"),
    "Early":           ((12, 18), "12/15-12/21"),
    "Echols":          ((10, 30), "10/27-11/02"),
    "Effingham":       ((10, 23), "10/20-10/26"),
    "Elbert":          ((11, 6), "11/03-11/09"),
    "Emanuel":         ((10, 23), "10/20-10/26"),
    "Evans":           ((10, 23), "10/20-10/26"),
    "Fannin":          ((11, 20), "11/17-11/23"),
    "Fayette":         ((11, 13), "11/10-11/16"),
    "Floyd":           ((11, 6), "11/03-11/09"),
    "Forsyth":         ((11, 13), "11/10-11/16"),
    "Franklin":        ((11, 13), "11/10-11/16"),
    "Fulton":          ((11, 13), "11/10-11/16"),
    "Gilmer":          ((11, 13), "11/10-11/16"),
    "Glascock":        ((10, 23), "10/20-10/26"),
    "Glynn":           ((10, 16), "10/13-10/19"),
    "Gordon":          ((11, 6), "11/03-11/09"),
    "Grady":           ((12, 11), "12/08-12/14"),
    "Greene":          ((11, 6), "11/03-11/09"),
    "Gwinnett":        ((11, 13), "11/10-11/16"),
    "Habersham":       ((11, 27), "11/24-11/30"),
    "Hall":            ((11, 13), "11/10-11/16"),
    "Hancock":         ((11, 6), "11/03-11/09"),
    "Haralson":        ((11, 6), "11/03-11/09"),
    "Harris":          ((11, 13), "11/10-11/16"),
    "Hart":            ((10, 30), "10/27-11/02"),
    "Heard":           ((11, 13), "11/10-11/16"),
    "Henry":           ((11, 6), "11/03-11/09"),
    "Houston":         ((11, 6), "11/03-11/09"),
    "Irwin":           ((11, 27), "11/24-11/30"),
    "Jackson":         ((11, 13), "11/10-11/16"),
    "Jasper":          ((10, 30), "10/27-11/02"),
    "Jeff Davis":      ((11, 6), "11/03-11/09"),
    "Jefferson":       ((10, 23), "10/20-10/26"),
    "Jenkins":         ((10, 16), "10/13-10/19"),
    "Johnson":         ((11, 6), "11/03-11/09"),
    "Jones":           ((11, 6), "11/03-11/09"),
    "Lamar":           ((11, 6), "11/03-11/09"),
    "Lanier":          ((10, 30), "10/27-11/02"),
    "Laurens":         ((11, 6), "11/03-11/09"),
    "Lee":             ((11, 20), "11/17-11/23"),
    "Liberty":         ((10, 23), "10/20-10/26"),
    "Lincoln":         ((10, 30), "10/27-11/02"),
    "Long":            ((10, 23), "10/20-10/26"),
    "Lowndes":         ((11, 6), "11/03-11/09"),
    "Lumpkin":         ((11, 27), "11/24-11/30"),
    "Macon":           ((11, 6), "11/03-11/09"),
    "Madison":         ((11, 13), "11/10-11/16"),
    "Marion":          ((11, 6), "11/03-11/09"),
    "McDuffie":        ((10, 23), "10/20-10/26"),
    "McIntosh":        ((10, 16), "10/13-10/19"),
    "Meriwether":      ((11, 6), "11/03-11/09"),
    "Miller":          ((12, 18), "12/15-12/21"),
    "Mitchell":        ((11, 27), "11/24-11/30"),
    "Monroe":          ((10, 30), "10/27-11/02"),
    "Montgomery":      ((11, 6), "11/03-11/09"),
    "Morgan":          ((11, 6), "11/03-11/09"),
    "Murray":          ((11, 13), "11/10-11/16"),
    "Muscogee":        ((11, 13), "11/10-11/16"),
    "Newton":          ((11, 6), "11/03-11/09"),
    "Oconee":          ((11, 13), "11/10-11/16"),
    "Oglethorpe":      ((11, 6), "11/03-11/09"),
    "Paulding":        ((11, 6), "11/03-11/09"),
    "Peach":           ((11, 6), "11/03-11/09"),
    "Pickens":         ((11, 13), "11/10-11/16"),
    "Pierce":          ((11, 6), "11/03-11/09"),
    "Pike":            ((11, 6), "11/03-11/09"),
    "Polk":            ((11, 6), "11/03-11/09"),
    "Pulaski":         ((11, 6), "11/03-11/09"),
    "Putnam":          ((10, 30), "10/27-11/02"),
    "Quitman":         ((12, 4), "12/01-12/07"),
    "Rabun":           ((11, 27), "11/24-11/30"),
    "Randolph":        ((11, 20), "11/17-11/23"),
    "Richmond":        ((10, 23), "10/20-10/26"),
    "Rockdale":        ((11, 6), "11/03-11/09"),
    "Schley":          ((10, 30), "10/27-11/02"),
    "Screven":         ((10, 23), "10/20-10/26"),
    "Seminole":        ((12, 25), "12/22-12/28"),
    "Spalding":        ((11, 6), "11/03-11/09"),
    "Stephens":        ((11, 27), "11/24-11/30"),
    "Stewart":         ((11, 20), "11/17-11/23"),
    "Sumter":          ((11, 13), "11/10-11/16"),
    "Talbot":          ((11, 13), "11/10-11/16"),
    "Taliaferro":      ((11, 6), "11/03-11/09"),
    "Tattnall":        ((11, 6), "11/03-11/09"),
    "Taylor":          ((10, 30), "10/27-11/02"),
    "Telfair":         ((10, 16), "10/13-10/19"),
    "Terrell":         ((11, 27), "11/24-11/30"),
    "Thomas":          ((11, 27), "11/24-11/30"),
    "Tift":            ((11, 20), "11/17-11/23"),
    "Toombs":          ((11, 6), "11/03-11/09"),
    "Towns":           ((11, 20), "11/17-11/23"),
    "Treutlen":        ((11, 6), "11/03-11/09"),
    "Troup":           ((11, 13), "11/10-11/16"),
    "Turner":          ((11, 13), "11/10-11/16"),
    "Twiggs":          ((11, 6), "11/03-11/09"),
    "Union":           ((11, 20), "11/17-11/23"),
    "Upson":           ((11, 6), "11/03-11/09"),
    "Walker":          ((11, 6), "11/03-11/09"),
    "Walton":          ((11, 6), "11/03-11/09"),
    "Ware":            ((10, 23), "10/20-10/26"),
    "Warren":          ((10, 30), "10/27-11/02"),
    "Washington":      ((10, 30), "10/27-11/02"),
    "Wayne":           ((11, 6), "11/03-11/09"),
    "Webster":         ((11, 27), "11/24-11/30"),
    "Wheeler":         ((11, 6), "11/03-11/09"),
    "White":           ((11, 27), "11/24-11/30"),
    "Whitfield":       ((11, 13), "11/10-11/16"),
    "Wilcox":          ((11, 13), "11/10-11/16"),
    "Wilkes":          ((11, 6), "11/03-11/09"),
    "Wilkinson":       ((10, 30), "10/27-11/02"),
    "Worth":           ((11, 20), "11/17-11/23"),
}


def _nc_county_entry(county):
    """Unified lookup record for one NC county."""
    month_day, n_samples = NC_COUNTY_PEAK_CONCEPTION[county]
    if month_day is None:
        zone = NC_COUNTY_ZONE[county]
        return {
            "peak": NC_ZONE_AVERAGES[zone],
            "estimated": False,
            "detail": (
                f"No county-level estimate published "
                f"({n_samples} sample{'' if n_samples == 1 else 's'}). "
                f"Showing the {zone} season-zone average instead."
            ),
            "caution": None,
        }
    caution = None
    if n_samples <= NC_LOW_SAMPLE_CUTOFF:
        caution = (
            f"Based on only {n_samples} reproductive "
            f"sample{'' if n_samples == 1 else 's'}. NCWRC notes that "
            "estimates from few samples are less precise, so treat this "
            "as a rough date."
        )
    return {
        "peak": month_day,
        "estimated": True,
        "detail": f"Median conception date, from {n_samples} reproductive samples.",
        "caution": caution,
    }


def _ga_county_entry(county):
    """Unified lookup record for one GA county."""
    month_day, week = GA_COUNTY_PEAK_MOVEMENT[county]
    return {
        "peak": month_day,
        "estimated": True,
        "detail": (
            f"Midpoint of Georgia WRD's peak movement week ({week}), "
            "which WRD reports as correlated with conception dates."
        ),
        "caution": None,
    }


# --- New York regional peak rut dates ------------------------------------
#
# Cheatum, E.L. and G.H. Morton. 1946. "Breeding Season of White-Tailed
# Deer in New York." Journal of Wildlife Management 10(3): 249-263, at
# p. 258. <https://www.jstor.org/stable/3795841>
#
# New York is deliberately NOT broken out by county: Cheatum and Morton
# work at the scale of a north/south regional contrast, so a county
# dropdown would imply a precision this source does not have.
#
# Verification: p. 258 was reviewed directly by the repo owner, who
# confirmed both dates against the paper (Sept 2026). It is paywalled on
# JSTOR, so it is not machine-checkable from this repo. Independent
# search separately confirms the paper's framing: it contrasts northern
# with southern New York herds, which is the split used here. For
# context, a secondary Adirondack source puts peak Adirondack breeding
# at November 10, three days off the northern figure below.
#
# Second caveat, on age: this is a 1946 study, by some margin the oldest
# source in this file. New York's deer range, densities and herd
# structure have all changed since. Photoperiod drives estrus timing, so
# the dates should be relatively stable - but they have not been
# re-derived from modern data here.
#
# Not to be confused with the same authors' companion paper "Regional
# Differences in Breeding Potential of White-Tailed Deer in New York,"
# which is about breeding *potential*, not breeding dates.
NY_REGION_PEAK = {
    "Northern New York": (11, 13),
    "Southern New York": (11, 20),
}


def _ny_region_entry(region):
    """Unified lookup record for one NY region."""
    return {
        "peak": NY_REGION_PEAK[region],
        "estimated": True,
        "detail": (
            "Regional estimate - New York is split north/south, not by "
            "county. Cheatum & Morton 1946, J. Wildlife Management "
            "10(3):249-263, p. 258."
        ),
        "caution": None,
    }


# --- Texas ecoregion peak breeding dates ---------------------------------
#
# Texas Parks and Wildlife Department, "The Rut in White-tailed Deer."
# <https://tpwd.texas.gov/huntwild/hunt/planning/rut_whitetailed_deer/>
#
# Study design: TPWD examined 2,436 does across 16 study areas covering
# the state's ecoregions over three years, aging fetuses by length to
# back-calculate conception dates. TPWD publishes a peak breeding date
# per study area, which is why this lookup has 16 entries - they are the
# study areas, not administrative units.
#
# Texas is by ecoregion rather than county for the same reason New York
# is by region: that is the scale the source works at. A hunter who
# doesn't know their ecoregion can find it from the county lists TPWD
# publishes alongside the map.
#
# Each entry also carries the ecoregion's full published breeding range,
# shown in the UI, because several of these ranges are very wide (South
# Texas runs Nov 9 - Feb 1) and a single peak date badly understates
# that spread.
# region -> ((month, day) peak, "published breeding range")
TX_ECOREGION_PEAK = {
    "Cross Timbers (north)":        ((11, 15), "Oct 13 - Dec 17"),
    "Cross Timbers (south)":        ((11, 17), "Oct 13 - Dec 17"),
    "Edwards Plateau (east)":       ((11, 7),  "Oct 9 - Jan 30"),
    "Edwards Plateau (central)":    ((11, 24), "Oct 9 - Jan 30"),
    "Edwards Plateau (west)":       ((12, 5),  "Oct 9 - Jan 30"),
    "Gulf Prairies and Marshes (north)": ((9, 30),  "Aug 24 - Nov 25"),
    "Gulf Prairies and Marshes (south)": ((10, 31), "Aug 24 - Nov 25"),
    "Pineywoods (north)":           ((11, 22), "Oct 21 - Jan 5"),
    "Pineywoods (south)":           ((11, 12), "Oct 21 - Jan 5"),
    "Post Oak Savannah (central)":  ((11, 10), "Sep 30 - Jan 16"),
    "Post Oak Savannah (south)":    ((11, 11), "Sep 30 - Jan 16"),
    "Rolling Plains (north)":       ((12, 3),  "Oct 8 - Dec 30"),
    "Rolling Plains (south)":       ((11, 20), "Oct 8 - Dec 30"),
    "South Texas Plains (east)":    ((12, 16), "Nov 9 - Feb 1"),
    "South Texas Plains (west)":    ((12, 24), "Nov 9 - Feb 1"),
    "Trans-Pecos":                  ((12, 8),  "Nov 4 - Jan 4"),
}


def _tx_region_entry(region):
    """Unified lookup record for one TX ecoregion."""
    month_day, span = TX_ECOREGION_PEAK[region]
    return {
        "peak": month_day,
        "estimated": True,
        "detail": (
            f"TPWD peak breeding date for this study area. Published "
            f"breeding range for the ecoregion: {span}."
        ),
        "caution": None,
    }


# --- Statewide single-date states ----------------------------------------
#
# Northern states have a far more synchronised rut than the South: the
# published spread within a state is small enough that no agency breaks
# it out below the state level. For these, one statewide date is the
# honest unit - a county picker would invent structure the source does
# not have, the same objection that keeps the isobar-map states out.
#
# Only states with a primary source get an entry here. Wisconsin and
# Ohio were considered and deliberately left out: the Wisconsin figure
# (Hunsaker et al. 2025, cited elsewhere in this project) covers only the
# southwest of the state and is a movement-changepoint window rather than
# a conception date, and the Ohio figure traces to Nixon 1971, which is a
# date range read secondhand. Michigan, Iowa, New Jersey, Vermont, New
# Hampshire and Maine turned up no agency primary source at all.
#
# state -> ((month, day), detail line)
STATEWIDE_PEAK = {
    "Illinois": (
        (11, 8),
        "Mean conception date for adult does. Green et al. 2017, "
        "Theriogenology 94:71-78 - 3,884 does and 4,781 fetuses collected "
        "over ten years from 2003. Yearlings averaged Nov 11 and fawns "
        "Dec 2, so a herd skewed young breeds later than this date.",
    ),
    "Pennsylvania": (
        (11, 15),
        "Median conception falls Nov 11-17, with peak breeding in "
        "mid-November. Pennsylvania Game Commission fetal aging of 3,507 "
        "road-killed does, 1999-2006. This is also the source of this "
        "app's default Nov 15 rut date.",
    ),
}


def _statewide_entry(state):
    """Unified lookup record for a state with a single statewide date."""
    month_day, detail = STATEWIDE_PEAK[state]
    return {"peak": month_day, "estimated": True, "detail": detail, "caution": None}


# Per-state county tables, built once at import. Each state's table is
# built only from that state's own source data, and the UI only ever
# reads the table for the state the user picked - so a North Carolina
# selection can never surface a Georgia county, or vice versa.
COUNTY_LOOKUPS = {
    "North Carolina": {
        county: _nc_county_entry(county) for county in NC_COUNTY_PEAK_CONCEPTION
    },
    "Georgia": {
        county: _ga_county_entry(county) for county in GA_COUNTY_PEAK_MOVEMENT
    },
    "New York": {
        region: _ny_region_entry(region) for region in NY_REGION_PEAK
    },
    "Texas": {
        region: _tx_region_entry(region) for region in TX_ECOREGION_PEAK
    },
}

# States with no sub-state breakdown - rendered without an area picker.
STATEWIDE_LOOKUPS = {
    state: _statewide_entry(state) for state in STATEWIDE_PEAK
}

# NC and GA share a number of county names (Macon, Jackson, Burke,
# Union, Warren, Wilkes, Cherokee, Clay and others) with *different*
# peak dates - NC's Macon is Dec 19, Georgia's is Nov 6. A county name
# is therefore only ever meaningful together with its state, and nothing
# in this module looks a county up without one: every read goes through
# COUNTY_LOOKUPS[state][county]. There is deliberately no flat
# county -> date mapping anywhere.

# Per-state lookups a user can consult if they don't know their local peak
# rut date. Only NC is wired up for now; add more states as county-level
# conception/breeding-date sources are found and verified.
RUT_DATE_HELP_STATES = {
    "North Carolina": {
        "url": "https://www.ncwildlife.gov/media/4373/download?attachment",
        "caption": (
            "NC Wildlife Resources Commission: median conception date by county. "
            "Conception date is peak breeding date, i.e. what this app calls "
            "\"peak rut\" - this varies by county, so pick the county you plan "
            "to hunt, not just the one you live in."
        ),
        "counties": COUNTY_LOOKUPS["North Carolina"],
    },
    "Georgia": {
        "url": "https://georgiawildlife.com/sites/default/files/wrd/pdf/research/Georgia-Rut-Map.pdf",
        "caption": (
            "Georgia DNR Wildlife Resources Division: peak deer *movement* "
            "week by county, mapped from Georgia DOT deer-vehicle-collision "
            "data. WRD reports that collision timing, conception dates and "
            "GPS movement rates correlate strongly, which is what makes this "
            "usable as a rut date - but it is a step further from conception "
            "than North Carolina's numbers. Many GA herds were restocked "
            "decades ago from out-of-state stock and kept their ancestral "
            "breeding clock, so adjacent counties can peak weeks apart - pick "
            "your county, not a regional average."
        ),
        "counties": COUNTY_LOOKUPS["Georgia"],
    },
    "New York": {
        "url": "https://www.jstor.org/stable/3795841",
        "source_label": "the source paper (JSTOR)",
        "area_label": "Region",
        "caption": (
            "Cheatum & Morton 1946, \"Breeding Season of White-Tailed Deer in "
            "New York\" (J. Wildlife Management 10(3):249-263, p. 258). New "
            "York is split north/south rather than by county, because that is "
            "the scale this source works at. Note this is a 1946 study - the "
            "oldest source behind any date in this app."
        ),
        "counties": COUNTY_LOOKUPS["New York"],
    },
    "Texas": {
        "url": "https://tpwd.texas.gov/huntwild/hunt/planning/rut_whitetailed_deer/",
        "source_label": "TPWD's rut page",
        "area_label": "Ecoregion",
        "caption": (
            "Texas Parks and Wildlife Department: peak breeding date per "
            "ecoregion study area, from fetal aging of 2,436 does across 16 "
            "study areas over three years. Texas is by ecoregion rather than "
            "county because that is the scale the study works at - TPWD's page "
            "lists which counties fall in each ecoregion."
        ),
        "counties": COUNTY_LOOKUPS["Texas"],
    },
    "Illinois": {
        "url": "https://doi.org/10.1016/j.theriogenology.2017.02.010",
        "source_label": "the source study (Green et al. 2017)",
        "caption": (
            "Illinois publishes no sub-state breakdown - the rut is "
            "synchronised enough statewide that the source reports a single "
            "date for the whole state, so this lookup does the same. Note "
            "this is the adult-doe figure; younger does breed later."
        ),
        "statewide": STATEWIDE_LOOKUPS["Illinois"],
    },
    "Pennsylvania": {
        "url": (
            "https://www.pa.gov/agencies/pgc/wildlife/discover-pa-wildlife/"
            "white-tailed-deer/when-is-the-rut"
        ),
        "source_label": "the PA Game Commission's rut page",
        "caption": (
            "Pennsylvania reports one statewide window rather than a "
            "county-level breakdown, so this lookup gives a single date."
        ),
        "statewide": STATEWIDE_LOOKUPS["Pennsylvania"],
    },
    # --- Link-out only below ---------------------------------------------
    #
    # These states publish their breeding dates as smooth contour
    # (isobar) maps whose bands cross county lines freely, rather than as
    # one value per county. Mississippi's Holmes County alone spans three
    # date bands; LDWF notes outright that several Louisiana parishes have
    # two or more distinct breeding periods. Picking one date per county
    # would invent a precision the source does not have, so these link out
    # to the map the way South Carolina does.
    #
    # (A second reason not to force them into the picker: both states run
    # well into January and February, and _season_date assumes a peak
    # falls in the same autumn calendar year. That assumption would need
    # revisiting before any Jan/Feb state is added to the lookup.)
    "Louisiana": {
        "url": "https://www.wlf.louisiana.gov/page/deer-breeding-periods",
        "source_label": "the LDWF breeding-period map",
        "caption": (
            "Louisiana DWF: breeding dates from fetal measurements, drawn as "
            "a contour map rather than county-by-county - the bands cross "
            "parish lines, and LDWF notes several parishes have two or more "
            "breeding periods, largely from historic restocking. Statewide "
            "the range runs late September to late February, so find your "
            "spot on the map rather than assuming a parish-wide date."
        ),
    },
    "Mississippi": {
        "url": (
            "https://www.mdwfp.com/wildlife-hunting/wildlife-species-program/"
            "deer-program/deer-breeding-date-map"
        ),
        "source_label": "the MDWFP breeding-date map",
        "caption": (
            "MDWFP: simulated mean conception dates from 20+ years of deer "
            "health checks, drawn as isobars rather than county-by-county - a "
            "single county can span three date bands. Breeding runs from "
            "about Nov 30 in the northwest to early February in the "
            "southeast, so read your location off the map."
        ),
    },
    "South Carolina": {
        "url": "https://www.dnr.sc.gov/wildlife/deer/reproductionmap.html",
        "source_label": "the SC DNR reproduction map",
        "caption": (
            "SC DNR: peak breeding dates by region (this one is regional, not "
            "county-by-county like NC/GA). Coastal counties peak in mid-"
            "October; most of the state peaks late October-early November; a "
            "small band near Seneca/Greenville peaks late November-early "
            "December."
        ),
    },
}

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
# The Penn State Deer-Forest Study's storm analysis does not support a
# larger rain penalty: its two years point in opposite directions (2016:
# 102 yph outside storms vs 113 during; 2017: 111 vs 98) and it concludes
# there was no significant effect, so it establishes neither the size nor
# the direction of one. The penalty's *direction* is a judgment call.
# Wind additionally doesn't engage at all below the threshold.
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
# evidence ever appears, and the band is reported in the window text for
# hunters who track it.
HPA_PER_INHG = 33.8639

PRESSURE_DROP_LOOKBACK_HOURS = 24
PRESSURE_DROP_MINOR_THRESHOLD_IN = 0.2
PRESSURE_DROP_MINOR_YPH = 2.5
PRESSURE_DROP_THRESHOLD_IN = 0.4
PRESSURE_DROP_YPH = 5.0

PRESSURE_BAND_LOW_IN = 29.8
PRESSURE_BAND_HIGH_IN = 30.3
PRESSURE_BAND_YPH = 0.0


# ---------------------------------------------------------------------------
# TUNABLE WEIGHTS
# ---------------------------------------------------------------------------
#
# Every constant above that a user is allowed to move in "build your own
# formula" mode is re-declared here as a WeightSpec, and the scoring
# functions below read from a weights dict rather than from the module
# constants. `DEFAULT_WEIGHTS` is built from the `default` field of each
# spec, and each of those defaults IS the constant above - so the default
# dict reproduces Kendall's formula exactly, and there is one place to
# look to see what is research-derived and what a user has moved.
#
# Two kinds of dial, per the README's split:
#   - WEIGHT: how big an effect is, in yph. These are the numbers that
#     trace to a study (or, for the weather terms, to a bounded judgment
#     call about a study's evidential standing).
#   - SHAPE: where an effect switches on or how far it spreads - degrees
#     below normal, mph, minutes, days. These are mostly definitional
#     choices carried over from how a study measured its effect.
#
# `snippet` and `cite` are shown verbatim in the UI next to each slider.
# They are condensed from the comment blocks above and from README.md;
# nothing here asserts a finding that isn't already documented there.

WEIGHT = "weight"
SHAPE = "shape"

GROUP_ACTIVITY = "Daily activity"
GROUP_RUT = "Rut"
GROUP_WEATHER = "Weather"


class WeightSpec:
    """One user-movable dial: its range and step, the researched value it
    starts at, and the research that value comes from."""

    def __init__(self, key, group, kind, label, unit, lo, hi, step, default,
                 caption, snippet, cite, fmt="{:.0f}"):
        self.key = key
        self.group = group
        self.kind = kind
        self.label = label
        self.unit = unit
        self.lo = lo
        self.hi = hi
        self.step = step
        self.default = default
        self.caption = caption
        self.snippet = snippet
        self.cite = cite
        self.fmt = fmt

    def show(self, value):
        return f"{self.fmt.format(value)} {self.unit}".strip()

    def snap(self, value):
        """`value` clamped into range and rounded onto this dial's step,
        so a hand-edited or stale share link can only ever produce a
        position the slider itself could have produced."""
        value = min(self.hi, max(self.lo, value))
        return round(self.lo + round((value - self.lo) / self.step) * self.step, 4)


WEIGHT_SPECS = [
    # --- Daily activity ---------------------------------------------------
    WeightSpec(
        "crepuscular_yph", GROUP_ACTIVITY, WEIGHT,
        "Dawn/dusk bonus", "yph", 0.0, 100.0, 1.0, CREPUSCULAR_YPH,
        "+48 yph measured - the most consistently replicated effect in the literature",
        "Within one hour of sunrise or sunset, bucks averaged 317 yph against the "
        "269 yph season mean (+48), and were bedded 21% of the time vs. 34% "
        "overall. Webb et al. 2010 reach the same conclusion from an independent "
        "7-year Oklahoma data set: routine crepuscular movement - not weather, not "
        "moon - is the dominant driver of fine-scale deer movement.",
        "Neary et al. 2025 (48 GPS-collared bucks, central Mississippi, 15-min "
        "fixes, Sept-Feb, 2 years); Webb et al. 2010 (32 deer, 7 years, Oklahoma)",
    ),
    WeightSpec(
        "crepuscular_half_min", GROUP_ACTIVITY, SHAPE,
        "Dawn/dusk halo half-width", "min", 15.0, 180.0, 15.0, 60.0,
        "+/-60 min - the band the +48 yph was measured over",
        "Sunrise and sunset are instants, but the effect isn't. Neary et al. "
        "defined the crepuscular effect over a +/-1 hour band around each event, "
        "so that is the band scored here. Widening this spreads the same bonus "
        "over more hours of a window; narrowing it concentrates the bonus into "
        "fewer.",
        "Neary et al. 2025",
    ),
    WeightSpec(
        "major_yph", GROUP_ACTIVITY, WEIGHT,
        "Solunar major (moon overhead/underfoot)", "yph", 0.0, 60.0, 1.0, MAJOR_YPH,
        "+3 yph measured - but three studies disagree, in both directions",
        "Neary et al. compared each buck against his own usual movement at the "
        "same time of day (which nets out rut phase and individual personality) "
        "and got +3 yph for majors - indistinguishable from zero next to a 269 yph "
        "baseline. Two other studies test solunar directly and disagree with Neary "
        "AND with each other: Sullivan et al. 2016 found major-period activity "
        "FELL near a new/full moon (0.540 -> 0.413 overhead), while Swartout and "
        "Ditchkoff 2025 found top-rated days gave 3.02x and 2.83x activity odds "
        "during moon underfoot/overhead. Swartout is the strongest pro-solunar "
        "result located; it reports odds of being 'active' rather than a movement "
        "rate, so converting it into yph would mean inventing a conversion. If you "
        "think solunar deserves more weight, this is the dial to raise.",
        "Neary et al. 2025; Sullivan et al. 2016 (38 bucks, South Carolina); "
        "Swartout and Ditchkoff 2025 (22 bucks, high-fenced Alabama)",
    ),
    WeightSpec(
        "minor_yph", GROUP_ACTIVITY, WEIGHT,
        "Solunar minor (moonrise/moonset)", "yph", -10.0, 60.0, 1.0, MINOR_YPH,
        "-0.1 yph measured, scored at 0",
        "Neary et al. measured -0.1 yph for minor periods. Sullivan et al. 2016 is "
        "the one study pointing the other way: near a new/full moon, MINOR-period "
        "activity rose (moonrise 0.384 -> 0.564, moonset 0.403 -> 0.591) - the "
        "opposite of the traditional chart, which rates minors as the weaker "
        "period. Swartout and Ditchkoff 2025 found minors weakest of all (0.30x "
        "and 0.37x odds). What all three agree on: majors matter more than minors, "
        "and minors are neutral-to-negative.",
        "Neary et al. 2025; Sullivan et al. 2016; Swartout and Ditchkoff 2025",
    ),

    # --- Rut --------------------------------------------------------------
    WeightSpec(
        "rut_peak_yph", GROUP_RUT, WEIGHT,
        "Peak-rut bonus", "yph", 0.0, 250.0, 2.0, RUT_PEAK_YPH,
        "+142 yph measured - the largest single effect in this model",
        "Daytime movement deltas against the season mean, by day offset from peak "
        "breeding: pre-rut +4, early rut +104, peak +142, late rut +78, post-rut "
        "+9. This one dial scales the whole ladder - the other four phases keep "
        "their measured ratios to peak - because the shape of the ladder is what "
        "the study establishes, and disagreeing with its overall size is the "
        "realistic disagreement to have. These are BUCK movement rates, and the "
        "offsets are relative to the peak breeding date you enter above.",
        "Neary et al. 2025, 'All data' series",
    ),
    WeightSpec(
        "rut_band_days", GROUP_RUT, SHAPE,
        "Width of each rut phase", "days", 7.0, 28.0, 7.0, float(RUT_BAND_DAYS),
        "14 days - the spacing Neary et al. use between phases",
        "Neary et al. space their phases 14 days apart (Mississippi pre-rut Nov 27 "
        "/ early Dec 11 / peak Dec 25 / late Jan 8 / post Jan 22), so each phase is "
        "treated as a 14-day band centered on its named day. Narrow this if you "
        "think your herd's rut is sharper than Mississippi's; widen it if the peak "
        "date you entered is a rough guess and you want the bonus to hedge across "
        "more days.",
        "Neary et al. 2025",
    ),
    WeightSpec(
        "no_rut_yph", GROUP_RUT, WEIGHT,
        "Outside the rut", "yph", -40.0, 0.0, 1.0, NO_RUT_YPH,
        "-40 yph measured, floored at 0 here",
        "Neary et al.'s 'No Rut' series measured -40 yph against the season mean - "
        "but that mean is pulled up by the rut days themselves, so scoring every "
        "non-rut day against it makes ordinary daytime movement look like a "
        "penalty. This app ranks windows within a several-day forecast, not "
        "against a whole season, so no-measured-rut-elevation is scored as neutral "
        "rather than as a deficit. Drag toward -40 to score against the full-season "
        "mean instead. Note that it only changes rankings when your forecast spans "
        "both rut and non-rut days.",
        "Neary et al. 2025",
    ),

    # --- Weather ----------------------------------------------------------
    WeightSpec(
        "cold_max_yph", GROUP_WEATHER, WEIGHT,
        "Cold-snap bonus", "yph", 0.0, 48.0, 1.0, COLD_MAX_YPH,
        "16 yph - a bounded judgment call, the best-supported weather term",
        "No located study publishes weather effects as a movement-rate change, so "
        "every weather dial is a bounded judgment call rather than a measured "
        "figure. Temperature gets the full bound because it is the one weather "
        "variable with consistent support: Webb et al. found general linear trends "
        "for weather in only 8 of 80 models, and temperature accounted for 5 of "
        "those 8. The bound itself is 'at full strength, contribute no more to a "
        "window than a single dawn does' - 48 yph over ~2 of a 6-hour window's "
        "hours, i.e. ~16 yph. A second check: Webb et al.'s weather parameter "
        "estimates never exceeded ~32 yph, and they attribute even that partly to "
        "collar error.",
        "Webb et al. 2010; bound derived from Neary et al. 2025's dawn/dusk effect",
    ),
    WeightSpec(
        "cold_scale_f", GROUP_WEATHER, SHAPE,
        "Degrees below normal for the full cold bonus", "°F", 5.0, 40.0, 1.0,
        COLD_ANOMALY_SCALE,
        "15 °F below this location's own trailing 7-day normal for that hour",
        "Scored as a departure below the location's own recent normal for that hour "
        "of day rather than against a fixed degree threshold: a 38 °F morning "
        "means something very different in Maine than in Georgia, and deer respond "
        "to change from what they are acclimated to. The normal is built from the "
        "trailing 7 days of observations for the same hour of day, so a daytime "
        "window is compared against daytime history.",
        "Judgment call; the anomaly-vs-threshold framing follows Webb et al. 2010",
    ),
    WeightSpec(
        "precip_penalty_yph", GROUP_WEATHER, WEIGHT,
        "Rain penalty at 100% chance", "yph", 0.0, 48.0, 1.0, PRECIP_MAX_PENALTY_YPH,
        "-8 yph - half the weather bound; even the direction is a judgment call",
        "Held to half the bound because rain was significant in exactly 1 of Webb "
        "et al.'s 8 significant models. The Penn State Deer-Forest Study's storm "
        "analysis does not support a larger penalty - and does not establish its "
        "direction either: its two years point opposite ways (2016: 102 yph outside "
        "storms vs. 113 during; 2017: 111 vs. 98) and it concludes there was no "
        "significant effect. Set this to 0 if you think that null is the honest "
        "reading, or raise it if you hunt country where rain shuts movement down.",
        "Webb et al. 2010; Penn State Deer-Forest Study (30 storm events, 52,279 "
        "GPS locations, 2016-17; research blog, not peer-reviewed)",
    ),
    WeightSpec(
        "wind_penalty_yph", GROUP_WEATHER, WEIGHT,
        "Wind penalty at full strength", "yph", 0.0, 48.0, 1.0, WIND_MAX_PENALTY_YPH,
        "-8 yph - same tier as rain, same evidential standing",
        "Wind was significant in exactly 1 of Webb et al.'s 8 significant models - "
        "the same standing as rain, hence the same half-bound ceiling. Unlike rain, "
        "it doesn't engage at all below a threshold.",
        "Webb et al. 2010",
    ),
    WeightSpec(
        "wind_threshold_mph", GROUP_WEATHER, SHAPE,
        "Wind starts to bite at", "mph", 0.0, 30.0, 1.0, WIND_PENALTY_THRESHOLD_MPH,
        "15 mph - no penalty below this",
        "Below this speed the wind penalty is zero; above it the penalty ramps "
        "linearly to full strength at the 'maxes out at' setting below. Neither "
        "endpoint comes from a published movement-rate study - they are the shape "
        "of a judgment call, not a measurement.",
        "Judgment call",
    ),
    WeightSpec(
        "wind_full_mph", GROUP_WEATHER, SHAPE,
        "Wind penalty maxes out at", "mph", 20.0, 60.0, 1.0, WIND_PENALTY_FULL_MPH,
        "40 mph - full penalty at or above this",
        "The top of the wind ramp. If you set this at or below the threshold above, "
        "the ramp collapses into a step: no penalty below the threshold, full "
        "penalty at or above it.",
        "Judgment call",
    ),
    WeightSpec(
        "pressure_drop_yph", GROUP_WEATHER, WEIGHT,
        "Falling-pressure bonus (0.4+ inHg over 24h)", "yph", 0.0, 48.0, 1.0,
        PRESSURE_DROP_YPH,
        "+5 yph, near-token - small and unproven rather than disproven",
        "Falling pressure has a sliver of support: Webb et al.'s separate "
        "day-over-day analysis of weather CHANGES found 10 of 80 models "
        "significant and attributed 3 of those 10 to pressure, and Goethlich 2019 "
        "found pressure affected activity in some seasons and times of day. "
        "Against that, Webb et al.'s within-day analysis found pressure was the "
        "only one of five weather variables with no linear trend at all, and Penn "
        "State found 'no statistical or biological significance' of oncoming storms "
        "(before/during/after/control rates all within ~94-113 yph). Pinned at half "
        "that ~10 yph spread, which is itself an upper bound on an effect that "
        "study could not detect. A smaller 0.2 inHg fall scores half of whatever "
        "you set here.",
        "Webb et al. 2010; Goethlich 2019 (116 collared deer, South Carolina, "
        "2009-2018); Penn State Deer-Forest Study",
    ),
    WeightSpec(
        "pressure_band_yph", GROUP_WEATHER, WEIGHT,
        "Pressure 'sweet spot' bonus (29.8-30.3 inHg)", "yph", 0.0, 48.0, 1.0,
        PRESSURE_BAND_YPH,
        "0 yph - this one traces to a magazine rule of thumb, not a study",
        "The 29.8-30.3 inHg 'sweet spot' band is widely repeated in the hunting "
        "press. No located study tests a static pressure LEVEL (as opposed to a "
        "change), and the closest thing to a test is Webb et al.'s within-day null "
        "- pressure was the only one of their five weather variables with no linear "
        "trend. So it ships at zero: the band is still reported in each window's "
        "description for hunters who track it, it just doesn't move the ranking. "
        "This is the dial with the weakest evidence behind it of anything on this "
        "page; it is here because it is the one people ask for.",
        "No supporting study located; Webb et al. 2010 is the nearest null result",
    ),
    WeightSpec(
        "weather_damping", GROUP_WEATHER, SHAPE,
        "Dawn/dusk weather damping", "", 0.0, 1.0, 0.05, WEATHER_CREPUSCULAR_DAMPING,
        "0.5 - weather matters less at dawn and dusk, when deer move anyway",
        "Two studies independently found that weather effects concentrate in "
        "NON-peak hours. Goethlich 2019 was 'most likely to see a significant "
        "relationship between abiotic factors and activity during daytime and "
        "nighttime and least likely to see an effect in the morning and evening'; "
        "Webb et al.'s weather effects surfaced at 0100-0200 and 1300 - 'hours of "
        "limited movements' - not at dawn or dusk. Hunsaker et al. 2025 found no "
        "weather effect at all on rut-period movement. Every weather term above is "
        "multiplied by (1 - this x how much of the hour sits in a sunrise/sunset "
        "halo): at 0.5 an hour fully inside a halo carries half weather weight, at "
        "0 weather counts the same everywhere, at 1 it is switched off entirely at "
        "dawn and dusk. The size is a judgment call - the sources say 'least "
        "likely' and 'less pronounced', not 'absent'.",
        "Goethlich 2019; Webb et al. 2010; Hunsaker et al. 2025 (188 collared "
        "males, southwest Wisconsin)",
        fmt="{:.2f}",
    ),
]

WEIGHT_SPECS_BY_KEY = {spec.key: spec for spec in WEIGHT_SPECS}
WEIGHT_GROUPS = [GROUP_ACTIVITY, GROUP_RUT, GROUP_WEATHER]

# Kendall's formula: the researched value of every dial. Passing this to
# the scoring functions reproduces the app's original behaviour exactly.
DEFAULT_WEIGHTS = {spec.key: spec.default for spec in WEIGHT_SPECS}


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
# real history for windows early in the forecast, which would otherwise
# have nothing preceding them to measure a trend against.
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


def rut_phase(day, peak_date, weights=None):
    """(phase label, yph delta) for calendar date `day`, based on its
    offset from `peak_date`. See RUT_PHASE_YPH for provenance.

    Band k covers [k*band - band/2, k*band + band/2) days from peak, and
    every phase's yph is scaled by how far the peak-rut dial sits from
    its measured 142, so the ladder keeps the shape the study found at
    whatever overall size the user picked."""
    weights = weights or DEFAULT_WEIGHTS
    band = weights["rut_band_days"]
    scale = weights["rut_peak_yph"] / RUT_PEAK_YPH

    offset = (day - peak_date).days
    for label, band_index, yph in RUT_PHASE_YPH:
        center = band_index * band
        if center - band / 2 <= offset < center + band / 2:
            return label, yph * scale
    return "Outside rut", weights["no_rut_yph"]


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
def _score_half_windows(weights):
    """Per-kind scoring halo. Major/Minor keep the halos their displayed
    periods are drawn from; the dawn/dusk halo is a dial."""
    return {
        "Major": MAJOR_HALF_WINDOW,
        "Minor": MINOR_HALF_WINDOW,
        "Sunrise": timedelta(minutes=weights["crepuscular_half_min"]),
        "Sunset": timedelta(minutes=weights["crepuscular_half_min"]),
    }


def _kind_yph(weights):
    return {
        "Major": weights["major_yph"],
        "Minor": weights["minor_yph"],
        "Sunrise": weights["crepuscular_yph"],
        "Sunset": weights["crepuscular_yph"],
    }


def build_hourly_timeline(days_data, samples, tz, rut_peak_date, weights=None):
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
    feed the temperature normals and the pressure lookback.

    `weights` defaults to DEFAULT_WEIGHTS - Kendall's formula."""
    weights = weights or DEFAULT_WEIGHTS
    score_half_window = _score_half_windows(weights)
    kind_yph = _kind_yph(weights)

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
            half = score_half_window.get(p["kind"], timedelta(0))
            overlap_hours = (
                min(center + half, hour_end) - max(center - half, hour_start)
            ).total_seconds() / 3600
            if overlap_hours > 0:
                activity += kind_yph.get(p["kind"], 0.0) * POINTS_PER_YPH * overlap_hours
                if p["kind"] in ("Sunrise", "Sunset"):
                    crepuscular += overlap_hours
                events.append(p)
        # Fraction of this hour inside a dawn/dusk halo (the two halos
        # can only overlap at extreme latitudes, hence the clamp). This
        # is what the weather terms are damped by - see
        # WEATHER_CREPUSCULAR_DAMPING.
        crepuscular = min(1.0, crepuscular)

        rut_label, rut_yph = rut_phase(hour_start.date(), rut_peak_date, weights)

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
    PAST_DAYS_LOOKBACK days of history that precede the timeline, which is
    what lets windows in the first 24 hours of the forecast score a
    pressure trend at all."""
    before = samples_by_hour.get(hour_start - timedelta(hours=PRESSURE_DROP_LOOKBACK_HOURS))
    now = samples_by_hour.get(hour_start)
    if not before or not now:
        return None
    if before["pressure_inhg"] is None or now["pressure_inhg"] is None:
        return None
    return before["pressure_inhg"] - now["pressure_inhg"]


def _pressure_drop_yph(pressure_drop, weights=None):
    """Tiered falling-pressure effect in yph for a pressure_drop (inHg,
    from _pressure_drop_in()) - 0.0 if None or below the minor
    threshold. Shared by _hour_weather_yph() (scored per hour) and
    format_window() (which reports the window-start reading) so the
    tiers behind the displayed label are the ones actually scored.

    The smaller tier keeps its measured-to-full ratio (2.5 of 5.0) as the
    full tier is dialled, so there is one pressure dial rather than two."""
    weights = weights or DEFAULT_WEIGHTS
    if pressure_drop is None:
        return 0.0
    full = weights["pressure_drop_yph"]
    if pressure_drop >= PRESSURE_DROP_THRESHOLD_IN:
        return full
    if pressure_drop >= PRESSURE_DROP_MINOR_THRESHOLD_IN:
        return full * (PRESSURE_DROP_MINOR_YPH / PRESSURE_DROP_YPH)
    return 0.0


CATEGORY_ACTIVITY = "Daily Activity"
CATEGORY_RUT = "Rut Phase"
CATEGORY_WEATHER = "Weather"


def _hour_weather_yph(hour, weights=None):
    """Net weather effect for one timeline hour, in yph, or None if the
    hour has no weather data at all. Cold bonus and pressure bonuses,
    minus the rain and wind penalties, then damped by how much of the
    hour sits inside a dawn/dusk halo (see WEATHER_CREPUSCULAR_DAMPING).

    Scored per hour rather than from window averages so that the
    damping can be applied to exactly the hours it belongs to, and so
    that the weather term is the same "mean over the window's hours"
    shape as the activity and rut terms."""
    weights = weights or DEFAULT_WEIGHTS
    weather = hour["weather"]
    if not weather:
        return None

    yph = 0.0
    if hour["temp_anomaly"] is not None:
        yph += weights["cold_max_yph"] * min(
            1.0, max(0.0, hour["temp_anomaly"] / weights["cold_scale_f"])
        )

    if weather["precip_max"] is not None:
        yph -= weights["precip_penalty_yph"] * weather["precip_max"] / 100.0

    if weather["wind_avg"] is not None:
        # The user can drag the ramp's top at or below its bottom; when
        # they do, the ramp collapses to a step at the threshold rather
        # than dividing by zero or going negative.
        wind_span = weights["wind_full_mph"] - weights["wind_threshold_mph"]
        over = weather["wind_avg"] - weights["wind_threshold_mph"]
        if wind_span <= 0:
            wind_frac = 1.0 if over >= 0 else 0.0
        else:
            wind_frac = min(1.0, max(0.0, over / wind_span))
        yph -= weights["wind_penalty_yph"] * wind_frac

    pressure = weather["pressure_avg"]
    if pressure is not None and PRESSURE_BAND_LOW_IN <= pressure <= PRESSURE_BAND_HIGH_IN:
        yph += weights["pressure_band_yph"]

    yph += _pressure_drop_yph(hour["pressure_drop"], weights)

    return yph * (1.0 - weights["weather_damping"] * hour["crepuscular"])


def score_breakdown(timeline, start_idx, window_hours=WINDOW_HOURS, weights=None):
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

    The timeline's activity and rut terms were already computed under
    `weights` by build_hourly_timeline(); the same dict has to be passed
    here so the weather term is scored under the same formula.
    """
    weights = weights or DEFAULT_WEIGHTS
    hours = timeline[start_idx:start_idx + window_hours]
    if len(hours) < window_hours:
        return None
    if not any(h["events"] for h in hours) and not any(h["weather"] for h in hours):
        return None

    activity = sum(h["activity"] for h in hours) / len(hours)
    rut = sum(h["rut"] for h in hours) / len(hours)

    hourly_weather = [
        w for w in (_hour_weather_yph(h, weights) for h in hours) if w is not None
    ]
    weather_yph = sum(hourly_weather) / len(hourly_weather) if hourly_weather else 0.0
    weather = weather_yph * POINTS_PER_YPH

    return {
        CATEGORY_ACTIVITY: activity,
        CATEGORY_RUT: rut,
        CATEGORY_WEATHER: weather,
        "total": activity + rut + weather,
    }


def score_window(timeline, start_idx, window_hours=WINDOW_HOURS, weights=None):
    """Combined goodness score for timeline[start_idx:start_idx+window_hours]
    - the 'total' from score_breakdown(), or None under the same
    conditions score_breakdown() returns None."""
    breakdown = score_breakdown(timeline, start_idx, window_hours, weights)
    return breakdown["total"] if breakdown else None


def _sun_times_by_date(days_data):
    """{date: {'sunrise': dt or None, 'sunset': dt or None}} from
    fetch_solunar()'s per-day periods, for the legal-light check below."""
    sun_times = {}
    for day in days_data:
        entry = {"sunrise": None, "sunset": None}
        for p in day["periods"]:
            if p["kind"] == "Sunrise":
                entry["sunrise"] = p["start"]
            elif p["kind"] == "Sunset":
                entry["sunset"] = p["start"]
        if day["periods"]:
            sun_times[day["periods"][0]["start"].date()] = entry
    return sun_times


def _is_illegal_night_window(window_start, window_end, sun_times):
    """True if [window_start, window_end) never touches legal shooting
    light, i.e. it starts at/after its own day's sunset+LEGAL_LIGHT_MARGIN
    and finishes at/before its end day's sunrise-LEGAL_LIGHT_MARGIN. Missing
    sunrise/sunset data (e.g. polar latitudes) never excludes a window."""
    sunset = sun_times.get(window_start.date(), {}).get("sunset")
    starts_after_dusk = sunset is not None and window_start >= sunset + LEGAL_LIGHT_MARGIN

    sunrise = sun_times.get(window_end.date(), {}).get("sunrise")
    ends_before_dawn = sunrise is not None and window_end <= sunrise - LEGAL_LIGHT_MARGIN

    return starts_after_dusk and ends_before_dawn


def score_all_windows(timeline, now_local, days_data, window_hours=WINDOW_HOURS, weights=None):
    """Every valid `window_hours`-wide window in `timeline` as a list of
    (score, start_idx), best first - before the non-overlap filter that
    find_candidate_windows() applies on top.

    A window is valid if it hasn't already fully elapsed against
    `now_local`, it touches legal shooting light (see
    _is_illegal_night_window), and it scores at all.

    Split out of find_candidate_windows() so the formula comparison can
    ask where a given window places in a *complete* ranking. It has to
    be the complete one: the two formulas pick different non-overlapping
    sets, so a window in your top 3 may be absent from Kendall's picks
    entirely while still having a perfectly well-defined rank."""
    sun_times = _sun_times_by_date(days_data)
    scored = []
    for start_idx in range(len(timeline) - window_hours + 1):
        window_start = timeline[start_idx]["dt"]
        window_end = window_start + timedelta(hours=window_hours)
        if window_end <= now_local:
            continue
        if _is_illegal_night_window(window_start, window_end, sun_times):
            continue
        s = score_window(timeline, start_idx, window_hours, weights)
        if s is not None:
            scored.append((s, start_idx))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored


def find_candidate_windows(timeline, now_local, days_data, top_n=CANDIDATE_WINDOW_COUNT, window_hours=WINDOW_HOURS, weights=None):
    """Slide a `window_hours`-wide window across EVERY possible starting
    hour in `timeline` and return the `top_n` best-scoring,
    non-overlapping windows, highest score first. A window that has
    already fully elapsed (its end is at or before `now_local`) is never
    a candidate, and neither is a window that never touches legal
    shooting light (see _is_illegal_night_window) - hunting at night
    isn't legal, so those windows aren't computed at all. Non-overlap is
    enforced greedily (best score first, skip anything sharing an hour
    with an already-picked window) so two windows that are really "the
    same" opportunity shifted by an hour don't crowd out genuine
    variety."""
    scored = score_all_windows(timeline, now_local, days_data, window_hours, weights)

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


def format_window(timeline, start_idx, today_date, window_hours=WINDOW_HOURS, weights=None):
    """One line describing timeline[start_idx:start_idx+window_hours],
    for direct display in the UI."""
    weights = weights or DEFAULT_WEIGHTS
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
                # Informational at the researched weight of 0, but a
                # custom formula can put real points on this band, so
                # the label says which it is rather than asserting the
                # band never scores.
                band_yph = weights["pressure_band_yph"]
                pressure_bit += (
                    f" (traditional 'sweet spot' band, +{band_yph:.0f} yph)"
                    if band_yph else " (traditional 'sweet spot' band)"
                )
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

st.set_page_config(page_title="Huntcaster: Hunting Window Forecast", page_icon=":deer:")
st.title(":deer: Huntcaster: Hunting Window Forecast")
st.caption(
    "Ranks the best 6-hour deer hunting windows by combining rut phase, "
    "hourly weather (temp, precip, wind, pressure), and solunar major/minor "
    "periods - all computed locally, no API keys, no external solunar "
    "service. Enter a postal code to get a forecast for that location."
)

RUT_PEAK_KEY = "rut_peak_date"
PENDING_RUT_PEAK_KEY = "_pending_rut_peak"
FORECAST_KEY = "forecast_request"

MODE_KENDALL = "Kendall's formula"
MODE_CUSTOM = "Build your own formula"
MODE_KEY = "formula_mode"
WEIGHT_STATE_PREFIX = "w_"

# --- Sharing a formula through the URL ------------------------------------
#
# Opt-in, from a control at the very bottom of the page: the address bar
# stays clean unless the user asks for a link, because a query string
# nobody wanted is clutter on every single visit.
#
# Once asked for, it stays in sync - every rerun rewrites it - so the
# link in the address bar can never describe a formula other than the
# one on screen. Only dials that differ from Kendall's are written, so a
# one-dial tweak makes a short link.
URL_FORMULA_VERSION = "1"
URL_VERSION_PARAM = "f"
URL_SEEDED_KEY = "_url_formula_seeded"
URL_SHARE_KEY = "_url_formula_share"


def _weights_from_query_params():
    """Dial positions carried in the URL, or None if there aren't any.

    Deliberately forgiving, unlike a strict code: unknown parameters are
    ignored, an unparseable value is skipped, and an out-of-range one is
    clamped and snapped onto its dial's step. A hand-edited link
    degrades into the nearest sane formula rather than failing. The
    version parameter is the one hard gate - if the dial set ever
    changes, old links stop being read instead of decoding to something
    different."""
    params = st.query_params
    if params.get(URL_VERSION_PARAM) != URL_FORMULA_VERSION:
        return None

    weights = dict(DEFAULT_WEIGHTS)
    found = False
    for spec in WEIGHT_SPECS:
        raw = params.get(spec.key)
        if raw is None:
            continue
        try:
            weights[spec.key] = spec.snap(float(raw))
            found = True
        except (TypeError, ValueError):
            continue
    return weights if found else None


def _formula_query_params(weights):
    """The query parameters that describe `weights` - the version marker
    plus only the dials that have actually been moved."""
    params = {URL_VERSION_PARAM: URL_FORMULA_VERSION}
    for spec in WEIGHT_SPECS:
        if weights[spec.key] != spec.default:
            params[spec.key] = f"{weights[spec.key]:g}"
    return params


def _share_url(params):
    """The full link to show the user: this page's own URL with the
    formula's query string on it. Built by hand rather than read back
    from st.context.url because that reflects the URL the run started
    with, not the parameters just written. Returns None if the host URL
    isn't available, in which case the caller falls back to telling them
    to copy the address bar."""
    try:
        base = st.context.url
    except Exception:
        return None
    if not base:
        return None
    return f"{base.split('?')[0]}?{urlencode(params)}"


def _weight_state_key(spec):
    return WEIGHT_STATE_PREFIX + spec.key


def _custom_weights():
    """The weights dict the custom sliders currently hold. Read straight
    out of session state rather than from the slider return values so it
    can be called before the sliders are drawn."""
    return {
        spec.key: st.session_state.get(_weight_state_key(spec), spec.default)
        for spec in WEIGHT_SPECS
    }


def _set_custom_weights(weights):
    """Push `weights` into the slider widgets. Only safe to call BEFORE
    the sliders are instantiated on this run - Streamlit refuses writes
    to a widget's key after the widget has been drawn - which is why the
    reset control is laid out above the sliders."""
    for spec in WEIGHT_SPECS:
        if spec.key in weights:
            st.session_state[_weight_state_key(spec)] = weights[spec.key]


def _render_weight_editor():
    """The 'build your own' panel: every dial, with the research behind
    it. Returns the weights dict the rest of the page should score with.

    Deliberately NOT inside an expander - each dial carries its own
    'Why this number' expander, and Streamlit won't nest those."""
    for spec in WEIGHT_SPECS:
        st.session_state.setdefault(_weight_state_key(spec), spec.default)

    with st.container(border=True):
        # yph is the unit everything in this model is expressed in, and
        # it is meaningless to anyone who hasn't read the research - so
        # it gets explained once, here, before the first slider.
        st.markdown(
            "**The effect dials are in yph - yards per hour.** That is the unit "
            "the GPS-collar studies measured movement in: the *extra* yards per "
            "hour bucks covered when a condition was present, against a 269 yph "
            "season average. Dawn/dusk sitting at 48 means bucks moved about 48 "
            "yards an hour more near sunrise and sunset than they did on an "
            "average day. Raise a dial to weight that condition more heavily, or "
            "drop it to zero to switch it off. The remaining dials set *where* an "
            "effect kicks in, in their own units (°F, mph, minutes, days)."
        )
        st.caption(
            "Every dial starts where the research put it, and shows what that "
            "value is based on. Move one and the ranking below re-computes "
            "immediately - no need to look up your location again."
        )

        # Reset comes first, because it writes to the slider keys and
        # that has to happen before the sliders below are drawn.
        if st.button("Reset to Kendall's"):
            _set_custom_weights(DEFAULT_WEIGHTS)
            st.success("Every dial is back at its researched value.")

        tabs = st.tabs(WEIGHT_GROUPS)
        for tab, group in zip(tabs, WEIGHT_GROUPS):
            with tab:
                for spec in (s for s in WEIGHT_SPECS if s.group == group):
                    decimals = 2 if spec.step < 0.5 else 0
                    slider_format = f"%.{decimals}f"
                    if spec.unit:
                        slider_format += f" {spec.unit}"
                    st.slider(
                        spec.label,
                        min_value=spec.lo,
                        max_value=spec.hi,
                        step=spec.step,
                        key=_weight_state_key(spec),
                        format=slider_format,
                    )
                    kind_tag = (
                        "How big the effect is"
                        if spec.kind == WEIGHT
                        else "Where the effect kicks in"
                    )
                    moved = (
                        ""
                        if st.session_state[_weight_state_key(spec)] == spec.default
                        else f"  — you've moved this from {spec.show(spec.default)}"
                    )
                    st.caption(f"{kind_tag}. {spec.caption}.{moved}")
                    with st.expander("Why this number"):
                        st.markdown(spec.snippet)
                        st.caption(f"**Source:** {spec.cite}")
                    st.write("")

    return _custom_weights()


def _render_share_formula(weights):
    """The opt-in 'Save this formula' block: nothing touches the query
    string until the button is pressed - see the URL_* block above for
    why the address bar stays clean by default.

    A function rather than inline UI because it has two homes. It
    normally sits directly under the formula comparison, but that only
    exists when there's a forecast on screen, and the link still has to
    be reachable (and, once enabled, still has to keep itself in sync)
    for someone who arrives on a shared link and adjusts dials before
    entering a postcode. The caller that renders it first wins; see
    `share_rendered`."""
    st.subheader(":link: Save this formula")

    if st.session_state.get(URL_SHARE_KEY):
        # Rewritten on every rerun while sharing is on, so the link can
        # never fall out of step with the dials above it.
        params = _formula_query_params(weights)
        st.query_params.from_dict(params)

        moved = len(params) - 1
        st.caption(
            f"Your address bar now carries this formula "
            f"({moved} dial{'s' if moved != 1 else ''} moved from Kendall's). "
            "Bookmark it, or send it to someone - opening it puts every dial "
            "back where it is now. It updates itself as you keep adjusting."
        )
        url = _share_url(params)
        if url:
            st.code(url, language=None)
        else:
            st.caption("Copy it straight from the address bar.")

        if st.button("Take it back out of the URL"):
            st.query_params.clear()
            st.session_state[URL_SHARE_KEY] = False
            st.rerun()
    else:
        if st.button("Put this formula in the URL", type="primary"):
            st.session_state[URL_SHARE_KEY] = True
            st.rerun()
        st.caption(
            "Your dials last until you reload the page. Press this and the "
            "formula is written into the page's own URL, so you can bookmark "
            "it or share it - nothing is stored anywhere, the link *is* the "
            "formula. Until then the address bar is left alone."
        )


def _rut_season_year():
    """The year of whichever rut the user is most likely to mean: the
    upcoming/current fall if it's July or later, otherwise the rut that
    just passed."""
    today = date.today()
    return today.year if today.month >= 7 else today.year - 1


def _season_date(month_day):
    month, day = month_day
    return date(_rut_season_year(), month, day)


def _fmt_md(d):
    """'Nov 30' - built by hand because %-d/%#d aren't portable."""
    return f"{d:%b} {d.day}"


def _default_rut_peak():
    """RUT_PEAK_DEFAULT_MONTH_DAY in the season year the user likely means."""
    return _season_date(RUT_PEAK_DEFAULT_MONTH_DAY)


_by_name = sorted(SUPPORTED_COUNTRIES, key=lambda pair: pair[1])
_name_to_code = {name: code for code, name in _by_name}
_country_names = list(_name_to_code.keys())

# A formula arriving in the URL is an INITIAL condition, applied once
# per session and never again. Re-reading it every run would fight the
# user: the moment they nudged a dial, the next rerun would drag it back
# to whatever the link said. Seeded here, above the mode radio and the
# sliders, because all of those read their values from these keys.
if URL_SEEDED_KEY not in st.session_state:
    st.session_state[URL_SEEDED_KEY] = True
    _linked_weights = _weights_from_query_params()
    if _linked_weights is not None:
        for _spec in WEIGHT_SPECS:
            st.session_state[_weight_state_key(_spec)] = _linked_weights[_spec.key]
        # A link only ever carries a custom formula, so open on it, and
        # keep the address bar in sync from the start - the user already
        # opted into a URL by following one.
        st.session_state[MODE_KEY] = MODE_CUSTOM
        st.session_state[URL_SHARE_KEY] = True

# --- Which formula ranks the windows -------------------------------------
#
# Rendered before anything else on the page, and switchable at any point
# without losing the forecast: the location, dates and weather are cached
# and re-scored under whichever formula is selected.
#
# The question is drawn as its own subheader rather than left as the
# radio's built-in label, which renders at caption size and is easy to
# scroll straight past. The label is kept and collapsed rather than
# dropped so screen readers still announce what the choice is.
st.subheader("Which formula should rank your hunting windows?")
formula_mode = st.radio(
    "Which formula should rank your hunting windows?",
    [MODE_KENDALL, MODE_CUSTOM],
    key=MODE_KEY,
    horizontal=True,
    label_visibility="collapsed",
    captions=[
        "The app as calibrated - every weight traced to a GPS-collar study.",
        "Set the weights yourself, with the research behind each one alongside it.",
    ],
)

if formula_mode == MODE_CUSTOM:
    active_weights = _render_weight_editor()
else:
    active_weights = DEFAULT_WEIGHTS

using_custom = active_weights != DEFAULT_WEIGHTS

# Set once the 'Save this formula' block has been drawn, so the
# fallback at the foot of the page doesn't draw it a second time.
share_rendered = False

# --- Peak breeding (rut) date ---------------------------------------------
#
# Deliberately outside the location form below: the state lookup needs a
# live button and a selectbox whose result updates as you pick, and a
# Streamlit form allows neither - nothing inside a form reacts until it
# is submitted. Being outside also means changing the date re-scores the
# forecast straight away, the same way the weight dials do.
if RUT_PEAK_KEY not in st.session_state:
    st.session_state[RUT_PEAK_KEY] = _default_rut_peak()

# The lookup's "Use this date" button sits BELOW the date widget now, so
# it can no longer write RUT_PEAK_KEY directly - Streamlit refuses writes
# to a widget's key once that widget has been drawn. It stashes the date
# here and reruns instead, and this drains it before the widget is built.
_pending_peak = st.session_state.pop(PENDING_RUT_PEAK_KEY, None)
if _pending_peak is not None:
    st.session_state[RUT_PEAK_KEY] = _pending_peak

with st.container(border=True):
    rut_peak = st.date_input(
        "Peak breeding (rut) date for your area",
        key=RUT_PEAK_KEY,
        help=(
            "Rut phase is the strongest driver of fall deer movement in the "
            "research this app's scoring is calibrated against, but peak rut "
            "date is regionally specific and not a clean function of "
            "latitude. The default (Nov 15) follows the Pennsylvania Game "
            "Commission's fetal-aging data (peak breeding mid-November, half "
            "of does bred by Nov 13). If your state wildlife agency publishes "
            "conception dates for your area, use those - or open the state "
            "lookup just below."
        ),
    )

    with st.expander("I don't know my peak rut date - help me find it by state"):
        st.caption(
            f"Few states supported so far ({', '.join(RUT_DATE_HELP_STATES.keys())}) - "
            "more will be added as county- or region-level sources are found."
        )
        rut_help_state = st.selectbox(
            "State",
            list(RUT_DATE_HELP_STATES.keys()),
            help="More states will be added as county- or region-level sources are found.",
        )
        rut_help = RUT_DATE_HELP_STATES[rut_help_state]
        counties = rut_help.get("counties")
        statewide = rut_help.get("statewide")

        if statewide:
            # No sub-state breakdown published, so there is nothing to pick.
            looked_up = _season_date(statewide["peak"])
            st.success(
                f"**Peak rut for {rut_help_state} (statewide): "
                f"{_fmt_md(looked_up)}**"
            )
            st.caption(statewide["detail"])
            if st.button(f"Use {_fmt_md(looked_up)}, {looked_up:%Y} as my peak rut date"):
                st.session_state[PENDING_RUT_PEAK_KEY] = looked_up
                st.rerun()
        elif counties:
            # counties is this state's own table; a county is never looked
            # up outside the state the user selected.
            # Keyed per state so the widget's remembered selection can't
            # survive a state switch and point at another state's area.
            area_label = rut_help.get("area_label", "County")
            area = st.selectbox(
                area_label, sorted(counties), key=f"area_{rut_help_state}"
            )
            entry = counties[area]
            looked_up = _season_date(entry["peak"])

            # "Macon County", but just "Northern New York" for a region.
            shown = f"{area} County" if area_label == "County" else area
            if entry["estimated"]:
                st.success(f"**Peak rut for {shown}: {_fmt_md(looked_up)}**")
            else:
                st.info(f"**{shown}: {_fmt_md(looked_up)}**")
            st.caption(entry["detail"])
            if entry["caution"]:
                st.warning(entry["caution"])

            if st.button(f"Use {_fmt_md(looked_up)}, {looked_up:%Y} as my peak rut date"):
                st.session_state[PENDING_RUT_PEAK_KEY] = looked_up
                st.rerun()

        st.caption(rut_help["caption"])
        label = rut_help.get("source_label", f"the full {rut_help_state} map (PDF)")
        st.markdown(f"[Open {label}]({rut_help['url']})")

with st.form("location_form"):
    col1, col2 = st.columns([2, 1])
    with col1:
        country_name = st.selectbox(
            "Country", _country_names, index=_country_names.index("United States"),
        )
    with col2:
        postcode = st.text_input("Postal / zip code")
    days = st.slider("Days", min_value=1, max_value=30, value=7)
    submitted = st.form_submit_button("Get hunting forecast", type="primary")

if submitted:
    if not postcode.strip():
        st.error("Enter a postal/zip code.")
        st.session_state.pop(FORECAST_KEY, None)
    else:
        # Remember what was asked for. Moving a weight slider reruns the
        # whole script, which clears `submitted` - without this the
        # forecast would blank out the moment anyone touched a dial. The
        # postcode/timezone lookups and the weather fetch are all
        # @st.cache_data, so re-scoring under new weights costs no network
        # calls. Snapshotting the submitted values (rather than reading
        # the live widgets) also means editing the postcode box without
        # pressing the button doesn't silently move the forecast.
        #
        # The peak rut date is deliberately NOT snapshotted: it lives
        # outside the form, and like the weight dials it only affects
        # scoring, so it re-ranks the existing forecast on the spot.
        st.session_state[FORECAST_KEY] = {
            "country_code": _name_to_code[country_name],
            "country_name": country_name,
            "postcode": postcode.strip(),
            "days": days,
        }

request = st.session_state.get(FORECAST_KEY)
if request:
    country_name = request["country_name"]
    country_code = request["country_code"]
    postcode = request["postcode"]
    days = request["days"]

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
            timeline = build_hourly_timeline(
                days_data, weather_samples, tz, rut_peak, active_weights
            )
            now_local = datetime.now(tz).replace(tzinfo=None)
            candidates = find_candidate_windows(
                timeline, now_local, days_data, weights=active_weights
            )
            if candidates:
                st.subheader(":dart: Best Hunting Windows")
                st.caption(
                    "Ranks every possible 6-hour window by rut phase, dawn/dusk "
                    "timing, solunar activity, temperature relative to local "
                    "normal, wind, and barometric pressure - weighted by "
                    + (
                        "the weights **you** set (see the scoring notes at the "
                        "bottom of the page, which are printed from your own "
                        "dial positions)."
                        if using_custom
                        else "measured effect sizes from GPS-collar research "
                        "(see the scoring notes at the bottom of the page)."
                    )
                )
                today_date = now_local.date()
                top_candidates = candidates[:3]
                for i, (_score, start_idx) in enumerate(top_candidates, start=1):
                    st.write(
                        f"**#{i}** - "
                        + format_window(
                            timeline, start_idx, today_date, weights=active_weights
                        )
                    )

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

                # The axis label is day + time only (no rank number) so
                # it reads as a plain left-to-right timeline; rank is
                # still available in the tooltip and in the top-3 list
                # above the chart, and doesn't need to march 1,2,3... in
                # this order since the bars are sorted by time, not rank.
                window_labels = []
                breakdown_rows = []
                net_rows = []
                for i, (score, start_idx) in ranked_by_time:
                    label = (
                        f"{_label_for_date(timeline[start_idx]['dt'].date(), today_date)}\n"
                        f"{_format_time(timeline[start_idx]['dt'])}"
                    )
                    window_labels.append(label)
                    breakdown = score_breakdown(
                        timeline, start_idx, weights=active_weights
                    )
                    for rank, category in enumerate(category_order):
                        breakdown_rows.append({
                            "Window": label,
                            "Category": category,
                            "CategoryRank": rank,
                            "Score": round(breakdown[category], 2),
                            "Net": round(breakdown["total"], 2),
                            "Rank": i,
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
                    # labelOverlap off forces every column to keep its
                    # label - the default hides some when 15 bars are
                    # this tightly packed, which is what made the order
                    # look scrambled. Rotated -90 so each label needs
                    # only its own bar's width instead of colliding with
                    # its neighbors.
                    axis=alt.Axis(
                        labelAngle=-90, labelOverlap=False,
                        labelAlign="right", labelBaseline="middle",
                    ),
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
                        alt.Tooltip("Rank:Q", title="Rank by score"),
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

                # --- Your formula vs. Kendall's ----------------------------
                #
                # Only worth drawing once the dials have actually moved.
                # Compared by RANK, not by raw score: scaling every dial
                # up multiplies every score without reordering anything,
                # so the two formulas' point totals aren't on a shared
                # scale and putting them side by side would invite a
                # comparison that means nothing. Where a window places is
                # the thing that survives rescaling.
                if using_custom:
                    st.subheader(":balance_scale: Your formula vs. Kendall's")

                    kendall_timeline = build_hourly_timeline(
                        days_data, weather_samples, tz, rut_peak, DEFAULT_WEIGHTS
                    )
                    yours_all = score_all_windows(
                        timeline, now_local, days_data, weights=active_weights
                    )
                    kendall_all = score_all_windows(
                        kendall_timeline, now_local, days_data, weights=DEFAULT_WEIGHTS
                    )
                    yours_rank = {idx: r for r, (_s, idx) in enumerate(yours_all, start=1)}
                    kendall_rank = {idx: r for r, (_s, idx) in enumerate(kendall_all, start=1)}
                    total_windows = len(yours_all)

                    kendall_candidates = find_candidate_windows(
                        kendall_timeline, now_local, days_data, weights=DEFAULT_WEIGHTS
                    )

                    def _window_label(idx):
                        start_dt = timeline[idx]["dt"]
                        end_dt = start_dt + timedelta(hours=WINDOW_HOURS)
                        return (
                            f"{_label_for_date(start_dt.date(), today_date)} "
                            f"{_format_time(start_dt)} - {_format_time(end_dt)}"
                        )

                    yours_top = [idx for _s, idx in candidates[:3]]
                    kendall_top = [idx for _s, idx in kendall_candidates[:3]]

                    col_you, col_kendall = st.columns(2)
                    with col_you:
                        st.markdown("**Your top 3**")
                        for i, idx in enumerate(yours_top, start=1):
                            st.write(f"**#{i}** - {_window_label(idx)}")
                            st.caption(
                                f"Kendall's formula ranks this "
                                f"#{kendall_rank.get(idx, '-')} of {total_windows}"
                            )
                    with col_kendall:
                        st.markdown("**Kendall's top 3**")
                        for i, idx in enumerate(kendall_top, start=1):
                            st.write(f"**#{i}** - {_window_label(idx)}")
                            st.caption(
                                f"Your formula ranks this "
                                f"#{yours_rank.get(idx, '-')} of {total_windows}"
                            )

                    shared = len(set(yours_top) & set(kendall_top))
                    if shared == 3:
                        verdict = (
                            "**Your formula picks the same top 3 Kendall's does.** "
                            "Moving those dials didn't change what the app "
                            "recommends - which is itself worth knowing."
                        )
                    elif shared:
                        verdict = (
                            f"**{shared} of your top 3 also make Kendall's top 3.** "
                            "The rest is where your weights actually bite."
                        )
                    else:
                        verdict = (
                            "**Your top 3 and Kendall's have nothing in common.** "
                            "Your weights have moved the recommendation "
                            "completely - worth checking the dials you changed "
                            "against the research beside them."
                        )
                    st.markdown(verdict)
                    st.caption(
                        f"Ranks are out of all {total_windows} legal, un-elapsed "
                        f"{WINDOW_HOURS}-hour windows in this forecast, scored under "
                        "each formula. Rank is the comparison rather than points "
                        "because scaling every dial up multiplies all your scores "
                        "without reordering anything - the point totals aren't on a "
                        "shared scale, but the ordering is."
                    )

                    # Right below the comparison, while the user is still
                    # looking at what their formula did - not at the foot
                    # of the page behind the per-day sun/moon cards.
                    _render_share_formula(active_weights)
                    share_rendered = True

                st.divider()
        else:
            st.warning(
                "Couldn't fetch the weather forecast, so hunting-window "
                "ranking is unavailable right now - the solunar and sunrise/sunset "
                "times are still shown below."
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
    + (
        f"ranking above weights majors at the {active_weights['major_yph']:.0f} yph "
        f"and minors at the {active_weights['minor_yph']:.0f} yph **you** set, not "
        "at the +3 / -0.1 yph that was measured."
        if using_custom
        else "ranking above weights them at their measured size, not their "
        "traditional one."
    )
    + " See the scoring notes below."
)

# Everything in the notes below is printed from the weights actually in
# force, so the page never describes a formula other than the one that
# produced the ranking above. The "Measured" column stays the published
# figure in both modes - that is a fact about the study, not a setting.
w = active_weights
_rut_scale = w["rut_peak_yph"] / RUT_PEAK_YPH
_halo_hours = w["crepuscular_half_min"] / 60.0
_dawn_share = min(WINDOW_HOURS, 2 * _halo_hours) / WINDOW_HOURS

with st.expander(":straight_ruler: How the hunting-window score is calculated"):
    if using_custom:
        st.warning(
            "**You're on your own formula, not Kendall's.** Every number on this "
            "page reflects the dials you set, so it stays an accurate description "
            "of the ranking above - but the citations describe what the research "
            "measured, which is no longer what you're scoring. The differences are "
            "called out per dial in the editor at the top of the page, and "
            "**Reset to Kendall's** puts everything back."
        )
    st.markdown(
        f"Each candidate is a rolling **{WINDOW_HOURS}-hour** window. Every effect is "
        "kept in the unit the research measured it in - **yards per hour (yph) of "
        "excess daytime buck movement** - and a window's score is the mean excess yph "
        "across its hours:"
    )
    st.latex(
        r"\text{score} = \underbrace{\text{activity}}_{\text{dawn/dusk} + \text{solunar}}"
        r" + \underbrace{\text{rut}}_{\text{per day}}"
        r" + \underbrace{(\text{cold} + \text{pressure} - \text{penalty}) \cdot d}_{\text{weather}}"
        r", \qquad k = \frac{3.0}{142} \approx " + f"{POINTS_PER_YPH:.4f}"
    )
    st.markdown(
        "$k$ is anchored so peak rut - the largest measured effect, +142 yph - is worth "
        f"3.0 points; $d$ damps weather at dawn and dusk (term 6). "
        + (
            "**Your dial positions, not the measured ones, are what gets scored.**"
            if using_custom
            else "**No activity weight here is hand-tuned.**"
        )
    )

    st.markdown(
        "### Your weights" if using_custom else "### The measured weights"
    )
    st.markdown(
        "Measured values are all from **Neary et al. 2025** (48 GPS-collared bucks, "
        "central Mississippi, Sept-Feb, 2 years), against a **269 yph season mean**. "
        "**Scored** is what this app actually put on each effect:\n\n"
        "| Effect | Measured | Scored (yph) | Points |\n"
        "|---|---|---|---|\n"
        f"| Peak rut | +142 yph | {142 * _rut_scale:.0f} | "
        f"{142 * _rut_scale * POINTS_PER_YPH:.2f} |\n"
        f"| Early rut | +104 yph | {104 * _rut_scale:.0f} | "
        f"{104 * _rut_scale * POINTS_PER_YPH:.2f} |\n"
        f"| Late rut | +78 yph | {78 * _rut_scale:.0f} | "
        f"{78 * _rut_scale * POINTS_PER_YPH:.2f} |\n"
        f"| **Within {_halo_hours:g} hr of sunrise/sunset** | **+48 yph** (317 vs. 269) "
        f"| **{w['crepuscular_yph']:.0f}** | "
        f"**{w['crepuscular_yph'] * POINTS_PER_YPH:.2f}** |\n"
        f"| Post-rut | +9 yph | {9 * _rut_scale:.0f} | "
        f"{9 * _rut_scale * POINTS_PER_YPH:.2f} |\n"
        f"| Pre-rut | +4 yph | {4 * _rut_scale:.0f} | "
        f"{4 * _rut_scale * POINTS_PER_YPH:.2f} |\n"
        f"| **Solunar Major** (moon overhead/underfoot) | **+3 yph** | "
        f"**{w['major_yph']:.0f}** | **{w['major_yph'] * POINTS_PER_YPH:.2f}** |\n"
        f"| **Solunar Minor** (moonrise/moonset) | **-0.1 yph** | "
        f"**{w['minor_yph']:.0f}** | **{w['minor_yph'] * POINTS_PER_YPH:.2f}** |\n"
        f"| Outside the rut | -40 yph | {w['no_rut_yph']:.0f} | "
        f"{w['no_rut_yph'] * POINTS_PER_YPH:.2f} |\n\n"
        "Points are the effect at its own rate; what it adds to a window also depends "
        f"on how many hours it covers - a dawn band covers ~{2 * _halo_hours:g} of "
        f"{WINDOW_HOURS}, so it contributes "
        f"~{w['crepuscular_yph'] * _dawn_share * POINTS_PER_YPH:.2f}.\n\n"
        + (
            "Rut phase and dawn/dusk are the two effects the research puts above "
            "everything else; whether your dials still reflect that is worth a look."
            if using_custom
            else "**Rut phase and dawn/dusk dominate; solunar major and minor are "
            "indistinguishable from zero.** They're computed and shown because "
            "hunters ask for them, not because they move the ranking."
        )
    )

    st.markdown("### The six terms")
    st.markdown(
        "| # | Term | How it's scored | Basis |\n"
        "|---|---|---|---|\n"
        "| 1 | **Daily activity** | Dawn/dusk (+/-60 min), solunar Major (+/-60 min) "
        "and Minor (+/-30 min), overlap-weighted per hour at the yph above | "
        "**Measured** - Neary et al. 2025 |\n"
        f"| 2 | **Rut phase** | {w['rut_band_days']:.0f}-day bands around the peak date "
        "you enter, applied to every hour of the window | **Measured** - Neary et al. "
        "2025 (timing is yours to supply) |\n"
        f"| 3 | **Cold** | Degrees F below this location's own trailing "
        f"{PAST_DAYS_LOOKBACK}-day normal *for that hour of day*, ramping to "
        f"{w['cold_max_yph']:.0f} yph at {w['cold_scale_f']:.0f} below | "
        "**Judgment call** - temperature drove 5 of the 8 significant weather models "
        "in Webb et al. 2010, more than any other variable |\n"
        f"| 4 | **Rain/wind** | Subtracted: rain to -{w['precip_penalty_yph']:.0f} yph "
        f"at 100% chance; wind to -{w['wind_penalty_yph']:.0f} yph above "
        f"{w['wind_threshold_mph']:.0f} mph, maxing at "
        f"{w['wind_full_mph']:.0f} | **Judgment call** - 1 of those 8 models each "
        "(Webb et al. 2010); the Penn State Deer-Forest Study's two storm years "
        "disagree on direction |\n"
        f"| 5 | **Pressure** | {w['pressure_drop_yph']:.0f} yph "
        f"({w['pressure_drop_yph'] * PRESSURE_DROP_MINOR_YPH / PRESSURE_DROP_YPH:.1f} "
        "for a smaller fall) on a falling 24-hour trend; the "
        f"{PRESSURE_BAND_LOW_IN}-{PRESSURE_BAND_HIGH_IN} inHg \"sweet spot\" band is "
        "reported and scores "
        + (
            f"**{w['pressure_band_yph']:.0f} yph**"
            if w["pressure_band_yph"]
            else "**zero**"
        )
        + " | **Near-token** - Penn State found no "
        "significant storm effect; pressure was the one variable of five in Webb et "
        "al. 2010 with no within-day trend |\n"
        f"| 6 | **Dawn/dusk damping** | Terms 3-5 scaled by "
        f"$1 - {w['weather_damping']:.2f} \\times$ (fraction of the hour inside a "
        "sunrise/sunset halo) | **Two studies** - Goethlich 2019 and Webb et al. 2010 "
        "found weather effects concentrate in non-peak hours; Hunsaker et al. 2025 "
        "found none at all during the rut |\n\n"
        "Terms 3-5 are bounded judgment calls: no located study reports weather as a "
        "movement *rate*, so each is capped at one dawn's worth of contribution."
    )

    st.markdown(
        "**Peak rut is regional, not a function of latitude**, which is why this app "
        "asks for it: Pennsylvania peaks mid-November (PA Game Commission), southwest "
        "Wisconsin Oct 23-Nov 12 *despite being further north* (Hunsaker et al. 2025), "
        "central Mississippi Dec 25 (Neary et al. 2025). Use your state agency's "
        "conception data if they publish it. These are **buck** movement rates."
    )

    st.markdown(
        f"**Reading the chart.** The top {CANDIDATE_WINDOW_COUNT} non-overlapping "
        "windows are shown (best score wins any overlap). Bonuses stack above the zero "
        "line and penalties hang below, so a bar's height is not its score - the "
        f"diamond marks the net total the ranking uses. {CATEGORY_RUT} and "
        f"{CATEGORY_ACTIVITY} never go negative; {CATEGORY_WEATHER} can."
    )

    st.markdown("### Sources")
    st.caption(
        "Every citation below was verified against the published source. The full "
        "write-up - how each constant was derived, where sources disagree with this "
        "model, the county rut-date lookups and the model's known gaps - is in "
        "[litreview.md](https://github.com/kendallhamm/huntcast/blob/master/litreview.md)."
    )
    st.markdown(
        "- **Neary, N., B. Strickland, L. Resop, S. Demarais, and W. McKinley. 2025.** "
        "*Lunar Legends: Does the Moon Influence Buck Activity?* Mississippi State "
        "University Extension Publication 4068. - supplies **every activity weight in "
        "this model**: the 269 yph season baseline, the +48 yph dawn/dusk effect, the "
        "+3 / -0.1 yph solunar major/minor effects, and the full rut-phase yph ladder. "
        "48 GPS-collared bucks, central Mississippi, 15-min fixes, Sept-Feb, 2 years.\n"
        "- **Webb, S.L., K.L. Gee, B.K. Strickland, S. Demarais, and R.W. DeYoung. 2010.** "
        "*Measuring Fine-Scale White-Tailed Deer Movements and Environmental Influences "
        "Using GPS Collars.* International Journal of Ecology 2010:1-12. - 32 deer, 7 "
        "years of 15-minute fixes in Oklahoma. Source for the weather tiering (4 of 5 "
        "variables mattered in only 8 of 80 models; temperature in 5 of those 8, leaving "
        "**pressure the one variable with no linear trend**), the null moon-phase "
        "result, and the finding that crepuscular movement is the dominant driver.\n"
        "- **Penn State Deer-Forest Study.** *Spidey Sense* (deer.psu.edu) - 30 storm "
        "events, 52,279 GPS locations, 4-8 collared does, 2016-17; source for the null "
        "storm/pressure result (\"no statistical or biological significance\") and the "
        "~94-113 yph spread the falling-pressure term is pinned under. Its two years "
        "disagree on whether deer moved more or less during storms, so it doesn't "
        "establish the rain penalty's direction. (Research-project blog, not "
        "peer-reviewed.)\n"
        "- **Pennsylvania Game Commission.** *When is the rut?* - fetal measurements from "
        "6,000+ road-killed does, 2000-2007; peak breeding by adult does in "
        "**mid-November**, which is where the Nov 15 default comes from.\n"
        "- **Cheatum, E.L. and G.H. Morton. 1946.** *Breeding Season of White-Tailed "
        "Deer in New York.* Journal of Wildlife Management 10(3):249-263, at p. 258. - "
        "source for the **New York** dates in the rut lookup (northern NY Nov 13, "
        "southern NY Nov 20). New York is the one state shown by *region* rather than "
        "county, because a north/south contrast is the scale this source works at. At "
        "1946 it is by some margin the oldest source used here; estrus timing is "
        "photoperiod-driven so the dates should be stable, but they have not been "
        "re-derived from modern New York data.\n"
        "- **Sullivan, J.D., S.S. Ditchkoff, B.A. Collier, C.R. Ruth, and J.B. Raglin. "
        "2016.** *Movement with the moon: white-tailed deer activity and solunar events.* "
        "Journal of the SEAFWA 3:225-232. - 38 bucks, Brosnan Forest, South Carolina. "
        "Near a new/full moon, **minor**-period activity rose (0.384->0.564 at moonrise) "
        "while **major**-period activity *fell* (0.540->0.413 overhead). Concluded "
        "solunar charts \"may be misleading\".\n"
        "- **Swartout, T.J., and S.S. Ditchkoff. 2025.** *Are Solunar Charts as "
        "Predictable as They Claim?* Southeastern Naturalist 24(2):137-150. - 22 bucks, "
        "high-fenced Alabama property. The strongest pro-solunar result found: top-rated "
        "days gave **3.02x / 2.83x** activity odds during moon underfoot/overhead, but "
        "only 0.30x / 0.37x at moonrise/moonset. Reported as odds, not movement rate, so "
        "it could not be converted into this model's units - see litreview.md.\n"
        "- **Hunsaker, M.A., M.L.J. Gilbertson, D.J. Storm, and W.C. Turner. 2025.** *The "
        "Breeding Season and Movement Ecology of Male White-Tailed Deer in Southwest "
        "Wisconsin.* Ecology and Evolution 15(7):e71589. - 188 collared males; source for "
        "the Oct 23-Nov 12 Wisconsin peak-breeding window used to show that rut timing "
        "isn't a simple latitude function, and for the null result that weather, hunting "
        "season and opening firearm weekend had no significant effect on rut-period "
        "movement.\n"
        "- **Little, A.R., S.L. Webb, S. Demarais, K.L. Gee, S.K. Riffell, and J.A. "
        "Gaskamp. 2016.** *Hunting intensity alters movement behaviour of white-tailed "
        "deer.* Basic and Applied Ecology 17:360-369. - 37 adult bucks, Oklahoma; "
        "hunting pressure as a real movement driver; a known gap this model does not "
        "attempt (see litreview.md).\n"
        "- **Goethlich, J. 2019.** *Effects of Abiotic Factors on White-tailed Deer "
        "Activity in South Carolina.* M.S. thesis, Auburn University. - 116 collared "
        "deer, 2009-2018: responses to abiotic factors were *\"typically less pronounced "
        "than circadian fluctuations in activity, and occurred most often during non-peak "
        "times of activity.\"* The basis, with Webb et al. 2010, for damping the weather "
        "terms at dawn and dusk (term 6).\n\n"
        "Also used for the state rut-date lookup and documented in litreview.md: "
        "**NC Wildlife Resources Commission 2025** (100-county peak conception), "
        "**Georgia DNR WRD** (159-county peak movement), **Texas Parks and Wildlife** "
        "(16 ecoregions), **Green et al. 2017** (Illinois statewide), and the "
        "**Louisiana DWF**, **Mississippi MDWFP** and **South Carolina DNR** contour "
        "maps."
    )


# The share block normally lives under the formula comparison, up with
# the forecast. This is the fallback for when there's no forecast on
# screen to put it under - someone who arrived on a shared link, or who
# built a formula before looking a postcode up. Once sharing is on it
# also has to keep rewriting the URL, so it can't simply be skipped.
if using_custom and not share_rendered:
    st.divider()
    _render_share_formula(active_weights)
