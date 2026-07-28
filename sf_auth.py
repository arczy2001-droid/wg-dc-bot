"""
sf_auth.py
==========
Discord-based Shakes & Fidget authentication, encrypted credential storage,
and hourly guild attack/defense monitoring.

╔══════════════════════════════════════════════════════════════════════════╗
║ READ THIS FIRST — TWO DESIGN DECISIONS THAT DIFFER FROM A NAIVE APPROACH ║
╚══════════════════════════════════════════════════════════════════════════╝

1. THE PASSWORD IS COLLECTED VIA A MODAL, NOT A SLASH COMMAND PARAMETER.
   A command like `/login <server> <username> <password>` is unsafe: Discord
   renders the slash-command *invocation* — including the values you typed
   into its parameters — to other people who can see the channel. Making the
   bot's *reply* ephemeral does not hide the invocation. `discord.ui.Modal`
   input is only ever visible to the person who filled it in, so that's what
   this module uses.

2. THE GAME CALL GOES THROUGH A RUST SUBPROCESS, NOT A PYTHON LIBRARY.
   `sf-api` is a Rust crate with no Python bindings, and S&F's login protocol
   is encrypted/undocumented, so it cannot be reimplemented reliably in
   aiohttp. This module shells out to the `sf_probe` binary (built from
   sf_probe.rs) and parses its JSON. Credentials are handed to it on STDIN,
   never as argv — argv is world-readable via `ps aux`.

──────────────────────────────────────────────────────────────────────────
HOW THE ENCRYPTION WORKS
──────────────────────────────────────────────────────────────────────────
Passwords are encrypted with Fernet (`cryptography` library), which is
AES-128-CBC for confidentiality plus an HMAC-SHA256 authentication tag, so
ciphertext that has been tampered with fails to decrypt rather than
silently returning garbage.

    plaintext password ──Fernet.encrypt(key)──► ciphertext ──► SQLite BLOB
    SQLite BLOB ──Fernet.decrypt(key)──► plaintext (held in RAM only,
                                          for the duration of one probe call)

The key lives in the SF_ENCRYPTION_KEY environment variable (put it in your
.env), NEVER in the database and never in this file. Generate one once with:

    python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

If SF_ENCRYPTION_KEY is missing or malformed, this module refuses to store
or read credentials at all rather than falling back to anything weaker.

⚠️ HONEST LIMITATION — PLEASE READ:
Encryption-at-rest protects against someone who obtains a *copy of the
database file alone* (a stolen backup, a leaked volume snapshot). It does
NOT protect against someone who compromises the VPS itself, because the key
sits in .env on that same machine — anyone with shell access has both
halves. This is the standard, unavoidable tradeoff for a bot that must log
in unattended on a schedule: it needs the real password at 03:00 with
nobody around to type it, so it must be able to recover the plaintext.
Treat "the bot can log in by itself" and "even I cannot recover these
passwords" as mutually exclusive — you can't have both.

Practical consequences worth accepting deliberately before deploying this:
  • Anyone with root/shell on the VPS can recover every stored password.
  • S&F passwords are often reused elsewhere; a breach here can cascade.
  • Automating game accounts may violate Playa Games' terms — the sf-api
    project's own README carries a ban warning. Storing OTHER PEOPLE'S
    credentials means you'd be putting their accounts at that risk too, so
    the safest scope is your own account only (see ALLOW_OTHER_USERS below).

INTEGRATION (in main.py):

    from sf_auth import (
        init_sf_auth_tables,
        gt_sf_login, gt_sf_logout, gt_sf_toggle_checks, gt_sf_status,
        SFMonitor,
    )

    # in setup_hook, before tree.sync():
    init_sf_auth_tables()
    for cmd in (gt_sf_login, gt_sf_logout, gt_sf_toggle_checks, gt_sf_status):
        self.tree.add_command(cmd)
    self.sf_monitor = SFMonitor(self)
    self.sf_monitor.start()
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import tasks

DB_PATH = "gildia.db"

# Path to the compiled Rust probe binary (see sf_probe.rs / Cargo.toml).
# Override with SF_PROBE_PATH in .env if you build it elsewhere.
SF_PROBE_PATH = os.getenv("SF_PROBE_PATH", "./target/release/sf_probe")

# When False, a member can only register their OWN S&F account and only an
# administrator may register on behalf of anyone else. Leaving this False is
# strongly recommended — see the honest-limitation note in the docstring.
ALLOW_OTHER_USERS = False

# How long a single probe may run before we give up (network timeouts).
PROBE_TIMEOUT_SECONDS = 60


# ---------------------------------------------------------------------------
# ENCRYPTION
# ---------------------------------------------------------------------------

class EncryptionUnavailable(RuntimeError):
    """Raised when SF_ENCRYPTION_KEY is missing or invalid.

    We deliberately raise instead of degrading to plaintext storage: a bot
    that silently stores passwords unencrypted because a key was missing is
    far worse than a bot that refuses to start the feature.
    """


def _get_fernet():
    """Builds the Fernet cipher from SF_ENCRYPTION_KEY.

    Imported lazily so the rest of the bot still runs if `cryptography`
    isn't installed — only this module's commands will fail, with a clear
    message, rather than the whole bot failing to boot.
    """
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise EncryptionUnavailable(
            "The `cryptography` package is not installed. Run: pip install cryptography"
        ) from exc

    key = os.getenv("SF_ENCRYPTION_KEY")
    if not key:
        raise EncryptionUnavailable(
            "SF_ENCRYPTION_KEY is not set. Generate one with:\n"
            '  python3 -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"\n'
            "then add it to your .env file."
        )
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except Exception as exc:
        raise EncryptionUnavailable(
            "SF_ENCRYPTION_KEY is malformed — it must be a urlsafe base64 32-byte "
            "key produced by Fernet.generate_key()."
        ) from exc


def encrypt_password(plaintext: str) -> bytes:
    """plaintext ──► Fernet ciphertext (what actually lands in SQLite)."""
    return _get_fernet().encrypt(plaintext.encode("utf-8"))


def decrypt_password(ciphertext: bytes) -> str:
    """Fernet ciphertext ──► plaintext, held in RAM only for one probe call.

    Fernet verifies the HMAC before decrypting, so a tampered-with or
    wrong-key blob raises rather than returning corrupted bytes.
    """
    return _get_fernet().decrypt(ciphertext).decode("utf-8")


# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------

def init_sf_auth_tables() -> None:
    """Idempotent — safe to call on every startup."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # ------------------------------------------------------------------
    # sf_accounts — ONE CHARACTER PER WORLD (per guild)
    #
    # Old schema keyed on (guild_id, discord_user_id, world_name), which let
    # TWO different Discord users each register the SAME world_name with
    # different S&F credentials — not what "one account per world" means.
    # New schema keys on (guild_id, world_name) only: whoever runs
    # /gt_sf_login for a world REPLACES any existing registration for that
    # world, regardless of which Discord user owns it. This is a deliberate,
    # user-requested behaviour change, not an accident — see _save_account().
    #
    # discord_user_id is KEPT as a plain column (who currently owns this
    # world's registration, for /gt_sf_status and DM-on-failure) — it's just
    # no longer part of the uniqueness key.
    #
    # MIGRATION: if the old 3-column PK schema exists, rebuild the table
    # (SQLite can't ALTER a PRIMARY KEY in place) and DEDUPE — if two users
    # had registered the same world, keep only the most recently created row
    # and print exactly what was dropped, so nothing disappears silently.
    # ------------------------------------------------------------------
    existing_cols = {row[1] for row in c.execute("PRAGMA table_info(sf_accounts)").fetchall()}
    if not existing_cols:
        c.execute("""
            CREATE TABLE sf_accounts (
                guild_id        TEXT NOT NULL,
                discord_user_id TEXT NOT NULL,
                world_name      TEXT NOT NULL,
                sf_username     TEXT NOT NULL,
                password_enc    BLOB NOT NULL,
                auto_checks     INTEGER NOT NULL DEFAULT 1,
                last_check      TIMESTAMP,
                last_status     TEXT,
                created_at      TIMESTAMP NOT NULL,
                PRIMARY KEY (guild_id, world_name)
            )
        """)
    else:
        pk_cols = [row[1] for row in c.execute("PRAGMA table_info(sf_accounts)").fetchall() if row[5] > 0]
        if set(pk_cols) != {"guild_id", "world_name"}:
            print("sf_auth: migrating sf_accounts to one-account-per-world schema...")

            # Find and report any (guild, world) pairs registered by more
            # than one user, BEFORE we drop anything.
            dupes = c.execute("""
                SELECT guild_id, world_name, COUNT(*) as n
                FROM sf_accounts GROUP BY guild_id, world_name HAVING n > 1
            """).fetchall()
            for guild_id, world_name, n in dupes:
                rows = c.execute(
                    """SELECT discord_user_id, sf_username, created_at FROM sf_accounts
                       WHERE guild_id=? AND world_name=? ORDER BY created_at DESC""",
                    (guild_id, world_name)
                ).fetchall()
                keeper = rows[0]
                print(f"sf_auth:   {world_name} (guild {guild_id}) had {n} registrations — "
                      f"keeping user {keeper[0]}'s '{keeper[1]}' (most recent), "
                      f"dropping: {[(r[0], r[1]) for r in rows[1:]]}")

            c.execute("ALTER TABLE sf_accounts RENAME TO sf_accounts_old")
            c.execute("""
                CREATE TABLE sf_accounts (
                    guild_id        TEXT NOT NULL,
                    discord_user_id TEXT NOT NULL,
                    world_name      TEXT NOT NULL,
                    sf_username     TEXT NOT NULL,
                    password_enc    BLOB NOT NULL,
                    auto_checks     INTEGER NOT NULL DEFAULT 1,
                    last_check      TIMESTAMP,
                    last_status     TEXT,
                    created_at      TIMESTAMP NOT NULL,
                    PRIMARY KEY (guild_id, world_name)
                )
            """)
            # Keep exactly one row per (guild_id, world_name): the one with
            # the latest created_at. rowid tie-break keeps this deterministic
            # if two rows somehow share a timestamp.
            c.execute("""
                INSERT INTO sf_accounts
                SELECT guild_id, discord_user_id, world_name, sf_username, password_enc,
                       auto_checks, last_check, last_status, created_at
                FROM sf_accounts_old o
                WHERE o.rowid = (
                    SELECT o2.rowid FROM sf_accounts_old o2
                    WHERE o2.guild_id = o.guild_id AND o2.world_name = o.world_name
                    ORDER BY o2.created_at DESC, o2.rowid DESC LIMIT 1
                )
            """)
            c.execute("DROP TABLE sf_accounts_old")
            print("sf_auth: migration complete.")

    # Remembers which battles we've already announced, so the hourly loop
    # doesn't re-ping the same role about the same attack 24 times a day.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sf_alert_state (
            guild_id    TEXT NOT NULL,
            world_name  TEXT NOT NULL,
            kind        TEXT NOT NULL,   -- 'attacking' | 'defending'
            battle_date TEXT NOT NULL,   -- RFC3339 string from the probe
            alerted_at  TIMESTAMP NOT NULL,
            PRIMARY KEY (guild_id, world_name, kind, battle_date)
        )
    """)
    conn.commit()
    conn.close()


def _save_account(guild_id: int, discord_user_id: int, world_name: str,
                  sf_username: str, password_plain: str) -> Optional[str]:
    """Encrypts, then stores. The plaintext argument is never persisted.

    ONE-ACCOUNT-PER-WORLD ENFORCEMENT: keyed on (guild_id, world_name) only,
    so if a DIFFERENT Discord user already registered this world, this call
    silently REPLACES their registration (INSERT OR REPLACE semantics via
    ON CONFLICT). Returns the discord_user_id that previously owned this
    world's slot, if any and if different from the caller — so the command
    handler can warn the new registrant that they just took over someone
    else's slot.
    """
    conn = sqlite3.connect(DB_PATH)
    existing = conn.execute(
        "SELECT discord_user_id FROM sf_accounts WHERE guild_id=? AND world_name=?",
        (str(guild_id), world_name.lower())
    ).fetchone()
    previous_owner = existing[0] if existing and existing[0] != str(discord_user_id) else None

    password_enc = encrypt_password(password_plain)
    conn.execute(
        """INSERT INTO sf_accounts
           (guild_id, discord_user_id, world_name, sf_username, password_enc,
            auto_checks, last_check, last_status, created_at)
           VALUES (?, ?, ?, ?, ?, 1, NULL, NULL, ?)
           ON CONFLICT(guild_id, world_name) DO UPDATE SET
               discord_user_id = excluded.discord_user_id,
               sf_username     = excluded.sf_username,
               password_enc    = excluded.password_enc,
               created_at      = excluded.created_at""",
        (str(guild_id), str(discord_user_id), world_name.lower(), sf_username,
         password_enc, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    conn.close()
    return previous_owner


def _delete_account(guild_id: int, discord_user_id: int, world_name: Optional[str]) -> int:
    conn = sqlite3.connect(DB_PATH)
    if world_name:
        cur = conn.execute(
            "DELETE FROM sf_accounts WHERE guild_id=? AND discord_user_id=? AND world_name=?",
            (str(guild_id), str(discord_user_id), world_name.lower())
        )
    else:
        cur = conn.execute(
            "DELETE FROM sf_accounts WHERE guild_id=? AND discord_user_id=?",
            (str(guild_id), str(discord_user_id))
        )
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    return deleted


def _set_auto_checks(guild_id: int, discord_user_id: int, world_name: Optional[str],
                     enabled: bool) -> int:
    conn = sqlite3.connect(DB_PATH)
    if world_name:
        cur = conn.execute(
            "UPDATE sf_accounts SET auto_checks=? WHERE guild_id=? AND discord_user_id=? AND world_name=?",
            (1 if enabled else 0, str(guild_id), str(discord_user_id), world_name.lower())
        )
    else:
        cur = conn.execute(
            "UPDATE sf_accounts SET auto_checks=? WHERE guild_id=? AND discord_user_id=?",
            (1 if enabled else 0, str(guild_id), str(discord_user_id))
        )
    changed = cur.rowcount
    conn.commit()
    conn.close()
    return changed


def _get_active_accounts() -> list[dict[str, Any]]:
    """Every account with auto_checks enabled, across all guilds."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """SELECT guild_id, discord_user_id, world_name, sf_username, password_enc, last_status
           FROM sf_accounts WHERE auto_checks=1"""
    ).fetchall()
    conn.close()
    return [
        {"guild_id": r[0], "discord_user_id": r[1], "world_name": r[2],
         "sf_username": r[3], "password_enc": r[4], "last_status": r[5]}
        for r in rows
    ]


def _record_check(guild_id: str, discord_user_id: str, world_name: str, status: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """UPDATE sf_accounts SET last_check=?, last_status=?
           WHERE guild_id=? AND discord_user_id=? AND world_name=?""",
        (datetime.now(timezone.utc).isoformat(), status[:200],
         guild_id, discord_user_id, world_name)
    )
    conn.commit()
    conn.close()


def _already_alerted(guild_id: str, world_name: str, kind: str, battle_date: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        """SELECT 1 FROM sf_alert_state
           WHERE guild_id=? AND world_name=? AND kind=? AND battle_date=?""",
        (guild_id, world_name, kind, battle_date)
    ).fetchone()
    conn.close()
    return row is not None


def _mark_alerted(guild_id: str, world_name: str, kind: str, battle_date: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR IGNORE INTO sf_alert_state VALUES (?, ?, ?, ?, ?)",
        (guild_id, world_name, kind, battle_date, datetime.now(timezone.utc).isoformat())
    )
    # Housekeeping: drop alert records older than 30 days so this table
    # doesn't grow without bound.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    conn.execute("DELETE FROM sf_alert_state WHERE alerted_at < ?", (cutoff,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# TIMEZONE FORMATTING
# ---------------------------------------------------------------------------

# guild_config.timezone stores the short labels offered by the /gt_setup
# wizard. Map them to IANA zones that zoneinfo understands.
_TZ_ALIASES = {
    "UTC": "UTC",
    "GMT": "GMT",
    "CET": "CET",
    "EET": "EET",
    "EST": "America/New_York",   # honours US DST, unlike fixed-offset "EST"
    "PST": "America/Los_Angeles",
}


def _get_guild_timezone(guild_id: str) -> str:
    """Reads the timezone chosen during /gt_setup. Falls back to UTC."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT timezone FROM guild_config WHERE guild_id=?", (guild_id,)
    ).fetchone()
    conn.close()
    return row[0] if row and row[0] else "UTC"


