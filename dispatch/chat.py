"""Plain-English chat: what Max types in #inbox / #today without a prefix, and Siri dictation.

The local AI (Qwen) only READS the sentence and picks one action from a closed list, as JSON.
Everything after that is deterministic:
  - dates come from parser.parse_when (the AI copies the time words; it never computes a date)
  - which task is meant comes from resolve(): an explicit #number in the message wins; else a
    fuzzy title match that must be clearly better than the runner-up; otherwise we ASK.
    A wrong-task action is worse than a question, so any doubt -> "Which one?".
  - every change goes through TaskService (so it is logged and can be undone).
No Discord in here: Router.handle() returns a Reply; bot.py sends it. Blocking AI calls go
through self.run (asyncio.to_thread in the bot) so the database stays on the event-loop thread.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Callable

from . import db, holds, legend, qwen, rules
from .areas import AREAS, UNSORTED, normalize
from .clock import ET, fmt_et
from .db import Task
from .parser import capture_kind, parse_add, parse_when
from .service import CantDo, LadderError, NotOpen, TaskService, UndoRefused, describe

log = logging.getLogger("dispatch.chat")

ACTIONS = ("reschedule", "re_estimate", "set_priority", "set_area", "done", "kill", "snooze",
           "do_now", "shift_after", "query", "add", "undo", "unclear",
           "set_mode", "minutes", "set_needs", "wait", "resume", "swap")
NEEDS_TASK = ("reschedule", "re_estimate", "set_priority", "set_area", "done", "kill",
              "snooze", "do_now", "set_needs", "wait", "resume", "swap")
QUERY_KINDS = ("due_today", "due_week", "active", "queued", "parked", "waiting")
SCHEMA_KEYS = {"action", "task", "when", "minutes", "payoff", "area", "kind", "title", "question",
               "mode", "max_minutes", "total_minutes", "place", "energy", "reason"}
MINUTES_KEY, MINUTES_TTL = "minutes_query", timedelta(minutes=30)
SPLIT_OFFER = "split_offer:"                   # + task id -> {"step", "minutes", "at"}

PENDING_KEY, CONTEXT_KEY = "pending_chat", "chat_context"
PENDING_TTL, CONTEXT_TTL = timedelta(minutes=10), timedelta(minutes=30)
RESOLVE_MIN, RESOLVE_GAP = 0.6, 0.15
SIRI_MAX_PER_HOUR, SIRI_MAX_CHARS = 20, 300
MAX_REPLY = 1990
# "new list" and friends: matched by rule, never by the AI (works with Qwen slow or off)
NEW_LIST_RE = re.compile(
    r"\s*(?:(?:new|fresh|refresh(?:ed)?|redo|update|re-?rank)\s+(?:the\s+|my\s+)?"
    r"(?:list|three|3|priorities|picks)|refresh|re-?rank|reshuffle|re-?prioriti[sz]e)"
    r"\s*(?:please|pls)?\s*[?!.]*\s*", re.IGNORECASE)


def is_new_list(text: str) -> bool:
    return bool(NEW_LIST_RE.fullmatch(text or ""))

HINT = ("Couldn't parse that - try \"add: ...\", \"done #7\", \"move #7 to Friday\", "
        "or tap a button.")
ASK_WHEN = "When exactly? e.g. \"tomorrow 3pm\", \"Friday\", \"in 2 hours\"."
ASK_WHICH = "Which item? Use its #number (say \"list\" to see them)."
PAYOFF_WORDS = (("not important", 1), ("unimportant", 1), ("low priority", 2), ("low", 2),
                ("top priority", 5), ("critical", 5), ("urgent", 5), ("high priority", 4),
                ("important", 4), ("high", 4))
ADD_CUES = re.compile(r"\b(add|remind|reminder|to-?do|new task|put .{1,60} on (my|the) list|"
                      r"track|note down|don'?t let me forget)\b", re.IGNORECASE)


@dataclass
class Reply:
    text: str
    undo_event_id: int | None = None      # show an Undo button for this event
    task_ids: list[int] = field(default_factory=list)   # tasks this reply is about
    action: str = ""                      # what happened (for tests / stats / the eval)
    buttons: list[tuple[str, str]] = field(default_factory=list)   # (label, custom_id) extras


# ---- the prompt ----------------------------------------------------------------------------
# Few-shot examples use different wording from the eval set (tools/eval_qwen.py) on purpose.
EXAMPLES = [
    ("kill the garden idea", {"action": "kill", "task": {"id": None, "text": "the garden idea"}}),
    ("move #7 to monday", {"action": "reschedule", "task": {"id": 7, "text": None}, "when": "monday"}),
    ("wrapped up the tax pro thing", {"action": "done", "task": {"id": None, "text": "the tax pro thing"}}),
    ("bump the git loop to tonight", {"action": "snooze", "task": {"id": None, "text": "the git loop"},
                                      "when": "tonight"}),
    ("give the thesis renumbering 45 min", {"action": "re_estimate",
                                            "task": {"id": None, "text": "the thesis renumbering"},
                                            "minutes": 45}),
    ("busy till 3, hold everything", {"action": "shift_after", "when": "3"}),
    ("whats left for today", {"action": "query", "kind": "due_today"}),
    ("show me my list", {"action": "query", "kind": "active"}),
    ("garden planning - monday", {"action": "reschedule", "task": {"id": None, "text": "garden planning"},
                                  "when": "monday"}),
    ("remind me to buy printer ink friday", {"action": "add", "title": "buy printer ink",
                                             "when": "friday"}),
    # Phase 2.1 (again: not the eval sentences)
    ("home sick today, nothing heavy", {"action": "set_mode", "mode": "sick"}),
    ("crazy day, only tiny things", {"action": "set_mode", "mode": "slammed"}),
    ("ok normal day again", {"action": "set_mode", "mode": "normal"}),
    ("got 20 min, what's quick", {"action": "minutes", "minutes": 20}),
    ("what could I knock out from the couch", {"action": "minutes", "place": "home",
                                               "energy": "light"}),
    ("#5 needs a trip out", {"action": "set_needs", "task": {"id": 5, "text": None},
                             "place": "out"}),
    ("the git loop is a mindless one", {"action": "set_needs",
                                        "task": {"id": None, "text": "the git loop"},
                                        "energy": "light"}),
    ("that one's an errand", {"action": "set_needs", "task": None, "place": "out"}),
    ("waiting on the broker for #4", {"action": "wait", "task": {"id": 4, "text": None}}),
    ("the broker got back to me", {"action": "resume",
                                   "task": {"id": None, "text": "the broker"}}),
    ("swap the git thing, wrong headspace", {"action": "swap",
                                             "task": {"id": None, "text": "the git thing"},
                                             "reason": "energy"}),
    # the "unclear" examples stay last (measured: with the Phase 2.1 examples after them the
    # model started answering "unclear" to plain commands such as "undo that")
    ("maybe I'll look at lease stuff someday", {"action": "unclear", "question": None}),
    ("cool, thanks!", {"action": "unclear", "question": None}),
]


def _task_lines(tasks: list[Task], limit: int = 60) -> str:
    order = {"active": 0, "queued": 1, "waiting": 2, "parked": 3}
    rows = sorted(tasks, key=lambda t: (order.get(t.status, 4), t.id))[:limit]

    def needs(t: Task) -> str:
        bits = [b for b in (t.place, t.energy) if b]
        if t.estimate_minutes:
            bits.append(f"~{t.estimate_minutes}min")
        return f"  [{' '.join(bits)}]" if bits else ""
    return "\n".join(f"#{t.id}  {t.status}  {t.title[:80]}{needs(t)}" for t in rows) or "(none)"


def build_prompt(text: str, now: datetime, tasks: list[Task]) -> str:
    ex = "\n".join(f'Message: "{m}"\n{json.dumps(o)}' for m, o in EXAMPLES)
    areas = ", ".join(AREAS)
    return (
        "You turn ONE chat message from Max into ONE action for his to-do bot. "
        "Reply with ONE JSON object and nothing else. Keys (leave out or null what you don't need):\n"
        '{"action": "<name>", "task": {"id": <number or null>, "text": "<words from the message '
        'that name the task, or null>"}, "when": "<the time words copied exactly from the '
        'message, or null>", "minutes": <int or null>, "payoff": <int 1-5 or null>, '
        '"area": "<area name or null>", "kind": "due_today|due_week|active|queued|parked|waiting '
        'or null", "title": "<new task title or null>", "question": null, '
        '"mode": "sick|slammed|normal or null", "max_minutes": <int or null>, '
        '"total_minutes": <int or null>, "place": "home|out|phone or null", '
        '"energy": "light|focused or null", "reason": "long|out|energy|waiting or null"}\n'
        "Actions:\n"
        "- done: he finished a task.\n"
        "- kill: drop / cancel / delete a task for good.\n"
        "- snooze: come back to a task later TODAY (later, tonight, in 2 hours, until 3pm).\n"
        "- reschedule: move a task to another day, or to a specific day and time.\n"
        "- do_now: he is starting a task right now.\n"
        "- re_estimate: how long a task takes; put it in minutes.\n"
        "- set_priority: how important a task is, as payoff 1-5 (not important=1, low priority=2, "
        "important/high priority=4, critical/top priority=5).\n"
        "- set_area: put a task in another area.\n"
        "- shift_after: he is busy until a time; hold everything until then (when = that time).\n"
        "- query: he asks what is on his list (kind: due_today, due_week, active = his current "
        "list / the three, queued, parked, waiting = waiting on someone).\n"
        "- add: ONLY when he clearly asks to add or be reminded of a NEW to-do (\"add ...\", "
        "\"remind me to ...\"). Title without the time words. A passing thought like "
        "\"I should call X sometime\" is NOT add.\n"
        "- undo: reverse the last change.\n"
        "- set_mode: he says what kind of day today is (sick / home day, slammed, back to "
        "normal); add place, max_minutes (per item) or total_minutes when he states them.\n"
        "- minutes: he asks what he can do in N minutes, or from bed / the couch "
        "(minutes = N; place home and energy light for bed/couch).\n"
        "- set_needs: he says where a task happens or how heavy it is. "
        f"{qwen.PLACE_LINE}. {qwen.ENERGY_LINE}.\n"
        "- wait: he is waiting on someone else before a task can move (when = check-in words "
        "if he gives any).\n"
        "- resume: the person got back to him / he got what he was waiting for.\n"
        "- swap: he wants a different task in place of one of his three; reason: long (too "
        "long), out (can't go out), energy (wrong energy / headspace), waiting (waiting on "
        "someone).\n"
        "- unclear: anything else - thanks, chit-chat, musings, or when you are not sure.\n"
        "Rules: task.id only when the message itself contains a task number like #7 or "
        "\"task 7\". task.text = the words copied from the MESSAGE that name the task - never "
        "copy a title from the task list, never invent. If the message is about a task that is "
        "already on the list, the action is never add. "
        "when = the time words exactly as written (e.g. \"Saturday 1pm\", \"next Wednesday\"); "
        "never compute a date.\n"
        f"Areas: {areas}\n"
        f"Now: {fmt_et(now)}\n"
        f"Open tasks (#id status title [place energy ~minutes]):\n{_task_lines(tasks)}\n"
        f"Examples:\n{ex}\n"
        f'Message: "{text.strip()[:500]}"\n')


# ---- validation ----------------------------------------------------------------------------
UNCLEAR = {"action": "unclear", "task": None, "when": None, "minutes": None, "payoff": None,
           "area": None, "kind": None, "title": None, "question": None, "mode": None,
           "max_minutes": None, "total_minutes": None, "place": None, "energy": None,
           "reason": None}


def _int_or_none(v, ok: list) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or float(v) != int(v):
        ok[0] = False
        return None
    return int(v)


def _str_or_none(v, ok: list) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str):
        ok[0] = False
        return None
    v = v.strip()
    return v or None


def validate(obj) -> dict:
    """Check the AI's JSON against the closed schema. Anything off-schema -> 'unclear'."""
    if not isinstance(obj, dict) or set(obj) - SCHEMA_KEYS or obj.get("action") not in ACTIONS:
        return dict(UNCLEAR)
    ok = [True]
    task = obj.get("task")
    if task is not None:
        if not isinstance(task, dict) or set(task) - {"id", "text"}:
            return dict(UNCLEAR)
        task = {"id": _int_or_none(task.get("id"), ok), "text": _str_or_none(task.get("text"), ok)}
        if task["id"] is None and task["text"] is None:
            task = None
    out = {"action": obj["action"], "task": task,
           "when": _str_or_none(obj.get("when"), ok),
           "minutes": _int_or_none(obj.get("minutes"), ok),
           "payoff": _int_or_none(obj.get("payoff"), ok),
           "area": _str_or_none(obj.get("area"), ok),
           "kind": _str_or_none(obj.get("kind"), ok),
           "title": _str_or_none(obj.get("title"), ok),
           "question": _str_or_none(obj.get("question"), ok),
           "mode": _str_or_none(obj.get("mode"), ok),
           "max_minutes": _int_or_none(obj.get("max_minutes"), ok),
           "total_minutes": _int_or_none(obj.get("total_minutes"), ok),
           "place": _str_or_none(obj.get("place"), ok),
           "energy": _str_or_none(obj.get("energy"), ok),
           "reason": _str_or_none(obj.get("reason"), ok)}
    for key, allowed in (("kind", QUERY_KINDS), ("mode", rules.MODE_NAMES),
                         ("place", rules.PLACES), ("energy", rules.ENERGIES),
                         ("reason", rules.SWAP_REASONS)):
        if out[key] is not None:
            out[key] = out[key].lower()
            if out[key] not in allowed:
                ok[0] = False
    if not ok[0]:
        return dict(UNCLEAR)
    return out


