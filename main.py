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

# ==========================================
# --- BAZA DANYCH I AUTOCZYSZCZENIE ---
# ==========================================

def init_db():
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS nieobecnosci (swiat TEXT, nick TEXT, data_wpisu TIMESTAMP)")
    cursor.execute("CREATE TABLE IF NOT EXISTS ustawienia (klucz TEXT PRIMARY KEY, wartosc TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS czlonkowie (swiat TEXT, nick TEXT, PRIMARY KEY(swiat, nick))")
    conn.commit()
    conn.close()

def usun_stare_raporty():
    """Kasuje wpisy starsze niż 7 dni i 1 godzina (169 godzin)"""
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    prog_czasowy = datetime.now() - timedelta(days=7, hours=1)
    
    cursor.execute("DELETE FROM nieobecnosci WHERE data_wpisu <= ?", (prog_czasowy,))
    usunieto = cursor.rowcount
    conn.commit()
    conn.close()
    
    if usunieto > 0:
        print(f"[Auto-Miotła] Wykasowano {usunieto} przedawnionych nieobecności (> 7d 1h).")

def get_ustawienie(klucz):
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("SELECT wartosc FROM ustawienia WHERE klucz = ?", (klucz,))
    res = cursor.fetchone()
    conn.close()
    return int(res[0]) if res else None

# ==========================================
# --- STRAŻNIK KANAŁÓW ---
# ==========================================

async def sprawdz_pozwolenie(interaction: discord.Interaction) -> bool:
    kanal_komend = get_ustawienie('kanal_dowodzenia')
    
    # Jeśli kanał jest ustawiony w bazie, a użytkownik pisze na innym:
    if kanal_komend and interaction.channel_id != kanal_komend:
        await interaction.response.send_message(
            f"❌ Komend bota można używać wyłącznie na kanale: <#{kanal_komend}>", 
            ephemeral=True  # Wiadomość widzi tylko spamiący
        )
        return False
    return True

# ==========================================
# --- SILNIK CHMUROWY OCR ---
# ==========================================

async def analizuj_screen_async(file_path):
    url = 'https://api.ocr.space/parse/image'
    try:
        with open(file_path, 'rb') as f:
            payload = {'apikey': OCR_API_KEY, 'language': 'eng', 'OCREngine': '2', 'scale': 'true', 'file': f}
            async with aiohttp.ClientSession() as session:
                async with session.post(url, data=payload) as resp:
                    result = await resp.json()
                    if result.get('IsErroredOnProcessing'):
                        print(f"BŁĄD CHMURY OCR: {result.get('ErrorMessage')}")
                        return []
                    return result['ParsedResults'][0]['ParsedText'].splitlines()
    except Exception as e:
        print(f"Błąd API OCR: {e}")
        return []

def dopasuj_nick(ocr_nick, sklad_gildii):
    clean = re.sub(r'\(.*?\)', '', ocr_nick)
    clean = re.sub(r'[^a-zA-Z0-9 ]', '', clean).strip()
    if len(clean) < 3: return None
    matches = difflib.get_close_matches(clean, sklad_gildii, n=1, cutoff=0.5)
    if matches: return matches[0]
    for m in sklad_gildii:
        if m.lower() in clean.lower(): return m
    return None

# ==========================================
# --- KLASA BOTA ---
# ==========================================

class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all())

    async def setup_hook(self):
        self.pętla_czyszczenia.start()
        await self.tree.sync() 
        print("Komendy Slash zsynchronizowane!")

    @tasks.loop(hours=1)
    async def pętla_czyszczenia(self):
        usun_stare_raporty()

    @pętla_czyszczenia.before_loop
    async def before_pętla(self):
        await self.wait_until_ready()

bot = MyBot()

# ===================================================
# --- KOMENDY ADMINISTRACYJNE (Brak blokady kanału) ---
# ===================================================

@bot.tree.command(name="wg_root", description="[ADMIN] Przypisz ten kanał jako JEDYNY do wpisywania komend")
@app_commands.checks.has_permissions(administrator=True)
async def wg_root(interaction: discord.Interaction):
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO ustawienia VALUES ('kanal_dowodzenia', ?)", (str(interaction.channel_id),))
    conn.commit(); conn.close()
    await interaction.response.send_message(f"🎯 Zamknięto strefę. Od teraz komendy działają tylko na <#{interaction.channel_id}>.")

