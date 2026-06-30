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
    # weekday: Python's datetime.weekday() convention -> Monday=0 ... Sunday=6
    c.execute("""CREATE TABLE IF NOT EXISTS ranking_schedule (
        guild_id TEXT PRIMARY KEY,
        weekday INTEGER NOT NULL DEFAULT 6,
        hour INTEGER NOT NULL DEFAULT 21,
        minute INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 1
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
        self.tree.add_command(setup_command)
        self.tree.add_command(setup_reset_command)
        self.tree.add_command(settings_command)
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

        # Only guilds with an explicit schedule row get checked here; guilds that
        # never ran /wg_set_ranking simply have automatic ranking off by default
        # (no surprise messages in a server that never configured this).
        due_guilds = conn.cursor().execute(
            "SELECT guild_id FROM ranking_schedule WHERE enabled=1 AND weekday=? AND hour=? AND minute=?",
            (now.weekday(), now.hour, now.minute)
        ).fetchall()

        for (guild_id,) in due_guilds:
            swiaty = conn.cursor().execute(
                "SELECT nazwa, kanal_id FROM swiaty WHERE guild_id=?", (guild_id,)
            ).fetchall()
            for swiat, kanal_id in swiaty:
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

@bot.tree.command(name="wg_set_ranking", description="Configure or disable the automatic weekly absence ranking")
@app_commands.choices(weekday=WEEKDAY_CHOICES)
@app_commands.describe(
    enabled="Turn the automatic weekly ranking on or off",
    weekday="Day of the week to post it (only used if enabled)",
    hour="Hour, 24h format (only used if enabled)",
    minute="Minute (only used if enabled)",
)
async def wg_set_ranking(
    interaction: discord.Interaction,
    enabled: bool,
    weekday: Optional[app_commands.Choice[int]] = None,
    hour: Optional[app_commands.Range[int, 0, 23]] = None,
    minute: Optional[app_commands.Range[int, 0, 59]] = None,
):
    if not await sprawdz_pozwolenie(interaction): return
    gid = str(interaction.guild_id)

    if enabled and (weekday is None or hour is None or minute is None):
        await interaction.response.send_message(
            "❌ To enable automatic ranking you must provide `weekday`, `hour`, and `minute`.",
            ephemeral=True,
        )
        return

    conn = sqlite3.connect("gildia.db")
    if enabled:
        conn.execute(
            """INSERT OR REPLACE INTO ranking_schedule (guild_id, weekday, hour, minute, enabled)
               VALUES (?, ?, ?, ?, 1)""",
            (gid, weekday.value, hour, minute)
        )
        conn.commit(); conn.close()
        await interaction.response.send_message(
            f"✅ Automatic ranking enabled — every **{weekday.name}** at **{hour:02d}:{minute:02d}** (server time)."
        )
    else:
        # Keep any existing weekday/hour/minute on record (so re-enabling later
        # restores the previous schedule) — just flip the flag off.
        existing = conn.execute("SELECT 1 FROM ranking_schedule WHERE guild_id=?", (gid,)).fetchone()
        if existing:
            conn.execute("UPDATE ranking_schedule SET enabled=0 WHERE guild_id=?", (gid,))
        else:
            conn.execute(
                "INSERT INTO ranking_schedule (guild_id, weekday, hour, minute, enabled) VALUES (?, 6, 21, 0, 0)",
                (gid,)
            )
        conn.commit(); conn.close()
        await interaction.response.send_message("🔕 Automatic weekly ranking disabled.")

@bot.tree.command(name="wg_ranking_status", description="Show the current automatic ranking schedule")
async def wg_ranking_status(interaction: discord.Interaction):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    row = conn.execute(
        "SELECT weekday, hour, minute, enabled FROM ranking_schedule WHERE guild_id=?",
        (str(interaction.guild_id),)
    ).fetchone()
    conn.close()

    if not row or not row[3]:
        await interaction.response.send_message(
            "🔕 Automatic weekly ranking is currently **disabled** for this server. "
            "Use `/wg_set_ranking` to turn it on."
        )
        return

    weekday, hour, minute, _ = row
    await interaction.response.send_message(
        f"📅 Automatic ranking is **enabled** — every **{WEEKDAY_NAMES.get(weekday, weekday)}** "
        f"at **{hour:02d}:{minute:02d}** (server time)."
    )

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

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if not message.guild:
        return  # DMs have no per-guild settings to look up

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

    # --- 2) Sprawdzanie obrazków pod kątem scamów (fałszywe kasyna/giveawaye) ---
    for att in message.attachments:
        if not (att.content_type and att.content_type.startswith("image/")):
            continue

        safe_name = re.sub(r'[^A-Za-z0-9._-]', '_', os.path.basename(att.filename))
        tmp_path = f"scamcheck_{message.id}_{safe_name}"
        try:
            await att.save(tmp_path)
            linie = await analizuj_screen(tmp_path)
            tekst = " ".join(linie)
            if policz_wskazniki_scamu(tekst) >= 2:
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
                        ("Wykryty tekst (OCR)", tekst[:1000] if tekst else "*[No text detected]*", False),
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
