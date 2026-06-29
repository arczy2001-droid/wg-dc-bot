"""
ONE-TIME migration script.

Run this ONCE, before switching to the new multi-server bot code, while the
bot is stopped. It takes your existing single-server database (old schema,
no guild_id column) and rewrites it into the new schema, attaching all your
existing worlds/members/reports/absences/settings to the Discord server ID
you provide.

Usage:
    python migrate_to_multiserver.py <YOUR_SERVER_ID>

To get YOUR_SERVER_ID: enable Developer Mode in Discord (User Settings ->
Advanced -> Developer Mode), then right-click your server icon -> "Copy
Server ID".

Safe to re-run: if it detects the database has already been migrated
(guild_id column already present), it exits without touching anything.
"""
import sqlite3
import sys
import shutil
import datetime

DB_PATH = "gildia.db"


def already_migrated(conn) -> bool:
    cols = [row[1] for row in conn.execute("PRAGMA table_info(ustawienia)").fetchall()]
    return "guild_id" in cols


def table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def main():
    if len(sys.argv) < 2:
        print("Usage: python migrate_to_multiserver.py <YOUR_SERVER_ID>")
        sys.exit(1)

    guild_id = sys.argv[1].strip()
    if not guild_id.isdigit():
        print(f"'{guild_id}' doesn't look like a Discord server ID (should be all digits).")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)

    if already_migrated(conn):
        print("This database already has the new (guild_id) schema. Nothing to do.")
        conn.close()
        return

    backup_path = f"gildia_backup_{datetime.datetime.now():%Y%m%d_%H%M%S}.db"
    shutil.copy(DB_PATH, backup_path)
    print(f"Backed up old database to: {backup_path}")

    c = conn.cursor()

    # ---- ustawienia (settings) ----
    if table_exists(conn, "ustawienia"):
        old_rows = c.execute("SELECT klucz, wartosc FROM ustawienia").fetchall()
        c.execute("ALTER TABLE ustawienia RENAME TO ustawienia_old")
        c.execute("CREATE TABLE ustawienia (guild_id TEXT, klucz TEXT, wartosc TEXT, PRIMARY KEY (guild_id, klucz))")
        for klucz, wartosc in old_rows:
            c.execute("INSERT INTO ustawienia VALUES (?, ?, ?)", (guild_id, klucz, wartosc))
        c.execute("DROP TABLE ustawienia_old")
        print(f"Migrated {len(old_rows)} setting(s).")
    else:
        c.execute("CREATE TABLE ustawienia (guild_id TEXT, klucz TEXT, wartosc TEXT, PRIMARY KEY (guild_id, klucz))")

    # ---- swiaty (worlds) ----
    if table_exists(conn, "swiaty"):
        old_rows = c.execute("SELECT nazwa, kanal_id FROM swiaty").fetchall()
        c.execute("ALTER TABLE swiaty RENAME TO swiaty_old")
        c.execute("CREATE TABLE swiaty (guild_id TEXT, nazwa TEXT, kanal_id TEXT, PRIMARY KEY (guild_id, nazwa))")
        for nazwa, kanal_id in old_rows:
            c.execute("INSERT INTO swiaty VALUES (?, ?, ?)", (guild_id, nazwa, kanal_id))
        c.execute("DROP TABLE swiaty_old")
        print(f"Migrated {len(old_rows)} world(s).")
    else:
        c.execute("CREATE TABLE swiaty (guild_id TEXT, nazwa TEXT, kanal_id TEXT, PRIMARY KEY (guild_id, nazwa))")

    # ---- czlonkowie (members) ----
    if table_exists(conn, "czlonkowie"):
        old_rows = c.execute("SELECT swiat, nick FROM czlonkowie").fetchall()
        c.execute("ALTER TABLE czlonkowie RENAME TO czlonkowie_old")
        c.execute("CREATE TABLE czlonkowie (guild_id TEXT, swiat TEXT, nick TEXT, PRIMARY KEY (guild_id, swiat, nick))")
        for swiat, nick in old_rows:
            c.execute("INSERT INTO czlonkowie VALUES (?, ?, ?)", (guild_id, swiat, nick))
        c.execute("DROP TABLE czlonkowie_old")
        print(f"Migrated {len(old_rows)} member(s).")
    else:
        c.execute("CREATE TABLE czlonkowie (guild_id TEXT, swiat TEXT, nick TEXT, PRIMARY KEY (guild_id, swiat, nick))")

    # ---- raporty (reports) ----
    if table_exists(conn, "raporty"):
        old_rows = c.execute("SELECT swiat, data_wpisu FROM raporty").fetchall()
        c.execute("ALTER TABLE raporty RENAME TO raporty_old")
        c.execute("CREATE TABLE raporty (guild_id TEXT, swiat TEXT, data_wpisu TIMESTAMP)")
        for swiat, data_wpisu in old_rows:
            c.execute("INSERT INTO raporty VALUES (?, ?, ?)", (guild_id, swiat, data_wpisu))
        c.execute("DROP TABLE raporty_old")
        print(f"Migrated {len(old_rows)} report(s).")
    else:
        c.execute("CREATE TABLE raporty (guild_id TEXT, swiat TEXT, data_wpisu TIMESTAMP)")

    # ---- nieobecnosci (absences) ----
    if table_exists(conn, "nieobecnosci"):
        old_rows = c.execute("SELECT swiat, nick, data_wpisu FROM nieobecnosci").fetchall()
        c.execute("ALTER TABLE nieobecnosci RENAME TO nieobecnosci_old")
        c.execute("CREATE TABLE nieobecnosci (guild_id TEXT, swiat TEXT, nick TEXT, data_wpisu TIMESTAMP)")
        for swiat, nick, data_wpisu in old_rows:
            c.execute("INSERT INTO nieobecnosci VALUES (?, ?, ?, ?)", (guild_id, swiat, nick, data_wpisu))
        c.execute("DROP TABLE nieobecnosci_old")
        print(f"Migrated {len(old_rows)} absence record(s).")
    else:
        c.execute("CREATE TABLE nieobecnosci (guild_id TEXT, swiat TEXT, nick TEXT, data_wpisu TIMESTAMP)")

    conn.commit()
    conn.close()
    print(f"\nDone. All existing data has been attached to server ID {guild_id}.")
    print("You can now start the new (multi-server) bot version.")


if __name__ == "__main__":
    main()
