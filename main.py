import discord
from discord.ext import commands
import os
import pytesseract
import sqlite3
import re
from datetime import datetime
from PIL import Image, ImageEnhance
import asyncio

# 1. KONFIGURACJA — TWOJE KANAŁY
KANALY_SWIATOW = {
    1518327717676716162: "Swiat1",
    1518327790703874139: "Swiat2",
    1518327805081817160: "Swiat3",
    1518327819292250254: "Swiat4"
}

TOKEN = os.getenv("DISCORD_BOT_TOKEN")

def init_db():
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS nieobecnosci 
        (swiat TEXT, nick TEXT, data_wpisu TIMESTAMP)
    """)
    conn.commit()
    conn.close()

# 3. PROFESJONALNY PRE-PROCESSING OBRAZU DLA TESSERACTA
def _blokujaca_analiza_tesseract(image_path):
    try:
        with Image.open(image_path) as img:
            # A. Wymuszenie stałej szerokości 1600px (idealna wielkość czcionki dla OCR)
            target_width = 1600
            w_percent = (target_width / float(img.width))
            target_height = int((float(img.height) * float(w_percent)))
            img = img.resize((target_width, target_height), Image.Resampling.LANCZOS)

            # B. Skala szarości + Ekstremalny Kontrast (2.5x)
            # Zabija tło pergaminu do zera i wymazuje ikonki klas postaci
            img = img.convert("L")
            enhancer = ImageEnhance.Contrast(img)
            img = enhancer.enhance(2.5)

            # C. PSM 6: Wymuszenie czytania linijka pod linijką w dół
            text = pytesseract.image_to_string(img, lang="pol+eng", config="--psm 6")
            return text.splitlines()
    except Exception as e:
        print(f"Błąd ocr: {e}")
        return []

async def analizuj_screen_async(image_path):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _blokujaca_analiza_tesseract, image_path)

# 4. GŁÓWNY KOD BOTA
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Zalogowano pomyślnie jako: {bot.user.name}")
    init_db()

@bot.command(name="stan")
async def stan(ctx):
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("SELECT swiat, nick, COUNT(*) FROM nieobecnosci GROUP BY swiat, nick")
    rows = cursor.fetchall()
    conn.close()
    
    if not rows:
        await ctx.send("📋 Baza danych jest pusta. Brak zapisanych nieobecności.")
        return
        
    raport = "📊 **Aktualny stan minusów (brak rejestracji):**\n"
    for swiat, nick, ilosc in rows:
        raport += f"• [{swiat}] **{nick}**: {ilosc}x ❌\n"
    await ctx.send(raport)

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.channel.id in KANALY_SWIATOW and message.attachments:
        swiat = KANALY_SWIATOW[message.channel.id]
        attachment = message.attachments[0]

        if attachment.filename.lower().endswith((".png", ".jpg", ".jpeg")):
            potwierdzenie = await message.channel.send("🔄 Przetwarzam raport...")
            file_path = f"temp_{attachment.filename}"
            await attachment.save(file_path)

            try:
                raw_lines = await analizuj_screen_async(file_path)
                
                nieobecni = []
                w_sekcji_nieobecnych = False

                for line in raw_lines:
                    text_clean = line.strip()
                    if not text_clean:
                        continue
                        
                    if "Niezarejestrowani" in text_clean:
                        w_sekcji_nieobecnych = True
                        continue
                    if "Zarejestrowani" in text_clean or "USUŃ" in text_clean:
                        w_sekcji_nieobecnych = False
                        break
                        
                    if w_sekcji_nieobecnych:
                        surowy_nick = re.split(r"\(", text_clean)[0].strip()
                        
                        # ULEPSZONY EGZORCYSTA: wyłapuje dwuliterowe duchy z ikon (np. 'CA', '47', 'II')
                        czlony = surowy_nick.split()
                        if len(czlony) > 1:
                            p = czlony[0]
                            if len(p) <= 2 and (p.isupper() or p.isdigit() or not p.isalnum()):
                                surowy_nick = " ".join(czlony[1:])

                        if len(surowy_nick) > 2:
                            nieobecni.append(surowy_nick)

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

    await bot.process_commands(message)

bot.run(TOKEN)
            
