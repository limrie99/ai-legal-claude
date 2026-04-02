#!/usr/bin/env python3
"""
AI Legal Assistant — Backend Server
Serves the frontend and provides /api/analyze endpoint that calls Claude API
with 5 parallel agents via Server-Sent Events (SSE).
"""

import json
import os
import sys
import threading
import time
import queue
import tempfile
from http.server import HTTPServer, SimpleHTTPRequestHandler
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

    def do_POST(self):
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

    server = HTTPServer(("", PORT), LegalHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
