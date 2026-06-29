"""
ONE-TIME migration script — run AFTER migrate_to_multiserver.py (if you needed
that one) and BEFORE deploying the main.py version that removes /wg_root and
/wg_set_logs.

What it does: for every guild that already has settings in the old
`ustawienia` table (kanal_glowy / kanal_logow) but does NOT yet have a row in
the new `guild_config` table, it copies those two channel IDs over and fills
the new fields (timezone, language, modules, api_token) with defaults — so
your already-configured server doesn't need to click through the wizard
again just to keep working.

Servers that have never been configured at all are untouched; they'll go
through /setup normally.

Usage:
    python migrate_settings_to_guild_config.py

Safe to re-run: only inserts for guilds missing from guild_config.
"""
import sqlite3
import json
import secrets
from datetime import datetime, timezone

DB_PATH = "gildia.db"


def ensure_guild_config_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS guild_config (
            guild_id      TEXT PRIMARY KEY,
            main_channel  TEXT,
            logs_channel  TEXT,
            timezone      TEXT,
            language      TEXT,
            admin_role    TEXT,
            modules       TEXT,
            api_token     TEXT,
            configured_at TIMESTAMP,
            configured_by TEXT
        )
        """
    )


def table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def main():
    conn = sqlite3.connect(DB_PATH)
    ensure_guild_config_table(conn)

    if not table_exists(conn, "ustawienia"):
        print("No 'ustawienia' table found — nothing to migrate.")
        conn.close()
        return

    cols = [row[1] for row in conn.execute("PRAGMA table_info(ustawienia)").fetchall()]
    if "guild_id" not in cols:
        print("ERROR: 'ustawienia' has no guild_id column yet.")
        print("Run migrate_to_multiserver.py first, then re-run this script.")
        conn.close()
        return

    guild_ids = [r[0] for r in conn.execute("SELECT DISTINCT guild_id FROM ustawienia").fetchall()]

    migrated, skipped = 0, 0
    for gid in guild_ids:
        already = conn.execute(
            "SELECT 1 FROM guild_config WHERE guild_id=?", (gid,)
        ).fetchone()
        if already:
            skipped += 1
            continue

        main_channel = conn.execute(
            "SELECT wartosc FROM ustawienia WHERE guild_id=? AND klucz='kanal_glowy'", (gid,)
        ).fetchone()
        logs_channel = conn.execute(
            "SELECT wartosc FROM ustawienia WHERE guild_id=? AND klucz='kanal_logow'", (gid,)
        ).fetchone()

        conn.execute(
            """INSERT INTO guild_config
               (guild_id, main_channel, logs_channel, timezone, language,
                admin_role, modules, api_token, configured_at, configured_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                gid,
                main_channel[0] if main_channel else None,
                logs_channel[0] if logs_channel else None,
                "UTC",                              # default — admin can change via a future settings command
                "en",                               # default
                None,                               # no admin_role on record yet
                json.dumps(["anti_phishing"]),       # matches current always-on behavior
                secrets.token_urlsafe(32),
                datetime.now(timezone.utc).isoformat(),
                "migration_script",
            ),
        )
        migrated += 1

    conn.commit()
    conn.close()
    print(f"Migrated {migrated} guild(s) into guild_config. Skipped {skipped} (already present).")


if __name__ == "__main__":
    main()
