# Dispatch - setup, step by step

**What this is.** Dispatch is a small program that runs on your Windows PC and talks to you through Discord (a free chat app). Every morning at 8:00 it posts your three to-dos for the day. Anything you haven't answered comes back about once an hour until you mark it done, move it to another day, or kill it. You can snooze an item twice; the third time it only offers **Do now**, **Pick a day** or **Kill**. It stays silent from midnight to 8:00 AM.

## How it will behave (read this first)

- **Every morning at 8:00 AM** it posts your three active to-dos in `#today` and @-mentions you (so your phone buzzes). If the PC was off all morning, the same list goes out as soon as it's back on, even in the afternoon.
- **An item with no date is treated as "do it today."** It shows up about once an hour until you tap one of its buttons. Tapping anything - Done, a snooze, Kill - is what makes it stop.
- **All reminders come in one message.** However many items are waiting, you get at most one message per hour, listing them all.
- **You get two snoozes per item.** The buttons are **+2h**, **Tonight** (8 PM) and **Tomorrow** (9 AM). The third time the item comes back, the snooze buttons are gone. You get only **Do now** (you're doing it; it checks back in an hour), **Pick a day** (you type a day, e.g. `Friday`) or **Kill** (drop it for good).
- **Only three at a time.** Extra items wait in a queue. When you finish, kill or move one of your three to a later day, the best-ranked queued item moves up (anything due within two days first; see "How the morning three are chosen" below). A queued item with a deadline still reminds you once that deadline arrives.
- **Adding things:** in `#inbox` or `#today`, `add: call Sam tomorrow 3pm` adds a to-do. The AI on your PC guesses its area, how much it matters and how long it takes, and tells you its guesses so you can correct them; it never decides to shelve anything.
- **Talking to it:** you can also just type, e.g. `move #7 to Friday` or `what's due today`. Every message you send in `#inbox` or `#today` gets exactly one reply. See "Talking to Dispatch" below.
- **Parking ideas:** start the message with `park:` or `idea:` (e.g. `idea: AI menu pricing for restaurants`) and it's saved but never reminds you. That's the only way something gets parked.
- **Every Sunday at 8:00 AM** a second message lists everything that is waiting - queued and parked - grouped by area, so nothing goes unseen for more than a week. If the PC was off on Sunday, it posts the next time the PC is on.
- **Quiet hours: midnight to 8:00 AM** (Eastern time, including when the clocks change). Nothing is sent then; anything that came due overnight is in the 8:00 AM post.

**How long:** about 45 minutes, once. **You need:** the PC, your iPhone, and your Windows login password.

**How to read the steps:** each step says what to do, then *"You'll see"* - what the screen should show if it worked. If you see something different, stop there and ask Claude, pasting what you see.

A few words used below:
- **PowerShell** - the blue/black window where you type commands. Open it: press the Windows key, type `powershell`, press Enter.
- **Command** - a line of text you paste into PowerShell and run by pressing Enter.
- **Folder path** - the bot lives in `C:\Users\Max Williams\Projects\dispatch`.

---

## Part A - Check the program on the PC (no Discord yet)

**1. Open PowerShell in the Dispatch folder.** Open PowerShell (see above), then paste this and press Enter:

```
cd "C:\Users\Max Williams\Projects\dispatch"
```

*You'll see* the line before your cursor end in `\Projects\dispatch>`.

**2. Install the add-ons Dispatch needs.** Python (the programming language Dispatch is written in) is already on this PC. It needs four add-on packages (ready-made code other people publish): `discord.py` (talks to Discord), `python-dotenv` (reads your settings file), `tzdata` (US Eastern time-zone rules, including daylight saving) and `pytest` (runs the self-checks). Paste:

```
python -m pip install -r requirements.txt
```

*You'll see* a few lines of downloading, ending with `Successfully installed ...` or `Requirement already satisfied`. (Claude installed these from inside the Claude app, which keeps its own private copy - so you still need to run this once yourself.)

**3. Run the self-checks.** Paste:

```
python -m pytest -q
```

*You'll see* a row of dots and then a line like `221 passed` (takes about 15 seconds - one check simulates a whole week). Any `failed` means stop and ask Claude.

**4. Watch a pretend day.** This runs Dispatch against a fake clock and a throwaway database and prints what it *would* send, without touching Discord:

```
python -m dispatch --dry-run --simulate 2026-09-28T07:00:00-04:00 --hours 27
```

*You'll see* a morning post at 8:00 AM with three items, then hourly messages, a snoozed item coming back, and nothing between midnight and 8:00 AM. The last lines say `during quiet hours 00:00-07:59 ET: 0`.

---

## Part B - Discord on your phone and PC

**5. Install Discord.** On the iPhone: App Store, search **Discord**, install, sign in (or create a free account). On the PC: go to `discord.com/download`, install, sign in with the same account.

*You'll see* the Discord window with a column of round icons down the left edge.

**6. Create your private server.** A **server** in Discord is a private space with its own chat rooms; only people you invite can see it. On the PC, click the round **+** icon at the bottom of the left column, choose **Create My Own**, then **For me and my friends**, name it `Dispatch HQ`, click **Create**.

*You'll see* a new round icon on the left and the server name at the top left.

**7. Create the three channels.** A **channel** is one chat room inside a server. Next to **TEXT CHANNELS** click the small **+**, keep **Text** selected, type `today`, click **Create Channel**. Repeat for `inbox` and `scoreboard`. Names must be exactly these, lowercase.

*You'll see* `# today`, `# inbox`, `# scoreboard` listed on the left. (`scoreboard` stays empty for now; it's for a later version.)

