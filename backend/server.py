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


def _read_spend():
    try:
        return json.loads(SPEND_LEDGER.read_text())
    except Exception:
        return {"spent_usd": 0.0}


def record_usage(response):
    """Add one Claude API call's cost to the persistent ledger."""
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
        except Exception:
            pass


def spend_so_far():
    with _spend_lock:
        return _read_spend().get("spent_usd", 0.0)


def over_budget():
    return spend_so_far() >= CASH_CAP_USD


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

The user describes a program/project (and maybe the funder). Produce a Markdown GRANT PROPOSAL DRAFT
with these sections, using [BRACKETED PLACEHOLDERS] for any facts/figures AMI must supply:
## Organization Summary  (AMI background — [PLACEHOLDERS])
## Statement of Need
## Project Description — Goals & Objectives
## Activities & Timeline
## Outcomes & How We'll Measure Them
## Budget Outline  (a Markdown table with line items and [AMOUNT] placeholders + total)
## Sustainability & Future Funding
## Closing
If a funder is named, tailor the language to that funder's likely priorities. Plain, specific, fundable.""",

    "check": _GRANT_INTRO + """

The user pastes a grant application / proposal draft. Review it like a helpful grant reviewer and output Markdown:
## Quick read  (1-2 sentences + an informal readiness sense, e.g. "needs work / close / strong")
## What's working
## Gaps & weaknesses  (flag missing or weak: statement of need, measurable outcomes, budget justification,
   alignment to the funder's priorities, clarity, evaluation plan, sustainability)
## Specific suggested improvements  (concrete, actionable — quote/rewrite weak lines where useful)
Be candid and useful, not flattering.""",

    "find": _GRANT_INTRO + """

The user describes AMI's mission, programs, and location. Suggest the TYPES of funders and grants a
faith-based humanitarian nonprofit like AMI could realistically pursue. Output Markdown:
## Where to look  (categories: community foundations, faith-based / denominational grants, family &
   private foundations, corporate giving / matching, and any relevant government categories — with a
   few well-known examples to RESEARCH, not apply to blindly)
## Likely fit for AMI  (which categories match the described work and why)
## How to search  (practical: Candid / Foundation Directory, Grants.gov, local community-foundation
   directories, denominational offices, peer nonprofits' 990s)
## Before you apply  (eligibility, 501(c)(3) status, deadlines)
CRITICAL: Do NOT invent specific open grants, dollar amounts, or deadlines — those change constantly and
you cannot see them. Name only well-known funders/databases to research, and tell the user to verify
current availability, eligibility, and deadlines themselves. State this clearly.""",
}

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

    def send(self, event_type, data):
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
                "content": f"{agent['prompt']}\n\n---\n\nCONTRACT TEXT:\n\n{contract_text}"
            }],
        )
        record_usage(response)

        # Parse the response — each line should be a JSON finding
        text = response.content[0].text.strip()
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
                "content": f"{SCORE_PROMPT}\n\nAGENT FINDINGS SUMMARY:\n{all_findings_text[:3000]}\n\nCONTRACT:\n{contract_text[:2000]}"
            }],
        )
        record_usage(response)
        text = response.content[0].text.strip()
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
    all_findings = []

    def agent_wrapper(agent):
        run_agent(agent, contract_text, session)

    for agent in AGENTS:
        t = threading.Thread(target=agent_wrapper, args=(agent,), daemon=True)
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    session.send("system", {"message": "All 5 agents complete — calculating score..."})

    # Collect findings for scoring (from queue is tricky, so just re-summarize)
    run_scoring(contract_text, "Analysis of contract complete with 5 agents.", session)

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
        super().do_GET()

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
                _json(200, {"markdown": response.content[0].text})
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
                response = client.messages.create(
                    model=MODEL,
                    max_tokens=4096,
                    messages=[{"role": "user", "content": f"{prompt}\n\n---\n\nFROM AMI STAFF:\n\n{description}"}],
                )
                record_usage(response)
                _json(200, {"markdown": response.content[0].text})
            except Exception as e:
                _json(500, {"error": f"Grant helper failed: {str(e)[:200]}"})
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
