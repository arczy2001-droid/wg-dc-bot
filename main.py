import discord
from discord import app_commands
from discord.ext import commands
import os
import sqlite3
import re
from datetime import datetime
from PIL import Image, ImageEnhance
import asyncio
import difflib
import pytesseract

TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# --- FUNKCJE POMOCNICZE OCR ---
async def analizuj_screen_async(file_path):
    try:
        img = Image.open(file_path)
        # Zaawansowana obróbka obrazu przed OCR
        img = img.convert('L') # Skala szarości
        img = ImageEnhance.Contrast(img).enhance(2.0)
        
        text = pytesseract.image_to_string(img, lang='pol')
        lines = text.splitlines()
        print(f"DEBUG OCR: Odczytano {len(lines)} linii tekstu.")
        return lines
    except Exception as e:
        print(f"BŁĄD OCR: {e}")
        return []

def dopasuj_nick(ocr_nick, sklad_gildii):

    clean_ocr = re.sub(r'\(.*?\)', '', ocr_nick).strip()
    
    normalized = clean_ocr.replace('1', 'l').replace('0', 'o').replace('!', 'i')
    
    matches = difflib.get_close_matches(normalized, sklad_gildii, n=1, cutoff=0.3)
    
    # Opcjonalny DEBUG (pomoże Ci zobaczyć w konsoli, co bot myśli)
    if matches:
        print(f"DEBUG: OCR '{ocr_nick}' -> znormalizowano na '{normalized}' -> dopasowano do '{matches[0]}'")
    else:
        print(f"DEBUG: Nie udało się dopasować: '{normalized}'")
        
    return matches[0] if matches else None

# --- BAZA DANYCH ---
def init_db():
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS nieobecnosci (swiat TEXT, nick TEXT, data_wpisu TIMESTAMP)")
    cursor.execute("CREATE TABLE IF NOT EXISTS ustawienia (klucz TEXT PRIMARY KEY, wartosc TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS swiaty (nazwa TEXT PRIMARY KEY, kanal_id INTEGER)")
    cursor.execute("CREATE TABLE IF NOT EXISTS czlonkowie (swiat TEXT, nick TEXT, PRIMARY KEY(swiat, nick))")
    conn.commit()
    conn.close()

class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all())
    async def setup_hook(self):
        await self.tree.sync() 
        print("Komendy Slash zsynchronizowane!")

bot = MyBot()

# --- KOMENDY SLASH ---

@bot.tree.command(name="wg_root", description="Konfiguruje kanał dowodzenia (Admin)")
@app_commands.checks.has_permissions(administrator=True)
async def wg_root(interaction: discord.Interaction):
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO ustawienia (klucz, wartosc) VALUES ('kanal_dowodzenia', ?)", (str(interaction.channel_id),))
    conn.commit()
    conn.close()
    await interaction.response.send_message("🎯 Ustawiono ten kanał jako Centrum Dowodzenia.")

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
        clean_line = line.strip()
        if "Niezarejestrowani" in clean_line or "Niezarejestrowani czlonkowie" in clean_line: 
            w_sekcji = True; continue
        if "Zarejestrowani" in clean_line or "Obrona" in clean_line: 
            w_sekcji = False
            
        if w_sekcji and len(clean_line) > 3:
            nick = dopasuj_nick(clean_line, sklad)
            if nick and nick not in nieobecni: 
                nieobecni.append(nick)
                cursor.execute("INSERT INTO nieobecnosci (swiat, nick, data_wpisu) VALUES (?, ?, ?)", 
                               (swiat.lower(), nick, datetime.now()))
    
    conn.commit()
    conn.close()
    if os.path.exists(file_path): os.remove(file_path)
    
    msg = f"🚨 Raport {swiat.upper()} przetworzony. Nieobecni: {', '.join(nieobecni)}" if nieobecni else f"✅ Raport {swiat.upper()} przetworzony. Brak nieobecnych."
    await interaction.followup.send(msg)

@bot.tree.command(name="wg_add_member", description="Dodaje graczy (rozdziela spacjami, przecinkami lub nowymi liniami)")
async def wg_add_member(interaction: discord.Interaction, swiat: str, nick: str):
    lista_nickow = re.split(r'[\n,]+|\s{2,}', nick)
    czysta_lista = [n.strip() for n in lista_nickow if n.strip()]
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    dodani = 0
    for n in czysta_lista:
        cursor.execute("INSERT OR IGNORE INTO czlonkowie (swiat, nick) VALUES (?, ?)", (swiat.lower(), n))
        if cursor.rowcount > 0: dodani += 1
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"✅ Dodano {dodani} graczy do {swiat.upper()}.")

@bot.tree.command(name="wg_member_list", description="Wyświetla listę członków gildii")
async def wg_member_list(interaction: discord.Interaction, swiat: str):
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("SELECT nick FROM czlonkowie WHERE swiat = ? ORDER BY nick ASC", (swiat.lower(),))
    res = [r[0] for r in cursor.fetchall()]
    conn.close()
    if not res: await interaction.response.send_message("👻 Skład jest pusty."); return
    
    embed = discord.Embed(title=f"📜 Lista członków: {swiat.upper()}", color=discord.Color.blue())
    # Dzielenie na mniejsze pola, aby nie przekroczyć limitu znaków
    embed.add_field(name="Członkowie", value="\n".join(res), inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="wg_delete_raport", description="Usuwa ostatni raport dla świata")
async def wg_delete_raport(interaction: discord.Interaction, swiat: str):
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("SELECT MAX(data_wpisu) FROM nieobecnosci WHERE swiat = ?", (swiat.lower(),))
    data = cursor.fetchone()[0]
    if data:
        cursor.execute("DELETE FROM nieobecnosci WHERE data_wpisu = ?", (data,))
        conn.commit()
        await interaction.response.send_message("⏪ Cofnięto ostatni raport.")
    else:
        await interaction.response.send_message("❌ Brak raportów.")
    conn.close()

@bot.event
async def on_ready():
    init_db()
    print(f"Bot {bot.user} gotowy!")

bot.run(TOKEN)
