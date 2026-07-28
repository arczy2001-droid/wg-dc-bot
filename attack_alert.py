"""
attack_alert.py
================
Per-world attack alert configuration.

PERMISSION MODEL:
    /gt_alerts_panel — requires Manage Server. Posts an interactive panel
                    (channel picker, ping-role picker, and a defense-mute
                    toggle) for one world. These are SERVER-WIDE per-world
                    settings, not per-user: they decide where a world's
                    automated attack/defense alerts go for everyone, so they
                    live with the admin, not in each player's /gt_sf_login.

WHY A PANEL AND NOT A MODAL/COMMAND:
    A discord.ui.Modal can only hold TextInput fields — no channel/role
    pickers, no toggle — so channel/role would have to be pasted as raw
    numeric IDs. A View (this panel) can host native ChannelSelect /
    RoleSelect components and a toggle button, which is why the config
    moved here from the old /gt_attack_setup command.

DATA MODEL (unchanged — SFMonitor in sf_auth.py reads these exactly as before):
    attack_config          (guild_id, world_name) -> channel_id, role_id
    world_notify_config    (guild_id, world_name) -> attack/defense channel
                            overrides + mute_defense flag
    The panel writes channel_id + role_id to attack_config and mute_defense
    to world_notify_config. SFMonitor uses world_notify_config's channels if
    set, falls back to attack_config's channel otherwise, and always pings
    attack_config's role.

INTEGRATION (in gildia_bot.py):

    from attack_alert import (
        init_attack_alert_table,
        gt_alerts_panel,
        register_alert_panel_views,
    )

    # in setup_hook, before tree.sync():
    init_attack_alert_table()
    self.tree.add_command(gt_alerts_panel)
    await register_alert_panel_views(self)   # re-attach the mute toggle after a restart
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
    # (no separate role field here — reuses what the panel configured).
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
# Shared helpers — used by /gt_alerts_panel below and by sf_auth.py's
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
# /gt_alerts_panel — admin posts a per-world config panel with native
# channel/role pickers and a defense-mute toggle. Replaces the old
# /gt_attack_setup command (see module docstring for why a panel).
# ---------------------------------------------------------------------------

def _mute_button_label(muted: bool) -> str:
    return "🔇 Defense pings: MUTED" if muted else "🔊 Defense pings: ON"


class AlertConfigView(discord.ui.View):
    """Per-world alert config panel.

    The channel and role selects are ephemeral (only the admin who ran the
    command sees them, timeout applies). The mute toggle is a PERSISTENT
    button — its custom_id embeds the world so it keeps working after a bot
    restart, re-registered via register_alert_panel_views(). Guild is taken
    from interaction.guild_id at click time (a button click always carries
    its guild), so it doesn't need to be baked into the custom_id.
    """

    def __init__(self, world_name: str, muted: bool = False):
        super().__init__(timeout=None)
        self.world_name = world_name.lower()

        channel_select = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text],
            placeholder="Channel to post this world's attack alerts",
            custom_id=f"alertcfg_channel_{self.world_name}",
            min_values=1, max_values=1,
        )
        channel_select.callback = self._on_channel
        self.add_item(channel_select)

        role_select = discord.ui.RoleSelect(
            placeholder="Role to ping on an attack alert",
            custom_id=f"alertcfg_role_{self.world_name}",
            min_values=1, max_values=1,
        )
        role_select.callback = self._on_role
        self.add_item(role_select)

        self.mute_button = discord.ui.Button(
            label=_mute_button_label(muted),
            style=discord.ButtonStyle.secondary,
            custom_id=f"alertcfg_mute_{self.world_name}",
        )
        self.mute_button.callback = self._on_mute_toggle
        self.add_item(self.mute_button)

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "❌ You need **Manage Server** permission to change alert settings.", ephemeral=True
            )
            return False
        return True

    async def _on_channel(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        channel = interaction.data["values"][0]  # channel id as str
        existing = _get_world_config(interaction.guild_id, self.world_name)
        role_id = existing[1] if existing else "0"  # keep role if already set, else placeholder
        _save_world_config(interaction.guild_id, self.world_name, int(channel), int(role_id))
        await interaction.response.send_message(
            f"✅ Attack alerts for **{self.world_name.upper()}** will post in <#{channel}>."
            + ("" if role_id != "0" else "\n⚠️ No ping role set yet — pick one below."),
            ephemeral=True,
        )

    async def _on_role(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        role_id = interaction.data["values"][0]
        existing = _get_world_config(interaction.guild_id, self.world_name)
        channel_id = existing[0] if existing else "0"
        _save_world_config(interaction.guild_id, self.world_name, int(channel_id), int(role_id))
        await interaction.response.send_message(
            f"✅ Attack alerts for **{self.world_name.upper()}** will ping <@&{role_id}>."
            + ("" if channel_id != "0" else "\n⚠️ No alert channel set yet — pick one above."),
            ephemeral=True,
        )

    async def _on_mute_toggle(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        current = get_notify_config(interaction.guild_id, self.world_name)
        now_muted = not (current["mute_defense"] if current else False)
        _upsert_notify_config(interaction.guild_id, self.world_name, mute_defense=now_muted)
        self.mute_button.label = _mute_button_label(now_muted)
        await interaction.response.edit_message(view=self)


@app_commands.command(
    name="gt_alerts_panel",
    description="Post the attack-alert config panel for one world (channel, ping role, defense mute).",
)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.autocomplete(world_name=registered_world_autocomplete)
@app_commands.describe(world_name="World this alert config applies to (e.g. eu20)")
async def gt_alerts_panel(
    interaction: discord.Interaction,
    world_name: app_commands.Transform[str, WorldTransformer],
):
    cfg = get_notify_config(interaction.guild_id, world_name)
    muted = cfg["mute_defense"] if cfg else False
    existing = _get_world_config(interaction.guild_id, world_name)

    status_lines = [f"**Alert settings — {world_name.upper()}**"]
    if existing:
        status_lines.append(f"• Channel: <#{existing[0]}>" if existing[0] != "0" else "• Channel: *(not set)*")
        status_lines.append(f"• Ping role: <@&{existing[1]}>" if existing[1] != "0" else "• Ping role: *(not set)*")
    else:
        status_lines.append("• Channel: *(not set)*\n• Ping role: *(not set)*")
    status_lines.append(f"• Defense pings: {'muted' if muted else 'on'}")

    await interaction.response.send_message(
        "\n".join(status_lines),
        view=AlertConfigView(world_name, muted=muted),
        ephemeral=True,
    )


@gt_alerts_panel.error
async def gt_alerts_panel_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ You need **Manage Server** permission for this.", ephemeral=True)
    else:
        print(f"gt_alerts_panel error: {error}")
        await interaction.response.send_message("❌ Something went wrong.", ephemeral=True)


async def register_alert_panel_views(bot: discord.Client) -> None:
    """Re-attach the persistent mute toggle for every world that has alert
    config, so the button keeps working after a restart. Channel/role selects
    are ephemeral (tied to a live /gt_alerts_panel invocation) and don't need
    re-registration. Mirrors recruitment.py's register_persistent_views()."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT world_name FROM attack_config").fetchall()
    extra = conn.execute("SELECT world_name FROM world_notify_config").fetchall()
    conn.close()
    worlds = {r[0] for r in rows} | {r[0] for r in extra}
    for world in worlds:
        bot.add_view(AlertConfigView(world))
        
