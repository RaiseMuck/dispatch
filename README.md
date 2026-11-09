# Dispatch

A personal task system that keeps every commitment in front of me until I close it, reschedule it or drop it. I built it for myself and have run it every day since September 2026. It replaced calendar reminders that I kept snoozing and forgetting.

It runs on my Windows PC as a Discord bot (Python, SQLite). I talk to it from my phone in plain English, and a local language model works out what I meant. There's also a phone line (**Augustine**) that gives me a spoken daily briefing and takes changes by voice.

## What it does

- **Morning top three.** Each morning it posts my three highest-value items, then sends batched hourly reminders until each one is done, snoozed or killed. A three-snooze ladder forces a real decision instead of endless pushing.
- **Expected-value ranking.** `score = payoff × likelihood ÷ hours × area weight × deadline boost`. A live scoreboard shows the share of each week's expected value I actually captured, so prioritizing gets measured instead of guessed.
- **Plain-English chat, with guardrails.** A local model (Qwen through Ollama) turns messages like "move the tuition thing to Thursday 2pm" into one action from a **closed list**. Every change gets an **Undo** button. If a request is ambiguous, the bot **asks instead of guessing**. If the model is down, buttons and `add:` / `did:` commands keep working and free-text messages get a hint instead of a guess, so the AI is never on the critical path. On the live evaluation (`tools/eval_qwen.py`, Qwen 27B), 40 of 42 messages were routed correctly with zero wrong-task executions.
- **"Fits my day" modes.** Sick, Slammed and Normal modes, "I've got N minutes", Swap (with reasons), and a waiting-on-someone list.
- **Calendar and inbox feeds.** Course deadlines import from the Canvas calendar feed, keyed to each event's id so nothing is added twice. Google Calendar and Gmail items arrive from scheduled AI agents through a command-line interface (`python -m dispatch cli add ...`) that de-duplicates on an external reference id.
- **Augustine, a voice agent (Vapi).** I call it, it reads my day, and it takes changes by voice. After the call, the transcript goes through the same router and Undo. It can also call me when a top item stalls. **Changes are accepted only from my own number, and that check is enforced in code, not by the AI** (the spoken code is a courtesy gate on top). Calls are capped at 3 a day and limited to set hours, and there's a hard monthly spend cap.

## Engineering notes

- **561 passing tests** (`pytest`). They include a minute-by-minute seven-day simulation proving no item goes silent for more than 24 hours, even with the PC off, plus exactly-once message handling and "no silent drops" tests.
- Versioned SQL migrations (`dispatch/migrations/`) with a migration check.
- Every message in the bot's channels gets exactly one reply, including messages sent while the PC was off.
- Secrets live only in `.env` (see `.env.example`). Nothing secret is in this repo.

## Layout

```
dispatch/     bot, chat router, scheduler, ranking rules, scoreboard, voice line, CLI
tests/        pytest suite and fixtures
tools/        model evaluation harness and migration check
SETUP.md      full setup and usage guide (written for myself; Windows)
```

## Run it

See it work in 30 seconds, no Discord needed. A fake clock replays a morning from the example list and prints each post, its buttons and why each item ranked where it did:

```
pip install -r requirements.txt
python -m dispatch --dry-run --simulate 2026-09-28T07:00:00-04:00 --hours 27
python -m pytest -q          # 561 passed, 2 skipped
```

To run it for real (Python 3.12+, tested on Windows):

```
copy .env.example .env       # fill in a Discord bot token and your server/user ids
python -m dispatch migrate
python -m dispatch import-seed --seed seed.json
python -m dispatch run
```

The local model is optional (Ollama at `OLLAMA_URL`). Without it, buttons and prefix commands still work. `SETUP.md` is my own full setup guide.

## About this copy

This is a public snapshot of a private, daily-use project. My real task database, logs, run history and personal details have been removed, and names in the examples are placeholders.
