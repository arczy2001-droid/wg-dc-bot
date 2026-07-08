"""
recruitment.py
===============
Guild Recruitment System: a persistent "Apply to Guild" panel, an application
modal, automatic private thread (or channel, as fallback) creation, and an
officer decision panel (Accept / Reject / Ask Question).

INTEGRATION (in gildia_bot.py):

    from recruitment import (
        init_recruitment_tables,
        register_persistent_views,
        recruitment_panel,
    )

    # in setup_hook, before tree.sync():
    init_recruitment_tables()
    self.tree.add_command(recruitment_panel)
    await register_persistent_views(self)   # re-attaches buttons after a restart

PERSISTENCE NOTE (read this before touching the Views below):
    Discord buttons only keep working after a bot restart if the View that
    owns them is re-registered via `bot.add_view(...)` AND every button has a
    fixed `custom_id`. There are two kinds of views here:

    1. RecruitmentPanelView — one static "Apply to Guild" button. Its
       custom_id never changes, so it's registered ONCE at startup regardless
       of how many guilds use it (the callback looks up guild_id from the
       interaction itself).

    2. OfficerDecisionView — three buttons (Accept/Reject/Ask) tied to ONE
       specific application. Its custom_ids embed the application's row id
       (e.g. "recruitment_accept_42"), so a fresh instance must be
       re-registered for every application still "Pending" after a restart.
       register_persistent_views() does exactly this by re-querying the DB.

THREAD FALLBACK NOTE:
    Discord's own documentation states private threads require server Boost
    Level 2, but this requirement has been inconsistently reported across
    sources and may no longer hold for all servers (confirmed working
    without Boost Level 2 in real-world testing on at least one server as of
    this writing). Rather than hardcode an assumption either way,
    _create_application_space() simply TRIES a private thread first and lets
    Discord's API be the source of truth: if the request is rejected for any
    reason (Forbidden/HTTPException — insufficient boost level or otherwise),
    it automatically falls back to creating a private text channel with
    explicit permission overwrites for just the applicant + officer role.
"""

import sqlite3
from datetime import datetime
from typing import Optional, Union

import discord
from discord import app_commands

from i18n import translator

DB_PATH = "gildia.db"


# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------

def init_recruitment_tables() -> None:
    """Idempotent — safe to call on every startup."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS recruitment_config (
            guild_id       TEXT PRIMARY KEY,
            channel_id     TEXT NOT NULL,
            officer_role_id TEXT NOT NULL,
            member_role_id TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS recruitment_apps (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id      TEXT NOT NULL,
            applicant_id  TEXT NOT NULL,
            thread_id     TEXT,
            nickname      TEXT NOT NULL,
            char_class    TEXT NOT NULL,
            playstyle     TEXT NOT NULL,
            experience    TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'Pending',  -- Pending | Accepted | Rejected
            reason        TEXT,
            created_at    TIMESTAMP NOT NULL,
            decided_by    TEXT,
            decided_at    TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def _get_config(guild_id: int) -> Optional[dict]:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT channel_id, officer_role_id, member_role_id FROM recruitment_config WHERE guild_id=?",
        (str(guild_id),)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {"channel_id": row[0], "officer_role_id": row[1], "member_role_id": row[2]}


def _save_application(guild_id: int, applicant_id: int, nickname: str, char_class: str,
                       playstyle: str, experience: str) -> int:
    """Inserts a new Pending application and returns its row id."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        """INSERT INTO recruitment_apps
           (guild_id, applicant_id, nickname, char_class, playstyle, experience, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'Pending', ?)""",
        (str(guild_id), str(applicant_id), nickname, char_class, playstyle, experience, datetime.now().isoformat())
    )
    conn.commit()
    app_id = cur.lastrowid
    conn.close()
    return app_id


def _set_thread_id(app_id: int, thread_id: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE recruitment_apps SET thread_id=? WHERE id=?", (str(thread_id), app_id))
    conn.commit()
    conn.close()


def _get_application(app_id: int) -> Optional[dict]:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        """SELECT guild_id, applicant_id, thread_id, nickname, char_class, playstyle, experience, status
           FROM recruitment_apps WHERE id=?""",
        (app_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "guild_id": row[0], "applicant_id": row[1], "thread_id": row[2],
        "nickname": row[3], "char_class": row[4], "playstyle": row[5],
        "experience": row[6], "status": row[7],
    }


def _set_decision(app_id: int, status: str, decided_by: int, reason: Optional[str] = None) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE recruitment_apps SET status=?, reason=?, decided_by=?, decided_at=? WHERE id=?",
        (status, reason, str(decided_by), datetime.now().isoformat(), app_id)
    )
    conn.commit()
    conn.close()


def _get_pending_application_ids() -> list[int]:
    """Used at startup to know which OfficerDecisionViews need re-registering."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT id FROM recruitment_apps WHERE status='Pending'").fetchall()
    conn.close()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# ADMIN SETUP COMMAND
