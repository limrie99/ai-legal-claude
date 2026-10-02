#!/usr/bin/env python3
"""
Weekly Disaster Watch digest for AMI (feature #4).

Builds a short brief of what CHANGED since last week so staff don't have to
remember to open the dashboard: new hazards in AMI regions, storms newly
forecast to approach an AMI coast, hazards that cleared, and the conflict
forecast's biggest risers. Diffs against a small state file so it only reports
change, not the whole board.

Delivery: always writes a dated markdown file under ../digests/. If DIGEST_TO
(or the older EMMA_EMAIL) is set it also emails, via Microsoft Graph using the
same app-only credentials scp-ops-alerts sends with. With no recipient set it
only writes the file, which is what the Monday cron has done since 2026-07-25.
Run weekly from cron.
"""
import os
import sys
import json
from pathlib import Path
from datetime import datetime, timezone

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import disasters
import predictive

STATE = HERE.parent / "digest_state.json"
OUTDIR = HERE.parent / "digests"


def _load_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {"threats": {}, "conflict": {}}


def build_digest():
    board = disasters.get_disaster_data(force=True)
    fc = predictive.get_conflict_forecast(force=True)
    prev = _load_state()

    threats = board.get("threats", [])
    now_threats = {t.get("name", "?"): t for t in threats}
    prev_threats = prev.get("threats", {})

    new = [t for n, t in now_threats.items() if n not in prev_threats]
    gone = [n for n in prev_threats if n not in now_threats]
    escalated = [t for n, t in now_threats.items()
                 if n in prev_threats and prev_threats[n] == "orange" and t.get("alert") == "red"]
    approaching = [t for t in threats if t.get("forecast")]
    ami_now = [t for t in threats if t.get("ami_relevant")]

    # conflict risers
    risers = [c for c in fc.get("countries", []) if c.get("trend") == "rising"
              and (c.get("pred_fatalities", 0) >= 10 or c.get("probability", 0) >= 0.3)]
    risers.sort(key=lambda c: -c.get("pred_fatalities", 0))

    L = []
    L.append(f"# 🌪️ AMI Disaster Watch — weekly brief")
    L.append(f"_As of {datetime.now(timezone.utc).strftime('%b %d, %Y')}. AI + open-data helper. Verify before acting._\n")

    L.append("## ⚡ What changed this week")
    if not (new or gone or escalated or approaching):
        L.append("- No major changes in AMI regions. Quiet week.")
    for t in escalated:
        L.append(f"- 🔴 **ESCALATED to severe:** {t['name']} ({t.get('region','')})")
    for t in new:
        if t.get("ami_relevant"):
            L.append(f"- 🎯 **New in an AMI region:** {t['name']} — {t.get('hazard_label','')} ({t.get('region','')})")
    for t in approaching:
        L.append(f"- 🛰️ **Storm approaching:** {t['name']} — {t.get('forecast','')}")
    for n in gone:
        L.append(f"- ✅ Cleared: {n}")

    L.append("\n## 🎯 Active in AMI regions right now")
    if ami_now:
        for t in ami_now:
            L.append(f"- **{t['name']}** ({t.get('hazard_label','')}) — {t.get('region','')}. {t.get('severity','')}")
    else:
        L.append("- Nothing at orange/red in an AMI region.")

    L.append("\n## 🔮 Conflict forecast — biggest risers (next ~3 months)")
    if risers:
        for c in risers[:5]:
            L.append(f"- **{c['country']}** ▲ rising — ~{c['pred_fatalities']} predicted conflict deaths/month "
                     f"(prob {int(c['probability']*100)}%)")
    else:
        L.append("- No AMI-region conflicts forecast to intensify this cycle.")
    L.append(f"\n_Conflict forecast: VIEWS run {fc.get('run')}. Hurricane season, hazards: NOAA/NHC + GDACS._")

    L.append("\n## ✅ Do next")
    top = ami_now[:2] + risers[:1]
    if top:
        for t in ami_now[:2]:
            L.append(f"- {t['name']}: contact partners in {t.get('region','')}, ready a rapid-response proposal, "
                     f"and start the matched funders in the grant helper.")
        for c in risers[:1]:
            L.append(f"- {c['country']}: conflict rising — check partner networks and line up relief funders now.")
    else:
        L.append("- Nothing urgent. Keep the formation watch and conflict forecast in view.")

    digest = "\n".join(L)

    # save state for next week's diff
    STATE.write_text(json.dumps({
        "threats": {n: t.get("alert") for n, t in now_threats.items()},
        "conflict": {c["iso"]: c["pred_fatalities"] for c in fc.get("countries", [])},
        "updated": datetime.now(timezone.utc).isoformat(),
    }))
    return digest


def _for_email(md: str) -> str:
    """Nudge the digest markdown into the subset ops_common._md_to_html renders.

    That helper understands **bold**, _italic_ and newlines, but not headings or
    bullets -- so a raw digest arrives with literal '#' and '-' characters. We
    turn headings into bold lines and bullets into real bullet characters here,
    rather than editing the shared helper that other alerts depend on.

    Whole-line italics are flattened: the footer contains a VIEWS run id like
    fatalities003_2026_08_t01, and leaving the surrounding underscores in place
    makes the italic regex chew through it.
    """
    out = []
    for line in md.splitlines():
        t = line.strip()
        if t.startswith("#"):
            out.append("**" + t.lstrip("#").strip() + "**")
        elif t.startswith("- "):
            out.append("\u2022 " + t[2:])
        elif len(t) > 1 and t.startswith("_") and t.endswith("_"):
            out.append(t[1:-1])
        else:
            out.append(line)
    return "\n".join(out)


def _send(subject: str, body_md: str, to: str) -> tuple[bool, str]:
    """Graph first, SMTP only as a fallback. Never both."""
    try:
        sys.path.insert(0, "/home/scpadmin/scp-apps-lauren/scp-ops-alerts")
        from ops_common import send_email  # client-credentials Graph, saveToSentItems=False
        if send_email(subject, _for_email(body_md), {"ALERT_EMAIL": to}):
            return True, "graph"
        reason = "graph declined (missing creds or recipient)"
    except Exception as e:
        reason = f"graph error: {e}"

    host = os.environ.get("SMTP_HOST", "")
    if not host:
        return False, reason
    try:
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(body_md)
        msg["Subject"] = subject
        msg["From"] = os.environ.get("SMTP_FROM", "disaster-watch@actsofmercy")
        msg["To"] = to
        with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587"))) as sv:
            sv.starttls()
            if os.environ.get("SMTP_USER"):
                sv.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASS", ""))
            sv.send_message(msg)
        return True, "smtp"
    except Exception as e:
        return False, f"{reason}; smtp error: {e}"


def deliver(digest):
    OUTDIR.mkdir(exist_ok=True)
    fname = OUTDIR / f"digest-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.md"
    fname.write_text(digest)

    # DIGEST_TO is the live switch. EMMA_EMAIL still works -- it is the older
    # name and the one the module docstring used to describe. With neither set
    # nothing is sent, which is the safe default and the current cron behaviour.
    to = (os.environ.get("DIGEST_TO") or os.environ.get("EMMA_EMAIL") or "").strip()
    if not to:
        return f"wrote {fname} \u00b7 no recipient (set DIGEST_TO to email it)"

    ok, how = _send("AMI Disaster Watch \u2014 weekly brief", digest, to)
    return f"wrote {fname} \u00b7 " + (f"emailed {to} via {how}" if ok else f"NOT SENT to {to}: {how}")


if __name__ == "__main__":
    d = build_digest()
    print(d)
    print("\n---")
    print(deliver(d))
