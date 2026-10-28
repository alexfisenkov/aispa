"""modelRoutes (0.5.0): the engine itself refuses subagent models and efforts off the route table.

Fixtures describe synthetic host evidence; no model or provider is ever called.
"""
import argparse
import json
from pathlib import Path
import shlex
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
import test_team
from test_participants import host_model_source
from aispa_core import controls, knowledge, model_routes, participants, task_tiers
from aispa_core.contracts import validate_plan
from aispa_core.store import Invalid

ROUTED = {"managedExecution": True, "taskTiers": True, "adaptiveCritique": True, "modelRoutes": True}
FORBIDDEN = ("fable", "best", "claude-fable-5-1", "gpt-6-astra", "my-Fable-preview", "gpt-6-astra-mini",
             "ASTRA", "inherit")
APPROVED = {"claude-code": ("claude-opus-5-5", "high"), "codex": ("gpt-6.1-sol", "high")}


class RoutedFixture(unittest.TestCase):
    setUp_team = test_team.TeamTests.setUp
    tearDown = test_team.TeamTests.tearDown
    task = test_team.TeamTests.task
    attempt = test_team.TeamTests.attempt
    report = test_team.TeamTests.report
    verify = test_team.TeamTests.verify

    def setUp(self):
        self.setUp_team()
        self.state.update(policy=dict(ROUTED), runtime="claude-code", taskHistory={}, actors={})
        self.team.prepare()
        self.serial = 0

    def register(self, role="brief-analyst", runtime="claude-code", native=None, session="host-session", **kwargs):
        self.serial += 1
        return participants.register(self.store, self.state, role, runtime, native or "native-%d" % self.serial,
                                     session, **kwargs)

    def start(self, tid="T01"):
        self.team.start_guard(tid)
        self.host.task(tid)["status"] = "running"

    def assign(self, tid, role, model, reasoning, runtime="claude-code", **kwargs):
        self.serial += 1
        return self.team.assign(tid, role, runtime, "%s-%s-%d" % (tid, role, self.serial), "session",
                                model=model, reasoning=reasoning, **kwargs)


class CatalogueTests(unittest.TestCase):
    def test_known_but_forbidden_and_haiku_are_catalogued_and_future_ids_are_nothing(self):
        self.assertEqual(participants.MODEL_CATALOGUE["openai"]["gpt-6-astra"][0], "astra")
        self.assertEqual(participants.MODEL_CATALOGUE["anthropic"]["claude-fable-5-1"][0], "fable")
        self.assertEqual(participants.MODEL_CATALOGUE["anthropic"]["claude-haiku-5-5"][0], "haiku")
        self.assertEqual(participants.SUBAGENT_FORBIDDEN_FAMILIES, frozenset({"fable", "astra"}))
        for runtime, provider, future in (("codex", "openai", "gpt-7-terra"), ("claude-code", "anthropic", "claude-opus-6")):
            self.assertFalse(participants._strong_model(runtime, provider, future))
            for cls in model_routes.CLASSES:
                if cls == "coordinator":
                    continue
                role = "worker" if cls in model_routes.WORKER_CLASSES else next(
                    r for r, c in model_routes.ROLE_CLASS.items() if c == cls)
                if cls != "worker" and cls in model_routes.WORKER_CLASSES:
                    continue  # worker subclasses are covered with a real task below
                effort = model_routes.ROUTES[cls][runtime]["effort"]
                with self.subTest(runtime=runtime, cls=cls), self.assertRaisesRegex(Invalid, "нужна"):
                    model_routes.check_assignment({"runtime": runtime, "policy": {"modelRoutes": True}}, role, runtime,
                                                  future, effort, session="s", native_id="n")


