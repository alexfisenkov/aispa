"""Seal source-first observations before exposing the final requirement mapping.

Native identities are recorded declarations, as in team.py; this module cannot
authenticate an agent with its host. It rejects identities already used by the
task team and binds the same final reviewer to both stages.
"""
import copy
import json

from . import carryover, cleanroom, phase_receipts, execution_obligations
from .contracts import active, artifact_valid, nonempty, require
from .store import Invalid, confined, digest, file_hash, timestamp


def _json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def _sources(store, state):
    sources = {}
    for source in state.get("briefs", []):
        require(artifact_valid(store, source), "Изменён исходный бриф blind-приёмки")
        require(source["id"] not in sources, "Повторный ID исходного брифа")
        sources[source["id"]] = {"id": source["id"], "text": store.text(source),
                                 "origin": {"run": state["run"], "file": source["file"], "sha256": source["sha256"]}}
    if state.get("carryover"):
        for source in carryover.brief_sources(store, state):
            require(source["id"] not in sources and nonempty(source.get("text")), "Некорректный исторический бриф")
            quotes = source.get("applicableQuotes")
            require(isinstance(quotes, list) and all(nonempty(q) and q in source["text"] for q in quotes),
                    "Историческому брифу нужны applicableQuotes текущего объёма")
            sources[source["id"]] = copy.deepcopy({key: source[key] for key in ("id", "text", "origin", "applicableQuotes")})
    require(sources, "Для blind-приёмки нужны оригинальные брифы")
    return sources


def _candidate(store, state):
    sources = _sources(store, state)
    context = {}
    if state.get("policy", {}).get("projectDocuments"):
        from . import project_documents
        context["projectIntent"] = project_documents.intent_reference(store, state)
    return {**context, "run": state["run"], "briefs": list(sources.values()), "sourceCode": store.code(),
            "head": store.git("rev-parse", "HEAD").decode("ascii").strip(),
            "tree": store.git("rev-parse", "HEAD^{tree}").decode("ascii").strip()}


def basis(store, state):
    """Stable source/code identity, independent of clone paths and test reruns."""
    return digest(_candidate(store, state))


def _reviewer(state, reviewer):
    require(nonempty(reviewer) and not any(c.isspace() for c in reviewer), "Нужен native ID отдельного итогового приёмщика")
    used = set(state.get("actors", {}))
    for actor in state.get("actors", {}).values():
        used.update(value for value in (actor.get("id"), actor.get("nativeId")) if isinstance(value, str))
    # Include replaced/historical task bindings even if a malformed state has
    # dropped the corresponding actor from the global registry.
    for history in state.get("taskHistory", {}).values():
        for attempt in history:
            used.update(attempt.get("actors", {}).values())
            used.update(attempt.get("writers", []))
            for replacement in attempt.get("actorHistory", []):
                used.update(v for v in (replacement.get("previous"), replacement.get("replacement")) if isinstance(v, str))
    if state.get("policy", {}).get("focusedReview") is True:
        from . import final_repairs
        used |= final_repairs.used_identities(state)
    require(reviewer not in used, "Итоговый blind-приёмщик должен отличаться от всех участников задач, ремонтов и их native ID")


def _positions(text, quote):
    start = text.find(quote)
    while start >= 0:
        yield start, start + len(quote)
        start = text.find(quote, start + 1)


def _overlap(text, left, right):
    """Both exact quotes must refer to an overlapping span in the original."""
    if not nonempty(left) or not nonempty(right):
        return False
    return any(a < d and c < b and text[max(a, c):min(b, d)].strip()
               for a, b in _positions(text, left) for c, d in _positions(text, right))


def _focused(state):
    return state.get("policy", {}).get("focusedReview") is True


def item_quotes(store, state, items):
    """Exact brief quotes of diverged requirements: all a recheck acceptor is told about them."""
    requirements = {r["id"]: r for r in state["requirements"]}
    rows = []
    for rid in items:
        require(rid in requirements, "Неизвестный пункт повторной приёмки: " + str(rid))
        original = _original_requirement(requirements, requirements[rid])
        require(nonempty(original.get("brief")) and nonempty(original.get("quote")),
                "У пункта повторной приёмки нет дословной цитаты брифа: " + rid)
        rows.append({"requirement": rid, "brief": original["brief"], "quote": original["quote"]})
    return rows


