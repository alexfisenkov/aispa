"""Source-bound feature scenarios and pre-plan interface ownership.

These are structural contracts. Exact quotations and hashes establish origin,
not the quality of a proposed scenario or the truth of an implementation.
"""
import json
import re
from pathlib import PurePosixPath

from .contracts import active, artifact_valid, nonempty
from .store import Invalid, canonical_zone, confined, digest, file_hash


SCHEMA = 1
DIMENSIONS = ("first", "main", "empty", "error", "retry", "access", "persistence", "effects")
DEPTHS = ("strict", "balanced", "deep")
# Lean runs scale the mandatory scenario dimensions with the chosen depth.
LEAN_REQUIRED = {"strict": ("main", "error"), "balanced": ("main", "empty", "error", "persistence"),
                 "deep": DIMENSIONS}


def required_dimensions(state, depth=None):
    depth = depth or state.get("depth", "balanced")
    if state.get("policy", {}).get("leanControl") is True:
        return LEAN_REQUIRED.get(depth, DIMENSIONS)
    return DIMENSIONS
MAX_REPORT_CHARS = 512000
MAX_FEATURES = 256
MAX_INTERFACES = 256


def safe_reference_name(name):
    from .discovery import SECRET_NAME
    path = PurePosixPath(name)
    _need(not any(SECRET_NAME.search(part) for part in path.parts)
          and path.suffix.lower() not in {".pem", ".key", ".p12", ".pfx", ".keystore", ".sqlite", ".db"},
          "секретный или личный файл не может служить материалом контракта")


def _need(ok, message):
    if not ok:
        raise Invalid("AISpa design: " + message)


def _text(value, label, limit=1600):
    _need(nonempty(value) and len(value) <= limit, label + ": нужен конкретный текст")
    return value


def _source_map(store, state):
    result = {}
    for ref in state["briefs"]:
        _need(artifact_valid(store, ref), "исходный бриф изменён")
        result[ref["id"]] = store.text(ref)
    return result


def _spec(store, state):
    ref = state.get("spec")
    _need(isinstance(ref, dict) and artifact_valid(store, ref), "нужна текущая спецификация")
    return store.text(ref)


def scenario_basis(state):
    return digest({"run": state["run"], "briefs": state["briefs"],
                   "requirements": state["requirements"], "spec": state.get("spec"),
                   **({"depth": state["depth"]} if "depth" in state else {})})


def interface_basis(state):
    return scenario_basis(state)


def _size(report):
    try:
        size = len(json.dumps(report, ensure_ascii=False, allow_nan=False))
    except (ValueError, TypeError, RecursionError) as exc:
        raise Invalid("AISpa design: нужен корректный JSON") from exc
    _need(size <= MAX_REPORT_CHARS, "слишком большой отчёт")