class RefusalTests(RoutedFixture):
    def test_every_subagent_path_refuses_forbidden_models_efforts_and_missing_fields(self):
        paths = {
            "participant": lambda m, r: self.register(model=m, reasoning=r),
            "assign": lambda m, r: self.assign("T01", "worker", m, r),
            "knowledge-assign": lambda m, r: knowledge.assign(self.store, self.state, "claude-code",
                                                              "writer-%d" % id(m), "host-session", model=m, reasoning=r),
            "reserve": lambda m, r: participants.launch_plan(self.store, self.state, runtime="claude-code",
                                                             session="host-session", model=m, reasoning=r,
                                                             role="critic1"),
        }
        self.start()
        for name, call in paths.items():
            for model in FORBIDDEN:
                with self.subTest(path=name, model=model), self.assertRaisesRegex(Invalid, "запрещ"):
                    call(model, "high")
            for effort in ("xhigh", "max", "ultra", "minimal"):
                with self.subTest(path=name, effort=effort), self.assertRaisesRegex(Invalid, "low, medium или high"):
                    call("claude-opus-5-5", effort)
            for model, effort in ((None, "high"), ("claude-opus-5-5", None), (None, None)):
                with self.subTest(path=name, model=model, effort=effort), \
                        self.assertRaisesRegex(Invalid, "--model.*--reasoning"):
                    call(model, effort)
        self.assertEqual(self.state.get("participants", {}), {})
        self.assertEqual(self.state.get("knowledge", {}).get("writer"), None)

    def test_off_table_model_or_effort_names_the_required_route(self):
        self.start()
        cases = (("critic1", "claude-code", "claude-sonnet-5-5", "high", "claude-opus-5-5 · high"),
                 ("critic1", "claude-code", "claude-opus-5-5", "medium", "claude-opus-5-5 · high"),
                 ("acceptor", "codex", "gpt-6-sol", "high", "gpt-6.1-sol · high"),
                 ("worker", "codex", "gpt-6-luna", "low", "gpt-6.1-terra · medium"),
                 ("worker", "claude-code", "claude-opus-5-5", "high", "claude-sonnet-5-5 · medium"))
        for role, runtime, model, effort, need in cases:
            self.state["runtime"] = runtime
            with self.subTest(role=role, runtime=runtime, model=model), self.assertRaisesRegex(
                    Invalid, "Для роли %s в среде %s нужна %s" % (role, runtime, need)):
                self.assign("T01", role, model, effort, runtime=runtime)
        with self.assertRaisesRegex(Invalid, "Для роли planner в среде codex нужна gpt-6.1-sol · high"):
            self.register("planner", "codex", model="gpt-6.1-terra", reasoning="high")

    def test_actor_runtime_outside_the_table_is_refused_in_a_routed_run(self):
        self.start()
        # The approved claude-code row would match: only the declared actor runtime is wrong.
        paths = {
            "assign": lambda rt: self.assign("T01", "critic1", "claude-opus-5-5", "high", runtime=rt),
            "participant": lambda rt: self.register("brief-reviewer", rt, model="claude-opus-5-5", reasoning="high"),
            "knowledge-assign": lambda rt: knowledge.assign(self.store, self.state, rt, "writer-" + rt, "host-session",
                                                            model="claude-opus-5-5", reasoning="medium"),
        }
        for run_runtime in ("claude-code", "codex"):
            self.state["runtime"] = run_runtime
            for name, call in paths.items():
                for foreign in ("cursor", "gemini", "openclaw"):
                    with self.subTest(run=run_runtime, path=name, runtime=foreign), self.assertRaisesRegex(
                            Invalid, "среда %s не входит в таблицу маршрутов" % foreign):
                        call(foreign)
        self.assertEqual(self.state.get("participants", {}), {})
        self.assertIsNone(self.state.get("knowledge", {}).get("writer"))
        self.assertIsNone(self.attempt()["actors"].get("critic1"))

    def test_reserve_refuses_roles_off_the_table_and_a_worker_without_its_task(self):
        def reserve(role, model, reasoning, task=None):
            return participants.launch_plan(self.store, self.state, runtime="claude-code", session="host-session",
                                            model=model, reasoning=reasoning, role=role, task=task)
        for role in ("foo", "Coordinator", " coordinator", "COORDINATOR "):
            with self.subTest(role=role), self.assertRaisesRegex(Invalid, "координатор — главный чат|Неизвестная роль"):
                reserve(role, "claude-sonnet-5-5", "low")
        with self.assertRaisesRegex(Invalid, "Неизвестная роль.*foo"):
            reserve("foo", "claude-sonnet-5-5", "low")
        with self.assertRaisesRegex(Invalid, "worker.*--task"):
            reserve("worker", "claude-sonnet-5-5", "medium")
        self.assertEqual(reserve(" Critic1 ", "claude-opus-5-5", "high")["routeClass"], "judge")
        sensitive = dict(self.host.task("T01"), zones=["src/auth/token_store.py"])
        with self.assertRaisesRegex(Invalid, "claude-opus-5-5 · high"):
            reserve("worker", "claude-sonnet-5-5", "medium", task=sensitive)
        self.assertEqual(reserve("worker", "claude-opus-5-5", "high", task=sensitive)["routeClass"], "sensitive_worker")

    def test_approved_route_is_recorded_with_class_request_and_unknown_actual(self):
        self.start()
        worker = self.assign("T01", "worker", "sonnet", "medium")
        row = participants.resolve(self.store, self.state, worker)
        self.assertEqual((row["routeClass"], row["requestedModel"], row["requestedReasoning"]),
                         ("worker", "sonnet", "medium"))
        self.assertEqual(row["actualModelStatus"], "unknown")
        aid = self.register("source-reader", "codex", model="gpt-6.1-terra", reasoning="high")
        row = participants.resolve(self.store, self.state, aid)
        self.assertEqual((row["routeClass"], row["requestedModel"], row["requestedReasoning"]),
                         ("research", "gpt-6.1-terra", "high"))
        writer = knowledge.assign(self.store, self.state, "claude-code", "writer", "host-session",
                                  model="claude-opus-5-5", reasoning="medium")
        self.assertEqual(writer["routeClass"], "research")

    def test_builtin_agent_without_effort_parameter_records_unsupported_reasoning(self):
        self.start()
        # The host tool takes a model but no effort: the request is kept, the effort is not claimed as applied.
        source = host_model_source(self.store, self.state, runtime="claude-code", provider="anthropic",
                                   requested="claude-sonnet-5-5", native="builtin-native", session="session")
        aid = self.team.assign("T01", "worker", "claude-code", "builtin-native", "session",
                               model="claude-sonnet-5-5", reasoning="medium", model_evidence=source)
        row = participants.resolve(self.store, self.state, aid)
        self.assertEqual((row["routeClass"], row["requestedReasoning"]), ("worker", "medium"))
        self.assertEqual(row["selection"]["reasoningSupport"], "unsupported")
        self.assertEqual(row["actualModelStatus"], "unknown")

    def test_runs_without_model_routes_keep_previous_participant_checks(self):
        self.state["policy"] = {"managedExecution": True, "taskTiers": True, "adaptiveCritique": True}
        aid = self.register("brief-analyst", "codex", model="gpt-6-astra", reasoning="xhigh")
        row = participants.resolve(self.store, self.state, aid)
        self.assertNotIn("routeClass", row)
        self.assertEqual(row["requestedModel"], "gpt-6-astra")
        self.start()
        self.assertTrue(self.team.assign("T01", "worker", "fixture", "legacy-worker", "session"))


