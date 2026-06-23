import discord
from discord import app_commands
from discord.ext import commands
import os
import sqlite3
import re
from datetime import datetime
import asyncio
import difflib
import aiohttp 

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
OCR_API_KEY = "K88973838788957"

# ==========================================
# --- SILNIK CHMUROWY (OCR.space API) ---
# ==========================================

async def analizuj_screen_async(file_path):
    """Wysyła plik do chmury OCR.space i zwraca tekst w formie listy linii."""
    url = 'https://api.ocr.space/parse/image'
    
    try:
        with open(file_path, 'rb') as f:
            payload = {
                'apikey': OCR_API_KEY,
                'language': 'eng',  # W grach 'eng' czyta nicki (np. kusn1leerz) o 200% lepiej niż 'pol'
                'OCREngine': '2',   # Silnik nr 2 = Sieć neuronowa dedykowana pod trudne tła i screenshoty
                'scale': 'true',    # Automatyczny upscaling po stronie serwerów chmury
                'file': f
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, data=payload) as resp:
                    result = await resp.json()
                    
                    if result.get('IsErroredOnProcessing'):
                        print(f"BŁĄD CHMURY OCR: {result.get('ErrorMessage')}")
                        return []
                        
                    parsed_text = result['ParsedResults'][0]['ParsedText']
                    lines = parsed_text.splitlines()
                    
                    print("\n--- ODPOWIEDŹ Z CHMURY OCR ---")
                    for l in lines: 
                        if l.strip(): print(f"Widzę: {l.strip()}")
                        
                    return lines

    except Exception as e:
        print(f"Krytyczny błąd połączenia z API OCR: {e}")
        return []

def dopasuj_nick(ocr_nick, sklad_gildii):
    """Zoptymalizowany parser tekstu pod chmurę."""
    # 1. Najpierw wycinamy nawias z poziomem (Poziom 440)
    clean = re.sub(r'\(.*?\)', '', ocr_nick)
    
    # 2. Dopiero teraz usuwamy ikony, krzaki i zostawiamy same litery/cyfry
    clean = re.sub(r'[^a-zA-Z0-9 ]', '', clean).strip()
    
    if len(clean) < 3: 
        return None
        
    # Bezpośrednie dopasowanie difflib (chmura rzadko robi literówki, więc cutoff=0.5 jest bezpieczny)
    matches = difflib.get_close_matches(clean, sklad_gildii, n=1, cutoff=0.5)
    if matches: 
        return matches[0]
        
    # Twardy fallback: sprawdzamy, czy prawdziwy nick nie zaszył się wewnątrz odczytanego ciągu
    for member in sklad_gildii:
        if member.lower() in clean.lower(): 
            return member
            
    return None

# ==========================================
# --- BAZA DANYCH ---
# ==========================================

def init_db():
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS nieobecnosci (swiat TEXT, nick TEXT, data_wpisu TIMESTAMP)")
    cursor.execute("CREATE TABLE IF NOT EXISTS ustawienia (klucz TEXT PRIMARY KEY, wartosc TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS czlonkowie (swiat TEXT, nick TEXT, PRIMARY KEY(swiat, nick))")
    conn.commit()
    conn.close()

# ==========================================
# --- KLASA BOTA ---
# ==========================================

class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all())

    async def setup_hook(self):
        await self.tree.sync() 
        print("Komendy Slash zsynchronizowane!")

bot = MyBot()

# ==========================================
# --- KOMENDY ---
# ==========================================

@bot.tree.command(name="wg_root", description="Ustawia ten kanał jako Centrum Dowodzenia")
@app_commands.checks.has_permissions(administrator=True)
async def wg_root(interaction: discord.Interaction):
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO ustawienia VALUES ('kanal_dowodzenia', ?)", (str(interaction.channel_id),))
    conn.commit(); conn.close()
    await interaction.response.send_message("🎯 Ustawiono ten kanał jako Centrum Dowodzenia.")

