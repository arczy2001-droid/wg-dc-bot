import discord
from discord import app_commands
from discord.ext import commands, tasks
import os
import sqlite3
import re
from datetime import datetime, timedelta
from typing import Optional
import asyncio
from playwright.async_api import async_playwright
import difflib
import aiohttp
from urllib.parse import urlparse

from setup_wizard import (
    setup as setup_command,
    setup_reset as setup_reset_command,
    settings as settings_command,
    init_setup_table,
)
from i18n import translator
from command_i18n import CommandTranslator
from sf_events import (
    init_sf_events_tables,
    handle_event_webhook,
    sf_events_setup,
    sf_events_toggle,
    sf_events_debug,
    events as events_command,
)

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
OCR_API_KEY = os.getenv("OCR_SPACE_API_KEY")

#    BAZA DANYCH (każda tabela jest teraz scoped per-guild via guild_id)
#    Uwaga: tabela 'ustawienia' (kanal_glowy / kanal_logow) zostala zastapiona
#    przez 'guild_config' z setup_wizard.py — nie tworzymy jej tu juz dla nowych instalacji.
def init_db():
    conn = sqlite3.connect("gildia.db")
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS nieobecnosci (guild_id TEXT, swiat TEXT, nick TEXT, data_wpisu TIMESTAMP)")
    c.execute("CREATE TABLE IF NOT EXISTS czlonkowie (guild_id TEXT, swiat TEXT, nick TEXT, PRIMARY KEY (guild_id, swiat, nick))")
    c.execute("CREATE TABLE IF NOT EXISTS swiaty (guild_id TEXT, nazwa TEXT, kanal_id TEXT, PRIMARY KEY (guild_id, nazwa))")
    c.execute("CREATE TABLE IF NOT EXISTS raporty (guild_id TEXT, swiat TEXT, data_wpisu TIMESTAMP)")
    # Perceptual hash blocklist — shared across ALL guilds so if any server
    # sees a scam image first, every other server is immediately protected too.
    # hash_value: hex string of the 64-bit pHash
    # distance_threshold stored per-entry so you can tune per image if needed
    c.execute("""CREATE TABLE IF NOT EXISTS image_blocklist (
        hash_value   TEXT PRIMARY KEY,
        added_by     TEXT,
        added_at     TIMESTAMP,
        reason       TEXT
    )""")

    # weekday: Python's datetime.weekday() convention -> Monday=0 ... Sunday=6
    # One-time migration: earlier versions had ranking_schedule keyed by guild_id
    # only (one schedule for ALL worlds). If that old table exists, carry each
    # guild's schedule over to every world it currently has, then move to the
    # new per-world schema (guild_id + swiat).
    cols = [row[1] for row in c.execute("PRAGMA table_info(ranking_schedule)").fetchall()]
    if cols and "swiat" not in cols:
        old_rows = c.execute("SELECT guild_id, weekday, hour, minute, enabled FROM ranking_schedule").fetchall()
        c.execute("ALTER TABLE ranking_schedule RENAME TO ranking_schedule_old_guildlevel")
        c.execute("""CREATE TABLE ranking_schedule (
            guild_id TEXT NOT NULL,
            swiat TEXT NOT NULL,
            weekday INTEGER NOT NULL DEFAULT 6,
            hour INTEGER NOT NULL DEFAULT 21,
            minute INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (guild_id, swiat)
        )""")
        for guild_id, weekday, hour, minute, enabled in old_rows:
            worlds = c.execute("SELECT nazwa FROM swiaty WHERE guild_id=?", (guild_id,)).fetchall()
            for (world_name,) in worlds:
                c.execute(
                    "INSERT OR REPLACE INTO ranking_schedule VALUES (?, ?, ?, ?, ?, ?)",
                    (guild_id, world_name, weekday, hour, minute, enabled)
                )
        c.execute("DROP TABLE ranking_schedule_old_guildlevel")
    else:
        c.execute("""CREATE TABLE IF NOT EXISTS ranking_schedule (
            guild_id TEXT NOT NULL,
            swiat TEXT NOT NULL,
            weekday INTEGER NOT NULL DEFAULT 6,
            hour INTEGER NOT NULL DEFAULT 21,
            minute INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (guild_id, swiat)
        )""")
    conn.commit(); conn.close()

#    OCR
async def analizuj_screen(file_path):
    url = 'https://api.ocr.space/parse/image'
    try:
        with open(file_path, 'rb') as f:
            payload = {'apikey': OCR_API_KEY, 'language': 'eng', 'OCREngine': '2', 'scale': 'true', 'file': f}
            async with aiohttp.ClientSession() as session:
                async with session.post(url, data=payload) as resp:
                    res = await resp.json()
                    return res['ParsedResults'][0]['ParsedText'].splitlines()
    except Exception as e:
        print(f"Error during OCR analysis: {e}")
        return []