class CapabilityBlockTests(RoutedFixture):
    def test_missing_approved_model_is_a_journaled_blocker_without_substitution(self):
        capability = {"models": ["claude-sonnet-5-5"], "parameters": ["model"]}
        block = model_routes.unavailable_block(self.state, "critic1", "claude-code", capability)
        self.assertEqual(block["code"], "BLOCKED_MODEL_CAPABILITY")
        self.assertEqual((block["role"], block["routeClass"], block["model"], block["effort"]),
                         ("critic1", "judge", "claude-opus-5-5", "high"))
        self.assertEqual(len(block["options"]), 3)
        self.assertIsNone(model_routes.unavailable_block(self.state, "critic1", "claude-code",
                                                         {"models": ["opus"], "parameters": ["model"]}))
        evidence = host_model_source(self.store, self.state, runtime="claude-code", provider="anthropic",
                                     requested="claude-sonnet-5-5", native="judge", session="host-session")
        with self.assertRaisesRegex(Invalid, "BLOCKED_MODEL_CAPABILITY.*claude-opus-5-5"):
            participants.register(self.store, self.state, "brief-reviewer", "claude-code", "judge", "host-session",
                                  model="claude-opus-5-5", reasoning="high", model_evidence=evidence,
                                  save=lambda event: self.store.append(self.state, event))
        saved = self.store.load()["modelBlocks"]
        self.assertEqual([(row["role"], row["model"]) for row in saved], [("brief-reviewer", "claude-opus-5-5")])
        self.assertEqual({o["id"] for o in saved[0]["options"]}, {"enable-model", "owner-allows-other", "leave-in-remaining"})
        self.assertEqual(self.state.get("participants", {}), {})

    def test_a_repeated_refusal_for_the_same_role_model_and_task_keeps_one_blocker(self):
        evidence = host_model_source(self.store, self.state, runtime="claude-code", provider="anthropic",
                                     requested="claude-sonnet-5-5", native="judge", session="host-session")
        events = []
        def save(event):
            events.append(event)
            self.store.append(self.state, event)
        for native in ("judge", "judge", " judge"):
            with self.assertRaisesRegex(Invalid, "BLOCKED_MODEL_CAPABILITY"):
                model_routes.enforce(self.store, self.state, "brief-reviewer", "claude-code", native, "host-session",
                                     "claude-opus-5-5", "high", evidence, save=save)
        self.assertEqual(len(self.store.load()["modelBlocks"]), 1)
        self.assertEqual(events, ["model-capability-blocked:brief-reviewer"])
        # Another role is a separate blocker.
        with self.assertRaisesRegex(Invalid, "BLOCKED_MODEL_CAPABILITY"):
            model_routes.enforce(self.store, self.state, "planner", "claude-code", "judge", "host-session",
                                 "claude-opus-5-5", "high", evidence, save=save)
        self.assertEqual([row["role"] for row in self.store.load()["modelBlocks"]], ["brief-reviewer", "planner"])

    class JournalFlow:
        """The saving seam of the `participant` command: every event goes to the journal and to ``events``."""

        def __init__(self, test, sources):
            self.test, self.sources, self.events = test, sources, []

        def running(self):
            pass

        def save(self, event):
            self.events.append(event)
            self.test.store.append(self.test.state, event)

        def read_input(self, name):
            return json.dumps(self.sources[name])

    def participant(self, flow, role, native, evidence):
        args = argparse.Namespace(action="participant", role=role, runtime="claude-code", native_id=native,
                                  session="host-session", model="claude-opus-5-5", reasoning="high",
                                  model_evidence=evidence)
        return controls.execute(self.store, self.state, flow, args)

    def block(self, flow, role, evidence):
        with self.assertRaisesRegex(Invalid, "BLOCKED_MODEL_CAPABILITY"):
            model_routes.enforce(self.store, self.state, role, "claude-code", "judge", "host-session",
                                 "claude-opus-5-5", "high", flow.sources[evidence], save=flow.save)

    def sources(self):
        return {name: host_model_source(self.store, self.state, runtime="claude-code", provider="anthropic",
                                        requested=model, native="judge", session="host-session")
                for name, model in (("missing", "claude-sonnet-5-5"), ("offered", "claude-opus-5-5"))}

    def test_a_later_assignment_lifts_the_blocker_with_its_own_event_and_writes_no_event_of_its_own(self):
        flow = self.JournalFlow(self, self.sources())
        for role in ("brief-reviewer", "planner"):
            self.block(flow, role, "missing")
        self.assertEqual(len(self.store.load()["modelBlocks"]), 2)
        self.participant(flow, "brief-reviewer", "judge", "offered")
        # Only the same role is lifted; the other role's blocker stays until its own assignment succeeds.
        self.assertEqual([row["role"] for row in self.store.load()["modelBlocks"]], ["planner"])
        self.assertEqual(flow.events, ["model-capability-blocked:brief-reviewer", "model-capability-blocked:planner",
                                       "participant-assigned:brief-reviewer"])
        self.assertFalse([event for event in flow.events if event.startswith("model-capability-cleared")])

    def test_an_assignment_in_another_runtime_lifts_the_blocker_of_the_same_role_and_task(self):
        # The work of the role no longer waits once it is seated, whichever host runtime seated it.
        flow = self.JournalFlow(self, self.sources())
        self.block(flow, "brief-reviewer", "missing")
        self.assertEqual(len(self.store.load()["modelBlocks"]), 1)
        codex = model_routes.ROUTES["judge"]["codex"]
        self.state["runtime"] = "codex"
        model_routes.enforce(self.store, self.state, "brief-reviewer", "codex", "codex-judge", "host-session",
                             codex["model"], codex["effort"], None)
        self.assertEqual(self.state["modelBlocks"], [])

    def test_enforce_alone_changes_state_but_saves_nothing(self):
        flow = self.JournalFlow(self, self.sources())
        self.block(flow, "brief-reviewer", "missing")
        saved = list(flow.events)
        route = model_routes.enforce(self.store, self.state, "brief-reviewer", "claude-code", "judge", "host-session",
                                     "claude-opus-5-5", "high", flow.sources["offered"], save=flow.save)
        self.assertEqual(route["model"], "claude-opus-5-5")
        self.assertEqual(self.state["modelBlocks"], [])
        self.assertEqual(flow.events, saved)
        self.assertEqual(len(self.store.load()["modelBlocks"]), 1)

    def test_an_assignment_that_fails_after_the_route_check_leaves_the_blocker_in_the_journal(self):
        flow = self.JournalFlow(self, self.sources())
        self.participant(flow, "brief-reviewer", "judge", "offered")
        self.block(flow, "brief-reviewer", "missing")
        self.assertEqual([row["role"] for row in self.store.load()["modelBlocks"]], ["brief-reviewer"])
        saved = list(flow.events)
        # The route check passes (and lifts the blocker in memory), then the same native identity is refused.
        with self.assertRaisesRegex(Invalid, "Native identity уже зарегистрирована"):
            self.participant(flow, "brief-reviewer", "judge", "offered")
        self.assertEqual(self.state["modelBlocks"], [])  # the lift did happen in memory, so the check above is not vacuous
        self.assertEqual(flow.events, saved)
        self.assertEqual([row["role"] for row in self.store.load()["modelBlocks"]], ["brief-reviewer"])