# ---------------------------------------------------------------------------

@app_commands.command(name="recruitment_panel", description="Post the guild recruitment application panel (admin only).")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(
    channel="Channel to post the recruitment panel in",
    officer_role="Role that can review and decide on applications",
    member_role="Optional: role automatically given to accepted applicants",
)
async def recruitment_panel(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    officer_role: discord.Role,
    member_role: Optional[discord.Role] = None,
):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO recruitment_config (guild_id, channel_id, officer_role_id, member_role_id)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(guild_id) DO UPDATE SET
               channel_id = excluded.channel_id,
               officer_role_id = excluded.officer_role_id,
               member_role_id = excluded.member_role_id""",
        (str(interaction.guild_id), str(channel.id), str(officer_role.id),
         str(member_role.id) if member_role else None)
    )
    conn.commit()
    conn.close()

    embed = discord.Embed(
        title="⚔️ Join Our Guild!",
        description=(
            "We're always looking for strong and active players!\n\n"
            "**What we offer:**\n"
            "• An active, discord community\n"
            "• Help at every stage of the game\n"
            "• Experienced management\n\n"
            "**What we look for:**\n"
            "• Regular activity and participation\n"
            "• Shroomers\n"
            "• A positive attitude and teamwork\n\n"
            "Click the button below to submit your application. An officer will "
            "review it and reach out to you shortly!"
        ),
        color=discord.Color.gold(),
    )
    embed.set_footer(text="Applications are reviewed privately by our officers.")

    view = RecruitmentPanelView()
    await channel.send(embed=embed, view=view)
    await interaction.response.send_message(
        f"✅ Recruitment panel posted in {channel.mention}. Officer role: {officer_role.mention}.",
        ephemeral=True,
    )


@recruitment_panel.error
async def recruitment_panel_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ You need **Administrator** permission for this.", ephemeral=True)
    else:
        print(f"recruitment_panel error: {error}")
        await interaction.response.send_message("❌ Something went wrong.", ephemeral=True)


# ---------------------------------------------------------------------------
# STATIC "APPLY TO GUILD" BUTTON — one persistent view, shared by every guild
# ---------------------------------------------------------------------------

class RecruitmentPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # persistent — must have no timeout

    @discord.ui.button(
        label="Apply to Guild",
        style=discord.ButtonStyle.green,
        emoji="📝",
        custom_id="recruitment_apply_button",  # fixed id required for persistence
    )
    async def apply_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        config = _get_config(interaction.guild_id)
        if not config:
            await interaction.response.send_message(
                "❌ The recruitment system isn't configured on this server.", ephemeral=True
            )
            return
        await interaction.response.send_modal(ApplicationModal())


# ---------------------------------------------------------------------------
# APPLICATION MODAL
# ---------------------------------------------------------------------------

class ApplicationModal(discord.ui.Modal, title="Guild Application"):
    nickname = discord.ui.TextInput(
        label="In-game Nickname",
        placeholder="Your exact in-game character name",
        max_length=32,
        required=True,
    )
    char_class = discord.ui.TextInput(
        label="Character Class",
        placeholder="e.g. Warrior, Mage, Scout",
        max_length=50,
        required=True,
    )
    playstyle = discord.ui.TextInput(
        label="Mushroom Budget / Playstyle",
        placeholder="e.g. F2P, ECO, P2W",
        max_length=100,
        required=True,
    )
    experience = discord.ui.TextInput(
        label="Experience / Achievements",
        style=discord.TextStyle.paragraph,
        placeholder="Tell us about your experience, past guilds, achievements...",
        max_length=1000,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        config = _get_config(interaction.guild_id)
        if not config:
            await interaction.followup.send("❌ Recruitment system isn't configured.", ephemeral=True)
            return

        app_id = _save_application(
            interaction.guild_id, interaction.user.id,
            self.nickname.value, self.char_class.value, self.playstyle.value, self.experience.value
        )

        thread_or_channel, used_thread = await _create_application_space(
            interaction, config, app_id, self.nickname.value
        )

        if thread_or_channel is None:
            await interaction.followup.send(
                "❌ I couldn't create a private space for your application. "
                "Please contact an officer directly.", ephemeral=True
            )
            return

        _set_thread_id(app_id, thread_or_channel.id)

        embed = _build_application_embed(interaction, app_id, self)
        officer_role = interaction.guild.get_role(int(config["officer_role_id"]))
        view = OfficerDecisionView(app_id)

        await thread_or_channel.send(
            content=f"{interaction.user.mention} {officer_role.mention if officer_role else ''}",
            embed=embed,
            view=view,
        )

        space_kind = "thread" if used_thread else "private channel"
        await interaction.followup.send(
            f"✅ Your application has been submitted! Check {thread_or_channel.mention} "
            f"(a {space_kind} just for you and our officers).",
            ephemeral=True,
        )


def _build_application_embed(interaction: discord.Interaction, app_id: int, modal: ApplicationModal) -> discord.Embed:
    """Builds the officer-facing application summary embed, localized to the
    server's configured language via the shared Translator."""
    gid = interaction.guild_id
    embed = discord.Embed(
        title=translator.get_text(gid, "recruitment.app_title", nickname=modal.nickname.value),
        color=discord.Color.blurple(),
    )
    embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
    embed.add_field(name=translator.get_text(gid, "recruitment.field_nickname"), value=modal.nickname.value, inline=True)
    embed.add_field(name=translator.get_text(gid, "recruitment.field_class"), value=modal.char_class.value, inline=True)
    embed.add_field(name=translator.get_text(gid, "recruitment.field_playstyle"), value=modal.playstyle.value, inline=True)
    embed.add_field(
        name=translator.get_text(gid, "recruitment.field_experience"),
        value=modal.experience.value[:1024],
        inline=False,
    )
    embed.set_footer(text=translator.get_text(gid, "recruitment.app_footer", app_id=app_id))
    embed.timestamp = discord.utils.utcnow()
    return embed


