// sf_report_diag.rs — diagnostic: open first message, dump what sections and
// report types come back. Tests whether PlayerMessageView:1 returns the
// bundled systemmessagelist.
use sf_api::command::Command;
use sf_api::session::Session;
use sf_api::sso::SFAccount;
use std::io::{self, BufRead};

fn server_host(s: &Session) -> String {
    s.server_url().host_str().map(|h| h.to_string()).unwrap_or_default()
}

async fn pmv(session: &Session, arg: &str) -> Result<String, String> {
    let cmd = Command::Custom { cmd_name: "PlayerMessageView".to_string(), arguments: vec![arg.to_string()] };
    let resp = session.send_command_raw(cmd).await.map_err(|e| format!("{e:?}"))?;
    Ok(resp.raw_response().to_string())
}

#[tokio::main]
async fn main() {
    let mut lines = io::stdin().lock().lines();
    let server = lines.next().and_then(|l| l.ok()).unwrap_or_default();
    let username = lines.next().and_then(|l| l.ok()).unwrap_or_default();
    let password = lines.next().and_then(|l| l.ok()).unwrap_or_default();

    let account = match SFAccount::login(username, password).await {
        Ok(a) => a, Err(e) => { println!("sso login failed: {e:?}"); return; }
    };
    let chars = match account.characters().await {
        Ok(c) => c, Err(e) => { println!("chars failed: {e:?}"); return; }
    };
    for maybe in chars {
        let mut session = match maybe { Ok(s) => s, Err(_) => continue };
        if server_host(&session) != server { continue; }
        if let Err(e) = session.login().await { println!("login failed: {e:?}"); return; }

        let raw = match pmv(&session, "1").await { Ok(r) => r, Err(e) => { println!("PlayerMessageView:1 -> {e}"); return; } };

        println!("=== Section keys in PlayerMessageView:1 response ===");
        for part in raw.split('&') {
            if let Some(k) = part.split(':').next() {
                if !k.is_empty() && k.len() < 40 { println!("  {}", k); }
            }
        }
        if let Some(section) = raw.split("systemmessagelist.r:").nth(1).and_then(|s| s.split('&').next()) {
            println!("=== systemmessagelist entries (msg_id | created | expires | TYPE) ===");
            for entry in section.split(';') {
                let entry = entry.trim();
                if entry.is_empty() { continue; }
                let f: Vec<&str> = entry.split(',').collect();
                if f.len() < 7 { continue; }
                let label = match f[6] { "2a" => "ATTACK", "2d" => "DEFENSE", "17" => "other", x => x };
                println!("  {} | {} | {} | {} ({})", f[0], f[4], f[5], f[6], label);
            }
        } else {
            println!(">>> NO systemmessagelist section even in PlayerMessageView:1");
        }
        return;
    }
    println!("no char matched {server}");
}
