"""The nine life areas a task can belong to, with their target weights (sum = 100)."""
from __future__ import annotations

AREAS: dict[str, int] = {
    "Coreflow/CLARA": 22,
    "School": 18,
    "Side-venture ideas": 8,
    "Portfolio": 9,
    "System improvement": 7,
    "Personal admin": 8,
    "Fantasy": 3,
    "Nationwide": 20,
    "Home & life": 5,
}
UNSORTED = "unsorted"
SIDE_VENTURES = "Side-venture ideas"

assert sum(AREAS.values()) == 100


def weight(area: str) -> int:
    return AREAS.get(area, 0)


def normalize(area: str | None) -> str:
    """Map free text (any case) to a known area name, else 'unsorted'."""
    if not area:
        return UNSORTED
    a = area.strip().lower()
    for name in AREAS:
        if name.lower() == a:
            return name
    return UNSORTED

# Note: an area never decides a task's status. Parking only happens on an explicit
# 'park:' / 'idea:' message or a seed row marked "parked" (see service.TaskService.add).