#    FISHFISH (sprawdzanie domen)
async def is_malicious_domain(domena, session):
    """Returns True only if FishFish has this domain catalogued as
    malware/phishing. NOTE: GET /domains/:domain returns HTTP 200 for ANY
    catalogued domain (including ones marked 'safe') and 404 only if the
    domain was never catalogued at all — so checking status code alone
    is not enough, we must read the 'category' field."""
    url_api = f"https://api.fishfish.gg/v1/domains/{domena}"
    try:
        async with session.get(url_api, timeout=aiohttp.ClientTimeout(total=5)) as response:
            if response.status == 200:
                data = await response.json()
                return data.get("category") in ("malware", "phishing")
            return False
    except Exception as e:
        print(f"Error checking domain {domena} on FishFish: {e}")
        return False

#    WYKRYWANIE SCAMÓW NA OBRAZKACH (np. fałszywe kasyna crypto / giveaway)
SCAM_KEYWORDS = [
    "withdrawal success", "activate code for bonus", "crypto casino",
    "deposit bonus", "rakeback", "claim your bonus", "airdrop claim",
    "free nitro", "discord nitro free", "steam gift", "giveaway winner",
    "selected as a winner", "claim now", "limited offer", "exclusive bonus",
    "trc20", "bep20", "erc20", "withdraw your winnings", "enter the promo code",
    "select a withdraw method", "select crypto to withdraw",
]

def policz_wskazniki_scamu(tekst: str) -> int:
    tekst = tekst.lower()
    return sum(1 for kw in SCAM_KEYWORDS if kw in tekst)


# ---------------------------------------------------------------------------
# PERCEPTUAL IMAGE HASHING
# Uses imagehash.phash() which produces a 64-bit fingerprint measuring
# *visual similarity* — two images that look the same to the human eye will
# have a Hamming distance <= HASH_THRESHOLD even if they've been resized,
# lightly compressed, or colour-tweaked by the spammer.
#
# Install on your VPS: pip install imagehash Pillow
# ---------------------------------------------------------------------------
HASH_THRESHOLD = 8  # max Hamming distance to count as "same image" (0-64).
                    # 8 is a good balance: catches re-saves/resizes, won't
                    # flag unrelated images. Lower = stricter, higher = looser.

def compute_phash(image_path: str) -> Optional[str]:
    """Returns the hex pHash string, or None if the file can't be read."""
    try:
        import imagehash
        from PIL import Image
        h = imagehash.phash(Image.open(image_path))
        return str(h)  # hex string, e.g. "f8e0c0c0e0f0f8f8"
    except Exception as e:
        print(f"pHash error on {image_path}: {e}")
        return None

def is_hash_blocked(phash_str: str) -> bool:
    """True if this image's perceptual hash is within HASH_THRESHOLD of any
    stored blocked hash. Iterates the full blocklist — fine at typical sizes
    (hundreds of entries); revisit with an index if it grows to tens of thousands."""
    try:
        import imagehash
        incoming = imagehash.hex_to_hash(phash_str)
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("SELECT hash_value FROM image_blocklist").fetchall()
        conn.close()
        for (stored_hex,) in rows:
            try:
                stored = imagehash.hex_to_hash(stored_hex)
                if (incoming - stored) <= HASH_THRESHOLD:
                    return True
            except Exception:
                continue
        return False
    except Exception as e:
        print(f"Hash blocklist check error: {e}")
        return False

