"""Separate immutable historical execution remainders for run handoffs.

No product mapping, current scope, PASS inheritance, or external-event receipt
is inferred here. The enclosing carryover ledger owns storage and journal guards.
"""
import copy
import json
import re

from .contracts import nonempty, require
from .portable import relative_name
from .store import Invalid, digest


def _execution_source(sources, source_id, kind, origin_run):
    require(isinstance(source_id, str) and source_id in sources,
            "Carryover: потерян источник обязательства исполнения")
    source = sources[source_id]
    origin = source["origin"]
    require(source.get("kind") == kind and origin.get("run") == origin_run,
            "Carryover: чужой источник обязательства исполнения")
    try:
        require(relative_name(origin["file"]) == origin["file"]
                and relative_name(origin_run) == origin_run and "/" not in origin_run,
                "Carryover: небезопасное происхождение обязательства исполнения")
        value = json.loads(source["text"])
    except (ValueError, TypeError, KeyError) as exc:
        raise Invalid("Carryover: повреждён источник обязательства исполнения") from exc
    require(isinstance(value, dict), "Carryover: источник исполнения должен быть объектом")
    return value, origin



def disposition(row, sources=None):
    """Owner disposition is separate from the unchanged pending/failed outcome."""
    from .execution_obligations import instant
    history = row.get("dispositions", [])
    require(isinstance(history, list), "Carryover: повреждена история решений об исполнении")
    current, prior, source_hashes = "outstanding", {}, set()
    for event in history:
        require(isinstance(event, dict) and isinstance(event.get("action"), str)
                and event["action"] in {"keep", "drop", "defer"} and current != "dropped",
                "Carryover: недопустимое решение об исполнении")
        fields = {"run", "action", "at", "binding"}
        if event["action"] in {"drop", "defer"}:
            fields |= {"brief", "quote", "source"}
        require(set(event) == fields and nonempty(event.get("run"))
                and event["run"] != row["origin"]["run"] and event.get("binding") == row["origin"],
                "Carryover: решение потеряло исходный E/record/run")
        instant(event.get("at"))
        if event["action"] in {"drop", "defer"}:
            require(isinstance(event["brief"], str) and re.fullmatch(r"B\d{3,}", event["brief"])
                    and nonempty(event["quote"]) and isinstance(event["source"], str),
                    "Carryover: решению нужен новый бриф и точная цитата")
            index = int(event["brief"][1:])
            require(index > prior.get(event["run"], 0), "Carryover: решению об исполнении нужен более новый бриф")
            prior[event["run"]] = index
            if sources is not None:
                source = sources.get(event["source"])
                require(source and source.get("kind") == "owner_brief"
                        and source["origin"].get("run") == event["run"] and event["quote"] in source["text"],
                        "Carryover: потеряна цитата решения владельца об исполнении")
                original = sources.get(row["source"]["source"])
                require(original and source["origin"]["sha256"] != original["origin"]["sha256"],
                        "Carryover: исходный бриф не разрешает снять остаток исполнения")
                require(source["origin"]["sha256"] not in source_hashes,
                        "Carryover: повторные байты брифа не являются новым решением владельца")
                source_hashes.add(source["origin"]["sha256"])
        current = {"drop": "dropped", "defer": "deferred", "keep": current}[event["action"]]
    return current


def apply_disposition(before, receipt, run, sources):
    after = receipt.get("after")
    require(isinstance(after, dict) and
            {k: v for k, v in after.items() if k != "dispositions"} ==
            {k: v for k, v in before.items() if k != "dispositions"},
            "Carryover: решение не вправе переписывать исходный результат исполнения")
    old, new = before.get("dispositions", []), after.get("dispositions")
    require(isinstance(new, list) and len(new) == len(old) + 1 and new[:-1] == old
            and isinstance(new[-1], dict) and new[-1].get("run") == run
            and new[-1].get("action") == receipt.get("action"),
            "Carryover: нарушена цепочка решений об исполнении")
    # Freshness is per obligation: one owner brief may explicitly address
    # several different CE items. validate checks this item's original source
    # and every earlier disposition source, including recomputed new B-IDs.
    validate([after], sources)
    return copy.deepcopy(after)

