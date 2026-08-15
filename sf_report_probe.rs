// ============================================================================
// sf_report_probe.rs — machine-readable guild ATTACK report probe
// ----------------------------------------------------------------------------
// Fetches the latest guild attack battle report for an account and prints its
// raw `messagetext.s` body as JSON on stdout, so the Python side
// (sf_absence.parse_absent) can extract who did not participate.
//
// WHY A SEPARATE PROBE FROM sf_probe.rs:
// sf_probe.rs reads guild *status* (attacking/defending), which sf-api parses
// into GameState. The guild *battle report* (the absentee list) is NOT parsed
// into GameState; it only exists in the raw server response under the
// "messagetext.s:" section. Reading that needs the raw response string, exposed
// by Session::send_command_raw() -> Response -> .raw_response(). SimpleSession
// hides that, so we obtain the underlying raw Session ourselves via
// SFAccount::login().characters() — exactly what SimpleSession::login_sf_account
// does internally, but we keep the raw Session.
//
// INPUT (stdin):  <server>\n<username>\n<password>\n
// OUTPUT (one JSON line):
//   {"ok":true,"msg_id":11240528,"body":"messagetext.s:2a/..."}
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

/// Newest guild ATTACK (type "2a") report's msg_id from "systemmessagelist.r:".
/// Entry: msg_id,0,1,14,created_ts,expires_ts,TYPE  (2a=attack, 2d=defense, 17=other)
fn newest_attack_msg_id(raw: &str) -> Option<i64> {
    let section = raw.split("systemmessagelist.r:").nth(1)?.split('&').next()?;
    let mut best: Option<(i64, i64)> = None; // (created_ts, msg_id)
    for entry in section.split(';') {
        let entry = entry.trim();
        if entry.is_empty() {
            continue;
        }
        let f: Vec<&str> = entry.split(',').collect();
        if f.len() < 7 || f[6] != "2a" {
            continue;
        }
        if let (Ok(msg_id), Ok(created)) = (f[0].parse::<i64>(), f[4].parse::<i64>()) {
            if best.map_or(true, |(bc, _)| created > bc) {
                best = Some((created, msg_id));
            }
        }
    }
    best.map(|(_, id)| id)
}

/// Fetch systemmessagelist + open the newest attack report on one (logged-in) Session.
async fn fetch_report(session: &Session) -> Result<(i64, String), String> {
    let resp = session
        .send_command_raw(Command::Update)
        .await
        .map_err(|e| format!("update failed: {e:?}"))?;
    let raw = resp.raw_response().to_string();

    let msg_id = newest_attack_msg_id(&raw).ok_or("no attack (2a) report found")?;

    // PlayerMessageView:<base64(msg_id)> — Command::Custom serializes to
    // "{cmd_name}:{args joined by /}", reproducing the exact browser request.
    let b64_id = B64.encode(msg_id.to_string());
    let open = Command::Custom {
        cmd_name: "PlayerMessageView".to_string(),
        arguments: vec![b64_id],
    };
    let resp2 = session
        .send_command_raw(open)
        .await
        .map_err(|e| format!("message open failed: {e:?}"))?;
    let raw2 = resp2.raw_response().to_string();

    if !raw2.contains("messagetext.s:") {
        return Err("opened message had no messagetext body".to_string());
    }
    Ok((msg_id, raw2))
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

    // SSO login, then keep the raw per-character Sessions (mirrors
    // SimpleSession::login_sf_account internally, but retains raw Session
    // access for send_command_raw / raw_response).
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
            Ok((msg_id, body)) => {
                println!(r#"{{"ok":true,"msg_id":{},"body":{}}}"#, msg_id, json_string(&body));
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
