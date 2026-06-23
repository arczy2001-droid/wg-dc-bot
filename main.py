import discord
from discord.ext import commands
import os
import pytesseract
import sqlite3
import re
from datetime import datetime
from PIL import Image, ImageEnhance
import asyncio

TOKEN = os.getenv("DISCORD_BOT_TOKEN")

def init_db():
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS nieobecnosci 
        (swiat TEXT, nick TEXT, data_wpisu TIMESTAMP)
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ustawienia 
        (klucz TEXT PRIMARY KEY, wartosc TEXT)
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS swiaty 
        (nazwa TEXT PRIMARY KEY, kanal_id INTEGER)
    """)
    conn.commit()
    conn.close()

# --- BAZA DANYCH: KONFIGURACJA ---
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

# --- SILNIK OCR ---
def _blokujaca_analiza_tesseract(image_path):
    try:
        with Image.open(image_path) as img:
            target_width = 1600
            w_percent = (target_width / float(img.width))
            target_height = int((float(img.height) * float(w_percent)))
            img = img.resize((target_width, target_height), Image.Resampling.LANCZOS)

            img = img.convert("L")
            enhancer = ImageEnhance.Contrast(img)
            img = enhancer.enhance(2.5)

            text = pytesseract.image_to_string(img, lang="pol+eng", config="--psm 6")
            return text.splitlines()
    except Exception as e:
        print(f"Błąd ocr: {e}")
        return []

async def analizuj_screen_async(image_path):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _blokujaca_analiza_tesseract, image_path)

# --- KONFIGURACJA DISCORDA ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    init_db()
    print(f"Zalogowano pomyślnie jako: {bot.user.name}")

def tylko_na_kanale_dowodzenia():
    async def predicate(ctx):
        kanal_dow = pobierz_dowodzenie_db()
        if not kanal_dow:
            await ctx.send("⚠️ **Błąd:** Bot nie ma przypisanego kanału dowodzenia! Użyj `!ustaw_dowodzenie`.")
            return False
        return ctx.channel.id == kanal_dow
    return commands.check(predicate)

# =====================================================================
#                  KOMENDY ADMINISTRACYJNE (PAROWANIE)
# =====================================================================

@bot.command(name="ustaw_dowodzenie")
@commands.has_permissions(administrator=True)
async def cmd_ustaw_dowodzenie(ctx):
    ustaw_dowodzenie_db(ctx.channel.id)
    await ctx.send(f"🎯 Ten kanał (`{ctx.channel.name}`) jest od teraz głównym Centrum Dowodzenia.")

@bot.command(name="dodaj_swiat")
@commands.has_permissions(administrator=True)
async def cmd_dodaj_swiat(ctx, nazwa_swiata: str, kanal_docelowy: discord.TextChannel):
    dodaj_swiat_db(nazwa_swiata, kanal_docelowy.id)
    await ctx.send(f"🌍 Skonfigurowano świat: **{nazwa_swiata.upper()}** -> {kanal_docelowy.mention}")

@bot.command(name="usun_swiat")
@commands.has_permissions(administrator=True)
async def cmd_usun_swiat(ctx, nazwa_swiata: str):
    usun_swiat_db(nazwa_swiata)
    await ctx.send(f"🗑️ Odpięto świat **{nazwa_swiata.upper()}**.")

@bot.command(name="swiaty")
@commands.has_permissions(administrator=True)
async def cmd_swiaty(ctx):
    swiaty = pobierz_swiaty_db()
    if not swiaty:
        await ctx.send("Brak skonfigurowanych światów.")
        return
    msg = "📋 **Lista powiązań:**\n" + "\n".join([f"• **{n.upper()}** -> <#{k}>" for n, k in swiaty.items()])
    await ctx.send(msg)

# =====================================================================
#                  KOMENDY OPERACYJNE (RAPORTY I BAZA)
# =====================================================================

@bot.command(name="raport")
@tylko_na_kanale_dowodzenia()
async def cmd_raport(ctx, nazwa_swiata: str):
    swiaty = pobierz_swiaty_db()
    nazwa_swiata = nazwa_swiata.lower()

    if nazwa_swiata not in swiaty:
        await ctx.send(f"❌ Nie znam świata `{nazwa_swiata}`.")
        return
    if not ctx.message.attachments:
        await ctx.send("❌ Załącz screenshot z gry!")
        return

    attachment = ctx.message.attachments[0]
    potwierdzenie = await ctx.send(f"🔄 Skanuję listę dla **{nazwa_swiata.upper()}**...")
    file_path = f"temp_{attachment.filename}"
    await attachment.save(file_path)

    try:
        raw_lines = await analizuj_screen_async(file_path)
        nieobecni = []
        w_sekcji = False

        for line in raw_lines:
            text_clean = line.strip()
            if not text_clean: continue
            if "Niezarejestrowani" in text_clean:
                w_sekcji = True
                continue
            if "Zarejestrowani" in text_clean or "USUŃ" in text_clean:
                break
                
            if w_sekcji:
                surowy_nick = re.split(r"\(", text_clean)[0].strip()
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
                cursor.execute("INSERT INTO nieobecnosci (swiat, nick, data_wpisu) VALUES (?, ?, ?)", (nazwa_swiata, nick, teraz))
            conn.commit()
            conn.close()

            kanal_swiata = bot.get_channel(swiaty[nazwa_swiata])
            lista_format = "\n".join([f"• **{n}**" for n in nieobecni])
            ogloszenie = f"🚨 **RAPORT BRAKU REJESTRACJ — {nazwa_swiata.upper()}** 🚨\nData: `{teraz.strftime('%d.%m.%Y %H:%M')}`\n\n{lista_format}"

            if kanal_swiata:
                await kanal_swiata.send(ogloszenie)
                await potwierdzenie.edit(content=f"✅ Zapisano i wysłano alert na {kanal_swiata.mention}")
            else:
                await potwierdzenie.edit(content="⚠️ Zapisano w bazie, ale docelowy kanał nie istnieje!")
        else:
            await potwierdzenie.edit(content=f"✅ **{nazwa_swiata.upper()}**: Wszyscy zarejestrowani!")

    except Exception as e:
        await potwierdzenie.edit(content=f"❌ Błąd: {str(e)}")
    finally:
        if os.path.exists(file_path): os.remove(file_path)

@bot.command(name="stan")
@tylko_na_kanale_dowodzenia()
async def cmd_stan(ctx, nazwa_swiata: str):
    swiaty = pobierz_swiaty_db()
    nazwa_swiata = nazwa_swiata.lower()
    
    if nazwa_swiata not in swiaty:
        await ctx.send(f"❌ Nie znam świata `{nazwa_swiata}`.")
        return

    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("SELECT nick, COUNT(*) FROM nieobecnosci WHERE swiat = ? GROUP BY nick ORDER BY COUNT(*) DESC", (nazwa_swiata,))
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        await ctx.send(f"🟢 **{nazwa_swiata.upper()}**: Brak jakichkolwiek minusów.")
        return

    raport = f"📊 **Tabela kar [{nazwa_swiata.upper()}]:**\n\n" + "\n".join([f"• **{nick}**: {ilosc}x ❌" for nick, ilosc in rows])
    await ctx.send(raport)

# =====================================================================
#                  NOWOŚĆ: KOREKTY RĘCZNE
# =====================================================================

@bot.command(name="cofnij_raport")
@tylko_na_kanale_dowodzenia()
async def cmd_cofnij_raport(ctx, nazwa_swiata: str):
    swiaty = pobierz_swiaty_db()
    nazwa_swiata = nazwa_swiata.lower()

    if nazwa_swiata not in swiaty:
        await ctx.send(f"❌ Nie znam świata `{nazwa_swiata}`.")
        return

    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    
    # Znajdź dokładny stempel czasowy ostatniego skanu na tym świecie
    cursor.execute("SELECT MAX(data_wpisu) FROM nieobecnosci WHERE swiat = ?", (nazwa_swiata,))
    ostatnia_data = cursor.fetchone()[0]

    if not ostatnia_data:
        conn.close()
        await ctx.send(f"⚠️ Na świecie **{nazwa_swiata.upper()}** nie ma żadnych raportów do cofnięcia.")
        return

    # Pobierz kogo usuwamy (tylko do podglądu w wiadomości zwrotnej)
    cursor.execute("SELECT nick FROM nieobecnosci WHERE swiat = ? AND data_wpisu = ?", (nazwa_swiata, ostatnia_data))
    usunięci_gracze = [r[0] for r in cursor.fetchall()]

    # Usuń całą paczkę z tą datą
    cursor.execute("DELETE FROM nieobecnosci WHERE swiat = ? AND data_wpisu = ?", (nazwa_swiata, ostatnia_data))
    conn.commit()
    conn.close()

    gracze_str = ", ".join(usunięci_gracze)
    dt_object = datetime.fromisoformat(ostatnia_data)
    
    await ctx.send(f"⏪ **Cofnięto raport** ze świata **{nazwa_swiata.upper()}** (z dnia `{dt_object.strftime('%d.%m %H:%M:%S')}`).\nAnulowano minusy graczom: **{gracze_str}**")

@bot.command(name="dodaj_minus")
@tylko_na_kanale_dowodzenia()
async def cmd_dodaj_minus(ctx, nazwa_swiata: str, *, nick_gracza: str):
    swiaty = pobierz_swiaty_db()
    nazwa_swiata = nazwa_swiata.lower()

    if nazwa_swiata not in swiaty:
        await ctx.send(f"❌ Nie ma takiego świata jak `{nazwa_swiata}`.")
        return

    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO nieobecnosci (swiat, nick, data_wpisu) VALUES (?, ?, ?)", (nazwa_swiata, nick_gracza.strip(), datetime.now()))
    conn.commit()

    # Sprawdź nowy stan gracza
    cursor.execute("SELECT COUNT(*) FROM nieobecnosci WHERE swiat = ? AND LOWER(nick) = LOWER(?)", (nazwa_swiata, nick_gracza.strip()))
    ile_ma = cursor.fetchone()[0]
    conn.close()

    await ctx.send(f"➕ Dodano ręczny minus graczowi **{nick_gracza}** na świecie **{nazwa_swiata.upper()}**.\n(Aktualnie posiada: `{ile_ma}x ❌`)")

@bot.command(name="usun_minus")
@tylko_na_kanale_dowodzenia()
async def cmd_usun_minus(ctx, nazwa_swiata: str, *, nick_gracza: str):
    swiaty = pobierz_swiaty_db()
    nazwa_swiata = nazwa_swiata.lower()
    nick_gracza = nick_gracza.strip()

    if nazwa_swiata not in swiaty:
        await ctx.send(f"❌ Nie ma takiego świata jak `{nazwa_swiata}`.")
        return

    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()

    # Znajdź ROWID najnowszego wpisu dla tego konkretnego gracza
    cursor.execute("""
        SELECT rowid FROM nieobecnosci 
        WHERE swiat = ? AND LOWER(nick) = LOWER(?) 
        ORDER BY data_wpisu DESC LIMIT 1
    """, (nazwa_swiata, nick_gracza))
    
    wiersz = cursor.fetchone()

    if not wiersz:
        conn.close()
        await ctx.send(f"🤷 Gracz **{nick_gracza}** nie ma żadnych minusów na świecie **{nazwa_swiata.upper()}**!")
        return

    docelowe_rowid = wiersz[0]
    cursor.execute("DELETE FROM nieobecnosci WHERE rowid = ?", (docelowe_rowid,))
    conn.commit()

    # Sprawdź ile mu zostało po usunięciu
    cursor.execute("SELECT COUNT(*) FROM nieobecnosci WHERE swiat = ? AND LOWER(nick) = LOWER(?)", (nazwa_swiata, nick_gracza))
    ile_zostalo = cursor.fetchone()[0]
    conn.close()

    await ctx.send(f"➖ Anulowano jeden minus graczowi **{nick_gracza}** na świecie **{nazwa_swiata.upper()}**.\n(Pozostało mu: `{ile_zostalo}x ❌`)")

bot.run(TOKEN)
    