# ---- times and durations (deterministic) ---------------------------------------------------
_LEADS = re.compile(r"^(?:until|till|til|after|to|for|at around|around|later|this|by|on|at)\s+")
_PUNCT = " ,.;:!?\"'()-"


def _clean_when(p: str) -> str:
    p = re.sub(r"\s+", " ", p.strip().lower()).strip(_PUNCT)
    prev = None
    while prev != p:
        prev, p = p, _LEADS.sub("", p)
    return p


def _loose_hour(p: str, now: datetime) -> datetime | None:
    """A bare clock number ("4", "4:30") -> the next time that clock shows (4 PM at 9 AM)."""
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?", p)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if mi > 59 or h > 23:
        return None
    now = now.astimezone(ET)
    hours = [h, h + 12] if 1 <= h <= 11 else [h]
    for d in (0, 1):
        for hh in hours:
            dt = datetime.combine(now.date() + timedelta(days=d), time(hh % 24, mi), tzinfo=ET)
            if dt > now:
                return dt
    return None


def parse_time_phrase(when: str | None, text: str, now: datetime) -> datetime | None:
    """The AI's time words first; then any 1-3 word span of the message, rightmost first."""
    if when:
        p = _clean_when(when)
        dt = parse_when(p, now) if p else None
        if dt is None and p:
            dt = _loose_hour(p, now)
        if dt is not None:
            return dt
    words = [w.strip(_PUNCT) for w in text.split()]
    words = [w for w in words if w]
    for end in range(len(words), 0, -1):
        for k in (3, 2, 1):
            if end - k < 0:
                continue
            span = " ".join(words[end - k:end]).lower()
            dt = parse_when(span, now)
            if dt is not None:
                return dt
    return None


_DUR = re.compile(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m)\b", re.IGNORECASE)


def parse_minutes(text: str) -> int | None:
    """'more like 3 hours' -> 180, '20 minute job' -> 20, 'an hour' -> 60. None if absent."""
    low = text.lower()
    m = _DUR.search(low)
    if m:
        n, unit = float(m.group(1)), m.group(2)
        return max(1, round(n * 60 if unit.startswith("h") else n))
    if re.search(r"\b(an? )?hour and a half\b", low):
        return 90
    if re.search(r"\bhalf an? hour\b", low):
        return 30
    if re.search(r"\bquarter (of an? )?hour\b", low):
        return 15
    if re.search(r"\ban? hour\b", low):
        return 60
    return None


# ---- day modes and needs (deterministic; Max's words beat the model's numbers) --------------
_MODE_WORDS = (("sick", re.compile(r"\b(sick|home day|staying (in|home)|feeling rough|"
                                   r"under the weather)\b")),
               ("slammed", re.compile(r"\b(slammed|swamped|crazy day|busy day|packed day)\b")),
               ("normal", re.compile(r"\b(normal|regular day|back to usual)\b")))