def validate(rows, sources):
    """Historical execution remains immutable and never enters product scope."""
    from . import execution_obligations
    require(isinstance(rows, list), "Carryover: executionRemainders должен быть списком")
    seen = set()
    for row in rows:
        fields = {"id", "origin", "source", "obligationSource", "startSource", "sealSource",
                  "resultSource", "resultIndex", "result", "status", "historical", "currentProof"}
        require(isinstance(row, dict) and set(row) in (fields, fields | {"dispositions"}),
            "Carryover: неполный остаток исполнения")
        origin = row["origin"]
        require(isinstance(origin, dict) and set(origin) == {"run", "obligation", "recordSha256"}
                and isinstance(origin.get("run"), str) and isinstance(origin.get("obligation"), str)
                and row["id"] == "CE-" + digest(origin)[:24] and row["id"] not in seen
                and isinstance(row["status"], str) and row["status"] in {"pending", "failed"}
                and row["historical"] is True and row["currentProof"] is False,
                "Carryover: остаток исполнения не является отдельным историческим обязательством")
        seen.add(row["id"])
        obligation, record_origin = _execution_source(sources, row["obligationSource"], "execution_obligation", origin["run"])
        require(record_origin["sha256"] == origin["recordSha256"]
                and obligation.get("id") == origin["obligation"] and obligation.get("run") == origin["run"]
                and obligation.get("kind") == "deadline" and isinstance(obligation.get("event"), str)
                and obligation["event"] in execution_obligations.EVENTS,
                "Carryover: исходное обязательство исполнения изменено")
        execution_obligations.literal_timing(obligation.get("quote"), obligation.get("event"), obligation.get("deadline"),
                                             started_at=obligation.get("startedAt"))
        require(execution_obligations.instant(obligation.get("startedAt")) <=
                execution_obligations.instant(obligation.get("deadline")),
                "Carryover: срок обязательства раньше начала")
        source = row["source"]
        require(isinstance(source, dict) and set(source) == {"source", "quote"}
                and isinstance(source["source"], str) and source["source"] in sources and sources[source["source"]].get("kind") == "owner_brief"
                and sources[source["source"]]["origin"].get("run") == origin["run"]
                and nonempty(source["quote"]) and source["quote"] == obligation.get("quote")
                and source["quote"] in sources[source["source"]]["text"],
                "Carryover: обязательство потеряло точную цитату владельца")
        disposition(row, sources)
        start, start_origin = _execution_source(sources, row["startSource"], "execution_start", origin["run"])
        require(obligation.get("startEvidence") == {k: start_origin[k] for k in ("file", "sha256")}
                and start.get("event") == "execution-start" and start.get("at") == obligation["startedAt"]
                and nonempty(start.get("note")), "Carryover: потеряно свидетельство начала исполнения")
        if row["sealSource"] is not None:
            seal, _ = _execution_source(sources, row["sealSource"], "execution_seal", origin["run"])
            expected = {**obligation, "record": {k: record_origin[k] for k in ("file", "sha256")}}
            require(seal.get("run") == origin["run"] and isinstance(seal.get("obligations"), list)
                    and expected in seal["obligations"],
                    "Carryover: обязательство не входило в исходную фиксацию")
            refs = seal.get("sourceReports")
            require(isinstance(refs, dict) and set(refs) == {"extraction", "review"},
                    "Carryover: нет независимых исходных интерпретаций срока")
            reports = []
            for key in ("extraction", "review"):
                ref = refs[key]
                require(isinstance(ref, dict) and all(isinstance(ref.get(k), str) for k in ("file", "sha256")),
                        "Carryover: повреждена ссылка исходного разбора срока")
                source_id = digest({"run": origin["run"], **{k: ref[k] for k in ("file", "sha256")}})
                saved, _ = _execution_source(sources, source_id, "execution_source_" + key, origin["run"])
                require(saved.get("run") == origin["run"] and saved.get("schema") == 1,
                        "Carryover: чужой исходный разбор срока")
                reports.append(saved.get("report"))
            execution_obligations.report_timing(obligation, *reports)
        if row["resultSource"] is None:
            require(row["resultIndex"] is None and row["result"] is None and row["status"] == "pending",
                    "Carryover: без события finish результат исполнения остаётся неизвестным")
            continue
        receipt, _ = _execution_source(sources, row["resultSource"], "execution_result", origin["run"])
        index, result = row["resultIndex"], row["result"]
        require(row["sealSource"] is not None and receipt.get("run") == origin["run"]
                and receipt.get("event") == "engine-finish" and isinstance(receipt.get("results"), list)
                and type(index) is int and 0 <= index < len(receipt["results"])
                and result == receipt["results"][index] and isinstance(result, dict)
                and result.get("id") == obligation["id"] and result.get("origin") == origin["recordSha256"]
                and result.get("event") == obligation["event"] and result.get("deadline") == obligation["deadline"]
                and result.get("status") == row["status"], "Carryover: потеряно происхождение результата исполнения")
        at = execution_obligations.instant(receipt.get("at"))
        expired = at > execution_obligations.instant(obligation["deadline"])
        require(result.get("deadlineElapsed") is expired and nonempty(result.get("note")),
                "Carryover: неверная проверка срока исполнения")
        if obligation["event"] == "engine-finish":
            require(expired and row["status"] == "failed" and result.get("at") == receipt["at"],
                    "Carryover: исторический PASS нельзя переносить как текущий остаток")
        else:
            require(row["status"] == "pending" and result.get("at") is None,
                    "Carryover: host final handoff не подтверждён событием engine finish")
    return rows


