// sf_report_probe.rs
// ===================
// Fetches the latest guild ATTACK battle report for a S&F account and prints
// its raw `messagetext.s` body as JSON on stdout. No browser — pure protocol,
// the same mechanism the attack-alert probe already uses.
//
// FLOW (every step confirmed against real captures + crate source):
//   1. Log in (per-server, else SSO fallback).
//   2. send_command_raw(Update) -> Response; read .raw_response() which
//      contains "systemmessagelist.r:" — the list of guild reports.
//        each entry: msg_id,0,1,14,created_ts,expires_ts,TYPE
//        TYPE "2a" = attack report, "2d" = defense, "17" = other.
//   3. Pick the newest "2a" entry (highest created_ts) -> its msg_id.
//   4. Base64-encode the msg_id, send
//        Command::Custom { cmd_name: "PlayerMessageView", arguments: [b64] }
//      which serializes to "PlayerMessageView:<b64>" — exactly the request
//      the browser sends (verified: params=MTEyNDA1Mjg= == base64("11240528")).
//   5. Read .raw_response() -> contains "messagetext.s:2a/..." -> print as JSON.
//
// The Python side (sf_absence.parse_absent) parses the body into the absent
// list — that parser is already validated against two real battles.
//
// USAGE (stdin: server\nlogin\npassword):
//   printf 's20.sfgame.eu\nLOGIN\nPASS\n' | ./target/release/sf_report_probe
//
// OUTPUT (stdout), one JSON line:
//   {"ok":true,"msg_id":11240528,"body":"messagetext.s:2a/..."}
//   {"ok":false,"error":"..."}

use base64::{engine::general_purpose::STANDARD as B64, Engine as _};
use sf_api::command::Command;
use sf_api::session::SimpleSession;

fn out_err(msg: &str) {
    // Minimal manual JSON so we don't need serde just for errors.
    let escaped = msg.replace('\\', "\\\\").replace('"', "\\\"");
    println!("{{\"ok\":false,\"error\":\"{escaped}\"}}");
}

#[tokio::main]
async fn main() {
    let mut lines = std::io::stdin().lines();
    let server = match lines.next() { Some(Ok(s)) => s.trim().to_string(), _ => { out_err("missing server"); return; } };
    let username = match lines.next() { Some(Ok(s)) => s.trim().to_string(), _ => { out_err("missing username"); return; } };
    let password = match lines.next() { Some(Ok(s)) => s, _ => { out_err("missing password"); return; } };

    // Reject control chars (field-injection guard, same as sf_probe).
    for (label, v) in [("server", &server), ("username", &username), ("password", &password)] {
        if v.contains('\n') || v.contains('\r') || v.contains('\0') {
            out_err(&format!("{label} contains a control character"));
            return;
        }
    }

    // --- 1. Login (per-server, else SSO) -----------------------------------
    let mut session = match SimpleSession::login(&username, &password, &server).await {
        Ok(s) => s,
        Err(_) => {
            let sessions = match SimpleSession::login_sf_account(&username, &password).await {
                Ok(v) => v,
                Err(e) => { out_err(&format!("login failed: {e:?}")); return; }
            };
            let mut found = None;
            for s in sessions {
                if s.server_url().host_str().unwrap_or("") == server { found = Some(s); break; }
            }
            match found { Some(s) => s, None => { out_err("no SSO character matched the server"); return; } }
        }
    };

    // --- 2. Update -> raw response with systemmessagelist ------------------
    let resp = match session.send_command_raw(Command::Update).await {
        Ok(r) => r,
        Err(e) => { out_err(&format!("update failed: {e:?}")); return; }
    };
    let raw = resp.raw_response().to_string();

    // --- 3. Find newest "2a" (attack) msg_id ------------------------------
    let sml = match raw.split("systemmessagelist.r:").nth(1) {
        Some(rest) => rest.split('&').next().unwrap_or(""),
        None => { out_err("no systemmessagelist in response"); return; }
    };
    let mut best: Option<(i64, i64)> = None; // (created_ts, msg_id)
    for entry in sml.split(';') {
        let entry = entry.trim();
        if entry.is_empty() { continue; }
        let f: Vec<&str> = entry.split(',').collect();
        if f.len() < 7 { continue; }
        if f[6] != "2a" { continue; } // attack reports only
        let (msg_id, created) = match (f[0].parse::<i64>(), f[4].parse::<i64>()) {
            (Ok(m), Ok(c)) => (m, c),
            _ => continue,
        };
        if best.map_or(true, |(bc, _)| created > bc) {
            best = Some((created, msg_id));
        }
    }
    let msg_id = match best { Some((_, m)) => m, None => { out_err("no attack (2a) report found"); return; } };

    // --- 4. Open that report via Custom PlayerMessageView -----------------
    let b64_id = B64.encode(msg_id.to_string());
    let open = Command::Custom {
        cmd_name: "PlayerMessageView".to_string(),
        arguments: vec![b64_id],
    };
    let resp2 = match session.send_command_raw(open).await {
        Ok(r) => r,
        Err(e) => { out_err(&format!("message open failed: {e:?}")); return; }
    };
    let raw2 = resp2.raw_response().to_string();

    // --- 5. Emit the messagetext body as JSON -----------------------------
    if !raw2.contains("messagetext.s:") {
        out_err("opened message had no messagetext body");
        return;
    }
    let body_escaped = raw2.replace('\\', "\\\\").replace('"', "\\\"").replace('\n', "\\n");
    println!("{{\"ok\":true,\"msg_id\":{msg_id},\"body\":\"{body_escaped}\"}}");
}
