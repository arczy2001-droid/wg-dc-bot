// ============================================================================
// sf_probe.rs — machine-readable guild status probe
// ----------------------------------------------------------------------------
// Companion binary for the Python Discord bot. Unlike sf_login_check.rs (which
// is interactive), this reads credentials from STDIN and writes a single JSON
// object to STDOUT so Python can parse it.
//
// WHY STDIN AND NOT COMMAND-LINE ARGUMENTS:
// Anything passed as an argv parameter is visible to every user on the machine
// via `ps aux`. Reading the password from stdin keeps it out of the process
// table entirely.
//
// PER-CHARACTER SERVER IDENTIFICATION:
// SimpleSession::server_url() is a real public getter (confirmed via the
// crate's own generated rustdoc, not guessed) returning the real server this
// specific session authenticated against. It's called once per session, so
// on an SSO login — which returns one session per character across every
// world tied to the account — each character is tagged with its OWN true
// server rather than inherited from whichever world the caller happened to
// be checking. This replaced an earlier approach that tried to infer the
// right character by matching login username to character name, which was
// a heuristic guess rather than a verified fact.
//
// INPUT (three lines on stdin):
//     <server>\n<username>\n<password>\n
//
// OUTPUT (single-line JSON on stdout), e.g.:
//     {"ok":true,"login_method":"sso","characters":[{"name":"ArczY",
//      "server":"s20.sfgame.eu","guild":"The Worldguard",
//      "attacking":{"opponent":"Some Guild","opponent_id":123,"date":"..."},
//      "defending":null,"next_attack_possible":"..."}]}
// or on failure:
//     {"ok":false,"error":"wrong pass"}
//
// Exit code is 0 whenever JSON was produced (even for auth failures) so the
// caller can distinguish "the game said no" from "the binary itself broke".
// ============================================================================

use sf_api::command::Command;
use sf_api::gamestate::guild::Guild;
use sf_api::session::SimpleSession;
use std::io::{self, BufRead};

#[tokio::main]
async fn main() {
    let mut lines = io::stdin().lock().lines();
    let server = lines.next().and_then(|l| l.ok()).unwrap_or_default();
    let username = lines.next().and_then(|l| l.ok()).unwrap_or_default();
    let password = lines.next().and_then(|l| l.ok()).unwrap_or_default();

    if server.is_empty() || username.is_empty() || password.is_empty() {
        println!(r#"{{"ok":false,"error":"missing credentials on stdin"}}"#);
        return;
    }

    // Try the per-server login first, then fall back to the unified S&F
    // Account (SSO) login — same two-path logic proven in sf_login_check.rs.
    //
    // Every character is now tagged with its ACTUAL server via
    // session.server_url() — a real public getter on SimpleSession
    // (confirmed via the crate's own generated docs, not guessed). This
    // replaces the earlier name-matching heuristic entirely: instead of
    // inferring "this is probably the right character because the name
    // looks similar," the Python side can now filter on an exact, verified
    // server match. login_method is still included for diagnostics, but is
    // no longer load-bearing for correctness.
    let mut entries: Vec<String> = Vec::new();
    let login_method: &str;

    match SimpleSession::login(&username, &password, &server).await {
        Ok(mut session) => {
            login_method = "per_server";
            let session_server = server_host(&session);
            if let Some(gs) = session.game_state() {
                entries.push(character_json(&gs.character.name, gs.guild.as_ref(), &session_server));
            }
        }
        Err(per_server_err) => {
            match SimpleSession::login_sf_account(&username, &password).await {
                Ok(sessions) => {
                    login_method = "sso";
                    for mut session in sessions {
                        // Read the server BEFORE send_command/game_state
                        // consume/mutate the session further, one call per
                        // session since each character can genuinely be on
                        // a different real server.
                        let session_server = server_host(&session);
                        if session.send_command(Command::Update).await.is_err() {
                            continue;
                        }
                        if let Some(gs) = session.game_state() {
                            entries.push(character_json(&gs.character.name, gs.guild.as_ref(), &session_server));
                        }
                    }
                }
                Err(sso_err) => {
                    // Both login styles rejected the credentials.
                    println!(
                        r#"{{"ok":false,"error":{}}}"#,
                        json_string(&format!("{per_server_err:?} / {sso_err:?}"))
                    );
                    return;
                }
            }
        }
    }

    println!(
        r#"{{"ok":true,"login_method":{},"characters":[{}]}}"#,
        json_string(login_method),
        entries.join(",")
    );
}

/// Extracts just the hostname (e.g. "s29.sfgame.eu") from a session's real
/// server URL, without the scheme/path — matching the plain-domain format
/// already used everywhere else in this bot (world_name in the DB, etc.).
fn server_host(session: &SimpleSession) -> String {
    session
        .server_url()
        .host_str()
        .map(|h| h.to_string())
        .unwrap_or_default()
}

/// Builds the JSON object for one character, including guild battle status
/// and the REAL server this specific session authenticated against.
fn character_json(char_name: &str, guild: Option<&Guild>, session_server: &str) -> String {
    let Some(guild) = guild else {
        return format!(
            r#"{{"name":{},"server":{},"guild":null,"attacking":null,"defending":null,"next_attack_possible":null}}"#,
            json_string(char_name),
            json_string(session_server)
        );
    };

    // PlanedBattle.other is an opponent guild ID (u32), not a name — resolve
    // it against fightable_guilds where possible, same as sf_login_check.rs.
    let resolve = |id: u32| -> String {
        guild
            .fightable_guilds
            .iter()
            .find(|g| g.id == id)
            .map(|g| g.name.clone())
            .unwrap_or_default()
    };

    let battle_json = |b: Option<&sf_api::gamestate::guild::PlanedBattle>| -> String {
        match b {
            Some(b) => format!(
                r#"{{"opponent":{},"opponent_id":{},"date":{}}}"#,
                json_string(&resolve(b.other)),
                b.other,
                json_string(&b.date.to_rfc3339())
            ),
            None => "null".to_string(),
        }
    };

    let next_attack = match &guild.next_attack_possible {
        Some(dt) => json_string(&dt.to_rfc3339()),
        None => "null".to_string(),
    };

    format!(
        r#"{{"name":{},"server":{},"guild":{},"attacking":{},"defending":{},"next_attack_possible":{}}}"#,
        json_string(char_name),
        json_string(session_server),
        json_string(&guild.name),
        battle_json(guild.attacking.as_ref()),
        battle_json(guild.defending.as_ref()),
        next_attack
    )
}

/// Minimal JSON string escaper — avoids pulling in serde just for output.
fn json_string(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}
