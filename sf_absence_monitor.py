"""
sf_absence_monitor.py
=====================
Automatic guild-attack absence tracking — the replacement for the old /wg
screenshot-OCR flow.

HOW IT WORKS (fully automatic, no browser):
    Once an hour, for each registered world that has a sentry account, this:
      1. Runs the Rust `sf_report_probe` binary (same subprocess+stdin pattern
         as sf_auth.py's status probe). The probe logs in via SSO, reads
         `systemmessagelist.r` out of the LOGIN response, and returns both the
         newest battle report's body AND the full list of reports still held
         by the server (~7 day retention).
      2. Works out which of those reports have not been posted yet, and fetches
         each missing one by msg_id (the probe accepts a msg_id on its 4th
         stdin line).
      3. Parses each body with sf_absence.parse_absent to get the opponent and
         the list of members who did not sign up.
      4. Writes absences into `nieobecnosci` (+ a `raporty` marker) in the SAME
         format the old /wg flow used, so rankings / /gt_absent_list etc. keep
         working unchanged.
      5. Posts a clean embed to that world's channel (`swiaty.kanal_id`).

WHY DEDUPE BY msg_id AND NOT BY CONTENT:
    The previous version hashed (opponent, absent_names). Two separate battles
    against the same guild with the same absentees would hash identically and
    the second would be silently discarded as a duplicate. msg_id is a stable
    server-side identifier, so it cannot collide across battles.

WHY CATCH-UP EXISTS:
    The probe returns one report body per run. Attack and defense reports can
    arrive less than three hours apart (observed on s20), so an hourly loop
    that only ever looked at the newest report would permanently lose the
    older one. Reports live ~7 days, so anything missed is still fetchable.

FIRST RUN BEHAVIOUR:
    On the very first run for a world there is no history, and the server may
    be holding 30+ old reports. Posting them all would flood the channel, so
    the first run posts only the newest and marks the rest as seen. Set
    BACKFILL_ON_FIRST_RUN = True to post everything instead.

INTEGRATION (main.py setup_hook, after init_db and the other init_* calls):

    from sf_absence_monitor import AbsenceMonitor, init_absence_tables
    init_absence_tables()
    self.absence_monitor = AbsenceMonitor(self)
    self.absence_monitor.start()

The Rust binary path defaults to ./target/release/sf_report_probe (override
with SF_REPORT_PROBE_PATH in .env). Build it with:
    cargo build --release --bin sf_report_probe
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

import discord
from discord import app_commands
from discord.ext import tasks

from sf_absence import extract_section, parse_absent
from sf_auth import decrypt_password  # reuse the existing Fernet decryption
from world_registry import WorldTransformer, registered_world_autocomplete

DB_PATH = "gildia.db"
SF_REPORT_PROBE_PATH = os.getenv("SF_REPORT_PROBE_PATH", "./target/release/sf_report_probe")
PROBE_TIMEOUT_SECONDS = 40  # a report fetch is 2 round-trips, allow a little more than status probe
BATTLE_TYPE_CODES = ("2a", "2d")  # 2a = attack, 2d = defense

# Which report kinds get written to `nieobecnosci`. The old /wg flow counted
# ATTACK absences only, and the ranking queries do not distinguish kinds — so
# including defense here would inflate everyone's absence count against a
# historical baseline that never had it. Set to ("attack", "defense") if you
# decide you want both.
TRACKED_KINDS = ("attack",)

# Safety cap on how many missed reports one run will post, so a long outage
# cannot dump dozens of embeds into a channel at once. The rest stay unseen
# and get picked up on subsequent runs.
MAX_CATCHUP_PER_RUN = 5

# See "FIRST RUN BEHAVIOUR" above.
BACKFILL_ON_FIRST_RUN = False


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH, timeout=5.0)


def init_absence_tables() -> None:
    """Idempotent. Tracks which battle reports have already been posted.

    MIGRATION: the original schema was keyed on a content hash
    (guild_id, swiat, report_hash). That key collides across distinct battles
    with identical opponent+absentees, so the table is rebuilt keyed on the
    server-side msg_id instead. Old rows are carried over where they recorded
    a usable msg_id; rows that only ever had a hash are dropped, which at worst
    causes one already-seen report to be posted a second time.
    """
    conn = _connect()
    cols = conn.execute("PRAGMA table_info(absence_reports_seen)").fetchall()
    pk_cols = {row[1] for row in cols if row[5]}

    target_schema = """
        CREATE TABLE absence_reports_seen (
            guild_id     TEXT    NOT NULL,
            swiat        TEXT    NOT NULL,
            msg_id       INTEGER NOT NULL,
            kind         TEXT,
            opponent     TEXT,
            created_ts   INTEGER,
            absent_count INTEGER,
            posted_at    TIMESTAMP NOT NULL,
            PRIMARY KEY (guild_id, swiat, msg_id)
        )
    """

    if not cols:
        conn.execute(target_schema)
    elif pk_cols != {"guild_id", "swiat", "msg_id"}:
        conn.execute("ALTER TABLE absence_reports_seen RENAME TO absence_reports_seen_old")
        conn.execute(target_schema)
        old_cols = {row[1] for row in
                    conn.execute("PRAGMA table_info(absence_reports_seen_old)").fetchall()}
        if "msg_id" in old_cols:
            conn.execute("""
                INSERT OR IGNORE INTO absence_reports_seen
                    (guild_id, swiat, msg_id, opponent, posted_at)
                SELECT guild_id, swiat, msg_id, opponent, posted_at
                FROM absence_reports_seen_old
                WHERE msg_id IS NOT NULL AND msg_id > 0
            """)
        conn.execute("DROP TABLE absence_reports_seen_old")
        print("sf_absence: rebuilt absence_reports_seen keyed on msg_id")

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _get_monitored_worlds() -> list[dict[str, Any]]:
    """Worlds that have BOTH a channel (swiaty.kanal_id) AND a sentry account
    (sf_accounts) for the same guild+world. Returns one row per such world."""
    conn = _connect()
    rows = conn.execute("""
        SELECT s.guild_id, s.nazwa, s.kanal_id, a.sf_username, a.password_enc
        FROM swiaty s
        JOIN sf_accounts a
          ON a.guild_id = s.guild_id AND a.world_name = s.nazwa
        WHERE s.kanal_id IS NOT NULL AND s.kanal_id != ''
    """).fetchall()
    conn.close()
    return [
        {"guild_id": r[0], "swiat": r[1], "kanal_id": r[2],
         "sf_username": r[3], "password_enc": r[4]}
        for r in rows
    ]


def _seen_msg_ids(guild_id: str, swiat: str) -> set[int]:
    conn = _connect()
    rows = conn.execute(
        "SELECT msg_id FROM absence_reports_seen WHERE guild_id=? AND swiat=?",
        (guild_id, swiat),
    ).fetchall()
    conn.close()
    return {int(r[0]) for r in rows}


def _has_history(guild_id: str, swiat: str) -> bool:
    conn = _connect()
    row = conn.execute(
        "SELECT 1 FROM absence_reports_seen WHERE guild_id=? AND swiat=? LIMIT 1",
        (guild_id, swiat),
    ).fetchone()
    conn.close()
    return row is not None


def _mark_seen(guild_id: str, swiat: str, msg_id: int, kind: str = "",
               opponent: str = "", created_ts: int = 0, absent_count: int = 0) -> None:
    conn = _connect()
    conn.execute(
        """INSERT OR IGNORE INTO absence_reports_seen
           (guild_id, swiat, msg_id, kind, opponent, created_ts, absent_count, posted_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (guild_id, swiat, int(msg_id), kind, opponent, int(created_ts), int(absent_count),
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def _write_absences(guild_id: str, swiat: str, absent: list[str], created_ts: int) -> str:
    """Insert rows in the SAME format the old /wg flow used, so existing
    ranking/query code keeps working unchanged.

    data_raportu is ISO (%Y-%m-%d) — this is what main.py's _parse_report_date
    produces and what every date-based query and cleanup path compares against.
    (The previous version wrote %d.%m.%Y here, which silently made these rows
    invisible to /gt_report_delete and the date filters.)

    The date comes from the report's own creation timestamp, not from now(),
    so a report caught up days late is still filed under the day it happened.
    """
    battle_dt = datetime.fromtimestamp(created_ts) if created_ts else datetime.now()
    data_raportu = battle_dt.strftime("%Y-%m-%d")
    now = datetime.now()
    swiat = swiat.lower()

    conn = _connect()
    conn.execute(
        "INSERT INTO raporty (guild_id, swiat, data_raportu, data_wpisu) VALUES (?, ?, ?, ?)",
        (guild_id, swiat, data_raportu, now),
    )
    for nick in absent:
        conn.execute(
            "INSERT INTO nieobecnosci (guild_id, swiat, nick, data_raportu, data_wpisu) "
            "VALUES (?, ?, ?, ?, ?)",
            (guild_id, swiat, nick, data_raportu, now),
        )
    conn.commit()
    conn.close()
    return data_raportu


# ---------------------------------------------------------------------------
# Probe runner (mirrors sf_auth.run_probe exactly)
# ---------------------------------------------------------------------------

async def run_report_probe(server: str, username: str, password: str,
                           msg_id: int | None = None) -> dict[str, Any]:
    """Run sf_report_probe, return parsed JSON. Password goes via stdin, never
    argv. Always returns a dict with at least {"ok": bool}.

    msg_id pins a specific report (4th stdin line); omit it for the newest.
    """
    if not os.path.exists(SF_REPORT_PROBE_PATH):
        return {"ok": False, "error": (
            f"probe binary not found at {SF_REPORT_PROBE_PATH} — build it with "
            f"`cargo build --release --bin sf_report_probe`"
        )}
    try:
        proc = await asyncio.create_subprocess_exec(
            SF_REPORT_PROBE_PATH,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        fourth = "" if msg_id is None else str(int(msg_id))
        payload = f"{server}\n{username}\n{password}\n{fourth}\n".encode("utf-8")
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(payload), timeout=PROBE_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        return {"ok": False, "error": f"probe timed out after {PROBE_TIMEOUT_SECONDS}s"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"probe failed to run: {exc}"}

    raw = stdout.decode("utf-8", errors="replace").strip()
    if not raw:
        err = stderr.decode("utf-8", errors="replace").strip()
        return {"ok": False, "error": f"probe produced no output. stderr: {err[:300]}"}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "error": f"probe output was not valid JSON: {raw[:300]}"}


# ---------------------------------------------------------------------------
# Embed
# ---------------------------------------------------------------------------

def _build_embed(swiat: str, opponent: str, absent: list[str], kind: str = "attack",
                 battle_ts: int = 0) -> discord.Embed:
    when = (datetime.fromtimestamp(battle_ts) if battle_ts else datetime.now()).strftime("%Y-%m-%d %H:%M")
    # Polish labels; attack vs defense.
    if kind == "defense":
        icon, label = "🛡️", "obronie"
    else:
        icon, label = "⚔️", "ataku"
    if absent:
        desc = "\n".join(f"• {name}" for name in absent)
        colour = discord.Color.red()
    else:
        desc = "✅ Wszyscy zarejestrowani członkowie wzięli udział."
        colour = discord.Color.green()
    embed = discord.Embed(
        title=f"{icon} Nieobecni w {label} gildii — {swiat.upper()}",
        description=desc, colour=colour,
    )
    embed.add_field(name="Przeciwnik", value=opponent or "—", inline=True)
    embed.add_field(name="Nieobecnych", value=str(len(absent)), inline=True)
    embed.set_footer(text=f"Bitwa: {when} • raport automatyczny")
    return embed


# ---------------------------------------------------------------------------
# The hourly monitor
# ---------------------------------------------------------------------------

class AbsenceMonitor:
    """Hourly loop. Structured exactly like sf_auth.SFMonitor."""

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
        # Sequential: each probe is a full login. One at a time keeps load low
        # on a small VPS and avoids hammering the S&F servers.
        for w in worlds:
            try:
                await self._check_world(w)
            except Exception as exc:  # noqa: BLE001
                print(f"sf_absence: error on world {w['swiat']}: {exc}")

    async def _check_world(self, w: dict[str, Any]) -> None:
        await self.check_world_once(w)

    async def _resolve_channel(self, kanal_id: str):
        channel = self.bot.get_channel(int(kanal_id))
        if channel is None:
            channel = await self.bot.fetch_channel(int(kanal_id))
        return channel

    async def check_world_once(self, w: dict[str, Any]) -> dict[str, Any]:
        """Run the full pipeline for one world, posting every battle report
        that has not been posted yet (oldest first).

        Returns:
            {"status": "posted"|"duplicate"|"no_report"|"error",
             "posted": [ {msg_id, kind, opponent, absent, data_raportu}, ... ],
             "skipped": int,      # unseen reports left for a later run
             "detail": str}
        """
        try:
            password = decrypt_password(w["password_enc"])
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "posted": [], "skipped": 0,
                    "detail": f"cannot decrypt sentry password: {exc}"}

        guild_id, swiat = w["guild_id"], w["swiat"]

        try:
            first = await run_report_probe(swiat, w["sf_username"], password)
            if not first.get("ok"):
                return {"status": "no_report", "posted": [], "skipped": 0,
                        "detail": first.get("error", "no report")}

            reports = [r for r in first.get("reports", [])
                       if r.get("kind") in TRACKED_KINDS]
            if not reports:
                return {"status": "no_report", "posted": [], "skipped": 0,
                        "detail": f"server holds no {'/'.join(TRACKED_KINDS)} reports"}

            seen = _seen_msg_ids(guild_id, swiat)
            unseen = [r for r in reports if int(r["msg_id"]) not in seen]
            # Oldest first, so the channel reads chronologically.
            unseen.sort(key=lambda r: int(r.get("created", 0)))

            if not unseen:
                newest = reports[0]
                return {"status": "duplicate", "posted": [], "skipped": 0,
                        "detail": f"newest report (msg_id {newest['msg_id']}) already posted"}

            # First run for this world: post only the newest, mark the backlog
            # as seen so the channel does not get flooded with old battles.
            if not BACKFILL_ON_FIRST_RUN and not _has_history(guild_id, swiat):
                backlog, unseen = unseen[:-1], unseen[-1:]
                for r in backlog:
                    _mark_seen(guild_id, swiat, int(r["msg_id"]), r.get("kind", ""),
                               "", int(r.get("created", 0)), 0)
                if backlog:
                    print(f"sf_absence: {swiat} first run — marked {len(backlog)} "
                          f"old report(s) as seen without posting")

            skipped = max(0, len(unseen) - MAX_CATCHUP_PER_RUN)
            unseen = unseen[:MAX_CATCHUP_PER_RUN]

            channel = None
            try:
                channel = await self._resolve_channel(w["kanal_id"])
            except Exception:  # noqa: BLE001
                return {"status": "error", "posted": [], "skipped": 0,
                        "detail": f"channel {w['kanal_id']} not found"}

            posted: list[dict[str, Any]] = []
            for r in unseen:
                msg_id = int(r["msg_id"])
                created = int(r.get("created", 0))
                kind = r.get("kind", "attack")

                # The newest report's body already came back with the first
                # probe call — only re-run the probe for the older ones.
                if msg_id == int(first.get("msg_id", -1)):
                    body = first.get("body", "")
                else:
                    again = await run_report_probe(swiat, w["sf_username"], password,
                                                   msg_id=msg_id)
                    if not again.get("ok"):
                        print(f"sf_absence: {swiat} could not fetch msg_id {msg_id}: "
                              f"{again.get('error')}")
                        continue
                    body = again.get("body", "")

                section = extract_section(body, "messagetext.s")
                type_code = section.split("/", 1)[0] if section else ""
                if not section or type_code not in BATTLE_TYPE_CODES:
                    print(f"sf_absence: {swiat} msg_id {msg_id} had no battle body, skipping")
                    continue

                opponent, absent = parse_absent(section)
                data_raportu = _write_absences(guild_id, swiat, absent, created)
                try:
                    await channel.send(embed=_build_embed(swiat, opponent, absent, kind, created))
                except discord.DiscordException as exc:
                    return {"status": "error", "posted": posted, "skipped": skipped,
                            "detail": f"failed to post msg_id {msg_id}: {exc}"}

                _mark_seen(guild_id, swiat, msg_id, kind, opponent, created, len(absent))
                posted.append({"msg_id": msg_id, "kind": kind, "opponent": opponent,
                               "absent": absent, "data_raportu": data_raportu})
                print(f"sf_absence: posted {swiat} {kind} vs {opponent} "
                      f"({len(absent)} absent, msg_id {msg_id})")
        finally:
            del password

        if not posted:
            return {"status": "no_report", "posted": [], "skipped": skipped,
                    "detail": "found unposted reports but none produced a usable body"}

        detail = f"posted {len(posted)} report(s) to <#{w['kanal_id']}>"
        if skipped:
            detail += f"; {skipped} more will follow next run"
        return {"status": "posted", "posted": posted, "skipped": skipped, "detail": detail}


# ---------------------------------------------------------------------------
# Manual test command: /gt_absence_check
# ---------------------------------------------------------------------------

def _get_one_world(guild_id: str, swiat: str) -> dict[str, Any] | None:
    """Fetch one world's config (channel + sentry account) for the manual
    check command. Returns None if the world isn't fully set up."""
    conn = _connect()
    row = conn.execute("""
        SELECT s.guild_id, s.nazwa, s.kanal_id, a.sf_username, a.password_enc
        FROM swiaty s
        JOIN sf_accounts a
          ON a.guild_id = s.guild_id AND a.world_name = s.nazwa
        WHERE s.guild_id = ? AND s.nazwa = ?
          AND s.kanal_id IS NOT NULL AND s.kanal_id != ''
    """, (guild_id, swiat.lower())).fetchone()
    conn.close()
    if not row:
        return None
    return {"guild_id": row[0], "swiat": row[1], "kanal_id": row[2],
            "sf_username": row[3], "password_enc": row[4]}


@app_commands.command(
    name="gt_absence_check",
    description="Manually fetch new guild battle reports now and post absences (for testing).",
)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.autocomplete(swiat=registered_world_autocomplete)
@app_commands.describe(swiat="Which world to check (must have a sentry account + channel set)")
@app_commands.guild_only()
async def gt_absence_check(
    interaction: discord.Interaction,
    swiat: app_commands.Transform[str, WorldTransformer],
):
    # The probe login + fetch can take many seconds — defer so we don't hit
    # Discord's 3-second interaction timeout.
    await interaction.response.defer(ephemeral=True, thinking=True)

    w = _get_one_world(str(interaction.guild_id), swiat)
    if not w:
        await interaction.followup.send(
            f"❌ **{swiat}** isn't fully set up. It needs both a registered channel "
            f"(via `/gt_world_add`) and a sentry account (via `/gt_sf_login`).",
            ephemeral=True,
        )
        return

    monitor = getattr(interaction.client, "absence_monitor", None)
    if monitor is None:
        await interaction.followup.send("❌ Absence monitor isn't running.", ephemeral=True)
        return

    res = await monitor.check_world_once(w)
    status = res.get("status")
    posted = res.get("posted", [])
    detail = res.get("detail", "")

    if status == "posted":
        lines = []
        for p in posted:
            names = ", ".join(p["absent"]) if p["absent"] else "nobody — everyone participated"
            lines.append(
                f"**{p['data_raportu']}** ({p['kind']}) vs **{p['opponent']}** — "
                f"{len(p['absent'])}: {names}"
            )
        await interaction.followup.send(
            f"✅ Posted {len(posted)} report(s) for **{swiat}**.\n" + "\n".join(lines) +
            f"\n{detail}",
            ephemeral=True,
        )
    elif status == "duplicate":
        await interaction.followup.send(
            f"ℹ️ Nothing new for **{swiat}** — {detail}.\n"
            f"_(The pipeline is working; this just means no new battle since last time.)_",
            ephemeral=True,
        )
    elif status == "no_report":
        await interaction.followup.send(
            f"⚠️ Couldn't fetch a report for **{swiat}**: {detail}\n"
            f"_(Check the sentry login works, and that the probe binary is built.)_",
            ephemeral=True,
        )
    else:  # error
        await interaction.followup.send(
            f"❌ Something went wrong for **{swiat}**: {detail}", ephemeral=True
        )


@gt_absence_check.error
async def gt_absence_check_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "❌ You need **Manage Server** permission for this."
    else:
        print(f"gt_absence_check error: {error}")
        msg = "❌ Something went wrong."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)
