"""Native participant identities across AISpa phases.

Task actors and the knowledge writer retain their original authoritative stores.
This module resolves them together with general phase participants; it never
claims that a declared native identity was authenticated by the host.
"""
import json
import copy

from .contracts import artifact_valid, require
from .store import Invalid, digest, timestamp


# Approved classification snapshot, 2026-10-27. This is policy, not a claim of
# host availability or of which model served a request. New IDs require review;
# incoming family/strong labels and prefix matches never extend this catalogue.
MODEL_CATALOGUE_DATE = "2026-10-27"
MODEL_CATALOGUE_SOURCES = {
    "openai": "native Codex host tool schema (gpt-6-sol/gpt-6.1-sol/gpt-6-luna/gpt-6.1-terra)",
    "anthropic": "https://code.claude.com/docs/en/model-config",
    "google": "https://ai.google.dev/gemini-api/docs/models",
}
MODEL_CATALOGUE = {
    "openai": {"gpt-6-sol": ("sol", True), "gpt-6.1-sol": ("sol", True),
               "gpt-6-luna": ("luna", False), "gpt-6.1-terra": ("terra", True)},
    "anthropic": {"claude-opus-5-5": ("opus", True), "claude-sonnet-5-5": ("sonnet", False)},
    "google": {"gemini-4-argon": ("argon", True), "gemini-3.1-pro-preview": ("pro", True),
               "gemini-3.8-flash": ("flash", False)},
}
HOST_PROVIDERS = {"codex": "openai", "claude-code": "anthropic", "gemini": "google"}
MULTI_PROVIDER_HOSTS = frozenset({"cursor", "openclaw", "antigravity"})


def _provider_allowed(runtime, provider):
    return (runtime in HOST_PROVIDERS and HOST_PROVIDERS[runtime] == provider
            or runtime in MULTI_PROVIDER_HOSTS and provider in MODEL_CATALOGUE)


def _strong_model(runtime, provider, canonical):
    return (_provider_allowed(runtime, provider)
            and MODEL_CATALOGUE.get(provider, {}).get(canonical, (None, False))[1])


def model_advice(runtime, need):
    """Provider-specific preference only; a packet has no live host catalogue."""
    provider = HOST_PROVIDERS.get(runtime)
    families = {"openai": {"complex": ["terra"], "ordinary": ["terra"], "mechanical": ["terra"]},
                "anthropic": {"complex": ["opus"], "ordinary": ["sonnet"], "mechanical": ["sonnet"]},
                "google": {"complex": ["argon", "pro"], "ordinary": ["flash"], "mechanical": ["flash"]}}
    return {"runtime": runtime, "provider": provider,
            "preferredFamilies": families.get(provider, {}).get(need, []), "availability": "unknown"}


def _present(value):
    return isinstance(value, str) and bool(value.strip())


def _resolution(store, state, value, capability, request, runtime, native_id, session, model, *, planned):
    resolutions = capability.get("resolutions")
    source = capability.get("resolutionSource")
    if resolutions is None and source is None:
        return {}
    require(isinstance(resolutions, dict) and resolutions and model in resolutions
            and all(_present(k) and _present(v) for k, v in resolutions.items())
            and isinstance(source, dict) and artifact_valid(store, source),
            "Model: alias resolution source отсутствует или изменён")
    binding = store.json(source)
    phase = "planned" if planned else "actual"
    expected = {"schema": 1, "phase": phase, "run": state["run"], "runtime": runtime,
                "provider": capability.get("provider", HOST_PROVIDERS.get(runtime)), "session": session,
                "nativeId": None if planned else native_id, "callId": request.get("callId"),
                "intentId": request.get("intentId"), "resolutions": resolutions,
                "scope": "native-subagent", "authority": "host-effective-model-resolution"}
    require(isinstance(binding, dict) and all(binding.get(k) == v for k, v in expected.items())
            and value.get("phase") == phase, "Model: alias resolution scope/authority/identity mismatch")
    return resolutions


