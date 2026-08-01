import re
import sqlite3
from typing import Optional

import discord
from discord import app_commands

DB_PATH = "gildia.db"

# Regexy do walidacji domen i formatów regionalnych
_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*\.[a-z]{2,}(\.[a-z]{2,})?$", re.IGNORECASE)
_REGIONAL_RE = re.compile(r"^([a-z]{2})(\d{1,3})$", re.IGNORECASE)

# Obsługiwane regiony dla automatycznego zgadywania (sN.sfgame.<tld>)
_REGION_TLD = {"eu": "eu", "de": "de", "pl": "pl", "us": "us", "mx": "mx", "fr": "fr", "it": "it"}

WORLD_ALIASES: dict[str, str] = {
    "f8": "f8.sfgame.net",
    "f9": "f9.sfgame.net",
    "f11": "f11.sfgame.net",
    "f14": "f14.sfgame.net",
    "f17": "f17.sfgame.net",
    "f19": "f19.sfgame.net",
    "f21": "f21.sfgame.net",
    "f22": "f22.sfgame.net",
    "f23": "f23.sfgame.net",
    "f24": "f24.sfgame.net",
    "f25": "f25.sfgame.net",
    "f26": "f26.sfgame.net",
    "maerwynn": "maerwynn.sfgame.net",
    "f27": "f27.sfgame.net",
    "black forest": "blackforest.sfgame.net",
    "gnarogrim": "gnarogrim.sfgame.net",
    "f28": "f28.sfgame.net",
    "stumble steppe": "stumblesteppe.sfgame.net",
    "am1": "am1.sfgame.net",
    "eu5": "s5.sfgame.eu",
    "eu6": "s6.sfgame.eu",
    "eu7": "s7.sfgame.eu",
    "eu8": "s8.sfgame.eu",
    "eu9": "s9.sfgame.eu",
    "eu10": "s10.sfgame.eu",
    "eu11": "s11.sfgame.eu",
    "eu12": "s12.sfgame.eu",
    "eu13": "s13.sfgame.eu",
    "eu14": "s14.sfgame.eu",
    "eu15": "s15.sfgame.eu",
    "eu16": "s16.sfgame.eu",
    "eu17": "s17.sfgame.eu",
    "eu18": "s18.sfgame.eu",
    "eu19": "s19.sfgame.eu",
    "eu20": "s20.sfgame.eu",
    "eu20a": "s20a.sfgame.eu",
    "eu21": "s21.sfgame.eu",
    "eu22": "s22.sfgame.eu",
    "eu23": "s23.sfgame.eu",
    "eu24": "s24.sfgame.eu",
    "eu25": "s25.sfgame.eu",
    "eu26": "s26.sfgame.eu",
    "eu27": "s27.sfgame.eu",
    "eu28": "s28.sfgame.eu",
    "eu29": "s29.sfgame.eu",
    "eu30": "s30.sfgame.eu",
}

_WORLD_ALIASES_NORMALIZED = {
    re.sub(r"\s+", "", k.strip().lower()): v.strip().lower() for k, v in WORLD_ALIASES.items()
}

class WorldResolutionError(app_commands.AppCommandError):
    """Błąd zgłaszany, gdy świat nie może zostać rozwiązany do kanonicznej domeny."""
    pass

def _normalize_alias(raw: str) -> str:
    """Normalizuje nazwę świata do klucza wyszukiwania."""
    return re.sub(r"\s+", "", raw.strip().lower())

def resolve_server_domain(guild_id: Optional[int], server_input: str) -> str:
    """Rozwiązuje wejście użytkownika na kanoniczną domenę serwera."""
    if guild_id is None:
        raise WorldResolutionError("❌ Identyfikatory światów mogą być rozwiązywane tylko w obrębie serwera.")

    normalized = _normalize_alias(server_input)
    if not normalized:
        raise WorldResolutionError("❌ Nazwa świata nie może być pusta.")

    # 1. Sprawdź, czy to już pełna domena
    if _DOMAIN_RE.match(normalized):
        return normalized

    # 2. Sprawdź aliasy
    if normalized in _WORLD_ALIASES_NORMALIZED:
        return _WORLD_ALIASES_NORMALIZED[normalized]

    # 3. Sprawdź format regionalny (np. eu20)
    m = _REGIONAL_RE.match(normalized)
    if m:
        region, number = m.group(1).lower(), m.group(2)
        tld = _REGION_TLD.get(region)
        if tld:
            return f"s{number}.sfgame.{tld}"

    raise WorldResolutionError(
        f"❌ `{server_input}` nie jest rozpoznanym światem. Jeśli to świat typu fusion lub "
        f"o niestandardowej nazwie, musi zostać dodany do `WORLD_ALIASES` w `world_registry.py`."
    )