def _recheck(store, state, report):
    """A recheck names the blind report it repeats and exactly the items of the checked repair."""
    from . import final_repairs
    require(_focused(state), "Повторная слепая приёмка есть только в прогонах с focusedReview")
    repair = final_repairs.pending_recheck(state)
    require(repair is not None, "Повторная слепая приёмка идёт только после проверенного ремонта расхождений")
    require(report.get("recheckOf") == {key: repair["recheckOf"][key] for key in ("file", "sha256")},
            "recheckOf должен указывать на blind-отчёт, по которому открыт ремонт " + repair["id"])
    require(isinstance(report.get("items"), list) and sorted(report["items"]) == repair["items"]
            and len(set(report["items"])) == len(report["items"]),
            "Повторная приёмка проверяет ровно расходившиеся пункты: " + ", ".join(repair["items"]))
    clone = (state.get("cleanroom") or {}).get("id")
    require(clone and clone not in {saved.get("cleanroom") for saved in _history(store, state)},
            "Повторная приёмка идёт только из нового чистого клона")
    return repair, [[row["brief"], row["quote"]] for row in item_quotes(store, state, repair["items"])]


def _validate_report(store, state, report, scope=None):
    require(isinstance(report, dict), "Blind-отчёт должен быть объектом JSON")
    require(report.get("basis") == basis(store, state), "Blind-отчёт устарел или относится к другому исходнику")
    _reviewer(state, report.get("reviewer"))
    phase_receipts.report_actor(store, state, "blind", report)
    require(report.get("independent") is True, "Нужен независимый blind-приёмщик")
    return _observations(store, _sources(store, state), report, scope=scope)


def _observations(store, sources, report, scope=None):
    rows = report.get("observations")
    require(isinstance(rows, list) and rows, "Нет независимых наблюдений по оригинальному брифу")
    seen, covered, artifacts = set(), set(), []
    for row in rows:
        require(isinstance(row, dict), "Некорректное blind-наблюдение")
        bid, quote = row.get("brief"), row.get("quote")
        require(isinstance(bid, str) and bid in sources and nonempty(quote), "Нужны исходный brief и дословная quote")
        source = sources[bid]
        require(quote in source["text"], "Цитата blind-наблюдения отсутствует в оригинале")
        key = (bid, quote)
        require(key not in seen, "Повторное blind-наблюдение по одной цитате")
        seen.add(key)
        covered.add(bid)
        quotes = source.get("applicableQuotes")
        if quotes is not None:
            require(any(_overlap(source["text"], quote, q) for q in quotes),
                    "Историческое наблюдение выходит за текущие applicableQuotes")
        require(row.get("status") in ("passed", "failed", "not_tested", "blocked"), "Неизвестный статус blind-наблюдения")
        require(nonempty(row.get("note")), "Наблюдению нужно объяснение")
        if scope is not None:
            require(any(bid == b and _overlap(source["text"], quote, q) for b, q in scope),
                    "Повторная приёмка наблюдает только расходившиеся пункты")
        if row["status"] == "passed":
            name = row.get("artifact")
            require(isinstance(name, str) and name.startswith("evidence/"), "Нужен файл доказательства из evidence/")
            path = confined(store.run, name)
            require(path.is_file() and path.stat().st_size > 0, "Нет непустого доказательства blind-наблюдения")
            artifacts.append({"file": name, "sha256": file_hash(path)})
    if scope is not None:
        require(all(any(row["brief"] == b and _overlap(sources[b]["text"], row["quote"], q) for row in rows)
                    for b, q in scope), "Повторная приёмка должна наблюдать каждый расходившийся пункт")
        return artifacts
    required = {bid for bid, source in sources.items() if source.get("applicableQuotes") is None or source["applicableQuotes"]}
    require(covered == required, "Blind-наблюдения должны охватывать каждый применимый оригинальный бриф")
    for bid, source in sources.items():
        for quote in source.get("applicableQuotes", []):
            require(any(row["brief"] == bid and _overlap(source["text"], row["quote"], quote) for row in rows),
                    "Blind-наблюдения не покрыли одну из applicableQuotes текущего объёма")
    return artifacts


