import discord
from discord import app_commands
from discord.ext import commands, tasks
import os
import sqlite3
import re
from datetime import datetime, timedelta
import asyncio
from playwright.async_api import async_playwright
import difflib
import aiohttp
from urllib.parse import urlparse

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
OCR_API_KEY = os.getenv("OCR_SPACE_API_KEY")

#    BAZA DANYCH
def init_db():
    conn = sqlite3.connect("gildia.db")
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS nieobecnosci (swiat TEXT, nick TEXT, data_wpisu TIMESTAMP)")
    c.execute("CREATE TABLE IF NOT EXISTS ustawienia (klucz TEXT PRIMARY KEY, wartosc TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS czlonkowie (swiat TEXT, nick TEXT, PRIMARY KEY(swiat, nick))")
    c.execute("CREATE TABLE IF NOT EXISTS swiaty (nazwa TEXT PRIMARY KEY, kanal_id TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS raporty (swiat TEXT, data_wpisu TIMESTAMP)")
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

#    BOT
class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all())

    async def setup_hook(self):
        init_db()
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
        if now.weekday() == 6 and now.hour == 21 and now.minute == 0:
            conn = sqlite3.connect("gildia.db")
            swiaty = conn.cursor().execute("SELECT nazwa, kanal_id FROM swiaty").fetchall()
            for swiat, kanal_id in swiaty:
                liczba_raportow = conn.cursor().execute("SELECT COUNT(*) FROM raporty WHERE swiat=?", (swiat.lower(),)).fetchone()[0]
                res = conn.cursor().execute("SELECT nick, COUNT(*) FROM nieobecnosci WHERE swiat=? GROUP BY nick ORDER BY COUNT(*) DESC", (swiat.lower(),)).fetchall()
                txt = "\n".join([f"{r[0]}: {r[1]}x" for r in res]) if res else "No absences have been recorded."

                naglowek = f"📊 **Top absent players from {swiat.upper()} based on ({liczba_raportow}) raports:**"
                try:
                    target_chan = self.get_channel(int(kanal_id)) or await self.fetch_channel(int(kanal_id))
                    if target_chan:
                        await target_chan.send(f"{naglowek}\n```\n{txt}\n```")
                except Exception as e:
                    print(f"Error sending the global ranking automatically {swiat}: {e}")
            conn.close()

bot = MyBot()

#    Sprawdzenie głównego kanału
async def sprawdz_pozwolenie(interaction: discord.Interaction) -> bool:
    conn = sqlite3.connect("gildia.db")
    res = conn.cursor().execute("SELECT wartosc FROM ustawienia WHERE klucz = 'kanal_glowy'").fetchone()
    conn.close()
    if res:
        kanal_id = int(res[0])
        if interaction.channel_id != kanal_id:
            await interaction.response.send_message(f"❌ Commands can only be entered on the main channel for commands: <#{kanal_id}>.", ephemeral=True)
            return False
    return True

#    Komendy
@bot.tree.command(name="wg_root", description="Choose channel for commands only")
async def wg_root(interaction: discord.Interaction):
    conn = sqlite3.connect("gildia.db")
    conn.cursor().execute("INSERT OR REPLACE INTO ustawienia VALUES ('kanal_glowy', ?)", (str(interaction.channel_id),))
    conn.commit(); conn.close()
    await interaction.response.send_message("✅ Main channel set.")

@bot.tree.command(name="wg_set_logs", description="Sets the channel for log notifications (antiphising and deleted messages)")
async def wg_set_logs(interaction: discord.Interaction, kanal: discord.TextChannel):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    conn.cursor().execute("INSERT OR REPLACE INTO ustawienia VALUES ('kanal_logow', ?)", (str(kanal.id),))
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"✅ From now on, logs and notifications about blocked viruses will be sent to: <#{kanal.id}>.")

@bot.tree.command(name="wg_add_world", description="Add world and assign a channel")
async def wg_add_world(interaction: discord.Interaction, nazwa: str, kanal: discord.TextChannel):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    conn.cursor().execute("INSERT OR REPLACE INTO swiaty VALUES (?, ?)", (nazwa.lower(), str(kanal.id)))
    conn.commit(); conn.close()
    await interaction.response.send_message(f"✅ World added successfully {nazwa} with assigned channel <#{kanal.id}>.")

@bot.tree.command(name="wg_delete_world", description="Deleting world")
async def wg_delete_world(interaction: discord.Interaction, nazwa: str):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    conn.cursor().execute("DELETE FROM swiaty WHERE nazwa = ?", (nazwa.lower(),))
    conn.commit(); conn.close()
    await interaction.response.send_message(f"🗑️ Deleted world {nazwa}. Do you feel like Thanos, the destroyer of worlds? ")

@bot.tree.command(name="wg_worlds", description="List of the worlds")
async def wg_worlds(interaction: discord.Interaction):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    res = conn.cursor().execute("SELECT nazwa, kanal_id FROM swiaty").fetchall()
    conn.close()
    txt = "\n".join([f"{r[0]} -> <#{r[1]}>" for r in res]) if res else "There is no added worlds."
    await interaction.response.send_message(f"🌍 Worlds:\n{txt}")

