"""
sf_absence_receiver.py
======================
Localhost-only HTTP receiver for guild-attack absence reports captured by the
browser userscript (see sf_absence_userscript.js).

SECURITY MODEL — why this is safe despite "running a web server":
    - Binds ONLY to 127.0.0.1 (localhost). The public internet cannot reach
      it. Data arrives via an SSH tunnel the operator opens while playing:
          ssh -L 8787:localhost:8787 user@vps
    - Every request must carry a shared secret token (SF_INGEST_TOKEN) in the
      X-Ingest-Token header. Requests without the exact token are rejected
      with 401. This protects against other local processes on the operator's
      machine, not just remote attackers (belt and suspenders).
    - The payload is the raw 'messagetext.s' body; parsing happens server-side
      with the already-validated parser, so the browser can't inject a
      hand-crafted absence list in a different format.

WHAT IT DOES on a valid POST:
    1. Parse the raw report body -> (opponent, absent[]).
    2. Resolve which world/guild this is for (from the request, matched to a
       registered world in `swiaty`).
    3. Dedupe by report content (opponent + absent set) so re-opening the same
       report in the browser doesn't double-post.
    4. Write absence rows into `nieobecnosci` (same schema the old /wg flow
       used, so rankings/queries keep working) + a `raporty` marker.
    5. Post a clean embed to that world's `swiaty.kanal_id`.

INTEGRATION (main.py setup_hook):
    from sf_absence_receiver import init_absence_tables, start_receiver
    init_absence_tables()
    await start_receiver(self)   # self = the bot/client
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import datetime, timezone

import discord
from aiohttp import web

from sf_absence import extract_section, parse_absent

DB_PATH = "gildia.db"
HOST = "127.0.0.1"           # localhost ONLY — never 0.0.0.0
PORT = int(os.getenv("SF_INGEST_PORT", "8787"))
# Shared secret. MUST be set in the environment; the userscript sends the same
# value. If unset, the receiver refuses to start rather than run unprotected.
INGEST_TOKEN = os.getenv("SF_INGEST_TOKEN")

ATTACK_TYPE_CODE = "2a"


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH, timeout=5.0)


def init_absence_tables() -> None:
    conn = _connect()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS absence_reports_seen (
            guild_id     TEXT NOT NULL,
            swiat        TEXT NOT NULL,
            report_hash  TEXT NOT NULL,
            opponent     TEXT,
            posted_at    TIMESTAMP NOT NULL,
            PRIMARY KEY (guild_id, swiat, report_hash)
        )
    """)
    conn.commit()
    conn.close()


def _report_hash(opponent: str, absent: list[str]) -> str:
    key = opponent + "|" + "|".join(sorted(absent))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def _resolve_world(guild_id: str, swiat: str) -> tuple[str, str] | None:
    """Confirm this (guild_id, swiat) is a registered world and return its
    channel id. Returns (kanal_id, swiat) or None if not registered."""
    conn = _connect()
    row = conn.execute(
        "SELECT kanal_id FROM swiaty WHERE guild_id=? AND nazwa=? AND kanal_id IS NOT NULL AND kanal_id != ''",
        (guild_id, swiat.lower()),
    ).fetchone()
    conn.close()
    return (row[0], swiat.lower()) if row else None


def _write_absences(guild_id: str, swiat: str, absent: list[str]) -> None:
    """Insert absence rows in the SAME format the old /wg flow used, so
    existing rankings/queries keep working unchanged."""
    now = datetime.now()
    data_raportu = now.strftime("%d.%m.%Y")
    conn = _connect()
    conn.execute(
        "INSERT INTO raporty (guild_id, swiat, data_raportu, data_wpisu) VALUES (?, ?, ?, ?)",
        (guild_id, swiat, data_raportu, now),
    )
    for nick in absent:
        conn.execute(
            "INSERT INTO nieobecnosci (guild_id, swiat, nick, data_raportu, data_wpisu) VALUES (?, ?, ?, ?, ?)",
            (guild_id, swiat, nick, data_raportu, now),
        )
    conn.commit()
    conn.close()


