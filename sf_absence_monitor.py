"""
sf_absence_monitor.py
=====================
Automatic guild-attack absence tracking — the replacement for the old /wg
screenshot-OCR flow.

HOW IT WORKS (fully automatic, no browser):
    Once an hour, for each registered world that has a sentry account, this:
      1. Runs the Rust `sf_report_probe` binary (same subprocess+stdin pattern
         as sf_auth.py's status probe) which logs in via SSO, fetches the
         newest guild ATTACK report over the raw protocol, and prints its
         `messagetext.s` body as JSON.
      2. Parses that body with sf_absence.parse_absent (validated against two
         real battles) to get the opponent + list of absent members.
      3. Dedupes by report content so the same battle is never posted twice.
      4. Writes absences into `nieobecnosci` (+ a `raporty` marker) in the
         SAME format the old /wg flow used, so rankings / /gt_absent_list etc.
         keep working unchanged.
      5. Posts a clean embed to that world's channel (`swiaty.kanal_id`) —
         where the old /wg reports used to land.

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
import hashlib
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


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH, timeout=5.0)


def init_absence_tables() -> None:
    """Idempotent. Tracks which battle reports we've already posted so a
    re-fetched report isn't posted twice."""
    conn = _connect()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS absence_reports_seen (
            guild_id     TEXT NOT NULL,
            swiat        TEXT NOT NULL,
            report_hash  TEXT NOT NULL,
            opponent     TEXT,
            msg_id       INTEGER,
            posted_at    TIMESTAMP NOT NULL,
            PRIMARY KEY (guild_id, swiat, report_hash)
        )
    """)
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


def _report_hash(opponent: str, absent: list[str]) -> str:
    key = opponent + "|" + "|".join(sorted(absent))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def _already_posted(guild_id: str, swiat: str, report_hash: str) -> bool:
    conn = _connect()
    row = conn.execute(
        "SELECT 1 FROM absence_reports_seen WHERE guild_id=? AND swiat=? AND report_hash=?",
        (guild_id, swiat, report_hash),
    ).fetchone()
    conn.close()
    return row is not None


def _mark_posted(guild_id: str, swiat: str, report_hash: str, opponent: str, msg_id: int) -> None:
    conn = _connect()
    conn.execute(
        """INSERT OR IGNORE INTO absence_reports_seen
           (guild_id, swiat, report_hash, opponent, msg_id, posted_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (guild_id, swiat, report_hash, opponent, msg_id,
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def _write_absences(guild_id: str, swiat: str, absent: list[str]) -> None:
    """Insert rows in the SAME format the old /wg flow used, so existing
    ranking/query code keeps working unchanged."""
    now = datetime.now()
    data_raportu = now.strftime("%d.%m.%Y")
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


# ---------------------------------------------------------------------------
# Probe runner (mirrors sf_auth.run_probe exactly)
# ---------------------------------------------------------------------------

async def run_report_probe(server: str, username: str, password: str) -> dict[str, Any]:
    """Run sf_report_probe, return parsed JSON. Password goes via stdin, never
    argv. Always returns a dict with at least {"ok": bool}."""
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
        payload = f"{server}\n{username}\n{password}\n".encode("utf-8")
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

def _build_embed(swiat: str, opponent: str, absent: list[str], kind: str = "attack") -> discord.Embed:
    when = datetime.now().strftime("%Y-%m-%d %H:%M")
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
    embed.set_footer(text=f"Automatyczny raport • {when}")
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

    async def check_world_once(self, w: dict[str, Any]) -> dict[str, Any]:
        """Run the full pipeline for one world. Returns a result dict so a
        manual command can report what happened:
            {"status": "posted"|"duplicate"|"no_report"|"error"|"not_attack",
             "opponent": str, "absent": [str], "detail": str}
        """
        try:
            password = decrypt_password(w["password_enc"])
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "detail": f"cannot decrypt sentry password: {exc}"}

        result = await run_report_probe(w["swiat"], w["sf_username"], password)
        del password

        if not result.get("ok"):
            return {"status": "no_report", "detail": result.get("error", "no report")}

        body = result.get("body", "")
        section = extract_section(body, "messagetext.s")
        type_code = section.split("/", 1)[0] if section else ""
        if not section or type_code not in BATTLE_TYPE_CODES:
            return {"status": "not_attack", "detail": "latest report was not a battle report"}

        # kind comes from the probe ("attack"/"defense"); fall back to type code.
        kind = result.get("kind") or ("defense" if type_code == "2d" else "attack")
        opponent, absent = parse_absent(section)
        rhash = _report_hash(opponent, absent)

        if _already_posted(w["guild_id"], w["swiat"], rhash):
            return {"status": "duplicate", "opponent": opponent, "absent": absent,
                    "kind": kind, "detail": "this battle was already posted"}

        channel = self.bot.get_channel(int(w["kanal_id"]))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(w["kanal_id"]))
            except Exception:  # noqa: BLE001
                return {"status": "error", "opponent": opponent, "absent": absent,
                        "detail": f"channel {w['kanal_id']} not found"}

        _write_absences(w["guild_id"], w["swiat"], absent)
        try:
            await channel.send(embed=_build_embed(w["swiat"], opponent, absent, kind))
        except discord.DiscordException as exc:
            return {"status": "error", "opponent": opponent, "absent": absent,
                    "kind": kind, "detail": f"failed to post: {exc}"}
        _mark_posted(w["guild_id"], w["swiat"], rhash, opponent, int(result.get("msg_id", 0)))
        print(f"sf_absence: posted {w['swiat']} {kind} vs {opponent} ({len(absent)} absent)")
        return {"status": "posted", "opponent": opponent, "absent": absent,
                "kind": kind, "detail": f"posted to <#{w['kanal_id']}>"}


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
    description="Manually fetch the latest guild attack report now and post absences (for testing).",
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
    opponent = res.get("opponent", "—")
    absent = res.get("absent", [])
    detail = res.get("detail", "")

    if status == "posted":
        names = ", ".join(absent) if absent else "nobody — everyone participated"
        await interaction.followup.send(
            f"✅ Fetched and posted the latest attack report for **{swiat}**.\n"
            f"**Opponent:** {opponent}\n**Absent ({len(absent)}):** {names}\n{detail}",
            ephemeral=True,
        )
    elif status == "duplicate":
        names = ", ".join(absent) if absent else "nobody"
        await interaction.followup.send(
            f"ℹ️ The latest report for **{swiat}** was already posted, so it wasn't re-posted.\n"
            f"**Opponent:** {opponent}\n**Absent ({len(absent)}):** {names}\n"
            f"_(The parser is working — this just means no new battle since last time.)_",
            ephemeral=True,
        )
    elif status == "not_attack":
        await interaction.followup.send(
            f"ℹ️ The newest report for **{swiat}** isn't an attack report ({detail}). "
            f"Nothing to post right now.",
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