async def _create_application_space(
    interaction: discord.Interaction, config: dict, app_id: int, nickname: str
) -> tuple[Optional[Union[discord.Thread, discord.TextChannel]], bool]:
    """
    Tries a private thread first (requires server Boost Level 2 — Community
    status is not required for this specifically, only the boost tier matters).
    Falls back to a private text channel with explicit overwrites if that
    fails for any reason. Returns (space, used_thread).
    """
    channel = interaction.guild.get_channel(int(config["channel_id"]))
    if channel is None:
        return None, False

    safe_name = "".join(c for c in nickname if c.isalnum() or c in "-_")[:80] or "applicant"
    thread_name = f"apply-{safe_name}"

    try:
        thread = await channel.create_thread(
            name=thread_name,
            type=discord.ChannelType.private_thread,
            invitable=False,
        )
        await thread.add_user(interaction.user)
        return thread, True
    except (discord.Forbidden, discord.HTTPException) as e:
        print(f"recruitment: private thread creation failed ({e}), falling back to private channel")

    # --- Fallback: private text channel with explicit overwrites ---
    try:
        officer_role = interaction.guild.get_role(int(config["officer_role_id"]))
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            interaction.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        }
        if officer_role:
            overwrites[officer_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        priv_channel = await interaction.guild.create_text_channel(
            name=thread_name,
            category=channel.category,
            overwrites=overwrites,
            reason=f"Guild application #{app_id}",
        )
        return priv_channel, False
    except (discord.Forbidden, discord.HTTPException) as e:
        print(f"recruitment: private channel fallback also failed: {e}")
        return None, False


# ---------------------------------------------------------------------------
# OFFICER DECISION PANEL
# ---------------------------------------------------------------------------

def _is_officer(interaction: discord.Interaction, officer_role_id: str) -> bool:
    if not isinstance(interaction.user, discord.Member):
        return False
    return any(str(r.id) == officer_role_id for r in interaction.user.roles) or interaction.user.guild_permissions.administrator


class OfficerDecisionView(discord.ui.View):
    """Persistent per-application view. custom_id embeds app_id so it can be
    re-registered after a restart via register_persistent_views()."""

    def __init__(self, app_id: int):
        super().__init__(timeout=None)
        self.app_id = app_id
        # Rebuild button custom_ids with this specific app_id baked in.
        self.accept_button.custom_id = f"recruitment_accept_{app_id}"
        self.reject_button.custom_id = f"recruitment_reject_{app_id}"
        self.ask_button.custom_id = f"recruitment_ask_{app_id}"

    async def _check_officer(self, interaction: discord.Interaction) -> Optional[dict]:
        app = _get_application(self.app_id)
        if not app:
            await interaction.response.send_message("❌ Application not found (may have been deleted).", ephemeral=True)
            return None
        config = _get_config(interaction.guild_id)
        if not config or not _is_officer(interaction, config["officer_role_id"]):
            await interaction.response.send_message("❌ Only officers can use this.", ephemeral=True)
            return None
        if app["status"] != "Pending":
            await interaction.response.send_message(f"❌ This application was already **{app['status']}**.", ephemeral=True)
            return None
        return app

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.green, emoji="🟢", custom_id="recruitment_accept_placeholder")
    async def accept_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        app = await self._check_officer(interaction)
        if not app:
            return

        applicant = interaction.guild.get_member(int(app["applicant_id"]))
        config = _get_config(interaction.guild_id)

        _set_decision(self.app_id, "Accepted", interaction.user.id)
        for child in self.children:
            child.disabled = True

        if applicant:
            # Assign member role
            if config and config["member_role_id"]:
                role = interaction.guild.get_role(int(config["member_role_id"]))
                if role:
                    try:
                        await applicant.add_roles(role, reason=f"Guild application #{self.app_id} accepted")
                    except discord.Forbidden:
                        print(f"recruitment: missing permission to add role to {applicant}")

            # Rename to in-game nickname
            try:
                await applicant.edit(nick=app["nickname"], reason="Accepted guild application")
            except discord.Forbidden:
                print(f"recruitment: missing permission to rename {applicant} (likely server owner or role hierarchy)")

            # Welcome DM
            try:
                await applicant.send(
                    translator.get_text(
                        interaction.guild_id, "recruitment.dm_accepted",
                        guild=interaction.guild.name, nickname=app["nickname"]
                    )
                )
            except discord.Forbidden:
                pass  # applicant has DMs closed — not fatal

        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            f"✅ Application **accepted** by {interaction.user.mention}.", ephemeral=False
        )

        await _archive_or_lock(interaction, app)

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.red, emoji="🔴", custom_id="recruitment_reject_placeholder")
    async def reject_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        app = await self._check_officer(interaction)
        if not app:
            return
        await interaction.response.send_modal(RejectReasonModal(self.app_id, self))

    @discord.ui.button(label="Ask Question", style=discord.ButtonStyle.blurple, emoji="💬", custom_id="recruitment_ask_placeholder")
    async def ask_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        app = _get_application(self.app_id)
        if not app:
            await interaction.response.send_message("❌ Application not found.", ephemeral=True)
            return
        config = _get_config(interaction.guild_id)
        if not config or not _is_officer(interaction, config["officer_role_id"]):
            await interaction.response.send_message("❌ Only officers can use this.", ephemeral=True)
            return

        applicant = interaction.guild.get_member(int(app["applicant_id"]))
        mention = applicant.mention if applicant else f"<@{app['applicant_id']}>"
        await interaction.response.send_message(
            f"{mention} 👋 One of our officers ({interaction.user.mention}) has a question about your "
            f"application — feel free to reply here whenever you're ready!"
        )