def format_battle_time(rfc3339: str, guild_id: str) -> str:
    """Converts the probe's RFC3339 timestamp into the guild's local timezone.

    Falls back to showing the raw value rather than raising — a mangled
    timestamp should never stop an attack alert from being delivered.
    """
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(rfc3339)
        tz_label = _get_guild_timezone(guild_id)
        tz = ZoneInfo(_TZ_ALIASES.get(tz_label, tz_label))
        return f"{dt.astimezone(tz).strftime('%d.%m.%Y %H:%M')} ({tz_label})"
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
        print(f"sf_auth: could not format '{rfc3339}' ({exc}); showing raw value")
        return rfc3339


# ---------------------------------------------------------------------------
# GAME PROBE (Rust subprocess bridge)
# ---------------------------------------------------------------------------

async def run_probe(server: str, username: str, password: str) -> dict[str, Any]:
    """Runs sf_probe and returns its parsed JSON.

    The password is written to the child's STDIN and never appears in argv,
    so it stays out of the process table.

    Always returns a dict with at least {"ok": bool}; transport-level
    problems are converted into {"ok": False, "error": ...} so callers only
    ever need one error path.
    """
    if not os.path.exists(SF_PROBE_PATH):
        return {"ok": False, "error": (
            f"probe binary not found at {SF_PROBE_PATH} — build it with "
            f"`cargo build --release --bin sf_probe`"
        )}

    try:
        proc = await asyncio.create_subprocess_exec(
            SF_PROBE_PATH,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        payload = f"{server}\n{username}\n{password}\n".encode("utf-8")
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(payload), timeout=PROBE_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        return {"ok": False, "error": f"probe timed out after {PROBE_TIMEOUT_SECONDS}s"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"probe failed to run: {exc}"}

    raw = stdout.decode("utf-8", errors="replace").strip()
    if not raw:
        err = stderr.decode("utf-8", errors="replace").strip()
        return {"ok": False, "error": f"probe produced no output. stderr: {err[:300]}"}

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "error": f"probe output was not valid JSON: {raw[:300]}"}


