import discord
from discord import app_commands
from discord.ext import commands
import os
import sqlite3
from datetime import datetime

TOKEN = os.getenv("DISCORD_BOT_TOKEN")

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

# --- BOTA Z SYSTEMEM SLASH ---
class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all())

    async def setup_hook(self):
        await self.tree.sync() 
        print("Komendy Slash zsynchronizowane!")

bot = MyBot()

# --- KOMENDY ---

@bot.tree.command(name="wg_root", description="Konfiguruje kanał dowodzenia")
@app_commands.checks.has_permissions(administrator=True)
async def wg_root(interaction: discord.Interaction):
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO ustawienia (klucz, wartosc) VALUES ('kanal_dowodzenia', ?)", (str(interaction.channel_id),))
    conn.commit()
    conn.close()
    await interaction.response.send_message("🎯 Ustawiono ten kanał jako Centrum Dowodzenia.")

@bot.tree.command(name="wg_add_world", description="Dodaje świat oraz przypisuje do niego odpowiedni kanał raportowania.")
@app_commands.checks.has_permissions(administrator=True)
async def wg_add_world(interaction: discord.Interaction, nazwa: str, kanal: discord.TextChannel):
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO swiaty (nazwa, kanal_id) VALUES (?, ?)", (nazwa.lower(), kanal.id))
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"🌍 Dodano świat: {nazwa.upper()}")

@bot.tree.command(name="wg_delete_world", description="Usuwa świat.")
@app_commands.checks.has_permissions(administrator=True)
async def wg_delete_world(interaction: discord.Interaction, nazwa: str):
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM swiaty WHERE nazwa = ?", (nazwa.lower(),))
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"🗑️ Usunięto świat: {nazwa.upper()}")

@bot.tree.command(name="wg_add_member", description="Dodaje gracza do świata.")
async def wg_add_member(interaction: discord.Interaction, swiat: str, nick: str):
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO czlonkowie (swiat, nick) VALUES (?, ?)", (swiat.lower(), nick))
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"✅ Dodano gracza {nick} do {swiat.upper()}.")

@bot.tree.command(name="wg_delete_member", description="Usuwa konkretnego gracza ze składu danego świata.")
async def wg_delete_member(interaction: discord.Interaction, swiat: str, nick: str):
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM czlonkowie WHERE swiat = ? AND nick = ?", (swiat.lower(), nick))
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"🗑️ Usunięto gracza {nick}.")

@bot.tree.command(name="wg_member_list", description="Wyświetla pełną listę członków gildii przypisanych dla wybranego świata.")
async def wg_member_list(interaction: discord.Interaction, swiat: str):
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("SELECT nick FROM czlonkowie WHERE swiat = ?", (swiat.lower(),))
    res = cursor.fetchall()
    conn.close()
    await interaction.response.send_message(f"📜 Lista członków {swiat.upper()}: {', '.join([r[0] for r in res])}")

@bot.tree.command(name="wg_absent_list", description="Wyświetla ranking nieobecności członków na danym świecie.")
async def wg_absent_list(interaction: discord.Interaction, swiat: str):
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("SELECT nick, COUNT(*) FROM nieobecnosci WHERE swiat = ? GROUP BY nick", (swiat.lower(),))
    res = cursor.fetchall()
    conn.close()
    await interaction.response.send_message(f"📊 Nieobecności {swiat.upper()}: " + "\n".join([f"{r[0]}: {r[1]}x" for r in res]))

@bot.tree.command(name="wg_add_absent", description="Ręcznie dodaj nieobecność")
async def wg_add_absent(interaction: discord.Interaction, swiat: str, nick: str):
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO nieobecnosci VALUES (?, ?, ?)", (swiat.lower(), nick, datetime.now()))
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"➕ Dodano nieobecność dla {nick}.")

@bot.tree.command(name="wg_delete_absent", description="Usuń nieobecność")
async def wg_delete_absent(interaction: discord.Interaction, swiat: str, nick: str):
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM nieobecnosci WHERE rowid IN (SELECT rowid FROM nieobecnosci WHERE swiat = ? AND nick = ? ORDER BY data_wpisu DESC LIMIT 1)", (swiat.lower(), nick))
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"➖ Usunięto nieobecność dla {nick}.")

@bot.tree.command(name="wg_delete_raport", description="Usuwa ostatni dodany raport dla wybranego świata.")
async def wg_delete_raport(interaction: discord.Interaction, swiat: str):
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("SELECT MAX(data_wpisu) FROM nieobecnosci WHERE swiat = ?", (swiat.lower(),))
    data = cursor.fetchone()[0]
    cursor.execute("DELETE FROM nieobecnosci WHERE data_wpisu = ?", (data,))
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"⏪ Cofnięto ostatni raport dla {swiat.upper()}.")

@bot.event
async def on_ready():
    init_db()
    print(f"Bot {bot.user} gotowy!")

bot.run(TOKEN)
