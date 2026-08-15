// ============================================================================
// sf_report_probe.rs — machine-readable guild ATTACK/DEFENSE report probe
// ----------------------------------------------------------------------------
// Fetches the latest guild battle report (attack "2a" or defense "2d") and
// prints its raw `messagetext.s` body as JSON on stdout, so the Python side
// (sf_absence.parse_absent) can extract who did not participate.
//
// TWO-PHASE FETCH (important):
// The list of report IDs (`systemmessagelist.r`) is NOT part of the Update
// response — it is only bundled by the server alongside a PlayerMessageView
// response. So we:
//   Phase 1: send PlayerMessageView:1 (open the first inbox message) purely to
//            obtain the bundled systemmessagelist.
//   Phase 2: from that list pick the newest battle report (2a or 2d), open it
//            by its base64 msg_id, and return the messagetext body.
//
// INPUT (stdin):  <server>\n<username>\n<password>\n
// OUTPUT (one JSON line):
//   {"ok":true,"msg_id":123,"kind":"attack","body":"messagetext.s:2a/..."}
//   {"ok":true,"msg_id":123,"kind":"defense","body":"messagetext.s:2d/..."}
//   {"ok":false,"error":"..."}
// ============================================================================

use base64::{engine::general_purpose::STANDARD as B64, Engine as _};
use sf_api::command::Command;
use sf_api::session::Session;
use sf_api::sso::SFAccount;
use std::io::{self, BufRead};

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

fn err_out(msg: &str) {
    println!(r#"{{"ok":false,"error":{}}}"#, json_string(msg));
}

fn server_host(session: &Session) -> String {
    session
        .server_url()
        .host_str()
        .map(|h| h.to_string())
        .unwrap_or_default()
}

/// Send Command::Custom "PlayerMessageView" with a single argument.
async fn player_message_view(session: &Session, arg: &str) -> Result<String, String> {
    let cmd = Command::Custom {
        cmd_name: "PlayerMessageView".to_string(),
        arguments: vec![arg.to_string()],
    };
    let resp = session
        .send_command_raw(cmd)
        .await
        .map_err(|e| format!("PlayerMessageView:{arg} failed: {e:?}"))?;
    Ok(resp.raw_response().to_string())
}

/// From a raw response containing "systemmessagelist.r:", pick the newest
/// battle report. Returns (msg_id, kind) where kind is "attack" or "defense".
/// Entry format: msg_id,0,1,14,created_ts,expires_ts,TYPE  (2a=attack, 2d=defense)
fn newest_battle_report(raw: &str) -> Option<(i64, &'static str)> {
    let section = raw.split("systemmessagelist.r:").nth(1)?.split('&').next()?;
    let mut best: Option<(i64, i64, &'static str)> = None; // (created_ts, msg_id, kind)
    for entry in section.split(';') {
        let entry = entry.trim();
        if entry.is_empty() {
            continue;
        }
        let f: Vec<&str> = entry.split(',').collect();
        if f.len() < 7 {
            continue;
        }
        let kind = match f[6] {
            "2a" => "attack",
            "2d" => "defense",
            _ => continue, // ignore type 17 and anything else
        };
        if let (Ok(msg_id), Ok(created)) = (f[0].parse::<i64>(), f[4].parse::<i64>()) {
            if best.map_or(true, |(bc, _, _)| created > bc) {
                best = Some((created, msg_id, kind));
            }
        }
    }
    best.map(|(_, id, kind)| (id, kind))
}

async fn fetch_report(session: &Session) -> Result<(i64, &'static str, String), String> {
    // Phase 1: open the first inbox message to obtain the bundled
    // systemmessagelist. If the inbox is empty, PlayerMessageView:1 may still
    // return the list section (which is what we actually need).
    let raw1 = player_message_view(session, "1").await?;

    let (msg_id, kind) =
        newest_battle_report(&raw1).ok_or("no attack/defense report found in message list")?;

    // Phase 2: open the chosen report by its base64-encoded msg_id.
    let b64_id = B64.encode(msg_id.to_string());
    let raw2 = player_message_view(session, &b64_id).await?;

    if !raw2.contains("messagetext.s:") {
        return Err("opened report had no messagetext body".to_string());
    }
    Ok((msg_id, kind, raw2))
}

#[tokio::main]
async fn main() {
    let mut lines = io::stdin().lock().lines();
    let server = lines.next().and_then(|l| l.ok()).unwrap_or_default();
    let username = lines.next().and_then(|l| l.ok()).unwrap_or_default();
    let password = lines.next().and_then(|l| l.ok()).unwrap_or_default();

    if server.is_empty() || username.is_empty() || password.is_empty() {
        err_out("missing credentials on stdin");
        return;
    }
    for (label, v) in [("server", &server), ("username", &username), ("password", &password)] {
        if v.contains('\n') || v.contains('\r') || v.contains('\0') {
            err_out(&format!("{label} contains a control character"));
            return;
        }
    }

    let account = match SFAccount::login(username.clone(), password.clone()).await {
        Ok(a) => a,
        Err(e) => {
            err_out(&format!("sso login failed: {e:?}"));
            return;
        }
    };
    let chars = match account.characters().await {
        Ok(c) => c,
        Err(e) => {
            err_out(&format!("characters fetch failed: {e:?}"));
            return;
        }
    };

    let mut last_err = String::from("no character matched the server");
    for maybe_session in chars {
        let mut session = match maybe_session {
            Ok(s) => s,
            Err(_) => continue,
        };
        if server_host(&session) != server {
            continue;
        }
        if let Err(e) = session.login().await {
            last_err = format!("character login failed: {e:?}");
            continue;
        }
        match fetch_report(&session).await {
            Ok((msg_id, kind, body)) => {
                println!(
                    r#"{{"ok":true,"msg_id":{},"kind":{},"body":{}}}"#,
                    msg_id,
                    json_string(kind),
                    json_string(&body)
                );
                return;
            }
            Err(e) => {
                last_err = e;
                continue;
            }
        }
    }
    err_out(&last_err);
}