# ---------------------------------------------------------------------------
# LOGIN — disclaimer confirmation, then modal (see design note #1 at the
# top of this file for why the modal exists at all)
# ---------------------------------------------------------------------------

class SFLoginDisclaimerView(discord.ui.View):
    """Ephemeral, un-editable legal notice shown BEFORE SFLoginModal opens.

    Unlike a TextInput default value (which the user could technically type
    over), this is a real embed — nothing about it can be altered by the
    person reading it. Only interaction.response.send_modal() can open a
    modal, and that call must be the FIRST response to an interaction, which
    is exactly why this has to be a separate button click rather than
    something shown inside the modal itself: the button's own interaction
    is what "spends" the response slot that opens the modal.
    """

    def __init__(self, invoker_id: int, target: discord.Member):
        super().__init__(timeout=120)
        self.invoker_id = invoker_id
        self.target = target
        self.message: Optional[discord.InteractionMessage] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # The message is ephemeral (only invoker_id can even see it), but
        # belt-and-suspenders in case Discord ever changes that guarantee.
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "❌ Only the person who ran this command can use this button.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(
                    content="⌛ This confirmation expired. Run `/gt_sf_login` again to continue.",
                    view=self,
                )
            except discord.HTTPException:
                pass  # message may already be gone (e.g. channel deleted) — nothing to do

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.danger)
    async def continue_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.send_modal(SFLoginModal(self.target))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Cancelled — no account was connected.", embed=None, view=self)


