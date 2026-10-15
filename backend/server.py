#!/usr/bin/env python3
"""
AI Legal Assistant — Backend Server
Serves the frontend and provides /api/analyze endpoint that calls Claude API
with 5 parallel agents via Server-Sent Events (SSE).
"""

import base64
import json
import os
import sys
import threading
import time
import queue
import tempfile
from http.server import HTTPServer, ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

# ── Find anthropic SDK ──
try:
    import anthropic
except ImportError:
    print("Error: anthropic SDK not installed. Run: pip3 install anthropic")
    sys.exit(1)

# ── Import PDF generator ──
SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
try:
    from generate_legal_pdf import build_pdf as _build_pdf_original
    HAS_PDF = True
except ImportError:
    HAS_PDF = False

# ── Import disaster-watch module (live NHC + GDACS hazard feeds) ──
sys.path.insert(0, str(Path(__file__).parent))
try:
    import disasters as disaster_feed
    HAS_DISASTERS = True
except Exception as _dexc:
    HAS_DISASTERS = False
    print(f"  (disaster watch unavailable: {_dexc})")

# ── Import predictive conflict-forecast module (VIEWS) ──
try:
    import predictive as conflict_feed
    HAS_PREDICTIVE = True
except Exception as _pexc:
    HAS_PREDICTIVE = False
    print(f"  (conflict forecast unavailable: {_pexc})")

# ── Config ──
PORT = int(os.environ.get("PORT", 8080))
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")

# ── Hard spend cap (protects the Anthropic API budget) ──
CASH_CAP_USD = float(os.environ.get("CASH_CAP_USD", "10"))
SPEND_LEDGER = Path(__file__).parent.parent / "spend_ledger.json"
PRICE_IN_PER_MTOK = float(os.environ.get("PRICE_IN_PER_MTOK", "3"))    # claude-sonnet-4-6 input
PRICE_OUT_PER_MTOK = float(os.environ.get("PRICE_OUT_PER_MTOK", "15"))  # claude-sonnet-4-6 output

# ── Shared-password gate (HTTP Basic Auth) ──
LEGAL_USER = os.environ.get("LEGAL_USER", "")
LEGAL_PASSWORD = os.environ.get("LEGAL_PASSWORD", "")

_spend_lock = threading.Lock()
_LEDGER_BROKEN = False  # if the ledger can't be written, fail CLOSED (block spend)


def _read_spend():
    try:
        return json.loads(SPEND_LEDGER.read_text())
    except Exception:
        return {"spent_usd": 0.0}


def record_usage(response):
    """Add one Claude API call's cost to the persistent ledger."""
    global _LEDGER_BROKEN
    try:
        u = response.usage
        cost = (u.input_tokens / 1_000_000) * PRICE_IN_PER_MTOK \
            + (u.output_tokens / 1_000_000) * PRICE_OUT_PER_MTOK
    except Exception:
        return
    with _spend_lock:
        d = _read_spend()
        d["spent_usd"] = round(d.get("spent_usd", 0.0) + cost, 6)
        try:
            SPEND_LEDGER.write_text(json.dumps(d))
        except Exception as e:
            # Can't persist spend → the cap can no longer protect the budget.
            # Trip a fail-closed flag so over_budget() blocks further AI calls.
            _LEDGER_BROKEN = True
            print(f"\033[1;31m[BUDGET] spend ledger unwritable ({e}); failing closed.\033[0m")


def spend_so_far():
    with _spend_lock:
        return _read_spend().get("spent_usd", 0.0)


def over_budget():
    return _LEDGER_BROKEN or spend_so_far() >= CASH_CAP_USD


# ── Web-search-backed generation (shared by grant "find" + conflict watch) ──
WEB_SEARCH_TOOL = os.environ.get("WEB_SEARCH_TOOL", "web_search_20260209")
_WEB_SEARCH_LOCK = threading.Lock()   # single-flight: one web-search request at a time
_LAST_WEB_SEARCH = [0.0]              # timestamp for a light cooldown
WEB_SEARCH_COOLDOWN = 45             # seconds between web-search runs (anti-spam)


def run_web_search(client, messages, max_uses=5, max_rounds=3):
    """One web-search-backed turn, resilient to the pause_turn server-tool
    resubmit bug: in this environment web_search also emits code-execution
    result blocks that the API REJECTS when echoed back on a continuation.
    The first call almost always completes in one turn (end_turn); any
    continuation that fails is swallowed and we return whatever text we have.
    Lets the FIRST call's exception propagate so real errors reach the caller.
    """
    tools = [{"type": WEB_SEARCH_TOOL, "name": "web_search", "max_uses": max_uses}]
    resp = client.messages.create(model=MODEL, max_tokens=4096, messages=messages, tools=tools)
    record_usage(resp)
    text_out = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
    rounds = 0
    while getattr(resp, "stop_reason", "") == "pause_turn" and rounds < max_rounds:
        rounds += 1
        try:
            messages.append({"role": "assistant", "content": resp.content})
            resp = client.messages.create(model=MODEL, max_tokens=4096, messages=messages, tools=tools)
            record_usage(resp)
        except Exception:
            break  # continuation rejected (server-tool resubmit) — keep last good text
        t = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
        if t:
            text_out = t
    return text_out


