// sf_members_diag.rs — read guild members + their battle registration from
// the Update response (owngroupmember). Prints each member and whether they're
// registered for Attack / Defense / Both / None, so we can compare against the
// in-game "Niezarejestrowani" report and confirm this matches.
use sf_api::command::Command;
use sf_api::session::Session;
use sf_api::sso::SFAccount;
use sf_api::gamestate::guild::BattlesJoined;
use std::io::{self, BufRead};

fn server_host(s: &Session) -> String {
    s.server_url().host_str().map(|h| h.to_string()).unwrap_or_default()
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
        if let Err(e) = session.send_command(Command::Update).await {
            println!("update failed: {e:?}"); return;
        }
        let gs = match session.game_state() { Some(g) => g, None => { println!("no gamestate"); return; } };
        let guild = match &gs.guild { Some(g) => g, None => { println!("not in a guild"); return; } };

        println!("=== Guild: {} ({} members) ===", guild.name, guild.members.len());
        println!("--- Registered for ATTACK? (absent from attack = NOT attack/both) ---");
        let mut absent_attack = Vec::new();
        let mut absent_defense = Vec::new();
        for m in &guild.members {
            let reg = match m.battles_joined {
                Some(BattlesJoined::Attack) => "ATTACK",
                Some(BattlesJoined::Defense) => "DEFENSE",
                Some(BattlesJoined::Both) => "BOTH",
                None => "NONE",
            };
            println!("  {:20} reg={}", m.name, reg);
            let joined_attack = matches!(m.battles_joined, Some(BattlesJoined::Attack) | Some(BattlesJoined::Both));
            let joined_defense = matches!(m.battles_joined, Some(BattlesJoined::Defense) | Some(BattlesJoined::Both));
            if !joined_attack { absent_attack.push(m.name.clone()); }
            if !joined_defense { absent_defense.push(m.name.clone()); }
        }
        println!();
        println!("ABSENT for ATTACK ({}): {}", absent_attack.len(), absent_attack.join(", "));
        println!("ABSENT for DEFENSE ({}): {}", absent_defense.len(), absent_defense.join(", "));
        return;
    }
    println!("no char matched {server}");
}