def _build_sf_login_disclaimer_embed() -> discord.Embed:
    embed = discord.Embed(
        title="⚠️ Before you connect your account",
        description=(
            "1. In the event of a data breach or compromised accounts, there will be "
            "absolutely no compensation from Playa Games.\n"
            "2. The bot has no affiliation whatsoever with Playa Games.\n"
            "3. The bot is in no way endorsed by Playa Games."
        ),
        color=discord.Color.red(),
    )
    return embed


class SFLoginModal(discord.ui.Modal, title="Shakes & Fidget Login"):
    """Modal input is visible ONLY to the user who opened it.

    This is the whole reason we don't take the password as a slash-command
    parameter — those are rendered in-channel alongside the invocation.

    The legal disclaimer is no longer a field on this modal — it's shown as
    a proper ephemeral embed with a "Continue" button BEFORE this modal is
    opened (see SFLoginDisclaimerView / gt_sf_login below). That gives an
    unambiguous, un-editable notice, instead of a TextInput default value
    the user could technically type over.

    ⚠️ PASSWORD MASKING — READ BEFORE ASSUMING THIS IS SECURE INPUT:
    Discord's TextInput component has exactly two styles, `short` (one line)
    and `paragraph` (multi-line) — there is no "password"/masked style in
    Discord's API, for any bot, in any client. The `password` field below is
    NOT obscured with bullets/asterisks; it renders as plain text while the
    user types. This is a platform limitation, not something fixable from
    this code. The actual protection here is narrower than "hidden input":
    only the user who opened the modal can see it at all (not the channel),
    and the value is Fernet-encrypted before it ever touches disk — but
    anyone who can see that user's own screen while they type can read it.
    """

    server = discord.ui.TextInput(
        label="Server",
        placeholder="e.g. s20.sfgame.eu",
        max_length=64,
        required=True,
    )
    username = discord.ui.TextInput(
        label="S&F Username",
        max_length=64,
        required=True,
    )
    password = discord.ui.TextInput(
        label="S&F Password",
        style=discord.TextStyle.short,  # Discord's only single-line style — NOT a masked/password style, see class docstring
        placeholder="Not masked by Discord — only you can see this modal, but the text itself is plain",
        max_length=128,
        required=True,
    )

    def __init__(self, target_user: discord.Member):
        super().__init__()
        self.target_user = target_user

    async def on_submit(self, interaction: discord.Interaction):
        # ephemeral=True: nobody else in the channel sees any of this.
        await interaction.response.defer(ephemeral=True, thinking=True)

        server = self.server.value.strip()
        username = self.username.value.strip()
        password = self.password.value

        # Fail fast if encryption isn't configured — we must never reach the
        # storage step without a working key.
        try:
            _get_fernet()
        except EncryptionUnavailable as exc:
            await interaction.followup.send(f"❌ Encryption is not configured:\n```{exc}```", ephemeral=True)
            return

        # Validate the credentials BEFORE storing anything, so we never
        # persist a password that doesn't actually work.
        result = await run_probe(server, username, password)

        if not result.get("ok"):
            await interaction.followup.send(
                f"❌ Login failed — nothing was saved.\n```{str(result.get('error'))[:400]}```",
                ephemeral=True,
            )
            return

        previous_owner_id = _save_account(interaction.guild_id, self.target_user.id, server, username, password)

        # Filter to only the character(s) whose VERIFIED server matches what
        # was actually registered — same logic as the hourly check in
        # SFMonitor._check_account. Without this, an SSO account with many
        # characters would summarize every guild it has anywhere, not just
        # the one on `server`, which is misleading right at the point of
        # registration (this exact bug is what caused cross-world alert
        # mislabeling before the server_url() fix).
        all_chars = result.get("characters", [])
        chars = [c for c in all_chars if c.get("server", "").lower() == server.lower()]

        guild_names = sorted({c["guild"] for c in chars if c.get("guild")})
        summary = ", ".join(guild_names) if guild_names else "(no guild found)"

        unverified_note = ""
        if not chars and all_chars:
            # The login succeeded and returned characters, but none carried
            # a verified match for THIS server — most likely an old probe
            # binary that hasn't been rebuilt with server_url() support yet.
            unverified_note = (
                f"\n⚠️ The login succeeded but no character could be verified as belonging to "
                f"`{server}` specifically. If this persists, make sure `sf_probe` has been rebuilt "
                f"with `cargo build --release --bin sf_probe`."
            )

        takeover_note = ""
        if previous_owner_id:
            takeover_note = (
                f"\n⚠️ **{server}** was already registered by <@{previous_owner_id}> — "
                f"that registration has been **replaced** by this one (one account per world)."
            )

        await interaction.followup.send(
            f"✅ Login verified and stored **encrypted** for `{server}`.\n"
            f"Guild found on this server: **{summary}**\n"
            f"Hourly checks are **on** — use `/gt_sf_toggle_checks` to change that."
            f"{unverified_note}{takeover_note}",
            ephemeral=True,
        )


