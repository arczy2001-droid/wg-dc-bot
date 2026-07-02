"""
sf_events.py
============
Tracks in-game event schedules posted by official game webhooks and exposes
a public /events command with full i18n support.
 
HOW IT HOOKS INTO on_message (CRITICAL — READ THIS):
 
    Discord's "Follow Channel" feature forwards messages via a Webhook.
    Webhooks are flagged as bots (message.author.bot = True), so your
    existing `if message.author.bot: return` guard would silently drop every
    event announcement before this module sees it.
 
    The solution: call `handle_event_webhook(message)` BEFORE that guard.
    Only if it returns False (message was not a relevant event webhook) should
    the normal bot-filter logic run.
 
INTEGRATION (in gildia_bot.py):
 
    from sf_events import (
        init_sf_events_tables,
        handle_event_webhook,
        sf_events_setup,
        sf_events_toggle,
        events as events_command,
    )
 
    # in setup_hook, before tree.sync():
    init_sf_events_tables()
    self.tree.add_command(sf_events_setup)
    self.tree.add_command(sf_events_toggle)
    self.tree.add_command(events_command)
 
    # in on_message, as the VERY FIRST thing — before `if message.author.bot`:
    if await handle_event_webhook(message):
        return   # was a webhook event message, already handled
 
EXAMPLE WEBHOOK MESSAGE FORMAT this parser handles:
 
    ⚔️ **Weekend Events** (26.06 - 28.06):
    • Double Gold
    • Sea Monster Invasion
    • Treasure Hunt
 
    Or without bullet points:
    Weekend Events (26.06 - 28.06)
    - Double Gold
    - Raid Event
    - Black Pearl Hunt
 
    Date separators supported: -, –, —, /
    Leading symbols on event lines: •, -, *, –, ▶, empty (plain text)
"""
 
import json
import re
import sqlite3
from datetime import datetime, date
from typing import Optional, List, Tuple
 
import discord
from discord import app_commands
 
DB_PATH = "gildia.db"
 
# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------
 
def init_sf_events_tables() -> None:
    """Idempotent — safe to call on every startup."""
    conn = sqlite3.connect(DB_PATH)

    # Per-guild module config: which channel to listen on, and is it enabled?
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sf_events_config (
            guild_id   TEXT PRIMARY KEY,
            channel_id TEXT,
            enabled    INTEGER NOT NULL DEFAULT 1
        )
    """)

    # Check if sf_events exists with the OLD primary key (guild_id, event_name).
    # The old schema only kept the LAST occurrence of each event name, causing
    # recurring events to disappear from earlier weekends.
    # The new schema is (guild_id, event_name, date_start) so the same event
    # can appear in multiple weekends simultaneously.
    cols = [row[1] for row in conn.execute("PRAGMA table_info(sf_events)").fetchall()]
    if cols and "date_start" not in [
        row[1] for row in conn.execute("PRAGMA index_list(sf_events)").fetchall()
    ]:
        # Check primary key by looking at the table's CREATE statement
        create_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='sf_events'"
        ).fetchone()
        if create_sql and "event_name)" in create_sql[0]:
            # Old schema detected — migrate
            print("sf_events: migrating table to new (guild_id, event_name, date_start) primary key...")
            conn.execute("ALTER TABLE sf_events RENAME TO sf_events_old")
            conn.execute("""
                CREATE TABLE sf_events (
                    guild_id   TEXT NOT NULL,
                    event_name TEXT NOT NULL,
                    date_start TEXT NOT NULL,
                    date_end   TEXT NOT NULL,
                    raw_date   TEXT NOT NULL,
                    updated_at TIMESTAMP NOT NULL,
                    PRIMARY KEY (guild_id, event_name, date_start)
                )
            """)
            # Carry over old data (might be incomplete but better than nothing)
            conn.execute("""
                INSERT OR IGNORE INTO sf_events
                SELECT guild_id, event_name, date_start, date_end, raw_date, updated_at
                FROM sf_events_old
            """)
            conn.execute("DROP TABLE sf_events_old")
            print("sf_events: migration complete. Recommend re-pasting the monthly event message.")
        else:
            pass  # already on new schema
    else:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sf_events (
                guild_id   TEXT NOT NULL,
                event_name TEXT NOT NULL,
                date_start TEXT NOT NULL,
                date_end   TEXT NOT NULL,
                raw_date   TEXT NOT NULL,
                updated_at TIMESTAMP NOT NULL,
                PRIMARY KEY (guild_id, event_name, date_start)
            )
        """)

    conn.commit()
    conn.close()
 
 
