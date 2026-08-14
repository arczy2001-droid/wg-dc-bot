"""
sf_absence_monitor.py
=====================
Hourly background task that replaces the old /wg screenshot-OCR flow.

For each registered world in `swiaty`, it uses the stored monitor/sentry
credentials to capture the latest guild-attack report from network traffic
(sf_capture), parses the absentee list (sf_absence, validated), dedupes by
report identity so the same battle is never posted twice, and posts a clean
embed to that world's configured channel (`swiaty.kanal_id`).

INTEGRATION (in main.py setup_hook, before tree.sync):
    from sf_absence_monitor import AbsenceMonitor, init_absence_tables
    init_absence_tables()
    self.absence_monitor = AbsenceMonitor(self)
    self.absence_monitor.start()

CREDENTIALS:
    Per-world sentry credentials are read from the existing sf_accounts table
    (guild_id, world_name, sf_username, password_enc) via sf_auth's helpers,
    so we reuse the SAME encrypted store the attack-monitor already uses — no
    new place that holds passwords. Only worlds that have BOTH a sentry
    account AND a kanal_id are checked.

SAFETY / ROBUSTNESS:
    - One world's failure (login, capture, parse) never aborts the others.
    - Dedup by (guild_id, world, opponent, absent-set hash) so re-captures of
      the same battle don't repost. msg_id would be ideal but the attack
      report isn't in the personal messagelist, so we key on report content.
    - All DB access parameterized; busy timeout for safety.
"""

from __future__ import annotations

import hashlib
import sqlite3
import traceback
from datetime import datetime, timezone
from typing import Optional

import discord
from discord.ext import tasks

from sf_capture import capture_report

DB_PATH = "gildia.db"


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH, timeout=5.0)


def init_absence_tables() -> None:
    """Idempotent. Tracks which battles we've already posted."""
    conn = _connect()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS absence_reports_seen (
            guild_id     TEXT NOT NULL,
            world        TEXT NOT NULL,
            report_hash  TEXT NOT NULL,   -- opponent + sorted absent names
            opponent     TEXT,
            posted_at    TIMESTAMP NOT NULL,
            PRIMARY KEY (guild_id, world, report_hash)
        )
    """)
    conn.commit()
    conn.close()


def _report_hash(opponent: str, absent: list[str]) -> str:
    """Stable identity for a battle report: opponent + the exact absent set.
    Two captures of the same battle produce the same hash → no double-post."""
    key = opponent + "|" + "|".join(sorted(absent))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def _already_posted(guild_id: str, world: str, report_hash: str) -> bool:
    conn = _connect()
    row = conn.execute(
        "SELECT 1 FROM absence_reports_seen WHERE guild_id=? AND world=? AND report_hash=?",
        (guild_id, world, report_hash),
    ).fetchone()
    conn.close()
    return row is not None


def _mark_posted(guild_id: str, world: str, report_hash: str, opponent: str) -> None:
    conn = _connect()
    conn.execute(
        """INSERT OR IGNORE INTO absence_reports_seen
           (guild_id, world, report_hash, opponent, posted_at) VALUES (?, ?, ?, ?, ?)""",
        (guild_id, world, report_hash, opponent, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def _get_monitored_worlds() -> list[dict]:
    """Worlds that have BOTH a configured channel (swiaty.kanal_id) AND a
    sentry account (sf_accounts). Returns per-world dicts with everything
    needed to log in and post."""
    conn = _connect()
    # sf_accounts schema (from sf_auth.py): guild_id, discord_user_id,
    # world_name, sf_username, password_enc, auto_checks, ...
    rows = conn.execute("""
        SELECT s.guild_id, s.nazwa, s.kanal_id,
               a.sf_username, a.password_enc
        FROM swiaty s
        JOIN sf_accounts a
          ON a.guild_id = s.guild_id AND a.world_name = s.nazwa
        WHERE s.kanal_id IS NOT NULL AND s.kanal_id != ''
    """).fetchall()
    conn.close()
    return [
        {"guild_id": r[0], "world": r[1], "kanal_id": r[2],
         "sf_username": r[3], "password_enc": r[4]}
        for r in rows
    ]


def _decrypt_password(password_enc) -> Optional[str]:
    """Reuse sf_auth's Fernet decryption so we never re-implement crypto."""
    try:
        from sf_auth import _decrypt  # existing helper in the project
        return _decrypt(password_enc)
    except Exception as exc:  # noqa: BLE001
        print(f"sf_absence: could not decrypt sentry password: {exc}")
        return None


