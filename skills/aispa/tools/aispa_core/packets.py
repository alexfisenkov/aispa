"""Bounded dispatch envelopes. Full evidence is referenced, never truncated."""
import hashlib
import json

from .contracts import (active, basis, definition, task_closure, require, artifact_valid)
from .store import common_input, confined, Invalid
from .portable import match_name, relative_name
from . import autonomy, docmeta, telemetry, review_policy, task_tiers, scale
from .focused_review import BLOCKING_CLASSES

MAX_PACKET_BYTES = 16000


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def _context_artifact(store, value):
    """Stable references for derived context only; never replace damaged bytes.

    CLI callers hold the journal lock. Fresh actors still read independently;
    address reuse is not evidence that any actor has read these bytes.
    """
    data = encode(value)
    sha = hashlib.sha256(data).hexdigest()
    if getattr(store, "scale_enabled", False) and len(data) > scale.Budget().chunk_bytes:
        # A separate index points to the immutable manifest, never changes an
        # existing by-sha artifact's meaning, and is not used as evidence.
        index_name = "contexts/manifest-index/" + sha + ".json"
        index_path = confined(store.run, index_name)
        if index_path.exists():
            ref = json.loads(scale.read_file(index_path, allow_manifest=False))
            require(artifact_valid(store, ref) and store.text(ref).encode() == data,
                    "Повреждён сохранённый пакетный раздел контекста")
            return ref
        ref = store.artifact("reviews", data, ".json")
        index_path.parent.mkdir(parents=True, exist_ok=True)
        with index_path.open("xb") as stream:
            stream.write(encode(ref))
        return ref
    name = "contexts/by-sha/" + sha + ".json"
    path = confined(store.run, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path = confined(store.run, name)
    try:
        with path.open("xb") as stream:
            stream.write(data)
    except FileExistsError:
        path = confined(store.run, name)
        require(path.is_file() and path.read_bytes() == data,
                "Повреждён сохранённый раздел контекста; автоматическая замена запрещена")
    return {"file": name, "sha256": sha}


MODEL_RULE = ("Дешёвая модель по умолчанию; сильная — только для задач в зоне безопасности, ключей, доступа, "
              "платежей и личных данных. Уровень мышления выбирает среда.")
MANAGED_MODEL_RULE = ("Выбирай модель по сложности работы и реальным возможностям среды: сильную для сложной, "
    "обычную для содержательной, экономную для механической. Чувствительная работа требует подтверждённого "
    "сильного маршрута; неизвестная возможность блокирует её. Совет не доказывает выбранную или actual модель. "
    "Класс проверки и режим проверок задаются отдельно.")


CODEX_MODEL_RULE = ("Для новых субагентов Codex выбирай последнюю разрешённую Terra из фактического каталога host; "
    "если она недоступна, сообщи о пробеле возможностей, не переключайся молча на Luna. "
    "Reasoning: low для механики, medium/high для обычной работы, high/xhigh для критики и безопасности. "
    "Запрошенная модель не доказывает actual model; без provider metadata она unknown.")


def _lean_strong(task, attempt):
    return any(task_tiers.strong_model_path(zone) for zone in task.get("zones", []))


ROUTE_RULE = ("Модель и усилие роли берутся из таблицы маршрутов (references/model-routing.md); "
              "замен нет: если утверждённой модели нет в среде, работа роли блокируется, а не уходит на другую.")


def routing(state, task, role, attempt=None, store=None):
    """Review shape of a role plus its model advice.

    With ``modelRoutes`` the model and effort come only from the route table
    (model_routes.route); otherwise the host-defined tier advice stays.
    """
    value = _review_routing(state, task, role, attempt, store)
    if state.get("policy", {}).get("modelRoutes") is not True:
        return value
    from . import model_routes
    route = model_routes.route(state, role, task, attempt)
    for key in ("modelRule", "modelNeed", "hostModelPolicy", "tier"):
        value.pop(key, None)
    value.update(routeClass=route["class"], model=route["model"], effort=route["effort"], route=route,
                 advisory=route["advisory"], selection=ROUTE_RULE, reference="references/model-routing.md")
    if "agent" in route:
        value["agent"] = route["agent"]
    if not task_tiers.worker_only(attempt if attempt is not None else task):
        value["reason"] = "Класс роли " + route["class"] + " по таблице маршрутов"
    return value


def _review_routing(state, task, role, attempt=None, store=None):
    """Recommend host-defined tiers, never invent a model ID or drop reviews."""
    foundation = any(task["id"] in other["dependsOn"] for other in state["tasks"])
    inherited_retry = False
    if store is not None and state.get("carryover"):
        from .carryover import view
        inherited_retry = any(item.get("attempts") and item.get("mapping", {}).get("run") == state["run"]
                              and item["mapping"]["requirement"] in task.get("requirements", [])
                              for item in view(store, state)["items"] if item.get("mapping"))
    strong = (role != "worker" or task.get("risk", "normal") != "low" or foundation
              or bool((attempt or {}).get("authorization")) or inherited_retry
              or bool((attempt or {}).get("repairPlan")))
    subject = attempt if attempt is not None else task
    lean = state.get("policy", {}).get("leanControl") is True
    managed = state.get("policy", {}).get("managedExecution") is True
    extra = {}
    selection_rule = "Use only models exposed by the host and permitted by project rules; otherwise keep the current model"
    if lean:
        strong = _lean_strong(task, attempt)
        if state.get("policy", {}).get("managedExecution"):
            strong = task_tiers.sensitive_task(state, task)
        extra = {"modelRule": MODEL_RULE}
    if managed:
        from .participants import model_advice
        need = task_tiers.model_need(state, task, attempt)
        strong = need == "complex"
        extra = {"modelRule": MANAGED_MODEL_RULE, "modelNeed": need,
                 "hostModelPolicy": model_advice(state.get("runtime"), need)}
        selection_rule = ("Preferences are advisory: verify the actual host selector/catalogue. Sensitive work requires "
                          "a source-bound approved strong route; unavailable required capability blocks the work.")
    routes = state.get("policy", {}).get("modelRoutes") is True
    if state.get("runtime") == "codex" and not routes:
        extra["modelRule"] = CODEX_MODEL_RULE
        selection_rule = ("Select the latest permitted Terra from the actual host catalogue; "
                          "if unavailable, report the capability gap without a silent Luna fallback. "
                          "Sensitive work requires a source-bound approved strong route. "
                          "A requested model is not an observed actual model.")
    if task_tiers.worker_only(subject):
        light = task_tiers.is_light(subject)
        fast = task_tiers.is_fast(subject)
        require(role == "worker", "У light, standard и fast нет роли критика или приёмщика")
        focused = review_policy.focused(subject) if "reviewPolicy" in subject else False
        return {**extra, "tier": "strong" if strong and (lean or managed or not light) else "standard", "advisory": True,
                "taskTier": subject["tier"], "tierReason": subject.get("tierReason"),
                "selection": selection_rule,
                "reason": ("Check mode: this task gets no separate critic (fast: only money, keys, access, personal data "
                           "and critical scenarios); the branch review and the final blind acceptance remain"
                           if focused and subject.get("tier") == "heavy" else
                           "Owner's fast mode: the task gets no per-task review; tests, the clean-copy check and the final blind acceptance remain" if fast else
                           "Explicit bounded low-risk change; semantic safety still requires final review" if light else
                           "Ordinary non-foundation code; the whole-version review replaces a per-task critic"),
                "reviewPolicy": "verified_completion", "criticRounds": 0,
                "minimumCriticRounds": 0, "maximumCriticRounds": 0, "actualCriticRounds": 0,
                "taskRoleSlots": {"minimum": 1, "maximum": 1},
                "perTaskReview": "skipped_" + subject["tier"], "projectReviewSeparate": True,
                "reviewRule": "Current verification and worker evidence complete implementation; independent project review and final acceptance remain mandatory."}
    policy = review_policy.mode(attempt) if attempt is not None else review_policy.selected(state)
    conditional = policy in (review_policy.ADAPTIVE, review_policy.LEAN)
    slots = {review_policy.LEAN: {"minimum": 2, "maximum": 3}, review_policy.ADAPTIVE: {"minimum": 3, "maximum": 4}}
    slots[review_policy.FOCUSED] = {"minimum": 2, "maximum": 3}
    rules = {review_policy.FOCUSED: "Only the short list blocks (blockingClass: " + ", ".join(_blocking_classes()) + "); "
             "warnings are collected for the branch review. First PASS completes the task unless the max mode requires "
             "a second fresh critic of a heavy task. A blocking finding requires repair-plan, repair and a fresh critic2 "
             "who re-checks the repair diff. No third round, no separate task acceptor.",
             review_policy.LEAN: "First PASS completes the task: the critic also checks closure criteria, constraints and the trace to the owner's words. WARN/BLOCK/FAIL requires repair and one fresh critic2 whose PASS completes the task. No third round, no separate task acceptor.",
             review_policy.ADAPTIVE: "First PASS goes to a separate acceptor; WARN/BLOCK/FAIL requires repair and one fresh critic2. No third round."}
    reason = (("Independent review, shared foundation, risk or retry" if strong else "Explicitly low-risk isolated implementation") if not lean else
              ("Sensitive zone: security, keys, access, payments or personal data" if strong else "Cheap model by default"))
    if managed:
        reason = {"complex": "Complex, shared, high-risk or sensitive work; strong model preference",
                  "ordinary": "Ordinary substantive work; provider-specific ordinary model preference",
                  "mechanical": "Bounded mechanical work; economical model preference"}[need]
    if state.get("runtime") == "codex" and not routes:
        reason = "Codex: Terra preference; reasoning follows work complexity and risk"
    return {**extra, "tier": "strong" if strong else "standard", "advisory": True,
            "reason": reason,
            "selection": selection_rule,
            "report": "Save the full structured report to a file; return its path and at most 25 lines of conclusions",
            "reviewPolicy": policy,
            "criticRounds": None if conditional else 2,
            "minimumCriticRounds": 1 if conditional else 2,
            "maximumCriticRounds": 2,
            "actualCriticRounds": len(attempt.get("rounds", [])) if attempt is not None else None,
            "taskRoleSlots": slots.get(policy, {"minimum": 4, "maximum": 4}),
            "roleSlotNote": "Base role slots for one attempt, not a native-start cap. Excludes preparation, actor replacements and newly authorized attempts; actual cost requires native usage.",
            "reviewRule": rules.get(policy, "Two independent critics, then a separate acceptor.")}


def _blocking_classes():
    return BLOCKING_CLASSES


FOCUSED_CRITIC_READING = ("Focused critic: read the role card, every file in zoneChanges in full (scope attempt: what "
    "this attempt changed; repair: only the repair diff after repairBaseline; branch: the whole branch of the final "
    "task), the test intent, expectedChecks and the exact owner quotes in traceRequirements. criteriaFiles are the "
    "files the criteria rest on: open one when you check a criterion that rests on it, not wholesale. Fields in "
    "onDemand are reference material. Accepted ancestors appear in expectedChecks only when this attempt changed "
    "their files. Only blockingClass " + ", ".join(BLOCKING_CLASSES) + " blocks; mark everything else warning: it is collected for the branch review.")


LEAN_CRITIC_READING = ("Lean critic: first read the role card, every file in zoneChanges in full (for the final task "
    "zoneChangesScope is branch: the whole diff, including standard tasks that no critic has read), the test intent, "
    "expectedChecks and the exact owner quotes in traceRequirements. Fields listed in onDemand are reference "
    "material: open them for a specific check or suspected drift, not wholesale. Every expectedCheck and every "
    "trace row stays mandatory; metadata never replaces evidence.")


def _brief_packet(store, state):
    return [{**b, "absolutePath": str(store.run / b["file"])} for b in state["briefs"]]


def document_context(store, state, kind, selected, source_paths=None):
    """Navigation is optional; original sources and proof contracts are not."""
    from .discovery import _kind, _names, _safe_path
    zones = {match_name(zone) for task in selected for zone in task["zones"] + task.get("readZones", [])}
    registered = state.get("documents", {})
    paths = [name for name in registered if kind != "task" or common_input(name) or any(
        zone == "." or match_name(name) == zone or match_name(name).startswith(zone + "/") for zone in zones)]
    if kind == "coverage" and source_paths is None:
        return {"documentRouting": {"catalog": "omitted", "reason": "brief-and-spec"}}

    known = None
    if source_paths is not None:
        require(isinstance(source_paths, list) and source_paths, "Нужен непустой список известных файлов")
        names = set(_names(store))
        known = []
        for raw in source_paths:
            try:
                name = relative_name(raw)
            except (ValueError, TypeError) as exc:
                raise Invalid("Небезопасный путь известного источника") from exc
            require(name in names and (_kind(name, explicit=True) is not None or common_input(name)),
                    "Источник отсутствует, исключён или не поддержан: " + name)
            require(_safe_path(store.root, name).is_file(), "Нужен точный файл, не каталог: " + name)
            require(kind != "task" or common_input(name) or any(zone == "." or match_name(name) == zone
                    or match_name(name).startswith(zone + "/") for zone in zones),
                    "Источник вне readZones/zones задачи: " + name)
            known.append(name)
        known = sorted(set(known))
    elif selected:
        selectors = sorted({zone for task in selected for zone in task["zones"] + task.get("readZones", [])})
        # Missing/future paths and directory selectors do not establish a full read list.
        try:
            names = set(_names(store))
            if selectors and all(name in names and (_kind(name, explicit=True) is not None or common_input(name))
                                 and _safe_path(store.root, name).is_file() for name in selectors):
                known = selectors
        except (OSError, ValueError):
            pass
    if known is not None:
        checked = sorted(set(known) | {name for name in paths if common_input(name)})
        route = {"catalog": "omitted", "reason": "explicit-files" if source_paths is not None else "task-files"}
        if source_paths is not None:
            route["sources"] = known
        result = {"documentRouting": route}
        warnings = docmeta.source_warnings(store, state, checked)
        if warnings:
            result["documentWarnings"] = warnings
        return result
    return {"documentCatalog": docmeta.catalog(store, state, paths=paths),
            "documentRouting": {"catalog": "compact", "reason": "navigation-needed"}}


def build(store, state, pipeline, kind, task_id=None, role=None, *, source_paths=None):
    if kind == 'acceptance' and state.get('policy', {}).get('criticalProductFlows') is True:
        from . import blind
        require(blind.valid(store, state), 'Product flows: сначала сохраните независимые source-first blind-наблюдения')
    if kind == "blind":
        require(source_paths is None, "Blind-пакет не принимает каталог или список источников проекта")
        # Deliberately omit spec, plan, registry, reviews, discovery and memory.
        from . import blind, carryover
        clone = state.get("cleanroom") or {}
        focused = state.get("policy", {}).get("focusedReview") is True
        recheck = {}
        if focused:
            from . import final_repairs
            repair = final_repairs.pending_recheck(state)
            if repair is not None:
                # Only the brief and the verbatim quotes of diverged items: never the spec or the registry.
                recheck = {"recheck": {"recheckOf": {k: repair["recheckOf"][k] for k in ("file", "sha256")},
                                       "items": repair["items"],
                                       "quotes": [{"brief": row["brief"], "quote": row["quote"]}
                                                  for row in blind.item_quotes(store, state, repair["items"])]}}
        original_intent = {}
        if state.get("policy", {}).get("projectDocuments"):
            from . import project_documents
            original_intent["projectIntent"] = project_documents.intent_reference(store, state)
        return {"kind": "blind", "basis": blind.basis(store, state),
                **({"autonomy": autonomy.view(state)} if autonomy.enabled(state) else {}),
                **original_intent,
                "briefs": _brief_packet(store, state),
                "inheritedBriefs": carryover.brief_sources(store, state),
                "clone": {k: clone[k] for k in ("path", "head", "tree") if k in clone}, **recheck,
                "instructions": "Read original briefs and run the cloned product first. If projectIntent is present, read its original source as persistent context, not a new task or permission. Do not open the source project's spec, registry, tasks or reviews. Save independent observations before requesting the reconciliation packet."}
    if kind == "knowledge":
        from . import executions
        from .discovery import _names, _kind, _safe_path
        from .store import digest
        require(source_paths is None, "Для автора знаний используйте список projectFiles и актуальный код")
        names = [name for name in _names(store) if _kind(name, explicit=True) is not None
                 and _safe_path(store.root, name).is_file()]
        check, clean = state.get("verification"), state.get("cleanrun")
        return {"kind": kind, "basis": store.code(), "writer": state.get("knowledge", {}).get("writer", {}).get("id"),
                **({"autonomy": autonomy.view(state)} if autonomy.enabled(state) else {}),
                "projectFiles": names[:128], "omittedFiles": max(0, len(names) - 128),
                "verification": check, "verificationDigest": digest(check) if check else None,
                "cleanrun": clean, "cleanrunDigest": digest(clean) if clean else None,
                "commandHistory": executions.view(store, state, limit=8),
                "handoff": state.get("handoff"),
                "runDirectory": str(store.run),
                "instructions": "Read actual source files and execute or inspect genuine verification evidence. Do not use the plan, spec, brief or task reports as proof of implemented behavior. Record command, trap, decision and glossary knowledge or explicit category uncertainties. A historical command check is not a current PASS."}
    selected = task_closure(state, task_id) if kind == "task" else state["tasks"]
    req_ids = {rid for task in selected for rid in task["requirements"]}
    reqs = [r for r in active(state) if kind != "task" or r["id"] in req_ids or r["kind"] == "constraint"]
    packet = {"kind": kind, "basis": basis(store, state, kind, task_id), "runDirectory": str(store.run),
              **({"autonomy": autonomy.view(state)} if autonomy.enabled(state) else {}),
              "briefs": _brief_packet(store, state), "requirements": reqs, "spec": state["spec"],
              "tasks": [dict(definition(t), status=t["status"]) for t in selected],
              "verification": state["verification"], "projectContext": state.get("projectContext"),
              "discovery": state.get("discovery"), "carryover": state.get("carryover")}
    packet.update(document_context(store, state, kind, selected, source_paths))
    if state.get("policy", {}).get("projectDocuments"):
        from . import project_documents
        packet["projectIntent"] = project_documents.intent_reference(store, state)
        packet["projectFileMap"] = ".aispa/context/INDEX.md"
    if state.get("ideaReview") and kind == "coverage":
        packet["ideaStudy"] = state["ideaReview"]
        if state.get("policy", {}).get("productChallenge"):
            from .product import QUESTIONS
            packet["requiredIdeaChecks"] = list(QUESTIONS)
    if state.get("planReview") and kind == "task":
        packet["deliveryPlan"] = state["planReview"]["report"]
    if kind == "task":
        for key in ("briefMap", "featureScenarios", "interfaceContract"):
            if state.get(key):
                require(artifact_valid(store, state[key]), "Повреждён источник контекста задачи: " + key)
                packet[key] = state[key]
        if state.get("interfaceContract"):
            from . import design_contract
            packet["interfaceReferences"] = design_contract.reference_snapshots(store, state)
    if kind == "task" and state.get("knowledge"):
        from . import knowledge
        paths = sorted({z for task in selected for z in task["zones"] + task.get("readZones", [])})
        hints = knowledge.query(store, state, paths=paths, limit=4, max_chars=3000)
        if hints.get("entries") or hints.get("omitted", {}).get("stale"):
            packet["knowledgeHints"] = hints
    packet["documentReadingRule"] = "Read original briefs, constraints and every required check. Known paths need no catalog; warnings require source inspection. Metadata never replaces evidence."
    if kind == "task":
        current = state["taskHistory"][task_id][-1]
        from . import test_intent
        task = next(t for t in selected if t["id"] == task_id)
        packet.update(verification=current.get("verification"), attempt=current["id"],
                      reviewPolicy=review_policy.mode(current),
                      taskTier=current.get("tier", "heavy"), tierReason=current.get("tierReason", ""),
                      verifiedCompletion=current.get("verifiedCompletion"),
                      maximumCriticRounds=0 if task_tiers.worker_only(current) else 2, actualCriticRounds=len(current["rounds"]),
                      perTaskReview=("skipped_" + current["tier"]) if task_tiers.worker_only(current) else "required",
                      testIntent=current.get("testIntent"),
                      testSourceChanges=test_intent.source_changes(store, state, task_id),
                      repairPlan=current.get("repairPlan"),
                      roles={key: value for key, value in current["roles"].items() if role is None or key == role},
                      actors=current["actors"],
                      rounds=[{k: r[k] for k in ("number", "passed", "verdict", "report") if k in r} for r in current["rounds"]],
                      repair={k: current["repair"][k] for k in ("report", "previousReport", "addresses") if k in current["repair"]} if current.get("repair") else None,
                      traceRequirements=pipeline.trace_requirements(task_id),
                      inheritedProofs=pipeline.inherited_proofs(task_id),
                      neighbors=[{"id": t["id"], "zones": t["zones"], "status": t["status"]}
                                 for t in state["tasks"] if t["id"] != task_id])
        from . import mixed_evidence
        summary = test_intent.route_summary(store, state, task_id)
        if summary.get("mode") == "mixed":
            packet["evidenceSummary"] = summary
            packet["observationTemplates"] = mixed_evidence.observation_templates(store, state, task_id)
            if state.get("policy", {}).get("errorBoundaryChecks"):
                from . import failure_checks
                packet["failureChecksTemplate"] = failure_checks.template(mode="mixed")
        if role and role.startswith("critic") and review_policy.focused(current):
            _focused_critic(store, state, packet, task, task_id, role, current)
        elif role and role.startswith("critic") and review_policy.lean(current):
            from . import integration
            # Read what changed first; the rest stays addressable for a finding.
            final = task_id == state.get("integrationTask") and state.get("policy", {}).get("integrationReview")
            # The final critic is the only independent reader of standard code:
            # every changed file of the branch is mandatory reading for it.
            packet["zoneChanges"] = (integration.context(store, state, task_id)["changes"] if final
                                     else integration.zone_changes(store, state, task["zones"]))
            packet["zoneChangesScope"] = "branch" if final else "task"
            packet["onDemand"] = [key for key in ("briefs", "spec", "discovery", "deliveryPlan", "briefMap",
                                                  "featureScenarios", "interfaceContract", "knowledgeHints",
                                                  "projectContext", "carryover") if key in packet]
            packet["documentReadingRule"] = LEAN_CRITIC_READING
        if role:
            packet["routing"] = routing(state, task, role, current, store)
            packet["expectedChecks"] = [{"requirement": rid, "criterion": criterion}
                                        for rid, criterion in sorted(pipeline.expected_checks(task_id, role))]
    if kind == "acceptance":
        packet.update(cleanroom=state.get("cleanroom"), cleanrun=state.get("cleanrun"),
                      blindReview=state.get("blindReview"), handoff=state.get("handoff"),
                      projectIntegrationReview=state.get("projectIntegrationReview"))
        if state.get("policy", {}).get("focusedReview") is True and state.get("blindReview"):
            from . import blind
            # blindMapping indexes this list: earlier observations of matched items, then the recheck's own.
            packet["blindObservations"] = blind.observations(store, state)
        if state.get("policy", {}).get("managedExecution"):
            packet["finalReportTemplate"] = {"basis": packet["basis"], "reviewer": "", "independent": True,
                "unmapped": [], "checks": [{"requirement": r["id"], "criterion": text, "status": "", "note": "", "artifact": ""}
                    for r in reqs for text in r.get("acceptance", [])],
                "cleanrun": state.get("cleanrun"), "blindReview": state.get("blindReview")}
            packet["evidenceGuide"] = {"pathBase": "run-relative", "example": {"file": "evidence/capture.jpg", "sha256": "<actual SHA-256>"},
                "types": {"procedure": "string", "observed": "string", "source": "{file,sha256}", "attachments": "array<{file,sha256}>"},
                "emptyOutput": "Confirm empty stdout/stderr with nonempty command metadata; never pad an empty attachment",
                "conclusions": "Keep statuses and conclusions blank until independently observed"}
        if state.get('policy', {}).get('criticalProductFlows') is True:
            from . import product_flows
            contract, checks, observations = product_flows.templates(store, state, packet['basis'])
            packet.update(productFlows=contract, productFlowCheckTemplates=checks,
                          productFlowObservationTemplates=observations,
                          productFlowReviewTemplate={'status': 'not_tested', 'note': ''})
        if state.get("policy", {}).get("executionObligations"):
            from . import execution_obligations
            packet["executionObligations"] = execution_obligations.view(store, state)
            packet["executionRule"] = "Map only exact presealed execution quotes/record SHA. Do not rewrite blind status or move product requirements. Future host handoff stays pending, never a product PASS."
    if (kind == "task" and task_id == state.get("integrationTask") and state.get("policy", {}).get("integrationReview")
            and state.get("policy", {}).get("focusedReview") is True):
        from . import integration
        # The whole branch is read once at the end by the lens reviewers, not by this task's critic.
        packet["projectReviewRole"] = "integration-reviewer"
        packet["projectReviewLenses"] = integration.lenses(state)
    elif kind == "task" and task_id == state.get("integrationTask") and state.get("policy", {}).get("integrationReview"):
        from . import integration
        packet["branchReview"] = integration.context(store, state, task_id)
        packet["branchReviewAxes"] = list(integration.AXES)
        if task_tiers.worker_only(state["taskHistory"][task_id][-1]):
            packet["projectReviewRole"] = "integration-reviewer"
            packet["projectReviewSeparate"] = True
    if kind == "task" and state.get("ownerReferences"):
        ids = {rid for task in selected for rid in task["requirements"]}
        packet["ownerReferences"] = [ref for ref in state["ownerReferences"] if set(ref["requirements"]) & ids]
    return packet


ON_DEMAND = ("briefs", "spec", "discovery", "deliveryPlan", "briefMap", "featureScenarios", "interfaceContract",
             "knowledgeHints", "projectContext", "carryover")


def _focused_critic(store, state, packet, task, task_id, role, attempt):
    """Focused critic packet: the attempt's own diff (or the repair diff), and criteria files on demand."""
    from . import focused_review
    repair = attempt.get("repair")
    # The final task's critic reads its own attempt too: the branch belongs to the lens reviewers.
    if role == "critic2" and repair and attempt.get("secondRound") is not True:
        packet["zoneChanges"] = focused_review.attempt_changes(store, state, task_id, attempt,
                                                               attempt.get("repairBaseline"), "ремонта")
        packet["zoneChangesScope"] = "repair"
    else:
        packet["zoneChanges"] = focused_review.attempt_changes(store, state, task_id, attempt, attempt.get("lightBaseline"))
        packet["zoneChangesScope"] = "attempt"
        if role == "critic2" and repair and attempt.get("repairBaseline"):
            # max: the second review sees the whole attempt with the repair diff marked.
            packet["repairChanges"] = focused_review.attempt_changes(store, state, task_id, attempt,
                                                                     attempt["repairBaseline"], "ремонта")
    if role == "critic2" and attempt.get("rounds"):
        first = attempt["rounds"][0]
        packet["repairFindingIds"] = sorted(review_policy.required_ids(first, review_policy.mode(attempt)))
        packet["repairFindings"] = [f for f in first.get("findings", []) if f["id"] in set(packet["repairFindingIds"])]
    packet["criteriaFiles"] = focused_review.criteria_files(store, state, task)
    packet["criteriaFilesReading"] = "on-demand"
    packet["blockingClasses"] = list(focused_review.BLOCKING_CLASSES)
    packet["onDemand"] = [key for key in ON_DEMAND if key in packet]
    packet["documentReadingRule"] = FOCUSED_CRITIC_READING


def write(store, packet, *, maximum=MAX_PACKET_BYTES):
    require(type(maximum) is int and maximum >= 2000, "Слишком малый размер пакета")
    envelope = dict(packet)
    original_bytes = len(encode(packet))
    sections = {}
    # Each reference is smaller than its payload. Never discard even one check.
    movable = sorted((key for key in packet if key not in ("kind", "basis", "autonomy")),
                     key=lambda key: len(encode(packet[key])), reverse=True)
    for key in movable:
        if len(encode(envelope)) <= maximum:
            break
        if len(encode(packet[key])) < 500:
            continue
        reference = _context_artifact(store, packet[key])
        sections[key] = {**reference, "absolutePath": str(store.run / reference["file"])}
        envelope.pop(key)
        envelope["sections"] = sections
        envelope["readingRule"] = "Open every referenced section needed for the role. All expectedChecks, original briefs and constraints remain mandatory; no summary replaces them."
    require(len(encode(envelope)) <= maximum, "Не удалось вместить указатели контекста; пакет не усечён")
    artifact = _context_artifact(store, envelope)
    return {**artifact, "basis": packet["basis"], "absolutePath": str(store.run / artifact["file"]),
            "envelopeBytes": len(encode(envelope)), "inlineBytesBefore": original_bytes,
            "sectionCount": len(sections), "tokenCount": None}


def write_task_bundle(store, state, pipeline, task_id, role, *, source_paths=None):
    """Prepare addressed inputs only; assignment and report submission stay separate."""
    from . import task_reports
    from .contracts import task_verified
    # Validate the actual assignment and lifecycle before creating any files.
    report = task_reports.template(pipeline, task_id, role)
    if role == "worker":
        require(task_verified(store, state, task_id),
                "Форма worker в пакете требует успешной адресной проверки текущей задачи")
    packet = build(store, state, pipeline, "task", task_id, role, source_paths=source_paths)
    require(packet["basis"] == report["basis"], "Кандидат изменился во время подготовки пакета")
    card = packet["roles"][role]
    return {"kind": "task-handoff", "task": task_id, "role": role, "actor": report["reviewer"],
            "card": {"file": card["file"], "sha256": card["sha256"],
                     "absolutePath": str(confined(store.run, card["file"]))},
            "context": write(store, packet),
            "template": task_reports.write(store, report, task_id, role)}


def record_usage(store, state, value):
    return telemetry.record(store, state, value)


def usage_summary(store, state):
    return telemetry.summary(store, state)