class WorldTransformer(app_commands.Transformer):
    """Transformer dla slash commands - automatycznie zamienia nazwę świata na domenę."""
    async def transform(self, interaction: discord.Interaction, value: str) -> str:
        return resolve_server_domain(interaction.guild_id, value)

def world_exists(guild_id: int, world_name: str) -> bool:
    """Sprawdza, czy świat jest zarejestrowany w bazie danych."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT 1 FROM swiaty WHERE guild_id=? AND nazwa=?", (str(guild_id), world_name.lower())
    ).fetchone()
    conn.close()
    return row is not None

async def world_alias_autocomplete(interaction: discord.Interaction, current: str):
    """Autouzupełnianie dla wszystkich znanych aliasów."""
    current_norm = _normalize_alias(current)
    matches = [
        (alias, domain) for alias, domain in WORLD_ALIASES.items()
        if current_norm in _normalize_alias(alias)
    ]
    return [
        app_commands.Choice(name=f"{alias} ({domain})"[:100], value=alias)
        for alias, domain in matches[:25]
    ]

async def registered_world_autocomplete(interaction: discord.Interaction, current: str):
    """Autouzupełnianie dla światów zarejestrowanych tylko na tym serwerze."""
    if interaction.guild_id is None:
        return []
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT nazwa FROM swiaty WHERE guild_id=? AND nazwa LIKE ? ORDER BY nazwa LIMIT 25",
        (str(interaction.guild_id), f"%{_normalize_alias(current)}%")
    ).fetchall()
    conn.close()
    return [app_commands.Choice(name=r[0], value=r[0]) for r in rows]

_MIGRATION_TARGETS = [
    ("swiaty", "nazwa", []),
    ("czlonkowie", "swiat", ["nick"]),
    ("nieobecnosci", "swiat", None),
    ("raporty", "swiat", None),
    ("ranking_schedule", "swiat", []),
    ("attack_config", "world_name", []),
    ("world_notify_config", "world_name", []),
    ("sf_accounts", "world_name", ["discord_user_id"]),
    ("sf_alert_state", "world_name", ["kind", "battle_date"]),
]

def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None

def _migrate_table_rows(conn: sqlite3.Connection, table: str, world_col: str,
                        pk_extra: Optional[list], guild_id: str, old_name: str, new_name: str) -> None:
    if not _table_exists(conn, table):
        return

    if pk_extra is None:
        conn.execute(f"UPDATE {table} SET {world_col}=? WHERE guild_id=? AND {world_col}=?",
                     (new_name, guild_id, old_name))
        return

    col_names = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    old_rows = conn.execute(
        f"SELECT rowid, * FROM {table} WHERE guild_id=? AND {world_col}=?", (guild_id, old_name)
    ).fetchall()

    for row in old_rows:
        row_dict = dict(zip(["rowid"] + col_names, row))
        if pk_extra:
            where_extra = " AND ".join(f"{c}=?" for c in pk_extra)
            extra_vals = [row_dict[c] for c in pk_extra]
            exists = conn.execute(
                f"SELECT 1 FROM {table} WHERE guild_id=? AND {world_col}=? AND {where_extra}",
                (guild_id, new_name, *extra_vals)
            ).fetchone()
        else:
            extra_vals = []
            where_extra = "1=1"
            exists = conn.execute(
                f"SELECT 1 FROM {table} WHERE guild_id=? AND {world_col}=?",
                (guild_id, new_name)
            ).fetchone()

        if exists:
            conn.execute(
                f"DELETE FROM {table} WHERE guild_id=? AND {world_col}=? AND {where_extra}",
                (guild_id, old_name, *extra_vals)
            )
        else:
            conn.execute(f"UPDATE {table} SET {world_col}=? WHERE rowid=?", (new_name, row_dict["rowid"]))

def migrate_world_identifiers() -> None:
    """Uruchom przy starcie bota, aby ujednolicić nazwy światów w bazie danych."""
    conn = sqlite3.connect(DB_PATH)
    if not _table_exists(conn, "swiaty"):
        conn.close()
        return

    rows = conn.execute("SELECT DISTINCT guild_id, nazwa FROM swiaty").fetchall()
    migrated, skipped = 0, 0
    for guild_id, old_name in rows:
        try:
            new_name = resolve_server_domain(int(guild_id), old_name)
        except (WorldResolutionError, ValueError):
            skipped += 1
            continue

        if new_name != old_name:
            for table, world_col, pk_extra in _MIGRATION_TARGETS:
                _migrate_table_rows(conn, table, world_col, pk_extra, str(guild_id), old_name, new_name)
            migrated += 1

    conn.commit()
    conn.close()
    if migrated or skipped:
        print(f"world_registry: Migracja zakończona — {migrated} światów ujednoliconych, {skipped} oczekuje na dodanie do WORLD_ALIASES.")