# a mode is only set when the message is clearly about TODAY ("sick of this project" is not)
_MODE_GUARD = re.compile(r"\b(today|day|tonight|right now|this (morning|afternoon)|home only|"
                         r"staying (in|home)|slammed|swamped|back to normal|feeling rough|"
                         r"under the weather)\b")
_MAX_AFTER = re.compile(r"^\s*-?\s*(chunks?|bursts?|blocks?|each|per (item|task|thing)|"
                        r"at a time|apiece|a pop)\b")
_MAX_BEFORE = re.compile(r"(bursts? of|chunks? of|under|less than|at most|max|up to|"
                         r"shorter than|no more than|nothing over|nothing longer than)\s*$")
_BED = re.compile(r"\b(bed|couch|sofa|lying down|laying down)\b")
PLACE_WORDS = (("out", re.compile(r"\b(out of the house|errand|go to|drive|trip out|in person|"
                                  r"store|go out)\b")),
               ("phone", re.compile(r"\b(call|phone|ring)\b")),
               ("home", re.compile(r"\b(at home|from home|at my desk|at the desk)\b")))
ENERGY_WORDS = (("light", re.compile(r"\b(easy|quick|mindless|light|no[- ]brainer)\b")),
                ("focused", re.compile(r"\b(deep|focus|focused|heavy|brain|hard|concentrat\w*)\b")))
SWAP_WORDS = (("long", re.compile(r"\b(too long|too big|way too long|takes forever)\b")),
              ("out", re.compile(r"\b(can'?t go out|cannot go out|can'?t leave|stuck (at )?home)\b")),
              ("energy", re.compile(r"\b(energy|headspace|tired|too hard|not up for)\b")),
              ("waiting", re.compile(r"\b(waiting on|blocked|waiting for)\b")))


_APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'"})


def _norm(text: str) -> str:
    """Curly apostrophes (the iPhone keyboard types them) become straight ones."""
    return (text or "").translate(_APOSTROPHES)


def _word_pick(table, low: str) -> str | None:
    """The one value whose words appear in the text; None when none or several do."""
    hits = [v for v, rx in table if rx.search(low)]
    return hits[0] if len(hits) == 1 else None


def _durations(low: str):
    """Every duration in the text as (minutes, start, end)."""
    for m in _DUR.finditer(low):
        n, unit = float(m.group(1)), m.group(2)
        yield max(1, round(n * 60 if unit.startswith("h") else n)), m.start(), m.end()
    for rx, v in ((r"\b(an? )?hour and a half\b", 90), (r"\bhalf an? hour\b", 30),
                  (r"\bquarter (of an? )?hour\b", 15), (r"(?<!half )\ban? hour\b(?! and a half)", 60)):
        for m in re.finditer(rx, low):
            yield v, m.start(), m.end()


def mode_from_text(text: str, p: dict) -> rules.DayMode | None:
    """The day mode Max described. Presets from the words (else the model's mode); durations,
    place and energy words refine it. None = not clearly about today."""
    low = _norm(text).lower().replace("-", " ")
    name = _word_pick(_MODE_WORDS, low) or p.get("mode")
    has_fields = any(p.get(k) for k in ("max_minutes", "total_minutes", "place"))
    if not _MODE_GUARD.search(low) and not (name is None and has_fields):
        return None
    base = rules.PRESETS.get(name or "", rules.NORMAL)
    max_m, total = None, None
    for mins, a, b in _durations(low):
        if _MAX_AFTER.match(low[b:]) or _MAX_BEFORE.search(low[:a]):
            max_m = mins if max_m is None else min(max_m, mins)
        else:
            total = mins if total is None else max(total, mins)
    if max_m is None and re.search(r"\b(short|small|tiny|quick) (bursts?|chunks?|bits?)\b", low):
        max_m = 30
    max_m = max_m or p.get("max_minutes")
    total = total or p.get("total_minutes")
    places = set(base.places)
    if re.search(r"\b(home only|from home|at home|staying (in|home)|can'?t go out|no errands)\b",
                 low):
        places = {"home"} if "phone" not in base.places or re.search(r"\bhome only\b", low) \
            else {"home", "phone"}
    elif p.get("place") in rules.PLACES and name is None:
        places = {p["place"]}
    if re.search(r"\bno (calls?|phone)\b", low):
        places.discard("phone")
    light = base.light_first or bool(re.search(r"\b(light|easy|nothing heavy|low energy)\b", low)) \
        or p.get("energy") == "light"
    if name == "normal" and not (max_m or total or places != set(rules.PLACES)):
        return rules.NORMAL
    max_m = min(x for x in (max_m, base.max_minutes) if x) if (max_m or base.max_minutes) else None
    mode = rules.DayMode(name or "custom", frozenset(places or {"home"}), max_m, light, total)
    return rules.PRESETS[name] if name in rules.PRESETS and mode == rules.PRESETS[name] else mode


# ---- which task? (the wrong-task = 0 rule) --------------------------------------------------
STOP = set("""
a an the and or but of to for from with without about at by on in into onto up out off over
is are was were be been am it its it's that that's this these those them they i i'm im me my
you your we our he she his her do did does doing done finish finished finishing wrapped
make made get got mark set move moved push pushed bump drop dropped kill killed snooze call
called later tonight today tomorrow next week weeks day days now soon until till til after
before thing things stuff one ones job task tasks item items todo to-do list please pls just
really actually more like maybe probably should gonna going want need needs some point
priority low high critical important not minute minutes min mins hour hours hr hrs time
monday tuesday wednesday thursday friday saturday sunday mon tue wed thu fri sat sun
am pm morning evening afternoon night what whats what's on plate due also still yet again
thats its ive im dont didnt cant wont lets ok okay yeah yes no nah
""".split())
_ID = re.compile(r"(?<![\w/:.$#])(#)?(\d{1,4})(?![\w/:%])")
_ID_WORD_BEFORE = re.compile(r"\b(?:task|item|number|no\.?)\s*$", re.IGNORECASE)
_UNIT_AFTER = re.compile(r"^\s*(?:min|mins|minutes?|hours?|hrs?|h|am|pm|days?|weeks?|%|x)\b",
                         re.IGNORECASE)


@dataclass
class Resolved:
    task: Task


@dataclass
class Ambiguous:
    candidates: list[Task]


@dataclass
class NotFound:
    query: str


@dataclass
class NoRef:
    pass


def _tokens(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (s or "").lower())


def content_tokens(s: str) -> list[str]:
    return [w for w in _tokens(s) if w not in STOP and (len(w) >= 3 or (w.isdigit() and len(w) >= 2))]


_SUFFIXES = ("s", "es", "ed", "d", "ing", "er", "ers")


def _tok_match(q: str, t: str) -> bool:
    """Same word, a simple inflection (return/returns, renumber/renumbering), or a near-typo.
    Not a mere prefix: 'broker' must not match 'brokerage', nor 'back' match 'backup'."""
    if q == t:
        return True
    short, long_ = sorted((q, t), key=len)
    if len(short) >= 3 and long_.startswith(short) and long_[len(short):] in _SUFFIXES:
        return True
    return len(q) >= 5 and len(t) >= 5 and difflib.SequenceMatcher(None, q, t).ratio() >= 0.85


def match_score(query: str, t: Task) -> float:
    """0..1: share of the query's content words found in the title (or the area name),
    or the whole-string similarity if that is higher."""
    q = content_tokens(query)
    if not q:
        return 0.0
    toks = _tokens(t.title) + _tokens(t.area)
    # Compound words: "lookup" matches "Look up", "log-in" matches "login" - also compare
    # against each pair of neighbouring title words written without the space/hyphen.
    words = set(toks) | {a + b for a, b in zip(toks, toks[1:])}
    raw = _tokens(query)
    joined_q = {a + b for a, b in zip(raw, raw[1:])} & set(toks)
    hit = sum(1 for w in q if any(_tok_match(w, x) for x in words)
              or any(j.startswith(w) or j.endswith(w) for j in joined_q))
    ratio = difflib.SequenceMatcher(None, " ".join(q), t.title.lower()).ratio()
    return max(hit / len(q), ratio)