@bot.tree.command(name="wg_add_member", description="Assign players to a world")
async def wg_add_member(interaction: discord.Interaction, swiat: str, lista: str):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    for n in [x.strip() for x in re.split(r'[\n,]+', lista) if x.strip()]:
        conn.cursor().execute("INSERT OR IGNORE INTO czlonkowie VALUES (?, ?)", (swiat.lower(), n))
    conn.commit(); conn.close()
    await interaction.response.send_message("✅ The number of lambs of God has increased.")

@bot.tree.command(name="wg_delete_member", description="Deleting player")
async def wg_delete_member(interaction: discord.Interaction, swiat: str, nick: str):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    conn.cursor().execute("DELETE FROM czlonkowie WHERE swiat=? AND nick=?", (swiat.lower(), nick))
    conn.commit(); conn.close()
    await interaction.response.send_message(f"🗑️ {nick} has been kicked out of the guild.")

@bot.tree.command(name="wg_member_list", description="Member list:")
async def wg_member_list(interaction: discord.Interaction, swiat: str):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("SELECT nick FROM czlonkowie WHERE swiat = ? ORDER BY nick ASC", (swiat.lower(),))
    res = [r[0] for r in cursor.fetchall()]
    conn.close()

    if not res:
        await interaction.response.send_message("👻 Member list is empty."); return

    size = (len(res) + 2) // 3
    c1 = res[0:size]
    c2 = res[size:size*2]
    c3 = res[size*2:]

    embed = discord.Embed(title=f"📜 Member list: {swiat.upper()}", color=discord.Color.blue())
    embed.add_field(name="I", value="\n".join(c1) or "-", inline=True)
    embed.add_field(name="II", value="\n".join(c2) or "-", inline=True)
    embed.add_field(name="III", value="\n".join(c3) or "-", inline=True)
    embed.set_footer(text=f"Total number of members: {len(res)}")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="wg", description="Upload the activity report")
async def wg(interaction: discord.Interaction, swiat: str, screen: discord.Attachment):
    if not await sprawdz_pozwolenie(interaction): return
    await interaction.response.defer()
    conn = sqlite3.connect("gildia.db")
    swiat_data = conn.cursor().execute("SELECT kanal_id FROM swiaty WHERE nazwa=?", (swiat.lower(),)).fetchone()
    if not swiat_data:
        conn.close()
        await interaction.followup.send("❌ Unknow world.")
        return

    # Sanitize filename so we never write outside the working dir
    safe_name = re.sub(r'[^A-Za-z0-9._-]', '_', os.path.basename(screen.filename))
    path = f"temp_{interaction.id}_{safe_name}"
    await screen.save(path)
    sklad = [r[0] for r in conn.cursor().execute("SELECT nick FROM czlonkowie WHERE swiat=?", (swiat.lower(),)).fetchall()]
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
    conn.cursor().execute("INSERT INTO raporty VALUES (?, ?)", (swiat.lower(), teraz))

    for n in nieobecni:
        conn.cursor().execute("INSERT INTO nieobecnosci VALUES (?, ?, ?)", (swiat.lower(), n, teraz))

    conn.commit(); conn.close()
    if os.path.exists(path): os.remove(path)

    opis_nieobecnych = ', '.join(nieobecni) if nieobecni else "No one — full attendance! 🎉"

    try:
        target_chan = bot.get_channel(int(swiat_data[0])) or await bot.fetch_channel(int(swiat_data[0]))
        await target_chan.send(f"🚨 Inactive during the last battle ({swiat.upper()}): {opis_nieobecnych}")
        await interaction.followup.send(f"✅ Report has been sent on {target_chan.mention}.")
    except Exception as e:
        print(f"Error sending the report to the world channel: {e}")
        await interaction.followup.send("✅ Report processed, but I couldn't notify the world channel (check its permissions).")

@bot.tree.command(name="wg_absent_list", description="List of absences")
async def wg_absent_list(interaction: discord.Interaction, swiat: str):
    if not await sprawdz_pozwolenie(interaction): return
    await interaction.response.defer()

    conn = sqlite3.connect("gildia.db")
    liczba_raportow = conn.cursor().execute("SELECT COUNT(*) FROM raporty WHERE swiat=?", (swiat.lower(),)).fetchone()[0]
    res = conn.cursor().execute("SELECT nick, COUNT(*) FROM nieobecnosci WHERE swiat=? GROUP BY nick ORDER BY COUNT(*) DESC", (swiat.lower(),)).fetchall()
    conn.close()

    txt = "\n".join([f"{r[0]}: {r[1]}x" for r in res]) if res else "None of inactive players."
    naglowek = f"📊 **Top absences on {swiat.upper()} world, based on {liczba_raportow} report numbers:**"

    await interaction.followup.send(f"{naglowek}\n{txt}")

