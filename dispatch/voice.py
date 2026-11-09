"""Phone line (added 2026-10-18): Max calls his Vapi number to hear his list and change it.

Nothing on the PC is opened to the internet. The bot only makes outgoing requests:
  push  - each minute it rebuilds a plain-text snapshot of the list; when that changed, it
          PATCHes the Vapi assistant's instructions, so every call starts current.
  pull  - each minute it lists recent ended calls (GET /call). For each new call FROM MAX'S
          OWN NUMBER the local AI (Qwen) reads the transcript and writes the changes Max asked
          for as ordinary Dispatch commands ("done #34", "add: call the bank tomorrow 3pm").
          Each one then goes through chat.Router exactly as if typed in #inbox: same closed
          action list, same "ask, don't guess", same Undo. The bot posts the results in #today.
  gate  - the voice AI asks for a 4-digit code before saying anything (stops casual and
          robo callers; it is an instruction to an AI, not a lock). Changes are applied only
          for calls from MAX_PHONE - that check is here, in code, and cannot be talked around.
Outbound ("it calls me") lives in calls.py, on a second free Vapi number that can dial out.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import db, qwen, rules
from .clock import ET, fmt_et

log = logging.getLogger("dispatch.voice")

API = "https://api.vapi.ai"
HASH_KEY, SEEN_KEY, TRIES_KEY = "voice_snapshot_hash", "voice_seen_calls", "voice_extract_tries"
LOOKBACK = timedelta(hours=48)         # how far back each pull looks for ended calls (PC asleep overnight)
MAX_TRIES = 5                          # minutes to keep retrying a call the local AI couldn't read
MAX_COMMANDS = 12
SEEN_KEEP = 200
VOICE = {"provider": "11labs", "voiceId": "nPczCjzI2devNBz1zQrb",   # "Brian": deep, resonant, natural
         "model": "eleven_flash_v2_5", "stability": 0.6, "similarityBoost": 0.75, "speed": 0.82}
MODEL = "gpt-4.1-mini"                 # fast, cheap, no "thinking" delay on the phone
EXTRACT_TIMEOUTS = (90.0, 30.0)        # seconds: primary model, fallback model
MAX_TOKENS = 160                       # hard cap per spoken turn (~40 s at speed 0.82) - backstop for "short turns"
INTERRUPT_PHRASES = ["stop", "wait", "hold on", "hang on", "one second", "one sec", "pause",
                     "augustine", "can i talk", "let me talk", "shut up", "quiet", "enough"]
# Backchannel that must NOT stop it mid-sentence (2026-11-02: at 0 words it stopped for breaths and
# "yeah", then restarted - the choppy, doubled "Got it. Got it."). "alright" is left out on
# purpose: he uses it to cut in.
ACK_PHRASES = ["yeah", "yes", "uh-huh", "mm-hmm", "mhmm", "okay", "ok", "right", "sure", "got it"]
# Words only his world uses, so the transcriber stops guessing ("consult Clive" for Claude, 10-31).
KEYTERMS = ["Augustine", "Dispatch", "Claude", "Hooli", "Coreflow", "CLARA", "Acme Plumbing", "Sam",
            "Nationwide", "Globex", "Initech", "Umbrella", "Priya", "Carmen", "Workday", "Handshake",
            "Keystone", "Discord", "Ohio State", "Behavioral Finance"]
UNANSWERED_KEY = "calls_unanswered:"   # + ET date: an outbound call he didn't pick up that day
BRIEF_KEY = "call_last_brief"          # what the last outbound call was about (for a missed call)
# Carrier recordings the transcriber writes down as if Max spoke (2026-11-02: once the opener could
# be interrupted, every voicemail greeting arrived as "User:" lines, so a missed call looked
# answered and the one-unanswered-call stop never fired).
CARRIER_RE = re.compile(r"(?i)forwarded to (?:voicemail|an automated)|is not available|at the tone|"
                        r"record your message|finished recording|mailbox is full|"
                        r"cannot accept (?:any )?messages|voice messaging system|leave a message")


class VapiError(Exception):
    """Vapi refused a request; str(exc) never contains the API key."""


class Vapi:
    def __init__(self, key: str, timeout: float = 30.0):
        self._key, self.timeout = key, timeout

    def __repr__(self) -> str:
        return "Vapi(key=***)"

    def call(self, method: str, path: str, body: dict | None = None):
        req = urllib.request.Request(
            API + path, method=method,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json",
                     "User-Agent": "dispatch-voice/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            raise VapiError(f"{method} {path.split('?')[0]} -> HTTP {e.code}: {detail}") from None


# ---- the snapshot the phone AI reads ---------------------------------------------------------
def cut(text: str, n: int) -> str:
    """At most n characters, ending on a whole word (a title cut mid-word is read aloud as
    'backup puru' or 'Antho')."""
    text = " ".join(text.split())
    if len(text) <= n:
        return text
    head = text[:n + 1].rsplit(" ", 1)[0] if " " in text[:n + 1] else text[:n]
    if head.count("(") > head.count(")"):        # never leave "(Newegg refund on a" hanging
        head = head[:head.rindex("(")]
    return head.rstrip(" ,;:-(+")


def _line(t, now: datetime) -> str:
    due = f", due {fmt_et(t.due_at)}" if t.due_at else ""
    late = " - OVERDUE" if t.due_at is not None and t.due_at < now else ""
    return f"#{t.id} {cut(t.title, 90)} (about {rules.fmt_minutes(rules.needs(t)[2])}{due}{late})"


def _ev(e: dict) -> str:
    if e["all_day"]:
        return f"{e['title']} (all day)"
    return f"{e['_start']:%I:%M %p} {e['title']}".lstrip("0")


def snapshot(service, now: datetime) -> str:
    """The briefing the phone AI reads before every call: the three with why and their notes,
    today's calendar, the 7-day view (calendar + due + check-ins), up next, waiting, holds."""
    from . import holds, pcclaude                   # late: both are optional add-ons
    queued = rules.rank(service.by_status("queued"), now)
    rest = [t for t in queued if t.due_at is None]
    # The same list Discord shows: his three picks, then every urgent or overdue item that Discord
    # also pings about hourly (service.due_batch). Phone and Discord must never disagree.
    active, urgent = service.today_list(now)
    shown = {t.id for t in active} | {t.id for t in urgent}
    out = [f"TODAY'S LIST - exactly what Discord shows him ({len(active) + len(urgent)} in all: "
           f"his {len(active)} picks first, then the urgent or overdue ones):"]
    for i, t in enumerate(active, 1):
        out.append(f"PICK {i}: " + _line(t, now) + f" - why it's here: {rules.explain(t, now)}")
        if t.notes:
            out.append(f"   notes: {' '.join(t.notes.split())[:500]}")
    for t in urgent:
        out.append("URGENT: " + _line(t, now) + f" - why: {rules.explain(t, now)}")
        if t.notes:
            out.append(f"   notes: {' '.join(t.notes.split())[:300]}")
    if not active and not urgent:
        out.append("(nothing active)")
    today = now.astimezone(ET).date()
    open_ = service.by_status("active", "queued", "waiting")
    out += ["", "THE NEXT 7 DAYS (calendar | due in Dispatch | check-ins):"]
    for i in range(7):
        d = today + timedelta(days=i)
        label = "Today" if i == 0 else ("Tomorrow" if i == 1 else f"{d:%A} {d.month}/{d.day}")
        bits = []
        ev = [_ev(e) for e in pcclaude.events_on(service, d)]
        if ev:
            bits.append("calendar: " + "; ".join(ev))
        due = [_line(t, now) for t in open_ if t.due_at and t.due_at.astimezone(ET).date() == d]
        if due:
            bits.append("due: " + "; ".join(due))
        waits = [f"check in on #{t.id} {cut(t.title, 60)}" for t in open_
                 if t.waiting_until and t.waiting_until.astimezone(ET).date() == d]
        if waits:
            bits.append("; ".join(waits))
        out.append(f"- {label}: " + (" | ".join(bits) if bits else "nothing scheduled"))
    late = [t for t in queued if t.due_at and t.due_at < now and t.id not in shown]
    if late:
        out += ["", "OVERDUE IN THE QUEUE:"] + [_line(t, now) for t in late[:6]]
    if not pcclaude.calendar(service).get("events"):
        out.append("(Calendar not loaded yet.)")
    if rest:
        out += ["", f"UP NEXT IN DISPATCH (no deadline, best first, {len(rest)} in all):"] + \
            [_line(t, now) for t in rest[:8]]
    out += ["", f"CALLS: {holds.status(service, now)}"]
    waiting = service.by_status("waiting")
    if waiting:
        out += ["", "WAITING ON SOMEONE:"] + [
            f"#{t.id} {cut(t.title, 90)}" + (f" (check in {fmt_et(t.waiting_until)})"
                                         if t.waiting_until else "") for t in waiting[:8]]
    mode = service.mode_record(now)
    if mode and mode.get("mode") not in (None, "normal"):
        out += ["", f"TODAY'S MODE: {rules.MODE_LABELS.get(mode['mode'], mode['mode'])}"]
    parked = len(service.by_status("parked"))
    if parked:
        out += ["", f"Parked ideas: {parked} (never read these out unless asked)."]
    return "\n".join(out)