class RoleLabelTests(unittest.TestCase):
    def test_every_routed_role_has_a_russian_label_and_an_english_one_in_the_panel(self):
        from aispa_core import display_feed
        html = (Path(__file__).resolve().parents[1] / "skills/aispa/assets/dashboard.html").read_text(encoding="utf-8")
        for role in model_routes.ROLE_CLASS:
            label = display_feed.ROLE_LABELS.get(role)
            self.assertIsNotNone(label, "роль %s без подписи в display_feed.ROLE_LABELS" % role)
            self.assertRegex(html, r"'%s':'[A-Za-z]" % label, "роль %s без английской подписи в панели" % role)


class CoordinatorTests(RoutedFixture):
    def test_coordinator_model_is_free_but_a_second_coordinator_in_the_session_is_refused(self):
        first = self.register("coordinator", "claude-code", native="main", model="claude-fable-5-1", reasoning="xhigh")
        self.assertEqual(participants.resolve(self.store, self.state, first)["routeClass"], "coordinator")
        with self.assertRaisesRegex(Invalid, "помощника регистрируйте его рабочей ролью"):
            self.register("coordinator", "claude-code", native="helper")
        self.register("coordinator", "claude-code", native="next-chat", session="other-session")
        with self.assertRaisesRegex(Invalid, "координатор — главный чат"):
            participants.launch_plan(self.store, self.state, runtime="claude-code", session="host-session",
                                     model="claude-opus-5-5", reasoning="high", role="coordinator")

    def test_the_coordinator_registers_once_and_only_in_the_accepted_host_session(self):
        self.state["hostSession"] = "host-session"
        with self.assertRaisesRegex(Invalid, "сессии хоста host-session"):
            self.register("coordinator", "claude-code", native="stray", session="other-session")
        self.register("coordinator", "claude-code", native="main", session="Host-Session",
                      model="claude-fable-5-1", reasoning="xhigh")
        for role, session in (("coordinator", "host-session"), ("coordinator", " HOST-SESSION "),
                              (" coordinator ", "host-session")):
            with self.subTest(role=role, session=session), self.assertRaisesRegex(
                    Invalid, "помощника регистрируйте его рабочей ролью"):
                self.register(role, "claude-code", native="helper", session=session)
        with self.assertRaisesRegex(Invalid, "сессии хоста host-session"):
            self.register("coordinator", "claude-code", native="next-chat", session="sess-2")
        self.assertEqual([row["nativeId"] for row in self.state["participants"].values()], ["main"])

    def test_an_external_role_named_coordinator_is_a_subagent_and_never_takes_the_coordinator_seat(self):
        for role in ("Coordinator", "COORDINATOR", "CoOrdinator"):
            for session in ("host-session", "sess-9"):
                for model, reasoning, refusal in (("claude-fable-5-1", "xhigh", "субагенту запрещена"),
                                                  ("gpt-6-astra", "high", "субагенту запрещена"),
                                                  ("inherit", "high", "субагенту запрещена"),
                                                  ("claude-opus-5-5", "xhigh", "субагенту запрещено"),
                                                  (None, None, "укажите --model и --reasoning")):
                    with self.subTest(role=role, session=session, model=model), self.assertRaisesRegex(Invalid, refusal):
                        self.register(role, "claude-code", session=session, external=True,
                                      model=model, reasoning=reasoning)
        self.assertEqual(self.state.get("participants", {}), {})
        self.state["hostSession"] = "host-session"
        stray = self.register("Coordinator", "claude-code", native="ext-first", external=True,
                              model="claude-opus-5-5", reasoning="high")
        self.assertIsNone(participants.resolve(self.store, self.state, stray).get("routeClass"))
        main = self.register("coordinator", "claude-code", native="main", model="claude-fable-5-1", reasoning="xhigh")
        self.assertEqual(participants.resolve(self.store, self.state, main)["routeClass"], "coordinator")
        self.assertFalse(participants.actor_check(self.store, self.state, stray, "coordinator"))
        with self.assertRaisesRegex(Invalid, "помощника регистрируйте его рабочей ролью"):
            self.register("coordinator", "claude-code", native="helper")


    def test_the_coordinator_role_marked_external_is_a_clear_refusal_not_a_crash(self):
        for external in (True, None, "yes", 1):
            for call in (lambda: model_routes.check_assignment(self.state, "coordinator", "claude-code", "claude-opus-5-5",
                                                               "high", session="host-session", native_id="x",
                                                               external=external),
                         lambda: model_routes.enforce(self.store, self.state, "coordinator", "claude-code", "x",
                                                      "host-session", "claude-opus-5-5", "high", None,
                                                      external=external)):
                with self.subTest(external=external), self.assertRaisesRegex(Invalid, "Роль coordinator.*главн"):
                    call()