def _mark_posted(guild_id: str, swiat: str, rhash: str, opponent: str) -> None:
    conn = _connect()
    conn.execute(
        """INSERT OR IGNORE INTO absence_reports_seen
           (guild_id, swiat, report_hash, opponent, posted_at) VALUES (?, ?, ?, ?, ?)""",
        (guild_id, swiat, rhash, opponent, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def _already_posted(guild_id: str, swiat: str, rhash: str) -> bool:
    conn = _connect()
    row = conn.execute(
        "SELECT 1 FROM absence_reports_seen WHERE guild_id=? AND swiat=? AND report_hash=?",
        (guild_id, swiat, rhash),
    ).fetchone()
    conn.close()
    return row is not None


def _build_embed(swiat: str, opponent: str, absent: list[str]) -> discord.Embed:
    when = datetime.now().strftime("%Y-%m-%d %H:%M")
    if absent:
        desc = "\n".join(f"• {name}" for name in absent)
        colour = discord.Color.red()
    else:
        desc = "✅ Wszyscy zarejestrowani członkowie wzięli udział."
        colour = discord.Color.green()
    embed = discord.Embed(
        title=f"⚔️ Nieobecni w ataku gildii — {swiat.upper()}",
        description=desc, colour=colour,
    )
    embed.add_field(name="Przeciwnik", value=opponent or "—", inline=True)
    embed.add_field(name="Nieobecnych", value=str(len(absent)), inline=True)
    embed.set_footer(text=f"Auto-raport z przeglądarki • {when}")
    return embed


def make_app(bot: discord.Client) -> web.Application:
    app = web.Application()

    async def handle_ingest(request: web.Request) -> web.Response:
        # 1. Auth: reject anything without the exact shared token.
        if not INGEST_TOKEN or request.headers.get("X-Ingest-Token") != INGEST_TOKEN:
            return web.json_response({"error": "unauthorized"}, status=401)

        # 2. Parse body: expects JSON {guild_id, swiat, raw}.
        try:
            data = await request.json()
            guild_id = str(data["guild_id"])
            swiat = str(data["swiat"])
            raw = str(data["raw"])
        except Exception:
            return web.json_response({"error": "bad payload"}, status=400)

        # 3. Confirm it's actually a guild-attack report, then parse.
        section = extract_section(raw, "messagetext.s")
        if not section or section.split("/", 1)[0] != ATTACK_TYPE_CODE:
            return web.json_response({"error": "not an attack report"}, status=422)
        opponent, absent = parse_absent(section)

        # 4. Registered world?
        resolved = _resolve_world(guild_id, swiat)
        if not resolved:
            return web.json_response({"error": "world not registered / no channel"}, status=404)
        kanal_id, swiat = resolved

        # 5. Dedupe.
        rhash = _report_hash(opponent, absent)
        if _already_posted(guild_id, swiat, rhash):
            return web.json_response({"status": "duplicate, ignored"}, status=200)

        # 6. Persist + post.
        _write_absences(guild_id, swiat, absent)
        channel = bot.get_channel(int(kanal_id))
        if channel is None:
            return web.json_response({"error": "channel not found"}, status=500)
        await channel.send(embed=_build_embed(swiat, opponent, absent))
        _mark_posted(guild_id, swiat, rhash, opponent)

        return web.json_response(
            {"status": "ok", "opponent": opponent, "absent_count": len(absent)}, status=200
        )

    app.router.add_post("/ingest", handle_ingest)
    return app


async def start_receiver(bot: discord.Client) -> None:
    """Start the localhost aiohttp server. Call from setup_hook (after the
    event loop is running). Refuses to start if the token isn't configured."""
    if not INGEST_TOKEN:
        print("sf_absence_receiver: SF_INGEST_TOKEN not set — receiver DISABLED "
              "(refusing to run without auth). Set it in your .env to enable.")
        return
    app = make_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HOST, PORT)
    await site.start()
    print(f"sf_absence_receiver: listening on http://{HOST}:{PORT}/ingest (localhost only)")