class RejectReasonModal(discord.ui.Modal, title="Rejection Reason"):
    reason = discord.ui.TextInput(
        label="Reason for rejection",
        style=discord.TextStyle.paragraph,
        placeholder="This will be sent to the applicant — keep it constructive.",
        max_length=500,
        required=True,
    )

    def __init__(self, app_id: int, decision_view: OfficerDecisionView):
        super().__init__()
        self.app_id = app_id
        self.decision_view = decision_view

    async def on_submit(self, interaction: discord.Interaction):
        app = _get_application(self.app_id)
        if not app or app["status"] != "Pending":
            await interaction.response.send_message("❌ This application is no longer pending.", ephemeral=True)
            return

        _set_decision(self.app_id, "Rejected", interaction.user.id, reason=self.reason.value)
        for child in self.decision_view.children:
            child.disabled = True

        applicant = interaction.guild.get_member(int(app["applicant_id"]))
        if applicant:
            try:
                await applicant.send(
                    translator.get_text(
                        interaction.guild_id, "recruitment.dm_rejected",
                        guild=interaction.guild.name, reason=self.reason.value
                    )
                )
            except discord.Forbidden:
                pass

        await interaction.response.send_message(
            f"🔴 Application **rejected** by {interaction.user.mention}.\n**Reason:** {self.reason.value}"
        )
        await interaction.message.edit(view=self.decision_view) if interaction.message else None

        await _archive_or_lock(interaction, app)


async def _archive_or_lock(interaction: discord.Interaction, app: dict) -> None:
    """Archives the thread, or locks+renames the fallback private channel."""
    space = interaction.channel
    try:
        if isinstance(space, discord.Thread):
            await space.edit(archived=True, locked=True)
        elif isinstance(space, discord.TextChannel):
            # Fallback-channel case: just strip the applicant's access rather
            # than deleting outright, so officers can still review history.
            applicant = interaction.guild.get_member(int(app["applicant_id"]))
            if applicant:
                await space.set_permissions(applicant, view_channel=False)
    except discord.Forbidden:
        print("recruitment: missing permission to archive/lock application space")


# ---------------------------------------------------------------------------
# STARTUP: re-register persistent views so buttons survive a restart
# ---------------------------------------------------------------------------

async def register_persistent_views(bot: discord.Client) -> None:
    """Call once in setup_hook, AFTER init_recruitment_tables()."""
    bot.add_view(RecruitmentPanelView())  # one static view for the Apply button

    pending_ids = _get_pending_application_ids()
    for app_id in pending_ids:
        bot.add_view(OfficerDecisionView(app_id))

    if pending_ids:
        print(f"recruitment: re-registered {len(pending_ids)} pending application view(s)")
