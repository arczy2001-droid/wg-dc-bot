import discord
from discord import app_commands
from discord.ext import commands, tasks
import os
import sqlite3
import re
from datetime import datetime, timedelta
import asyncio
import difflib
import aiohttp

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
OCR_API_KEY = os.getenv("OCR_SPACE_API_KEY")

# --- BAZA DANYCH ---
def init_db():
    conn = sqlite3.connect("gildia.db")
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS nieobecnosci (swiat TEXT, nick TEXT, data_wpisu TIMESTAMP)")
    c.execute("CREATE TABLE IF NOT EXISTS ustawienia (klucz TEXT PRIMARY KEY, wartosc TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS czlonkowie (swiat TEXT, nick TEXT, PRIMARY KEY(swiat, nick))")
    c.execute("CREATE TABLE IF NOT EXISTS swiaty (nazwa TEXT PRIMARY KEY, kanal_id TEXT)")
    conn.commit(); conn.close()

# --- OCR ---
async def analizuj_screen(file_path):
    url = 'https://api.ocr.space/parse/image'
    try:
        with open(file_path, 'rb') as f:
            payload = {'apikey': OCR_API_KEY, 'language': 'eng', 'OCREngine': '2', 'scale': 'true', 'file': f}
            async with aiohttp.ClientSession() as session:
                async with session.post(url, data=payload) as resp:
                    res = await resp.json()
                    return res['ParsedResults'][0]['ParsedText'].splitlines()
    except: return []

# --- BOT ---
class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all())
    async def setup_hook(self):
        self.czyszczenie.start()
        await self.tree.sync()

    @tasks.loop(hours=1)
    async def czyszczenie(self):
        conn = sqlite3.connect("gildia.db")
        conn.cursor().execute("DELETE FROM nieobecnosci WHERE data_wpisu <= ?", (datetime.now() - timedelta(days=7, hours=1),))
        conn.commit(); conn.close()

bot = MyBot()

# --- KOMENDY ---
@bot.tree.command(name="wg_root", description="Konfiguruje kanał główny")
async def wg_root(interaction: discord.Interaction):
    conn = sqlite3.connect("gildia.db")
    conn.cursor().execute("INSERT OR REPLACE INTO ustawienia VALUES ('kanal_glowy', ?)", (str(interaction.channel_id),))
    conn.commit(); conn.close()
    await interaction.response.send_message("✅ Kanał główny ustawiony.")

@bot.tree.command(name="wg_add_world", description="Dodaje świat i przypisuje kanał")
async def wg_add_world(interaction: discord.Interaction, nazwa: str, kanal: discord.TextChannel):
    conn = sqlite3.connect("gildia.db")
    conn.cursor().execute("INSERT OR REPLACE INTO swiaty VALUES (?, ?)", (nazwa.lower(), str(kanal.id)))
    conn.commit(); conn.close()
    await interaction.response.send_message(f"✅ Dodano świat {nazwa} z kanałem <#{kanal.id}>.")

@bot.tree.command(name="wg_delete_world", description="Usuwa świat")
async def wg_delete_world(interaction: discord.Interaction, nazwa: str):
    conn = sqlite3.connect("gildia.db")
    conn.cursor().execute("DELETE FROM swiaty WHERE nazwa = ?", (nazwa.lower(),))
    conn.commit(); conn.close()
    await interaction.response.send_message(f"🗑️ Usunięto świat {nazwa}.")

@bot.tree.command(name="wg_worlds", description="Wyświetla listę światów")
async def wg_worlds(interaction: discord.Interaction):
    conn = sqlite3.connect("gildia.db")
    res = conn.cursor().execute("SELECT nazwa, kanal_id FROM swiaty").fetchall()
    conn.close()
    txt = "\n".join([f"{r[0]} -> <#{r[1]}>" for r in res]) if res else "Brak światów."
    await interaction.response.send_message(f"🌍 Światy:\n{txt}")

@bot.tree.command(name="wg_add_member", description="Dodaje graczy do świata")
async def wg_add_member(interaction: discord.Interaction, swiat: str, lista: str):
    conn = sqlite3.connect("gildia.db")
    for n in [x.strip() for x in re.split(r'[\n,]+', lista)]:
        conn.cursor().execute("INSERT OR IGNORE INTO czlonkowie VALUES (?, ?)", (swiat.lower(), n))
    conn.commit(); conn.close()
    await interaction.response.send_message("✅ Gracze dodani.")

@bot.tree.command(name="wg_delete_member", description="Usuwa gracza")
async def wg_delete_member(interaction: discord.Interaction, swiat: str, nick: str):
    conn = sqlite3.connect("gildia.db")
    conn.cursor().execute("DELETE FROM czlonkowie WHERE swiat=? AND nick=?", (swiat.lower(), nick))
    conn.commit(); conn.close()
    await interaction.response.send_message(f"🗑️ Usunięto {nick}.")

