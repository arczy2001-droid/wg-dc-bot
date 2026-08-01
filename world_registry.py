"""
world_registry.py
==================
Single source of truth for turning ANY user-typed world identifier —
shorthand ("EU20"), regional code ("PL1"), or a full domain
("s20.sfgame.eu") — into one canonical, connectable domain string, used
consistently across main.py (absence tracking), attack_alert.py (alerts),
and sf_auth.py (actual game login).

WHY THIS EXISTS / WHAT IT DELIBERATELY DOES NOT DO:
    Shakes & Fidget's real domain scheme is NOT a clean formula:
      - Regular numbered worlds follow a stable sN.sfgame.<tld> pattern
        (confirmed by this project's own Cargo.toml example, s20.sfgame.eu)
        — this part IS derived algorithmically below.
      - Fusion/merged worlds do NOT follow a derivable pattern. Playa Games
        has used f1.sfgame.net, s1.sfgame.us, s1.sfgame.mx, and plain human
        names like "Blackforest" for fusion results — sometimes changing
        which scheme is used between one fusion and the next. There is no
        formula that reliably predicts a fusion world's real domain.
      - As of Oct 2023, S&F's client no longer even exposes per-world URLs
        in the browser (everything redirects through sfgame.net/play), so
        there's no way to algorithmically verify a guess against the live
        site either.
    Because this domain is fed directly into a real login attempt with a
    real password (via sf_probe.rs / sf-api), guessing wrong here isn't a
    cosmetic bug — it risks sending credentials to an unintended host. So:
    fusion/custom worlds are NEVER guessed. They only work once added to
    WORLD_ALIASES below, by hand, by whoever maintains this file.

HOW TO ADD A NEW SERVER / ALIAS:
    Just add a line to the WORLD_ALIASES dict below and redeploy — no
    database, no bot command, no restart-order dependency. Keys are
    matched case-insensitively with all whitespace stripped (so "F8",
    "f8", and "f 8" are all the same entry — write the key in whichever
    form is easiest to read).

INTEGRATION:
    Every slash-command parameter that accepts a world identifier should be
    typed as `app_commands.Transform[str, WorldTransformer]` instead of
    `str` — this guarantees resolution happens BEFORE the command body ever
    runs, for every command, without relying on each command remembering to
    call resolve_server_domain() itself. The one exception is SFLoginModal
    in sf_auth.py (a discord.ui.Modal field, not a slash-command option,
    which Transform doesn't cover) — that one calls resolve_server_domain()
    directly inside on_submit().

    from world_registry import WorldTransformer, resolve_server_domain, WorldResolutionError

    @app_commands.command(...)
    async def some_command(interaction: discord.Interaction,
                            swiat: app_commands.Transform[str, WorldTransformer]):
        ...  # `swiat` is ALREADY the canonical domain by the time this runs
"""

import re
import sqlite3
from typing import Optional

import discord
from discord import app_commands

DB_PATH = "gildia.db"

_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*\.[a-z]{2,}(\.[a-z]{2,})?$", re.IGNORECASE)
_REGIONAL_RE = re.compile(r"^([a-z]{2})(\d{1,3})$", re.IGNORECASE)

# Only regions with a long-standing, stable sN.sfgame.<tld> pattern. NOT
# exhaustive — deliberately conservative. Anything not listed here (or any
# fusion/custom world) needs an entry in WORLD_ALIASES below instead of
# being guessed.
_REGION_TLD = {"eu": "eu", "de": "de", "pl": "pl", "us": "us", "mx": "mx", "fr": "fr", "it": "it"}

# ---------------------------------------------------------------------------
# EDIT THIS BY HAND when a new fusion/custom-named world needs shorthand
# support, or when a regional world doesn't follow the sN.sfgame.<tld>
# pattern below. Keys are normalized (lowercased, whitespace stripped)
# before lookup — write them however's readable.
#
#   "shorthand people will type": "real.connectable.domain",
#
# Example entries (replace with your guild's actual worlds):
#   "f8":              "f8.sfgame.net",
#   "stumble steppe":  "stumblesteppe.sfgame.net",
#   "blackforest":     "blackforest.sfgame.net",
# ---------------------------------------------------------------------------
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
    "eu20a": "s20.sfgame.eu",
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
    """Raised whenever a world identifier can't be resolved to a canonical,
    connectable domain. The message is written to be shown to the user
    as-is (see on_app_command_error in main.py)."""
    pass


def _normalize_alias(raw: str) -> str:
    """Lowercased, internal whitespace collapsed away entirely — so 'F8',
    'f8', and 'f 8' are all the same lookup key, matching how 'EU20' and
    'eu20' are already meant to be interchangeable."""
    return re.sub(r"\s+", "", raw.strip().lower())