def _today(now: datetime) -> str:
    d = now.astimezone(ET)
    return f"{d:%A} {d.month}/{d.day}"


ABOUT_MAX = """- The owner: a full-time student who also works part-time and is building a side venture.
- Fixed times (classes, shifts, meetings) come from the calendar briefing above; never guess them.
- Not a morning person. Stalls on tedious small tasks and flies in focused sprints.
- Wants it direct, like a sharp peer. No hype, no fluff, no generic motivation."""


_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"]
_ORD_ONES = ["", "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth",
             "ninth", "tenth", "eleventh", "twelfth", "thirteenth", "fourteenth", "fifteenth",
             "sixteenth", "seventeenth", "eighteenth", "nineteenth"]


_CARD = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
         "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
         "eighteen", "nineteen", "twenty"]


def _ordinal_words(n: int) -> str:
    """1..31 as spoken words: 29 -> 'twenty-ninth' (the voice reads '10/29' as 'October 20 ninth')."""
    if n < 20:
        return _ORD_ONES[n]
    if n == 20:
        return "twentieth"
    if n == 30:
        return "thirtieth"
    return ("twenty-" if n < 30 else "thirty-") + _ORD_ONES[n % 10]


_MONEY_UNITS = {"m": "million", "mm": "million", "million": "million", "b": "billion",
                "bn": "billion", "billion": "billion", "k": "thousand", "thousand": "thousand"}