def _features(store, state, report):
    _need(isinstance(report, dict) and {"basis", "depth", "depthReason", "features"} <= set(report)
          <= {"basis", "depth", "depthReason", "features", "noFeatureReason"},
          "сценарии требуют basis, depth, depthReason, features")
    _need(report["basis"] == scenario_basis(state), "сценарии устарели")
    _need(report["depth"] in DEPTHS, "depth: strict, balanced или deep")
    _need("depth" not in state or report["depth"] == state["depth"],
          "глубина сценариев должна совпадать с выбранной глубиной прогона")
    _text(report["depthReason"], "причина глубины", 600)
    spec = _spec(store, state)
    sources = _source_map(store, state)
    reqs = {r["id"]: r for r in active(state)}
    expected = {rid: r for rid, r in reqs.items() if r.get("kind", "requested") in ("requested", "addition")}
    rows = report["features"]
    _need(isinstance(rows, list) and len(rows) <= MAX_FEATURES,
          "нужен ограниченный список действующих функций")
    if not rows:
        _need(not expected and "noFeatureReason" in report,
              "пустые сценарии допустимы только без действующих функций и с объяснением")
        _text(report["noFeatureReason"], "почему нет функций")
    else:
        _need("noFeatureReason" not in report, "объяснение отсутствия функций противоречит списку")
    seen = set()
    for row in rows:
        _need(isinstance(row, dict) and set(row) ==
              {"requirement", "changeKind", "sourceQuote", "specQuote", "dimensions"},
              "карточке функции нужны источник, спецификация и сценарии по глубине")
        rid = row["requirement"]
        _need(isinstance(rid, str) and rid in expected and rid not in seen,
              "неизвестная, повторная или неактивная функция")
        seen.add(rid)
        req = expected[rid]
        kind = req.get("kind", "requested")
        _need(row["changeKind"] == kind, "дополнение нельзя представить уточнением исходного запроса")
        if report["depth"] == "strict":
            _need(kind != "addition", "strict запрещает включать инициативу агента")
        anchor = req if kind == "requested" else reqs.get(req.get("parent"))
        _need(isinstance(anchor, dict) and anchor.get("brief") in sources,
              "функция не связана с исходным брифом")
        quote = _text(row["sourceQuote"], "цитата исходного брифа")
        _need(quote == anchor.get("quote") and quote in sources[anchor["brief"]],
              "цитата должна совпадать с зарегистрированным источником")
        _need(_text(row["specQuote"], "цитата спецификации", 3000) in spec,
              "сценарии не привязаны к действующей спецификации")
        dimensions = row["dimensions"]
        required = set(required_dimensions(state, report["depth"]))
        _need(isinstance(dimensions, list) and len(required) <= len(dimensions) <= len(DIMENSIONS),
              "для каждой функции нужны обязательные измерения выбранной глубины")
        checked = set()
        for cell in dimensions:
            _need(isinstance(cell, dict) and set(cell) ==
                  {"name", "status", "scenario", "verification", "counterexample"},
                  "измерению нужны сценарий, проверка и контрпример")
            name = cell["name"]
            _need(isinstance(name, str) and name in DIMENSIONS and name not in checked,
                  "неизвестное или повторное измерение")
            checked.add(name)
            _need(cell["status"] in ("covered", "not_applicable"),
                  "статус измерения: covered или not_applicable")
            _text(cell["scenario"], name + ": сценарий или причина неприменимости")
            _text(cell["verification"], name + ": способ проверки")
            _need(isinstance(cell["counterexample"], str) and len(cell["counterexample"]) <= 1600,
                  "контрпример должен быть текстом")
            if report["depth"] == "deep" and cell["status"] == "covered":
                _text(cell["counterexample"], name + ": контрпример для deep")
        _need(required <= checked, "сценарии функции неполны")
    _need(seen == set(expected), "не все действующие функции получили сценарии")
    _size(report)


