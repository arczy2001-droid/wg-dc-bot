"""
setup_wizard.py
================
One-time, interactive `/setup` slash command for guild configuration.

INTEGRATION (in your main bot file):
    from setup_wizard import setup as setup_command, setup_reset as setup_reset_command, init_setup_table

    class MyBot(commands.Bot):
        async def setup_hook(self):
            init_setup_table()                          # creates guild_config table if missing
            self.tree.add_command(setup_command)          # registers /setup
            self.tree.add_command(setup_reset_command)    # registers /setup_reset
            ...

NOTE: this introduces a NEW table (`guild_config`) separate from your existing
`ustawienia` key-value table. If you want `/setup`'s main/logs channel to fully
replace `/wg_root` and `/wg_set_logs`, update `sprawdz_pozwolenie()` and
`wyslij_log()` in your main file to read from `guild_config` instead of
`ustawienia` once you're ready to cut over.
"""

import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

import discord
from discord import app_commands

DB_PATH = "gildia.db"

# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------

def init_setup_table() -> None:
    """Idempotent — safe to call on every startup."""
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS guild_config (
            guild_id      TEXT PRIMARY KEY,
            main_channel  TEXT,
            logs_channel  TEXT,
            timezone      TEXT,
            language      TEXT,
            admin_role    TEXT,
            modules       TEXT,   -- JSON-encoded list, e.g. '["anti_phishing"]'
            api_token     TEXT,
            configured_at TIMESTAMP,
            configured_by TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def is_guild_configured(guild_id: int) -> bool:
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT 1 FROM guild_config WHERE guild_id = ?", (str(guild_id),)
    ).fetchone()
    conn.close()
    return row is not None


def save_guild_config(state: "WizardState", api_token: str) -> None:
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        INSERT OR REPLACE INTO guild_config
            (guild_id, main_channel, logs_channel, timezone, language,
             admin_role, modules, api_token, configured_at, configured_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(state.guild_id),
            str(state.main_channel),
            str(state.logs_channel),
            state.timezone,
            state.language,
            str(state.admin_role),
            json.dumps(state.modules),
            api_token,
            datetime.now(timezone.utc).isoformat(),
            str(state.author_id),
        ),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# WIZARD STATE — carried across every step
# ---------------------------------------------------------------------------

@dataclass
class WizardState:
    guild_id: int
    author_id: int
    main_channel: Optional[int] = None
    logs_channel: Optional[int] = None
    timezone: Optional[str] = None
    language: Optional[str] = None
    admin_role: Optional[int] = None
    modules: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# OPTION CATALOGS — extend these as the bot grows
# ---------------------------------------------------------------------------

TIMEZONE_CHOICES = [
    discord.SelectOption(label="UTC", value="UTC", description="Coordinated Universal Time"),
    discord.SelectOption(label="CET", value="CET", description="Central European Time (UTC+1)"),
    discord.SelectOption(label="EET", value="EET", description="Eastern European Time (UTC+2)"),
    discord.SelectOption(label="GMT", value="GMT", description="Greenwich Mean Time (UTC+0)"),
    discord.SelectOption(label="EST", value="EST", description="US Eastern Time (UTC-5)"),
    discord.SelectOption(label="PST", value="PST", description="US Pacific Time (UTC-8)"),
]

# value is an ISO language code — keeps this ready for real i18n later
# (e.g. a `translations/{code}.json` lookup keyed on this exact value).
LANGUAGE_CHOICES = [
    discord.SelectOption(label="English", value="en", emoji="🇬🇧"),
    discord.SelectOption(label="Polski", value="pl", emoji="🇵🇱"),
]

# Add future modules here — the multi-select grows automatically since
# max_values is derived from len(MODULE_CHOICES) below.
MODULE_CHOICES = [
    discord.SelectOption(
        label="Anti-Phishing Link Scanner",
        value="anti_phishing",
        description="Auto-delete messages containing known malicious links",
        default=True,  # pre-checked since it's the bot's core safety feature
    ),
    # discord.SelectOption(label="Scam Image Detection (OCR)", value="scam_ocr",
    #                       description="Flag fake-giveaway / crypto-casino screenshots"),
]


# ---------------------------------------------------------------------------
# SHARED VIEW BEHAVIOR
# ---------------------------------------------------------------------------

