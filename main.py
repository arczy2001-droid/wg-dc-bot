import discord
from discord import app_commands
from discord.ext import commands
import os
import sqlite3
import re
from datetime import datetime
from PIL import Image, ImageEnhance, ImageFilter
import asyncio
import difflib
import pytesseract

TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# --- FUNKCJE POMOCNICZE OCR ---
async def analizuj_screen_async(file_path):
    try:
        img = Image.open(file_path)
        img = img.resize((img.width * 2, img.height * 2), Image.Resampling.LANCZOS)
        img = img.convert('L').filter(ImageFilter.SHARPEN)
        img = ImageEnhance.Contrast(img).enhance(2.0)
        
        text = pytesseract.image_to_string(img, lang='pol')
        return text.splitlines()
    except Exception as e:
        print(f"BŁĄD OCR: {e}")
        return []

def dopasuj_nick(ocr_nick, sklad_gildii):
    clean_ocr = re.sub(r'[^a-zA-Z0-9 ]', '', ocr_nick)
    clean_ocr = re.sub(r'\(.*?\)', '', clean_ocr)
    words = [w for w in clean_ocr.split() if len(w) > 2]
    if not words: return None
    candidate = max(words, key=len)
    
    matches = difflib.get_close_matches(candidate, sklad_gildii, n=1, cutoff=0.25)
    return matches[0] if matches else None

# --- BAZA DANYCH ---
def init_db():
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS nieobecnosci (swiat TEXT, nick TEXT, data_wpisu TIMESTAMP)")
    cursor.execute("CREATE TABLE IF NOT EXISTS swiaty (nazwa TEXT PRIMARY KEY, kanal_id INTEGER)")
    cursor.execute("CREATE TABLE IF NOT EXISTS czlonkowie (swiat TEXT, nick TEXT, PRIMARY KEY(swiat, nick))")
    conn.commit()
    conn.close()

class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all())
    async def setup_hook(self):
        await self.tree.sync() 

bot = MyBot()

# --- KOMENDY ---

@bot.tree.command(name="wg", description="Analizuje screenshot raportu")
async def wg_raport(interaction: discord.Interaction, swiat: str, screen: discord.Attachment):
    await interaction.response.defer()
    file_path = f"temp_{screen.filename}"
    await screen.save(file_path)
    
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("SELECT nick FROM czlonkowie WHERE swiat = ?", (swiat.lower(),))
    sklad = [r[0] for r in cursor.fetchall()]
    
    lines = await analizuj_screen_async(file_path)
    nieobecni = []
    w_sekcji = False
    
    for line in lines:
        if "Niezarejestrowani" in line: w_sekcji = True; continue
        if "Zarejestrowani" in line or "Obrona" in line: w_sekcji = False; break
        if w_sekcji and len(line.strip()) > 3:
            nick = dopasuj_nick(line, sklad)
            if nick and nick not in nieobecni: 
                nieobecni.append(nick)
                cursor.execute("INSERT INTO nieobecnosci (swiat, nick, data_wpisu) VALUES (?, ?, ?)", 
                               (swiat.lower(), nick, datetime.now()))
    
    conn.commit()
    conn.close()
    if os.path.exists(file_path): os.remove(file_path)
    
    msg = f"🚨 Nieobecni ({swiat.upper()}): {', '.join(nieobecni)}" if nieobecni else f"✅ Raport {swiat.upper()} ok, brak nieobecnych."
    await interaction.followup.send(msg)

@bot.tree.command(name="wg_add_member", description="Dodaje graczy")
async def wg_add_member(interaction: discord.Interaction, swiat: str, nick: str):
    lista = [n.strip() for n in re.split(r'[\n,]+|\s{2,}', nick) if n.strip()]
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    dodani = 0
    for n in lista:
        cursor.execute("INSERT OR IGNORE INTO czlonkowie (swiat, nick) VALUES (?, ?)", (swiat.lower(), n))
        if cursor.rowcount > 0: dodani += 1
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"✅ Dodano {dodani} graczy.")

@bot.tree.command(name="wg_member_list", description="Lista członków w 3 kolumnach")
async def wg_member_list(interaction: discord.Interaction, swiat: str):
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("SELECT nick FROM czlonkowie WHERE swiat = ? ORDER BY nick ASC", (swiat.lower(),))
    res = [r[0] for r in cursor.fetchall()]
    conn.close()
    if not res: await interaction.response.send_message("👻 Pusto."); return
    
    size = (len(res) + 2) // 3
    c1, c2, c3 = res[0:size], res[size:size*2], res[size*2:]
    
    embed = discord.Embed(title=f"📜 Skład: {swiat.upper()}", color=discord.Color.blue())
    embed.add_field(name="I", value="\n".join(c1) or "-", inline=True)
    embed.add_field(name="II", value="\n".join(c2) or "-", inline=True)
    embed.add_field(name="III", value="\n".join(c3) or "-", inline=True)
    embed.set_footer(text=f"Łącznie: {len(res)}")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="wg_absent_list", description="Ranking nieobecności")
async def wg_absent_list(interaction: discord.Interaction, swiat: str):
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("SELECT nick, COUNT(*) FROM nieobecnosci WHERE swiat = ? GROUP BY nick", (swiat.lower(),))
    res = cursor.fetchall()
    conn.close()
    txt = "\n".join([f"{r[0]}: {r[1]}x" for r in res]) if res else "Brak nieobecności."
    await interaction.response.send_message(f"📊 Nieobecności:\n```{txt}```")

@bot.tree.command(name="wg_delete_raport", description="Cofnij ostatni raport")
async def wg_delete_raport(interaction: discord.Interaction, swiat: str):
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("SELECT MAX(data_wpisu) FROM nieobecnosci WHERE swiat = ?", (swiat.lower(),))
    data = cursor.fetchone()[0]
    if data:
        cursor.execute("DELETE FROM nieobecnosci WHERE data_wpisu = ?", (data,))
        conn.commit()
        await interaction.response.send_message("⏪ Cofnięto raport.")
    else:
        await interaction.response.send_message("❌ Brak raportów.")
    conn.close()

@bot.event
async def on_ready():
    init_db()
    print(f"Bot {bot.user} gotowy!")

bot.run(TOKEN)
