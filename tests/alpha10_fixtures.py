"""Explicit synthetic reports for the alpha.10 full CLI contract test.

These reports exercise structural guards. They are not native model judgments.
"""
from aispa_core import design_contract, integration
from aispa_core.store import Journal, file_hash


def source_report(context, actor):
    return {"basis": context["basis"], "actor": actor,
            "units": [{**unit, "kind": "intent",
                       "summary": ("The owner authorizes README.md as a concrete local reference."
                                   if "README.md" in unit["text"] else
                                   "The owner requests observable ready output."),
                       "reason": ""}
                      for brief in context["briefs"] for unit in brief["units"]]}


def source_map_report(context, actor, requirement="R01"):
    return {"basis": context["basis"], "actor": actor,
            "rows": [{"brief": unit["brief"], "start": unit["start"], "end": unit["end"],
                      "disposition": "requirement", "requirements": [requirement],
                      "reason": "This source unit maps to the stated output requirement."}
                     for brief in context["briefs"] for unit in brief["units"]]}


def scenarios(state, *, source_quote, spec_quote):
    explanations = {
        "first": "Run the command once and observe the first output.",
        "main": "Run the normal command and compare its output to ready.",
        "empty": "The command accepts no input; there is no empty-input branch.",
        "error": "The fixture has no user input or external service that can fail.",
        "retry": "The command has no stored state; rerunning repeats the output.",
        "access": "This local fixture has no authentication or permissions model.",
        "persistence": "The program stores no data between invocations.",
        "effects": "The only external effect is one line on standard output.",
    }
    covered = {"first", "main", "retry", "effects"}
    return {"basis": design_contract.scenario_basis(state), "depth": state["depth"],
            "depthReason": "A single print statement warrants balanced review of every dimension.",
            "features": [{"requirement": "R01", "changeKind": "requested",
                          "sourceQuote": source_quote, "specQuote": spec_quote,
                          "dimensions": [{"name": name,
                                          "status": "covered" if name in covered else "not_applicable",
                                          "scenario": explanations[name],
                                          "verification": "Run app.py and inspect stdout; review the source for absent branches.",
                                          "counterexample": ""}
                                         for name in design_contract.DIMENSIONS]}]}


def interfaces(state):
    return {"basis": design_contract.interface_basis(state), "interfaces": [],
            "noInterfaceReason": "The one-statement command has no cross-component interface or consumer."}


def existing_behavior_intent(task_context, root):
    return {"mode": "not_applicable", "attempt": task_context["attempt"],
            "basis": task_context["basis"], "criteria": ["Выводит ready"],
            "notApplicable": [{"criterion": "Выводит ready", "purpose": "existing_behavior",
                               "source": {"path": "app.py", "sha256": file_hash(root / "app.py")},
                               "reason": "The pre-existing app.py already prints ready; this integration fixture checks that behavior without changing it."}]}


def branch_review(root, task_id, evidence):
    store = Journal(root)
    state = store.load()
    context = integration.context(store, state, task_id)
    return {"basis": context["basis"], "code": context["code"],
            "files": context["files"], "changes": context["changes"],
            "axes": [{"axis": axis, "status": "passed",
                      "note": "Synthetic fixture reviewed the full branch and its recorded command evidence for this axis.",
                      "evidence": evidence} for axis in integration.AXES],
            "note": "Synthetic structural branch review; this does not establish semantic model quality.",
            "evidence": evidence}