def _save(store, state, report, key, history, group, extra=None):
    if state.get(key) and extra is None:
        try:
            if _saved(store, state, key, history, group) == report:
                return state[key]
        except (OSError, ValueError, TypeError, KeyError):
            pass
    previous = state.get(history, [])
    _need(isinstance(previous, list), "повреждена история: " + history)
    data = {"schema": SCHEMA, "run": state["run"], "report": report, **(extra or {})}
    ref = {**store.artifact(group, (json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n").encode(), ".json"),
           "basis": report["basis"]}
    state[key] = ref
    state[history] = [*previous, dict(ref)]
    return ref


def _saved(store, state, key, history, group):
    ref = state.get(key)
    _need(isinstance(ref, dict) and isinstance(state.get(history), list)
          and state[history] and state[history][-1] == ref, "нет актуальной истории: " + key)
    _need(isinstance(ref.get("file"), str) and ref["file"].startswith(group + "/")
          and artifact_valid(store, ref), "отчёт изменён: " + key)
    payload = json.loads(store.text(ref))
    _need(isinstance(payload, dict) and payload.get("schema") == SCHEMA
          and payload.get("run") == state["run"] and isinstance(payload.get("report"), dict),
          "чужая или повреждённая схема: " + key)
    return payload["report"]


def record_scenarios(store, state, report):
    _features(store, state, report)
    return _save(store, state, report, "featureScenarios", "featureScenarioHistory", "feature-scenarios")


def valid_scenarios(store, state):
    try:
        _features(store, state, _saved(store, state, "featureScenarios", "featureScenarioHistory", "feature-scenarios"))
        return True
    except (Invalid, OSError, ValueError, TypeError, KeyError, RecursionError):
        return False


def _path_ref(store, row, check_sources=True):
    _need(isinstance(row, dict) and set(row) == {"file", "sha256"},
          "указатель материала требует путь и SHA-256")
    file = canonical_zone(row["file"])
    safe_reference_name(file)
    _need(isinstance(row["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]),
          "материалу нужен SHA-256")
    if not check_sources:
        return
    path = confined(store.root, file)
    _need(path.is_file() and file_hash(path) == row["sha256"],
          "изменён исходный материал: " + file)


def _interfaces(store, state, report, check_sources=True):
    _need(isinstance(report, dict) and {"basis", "interfaces"} <= set(report)
          <= {"basis", "interfaces", "noInterfaceReason"}
          and report["basis"] == interface_basis(state), "контракт интерфейсов устарел или неполон")
    _spec(store, state)
    _source_map(store, state)
    active_ids = {r["id"] for r in active(state)}
    rows = report["interfaces"]
    _need(isinstance(rows, list) and len(rows) <= MAX_INTERFACES,
          "до плана нужен ограниченный список интерфейсов")
    if not rows:
        _need("noInterfaceReason" in report, "объясните, почему контракту не нужны интерфейсы")
        _text(report["noInterfaceReason"], "почему нет интерфейсов")
    else:
        _need("noInterfaceReason" not in report, "объяснение отсутствия интерфейсов противоречит списку")
    names = set()
    for row in rows:
        _need(isinstance(row, dict) and set(row) ==
              {"name", "signature", "ownerZone", "consumers", "testSeam", "requirements", "references", "referenceReason"},
              "интерфейсу нужны имя, сигнатура, зоны, тест и ссылки")
        name = _text(row["name"], "имя интерфейса", 160)
        _need(bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]*", name)) and name not in names,
              "имя интерфейса должно быть точным и уникальным")
        names.add(name)
        _text(row["signature"], "точная сигнатура", 1000)
        _text(row["testSeam"], "тестовый шов", 1600)
        owner = canonical_zone(row["ownerZone"])
        consumers = row["consumers"]
        _need(isinstance(consumers, list) and all(isinstance(z, str) for z in consumers)
              and len(consumers) == len(set(consumers)) and len(consumers) <= 64,
              "потребители должны быть списком зон")
        _need(all(canonical_zone(z) != owner for z in consumers), "владелец не является внешним потребителем")
        ids = row["requirements"]
        _need(isinstance(ids, list) and ids and all(isinstance(i, str) and i in active_ids for i in ids)
              and len(ids) == len(set(ids)),
              "интерфейс должен быть привязан к действующим требованиям")
        refs = row["references"]
        _need(isinstance(refs, list) and len(refs) <= 32, "слишком много указателей материалов")
        for ref in refs:
            _path_ref(store, ref, check_sources)
        _text(row["referenceReason"], "основание выбора материалов или их отсутствия", 1000)
    _size(report)


def record_interfaces(store, state, report):
    _interfaces(store, state, report)
    import hashlib
    from .discovery import SECRET_CONTENT
    snapshots, total = {}, 0
    for item in report["interfaces"]:
        for source in item["references"]:
            key = (source["file"], source["sha256"])
            if key in snapshots:
                continue
            path = confined(store.root, source["file"])
            _need(path.stat().st_size <= 5 * 1024 * 1024, "материал интерфейса превышает 5 MiB")
            raw = path.read_bytes()
            total += len(raw)
            _need(total <= 20 * 1024 * 1024 and len(snapshots) < 64, "слишком много материалов интерфейса")
            _need(hashlib.sha256(raw).hexdigest() == source["sha256"], "материал изменился во время чтения")
            _need(not SECRET_CONTENT.search(raw.decode("utf-8", "replace")), "материал содержит возможный секрет")
            snapshots[key] = {**source, "snapshot": store.artifact("interface-references", raw, path.suffix or ".bin")}
    return _save(store, state, report, "interfaceContract", "interfaceContractHistory", "interface-contracts",
                 {"referenceSnapshots": list(snapshots.values())})


def reference_snapshots(store, state):
    report = _saved(store, state, "interfaceContract", "interfaceContractHistory", "interface-contracts")
    payload = json.loads(store.text(state["interfaceContract"]))
    rows = payload.get("referenceSnapshots", [])
    expected = {(ref["file"], ref["sha256"]) for item in report["interfaces"] for ref in item["references"]}
    _need(isinstance(rows, list) and len(rows) == len(expected), "снимки материалов интерфейса неполны")
    seen = set()
    for row in rows:
        _need(isinstance(row, dict) and set(row) == {"file", "sha256", "snapshot"}, "повреждён снимок материала")
        key = (row["file"], row["sha256"])
        _need(key in expected and key not in seen and artifact_valid(store, row["snapshot"])
              and row["snapshot"]["sha256"] == row["sha256"], "материал интерфейса повреждён")
        seen.add(key)
    return rows


def valid_interfaces(store, state, *, check_sources=True):
    try:
        _interfaces(store, state, _saved(store, state, "interfaceContract", "interfaceContractHistory", "interface-contracts"),
                    check_sources)
        reference_snapshots(store, state)
        return True
    except (Invalid, OSError, ValueError, TypeError, KeyError, RecursionError):
        return False


def _contains(zone, path):
    return zone in ("", ".") or zone == path or path.startswith(zone + "/")


def validate_plan_bindings(store, state, tasks):
    """Validate the proposed plan before writing it; returns a small read-back."""
    report = _saved(store, state, "interfaceContract", "interfaceContractHistory", "interface-contracts")
    _interfaces(store, state, report)
    _need(isinstance(tasks, list) and tasks, "нужен план задач для привязки интерфейсов")
    indexed = {t["id"]: t for t in tasks}
    _need(len(indexed) == len(tasks), "повторный ID задачи")
    def ancestors(task):
        seen, pending = set(), list(task["dependsOn"])
        while pending:
            tid = pending.pop()
            _need(tid in indexed, "неизвестная зависимость плана")
            if tid not in seen:
                seen.add(tid)
                pending.extend(indexed[tid]["dependsOn"])
        return seen

    def first_owner(matches, label):
        # A later integration/repair task may read or write the whole tree.
        # It does not replace the earliest owner of this interface.
        roots = [t for t in matches if not any(
            other["id"] in ancestors(t) for other in matches if other is not t)]
        _need(len(roots) == 1, label + " должен иметь одного первоначального исполнителя")
        return roots[0]
    bindings = []
    for item in report["interfaces"]:
        owner = canonical_zone(item["ownerZone"])
        providers = [t for t in tasks if any(_contains(canonical_zone(z), owner) for z in t["zones"])]
        provider = first_owner(providers, "интерфейс " + item["name"])
        _need(set(item["requirements"]) <= set(provider["requirements"]),
              "исполнитель интерфейса не несёт его требований: " + item["name"])
        consumers = []
        for zone in item["consumers"]:
            zone = canonical_zone(zone)
            writers = [t for t in tasks if any(_contains(canonical_zone(z), zone) for z in t["zones"])]
            matches = writers or [t for t in tasks if any(_contains(canonical_zone(z), zone)
                                                        for z in t.get("readZones", []))]
            consumer = first_owner(matches, "потребитель интерфейса")
            if consumer["id"] != provider["id"]:
                visited, pending = set(), list(consumer["dependsOn"])
                while pending:
                    tid = pending.pop()
                    _need(tid in indexed, "неизвестная зависимость плана")
                    if tid not in visited:
                        visited.add(tid)
                        pending.extend(indexed[tid]["dependsOn"])
                _need(provider["id"] in visited, "потребитель должен зависеть от владельца интерфейса")
            consumers.append(consumer["id"])
        bindings.append({"interface": item["name"], "provider": provider["id"],
                         "consumers": consumers})
    return bindings


def view(store, state):
    return {"scenarioBasis": scenario_basis(state), "scenariosRecorded": bool(state.get("featureScenarios")),
            "scenariosValid": valid_scenarios(store, state),
            "interfaceBasis": interface_basis(state), "interfacesRecorded": bool(state.get("interfaceContract")),
            "interfacesValid": valid_interfaces(store, state),
            "assessment": "source-and-structure-only"}
