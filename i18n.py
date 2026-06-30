"""
i18n.py
=======
Internationalization (i18n) system for the bot.

FOLDER STRUCTURE:
    locales/
        en_US.json   <- default / fallback language. Must always exist and
                        contain every key the bot uses — it's the safety net.
        pl_PL.json
        de_DE.json
        ... add more by dropping in a new locales/<code>.json file.

JSON STRUCTURE (nested by feature area, dot-notation key access):
    {
      "setup": {
        "main_channel_set": "✅ Main channel set to {channel}."
      },
      "errors": {
        "no_permission": "❌ You need the **{permission}** permission to use this command."
      }
    }
    -> get_text(guild_id, "setup.main_channel_set", channel="#general")
    -> get_text(guild_id, "errors.no_permission", permission="Administrator")

INTEGRATION (in your main bot file):
    from i18n import translator

    # in setup_hook, before tree.sync() — language IDs in guild_config must
    # already exist as locale files by the time the bot starts handling events:
    #   (nothing to call here — `translator` loads its files at import time,
    #    see the singleton at the bottom of this module)

    # in setup_wizard.py, after a guild finishes the wizard:
    translator.set_guild_language(state.guild_id, state.language)

USAGE — see the bottom of this file for full inline examples covering both
an app_commands.Command and an event listener.
"""

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("i18n")

DEFAULT_LANGUAGE = "en_US"
LOCALES_DIR = Path(__file__).parent / "locales"
DB_PATH = "gildia.db"


