// Contract metadata. Score fields are filled in when a live review returns a score.
export const contract = {
  name: "Independent Contractor Services Agreement",
  parties: "Nexus Digital Solutions LLC \u2194 Contractor",
  effectiveDate: "March 1, 2026",
  term: "12 months (auto-renews)",
  value: "$15,000",
  governingLaw: "British Virgin Islands",
  type: "Freelancer / Independent Contractor",
  score: 18,
  grade: "F",
  label: "DANGEROUS",
};

// Sidebar roster for the live review. Findings come from the API, not from here.
export const agents = [
  {
    id: "clause",
    name: "Clause Analyst",
    color: "#22c55e",
  },
  {
    id: "risk",
    name: "Risk Assessor",
    color: "#ef4444",
  },
  {
    id: "compliance",
    name: "Compliance Checker",
    color: "#a855f7",
  },
  {
    id: "terms",
    name: "Terms Mapper",
    color: "#06b6d4",
  },
  {
    id: "recommendation",
    name: "Recommendations",
    color: "#f97316",
  },
];
