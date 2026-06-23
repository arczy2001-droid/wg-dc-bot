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
    await bot.process_commands(message)

# --- KOMENDY ---

@bot.command(name="wg_root")
@commands.has_permissions(administrator=True)
async def cmd_root(ctx):
    ustaw_dowodzenie_db(ctx.channel.id)
    await ctx.send("🎯 Ustawiono centrum dowodzenia.")

@bot.command(name="wg_add_world")
@commands.has_permissions(administrator=True)
async def cmd_add_world(ctx, nazwa: str, kanal: discord.TextChannel):
    dodaj_swiat_db(nazwa, kanal.id)
    await ctx.send(f"🌍 Świat {nazwa.upper()} przypisany.")

@bot.command(name="wg_delete_world")
@commands.has_permissions(administrator=True)
async def cmd_del_world(ctx, nazwa: str):
    usun_swiat_db(nazwa)
    await ctx.send(f"🗑️ Usunięto świat {nazwa.upper()}.")

@bot.command(name="wg_worlds")
async def cmd_worlds(ctx):
    swiaty = pobierz_swiaty_db()
    await ctx.send("📋 " + ", ".join([n.upper() for n in swiaty]))

@bot.command(name="wg_add_member")
@tylko_na_kanale_dowodzenia()
async def cmd_add_member(ctx, nazwa_swiata: str, *, argumenty: str):
    nazwa_swiata = nazwa_swiata.lower()
    nicki = [n.strip() for n in argumenty.replace("\n", ",").split(",") if n.strip()]
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    for n in nicki: cursor.execute("INSERT OR IGNORE INTO czlonkowie (swiat, nick) VALUES (?, ?)", (nazwa_swiata, n))
    conn.commit()
    conn.close()
    await ctx.send(f"✅ Dodano {len(nicki)} graczy do {nazwa_swiata.upper()}.")

@bot.command(name="wg_delete_member")
@tylko_na_kanale_dowodzenia()
async def cmd_delete_member(ctx, nazwa_swiata: str, *, nick: str):
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM czlonkowie WHERE swiat = ? AND LOWER(nick) = LOWER(?)", (nazwa_swiata.lower(), nick.strip()))
    conn.commit()
    conn.close()
    await ctx.send(f"🗑️ Usunięto {nick}.")

@bot.command(name="wg_member_list")
@tylko_na_kanale_dowodzenia()
async def cmd_member_list(ctx, nazwa_swiata: str):
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("SELECT nick FROM czlonkowie WHERE swiat = ?", (nazwa_swiata.lower(),))
    wyniki = [r[0] for r in cursor.fetchall()]
    conn.close()
    await ctx.send(f"📜 Skład {nazwa_swiata.upper()}:\n```\n{', '.join(wyniki)}\n```")

@bot.command(name="wg")
@tylko_na_kanale_dowodzenia()
async def cmd_raport(ctx, nazwa_swiata: str):
    if not ctx.message.attachments: return
    att = ctx.message.attachments[0]
    file_path = f"temp_{att.filename}"
    await att.save(file_path)
    
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("SELECT nick FROM czlonkowie WHERE swiat = ?", (nazwa_swiata.lower(),))
    sklad = [r[0] for r in cursor.fetchall()]
    
    lines = await analizuj_screen_async(file_path)
    nieobecni = []
    w_sekcji = False
    for line in lines:
        if "Niezarejestrowani" in line: w_sekcji = True
        if "Zarejestrowani" in line or "USUŃ" in line: break
        if w_sekcji:
            nick = dopasuj_nick(re.split(r"\(", line)[0], sklad)
            if nick and nick not in nieobecni: nieobecni.append(nick)
    
    if nieobecni:
        for n in nieobecni: cursor.execute("INSERT INTO nieobecnosci (swiat, nick, data_wpisu) VALUES (?, ?, ?)", (nazwa_swiata.lower(), n, datetime.now()))
        conn.commit()
        await ctx.send(f"🚨 Raport {nazwa_swiata.upper()}: {', '.join(nieobecni)}")
    conn.close()
    os.remove(file_path)

@bot.command(name="wg_absent_list")
@tylko_na_kanale_dowodzenia()
async def cmd_absent_list(ctx, nazwa_swiata: str):
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("SELECT nick, COUNT(*) FROM nieobecnosci WHERE swiat = ? GROUP BY nick", (nazwa_swiata.lower(),))
    res = cursor.fetchall()
    conn.close()
    await ctx.send("📊 " + "\n".join([f"{r[0]}: {r[1]}x" for r in res]))

@bot.command(name="wg_del_raport")
@tylko_na_kanale_dowodzenia()
async def cmd_del_raport(ctx, nazwa_swiata: str):
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("SELECT MAX(data_wpisu) FROM nieobecnosci WHERE swiat = ?", (nazwa_swiata.lower(),))
    data = cursor.fetchone()[0]
    cursor.execute("DELETE FROM nieobecnosci WHERE data_wpisu = ?", (data,))
    conn.commit()
    conn.close()
    await ctx.send("⏪ Cofnięto ostatni raport.")

@bot.command(name="wg_add_absent")
@tylko_na_kanale_dowodzenia()
async def cmd_add_absent(ctx, nazwa_swiata: str, *, nick: str):
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO nieobecnosci VALUES (?, ?, ?)", (nazwa_swiata.lower(), nick.strip(), datetime.now()))
    conn.commit()
    conn.close()
    await ctx.send(f"➕ Dodano minus dla {nick}.")

@bot.command(name="wg_del_absent")
@tylko_na_kanale_dowodzenia()
async def cmd_del_absent(ctx, nazwa_swiata: str, *, nick: str):
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM nieobecnosci WHERE rowid IN (SELECT rowid FROM nieobecnosci WHERE swiat = ? AND nick = ? ORDER BY data_wpisu DESC LIMIT 1)", (nazwa_swiata.lower(), nick.strip()))
    conn.commit()
    conn.close()
    await ctx.send(f"➖ Usunięto minus dla {nick}.")

bot.run(TOKEN)
