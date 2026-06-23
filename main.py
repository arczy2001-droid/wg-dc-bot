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
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS nieobecnosci (swiat TEXT, nick TEXT, data_wpisu TIMESTAMP)")
    cursor.execute("CREATE TABLE IF NOT EXISTS ustawienia (klucz TEXT PRIMARY KEY, wartosc TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS czlonkowie (swiat TEXT, nick TEXT, PRIMARY KEY(swiat, nick))")
    conn.commit(); conn.close()

def get_ustawienie(klucz):
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("SELECT wartosc FROM ustawienia WHERE klucz = ?", (klucz,))
    res = cursor.fetchone()
    conn.close()
    return int(res[0]) if res else None

# --- STRAŻNIK KANAŁU ---
async def sprawdz_pozwolenie(interaction: discord.Interaction) -> bool:
    kanal = get_ustawienie('kanal_glowy')
    if kanal and interaction.channel_id != kanal:
        await interaction.response.send_message(f"❌ Użyj kanału <#{kanal}>.", ephemeral=True)
        return False
    return True

# --- LOGIKA ---
async def analizuj_screen_async(file_path):
    url = 'https://api.ocr.space/parse/image'
    try:
        with open(file_path, 'rb') as f:
            payload = {'apikey': OCR_API_KEY, 'language': 'eng', 'OCREngine': '2', 'scale': 'true', 'file': f}
            async with aiohttp.ClientSession() as session:
                async with session.post(url, data=payload) as resp:
                    result = await resp.json()
                    return result['ParsedResults'][0]['ParsedText'].splitlines()
    except: return []

def dopasuj_nick(ocr_nick, sklad):
    clean = re.sub(r'[^a-zA-Z0-9 ]', '', re.sub(r'\(.*?\)', '', ocr_nick)).strip()
    if len(clean) < 3: return None
    matches = difflib.get_close_matches(clean, sklad, n=1, cutoff=0.5)
    return matches[0] if matches else None

# --- BOT ---
class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all())
    async def setup_hook(self):
        self.pętla_czyszczenia.start()
        await self.tree.sync()

    @tasks.loop(hours=1)
    async def pętla_czyszczenia(self):
        conn = sqlite3.connect("gildia.db")
        conn.cursor().execute("DELETE FROM nieobecnosci WHERE data_wpisu <= ?", (datetime.now() - timedelta(days=7, hours=1),))
        conn.commit(); conn.close()

bot = MyBot()

# --- KOMENDY ---
@bot.tree.command(name="wg_root", description="[ADMIN] Ustaw kanał dla komend i raportów")
async def wg_root(interaction: discord.Interaction):
    conn = sqlite3.connect("gildia.db")
    conn.cursor().execute("INSERT OR REPLACE INTO ustawienia VALUES ('kanal_glowy', ?)", (str(interaction.channel_id),))
    conn.commit(); conn.close()
    await interaction.response.send_message(f"✅ Kanał <#{interaction.channel_id}> jest teraz centrum operacyjnym.")

@bot.tree.command(name="wg", description="Przetwórz raport")
async def wg(interaction: discord.Interaction, swiat: str, screen: discord.Attachment):
    if not await sprawdz_pozwolenie(interaction): return
    await interaction.response.defer()
    path = f"temp_{screen.filename}"
    await screen.save(path)
    conn = sqlite3.connect("gildia.db")
    sklad = [r[0] for r in conn.cursor().execute("SELECT nick FROM czlonkowie WHERE swiat = ?", (swiat.lower(),)).fetchall()]
    lines = await analizuj_screen_async(path)
    nieobecni = []
    w_sekcji = False
    for line in lines:
        if "Niezarejestrowani" in line: w_sekcji = True; continue
        if "Zarejestrowani" in line: break
        if w_sekcji:
            n = dopasuj_nick(line, sklad)
            if n and n not in nieobecni:
                nieobecni.append(n)
                conn.cursor().execute("INSERT INTO nieobecnosci VALUES (?, ?, ?)", (swiat.lower(), n, datetime.now()))
    conn.commit(); conn.close()
    if os.path.exists(path): os.remove(path)
    msg = f"🚨 Nieobecni ({swiat.upper()}): {', '.join(nieobecni)}" if nieobecni else f"✅ Raport {swiat.upper()} - brak nieobecnych."
    await interaction.followup.send(msg)

@bot.tree.command(name="wg_add_member", description="Dodaj graczy")
async def wg_add_member(interaction: discord.Interaction, swiat: str, nick: str):
    if not await sprawdz_pozwolenie(interaction): return
    lista = [n.strip() for n in re.split(r'[\n,]+', nick) if n.strip()]
    conn = sqlite3.connect("gildia.db")
    for n in lista: conn.cursor().execute("INSERT OR IGNORE INTO czlonkowie VALUES (?, ?)", (swiat.lower(), n))
    conn.commit(); conn.close()
    await interaction.response.send_message(f"✅ Dodano/Zaktualizowano świat {swiat.upper()}.")

@bot.tree.command(name="wg_delete_member", description="Usuń gracza")
async def wg_delete_member(interaction: discord.Interaction, swiat: str, nick: str):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    conn.cursor().execute("DELETE FROM czlonkowie WHERE swiat = ? AND nick = ?", (swiat.lower(), nick))
    conn.commit(); conn.close()
    await interaction.response.send_message(f"🗑️ Usunięto {nick}.")

@bot.tree.command(name="wg_absent_list", description="Ranking nieobecności")
async def wg_absent_list(interaction: discord.Interaction, swiat: str):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    res = conn.cursor().execute("SELECT nick, COUNT(*) FROM nieobecnosci WHERE swiat = ? GROUP BY nick ORDER BY COUNT(*) DESC", (swiat.lower(),)).fetchall()
    conn.close()
    txt = "\n".join([f"{r[0]}: {r[1]}x" for r in res]) if res else "Brak danych."
    await interaction.response.send_message(f"📊 Ranking:\n```\n{txt}\n```")

@bot.tree.command(name="wg_clear_all", description="[ADMIN] Czyści wszystkie nieobecności")
async def wg_clear_all(interaction: discord.Interaction):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    conn.cursor().execute("DELETE FROM nieobecnosci")
    conn.commit(); conn.close()
    await interaction.response.send_message("💥 Baza nieobecności wyczyszczona.")

@bot.event
async def on_ready():
    init_db()
    print("Bot gotowy!")

bot.run(TOKEN)
