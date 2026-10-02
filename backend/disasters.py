#!/usr/bin/env python3
"""
AMI Disaster Watch — live, predictive humanitarian-threat layer.

Pulls real-time hazard data and normalizes it into ONE threat model that the
dashboard renders. Sources (all free, no key):

  • NOAA / National Hurricane Center  — active tropical cyclones + FORMATION
    outlook (the pre-storm signal: % chance a disturbance becomes a storm).
  • GDACS (EU/UN)                     — global multi-hazard alerts with
    severity: cyclones, floods, droughts, earthquakes, volcanoes, wildfires.

Everything is flagged for AMI relevance (does it hit a country AMI works in?)
and matched to the disaster-relief funders already on AMI's grant board, so the
board answers the real question: *what should AMI do now, and whose money do we
go get* — before landfall, not after.

Conflict / war is handled separately by an on-demand AI escalation watch in
server.py (/api/conflict-watch), since it needs synthesis, not a data feed.

No Anthropic API calls happen here — this module is free to run and cached.
"""

import re
import json
import math
import time
import threading
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

# ─────────────────────────────────────────────────────────────────────────────
# AMI OPERATING REGIONS  ←←←  EDIT THIS to your real partner countries.
# A threat is flagged "AMI-relevant" if it affects any country in this set.
# Lower-case, matched against the country names each feed reports. Keeping it
# broad is fine — over-flagging is safer than missing a place you serve.
# ─────────────────────────────────────────────────────────────────────────────
AMI_COUNTRIES = {
    # Lauren confirmed AMI's four operating regions 2026-07-24. Add/remove freely.
    # ── East Africa / Horn ──
    "kenya", "ethiopia", "somalia", "uganda", "tanzania", "south sudan", "sudan",
    "rwanda", "burundi", "djibouti", "eritrea",
    # ── Caribbean & Central America ──
    "haiti", "dominican republic", "cuba", "jamaica", "bahamas",
    "guatemala", "honduras", "nicaragua", "el salvador", "belize", "mexico",
    # ── South & Southeast Asia ──
    "india", "nepal", "bangladesh", "pakistan", "sri lanka",
    "philippines", "indonesia", "myanmar", "thailand", "vietnam",
    # ── Middle East (mostly conflict-driven) ──
    "syria", "lebanon", "yemen", "iraq", "jordan",
}

# ─────────────────────────────────────────────────────────────────────────────
# NOAA 2026 Atlantic hurricane-season outlook (a seasonal forecast — update
# once a year, or when NOAA issues its August revision).
# ─────────────────────────────────────────────────────────────────────────────
SEASON = {
    "year": 2026,
    "basin": "Atlantic",
    "outlook": "below-normal",
    "named_storms": "8–14",
    "hurricanes": "3–6",
    "major_hurricanes": "1–3",
    "prob": {"below": 55, "near": 35, "above": 10},   # % likelihood of each
    "start": "2026-06-01",
    "end": "2026-11-30",
    "peak": "2026-09-10",
    "note": "El Niño is expected to suppress Atlantic activity this year.",
    "source": "https://www.noaa.gov/news-release/noaa-predicts-below-normal-2026-atlantic-hurricane-season",
}

# Disaster-relief funders already on AMI's grant board, matched to hazard type.
# This is what turns "a storm is coming" into "go talk to these funders now."
FUNDERS = {
    "lilly":   {"name": "Lilly Endowment — “Hope After the Storm”",
                "detail": "faith-based disaster relief, $500K–$5M · invitation-only — get on their radar BEFORE you need it"},
    "semnani": {"name": "Semnani Family Foundation",
                "detail": "international disaster relief, rolling — apply anytime"},
    "drk":     {"name": "Draper Richards Kaplan Foundation",
                "detail": "scalable relief models, up to $300K over 3 yrs, rolling"},
    "parkinson": {"name": "Parkinson Family Foundation",
                  "detail": "Food & Water / Health & Humanitarian, rolling, up to $5K — fast small grants"},
    "aja":     {"name": "AJA Foundation",
                "detail": "clean water incl. rural Africa (invitations currently paused — watch)"},
    "gupta":   {"name": "Gupta Family Foundation",
                "detail": "U.S. 501(c)(3)s operating internationally, $5K–$250K"},
}
FUNDER_MATCH = {
    "tropical":   ["lilly", "semnani", "drk"],
    "flood":      ["lilly", "semnani", "drk"],
    "earthquake": ["semnani", "drk", "lilly"],
    "volcano":    ["semnani", "drk"],
    "wildfire":   ["semnani", "drk"],
    "drought":    ["parkinson", "aja", "gupta"],
    "conflict":   ["semnani", "drk", "gupta"],
    "default":    ["semnani", "drk"],
}