def _get_config(guild_id: int) -> Optional[Tuple[str, bool]]:
    """Returns (channel_id, enabled) or None if never configured."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT channel_id, enabled FROM sf_events_config WHERE guild_id = ?",
        (str(guild_id),)
    ).fetchone()
    conn.close()
    if row:
        return row[0], bool(row[1])
    return None
 
 
def _save_events(guild_id: int, events: List[str], date_start: date, date_end: date, raw_date: str) -> None:
    """
    Upserts events for ONE date section.
    PK is now (guild_id, event_name, date_start) so the same event name
    can coexist across multiple weekends without overwriting each other.
    """
    conn = sqlite3.connect(DB_PATH)
    now = datetime.now().isoformat()
    # Clear old entries for this exact weekend (handles game editing its post)
    conn.execute(
        "DELETE FROM sf_events WHERE guild_id = ? AND date_start = ?",
        (str(guild_id), date_start.isoformat())
    )
    for name in events:
        conn.execute(
            """INSERT OR REPLACE INTO sf_events
               (guild_id, event_name, date_start, date_end, raw_date, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (str(guild_id), name, date_start.isoformat(), date_end.isoformat(), raw_date, now)
        )
    conn.commit()
    conn.close()
    print(f"sf_events: saved {len(events)} events for {raw_date} (guild {guild_id})")
 
 
# ---------------------------------------------------------------------------
# EVENT NAME TRANSLATIONS
# ---------------------------------------------------------------------------
# Keys are the English event names AS THEY APPEAR IN THE WEBHOOK (case-insensitive
# match is applied, see _translate_event below).
# Add new events here as you encounter them — the /events command will
# automatically use the translation once added.

EVENT_TRANSLATIONS: dict[str, dict[str, str]] = {
    "Glorious Gold Galore": {
        "pl_PL": "Złote zbiory",
        "de_DE": "THere is no translation right now",
    },
    "Fantastic Fortress Festivity": {
        "pl_PL": "Tango w Twierdzy",
        "de_DE": "THere is no translation right now",
    },
    "Tidy Toilet Time": {
        "pl_PL": "Sterylne Sedesy",
        "de_DE": "THere is no translation right now",
    },
    "Lucky Day": {
        "pl_PL": "Szczęśliwy Dzień",
        "de_DE": "THere is no translation right now",
    },
    "Assembly of Awesome Animals": {
        "pl_PL": "Zgromadzenie Zjawiskowej Zwierzyny",
        "de_DE": "THere is no translation right now",
    },
    "Forge Frenzy Festival": {
        "pl_PL": "Kocioł w Kuźni",
        "de_DE": "THere is no translation right now",
    },
    "Days of Doomed Souls": {
        "pl_PL": "Dni Diabelskich Dusz",
        "de_DE": "THere is no translation right now",
    },
    "Witches' Dance": {
        "pl_PL": "Wiedźmowy Taniec",
        "de_DE": "THere is no translation right now",
    },
    "Exceptional XP Event": {
        "pl_PL": "Prawdziwa Profuzja PD",
        "de_DE": "THere is no translation right now",
    },
    "Sands of Time Special": {
        "pl_PL": "Pięknych Piasków Czasu Czas",
        "de_DE": "THere is no translation right now",
    },
    "Epic Quest Extravaganza": {
        "pl_PL": "Epickie Zapotrzebowanie na Zadanie",
        "de_DE": "THere is no translation right now",
    },
    "Rumble for Riches": {
        "pl_PL": "Walka o Bogactwo",
        "de_DE": "THere is no translation right now",
    },
    "Epic Good Luck Extravaganza": {
        "pl_PL": "Epicki Festiwal Farciarzy",
        "de_DE": "THere is no translation right now",
    },
    "Epic Shopping Spree Extravaganza": {
        "pl_PL": "Epickie Szaleństwo Zakupowe",
        "de_DE": "THere is no translation right now",
    },
    "Catalytic Kaboom": {
        "pl_PL": "Katalizatorowy Wybuch",
        "de_DE": "Katalytischer Knall",
    },
    "Piecework Party": {
        "pl_PL": "Akordowa awanatura",
        "de_DE": "Akkordarbeit-Party",
    },
    "Crazy Mushroom Harvest": {
        "pl_PL": "Szalone Grzybowe Żniwa",
        "de_DE": "Verrückte Pilzernte",
    },
}

_TRANSLATIONS_LOWER = {k.lower(): v for k, v in EVENT_TRANSLATIONS.items()}


def _translate_event(event_name: str, language: str) -> str:
    """Returns the translated event name, or the original English if not found."""
    entry = _TRANSLATIONS_LOWER.get(event_name.lower())
    if entry and language in entry:
        return entry[language]
    return event_name  # graceful fallback — never shows a blank


