"""The rules engine. Pure functions only: no database, no Discord, no clock reads.

Every function takes 'now' (or a datetime) as an argument so tests can pin time.
"""
from __future__ import annotations

import re as _re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Iterable, Sequence

from .areas import weight
from .clock import ET

ACTIVE_CAP = 3
QUIET_START = time(0, 0)   # quiet hours run 00:00 ...
QUIET_END = time(8, 0)     # ... up to (not including) 08:00 ET
MORNING_AT = time(8, 0)
MORNING_LAST = time(12, 0)  # if the PC was off at 08:00, still post the morning list until noon
BATCH_SPACING = timedelta(hours=1)
TONIGHT_AT = time(20, 0)
TOMORROW_AT = time(9, 0)

# Button choices
DONE, PLUS2H, TONIGHT, TOMORROW, KILL = "done", "plus2h", "tonight", "tomorrow", "kill"
DO_NOW, PICK_DAY = "donow", "pickday"
SNOOZE_CHOICES = (PLUS2H, TONIGHT, TOMORROW)
SWAP, GOTIT, WAITMORE, START, SKIP, SPLIT = "swap", "gotit", "waitmore", "start", "skip", "split"
LABELS = {DONE: "Done", PLUS2H: "+2h", TONIGHT: "Tonight", TOMORROW: "Tomorrow", KILL: "Kill",
          DO_NOW: "Do now", PICK_DAY: "Pick a day", SWAP: "Swap", GOTIT: "Got it",
          WAITMORE: "Wait more", START: "Start", SKIP: "Skip", SPLIT: "Split it"}
MAX_SNOOZES = 2

# Swap reasons (the button under each of the three) and their limits
SWAP_REASONS = ("long", "out", "energy", "waiting")
SWAP_LABELS = {"long": "Too long", "out": "Can't go out", "energy": "Wrong energy",
               "waiting": "Waiting on someone"}
MAX_SWAPS = 2                         # per item, for life; the 3rd attempt forces a decision
MAX_WAIT_MORE = 2                     # "Wait more" extensions before Pick a day replaces it
WAIT_DEFAULT = timedelta(days=3)
CHECKIN_REPEAT = timedelta(days=7)    # an unanswered check-in comes back once a week
SPLIT_STEP_MAX_MIN = 20
SPLIT_LONG_MIN = 60
SPLIT_UNTOUCHED = timedelta(days=2)


# ---- active cap -------------------------------------------------------------------------
def apply_active_cap(active_count: int, cap: int = ACTIVE_CAP) -> str:
    """Status a new (non-parked) task gets: 'active' while there is a free slot, else 'queued'."""
    return "active" if active_count < cap else "queued"


def pick_promotion(queued: Sequence, now: datetime) -> object | None:
    """Which queued task moves up when a slot frees: the best-ranked one (see rank())."""
    ordered = rank(queued, now)
    return ordered[0] if ordered else None


# ---- snooze ladder ----------------------------------------------------------------------
def snooze_options(snooze_count: int) -> list[str]:
    """Buttons to show. After two snoozes the snooze buttons disappear."""
    if snooze_count >= MAX_SNOOZES:
        return [DO_NOW, PICK_DAY, KILL]
    return [DONE, PLUS2H, TONIGHT, TOMORROW, KILL]


def waiting_options(wait_more_count: int) -> list[str]:
    """Buttons on a waiting item's check-in. After two extensions, Pick a day replaces Wait more."""
    if wait_more_count >= MAX_WAIT_MORE:
        return [GOTIT, PICK_DAY, KILL]
    return [GOTIT, WAITMORE, KILL]


def flagged_options() -> list[str]:
    """A deadline that can't fit today: no snooze (it would only recycle it)."""
    return [DO_NOW, PICK_DAY, KILL]