def _model_evidence(store, state, evidence, runtime, native_id, session, model, reasoning, *, planned=False):
    require(isinstance(evidence, dict) and set(evidence) == {"source"}
            and artifact_valid(store, evidence["source"]), "Model: source evidence отсутствует или изменён")
    value = store.json(evidence["source"])
    require(isinstance(value, dict) and value.get("schema") == 1 and value.get("run") == state["run"]
            and value.get("runtime") == runtime and value.get("session") == session
            and value.get("nativeId") == (None if planned else native_id)
            and value.get("phase", "planned" if planned else "actual") == ("planned" if planned else "actual"),
            "Model: чужие run/runtime/session/native identity")
    capability, request = value.get("capability"), value.get("request")
    require(isinstance(capability, dict) and capability.get("source") == "host-api-schema"
            and isinstance(capability.get("parameters"), list) and isinstance(capability.get("models"), list)
            and _present(capability.get("tool"))
            and isinstance(request, dict) and request.get("tool") == capability.get("tool")
            and (_present(request.get("callId")) or planned and request.get("callId") is None
                 and _present(request.get("intentId")))
            and isinstance(request.get("arguments"), dict), "Model: capability/request binding отсутствует")
    require(request.get("intentId") is None or _present(request["intentId"]), "Model: invalid planned intent ID")
    provider = capability.get("provider", HOST_PROVIDERS.get(runtime))
    require(provider is None or _present(provider), "Model: invalid provider")
    resolutions = _resolution(store, state, value, capability, request, runtime, native_id, session, model, planned=planned)
    canonical = resolutions.get(model, model if model in MODEL_CATALOGUE.get(provider, {}) else None)
    args = request["arguments"]
    observed = value.get("observed")
    explicit = ("model" in capability["parameters"] and model in capability["models"]
                and args.get("model") == model and model is not None)
    if reasoning is not None and "reasoning_effort" in capability["parameters"]:
        require(args.get("reasoning_effort") == reasoning, "Model: reasoning argument mismatch")
    if planned:
        require(value.get("response") is None and value.get("planSource") is None,
                "Model: planned intent не является native response")
    else:
        response = value.get("response")
        require(_present(native_id) and isinstance(response, dict) and response.get("ok") is True
                and response.get("callId") == request["callId"] and response.get("nativeId") == native_id,
                "Model: нет успешного source-bound native response")
        if value.get("planSource") is not None:
            plan = _model_evidence(store, state, {"source": value["planSource"]}, runtime, None, session,
                                   model, reasoning, planned=True)
            selection = plan["selection"]
            require(plan["provider"] == provider and plan["resolvedRequestedModel"] == canonical
                    and selection["tool"] == capability["tool"]
                    and selection["mode"] == "explicit" and explicit
                    and selection["reasoningSupport"] == ("supported" if "reasoning_effort" in capability["parameters"] else "unsupported")
                    and selection["intentId"] == (request.get("intentId") or request["callId"]),
                    "Model: actual request не соответствует source-bound planned intent")
    require(explicit or isinstance(observed, dict), "Model: explicit parameter/model unsupported")
    actual = actual_reasoning = actual_canonical = None
    status = "unknown"
    if observed is not None:
        require(not planned and isinstance(observed, dict) and observed.get("source") == "provider-metadata"
                and isinstance(observed.get("model"), str) and observed["model"].strip(),
                "Model: actual требует связанного provider metadata")
        actual, actual_reasoning = observed["model"], observed.get("reasoning")
        require(actual_reasoning is None or isinstance(actual_reasoning, str) and actual_reasoning.strip(),
                "Model: invalid observed reasoning")
        actual_canonical = resolutions.get(actual, actual if actual in MODEL_CATALOGUE.get(provider, {}) else None)
        status = "mismatch" if model is not None and (actual_canonical or actual) != (canonical or model) else "observed"
    return {"requestedModel": model, "resolvedRequestedModel": canonical, "provider": provider,
            "requestedReasoning": reasoning, "resolvedActualModel": actual_canonical,
            "actualModel": actual, "actualReasoning": actual_reasoning, "actualModelStatus": status,
            "actualModelSource": copy.deepcopy(evidence["source"]) if observed else None,
            "modelEvidence": copy.deepcopy(evidence),
            "selection": {"mode": "explicit" if explicit else "unsupported",
                          "status": "mismatch" if status == "mismatch" else "planned" if planned else "accepted" if explicit else "unsupported",
                          "tool": capability["tool"], "callId": None if planned else request["callId"],
                          "intentId": (request.get("intentId") or request.get("callId")) if planned else request.get("intentId"),
                          "source": copy.deepcopy(evidence["source"]),
                          "nativeId": None if planned else native_id,
                          "reasoningSupport": "supported" if "reasoning_effort" in capability["parameters"] else "unsupported"}}


