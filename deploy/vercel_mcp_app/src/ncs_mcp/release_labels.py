from __future__ import annotations

from typing import Any


BLOCKER_DISPLAY_LABELS = {
    "review_debt:human_reviewed_concepts": "needs explicit human review: ontology concept definitions",
    "review_debt:human_reviewed_goal_links": "needs explicit human review: training-goal KSA links",
    "review_debt:human_reviewed_task_relations": "needs explicit human review: task-KSA relations",
    "human_review:provenance_reconfirmation_required": (
        "needs provenance reconfirmation for legacy trusted-review rows"
    ),
}

BLOCKER_DISPLAY_MESSAGES = {
    "review_debt:human_reviewed_concepts": (
        "Packet-backed manual-review evidence count for ontology concept definitions is still zero."
    ),
    "review_debt:human_reviewed_goal_links": (
        "Packet-backed manual-review evidence count for training-goal KSA links is still zero."
    ),
    "review_debt:human_reviewed_task_relations": (
        "Packet-backed manual-review evidence count for task-KSA relations is still zero."
    ),
}


def blocker_display_label(name: Any) -> str:
    text = str(name or "")
    return BLOCKER_DISPLAY_LABELS.get(text, text)


def blocker_display_message(name: Any, message: Any = "") -> str:
    text = str(name or "")
    return BLOCKER_DISPLAY_MESSAGES.get(text, str(message or ""))


def blocker_display_labels(names: Any) -> list[str]:
    if not isinstance(names, list):
        return []
    return [blocker_display_label(name) for name in names if str(name or "").strip()]


def add_blocker_display_fields(blocker: dict[str, Any]) -> dict[str, Any]:
    name = str(blocker.get("name") or blocker.get("blocker") or "")
    if name:
        blocker.setdefault("display_label", blocker_display_label(name))
        blocker.setdefault("display_message", blocker_display_message(name, blocker.get("message")))
        blocker.setdefault("machine_name", name)
    return blocker
