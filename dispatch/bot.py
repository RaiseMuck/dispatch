"""The Discord side: buttons (incl. Undo), the Pick-a-day box, chat in #inbox/#today, the Siri
webhook, the startup catch-up of #inbox, the Canvas poll, and the minute loop.
Phase 2.1: day-mode buttons on the morning post, a Swap button per active item (reason menu,
"check in when?" box), Got it / Wait more on check-ins, Start / Skip on a best-fit suggestion,
split offers. Anything that asks the local AI from a button defers first (3-second rule).

Everything that decides anything lives in rules.py / service.py / scheduler.py / chat.py /
canvas.py. This file only translates between Discord and those.

No-silent rule: every message from Max (or the configured Siri webhook) in #inbox or #today gets
exactly one reply. Messages from anyone else, or in other channels, are ignored.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from datetime import timedelta

import discord
from discord import ui

from . import canvas, chat, db, legend, parser, rules, scoreboard
from .clock import Clock, fmt_et
from .config import Settings
from .db import Task
from .scheduler import DiscordPoster, Message, RunState, list_after_done, tick, write_heartbeat
from .service import (UNDO_WINDOW, UNDOABLE, CantDo, LadderError, NotOpen, TaskService,
                      UndoRefused)

log = logging.getLogger("dispatch.bot")

SPLIT_NO, SPLIT_ASK = "splitno", "splitask"
ACTIONS = "|".join((rules.DONE, rules.PLUS2H, rules.TONIGHT, rules.TOMORROW, rules.KILL,
                    rules.DO_NOW, rules.PICK_DAY, rules.GOTIT, rules.WAITMORE, rules.START,
                    rules.SKIP, SPLIT_NO, SPLIT_ASK, rules.SPLIT))
LABELS = {**rules.LABELS, SPLIT_NO: "Keep as is", SPLIT_ASK: "Split it"}
STYLES = {rules.DONE: discord.ButtonStyle.success, rules.KILL: discord.ButtonStyle.danger,
          rules.DO_NOW: discord.ButtonStyle.primary, rules.START: discord.ButtonStyle.primary,
          rules.GOTIT: discord.ButtonStyle.success, rules.SPLIT: discord.ButtonStyle.primary}
MAX_ROWS = 5          # Discord allows 5 rows of buttons per message
ITEMS_WITH_MODE_ROW = 3   # morning / catch-up: 3 item rows + swap row + mode row = 5
ITEMS_PER_MESSAGE = 4     # other posts: 4 item rows + swap row
MAX_CHARS = 2000      # and 2000 characters of text
RECENT_MAX = 200      # bot messages remembered for "reply to this message" (in memory only)
META_INBOX_SEEN = "inbox_last_message_id"
META_TODAY_SEEN = "today_last_message_id"
HANDLED_PREFIX = "handled:"               # meta key per handled message id -> stored reply
HANDLED_KEEP = timedelta(days=7)
AGAIN = "(again - my earlier reply didn't go through) "
LIST_DELAY = 10      # seconds from a Done to the fresh list: several taps in a row -> one list


class TaskButton(ui.DynamicItem[ui.Button], template=rf"(?P<action>{ACTIONS}):(?P<task_id>[0-9]+)"):
    """A button whose custom_id is '<action>:<task_id>'.

    Registered with add_dynamic_items, so buttons on old messages keep working after a restart.
    """

    def __init__(self, action: str, task_id: int, row: int | None = None, first: bool = False,
                 label: str | None = None):
        label = label or LABELS[action]
        if first:
            label = f"#{task_id} {label}"
        super().__init__(ui.Button(label=label, custom_id=f"{action}:{task_id}", row=row,
                                   style=STYLES.get(action, discord.ButtonStyle.secondary)))
        self.action = action
        self.task_id = task_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: ui.Button, match, /):
        return cls(match["action"], int(match["task_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.client.handle_button(interaction, self.action, self.task_id)


class ModeButton(ui.DynamicItem[ui.Button], template=r"mode:(?P<mode>sick|slammed|normal)"):
    """'Sick/home day' / 'Slammed' / 'Normal' on the morning post; custom_id 'mode:<name>'."""

    def __init__(self, mode: str, row: int | None = None):
        super().__init__(ui.Button(label=rules.MODE_LABELS[mode], custom_id=f"mode:{mode}",
                                   row=row, style=discord.ButtonStyle.secondary))
        self.mode = mode

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: ui.Button, match, /):
        return cls(match["mode"])

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.client.handle_mode(interaction, self.mode)


class SwapButton(ui.DynamicItem[ui.Button], template=r"swap:(?P<task_id>[0-9]+)"):
    """'Swap #N' under one of the three; opens the reason menu."""

    def __init__(self, task_id: int, row: int | None = None):
        super().__init__(ui.Button(label=f"Swap #{task_id}", custom_id=f"swap:{task_id}", row=row,
                                   style=discord.ButtonStyle.secondary))
        self.task_id = task_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: ui.Button, match, /):
        return cls(int(match["task_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.client.handle_swap(interaction, self.task_id)


class SwapReasonButton(ui.DynamicItem[ui.Button],
                       template=r"swapwhy:(?P<reason>long|out|energy|waiting):(?P<task_id>[0-9]+)"):
    def __init__(self, reason: str, task_id: int):
        super().__init__(ui.Button(label=rules.SWAP_LABELS[reason],
                                   custom_id=f"swapwhy:{reason}:{task_id}",
                                   style=discord.ButtonStyle.secondary))
        self.reason, self.task_id = reason, task_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: ui.Button, match, /):
        return cls(match["reason"], int(match["task_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.client.handle_swap_reason(interaction, self.reason, self.task_id)


class WaitUntilModal(ui.Modal, title="Waiting on someone"):
    when = ui.TextInput(label="Check in when?", default="in 3 days",
                        placeholder="Friday, in 3 days, 10/9", max_length=40)

    def __init__(self, task_id: int):
        super().__init__()
        self.task_id = task_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.client.handle_wait_until(interaction, self.task_id, str(self.when.value))


class UndoButton(ui.DynamicItem[ui.Button], template=r"undo:(?P<event_id>[0-9]+)"):
    """'Undo' under a confirmation; custom_id 'undo:<event_id>'. Survives restarts."""

    def __init__(self, event_id: int, label: str = "Undo"):
        super().__init__(ui.Button(label=label, custom_id=f"undo:{event_id}",
                                   style=discord.ButtonStyle.secondary))
        self.event_id = event_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: ui.Button, match, /):
        return cls(int(match["event_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.client.handle_undo(interaction, self.event_id)


class PickDayModal(ui.Modal, title="Pick a day"):
    when = ui.TextInput(label="When?", placeholder="Friday, 10/3, tomorrow 3pm, next Mon",
                        max_length=40)

    def __init__(self, task_id: int):
        super().__init__()
        self.task_id = task_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.client.handle_pick_day(interaction, self.task_id, str(self.when.value))


def split_text(text: str, limit: int = MAX_CHARS) -> list[str]:
    """Split on line breaks into pieces Discord accepts (each <= 2000 characters)."""
    out, cur = [], ""
    for line in text.split("\n"):
        line = line[:limit]
        if cur and len(cur) + 1 + len(line) > limit:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    out.append(cur)
    return out


def build_view(items: list[Task], msg: Message | None = None, mode_row: bool = False) -> ui.View:
    """One row of buttons per item (its snooze / flagged / check-in choices), then one Swap
    button per ACTIVE item, then (morning / catch-up) the three day-mode buttons."""
    view = ui.View(timeout=None)
    per = ITEMS_WITH_MODE_ROW if mode_row else (ITEMS_PER_MESSAGE if msg is not None else MAX_ROWS)
    items = items[:per]
    for row, t in enumerate(items):
        choices = msg.buttons_for(t) if msg is not None else rules.snooze_options(t.snooze_count)
        for i, action in enumerate(choices):
            view.add_item(TaskButton(action, t.id, row=row, first=(i == 0)))
    row = len(items)
    swaps = [i for i in (msg.swap_ids() if msg is not None else []) if i in {t.id for t in items}]
    if swaps:
        for tid in swaps:
            view.add_item(SwapButton(tid, row=row))
        row += 1
    if mode_row:
        for m in rules.MODE_NAMES:
            view.add_item(ModeButton(m, row=row))
    return view


def view_from_ids(buttons: list[tuple[str, str]], undo_event_id: int | None = None):
    """A reply's extra buttons ('start:16', 'split:11', 'pickday:3', ...) plus its Undo."""
    if not buttons and not undo_event_id:
        return None
    view = ui.View(timeout=None)
    for label, cid in buttons[:24]:
        action, _, tid = cid.partition(":")
        if tid.isdigit() and action in ACTIONS.split("|"):
            view.add_item(TaskButton(action, int(tid), label=label))
    if undo_event_id:
        view.add_item(UndoButton(undo_event_id))
    return view