class Translator:
    """
    Loads every locale JSON file into memory ONCE (at construction time —
    not per-message, not per-command), caches each guild's chosen language
    so the language lookup is a single DB read per guild rather than per
    message, and resolves dotted translation keys (e.g. "errors.no_permission")
    with automatic fallback to DEFAULT_LANGUAGE when a key — or an entire
    locale file — is missing.
    """

    def __init__(self, locales_dir: Path = LOCALES_DIR, default_language: str = DEFAULT_LANGUAGE) -> None:
        self.default_language: str = default_language
        self._translations: Dict[str, Dict[str, Any]] = {}
        self._guild_language_cache: Dict[int, str] = {}
        self._load_all_locales(locales_dir)

    # ------------------------------------------------------------------
    # LOADING — happens once, at startup
    # ------------------------------------------------------------------
    def _load_all_locales(self, locales_dir: Path) -> None:
        """Reads every *.json file in locales_dir into memory exactly once."""
        if not locales_dir.exists():
            raise FileNotFoundError(f"Locales directory not found: {locales_dir}")

        for path in sorted(locales_dir.glob("*.json")):
            language_code = path.stem  # "en_US.json" -> "en_US"
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self._translations[language_code] = json.load(f)
                logger.info(f"Loaded locale '{language_code}' from {path.name}")
            except (json.JSONDecodeError, OSError) as e:
                logger.error(f"Failed to load locale file {path}: {e}")

        if self.default_language not in self._translations:
            raise RuntimeError(
                f"Default language '{self.default_language}' has no locale file in {locales_dir}. "
                "The fallback mechanism requires it to exist."
            )

    def reload(self, locales_dir: Path = LOCALES_DIR) -> None:
        """Hot-reload every translation file without restarting the bot
        (handy after editing copy — wire this to an owner-only debug command if useful)."""
        self._translations.clear()
        self._load_all_locales(locales_dir)

    # ------------------------------------------------------------------
    # GUILD LANGUAGE — DB-backed, cached so we don't query per message
    # ------------------------------------------------------------------
    def get_guild_language(self, guild_id: int) -> str:
        """Returns the guild's configured language. Hits the DB only on a cache miss."""
        cached = self._guild_language_cache.get(guild_id)
        if cached is not None:
            return cached

        language = self._fetch_language_from_db(guild_id)
        self._guild_language_cache[guild_id] = language
        return language

    def _fetch_language_from_db(self, guild_id: int) -> str:
        conn = sqlite3.connect(DB_PATH)
        try:
            row = conn.execute(
                "SELECT language FROM guild_config WHERE guild_id = ?", (str(guild_id),)
            ).fetchone()
        finally:
            conn.close()

        if row and row[0] and row[0] in self._translations:
            return row[0]
        return self.default_language  # unconfigured guild, or a language code we don't have a file for

    def set_guild_language(self, guild_id: int, language_code: str) -> None:
        """Call this immediately whenever a guild's language changes (e.g. right
        after /setup saves it, or from a future /language command) — otherwise
        the cache keeps serving the old value until it happens to expire/restart."""
        self._guild_language_cache[guild_id] = (
            language_code if language_code in self._translations else self.default_language
        )

    def invalidate_guild_cache(self, guild_id: int) -> None:
        """Forces the next lookup for this guild to re-read the DB instead of the cache."""
        self._guild_language_cache.pop(guild_id, None)

    # ------------------------------------------------------------------
    # KEY RESOLUTION
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_key(translations: Dict[str, Any], dotted_key: str) -> Optional[str]:
        """Walks a dotted key like 'errors.no_permission' through nested dicts."""
        node: Any = translations
        for part in dotted_key.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node if isinstance(node, str) else None

    def get_raw_or_none(self, language_code: str, key: str) -> Optional[str]:
        """Looks up `key` in exactly the given language — no fallback, no
        formatting. Returns None if missing. Used by CommandTranslator
        (command_translator.py), where None has a specific meaning to
        discord.py: 'no localization for this locale, show the literal
        default text instead' — Discord handles that fallback itself."""
        return self._resolve_key(self._translations.get(language_code, {}), key)

    def get_text(self, guild_id: int, key: str, **kwargs: Any) -> str:
        """
        Main entry point. Resolves `key` in the guild's language, formats it
        with kwargs, and falls back to the default language if the key is
        missing there — or to a visibly-broken placeholder if it's missing
        from every locale (so a typo'd key is obvious in testing instead of
        silently rendering blank text in production).
        """
        language = self.get_guild_language(guild_id)

        text = self._resolve_key(self._translations.get(language, {}), key)
        if text is None and language != self.default_language:
            logger.warning(f"Missing key '{key}' in locale '{language}' — falling back to '{self.default_language}'.")
            text = self._resolve_key(self._translations.get(self.default_language, {}), key)

        if text is None:
            logger.error(f"Missing key '{key}' in every locale, including the default.")
            return f"[[{key}]]"

        try:
            return text.format(**kwargs)
        except KeyError as e:
            logger.error(f"Translation '{key}' ({language}) expects placeholder {e}, but it wasn't supplied.")
            return text  # show unformatted text rather than crash the calling command


# ---------------------------------------------------------------------------
# SINGLETON — instantiated at import time, so "loaded when the bot starts"
# means exactly that: this line runs as part of `import i18n`, before
# setup_hook or any event handler fires.
# ---------------------------------------------------------------------------
translator = Translator()


# ---------------------------------------------------------------------------
# USAGE EXAMPLES (for reference — not executed)
# ---------------------------------------------------------------------------
"""
--- Inside an app_commands.Command (slash command) ---

    from i18n import translator

    @bot.tree.command(name="wg_absent_list", description="List of absences")
    async def wg_absent_list(interaction: discord.Interaction, swiat: str):
        if not await sprawdz_pozwolenie(interaction):
            return
        ...
        if not swiat_data:
            await interaction.followup.send(
                translator.get_text(interaction.guild_id, "wg.unknown_world")
            )
            return


--- Inside an event listener (on_message) ---

    from i18n import translator

    @bot.event
    async def on_message(message: discord.Message):
        ...
        if await is_malicious_domain(domena, session):
            await message.delete()
            await message.channel.send(
                translator.get_text(
                    message.guild.id,
                    "security.phishing_blocked",
                    mention=message.author.mention,
                ),
                delete_after=10,
            )
"""
