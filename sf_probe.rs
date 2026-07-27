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
// INPUT (three lines on stdin):
//     <server>\n<username>\n<password>\n
//
// OUTPUT (single-line JSON on stdout), e.g.:
//     {"ok":true,"characters":[{"name":"ArczY","guild":"The Worldguard",
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
    // login_method is included in the output because it's the ONLY reliable
    // signal for whether the returned character(s) can be trusted to belong
    // to `server`: a per-server login authenticates against exactly one
    // world by construction, but SSO login returns every character across
    // every world tied to the account with no per-character world field
    // (sf-api's structs don't expose one — confirmed by reading the actual
    // crate docs, not guessed). The caller MUST NOT trust an SSO character's
    // world without independently verifying it (e.g. by name match) — even
    // if only one character comes back, since a single wrong character is
    // just as capable of producing a mislabeled alert as several.
    let mut entries: Vec<String> = Vec::new();
    let login_method: &str;

    match SimpleSession::login(&username, &password, &server).await {
        Ok(mut session) => {
            login_method = "per_server";
            if let Some(gs) = session.game_state() {
                entries.push(character_json(&gs.character.name, gs.guild.as_ref()));
            }
        }
        Err(per_server_err) => {
            match SimpleSession::login_sf_account(&username, &password).await {
                Ok(sessions) => {
                    login_method = "sso";
                    for mut session in sessions {
                        if session.send_command(Command::Update).await.is_err() {
                            continue;
                        }
                        if let Some(gs) = session.game_state() {
                            entries.push(character_json(&gs.character.name, gs.guild.as_ref()));
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

/// Builds the JSON object for one character, including guild battle status.
fn character_json(char_name: &str, guild: Option<&Guild>) -> String {
    let Some(guild) = guild else {
        return format!(
            r#"{{"name":{},"guild":null,"attacking":null,"defending":null,"next_attack_possible":null}}"#,
            json_string(char_name)
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
        r#"{{"name":{},"guild":{},"attacking":{},"defending":{},"next_attack_possible":{}}}"#,
        json_string(char_name),
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