def _build_embed(world: str, opponent: str, absent: list[str]) -> discord.Embed:
    when = datetime.now().strftime("%Y-%m-%d %H:%M")
    if absent:
        desc = "\n".join(f"• {name}" for name in absent)
        colour = discord.Color.red()
        title = f"⚔️ Nieobecni w ataku gildii — {world.upper()}"
    else:
        desc = "✅ Wszyscy zarejestrowani członkowie wzięli udział."
        colour = discord.Color.green()
        title = f"⚔️ Atak gildii — {world.upper()}"

    embed = discord.Embed(title=title, description=desc, colour=colour)
    embed.add_field(name="Przeciwnik", value=opponent or "—", inline=True)
    embed.add_field(name="Nieobecnych", value=str(len(absent)), inline=True)
    embed.set_footer(text=f"Automatyczny raport • {when}")
    return embed


class AbsenceMonitor:
    """Hourly loop. Mirrors the structure of sf_auth.SFMonitor."""

    def __init__(self, bot: discord.Client) -> None:
        self.bot = bot
        self._loop = tasks.loop(hours=1)(self._run)
        self._loop.before_loop(self._before)

    def start(self) -> None:
        self._loop.start()

    def stop(self) -> None:
        self._loop.cancel()

    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    async def _run(self) -> None:
        worlds = _get_monitored_worlds()
        if not worlds:
            return
        print(f"sf_absence: hourly check for {len(worlds)} world(s)")

        # Sequential on purpose: each capture launches a headless Chromium
        # (~200-400 MB). Running them one at a time keeps peak RAM to a single
        # browser, which matters on the 1 GB box. Reuse of a browser across
        # worlds is possible later, but sequential-and-simple is safer first.
        for w in worlds:
            try:
                await self._check_world(w)
            except Exception as exc:  # noqa: BLE001
                print(f"sf_absence: error on world {w['world']}: {exc}")
                if __debug__:
                    traceback.print_exc()

    async def _check_world(self, w: dict) -> None:
        password = _decrypt_password(w["password_enc"])
        if not password:
            return

        # Derive the connectable server domain from the world label if needed.
        # world_registry.resolve_server_domain would do this; sf_accounts
        # already stores canonical names in this project, so use as-is.
        server = w["world"]

        result = await capture_report(server, w["sf_username"], password, headless=True)
        del password

        if not result:
            print(f"sf_absence: no report captured for {w['world']} this cycle")
            return

        opponent = result["opponent"]
        absent = result["absent"]
        rhash = _report_hash(opponent, absent)

        if _already_posted(w["guild_id"], w["world"], rhash):
            return  # same battle already posted — skip silently

        channel = self.bot.get_channel(int(w["kanal_id"]))
        if channel is None:
            print(f"sf_absence: channel {w['kanal_id']} not found for {w['world']}")
            return

        try:
            await channel.send(embed=_build_embed(w["world"], opponent, absent))
            _mark_posted(w["guild_id"], w["world"], rhash, opponent)
            print(f"sf_absence: posted report for {w['world']} vs {opponent} "
                  f"({len(absent)} absent)")
        except discord.DiscordException as exc:
            print(f"sf_absence: failed to post to channel for {w['world']}: {exc}")