class WizardBaseView(discord.ui.View):
    """Common plumbing every step shares: restrict to the admin who started
    setup, and gracefully handle the 5-minute inactivity timeout."""

    def __init__(self, state: WizardState, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.state = state
        self.message: Optional[discord.InteractionMessage] = None  # set by caller

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.state.author_id:
            await interaction.response.send_message(
                "❌ Only the admin who started this setup can use these controls.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(
                    content="⌛ Setup wizard timed out after 5 minutes of inactivity. Run `/setup` again to restart.",
                    view=self,
                )
            except discord.HTTPException:
                pass


# ---------------------------------------------------------------------------
# STEP 1 — Main command channel
# ---------------------------------------------------------------------------

class Step1_MainChannel(WizardBaseView):
    def __init__(self, state: WizardState):
        super().__init__(state)
        select = discord.ui.ChannelSelect(
            placeholder="Choose the main command channel...",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
        )
        select.callback = self._on_select
        self.add_item(select)
        self._select = select

    async def _on_select(self, interaction: discord.Interaction):
        channel = self._select.values[0]
        self.state.main_channel = channel.id

        next_view = Step2_LogsChannel(self.state)
        next_view.message = self.message
        await interaction.response.edit_message(
            content=(
                "**🛠️ Server Setup — Step 2/6**\n"
                f"✅ Main channel set to {channel.mention}.\n\n"
                "Now choose the channel for security logs "
                "(blocked phishing links, deleted messages):"
            ),
            view=next_view,
        )


# ---------------------------------------------------------------------------
# STEP 2 — Logs channel
# ---------------------------------------------------------------------------

class Step2_LogsChannel(WizardBaseView):
    def __init__(self, state: WizardState):
        super().__init__(state)
        select = discord.ui.ChannelSelect(
            placeholder="Choose the security logs channel...",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
        )
        select.callback = self._on_select
        self.add_item(select)
        self._select = select

    async def _on_select(self, interaction: discord.Interaction):
        channel = self._select.values[0]
        self.state.logs_channel = channel.id

        next_view = Step3_Timezone(self.state)
        next_view.message = self.message
        await interaction.response.edit_message(
            content=(
                "**🛠️ Server Setup — Step 3/6**\n"
                f"✅ Logs channel set to {channel.mention}.\n\n"
                "Choose this server's timezone:"
            ),
            view=next_view,
        )


# ---------------------------------------------------------------------------
# STEP 3 — Timezone
# ---------------------------------------------------------------------------

class Step3_Timezone(WizardBaseView):
    def __init__(self, state: WizardState):
        super().__init__(state)
        select = discord.ui.Select(
            placeholder="Choose your server's timezone...",
            options=TIMEZONE_CHOICES,
            min_values=1,
            max_values=1,
        )
        select.callback = self._on_select
        self.add_item(select)
        self._select = select

    async def _on_select(self, interaction: discord.Interaction):
        self.state.timezone = self._select.values[0]

        next_view = Step4_Language(self.state)
        next_view.message = self.message
        await interaction.response.edit_message(
            content=(
                "**🛠️ Server Setup — Step 4/6**\n"
                f"✅ Timezone set to `{self.state.timezone}`.\n\n"
                "Choose the bot's language:"
            ),
            view=next_view,
        )


# ---------------------------------------------------------------------------
# STEP 4 — Language
# ---------------------------------------------------------------------------

class Step4_Language(WizardBaseView):
    def __init__(self, state: WizardState):
        super().__init__(state)
        select = discord.ui.Select(
            placeholder="Choose the bot's language...",
            options=LANGUAGE_CHOICES,
            min_values=1,
            max_values=1,
        )
        select.callback = self._on_select
        self.add_item(select)
        self._select = select

    async def _on_select(self, interaction: discord.Interaction):
        self.state.language = self._select.values[0]

        next_view = Step5_AdminRole(self.state)
        next_view.message = self.message
        await interaction.response.edit_message(
            content=(
                "**🛠️ Server Setup — Step 5/6**\n"
                f"✅ Language set to `{self.state.language}`.\n\n"
                "Choose the role allowed to manage advanced bot settings later:"
            ),
            view=next_view,
        )


# ---------------------------------------------------------------------------
# STEP 5 — Admin role
# ---------------------------------------------------------------------------

class Step5_AdminRole(WizardBaseView):
    def __init__(self, state: WizardState):
        super().__init__(state)
        select = discord.ui.RoleSelect(
            placeholder="Choose the bot-admin role...",
            min_values=1,
            max_values=1,
        )
        select.callback = self._on_select
        self.add_item(select)
        self._select = select

    async def _on_select(self, interaction: discord.Interaction):
        role = self._select.values[0]
        self.state.admin_role = role.id

        next_view = Step6_Modules(self.state)
        next_view.message = self.message
        await interaction.response.edit_message(
            content=(
                "**🛠️ Server Setup — Step 6/6**\n"
                f"✅ Bot-admin role set to {role.mention}.\n\n"
                "Finally, choose which modules to enable "
                "(Anti-Phishing is pre-selected and recommended):"
            ),
            view=next_view,
        )


# ---------------------------------------------------------------------------
# STEP 6 — Toggle modules (final interactive step)
# ---------------------------------------------------------------------------

class Step6_Modules(WizardBaseView):
    def __init__(self, state: WizardState):
        super().__init__(state)
        select = discord.ui.Select(
            placeholder="Select modules to enable...",
            options=MODULE_CHOICES,
            min_values=0,
            max_values=len(MODULE_CHOICES),
        )
        select.callback = self._on_select
        self.add_item(select)
        self._select = select

    async def _on_select(self, interaction: discord.Interaction):
        self.state.modules = list(self._select.values)  # may be [] if admin deselects everything
        await _finish_setup(interaction, self.state)


# ---------------------------------------------------------------------------
# FINALIZE — generate token, persist, confirm
# ---------------------------------------------------------------------------

async def _finish_setup(interaction: discord.Interaction, state: WizardState) -> None:
    # secrets.token_urlsafe is CSPRNG-backed — safe for an auth token.
    # NOTE: this stores the token in plaintext in SQLite. For a production
    # dashboard you'd typically store only a hash and show the plaintext once;
    # kept simple here per the requested scope.
    api_token = secrets.token_urlsafe(32)
    save_guild_config(state, api_token)

    summary = (
        "**✅ Setup complete!**\n"
        f"• Main channel: <#{state.main_channel}>\n"
        f"• Logs channel: <#{state.logs_channel}>\n"
        f"• Timezone: `{state.timezone}`\n"
        f"• Language: `{state.language}`\n"
        f"• Bot-admin role: <@&{state.admin_role}>\n"
        f"• Enabled modules: {', '.join(state.modules) if state.modules else '*none*'}\n\n"
        "This configuration is now **locked** — running `/setup` again will be refused."
    )

    dm_sent = False
    try:
        user = await interaction.client.fetch_user(state.author_id)
        await user.send(
            "🔑 Here is your **API integration token** for the future web dashboard.\n"
            "Keep it secret — anyone holding this token can authenticate as your server.\n\n"
            f"```\n{api_token}\n```"
        )
        dm_sent = True
    except (discord.Forbidden, discord.HTTPException):
        dm_sent = False

    if dm_sent:
        summary += "\n\n🔑 Your API token has been sent to you via DM."
    else:
        summary += (
            "\n\n⚠️ I couldn't DM you (check your privacy settings), so here is your "
            f"API token instead — **save it now, it will not be shown again**:\n```\n{api_token}\n```"
        )

    await interaction.response.edit_message(content=summary, view=None)


# ---------------------------------------------------------------------------
# THE COMMAND
# ---------------------------------------------------------------------------

@app_commands.command(name="setup", description="Run the one-time server configuration wizard (admin only).")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.guild_only()
async def setup(interaction: discord.Interaction):
    init_setup_table()  # idempotent safety net in case startup didn't call it

    if is_guild_configured(interaction.guild_id):
        await interaction.response.send_message(
            "🔒 This server is already configured. The setup wizard is locked to "
            "prevent accidental reconfiguration.",
            ephemeral=True,
        )
        return

    state = WizardState(guild_id=interaction.guild_id, author_id=interaction.user.id)
    view = Step1_MainChannel(state)
    await interaction.response.send_message(
        content="**🛠️ Server Setup — Step 1/6**\nChoose the channel where bot commands are allowed:",
        view=view,
        ephemeral=True,
    )
    view.message = await interaction.original_response()


@setup.error
async def setup_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "❌ You need the **Administrator** permission to run server setup."
    elif isinstance(error, app_commands.NoPrivateMessage):
        msg = "❌ This command can only be used inside a server."
    else:
        print(f"Unhandled error in /setup: {error}")
        msg = "❌ Something went wrong starting setup."

    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


# ---------------------------------------------------------------------------
# RESET — without this, a locked server has no way back in
# ---------------------------------------------------------------------------

@app_commands.command(name="setup_reset", description="Unlock configuration so /setup can be run again (admin only).")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.guild_only()
async def setup_reset(interaction: discord.Interaction):
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM guild_config WHERE guild_id = ?", (str(interaction.guild_id),))
    conn.commit()
    conn.close()
    await interaction.response.send_message(
        "🔓 Configuration reset. Run `/setup` again to reconfigure this server from scratch.",
        ephemeral=True,
    )


@setup_reset.error
async def setup_reset_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "❌ You need the **Administrator** permission to reset setup."
    else:
        print(f"Unhandled error in /setup_reset: {error}")
        msg = "❌ Something went wrong resetting setup."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)
