"""Command legend (added 2026-10-10): one source, two renderings.

- embed_dict(): the card the bot keeps in #commands (re-posted if deleted, edited if this file
  changes).
- help_text(): the compact reply to "help" / "commands" / "?" typed in #inbox or #today. It
  never asks the local AI, so it works with Qwen slow or off. Must stay under the 1990-char
  reply limit (tests/test_legend.py checks).
Every example here is something chat.py / parser.py actually handles (see SETUP.md,
"Talking to Dispatch" and "Fitting the list to your day").
"""
from __future__ import annotations

import re

META_MESSAGE = "commands_message_id"
COLOR = 0x57F287
HELP_RE = re.compile(r"\s*(help|commands?|cmds|legend|\?)\s*[?!.]*\s*", re.IGNORECASE)

# (section title, [(example, what it does)]) - "" means the example explains itself.
SECTIONS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Add", [
        ("add: call Sam tomorrow 3pm", "new to-do; the when is optional"),
        ("park: X  /  idea: X", "saved, never pings (shows Sundays)"),
        ("did: called the bank", "off-list work, counts as done today"),
    ]),
    ("Act on an item (#3 = its number)", [
        ("done #3  /  kill #3", ""),
        ("move #3 to Friday 1pm", ""),
        ("snooze #3 until 3pm", ""),
        ("undo that", "or tap Undo (works 48 h)"),
    ]),
    ("Fix its numbers", [
        ("#3 more like 20 minutes", "time it takes"),
        ("#3 is critical", "or important / low priority / not important"),
        ("#3 is an errand  /  #3 is a phone call", "where"),
        ("#3 is a focus job  /  #3 is an easy one", "energy"),
    ]),
    ("Fit your day", [
        ("sick day  /  slammed today  /  back to normal", "or the 8 AM buttons"),
        ("I've got 20 minutes", "one pick that fits: Start / Skip"),
        ("I'm slammed until 4pm", "holds every reminder until then"),
        ("new list", "re-rank now after finishing things (Undo puts it back)"),
    ]),
    ("Waiting on someone", [
        ("waiting on Sam for #4", "add 'check in Friday' to pick the day"),
        ("Sam got back to me on #4", "back on the list"),
        ("what am I waiting on", ""),
    ]),
    ("Look, change nothing", [
        ("list  /  what's due today  /  what's parked", ""),
        ("what's on my plate this week", ""),
    ]),
]

BUTTONS = ("Done · +2h · Tonight (8 PM) · Tomorrow (9 AM). Two snoozes per item, then "
           "Do now / Pick a day / Kill. Swap #N asks why: Too long, Can't go out, Wrong energy, "
           "Waiting.")
TIPS = ("Use the #number when you can - it's the surest. Asked \"Which one?\" Reply 2 or #16 "
        "within 10 min. Dates: Fri, 10/3, tonight, in 2 days; next Wed = the week after. "
        "Quiet midnight-8 AM. Type help in #inbox to get this list.")


def is_help(text: str) -> bool:
    return bool(HELP_RE.fullmatch(text or ""))


def _line(example: str, what: str, code: str = "`") -> str:
    ex = "  /  ".join(f"{code}{p.strip()}{code}" for p in example.split("  /  "))
    return f"{ex} - {what}" if what else ex


def embed_dict() -> dict:
    fields = [{"name": title, "value": "\n".join(_line(e, w) for e, w in rows), "inline": False}
              for title, rows in SECTIONS]
    fields.append({"name": "Buttons", "value": BUTTONS, "inline": False})
    fields.append({"name": "Tips", "value": TIPS, "inline": False})
    return {"title": "Dispatch commands",
            "description": "Type these in #inbox or #today. Plain sentences work too - "
                           "these are the forms it never misreads.",
            "color": COLOR, "fields": fields}


def help_text() -> str:
    out = ["**Dispatch commands** (type in #inbox or #today)"]
    for title, rows in SECTIONS:
        out.append(f"**{title}**")
        out.extend(_line(e, w) for e, w in rows)
    out.append(f"**Buttons** {BUTTONS}")
    out.append(f"**Tips** {TIPS}")
    return "\n".join(out)
