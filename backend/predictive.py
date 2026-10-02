#!/usr/bin/env python3
"""
Predictive conflict layer for AMI's Disaster Watch (feature #5).

Pulls VIEWS (viewsforecasting.org) - a peer-reviewed, free, no-key project that
publishes MONTHLY PROBABILISTIC forecasts of armed-conflict fatalities per country,
up to 36 months out. A genuine prediction model, not current-events: it answers
"which of AMI's regions is conflict most likely to intensify in the coming months."

Used two ways:
  - /api/conflict-forecast renders a ranked, no-cost, no-click forecast panel.
  - The on-demand AI escalation watch is GROUNDED in these numbers so it can run a
    couple of quick searches for current context instead of scanning the world.

Free, cached 24h (VIEWS updates monthly).
"""

import json
import time
import threading
import urllib.request
from datetime import datetime, timezone

AMI_CONFLICT_ISO = {
    "SDN": "Sudan", "SOM": "Somalia", "ETH": "Ethiopia", "SSD": "South Sudan",
    "KEN": "Kenya", "UGA": "Uganda", "COD": "DR Congo",
    "SYR": "Syria", "YEM": "Yemen", "IRQ": "Iraq", "LBN": "Lebanon",
    "MMR": "Myanmar", "PAK": "Pakistan", "HTI": "Haiti",
}

_UA = {"User-Agent": "AMI-DisasterDashboard/1.0 (Acts of Mercy International; internal use)"}
_BASE = "https://api.viewsforecasting.org"
_CACHE = {"ts": 0.0, "data": None}
_LOCK = threading.Lock()
TTL = 86400


def _get(url, timeout=15):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(8_000_000).decode("utf-8", "replace")


def _latest_run():
    d = json.loads(_get(_BASE + "/"))
    fat = [r for r in d.get("runs", []) if r.startswith("fatalities") and r.endswith("_t01")]
    return fat[-1] if fat else None


def _country_forecast(run, iso):
    d = json.loads(_get(f"{_BASE}/{run}/cm?iso={iso}"))
    recs = sorted((d.get("data") or []), key=lambda x: x.get("month_id", 0))
    if not recs:
        return None
    near = recs[:3]
    horizon = recs[:6]
    avg3 = sum(r.get("main_mean", 0) or 0 for r in near) / len(near)
    prob = max((r.get("main_dich", 0) or 0) for r in near)
    trend = "steady"
    if len(horizon) >= 6:
        first = sum(r.get("main_mean", 0) or 0 for r in horizon[:3]) / 3
        last = sum(r.get("main_mean", 0) or 0 for r in horizon[3:6]) / 3
        if last > first * 1.25:
            trend = "rising"
        elif last < first * 0.8:
            trend = "easing"
    start = near[0]
    return {
        "iso": iso,
        "country": AMI_CONFLICT_ISO.get(iso, iso),
        "pred_fatalities": round(avg3),
        "probability": round(prob, 2),
        "trend": trend,
        "from": f"{start.get('year')}-{str(start.get('month')).zfill(2)}",
    }


def get_conflict_forecast(force=False):
    with _LOCK:
        if not force and _CACHE["data"] and (time.time() - _CACHE["ts"] < TTL):
            return _CACHE["data"]

    result = {"run": None, "countries": [], "error": None,
              "generated": None, "source": "https://viewsforecasting.org"}
    try:
        run = _latest_run()
        result["run"] = run
        rows = []
        for iso in AMI_CONFLICT_ISO:
            try:
                r = _country_forecast(run, iso)
                if r:
                    rows.append(r)
            except Exception:
                continue
        rows.sort(key=lambda x: -x["pred_fatalities"])
        result["countries"] = rows
    except Exception as e:
        result["error"] = str(e)[:120]

    result["generated"] = datetime.now(timezone.utc).isoformat()
    with _LOCK:
        if result["countries"] or not _CACHE["data"]:
            _CACHE["ts"] = time.time()
            _CACHE["data"] = result
            return result
        return _CACHE["data"]


def forecast_brief(max_countries=8):
    data = get_conflict_forecast()
    rows = data.get("countries", [])[:max_countries]
    if not rows:
        return ""
    lines = [f"- {r['country']}: ~{r['pred_fatalities']} predicted conflict deaths/month "
             f"(prob {r['probability']}), trend {r['trend'].upper()}" for r in rows]
    return ("VIEWS conflict-fatality forecast for AMI regions (run " + str(data.get("run")) +
            ", next ~3 months, most intense first):\n" + "\n".join(lines))


if __name__ == "__main__":
    print(json.dumps(get_conflict_forecast(force=True), indent=2)[:3000])