def resolve_server_domain(guild_id: Optional[int], server_input: str) -> str:
    """
    Resolves user input to a canonical connectable domain. Resolution order:

      1. Already looks like a full domain -> pass through, lowercased.
         Covers s20.sfgame.eu, f8.sfgame.net, blackforest.sfgame.net, or
         literally anything else typed in full, INCLUDING fusion/custom
         worlds — those just have to be typed in full at least once (or
         added to WORLD_ALIASES so shorthand keeps working after that).
      2. WORLD_ALIASES lookup (hand-maintained, top of this file). This is
         how fusion/custom worlds get shorthand support, and how you can
         override the regional guess below if a world doesn't follow it.
      3. Regional numeric shorthand (EU20, PL1, DE5...) -> derived via the
         stable sN.sfgame.<tld> pattern ONLY (see module docstring for why
         this is the only pattern trusted to be derived, not guessed).
      4. Nothing matched -> WorldResolutionError, telling the person this
         world needs to be added to WORLD_ALIASES rather than guessed.
    """
    if guild_id is None:
        raise WorldResolutionError("❌ World identifiers can only be resolved inside a server.")

    normalized = _normalize_alias(server_input)
    if not normalized:
        raise WorldResolutionError("❌ World name can't be empty.")

    if _DOMAIN_RE.match(normalized):
        return normalized

    if normalized in _WORLD_ALIASES_NORMALIZED:
        return _WORLD_ALIASES_NORMALIZED[normalized]

    m = _REGIONAL_RE.match(normalized)
    if m:
        region, number = m.group(1).lower(), m.group(2)
        tld = _REGION_TLD.get(region)
        if tld:
            return f"s{number}.sfgame.{tld}"

    raise WorldResolutionError(
        f"❌ `{server_input}` isn't a recognized world. If this is a fusion or "
        f"custom-named world, it needs to be added to WORLD_ALIASES in "
        f"world_registry.py — its real domain can't be guessed automatically."
    )


class WorldTransformer(app_commands.Transformer):
    """Attach via app_commands.Transform[str, WorldTransformer] on any slash
    command parameter that accepts a world identifier. Resolution happens
    before the command body runs, so the command itself always receives an
    already-canonical domain — see module docstring."""

    async def transform(self, interaction: discord.Interaction, value: str) -> str:
        return resolve_server_domain(interaction.guild_id, value)


def world_exists(guild_id: int, world_name: str) -> bool:
    """Whether `world_name` (already-canonical, e.g. after resolve_server_domain)
    is registered in `swiaty` for this guild."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT 1 FROM swiaty WHERE guild_id=? AND nazwa=?", (str(guild_id), world_name.lower())
    ).fetchone()
    conn.close()
    return row is not None


async def world_alias_autocomplete(interaction: discord.Interaction, current: str):
    """Suggests from the COMPLETE WORLD_ALIASES list — for commands where
    someone is picking any real server, e.g. registering a new one with
    /gt_world_add. Not limited to worlds already set up on this guild.

    Discord caps autocomplete results at 25 regardless of how many total
    aliases exist — this filters by what's typed so far rather than
    truncating the list, so all 40+ entries stay reachable by typing.
    """
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
    """Suggests from `swiaty` — worlds already registered on THIS guild.
    Used by every command that operates on an existing world (adding
    members, submitting reports, etc.) rather than registering a new one;
    typing a world nobody's added yet wouldn't do anything useful there."""
    if interaction.guild_id is None:
        return []
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT nazwa FROM swiaty WHERE guild_id=? AND nazwa LIKE ? ORDER BY nazwa LIMIT 25",
        (str(interaction.guild_id), f"%{_normalize_alias(current)}%")
    ).fetchall()
    conn.close()
    return [app_commands.Choice(name=r[0], value=r[0]) for r in rows]


# ---------------------------------------------------------------------------
# STARTUP MIGRATION — folds pre-existing free-form world labels (e.g. the
# literal string "eu20", stored back when this bot had no domain resolver)
# into their canonical domain, across EVERY table that keys on
# (guild_id, world). Safe to call on every startup: idempotent, and picks
# up newly-added WORLD_ALIASES entries (for previously-unresolvable
# fusion/custom worlds) on the next deploy without needing a special flag.
# ---------------------------------------------------------------------------

# (table, world_column, extra_primary_key_columns_or_None)
#   extra_primary_key_columns == None   -> table has no PK on this column,
#       a plain UPDATE can't collide, used as-is.
#   extra_primary_key_columns == [...]  -> table's PK includes the world
#       column (+ these other columns), so a rename could collide with an
#       existing canonical row; handled row-by-row with a merge instead of
#       a blind UPDATE (see _migrate_table_rows).
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
        return  # module owning this table hasn't been loaded/migrated to it yet — skip harmlessly

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
            # A canonical-named row already exists for this key — merging
            # would violate the PK, so the old-alias duplicate is dropped
            # in favor of the already-existing canonical row.
            conn.execute(
                f"DELETE FROM {table} WHERE guild_id=? AND {world_col}=? AND {where_extra}",
                (guild_id, old_name, *extra_vals)
            )
        else:
            conn.execute(f"UPDATE {table} SET {world_col}=? WHERE rowid=?", (new_name, row_dict["rowid"]))


def migrate_world_identifiers() -> None:
    """Call once at startup (after every module's own init_*_table() has
    already run, so all tables in _MIGRATION_TARGETS exist)."""
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
            print(f"world_registry: '{old_name}' (guild {guild_id}) has no WORLD_ALIASES entry yet — "
                  f"left as-is. Add one in world_registry.py to migrate it.")
            continue

        if new_name != old_name:
            for table, world_col, pk_extra in _MIGRATION_TARGETS:
                _migrate_table_rows(conn, table, world_col, pk_extra, str(guild_id), old_name, new_name)
            migrated += 1

    conn.commit()
    conn.close()
    if migrated or skipped:
        print(f"world_registry: migration pass complete — {migrated} world(s) renamed to canonical "
              f"domains, {skipped} pending a WORLD_ALIASES entry.")
