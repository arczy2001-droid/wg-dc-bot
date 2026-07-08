"""
attack_alert.py
================
Multi-World Guild Attack Alert system.

Deliberately minimal by design: no buttons, no tracking, no roster — just a
fast trigger that posts a themed embed and pings the right role for the
right world. Two commands, one table.

PERMISSION MODEL:
    /attack_setup — requires Manage Server (consistent with other per-world
                    setup commands like /wg_add_world in the main bot).
    /attack       — requires "administrators or configured officer roles."
                    Rather than build a second, parallel officer-role system
                    just for this feature, this reuses the bot-admin role
                    already stored in guild_config.admin_role (set via your
                    existing /setup wizard) — one role to configure per
                    server, not two. Server Administrator permission always
                    passes as well.

INTEGRATION (in gildia_bot.py):

    from attack_alert import (
        init_attack_alert_table,
        attack_setup,
        attack,
    )

    # in setup_hook, before tree.sync():
    init_attack_alert_table()
    self.tree.add_command(attack_setup)
    self.tree.add_command(attack)
"""

import sqlite3
from typing import Optional

import discord
from discord import app_commands

from i18n import translator

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


def _get_bot_admin_role_id(guild_id: int) -> Optional[str]:
    """Reads guild_config.admin_role, set by /setup. Returns None if the
    server never configured one (in which case only real Administrators
    can use /attack)."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT admin_role FROM guild_config WHERE guild_id=?", (str(guild_id),)
    ).fetchone()
    conn.close()
    return row[0] if row and row[0] else None


def _is_officer_or_admin(interaction: discord.Interaction) -> bool:
    if not isinstance(interaction.user, discord.Member):
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    bot_admin_role_id = _get_bot_admin_role_id(interaction.guild_id)
    if not bot_admin_role_id:
        return False
    return any(str(r.id) == bot_admin_role_id for r in interaction.user.roles)


# ---------------------------------------------------------------------------
# /attack_setup — admin-only config command
# ---------------------------------------------------------------------------

@app_commands.command(name="attack_setup", description="Configure the alert channel and ping role for a world's attack notifications.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    world_name="World name (e.g. eu20)",
    channel="Channel where attack alerts for this world will be posted",
    ping_role="Role to ping when an attack alert is triggered for this world",
)
async def attack_setup(
    interaction: discord.Interaction,
    world_name: str,
    channel: discord.TextChannel,
    ping_role: discord.Role,
):
    _save_world_config(interaction.guild_id, world_name, channel.id, ping_role.id)
    await interaction.response.send_message(
        f"✅ Attack alerts for **{world_name.upper()}** will now be posted in {channel.mention} "
        f"and ping {ping_role.mention}.",
        ephemeral=True,
    )


@attack_setup.error
async def attack_setup_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ You need **Manage Server** permission for this.", ephemeral=True)
    else:
        print(f"attack_setup error: {error}")
        await interaction.response.send_message("❌ Something went wrong.", ephemeral=True)


# ---------------------------------------------------------------------------
# /attack — the fast trigger
# ---------------------------------------------------------------------------

@app_commands.command(name="attack", description="Trigger a guild attack/raid alert for a specific world.")
@app_commands.describe(
    world_name="World name (e.g. eu20)",
    time="Attack time, e.g. 20:00 (omit for an immediate 'happening now' alert)",
)
async def attack(interaction: discord.Interaction, world_name: str, time: Optional[str] = None):
    # Permission check: real Administrator OR the server's configured
    # bot-admin role (guild_config.admin_role, set via /setup). No separate
    # officer-role system — reuses what's already there.
    if not _is_officer_or_admin(interaction):
        await interaction.response.send_message(
            "❌ You need Administrator permission or the server's configured admin role to use this.",
            ephemeral=True,
        )
        return

    config = _get_world_config(interaction.guild_id, world_name)
    if not config:
        await interaction.response.send_message(
            f"❌ No attack alert configuration found for **{world_name.upper()}**. "
            f"An admin needs to run `/attack_setup` for this world first.",
            ephemeral=True,
        )
        return

    channel_id, role_id = config
    channel = interaction.guild.get_channel(int(channel_id))
    role = interaction.guild.get_role(int(role_id))

    if not channel:
        await interaction.response.send_message(
            f"❌ The configured channel for **{world_name.upper()}** no longer exists. "
            f"Please run `/attack_setup` again.",
            ephemeral=True,
        )
        return

    # "now" is both the default (time=None) and a valid explicit value the
    # user could type — treat both the same way.
    is_now = time is None or time.strip().lower() == "now"

    embed = discord.Embed(color=discord.Color.dark_red())
    if is_now:
        embed.description = (
            f"⚔️ **Emergency! Guild Attack/Raid is happening NOW!** "
            f"Join the fight immediately! ⚔️"
        )
    else:
        embed.description = (
            f"⚔️ **Guild Attack/Raid scheduled for: {time}!** "
            f"Preparedness is key! ⚔️"
        )
    embed.set_footer(text=f"World: {world_name.upper()}")

    ping_text = role.mention if role else f"@here (configured role no longer exists)"

    try:
        await channel.send(content=ping_text, embed=embed)
    except discord.Forbidden:
        await interaction.response.send_message(
            f"❌ I don't have permission to send messages in {channel.mention}.", ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"✅ Attack alert sent to {channel.mention} for **{world_name.upper()}**.", ephemeral=True
    )


@attack.error
async def attack_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    print(f"attack command error: {error}")
    if interaction.response.is_done():
        await interaction.followup.send("❌ Something went wrong.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Something went wrong.", ephemeral=True)