def speakable(text: str) -> str:
    """Rewrite money and dates in the briefing the way they should be SAID, so the phone AI never
    has to read symbols: '$460M' -> '460 million' (no 'dollars million'), '$367' -> '367 dollars',
    '$12K' -> '12 thousand dollars', '10/29' -> 'October twenty-ninth', 'PI-47' -> 'P I 47'
    (not 'p I minus 47'), 'backup_prune' -> 'backup prune'."""
    import re
    text = re.sub(r"\b([A-Z]{1,3})-(\d{1,4})\b", lambda m: " ".join(m.group(1)) + " " + m.group(2), text)
    text = re.sub(r"(?<=[A-Za-z0-9])_(?=[A-Za-z0-9])", " ", text)

    def big(m):
        unit = _MONEY_UNITS[m.group(2).lower()]
        return f"{m.group(1)} {unit}" + (" dollars" if unit == "thousand" else "")
    text = re.sub(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s?(million|billion|thousand|bn|mm|[MBKmbk])\b", big, text)
    text = re.sub(r"\$\s?(\d[\d,]*(?:\.\d+)?)", r"\1 dollars", text)

    # Dates the code writes always carry a weekday ("Thu 10/29", "Monday 10/26"); any other
    # n/m is a fraction and is spoken as one ("3/4" -> "three fourths", never "March fourth").
    def date(m):
        mo, d = int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{m.group(1)} {_MONTHS[mo - 1]} {_ordinal_words(d)}"
        return m.group(0)
    text = re.sub(r"\b((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\.?,?)\s+(\d{1,2})/(\d{1,2})(?![\d/%.]|\s*/)",
                  date, text)

    def frac(m):
        n, den = int(m.group(1)), int(m.group(2))
        if not (0 < n <= 20 and 2 <= den <= 12):
            return m.group(0)
        if den == 2:
            return "one half" if n == 1 else f"{_CARD[n]} halves"
        word = _ORD_ONES[den]
        return f"{_CARD[n]} {word}" + ("" if n == 1 else "s")
    return re.sub(r"(?<![\d./])(\d{1,2})/(\d{1,2})(?![\d/%.]|\s*/)", frac, text)


def build_prompt(snap: str, pin: str, now: datetime, outbound: str = "") -> str:
    snap = speakable(snap)
    code = (f"""CODE CHECK - BEFORE ANYTHING ELSE:
- Your greeting already asked for his code. The code is {pin}. Digits may be spoken as words ("four two" = 42).
- Until the caller says exactly that code, reveal NOTHING, do not confirm whose line this is, discuss nothing else.
- Wrong code: "That's not it." One more try. Second wrong code: say "Sorry." and hang up (end the call).
- Never say the code, hint at it, or repeat what they said.""" if not outbound else
            f"""THIS IS A CALL YOU MADE TO MAX (no code needed - you dialed his number).
Why you called: {outbound}
Your greeting already said why. Do NOT say "You're in" and do NOT read today's list unless he asks for it.
If he says "repeat that" or "what?", repeat only your last sentence, shorter. Then wait.""")
    return f"""You are Augustine, Max's personal chief of staff on the phone (Dispatch is the task system you work inside; your name is Augustine, always say it in full). Think of a sharp, calm executive assistant who knows his whole list and his week, and who keeps him moving.

{code}

HOW YOU TALK - THE MOST IMPORTANT RULE ON THIS CALL:
- One or two short sentences per turn, then STOP and wait for him. About 10 seconds of speech, never more than 20.
- Never read more than 3 items in one turn. After 3, ask "Want the rest?" and wait.
- When you read an item, say its number and a few words of its title. Just "overdue" or the day it's due - never the exact time it went overdue, never its notes unless he asks.
- If he cuts in, you were talking too long: drop what you were saying, don't finish it, don't go back to it. Answer what he just said.
- "Stop", "wait", "hold on", "one second", or your name means: stop talking and wait for him in silence.
- If he pauses mid-sentence ("I'm still waiting on my dad, and..."), he isn't done: say nothing and let him finish. Never fill a pause with "Go ahead".
- He would rather ask "what else?" than sit through a list. When in doubt, say less.

WHO HE IS:
{ABOUT_MAX}

ON A CALL HE STARTED (after the code): say "You're in." then ONLY his three picks from TODAY'S LIST, by number and short title, then one sentence with how many more are on the list and how many are overdue ("Plus 7 more, 4 overdue."). Then: "What do you want to do?" Read the rest, or today's calendar, only when he asks. TODAY'S LIST is the same list his Discord messages show, so they always match. If he says Discord shows something different, tell him they are the same list, that a snoozed or just-closed item can differ for a minute, and offer to read it again - never guess a reason.

WHAT YOU DO:
1. Explain any item in detail when asked: what it is, why it matters, the concrete first step, how long it should take. Use its notes and the briefing below. If the detail isn't in the briefing, say so plainly and offer to have Claude look it up (an errand) - never invent facts.
2. The week: when asked, go a day or two at a time and ask before continuing.
3. Changes - say each one back in one sentence starting "Got it:" with the item NUMBER AND a few words of its title, so a mishearing is caught:
   "Got it: number 34, the Globex review, done." / "Got it: number 9, the old idea, killed." / "Got it: number 7, the fan return, moved to Friday at 2 PM." / "Got it: adding call the bank, tomorrow at 3 PM." / "Got it: number 12, waiting on Sam." / "Got it: sick day." / "Got it: new list."
   - Use his word: "kill it" / "drop it" / "it's closed" (meaning it no longer applies) = killed; "I did it" / "finished" = done. If unsure which, ask.
   - Only these changes exist: done, killed, moved, snoozed, waiting on someone, added, sick/slammed/normal day, new list, call holds and callbacks. "In progress", "postponed", "noted" are not changes - if he says "I'm working on it", just say "Good." and change nothing.
   - Everything due today, at once ("move all my stuff today to tomorrow, except hard deadlines"): first ask or name which ones stay, then confirm it in ONE sentence that names every exception by number: "Got it: moving everything due today to tomorrow, except number 91 and number 138." With no exceptions: "Got it: moving everything due today to tomorrow."
   - Any other group ("move all the Nationwide ones to Monday"): name each item by number and confirm each on its own "Got it".
   - Times must be a real day and clock time. "After work", "tonight", "later" - ask "What time?" or offer one ("8 PM tonight?") and confirm it before saying "Got it".
   - "Move that to Wednesday" or "not till Wednesday" is about the item you were just talking about - never a call hold. Call holds only when he talks about calls ("don't call me", "no calls").
   - If what he said was garbled or you're not sure, ask once - never guess a change.
   - The phone line mishears names. If he says a name you don't recognise from the briefing or his life ("consult Clive"), it was probably misheard: ask "Who - can you spell it?" before saving it.
3b. Follow-ups and notes: when he closes or changes an item but also wants to check or remember something later ("close 76, but I need to verify the refund lands"), confirm the change first, then confirm the follow-up as its OWN separate sentence, as a new to-do that names the original item and a day if he gave one:
   "Got it: number 76, the cable return, done." then "Got it: adding verify the Strimer refund landed, follow-up from number 76, Friday."
   A note with no action ("note that Sam prefers texts") becomes a to-do too: "Got it: adding remember Sam prefers texts, note on number 3." Never try to edit an existing item's notes - you can only add new to-dos.
4. Brain dumps: let him talk without interrupting. Then turn it into short, concrete to-dos (each starts with a verb, has a time if he gave one), read them back as a numbered list, and ask which to keep. After his answer, confirm each kept one with "Got it: adding ...". Everything he said is also saved as a parked note automatically.
5. Calls: if he's busy - "Got it: no calls for 2 hours." / "Got it: no calls until 5 PM." / "Got it: no calls today." If he wants a call back - "Got it: I'll call you at 3 PM." / "Got it: I'll call you in 20 minutes."
6. Computer errands (things for Claude on his PC to look up or do): write the request as one clear sentence, read it back word for word, and ask "Send that?" ONLY after a clear yes say "Got it: errand - <the exact sentence>." Be honest about what happens next: it is SAVED in Discord for Claude to pick up later - it does not run on its own yet. Never promise a call back or say "I'll get back to you" about it.
7. Getting him moving - this matters most. When he's stalling, avoiding, or "just sitting around":
   - Name the single next physical action (open the doc, text Sam, put shoes on), small enough to start in 2 minutes.
   - Get a start time in the next 10 minutes and a time box (15-25 minutes). Offer the call-back as the check: "I'll call you at 3:25 to see where it's at."
   - If something has been snoozed or pushed repeatedly, ask straight what's actually blocking it, then shrink it, delegate it, or kill it.
   - Be blunt and specific, never preachy. One push, then act on his answer.

RULES:
- You cannot change anything yourself: Dispatch applies every "Got it" about a minute after the call and posts it in Discord with Undo buttons. Never claim it's already done.
- Use only numbers and titles from the briefing. If unsure which item he means, ask.
- Times are Eastern. Today is {_today(now)}.{f" It is {now.astimezone(ET):%I:%M %p} now." if outbound else " For call-backs and time boxes, ask him what time it is now if you need it; relative times (in 20 minutes) are fine to take down as said."}
- Short, plain sentences. Never read symbols or markdown aloud; say "number 34".
- Money: under a thousand say "367 dollars"; thousands "12 thousand dollars"; millions and billions just "580 million" or "2 billion" - NEVER put the word "dollars" next to million or billion ("460 dollars million" is wrong).
- Dates: say the month and the day in words ("October twenty-ninth", "Monday, November ninth"). Never say "October 20 ninth".
- Names: say company names, not ticker letters (Globex, Initech, Umbrella), and write them in normal spelling, never respelled phonetically.
- Answer the question he asked in your first sentence ("Not yet - they report next Wednesday"), then add detail only if useful.
- Opening: after TODAY'S LIST, also name anything else overdue that has a hard deadline (an exam, a quiz, a bill) in one sentence.
- Ending: when he sounds done ("that's it", "nope", "thanks"), recap EVERY "Got it" from this call in one sentence - say "No changes." only if there were truly none - then ask "Anything else?" and wait. Only when he says no (or "bye"), say "Talk later." and hang up (end the call). Never hang up because you didn't understand him - ask instead.

BRIEFING (current as of the call):
{snap}
"""


def assistant_payload(prompt: str) -> dict:
    return {
        "name": "Augustine",
        "firstMessage": "Augustine. What's your code?",
        "firstMessageMode": "assistant-speaks-first",
        # endCall: it hangs up only when it decides he's done. The old "Goodbye" end-call phrase
        # cut the line the instant that word was said, recap and all (2026-11-02); [] clears it,
        # because a PATCH that leaves the key out keeps the old value.
        "model": {"provider": "openai", "model": MODEL, "temperature": 0.2, "maxTokens": MAX_TOKENS,
                  "messages": [{"role": "system", "content": prompt}],
                  "tools": [{"type": "endCall"}]},
        "voice": dict(VOICE),
        "transcriber": {"provider": "deepgram", "model": "nova-3", "language": "en",
                        "keyterm": KEYTERMS},
        "endCallPhrases": [],
        "maxDurationSeconds": 600,
        "backgroundSound": "off",
        # Interruptions: numWords 2 ignored one-word cut-ins ("Alright." "Augustine.") for 25 s
        # (10-25); 0 then stopped it for any 0.2 s of sound - breaths, "yeah", a voicemail
        # greeting - and it restarted in fragments (10-30 to 11-02). One real word stops it;
        # backchannel doesn't; the phrases stop it instantly.
        "stopSpeakingPlan": {"numWords": 1, "voiceSeconds": 0.3, "backoffSeconds": 0.8,
                             "interruptionPhrases": INTERRUPT_PHRASES,
                             "acknowledgementPhrases": ACK_PHRASES},
        # Wait for the end of his thought, not the first 0.4 s pause (it cut him off mid-sentence).
        "startSpeakingPlan": {"waitSeconds": 0.6, "smartEndpointingPlan": {"provider": "livekit"}},
        "firstMessageInterruptionsEnabled": True,    # the outbound opener can be cut off too
        "artifactPlan": {"recordingEnabled": False},     # transcript only; no audio kept
        "hooks": [{"on": "customer.speech.timeout",
                   "options": {"timeoutSeconds": 30, "triggerMaxCount": 1},
                   "do": [{"type": "say", "exact": "I can't hear you, so I'll hang up. Goodbye."},
                          {"type": "tool", "tool": {"type": "endCall"}}]}],
    }


# ---- reading a finished call -----------------------------------------------------------------
FORMS = ('"done #N", "kill #N", "move #N to <when>", "move due today to <when>", '
         '"move due today except #N #M to <when>", "snooze #N until <when>", '
         '"add: <title> <when, optional>", "did: <what he did>", "idea: <text>", '
         '"waiting on <who> for #N", "<who> got back to me on #N", "#N more like <N> minutes", '
         '"sick day", "slammed today", "back to normal", "new list", '
         '"no calls for <N> hours", "no calls until <when>", "no calls today", '
         '"call me at <when>", "call me in <N> minutes", "errand: <the exact sentence he approved>", '
         '"dump: <everything he said in a brain dump, in his own words>"')
CMD_RE = re.compile(r"^(add:|did:|idea:|park:|done #\d|kill #\d|move #\d|move due today |snooze #\d|#\d+ more like "
                    r"|waiting on .+ for #\d|.{1,60} got back to me on #\d|sick day|slammed today"
                    r"|back to normal|new list|no calls|call me (?:at|in) |errand:|dump:)",
                    re.IGNORECASE)


def transcript_text(call: dict) -> str:
    art = call.get("artifact") or {}
    if isinstance(art.get("transcript"), str) and art["transcript"].strip():
        return art["transcript"]
    lines = []
    for m in art.get("messages") or call.get("messages") or []:
        role, text = m.get("role"), m.get("message") or m.get("content")
        if role in ("user", "bot", "assistant") and isinstance(text, str):
            lines.append(f"{'User' if role == 'user' else 'AI'}: {text}")
    return "\n".join(lines)


def max_lines(transcript: str) -> list[str]:
    return [line.split(":", 1)[1].strip() for line in transcript.splitlines()
            if line.lower().startswith(("user:", "customer:"))]


def build_extract_prompt(transcript: str, snap: str, now: datetime) -> str:
    return f"""Max phoned his task line. Read the call transcript (User = Max, AI = the line) and list every change Max asked for that the line confirmed with "Got it", in the order he asked.

Write each change as ONE Dispatch command, using only these forms: {FORMS}.
- Every "Got it" line from the AI is one change - output one command for EACH of them, including "done" and "kill". The only exception: when a later "Got it" corrects an earlier one, keep only the correction.
- "Got it: number 3, the Sam text, done." -> "done #3". "Got it: number 7, the fan return, moved to Friday." -> "move #7 to Friday". "Got it: adding X, tomorrow at 5 PM." -> "add: X tomorrow 5pm".
- "Got it: moving everything due today to tomorrow, except number 91 and number 138." -> "move due today except #91 #138 to tomorrow". Without exceptions -> "move due today to tomorrow".
- "Got it: no calls for 2 hours." -> "no calls for 2 hours". "Got it: I'll call you at 3 PM." -> "call me at 3pm". "Got it: I'll call you in 20 minutes." -> "call me in 20 minutes".
- "Got it: errand - email me the Costco memo." -> "errand: email me the Costco memo". Only if he said yes to the read-back.
- If he did a brain dump, add ONE "dump: ..." with everything he said in it, close to his own words, before the adds that came from it.
- If the number the AI said doesn't fit the title it said (check the list), leave that change out.
- Use the item NUMBER from the list below (#34). If you cannot tell which item he meant, leave that change out.
- Copy his time words as spoken ("tomorrow 3pm", "Friday", "tonight"); never compute a date.
- If he changed his mind, keep only the final decision. Questions ("what's due", "read my list") are not changes.
- The code he said at the start is not a command.
Today is {_today(now)}.

HIS LIST:
{snap[:7000]}

TRANSCRIPT:
{transcript[:14000]}

Answer only JSON: {{"commands": ["done #34", "add: call the bank tomorrow 3pm"]}} or {{"commands": []}}"""


def parse_commands(obj) -> list[str] | None:
    """None = no usable answer (retry later); [] = the call changed nothing."""
    if not isinstance(obj, dict) or not isinstance(obj.get("commands"), list):
        return None
    out = []
    for c in obj["commands"]:
        if isinstance(c, str):
            c = " ".join(c.split())
            c = c[:6000] if c.lower().startswith("dump:") else c[:300]
            if CMD_RE.match(c):
                out.append(c)
    return out[:MAX_COMMANDS]


_CMD_NUM = re.compile(r"#(\d+)")
# The transcript of the line's own voice writes 106 as "1 0 6" and 112 as "1 12"; join them.
_SPOKEN_NUM = re.compile(r"(?i)\bnumber ((?:\d+ )*\d+)\b")
# ...and on 10-31 wrote 138 as "1 to 38", 125 as "1 or 25", 101 as "1 on o 1" (2026-11-02).
_SPOKEN_GLUED = re.compile(r"(?i)\bnumber (\d)((?: (?:to|or|on|oh|o|\d+))+)")
BULK_TODAY_RE = re.compile(r"(?i)^move due today(?: except ((?:#?\d+[\s,]*(?:and\s+)?)+?))? to (.+)$")


def confirmed_numbers(transcript: str) -> set[int]:
    """Item numbers the phone AI confirmed out loud ("Got it: number 34, ... done")."""
    out = set()
    for line in transcript.splitlines():
        if line.lower().startswith(("ai:", "assistant:", "bot:")):
            for seg in re.split(r"(?i)got it", line)[1:]:      # several "Got it"s on one line
                for m in _SPOKEN_NUM.findall(seg):
                    digits = m.replace(" ", "")
                    if len(digits) <= 4:
                        out.add(int(digits))
                for first, rest in _SPOKEN_GLUED.findall(seg):
                    digits = first + "".join("0" if w in ("o", "oh") else w for w in rest.lower().split()
                                             if w not in ("to", "or", "on"))
                    if len(digits) <= 3:
                        out.add(int(digits))
    return out


def _confirmed_bulk_today(transcript: str) -> bool:
    return any(line.lower().startswith(("ai:", "assistant:", "bot:"))
               and re.search(r"(?i)got it\W+moving (?:everything|all)\b[^.]*\btoday", line)
               for line in transcript.splitlines())


def only_confirmed(cmds: list[str], transcript: str) -> tuple[list[str], list[str]]:
    """Hard gate against a misreading model: a command that names an item (#N) passes only if
    the phone AI said "Got it ... number N" on the call; "move due today" only if it said
    "Got it: moving everything due today" (its #N are the ones that stay). Returns (kept, dropped)."""
    ok = confirmed_numbers(transcript)
    kept, dropped = [], []
    for c in cmds:
        if BULK_TODAY_RE.match(c):
            (kept if _confirmed_bulk_today(transcript) else dropped).append(c)
            continue
        nums = {int(n) for n in _CMD_NUM.findall(c)}
        (kept if not nums or nums <= ok else dropped).append(c)
    return kept, dropped


def expand_bulk(service, cmd: str, now: datetime) -> list[str] | None:
    """'move due today except #91 #138 to tomorrow' -> one 'move #N to tomorrow' per open item due
    before midnight tonight (overdue ones too), so each gets its own Undo. None if not bulk."""
    m = BULK_TODAY_RE.match(cmd)
    if not m:
        return None
    keep = {int(n) for n in re.findall(r"\d+", m.group(1) or "")}
    midnight = datetime.combine(now.astimezone(ET).date() + timedelta(days=1),
                                datetime.min.time(), tzinfo=ET)
    due = [t for t in service.by_status("active", "queued")
           if t.due_at is not None and t.due_at < midnight and t.id not in keep]
    return [f"move #{t.id} to {m.group(2)}" for t in sorted(due, key=lambda t: t.id)]


def promised(transcript: str) -> list[str]:
    """The changes the phone AI confirmed out loud ("Got it: ..." with something after it)."""
    out = []
    for line in transcript.splitlines():
        if line.lower().startswith(("ai:", "assistant:", "bot:")):
            for seg in re.split(r"(?i)got it[.:,]?", line)[1:]:
                seg = seg.strip(" .,")
                if len(seg.split()) >= 3:
                    out.append(cut(seg, 140))
    return out


def same_number(a: str | None, b: str | None) -> bool:
    da, dbb = re.sub(r"\D", "", a or ""), re.sub(r"\D", "", b or "")
    return len(da) >= 10 and len(dbb) >= 10 and da[-10:] == dbb[-10:]


@dataclass
class CallResult:
    call_id: str
    at: datetime | None
    commands: list[str] = field(default_factory=list)
    replies: list = field(default_factory=list)          # [(command, chat.Reply)]
    note: str = ""                                       # "", "other", "unread", "empty"
    caller_tail: str = ""
    said: list[str] = field(default_factory=list)        # Max's own lines (for "unread")
    dropped: list[str] = field(default_factory=list)     # changes the call never confirmed
    outbound: bool = False                               # a call the line placed to Max
    promised: list[str] = field(default_factory=list)    # every "Got it: ..." it said on the call
    mailbox_full: bool = False                           # missed call AND nothing was recorded
    brief: str = ""                                      # what a missed call was about


def _parse_at(v) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


class VoiceLink:
    """Everything that touches the database runs on the caller's thread; network and the local
    AI go through `run` (asyncio.to_thread in the bot, inline in tests)."""

    def __init__(self, service, router, vapi, assistant_id: str, pin: str, max_phone: str,
                 ollama_url: str = qwen.DEFAULT_URL, extract=None, run=None):
        self.s, self.router, self.vapi = service, router, vapi
        self.assistant_id, self.pin, self.max_phone = assistant_id, pin, max_phone
        self.extract = extract or (lambda prompt: qwen.ask_json(
            prompt, ollama_url, EXTRACT_TIMEOUTS, num_predict=2000)[0])
        self.run = run or asyncio.to_thread

    async def push(self, now: datetime, force: bool = False) -> bool:
        prompt = build_prompt(snapshot(self.s, now), self.pin, now)
        h = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        if not force and self.s.meta_get(HASH_KEY) == h:
            return False
        await self.run(self.vapi.call, "PATCH", f"/assistant/{self.assistant_id}",
                       assistant_payload(prompt))
        self.s.meta_set(HASH_KEY, h)
        return True

    def _save_seen(self, seen: list[str]) -> None:
        self.s.meta_set(SEEN_KEY, json.dumps(seen[-SEEN_KEEP:]))

    async def pull(self, now: datetime) -> list[CallResult]:
        since = (now - LOOKBACK).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        calls = await self.run(self.vapi.call, "GET",
                               f"/call?assistantId={self.assistant_id}&createdAtGt={since}&limit=50")
        seen = json.loads(self.s.meta_get(SEEN_KEY) or "[]")
        tries = json.loads(self.s.meta_get(TRIES_KEY) or "{}")
        out: list[CallResult] = []
        for c in sorted(calls or [], key=lambda c: str(c.get("createdAt", ""))):
            cid = c.get("id")
            if not cid or cid in seen or c.get("status") != "ended":
                continue
            at = _parse_at(c.get("startedAt") or c.get("createdAt"))
            num = (c.get("customer") or {}).get("number")
            if not same_number(num, self.max_phone):
                seen.append(cid)
                self._save_seen(seen)
                out.append(CallResult(cid, at, note="other",
                                      caller_tail=re.sub(r"\D", "", num or "")[-4:]))
                continue
            text = transcript_text(c)
            outbound = str(c.get("type", "")).startswith("outbound")
            said = [x for x in max_lines(text) if not CARRIER_RE.search(x)] if outbound else max_lines(text)
            if not said or (outbound and c.get("endedReason") == "voicemail"):
                seen.append(cid)
                self._save_seen(seen)
                res = CallResult(cid, at, note="empty", outbound=outbound)
                if outbound and at:              # he didn't pick up: no more check-in calls that day
                    self.s.meta_set(UNANSWERED_KEY + at.astimezone(ET).date().isoformat(), "1")
                    res.mailbox_full = bool(re.search(r"(?i)mailbox is full|cannot accept", text))
                    b = self.s.meta_json(BRIEF_KEY) or {}
                    b_at = _parse_at(b.get("at"))
                    if b_at and abs(b_at - at) <= timedelta(minutes=15):
                        res.brief = b.get("text", "")
                out.append(res)
                continue
            try:
                obj = await self.run(self.extract,
                                     build_extract_prompt(text, snapshot(self.s, now), now))
            except Exception:  # noqa: BLE001 - treated like no answer: retried
                obj = None
            cmds = parse_commands(obj)
            if cmds is None:
                tries[cid] = tries.get(cid, 0) + 1
                if tries[cid] < MAX_TRIES:
                    self.s.meta_set(TRIES_KEY, json.dumps(tries))
                    continue
                tries.pop(cid, None)
                self.s.meta_set(TRIES_KEY, json.dumps(tries))
                seen.append(cid)
                self._save_seen(seen)
                out.append(CallResult(cid, at, note="unread", said=max_lines(text)))
                continue
            # mark seen BEFORE acting: a crash mid-way must never apply a call twice
            seen.append(cid)
            self._save_seen(seen)
            if cid in tries:
                tries.pop(cid)
                self.s.meta_set(TRIES_KEY, json.dumps(tries))
            cmds, dropped = only_confirmed(cmds, text)
            log.info("call %s: applying %s; dropped %s", cid, cmds, dropped)
            flat = []
            for cmd in cmds:
                flat += expand_bulk(self.s, cmd, now) or [cmd]
            res = CallResult(cid, at, flat, dropped=dropped, outbound=outbound,
                             promised=promised(text))
            for cmd in flat:
                res.replies.append((cmd, await self.apply(cmd, now, cid)))
            out.append(res)
        return out

    async def apply(self, cmd: str, now: datetime, call_id: str | None = None):
        """One extracted command. Calls/holds, errands and brain dumps are handled here; every
        list change goes through chat.Router like a typed message."""
        from . import errands, holds
        from .chat import Reply
        low = cmd.lower()
        if low.startswith(("no calls", "call me ")):
            return Reply(holds.command(self.s, cmd, now) or "Couldn't read that call setting.",
                         None, [], "calls")
        if low.startswith("errand:"):
            job = errands.queue(self.s, cmd.split(":", 1)[1], now, call_id)
            return Reply(f"saved as errand #{job['id']} - NOT run: running errands from calls "
                         "isn't switched on yet.", None, [], "errand")
        if low.startswith("dump:"):
            stamp = f"{now.astimezone(ET):%a %m/%d %I:%M %p}"
            t = self.s.add(f"Brain dump {stamp}", status="parked", source="inbox",
                           notes=cmd.split(":", 1)[1].strip()[:8000], actor="chat")
            return Reply(f"kept the whole dump as parked #{t.id}.", self.s.last_event_id,
                         [t.id], "dump")
        return await self.router.handle(cmd)

    def call_me(self, phone_number_id: str) -> dict:
        """Outbound: Dispatch phones Max. Fails on a free Vapi number (inbound only)."""
        return self.vapi.call("POST", "/call", {"assistantId": self.assistant_id,
                                                "phoneNumberId": phone_number_id,
                                                "customer": {"number": self.max_phone}})


def render(res: CallResult) -> str:
    when = fmt_et(res.at.astimezone(ET)) if res.at else "just now"
    if res.note == "other":
        return (f"\U0001F4DE A call to your Dispatch line came from another number (ending "
                f"{res.caller_tail or '????'}) at {when}. Nothing was changed.")
    if res.note == "empty" and res.outbound:
        full = " Your voicemail is full, so nothing was recorded." if res.mailbox_full else ""
        about = f"\nWhat it was about:\n{res.brief}" if res.brief else ""
        return (f"\U0001F4DE Augustine called at {when} and you didn't pick up.{full} No more check-in "
                "calls today (callbacks you asked for still ring); Discord reminders keep going."
                + about)
    if res.note == "empty":
        return f"\U0001F4DE Call at {when}: you didn't say anything, so nothing changed."
    if res.note == "unread":
        said = " / ".join(res.said)[:1200]
        return (f"\U0001F4DE Call at {when}: the local AI couldn't read it after {MAX_TRIES} tries, "
                f"so nothing was changed. What you said: {said}\nType the changes here instead.")
    skipped = ("\nSkipped (the call never confirmed these numbers - type them here if you meant "
               "them): " + "; ".join(res.dropped)) if res.dropped else ""
    if not res.commands:
        # 10-31: it said "Got it" twice to "move everything to tomorrow" and nothing moved, with
        # no word about it here. If it promised something, say plainly that none of it happened.
        if res.promised:
            return (f"\U0001F4DE Call at {when}: Augustine said \"Got it\" to these, but NONE of "
                    "them were applied - type the ones you meant here:\n- "
                    + "\n- ".join(res.promised[:6]) + skipped)
        return f"\U0001F4DE Call at {when}: no changes.{skipped}"
    lines = [f"\U0001F4DE From your call at {when}:"]
    for cmd, r in res.replies:
        shown = cmd if not cmd.lower().startswith("dump:") else "brain dump"
        lines.append(f"- {shown} -> {r.text}")
    return "\n".join(lines) + skipped


def from_settings(settings, service, router) -> VoiceLink | None:
    if not settings.features.get("voice"):
        return None
    return VoiceLink(service, router, Vapi(settings.vapi_api_key), settings.vapi_assistant_id,
                     settings.voice_pin, settings.max_phone, settings.ollama_url)


def main(argv: list[str]) -> int:
    """python -m dispatch voice status | push | call-me"""
    from .chat import Router
    from .clock import SystemClock
    from .config import load_settings
    from .service import TaskService
    ap = argparse.ArgumentParser(prog="python -m dispatch voice")
    ap.add_argument("what", choices=["status", "push", "call-me"])
    a = ap.parse_args(argv)
    st = load_settings()
    missing = [n for n, v in (("VAPI_API_KEY", st.vapi_api_key),
                              ("VAPI_ASSISTANT_ID", st.vapi_assistant_id),
                              ("VOICE_PIN", st.voice_pin), ("MAX_PHONE", st.max_phone)) if not v]
    if a.what == "status":
        print("phone line:", "ON" if not missing else "OFF - missing in .env: " + ", ".join(missing))
        return 0
    if missing:
        print("Can't: missing in .env: " + ", ".join(missing))
        return 2
    conn = db.connect(st.db_path)
    service = TaskService(conn, SystemClock())
    link = from_settings(st, service, Router(service, base_url=st.ollama_url))
    if a.what == "push":
        asyncio.run(link.push(service.clock.now(), force=True))
        print("pushed the current list to the phone line")
        return 0
    try:
        r = link.call_me(st.vapi_phone_number_id)
        print("calling you now:", (r or {}).get("id"))
        return 0
    except VapiError as e:
        print("Vapi refused the call:", e)
        print("A free Vapi number can only receive calls - outbound needs a paid number.")
        return 1