# ── Acts of Mercy International framing (applied to every agent + the scorer) ──
AMI_CONTEXT = """You are reviewing a document for ACTS OF MERCY INTERNATIONAL (AMI),
a faith-based humanitarian / mission nonprofit. AMI runs activities like mission trips,
volunteer service, events, and outreach, so the documents you review are usually
LIABILITY WAIVERS, releases, assumption-of-risk forms, volunteer/participant agreements,
parental/guardian consent forms, medical & emergency-treatment authorizations, and
photo/media release forms — not commercial contracts.

Evaluate every document from AMI's protection standpoint. Pay special attention to whether
the document includes the protections a nonprofit needs: a clear assumption of risk,
a release/waiver of liability, indemnification of the organization, medical & emergency-care
authorization, a photo/media release, special handling for MINORS (parent/guardian signature),
proof-of-insurance or insurance requirements, governing law / venue, and a signature + date block.
When something important is MISSING, say so plainly — that is one of the most useful things you can do.

IMPORTANT — say this in your own findings where relevant: this is an AI helper for AMI staff,
NOT a lawyer and NOT legal advice. Flag anything significant for review by a licensed attorney
in AMI's state. Keep the exact JSON output format described below."""


# ── Draft / "figure out the paperwork" prompt (the CREATE side) ──
DRAFT_PROMPT = """You are helping staff at ACTS OF MERCY INTERNATIONAL (AMI), a faith-based
humanitarian / mission nonprofit, figure out and DRAFT the paperwork they need for an activity.
You are NOT a lawyer and this is NOT legal advice.

The user will describe an activity, event, trip, or situation. Produce ONE Markdown document with
exactly these sections and headings:

# Not legal advice
One short paragraph: this is an AI-generated starting point for AMI staff, not legal advice, and
every document must be reviewed by a licensed attorney in AMI's state before anyone signs it.

## Paperwork & forms you likely need
A checklist using `- [ ]` items of the documents AMI should have for the described activity. Consider:
liability waiver / release, assumption of risk, medical & emergency-treatment authorization,
photo/media release, parent/guardian consent for anyone under 18, participant code of conduct,
emergency-contact form, proof of insurance, and — if the activity involves minors or vulnerable
people — volunteer background checks. For each item add a short "— why it matters" note.

## Draft: Liability Waiver & Release
A complete, ready-to-edit draft waiver tailored to the activity, using [BRACKETED PLACEHOLDERS] for
anything AMI must fill in. Include: the parties, a description of the activity, assumption of risk,
release/waiver of liability, indemnification of AMI and its staff/volunteers, medical & emergency-care
authorization, a photo/media release, governing law / venue ([STATE]), and a signature + date block —
WITH a separate parent/guardian signature block for participants under 18.

## Before you use this
2-3 bullets: have a licensed attorney review it, keep signed copies on file, and check your state's
rules (some states limit how enforceable waivers are - especially for minors).

Be practical and plain-spoken. Output ONLY the Markdown document, nothing else."""


# ── Grant helper prompts (write / check / find) ──
_GRANT_INTRO = """You are helping staff at ACTS OF MERCY INTERNATIONAL (AMI), a faith-based
humanitarian / mission nonprofit, with GRANT work. You are NOT a professional grant writer, lawyer,
or accountant, and this is NOT professional, legal, or financial advice — it is an AI starting point
that AMI staff must review and verify. Begin your output with a one-line "Not professional advice —
review & verify before submitting" note."""