def explicit_ids(text: str, exclude: set[int] = frozenset()) -> tuple[list[int], list[int]]:
    """(numbers written like task ids, of which: those with a '#'). Numbers followed by a unit
    ("20 minute", "3 hours", "4pm") or listed in `exclude` (e.g. inside the time words) are not ids."""
    ids, hashed = [], []
    for m in _ID.finditer(text):
        n = int(m.group(2))
        if not m.group(1):
            # A bare number is a task id only when a word says so ("task 2", "item 2",
            # "number 2"): "took 2 tries" or "the 1099 thing" must never pick #2 / #1099.
            if not _ID_WORD_BEFORE.search(text[:m.start()]):
                continue
            if n in exclude or _UNIT_AFTER.match(text[m.end():]):
                continue
        if n not in ids:
            ids.append(n)
        if m.group(1):
            hashed.append(n)
    return ids, hashed


def grounded(words: str | None, message: str) -> str | None:
    """Keep only the AI's task words that Max actually wrote. A model sometimes copies a whole
    title from the task list into task.text; that must never pick the task for him."""
    if not words:
        return None
    said = set(_tokens(message))
    kept = [w for w in content_tokens(words) if any(_tok_match(w, s) for s in said)]
    return " ".join(kept) or None


def resolve(ref: dict | None, text: str, tasks: list[Task], exclude: set[int] = frozenset()):
    """Resolved | Ambiguous | NotFound | NoRef. Never guesses between close candidates."""
    by_id = {t.id: t for t in tasks}
    ids, hashed = explicit_ids(text, exclude)
    hits = [n for n in ids if n in by_id]
    if len(hits) == 1 and hits[0] not in hashed:
        # A bare number ("drop 14") is an id only if the rest of the words don't point at a
        # different task ("done with the 3 streaming subscriptions one" must not close #3).
        rest = re.sub(rf"(?<!\d){hits[0]}(?!\d)", " ", text)
        others = [t for t in tasks if match_score(rest, t) >= RESOLVE_MIN and t.id != hits[0]]
        if others and max(match_score(rest, t) for t in others) > match_score(rest, by_id[hits[0]]):
            return Ambiguous([by_id[hits[0]]] + sorted(others, key=lambda t: -match_score(rest, t))[:3])
    if len(hits) == 1:
        return Resolved(by_id[hits[0]])
    if len(hits) > 1:
        return Ambiguous([by_id[n] for n in hits][:4])
    if hashed:
        return NotFound(f"#{hashed[0]}")
    query = grounded((ref or {}).get("text"), text)
    from_message = not (query and content_tokens(query))
    if from_message:
        query = text
    if not content_tokens(query):
        return NoRef()
    scored = sorted(((match_score(query, t), t) for t in tasks), key=lambda st: (-st[0], st[1].id))
    if not scored or scored[0][0] < RESOLVE_MIN:
        return NoRef() if from_message else NotFound(query)
    best, second = scored[0][0], (scored[1][0] if len(scored) > 1 else 0.0)
    if best - second >= RESOLVE_GAP:
        return Resolved(scored[0][1])
    # Near-tie: always ask. The model's task.id (hint) never breaks a tie - a model can copy
    # an id from the task list without Max having written it.
    close = [t for s, t in scored if s >= RESOLVE_MIN and best - s < RESOLVE_GAP]
    return Ambiguous(close[:4])


# ---- reply text ----------------------------------------------------------------------------
def _mins(m: int) -> str:
    return f"~{m} min" if m < 60 else f"~{m / 60:.1f}".rstrip("0").rstrip(".") + " h"


def format_added(t: Task, g: qwen.Guess) -> str:
    verb = "Parked" if t.status == "parked" else "Added"
    line = f"{verb} {describe(t)} ({t.status})."
    if not g.guessed:
        return (line + f" Filed as {t.area} with default weights (the local AI was off or unsure)."
                " Reply to fix, e.g. \"more like 3 hours\" or \"low priority\".")
    bits = []
    if "payoff" in g.guessed:
        bits.append(f"payoff {g.payoff}/5")
    if "likelihood" in g.guessed:
        bits.append(f"{g.likelihood:.0%} likely to matter")
    if "estimate_minutes" in g.guessed:
        bits.append(_mins(g.estimate_minutes))
    place = {"home": "at home", "out": "out of the house", "phone": "a call"}
    if "place" in g.guessed:
        bits.append(place[g.place])
    if "energy" in g.guessed:
        bits.append(g.energy)
    if "area" not in g.guessed and t.area == UNSORTED:
        bits.append("area unsorted")
    return (line + (f" Guessed: {', '.join(bits)}." if bits else "")
            + " Reply to fix, e.g. \"more like 3 hours\" or \"low priority\".")


def _promo(promoted: list[Task]) -> str:
    return "".join(f" Up next: #{p.id} {p.title}." for p in promoted)


def _clip(text: str) -> str:
    return text if len(text) <= MAX_REPLY else text[:MAX_REPLY - 20].rsplit("\n", 1)[0] + "\n... (cut short)"