@bot.tree.command(name="wg_delete_raport", description="Deleting last assigned report")
async def wg_delete_raport(interaction: discord.Interaction, swiat: str):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    res = conn.cursor().execute("SELECT MAX(data_wpisu) FROM raporty WHERE swiat=?", (swiat.lower(),)).fetchone()

    if res and res[0]:
        ostatnia_data = res[0]
        conn.cursor().execute("DELETE FROM nieobecnosci WHERE swiat=? AND data_wpisu=?", (swiat.lower(), ostatnia_data))
        conn.cursor().execute("DELETE FROM raporty WHERE swiat=? AND data_wpisu=?", (swiat.lower(), ostatnia_data))
        conn.commit()
        await interaction.response.send_message("⏪ The latest report has been withdrawn.")
    else:
        await interaction.response.send_message("❌ No reports were found for this world.")
    conn.close()

@bot.tree.command(name="wg_add_absent", description="Add single absence to a member")
async def wg_add_absent(interaction: discord.Interaction, swiat: str, nick: str):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    conn.cursor().execute("INSERT INTO nieobecnosci VALUES (?, ?, ?)", (swiat.lower(), nick, datetime.now()))
    conn.commit(); conn.close()
    await interaction.response.send_message(f"➕ Added absence for {nick}.")

@bot.tree.command(name="wg_delete_absent", description="Delete single absence for a member")
async def wg_delete_absent(interaction: discord.Interaction, swiat: str, nick: str):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    conn.cursor().execute("DELETE FROM nieobecnosci WHERE rowid IN (SELECT rowid FROM nieobecnosci WHERE swiat=? AND nick=? ORDER BY data_wpisu DESC LIMIT 1)", (swiat.lower(), nick))
    conn.commit(); conn.close()
    await interaction.response.send_message(f"➖ Deleted absence from {nick} member.")

@bot.tree.command(name="wg_clear_all", description="Clearing every absensce report")
async def wg_clear_all(interaction: discord.Interaction):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    conn.cursor().execute("DELETE FROM nieobecnosci")
    conn.cursor().execute("DELETE FROM raporty")
    conn.commit(); conn.close()
    await interaction.response.send_message("💥 The absence database and the report counter have been cleared.")

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

    # Logika sprawdzania linków
    znalezione_linki = re.findall(r'(https?://[^\s]+)', message.content)
    for link in znalezione_linki:
        domena = urlparse(link).netloc
        if not domena:
            continue

        try:
            async with aiohttp.ClientSession() as session:
                url_api = f"https://api.fishfish.gg/v1/domains/{domena}"
                async with session.get(url_api) as response:
                    if response.status == 200:
                        try:
                            await message.delete()
                        except discord.NotFound:
                            pass

                        await message.channel.send(f"🛡️ **A dangerous link has been blocked!** {message.author.mention} tried to send a SCAM.", delete_after=10)

                        conn = sqlite3.connect("gildia.db")
                        res = conn.cursor().execute("SELECT wartosc FROM ustawienia WHERE klucz = 'kanal_logow'").fetchone()
                        conn.close()

                        if res:
                            kanal_logow = bot.get_channel(int(res[0]))
                            if kanal_logow:
                                embed = discord.Embed(title="🚨 A phishing attempt has been blocked", color=discord.Color.red())
                                embed.set_author(name=f"{message.author.display_name} ({message.author.id})", icon_url=message.author.display_avatar.url)
                                embed.add_field(name="Kanał", value=message.channel.mention, inline=True)
                                embed.add_field(name="Domena", value=f"`{domena}`", inline=True)
                                embed.add_field(
                                    name="Treść",
                                    value=(message.content[:1000] if message.content else "*[No text]*"),
                                    inline=False
                                )
                                await kanal_logow.send(embed=embed)
                        break  
        except Exception as e:
            print(f"Error checking link {link}: {e}")

    await bot.process_commands(message)

#    Kanał logów usuniętych wiadomości
@bot.event
async def on_message_delete(message: discord.Message):
    if message.author.bot:
        return

    conn = sqlite3.connect("gildia.db")
    res = conn.cursor().execute("SELECT wartosc FROM ustawienia WHERE klucz = 'kanal_logow'").fetchone()
    conn.close()

    if res:
        try:
            kanal_logow = bot.get_channel(int(res[0]))
            if kanal_logow:
                embed = discord.Embed(title="🗑️ Deleted message", color=discord.Color.orange())
                embed.set_author(name=f"{message.author.display_name} ({message.author.id})", icon_url=message.author.display_avatar.url)
                embed.add_field(name="Kanał", value=message.channel.mention, inline=True)
                tresc = message.content if message.content else "*[No text]*"
                embed.add_field(name="Treść", value=tresc[:1000], inline=False)
                await kanal_logow.send(embed=embed)
        except Exception as e:
            print(f"Error logging deleted message: {e}")

@bot.event
async def on_ready():
    print("Bot gotowy!")

bot.run(TOKEN)