GRANT_PROMPTS = {
    "write": _GRANT_INTRO + """

Act like an EXPERIENCED PROFESSIONAL GRANT WRITER on AMI's staff. A real grant writer first gets the
facts, then writes. So do both: tell them what you need, then write the strongest draft you can.

Output Markdown in this exact order:

## 📋 Information I need from you (to make this submission-ready)
A short checklist of the SPECIFIC data a grant writer needs that wasn't provided — only the items
actually missing. Typically: EIN & date of 501(c)(3) determination; last fiscal year's total revenue
and expenses; number of people served last year; the exact project budget broken into line items;
measurable target outcomes (real numbers); key staff/leadership names & one-line bios; named local
partners on the ground; and the funder's exact program name, priorities, and deadline. End with:
"Reply with these (or fill the [brackets] below) and I'll turn this into a submission-ready draft."

Then the full proposal, using [BRACKETED PLACEHOLDERS] ONLY where real data is missing:
## Organization Summary
## Statement of Need  (name the real problem; mark any statistic you can't verify as [VERIFY])
## Project Description — Goals & Objectives
## Activities & Timeline
## Outcomes & How We'll Measure Them  (concrete, numeric targets)
## Budget Outline  (Markdown table: line items + [AMOUNT] + total)
## Sustainability & Future Funding
## Closing

Write specific, fundable, professional prose — never generic filler. If a funder is named, tailor the
language to that funder's stated priorities. NEVER invent facts about AMI — use a [PLACEHOLDER] and make
sure it's listed in the "Information I need" checklist above.""",

    "check": _GRANT_INTRO + """

The user pastes a grant application / proposal draft. Review it like a helpful grant reviewer and output Markdown:
## Quick read  (1-2 sentences + an informal readiness sense, e.g. "needs work / close / strong")
## What's working
## Gaps & weaknesses  (flag missing or weak: statement of need, measurable outcomes, budget justification,
   alignment to the funder's priorities, clarity, evaluation plan, sustainability)
## Specific suggested improvements  (concrete, actionable — quote/rewrite weak lines where useful)
Be candid and useful, not flattering.""",

    "find": _GRANT_INTRO + """

You HAVE a web search tool — USE IT to find REAL, CURRENT grant opportunities for this nonprofit.
Run several searches (e.g. grants for faith-based humanitarian nonprofits, mission / disaster-relief /
clean-water funding, foundations giving in the org's state/region, and the org's specific programs).

Output Markdown:
## Grant opportunities to look at
A list of REAL opportunities you actually found. For each, give: the funder/program name as a
**clickable markdown link to the source page**, who it funds, why it could fit AMI, and any deadline or
eligibility you saw — each clearly marked "(as of the page I found — VERIFY current details)".
## Also worth searching yourself
Databases & funder types to check directly: Candid / Foundation Directory, Grants.gov, local community
foundations, denominational grant offices, peer nonprofits' Form 990s.
## Before you apply
Eligibility, 501(c)(3) status, and confirming current deadlines/amounts on each funder's official site.

RULES: Only list opportunities you genuinely found via search, and include the source link for each.
If searches turn up little, say so honestly — do NOT invent grants, amounts, or deadlines. Always tell
the user to verify current availability, amounts, and deadlines on the funder's official site (these
change constantly).

Begin your reply DIRECTLY with the "Not professional advice" line. Do NOT include any preamble, planning
notes, or meta-commentary (no "Now I have enough results…", "Let me compile…", etc.) — output only the
final Markdown document.""",
}

# ── Conflict / war escalation watch (on-demand, web-search backed) ──
CONFLICT_PROMPT = """You are a humanitarian conflict-watch analyst for ACTS OF MERCY INTERNATIONAL (AMI),
a faith-based international relief nonprofit. This is an AI assessment, NOT verified intelligence — AMI
staff must verify before acting on anything here.

You HAVE a web search tool — USE IT. Run several searches for CURRENT (last few weeks) armed conflict,
war, political violence, coups, mass displacement, and complex humanitarian emergencies, focused on the
regions AMI works in (listed below). Be efficient: a handful of well-chosen searches, then write up your
assessment in one pass. Focus on the 5-8 most significant situations, not every country.

Output Markdown, beginning EXACTLY with: "AI conflict assessment — verify before acting."

## ⚔️ Active conflicts affecting AMI regions
For each relevant situation give: **Place** — one line on what's happening — **Escalation: RISING / STEADY /
EASING** with a one-line reason — humanitarian impact (displacement, access, needs) — tagged
"(as of the reporting I found — VERIFY)". Order by escalation risk, then severity.

## 🔮 Most likely to escalate next (weeks–months)
The 2–4 situations you predict are most likely to worsen or create new displacement, each with the
specific early-warning signal you're reading (troop movements, failed talks, election, famine, etc.).

## 🅰️ What this means for AMI
Per hot situation: which church / Antioch partner networks to check on, whether to pre-position now,
and which relief funders to line up (rolling: Semnani, Draper Richards Kaplan; larger faith-based
disaster relief: Lilly "Hope After the Storm").

RULES: report ONLY what you actually found via search, cite each with a source link, never invent
casualty figures or events, and say plainly if a region is currently quiet. Output only the final
Markdown — no planning notes or preamble beyond the required first line."""