def model_metadata(store, state, runtime, native_id, session, *, model=None, reasoning=None, evidence=None):
    require(model is None or isinstance(model, str) and model.strip(), "Model: invalid requested ID")
    require(reasoning is None or isinstance(reasoning, str) and reasoning.strip(), "Model: invalid requested reasoning")
    if evidence is not None:
        return _model_evidence(store, state, evidence, runtime, native_id, session, model, reasoning)
    return {"requestedModel": model, "resolvedRequestedModel": None, "provider": None,
            "requestedReasoning": reasoning, "actualModel": None, "resolvedActualModel": None,
            "actualReasoning": None, "actualModelStatus": "unknown", "actualModelSource": None,
            "selection": {"mode": "advisory", "status": "planned", "reasoningSupport": "unknown"}}


def launch_plan(store, state, *, runtime, session, model=None, reasoning=None, evidence=None, sensitive=False):
    require(isinstance(session, str) and session.strip(), "Model: нужна реальная session")
    require(model is None or _present(model), "Model: invalid requested ID")
    require(reasoning is None or _present(reasoning), "Model: invalid requested reasoning")
    try:
        value = (_model_evidence(store, state, evidence, runtime, None, session, model, reasoning, planned=True)
                 if evidence is not None else model_metadata(store, state, runtime, None, session, model=model, reasoning=reasoning))
    except Invalid as error:
        from .verified_reads import ResourceLimit
        if sensitive and not isinstance(error, ResourceLimit):
            raise Invalid("BLOCKED_MODEL_CAPABILITY: " + str(error)) from error
        raise
    if sensitive:
        require(_strong_model(runtime, value["provider"], value["resolvedRequestedModel"])
                and value["selection"]["mode"] == "explicit",
                "BLOCKED_MODEL_CAPABILITY: нет поддержанного явного сильного маршрута до запуска")
    return value


def require_model_route(store, state, actor, *, sensitive=False):
    if not state.get("policy", {}).get("managedExecution") or not sensitive:
        return
    require(isinstance(actor, dict) and actor.get("actualModelStatus") != "mismatch",
            "BLOCKED_MODEL_CAPABILITY: наблюдённый маршрут не совпал с запросом")
    require(actor.get("modelEvidence"), "BLOCKED_MODEL_CAPABILITY: sensitive work требует source-bound host evidence")
    try:
        current = _model_evidence(store, state, actor["modelEvidence"], actor["runtime"], actor["nativeId"], actor["session"],
                                  actor.get("requestedModel"), actor.get("requestedReasoning"))
    except Invalid as error:
        from .verified_reads import ResourceLimit
        if isinstance(error, ResourceLimit):
            raise
        raise Invalid("BLOCKED_MODEL_CAPABILITY: " + str(error)) from error
    require(current["actualModelStatus"] != "mismatch", "BLOCKED_MODEL_CAPABILITY: наблюдённый маршрут не совпал с запросом")
    actual = (current["actualModelStatus"] == "observed"
              and _strong_model(actor["runtime"], current["provider"], current["resolvedActualModel"]))
    selection = current["selection"]
    selected = (selection.get("mode") == "explicit" and selection.get("status") == "accepted"
                and _strong_model(actor["runtime"], current["provider"], current["resolvedRequestedModel"]))
    require(actual or selected, "BLOCKED_MODEL_CAPABILITY: sensitive work требует допустимый сильный маршрут")


def _model_view(store, state, actor):
    result = dict(actor)
    if "requestedModel" not in result:
        result.update(model_metadata(store, state, actor["runtime"], actor["nativeId"], actor["session"], model=actor.get("model")))
    if actor.get("modelEvidence"):
        result.update(_model_evidence(store, state, actor["modelEvidence"], actor["runtime"], actor["nativeId"], actor["session"],
                      actor.get("requestedModel"), actor.get("requestedReasoning")))
    for row in state.get("modelObservations", []):
        if row.get("actor") != actor["id"]:
            continue
        require(artifact_valid(store, row.get("record", {})) and store.json(row["record"]) ==
                {k: v for k, v in row.items() if k != "record"}, "Model: observation record изменён")
        observed = _model_evidence(store, state, row["evidence"], actor["runtime"], actor["nativeId"], actor["session"],
                                   result.get("requestedModel"), result.get("requestedReasoning"))
        # A later observation cannot silently reinterpret an already bound
        # request's provider/configuration as a different canonical selection.
        if (result.get("provider") is not None and observed["provider"] != result["provider"]
                or result.get("resolvedRequestedModel") is not None
                and observed["resolvedRequestedModel"] != result["resolvedRequestedModel"]):
            observed["actualModelStatus"] = observed["selection"]["status"] = "mismatch"
        result.update(observed)
        result["modelObservation"] = row["record"]
    return result


