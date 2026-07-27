"""
attack_alert.py
================
Multi-World Guild Attack Alert system.

Deliberately minimal by design: no buttons, no tracking, no roster — just a
fast trigger that posts a themed embed and pings the right role for the
right world. Two commands, one table.

PERMISSION MODEL:
    /gt_attack_setup — requires Manage Server (consistent with other per-world
                    setup commands like /gt_world_add in the main bot).
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
        gt_attack_setup,
        attack,
    )

    # in setup_hook, before tree.sync():
    init_attack_alert_table()
    self.tree.add_command(gt_attack_setup)
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
# /gt_attack_setup — admin-only config command
# ---------------------------------------------------------------------------

@app_commands.command(name="gt_attack_setup", description="Configure the alert channel and ping role for a world's attack notifications.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    world_name="World name (e.g. eu20)",
    channel="Channel where attack alerts for this world will be posted",
    ping_role="Role to ping when an attack alert is triggered for this world",
)
async def gt_attack_setup(
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


@gt_attack_setup.error
async def gt_attack_setup_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ You need **Manage Server** permission for this.", ephemeral=True)
    else:
        print(f"gt_attack_setup error: {error}")
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
            f"An admin needs to run `/gt_attack_setup` for this world first.",
            ephemeral=True,
        )
        return

    channel_id, role_id = config
    channel = interaction.guild.get_channel(int(channel_id))
    role = interaction.guild.get_role(int(role_id))

    if not channel:
        await interaction.response.send_message(
            f"❌ The configured channel for **{world_name.upper()}** no longer exists. "
            f"Please run `/gt_attack_setup` again.",
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


# ---------------------------------------------------------------------------
# /gt_sf_toggle — per-world notification routing + defense muting
# ---------------------------------------------------------------------------

def _world_exists(guild_id: int, world_name: str) -> bool:
    """Validates against `swiaty`, the bot's canonical registered-worlds
    table (populated by /gt_world_add) — the same source /gt_absent_list
    etc. already validate world names against."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT 1 FROM swiaty WHERE guild_id=? AND nazwa=?", (str(guild_id), world_name.lower())
    ).fetchone()
    conn.close()
    return row is not None


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


async def _world_name_autocomplete(interaction: discord.Interaction, current: str):
    """Populates the world_name dropdown from `swiaty` (configured worlds
    for this server), filtered by whatever the user has typed so far.

    NOTE: Discord's autocomplete is a SUGGESTION list, not a hard constraint
    — a user can still type an arbitrary string and submit it. That's why
    the command handler below validates with _world_exists() regardless of
    what this function offered.
    """
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT nazwa FROM swiaty WHERE guild_id=? AND nazwa LIKE ? ORDER BY nazwa LIMIT 25",
        (str(interaction.guild_id), f"%{current.lower()}%")
    ).fetchall()
    conn.close()
    return [app_commands.Choice(name=r[0], value=r[0]) for r in rows]


@app_commands.command(
    name="gt_sf_toggle",
    description="Configure per-world attack/defense alert channels, or view current settings.",
)
@app_commands.autocomplete(world_name=_world_name_autocomplete)
@app_commands.describe(
    world_name="World to configure (pick from the list)",
    attack_channel="Channel for attack alerts (leave unset to keep current)",
    defense_channel="Channel for defense alerts (leave unset to keep current)",
    mute_defense="Suppress defense alerts entirely (True/False)",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def gt_sf_toggle(
    interaction: discord.Interaction,
    world_name: str,
    attack_channel: Optional[discord.TextChannel] = None,
    defense_channel: Optional[discord.TextChannel] = None,
    mute_defense: Optional[bool] = None,
):
    if not _world_exists(interaction.guild_id, world_name):
        await interaction.response.send_message(
            f"❌ **{world_name}** isn't a configured world on this server. "
            f"Use `/gt_world_add` first, or pick one from the autocomplete list.",
            ephemeral=True,
        )
        return

    # --- STATUS MODE: no optional args given at all -> just show current config ---
    if attack_channel is None and defense_channel is None and mute_defense is None:
        cfg = get_notify_config(interaction.guild_id, world_name)
        embed = discord.Embed(
            title=f"🔔 Notification settings — {world_name.upper()}",
            color=discord.Color.blurple(),
        )
        if not cfg:
            embed.description = "No custom notification settings configured yet — using attack-alert defaults."
        else:
            atk_ch = f"<#{cfg['attack_channel_id']}>" if cfg["attack_channel_id"] else "*(using /gt_attack_setup default)*"
            def_ch = f"<#{cfg['defense_channel_id']}>" if cfg["defense_channel_id"] else "*(using /gt_attack_setup default)*"
            embed.add_field(name="⚔️ Attack channel", value=atk_ch, inline=False)
            embed.add_field(name="🛡️ Defense channel", value=def_ch, inline=False)
            embed.add_field(name="🔇 Defense muted", value="Yes" if cfg["mute_defense"] else "No", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # --- CONFIG MODE: at least one optional arg given -> partial update ---
    result = _upsert_notify_config(
        interaction.guild_id, world_name,
        attack_channel_id=attack_channel.id if attack_channel else None,
        defense_channel_id=defense_channel.id if defense_channel else None,
        mute_defense=mute_defense,
    )

    lines = [f"✅ Updated notification settings for **{world_name.upper()}**:"]
    if attack_channel:
        lines.append(f"  • Attack alerts → {attack_channel.mention}")
    if defense_channel:
        lines.append(f"  • Defense alerts → {defense_channel.mention}")
    if mute_defense is not None:
        lines.append(f"  • Defense alerts muted: **{'Yes' if mute_defense else 'No'}**")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@gt_sf_toggle.error
async def gt_sf_toggle_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ You need **Manage Server** permission for this.", ephemeral=True)
    else:
        print(f"gt_sf_toggle error: {error}")
        if interaction.response.is_done():
            await interaction.followup.send("❌ Something went wrong.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Something went wrong.", ephemeral=True)