HAZARD = {
    "TC": ("Tropical Cyclone", "🌀", "tropical"),
    "TS": ("Tropical Storm", "🌀", "tropical"),
    "HU": ("Hurricane", "🌀", "tropical"),
    "FL": ("Flood", "🌊", "flood"),
    "DR": ("Drought", "🌵", "drought"),
    "EQ": ("Earthquake", "🌎", "earthquake"),
    "VO": ("Volcano", "🌋", "volcano"),
    "WF": ("Wildfire", "🔥", "wildfire"),
    "TS_STORM": ("Severe Storm", "⛈️", "storm"),
}

_UA = {"User-Agent": "AMI-DisasterDashboard/1.0 (Acts of Mercy International; internal use)"}
_CACHE = {"ts": 0.0, "data": None}     # last SUCCESSFUL payload (kept for stale-on-error)
_LOCK = threading.Lock()               # guards _CACHE reads/writes
_FETCH_LOCK = threading.Lock()         # single-flight: only one thread hits the feeds at a time
TTL = 1800  # seconds — feeds update a few times/day; don't hammer them
MIN_REFRESH = 60  # ignore ?refresh=1 if we fetched this recently (protects the feeds)


# ── helpers ──────────────────────────────────────────────────────────────────
def _get(url, timeout=12):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(4_000_000).decode("utf-8", "replace")  # cap body size


def _safe_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ── Storm forecast track → "closest approach to an AMI coast + ETA" (#1) ──────
# Representative landfall points for AMI regions in the basins NHC forecasts
# (Atlantic + East Pacific). Global-basin cyclones (Philippines, Bay of Bengal)
# arrive via GDACS country-matching, not NHC tracks.
AMI_APPROACH_POINTS = {
    "Haiti": (19.0, -72.4), "Dominican Republic": (18.7, -70.2), "Cuba": (21.8, -78.0),
    "Jamaica": (18.1, -77.3), "Bahamas": (24.5, -76.5), "Belize": (17.2, -88.5),
    "Honduras": (15.8, -85.5), "Guatemala": (15.7, -88.6), "Nicaragua": (13.5, -83.5),
    "Mexico (Gulf/Caribbean)": (19.5, -90.5), "Mexico (Pacific)": (16.8, -99.9),
    "El Salvador": (13.5, -89.2),
}
APPROACH_KM = 450  # forecast point within this of an AMI coast → "approaching"


def _haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))


def _dt_from_daytime(day, hh, now):
    """NHC advisories stamp forecast points as DD/HHMMZ (day-of-month). Rebuild a
    full UTC datetime, rolling into next month when the day has wrapped."""
    for yr, mo in [(now.year, now.month),
                   (now.year + (now.month == 12), now.month % 12 + 1)]:
        try:
            dt = datetime(yr, mo, day, hh, 0, tzinfo=timezone.utc)
        except ValueError:
            continue
        if dt >= now - timedelta(days=1):
            return dt
    return now


def _parse_forecast_points(text, now=None):
    now = now or datetime.now(timezone.utc)
    text = re.sub(r"<[^>]+>", " ", text)
    pts = []
    for m in re.finditer(r"(\d{2})/(\d{2})(\d{2})Z\s+(\d{1,2}\.\d)([NS])\s+(\d{1,3}\.\d)([EW])", text):
        day, hh, _mm, lat, ns, lon, ew = m.groups()
        pts.append({
            "hours": (_dt_from_daytime(int(day), int(hh), now) - now).total_seconds() / 3600.0,
            "lat": float(lat) * (1 if ns == "N" else -1),
            "lon": float(lon) * (1 if ew == "E" else -1),
        })
    return pts


def _closest_ami_approach(points):
    """Nearest an AMI coast the forecast track gets, and when."""
    best = None
    for p in points:
        if p["hours"] < -3:
            continue
        for name, (alat, alon) in AMI_APPROACH_POINTS.items():
            d = _haversine_km(p["lat"], p["lon"], alat, alon)
            if best is None or d < best["km"]:
                best = {"country": name, "km": round(d), "hours": round(p["hours"])}
    return best


def _storm_forecast_approach(storm_json):
    """Return {country, km, hours} if this storm is forecast to pass near an AMI
    coast, plus whether we actually got forecast data. Never raises."""
    try:
        url = (storm_json.get("forecastAdvisory") or {}).get("url")
        if not url:
            return None, False
        pts = _parse_forecast_points(_get(url))
        if not pts:
            return None, False
        appr = _closest_ami_approach(pts)
        return (appr if (appr and appr["km"] <= APPROACH_KM) else None), True
    except Exception:
        return None, False


