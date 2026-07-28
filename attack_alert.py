"""
attack_alert.py
================
Per-world attack alert configuration.

PERMISSION MODEL:
    /gt_attack_setup — requires Manage Server (consistent with other per-world
                    setup commands like /gt_world_add in the main bot).
                    Also carries an optional mute_defense flag, letting an
                    admin suppress automated defense-alert pings for a world
                    (used by sf_auth.py's SFMonitor) in the same command.

NOTE: the standalone /attack (manual trigger) and /gt_sf_toggle (per-kind
channel routing) commands have been removed. attack_config (channel/role)
and world_notify_config (per-kind channel + mute_defense) still exist as
tables — get_notify_config()/_upsert_notify_config() remain here because
sf_auth.py's SFMonitor imports get_notify_config() directly to route its
automated alerts. Only mute_defense is exposed again, via /gt_attack_setup;
attack_channel_id/defense_channel_id in world_notify_config now have no
command that sets them, so SFMonitor always falls back to attack_config's
single channel for both attack and defense alerts.

INTEGRATION (in gildia_bot.py):

    from attack_alert import (
        init_attack_alert_table,
        gt_attack_setup,
    )

    # in setup_hook, before tree.sync():
    init_attack_alert_table()
    self.tree.add_command(gt_attack_setup)
"""

import sqlite3
from typing import Optional

import discord
from discord import app_commands

from i18n import translator
from world_registry import WorldTransformer, registered_world_autocomplete

DB_PATH = "gildia.db"


# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------

def init_attack_alert_table() -> None:
    """Idempotent — safe to call on every startup."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS attack_config (
            guild_id   TEXT NOT NULL,
            world_name TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            role_id    TEXT NOT NULL,
            PRIMARY KEY (guild_id, world_name)
        )
    """)

    # Per-world routing for the AUTOMATED hourly monitor (sf_auth.py's
    # SFMonitor) — separate attack/defense channels, and a mute flag for
    # defense pings. Distinct from attack_config above, which is for the
    # manual /gt_attack trigger command (single channel + role).
    # Both are consulted when a battle is detected: SFMonitor uses
    # world_notify_config's channels if set, falling back to attack_config's
    # channel otherwise, and always uses attack_config's role_id for pings
    # (no separate role field here — reuses what /gt_attack_setup configured).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS world_notify_config (
            guild_id            TEXT NOT NULL,
            world_name          TEXT NOT NULL,
            attack_channel_id   TEXT,
            defense_channel_id  TEXT,
            mute_defense        INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, world_name)
        )
    """)
    conn.commit()
    conn.close()


def _get_world_config(guild_id: int, world_name: str) -> Optional[tuple[str, str]]:
    """Returns (channel_id, role_id) for this world, or None if unconfigured."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT channel_id, role_id FROM attack_config WHERE guild_id=? AND world_name=?",
        (str(guild_id), world_name.lower())
    ).fetchone()
    conn.close()
    return (row[0], row[1]) if row else None


def _save_world_config(guild_id: int, world_name: str, channel_id: int, role_id: int) -> None:
    """Overwrites any existing config for this (guild, world) pair."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO attack_config (guild_id, world_name, channel_id, role_id)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(guild_id, world_name) DO UPDATE SET
               channel_id = excluded.channel_id,
               role_id = excluded.role_id""",
        (str(guild_id), world_name.lower(), str(channel_id), str(role_id))
    )
    conn.commit()
    conn.close()



# ---------------------------------------------------------------------------
# Shared helpers — used by /gt_attack_setup below and by sf_auth.py's
# SFMonitor (which imports get_notify_config directly).
#
# NOTE: world existence checks and world_name autocomplete used to be
# defined locally here, but were never actually called (world_exists was
# dead code) and duplicated logic that now lives centrally in
# world_registry.py (world_exists / registered_world_autocomplete) so
# main.py can share the exact same behavior instead of every file
# reimplementing its own "worlds registered on this guild" query.
# ---------------------------------------------------------------------------