def resolve_snooze(choice: str, now: datetime, explicit: datetime | None = None) -> datetime:
    """Turn a snooze button into the time the item comes back."""
    now = now.astimezone(ET)
    if explicit is not None:
        return explicit.astimezone(ET)
    if choice == PLUS2H:
        return now + timedelta(hours=2)
    if choice == TONIGHT:
        t = datetime.combine(now.date(), TONIGHT_AT, tzinfo=ET)
        return t if t > now else datetime.combine(now.date() + timedelta(days=1), TONIGHT_AT, tzinfo=ET)
    if choice == TOMORROW:
        return datetime.combine(now.date() + timedelta(days=1), TOMORROW_AT, tzinfo=ET)
    raise ValueError(f"not a snooze choice: {choice!r}")


# ---- quiet hours & batching -------------------------------------------------------------
def in_quiet_hours(dt: datetime) -> bool:
    """True from 00:00 to 07:59 US Eastern (handles both EDT and EST)."""
    local = dt.astimezone(ET).time()
    return QUIET_START <= local < QUIET_END


def next_allowed(dt: datetime) -> datetime:
    """dt itself if outside quiet hours, else 08:00 ET that morning."""
    local = dt.astimezone(ET)
    if not in_quiet_hours(local):
        return local
    return datetime.combine(local.date(), QUIET_END, tzinfo=ET)


def is_due(task, now: datetime) -> bool:
    """Waiting for Max: open (active or queued) and its next nag time has arrived."""
    return (task.status in ("active", "queued") and task.next_nag_at is not None
            and task.next_nag_at <= now)


def batch_due(tasks: Iterable, now: datetime) -> list:
    return rank([t for t in tasks if is_due(t, now)], now)


def next_batch_time(last_sent: datetime | None, now: datetime) -> datetime:
    """Earliest moment the next batched nag may go out (>= 1h after the last, never in quiet hours)."""
    earliest = now if last_sent is None else max(now, last_sent + BATCH_SPACING)
    return next_allowed(earliest)


def in_morning_window(now: datetime) -> bool:
    t = now.astimezone(ET).time()
    return MORNING_AT <= t < MORNING_LAST


# ---- ranking (Phase 2) ------------------------------------------------------------------
# score = payoff x likelihood / hours x area weight x deadline boost.
#   payoff      1-5, how much it matters if done            (missing -> 3)
#   likelihood  0-1, the chance it actually matters/is needed (missing -> 0.7)
#   hours       estimated work, never below 5 minutes         (missing -> 30 minutes)
#   area weight the area's share of attention / 10; unknown areas count as the lightest (5)
# Guardrail 2 is a TIER above the score: anything due within 48 h (or overdue) outranks every
# item that is not, whatever its area or payoff.
_FAR = datetime(9999, 1, 1, tzinfo=ET)
DEFAULT_PAYOFF = 3.0
DEFAULT_LIKELIHOOD = 0.7
DEFAULT_ESTIMATE_MIN = 30
MIN_ESTIMATE_MIN = 5
MIN_AREA_WEIGHT = 5
URGENT_WINDOW = timedelta(hours=48)


def _num(v, default: float, lo: float, hi: float) -> tuple[float, bool]:
    """(clamped number, used_default). Never raises."""
    try:
        x = float(v)
        if x != x:          # NaN
            raise ValueError
    except (TypeError, ValueError):
        return default, True
    return min(max(x, lo), hi), False


def deadline_boost(due_at: datetime | None, now: datetime) -> float:
    if due_at is None:
        return 1.0
    h = (due_at - now).total_seconds() / 3600
    if h <= 0:
        return 4.0
    if h <= 24:
        return 3.0
    if h <= 48:
        return 2.0
    if h <= 168:
        return 1.0 + (168 - h) / 120
    return 1.0