def _countries_hit(country_str):
    """Return the AMI countries a country string touches. Substring match so
    feed variants like 'Tanzania, United Rep. of' or 'Congo, Dem. Rep.' still
    hit (over-flagging is safer than missing a place AMI serves)."""
    if not country_str:
        return []
    s = str(country_str).lower()
    return [c for c in AMI_COUNTRIES if c in s]


def _funders_for(hclass):
    keys = FUNDER_MATCH.get(hclass, FUNDER_MATCH["default"])
    return [FUNDERS[k] for k in keys]


def _readiness(threat):
    """Template readiness guidance — 'what AMI should do now' for a threat."""
    hclass = threat["hazard_class"]
    where = threat.get("ami_countries") or threat.get("countries_list") or []
    where_txt = ", ".join(c.title() for c in where[:4]) if where else threat.get("region", "the affected area")
    actions = []
    if threat.get("timing_status") == "forecast" or threat.get("formation"):
        actions.append(f"Pre-position: alert AMI / Antioch church partners in {where_txt} while there's still lead time.")
    else:
        actions.append(f"Contact AMI / Antioch church partners in {where_txt} to confirm safety and immediate needs.")
    if hclass == "tropical":
        actions.append("Ready a rapid-response proposal now — funders move fastest in the 72 hrs around landfall.")
    elif hclass == "drought":
        actions.append("Slow-onset crisis: line up clean-water / food-security funding before it peaks.")
    elif hclass == "earthquake":
        actions.append("Assess partner facilities; earthquake relief windows open and close fast.")
    else:
        actions.append("Draft a short situation brief you can attach to a funder request.")
    actions.append("Log this in the grant helper and start the matched proposals below.")
    return {"actions": actions, "funders": _funders_for(hclass)}


# ── NHC: active tropical cyclones ────────────────────────────────────────────
def _fetch_nhc_storms():
    out = []
    raw = _get("https://www.nhc.noaa.gov/CurrentStorms.json")
    data = json.loads(raw)
    for s in (data.get("activeStorms") or []):
      try:
        cls = s.get("classification") or ""
        winds = s.get("intensity") or ""
        try:
            winds_mph = round(int(winds) * 1.15078)
        except (TypeError, ValueError):
            winds_mph = None
        label, icon, hclass = HAZARD.get(cls, ("Tropical System", "🌀", "tropical"))
        # category for hurricanes
        cat = ""
        if cls == "HU" and winds_mph:
            for lo, c in [(157, "5"), (130, "4"), (111, "3"), (96, "2"), (74, "1")]:
                if winds_mph >= lo:
                    cat = f"Cat {c}"
                    break
        basin = "Atlantic" if s.get("binNumber", "").startswith("AT") else \
                "East Pacific" if s.get("binNumber", "").startswith("EP") else \
                "Central Pacific" if s.get("binNumber", "").startswith("CP") else "—"
        adv = (s.get("publicAdvisory") or {}).get("url") or \
              (s.get("forecastGraphics") or {}).get("url", "https://www.nhc.noaa.gov")
        # #1 — forecast track: is this storm heading for an AMI coast, and when?
        approach, got_fc = _storm_forecast_approach(s)
        forecast_txt = ""
        if approach:
            days = approach["hours"] / 24.0
            eta = f"~{approach['hours']}h" if approach["hours"] < 48 else f"~{days:.1f} days"
            miles = round(approach["km"] * 0.621)
            forecast_txt = f"Forecast to pass near {approach['country']} in {eta} ({miles} mi at closest)"
        out.append({
            "source": "NHC",
            "hazard_class": hclass,
            "hazard_label": (f"{label} {cat}").strip(),
            "icon": icon,
            "name": s.get("name", "Unnamed"),
            "alert": "red" if cls == "HU" else "orange",
            "basin": basin,
            "region": basin,
            "position": f'{s.get("latitude","")}, {s.get("longitude","")}',
            "winds_mph": winds_mph,
            "movement": f'toward {s.get("movementDir","?")}° at {s.get("movementSpeed","?")} kt',
            "headline": f'{label} {s.get("name","")} — {winds_mph or "?"} mph, {basin}'.strip(),
            "countries_list": [],
            "ami_countries": [approach["country"]] if approach else [],
            "forecast": forecast_txt,                       # human-readable ETA line
            "eta_hours": approach["hours"] if approach else None,
            # Relevant only if forecast to approach an AMI coast. If the advisory
            # couldn't be parsed, fall back to the old Atlantic heuristic so we
            # never silently drop a real Atlantic threat.
            "ami_relevant": bool(approach) or (not got_fc and basin == "Atlantic"),
            "timing_status": "forecast" if approach else "active",
            "url": adv,
            "updated": s.get("lastUpdate", ""),
        })
      except Exception:
        continue
    return out


