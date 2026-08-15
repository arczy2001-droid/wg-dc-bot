// ============================================================================
// sf_report_fetch_diag.rs — work out HOW to open a guild battle report.
// ----------------------------------------------------------------------------
// ESTABLISHED SO FAR (from sf_mail_diag):
//   * The login response DOES contain systemmessagelist.r — the crate simply
//     discards that key ("systemmessagelist" => {}), which is why every
//     GameState-based probe came up empty.
//   * Rows look like:
//         11256298,0,0,14,1786816431,1787423146,2a
//         ^msg_id           ^created    ^expires   ^type
//     type 2a = guild ATTACK report, 2d = DEFENSE, 17 = other.
//   * Reports expire ~7 days after creation, so an hourly loop has plenty of
//     slack to catch every one.
//   * Command::MessageOpen { pos: 0 } serialises to "PlayerMessageView:1" and
//     the server answered ServerError("messageid not found").
//
// WHAT THIS PROBE ANSWERS:
// Both messagelist.r and systemmessagelist.r are ID-FIRST, and the crate's
// sibling command PlayerNewsView takes an id rather than a position. So the
// likely truth is that PlayerMessageView wants a msg_id and the crate's
// `pos + 1` is legacy/wrong. Rather than guess one variant per login, this
// tries several candidate forms against the newest report id and reports
// which one actually returns a messagetext section.
//
// It is READ-ONLY: PlayerMessageView only opens a message. No deletes, no
// state changes. A short sleep separates attempts so this does not look like
// a hammering client.
//
// INPUT (stdin):
//     <server>\n<username>\n<password>\n[msg_id]\n
// msg_id is optional — default is the newest type-2a entry in the list.
//
// OUTPUT: human-readable diagnostic on stdout.
// ============================================================================

use sf_api::command::Command;
use sf_api::session::Session;
use sf_api::sso::SFAccount;
use std::io::{self, BufRead};
use std::time::Duration;

fn server_host(s: &Session) -> String {
    s.server_url()
        .host_str()
        .map(std::string::ToString::to_string)
        .unwrap_or_default()
}

/// Mirrors extract_section() in sf_absence.py so both sides agree exactly.
fn extract_section(raw: &str, key: &str) -> Option<String> {
    let marker = format!("{key}:");
    let after = raw.split(&marker).nth(1)?;
    Some(after.split('&').next().unwrap_or("").to_string())
}

/// One parsed row of systemmessagelist.r
#[derive(Debug, Clone)]
struct SysMsg {
    msg_id: String,
    created: i64,
    expires: i64,
    typ: String,
}

fn parse_system_list(section: &str) -> Vec<SysMsg> {
    let mut out = Vec::new();
    for row in section.split(';') {
        let row = row.trim();
        if row.is_empty() {
            continue;
        }
        let f: Vec<&str> = row.split(',').collect();
        if f.len() < 7 {
            continue;
        }
        out.push(SysMsg {
            msg_id: f[0].to_string(),
            created: f[4].parse().unwrap_or(0),
            expires: f[5].parse().unwrap_or(0),
            typ: f[6].to_string(),
        });
    }
    out
}

/// Sends a raw custom command and reports whether the response carried a
/// messagetext section. Returns the body on success.
async fn try_command(
    session: &Session,
    label: &str,
    cmd_name: &str,
    args: &[&str],
) -> Option<String> {
    let cmd = Command::Custom {
        cmd_name: cmd_name.to_string(),
        arguments: args.iter().map(|a| (*a).to_string()).collect(),
    };
    println!("--- trying {label}: {cmd_name}:{} ---", args.join("/"));
    match session.send_command_raw(cmd).await {
        Err(e) => {
            println!("    ERROR {e:?}");
            None
        }
        Ok(resp) => {
            let raw = resp.raw_response().to_string();
            match extract_section(&raw, "messagetext.s") {
                Some(body) if !body.trim().is_empty() => {
                    println!("    OK — messagetext.s present ({} chars)", body.len());
                    Some(body)
                }
                _ => {
                    // No body, but show what DID come back so a partial hit
                    // is still informative.
                    let keys: Vec<&str> = raw
                        .split('&')
                        .filter_map(|p| p.split(':').next())
                        .filter(|k| !k.is_empty() && k.len() < 40)
                        .collect();
                    println!("    no messagetext.s; keys: {}", keys.join(", "));
                    None
                }
            }
        }
    }
}