def _parts(t, now: datetime) -> dict:
    payoff, d_p = _num(t.payoff, DEFAULT_PAYOFF, 1.0, 5.0)
    lik_raw = t.likelihood if t.likelihood else None           # 0 would hide the item
    likelihood, d_l = _num(lik_raw, DEFAULT_LIKELIHOOD, 0.05, 1.0)
    est_raw = t.estimate_minutes if t.estimate_minutes else None
    minutes, d_e = _num(est_raw, DEFAULT_ESTIMATE_MIN, MIN_ESTIMATE_MIN, 10 ** 6)
    w = max(weight(t.area), MIN_AREA_WEIGHT)
    return {"payoff": payoff, "likelihood": likelihood, "hours": minutes / 60, "w": w / 10,
            "boost": deadline_boost(t.due_at, now),
            "defaults": [n for n, d in (("payoff", d_p), ("likelihood", d_l), ("estimate", d_e)) if d]}


def score(t, now: datetime) -> float:
    p = _parts(t, now)
    return p["payoff"] * p["likelihood"] / p["hours"] * p["w"] * p["boost"]


def urgent(t, now: datetime) -> bool:
    return t.due_at is not None and t.due_at - now <= URGENT_WINDOW


def rank_key(t, now: datetime) -> tuple:
    return (0 if urgent(t, now) else 1, -round(score(t, now), 6), t.due_at or _FAR,
            -weight(t.area), t.id)


def rank(tasks: Iterable, now: datetime) -> list:
    """Best first. Tie-breaks: urgent tier -> score -> earliest due -> heavier area -> oldest id."""
    return sorted(tasks, key=lambda t: rank_key(t, now))


def explain(t, now: datetime) -> str:
    """One line saying why an item sits where it does, e.g. for the morning post."""
    p = _parts(t, now)
    s = p["payoff"] * p["likelihood"] / p["hours"] * p["w"] * p["boost"]
    line = (f"score {s:.2f} = payoff {p['payoff']:g} x {p['likelihood']:.0%} / {p['hours']:.2g}h"
            f" x area {p['w']:.1f} x boost {p['boost']:.2f}")
    if p["defaults"]:
        line += f" [defaults: {', '.join(p['defaults'])}]"
    if urgent(t, now):
        h = (t.due_at - now).total_seconds() / 3600
        when = "overdue" if h <= 0 else f"due in {h:.0f}h"
        line = f"URGENT ({when}) " + line
    return line


# ---- fits my day (Phase 2.1) ------------------------------------------------------------
# Each item has a place (home = at home/at a desk, out = must go somewhere, phone = a call or
# live conversation) and an energy (light = doable tired, focused = needs a clear head). A day
# mode says which items fit today. Unknown place/energy/estimate count as home/light/30 min -
# a default makes an item MORE likely to be shown, never hidden.
PLACES = ("home", "out", "phone")
ENERGIES = ("light", "focused")
MODE_NAMES = ("sick", "slammed", "normal")


@dataclass(frozen=True)
class DayMode:
    name: str                          # "sick" | "slammed" | "normal" | "custom"
    places: frozenset                  # allowed places
    max_minutes: int | None            # per-item cap
    light_first: bool                  # light items first; focused only when no light item fits
    total_minutes: int | None = None   # optional budget, used only when a mode re-picks the three

    def to_json(self) -> dict:
        return {"name": self.name, "places": sorted(self.places), "max_minutes": self.max_minutes,
                "light_first": self.light_first, "total_minutes": self.total_minutes}

    @classmethod
    def from_json(cls, d: dict) -> "DayMode":
        return cls(str(d.get("name") or "custom"), frozenset(d.get("places") or PLACES),
                   d.get("max_minutes"), bool(d.get("light_first")), d.get("total_minutes"))

    def describe(self) -> str:
        """Plain words, e.g. 'home only, light stuff first, items <= 45 min, ~2 h total'."""
        if self.name == "normal":
            return "no filter"
        bits = []
        if set(self.places) != set(PLACES):
            bits.append(" or ".join(p if p != "phone" else "calls" for p in PLACES
                                    if p in self.places) + " only")
        if self.light_first:
            bits.append("light stuff first")
        if self.max_minutes:
            bits.append(f"items <= {self.max_minutes} min")
        if self.total_minutes:
            h = self.total_minutes / 60
            bits.append(f"~{h:g} h total" if h >= 1 else f"~{self.total_minutes} min total")
        return ", ".join(bits) or "no filter"