# ── NHC: formation outlook (the predictive pre-storm signal) ─────────────────
def _parse_formation(xml_text, basin):
    root = ET.fromstring(xml_text)
    desc = ""
    for item in root.iter("item"):
        d = item.findtext("description") or ""
        if d:
            desc = d
            break
    text = re.sub(r"<[^>]+>", " ", desc)
    text = re.sub(r"\s+", " ", text).strip()
    # Strip the WMO / product header cruft ("000 ABPZ20 KNHC ... Miami FL <time> <date>")
    m = re.search(r"For the (?:North Atlantic|eastern|central|Gulf|Caribbean|western)[^:]{0,80}:", text)
    if m:
        text = text[m.start():]
    else:
        text = re.sub(r"^\s*\d{3}\s+[A-Z0-9 ]+?\d{4}\s+", "", text)  # drop leading code block
    pcts = [int(p) for p in re.findall(r"(\d{1,3})\s*percent", text)]
    max_pct = max(pcts) if pcts else 0
    if "not expected" in text.lower() or not text:
        risk, summary = "none", "Quiet — no tropical development expected in the next 7 days."
    elif max_pct >= 60:
        risk = "high"
    elif max_pct >= 30:
        risk = "medium"
    else:
        risk = "low"
    if text and "not expected" not in text.lower():
        summary = (text[:280] + "…") if len(text) > 280 else text
    return {
        "basin": basin,
        "risk": risk,
        "max_pct": max_pct,
        "summary": summary,
        "source": "https://www.nhc.noaa.gov/gtwo.php",
    }


def _fetch_nhc_formation():
    outlooks = []
    for url, basin in [
        ("https://www.nhc.noaa.gov/xml/TWOAT.xml", "Atlantic"),
        ("https://www.nhc.noaa.gov/xml/TWOEP.xml", "East Pacific"),
    ]:
        try:
            outlooks.append(_parse_formation(_get(url), basin))
        except Exception as e:
            outlooks.append({"basin": basin, "risk": "unknown", "max_pct": 0,
                             "summary": f"Outlook unavailable ({str(e)[:60]}).",
                             "source": "https://www.nhc.noaa.gov/gtwo.php"})
    return outlooks


# ── GDACS: global multi-hazard alerts ────────────────────────────────────────
def _fetch_gdacs():
    # MAP = GDACS's *currently active* event set (SEARCH is a year-long archive
    # that's ~98% stale). Filter to Orange/Red and dedupe by eventid, keeping the
    # latest episode, so ongoing droughts/floods don't show as duplicates.
    out = []
    raw = _get("https://www.gdacs.org/gdacsapi/api/events/geteventlist/MAP")
    data = json.loads(raw)
    best = {}  # eventid -> (episodeid, props)
    for feat in data.get("features", []):
        p = feat.get("properties", {})
        if (p.get("alertlevel") or "").lower() not in ("orange", "red"):
            continue
        eid = p.get("eventid")
        try:
            ep = int(p.get("episodeid") or 0)
        except (TypeError, ValueError):
            ep = 0
        if eid not in best or ep > best[eid][0]:
            best[eid] = (ep, p)
    for _, p in best.values():
      try:
        etype = p.get("eventtype") or ""
        label, icon, hclass = HAZARD.get(etype, (etype or "Hazard", "⚠️", "other"))
        country_str = p.get("country") or ", ".join(
            (c.get("countryname") or "") for c in (p.get("affectedcountries") or []))
        hit = _countries_hit(country_str)
        sev = (p.get("severitydata") or {}).get("severitytext", "")
        url = ""
        u = p.get("url")
        if isinstance(u, dict):
            url = u.get("report") or u.get("details") or ""
        out.append({
            "source": "GDACS",
            "hazard_class": hclass,
            "hazard_label": label,
            "icon": icon,
            "name": p.get("eventname") or p.get("name") or label,
            "alert": (p.get("alertlevel") or "green").lower(),
            "region": country_str,
            "countries_list": [c.strip() for c in re.split(r"[,;]", country_str) if c.strip()],
            "ami_countries": hit,
            "ami_relevant": bool(hit),
            "severity": sev,
            "headline": ((p.get("htmldescription") or p.get("description") or p.get("name") or "")).strip(),
            "timing_status": "active",
            "from": p.get("fromdate") or "",
            "to": p.get("todate") or "",
            "alertscore": _safe_float(p.get("alertscore")),
            "url": url or "https://www.gdacs.org",
        })
      except Exception:
        continue
    return out


