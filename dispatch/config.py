"""Settings, read only from the .env file (or real environment variables).

The Discord token is never printed: Settings.__repr__ shows it as '***'.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _int_or_none(v: str | None) -> int | None:
    v = (v or "").strip()
    return int(v) if v.isdigit() else None


@dataclass
class Settings:
    discord_token: str = field(default="", repr=False)
    guild_id: int | None = None
    max_user_id: int | None = None
    channel_today: int | None = None
    channel_inbox: int | None = None
    channel_scoreboard: int | None = None
    channel_commands: int | None = None
    ollama_url: str = "http://127.0.0.1:11434"
    db_path: Path = PROJECT_ROOT / "dispatch.db"
    heartbeat_path: Path = PROJECT_ROOT / "heartbeat.json"
    # Channel names used when the IDs above are blank.
    name_today: str = "today"
    name_inbox: str = "inbox"
    name_scoreboard: str = "scoreboard"
    name_commands: str = "commands"
    # Phase 2, both optional (blank = feature off; never blocks start-up).
    # The Canvas feed link is a secret (it opens the whole Canvas calendar): never printed.
    canvas_ics_url: str = field(default="", repr=False)
    inbox_webhook_id: int | None = None      # only the number from the webhook URL, not its token
    # Phone line (2026-10-18), optional: key + assistant + pin + phone = on. Secrets never printed.
    vapi_api_key: str = field(default="", repr=False)
    vapi_assistant_id: str = ""
    vapi_phone_number_id: str = ""
    voice_pin: str = field(default="", repr=False)
    max_phone: str = field(default="", repr=False)
    vapi_outbound_number_id: str = ""        # a number that can dial out; blank = it never calls
    voice_budget_usd: float = 13.0           # stop calling once this month's Vapi spend hits this

    @property
    def features(self) -> dict:
        return {"canvas": bool(self.canvas_ics_url), "siri": self.inbox_webhook_id is not None,
                "voice": bool(self.vapi_api_key and self.vapi_assistant_id and self.voice_pin
                              and self.max_phone)}

    def __repr__(self) -> str:
        tok = "***" if self.discord_token else "(missing)"
        f = self.features
        return (f"Settings(discord_token={tok}, guild_id={self.guild_id}, "
                f"max_user_id={self.max_user_id}, channel_today={self.channel_today}, "
                f"channel_inbox={self.channel_inbox}, db_path={self.db_path}, "
                f"canvas={'on' if f['canvas'] else 'off'}, siri={'on' if f['siri'] else 'off'}, "
                f"voice={'on' if f['voice'] else 'off'})")

    __str__ = __repr__

    # What .env actually said, kept only to explain problems in plain words.
    guild_id_raw: str = ""
    max_user_id_raw: str = ""

    def problems(self) -> list[str]:
        out = []
        if not self.discord_token or "paste" in self.discord_token:
            out.append("DISCORD_TOKEN is missing in .env")
        for name, raw, val, what in (
                ("DISCORD_GUILD_ID", self.guild_id_raw, self.guild_id, "the server ID"),
                ("MAX_USER_ID", self.max_user_id_raw, self.max_user_id, "your Discord user ID")):
            if not raw.strip():
                out.append(f"{name} is blank in .env - paste {what} (a long number, SETUP.md step 9)")
            elif not val:
                out.append(f"{name} in .env is not a valid ID - it must be only digits, the "
                           f"long number you copied as {what} (SETUP.md step 9)")
        return out


def load_settings(env_file: Path | None = None) -> Settings:
    try:
        from dotenv import load_dotenv
        load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)
    except ImportError:  # dotenv missing: fall back to real env vars only
        pass
    e = os.environ
    return Settings(
        discord_token=e.get("DISCORD_TOKEN", "").strip(),
        guild_id=_int_or_none(e.get("DISCORD_GUILD_ID")),
        max_user_id=_int_or_none(e.get("MAX_USER_ID")),
        channel_today=_int_or_none(e.get("CHANNEL_TODAY")),
        channel_inbox=_int_or_none(e.get("CHANNEL_INBOX")),
        channel_scoreboard=_int_or_none(e.get("CHANNEL_SCOREBOARD")),
        channel_commands=_int_or_none(e.get("CHANNEL_COMMANDS")),
        ollama_url=(e.get("OLLAMA_URL") or "http://127.0.0.1:11434").strip(),
        canvas_ics_url=(e.get("CANVAS_ICS_URL") or "").strip(),
        inbox_webhook_id=_int_or_none(e.get("INBOX_WEBHOOK_ID")),
        vapi_api_key=(e.get("VAPI_API_KEY") or "").strip(),
        vapi_assistant_id=(e.get("VAPI_ASSISTANT_ID") or "").strip(),
        vapi_phone_number_id=(e.get("VAPI_PHONE_NUMBER_ID") or "").strip(),
        voice_pin=(e.get("VOICE_PIN") or "").strip(),
        max_phone=(e.get("MAX_PHONE") or "").strip(),
        vapi_outbound_number_id=(e.get("VAPI_OUTBOUND_NUMBER_ID") or "").strip(),
        voice_budget_usd=float((e.get("VOICE_BUDGET_USD") or "13").strip() or 13),
        guild_id_raw=e.get("DISCORD_GUILD_ID", ""),
        max_user_id_raw=e.get("MAX_USER_ID", ""),
    )