**8. Turn on Developer Mode.** Developer Mode is a Discord setting that adds a "Copy ID" option to menus; every server, channel and person has a long ID number, and Dispatch needs two of them. Click the gear icon next to your name (bottom left) -> scroll the left menu to **Advanced** -> switch **Developer Mode** on. Press Esc.

**9. Copy two ID numbers into Notepad.** Open Notepad (Windows key, type `notepad`, Enter).
- Right-click the **Dispatch HQ** icon on the left -> **Copy Server ID** -> paste into Notepad after `server:`.
- Right-click your own name (bottom left, or in any message) -> **Copy User ID** -> paste after `me:`.

*You'll see* two numbers, each 17-20 digits long.

---

## Part C - Create the bot

**10. Create the application.** A **bot** is a Discord account run by a program instead of a person; Dispatch will post as the bot. In a browser go to `https://discord.com/developers/applications`, sign in, click **New Application** (top right), name it `Dispatch`, tick the agreement box, click **Create**.

*You'll see* a page for "Dispatch" with a menu on the left (General Information, OAuth2, Bot, ...).

**11. Get the bot's token and store it safely.** The **token** is the bot's password: whoever has it can post as your bot, so it must never be pasted into a chat, an email or a screenshot. In the left menu click **Bot** -> **Reset Token** -> **Yes, do it!** (enter your Discord password or code if asked) -> **Copy**.
- First save it in a password manager. If you don't have one: go to `bitwarden.com`, create a free account, click **New** -> **Login**, name it `Dispatch bot token`, paste the token into the **Password** field, **Save**.
- Discord shows the token only once. If you lose it, click Reset Token again and use the new one.

*You'll see* in Bitwarden an item called "Dispatch bot token".

**12. Allow the bot to read your messages, and keep it private.** Still on the **Bot** page, scroll to **Privileged Gateway Intents**. An **intent** is a permission switch that tells Discord which kinds of events the bot is allowed to receive. Switch on **Message Content Intent** and click **Save Changes**.

Next, make it private (so nobody else can add your bot to their server). Discord refuses to switch **Public Bot** off while an install link exists, so do this in order:
- In the left menu click **Installation**. Under **Install Link**, change **Discord Provided Link** to **None**. Click **Save Changes**.
- Go back to **Bot** in the left menu and switch **Public Bot** off. Click **Save Changes**.

*You'll see* a green "Your changes have been saved" bar each time. (If Discord shows an error saying a public bot is needed for the install link, the first bullet was skipped.)

**13. Put your settings in the .env file.** The **.env file** is a private settings file in the Dispatch folder; the bot reads its token and IDs from there. It is listed in `.gitignore`, which means git (the tool that saves versions of the code) never saves or uploads it. In PowerShell (still in the Dispatch folder) paste:

```
Copy-Item .env.example .env
notepad .env
```