# ---- the router ----------------------------------------------------------------------------
class Router:
    """One incoming message -> exactly one Reply (never None, never an exception)."""

    def __init__(self, service: TaskService, base_url: str = qwen.DEFAULT_URL,
                 ask: Callable[[str], dict | None] | None = None,
                 guess: Callable[[str, str | None], qwen.Guess] | None = None,
                 run=None, propose=None, needs=None):
        self.s = service
        self.ask = ask or (lambda prompt: qwen.ask_json(prompt, base_url, qwen.CHAT_TIMEOUTS)[0])
        # split proposals and the place/energy backfill are off when a test injects its own
        # guesser but not these (so no test can reach a real Ollama by accident)
        self.propose = propose if propose is not None else (
            None if guess is not None else (lambda title, notes, minutes:
                                            qwen.propose_step(title, notes, minutes, base_url)))
        self.needs = needs if needs is not None else (
            None if guess is not None else (lambda items: qwen.guess_needs(items, base_url)))
        self.guess = guess or (lambda title, notes: qwen.guess_fields(title, notes, base_url))
        self.run = run or asyncio.to_thread
        self._needs_failed_at: datetime | None = None
        self._needs_tries: dict[int, int] = {}     # backfill asks per item (this process)

    # entry points ---------------------------------------------------------------------
    async def handle(self, text: str, reply_ids: list[int] | None = None) -> Reply:
        """A message Max typed in #inbox or #today."""
        try:
            return await self._handle(text or "", reply_ids or [])
        except Exception:  # noqa: BLE001 - never silent: say something even on a bug
            log.exception("chat handler error")
            return self._said(text, "error", "Something went wrong on my side - nothing was "
                              "changed. Try again, or use \"add: ...\" / the buttons.")

    async def handle_siri(self, text: str) -> Reply:
        """A dictated message from the Siri webhook: always a capture (add, or idea/park)."""
        try:
            return await self._siri(text or "")
        except Exception:  # noqa: BLE001
            log.exception("siri handler error")
            return self._said(text, "error", "Something went wrong on my side - nothing was added. "
                              "Try again in a minute.", actor="siri")

    # helpers --------------------------------------------------------------------------
    def _said(self, text: str, action: str, reply: str, task_id: int | None = None,
              actor: str = "chat", task_ids: list[int] | None = None) -> Reply:
        """A reply that changed nothing - still logged, so silence is auditable."""
        self.s.log(task_id, "chat", actor=actor, text=(text or "")[:300], action=action,
                   reply=reply[:300])
        return Reply(_clip(reply), None, task_ids or ([task_id] if task_id else []), action)

    def _context(self, task_id: int) -> None:
        self.s.meta_set(CONTEXT_KEY, json.dumps({"task_id": task_id,
                                                 "at": self.s.clock.now().isoformat()}))

    def _fresh(self, key: str, ttl: timedelta) -> dict | None:
        d = self.s.meta_json(key)
        if not d:
            return None
        try:
            at = datetime.fromisoformat(d["at"])
        except (KeyError, ValueError, TypeError):
            return None
        return d if self.s.clock.now() - at <= ttl else None


    async def _add(self, title: str, due: datetime | None, parked: bool, actor: str,
                   text: str) -> Reply:
        g = await self.run(self.guess, title, None)
        if not isinstance(g, qwen.Guess):
            g = qwen.Guess()
        t = self.s.add(title, area=g.area, due_at=due, source="inbox",
                       status="parked" if parked else None,
                       payoff=g.payoff if "payoff" in g.guessed else None,
                       likelihood=g.likelihood if "likelihood" in g.guessed else None,
                       estimate_minutes=g.estimate_minutes if "estimate_minutes" in g.guessed else None,
                       guessed=list(g.guessed), actor=actor, info=g.info(),
                       **qwen.needs_kwargs(g))
        self._context(t.id)
        return Reply(_clip(format_added(t, g)), self.s.last_event_id, [t.id], "add")

    async def _did(self, title: str, actor: str) -> Reply:
        """'did: X' - finished off the list; counts for today (and the streak)."""
        g = await self.run(self.guess, title, None)
        if not isinstance(g, qwen.Guess):
            g = qwen.Guess()
        area_guessed = ["area"] if "area" in g.guessed else []
        t = self.s.log_done(title, area=g.area, actor=actor, guessed=area_guessed,
                            info={"model": g.model, "ms": g.ms})
        return Reply(_clip(f"Logged as done: #{t.id} {t.title} ({t.area}). Counts for today."),
                     self.s.last_event_id, [t.id], "did")

    async def _capture(self, text: str, kind: str, actor: str = "chat") -> Reply:
        title, due = parse_add(text, self.s.clock.now())
        if not title:
            example = "did: called the bank" if kind == "did" else f"{kind}: email prof by Friday"
            return self._said(text, "empty", f"Need something after {kind}: - e.g. {example}",
                              actor=actor)
        if kind == "did":
            return await self._did(title, actor)
        return await self._add(title, due, kind in ("park", "idea"), actor, text)

    async def _siri(self, text: str) -> Reply:
        text = text.strip()
        if not text:
            return self._said(text, "empty", "I heard nothing - try again.", actor="siri")
        since = self.s.clock.now() - timedelta(hours=1)
        n = self.s.conn.execute("SELECT COUNT(*) FROM events WHERE kind='created' AND actor='siri' "
                                "AND at >= ?", (db.to_db(since),)).fetchone()[0]
        if n >= SIRI_MAX_PER_HOUR:
            first = self.s.conn.execute(
                "SELECT MIN(at) FROM events WHERE kind='created' AND actor='siri' AND at >= ?",
                (db.to_db(since),)).fetchone()[0]
            until = db.from_db(first) + timedelta(hours=1)
            return self._said(text, "rate_limited",
                              f"Too many Siri adds this hour - paused until {fmt_et(until)}. If this "
                              "wasn't you, delete the webhook in Discord (Server Settings -> "
                              "Integrations -> Webhooks) and make a new one.", actor="siri")
        kind = capture_kind(text)
        if kind is None:
            m = re.match(r"^\s*(idea|park)\b[\s,:;.\-]*", text, re.IGNORECASE)
            if m:
                kind, text = m.group(1).lower(), f"{m.group(1).lower()}: {text[m.end():]}"
            else:
                kind, text = "add", f"add: {text}"
        title, due = parse_add(text, self.s.clock.now())
        if not title:
            return self._said(text, "empty", "I heard nothing - try again.", actor="siri")
        if len(title) > SIRI_MAX_CHARS:
            title = title[:SIRI_MAX_CHARS - 3].rstrip() + "..."
        if kind == "did":
            return await self._did(title, "siri")
        return await self._add(title, due, kind in ("park", "idea"), "siri", text)

    async def _handle(self, text: str, reply_ids: list[int]) -> Reply:
        stripped = text.strip()
        if not stripped:
            return self._said(text, "empty", "I only read text. " + HINT)
        if legend.is_help(stripped):                     # no AI: works with Qwen slow or off
            return self._said(text, "help", legend.help_text())
        if is_new_list(stripped):                        # no AI either
            self.s.meta_set(PENDING_KEY, None)
            return self._new_list(text)
        held = holds.command(self.s, stripped, self.s.clock.now())   # "no calls until 5" etc.
        if held is not None:
            return self._said(text, "calls", held)
        kind = capture_kind(stripped)
        if kind is not None:
            self.s.meta_set(PENDING_KEY, None)
            return await self._capture(stripped, kind)
        pending = self._fresh(PENDING_KEY, PENDING_TTL)
        m = re.fullmatch(r"\s*(#)?(\d{1,4})\s*[.)!]?\s*", stripped)
        if pending and m:
            hashed, n, cands = bool(m.group(1)), int(m.group(2)), pending.get("candidates", [])
            opts = " ".join(f"{i}) #{c}" for i, c in enumerate(cands, 1))
            if hashed:                                   # "#N" is ALWAYS a task id
                tid = n if n in cands else None
            else:
                as_index = cands[n - 1] if 1 <= n <= len(cands) else None
                as_id = n if n in cands else None
                if as_index is not None and as_id is not None and as_index != as_id:
                    # "1" could mean option 1 or task #1 - both are on the list: ask again
                    return self._said(text, "clarify", f"Do you mean option {n} (#{as_index}) or "
                                      f"task #{n}? Reply with the #number, e.g. #{as_index}.")
                tid = as_index if as_index is not None else as_id
            if tid is None:
                return self._said(text, "clarify", f"That isn't one of the choices ({opts}). "
                                  "Reply with the option number or the #number.")
            self.s.meta_set(PENDING_KEY, None)
            return await self._execute(pending["payload"], pending.get("text", ""), reply_ids,
                                       forced=tid)
        if self.s.meta_get(PENDING_KEY):
            self.s.meta_set(PENDING_KEY, None)          # anything else: parse it fresh
        now = self.s.clock.now()
        prompt = build_prompt(stripped, now, self.s.open_tasks())
        try:
            obj = await self.run(self.ask, prompt)
        except Exception as exc:  # noqa: BLE001 - AI down: the hint, nothing executed
            log.info("chat qwen failed: %s", type(exc).__name__)
            obj = None
        if obj is None:
            return self._said(text, "unclear", HINT)
        return await self._execute(validate(obj), stripped, reply_ids)


    # executing one validated action -------------------------------------------------------
    async def _execute(self, p: dict, text: str, reply_ids: list[int],
                       forced: int | None = None) -> Reply:
        a = p["action"]
        text = _norm(text)          # curly apostrophes -> straight, before any word rule
        now = self.s.clock.now()
        if a == "unclear":
            return self._said(text, "unclear", HINT)
        if a == "query":
            return self._query(p.get("kind") or "active", text)
        if a == "undo":
            eid = self.s.latest_undoable(now)
            if eid is None:
                return self._said(text, "undo", "Nothing to undo in the last 48 hours.")
            try:
                res = self.s.undo(eid, actor="chat")
            except UndoRefused as exc:
                return self._said(text, "undo", str(exc))
            return Reply(_clip(res.text), None, [], "undo")
        if a in ("set_mode", "minutes", "swap"):
            await self.backfill()              # label old items before judging what fits
        if a == "set_mode":
            mode = mode_from_text(text, p)
            if mode is None:
                return self._said(text, "ask", "Is today a sick/home day? Say \"sick day\", "
                                  "\"slammed today\" or \"normal day\" to set it.")
            res = self.s.set_mode(mode, actor="chat", source="chat")
            if res.event_id is None:
                return self._said(text, "set_mode", res.text)
            return Reply(_clip(res.text), res.event_id, [t.id for t in res.active], "set_mode")
        if a == "minutes":
            return self._minutes(p, text)
        if a == "resume" and not self.s.by_status("waiting"):
            return self._said(text, "not_found", "Nothing is marked as waiting on someone. "
                              "Say \"waiting on Sam for #3\" to mark one.")
        if a == "add":
            if not ADD_CUES.search(text):
                title = p.get("title") or text
                return self._said(text, "add_unconfirmed",
                                  f"Want me to add that as a to-do? Say \"add: {title}\".")
            title = (p.get("title") or "").strip(_PUNCT)
            due = parse_time_phrase(p.get("when"), text, now) if p.get("when") else None
            if not title:
                title, due2 = parse_add(re.sub(r"^\s*(add|remind me to)\s+", "", text, flags=re.I), now)
                due = due or due2
            if p.get("when") and title.lower().endswith(p["when"].lower()):
                title = title[: -len(p["when"])].strip(_PUNCT + " ")
            if due is None and title:            # date words left inside the model's title
                t2, d2 = parse_add(title, now)
                if d2 is not None and t2:
                    title, due = t2, d2
            if not title:
                return self._said(text, "empty", "Add what? e.g. \"add: email prof by Friday\".")
            reply = await self._add(title, due, False, "chat", text)
            if p.get("when") and due is None:          # never drop a date silently
                reply.text += (f" (I couldn't read the date \"{p['when'][:40]}\" - say "
                               f"\"move #{reply.task_ids[0]} to Friday 3pm\" to set one.)")
            return reply
        if a == "shift_after":
            until = parse_time_phrase(p.get("when"), text, now)
            if until is None:
                return self._said(text, "ask_when", ASK_WHEN)
            if until <= now:
                return self._said(text, "ask_when", f"{fmt_et(until)} has already passed. " + ASK_WHEN)
            moved, eid = self.s.shift_after(until, actor="chat")
            clock = fmt_et(until).split(" ", 2)[2] if until.date() == now.date() else fmt_et(until)
            if moved:
                ids = ", ".join(f"#{t.id}" for t in moved)
                msg = f"Quiet until {clock} - {len(moved)} item{'s' if len(moved) != 1 else ''} moved ({ids})."
            else:
                msg = f"Nothing was due before {clock}, so nothing moved."
            return Reply(msg, eid, [t.id for t in moved], "shift_after")
        # every other action needs exactly one task
        target = await self._target(p, text, reply_ids, forced)
        if isinstance(target, Reply):
            return target
        if a == "swap":
            reason = _word_pick(SWAP_WORDS, text.lower()) or p.get("reason")
            if reason not in rules.SWAP_REASONS:
                return self._said(text, "ask", f"Swap #{target.id} for what reason - too long, "
                                  "can't go out, wrong energy, or waiting on someone?", target.id)
            until = None
            if reason == "waiting" and p.get("when"):
                until = parse_time_phrase(p.get("when"), text, now)
            self._context(target.id)
            return await self.swap_reply(target.id, reason, "chat", text, until=until)
        return self._act(p, text, target, now)


    async def _target(self, p: dict, text: str, reply_ids: list[int], forced: int | None):
        """The one task this action is about, or a Reply asking/explaining."""
        tasks = self.s.open_tasks()
        if p.get("action") == "resume":            # only a waiting item can be resumed
            tasks = [t for t in tasks if t.status == "waiting"]
        by_id = {t.id: t for t in tasks}
        if forced is not None:
            t = by_id.get(forced)
            return t if t else self._said(text, "closed", f"#{forced} is no longer open.")
        exclude = set()
        for v in (p.get("minutes"), p.get("payoff")):
            if v is not None:
                exclude.add(v)
        if p.get("when"):
            exclude |= {int(x) for x in re.findall(r"\d{1,4}", p["when"])}
        words = text
        if p.get("action") == "set_needs":
            # "the Sam call is a phone thing": the where/energy words describe the need, they
            # don't name the task - leave them out when matching titles
            words = text.lower().replace("-", " ")
            for _, rx in PLACE_WORDS + ENERGY_WORDS:
                words = rx.sub(" ", words)
        res = resolve(p.get("task"), words, tasks, exclude)
        if isinstance(res, Resolved):
            return res.task
        if isinstance(res, Ambiguous):
            ids = [t.id for t in res.candidates]
            self.s.meta_set(PENDING_KEY, json.dumps({"payload": p, "text": text, "candidates": ids,
                                                     "at": self.s.clock.now().isoformat()}))
            opts = "  ".join(f"{i}) #{t.id} {t.title[:60]}" for i, t in enumerate(res.candidates, 1))
            return self._said(text, "ambiguous", f"Which one? {opts} - reply with the number.",
                              task_ids=ids)
        if isinstance(res, NotFound):
            if p.get("action") == "done" and not res.query.startswith("#"):   # off-list work
                q = res.query[:60]
                return self._said(text, "not_found",
                                  f"I don't see an open item like \"{q}\". Already did it? Say "
                                  f"\"did: {q}\" and it counts for today.")
            return self._said(text, "not_found",
                              f"I don't see an open item like \"{res.query[:60]}\". Say \"list\" to see them.")
        # no reference in the message: a Discord reply to one of my messages, else the last item
        # we talked about (30 min), else ask
        mine = [i for i in reply_ids if i in by_id]
        if len(mine) == 1:
            return by_id[mine[0]]
        if len(mine) > 1:
            self.s.meta_set(PENDING_KEY, json.dumps({"payload": p, "text": text, "candidates": mine[:4],
                                                     "at": self.s.clock.now().isoformat()}))
            opts = "  ".join(f"{i}) #{n} {by_id[n].title[:60]}" for i, n in enumerate(mine[:4], 1))
            return self._said(text, "ambiguous", f"Which one? {opts} - reply with the number.",
                              task_ids=mine[:4])
        ctx = self._fresh(CONTEXT_KEY, CONTEXT_TTL)
        if ctx and ctx.get("task_id") in by_id:
            return by_id[ctx["task_id"]]
        return self._said(text, "ask_which", ASK_WHICH)


    def _act(self, p: dict, text: str, t: Task, now: datetime) -> Reply:
        a = p["action"]
        try:
            if a in ("done", "kill"):
                closed, promoted = (self.s.mark_done if a == "done" else self.s.kill)(t.id, actor="chat")
                msg = f"{'Done' if a == 'done' else 'Killed'}: #{t.id} {t.title}." + _promo(promoted)
            elif a == "do_now":
                t2 = self.s.do_now(t.id, actor="chat")
                msg = f"Go: #{t.id} {t.title}. I'll check back {fmt_et(t2.next_nag_at)}."
            elif a in ("snooze", "reschedule"):
                when = parse_time_phrase(p.get("when"), text, now)
                if when is None:
                    return self._said(text, "ask_when", ASK_WHEN, t.id)
                if a == "snooze" and when.date() == now.date():
                    if when <= now:
                        return self._said(text, "ask_when", f"{fmt_et(when)} has already passed. "
                                          + ASK_WHEN, t.id)
                    t2 = self.s.snooze(t.id, rules.PLUS2H, explicit=when, actor="chat")
                    msg = (f"Snoozed #{t.id} {t.title} to {fmt_et(when)} ({t2.snooze_count}/2).")
                else:
                    t2, promoted = self.s.reschedule(t.id, when, actor="chat")
                    msg = f"Moved #{t.id} {t.title} to {fmt_et(when)}." + _promo(promoted)
            elif a == "re_estimate":
                m = parse_minutes(text) or p.get("minutes")
                if not m or m <= 0:
                    return self._said(text, "ask", "How long? e.g. \"more like 2 hours\".", t.id)
                self.s.set_fields(t.id, actor="chat", estimate_minutes=m)
                msg = f"#{t.id} {t.title}: estimate now {_mins(int(m))}."
            elif a == "set_priority":
                # Max's own words ("low priority", "critical") beat the AI's number, the same
                # way the date parser beats the AI for dates.
                low = text.lower()
                pay = next((v for w, v in PAYOFF_WORDS if re.search(rf"\b{w}\b", low)), None)
                if pay is None:
                    pay = p.get("payoff")
                if pay is None:
                    return self._said(text, "ask", "How important, 1-5? e.g. \"low priority\" "
                                      "or \"critical\".", t.id)
                pay = min(max(int(pay), 1), 5)
                self.s.set_fields(t.id, actor="chat", payoff=pay)
                msg = f"#{t.id} {t.title}: payoff now {pay}/5."
            elif a == "set_area":
                area = normalize(p.get("area"))
                if area == UNSORTED:
                    low = text.lower()
                    hits = [x for x in AREAS if x.lower() in low]
                    area = hits[0] if len(hits) == 1 else UNSORTED
                if area == UNSORTED:
                    return self._said(text, "ask", "Which area? " + ", ".join(AREAS) + ".", t.id)
                self.s.set_fields(t.id, actor="chat", area=area)
                msg = f"#{t.id} {t.title}: area now {area}."
            elif a == "set_needs":
                # Max's words beat the model's value - but a task's own title words never count
                # ("Call Sam" is not "a phone thing" by itself), so read only the other words
                title_words = set(_tokens(t.title))
                rest = " ".join(w for w in re.findall(r"[a-z0-9']+", text.lower())
                                if w not in title_words)
                place = _word_pick(PLACE_WORDS, rest) or p.get("place")
                energy = _word_pick(ENERGY_WORDS, rest) or p.get("energy")
                fields = {k: v for k, v in (("place", place), ("energy", energy))
                          if v in (rules.PLACES if k == "place" else rules.ENERGIES)}
                if not fields:
                    return self._said(text, "ask", f"Where or how heavy is #{t.id}? e.g. \"#{t.id} "
                                      "is an errand\", \"a phone call\", \"an easy one\".", t.id)
                t2 = self.s.set_fields(t.id, actor="chat", **fields)
                words = {"home": "at home", "out": "out of the house", "phone": "a phone call"}
                msg = (f"#{t.id} {t.title}: {words.get(t2.place, 'place not set')}, "
                       f"{t2.energy or 'energy not set'}.")
            elif a == "wait":
                until = parse_time_phrase(p.get("when"), text, now) if p.get("when") else None
                if until is not None and until <= now:
                    until = None
                t2, promoted = self.s.set_waiting(t.id, until, actor="chat")
                msg = (f"#{t.id} {t.title} is waiting on someone - I'll check in "
                       f"{fmt_et(t2.waiting_until)}." + _promo(promoted))
            elif a == "resume":
                t2, promoted = self.s.resume_waiting(t.id, actor="chat")
                where = "in your three" if t2.status == "active" else "back in the queue, due now"
                msg = f"Good - #{t.id} {t.title} is {where}." + _promo(
                    [x for x in promoted if x.id != t.id])
            else:
                return self._said(text, "unclear", HINT)
        except CantDo as exc:
            return self._said(text, "refused", str(exc), t.id)
        except LadderError:
            return self._said(text, "ladder", f"#{t.id} has used both snoozes. Say \"move #{t.id} to "
                              f"Friday\" (that reschedules it), \"do now\", or \"kill\".", t.id)
        except NotOpen:
            return self._said(text, "closed", f"#{t.id} is already closed.", t.id)
        self._context(t.id)
        return Reply(_clip(msg), self.s.last_event_id, [t.id], a)


    def _new_list(self, text: str) -> Reply:
        res = self.s.new_list(actor="chat")
        ids = [t.id for t in res.active]
        self.s.log(None, "chat", actor="chat", text=(text or "")[:300], action="new_list",
                   reply=res.text[:300], event=res.event_id)
        return Reply(_clip(res.text), res.event_id, ids, "new_list",
                     [(f"Done #{i}", f"{rules.DONE}:{i}") for i in ids])

    def _query(self, kind: str, text: str) -> Reply:
        now = self.s.clock.now()
        if kind in ("due_today", "due_week"):
            limit = (datetime.combine(now.date() + timedelta(days=1), time(0), tzinfo=ET)
                     if kind == "due_today" else now + timedelta(days=7))
            items = [t for t in self.s.by_status("active", "queued")
                     if t.due_at is not None and t.due_at < limit]
            title = "Due today (or overdue)" if kind == "due_today" else "Due in the next 7 days"
        else:
            items = self.s.by_status(kind)
            title = {"active": "Your active three", "queued": "Queued (waiting for a slot)",
                     "parked": "Parked ideas", "waiting": "Waiting on someone"}[kind]
        items = rules.rank(items, now)
        if not items:
            empty = {"due_today": "Nothing is due today.", "due_week": "Nothing is due in the next 7 days.",
                     "active": "Nothing is active right now.", "queued": "The queue is empty.",
                     "parked": "Nothing is parked.",
                     "waiting": "Nothing is waiting on someone."}[kind]
            return self._said(text, f"query:{kind}", empty)
        lines = [f"{title} ({len(items)}):"]
        for t in items[:25]:
            due = f" - due {fmt_et(t.due_at)}" if t.due_at else ""
            st = "" if kind != "due_today" and kind != "due_week" else f" [{t.status}]"
            lines.append(f"#{t.id} {t.title[:70]}{due}{st}")
        if len(items) > 25:
            lines.append(f"... and {len(items) - 25} more")
        return self._said(text, f"query:{kind}", "\n".join(lines), task_ids=[t.id for t in items[:25]])

    # ---- Phase 2.1: "I've got N minutes", Start / Skip, Swap, Split -----------------------
    def minutes_constraint(self, n: int | None, bed: bool) -> rules.DayMode:
        """What fits right now: N minutes (and home + light first from bed), never looser than
        today's day mode (the mode's per-item cap wins when it is smaller)."""
        mode = self.s.current_mode()
        places = set(mode.places) if mode else set(rules.PLACES)
        light = mode.light_first if mode else False
        if bed:
            places = (places & {"home"}) or {"home"}
            light = True
        cap = n
        if mode is not None and mode.max_minutes:
            cap = min(cap, mode.max_minutes) if cap else mode.max_minutes
        return rules.DayMode("custom", frozenset(places), cap, light)

    def minutes_pick(self, c: rules.DayMode, skipped: list[int]) -> Task | None:
        """Best fit: one of the three before the queue (never waiting or parked) - except that
        when light items come first (bed, a sick day) a light queued item beats a focused one
        of the three."""
        now = self.s.clock.now()
        cands = [t for t in self.s.by_status("active", "queued")
                 if t.id not in skipped and rules.fits(t, c)]
        if not cands:
            return None

        def key(t):
            k = rules.fit_key(t, c, now)          # (deadline tier, light, rank...)
            return (k[0], k[1], 0 if t.status == "active" else 1) + k[2:]
        return min(cands, key=key)

    def _fit_line(self, t: Task, n: int | None, c: rules.DayMode) -> str:
        place, energy, mins = rules.needs(t)
        head = f"Best fit for {n} min" if n else "Best fit from bed"
        line = f"{head}: #{t.id} {t.title} (~{rules.fmt_minutes(mins)}, {place}, {energy})."
        mode = self.s.current_mode()
        if mode is not None and n and mode.max_minutes and mode.max_minutes < n:
            line += f" ({mode.name} day: items up to {mode.max_minutes} min.)"
        return line + " Your three stay as they are unless you tap Start."

    def _minutes(self, p: dict, text: str) -> Reply:
        low = text.lower()
        n = parse_minutes(text) or p.get("minutes")
        bed = bool(_BED.search(low)) or (p.get("place") == "home" and p.get("energy") == "light"
                                         and not n)
        if not n and not bed:
            return self._said(text, "ask", "How many minutes have you got? e.g. \"I've got 30 "
                              "minutes\".")
        c = self.minutes_constraint(n, bed)
        best = self.minutes_pick(c, [])
        self.s.meta_set(MINUTES_KEY, json.dumps({"n": n, "bed": bed, "constraint": c.to_json(),
                                                 "skipped": [],
                                                 "at": self.s.clock.now().isoformat()}))
        return self._minutes_reply(text, best, n, c, "chat")

    def _minutes_reply(self, text: str, best: Task | None, n: int | None, c: rules.DayMode,
                       actor: str, skipped: bool = False) -> Reply:
        if best is None:
            opens = [t for t in self.s.by_status("active", "queued")]
            shortest = min(opens, key=lambda t: (rules.needs(t)[2], t.id)) if opens else None
            what = f"in {n} minutes" if n else "from bed"
            msg = ("Nothing else fits" if skipped else f"Nothing on the list fits {what}") + "."
            if shortest is not None:
                msg += (f" Shortest is #{shortest.id} {shortest.title} "
                        f"(~{rules.fmt_minutes(rules.needs(shortest)[2])}) - tap Start if you "
                        "want it anyway.")
                r = self._said(text, "minutes", msg, shortest.id, actor=actor)
                r.buttons = [("Start", f"start:{shortest.id}")]
                return r
            return self._said(text, "minutes", msg, actor=actor)
        r = self._said(text, "minutes", self._fit_line(best, n, c), best.id, actor=actor)
        r.buttons = [("Start", f"start:{best.id}"), ("Skip", f"skip:{best.id}")]
        return r

    def skip_reply(self, task_id: int, actor: str = "button") -> Reply:
        q = self._fresh(MINUTES_KEY, MINUTES_TTL)
        if not q:
            return self._said("", "minutes", "That suggestion expired - say \"I've got 30 "
                              "minutes\" again.", actor=actor)
        q["skipped"] = list(dict.fromkeys(q.get("skipped", []) + [task_id]))
        self.s.meta_set(MINUTES_KEY, json.dumps(q))
        c = rules.DayMode.from_json(q["constraint"])
        best = self.minutes_pick(c, q["skipped"])
        return self._minutes_reply("", best, q.get("n"), c, actor, skipped=True)

    def start_reply(self, task_id: int, actor: str = "button") -> Reply:
        try:
            res = self.s.start(task_id, actor=actor)
        except CantDo as exc:
            return self._said("", "refused", str(exc), task_id, actor=actor)
        except NotOpen:
            return self._said("", "closed", f"#{task_id} is already closed.", task_id, actor=actor)
        t = res.task
        msg = f"Go: #{t.id} {t.title}. I'll check back {fmt_et(t.next_nag_at)}."
        if res.demoted is not None:
            msg += f" #{res.demoted.id} {res.demoted.title} goes back to the queue for now."
        self.s.meta_set(MINUTES_KEY, None)
        return Reply(_clip(msg), res.event_id, [t.id], "start")

    def _options(self, t: Task, choices: list[str]) -> list[tuple[str, str]]:
        return [(rules.LABELS[c], f"{c}:{t.id}") for c in choices]

    async def swap_reply(self, task_id: int, reason: str, actor: str, text: str = "",
                         until: datetime | None = None) -> Reply:
        try:
            res = self.s.swap(task_id, reason, actor=actor, until=until)
        except CantDo as exc:
            return self._said(text, "refused", str(exc), task_id, actor=actor)
        except NotOpen:
            return self._said(text, "closed", f"#{task_id} is already closed.", task_id, actor=actor)
        t = res.task
        if res.forced:
            r = self._said(text, "swap_forced", f"#{t.id} {t.title} has been swapped twice "
                           "already. Decide: Do now, Pick a day, Kill, or Split it.", t.id,
                           actor=actor)
            r.buttons = self._options(t, rules.flagged_options()) + [("Split it",
                                                                     f"splitask:{t.id}")]
            return r
        if res.nothing_fits:
            place, energy, mins = rules.needs(t)
            cond = {"long": f"shorter than ~{rules.fmt_minutes(mins)}",
                    "out": "that can be done without going out",
                    "energy": "at a different energy level"}[reason]
            r = Reply(f"Nothing in the queue {cond} fits today. #{t.id} stays. Options:", None,
                      [t.id], "swap_nothing_fits")
            opts = rules.snooze_options(t.snooze_count)
            r.buttons = self._options(t, opts) + \
                ([] if rules.PICK_DAY in opts else [("Pick a day", f"pickday:{t.id}")]) + \
                [("Split it", f"splitask:{t.id}")]
            return r
        rep = res.replacement
        if reason == "waiting":
            msg = (f"#{t.id} {t.title} is waiting on someone - I'll check in "
                   f"{fmt_et(t.waiting_until)}.")
        else:
            msg = f"Swapped out #{t.id} {t.title} ({rules.SWAP_LABELS[reason].lower()})."
        if rep is not None:
            place, _energy, mins = rules.needs(rep)
            msg += f" In: #{rep.id} {rep.title} (~{rules.fmt_minutes(mins)}, {place})."
        elif reason == "waiting":
            msg += " Slot left open."
        reply = Reply(msg, res.event_id, [t.id] + ([rep.id] if rep else []), "swap")
        if reason == "long" and res.long_swaps >= 2:
            offer = await self._propose_split(t)
            if offer is not None:
                step, m = offer
                reply.text += (f" Split it? First step: \"{step}\" (~{m} min).")
                reply.buttons = [("Split it", f"split:{t.id}"), ("Keep as is", f"splitno:{t.id}")]
        reply.text = _clip(reply.text)
        return reply

    async def _propose_split(self, t: Task) -> tuple[str, int] | None:
        """Ask the local AI for a first step and remember the offer for 24 h (the button uses
        it). None when the AI is off or unsure - then no offer is made."""
        if self.propose is None:
            return None
        try:
            got = await self.run(self.propose, t.title, t.notes, t.estimate_minutes)
        except Exception:  # noqa: BLE001
            got = None
        if not got:
            return None
        step, m = got
        self.s.meta_set(SPLIT_OFFER + str(t.id), json.dumps(
            {"step": step, "minutes": int(m), "at": db.to_db(self.s.clock.now())}))
        return step, int(m)

    async def offer_split(self, task_id: int, actor: str = "button") -> Reply:
        t = self.s.get(task_id)
        if t is None or t.status not in ("active", "queued"):
            return self._said("", "closed", f"#{task_id} can't be split now.", task_id, actor=actor)
        offer = await self._propose_split(t)
        if offer is None:
            return self._said("", "split_unavailable", f"I couldn't come up with a first step for "
                              f"#{t.id} right now (the local AI is off or unsure). Add one "
                              "yourself with \"add: ...\".", t.id, actor=actor)
        step, m = offer
        r = self._said("", "split_offer", f"Split #{t.id} {t.title}? First step: \"{step}\" "
                       f"(~{m} min) - it takes #{t.id}'s slot and #{t.id} waits in the queue.",
                       t.id, actor=actor)
        r.buttons = [("Split it", f"split:{t.id}"), ("Keep as is", f"splitno:{t.id}")]
        return r

    def accept_split(self, task_id: int, actor: str = "button") -> Reply:
        rec = self.s.meta_json(SPLIT_OFFER + str(task_id))
        if not rec or db.from_db(rec.get("at")) < self.s.clock.now() - timedelta(hours=24):
            return self._said("", "split_expired", "That split offer expired - tap Split it "
                              "again for a fresh one.", task_id, actor=actor)
        try:
            child, parent, sid = self.s.split(task_id, rec["step"], rec["minutes"], actor=actor)
        except (CantDo, NotOpen) as exc:
            return self._said("", "refused", str(exc) or f"#{task_id} can't be split now.",
                              task_id, actor=actor)
        self.s.meta_set(SPLIT_OFFER + str(task_id), None)
        where = "is in your three" if child.status == "active" else "is queued"
        return Reply(_clip(f"Split: #{child.id} {child.title} (~{child.estimate_minutes} min) "
                           f"{where}; #{parent.id} {parent.title} waits in the queue."),
                     sid, [child.id, parent.id], "split")

    def decline_split(self, task_id: int, actor: str = "button") -> Reply:
        self.s.meta_set(SPLIT_OFFER + str(task_id), None)
        self.s.meta_set(f"split_declined:{task_id}",
                        json.dumps({"at": db.to_db(self.s.clock.now())}))
        return self._said("", "split_declined", "OK, left as is.", task_id, actor=actor)

    # ---- place/energy backfill for items added before Phase 2.1 --------------------------
    NEEDS_RETRY = timedelta(minutes=5)
    NEEDS_MAX_TRIES = 3            # per item and process; after that it waits for a restart

    async def backfill(self, limit: int = qwen.NEEDS_BATCH) -> int:
        """Ask the local AI once for the place/energy of up to 8 open items that have none
        (one small batch per call; the minute loop calls this every minute until all are
        labelled). Writes only empty columns, marked as guesses (actor qwen, untagged - never
        part of a chat message's changes). Never raises. On failure nothing is written - the
        items stay unknown (the fit then uses the title hints, then home/light) - and the
        batch is retried on a later minute, after a 5-minute rest. An item the AI keeps
        skipping is not asked about more than 3 times per run. Returns the number labelled."""
        if self.needs is None:
            return 0
        now = self.s.clock.now()
        if self._needs_failed_at is not None and now - self._needs_failed_at < self.NEEDS_RETRY:
            return 0
        tries = self._needs_tries
        todo = sorted((t for t in self.s.open_tasks()
                       if t.status != "parked" and (t.place is None or t.energy is None)
                       and tries.get(t.id, 0) < self.NEEDS_MAX_TRIES),
                      key=lambda t: (tries.get(t.id, 0), t.id))[:limit]
        if not todo:
            return 0
        try:
            got = await self.run(self.needs, [(t.id, t.title) for t in todo])
        except Exception:  # noqa: BLE001
            got = {}
        if not got:
            self._needs_failed_at = now
            return 0
        for t in todo:
            if t.id not in got or None in got[t.id]:
                tries[t.id] = tries.get(t.id, 0) + 1
        token = db.EVENT_CONTEXT.set({})
        try:
            for tid, (place, energy) in got.items():
                self.s.set_needs_guess(tid, place, energy)
        finally:
            db.EVENT_CONTEXT.reset(token)
        return len(got)