def capture(store, state, inherited, sources, briefs):
    from .carryover import _load, _source, _merge_sources
    from . import execution_obligations
    result = copy.deepcopy(inherited.get("executionRemainders", []))
    if not execution_obligations.enabled(state):
        return result
    rows = execution_obligations.obligations(store, state, sealed=bool(state.get("executionSeal")))
    history = state.get("executionResultHistory", [])
    require(isinstance(history, list), "Carryover: повреждена история finish")
    last, last_source = None, None
    for ref in history:
        receipt = _load(store, ref)
        require(receipt.get("run") == state["run"] and receipt.get("event") == "engine-finish",
                "Carryover: чужое событие finish")
        expected = execution_obligations.evaluate(store, state, receipt.get("at"))
        require(receipt.get("results") == expected, "Carryover: finish не соответствует исходным обязательствам")
        last, last_source = receipt, ref
    require(not state.get("executionResults") if last is None else state.get("executionResults") == last["results"],
            "Carryover: результат исполнения не связан с сохранённым событием")
    evaluated = {r["id"]: (index, r) for index, r in enumerate(last["results"])} if last else {}
    def source(ref, kind):
        value = _source(store, state, ref, kind)
        _merge_sources(sources, [value])
        return value["id"]
    if rows and state.get("executionSeal"):
        seal = _load(store, state["executionSeal"])
        for key, ref in seal["sourceReports"].items():
            source(ref, "execution_source_" + key)
    for obligation in rows:
        index, outcome = evaluated.get(obligation["id"], (None, None))
        if outcome and outcome["status"] == "passed":
            continue
        origin = {"run": state["run"], "obligation": obligation["id"],
                  "recordSha256": obligation["record"]["sha256"]}
        row = {"id": "CE-" + digest(origin)[:24], "origin": origin,
               "source": {"source": briefs[obligation["brief"]], "quote": obligation["quote"]},
               "obligationSource": source(obligation["record"], "execution_obligation"),
               "startSource": source(obligation["startEvidence"], "execution_start"),
               "sealSource": source(state["executionSeal"], "execution_seal") if state.get("executionSeal") else None,
               "resultSource": source(last_source, "execution_result") if last_source else None,
               "resultIndex": index, "result": copy.deepcopy(outcome),
               "status": outcome["status"] if outcome else "pending", "historical": True, "currentProof": False}
        previous = next((item for item in result if item["id"] == row["id"]), None)
        require(previous is None or previous == row, "Carryover: конфликт происхождения остатка исполнения")
        if previous is None:
            result.append(row)
    return result

def compact(ledger, *, unresolved_only=False):
    sources = {source["id"]: source for source in ledger["sources"]}
    result = []
    for row in ledger.get("executionRemainders", []):
        decision = disposition(row)
        if unresolved_only and decision == "dropped":
            continue
        obligation = json.loads(sources[row["obligationSource"]]["text"])
        owner = copy.deepcopy(row["dispositions"][-1]) if row.get("dispositions") else None
        if owner and isinstance(owner.get("quote"), str):
            owner["quoteOmittedChars"] = max(0, len(owner["quote"]) - 1200)
            owner["quote"] = owner["quote"][:1200]
        result.append({"id": row["id"], "originRun": row["origin"]["run"],
                       "obligationId": row["origin"]["obligation"], "event": obligation["event"],
                       "deadline": obligation["deadline"], "status": row["status"],
                       "sourceRecordSHA": row["origin"]["recordSha256"],
                       "sealed": row["sealSource"] is not None, "historical": True, "currentProof": False,
                       "note": row["result"]["note"] if row["result"] else "No engine finish event was recorded",
                       **({"disposition": decision, "ownerDecision": owner,
                           "timingOutcomeUnchanged": True} if row.get("dispositions") else {})})
    return result
