import discord
from discord.ext import commands
import os
import easyocr
import sqlite3
import re
from datetime import datetime

# 1. KONFIGURACJA — TWOJE PRAWDZIWE ID KANAŁÓW Z DISCORDA
KANALY_SWIATOW = {
    1518327717676716162: "Swiat1",
    1518327790703874139: "Swiat2",
    1518327805081817160: "Swiat3",
    1518327819292250254: "Swiat4"
}

TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# 2. INICJALIZACJA SILNIKA
print("Ładowanie modelu EasyOCR...")
reader = easyocr.Reader(['pl', 'en'], gpu=False)

def init_db():
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS nieobecnosci 
        (swiat TEXT, nick TEXT, data_wpisu TIMESTAMP)
    """)
    conn.commit()
    conn.close()

# 3. SILNIK OCR
def analizuj_screen(image_path):
    result = reader.readtext(image_path)
    nieobecni = []
    w_sekcji_nieobecnych = False

    for bbox, text, prob in result:
        text_clean = text.strip()
        if "Niezarejestrowani" in text_clean:
            w_sekcji_nieobecnych = True
            continue
        if "Zarejestrowani" in text_clean or "USUŃ" in text_clean:
            w_sekcji_nieobecnych = False
            break
        if w_sekcji_nieobecnych and text_clean:
            nick = re.split(r"\(", text_clean)[0].strip()
            if len(nick) > 2:
                nieobecni.append(nick)
    return nieobecni

# 4. GŁÓWNY KOD BOTA
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Zalogowano pomyślnie jako: {bot.user.name}")
    init_db()

# KOMENDA !stan
@bot.command(name="stan")
async def stan(ctx):
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("SELECT swiat, nick, COUNT(*) FROM nieobecnosci GROUP BY swiat, nick")
    rows = cursor.fetchall()
    conn.close()
    
    if not rows:
        await ctx.send("📋 Baza danych jest pusta. Brak zapisanych minusów.")
        return
        
    raport = "📊 **Aktualny stan minusów (brak rejestracji):**\n"
    for swiat, nick, ilosc in rows:
        raport += f"• [{swiat}] **{nick}**: {ilosc}x ❌\n"
    await ctx.send(raport)

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # Sprawdzanie czy kanał jest w naszej konfiguracji i czy dodano plik
    if message.channel.id in KANALY_SWIATOW and message.attachments:
        swiat = KANALY_SWIATOW[message.channel.id]
        attachment = message.attachments[0]

        if attachment.filename.lower().endswith((".png", ".jpg", ".jpeg")):
            potwierdzenie = await message.channel.send("🔄 Przetwarzam raport...")
            file_path = f"temp_{attachment.filename}"
            await attachment.save(file_path)

            try:
                nieobecni = analizuj_screen(file_path)
                if nieobecni:
                    conn = sqlite3.connect("gildia.db")
                    cursor = conn.cursor()
                    teraz = datetime.now()
                    for nick in nieobecni:
                        cursor.execute(
                            "INSERT INTO nieobecnosci (swiat, nick, data_wpisu) VALUES (?, ?, ?)",
                            (swiat, nick, teraz)
                        )
                    conn.commit()
                    conn.close()
                    lista_graczy = ", ".join([f"**{n}**" for n in nieobecni])
                    await potwierdzenie.edit(content=f"✅ Zapisano minusy (**{swiat}**)\n❌ Brak rejestracji: {lista_graczy}")
                else:
                    await potwierdzenie.edit(content=f"✅ Świat **{swiat}**: Wszyscy zarejestrowani!")
            except Exception as e:
                await potwierdzenie.edit(content=f"❌ Błąd przetwarzania: {str(e)}")
            finally:
                if os.path.exists(file_path):
                    os.remove(file_path)

    # To pozwala na działanie komend tekstowych (np. !stan) obok sprawdzania obrazków
    await bot.process_commands(message)

bot.run(TOKEN)
