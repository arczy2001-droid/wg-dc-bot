import discord
from discord.ext import commands
import os
import pytesseract
import sqlite3
import re
from datetime import datetime
from PIL import Image, ImageEnhance
import asyncio
import difflib

TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# ... (Funkcje init_db, bazodanowe i OCR zostają bez zmian, kopiuj je z poprzedniej wersji) ...

# --- KONFIGURACJA BOTA Z WŁASNĄ POMOCĄ ---
intents = discord.Intents.default()
intents.message_content = True

# Definiujemy bota z własną funkcją help
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

@bot.event
async def on_ready():
    init_db()
    print(f"Zalogowano: {bot.user.name}")

# Obsługa komend ! lub !wg (wyświetlanie pomocy)
@bot.event
async def on_message(message):
    if message.author.bot: return
    
    # Obsługa "pustej" komendy ! lub !wg
    if message.content.strip() in ["!", "!wg"]:
        await show_help(message.channel)
        return
        
    await bot.process_commands(message)

async def show_help(channel):
    help_text = """
### 🤖 Lista komend WG-BOT:
`!wg_root` - konfiguruje kanał dowodzenia.
`!wg_add_world <nazwa> <#kanał>` - dodaje świat i przypisuje kanał.
`!wg_delete_world <nazwa>` - usuwa świat.
`!wg_worlds` - wyświetla listę światów.
`!wg_add_member <nazwa_świata> <lista>` - dodaje graczy do bazy.
`!wg_delete_member <nazwa_świata> <nick>` - usuwa gracza ze składu.
`!wg_member_list <nazwa_świata>` - wyświetla skład gildii.
`!wg <nazwa_świata>` - raport (wymaga screena).
`!wg_absent_list <nazwa_świata>` - wyświetla tabelę minusów.
`!wg_del_raport <nazwa_świata>` - cofa ostatni raport.
`!wg_add_absent <nazwa_świata> <nick>` - dodaje ręcznie minus.
`!wg_del_absent <nazwa_świata> <nick>` - usuwa minus.
    """
    await channel.send(help_text)

# --- KOMENDY Z NOWYMI NAZWAMI ---

@bot.command(name="wg_root")
@commands.has_permissions(administrator=True)
async def cmd_root(ctx):
    ustaw_dowodzenie_db(ctx.channel.id)
    await ctx.send("🎯 Ten kanał jest od teraz Centrum Dowodzenia.")

@bot.command(name="wg_add_world")
@commands.has_permissions(administrator=True)
async def cmd_add_world(ctx, nazwa: str, kanal: discord.TextChannel):
    dodaj_swiat_db(nazwa, kanal.id)
    await ctx.send(f"🌍 Świat **{nazwa.upper()}** przypisany do {kanal.mention}")

@bot.command(name="wg_delete_world")
@commands.has_permissions(administrator=True)
async def cmd_del_world(ctx, nazwa: str):
    usun_swiat_db(nazwa)
    await ctx.send(f"🗑️ Usunięto świat **{nazwa.upper()}**.")

@bot.command(name="wg_worlds")
@commands.has_permissions(administrator=True)
async def cmd_worlds(ctx):
    swiaty = pobierz_swiaty_db()
    msg = "📋 **Światy:**\n" + "\n".join([f"• {n.upper()} -> <#{k}>" for n, k in swiaty.items()])
    await ctx.send(msg)

@bot.command(name="wg_add_member")
@tylko_na_kanale_dowodzenia()
async def cmd_add_member(ctx, swiat: str, *, args: str):
    # Logika dodawania z poprzedniego kodu...
    pass

@bot.command(name="wg_delete_member")
@tylko_na_kanale_dowodzenia()
async def cmd_del_member(ctx, swiat: str, *, nick: str):
    # Logika usuwania z poprzedniego kodu...
    pass

@bot.command(name="wg_member_list")
@tylko_na_kanale_dowodzenia()
async def cmd_list(ctx, swiat: str):
    # Logika listowania z poprzedniego kodu...
    pass

@bot.command(name="wg")
@tylko_na_kanale_dowodzenia()
async def cmd_raport(ctx, nazwa_swiata: str):
    # Logika raportowania z poprzedniego kodu...
    pass

@bot.command(name="wg_absent_list")
@tylko_na_kanale_dowodzenia()
async def cmd_stan(ctx, nazwa: str):
    # Logika !stan z poprzedniego kodu...
    pass

@bot.command(name="wg_del_raport")
@tylko_na_kanale_dowodzenia()
async def cmd_del_raport(ctx, nazwa: str):
    # Logika !cofnij_raport z poprzedniego kodu...
    pass

@bot.command(name="wg_add_absent")
@tylko_na_kanale_dowodzenia()
async def cmd_add_absent(ctx, swiat: str, *, nick: str):
    # Logika !dodaj_minus z poprzedniego kodu...
    pass

@bot.command(name="wg_del_absent")
@tylko_na_kanale_dowodzenia()
async def cmd_del_absent(ctx, swiat: str, *, nick: str):
    # Logika !usun_minus z poprzedniego kodu...
    pass

bot.run(TOKEN)