# ── season context (turns the calendar into a prediction) ────────────────────
def _season_context():
    today = date.today()
    try:
        start = date.fromisoformat(SEASON["start"])
        end = date.fromisoformat(SEASON["end"])
        peak = date.fromisoformat(SEASON["peak"])
    except Exception:
        return dict(SEASON, active=False)
    total = (end - start).days
    elapsed = (today - start).days
    active = start <= today <= end
    return dict(
        SEASON,
        active=active,
        day=max(0, elapsed),
        total_days=total,
        pct_through=round(100 * max(0, min(elapsed, total)) / total) if total else 0,
        days_to_peak=(peak - today).days,
        near_peak=abs((peak - today).days) <= 45,
    )


# ── public entrypoint ────────────────────────────────────────────────────────
def _assemble():
    """Fetch + normalize. Everything after the fetches is wrapped so one bad
    record or an upstream shape change can never 500 the whole board."""
    sources_ok, errors = [], []

    def _try(fn, name):
        try:
            r = fn()
            sources_ok.append(name)
            return r
        except Exception as e:
            errors.append(f"{name}: {str(e)[:80]}")
            return []

    storms = _try(_fetch_nhc_storms, "NHC storms")
    formation = _try(_fetch_nhc_formation, "NHC formation") or []
    gdacs = _try(_fetch_gdacs, "GDACS")

    threats = list(storms)
    try:
        # merge storms + gdacs (dedupe TCs GDACS also lists), attach readiness, rank
        storm_names = {(s.get("name") or "").lower() for s in storms}
        for g in gdacs:
            gname = (g.get("name") or "").lower().split("-")[0].strip()
            if g.get("hazard_class") == "tropical" and gname in storm_names:
                continue
            threats.append(g)
        for t in threats:
            try:
                t["readiness"] = _readiness(t)
            except Exception:
                t["readiness"] = {"actions": [], "funders": []}
        rank_alert = {"red": 0, "orange": 1, "green": 2}
        threats.sort(key=lambda t: (
            rank_alert.get(t.get("alert", "green"), 3),
            0 if t.get("ami_relevant") else 1,
            -_safe_float(t.get("alertscore", 0)),
        ))
    except Exception as e:
        errors.append(f"assembly: {str(e)[:80]}")  # keep partial (unsorted) threats

    return {
        "generated": datetime.now(timezone.utc).isoformat(),
        "season": _season_context(),
        "formation": formation,
        "threats": threats,
        "summary": {
            "total": len(threats),
            "red": len([t for t in threats if t.get("alert") == "red"]),
            "orange": len([t for t in threats if t.get("alert") == "orange"]),
            "ami_relevant": len([t for t in threats if t.get("ami_relevant")]),
            "active_storms": len(storms),
            "atlantic_storms": len([s for s in storms if s.get("basin") == "Atlantic"]),
        },
        "ami_countries": sorted(AMI_COUNTRIES),
        "sources_ok": sources_ok,
        "errors": errors,
    }


def get_disaster_data(force=False):
    """Assemble the normalized threat board. Cached TTL seconds; single-flight
    on refresh; serves last-known-good (flagged stale) if all feeds fail."""
    now = time.time()
    with _LOCK:
        cached = _CACHE["data"]
        age = now - _CACHE["ts"]
        if cached and (not force or age < MIN_REFRESH) and age < TTL:
            return cached

    # single-flight: one thread fetches while others wait, then read the fresh cache
    with _FETCH_LOCK:
        with _LOCK:
            cached = _CACHE["data"]
            age = time.time() - _CACHE["ts"]
            if cached and (not force or age < MIN_REFRESH) and age < TTL:
                return cached
        result = _assemble()
        # If everything failed, don't cache the empty board — serve last-known-good.
        if not result.get("sources_ok") and _CACHE["data"]:
            stale = dict(_CACHE["data"])
            stale["stale"] = True
            stale["errors"] = result.get("errors", [])
            return stale
        with _LOCK:
            _CACHE["ts"] = time.time()
            _CACHE["data"] = result
        return result


if __name__ == "__main__":
    print(json.dumps(get_disaster_data(force=True), indent=2)[:4000])