SICK = DayMode("sick", frozenset({"home"}), 45, True)
SLAMMED = DayMode("slammed", frozenset({"home", "phone"}), 20, False)
NORMAL = DayMode("normal", frozenset(PLACES), None, False)
PRESETS = {"sick": SICK, "slammed": SLAMMED, "normal": NORMAL}
MODE_LABELS = {"sick": "Sick/home day", "slammed": "Slammed", "normal": "Normal"}


# Cheap, deterministic hints used only while an item's place is still unknown (before the
# local AI labels it): a title that clearly means going somewhere or making a call must not
# count as fitting a home-only day.
# ("drive" only as travel - "drive to/over/out/down/by", "driving" - never the USB kind:
# measured on the live items, "external USB drive" was hinted "out" by a bare "drive".)
_OUT_HINT = _re.compile(r"\b(return|returns|returning|store|pick up|pickup|drop off|errand|"
                        r"go to|driving|drive (?:to|over|out|down|by))\b", _re.IGNORECASE)
_PHONE_HINT = _re.compile(r"\b(call|calls|calling|phone)\b", _re.IGNORECASE)


def place_hint(title: str | None) -> str | None:
    """'out' / 'phone' when the title clearly says so, else None."""
    title = title or ""
    if _OUT_HINT.search(title):
        return "out"
    if _PHONE_HINT.search(title):
        return "phone"
    return None


def needs(t) -> tuple[str, str, int]:
    """(place, energy, minutes) with the defaults applied - never written back to the row.
    Unknown place: the title hint (return/store/pick up/drive -> out; call/phone -> phone),
    then home."""
    place = getattr(t, "place", None)
    if place not in PLACES:
        place = place_hint(getattr(t, "title", None)) or "home"
    energy = getattr(t, "energy", None) if getattr(t, "energy", None) in ENERGIES else "light"
    minutes = t.estimate_minutes if t.estimate_minutes and t.estimate_minutes > 0 \
        else DEFAULT_ESTIMATE_MIN
    return place, energy, int(minutes)


def fits(t, mode: DayMode | None) -> bool:
    """Does the item fit today? Energy is a preference (see fit_key), never a filter."""
    if mode is None or mode.name == "normal":
        return True
    place, _energy, minutes = needs(t)
    return place in mode.places and (mode.max_minutes is None or minutes <= mode.max_minutes)


def fit_key(t, mode: DayMode | None, now: datetime) -> tuple:
    """Guardrail 2 stays outermost: a fitting item due within 48 h beats everything, then
    light before focused (when the mode asks for it), then the usual rank."""
    light = (0 if needs(t)[1] == "light" else 1) if mode is not None and mode.light_first else 0
    k = rank_key(t, now)
    return (k[0], light) + k[1:]


def fit_rank(tasks: Iterable, mode: DayMode | None, now: datetime) -> list:
    """The items that fit, best first (light first when the mode asks for it)."""
    return sorted((t for t in tasks if fits(t, mode)), key=lambda t: fit_key(t, mode, now))


def why_not(t, mode: DayMode | None) -> str:
    """Why an item does not fit, e.g. 'needs going out, ~90 min > 45'. '' when it fits."""
    if fits(t, mode):
        return ""
    place, _energy, minutes = needs(t)
    bits = []
    if place not in mode.places:
        bits.append({"out": "needs going out", "phone": "is a phone call",
                     "home": "needs to be at home"}[place])
    if mode.max_minutes is not None and minutes > mode.max_minutes:
        bits.append(f"~{fmt_minutes(minutes)} > {mode.max_minutes} min")
    return ", ".join(bits)


def fmt_minutes(m: int) -> str:
    """45 -> '45 min', 90 -> '1.5 h', 120 -> '2 h'."""
    return f"{m} min" if m < 60 else f"{m / 60:g} h"