# ── Agent definitions ──
AGENTS = [
    {
        "id": "clause",
        "name": "Clause Analyst",
        "color": "#22c55e",
        "weight": "20%",
        "prompt": """You are the Clause Analysis agent for a legal contract review tool.
Analyze the following contract and identify EVERY clause. For each clause provide:
1. Section number and name
2. Plain English summary (1-2 sentences)
3. Who it favors: Client, Contractor/Other Party, or Neutral
4. Any unusual or notable language

At the end, provide a summary count: total clauses, how many favor each party.

IMPORTANT: Output your analysis as a series of findings. For each finding, output a JSON line in this format:
{"type":"info|medium|high","badge":"short label","summary":"one line summary","detail":{"title":"...","risk":"HIGH|MEDIUM|LOW","text":"explanation","fix":"suggested fix or null"}}

End with a final line:
{"type":"info","badge":"Complete","summary":"Clause analysis complete: X clauses — Y favor Client, Z favor Contractor, W neutral"}

Output ONLY these JSON lines, one per line. No other text.""",
    },
    {
        "id": "risk",
        "name": "Risk Assessor",
        "color": "#ef4444",
        "weight": "25%",
        "prompt": """You are the Risk Assessment agent for a legal contract review tool.
Score every clause in this contract for risk from the weaker party's perspective (usually the contractor/employee/freelancer).

For each risky clause provide a score (1-10) and estimate financial exposure where possible.

IMPORTANT: Output your findings as JSON lines in this format:
{"type":"high|medium|low","badge":"X/10","summary":"Section X.X: description — $XX exposure","detail":{"title":"...","risk":"HIGH (X/10)","text":"detailed explanation","fix":"recommended change"}}

Focus on the highest-risk clauses first. Include at least the top 8-10 risks.

End with:
{"type":"info","badge":"Complete","summary":"Risk assessment complete: Overall X/10 — X high, X medium, X low risk clauses"}

Output ONLY these JSON lines, one per line. No other text.""",
    },
    {
        "id": "compliance",
        "name": "Compliance Checker",
        "color": "#a855f7",
        "weight": "20%",
        "prompt": """You are the Compliance Check agent for a legal contract review tool.
Flag any regulatory, legal, or jurisdictional concerns including:
- Worker misclassification risks
- Non-compete enforceability issues
- IP assignment legality
- Jurisdictional/governing law concerns
- Data protection / privacy gaps
- Unconscionability (aggregate one-sidedness)
- Tax compliance issues

IMPORTANT: Output findings as JSON lines:
{"type":"critical|high|medium","badge":"CRITICAL|HIGH|MODERATE","summary":"one line description","detail":{"title":"...","risk":"CRITICAL|HIGH|MODERATE","text":"explanation","fix":"recommendation"}}

End with:
{"type":"info","badge":"Complete","summary":"Compliance check complete: X critical, X high, X moderate issues"}

Output ONLY these JSON lines, one per line. No other text.""",
    },
    {
        "id": "terms",
        "name": "Terms Mapper",
        "color": "#06b6d4",
        "weight": "15%",
        "prompt": """You are the Terms & Obligations agent for a legal contract review tool.
Map every duty, deadline, trigger, and consequence for ALL parties. Include:
- All obligations for each party
- Payment terms and milestones
- Termination triggers and consequences
- Key deadlines and notice periods
- What survives termination
- Missing protections that should be present

IMPORTANT: Output findings as JSON lines:
{"type":"info|medium|high","badge":"short label","summary":"one line description","detail":{"title":"...","risk":"HIGH|MEDIUM|LOW|INFO","text":"explanation","fix":"recommendation or null"}}

End with:
{"type":"info","badge":"Complete","summary":"Terms mapping complete: X obligations, X deadlines, X missing protections"}

Output ONLY these JSON lines, one per line. No other text.""",
    },
    {
        "id": "recommendation",
        "name": "Recommendations",
        "color": "#f97316",
        "weight": "20%",
        "prompt": """You are the Recommendations agent for a legal contract review tool.
Generate specific fix recommendations with ACTUAL REPLACEMENT CONTRACT LANGUAGE for every problematic clause.

For each issue provide:
1. The problem
2. Why it matters
3. Specific replacement language to propose
4. Priority: CRITICAL (walk-away issue) / HIGH / MEDIUM

IMPORTANT: Output findings as JSON lines:
{"type":"critical|high|medium","badge":"FIX #N","summary":"one line: what to change","detail":{"title":"Fix: Issue Name (Section X.X)","risk":"CRITICAL|HIGH|MEDIUM","text":"Current: ...\n\nProposed: ...","fix":"Specific replacement contract language"}}

Number fixes from most critical to least. End with:
{"type":"info","badge":"Complete","summary":"X fixes generated: X critical, X high, X medium — DO NOT SIGN without addressing critical items"}

Output ONLY these JSON lines, one per line. No other text.""",
    },
]