def observe_model(store, state, aid, evidence):
    actor = resolve(store, state, aid)
    require(actor is not None, "Model: неизвестный author")
    metadata = _model_evidence(store, state, evidence, actor["runtime"], actor["nativeId"], actor["session"],
                               actor.get("requestedModel"), actor.get("requestedReasoning"))
    require(metadata["actualModel"] is not None, "Model: observation не содержит provider metadata")
    row = {"actor": aid, "run": state["run"], "session": actor["session"], "evidence": copy.deepcopy(evidence), "at": timestamp()}
    row["record"] = store.artifact("model-observations", (json.dumps(row, sort_keys=True) + "\n").encode(), ".json")
    state.setdefault("modelObservations", []).append(row)
    return _model_view(store, state, actor)


ROLES = frozenset({
    "coordinator", "idea", "source-reader", "brief-analyst", "brief-reviewer",
    "branch-reviewer", "integration-reviewer", "recovery-reviewer", "discovery", "coverage", "planner",
    "worker", "critic1", "critic2", "acceptor", "blind", "knowledge-writer",
})
INDEPENDENT_ROLES = frozenset({
    "brief-reviewer", "branch-reviewer", "integration-reviewer", "recovery-reviewer", "coverage",
    "critic1", "critic2", "acceptor", "blind", "knowledge-writer",
})
TASK_ROLES = frozenset({"worker", "critic1", "critic2", "acceptor"})


def _sealed(store, actor, field):
    ref = actor.get(field)
    if not isinstance(ref, dict) or not artifact_valid(store, ref):
        return False
    try:
        return store.json(ref) == {k: v for k, v in actor.items() if k != field}
    except (OSError, ValueError, TypeError):
        return False


def _identity(actor, aid):
    return (isinstance(actor, dict) and actor.get("id") == aid
            and aid == digest([actor.get("runtime"), actor.get("nativeId"), actor.get("session")]))


def _knowledge(state):
    memory = state.get("knowledge") or {}
    if not isinstance(memory, dict):
        return []
    history = memory.get("writerHistory", [])
    if not isinstance(history, list):
        history = []
    return [row for row in [memory.get("writer"), *history]
            if isinstance(row, dict)]


def identity_conflicts(state, runtime, native_id, *, exclude_actor=None):
    """Return IDs reusing one native identity within the same runtime.

    This checks all current-run registries without trusting a changed session
    to make a reviewer independent. It never equates IDs from different hosts.
    """
    require(isinstance(runtime, str) and runtime.strip()
            and isinstance(native_id, str) and native_id.strip(),
            "Нужны runtime и native ID для проверки повторного участника")
    runtime, native_id = runtime.strip().casefold(), native_id.strip()
    rows = [*state.get("participants", {}).items(), *state.get("actors", {}).items()]
    rows.extend((row.get("id"), row) for row in _knowledge(state))
    return sorted({aid for aid, row in rows if isinstance(aid, str) and aid != exclude_actor
                   and isinstance(row, dict) and isinstance(row.get("runtime"), str)
                   and row["runtime"].casefold() == runtime and row.get("nativeId") == native_id})


def require_unused_identity(state, runtime, native_id, *, exclude_actor=None):
    """Guard for team and knowledge assignment before creating a new role."""
    require(not identity_conflicts(state, runtime, native_id, exclude_actor=exclude_actor),
            "Native identity уже участвовала в этом прогоне")