@bot.tree.command(name="wg_member_list", description="Lista członków świata")
async def wg_member_list(interaction: discord.Interaction, swiat: str):
    conn = sqlite3.connect("gildia.db")
    res = [r[0] for r in conn.cursor().execute("SELECT nick FROM czlonkowie WHERE swiat=?", (swiat.lower(),)).fetchall()]
    conn.close()
    await interaction.response.send_message(f"📜 Skład ({swiat}):\n{', '.join(res)}")

@bot.tree.command(name="wg", description="Analizuje raport")
async def wg(interaction: discord.Interaction, swiat: str, screen: discord.Attachment):
    await interaction.response.defer()
    conn = sqlite3.connect("gildia.db")
    swiat_data = conn.cursor().execute("SELECT kanal_id FROM swiaty WHERE nazwa=?", (swiat.lower(),)).fetchone()
    if not swiat_data: await interaction.followup.send("❌ Nieznany świat."); return
    
    path = f"temp_{screen.filename}"
    await screen.save(path)
    sklad = [r[0] for r in conn.cursor().execute("SELECT nick FROM czlonkowie WHERE swiat=?", (swiat.lower(),)).fetchall()]
    lines = await analizuj_screen(path)
    nieobecni = [n for n in [difflib.get_close_matches(re.sub(r'[^a-zA-Z0-9 ]', '', re.sub(r'\(.*?\)', '', l)).strip(), sklad, n=1, cutoff=0.5) for l in lines] if n]
    
    nieobecni = [n[0] for n in nieobecni]
    for n in nieobecni: conn.cursor().execute("INSERT INTO nieobecnosci VALUES (?, ?, ?)", (swiat.lower(), n, datetime.now()))
    conn.commit(); conn.close()
    if os.path.exists(path): os.remove(path)
    
    target_chan = bot.get_channel(int(swiat_data[0]))
    await target_chan.send(f"🚨 Nieobecni ({swiat}): {', '.join(set(nieobecni))}")
    await interaction.followup.send("✅ Raport przetworzony.")

@bot.tree.command(name="wg_absent_list", description="Ranking nieobecności")
async def wg_absent_list(interaction: discord.Interaction, swiat: str):
    conn = sqlite3.connect("gildia.db")
    res = conn.cursor().execute("SELECT nick, COUNT(*) FROM nieobecnosci WHERE swiat=? GROUP BY nick ORDER BY COUNT(*) DESC", (swiat.lower(),)).fetchall()
    conn.close()
    txt = "\n".join([f"{r[0]}: {r[1]}x" for r in res])
    await interaction.response.send_message(f"📊 Ranking:\n{txt}")

@bot.tree.command(name="wg_delete_raport", description="Usuwa ostatni raport świata")
async def wg_delete_raport(interaction: discord.Interaction, swiat: str):
    conn = sqlite3.connect("gildia.db")
    data = conn.cursor().execute("SELECT MAX(data_wpisu) FROM nieobecnosci WHERE swiat=?", (swiat.lower(),)).fetchone()[0]
    conn.cursor().execute("DELETE FROM nieobecnosci WHERE data_wpisu=?", (data,))
    conn.commit(); conn.close()
    await interaction.response.send_message("⏪ Cofnięto ostatni raport.")

@bot.tree.command(name="wg_add_absent", description="Dodaj punkt nieobecności")
async def wg_add_absent(interaction: discord.Interaction, swiat: str, nick: str):
    conn = sqlite3.connect("gildia.db")
    conn.cursor().execute("INSERT INTO nieobecnosci VALUES (?, ?, ?)", (swiat.lower(), nick, datetime.now()))
    conn.commit(); conn.close()
    await interaction.response.send_message(f"➕ Dodano nieobecność dla {nick}.")

@bot.tree.command(name="wg_delete_absent", description="Usuń punkt nieobecności")
async def wg_delete_absent(interaction: discord.Interaction, swiat: str, nick: str):
    conn = sqlite3.connect("gildia.db")
    conn.cursor().execute("DELETE FROM nieobecnosci WHERE rowid IN (SELECT rowid FROM nieobecnosci WHERE swiat=? AND nick=? ORDER BY data_wpisu DESC LIMIT 1)", (swiat.lower(), nick))
    conn.commit(); conn.close()
    await interaction.response.send_message(f"➖ Usunięto nieobecność {nick}.")

@bot.tree.command(name="wg_clear_all", description="Czyści wszystko")
async def wg_clear_all(interaction: discord.Interaction):
    conn = sqlite3.connect("gildia.db")
    conn.cursor().execute("DELETE FROM nieobecnosci")
    conn.commit(); conn.close()
    await interaction.response.send_message("💥 Baza nieobecności wyczyszczona.")

bot.run(TOKEN)
