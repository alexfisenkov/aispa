"""Read-only, source-preserving diagnostics. Grouping is a proposal, not a plan."""
from collections import defaultdict
import difflib
import hashlib
import re

from . import scale
from .store import confined, digest


def _normalize(text):
    return " ".join(re.findall(r"\w+", text.casefold()))


def analyze(store, state):
    budget = scale.Budget()
    sources = []
    for ref in state.get("briefs", []):
        raw = scale.read_file(confined(store.run, ref["file"]), allow_manifest=scale.enabled(state),
                              expected_sha256=ref["sha256"], budget=budget)
        sources.append({**ref, "payloadSha256": hashlib.sha256(raw).hexdigest()})
    rows = state.get("requirements", [])
    groups, constraints = defaultdict(list), defaultdict(list)
    criteria, provenance, exact = [], [], []
    for row in rows:
        budget.check()
        key = _normalize(row.get("text", ""))
        groups[key].append(row)
        if row.get("kind") == "constraint":
            constraints[key].append(row)
        provenance.append({"requirement": row["id"], "brief": row.get("brief"), "quote": row.get("quote"), "scope": row.get("scope")})
        criteria.extend({"requirement": row["id"], "index": index, "text": text}
                        for index, text in enumerate(row.get("acceptance", [])))
    def group(values):
        return {"requirements": [r["id"] for r in values], "quotes": [r.get("quote") for r in values],
                "reason": "Identical normalized requirement text; every original identity is retained"}
    exact = [group(values) for key, values in groups.items() if key and len(values) > 1]
    repeated = [group(values) for key, values in constraints.items() if key and len(values) > 1]
    similar = []
    names = list(groups)
    for index, first in enumerate(names):
        for second in names[index + 1:]:
            budget.check()
            if first and second and first != second and difflib.SequenceMatcher(None, first, second).ratio() >= .85:
                similar.append({"requirements": [r["id"] for r in groups[first] + groups[second]],
                                "reason": "Text similarity candidate; semantic equivalence is unconfirmed"})
    fragmented = [{**value, "reason": "Heuristic: at least five identities repeat one stated result; consider one coherent task, retaining every condition",
                   "rule": "repeated-result-identities>=5"} for value in exact if len(value["requirements"]) >= 5]
    proposal = {"applied": False, "requirements": [r["id"] for r in rows], "criteria": criteria,
                "provenance": provenance, "risks": [{"task": t["id"], "risk": t.get("risk"), "reason": t.get("riskReason")}
                    for t in state.get("tasks", [])],
                "dependencies": [{"task": t["id"], "dependsOn": t.get("dependsOn", [])} for t in state.get("tasks", [])],
                "acceptanceRule": "Existing planning validator must preserve all criteria, risks and dependencies"}
    return {"basis": digest({"sources": sources, "requirements": rows}), "sources": sources,
            "exactDuplicates": exact, "repeatedConstraints": repeated, "similarCandidates": similar,
            "fragmentation": fragmented, "groupingProposal": proposal,
            "authority": "diagnostic only; no deletion, renumbering, scope or check-mode change"}