def resolve(store, state, actor):
    """Return a validated identity, or None. No synthetic role is inferred."""
    if not isinstance(actor, str) or not actor:
        return None
    general = state.get("participants", {}).get(actor)
    if general is not None:
        if (_identity(general, actor) and general.get("run") == state.get("run")
                and _sealed(store, general, "record")):
            return _model_view(store, state, general)
        return None
    task = state.get("actors", {}).get(actor)
    if task is not None:
        if _identity(task, actor) and _sealed(store, task, "record"):
            return _model_view(store, state, task)
        return None
    for author in _knowledge(state):
        if author.get("id") == actor:
            if (_identity(author, actor) and author.get("run") == state.get("run")
                    and author.get("role") == "knowledge-writer" and _sealed(store, author, "artifact")):
                return _model_view(store, state, author)
            return None
    return None


def actor_check(store, state, actor, role):
    resolved = resolve(store, state, actor)
    if resolved and state.get("policy", {}).get("managedExecution"):
        from . import task_tiers
        try:
            require_model_route(store, state, resolved, sensitive=task_tiers.sensitive_task(state))
        except Invalid as error:
            from .verified_reads import ResourceLimit
            if isinstance(error, ResourceLimit):
                raise
            return False
    return bool(resolved and (resolved.get("role") == role or
                state.get("policy", {}).get("phaseReceipts") and resolved.get("role") == "coordinator"
                and role in {"idea", "brief-analyst", "discovery", "planner"}))


def list_participants(store, state):
    """Validated view; malformed legacy records are omitted, never repaired."""
    ids = set(state.get("participants", {})) | set(state.get("actors", {}))
    ids.update(row["id"] for row in _knowledge(state) if isinstance(row.get("id"), str))
    return [row for aid in sorted(aid for aid in ids if isinstance(aid, str))
            if (row := resolve(store, state, aid)) is not None]


def register(store, state, role, runtime, native_id, session, *, model=None,
             external=False, fresh=True, reasoning=None, model_evidence=None):
    """Register one host-reported identity for a non-task phase role.

    ``fresh`` is a declaration, not proof of an independent model context.
    Unknown named roles require the explicit external flag and cannot gain the
    independent-review semantics of built-in roles.
    """
    recovery = (role == "recovery-reviewer" and state.get("status") == "paused"
                and isinstance(state.get("journalRecovery"), dict)
                and state["journalRecovery"].get("pending") is True)
    require(state.get("status") == "running" or recovery,
            "Участников назначают только в активном прогоне или при восстановлении журнала")
    require(isinstance(role, str) and bool(role.strip()), "Нужна роль участника")
    role = role.strip()
    require(role not in TASK_ROLES and role != "knowledge-writer",
            "Роли задач и автора знаний назначаются своими командами")
    require(type(external) is bool and type(fresh) is bool, "Неверные флаги участника")
    require((role in ROLES and not external) or (role not in ROLES and external),
            "Внешняя роль требует отдельного имени и явного external=true")
    require(role not in INDEPENDENT_ROLES or fresh, "Проверяющему нужен свежий контекст")
    require(all(isinstance(v, str) and v.strip() for v in (runtime, native_id, session)),
            "Нужны реальные runtime, native ID и session")
    require(model is None or isinstance(model, str) and model.strip(), "Неверный ID модели")
    runtime, native_id, session = runtime.strip().casefold(), native_id.strip(), session.strip()
    aid = digest([runtime, native_id, session])
    require(resolve(store, state, aid) is None and aid not in state.get("actors", {})
            and aid not in state.get("participants", {})
            and all(row.get("id") != aid for row in _knowledge(state)),
            "Native identity уже зарегистрирована")
    # The same native ID in a different session still names the same host actor;
    # do not let that make a reviewer appear independent.
    require_unused_identity(state, runtime, native_id)
    record = {"id": aid, "runtime": runtime, "nativeId": native_id, "session": session,
              "role": role, "external": external, "fresh": fresh, "model": model,
              "run": state["run"], "at": timestamp(), "identityEvidence": "host-declared"}
    record.update(model_metadata(store, state, runtime, native_id, session, model=model,
                                 reasoning=reasoning, evidence=model_evidence))
    from . import task_tiers
    require_model_route(store, state, record, sensitive=task_tiers.sensitive_task(state))
    record["record"] = store.artifact("participants", (json.dumps(record, ensure_ascii=False,
        sort_keys=True, indent=2) + "\n").encode(), ".json")
    state.setdefault("participants", {})[aid] = record
    return aid
