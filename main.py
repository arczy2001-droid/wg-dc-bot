import os
import re
import sqlite3
from datetime import datetime, timedelta
import discord
from discord.ext import commands, tasks
import easyocr

# =========================================================
# 1. KONFIGURACJA KANAŁÓW (TUTAJ WPISZ ID SWOICH KANAŁÓW)
# =========================================================

KANALY_SWIATOW = {

    1518327717676716162: "1",

    1518327790703874139: "2",

    1518327805081817160: "3",

    1518327819292250254: "4",

}


# =========================================================
# 2. BAZA DANYCH
# =========================================================
def init_db():
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS nieobecnosci (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            swiat TEXT NOT NULL,
            nick TEXT NOT NULL,
            data_wpisu DATETIME NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def wyczysc_stare_wpisy():
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    siedem_dni_temu = datetime.now() - timedelta(days=7)
    cursor.execute(
        "DELETE FROM nieobecnosci WHERE data_wpisu < ?", (siedem_dni_temu,)
    )
    conn.commit()
    conn.close()


# =========================================================
# 3. SILNIK OCR (CZYTANIE ZDJĘĆ)
# =========================================================
def analizuj_screen(image_path):
    reader = easyocr.Reader(["pl", "en"])
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
            # Odcina poziom, np. "FeedErik (Poziom 439)" zostawia "FeedErik"
            nick = re.split(r"\(", text_clean)[0].strip()

            if len(nick) > 2:
                nieobecni.append(nick)

    return nieobecni


# =========================================================
# 4. GŁÓWNY KOD BOTA
# =========================================================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Zalogowano pomyślnie jako: {bot.user.name}")
    init_db()
    czyszczenie_bazy.start()


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.channel.id in KANALY_SWIATOW and message.attachments:
        swiat = KANALY_SWIATOW[message.channel.id]
        attachment = message.attachments[0]

        if attachment.filename.lower().endswith((".png", ".jpg", ".jpeg")):
            potwierdzenie = await message.channel.send(
                "🔄 Przetwarzam raport..."
            )

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
                            (swiat, nick, teraz),
                        )
                    conn.commit()
                    conn.close()

                    lista_graczy = ", ".join([f"**{n}**" for n in nieobecni])
                    await potwierdzenie.edit(
                        content=f"✅ Zapisano minusy (**{swiat}**)\n❌ Brak rejestracji: {lista_graczy}"
                    )
                else:
                    await potwierdzenie.edit(
                        content=f"ℹ️ Świat **{swiat}**: Wszyscy zarejestrowani!"
                    )

            except Exception as e:
                await potwierdzenie.edit(content="❌ Błąd analizy zdjęcia.")
                print(f"Błąd OCR: {e}")

            finally:
                if os.path.exists(file_path):
                    os.remove(file_path)

    await bot.process_commands(message)


@bot.command(name="stan")
async def sprawdz_stan(ctx):
    wyczysc_stare_wpisy()

    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT swiat, nick, COUNT(*) as kary 
        FROM nieobecnosci 
        GROUP BY swiat, nick 
        ORDER BY swiat, kary DESC
    """)
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        await ctx.send("😇 Wszyscy pilnie klikają walki. Brak minusów z 7 dni!")
        return

    raport = "📋 **Nieobecności z ostatnich 7 dni:**\n"
    aktualny_swiat = ""

    for swiat, nick, kary in rows:
        if swiat != aktualny_swiat:
            aktualny_swiat = swiat
            raport += f"\n🌍 **Świat {aktualny_swiat}:**\n"
        raport += f"▫️ {nick} : {kary}x ❌\n"

    await ctx.send(raport)


@tasks.loop(hours=24)
async def czyszczenie_bazy():
    wyczysc_stare_wpisy()


# =========================================================
# 5. URUCHOMIENIE (TUTAJ WKLEJ TOKEN BOTA W CIAPKI)
# =========================================================
import os
# ... reszta importów ...

# Zamiast wklejania tokena, użyj tego:
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# A na dole w funkcji startowej:
bot.run(TOKEN)
