"""
sf_absence.py
=============
Parse Shakes & Fidget guild-attack reports into an absentee list, and drive
the hourly automated check that replaces the old /wg screenshot-OCR flow.

DATA SOURCE — why this works despite the Unity canvas:
    The S&F web client is a Unity WebGL app: the entire UI (including the
    mailbox) is painted into a single <canvas>, so there is NO HTML to scrape
    and no DOM selectors to click. HOWEVER, Unity still fetches the mail data
    from the game server over the network as plain text BEFORE painting it.
    We intercept that network response with Playwright and parse it directly,
    bypassing the canvas entirely. (Confirmed by live capture: the response is
    unencrypted at the browser boundary.)

FORMAT — validated against two independent real battles (different opponents,
different absentees, both matched the in-game "Niezarejestrowani członkowie"
screen exactly, including correctly NOT flagging a boundary player who fought):

    messagetext.s: <type> / <OPPONENT_GUILD> / <player groups...>
    each player group = flag / id / name / level / class     (5 tokens)
        flag == "1"  -> participated (present)
        flag == "0"  -> absent
    <type> is the same code as in systemmessagelist: "2a" = guild attack,
    "2d" = guild defense.

    Player groups begin at token index 2. Verified arithmetically against a
    real 50-member report: 252 tokens == 2 + 50*5 exactly, zero remainder,
    and the group count equals the guild member count.

    CORRECTED 2026-08: this previously used offset 7, which silently skipped
    member #0 (the guild leader). Both earlier validations still matched
    because that member had participated in both battles, so the omission was
    invisible — but a leader who skips an attack would never be reported.
    There is no truncated final group; that was an artifact of the bad offset.

    messagelist.r: <msg_id>,<sender>,<tab>,<subject>,<unix_ts>; ...
        used to find WHICH inbox entry is the attack report and to dedupe by
        msg_id (a stable server-side id — more reliable than timestamp).
"""

from __future__ import annotations

from typing import Optional


# ---------------------------------------------------------------------------
# PARSING  (pure functions, no I/O — unit-testable, and already validated)
# ---------------------------------------------------------------------------

def extract_section(raw_response: str, key: str) -> str:
    """Pull one '<key>:' section out of a full S&F network response body.
    Sections are '&'-delimited; returns '' if the key isn't present."""
    marker = f"{key}:"
    if marker not in raw_response:
        return ""
    return raw_response.split(marker, 1)[1].split("&", 1)[0]


def parse_absent(messagetext_section: str) -> tuple[str, list[str]]:
    """
    Input: the raw string of the 'messagetext.s' section (after the colon,
           before the next '&').
    Returns: (opponent_guild_name, [absent_player_names])

    A player is absent iff their group's leading 'flag' token == "0".
    Validated exact-match on two real reports (Evil Returns, SANSIBAR).
    """
    tokens = messagetext_section.split("/")
    if len(tokens) < 7:
        return ("", [])

    opponent = tokens[1]
    absent: list[str] = []

    # Player groups start immediately after <type>/<opponent>. Offset 2, not 7:
    # offset 7 drops the first member from consideration entirely.
    i = 2
    while i + 4 < len(tokens):
        flag, _id, name, _lvl, _cls = tokens[i:i + 5]
        if flag == "0":
            absent.append(name)
        i += 5

    # A well-formed report divides exactly, so this should never fire. Kept as
    # a guard: if the server ever sends a short final group, a leading "0"
    # still means absent and the name is the third token.
    if i < len(tokens):
        rem = tokens[i:]
        if len(rem) >= 3 and rem[0] == "0":
            absent.append(rem[2])

    return (opponent, absent)


# ---------------------------------------------------------------------------
# MAIL LIST  — find the newest guild-attack report and its msg_id
# ---------------------------------------------------------------------------

# The exact subject the attack report arrives under. VERIFY per account/
# language — on the captured account the guild-attack system messages weren't
# in the personal inbox subject list at all (they're system messages), so we
# match the messagetext TYPE code instead where possible. See find_attack_*.
ATTACK_SUBJECT_HINTS = (
    "Raport z walki gildii",   # Polish
    "Guild fight report",      # English fallback
)


def parse_messagelist(messagelist_section: str) -> list[dict]:
    """
    'messagelist.r' rows: msg_id,sender,tab,subject,unix_ts ; ...
    Returns a list of dicts. Robust to the trailing ';;' seen in captures.
    """
    out = []
    for row in messagelist_section.split(";"):
        row = row.strip()
        if not row:
            continue
        parts = row.split(",")
        if len(parts) < 5:
            continue
        msg_id, sender, tab, subject, ts = parts[0], parts[1], parts[2], ",".join(parts[3:-1]), parts[-1]
        try:
            out.append({
                "msg_id": int(msg_id),
                "sender": sender,
                "tab": tab,
                "subject": subject,
                "unix_ts": int(ts),
            })
        except ValueError:
            continue
    return out
