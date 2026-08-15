// ============================================================================
// sf_report_probe.rs — machine-readable guild ATTACK/DEFENSE report probe
// ----------------------------------------------------------------------------
// Fetches the latest guild battle report (attack "2a" or defense "2d") and
// prints its raw `messagetext.s` body as JSON on stdout, so the Python side
// (sf_absence.parse_absent) can extract who did not participate.
//
// TWO-PHASE FETCH (important — both phases were wrong before, now verified
// live against s20.sfgame.eu):
//   Phase 1: the report list `systemmessagelist.r` IS part of the LOGIN
//            response. The crate hides this because GameState::update()
//            explicitly discards the key ("systemmessagelist" => {}), so every
//            GameState-based probe saw nothing. Session::login() hands back the
//            Response, so we read its raw text directly.
//            (The old code sent PlayerMessageView:1 to get the list; the server
//            answers ServerError("messageid not found") — inbox positions are
//            not valid arguments.)
//   Phase 2: open the chosen report with its PLAIN DECIMAL msg_id:
//            PlayerMessageView:11256298. Not base64 — that was a guess and it
//            does not work. Confirmed: returns messagetext.s directly.
//
// INPUT (stdin):  <server>\n<username>\n<password>\n[msg_id]\n
//   The optional 4th line pins a specific report id instead of taking the
//   newest — used to backfill a battle the hourly loop missed.
// OUTPUT (one JSON line):
//   {"ok":true,"msg_id":123,"kind":"attack","created":168...,"expires":168...,
//    "reports":[{"msg_id":..,"kind":"..","created":..,"expires":..}, ...],
//    "body":"...messagetext.s:2a/..."}
//   {"ok":false,"error":"..."}
//
// `reports` lists every battle report still on the server (~7 day retention),
// newest first, so the caller can spot ones it has not posted yet and re-run
// with that id. Existing fields are unchanged, so this stays compatible.
// ============================================================================

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

/// One battle report entry from systemmessagelist.r
/// Row format: msg_id,0,1,14,created_ts,expires_ts,TYPE   (2a=attack, 2d=defense)
#[derive(Clone, Copy)]
struct ReportEntry {
    msg_id: i64,
    created: i64,
    expires: i64,
    kind: &'static str,
}

/// Every battle report in the list, newest first. Type 17 and anything else
/// that is not a guild battle is skipped.
fn battle_reports(raw: &str) -> Vec<ReportEntry> {
    let Some(section) = raw
        .split("systemmessagelist.r:")
        .nth(1)
        .and_then(|s| s.split('&').next())
    else {
        return Vec::new();
    };
    let mut out: Vec<ReportEntry> = Vec::new();
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
            _ => continue,
        };
        if let (Ok(msg_id), Ok(created)) = (f[0].parse::<i64>(), f[4].parse::<i64>()) {
            out.push(ReportEntry {
                msg_id,
                created,
                expires: f[5].parse::<i64>().unwrap_or(0),
                kind,
            });
        }
    }
    out.sort_unstable_by(|a, b| b.created.cmp(&a.created));
    out
}

async fn fetch_report(
    login_raw: &str,
    session: &Session,
    wanted_id: Option<i64>,
) -> Result<(ReportEntry, Vec<ReportEntry>, String), String> {
    // Phase 1: read the list straight out of the login response.
    let reports = battle_reports(login_raw);
    if reports.is_empty() {
        return Err("no attack/defense report in systemmessagelist".to_string());
    }

    // Newest by default; a pinned id lets the caller backfill an older battle
    // that is still inside the ~7 day retention window.
    let target = match wanted_id {
        Some(id) => *reports
            .iter()
            .find(|r| r.msg_id == id)
            .ok_or_else(|| format!("msg_id {id} is not a battle report in the current list"))?,
        None => reports[0],
    };

    // Phase 2: plain decimal id, no base64.
    let raw2 = player_message_view(session, &target.msg_id.to_string()).await?;
    if !raw2.contains("messagetext.s:") {
        return Err("opened report had no messagetext body".to_string());
    }
    Ok((target, reports, raw2))
}

/// Serialises the report list as a JSON array for the `reports` field.
fn reports_json(reports: &[ReportEntry]) -> String {
    let items: Vec<String> = reports
        .iter()
        .map(|r| {
            format!(
                r#"{{"msg_id":{},"kind":{},"created":{},"expires":{}}}"#,
                r.msg_id,
                json_string(r.kind),
                r.created,
                r.expires
            )
        })
        .collect();
    format!("[{}]", items.join(","))
}

#[tokio::main]
async fn main() {
    let mut lines = io::stdin().lock().lines();
    let server = lines.next().and_then(|l| l.ok()).unwrap_or_default();
    let username = lines.next().and_then(|l| l.ok()).unwrap_or_default();
    let password = lines.next().and_then(|l| l.ok()).unwrap_or_default();
    let wanted_id: Option<i64> = lines
        .next()
        .and_then(|l| l.ok())
        .and_then(|l| l.trim().parse::<i64>().ok());

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
        let login_raw = match session.login().await {
            Ok(resp) => resp.raw_response().to_string(),
            Err(e) => {
                last_err = format!("character login failed: {e:?}");
                continue;
            }
        };
        match fetch_report(&login_raw, &session, wanted_id).await {
            Ok((target, reports, body)) => {
                println!(
                    r#"{{"ok":true,"msg_id":{},"kind":{},"created":{},"expires":{},"reports":{},"body":{}}}"#,
                    target.msg_id,
                    json_string(target.kind),
                    target.created,
                    target.expires,
                    reports_json(&reports),
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