def get_notify_config(guild_id: int, world_name: str) -> Optional[dict]:
    """Public — sf_auth.py's SFMonitor calls this to route alerts."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        """SELECT attack_channel_id, defense_channel_id, mute_defense
           FROM world_notify_config WHERE guild_id=? AND world_name=?""",
        (str(guild_id), world_name.lower())
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "attack_channel_id": row[0],
        "defense_channel_id": row[1],
        "mute_defense": bool(row[2]),
    }


def _upsert_notify_config(guild_id: int, world_name: str, *,
                          attack_channel_id: Optional[int] = None,
                          defense_channel_id: Optional[int] = None,
                          mute_defense: Optional[bool] = None) -> dict:
    """
    PARTIAL UPDATE: only overwrites the columns the caller actually passed a
    value for (not None). If a row doesn't exist yet, missing fields default
    to NULL/0 rather than being required. This is why we read-then-merge
    instead of a single ON CONFLICT ... DO UPDATE SET x=excluded.x — that
    pattern would blank out a previously-set channel if this call only
    intended to flip mute_defense.

    Returns the final merged config as a dict.
    """
    conn = sqlite3.connect(DB_PATH)
    existing = conn.execute(
        """SELECT attack_channel_id, defense_channel_id, mute_defense
           FROM world_notify_config WHERE guild_id=? AND world_name=?""",
        (str(guild_id), world_name.lower())
    ).fetchone()

    merged_attack = str(attack_channel_id) if attack_channel_id is not None else (existing[0] if existing else None)
    merged_defense = str(defense_channel_id) if defense_channel_id is not None else (existing[1] if existing else None)
    merged_mute = int(mute_defense) if mute_defense is not None else (existing[2] if existing else 0)

    conn.execute(
        """INSERT INTO world_notify_config (guild_id, world_name, attack_channel_id, defense_channel_id, mute_defense)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(guild_id, world_name) DO UPDATE SET
               attack_channel_id  = excluded.attack_channel_id,
               defense_channel_id = excluded.defense_channel_id,
               mute_defense       = excluded.mute_defense""",
        (str(guild_id), world_name.lower(), merged_attack, merged_defense, merged_mute)
    )
    conn.commit()
    conn.close()
    return {"attack_channel_id": merged_attack, "defense_channel_id": merged_defense, "mute_defense": bool(merged_mute)}


# ---------------------------------------------------------------------------
# /gt_attack_setup — admin-only config command
# (mute_defense merged in here from the removed /gt_sf_toggle command;
# attack_channel/defense_channel per-kind routing has no command anymore —
# see the module docstring.)
# ---------------------------------------------------------------------------

@app_commands.command(name="gt_attack_setup", description="Configure the alert channel and ping role for a world's attack notifications.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.autocomplete(world_name=registered_world_autocomplete)
@app_commands.describe(
    world_name="World name (e.g. eu20)",
    channel="Channel where attack alerts for this world will be posted",
    ping_role="Role to ping when an attack alert is triggered for this world",
    mute_defense="Suppress automated defense-alert pings for this world (leave unset to keep current)",
)
async def gt_attack_setup(
    interaction: discord.Interaction,
    world_name: app_commands.Transform[str, WorldTransformer],
    channel: discord.TextChannel,
    ping_role: discord.Role,
    mute_defense: Optional[bool] = None,
):
    _save_world_config(interaction.guild_id, world_name, channel.id, ping_role.id)

    mute_line = ""
    if mute_defense is not None:
        _upsert_notify_config(interaction.guild_id, world_name, mute_defense=mute_defense)
        mute_line = f"\n🔇 Defense alert pings for this world: **{'muted' if mute_defense else 'unmuted'}**."

    await interaction.response.send_message(
        f"✅ Attack alerts for **{world_name.upper()}** will now be posted in {channel.mention} "
        f"and ping {ping_role.mention}.{mute_line}",
        ephemeral=True,
    )


@gt_attack_setup.error
async def gt_attack_setup_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ You need **Manage Server** permission for this.", ephemeral=True)
    else:
        print(f"gt_attack_setup error: {error}")
        await interaction.response.send_message("❌ Something went wrong.", ephemeral=True)