# ---------------------------------------------------------------------------
# REGEX PARSING
# ---------------------------------------------------------------------------

# Matches date ranges like:
#   (26.06 - 28.06)   standard format
#   (03.07. - 05.07.) trailing dot after month (seen in real webhooks)
#   26.06 – 28.06     no parens, en-dash
#   26.06/28.06       slash separator
# \.? makes the trailing dot after the month optional.
# Groups: (1) start_day, (2) start_month, (3) end_day, (4) end_month
_DATE_RANGE_RE = re.compile(
    r'\(?\s*(\d{1,2})\.(\d{2})\.?\s*[-–—/]\s*(\d{1,2})\.(\d{2})\.?\s*\)?'
)

# Matches a line that starts with a bullet/dash/asterisk and has content.
_BULLET_LINE_RE = re.compile(
    r'^[\s]*[•\-\*–▶➤►][\s]+(.+)$',
    re.MULTILINE
)


def _resolve_year(day: int, month: int) -> int:
    """
    Picks the correct year for a (day, month) pair so the date is always
    in the near future: if the month has already passed this year, we assume
    next year (handles Dec→Jan rollovers cleanly).
    """
    from datetime import timedelta
    today = date.today()
    candidate = date(today.year, month, day)
    if candidate < today - timedelta(days=7):
        candidate = date(today.year + 1, month, day)
    return candidate.year


def parse_event_message(content: str) -> Optional[List[Tuple[date, date, str, List[str]]]]:
    """
    Extracts ALL date sections from a webhook message.

    The game posts a full monthly calendar in one message — multiple weekends,
    each with their own date header and bullet list. This function finds every
    date range in the message and returns the events listed under each one.

    Returns a list of (date_start, date_end, raw_date, [event_names]) tuples,
    one per date section found. Returns None if no date range is found at all.

    Example input:
        Legendary sprint (26.06 - 28.06)
        • Double Gold
        • Sea Monster Invasion

        Festive Variety (03.07. - 05.07.)
        • Exceptional XP Event
        • Rumble for Riches
    """
    # Find all date range matches and their positions in the text
    date_matches = list(_DATE_RANGE_RE.finditer(content))
    if not date_matches:
        return None

    results = []

    for i, match in enumerate(date_matches):
        start_day, start_month, end_day, end_month = (int(x) for x in match.groups())
        raw_date = match.group(0).strip("() \n")

        try:
            date_start = date(_resolve_year(start_day, start_month), start_month, start_day)
            date_end   = date(_resolve_year(end_day,   end_month),   end_month,   end_day)
        except ValueError as e:
            print(f"sf_events: invalid date ({raw_date}): {e}")
            continue

        # The section's text runs from this date match to the next date match
        # (or end of message for the last section).
        section_start = match.end()
        section_end   = date_matches[i + 1].start() if i + 1 < len(date_matches) else len(content)
        section_text  = content[section_start:section_end]

        # Extract bullet lines from this section
        events = [m.group(1).strip() for m in _BULLET_LINE_RE.finditer(section_text)]

        # Fallback: plain non-empty lines that don't look like headers
        if not events:
            for line in section_text.splitlines():
                line = line.strip()
                if line and not line.startswith(("**", "__", "#", "•")):
                    events.append(line)

        if events:
            results.append((date_start, date_end, raw_date, events))

    return results if results else None


# ---------------------------------------------------------------------------
# WEBHOOK HOOK — called from on_message BEFORE the bot filter
# ---------------------------------------------------------------------------