class RoutingBlockTests(RoutedFixture):
    class Flow:
        def __init__(self, test):
            self.test = test

        def running(self):
            pass

        def save(self, event):
            self.test.store.append(self.test.state, event)

        def read_input(self, path):
            return Path(path).read_text(encoding="utf-8")

    def test_participant_and_knowledge_assign_return_the_card_routing_shape(self):
        flow = self.Flow(self)
        flow.store, flow.s = self.store, self.state
        args = argparse.Namespace(action="participant", role="coordinator", runtime="claude-code", native_id="main",
                                  session="host-session", model=None, reasoning=None, model_evidence=None)
        controls.execute(self.store, self.state, flow, args)
        args = argparse.Namespace(action="participant", role="planner", runtime="claude-code", native_id="planner",
                                  session="host-session", model="opus", reasoning="high", model_evidence=None)
        receipt = controls.execute(self.store, self.state, flow, args)
        card_keys = {"routeClass", "model", "effort", "route", "agent", "advisory", "selection", "reference"}
        self.assertLessEqual(card_keys, set(receipt["routing"]))
        self.assertEqual((receipt["routing"]["routeClass"], receipt["routing"]["model"], receipt["routing"]["agent"]),
                         ("judge", "claude-opus-5-5", "aispa-judge"))
        writer = controls.knowledge_assign(flow, runtime="claude-code", native_id="writer", session="host-session",
                                           model="claude-opus-5-5", reasoning="medium", model_evidence=None)
        self.assertEqual(writer["actor"]["role"], "knowledge-writer")
        self.assertEqual((writer["routing"]["routeClass"], writer["routing"]["effort"]), ("research", "medium"))
        self.assertLessEqual(card_keys, set(writer["routing"]))