@bot.tree.command(name="wg_set_output", description="[ADMIN] Przypisz kanał, na który bot ma automatycznie wysyłać listy nieobecnych")
@app_commands.checks.has_permissions(administrator=True)
async def wg_set_output(interaction: discord.Interaction):
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO ustawienia VALUES ('kanal_raportow', ?)", (str(interaction.channel_id),))
    conn.commit(); conn.close()
    await interaction.response.send_message(f"📢 Przypisano tablicę ogłoszeń. Wyniki raportów będą lądować na <#{interaction.channel_id}>.")

@bot.tree.command(name="wg_status", description="[ADMIN] Sprawdź stan konfiguracji bota")
@app_commands.checks.has_permissions(administrator=True)
async def wg_status(interaction: discord.Interaction):
    k_cmd = get_ustawienie('kanal_dowodzenia')
    k_out = get_ustawienie('kanal_raportow')
    conn = sqlite3.connect("gildia.db")
    c = conn.cursor()
    graczy = c.execute("SELECT COUNT(*) FROM czlonkowie").fetchone()[0]
    wpisow = c.execute("SELECT COUNT(*) FROM nieobecnosci").fetchone()[0]
    conn.close()

    txt = (f"🛠️ **Raport Konfiguracyjny**:\n"
           f"📍 **Kanał komend**: {f'<#{k_cmd}>' if k_cmd else 'Brak (komendy działają wszędzie)'}\n"
           f"📢 **Kanał wysyłkowy**: {f'<#{k_out}>' if k_out else 'Brak (odpowiada w miejscu wpisania)'}\n"
           f"👥 Baza graczy: **{graczy}** | 📊 Aktywne nieobecności: **{wpisow}**")
    await interaction.response.send_message(txt, ephemeral=True)

# ===================================================
# --- KOMENDY UŻYTKOWE (Objęte blokadą kanału) ---
# ===================================================

@bot.tree.command(name="wg", description="Przetwórz raport ze zrzutu ekranu")
async def wg_raport(interaction: discord.Interaction, swiat: str, screen: discord.Attachment):
    if not await sprawdz_pozwolenie(interaction): return
    
    await interaction.response.defer()
    file_path = f"temp_{screen.filename}"
    await screen.save(file_path)
    
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    sklad = [r[0] for r in cursor.execute("SELECT nick FROM czlonkowie WHERE swiat = ?", (swiat.lower(),)).fetchall()]
    
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
                cursor.execute("INSERT INTO nieobecnosci VALUES (?, ?, ?)", (swiat.lower(), nick, datetime.now()))
    
    conn.commit(); conn.close()
    if os.path.exists(file_path): os.remove(file_path)
    
    msg = f"🚨 Nieobecni ({swiat.upper()}): {', '.join(nieobecni)}" if nieobecni else f"✅ Raport {swiat.upper()} w porządku, brak nieobecnych."

    kanal_wyjsciowy = get_ustawienie('kanal_raportow')
    if kanal_wyjsciowy:
        try:
            channel = bot.get_channel(kanal_wyjsciowy) or await bot.fetch_channel(kanal_wyjsciowy)
            await channel.send(msg)
            await interaction.followup.send(f"✅ Raport przetworzony. Wynik opublikowano na kanale <#{kanal_wyjsciowy}>.")
        except Exception as e:
            await interaction.followup.send(f"⚠️ Raport przetworzony, ale bot nie ma uprawnień pisać na przypisanym kanale! Wynik:\n{msg}")
    else:
        await interaction.followup.send(msg)

@bot.tree.command(name="wg_absent_list", description="Ranking nieobecności (z ostatnich 7d 1h)")
async def wg_absent_list(interaction: discord.Interaction, swiat: str):
    if not await sprawdz_pozwolenie(interaction): return
    usun_stare_raporty()
    
    conn = sqlite3.connect("gildia.db")
    res = conn.cursor().execute("SELECT nick, COUNT(*) FROM nieobecnosci WHERE swiat = ? GROUP BY nick ORDER BY COUNT(*) DESC", (swiat.lower(),)).fetchall()
    conn.close()
    
    txt = "\n".join([f"{r[0]}: {r[1]}x" for r in res]) if res else "Brak nieobecności w ostatnich 7 dniach."
    await interaction.response.send_message(f"📊 Ranking nieobecności ({swiat.upper()}):\n```\n{txt}\n```")

