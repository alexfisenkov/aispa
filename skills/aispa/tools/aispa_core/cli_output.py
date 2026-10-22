"""Optional receipts for CLI output; the saved JSON remains the full response.

The caller owns the current run and its lock. This module only formats an
already computed response: it never reloads state or evaluates workflow gates.
"""
import json
import sys

from .store import Invalid


INLINE_RECEIPT_BYTES = 2048
TASK_STATUSES = ("pending", "running", "blocked", "completed")


def encode(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def compact(store, output):
    raw = encode(output)
    reference = store.artifact("cli-output", raw, ".json")
    result = {
        "compact": True,
        "ok": output.get("ok", True),
        **{key: output[key] for key in ("run", "runDirectory", "status", "stage", "gates", "requiredHostActions") if key in output},
        **reference,
        "absolutePath": str(store.run / reference["file"]),
        "bytes": len(raw),
        "readingRule": "Open absolutePath for the complete response and evidence; this receipt is not an acceptance report.",
    }
    if "stage" not in result and "displayStage" in output:
        result["stage"] = output["displayStage"]
    if "counts" in output:
        result["counts"] = output["counts"]
    elif "tasks" in output:
        result["counts"] = {name: sum(task["status"] == name for task in output["tasks"])
                            for name in TASK_STATUSES}
    if "task" in output:
        task = output["task"]
        result["task"] = {key: task[key] for key in ("id", "status", "phase") if key in task} if task else None
    if "receipt" in output:
        receipt = output["receipt"]
        result["receipt"] = receipt if len(encode(receipt)) <= INLINE_RECEIPT_BYTES else {
            "inFullResponse": True, "jsonPointer": "/receipt",
        }
    if not result["ok"]:
        result["readingRule"] = "The command failed. Open absolutePath for the complete failure response and evidence."
    return result


def emit(store, output, *, compact_mode=False):
    value = output
    if compact_mode:
        try:
            value = compact(store, output)
        except (Invalid, OSError):
            # The workflow may already have committed its journal event.
            # A presentation failure must not pretend that action was rolled back.
            print("AISpa: Краткий ответ сохранить не удалось; ниже полный результат команды. "
                  "Не повторяйте действие из-за этой ошибки форматирования.", file=sys.stderr)
    print(json.dumps(value, ensure_ascii=False, indent=2))
