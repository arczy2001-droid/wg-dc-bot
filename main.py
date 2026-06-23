import discord
from discord.ext import commands
import os
import pytesseract
import sqlite3
import re
from datetime import datetime
from PIL import Image, ImageEnhance
import asyncio
import difflib

TOKEN = os.getenv("DISCORD_BOT_TOKEN")

def init_db():
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS nieobecnosci (swiat TEXT, nick TEXT, data_wpisu TIMESTAMP)")
    cursor.execute("CREATE TABLE IF NOT EXISTS ustawienia (klucz TEXT PRIMARY KEY, wartosc TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS swiaty (nazwa TEXT PRIMARY KEY, kanal_id INTEGER)")
    cursor.execute("CREATE TABLE IF NOT EXISTS czlonkowie (swiat TEXT, nick TEXT, PRIMARY KEY(swiat, nick))")
    conn.commit()
    conn.close()

# --- FUNKCJE BAZODANOWE ---
def ustaw_dowodzenie_db(kanal_id):
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO ustawienia (klucz, wartosc) VALUES ('kanal_dowodzenia', ?)", (str(kanal_id),))
    conn.commit()
    conn.close()

def pobierz_dowodzenie_db():
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("SELECT wartosc FROM ustawienia WHERE klucz = 'kanal_dowodzenia'")
    row = cursor.fetchone()
    conn.close()
    return int(row[0]) if row else None

def dodaj_swiat_db(nazwa, kanal_id):
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO swiaty (nazwa, kanal_id) VALUES (?, ?)", (nazwa.lower(), kanal_id))
    conn.commit()
    conn.close()

def usun_swiat_db(nazwa):
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM swiaty WHERE nazwa = ?", (nazwa.lower(),))
    conn.commit()
    conn.close()

def pobierz_swiaty_db():
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("SELECT nazwa, kanal_id FROM swiaty")
    rows = cursor.fetchall()
    conn.close()
    return {row[0]: row[1] for row in rows}

# --- FILTR NICKÓW ---
def dopasuj_nick(ocr_tekst, lista_czlonkow):
    ocr_low = ocr_tekst.lower().strip()
    if not ocr_low or not lista_czlonkow: return None
    for czl in lista_czlonkow:
        if czl.lower() == ocr_low: return czl
    posortowani = sorted(lista_czlonkow, key=len, reverse=True)
    for czl in posortowani:
        if czl.lower() in ocr_low: return czl
    pasujace = difflib.get_close_matches(ocr_low, [c.lower() for c in lista_czlonkow], n=1, cutoff=0.55)
    if pasujace:
        for czl in lista_czlonkow:
            if czl.lower() == pasujace[0]: return czl
    return None

# --- SILNIK OCR ---
async def analizuj_screen_async(image_path):
    def _ocr():
        with Image.open(image_path) as img:
            img = img.resize((1600, int(img.height * (1600 / img.width))), Image.Resampling.LANCZOS)
            img = ImageEnhance.Contrast(img.convert("L")).enhance(2.5)
            text = pytesseract.image_to_string(img, lang="pol+eng", config="--psm 6")
            return text.splitlines()
    return await asyncio.get_running_loop().run_in_executor(None, _ocr)

# --- KONFIGURACJA BOTA ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

def tylko_na_kanale_dowodzenia():
    async def predicate(ctx):
        if ctx.channel.id == pobierz_dowodzenie_db(): return True
        await ctx.send("❌ Komenda tylko na kanale dowodzenia!")
        return False
    return commands.check(predicate)

@bot.event
async def on_message(message):
    if message.author.bot: return
    if message.content.strip() in ["!", "!wg"]:
        help_text = "### 🤖 Komendy WG-BOT:\n`!wg_root`, `!wg_add_world`, `!wg_delete_world`, `!wg_worlds`, `!wg_add_member`, `!wg_delete_member`, `!wg_member_list`, `!wg`, `!wg_absent_list`, `!wg_del_raport`, `!wg_add_absent`, `!wg_del_absent`"
        await message.channel.send(help_text)
        return
    await bot.
