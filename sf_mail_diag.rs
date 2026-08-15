// ============================================================================
// sf_mail_diag.rs — find out WHERE the guild battle report actually lives.
// ----------------------------------------------------------------------------
// BACKGROUND:
// `battles_joined` (from owngroupsave) turned out to be LIVE registration
// state, not history — with no attack scheduled every member reads as
// "not registered for attack", so it can never answer "who missed the last
// battle". Back to the mailbox.
//
// WHY THE MAILBOX LOOKED IMPOSSIBLE BEFORE, AND WHY IT ISN'T:
// The crate explicitly THROWS AWAY the `systemmessagelist` key:
//     "systemmessagelist" => {}
// ...but it fully parses `messagelist` into gs.mail.inbox. So probing the
// typed GameState for "systemmessagelist" was always going to come up empty
// regardless of whether the server sent it. This probe reads the RAW response
// text instead, so we see every key the server actually returns.
//
// WHY RAW AND NOT Command::MessageOpen:
// MessageOpen stores the body via from_sf_string(), which decodes $s -> '/'.
// parse_absent() in sf_absence.py splits player groups on '/' and was
// validated against the RAW section. Decoding first could inject extra
// separators and silently misalign the 5-token groups. Raw text keeps the
// validated parser fed exactly what it was tested on.
//
// INPUT (stdin):
//     <server>\n<username>\n<password>\n[message_index]\n
// message_index is optional, 0-based, defaults to 0 (newest). It selects
// which inbox message to open and dump.
//
// OUTPUT: human-readable diagnostic on stdout.
// ============================================================================

use sf_api::command::Command;
use sf_api::session::Session;
use sf_api::sso::SFAccount;
use std::io::{self, BufRead};

fn server_host(s: &Session) -> String {
    s.server_url()
        .host_str()
        .map(std::string::ToString::to_string)
        .unwrap_or_default()
}

/// Pulls one '<key>:' section out of a raw response body, mirroring
/// extract_section() in sf_absence.py so the two agree exactly.
fn extract_section(raw: &str, key: &str) -> Option<String> {
    let marker = format!("{key}:");
    let after = raw.split(&marker).nth(1)?;
    Some(after.split('&').next().unwrap_or("").to_string())
}

/// Prints every top-level key present in a raw response, sorted.
fn dump_keys(label: &str, raw: &str) {
    let mut keys: Vec<&str> = Vec::new();
    for part in raw.split('&') {
        if let Some(k) = part.split(':').next() {
            let k = k.trim();
            if !k.is_empty() && k.len() < 40 && !keys.contains(&k) {
                keys.push(k);
            }
        }
    }
    keys.sort_unstable();
    println!("--- keys in {label} ({} total) ---", keys.len());
    for k in &keys {
        println!("    {k}");
    }
    let has_ml = keys.iter().any(|k| k.starts_with("messagelist"));
    let has_sml = keys.iter().any(|k| k.starts_with("systemmessagelist"));
    println!("    >> messagelist present:       {has_ml}");
    println!("    >> systemmessagelist present: {has_sml}");
    println!();
}

/// Prints a message-list section row by row so we can see which entries are
/// guild battle reports and what their subject/type codes look like.
fn dump_list(label: &str, section: &str) {
    println!("--- {label} rows ---");
    let mut n = 0;
    for row in section.split(';') {
        let row = row.trim();
        if row.is_empty() {
            continue;
        }
        println!("  [{n}] {row}");
        n += 1;
    }
    if n == 0 {
        println!("  (empty)");
    }
    println!();
}

#[tokio::main]
async fn main() {
    let mut lines = io::stdin().lock().lines();
    let server = lines.next().and_then(|l| l.ok()).unwrap_or_default();
    let username = lines.next().and_then(|l| l.ok()).unwrap_or_default();
    let password = lines.next().and_then(|l| l.ok()).unwrap_or_default();
    let msg_index: i32 = lines
        .next()
        .and_then(|l| l.ok())
        .and_then(|l| l.trim().parse().ok())
        .unwrap_or(0);

    if server.is_empty() || username.is_empty() || password.is_empty() {
        println!("missing credentials on stdin (server / username / password [/ index])");
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
    let mut seen: Vec<String> = Vec::new();
    for maybe in chars {
        let s = match maybe {
            Ok(s) => s,
            Err(_) => continue,
        };
        let host = server_host(&s);
        if host == server && session.is_none() {
            session = Some(s);
        } else {
            seen.push(host);
        }
    }
    let Some(mut session) = session else {
        println!("no character on {server}; account has: {}", seen.join(", "));
        return;
    };

    // STEP 1: the login response. Session::login() hands back the full
    // Response, so we can inspect the raw text directly — no GameState
    // involved, which is what broke the earlier probe.
    let login_resp = match session.login().await {
        Ok(r) => r,
        Err(e) => {
            println!("login failed: {e:?}");
            return;
        }
    };
    let login_raw = login_resp.raw_response().to_string();
    println!("=== STEP 1: LOGIN RESPONSE ===");
    dump_keys("login response", &login_raw);

    if let Some(sec) = extract_section(&login_raw, "messagelist.r") {
        dump_list("messagelist.r (login)", &sec);
    }
    if let Some(sec) = extract_section(&login_raw, "systemmessagelist.r") {
        dump_list("systemmessagelist.r (login)", &sec);
    }

    // STEP 2: open one message and see what comes back with it. The report
    // body arrives as messagetext.s — this is the section parse_absent()
    // consumes, so it is printed verbatim and unmodified.
    println!("=== STEP 2: PlayerMessageView (index {msg_index}) ===");
    let open_resp = match session
        .send_command_raw(Command::MessageOpen { pos: msg_index })
        .await
    {
        Ok(r) => r,
        Err(e) => {
            println!("MessageOpen({msg_index}) failed: {e:?}");
            return;
        }
    };
    let open_raw = open_resp.raw_response().to_string();
    dump_keys("MessageOpen response", &open_raw);

    if let Some(sec) = extract_section(&open_raw, "systemmessagelist.r") {
        dump_list("systemmessagelist.r (MessageOpen)", &sec);
    }

    match extract_section(&open_raw, "messagetext.s") {
        Some(body) => {
            println!("--- messagetext.s (RAW, feed this to parse_absent) ---");
            println!("{body}");
            println!();
            // Quick sanity read so the shape is obvious at a glance: a battle
            // report should have many '/'-separated tokens, opponent at [1].
            let tokens: Vec<&str> = body.split('/').collect();
            println!(
                "token count: {} | tokens[0..4]: {:?}",
                tokens.len(),
                &tokens[..tokens.len().min(4)]
            );
        }
        None => println!(">>> no messagetext.s in the MessageOpen response"),
    }
}
