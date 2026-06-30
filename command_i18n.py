"""
command_i18n.py
================
Localizes slash COMMAND METADATA (names, descriptions, parameter
descriptions) — the text shown in Discord's "/" command picker.

THIS IS A DIFFERENT SYSTEM FROM i18n.py — READ THIS FIRST:

    i18n.py (Translator / get_text)
        Translates messages the BOT SENDS (replies, embeds, log entries).
        Driven by YOUR `guild_config.language` column — one language per
        SERVER, set via /setup. You call it yourself: translator.get_text(...).

    command_i18n.py (CommandTranslator, this file)
        Translates the command PICKER ITSELF — the name/description users
        see when they type "/" and browse commands, before they've even run
        anything. Driven by DISCORD'S OWN per-USER client language setting
        (Settings -> Language in the Discord app) — every user can see
        command names in their own language, even within the same server.
        You never call this directly; discord.py calls it automatically
        at CommandTree.sync() time, once per locale, for every command.

    Practical consequence: a Polish admin and a German admin in the SAME
    server (whose guild_config.language is, say, "en_US") will each see
    "/wg_worlds" with a description in their OWN client language in the
    picker — but the bot's actual reply text will be in the server's
    configured language. Both systems are correct to use together.

DISCORD'S LOCALE CODES ARE NOT THE SAME AS guild_config'S CODES:
    Discord uses its own short codes (discord.Locale): "en-US", "en-GB",
    "pl", "de", "fr", "ja", etc. Note "pl" and "de" have NO region suffix,
    unlike your message locales "pl_PL" / "de_DE". This module's locale
    files therefore live in their own folder (locales/commands/) keyed by
    Discord's codes, e.g. locales/commands/pl.json, NOT locales/commands/pl_PL.json.
    Full list: https://discord.com/developers/docs/reference#locales

FILE STRUCTURE:
    locales/commands/
        en-US.json   <- default / fallback, must be complete
        pl.json
        de.json

JSON STRUCTURE (keyed by command name, matching the `name=` you pass to
@bot.tree.command):
    {
      "wg_worlds": {
        "description": "List of the worlds"
      },
      "wg_add_member": {
        "description": "Assign players to a world",
        "options": {
          "swiat": "World name",
          "lista": "Player nicknames, separated by commas or new lines"
        }
      }
    }

INTEGRATION (in your main bot file, in setup_hook, BEFORE tree.sync()):

    from command_i18n import CommandTranslator

    async def setup_hook(self):
        ...
        await self.tree.set_translator(CommandTranslator())
        await self.tree.sync()

NOTHING ELSE CHANGES IN YOUR COMMAND DEFINITIONS. Because discord.py's
`auto_locale_strings` defaults to True, the plain `description="..."` you
already pass to @bot.tree.command and the parameter docs you'd add via
@app_commands.describe(...) are automatically eligible for translation —
you do not need to wrap them in `locale_str(...)` yourself.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

import discord
from discord import app_commands

logger = logging.getLogger("command_i18n")

DEFAULT_DISCORD_LOCALE = "en-US"
COMMAND_LOCALES_DIR = Path(__file__).parent / "locales" / "commands"


class CommandTranslator(app_commands.Translator):
    """
    discord.py calls `.translate(...)` once per command/parameter/locale
    combination at sync() time — not per-interaction — so this is cheap
    even with many locales.
    """

    def __init__(self, locales_dir: Path = COMMAND_LOCALES_DIR, default_locale: str = DEFAULT_DISCORD_LOCALE) -> None:
        self.default_locale = default_locale
        self._data: Dict[str, Dict[str, Any]] = {}
        self._load_all(locales_dir)

    def _load_all(self, locales_dir: Path) -> None:
        if not locales_dir.exists():
            raise FileNotFoundError(f"Command locales directory not found: {locales_dir}")

        for path in sorted(locales_dir.glob("*.json")):
            locale_code = path.stem  # "pl.json" -> "pl", "en-US.json" -> "en-US"
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self._data[locale_code] = json.load(f)
                logger.info(f"Loaded command locale '{locale_code}' from {path.name}")
            except (json.JSONDecodeError, OSError) as e:
                logger.error(f"Failed to load command locale file {path}: {e}")

        if self.default_locale not in self._data:
            raise RuntimeError(
                f"Default command locale '{self.default_locale}' has no file in {locales_dir}."
            )

    # ------------------------------------------------------------------
    # discord.py's required override
    # ------------------------------------------------------------------
    async def translate(
        self,
        string: app_commands.locale_str,
        locale: discord.Locale,
        context: app_commands.TranslationContext,
    ) -> Optional[str]:
        """
        Returning None tells discord.py "no translation available, keep the
        original string" — that's the correct fallback here (rather than
        manually re-implementing English text), since the original string
        IS the English text we wrote in the @command/@describe decorators.
        """
        locale_code = locale.value  # discord.Locale -> "pl", "de", "en-US", ...
        if locale_code not in self._data:
            return None  # we don't have this language at all -> Discord shows the original

        command_name, field = self._resolve_field_path(context)
        if command_name is None or field is None:
            return None  # context we don't have a mapping for (e.g. choice names) -> leave as-is

        entry = self._data[locale_code].get(command_name)
        if entry is None:
            logger.warning(f"No '{command_name}' entry in command locale '{locale_code}', falling back.")
            return None

        if field == "description":
            return entry.get("description")  # None -> discord.py falls back to original automatically

        # field is an option/parameter name
        option_name = field
        options = entry.get("options", {})
        translated = options.get(option_name)
        if translated is None:
            logger.warning(
                f"No translation for option '{option_name}' of command '{command_name}' "
                f"in locale '{locale_code}', falling back to original."
            )
        return translated

    @staticmethod
    def _resolve_field_path(context: app_commands.TranslationContext):
        """
        Maps a TranslationContext to (command_name, field) so translate()
        above can do a flat dict lookup. Returns (None, None) for contexts
        this implementation doesn't handle (e.g. choice names, group
        descriptions) — discord.py will just keep the original text for those.
        """
        location = context.location
        data = context.data

        if location is app_commands.TranslationContextLocation.command_description:
            # data is the Command object itself
            return getattr(data, "name", None), "description"

        if location is app_commands.TranslationContextLocation.parameter_description:
            # data is a Parameter; data.command is its owning Command
            command = getattr(data, "command", None)
            command_name = getattr(command, "name", None) if command else None
            param_name = getattr(data, "name", None)
            return command_name, param_name

        # command_name / parameter_name / group_* / choice_name are intentionally
        # NOT translated here — command and parameter NAMES (not descriptions)
        # must stay stable identifiers Discord can route reliably, and choices
        # aren't used anywhere in this bot yet.
        return None, None