def store_phash(phash_str: str, guild_id: int, reason: str = "auto-detected scam image") -> None:
    """Saves a new hash to the global blocklist. Safe to call even if the
    hash already exists (INSERT OR IGNORE)."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR IGNORE INTO image_blocklist VALUES (?, ?, ?, ?)",
        (phash_str, str(guild_id), datetime.now().isoformat(), reason)
    )
    conn.commit()
    conn.close()

async def wyslij_log(guild_id: str, tytul: str, kolor: discord.Color, autor: discord.abc.User, pola: list):
    """Wspólna funkcja do logowania (phishing / scam image / deleted message). Czyta logs_channel z guild_config."""
    conn = sqlite3.connect("gildia.db")
    res = conn.cursor().execute(
        "SELECT logs_channel FROM guild_config WHERE guild_id=?", (guild_id,)
    ).fetchone()
    conn.close()
    if not res or not res[0]:
        return
    try:
        kanal_logow = bot.get_channel(int(res[0]))
        if not kanal_logow:
            return
        embed = discord.Embed(title=tytul, color=kolor)
        embed.set_author(name=f"{autor.display_name} ({autor.id})", icon_url=autor.display_avatar.url)
        for nazwa, wartosc, inline in pola:
            embed.add_field(name=nazwa, value=wartosc, inline=inline)
        await kanal_logow.send(embed=embed)
    except Exception as e:
        print(f"Error sending log embed: {e}")

#    BOT
class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all())

    async def setup_hook(self):
        init_db()
        init_setup_table()
        init_sf_events_tables()
        self.tree.add_command(setup_command)
        self.tree.add_command(setup_reset_command)
        self.tree.add_command(settings_command)
        self.tree.add_command(sf_events_setup)
        self.tree.add_command(sf_events_toggle)
        self.tree.add_command(sf_events_debug)
        self.tree.add_command(events_command)
        await self.tree.set_translator(CommandTranslator())  # must be set before sync()
        self.czyszczenie.start()
        self.niedzielny_ranking.start()
        await self.tree.sync()

    @tasks.loop(hours=1)
    async def czyszczenie(self):
        conn = sqlite3.connect("gildia.db")
        granica = datetime.now() - timedelta(days=7, hours=1)
        conn.cursor().execute("DELETE FROM nieobecnosci WHERE data_wpisu <= ?", (granica,))
        conn.cursor().execute("DELETE FROM raporty WHERE data_wpisu <= ?", (granica,))
        conn.commit(); conn.close()

    @tasks.loop(minutes=1)
    async def niedzielny_ranking(self):
        now = datetime.now()
        conn = sqlite3.connect("gildia.db")

        # Only worlds with an explicit schedule row get checked here; worlds that
        # never had /wg_set_ranking run for them simply have automatic ranking
        # off by default (no surprise messages for a world that never configured this).
        due_worlds = conn.cursor().execute(
            "SELECT guild_id, swiat FROM ranking_schedule WHERE enabled=1 AND weekday=? AND hour=? AND minute=?",
            (now.weekday(), now.hour, now.minute)
        ).fetchall()

        for guild_id, swiat in due_worlds:
            kanal_row = conn.cursor().execute(
                "SELECT kanal_id FROM swiaty WHERE guild_id=? AND nazwa=?", (guild_id, swiat)
            ).fetchone()
            if not kanal_row:
                continue  # world was deleted after its schedule was set; nothing to post to

            kanal_id = kanal_row[0]
            liczba_raportow = conn.cursor().execute(
                "SELECT COUNT(*) FROM raporty WHERE guild_id=? AND swiat=?", (guild_id, swiat.lower())
            ).fetchone()[0]
            res = conn.cursor().execute(
                "SELECT nick, COUNT(*) FROM nieobecnosci WHERE guild_id=? AND swiat=? GROUP BY nick ORDER BY COUNT(*) DESC",
                (guild_id, swiat.lower())
            ).fetchall()
            txt = "\n".join([f"{r[0]}: {r[1]}x" for r in res]) if res else translator.get_text(int(guild_id), "wg.no_absences")

            naglowek = f"📊 **Top absent players from {swiat.upper()} based on ({liczba_raportow}) raports:**"
            try:
                target_chan = self.get_channel(int(kanal_id)) or await self.fetch_channel(int(kanal_id))
                if target_chan:
                    await target_chan.send(f"{naglowek}\n```\n{txt}\n```")
            except Exception as e:
                print(f"Error sending the global ranking automatically {swiat} ({guild_id}): {e}")
        conn.close()

bot = MyBot()

#    Globalny handler błędów komend (np. brak uprawnień)
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "❌ You need the **Manage Server** permission to use this command."
    elif isinstance(error, app_commands.CheckFailure):
        msg = "❌ You don't have permission to use this command here."
    else:
        print(f"Unhandled app command error: {error}")
        msg = "❌ Something went wrong while running that command."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass

#    Sprawdzenie głównego kanału (scoped per guild, ustawione przez /setup)
async def sprawdz_pozwolenie(interaction: discord.Interaction) -> bool:
    if not interaction.guild_id:
        await interaction.response.send_message("❌ This command can only be used inside a server.", ephemeral=True)
        return False
    conn = sqlite3.connect("gildia.db")
    res = conn.cursor().execute(
        "SELECT main_channel FROM guild_config WHERE guild_id=?",
        (str(interaction.guild_id),)
    ).fetchone()
    conn.close()
    if res and res[0]:
        kanal_id = int(res[0])
        if interaction.channel_id != kanal_id:
            await interaction.response.send_message(f"❌ Commands can only be entered on the main channel for commands: <#{kanal_id}>.", ephemeral=True)
            return False
    return True

#    Komendy
@bot.tree.command(name="wg_add_world", description="Add world and assign a channel")
@app_commands.checks.has_permissions(manage_guild=True)
async def wg_add_world(interaction: discord.Interaction, nazwa: str, kanal: discord.TextChannel):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    conn.cursor().execute(
        "INSERT OR REPLACE INTO swiaty VALUES (?, ?, ?)",
        (str(interaction.guild_id), nazwa.lower(), str(kanal.id))
    )
    conn.commit(); conn.close()
    await interaction.response.send_message(
        translator.get_text(interaction.guild_id, "worlds.added", world=nazwa, channel=kanal.mention)
    )

@bot.tree.command(name="wg_delete_world", description="Deleting world")
@app_commands.checks.has_permissions(manage_guild=True)
async def wg_delete_world(interaction: discord.Interaction, nazwa: str):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    gid = str(interaction.guild_id)
    cur = conn.cursor()
    cur.execute("DELETE FROM swiaty WHERE guild_id=? AND nazwa=?", (gid, nazwa.lower()))
    # Cascade: clear out related members/reports/absences for this world too, so they don't linger orphaned.
    cur.execute("DELETE FROM czlonkowie WHERE guild_id=? AND swiat=?", (gid, nazwa.lower()))
    cur.execute("DELETE FROM raporty WHERE guild_id=? AND swiat=?", (gid, nazwa.lower()))
    cur.execute("DELETE FROM nieobecnosci WHERE guild_id=? AND swiat=?", (gid, nazwa.lower()))
    conn.commit(); conn.close()
    await interaction.response.send_message(
        translator.get_text(interaction.guild_id, "worlds.deleted", world=nazwa)
    )

@bot.tree.command(name="wg_worlds", description="List of the worlds")
async def wg_worlds(interaction: discord.Interaction):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    res = conn.cursor().execute(
        "SELECT nazwa, kanal_id FROM swiaty WHERE guild_id=?", (str(interaction.guild_id),)
    ).fetchall()
    conn.close()
    list_empty_text = translator.get_text(interaction.guild_id, "worlds.list_empty")
    txt = "\n".join([f"{r[0]} -> <#{r[1]}>" for r in res]) if res else list_empty_text
    header = translator.get_text(interaction.guild_id, "worlds.list_header")
    await interaction.response.send_message(f"{header}\n{txt}")

@bot.tree.command(name="wg_add_member", description="Assign players to a world")
@app_commands.checks.has_permissions(manage_guild=True)
async def wg_add_member(interaction: discord.Interaction, swiat: str, lista: str):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    gid = str(interaction.guild_id)
    for n in [x.strip() for x in re.split(r'[\n,]+', lista) if x.strip()]:
        conn.cursor().execute("INSERT OR IGNORE INTO czlonkowie VALUES (?, ?, ?)", (gid, swiat.lower(), n))
    conn.commit(); conn.close()
    await interaction.response.send_message(translator.get_text(interaction.guild_id, "members.added"))

@bot.tree.command(name="wg_delete_member", description="Deleting player")
@app_commands.checks.has_permissions(manage_guild=True)
async def wg_delete_member(interaction: discord.Interaction, swiat: str, nick: str):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    conn.cursor().execute(
        "DELETE FROM czlonkowie WHERE guild_id=? AND swiat=? AND nick=?",
        (str(interaction.guild_id), swiat.lower(), nick)
    )
    conn.commit(); conn.close()
    await interaction.response.send_message(
        translator.get_text(interaction.guild_id, "members.deleted", nick=nick)
    )

@bot.tree.command(name="wg_member_list", description="Member list:")
async def wg_member_list(interaction: discord.Interaction, swiat: str):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute(
        "SELECT nick FROM czlonkowie WHERE guild_id=? AND swiat=? ORDER BY nick ASC",
        (str(interaction.guild_id), swiat.lower())
    )
    res = [r[0] for r in cursor.fetchall()]
    conn.close()

    if not res:
        await interaction.response.send_message(translator.get_text(interaction.guild_id, "members.list_empty")); return

    size = (len(res) + 2) // 3
    c1 = res[0:size]
    c2 = res[size:size*2]
    c3 = res[size*2:]

    embed = discord.Embed(
        title=translator.get_text(interaction.guild_id, "members.list_title", world=swiat.upper()),
        color=discord.Color.blue()
    )
    embed.add_field(name="I", value="\n".join(c1) or "-", inline=True)
    embed.add_field(name="II", value="\n".join(c2) or "-", inline=True)
    embed.add_field(name="III", value="\n".join(c3) or "-", inline=True)
    embed.set_footer(text=translator.get_text(interaction.guild_id, "members.list_footer", count=len(res)))
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="wg", description="Upload the activity report")
async def wg(interaction: discord.Interaction, swiat: str, screen: discord.Attachment):
    if not await sprawdz_pozwolenie(interaction): return
    await interaction.response.defer()
    conn = sqlite3.connect("gildia.db")
    gid = str(interaction.guild_id)
    swiat_data = conn.cursor().execute(
        "SELECT kanal_id FROM swiaty WHERE guild_id=? AND nazwa=?", (gid, swiat.lower())
    ).fetchone()
    if not swiat_data:
        conn.close()
        await interaction.followup.send(translator.get_text(interaction.guild_id, "wg.unknown_world"))
        return

    # Sanitize filename so we never write outside the working dir
    safe_name = re.sub(r'[^A-Za-z0-9._-]', '_', os.path.basename(screen.filename))
    path = f"temp_{interaction.id}_{safe_name}"
    await screen.save(path)
    sklad = [r[0] for r in conn.cursor().execute(
        "SELECT nick FROM czlonkowie WHERE guild_id=? AND swiat=?", (gid, swiat.lower())
    ).fetchall()]
    lines = await analizuj_screen(path)

    nieobecni = []
    for l in lines:
        cleaned = re.sub(r'[^a-zA-Z0-9 ]', '', re.sub(r'\(.*?\)', '', l)).strip()
        match = difflib.get_close_matches(cleaned, sklad, n=1, cutoff=0.5)
        if match:
            nick = match[0]
            if nick not in nieobecni:
                nieobecni.append(nick)

    teraz = datetime.now()
    conn.cursor().execute("INSERT INTO raporty VALUES (?, ?, ?)", (gid, swiat.lower(), teraz))

    for n in nieobecni:
        conn.cursor().execute("INSERT INTO nieobecnosci VALUES (?, ?, ?, ?)", (gid, swiat.lower(), n, teraz))

    conn.commit(); conn.close()
    if os.path.exists(path): os.remove(path)

    opis_nieobecnych = ', '.join(nieobecni) if nieobecni else translator.get_text(interaction.guild_id, "wg.full_attendance")

    try:
        target_chan = bot.get_channel(int(swiat_data[0])) or await bot.fetch_channel(int(swiat_data[0]))
        await target_chan.send(
            translator.get_text(interaction.guild_id, "wg.inactive_players", world=swiat.upper(), players=opis_nieobecnych)
        )
        await interaction.followup.send(
            translator.get_text(interaction.guild_id, "wg.report_sent", channel=target_chan.mention)
        )
    except Exception as e:
        print(f"Error sending the report to the world channel: {e}")
        await interaction.followup.send("✅ Report processed, but I couldn't notify the world channel (check its permissions).")

@bot.tree.command(name="wg_absent_list", description="List of absences")
async def wg_absent_list(interaction: discord.Interaction, swiat: str):
    if not await sprawdz_pozwolenie(interaction): return
    await interaction.response.defer()

    conn = sqlite3.connect("gildia.db")
    gid = str(interaction.guild_id)
    liczba_raportow = conn.cursor().execute(
        "SELECT COUNT(*) FROM raporty WHERE guild_id=? AND swiat=?", (gid, swiat.lower())
    ).fetchone()[0]
    res = conn.cursor().execute(
        "SELECT nick, COUNT(*) FROM nieobecnosci WHERE guild_id=? AND swiat=? GROUP BY nick ORDER BY COUNT(*) DESC",
        (gid, swiat.lower())
    ).fetchall()
    conn.close()

    txt = "\n".join([f"{r[0]}: {r[1]}x" for r in res]) if res else translator.get_text(interaction.guild_id, "wg.no_absences")
    naglowek = translator.get_text(
        interaction.guild_id, "wg.absent_list_header", world=swiat.upper(), report_count=liczba_raportow
    )

    await interaction.followup.send(f"{naglowek}\n{txt}")

@bot.tree.command(name="wg_delete_raport", description="Deleting last assigned report")
@app_commands.checks.has_permissions(manage_guild=True)
async def wg_delete_raport(interaction: discord.Interaction, swiat: str):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    gid = str(interaction.guild_id)
    res = conn.cursor().execute(
        "SELECT MAX(data_wpisu) FROM raporty WHERE guild_id=? AND swiat=?", (gid, swiat.lower())
    ).fetchone()

    if res and res[0]:
        ostatnia_data = res[0]
        conn.cursor().execute(
            "DELETE FROM nieobecnosci WHERE guild_id=? AND swiat=? AND data_wpisu=?",
            (gid, swiat.lower(), ostatnia_data)
        )
        conn.cursor().execute(
            "DELETE FROM raporty WHERE guild_id=? AND swiat=? AND data_wpisu=?",
            (gid, swiat.lower(), ostatnia_data)
        )
        conn.commit()
        await interaction.response.send_message(translator.get_text(interaction.guild_id, "reports.withdrawn"))
    else:
        await interaction.response.send_message(translator.get_text(interaction.guild_id, "reports.none_found"))
    conn.close()

@bot.tree.command(name="wg_add_absent", description="Add single absence to a member")
@app_commands.checks.has_permissions(manage_guild=True)
async def wg_add_absent(interaction: discord.Interaction, swiat: str, nick: str):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    conn.cursor().execute(
        "INSERT INTO nieobecnosci VALUES (?, ?, ?, ?)",
        (str(interaction.guild_id), swiat.lower(), nick, datetime.now())
    )
    conn.commit(); conn.close()
    await interaction.response.send_message(
        translator.get_text(interaction.guild_id, "reports.absence_added", nick=nick)
    )

@bot.tree.command(name="wg_delete_absent", description="Delete single absence for a member")
@app_commands.checks.has_permissions(manage_guild=True)
async def wg_delete_absent(interaction: discord.Interaction, swiat: str, nick: str):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    conn.cursor().execute(
        """DELETE FROM nieobecnosci WHERE rowid IN (
               SELECT rowid FROM nieobecnosci WHERE guild_id=? AND swiat=? AND nick=?
               ORDER BY data_wpisu DESC LIMIT 1
           )""",
        (str(interaction.guild_id), swiat.lower(), nick)
    )
    conn.commit(); conn.close()
    await interaction.response.send_message(
        translator.get_text(interaction.guild_id, "reports.absence_deleted", nick=nick)
    )

@bot.tree.command(name="wg_clear_all", description="Clearing every absensce report (this server only)")
@app_commands.checks.has_permissions(manage_guild=True)
async def wg_clear_all(interaction: discord.Interaction):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    gid = str(interaction.guild_id)
    conn.cursor().execute("DELETE FROM nieobecnosci WHERE guild_id=?", (gid,))
    conn.cursor().execute("DELETE FROM raporty WHERE guild_id=?", (gid,))
    conn.commit(); conn.close()
    await interaction.response.send_message(translator.get_text(interaction.guild_id, "reports.all_cleared"))

WEEKDAY_CHOICES = [
    app_commands.Choice(name="Monday", value=0),
    app_commands.Choice(name="Tuesday", value=1),
    app_commands.Choice(name="Wednesday", value=2),
    app_commands.Choice(name="Thursday", value=3),
    app_commands.Choice(name="Friday", value=4),
    app_commands.Choice(name="Saturday", value=5),
    app_commands.Choice(name="Sunday", value=6),
]
WEEKDAY_NAMES = {c.value: c.name for c in WEEKDAY_CHOICES}

@bot.tree.command(name="wg_set_ranking", description="Configure or disable the automatic weekly absence ranking for one world")
@app_commands.choices(weekday=WEEKDAY_CHOICES)
@app_commands.describe(
    swiat="World this schedule applies to",
    enabled="Turn the automatic weekly ranking on or off for this world",
    weekday="Day of the week to post it (only used if enabled)",
    hour="Hour, 24h format (only used if enabled)",
    minute="Minute (only used if enabled)",
)
async def wg_set_ranking(
    interaction: discord.Interaction,
    swiat: str,
    enabled: bool,
    weekday: Optional[app_commands.Choice[int]] = None,
    hour: Optional[app_commands.Range[int, 0, 23]] = None,
    minute: Optional[app_commands.Range[int, 0, 59]] = None,
):
    if not await sprawdz_pozwolenie(interaction): return
    gid = str(interaction.guild_id)
    swiat_lower = swiat.lower()

    conn = sqlite3.connect("gildia.db")
    world_exists = conn.execute("SELECT 1 FROM swiaty WHERE guild_id=? AND nazwa=?", (gid, swiat_lower)).fetchone()
    if not world_exists:
        conn.close()
        await interaction.response.send_message(translator.get_text(interaction.guild_id, "wg.unknown_world"))
        return

    if enabled and (weekday is None or hour is None or minute is None):
        conn.close()
        await interaction.response.send_message(
            "❌ To enable automatic ranking you must provide `weekday`, `hour`, and `minute`.",
            ephemeral=True,
        )
        return

    if enabled:
        conn.execute(
            """INSERT OR REPLACE INTO ranking_schedule (guild_id, swiat, weekday, hour, minute, enabled)
               VALUES (?, ?, ?, ?, ?, 1)""",
            (gid, swiat_lower, weekday.value, hour, minute)
        )
        conn.commit(); conn.close()
        await interaction.response.send_message(
            f"✅ Automatic ranking enabled for **{swiat.upper()}** — every **{weekday.name}** "
            f"at **{hour:02d}:{minute:02d}** (server time)."
        )
    else:
        # Keep any existing weekday/hour/minute on record (so re-enabling later
        # restores the previous schedule) — just flip the flag off.
        existing = conn.execute(
            "SELECT 1 FROM ranking_schedule WHERE guild_id=? AND swiat=?", (gid, swiat_lower)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE ranking_schedule SET enabled=0 WHERE guild_id=? AND swiat=?", (gid, swiat_lower)
            )
        else:
            conn.execute(
                "INSERT INTO ranking_schedule (guild_id, swiat, weekday, hour, minute, enabled) VALUES (?, ?, 6, 21, 0, 0)",
                (gid, swiat_lower)
            )
        conn.commit(); conn.close()
        await interaction.response.send_message(f"🔕 Automatic weekly ranking disabled for **{swiat.upper()}**.")

@bot.tree.command(name="wg_ranking_status", description="Show the current automatic ranking schedule")
@app_commands.describe(swiat="Show only this world's schedule (omit to list every world)")
async def wg_ranking_status(interaction: discord.Interaction, swiat: Optional[str] = None):
    if not await sprawdz_pozwolenie(interaction): return
    gid = str(interaction.guild_id)
    conn = sqlite3.connect("gildia.db")

    if swiat:
        swiat_lower = swiat.lower()
        world_exists = conn.execute("SELECT 1 FROM swiaty WHERE guild_id=? AND nazwa=?", (gid, swiat_lower)).fetchone()
        if not world_exists:
            conn.close()
            await interaction.response.send_message(translator.get_text(interaction.guild_id, "wg.unknown_world"))
            return

        row = conn.execute(
            "SELECT weekday, hour, minute, enabled FROM ranking_schedule WHERE guild_id=? AND swiat=?",
            (gid, swiat_lower)
        ).fetchone()
        conn.close()

        if not row or not row[3]:
            await interaction.response.send_message(
                f"🔕 Automatic weekly ranking is currently **disabled** for **{swiat.upper()}**. "
                "Use `/wg_set_ranking` to turn it on."
            )
            return

        weekday, hour, minute, _ = row
        await interaction.response.send_message(
            f"📅 Automatic ranking for **{swiat.upper()}** is **enabled** — every "
            f"**{WEEKDAY_NAMES.get(weekday, weekday)}** at **{hour:02d}:{minute:02d}** (server time)."
        )
    else:
        rows = conn.execute(
            "SELECT swiat, weekday, hour, minute, enabled FROM ranking_schedule WHERE guild_id=?", (gid,)
        ).fetchall()
        conn.close()

        active = [r for r in rows if r[4]]
        if not active:
            await interaction.response.send_message(
                "🔕 Automatic weekly ranking is currently **disabled** for every world on this server. "
                "Use `/wg_set_ranking` to turn it on for a specific world."
            )
            return

        lines = [
            f"**{world.upper()}**: every {WEEKDAY_NAMES.get(weekday, weekday)} at {hour:02d}:{minute:02d}"
            for world, weekday, hour, minute, _ in active
        ]
        await interaction.response.send_message("📅 Automatic ranking schedule:\n" + "\n".join(lines))

@bot.tree.command(name="wg_block_image", description="Manually add an image to the scam blocklist")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    image="The image to block",
    reason="Why this image is being blocked (optional)"
)
async def wg_block_image(interaction: discord.Interaction, image: discord.Attachment, reason: Optional[str] = "manually blocked by moderator"):
    if not await sprawdz_pozwolenie(interaction): return
    await interaction.response.defer(ephemeral=True)

    safe_name = re.sub(r'[^A-Za-z0-9._-]', '_', os.path.basename(image.filename))
    tmp_path = f"block_{interaction.id}_{safe_name}"
    try:
        await image.save(tmp_path)
        phash_str = compute_phash(tmp_path)
        if not phash_str:
            await interaction.followup.send("❌ Could not compute hash for this image — is it a valid image file?", ephemeral=True)
            return

        already = is_hash_blocked(phash_str)
        store_phash(phash_str, interaction.guild_id, reason)

        if already:
            await interaction.followup.send(f"ℹ️ This image (or a visually similar one) was already in the blocklist.", ephemeral=True)
        else:
            await interaction.followup.send(f"✅ Image added to the global scam blocklist.\n`hash: {phash_str}`\nReason: {reason}", ephemeral=True)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

@bot.tree.command(name="wg_blocklist_stats", description="Show how many images are in the scam hash blocklist")
async def wg_blocklist_stats(interaction: discord.Interaction):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect(DB_PATH)
    total = conn.execute("SELECT COUNT(*) FROM image_blocklist").fetchone()[0]
    recent = conn.execute(
        "SELECT hash_value, added_by, added_at, reason FROM image_blocklist ORDER BY added_at DESC LIMIT 5"
    ).fetchall()
    conn.close()

    lines = [f"🛡️ **Scam image blocklist: {total} entries**\n\n**Last 5 added:**"]
    for h, guild, ts, rsn in recent:
        lines.append(f"`{h[:16]}...` — {rsn} (guild {guild}, {ts[:10]})")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

@bot.tree.command(name="test_scrapera", description="Test połączenia bota z SFDataHub")
async def test_scrapera(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            url = "https://sfdatahub.com/#/toplists"
            await page.goto(url)
            await page.wait_for_timeout(4000)
            tytul = await page.title()
            surowy_tekst = await page.locator("body").inner_text()
            podglad_tekstu = surowy_tekst[:600]

            await browser.close()

            odpowiedz = (
                f"✅ **Połączenie nawiązane!**\n\n"
                f"**Tytuł karty:** `{tytul}`\n"
                f"**Co bot widzi na stronie (fragment):**\n"
                f"```text\n{podglad_tekstu}\n```"
            )
            await interaction.followup.send(odpowiedz)

    except Exception as e:
        await interaction.followup.send(f"❌ **Wystąpił błąd podczas skanowania:**\n`{e}`")

# ---------------------------------------------------------------------------
# ANTI-RAID: per-guild, per-user channel tracker
# Tracks how many DISTINCT channels a user has posted in within a rolling
# time window. Hitting the threshold means they're mass-spamming channels —
# the bot kicks them and deletes every tracked message instantly, before OCR
# or FishFish even runs (which is why the spammer slipped through last time:
# those checks are async/rate-limited, this one is pure in-memory, O(1)).
# ---------------------------------------------------------------------------
from collections import defaultdict

# Structure: {guild_id: {user_id: [(channel_id, message, timestamp), ...]}}
_spam_tracker: dict = defaultdict(lambda: defaultdict(list))

RAID_CHANNEL_THRESHOLD = 5    # distinct channels within the window
RAID_WINDOW_SECONDS    = 30   # rolling window


async def _check_raid(message: discord.Message) -> bool:
    """Returns True if this message triggered a raid action (caller should return immediately)."""
    guild_id = message.guild.id
    user_id  = message.author.id
    now      = message.created_at.timestamp()
    window_start = now - RAID_WINDOW_SECONDS

    record = _spam_tracker[guild_id][user_id]

    # Prune entries older than the window
    record[:] = [(ch, msg, ts) for ch, msg, ts in record if ts >= window_start]

    # Add current message
    record.append((message.channel.id, message, now))

    # Count distinct channels in the window
    distinct_channels = len({ch for ch, _, _ in record})
    if distinct_channels < RAID_CHANNEL_THRESHOLD:
        return False

    # --- RAID DETECTED ---
    # 1. Delete every tracked message across all channels immediately
    for _, tracked_msg, _ in record:
        try:
            await tracked_msg.delete()
        except (discord.NotFound, discord.Forbidden):
            pass

    # 2. Clear tracker for this user so we don't double-process
    _spam_tracker[guild_id].pop(user_id, None)

    # 3. Kick the user (requires Kick Members permission on the bot's role)
    try:
        await message.guild.kick(
            message.author,
            reason=f"Anti-raid: posted in {distinct_channels} channels within {RAID_WINDOW_SECONDS}s"
        )
    except discord.Forbidden:
        print(f"Anti-raid: couldn't kick {message.author} — missing Kick Members permission")

    # 4. Log to the guild's log channel
    await wyslij_log(
        str(guild_id),
        "🚨 Raid/spam account kicked",
        discord.Color.dark_red(),
        message.author,
        [
            ("Reason", f"Posted in **{distinct_channels}** distinct channels within {RAID_WINDOW_SECONDS}s", False),
            ("Action", "All messages deleted, user kicked from server", False),
        ]
    )

    return True


@bot.event
async def on_message(message: discord.Message):
    # --- WEBHOOK CHECK MUST BE FIRST — before ANY bot filter ---
    # Discord's "Follow Channel" feature delivers official news via webhooks,
    # which are flagged as bots (message.author.bot = True). If we let the
    # bot-filter run first, every event announcement is silently dropped.
    # handle_event_webhook() returns True only for relevant webhook messages
    # in the configured channel; everything else falls through normally.
    if message.guild and await handle_event_webhook(message):
        return

    if message.author.bot:
        return
    if not message.guild:
        return  # DMs have no per-guild settings to look up

    # --- 0) Anti-raid check FIRST — pure in-memory, no API rate limits ---
    # This catches mass multi-channel spammers even before OCR or FishFish
    # run, which is exactly the gap that let some messages through last time.
    if await _check_raid(message):
        await bot.process_commands(message)
        return

    # --- 1) Sprawdzanie linków przeciw bazie FishFish (malware/phishing) ---
    znalezione_linki = re.findall(r'(https?://[^\s]+)', message.content)
    if znalezione_linki:
        async with aiohttp.ClientSession() as session:
            for link in znalezione_linki:
                domena = urlparse(link).netloc
                if not domena:
                    continue

                if await is_malicious_domain(domena, session):
                    try:
                        await message.delete()
                    except discord.NotFound:
                        pass

                    await message.channel.send(
                        translator.get_text(message.guild.id, "security.phishing_blocked", mention=message.author.mention),
                        delete_after=10
                    )
                    await wyslij_log(
                        str(message.guild.id),
                        "🚨 A phishing attempt has been blocked",
                        discord.Color.red(),
                        message.author,
                        [
                            ("Kanał", message.channel.mention, True),
                            ("Domena", f"`{domena}`", True),
                            ("Treść", message.content[:1000] if message.content else "*[No text]*", False),
                        ]
                    )
                    await bot.process_commands(message)
                    return  # message already deleted, nothing more to scan

    # --- 2) Image scanning: hash check first (free, instant), then OCR fallback ---
    for att in message.attachments:
        if not (att.content_type and att.content_type.startswith("image/")):
            continue

        safe_name = re.sub(r'[^A-Za-z0-9._-]', '_', os.path.basename(att.filename))
        tmp_path = f"scamcheck_{message.id}_{safe_name}"
        try:
            await att.save(tmp_path)

            # Step A: perceptual hash check — zero API cost, runs in milliseconds.
            # If this image (or a visually similar one) was already caught on any
            # server, block it instantly without touching OCR.space.
            phash_str = compute_phash(tmp_path)
            hash_hit = phash_str and is_hash_blocked(phash_str)

            # Step B: OCR keyword scan — only runs if the hash didn't match,
            # so every confirmed scam image reduces future OCR usage.
            ocr_hit = False
            if not hash_hit:
                linie = await analizuj_screen(tmp_path)
                tekst = " ".join(linie)
                if policz_wskazniki_scamu(tekst) >= 2:
                    ocr_hit = True
                    # Store the hash so this image (and visually similar ones)
                    # are caught instantly on every server from now on.
                    if phash_str:
                        store_phash(phash_str, message.guild.id, "auto-detected by OCR keyword scan")

            if hash_hit or ocr_hit:
                detection_method = "hash blocklist" if hash_hit else "OCR keyword scan"
                try:
                    await message.delete()
                except discord.NotFound:
                    pass

                await message.channel.send(
                    translator.get_text(message.guild.id, "security.scam_image_blocked", mention=message.author.mention),
                    delete_after=10
                )
                await wyslij_log(
                    str(message.guild.id),
                    "🚨 A scam image has been blocked",
                    discord.Color.red(),
                    message.author,
                    [
                        ("Kanał", message.channel.mention, True),
                        ("Plik", att.filename, True),
                        ("Wykryto przez", detection_method, True),
                    ]
                )
                await bot.process_commands(message)
                return
        except Exception as e:
            print(f"Error scanning attachment for scam content: {e}")
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    await bot.process_commands(message)

#    Kanał logów usuniętych wiadomości
@bot.event
async def on_message_delete(message: discord.Message):
    if message.author.bot:
        return
    if not message.guild:
        return

    tresc = message.content if message.content else "*[No text]*"
    await wyslij_log(
        str(message.guild.id),
        "🗑️ Deleted message",
        discord.Color.orange(),
        message.author,
        [
            ("Kanał", message.channel.mention, True),
            ("Treść", tresc[:1000], False),
        ]
    )

@bot.event
async def on_ready():
    print("Bot gotowy!")

bot.run(TOKEN)