In Notepad fill in, with no spaces or quotes around the values:
- `DISCORD_TOKEN=` the token (copy it from Bitwarden)
- `DISCORD_GUILD_ID=` the server number from step 9 ("guild" is Discord's internal word for server)
- `MAX_USER_ID=` your user number from step 9

Leave the channel lines blank - the bot finds `today` and `inbox` by name. Save (Ctrl+S) and close Notepad. Then delete the numbers from your step-9 Notepad scratch window (close it, **Don't Save**).

*You'll see* nothing in PowerShell - that's fine.

**14. Invite the bot into Dispatch HQ.** In the Developer Portal, left menu **OAuth2** -> **URL Generator**.
- Under **Scopes** tick `bot` and `applications.commands`. (Scopes say what the bot is being added as: a bot account, allowed to use Discord's buttons and commands.)
- Under **Bot Permissions** tick only these four: **View Channels**, **Send Messages**, **Read Message History**, **Embed Links**. That is everything Dispatch needs; it cannot delete, ban, or change your server.
- Copy the **Generated URL** at the bottom, paste it into your browser, choose **Dispatch HQ**, click **Continue** -> **Authorize**, complete the "I am human" check.

*You'll see* "Authorized". In Discord, "Dispatch" appears in the server's member list, greyed out (offline) until step 16.

---

## Part D - First run

**15. Create the database and load your starting list.** The database (`dispatch.db`) is the single file where every task and every button press is recorded. `seed.json` holds 18 real open items taken from your vault's backlog, plus the Micro Center Strimer return due Sat Oct 3, 11:00 AM. Paste:

```
python -m dispatch migrate
python -m dispatch import-seed
```

*You'll see* `applied [1, 2, 3]` (the three numbered versions of the database layout), then `imported 18 new seed items` and your three active items (the Micro Center return first). Running import-seed again later adds nothing twice.

**16. Start the bot by hand once.** Paste:

```
python -m dispatch run
```

*You'll see* a line containing `connected as Dispatch` and, in Discord, the bot turns green (online). Within a minute it posts in `#today` and @-mentions you: the morning list if it's between 8:00 AM and noon, otherwise an "items waiting on you" message. Try it:
- Tap **+2h** on one item - the bot answers in one line with the new time.
- In `#inbox` type `add: test the bot tomorrow 3pm` - it answers `Added #19 test the bot - ...`.

Stop it with **Ctrl+C** in PowerShell. (If it printed `Can't start yet`, a value in `.env` is missing - it tells you which.)

---

## Part E - Make it run on its own

**17. Keep the PC awake.** If the PC sleeps, Dispatch sleeps. Windows Settings -> **System** -> **Power** (or "Power & battery") -> **Screen, sleep & hibernate timeouts** -> set **"When plugged in, put my device to sleep after"** to **Never**. The screen can still turn off.

**18. Register the start-at-boot task.** **Task Scheduler** is the part of Windows that runs programs on a schedule or at startup. `install_task.ps1` is a small script that registers Dispatch there: start 1 minute after every boot, run even when you're not logged in, and restart it up to 3 times (1 minute apart) if it crashes.
- Press the Windows key, type `powershell`, then **right-click** Windows PowerShell -> **Run as administrator** -> **Yes**. ("Administrator" means allowed to change system settings, which registering a startup task needs.)
- Paste:

```
cd "C:\Users\Max Williams\Projects\dispatch"
powershell -ExecutionPolicy Bypass -File .\install_task.ps1
```

- A box asks for your password: type your **Windows login password** (not your PIN). Windows stores it so the task can run while you're logged out; the script doesn't save it anywhere.

*You'll see* `Installed 'Dispatch bot'`. Start it now without rebooting:

```
Start-ScheduledTask -TaskName "Dispatch bot"
```

*You'll see* the bot go green in Discord within about 30 seconds. From now on, **don't also run `python -m dispatch run` by hand** - two copies would send every message twice.

**19. How to check it's alive later.** Dispatch rewrites `heartbeat.json` in its folder every minute (time of the last loop, and whether Discord is connected). Your vault's Claude sessions look at this file to know Dispatch is live. If the time in it is old, the bot has stopped: run `Start-ScheduledTask -TaskName "Dispatch bot"` or reboot. Error details go to `logs\dispatch.log`.

---

## Part F - iPhone, so the pings actually reach you

**20. Notify on every message from Dispatch HQ.** In the Discord iPhone app, long-press the **Dispatch HQ** icon -> **Notifications** -> choose **All Messages**.

**21. Mute everything else.** Long-press each other server -> **Notifications** -> **Only @mentions** or **Nothing**, or **Mute Server**. This way a Discord buzz almost always means Dispatch.

**22. Let Discord through Focus modes.** Focus is the iPhone feature (Do Not Disturb, Sleep, Work...) that silences apps. iPhone **Settings** -> **Focus** -> for each Focus you use: **Apps** -> **Add Apps** -> **Discord**. Also check iPhone **Settings** -> **Notifications** -> **Discord** -> **Allow Notifications** is on. (Dispatch is already silent midnight-8 AM, so letting it through Sleep focus won't wake you.)

**23. Put Discord in the dock.** Long-press the Discord icon -> **Edit Home Screen** -> drag it into the bottom row of 4 apps. Tap **Done**.

---

## Using it day to day

- **Buttons:** **Done** closes it. **+2h** / **Tonight** (8 PM) / **Tomorrow** (9 AM) snooze it - two snoozes per item. After that the item shows **Do now** (you're on it; it checks back in an hour), **Pick a day** (type a day like `Friday`, `10/3` or `tomorrow 3pm`; a day after today also frees one of your three slots) or **Kill** (drop it for good).
- **Adding:** in `#inbox` or `#today`, type `add:` then the task, optionally ending with when: `add: email professor by Friday`, `add: call Sam tomorrow 3pm`, `add: pay rent 10/1`, `add: stretch in 2 days`, `tonight`, `Wed`, `next Wed`. A plain day (or "this Wed") means the nearest one coming up; "next Wed" means the Wednesday of the following week (weeks start on Sunday), so on a Monday "next Wed" is nine days away. A day with no time means 9:00 AM. The local Qwen model (the AI running on your PC) guesses the area and three numbers explained below; if Qwen is off the item is filed as `unsorted` with standard numbers and still added.
- **Fresh list after a Done:** whenever you finish something (button, typed "done #3", a phone call, or the command line), about 10 seconds later `#today` gets your list as it stands now: your three (saying which one moved up), then anything else due, with the usual buttons. Several Dones in a row give one list. It never @-mentions you, and it isn't sent for `did:` items, Kill, a Done you undid, or between midnight and 8 AM.
- **Three at a time:** only 3 items are *active*. Extra ones wait in a queue and the best-ranked one moves up whenever you finish, kill or push out an active one. Queued items with a deadline still ping you once that deadline arrives. Only `park:` / `idea:` messages are *parked* (never ping); they appear in the Sunday list.
- **Quiet hours:** nothing is sent from midnight to 8:00 AM Eastern; anything that came due overnight is in the 8:00 AM post.

---

## Talking to Dispatch

You don't need `add:` or the buttons for everything. In `#today` or `#inbox` you can type an ordinary sentence and Dispatch does one thing with it. Examples:

- `done #3` or `called Sam, that's done` - marks it done.
- `move the Strimer return to Saturday 1pm` - gives it a new day and time.
- `snooze #11 until 3pm`, `later tonight for the lease thing` - comes back later today.
- `that's more like 3 hours` - changes how long it takes (right after adding something, "that" means the item you just added).
- `low priority: THESIS_TRACKER renumbering`, `the broker call is critical` - changes how much it matters.
- `I'm slammed until 4, move everything after that` - holds every reminder until 4 PM.
- `what's due today`, `what's on my plate this week`, `list`, `what's parked` - answers, changes nothing.
- `undo that` - reverses your last change.

**How it works.** The **#number** is the item's number, shown in front of every item (`#3 Call Sam...`). The local AI (Qwen, a language model that runs on your PC, not on the internet) only *reads* your sentence and picks one action from a fixed list - done, kill, snooze, move, estimate, priority, area, hold-until, question, add, undo, and the "fits my day" ones below (day mode, minutes, where/energy, waiting, got-it, swap). It cannot do anything outside that list. Dates are then read by fixed rules (the same ones as `add:`), never guessed by the AI.

**It asks instead of guessing.** If two items could match - "the 1099 thing" fits two items - it replies `Which one? 1) #15 ... 2) #16 ...`; within 10 minutes answer with the option number (`2`) or the item's #number (`#16`). A `#` always means the item's number. If a plain number could mean either (say `1` when #1 is also one of the choices), it asks once more. If there is no item like what you said, it says so. It never acts on an item it isn't sure about. Writing the #number is always the surest way; a plain number in a sentence only counts as an item number after the word "task" or "item" (`done task 14`), so "took 2 tries" never touches #2.

**It always answers.** Every message you send in these two channels gets exactly one reply - including messages sent while the PC was off: it answers them, oldest first, when it comes back. If it didn't understand, the reply is a short hint: `Couldn't parse that - try "add: ...", "done #7", "move #7 to Friday", or tap a button.` The same hint comes if the AI is switched off; `add:`, `park:`, `idea:` and every button keep working without it. A passing thought ("I should call Sam at some point") is never added by itself - it asks you to confirm with `add:`.

**Undo.** Every change you make by text or button comes with an **Undo** button. Undo puts things back exactly as they were, including an item that moved up into your three because of it. It works for 48 hours. If the item changed again since (say you snoozed it after marking something done), Undo refuses and tells you why, rather than half-undoing.

**The guesses shown when you add something.** Each new item gets three numbers and two labels from the AI, shown in the reply so you can fix them in one message:
- **payoff** (1-5) - how much it matters if you do it; 5 is a big consequence. Fix with "low priority" (2), "not important" (1), "important" (4), "critical" (5).
- **likely to matter** (a percentage) - the chance this task will really be needed. Something that may never matter gets a low number.
- **estimate** - how long it takes. Fix with "more like 20 minutes" or "that's a 3 hour job".
- **where** - *at home* (anything you can do at home or at a desk), *out of the house* (you have to go somewhere: a store, an office), or *a call* (a phone call or a live conversation). Fix with "that's an errand", "#3 is a phone call".
- **energy** - *light* (easy, doable when tired) or *focused* (needs a clear head: thinking, writing, deciding). Fix with "easy one", "#6 is a focus job".
If the AI was off, the item gets standard numbers (payoff 3, 70% likely, 30 minutes, at home, light) and the reply says so. Items added before these two existed get them guessed in the background, eight at a time (about a minute for the whole list). Until then Dispatch goes by the title: "return", "store", "pick up", "drop off" or "drive to" means out of the house, "call" or "phone" means a call, anything else counts as at home and light.

**Siri messages** (see "Say it to Siri" below) are always *adds* (or ideas, if you start with "idea"; or finished work, if you start with "did:"). Corrections and questions are typed in Discord.

## Fitting the list to your day

**Telling it what kind of day it is.** The 8 AM post has three buttons at the bottom: **Sick/home day**, **Slammed**, **Normal**. Typing works too: "home sick today", "slammed today, only quick stuff", "back to normal".
- **Sick/home day** - only things you can do at home, 45 minutes or less, easy ones first (a focused one only when nothing easy fits - except that anything due within two days always comes first).
- **Slammed** - only things of 20 minutes or less, at home or a quick call.
- **Normal** - no filter (the usual three).
You can add details in words: "sick, home only, maybe 2 hours in short bursts" means home only, pieces of up to 30 minutes, about 2 hours in total; "no calls" leaves calls out. The mode lasts until midnight. Setting it picks a new three from what fits, and the reply says what it set aside and why, with an **Undo** button. **Nothing set aside is lost:** those items go back to the queue without using a snooze, come back as soon as you say "normal" (the fillers the mode picked give their places back right away) or the next morning (see below), and appear in the Sunday list. Anything due within two days keeps showing in every hourly message with "can't fit today - pick a day?" and the buttons **Do now**, **Pick a day**, **Kill**.

**The morning after.** On the first morning without a mode, anything the mode put in your three that you didn't touch gives its place back to the items that normally rank higher; the 8 AM post says which ("Back after yesterday's sick day: ..."). Say "undo that" to keep yesterday's three.

**Swapping one of the three.** Each of your three has a **Swap** button. It asks why:
- **Too long** - you get something strictly shorter instead.
- **Can't go out** - something that doesn't need going out (and the item is marked "out of the house").
- **Wrong energy** - something at the other energy level (light instead of focused, or the other way round).
- **Waiting on someone** - see below.
The item you swapped out goes back to the queue (no snooze used) and won't come back the same day. If nothing in the queue fits the reason, it says so, keeps the item, and offers its usual buttons plus **Split it**. Each item can be swapped **twice** in its life; the third time you have to decide: **Do now**, **Pick a day**, **Kill**, or **Split it**. Every swap has **Undo**.

**Splitting a big item.** After an item was swapped for "Too long" twice, or when an item of more than an hour sat in your three untouched for two days, Dispatch suggests a first step of 20 minutes or less (worked out by the local AI). **Split it** puts that step in the item's place in your three; the big item waits in the queue, linked to it ("step of #11"). **Keep as is** leaves everything alone. At most one suggestion a morning; none when the AI is off.

**"I've got 30 minutes" / "what can I do from bed".** You get one suggestion that fits the time (and today's mode - on a slammed day, never more than 20 minutes), with **Start** and **Skip**. Your three stay as they are unless you tap **Start**; Start works like Do now, and if the suggestion was in the queue it takes the place of the lowest-ranked of your three (Undo puts both back). **Skip** shows the next best.

**Waiting on someone.** When an item is blocked on another person ("waiting on the broker for #4", or Swap -> **Waiting on someone**), it leaves your three and stops pinging. On the check-in day (3 days later unless you say another day, e.g. "check in Friday") it shows up once in the hourly message (and again once a week for as long as you leave it unanswered) with **Got it** (back to the list, due now), **Wait more** (3 more days; twice at most, then **Pick a day** takes its place) and **Kill**. "What am I waiting on" lists them, and the Sunday list shows them first under **Waiting**. "Sam got back to me" does the same as Got it.

**Finished something that wasn't on the list?** Type `did: called the bank` (in `#inbox` or `#today`, or by Siri). It is logged as done today - it counts in the stats, and it keeps a sick day from breaking your streak. If you type "done with X" and X isn't on the list, Dispatch points you to `did:`.

## How the morning three are chosen

Every morning, and whenever a slot frees up, Dispatch ranks everything:
1. **Anything due within the next two days (or already overdue) comes first**, whatever its area. A deadline beats everything else.
2. Otherwise each item gets a **score = payoff x likelihood / hours x area weight**, raised as a deadline gets closer (up to 2x at two days, 3x within a day, 4x once overdue). The **area weight** is the share of attention each area gets (Coreflow/CLARA 22, School 18, Side-venture ideas 14, Portfolio 12, System improvement 10, Personal admin 8, Fantasy 6, Nationwide 5, Home & life 5; unsorted counts as 5).
3. Missing numbers use the standard ones (payoff 3, 70%, 30 minutes), so nothing is ever hidden for lack of a guess.

So a quick, important Coreflow item can beat a small errand due in five days, but not one due tomorrow.

**Is it working?** `python -m dispatch stats --days 7` (in PowerShell, in the Dispatch folder) shows, for each of the last 7 days, how many of the morning three you finished before the next morning, and whether you are meeting the Phase 2 goal: at least half of the daily three on at least 4 of 7 days. Days before Phase 2, or when the PC was off all day, say `no data`. Each day also shows everything you finished that day (`done today: 4 (1 off-list)` - "off-list" are `did:` items). The **streak** counts days in a row that went well: at least half of the three done, or a sick/slammed day on which you finished at least one thing; a day with the PC off neither counts nor breaks it.

---

## Canvas feed (optional): your assignments arrive by themselves

Canvas (Ohio State's CarmenCanvas) can publish your whole course calendar as a private **calendar feed** - a web link to a file in the standard calendar format (an `.ics` file) that calendar apps can read. Dispatch reads it every 2 hours, adds each upcoming assignment as a School item with its due date, moves the date if your professor moves it, and never brings back something you already marked done or killed. Past items are skipped.

**If you move a Canvas item yourself** (e.g. `move #19 to Thursday 6pm` to study before the deadline), your date stays: Dispatch only changes the date when Canvas itself shows a new date for that assignment. When that happens you get one short message in `#today`, e.g. `Canvas moved a date - #19 Quiz 3: now due Sat 10/17 11:59 PM (was Fri 10/16 11:59 PM)`, with an **Undo** button (never at night - it waits until 8 AM; several moves come in one message). The morning post also lists them under "Canvas moved since yesterday".

1. On the PC, open `carmen.osu.edu` in a browser and sign in.
2. In the left bar click **Calendar**.
3. In the right-hand column, scroll down and click **Calendar Feed**.
4. A box shows a long link starting with `https://carmen.osu.edu/feeds/calendars/`. Click it once to select it and copy it (Ctrl+C). *You'll see* the link highlighted.
5. In PowerShell run `notepad "C:\Users\Max Williams\Projects\dispatch\.env"`. Find the line `CANVAS_ICS_URL=` (add it at the bottom if it isn't there) and paste the link right after the `=`, with no spaces. Save (Ctrl+S) and close.
6. Restart the bot so it reads the new setting:
   ```
   Stop-ScheduledTask -TaskName "Dispatch bot"; Start-ScheduledTask -TaskName "Dispatch bot"
   ```

*You'll see*, within about 2 minutes, new School items in the next hourly message or the 8 AM post (the morning post also lists `New from Canvas since yesterday: #..`), each with `Canvas:` and the assignment's web address in its notes. An assignment with no set time is due at 11:59 PM that day.

**This link is a password for your Canvas calendar** - anyone who has it can read your calendar. Never paste it into a chat, an email or a screenshot. If it ever leaks, click **Reset** next to Calendar Feed in Canvas and repeat steps 4-6. If the link stops working, Dispatch keeps going, tries again every 2 hours, and sends you at most one short note a day (never at night). Canvas only lists about the next month, so later assignments appear by themselves as they come into range.

---

## Say it to Siri (optional): add a to-do from anywhere

You say "Hey Siri, Dispatch", say the to-do, and a few seconds later it is in Dispatch - from anywhere, on Wi-Fi or cellular, with nothing opened on the PC. It works through a **webhook**: a secret web address that lets whoever holds it post messages into one Discord channel. The iPhone posts your words there; Dispatch reads them like your own messages. A webhook can only *write* into that one channel - it cannot read anything or control the bot.

**Part 1 - the webhook (on the PC, about 3 minutes)**
1. In Discord open **Dispatch HQ**, hover over `# inbox` and click the small gear (**Edit Channel**).
2. In the left menu click **Integrations** -> **Webhooks** -> **New Webhook**.
3. Click the new webhook to open it. Name it `Siri`. The channel must say `#inbox`.
4. Click **Copy Webhook URL**. This **webhook URL** works like a password: never paste it into a chat or an email.
5. Save it in Bitwarden as a new login named `Dispatch Siri webhook` (paste into **Password**), **Save**.
6. The link has the shape `.../api/webhooks/<a long number>/<a long code>`. The long number between `webhooks/` and the next `/` is the **webhook ID**. Open `.env` (`notepad "C:\Users\Max Williams\Projects\dispatch\.env"`), and on the line `INBOX_WEBHOOK_ID=` paste only that number. Save and close. (Only the number goes in `.env`; the full link lives only in the iPhone Shortcut.)
7. Restart the bot (the same two commands as in the Canvas section, step 6).

*You'll see* `siri=on` at the end of the `connected as Dispatch ...` line in `logs\dispatch.log`.

**Part 2 - the Shortcut (on the iPhone, about 5 minutes).** **Shortcuts** is Apple's built-in app for small automations that Siri can run by name.
1. Open the **Shortcuts** app (if it's missing: App Store -> Shortcuts).
2. Tap **+** (top right).
3. Tap the name at the top and rename it `Dispatch` - this is the phrase Siri listens for.
4. Tap **Add Action**, search `Dictate Text`, tap it. Tap the small arrow on that action and set **Stop Listening** to **After Pause**.
5. Tap **Add Action** again, search `Get Contents of URL`, tap it.
6. In that action tap `URL` and paste the webhook URL from Bitwarden. Tap the small arrow to show more: **Method** -> **POST**; **Request Body** -> **JSON**; tap **Add new field** -> **Text**; for the key type `content`; for the value tap the field and choose the blue **Dictated Text** (offered above the keyboard under "Select Variable").
7. Optional: **Add Action** -> `Show Notification`, text `Sent to Dispatch`.
8. Tap **Done**.

*Test:* say "Hey Siri, Dispatch", wait for the tone, say "call Sam tomorrow at three pm", then pause. *You'll see* within about 5 seconds a message in `#inbox` from "Siri" and Dispatch's reply `Added #.. call Sam - ... due Tue ... 3:00 PM`, and your phone buzzes for it. If Siri says it can't find the shortcut, the name must be exactly `Dispatch`.

- To save an idea instead, start with the word "idea": "idea, a fantasy highlight reel app".
- Don't share the Shortcut with anyone or through an iCloud link - it contains the webhook URL.
- If the PC is off, the message still waits in `#inbox`; Dispatch picks it up and replies when it is back on.
- Limit: 20 Siri adds per hour. If the webhook link ever leaks, strangers could post junk into `#inbox` (they cannot see or change anything else). Fix: Discord -> Server Settings -> **Integrations** -> **Webhooks** -> delete `Siri`, then repeat Part 1 and step 6 of Part 2 with the new link.

---

## Command line for Claude (no Discord needed)

Claude sessions and scheduled tasks use `python -m dispatch cli ...` to add, list, finish or move items; the answer is always machine-readable text (JSON). A second add of the same Gmail message or Canvas assignment is ignored. Full list: the top of `dispatch\cli.py`. You never need to type these yourself.

---

## Switching from Phase 1 to Phase 2 (once, on switch day)

`switch_to_phase2.ps1` does the whole switch (to Phase 2 with the 2.1 "fits my day" additions, version 0.2.1; the database gets upgrades 3 and 4) and checks itself; if any check fails it puts Phase 1 back by itself. In a normal PowerShell window:

```
cd "C:\Users\Max Williams\Projects\dispatch"
powershell -ExecutionPolicy Bypass -File ..\dispatch-phase2\switch_to_phase2.ps1
```

*You'll see* twelve numbered steps, each ending in `OK`, and finally `Phase 2.1 is live.` followed by a 5-minute checklist to try in Discord. If a step says `Access is denied`, re-open PowerShell with **Run as administrator** and run the same two lines again (nothing was changed). If any check fails, the script puts Phase 1 back by itself (using the database copy it made a few minutes earlier, so nothing is lost) and tells you whether that worked.

**Going back to Phase 1 later:** in the Dispatch folder run `powershell -ExecutionPolicy Bypass -File .\rollback_phase2.ps1`. It puts the Phase 1 code back and **keeps your database as it is** - every item, done and snooze since the switch stays (Phase 1 can read the upgraded database; items that are "waiting on someone" and the day modes are simply not visible to Phase 1 until you switch again). It also saves a copy in `backups\` first. Only if you want to return to the exact database from switch day, add `-RestoreBackup` at the end; that drops everything added or changed since the switch (the copy in `backups\` still has it).

---

## Phone line (optional): call your list

Call your Dispatch number (a Vapi number, currently (your Vapi number)). It says "Dispatch. What's your code?"; say your 4-digit code (`VOICE_PIN` in `.env`). It reads your three, answers what's due, and takes changes: done, kill, move, snooze, add, `did:`, idea, waiting on someone, sick/slammed/normal day, new list. It confirms each with "Got it" and the item's number.

**What happens after you hang up:** within about a minute the local AI on your PC reads the call's transcript and turns each "Got it" into a normal Dispatch command. Each one runs exactly as if you had typed it in #inbox, and #today gets one message, "From your call at ...", listing what happened, with an **Undo #N** button per change. If something was unclear, you get the usual "Which one?" question in Discord.

**Safety, in plain words.**
- The code check is done by the phone AI. It stops casual and robot callers from hearing your list. It is not a lock.
- The lock is in Dispatch itself: only calls from `MAX_PHONE` (your cell) can change anything. A call from any other number changes nothing, and Discord tells you a stranger called and shows the last four digits.
- No audio is recorded. Vapi keeps the call's text transcript, which is what the local AI reads.
- Your PC isn't opened to the internet. The bot only sends requests out to Vapi once a minute.

**Settings in `.env`:** `VAPI_API_KEY` (a password: never paste it in a chat), `VAPI_ASSISTANT_ID`, `VAPI_PHONE_NUMBER_ID`, `VOICE_PIN`, `MAX_PHONE` (e.g. `+16145551234`). The line is on only when key, assistant, code and phone are all filled in. To check: `python -m dispatch voice status`. After you change the code, restart the bot, or run `python -m dispatch voice push`.

**It calling you:** `python -m dispatch voice call-me` is built but needs a number that can make calls. The free Vapi number can only receive them. That takes a paid number (about $1-2/month) and a card on file in Vapi.

### Phone line v2 (2026-10-18): briefing, brain dumps, holds, Dispatch calls you

- **The briefing.** Before every call, the phone AI gets: the three, each with why it's there and its notes; today's calendar; the next 7 days (calendar, what's due, check-ins); what's up next in Dispatch; and whether calls are on hold. Your calendar is read every 3 hours by Claude Code on this PC, read-only, through your own Google connection.
- **Things you can say on a call:** "what do I need to get done today", "explain number 34", "what's the rest of my week look like", a brain dump (it reads back to-dos and asks which to keep; the whole dump is also saved as a parked note), "no calls for 2 hours", "call me at 4", or "I'm stuck". It's set up as a direct, no-fluff coach: one next action, a start time, a time box, a call back to check.
- **Safety gate.** After the call, a change to a numbered item (done #34, move #7...) is applied only if the phone AI actually said "Got it ... number 34" on the call. Anything else is listed under "Skipped" in #today, so a misread by the local AI can't touch an item you never confirmed.
- **Typed in Discord (no AI needed):** `no calls for 2 hours`, `no calls until 5pm`, `no calls today`, `I'm busy for 30 minutes`, `calls on`, `call me at 4pm`, `call me in 20 minutes`.
- **Dispatch calls you** once `VAPI_OUTBOUND_NUMBER_ID` is set (a paid Vapi number). It calls when:
  - you asked for a call back;
  - one of your three is due within 24 hours or overdue and nobody has touched it for 3 hours;
  - it's after 2 PM and nothing has moved all day.

  It posts a Discord heads-up 5 minutes before an accountability call, so you can reply "no calls" to stop it. Limits:
  - accountability calls only 10 AM-9 PM, callbacks 8 AM-11 PM;
  - at most 3 calls a day, one per item, with 2 hours between accountability calls;
  - no calls once this month's Vapi spend reaches `VOICE_BUDGET_USD` (default $13).
- **Errands** ("email me the Costco memo") are taken down and saved as errands, but **not run yet**: running Claude on the PC from a phone call is switched off pending a safer approval step.