@bot.tree.command(name="wg", description="Analizuje raport nieobecności ze screena")
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
                cursor.execute("INSERT INTO nieobecnosci VALUES (?, ?, ?)", 
                               (swiat.lower(), nick, datetime.now()))
    
    conn.commit(); conn.close()
    if os.path.exists(file_path): os.remove(file_path)
    
    msg = f"🚨 Nieobecni ({swiat.upper()}): {', '.join(nieobecni)}" if nieobecni else f"✅ Raport {swiat.upper()} ok, brak nieobecnych."
    await interaction.followup.send(msg)

@bot.tree.command(name="wg_add_member", description="Dodaje graczy do gildii")
async def wg_add_member(interaction: discord.Interaction, swiat: str, nick: str):
    lista = [n.strip() for n in re.split(r'[\n,]+|\s{2,}', nick) if n.strip()]
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    dodani = 0
    for n in lista:
        cursor.execute("INSERT OR IGNORE INTO czlonkowie VALUES (?, ?)", (swiat.lower(), n))
        if cursor.rowcount > 0: dodani += 1
    conn.commit(); conn.close()
    await interaction.response.send_message(f"✅ Dodano {dodani} graczy do świata {swiat.upper()}.")

@bot.tree.command(name="wg_delete_member", description="Usuwa gracza z bazy")
async def wg_delete_member(interaction: discord.Interaction, swiat: str, nick: str):
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM czlonkowie WHERE swiat = ? AND nick = ?", (swiat.lower(), nick))
    conn.commit(); conn.close()
    await interaction.response.send_message(f"🗑️ Usunięto gracza {nick}.")

@bot.tree.command(name="wg_member_list", description="Wyświetla skład gildii w 3 kolumnach")
async def wg_member_list(interaction: discord.Interaction, swiat: str):
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("SELECT nick FROM czlonkowie WHERE swiat = ? ORDER BY nick ASC", (swiat.lower(),))
    res = [r[0] for r in cursor.fetchall()]
    conn.close()
    
    if not res: await interaction.response.send_message("👻 Skład jest pusty."); return
    
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
    cursor.execute("SELECT nick, COUNT(*) FROM nieobecnosci WHERE swiat = ? GROUP BY nick ORDER BY COUNT(*) DESC", (swiat.lower(),))
    res = cursor.fetchall()
    conn.close()
    
    txt = "\n".join([f"{r[0]}: {r[1]}x" for r in res]) if res else "Brak nieobecności."
    await interaction.response.send_message(f"📊 Nieobecności ({swiat.upper()}):\n```\n{txt}\n```")

@bot.tree.command(name="wg_add_absent", description="Ręcznie dopisz komuś nieobecność")
async def wg_add_absent(interaction: discord.Interaction, swiat: str, nick: str):
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO nieobecnosci VALUES (?, ?, ?)", (swiat.lower(), nick, datetime.now()))
    conn.commit(); conn.close()
    await interaction.response.send_message(f"➕ Dodano nieobecność dla {nick}.")

@bot.tree.command(name="wg_delete_absent", description="Skasuj ostatnią nieobecność gracza")
async def wg_delete_absent(interaction: discord.Interaction, swiat: str, nick: str):
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM nieobecnosci WHERE rowid IN (SELECT rowid FROM nieobecnosci WHERE swiat=? AND nick=? ORDER BY data_wpisu DESC LIMIT 1)", (swiat.lower(), nick))
    conn.commit(); conn.close()
    await interaction.response.send_message(f"➖ Usunięto 1 nieobecność gracza {nick}.")

@bot.tree.command(name="wg_delete_raport", description="Cofnij cały ostatni wrzucony raport ze świata")
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
        await interaction.response.send_message("❌ Brak raportów do cofnięcia.")
    conn.close()

@bot.event
async def on_ready():
    init_db()
    print(f"Bot {bot.user} zalogowany i gotowy do akcji!")

bot.run(TOKEN)