async def handle_event_webhook(message: discord.Message) -> bool:
    """
    Main entry point called from on_message before `if message.author.bot`.
    Processes any message in the configured events channel.
    Returns True if parsed as an event schedule (caller should return immediately).
    """
    if not message.guild:
        return False

    config = _get_config(message.guild.id)
    if not config:
        print(f"sf_events: guild {message.guild.id} — no config found, skipping.")
        return False

    channel_id, enabled = config
    if not enabled:
        print(f"sf_events: guild {message.guild.id} — module disabled, skipping.")
        return False

    if str(message.channel.id) != str(channel_id):
        # Not the events channel — silent, this fires for every message
        return False

    # At this point we know it's the right channel — log everything
    print(f"sf_events: message in configured channel from {message.author} (webhook={message.webhook_id})")

    content = message.content or ""

    # Discord's "Forward Message" feature (the arrow button) stores the
    # original message in message_snapshots, NOT in message.content which
    # is always empty for forwards. This lets admins test by forwarding
    # the game's monthly post manually.
    if not content:
        # Try discord.py's native attribute (newer versions)
        snapshots = getattr(message, 'message_snapshots', None)
        if snapshots:
            for snapshot in snapshots:
                snap_msg = getattr(snapshot, 'message', None)
                if snap_msg:
                    content = getattr(snap_msg, 'content', '') or ''
                    if content:
                        print(f"sf_events: reading content from message_snapshots (native)")
                        break

        # Fallback: read raw Discord API dict directly (works on all discord.py versions)
        if not content:
            raw = getattr(message, '_data', {}) or {}
            for snap in raw.get('message_snapshots', []):
                content = snap.get('message', {}).get('content', '') or ''
                if content:
                    print(f"sf_events: reading content from _data['message_snapshots'] (raw)")
                    break

    # Also check embeds — some webhooks put content in embed descriptions
    if not content and message.embeds:
        content = "\n".join(
            (e.description or "") + "\n" + "\n".join(f.value for f in e.fields)
            for e in message.embeds
        )

    print(f"sf_events: content length = {len(content)} chars, first 200: {repr(content[:200])}")

    results = parse_event_message(content)
    if not results:
        print(f"sf_events: parse_event_message returned None — no date pattern found in message.")
        return False

    total_events = 0
    for date_start, date_end, raw_date, events in results:
        _save_events(message.guild.id, events, date_start, date_end, raw_date)
        total_events += len(events)

    print(f"sf_events: ✅ saved {len(results)} sections, {total_events} events total for guild {message.guild.id}")
    return True


# ---------------------------------------------------------------------------
# ADMIN COMMANDS
# ---------------------------------------------------------------------------

@app_commands.command(
    name="sf_events_setup",
    description="Set the channel where official game event webhooks arrive (admin only)."
)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(channel="The channel that receives official game news/event webhooks")
async def sf_events_setup(interaction: discord.Interaction, channel: discord.TextChannel):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO sf_events_config (guild_id, channel_id, enabled)
           VALUES (?, ?, 1)
           ON CONFLICT(guild_id) DO UPDATE SET channel_id = excluded.channel_id""",
        (str(interaction.guild_id), str(channel.id))
    )
    conn.commit()
    conn.close()
    await interaction.response.send_message(
        f"✅ SF Events module set up. Watching {channel.mention} for event schedules.\n"
        f"Use `/sf_events_toggle` to enable or disable the module at any time.",
        ephemeral=True,
    )


@sf_events_setup.error
async def sf_events_setup_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ You need **Manage Server** permission for this.", ephemeral=True)
    else:
        print(f"sf_events_setup error: {error}")
        await interaction.response.send_message("❌ Something went wrong.", ephemeral=True)


@app_commands.command(
    name="sf_events_toggle",
    description="Enable or disable the SF events tracking module (admin only)."
)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(state="True to enable, False to disable")
async def sf_events_toggle(interaction: discord.Interaction, state: bool):
    conn = sqlite3.connect(DB_PATH)
    existing = conn.execute(
        "SELECT 1 FROM sf_events_config WHERE guild_id = ?", (str(interaction.guild_id),)
    ).fetchone()

    if not existing:
        conn.close()
        await interaction.response.send_message(
            "❌ Module not configured yet. Run `/sf_events_setup` first.",
            ephemeral=True,
        )
        return

    conn.execute(
        "UPDATE sf_events_config SET enabled = ? WHERE guild_id = ?",
        (1 if state else 0, str(interaction.guild_id))
    )
    conn.commit()
    conn.close()

    emoji = "✅" if state else "🔕"
    status = "enabled" if state else "disabled"
    await interaction.response.send_message(
        f"{emoji} SF Events module is now **{status}** for this server.",
        ephemeral=True,
    )


@sf_events_toggle.error
async def sf_events_toggle_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ You need **Manage Server** permission for this.", ephemeral=True)
    else:
        print(f"sf_events_toggle error: {error}")
        await interaction.response.send_message("❌ Something went wrong.", ephemeral=True)


# ---------------------------------------------------------------------------
# PUBLIC /events COMMAND
# ---------------------------------------------------------------------------

def _get_guild_language(guild_id: int) -> str:
    """Reads the server's configured language from guild_config."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT language FROM guild_config WHERE guild_id = ?", (str(guild_id),)
    ).fetchone()
    conn.close()
    return row[0] if row and row[0] else "en_US"


