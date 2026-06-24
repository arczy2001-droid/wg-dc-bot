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
    c.execute("CREATE TABLE IF NOT EXISTS raporty (swiat TEXT, data_wpisu TIMESTAMP)")  # Nowa tabela licznika raportów
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
                txt = "\n".join([f"{r[0]}: {r[1]}x" for r in res]) if res else "Brak zarejestrowanych nieobecności."
                
                naglowek = f"📊 **Ranking z świata {swiat.upper()} na bazie raportów: ({liczba_raportow}):**"
                try:
                    target_chan = self.get_channel(int(kanal_id)) or await self.fetch_channel(int(kanal_id))
                    if target_chan:
                        await target_chan.send(f"{naglowek}\n```\n{txt}\n```")
                except Exception as e:
                    print(f"Błąd automatycznego wysyłania rankingu dla świata {swiat}: {e}")
            conn.close()

bot = MyBot()

# --- STRAŻNIK KANAŁU ---
async def sprawdz_pozwolenie(interaction: discord.Interaction) -> bool:
    conn = sqlite3.connect("gildia.db")
    res = conn.cursor().execute("SELECT wartosc FROM ustawienia WHERE klucz = 'kanal_glowy'").fetchone()
    conn.close()
    if res:
        kanal_id = int(res[0])
        if interaction.channel_id != kanal_id:
            await interaction.response.send_message(f"❌ Gamoniu, miałeś tłumaczone, że tylko na kanale głównym do komend można wpisywać polecenia: <#{kanal_id}>.", ephemeral=True)
            return False
    return True

# --- KOMENDY ---
@bot.tree.command(name="wg_root", description="Konfiguruje kanał główny")
async def wg_root(interaction: discord.Interaction):
    conn = sqlite3.connect("gildia.db")
    conn.cursor().execute("INSERT OR REPLACE INTO ustawienia VALUES ('kanal_glowy', ?)", (str(interaction.channel_id),))
    conn.commit(); conn.close()
    await interaction.response.send_message("✅ Kanał główny ustawiony.")

@bot.tree.command(name="wg_add_world", description="Dodaje świat i przypisuje kanał")
async def wg_add_world(interaction: discord.Interaction, nazwa: str, kanal: discord.TextChannel):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    conn.cursor().execute("INSERT OR REPLACE INTO swiaty VALUES (?, ?)", (nazwa.lower(), str(kanal.id)))
    conn.commit(); conn.close()
    await interaction.response.send_message(f"✅ Dodano świat {nazwa} z kanałem <#{kanal.id}>.")

@bot.tree.command(name="wg_delete_world", description="Usuwa świat")
async def wg_delete_world(interaction: discord.Interaction, nazwa: str):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    conn.cursor().execute("DELETE FROM swiaty WHERE nazwa = ?", (nazwa.lower(),))
    conn.commit(); conn.close()
    await interaction.response.send_message(f"🗑️ Usunięto świat {nazwa}. Czujesz się jak Thanos niszczyciel światów? ")

@bot.tree.command(name="wg_worlds", description="Wyświetla listę światów")
async def wg_worlds(interaction: discord.Interaction):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    res = conn.cursor().execute("SELECT nazwa, kanal_id FROM swiaty").fetchall()
    conn.close()
    txt = "\n".join([f"{r[0]} -> <#{r[1]}>" for r in res]) if res else "Brak światów."
    await interaction.response.send_message(f"🌍 Światy:\n{txt}")

@bot.tree.command(name="wg_add_member", description="Dodaje graczy do świata")
async def wg_add_member(interaction: discord.Interaction, swiat: str, lista: str):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    for n in [x.strip() for x in re.split(r'[\n,]+', lista)]:
        conn.cursor().execute("INSERT OR IGNORE INTO czlonkowie VALUES (?, ?)", (swiat.lower(), n))
    conn.commit(); conn.close()
    await interaction.response.send_message("✅ Liczba baranków Bożych wzrosła.")

@bot.tree.command(name="wg_delete_member", description="Usuwa gracza")
async def wg_delete_member(interaction: discord.Interaction, swiat: str, nick: str):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    conn.cursor().execute("DELETE FROM czlonkowie WHERE swiat=? AND nick=?", (swiat.lower(), nick))
    conn.commit(); conn.close()
    await interaction.response.send_message(f"🗑️ Kopa w dupę dostał/a {nick}.")

@bot.tree.command(name="wg_member_list", description="Oto nasz skład:")
async def wg_member_list(interaction: discord.Interaction, swiat: str):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("SELECT nick FROM czlonkowie WHERE swiat = ? ORDER BY nick ASC", (swiat.lower(),))
    res = [r[0] for r in cursor.fetchall()]
    conn.close()
    
    if not res: 
        await interaction.response.send_message("👻 Skład jest pusty."); return
    
    size = (len(res) + 2) // 3
    c1 = res[0:size]
    c2 = res[size:size*2]
    c3 = res[size*2:]
    
    embed = discord.Embed(title=f"📜 Skład: {swiat.upper()}", color=discord.Color.blue())
    embed.add_field(name="I", value="\n".join(c1) or "-", inline=True)
    embed.add_field(name="II", value="\n".join(c2) or "-", inline=True)
    embed.add_field(name="III", value="\n".join(c3) or "-", inline=True)
    embed.set_footer(text=f"Łącznie członków: {len(res)}")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="wg", description="Załaduję raport..jak mi się zachce. Teraz poczekaj")