def _sealed_report(store, saved, run):
    """Check an immutable record against its own candidate, including old code."""
    require(isinstance(saved, dict) and all(artifact_valid(store, saved.get(key, {}))
            for key in ("record", "report", "candidate")), "Повреждена история blind-приёмки")
    require(store.json(saved["record"]) == {k: v for k, v in saved.items() if k != "record"},
            "Изменена запись blind-приёмки")
    report = store.json(saved["report"])
    candidate = store.json(saved["candidate"])
    require(isinstance(report, dict) and isinstance(candidate, dict) and candidate.get("run") == run
            and digest(candidate) == saved.get("basis"), "Нарушена привязка blind-истории к исходному кандидату")
    require(all(saved.get(key) == report.get(key) for key in ("basis", "reviewer", "independent", "observations"))
            and report.get("independent") is True, "Отчёт не соответствует записи blind-приёмки")
    _reviewer({}, report.get("reviewer"))
    sources = {source["id"]: source for source in candidate["briefs"]}
    require(len(sources) == len(candidate["briefs"]) and sources, "Повреждены исходные брифы blind-истории")
    require(all(saved.get(key) == report.get(key) for key in ("recheckOf", "items")),
            "Отчёт повторной приёмки не соответствует записи")
    require(_observations(store, sources, report, scope=saved.get("scope")) == saved.get("artifacts") and
            all(artifact_valid(store, artifact) for artifact in saved["artifacts"]),
            "Изменено доказательство blind-истории")
    return report


def _history(store, state):
    history = state.get("blindHistory", [])
    require(isinstance(history, list), "Повреждён список blindHistory")
    current = state.get("blindReview")
    require(not history or current is not None, "История blind-приёмки потеряла последнюю запись")
    records = history + ([current] if current is not None else [])
    previous, bases, reviewers = None, set(), set()
    for saved in records:
        _sealed_report(store, saved, state["run"])
        require(saved.get("previous") == previous, "Нарушен порядок или полнота blindHistory")
        require(saved["basis"] not in bases and saved["reviewer"] not in reviewers,
                "Повторный кандидат или использованный итоговый приёмщик в blindHistory")
        bases.add(saved["basis"])
        reviewers.add(saved["reviewer"])
        previous = saved["record"]["sha256"]
    return records


def record(store, state, report):
    """Save the first immutable observation set for this candidate; no append."""
    require(cleanroom.valid(store, state), "Blind-наблюдения требуют актуальный успешный clean-run")
    history = _history(store, state)
    recheck = isinstance(report, dict) and ("recheckOf" in report or "items" in report)
    repair, scope = _recheck(store, state, report) if recheck else (None, None)
    artifacts = _validate_report(store, state, report, scope)
    require(report["basis"] not in {saved["basis"] for saved in history},
            "Первичные blind-наблюдения этого кандидата уже зафиксированы; их нельзя заменить после раскрытия реестра")
    require(report["reviewer"] not in {saved["reviewer"] for saved in history},
            "Новому кандидату нужен новый итоговый приёмщик, не видевший прежний реестр")
    extra = {}
    if _focused(state):
        from . import final_repairs
        require(recheck or final_repairs.pending_recheck(state) is None,
                "После ремонта расхождений нужна повторная приёмка с recheckOf и items, а не новая полная")
        extra["cleanroom"] = state["cleanroom"]["id"]
        if recheck:
            extra.update(recheckOf=copy.deepcopy(report["recheckOf"]), items=sorted(report["items"]),
                         scope=scope, repair=repair["id"])
    source = store.artifact("blind-reviews", _json(report), ".json")
    candidate = store.artifact("blind-candidates", _json(_candidate(store, state)), ".json")
    result = {"basis": report["basis"], "reviewer": report["reviewer"], "independent": True,
              "observations": copy.deepcopy(report["observations"]), "artifacts": artifacts,
              "report": source, "candidate": candidate, **extra,
              "previous": history[-1]["record"]["sha256"] if history else None, "at": timestamp()}
    result["record"] = store.artifact("blind-records", _json(result), ".json")
    state["blindHistory"] = copy.deepcopy(history)
    state["blindReview"] = result
    actor = phase_receipts.report_actor(store, state, "blind", report)
    phase_receipts.record(store, state, "blind", source, actor=actor, basis=report["basis"])
    return copy.deepcopy(result)