SCORE_PROMPT = """Based on the following contract analysis from 5 agents, provide a Contract Safety Score.

Score the contract from 0-100 where:
- 90-100 (A+): Safe — low risk, standard favorable terms
- 80-89 (A): Good — minor issues
- 70-79 (B): Fair — some concerning clauses
- 60-69 (C): Caution — multiple risky clauses
- 40-59 (D): Risky — significant risks
- 0-39 (F): Dangerous — do not sign without major revisions

Respond with ONLY a JSON object:
{"score": NUMBER, "grade": "LETTER", "label": "WORD", "summary": "2-3 sentence executive summary"}
"""


# ── SSE Event Queue ──
class AnalysisSession:
    def __init__(self):
        self.queue = queue.Queue()
        self.done = threading.Event()
        self.findings = []

    def send(self, event_type, data):
        if event_type == "finding":
            self.findings.append(data)
        self.queue.put(f"event: {event_type}\ndata: {json.dumps(data)}\n\n")

    def finish(self):
        self.queue.put("event: done\ndata: {}\n\n")
        self.done.set()


def run_agent(agent, contract_text, session):
    """Run a single agent against the contract via Claude API."""
    client = anthropic.Anthropic(api_key=API_KEY)

    session.send("agent_start", {
        "agent": agent["id"],
        "name": agent["name"],
        "color": agent["color"],
        "weight": agent["weight"],
    })

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            messages=[{
                "role": "user",
                "content": f"{AMI_CONTEXT}\n\n{agent['prompt']}\n\n---\n\nDOCUMENT TO REVIEW (for Acts of Mercy International):\n\n{contract_text}"
            }],
        )
        record_usage(response)

        # Parse the response — each line should be a JSON finding
        # First TEXT block, not content[0]: thinking-enabled models lead with a `thinking` block whose .text is None.
        text = "".join(getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text").strip()
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                finding = json.loads(line)
                finding["agent"] = agent["id"]
                finding["agentName"] = agent["name"]
                finding["agentColor"] = agent["color"]
                session.send("finding", finding)
            except json.JSONDecodeError:
                # Non-JSON line — send as info
                if len(line) > 10:
                    session.send("finding", {
                        "agent": agent["id"],
                        "agentName": agent["name"],
                        "agentColor": agent["color"],
                        "type": "info",
                        "badge": "Note",
                        "summary": line[:200],
                    })

    except Exception as e:
        session.send("finding", {
            "agent": agent["id"],
            "agentName": agent["name"],
            "agentColor": agent["color"],
            "type": "high",
            "badge": "Error",
            "summary": f"Agent error: {str(e)[:150]}",
        })

    session.send("agent_complete", {
        "agent": agent["id"],
        "name": agent["name"],
    })


def run_scoring(contract_text, all_findings_text, session):
    """Run the scoring agent after all others complete."""
    client = anthropic.Anthropic(api_key=API_KEY)
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": f"{AMI_CONTEXT}\n\n{SCORE_PROMPT}\n\nAGENT FINDINGS SUMMARY:\n{all_findings_text[:3000]}\n\nDOCUMENT:\n{contract_text[:2000]}"
            }],
        )
        record_usage(response)
        text = "".join(getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text").strip()
        # Try to parse JSON from the response
        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("{"):
                try:
                    score_data = json.loads(line)
                    session.send("score", score_data)
                    return
                except json.JSONDecodeError:
                    pass
        # Fallback
        session.send("score", {"score": 50, "grade": "D", "label": "RISKY", "summary": "Could not parse score."})
    except Exception as e:
        session.send("score", {"score": 50, "grade": "D", "label": "RISKY", "summary": f"Scoring error: {e}"})


def run_analysis(contract_text, session):
    """Orchestrate all 5 agents in parallel, then score."""
    session.send("system", {"message": "Contract received — launching 5 parallel agents..."})

    threads = []
    for agent in AGENTS:
        t = threading.Thread(target=run_agent, args=(agent, contract_text, session), daemon=True)
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    session.send("system", {"message": "All 5 agents complete — calculating score..."})

    lines = []
    for f in session.findings:
        name = f.get("agentName") or f.get("agent") or ""
        summary = (f.get("summary") or "").replace("\n", " ")
        lines.append(f"[{name}] {f.get('type', '')} {f.get('badge', '')}: {summary}")
    run_scoring(contract_text, "\n".join(lines), session)

    session.finish()


# ── PDF Data Builder ──
def build_pdf_from_findings(data):
    """Transform frontend event data into the format expected by the PDF generator."""
    findings = data.get("findings", [])
    score_data = data.get("score", {})
    contract_info = data.get("contract", {})

    # Categorize findings by risk
    high_clauses = []
    med_clauses = []
    low_clauses = []
    all_clauses = []
    priorities = []
    missing = []

    for f in findings:
        ftype = f.get("type", "info")
        agent = f.get("agent", "")
        badge = f.get("badge", "")
        summary = f.get("summary", "")
        detail = f.get("detail", {})

        if badge in ("Agent", "Init", "Complete", "Note") or ftype == "system":
            continue

        clause_entry = {
            "name": detail.get("title", badge),
            "section": badge,
            "summary": summary,
            "risk_explanation": detail.get("text", ""),
            "recommendation": detail.get("fix", ""),
        }

        if ftype in ("high", "critical"):
            clause_entry["risk"] = "high"
            high_clauses.append(summary[:60])
            all_clauses.append(clause_entry)
        elif ftype == "medium":
            clause_entry["risk"] = "medium"
            med_clauses.append(summary[:60])
            all_clauses.append(clause_entry)
        elif ftype == "low":
            clause_entry["risk"] = "low"
            low_clauses.append(summary[:60])
            all_clauses.append(clause_entry)
        else:
            clause_entry["risk"] = "low"
            all_clauses.append(clause_entry)

        # Collect recommendations
        if agent == "recommendation" and detail.get("fix"):
            priorities.append(f"{badge}: {summary}")

        # Collect missing protections
        if agent == "terms" and "missing" in badge.lower():
            missing.append(summary)

    score = score_data.get("score", 50)
    grade = score_data.get("grade", "D")
    label = score_data.get("label", "RISKY")
    exec_summary = score_data.get("summary", "Analysis complete. Review findings below.")

    return {
        "score": score,
        "grade": grade,
        "grade_label": label,
        "executive_summary": exec_summary,
        "details": {
            "type": contract_info.get("type", "Contract"),
            "parties": contract_info.get("parties", "See document"),
            "effective_date": contract_info.get("effectiveDate", "See document"),
            "term": contract_info.get("term", "See document"),
            "total_value": contract_info.get("value", "See document"),
            "governing_law": contract_info.get("governingLaw", "See document"),
        },
        "risks": {
            "high": len(high_clauses),
            "medium": len(med_clauses),
            "low": len(low_clauses),
            "high_clauses": "; ".join(high_clauses[:5]) or "None",
            "medium_clauses": "; ".join(med_clauses[:5]) or "None",
            "low_clauses": "; ".join(low_clauses[:5]) or "None",
        },
        "clauses": all_clauses[:30],  # Limit to 30 for PDF size
        "negotiation_priorities": priorities[:10] or [
            "Review all HIGH RISK clauses with a licensed attorney",
            "Negotiate terms flagged as one-sided",
            "Ensure all missing protections are added",
            "Consult an attorney before signing",
        ],
        "missing_protections": missing or [
            "See clause analysis above for details",
        ],
        "next_steps": [
            "Do not sign this contract without addressing the issues identified above",
            "Send the counterparty a redline with the recommended changes",
            "Consult a licensed attorney in your jurisdiction for final review",
            "If the counterparty refuses to negotiate critical items, consider declining the engagement",
            "Obtain professional liability insurance if you proceed",
        ],
    }


# ── HTTP Handler ──
class LegalHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FRONTEND_DIR), **kwargs)

    def _check_auth(self):
        """Shared-password gate. Returns True if allowed; sends 401 and returns False otherwise."""
        if not LEGAL_PASSWORD:
            return True  # gate disabled when no password configured
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(hdr[6:]).decode("utf-8").partition(":")
                if pw == LEGAL_PASSWORD and (not LEGAL_USER or user == LEGAL_USER):
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="AI Legal Assistant"')
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Authentication required")
        return False

    def do_GET(self):
        if not self._check_auth():
            return
        _p = urlparse(self.path).path
        if _p == "/api/disasters":
            return self._serve_disasters()
        if _p == "/api/conflict-forecast":
            return self._serve_conflict_forecast()
        super().do_GET()

    def _serve_disasters(self):
        """Live multi-hazard board (NHC + GDACS). Read-only, cached, no AI spend."""
        force = "refresh=1" in (urlparse(self.path).query or "")
        try:
            if not HAS_DISASTERS:
                payload, code = json.dumps({"error": "Disaster module not loaded."}).encode(), 500
            else:
                payload = json.dumps(disaster_feed.get_disaster_data(force=force)).encode()
                code = 200
        except Exception as e:
            payload = json.dumps({"error": f"Disaster fetch failed: {str(e)[:200]}"}).encode()
            code = 502
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def _serve_conflict_forecast(self):
        """VIEWS conflict-fatality forecast for AMI regions. Read-only, cached 24h, no AI spend."""
        force = "refresh=1" in (urlparse(self.path).query or "")
        try:
            if not HAS_PREDICTIVE:
                payload, code = json.dumps({"error": "Conflict forecast unavailable."}).encode(), 500
            else:
                payload = json.dumps(conflict_feed.get_conflict_forecast(force=force)).encode()
                code = 200
        except Exception as e:
            payload = json.dumps({"error": f"Conflict forecast failed: {str(e)[:200]}"}).encode()
            code = 502
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        if not self._check_auth():
            return
        parsed = urlparse(self.path)

        if parsed.path == "/api/analyze":
            # Read the posted contract text
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")

            try:
                data = json.loads(body)
                contract_text = data.get("text", "")
            except json.JSONDecodeError:
                contract_text = body

            if not contract_text.strip():
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "No contract text provided"}).encode())
                return

            if not API_KEY:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "ANTHROPIC_API_KEY not set. Export it before starting the server."}).encode())
                return

            # Start SSE stream
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            session = AnalysisSession()

            if over_budget():
                # Hard spend cap reached — refuse new analysis, surface it visibly.
                session.send("system", {
                    "message": f"Spend cap of ${CASH_CAP_USD:.0f} reached — analysis paused to protect the API budget."
                })
                session.send("finding", {
                    "agent": "system",
                    "agentName": "Budget Guard",
                    "agentColor": "#ef4444",
                    "type": "high",
                    "badge": "🛑 Budget Cap",
                    "summary": f"This tool has hit its ${CASH_CAP_USD:.0f} AI-spend limit and is temporarily disabled.",
                    "detail": {
                        "title": "Spending cap reached",
                        "risk": "HIGH",
                        "text": f"To prevent runaway cost, this app stops running new analyses once it has spent ${CASH_CAP_USD:.0f} on AI. Current spend: ${spend_so_far():.2f}.",
                        "fix": "Owner: raise CASH_CAP_USD in .env (or reset spend_ledger.json) and restart the server.",
                    },
                })
                session.finish()
            else:
                # Run analysis in background thread
                analysis_thread = threading.Thread(
                    target=run_analysis, args=(contract_text, session), daemon=True
                )
                analysis_thread.start()

            # Stream events to client
            try:
                while not session.done.is_set() or not session.queue.empty():
                    try:
                        event = session.queue.get(timeout=0.5)
                        self.wfile.write(event.encode())
                        self.wfile.flush()
                    except queue.Empty:
                        # Send keepalive
                        self.wfile.write(": keepalive\n\n".encode())
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

            return

        if parsed.path == "/api/generate-pdf":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")

            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Invalid JSON"}).encode())
                return

            if not HAS_PDF:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "reportlab not installed. Run: pip3 install reportlab"}).encode())
                return

            try:
                pdf_data = build_pdf_from_findings(data)
                tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
                _build_pdf_original(pdf_data, tmp.name)

                with open(tmp.name, "rb") as f:
                    pdf_bytes = f.read()

                os.unlink(tmp.name)

                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Disposition", "attachment; filename=Contract-Review-Report.pdf")
                self.send_header("Content-Length", str(len(pdf_bytes)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(pdf_bytes)
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": f"PDF generation failed: {str(e)}"}).encode())
            return

        if parsed.path == "/api/draft":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                description = json.loads(body).get("text", "").strip()
            except json.JSONDecodeError:
                description = body.strip()

            def _json(code, obj):
                payload = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(payload)

            if not description:
                _json(400, {"error": "Please describe the activity, event, or trip."})
                return
            if not API_KEY:
                _json(500, {"error": "ANTHROPIC_API_KEY not set."})
                return
            if over_budget():
                _json(200, {"markdown": f"# 🛑 Spend cap reached\n\nThis tool has hit its ${CASH_CAP_USD:.0f} AI-spend limit and is temporarily disabled (current spend: ${spend_so_far():.2f}). Ask the owner to raise the cap and try again."})
                return
            try:
                client = anthropic.Anthropic(api_key=API_KEY)
                response = client.messages.create(
                    model=MODEL,
                    max_tokens=4096,
                    messages=[{
                        "role": "user",
                        "content": f"{DRAFT_PROMPT}\n\n---\n\nACTIVITY / SITUATION DESCRIBED BY AMI STAFF:\n\n{description}"
                    }],
                )
                record_usage(response)
                # First TEXT block, not content[0]: on thinking-enabled models content[0]
                # is a `thinking` block with text=None, and this endpoint returned a 500.
                _json(200, {"markdown": "".join(getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text")})
            except Exception as e:
                _json(500, {"error": f"Draft failed: {str(e)[:200]}"})
            return

        if parsed.path == "/api/grant":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                payload = json.loads(body)
                description = payload.get("text", "").strip()
                mode = payload.get("mode", "write")
            except json.JSONDecodeError:
                description, mode = body.strip(), "write"

            def _json(code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)

            prompt = GRANT_PROMPTS.get(mode, GRANT_PROMPTS["write"])
            if not description:
                _json(400, {"error": "Please describe your program, draft, or mission first."})
                return
            if not API_KEY:
                _json(500, {"error": "ANTHROPIC_API_KEY not set."})
                return
            if over_budget():
                _json(200, {"markdown": f"# 🛑 Spend cap reached\n\nThis tool has hit its ${CASH_CAP_USD:.0f} AI-spend limit (current spend: ${spend_so_far():.2f}). Ask the owner to raise the cap and try again."})
                return
            try:
                client = anthropic.Anthropic(api_key=API_KEY)
                messages = [{"role": "user", "content": f"{prompt}\n\n---\n\nFROM AMI STAFF:\n\n{description[:12000]}"}]
                if mode == "find":
                    # LIVE web search — via the shared helper that survives the
                    # pause_turn server-tool resubmit bug.
                    text_out = run_web_search(client, messages, max_uses=4, max_rounds=3)
                else:
                    response = client.messages.create(model=MODEL, max_tokens=4096, messages=messages)
                    record_usage(response)
                    text_out = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
                _json(200, {"markdown": text_out or "_No results came back — try adding more detail (location, programs) and search again._"})
            except Exception as e:
                _json(500, {"error": f"Grant helper failed: {str(e)[:200]}"})
            return

        if parsed.path == "/api/conflict-watch":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8") if content_length else ""
            try:
                extra = json.loads(body).get("text", "").strip() if body else ""
            except json.JSONDecodeError:
                extra = ""

            def _json(code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)

            if not API_KEY:
                _json(500, {"error": "ANTHROPIC_API_KEY not set."})
                return
            if over_budget():
                _json(200, {"markdown": f"# 🛑 Spend cap reached\n\nThis tool has hit its ${CASH_CAP_USD:.0f} AI-spend limit (current spend: ${spend_so_far():.2f}). Ask the owner to raise the cap and try again."})
                return

            extra = extra[:2000]  # cap free-text input (cost + injection surface)

            # Light cooldown so a double-click / refresh can't fire ~$2 calls back-to-back.
            wait = WEB_SEARCH_COOLDOWN - (time.time() - _LAST_WEB_SEARCH[0])
            if wait > 0:
                _json(200, {"markdown": f"_The watch just ran — please wait about {int(wait)}s and try again._"})
                return
            # Single-flight: never run two ~$2 web-search calls at once.
            if not _WEB_SEARCH_LOCK.acquire(blocking=False):
                _json(200, {"markdown": "_A live search is already running — give it a moment and try again._"})
                return
            try:
                _LAST_WEB_SEARCH[0] = time.time()
                regions = ", ".join(sorted(disaster_feed.AMI_COUNTRIES)) if HAS_DISASTERS else "worldwide humanitarian hotspots"
                staff_note = f"Focus / notes from AMI staff: {extra}\n\n" if extra else ""
                user = f"AMI regions of concern: {regions}.\n\n{staff_note}"
                client = anthropic.Anthropic(api_key=API_KEY)
                brief = conflict_feed.forecast_brief() if HAS_PREDICTIVE else ""
                grounding = f"\n\nGROUND YOUR ASSESSMENT IN THIS FORECAST DATA (cite the numbers):\n{brief}\n" if brief else ""
                messages = [{"role": "user", "content": f"{CONFLICT_PROMPT}\n\n---\n\n{user}{grounding}"}]
                # Grounded in VIEWS forecast → needs only a couple of context searches → fast.
                text_out = run_web_search(client, messages, max_uses=3, max_rounds=2)
                _json(200, {"markdown": text_out or "_No conflict reporting came back — try again in a moment._"})
            except Exception as e:
                _json(500, {"error": f"Conflict watch failed: {str(e)[:200]}"})
            finally:
                _WEB_SEARCH_LOCK.release()
            return

        self.send_response(404)
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        # Quieter logging
        if "/api/" in (args[0] if args else ""):
            super().log_message(format, *args)


def main():
    if not API_KEY:
        print("\n\033[1;33m" + "=" * 60)
        print("  WARNING: ANTHROPIC_API_KEY is not set!")
        print("=" * 60 + "\033[0m")
        print("\n  The server will start but AI analysis won't work.")
        print("  Demo mode will still function.\n")
        print("  To enable real analysis, set your API key:")
        print("  \033[36mexport ANTHROPIC_API_KEY='sk-ant-...'\033[0m\n")
    else:
        print(f"\n  \033[32m✓\033[0m Anthropic API key configured")
        print(f"  \033[32m✓\033[0m Model: {MODEL}")

    print(f"\n  \033[32m✓\033[0m Serving frontend from: {FRONTEND_DIR}")
    print(f"\n  \033[1;36m→ Open http://localhost:{PORT}\033[0m\n")

    server = ThreadingHTTPServer(("", PORT), LegalHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