async def wg(interaction: discord.Interaction, swiat: str, screen: discord.Attachment):
    if not await sprawdz_pozwolenie(interaction): return
    await interaction.response.defer()
    conn = sqlite3.connect("gildia.db")
    swiat_data = conn.cursor().execute("SELECT kanal_id FROM swiaty WHERE nazwa=?", (swiat.lower(),)).fetchone()
    if not swiat_data: await interaction.followup.send("❌ Nieznany świat."); return
    
    path = f"temp_{screen.filename}"
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
    
    terar = datetime.now()
    conn.cursor().execute("INSERT INTO raporty VALUES (?, ?)", (swiat.lower(), terar))
    
    for n in nieobecni: 
        conn.cursor().execute("INSERT INTO nieobecnosci VALUES (?, ?, ?)", (swiat.lower(), n, terar))
        
    conn.commit(); conn.close()
    if os.path.exists(path): os.remove(path)
    
    try:
        target_chan = bot.get_channel(int(swiat_data[0])) or await bot.fetch_channel(int(swiat_data[0]))
        await target_chan.send(f"🚨 Nieobecni ({swiat.upper()}): {', '.join(nieobecni)}")
    except Exception as e:
        print(f"Błąd wysyłania raportu na kanał świata: {e}")
        
    await interaction.followup.send("✅ Raport wysłany na odpowiedni kanał.")

@bot.tree.command(name="wg_absent_list", description="Ranking nieobecności")
async def wg_absent_list(interaction: discord.Interaction, swiat: str):
    if not await sprawdz_pozwolenie(interaction): return
    # Dodajemy defer(), aby Discord nie zrywał połączenia po 3 sekundach lagów na bazie danych
    await interaction.response.defer()
    
    conn = sqlite3.connect("gildia.db")
    liczba_raportow = conn.cursor().execute("SELECT COUNT(*) FROM raporty WHERE swiat=?", (swiat.lower(),)).fetchone()[0]
    res = conn.cursor().execute("SELECT nick, COUNT(*) FROM nieobecnosci WHERE swiat=? GROUP BY nick ORDER BY COUNT(*) DESC", (swiat.lower(),)).fetchall()
    conn.close()
    
    txt = "\n".join([f"{r[0]}: {r[1]}x" for r in res]) if res else "Brak nieobecności."
    naglowek = f"📊 **Ranking z świata {swiat.upper()} na bazie raportów: ({liczba_raportow}):**"
    
    # Odpowiadamy przez followup, ponieważ użyliśmy defer()
    await interaction.followup.send(f"{naglowek}\n{txt}")

@bot.tree.command(name="wg_delete_raport", description="Usuwa ostatni raport z wybranego świata")
async def wg_delete_raport(interaction: discord.Interaction, swiat: str):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    res = conn.cursor().execute("SELECT MAX(data_wpisu) FROM raporty WHERE swiat=?", (swiat.lower(),)).fetchone()
    
    if res and res[0]:
        ostatnia_data = res[0]
        conn.cursor().execute("DELETE FROM nieobecnosci WHERE swiat=? AND data_wpisu=?", (swiat.lower(), ostatnia_data))
        conn.cursor().execute("DELETE FROM raporty WHERE swiat=? AND data_wpisu=?", (swiat.lower(), ostatnia_data))
        conn.commit()
        await interaction.response.send_message("⏪ Cofnięto ostatni raport (i zaktualizowano licznik).")
    else:
        await interaction.response.send_message("❌ Nie znaleziono żadnych raportów dla tego świata.")
    conn.close()

@bot.tree.command(name="wg_add_absent", description="Dodaj punkt nieobecności")
async def wg_add_absent(interaction: discord.Interaction, swiat: str, nick: str):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    conn.cursor().execute("INSERT INTO nieobecnosci VALUES (?, ?, ?)", (swiat.lower(), nick, datetime.now()))
    conn.commit(); conn.close()
    await interaction.response.send_message(f"➕ Dodano nieobecność dla {nick}.")

@bot.tree.command(name="wg_delete_absent", description="Usuń punkt nieobecności")
async def wg_delete_absent(interaction: discord.Interaction, swiat: str, nick: str):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    conn.cursor().execute("DELETE FROM nieobecnosci WHERE rowid IN (SELECT rowid FROM nieobecnosci WHERE swiat=? AND nick=? ORDER BY data_wpisu DESC LIMIT 1)", (swiat.lower(), nick))
    conn.commit(); conn.close()
    await interaction.response.send_message(f"➖ Usunięto nieobecność {nick}.")

@bot.tree.command(name="wg_clear_all", description="Czyści wszystko")
async def wg_clear_all(interaction: discord.Interaction):
    if not await sprawdz_pozwolenie(interaction): return
    conn = sqlite3.connect("gildia.db")
    conn.cursor().execute("DELETE FROM nieobecnosci")
    conn.cursor().execute("DELETE FROM raporty")
    conn.commit(); conn.close()
    await interaction.response.send_message("💥 Baza nieobecności oraz licznik raportów wyczyszczone.")

@bot.event
async def on_ready():
    print("Bot gotowy!")

bot.run(TOKEN)