class ScriptCheckTests(RoutedFixture):
    COMMAND = "python3 tools/check_docs.py --strict"

    def mechanical(self, **extra):
        return dict(self.task("T01", "R01", "a.txt"), tier="light", tierReason="Wording only",
                    changeKind="documentation", risk="low", riskReason="Plain prose only", **extra)

    def raw(self, **extra):
        value = {key: self.mechanical()[key] for key in ("id", "title", "requirements", "dependsOn", "zones",
                 "criteria", "tier", "tierReason", "changeKind", "risk", "riskReason")}
        value["requirements"] = ["R01", "R02", "G01"]
        value.update(extra)
        return value

    def test_plan_accepts_script_check_only_on_mechanical_work(self):
        tasks = validate_plan(self.state, [self.raw(scriptCheck=self.COMMAND)])
        self.assertEqual(tasks[0]["scriptCheck"], self.COMMAND)
        for bad in ({"risk": "normal", "riskReason": "", "tier": "heavy"}, {"zones": ["src/app.py"], "changeKind": "feature",
                                                            "tier": "heavy"}):
            with self.subTest(bad=bad), self.assertRaisesRegex(Invalid, "scriptCheck"):
                validate_plan(self.state, [self.raw(scriptCheck=self.COMMAND, **bad)])
        for value in ("", "   ", 7, ["python3"]):
            with self.subTest(value=value), self.assertRaisesRegex(Invalid, "scriptCheck"):
                validate_plan(self.state, [self.raw(scriptCheck=value)])
        dependent = dict(self.raw(), id="T02", title="Next", dependsOn=["T01"], zones=["b.txt"])
        with self.assertRaisesRegex(Invalid, "scriptCheck"):
            validate_plan(self.state, [self.raw(scriptCheck=self.COMMAND), dependent])
        legacy = dict(self.state, policy={"taskTiers": True, "adaptiveCritique": True})
        with self.assertRaisesRegex(Invalid, "scriptCheck"):
            validate_plan(legacy, [self.raw(scriptCheck=self.COMMAND)])

    def plan(self, task):
        self.state.update(tasks=[task], taskHistory={}, actors={})
        self.team.prepare()

    def test_scripted_mechanics_get_haiku_and_close_only_on_the_script_receipt(self):
        self.plan(self.mechanical(scriptCheck=self.COMMAND))
        self.start()
        self.assertEqual(model_routes.route_class(self.state, "worker", self.host.task("T01")), "mechanical_scripted")
        with self.assertRaisesRegex(Invalid, "claude-haiku-5-5 · low"):
            self.assign("T01", "worker", "claude-sonnet-5-5", "low")
        self.assign("T01", "worker", "haiku", "low")
        self.verify()
        with self.assertRaisesRegex(Invalid, "verify --task T01.*scriptCheck"):
            self.team.submit("T01", self.report())
        self.assertIsNone(self.attempt()["submitted"])
        self.attempt()["verification"]["command"] = shlex.split(self.COMMAND)
        receipt = self.team.submit("T01", self.report())
        self.assertEqual(receipt["completionKind"], "verified_completion")

    def test_mechanics_without_script_stay_on_the_ordinary_mechanical_route(self):
        self.plan(self.mechanical())
        self.start()
        with self.assertRaisesRegex(Invalid, "claude-sonnet-5-5 · low"):
            self.assign("T01", "worker", "claude-haiku-5-5", "low")
        self.assign("T01", "worker", "claude-sonnet-5-5", "low")
        self.verify()
        self.assertEqual(self.team.submit("T01", self.report())["completionKind"], "verified_completion")


if __name__ == "__main__":
    unittest.main()