def _get_current_events(guild_id: int) -> List[Tuple[str, str, str, str]]:
    """
    Returns events for the NEXT upcoming weekend only (not the whole month).
    Logic:
    - If there is an active event RIGHT NOW (today between date_start and date_end),
      return that weekend's events.
    - Otherwise return the next future weekend's events.
    This keeps the /events embed short and relevant.
    """
    today = date.today().isoformat()
    conn = sqlite3.connect(DB_PATH)

    # First check: is there a currently active weekend?
    active = conn.execute(
        """SELECT event_name, date_start, date_end, raw_date
           FROM sf_events
           WHERE guild_id = ? AND date_start <= ? AND date_end >= ?
           ORDER BY date_start ASC""",
        (str(guild_id), today, today)
    ).fetchall()

    if active:
        conn.close()
        return active

    # No active event — find the next upcoming weekend
    next_date = conn.execute(
        "SELECT MIN(date_start) FROM sf_events WHERE guild_id = ? AND date_start > ?",
        (str(guild_id), today)
    ).fetchone()[0]

    if not next_date:
        conn.close()
        return []

    rows = conn.execute(
        """SELECT event_name, date_start, date_end, raw_date
           FROM sf_events
           WHERE guild_id = ? AND date_start = ?
           ORDER BY event_name ASC""",
        (str(guild_id), next_date)
    ).fetchall()
    conn.close()
    return rows


# Embed colour per language — purely cosmetic
_EMBED_COLOURS = {
    "pl_PL": discord.Color.from_rgb(255, 215, 0),   # gold
    "de_DE": discord.Color.from_rgb(0, 0, 0),        # black
    "en_US": discord.Color.blurple(),
}

# Localised embed header strings
_EMBED_TITLES = {
    "pl_PL": "⚔️ Nadchodzące wydarzenia",
    "de_DE": "⚔️ Bevorstehende Events",
    "en_US": "⚔️ Upcoming Events",
}

_EMBED_NO_EVENTS = {
    "pl_PL": "Brak zaplanowanych wydarzeń na ten weekend.",
    "de_DE": "Keine Events für dieses Wochenende geplant.",
    "en_US": "No events scheduled for this weekend.",
}

_EMBED_DATE_LABEL = {
    "pl_PL": "📅 Termin",
    "de_DE": "📅 Zeitraum",
    "en_US": "📅 Dates",
}

_EMBED_FOOTER = {
    "pl_PL": "Dane z oficjalnego kanału aktualności · Aktualizowane automatycznie",
    "de_DE": "Daten aus dem offiziellen News-Kanal · Wird automatisch aktualisiert",
    "en_US": "Data from official news channel · Updated automatically",
}


@app_commands.command(
    name="events",
    description="Show current and upcoming in-game events for this weekend."
)
@app_commands.guild_only()
async def events(interaction: discord.Interaction):
    # NOTE: /events intentionally does NOT call sprawdz_pozwolenie() —
    # it's a public info command that should work in any channel.
    config = _get_config(interaction.guild_id)
    if not config or not config[1]:
        await interaction.response.send_message(
            "❌ The events module is not set up or is disabled on this server. "
            "Ask an admin to run `/sf_events_setup`.",
            ephemeral=True,
        )
        return

    language = _get_guild_language(interaction.guild_id)
    rows = _get_current_events(interaction.guild_id)

    colour = _EMBED_COLOURS.get(language, discord.Color.blurple())
    title  = _EMBED_TITLES.get(language, _EMBED_TITLES["en_US"])
    footer = _EMBED_FOOTER.get(language, _EMBED_FOOTER["en_US"])

    embed = discord.Embed(title=title, color=colour)
    embed.set_footer(text=footer)
    embed.timestamp = discord.utils.utcnow()

    if not rows:
        embed.description = _EMBED_NO_EVENTS.get(language, _EMBED_NO_EVENTS["en_US"])
        await interaction.response.send_message(embed=embed)
        return

    # Group events by date range so events sharing the same weekend
    # appear neatly under one date header rather than repeating it.
    groups: dict[str, list[str]] = {}
    for event_name, date_start, date_end, raw_date in rows:
        translated = _translate_event(event_name, language)
        groups.setdefault(raw_date, []).append(translated)

    date_label = _EMBED_DATE_LABEL.get(language, _EMBED_DATE_LABEL["en_US"])
    for raw_date, event_list in groups.items():
        embed.add_field(
            name=f"{date_label}: `{raw_date}`",
            value="\n".join(f"• {e}" for e in event_list),
            inline=False,
        )

    await interaction.response.send_message(embed=embed)


@events.error
async def events_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.NoPrivateMessage):
        await interaction.response.send_message("❌ This command can only be used inside a server.", ephemeral=True)
    else:
        print(f"events command error: {error}")
        await interaction.response.send_message("❌ Something went wrong fetching events.", ephemeral=True)
