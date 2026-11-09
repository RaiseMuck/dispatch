"""Ask the local Qwen model (via Ollama) for JSON: field guesses for a new task, or which chat
action a sentence means (chat.py builds that prompt).

Nice-to-have by design. ANY problem - Ollama not running, no qwen model, slow answer, an answer
that is not valid JSON - falls through to the next model and finally to deterministic
defaults; nothing here raises. The bot calls this in a worker thread so Discord never freezes.

Model policy: installed qwen models in QWEN_PREFERENCE order (env QWEN_MODEL, if set and
installed, goes first). The first model gets the primary timeout, the second the fallback one.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.request
from dataclasses import asdict, dataclass, field

from .areas import AREAS, UNSORTED, normalize

log = logging.getLogger("dispatch.qwen")
DEFAULT_URL = "http://127.0.0.1:11434"

# Order chosen from the live eval (tools/eval_qwen.py -> tools/eval-results.md, 2026-09-28,
# RTX 5090): chat 27b 32/32 correct, 0 wrong-task, warm median 0.88 s / p95 1.85 s, cold load
# 5.9-9.5 s; 4b 27/32, 0 wrong-task, p95 0.33 s. Field guess p95: 27b 1.0 s, 4b 0.26 s.
# Plan rule 12.4: 27b passes (>= 29/32, warm p95 <= 10 s) -> 27b first, 4b as the fallback.
QWEN_PREFERENCE = ("qwen3.5:27b", "qwen3.5:4b")
GUESS_TIMEOUTS = (45.0, 15.0)     # seconds: primary model, fallback model. Raised 2026-10-10 from
# (12, 6): after 30 idle minutes the 27b unloads and its cold load (with 4b also resident)
# overran 10-12 s, so the first message after a gap fell to the weaker 4b and misread
# "more like 10 minutes". 45 s covers a cold load; warm calls still answer in ~1-2 s.
CHAT_TIMEOUTS = (45.0, 15.0)
CONNECT_TIMEOUT = 2.0
KEEP_ALIVE = "30m"                # keep the model loaded so the cold-load cost is paid once


def _get_json(url: str, timeout: float, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def choose_models(base_url: str = DEFAULT_URL, timeout: float = CONNECT_TIMEOUT) -> list[str]:
    """Installed qwen model names, best first."""
    tags = _get_json(base_url.rstrip("/") + "/api/tags", timeout)
    models = [m for m in tags.get("models", []) if "qwen" in m.get("name", "").lower()]
    names = [m["name"] for m in models]
    order: list[str] = []
    env = os.environ.get("QWEN_MODEL", "").strip()
    if env and env in names:
        order.append(env)
    order += [p for p in QWEN_PREFERENCE if p in names and p not in order]
    order += [m["name"] for m in sorted(models, key=lambda m: m.get("size", 0))
              if m["name"] not in order]
    return order


def find_model(base_url: str = DEFAULT_URL, timeout: float = CONNECT_TIMEOUT) -> str | None:
    """The model that would be tried first (kept for Phase 1 callers)."""
    names = choose_models(base_url, timeout)
    return names[0] if names else None


def _extract_json(text: str) -> dict | None:
    """First balanced {...} block in the answer (after removing <think> sections), or None."""
    if not isinstance(text, str):
        return None
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                    except ValueError:
                        break
                    return obj if isinstance(obj, dict) else None
        start = text.find("{", start + 1)
    return None


def generate(model: str, prompt: str, base_url: str, timeout: float, num_predict: int = 200) -> dict | None:
    """One /api/generate call with JSON output forced. Raises on network errors."""
    payload = {"model": model, "prompt": prompt, "stream": False, "format": "json",
               "think": False, "keep_alive": KEEP_ALIVE,
               "options": {"temperature": 0, "num_predict": num_predict}}
    out = _get_json(base_url.rstrip("/") + "/api/generate", timeout, payload)
    return _extract_json(out.get("response", ""))


def ask_json(prompt: str, base_url: str = DEFAULT_URL, timeouts: tuple = CHAT_TIMEOUTS,
             connect_timeout: float = CONNECT_TIMEOUT,
             num_predict: int = 200) -> tuple[dict | None, str | None, int]:
    """(answer, model that answered, milliseconds). (None, None, ms) if nothing usable."""
    t0 = time.monotonic()
    ms = lambda: int((time.monotonic() - t0) * 1000)        # noqa: E731
    try:
        models = choose_models(base_url, connect_timeout)
    except Exception as exc:  # noqa: BLE001 - Ollama down: callers use defaults
        log.info("qwen unavailable: %s", type(exc).__name__)
        return None, None, ms()
    for model, tmo in zip(models, timeouts):
        try:
            obj = generate(model, prompt, base_url, tmo, num_predict)
        except Exception as exc:  # noqa: BLE001 - slow or broken model: try the next one
            log.info("qwen %s failed: %s", model, type(exc).__name__)
            continue
        if obj is not None:
            return obj, model, ms()
        log.info("qwen %s gave no JSON", model)
    return None, None, ms()


HINTS = {
    "Coreflow/CLARA": "Coreflow Mechanical, the CLARA AI phone dispatcher, Acme Plumbing, Sam, clients, pilots",
    "School": "Ohio State classes, homework, quizzes, exams, group projects, the Keystone class venture",
    "Side-venture ideas": "brand-new business ideas that are not started yet",
    "Portfolio": "investing, stocks, trades, allocation",
    "System improvement": "the PC, the Mac, the notes vault, backups, automation, software tools",
    "Personal admin": "errands, store returns, bills, money paperwork, forms, appointments",
    "Fantasy": "fantasy football and fantasy hockey leagues, waivers, lineups, trades",
    "Nationwide": "the Nationwide job and career",
    "Home & life": "house, family, friends, garden, chores, fitness",
}
GUESS_FIELDS = ("area", "payoff", "likelihood", "estimate_minutes", "place", "energy")
PLACES = ("home", "out", "phone")
ENERGIES = ("light", "focused")
# One line each, shared by the field guess, the backfill and the chat prompt.
PLACE_LINE = ('place: "out" = he must physically go somewhere (store, office, drive); '
              '"phone" = a call or live conversation; "home" = anything doable at home or a desk')
ENERGY_LINE = ('energy: "light" = easy, quick or mindless, doable tired; '
               '"focused" = needs a clear head (thinking, writing, deciding)')


def area_lines() -> str:
    return "\n".join(f"- {a}: {HINTS.get(a, '')}" for a in AREAS)


def build_guess_prompt(title: str, notes: str | None = None) -> str:
    notes = (notes or "none").replace('"', "'")[:300]
    title = title.replace('"', "'")[:300]
    return ("You label a personal to-do for a task manager. Reply with ONE JSON object and nothing else:\n"
            '{"area": <one of the area names below, copied exactly>, '
            '"payoff": <integer 1-5, 5 = big consequence if done>, '
            '"likelihood": <number 0-1, chance this task actually matters / will be needed>, '
            '"estimate_minutes": <integer, honest minutes of focused work>, '
            '"place": <"home" | "out" | "phone">, "energy": <"light" | "focused">}\n'
            f"{PLACE_LINE}\n{ENERGY_LINE}\n"
            f"Areas:\n{area_lines()}\n"
            f'To-do: "{title}"\n'
            f'Notes: "{notes}"\n')


@dataclass
class Guess:
    area: str = UNSORTED
    payoff: int = 3
    likelihood: float = 0.7
    estimate_minutes: int = 30
    place: str = "home"
    energy: str = "light"
    guessed: list[str] = field(default_factory=list)   # exactly the fields the AI supplied
    model: str | None = None
    ms: int = 0

    def info(self) -> dict:
        """What goes into the 'created' event detail."""
        d = asdict(self)
        return {"guess": {k: d[k] for k in GUESS_FIELDS if k in self.guessed},
                "model": self.model, "ms": self.ms}


def match_area(answer: str) -> str:
    answer = re.sub(r"<think>.*?</think>", "", answer, flags=re.DOTALL).strip()
    first = answer.splitlines()[0].split(":")[0].strip(" -*.`\"'") if answer else ""
    exact = normalize(first)
    if exact != UNSORTED:
        return exact
    low = answer.lower()
    hits = [a for a in AREAS if a.lower() in low]
    return hits[0] if len(hits) == 1 else UNSORTED


def _int_in(v, lo: int, hi: int) -> int | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x or x in (float("inf"), float("-inf")):
        return None
    return int(min(max(round(x), lo), hi))


def validate_guess(obj: dict | None) -> Guess:
    """Clamp each field; a field that does not parse keeps its default and is not 'guessed'."""
    g = Guess()
    if not isinstance(obj, dict):
        return g
    if isinstance(obj.get("area"), str):
        area = match_area(obj["area"])
        if area != UNSORTED:
            g.area = area
            g.guessed.append("area")
    p = _int_in(obj.get("payoff"), 1, 5)
    if p is not None:
        g.payoff = p
        g.guessed.append("payoff")
    try:
        lk = float(obj.get("likelihood"))
        if lk != lk:
            raise ValueError
        if 1.0 < lk <= 100.0:
            lk = lk / 100.0                   # "70" meant 70 %
        g.likelihood = round(min(max(lk, 0.05), 1.0), 2)
        g.guessed.append("likelihood")
    except (TypeError, ValueError):
        pass
    e = _int_in(obj.get("estimate_minutes"), 5, 2400)
    if e is not None:
        g.estimate_minutes = e
        g.guessed.append("estimate_minutes")
    for name, allowed in (("place", PLACES), ("energy", ENERGIES)):
        v = obj.get(name)
        if isinstance(v, str) and v.strip().lower() in allowed:
            setattr(g, name, v.strip().lower())
            g.guessed.append(name)
    return g


def needs_kwargs(g: "Guess") -> dict:
    """place/energy for TaskService.add - only the ones the AI actually supplied."""
    return {k: getattr(g, k) for k in ("place", "energy") if k in g.guessed}


def guess_fields(title: str, notes: str | None = None, base_url: str = DEFAULT_URL,
                 timeouts: tuple = GUESS_TIMEOUTS, connect_timeout: float = CONNECT_TIMEOUT) -> Guess:
    """area, payoff, likelihood, estimate_minutes for a new task. Never raises."""
    try:
        obj, model, ms = ask_json(build_guess_prompt(title, notes), base_url, timeouts,
                                  connect_timeout, num_predict=160)
        g = validate_guess(obj)
        g.model, g.ms = (model if g.guessed else None), ms
        return g
    except Exception as exc:  # noqa: BLE001 - by design, nothing here may break task capture
        log.info("qwen guess failed: %s", type(exc).__name__)
        return Guess()


def pick_area(title: str, base_url: str = DEFAULT_URL, timeout: float = CONNECT_TIMEOUT,
              answer_timeout: float = 20.0) -> str:
    """Return one of the nine area names, or 'unsorted'. Never raises. (Phase 1 API.)"""
    return guess_fields(title, None, base_url, (answer_timeout, min(answer_timeout, 6.0)),
                        timeout).area


# ---- Phase 2.1: place/energy backfill and split proposals -------------------------------
# Measured 2026-09-29 on a copy of the 23 open live items (RTX 5090, warm): 27b 3.5-3.8 s per
# batch of 8 (one outlier at 34 s), 7.1 s for all 23 at once; 4b 0.6-0.7 s per 8. Batches of
# 8 with their own timeouts (5x the typical 27b call, then 4b) leave plenty of headroom; a
# batch that still fails is simply retried on a later minute.
NEEDS_BATCH = 8
NEEDS_TIMEOUTS = (60.0, 20.0)     # seconds: primary model, fallback model (raised 2026-10-10, see above)


def build_needs_prompt(items: list[tuple[int, str]]) -> str:
    lines = "\n".join(f"#{i} {t.replace(chr(10), ' ')[:160]}" for i, t in items)
    return ("You label personal to-dos for a task manager. For EVERY to-do below give its place "
            "and energy.\n"
            f"{PLACE_LINE}\n{ENERGY_LINE}\n"
            'Reply with ONE JSON object: {"items": [{"id": <id>, "place": "home|out|phone", '
            '"energy": "light|focused"}]}\n'
            f"To-dos:\n{lines}\n")


def validate_needs(obj: dict | None, ids: set[int]) -> dict[int, tuple[str | None, str | None]]:
    """Per item: (place or None, energy or None). Unknown ids and bad values are dropped."""
    out: dict[int, tuple[str | None, str | None]] = {}
    if not isinstance(obj, dict) or not isinstance(obj.get("items"), list):
        return out
    for it in obj["items"]:
        if not isinstance(it, dict):
            continue
        try:
            i = int(str(it.get("id")).lstrip("#"))
        except (TypeError, ValueError):
            continue
        if i not in ids:
            continue
        p = str(it.get("place") or "").strip().lower()
        e = str(it.get("energy") or "").strip().lower()
        p, e = (p if p in PLACES else None), (e if e in ENERGIES else None)
        if p or e:
            out[i] = (p, e)
    return out


def guess_needs(items: list[tuple[int, str]], base_url: str = DEFAULT_URL,
                timeouts: tuple = NEEDS_TIMEOUTS,
                connect_timeout: float = CONNECT_TIMEOUT) -> dict[int, tuple[str | None, str | None]]:
    """place/energy for up to NEEDS_BATCH existing items in ONE call. Never raises; {} on failure."""
    items = items[:NEEDS_BATCH]
    if not items:
        return {}
    try:
        obj, _model, _ms = ask_json(build_needs_prompt(items), base_url, timeouts,
                                    connect_timeout, num_predict=20 + 30 * len(items))
        return validate_needs(obj, {i for i, _ in items})
    except Exception as exc:  # noqa: BLE001
        log.info("qwen needs backfill failed: %s", type(exc).__name__)
        return {}


def build_step_prompt(title: str, notes: str | None, minutes: int | None) -> str:
    title = title.replace('"', "'")[:300]
    notes = (notes or "none").replace('"', "'")[:300]
    return ("Max cannot start this to-do because it is too big. Propose the FIRST concrete step, "
            "doable in <= 20 minutes at home. Reply with ONE JSON object: "
            '{"first_step": "<imperative, <= 12 words>", "minutes": <5-20>}\n'
            f'To-do: "{title}"\nNotes: "{notes}"\n'
            f"Estimate: {minutes or 'unknown'} minutes\n")


def validate_step(obj: dict | None) -> tuple[str, int] | None:
    if not isinstance(obj, dict):
        return None
    step = obj.get("first_step")
    if not isinstance(step, str):
        return None
    step = " ".join(step.split()).strip(" .\"'")
    if not step or len(step) > 120:
        return None
    m = _int_in(obj.get("minutes"), 5, 20)
    return step, (m if m is not None else 15)


def propose_step(title: str, notes: str | None = None, minutes: int | None = None,
                 base_url: str = DEFAULT_URL, timeouts: tuple = GUESS_TIMEOUTS,
                 connect_timeout: float = CONNECT_TIMEOUT) -> tuple[str, int] | None:
    """(first step, minutes 5-20) for a split offer, or None (no offer). Never raises."""
    try:
        obj, _model, _ms = ask_json(build_step_prompt(title, notes, minutes), base_url, timeouts,
                                    connect_timeout, num_predict=80)
        return validate_step(obj)
    except Exception as exc:  # noqa: BLE001
        log.info("qwen split proposal failed: %s", type(exc).__name__)
        return None
