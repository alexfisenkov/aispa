"""Final repairs of branch findings and blind-acceptance divergences: state["finalRepairs"].

The logic arrives with ticket 06 of the 0.5.0 build; until then the CLI
answers honestly that it is not ready. A repair never enters state["tasks"].
"""
from .store import Invalid

MAX_ROUNDS = 2
MAX_BLIND_REPAIRS = 2
TICKET = "06"


def execute(store, state, flow, args):
    """CLI `final-repair open|assign|submit|check`: returns the receipt; each step saves its own event."""
    raise Invalid("Команда final-repair %s появится в задаче %s сборки 0.5.0; в этой версии пакета её ещё нет"
                  % (args.repair_action, TICKET))