@app_commands.command(
    name="gt_sf_login",
    description="Securely connect a Shakes & Fidget account for guild monitoring.",
)
@app_commands.describe(
    user="Admin only: register on behalf of another member (defaults to yourself)"
)
@app_commands.guild_only()
async def gt_sf_login(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    target = user or interaction.user

    # Registering someone else's credentials is gated: it means holding
    # another person's game password, so only admins may do it, and only if
    # ALLOW_OTHER_USERS is switched on deliberately.
    if target.id != interaction.user.id:
        if not ALLOW_OTHER_USERS:
            await interaction.response.send_message(
                "❌ Registering another member's account is disabled on this bot.\n"
                "Ask them to run `/gt_sf_login` themselves — that way nobody else "
                "ever handles their password.",
                ephemeral=True,
            )
            return
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "❌ Only administrators can register an account for another member.",
                ephemeral=True,
            )
            return

    view = SFLoginDisclaimerView(invoker_id=interaction.user.id, target=target)
    await interaction.response.send_message(
        embed=_build_sf_login_disclaimer_embed(), view=view, ephemeral=True
    )
    view.message = await interaction.original_response()


# ---------------------------------------------------------------------------
# LOGOUT
# ---------------------------------------------------------------------------

@app_commands.command(
    name="gt_sf_logout",
    description="Delete your stored Shakes & Fidget credentials from the bot.",
)
@app_commands.describe(world="Only remove this world (omit to remove all of yours)")
@app_commands.guild_only()
async def gt_sf_logout(interaction: discord.Interaction, world: Optional[str] = None):
    deleted = _delete_account(interaction.guild_id, interaction.user.id, world)
    if deleted:
        await interaction.response.send_message(
            f"✅ Removed **{deleted}** stored account(s). The encrypted password has been deleted.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            "ℹ️ You have no stored accounts matching that.", ephemeral=True
        )