def undo_view(event_id: int | None):
    if not event_id:
        return None
    view = ui.View(timeout=None)
    view.add_item(UndoButton(event_id))
    return view


def undo_many_view(pairs: list[tuple[int, int]]):
    """One 'Undo #<task>' button per (event id, task id); Discord allows 25 per message."""
    if not pairs:
        return None
    view = ui.View(timeout=None)
    for eid, tid in pairs[:25]:
        view.add_item(UndoButton(eid, label=f"Undo #{tid}"))
    return view


class DispatchBot(discord.Client):
    def __init__(self, settings: Settings, service: TaskService, clock: Clock, router=None):
        intents = discord.Intents.default()
        intents.message_content = True   # needed to read messages in #inbox / #today
        super().__init__(intents=intents)
        self.settings = settings
        self.service = service
        self.clock = clock
        self.router = router or chat.Router(service, base_url=settings.ollama_url)
        self.poster = DiscordPoster()
        self.run_state = RunState(heartbeat_path=settings.heartbeat_path, bot_connected=False,
                                  extra={"features": settings.features})
        self.today_channel: discord.TextChannel | None = None
        self.inbox_channel: discord.TextChannel | None = None
        self.scoreboard_channel: discord.TextChannel | None = None
        self._scoreboard_key: str | None = None   # last body posted; edit only on change
        self.commands_channel: discord.TextChannel | None = None
        self._legend_hour: str | None = None      # legend checked once per clock hour
        self.recent: OrderedDict[int, list[int]] = OrderedDict()   # bot message id -> task ids
        self._seen: set[int] = set()                               # message ids already handled
        from . import voice                                         # phone line (optional)
        self.voice = voice.from_settings(settings, service, self.router)
        self._cal_busy = False                                      # calendar refresh running
        self.list_delay = LIST_DELAY
        self._list_task: asyncio.Task | None = None                 # pending list-after-done

    # ---- lifecycle -------------------------------------------------------------------
    async def setup_hook(self) -> None:
        self.add_dynamic_items(TaskButton, UndoButton, ModeButton, SwapButton, SwapReasonButton)
        self.loop.create_task(self.minute_loop())

    async def on_ready(self) -> None:
        self._resolve_channels()
        self.run_state.bot_connected = True
        log.info("connected as %s; today=%s inbox=%s; %s", self.user,
                 getattr(self.today_channel, "name", None), getattr(self.inbox_channel, "name", None),
                 self.settings)
        try:
            await self.replay_inbox()
        except Exception:  # noqa: BLE001 - catch-up is best effort
            log.exception("inbox catch-up failed")

    async def on_resumed(self) -> None:
        self.run_state.bot_connected = True

    async def on_disconnect(self) -> None:
        self.run_state.bot_connected = False

    def _resolve_channels(self) -> None:
        guild = self.get_guild(self.settings.guild_id) if self.settings.guild_id else None
        if guild is None:
            log.error("bot is not in the server DISCORD_GUILD_ID=%s", self.settings.guild_id)
            return

        def find(cid, name):
            ch = guild.get_channel(cid) if cid else None
            return ch or discord.utils.get(guild.text_channels, name=name)

        self.today_channel = find(self.settings.channel_today, self.settings.name_today)
        self.inbox_channel = find(self.settings.channel_inbox, self.settings.name_inbox)
        self.scoreboard_channel = find(self.settings.channel_scoreboard,
                                       self.settings.name_scoreboard)
        self.commands_channel = find(self.settings.channel_commands, self.settings.name_commands)

    async def minute_loop(self) -> None:
        while not self.is_closed():
            try:
                now = self.clock.now()
                if self.today_channel is None:
                    write_heartbeat(self.settings.heartbeat_path, now, False, self.settings.features)
                else:
                    tick(self.service, self.poster, self.clock, self.run_state)
                    if self.settings.canvas_ics_url:
                        await canvas.poll_in_bot(self.service, self.settings.canvas_ics_url, now,
                                                 self.settings.ollama_url)
                        canvas.flush_note(self.service, self.poster, self.clock.now())
                        canvas.flush_move_notes(self.service, self.poster, self.clock.now())
                    for msg in self.poster.drain():
                        await self.send_batch(msg)
                    self.nudge_list()                # a Done from the CLI or anywhere else
                    if self.voice is not None:
                        await self.voice_sync()
                    await self.router.backfill()     # label old items' place/energy, 25 a time
            except Exception:  # noqa: BLE001 - one bad minute must not kill the loop
                log.exception("minute loop error")
            try:
                await self.update_scoreboard()
            except Exception:  # noqa: BLE001 - the scoreboard never blocks reminders
                log.exception("scoreboard update failed")
            try:
                hour = self.clock.now().strftime("%Y-%m-%d %H")
                # Only once connected (the first loop runs before on_ready, when no channel is
                # known yet - counting that as "checked" hid the legend for an hour).
                if hour != self._legend_hour and self.today_channel is not None:
                    if self.commands_channel is None:
                        self._resolve_channels()   # #commands may have been created since
                    self._legend_hour = hour
                    await self.update_legend()
            except Exception:  # noqa: BLE001
                log.exception("legend update failed")
            now = self.clock.now()
            await asyncio.sleep(60 - now.second + 0.5)

    async def voice_sync(self) -> None:
        """Phone line: push the list when it changed, then apply finished calls. A Vapi or
        network problem is logged and never touches reminders."""
        from . import voice
        try:
            await self.voice.push(self.clock.now())
        except Exception:  # noqa: BLE001
            log.exception("voice push failed")
        try:
            results = await self.voice.pull(self.clock.now())
        except Exception:  # noqa: BLE001
            log.exception("voice pull failed")
            return
        quiet = discord.AllowedMentions.none()
        for res in results:
            pairs = [(r.undo_event_id, r.task_ids[0] if r.task_ids else 0)
                     for _, r in res.replies if r.undo_event_id]
            texts = split_text(voice.render(res))
            view = undo_many_view(pairs) or discord.utils.MISSING
            sent = await self.today_channel.send(texts[0], view=view, allowed_mentions=quiet)
            self._remember(getattr(sent, "id", 0), [tid for _, tid in pairs])
            for extra in texts[1:]:
                await self.today_channel.send(extra, allowed_mentions=quiet)
        if results:
            self.nudge_list()
            try:                                    # the list changed: refresh what the line knows
                await self.voice.push(self.clock.now())
            except Exception:  # noqa: BLE001
                log.exception("voice push failed")
        await self.phone_extras()

    def nudge_list(self) -> None:
        """Something may have been finished: post the fresh list after a short pause, so the
        reply to the Done goes first and several Dones in a row give one list. Cheap and safe to
        call often (a no-op while one is pending; list_after_done decides if there is news)."""
        if self.today_channel is None or (self._list_task is not None and not self._list_task.done()):
            return
        self._list_task = asyncio.get_running_loop().create_task(self._list_soon())

    async def _list_soon(self) -> None:
        await asyncio.sleep(self.list_delay)
        try:
            msg = list_after_done(self.service, self.clock.now())
            if msg is not None:
                await self.send_batch(msg)
        except Exception:  # noqa: BLE001 - the list is a convenience; never breaks anything
            log.exception("list after done failed")

    async def phone_extras(self) -> None:
        """Calendar refresh (read-only) and Dispatch-calls-Max. Each in its own try."""
        from . import calls, pcclaude
        now = self.clock.now()
        if not self._cal_busy and pcclaude.calendar_stale(self.service, now):
            self._cal_busy = True
            self.loop.create_task(self._refresh_calendar())
        try:
            for text in calls.tick(self.service, self.voice, self.settings.vapi_outbound_number_id,
                                   now, self.settings.voice_budget_usd):
                await self.today_channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        except Exception:  # noqa: BLE001
            log.exception("call engine failed")

    async def _refresh_calendar(self) -> None:
        from . import pcclaude
        try:
            await pcclaude.refresh_calendar(self.service, self.clock.now())
        except Exception:  # noqa: BLE001
            log.exception("calendar refresh failed")
        finally:
            self._cal_busy = False


    async def update_scoreboard(self) -> None:
        """Keep ONE message in #scoreboard current: edit it when any number changed, re-post
        it if it was deleted. Never mentions anyone; carries counts only, no titles."""
        ch = self.scoreboard_channel
        if ch is None:
            return
        now = self.clock.now()
        data = scoreboard.compute(self.service.conn, now)
        key = json.dumps(scoreboard.embed_dict(data), sort_keys=True)
        if key == self._scoreboard_key:
            return
        await self._keep_one_message(ch, scoreboard.META_MESSAGE,
                                     scoreboard.embed_dict(data, stamp=fmt_et(now) + " ET"))
        self._scoreboard_key = key

    async def update_legend(self) -> None:
        """The command legend in #commands: posted once, edited if legend.py changed, re-posted
        if deleted. Checked at start-up and then hourly (it only changes with a code update)."""
        ch = self.commands_channel
        if ch is None:
            return
        await self._keep_one_message(ch, legend.META_MESSAGE, legend.embed_dict(), only_if_new=True)

    async def _keep_one_message(self, ch, meta_key: str, body: dict,
                                only_if_new: bool = False) -> None:
        """Edit the bot's one message (id stored under meta_key) in ch, or post it if missing.
        only_if_new: leave an existing message alone when its embed already matches."""
        embed = discord.Embed.from_dict(body)
        mid = db.meta_get(self.service.conn, meta_key)
        msg = None
        try:
            if mid and mid.isdigit():
                try:
                    msg = await ch.fetch_message(int(mid))
                except discord.NotFound:
                    msg = None
            if msg is not None:
                old = msg.embeds[0].to_dict() if getattr(msg, "embeds", None) else None
                if only_if_new and old is not None and \
                        old.get("fields") == embed.to_dict().get("fields") and \
                        old.get("description") == embed.to_dict().get("description"):
                    return
                await msg.edit(content=None, embed=embed)
            else:
                msg = await ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                self.service.meta_set(meta_key, str(msg.id))
        except discord.Forbidden:
            log.warning("no permission to post/edit in #%s", getattr(ch, "name", "?"))

    # ---- sending ---------------------------------------------------------------------
    def _remember(self, message_id: int, task_ids: list[int]) -> None:
        if not task_ids:
            return
        self.recent[message_id] = list(task_ids)
        while len(self.recent) > RECENT_MAX:
            self.recent.popitem(last=False)

    async def send_batch(self, msg: Message) -> None:
        mention = f"<@{self.settings.max_user_id}>" if self.settings.max_user_id else "@Max"
        texts = split_text(msg.render(mention))          # long weekly lists -> several messages
        size = ITEMS_WITH_MODE_ROW if msg.mode_row else ITEMS_PER_MESSAGE
        chunks = [msg.items[i:i + size] for i in range(0, len(msg.items), size)]
        allowed = discord.AllowedMentions(users=True, everyone=False, roles=False)
        quiet = discord.AllowedMentions.none()   # follow-ups never @-mention: one notification only
        if chunks:
            first_view = build_view(chunks[0], msg, mode_row=msg.mode_row)
        elif msg.mode_row:                        # a morning with nothing active: still the modes
            first_view = build_view([], msg, mode_row=True)
        else:
            first_view = view_from_ids(msg.buttons) or undo_many_view(msg.undo) or discord.utils.MISSING
        sent = await self.today_channel.send(texts[0], view=first_view,
                                             allowed_mentions=allowed if msg.mention else quiet)
        self._remember(getattr(sent, "id", 0),
                       [t.id for t in chunks[0]] if chunks else [tid for _, tid in msg.undo])
        for extra in texts[1:]:
            await self.today_channel.send(extra, allowed_mentions=quiet)
        for chunk in chunks[1:]:
            sent = await self.today_channel.send("(continued)", view=build_view(chunk, msg),
                                                 allowed_mentions=quiet)
            self._remember(getattr(sent, "id", 0), [t.id for t in chunk])
        if msg.split_offer is not None:               # asks the local AI: in a worker thread
            r = await self.router.offer_split(msg.split_offer, actor="scheduler")
            if r.action == "split_offer":
                sent = await self.today_channel.send(r.text, view=view_from_ids(r.buttons),
                                                     allowed_mentions=quiet)
                self._remember(getattr(sent, "id", 0), r.task_ids)

    async def _reply(self, message: discord.Message, r: chat.Reply) -> bool:
        """The single reply every handled message gets. True if Discord accepted it."""
        try:
            kwargs = {"mention_author": False, "allowed_mentions": discord.AllowedMentions.none()}
            view = view_from_ids(r.buttons, r.undo_event_id)
            if view is not None:
                kwargs["view"] = view
            sent = await message.reply((r.text or chat.HINT)[:MAX_CHARS], **kwargs)
        except Exception:  # noqa: BLE001 - not marked answered: replayed at the next start
            log.exception("could not reply to a message")
            return False
        self._remember(getattr(sent, "id", 0), r.task_ids)
        return True

    # ---- buttons ---------------------------------------------------------------------
    def _is_max(self, user: discord.abc.User) -> bool:
        # MAX_USER_ID is required at startup (see config.Settings.problems), so no fallback here.
        return self.settings.max_user_id is not None and user.id == self.settings.max_user_id

    async def _answer(self, interaction: discord.Interaction, text: str, undo: int | None = None,
                      **kw) -> None:
        view = undo_view(undo)
        if view is not None:
            kw["view"] = view
        await interaction.response.send_message(text, **kw)

    async def handle_button(self, interaction: discord.Interaction, action: str, task_id: int) -> None:
        if not self._is_max(interaction.user):
            await interaction.response.send_message("Only Max can use these buttons.", ephemeral=True)
            return
        t = self.service.get(task_id)
        if t is None or t.status in ("done", "killed"):
            await interaction.response.send_message(f"#{task_id} is already closed.", ephemeral=True)
            return
        if action in NEW_ACTIONS:
            await self._handle_new_button(interaction, action, t)
            return
        # Re-check the ladder on the server: an old message may still show snooze buttons.
        if action in (rules.DONE,) + rules.SNOOZE_CHOICES and \
                action not in rules.snooze_options(t.snooze_count):
            await interaction.response.send_message(
                f"#{t.id} has used both snoozes. Do now, Pick a day, or Kill:",
                view=build_view([t]))
            return
        if action == rules.PICK_DAY:
            await interaction.response.send_modal(PickDayModal(task_id))
            return
        undo = None
        try:
            if action == rules.DONE:
                t, promoted = self.service.mark_done(task_id, actor="button")
                line = f"Done: #{t.id} {t.title}." + _promo(promoted)
            elif action == rules.KILL:
                t, promoted = self.service.kill(task_id, actor="button")
                line = f"Killed: #{t.id} {t.title}." + _promo(promoted)
            elif action == rules.DO_NOW:
                t = self.service.do_now(task_id, actor="button")
                line = f"Go: #{t.id} {t.title}. I'll check back {fmt_et(t.next_nag_at)}."
            else:
                t = self.service.snooze(task_id, action, actor="button")
                line = f"Snoozed #{t.id} to {fmt_et(t.next_nag_at)} ({t.snooze_count}/2)."
                if t.snooze_count >= rules.MAX_SNOOZES:
                    line += " Next time it's Do now, Pick a day, or Kill."
            undo = self.service.last_event_id
        except (NotOpen, LadderError, CantDo) as exc:
            line = f"Couldn't do that: {exc}"
        await self._answer(interaction, line, undo)
        if action == rules.DONE:
            self.nudge_list()

    async def _send_reply(self, interaction: discord.Interaction, r: chat.Reply,
                          deferred: bool = False, ephemeral: bool = False) -> None:
        kw = {}
        view = view_from_ids(r.buttons, r.undo_event_id)
        if view is not None:
            kw["view"] = view
        if ephemeral:
            kw["ephemeral"] = True
        if deferred:
            await interaction.followup.send((r.text or chat.HINT)[:MAX_CHARS], **kw)
        else:
            await interaction.response.send_message((r.text or chat.HINT)[:MAX_CHARS], **kw)

    async def _handle_new_button(self, interaction: discord.Interaction, action: str, t: Task) -> None:
        """Got it / Wait more / Start / Skip / Split it / Keep as is."""
        if action == SPLIT_ASK:                        # asks the local AI: defer first
            await interaction.response.defer()
            r = await self.router.offer_split(t.id, actor="button")
            await self._send_reply(interaction, r, deferred=True)
            return
        if action == rules.GOTIT:
            try:
                t2, promoted = self.service.resume_waiting(t.id, actor="button")
                where = "in your three" if t2.status == "active" else "back in the queue, due now"
                r = chat.Reply(f"Good - #{t.id} {t.title} is {where}."
                               + _promo([p for p in promoted if p.id != t.id]),
                               self.service.last_event_id, [t.id], "resume")
            except CantDo as exc:
                r = chat.Reply(str(exc))
        elif action == rules.WAITMORE:
            try:
                t2 = self.service.wait_more(t.id, actor="button")
                r = chat.Reply(f"OK - I'll check in on #{t.id} again {fmt_et(t2.waiting_until)}.",
                               self.service.last_event_id, [t.id], "wait_more")
            except CantDo as exc:
                r = chat.Reply(str(exc), None, [t.id], "wait_more",
                               [(LABELS[c], f"{c}:{t.id}")
                                for c in rules.waiting_options(rules.MAX_WAIT_MORE)])
        elif action == rules.START:
            r = self.router.start_reply(t.id, actor="button")
        elif action == rules.SKIP:
            r = self.router.skip_reply(t.id, actor="button")
        elif action == rules.SPLIT:
            r = self.router.accept_split(t.id, actor="button")
        else:                                          # SPLIT_NO
            r = self.router.decline_split(t.id, actor="button")
            await self._send_reply(interaction, r, ephemeral=True)
            return
        await self._send_reply(interaction, r)

    async def handle_mode(self, interaction: discord.Interaction, mode: str) -> None:
        if not self._is_max(interaction.user):
            await interaction.response.send_message("Only Max can use these buttons.", ephemeral=True)
            return
        await interaction.response.defer()              # the backfill may ask the local AI
        await self.router.backfill()
        res = self.service.set_mode(rules.PRESETS[mode], actor="button", source="button")
        await self._send_reply(interaction, chat.Reply(res.text, res.event_id,
                                                       [t.id for t in res.active], "set_mode"),
                               deferred=True)

    async def handle_swap(self, interaction: discord.Interaction, task_id: int) -> None:
        if not self._is_max(interaction.user):
            await interaction.response.send_message("Only Max can use these buttons.", ephemeral=True)
            return
        t = self.service.get(task_id)
        if t is None or t.status != "active":
            await interaction.response.send_message(
                f"#{task_id} isn't one of your three any more.", ephemeral=True)
            return
        view = ui.View(timeout=None)
        for reason in rules.SWAP_REASONS:
            view.add_item(SwapReasonButton(reason, task_id))
        await interaction.response.send_message(f"Why swap #{t.id} {t.title}?", view=view,
                                                ephemeral=True)

    async def handle_swap_reason(self, interaction: discord.Interaction, reason: str,
                                 task_id: int) -> None:
        if not self._is_max(interaction.user):
            await interaction.response.send_message("Only Max can use these buttons.", ephemeral=True)
            return
        if reason == "waiting":
            await interaction.response.send_modal(WaitUntilModal(task_id))
            return
        # the 2nd 'Too long' may ask the local AI for a split step: defer first
        slow = reason == "long" and self.service.long_swaps(task_id) >= 1
        if slow:
            await interaction.response.defer()
        r = await self.router.swap_reply(task_id, reason, "button")
        await self._send_reply(interaction, r, deferred=slow)

    async def handle_wait_until(self, interaction: discord.Interaction, task_id: int,
                                text: str) -> None:
        until = parser.parse_when(text, self.clock.now())
        if until is None or until <= self.clock.now():
            await interaction.response.send_message(
                f"Couldn't read \"{text}\". Try: Friday, in 3 days, 10/9.", ephemeral=True)
            return
        r = await self.router.swap_reply(task_id, "waiting", "button", until=until)
        await self._send_reply(interaction, r)

    async def handle_pick_day(self, interaction: discord.Interaction, task_id: int, text: str) -> None:
        due = parser.parse_when(text, self.clock.now())
        if due is None:
            await interaction.response.send_message(
                f"Couldn't read \"{text}\". Try: Friday, 10/3, tomorrow 3pm.", ephemeral=True)
            return
        undo = None
        try:
            t, promoted = self.service.reschedule(task_id, due, actor="button")
            line = f"Moved #{t.id} to {fmt_et(due)}." + _promo(promoted)
            undo = self.service.last_event_id
        except NotOpen as exc:
            line = f"Couldn't do that: {exc}"
        await self._answer(interaction, line, undo)

    async def handle_undo(self, interaction: discord.Interaction, event_id: int) -> None:
        """Pure SQLite work, no AI: always answers well inside Discord's 3-second limit."""
        if not self._is_max(interaction.user):
            await interaction.response.send_message("Only Max can use these buttons.", ephemeral=True)
            return
        try:
            line = self.service.undo(event_id, actor="button").text
        except UndoRefused as exc:
            line = str(exc)
        await interaction.response.send_message(line)

    # ---- messages: chat, captures, Siri ----------------------------------------------
    def _channel_ids(self) -> set[int]:
        return {c.id for c in (self.inbox_channel, self.today_channel) if c is not None}

    def _is_siri(self, message: discord.Message) -> bool:
        wid = self.settings.inbox_webhook_id
        return wid is not None and getattr(message, "webhook_id", None) == wid

    def _seen_key(self, channel_id: int) -> str | None:
        if self.inbox_channel is not None and channel_id == self.inbox_channel.id:
            return META_INBOX_SEEN
        if self.today_channel is not None and channel_id == self.today_channel.id:
            return META_TODAY_SEEN
        return None

    def _mark_answered(self, message: discord.Message) -> None:
        """Record the channel's high-water mark - only AFTER the reply went out, so a crash in
        between means the message is replayed at the next start (a duplicate beats silence)."""
        key = self._seen_key(message.channel.id)
        if key is None:
            return
        try:
            last = self.service.meta_get(key)
            if last is None or (last.isdigit() and message.id > int(last)):
                self.service.meta_set(key, str(message.id))
        except Exception:  # noqa: BLE001 - e.g. database busy; the reply was already sent
            log.exception("could not record the answered-message mark")

    async def on_message(self, message: discord.Message) -> None:
        if message.channel.id not in self._channel_ids():
            return
        siri = self._is_siri(message)
        # Webhook messages are bot-authored, so the Siri check must come before the bot filter.
        if not siri and (message.author.bot or not self._is_max(message.author)):
            return
        if message.id in self._seen:          # gateway + startup catch-up can both deliver it
            return
        self._seen.add(message.id)
        # Execute each message exactly once, never go silent:
        #  1. every event the handling writes carries {"msg": <message id>} in the SAME database
        #     transaction as the change (db.EVENT_CONTEXT) - so "did this message already act?"
        #     is answered by the database itself, even after a crash;
        #  2. right after the router returns (no await in between) the reply is stored as a
        #     handled record;
        #  3. the reply is sent; only then is the channel's high-water mark advanced.
        # Any later delivery of the same message (startup catch-up after a failed reply or a
        # crash) finds 1 or 2 and RE-SENDS the reply instead of acting again.
        r = self._already_handled(message.id)
        if r is None:
            try:
                ref = getattr(message, "reference", None)
                reply_ids = self.recent.get(getattr(ref, "message_id", None), []) if ref else []
                with db.event_context(msg=message.id):
                    async with message.channel.typing():
                        if siri:
                            r = await self.router.handle_siri(message.content)
                        else:
                            r = await self.router.handle(message.content, reply_ids)
            except Exception:  # noqa: BLE001 - e.g. typing() failed; still answer
                log.exception("message handling error")
                if db.events_for_message(self.service.conn, message.id):
                    r = self._rebuilt_reply(message.id, again=False)
                else:
                    r = chat.Reply("Something went wrong on my side - nothing was changed. Try again.")
            self._store_handled(message.id, r)
        if await self._reply(message, r):
            self._mark_answered(message)
        self.nudge_list()                     # "done #3" etc.: the fresh list follows the reply

    # ---- handled records (exactly-once) -----------------------------------------------
    def _store_handled(self, message_id: int, r: chat.Reply) -> None:
        now = self.clock.now()
        try:
            self.service.meta_set(HANDLED_PREFIX + str(message_id), json.dumps(
                {"text": r.text, "undo": r.undo_event_id, "task_ids": r.task_ids,
                 "action": r.action, "buttons": [list(b) for b in r.buttons],
                 "at": now.isoformat()}))
            with self.service.conn:           # prune records older than 7 days
                self.service.conn.execute(
                    "DELETE FROM meta WHERE key LIKE ? AND json_extract(value, '$.at') < ?",
                    (HANDLED_PREFIX + "%", (now - HANDLED_KEEP).isoformat()))
        except Exception:  # noqa: BLE001 - the events are tagged anyway (see on_message)
            log.exception("could not store the handled record")

    def _undo_still_applies(self, event_id: int | None) -> int | None:
        if not event_id:
            return None
        ev = db.get_event(self.service.conn, event_id)
        if ev is None or ev["undone_by"] is not None or ev["kind"] not in UNDOABLE:
            return None
        if db.from_db(ev["at"]) < self.clock.now() - UNDO_WINDOW:
            return None
        return event_id

    def _already_handled(self, message_id: int) -> chat.Reply | None:
        """The stored reply (or one rebuilt from the tagged events) if this message was
        already handled - so it is re-sent, never executed again."""
        rec = self.service.meta_json(HANDLED_PREFIX + str(message_id))
        if rec:
            return chat.Reply(AGAIN + rec.get("text", ""), self._undo_still_applies(rec.get("undo")),
                              list(rec.get("task_ids") or []), "resent",
                              [tuple(b) for b in rec.get("buttons") or []])
        if db.events_for_message(self.service.conn, message_id):
            return self._rebuilt_reply(message_id, again=True)
        return None

    def _rebuilt_reply(self, message_id: int, again: bool) -> chat.Reply:
        """The change committed but its reply text was lost (process died in between)."""
        evs = db.events_for_message(self.service.conn, message_id)
        said = [e for e in evs if e["kind"] == "chat"]
        if said and len(said) == len(evs):
            text = db.detail_of(said[-1]).get("reply") or chat.HINT
            return chat.Reply((AGAIN if again else "") + text, None, [], "resent")
        changes = [e for e in evs if e["kind"] not in ("chat", "promoted")]
        parts = [f"{e['kind'].replace('_', ' ')}" + (f" #{e['task_id']}" if e["task_id"] else "")
                 for e in changes]
        undo = next((e["id"] for e in reversed(changes) if self._undo_still_applies(e["id"])), None)
        ids = [e["task_id"] for e in changes if e["task_id"]]
        return chat.Reply((AGAIN if again else "") + "Already done: " + ", ".join(parts) + ".",
                          undo, ids, "resent")

    async def replay_inbox(self) -> int:
        """Catch up on #inbox AND #today messages posted while the bot was down (Discord does not
        resend them), however many there are. First run ever for a channel: just remember where
        we are, never replay old history."""
        n = 0
        for channel in (self.inbox_channel, self.today_channel):
            if channel is None:
                continue
            key = self._seen_key(channel.id)
            last = self.service.meta_get(key)
            if last is None or not last.isdigit():
                async for m in channel.history(limit=1):
                    self.service.meta_set(key, str(m.id))
                continue
            # The walk starts at the channel's high-water mark (Discord pages it for us). #today
            # is mostly the bot's own posts: those are skipped with one attribute check, and
            # once every earlier message from Max is answered the mark moves past them, so the
            # next start does not walk them again.
            newest, all_answered = int(last), True
            async for m in channel.history(limit=None, after=discord.Object(id=int(last)),
                                           oldest_first=True):
                newest = max(newest, m.id)
                if m.author.bot and not self._is_siri(m):
                    continue                  # our own posts (and other bots): cheap skip
                if not self._is_max(m.author) and not self._is_siri(m):
                    continue                  # someone else: never answered, never waited for
                if m.id not in self._seen:
                    await self.on_message(m)
                    n += 1
                mark = self.service.meta_get(key) or "0"
                if not (mark.isdigit() and int(mark) >= m.id):
                    all_answered = False      # its reply failed: keep the mark before it
            if all_answered and newest > int(self.service.meta_get(key) or 0):
                self.service.meta_set(key, str(newest))
        if n:
            log.info("caught up on %d message(s) sent while the bot was down", n)
        return n


NEW_ACTIONS = (rules.GOTIT, rules.WAITMORE, rules.START, rules.SKIP, rules.SPLIT, SPLIT_NO,
               SPLIT_ASK)


def _promo(promoted: list[Task]) -> str:
    return "".join(f" Up next: #{p.id} {p.title}." for p in promoted)


def run_bot(settings: Settings, service: TaskService, clock: Clock) -> None:
    bot = DispatchBot(settings, service, clock)
    bot.run(settings.discord_token, log_handler=None)   # logging is configured by __main__