def valid(store, state):
    try:
        saved = state.get("blindReview")
        if not isinstance(saved, dict) or not cleanroom.valid(store, state):
            return False
        _history(store, state)
        report = store.json(saved["report"])
        return (_validate_report(store, state, report, saved.get("scope")) == saved.get("artifacts")
                and phase_receipts.valid(store, state, "blind"))
    except (Invalid, OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def _original_requirement(requirements, target):
    found = set()
    while target.get("kind") == "addition":
        require(target["id"] not in found, "Цикл исходных требований дополнения")
        found.add(target["id"])
        target = requirements.get(target.get("parent"))
        require(target is not None, "У дополнения нет исходного требования")
    return target


def _mapped_quote(store, state, source, requirement):
    if source["id"].startswith("H-"):
        quotes = []
        for item in carryover.view(store, state)["items"]:
            linked = item.get("mapping") or {}
            original = linked.get("originalSource") or {}
            if (linked.get("run") == state["run"] and linked.get("requirement") == requirement["id"]
                    and "H-" + str(original.get("source")) == source["id"]):
                quotes.append(original.get("quote"))
        return quotes
    if requirement.get("brief") == source["id"]:
        return [requirement.get("quote")]
    return []


def _scope_decision(state, target, supplied, sources):
    decision = target.get("decision")
    require(isinstance(decision, dict) and supplied == decision,
            "scopeDecision должен точно совпадать с текущим решением владельца")
    action, bid, quote = decision.get("action"), decision.get("brief"), decision.get("quote")
    require(action in ("drop", "defer", "restore") and
            target.get("scope", "active") == {"drop": "dropped", "defer": "deferred", "restore": "active"}[action],
            "Решение владельца не соответствует текущему scope требования")
    require(bid in {b["id"] for b in state["briefs"]} and bid in sources and
            nonempty(quote) and quote in sources[bid]["text"], "Решение scope не связано с текущим оригинальным брифом владельца")
    return decision


def observations(store, state, saved=None):
    """Observations acceptance maps: a recheck keeps the earlier ones of matched items and adds its own."""
    saved = saved if saved is not None else state["blindReview"]
    if "recheckOf" not in saved:
        return copy.deepcopy(saved["observations"])
    records = {row["report"]["sha256"]: row for row in _history(store, state)}
    prior = records.get(saved["recheckOf"]["sha256"])
    require(prior is not None and prior is not saved, "Повторная приёмка потеряла исходный blind-отчёт")
    sources = _sources(store, state)
    kept = [row for row in observations(store, state, prior)
            if not any(row["brief"] == b and _overlap(sources[b]["text"], row["quote"], q) for b, q in saved["scope"])]
    return kept + copy.deepcopy(saved["observations"])


def divergent_items(store, state, report):
    """Requirement IDs whose criteria or blind observations failed in this final report."""
    failed = {row.get("requirement") for row in report.get("checks", [])
              if isinstance(row, dict) and row.get("status") != "passed"}
    failed |= {row["requirement"] for row in mapping(store, state, report) if row.get("kind") != "execution"}
    return sorted(rid for rid in failed if isinstance(rid, str))


def mapping(store, state, final_report):
    """Reconcile every observation, returning failures that final rows cannot erase."""
    require(valid(store, state), "Нет актуального неизменного blind-отчёта до итоговой сверки")
    saved = state["blindReview"]
    require(isinstance(final_report, dict) and final_report.get("reviewer") == saved["reviewer"]
            and final_report.get("independent") is True, "Итоговую сверку должен продолжить тот же отдельный blind-приёмщик")
    require(final_report.get("blindReport") == saved["report"]["sha256"], "Нет ссылки на точный исходный blind-отчёт")
    rows = final_report.get("blindMapping")
    seen_rows = observations(store, state, saved) if _focused(state) else saved["observations"]
    require(isinstance(rows, list) and len(rows) == len(seen_rows), "blindMapping должен содержать каждое наблюдение ровно один раз")
    unmapped = final_report.get("unmapped")
    require(isinstance(unmapped, list), "Итоговому отчёту нужен список unmapped")
    requirements = {r["id"]: r for r in state["requirements"]}
    available = {r["id"]: r for r in active(state)}
    sources = _sources(store, state)
    seen, failures = set(), []
    for row in rows:
        require(isinstance(row, dict), "Некорректная строка blindMapping")
        index = row.get("observation")
        require(type(index) is int and 0 <= index < len(seen_rows) and index not in seen,
                "Некорректный или повторный индекс наблюдения blindMapping")
        seen.add(index)
        observation = seen_rows[index]
        require("requirement" in row, "blindMapping должен явно назвать requirement или null")
        rid = row["requirement"]
        if "executionObligation" in row:
            require(execution_obligations.enabled(state) and rid is None and "scopeDecision" not in row,
                    "Обязательство исполнения не заменяет продуктовое требование")
            obligations = {item["id"]: item for item in execution_obligations.obligations(store, state, sealed=True)}
            obligation = obligations.get(row["executionObligation"])
            require(obligation is not None and row.get("executionRecord") == obligation["record"]["sha256"],
                    "Нужна точная ссылка на заранее зафиксированное обязательство исполнения")
            require(observation["brief"] == obligation["brief"] and observation["quote"] == obligation["quote"],
                    "Наблюдение должно точно соответствовать отдельной исходной цитате обязательства")
            failures.append({"kind": "execution", "executionObligation": obligation["id"],
                             "executionRecord": obligation["record"]["sha256"], "requirement": None,
                             "criterion": "blind:%d" % index, "status": observation["status"],
                             "note": observation["note"], "observation": index})
            continue
        if rid is None:
            require(bool(unmapped), "Потерянную просьбу необходимо явно оставить в unmapped")
            failures.append({"requirement": None, "criterion": "blind:%d" % index, "status": "blocked",
                             "note": "Исходное наблюдение не связано с активным требованием: " + observation["note"],
                             "observation": index})
            continue
        require(isinstance(rid, str) and rid in requirements, "Наблюдение связано с отсутствующим требованием")
        target = requirements[rid]
        original = _original_requirement(requirements, target)
        source = sources[observation["brief"]]
        quotes = _mapped_quote(store, state, source, original)
        original_match = any(_overlap(source["text"], observation["quote"], quote) for quote in quotes)
        if "scopeDecision" in row:
            decision = _scope_decision(state, target, row["scopeDecision"], sources)
            decision_match = source["id"] == decision["brief"] and _overlap(
                source["text"], observation["quote"], decision["quote"])
            require(decision_match or (rid not in available and original_match),
                    "scopeDecision не заменяет проверку действующего исходного требования")
            if observation["status"] != "passed":
                if decision_match:
                    failures.append({"requirement": None, "criterion": "blind:%d" % index,
                                     "status": observation["status"], "note": observation["note"], "observation": index})
                elif original["id"] in available:
                    failures.append({"requirement": original["id"], "criterion": "blind:%d" % index,
                                     "status": observation["status"], "note": observation["note"], "observation": index})
            # Missing implementation of an explicitly dropped/deferred request
            # is intentional, but a failed owner-decision check is not optional.
            continue
        require(rid in available, "Снятое требование требует явного текущего scopeDecision")
        require(original_match, "Требование не связано с дословным источником blind-наблюдения; потерянное оставьте в unmapped")
        if observation["status"] != "passed":
            # Mapping through an optional addition must not hide a failed core parent.
            ids = {rid} | ({original["id"]} if original["id"] in available else set())
            for failed_id in sorted(ids):
                failures.append({"requirement": failed_id, "criterion": "blind:%d" % index,
                                 "status": observation["status"], "note": observation["note"], "observation": index})
    require(seen == set(range(len(seen_rows))), "Потеряно исходное blind-наблюдение")
    return failures


def derived_waves_valid(store, state):
    """Derived final checkpoints bind the exact final report and original predicate."""
    from .contracts import basis as gate_basis, plan_basis, evidence_rows
    try:
        for number, wave in state.get("waves", {}).items():
            if "derivedFrom" not in wave:
                continue
            require(state.get("policy", {}).get("finalWaveReuse") and
                    state.get("policy", {}).get("cleanClone") and len(state["tasks"]) == 1 and
                    state["tasks"][0]["status"] == "completed" and str(state["tasks"][0]["wave"]) == number,
                    "Производная сверка разрешена только для одной финальной задачи")
            gate = state["gates"].get("acceptance", {})
            require(gate.get("passed") and artifact_valid(store, gate.get("report", {})) and
                    gate["report"]["sha256"] == wave["derivedFrom"] and
                    wave["basis"] == gate_basis(store, state, "wave") and
                    wave["plan"] == plan_basis(state) and artifact_valid(store, wave["report"]),
                    "Производная сверка волны устарела")
            saved = store.json(wave["report"])
            final = store.json(gate["report"])
            require(saved.get("kind") == "derived-final-wave" and saved.get("run") == state["run"] and
                    saved.get("wave") == int(number) and saved.get("basis") == wave["basis"] and
                    saved.get("plan") == wave["plan"] and saved.get("derivedFrom") == wave["derivedFrom"] and
                    saved.get("blindReport") == wave["blindReport"] == state["blindReview"]["report"]["sha256"] and
                    saved.get("checks") == final.get("checks") and saved.get("unmapped") == final.get("unmapped") == [] and
                    saved.get("predicate") == "full-scope-delivered-requirements-passed",
                    "Производная сверка не соответствует исходной приёмке")
            expected = {(r["id"], c) for r in active(state) for c in r["acceptance"]}
            artifacts, failures = evidence_rows(store, final, expected)
            require(not failures and artifacts == wave.get("artifacts") and
                    not any(row.get("kind") != "execution" for row in mapping(store, state, final)),
                    "Производная сверка потеряла критерии или исходное наблюдение")
        return True
    except (Invalid, OSError, ValueError, KeyError, TypeError, AttributeError):
        return False