# ---------------------------------------------------------------------------
# TOGGLE AUTOMATIC CHECKS
# ---------------------------------------------------------------------------

@app_commands.command(
    name="gt_sf_toggle_checks",
    description="Turn the hourly guild attack/defense check on or off for your account.",
)
@app_commands.describe(
    state="True to enable hourly checks, False to disable",
    world="Apply to this world only (omit for all of your accounts)",
)
@app_commands.guild_only()
async def gt_sf_toggle_checks(interaction: discord.Interaction, state: bool,
                              world: Optional[str] = None):
    changed = _set_auto_checks(interaction.guild_id, interaction.user.id, world, state)
    if not changed:
        await interaction.response.send_message(
            "ℹ️ No stored accounts matched — run `/gt_sf_login` first.", ephemeral=True
        )
        return
    await interaction.response.send_message(
        f"{'✅ Enabled' if state else '🔕 Disabled'} hourly checks for **{changed}** account(s).",
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# STATUS
# ---------------------------------------------------------------------------

@app_commands.command(
    name="gt_sf_status",
    description="Show your connected Shakes & Fidget accounts and their check status.",
)
@app_commands.guild_only()
async def gt_sf_status(interaction: discord.Interaction):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """SELECT world_name, sf_username, auto_checks, last_check, last_status
           FROM sf_accounts WHERE guild_id=? AND discord_user_id=?
           ORDER BY world_name""",
        (str(interaction.guild_id), str(interaction.user.id))
    ).fetchall()
    conn.close()

    if not rows:
        await interaction.response.send_message(
            "ℹ️ You have no connected accounts. Use `/gt_sf_login` to add one.", ephemeral=True
        )
        return

    lines = ["**Your connected Shakes & Fidget accounts:**"]
    for world, user, auto, last_check, last_status in rows:
        state = "✅ on" if auto else "🔕 off"
        when = last_check[:16].replace("T", " ") if last_check else "never"
        lines.append(f"• `{world}` as **{user}** — hourly checks {state}, last check: {when}")
        if last_status:
            lines.append(f"  ↳ {last_status}")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)


# ---------------------------------------------------------------------------
# BACKGROUND MONITOR
# ---------------------------------------------------------------------------