@bot.tree.command(name="wg_member_list", description="Wyświetla skład gildii")
async def wg_member_list(interaction: discord.Interaction, swiat: str):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    res = [r[0] for r in conn.cursor().execute("SELECT nick FROM czlonkowie WHERE swiat = ? ORDER BY nick ASC", (swiat.lower(),)).fetchall()]
    conn.close()
    if not res: await interaction.response.send_message("👻 Skład jest pusty."); return
    
    size = (len(res) + 2) // 3
    embed = discord.Embed(title=f"📜 Skład: {swiat.upper()}", color=discord.Color.blue())
    embed.add_field(name="I", value="\n".join(res[0:size]) or "-", inline=True)
    embed.add_field(name="II", value="\n".join(res[size:size*2]) or "-", inline=True)
    embed.add_field(name="III", value="\n".join(res[size*2:]) or "-", inline=True)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="wg_add_member", description="Dodaje graczy do bazy")
async def wg_add_member(interaction: discord.Interaction, swiat: str, nick: str):
    if not await sprawdz_pozwolenie(interaction): return
    lista = [n.strip() for n in re.split(r'[\n,]+|\s{2,}', nick) if n.strip()]
    conn = sqlite3.connect("gildia.db"); cursor = conn.cursor()
    dodani = sum(1 for n in lista if cursor.execute("INSERT OR IGNORE INTO czlonkowie VALUES (?, ?)", (swiat.lower(), n)).rowcount > 0)
    conn.commit(); conn.close()
    await interaction.response.send_message(f"✅ Dodano {dodani} graczy do świata {swiat.upper()}.")

@bot.tree.command(name="wg_delete_member", description="Usuwa gracza z bazy")
async def wg_delete_member(interaction: discord.Interaction, swiat: str, nick: str):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    conn.cursor().execute("DELETE FROM czlonkowie WHERE swiat = ? AND nick = ?", (swiat.lower(), nick))
    conn.commit(); conn.close()
    await interaction.response.send_message(f"🗑️ Usunięto gracza {nick}.")

@bot.tree.command(name="wg_add_absent", description="Ręcznie dopisz komuś nieobecność")
async def wg_add_absent(interaction: discord.Interaction, swiat: str, nick: str):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    conn.cursor().execute("INSERT INTO nieobecnosci VALUES (?, ?, ?)", (swiat.lower(), nick, datetime.now()))
    conn.commit(); conn.close()
    await interaction.response.send_message(f"➕ Dodano nieobecność dla {nick}.")

@bot.tree.command(name="wg_delete_absent", description="Skasuj ostatnią nieobecność gracza")
async def wg_delete_absent(interaction: discord.Interaction, swiat: str, nick: str):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    conn.cursor().execute("DELETE FROM nieobecnosci WHERE rowid IN (SELECT rowid FROM nieobecnosci WHERE swiat=? AND nick=? ORDER BY data_wpisu DESC LIMIT 1)", (swiat.lower(), nick))
    conn.commit(); conn.close()
    await interaction.response.send_message(f"➖ Usunięto 1 nieobecność gracza {nick}.")

@bot.tree.command(name="wg_delete_raport", description="Cofnij cały ostatni wrzucony raport")
async def wg_delete_raport(interaction: discord.Interaction, swiat: str):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db"); cursor = conn.cursor()
    data = cursor.execute("SELECT MAX(data_wpisu) FROM nieobecnosci WHERE swiat = ?", (swiat.lower(),)).fetchone()[0]
    if data:
        cursor.execute("DELETE FROM nieobecnosci WHERE data_wpisu = ?", (data,))
        await interaction.response.send_message("⏪ Cofnięto ostatni wrzucony raport.")
    else:
        await interaction.response.send_message("❌ Brak raportów do cofnięcia.")
    conn.commit(); conn.close()
    
@bot.tree.command(name="wg_clear_all", description="[ADMIN] UWAGA: Usuwa WSZYSTKIE nieobecności ze wszystkich światów")
@app_commands.checks.has_permissions(administrator=True)
async def wg_clear_all(interaction: discord.Interaction):
    # Tutaj nie blokujemy kanałem, bo to komenda totalnie niszcząca dane
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM nieobecnosci")
    conn.commit()
    conn.close()
    await interaction.response.send_message("💥 Baza nieobecności wyczyszczona do zera!", ephemeral=True)
    
@bot.event
async def on_ready():
    init_db()
    print(f"Bot zalogowany jako {bot.user}")

bot.run(TOKEN)