#[tokio::main]
async fn main() {
    let mut lines = io::stdin().lock().lines();
    let server = lines.next().and_then(|l| l.ok()).unwrap_or_default();
    let username = lines.next().and_then(|l| l.ok()).unwrap_or_default();
    let password = lines.next().and_then(|l| l.ok()).unwrap_or_default();
    let forced_id = lines
        .next()
        .and_then(|l| l.ok())
        .map(|l| l.trim().to_string())
        .filter(|l| !l.is_empty());

    if server.is_empty() || username.is_empty() || password.is_empty() {
        println!("missing credentials on stdin (server / username / password [/ msg_id])");
        return;
    }

    let account = match SFAccount::login(username, password).await {
        Ok(a) => a,
        Err(e) => {
            println!("sso login failed: {e:?}");
            return;
        }
    };
    let chars = match account.characters().await {
        Ok(c) => c,
        Err(e) => {
            println!("characters() failed: {e:?}");
            return;
        }
    };

    let mut session: Option<Session> = None;
    for maybe in chars {
        let s = match maybe {
            Ok(s) => s,
            Err(_) => continue,
        };
        if server_host(&s) == server && session.is_none() {
            session = Some(s);
        }
    }
    let Some(mut session) = session else {
        println!("no character on {server}");
        return;
    };

    let login_resp = match session.login().await {
        Ok(r) => r,
        Err(e) => {
            println!("login failed: {e:?}");
            return;
        }
    };
    let login_raw = login_resp.raw_response().to_string();

    let Some(section) = extract_section(&login_raw, "systemmessagelist.r") else {
        println!("no systemmessagelist.r in login response");
        return;
    };
    let msgs = parse_system_list(&section);
    println!("systemmessagelist: {} entries", msgs.len());

    // Newest ATTACK report, unless the caller pinned a specific id.
    let target = match &forced_id {
        Some(id) => msgs.iter().find(|m| &m.msg_id == id).cloned().or(Some(SysMsg {
            msg_id: id.clone(),
            created: 0,
            expires: 0,
            typ: "?".to_string(),
        })),
        None => msgs
            .iter()
            .filter(|m| m.typ == "2a")
            .max_by_key(|m| m.created)
            .cloned(),
    };
    let Some(target) = target else {
        println!("no type-2a entry found; types present: {:?}",
            msgs.iter().map(|m| m.typ.as_str()).collect::<Vec<_>>());
        return;
    };
    println!(
        "target: msg_id={} type={} created={} expires={}",
        target.msg_id, target.typ, target.created, target.expires
    );

    // Position of this entry in the list, 0-based and 1-based, in case the
    // server really does want an index rather than an id.
    let pos0 = msgs.iter().position(|m| m.msg_id == target.msg_id);
    let pos1 = pos0.map(|p| p + 1);
    println!("list position: 0-based={pos0:?} 1-based={pos1:?}");
    println!();

    let id = target.msg_id.clone();
    let p1 = pos1.unwrap_or(1).to_string();

    // Candidates, most likely first. Stop at the first one that returns a body.
    let mut body: Option<String> = None;

    if body.is_none() {
        body = try_command(&session, "id form", "PlayerMessageView", &[&id]).await;
        tokio::time::sleep(Duration::from_millis(1500)).await;
    }
    if body.is_none() {
        body = try_command(&session, "id + flag", "PlayerMessageView", &[&id, "1"]).await;
        tokio::time::sleep(Duration::from_millis(1500)).await;
    }
    if body.is_none() {
        body = try_command(&session, "news form", "PlayerNewsView", &[&id]).await;
        tokio::time::sleep(Duration::from_millis(1500)).await;
    }
    if body.is_none() {
        body = try_command(&session, "list index", "PlayerMessageView", &[&p1]).await;
        tokio::time::sleep(Duration::from_millis(1500)).await;
    }

    println!();
    match body {
        Some(body) => {
            println!("=== messagetext.s (RAW — feed straight to parse_absent) ===");
            println!("{body}");
            println!();
            let tokens: Vec<&str> = body.split('/').collect();
            println!(
                "token count: {} | opponent guess (tokens[1]): {:?}",
                tokens.len(),
                tokens.get(1)
            );
            let groups = tokens.len().saturating_sub(7) / 5;
            println!("=> ~{groups} player groups from offset 7");
        }
        None => {
            println!(">>> none of the candidate commands returned a messagetext.");
            println!(">>> Next step would be capturing the real client's request for");
            println!(">>> a report in devtools, to read the exact command name/args.");
        }
    }
}