class SFMonitor:
    """Hourly loop that probes every enabled account and posts attack alerts.

    Kept as a class (rather than a bare @tasks.loop function) so it can hold
    a reference to the bot for channel/role lookups without relying on
    module-level globals.
    """

    def __init__(self, bot: discord.Client) -> None:
        self.bot = bot
        self._loop = tasks.loop(hours=1)(self._run)
        self._loop.before_loop(self._before)

    def start(self) -> None:
        self._loop.start()

    def stop(self) -> None:
        self._loop.cancel()

    async def _before(self) -> None:
        # Don't probe before the bot can resolve channels/roles.
        await self.bot.wait_until_ready()

    async def _run(self) -> None:
        accounts = _get_active_accounts()
        if not accounts:
            return
        print(f"sf_auth: hourly check running for {len(accounts)} account(s)")

        for acc in accounts:
            try:
                await self._check_account(acc)
            except Exception as exc:  # noqa: BLE001
                # One bad account must never kill the loop for the others.
                print(f"sf_auth: unexpected error checking {acc['world_name']}: {exc}")

    async def _check_account(self, acc: dict[str, Any]) -> None:
        guild_id = acc["guild_id"]
        world = acc["world_name"]

        # Decrypt only for the duration of this call.
        try:
            password = decrypt_password(acc["password_enc"])
        except EncryptionUnavailable as exc:
            print(f"sf_auth: cannot decrypt for {world}: {exc}")
            return
        except Exception:
            # Wrong key or corrupted blob — flag it so the user can re-login
            # rather than silently failing forever.
            _record_check(guild_id, acc["discord_user_id"], world,
                          "❌ stored password could not be decrypted — please re-run /gt_sf_login")
            await self._dm_user(acc["discord_user_id"],
                                f"⚠️ Your stored S&F credentials for `{world}` could not be "
                                f"decrypted (the encryption key may have changed). "
                                f"Please run `/gt_sf_login` again to reconnect.")
            return

        result = await run_probe(world, acc["sf_username"], password)
        del password  # drop the plaintext reference as soon as we're done

        if not result.get("ok"):
            error = str(result.get("error", "unknown error"))

            # Distinguish "your login stopped working" (needs the user to act)
            # from a transient network blip (which will just retry next hour).
            #
            # IMPORTANT: this keyword match is a guess, not a verified signal —
            # sf_probe.rs reports the raw Rust Debug text of whatever error the
            # sf-api crate returned, and words like "invalid"/"auth" can show
            # up in plenty of TRANSIENT error variants too (a dropped
            # connection during the game's daily server reset, a momentary
            # session hiccup, etc.), not just an actually-wrong password.
            # A single match is therefore not trustworthy on its own — we only
            # tell the user to re-login once the SAME classification happens
            # on two checks in a row, an hour apart. That one-off midnight-ish
            # blips (which recover on their own next hour) don't turn into a
            # false "your password changed" alert.
            looks_like_bad_creds = any(
                tok in error.lower() for tok in ("wrong pass", "invalid", "auth", "password")
            )
            previously_suspected = str(acc.get("last_status") or "").startswith("❌ CRED_SUSPECT:")

            if looks_like_bad_creds and previously_suspected:
                _record_check(guild_id, acc["discord_user_id"], world, f"❌ {error[:150]}")
                await self._dm_user(
                    acc["discord_user_id"],
                    f"⚠️ Automatic S&F check for `{world}` failed: the stored credentials were "
                    f"rejected on two checks in a row. This usually means the password changed.\n"
                    f"Run `/gt_sf_login` to reconnect — hourly checks will keep failing until then."
                )
            elif looks_like_bad_creds:
                # First occurrence — record it as "suspected" but don't alarm
                # the user yet; next hour's check will confirm or clear it.
                _record_check(guild_id, acc["discord_user_id"], world, f"❌ CRED_SUSPECT: {error[:150]}")
            else:
                _record_check(guild_id, acc["discord_user_id"], world, f"❌ {error[:150]}")
            return

        characters = result.get("characters", [])
        login_method = result.get("login_method")

        # ------------------------------------------------------------------
        # Every character is now tagged with its REAL server (see
        # sf_probe.rs: SimpleSession.server_url(), a genuine public getter
        # confirmed via the crate's own generated docs — not a guess). This
        # replaced an earlier heuristic that tried to infer the right
        # character by matching login username to character name, which
        # was a plausible-looking guess rather than a verified fact, and
        # produced at least one real mislabeled alert (a guild on S20
        # showed up tagged "S19.SFGAME.EU") before this fix.
        #
        # Filtering is now exact: only characters whose verified `server`
        # matches this registration's world_name are ever alerted on. This
        # also means a probe that (for whatever reason) omits `server` for
        # a character — e.g. an older probe binary that hasn't been
        # rebuilt yet — is treated as unverifiable and skipped, rather than
        # silently trusted the way an unlabelled character used to be.
        # ------------------------------------------------------------------
        matches = [c for c in characters if c.get("server", "").lower() == world.lower()]
        skipped = len(characters) - len(matches)
        if skipped:
            print(f"sf_auth: {world} — ignored {skipped} character(s) from this probe response "
                  f"belonging to a different (or unverified) server")
        characters = matches

        if not characters:
            _record_check(
                guild_id, acc["discord_user_id"], world,
                f"⚠️ No character found on {world} in this login's response "
                f"({login_method or 'unknown'} login). Re-run /gt_sf_login if this persists."
            )
            return

        _record_check(guild_id, acc["discord_user_id"], world,
                      f"✅ ok — {len(characters)} character(s)")

        for char in characters:
            if not char.get("guild"):
                continue
            for kind in ("attacking", "defending"):
                battle = char.get(kind)
                if battle:
                    await self._maybe_alert(guild_id, world, kind, char, battle)

    async def _maybe_alert(self, guild_id: str, world: str, kind: str,
                           char: dict[str, Any], battle: dict[str, Any]) -> None:
        """Posts an attack/defense alert, once per distinct battle.

        Channel routing: prefers the per-kind channel from
        world_notify_config if configured (there is currently no command
        that sets attack_channel_id/defense_channel_id — see attack_alert.py's
        module docstring); falls back to attack_config's single channel
        (/gt_attack_setup) otherwise. The ping role always comes from
        attack_config — world_notify_config doesn't configure a separate
        role, it only holds per-kind channel overrides + defense muting.

        mute_defense: if set for this world (via /gt_attack_setup), a
        defending battle is marked as alerted WITHOUT actually sending
        anything — i.e. "acknowledge and suppress", not "defer until
        unmuted". If you'd rather a later /gt_attack_setup unmute cause a
        backlog of suppressed alerts to fire, that's a one-line change
        (skip the _mark_alerted call below instead).
        """
        battle_date = str(battle.get("date", ""))
        if _already_alerted(guild_id, world, kind, battle_date):
            return  # already announced this exact battle

        # Import here (not at module top) to avoid a circular import at
        # startup, since attack_alert.py doesn't need anything from sf_auth.py.
        from attack_alert import get_notify_config

        notify_cfg = get_notify_config(int(guild_id), world)

        if kind == "defending" and notify_cfg and notify_cfg["mute_defense"]:
            _mark_alerted(guild_id, world, kind, battle_date)  # suppressed, not deferred — see docstring
            return

        # Reuse the per-world role configured via /gt_attack_setup; prefer
        # world_notify_config's per-kind channel if set (currently only
        # mute_defense is ever written there), else fall back to
        # attack_config's single channel.
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT channel_id, role_id FROM attack_config WHERE guild_id=? AND world_name=?",
            (guild_id, world.lower())
        ).fetchone()
        conn.close()

        fallback_channel_id, role_id = row if row else (None, None)

        if notify_cfg and kind == "attacking" and notify_cfg["attack_channel_id"]:
            channel_id = notify_cfg["attack_channel_id"]
        elif notify_cfg and kind == "defending" and notify_cfg["defense_channel_id"]:
            channel_id = notify_cfg["defense_channel_id"]
        else:
            channel_id = fallback_channel_id

        if not channel_id:
            print(f"sf_auth: {world} has a {kind} battle but no channel configured "
                  f"(checked world_notify_config and /gt_attack_setup) — skipping alert")
            return

        guild_obj = self.bot.get_guild(int(guild_id))
        if not guild_obj:
            return
        channel = guild_obj.get_channel(int(channel_id))
        if not channel:
            return
        role = guild_obj.get_role(int(role_id)) if role_id else None

        # Timezone-aware formatting using the guild's configured timezone.
        when = format_battle_time(battle_date, guild_id)
        opponent = battle.get("opponent") or f"guild #{battle.get('opponent_id')}"

        if kind == "attacking":
            title = "⚔️ Guild Attack Scheduled"
            body = f"**{char['guild']}** is attacking **{opponent}**"
            colour = discord.Color.dark_red()
        else:
            title = "🛡️ Incoming Attack!"
            body = f"**{opponent}** is attacking **{char['guild']}**"
            colour = discord.Color.orange()

        embed = discord.Embed(title=title, description=body, color=colour)
        embed.add_field(name="🕒 Time", value=when, inline=True)
        embed.add_field(name="🌍 World", value=world.upper(), inline=True)
        embed.set_footer(text="Detected automatically by hourly guild check")

        try:
            await channel.send(content=role.mention if role else None, embed=embed)
            _mark_alerted(guild_id, world, kind, battle_date)
        except discord.Forbidden:
            print(f"sf_auth: missing permission to post {kind} alert in channel {channel_id}")

    async def _dm_user(self, discord_user_id: str, message: str) -> None:
        """Best-effort DM — a user with closed DMs shouldn't break the loop."""
        try:
            user = self.bot.get_user(int(discord_user_id)) or \
                   await self.bot.fetch_user(int(discord_user_id))
            if user:
                await user.send(message)
        except (discord.Forbidden, discord.HTTPException):
            pass
