// ============================================================================
// sf_members_diag.rs — diagnostic: read guild members + their battle
// registration straight out of the Update response.
// ----------------------------------------------------------------------------
// WHY THIS EXISTS:
// The mailbox battle report (systemmessagelist) is not returned by
// Command::Update, so the report-fetch path is a dead end for now. The Update
// response DOES carry `owngroupsave`, from which the crate parses each guild
// member's `battles_joined` (Attack / Defense / Both / not registered). If that
// set matches the report's "Niezarejestrowani czlonkowie" list, we can drop the
// mailbox approach entirely and read absence directly from Update.
//
// WHY SimpleSession AND NOT A RAW Session:
// The previous version used SFAccount::login().characters() -> raw Session,
// called session.login(), then Command::Update + GameState::new(resp). That
// fails with:
//     ParsingError("response did not contain full player state", "")
// because GameState::new() expects the FULL login response, not a bare Update.
// SimpleSession::send_command() does this correctly: if it has no gamestate it
// first calls session.login(), builds the GameState from THAT response, and
// only then sends your command and folds the result in via gs.update(). This is
// the exact pattern the working sf_probe.rs uses.
//
// INPUT (three lines on stdin, same contract as sf_probe):
//     <server>\n<username>\n<password>\n
// OUTPUT: human-readable diagnostic text on stdout.
// ============================================================================

use sf_api::command::Command;
use sf_api::gamestate::guild::{BattlesJoined, GuildRank};
use sf_api::session::SimpleSession;
use std::io::{self, BufRead};

/// Just the hostname (e.g. "s20.sfgame.eu") of the server this session really
/// authenticated against — SSO hands back one session per character across
/// every world on the account, so this is how we pick the right one.
fn server_host(session: &SimpleSession) -> String {
    session
        .server_url()
        .host_str()
        .map(std::string::ToString::to_string)
        .unwrap_or_default()
}

#[tokio::main]
async fn main() {
    let mut lines = io::stdin().lock().lines();
    let server = lines.next().and_then(|l| l.ok()).unwrap_or_default();
    let username = lines.next().and_then(|l| l.ok()).unwrap_or_default();
    let password = lines.next().and_then(|l| l.ok()).unwrap_or_default();

    if server.is_empty() || username.is_empty() || password.is_empty() {
        println!("missing credentials on stdin (expected: server / username / password)");
        return;
    }

    // Per-server login first, SSO fallback — same two-path logic as sf_probe.rs.
    let mut session = match SimpleSession::login(&username, &password, &server).await {
        Ok(s) => {
            println!("login_method: per_server ({})", server_host(&s));
            s
        }
        Err(per_server_err) => match SimpleSession::login_sf_account(&username, &password).await {
            Ok(sessions) => {
                let mut found: Option<SimpleSession> = None;
                let mut seen: Vec<String> = Vec::new();
                for s in sessions {
                    let host = server_host(&s);
                    if host == server && found.is_none() {
                        found = Some(s);
                    } else {
                        seen.push(host);
                    }
                }
                match found {
                    Some(s) => {
                        println!("login_method: sso ({server})");
                        s
                    }
                    None => {
                        println!("no character on {server}; account has: {}", seen.join(", "));
                        return;
                    }
                }
            }
            Err(sso_err) => {
                println!("login failed: per_server={per_server_err:?} / sso={sso_err:?}");
                return;
            }
        },
    };

    // SimpleSession::send_command logs in (building the GameState from the full
    // login response) before sending Update, and returns the updated state.
    let gs = match session.send_command(Command::Update).await {
        Ok(gs) => gs,
        Err(e) => {
            println!("Update failed: {e:?}");
            return;
        }
    };

    println!("character: {}", gs.character.name);

    let Some(guild) = gs.guild.as_ref() else {
        println!("not in a guild (gs.guild is None)");
        return;
    };

    println!("=== Guild: {} ({} entries) ===", guild.name, guild.members.len());

    // Battle context — helps judge whether battles_joined is meaningful right
    // now, or is just leftover/reset state with no battle pending.
    let fmt_battle = |b: Option<&sf_api::gamestate::guild::PlanedBattle>| -> String {
        match b {
            Some(b) => format!("vs guild id {} at {}", b.other, b.date.to_rfc3339()),
            None => "none".to_string(),
        }
    };
    println!("attacking:  {}", fmt_battle(guild.attacking.as_ref()));
    println!("defending:  {}", fmt_battle(guild.defending.as_ref()));
    println!(
        "next attack possible: {}",
        guild
            .next_attack_possible
            .as_ref()
            .map(|d| d.to_rfc3339())
            .unwrap_or_else(|| "-".to_string())
    );
    println!();

    println!("{:<22} {:<9} {:<8} {}", "MEMBER", "RANK", "JOINED", "LAST ONLINE");
    println!("{}", "-".repeat(70));

    let mut absent_attack: Vec<String> = Vec::new();
    let mut absent_defense: Vec<String> = Vec::new();
    let mut invited: Vec<String> = Vec::new();

    for m in &guild.members {
        // BattlesJoined has NO "None" variant — the crate models "registered
        // for nothing" as Option::None on the field itself.
        let reg = match m.battles_joined {
            Some(BattlesJoined::Attack) => "ATTACK",
            Some(BattlesJoined::Defense) => "DEFENSE",
            Some(BattlesJoined::Both) => "BOTH",
            None => "-",
        };
        let rank = match m.guild_rank {
            GuildRank::Leader => "leader",
            GuildRank::Officer => "officer",
            GuildRank::Member => "member",
            GuildRank::Invited => "INVITED",
        };
        let last_online = m
            .last_online
            .as_ref()
            .map(|d| d.to_rfc3339())
            .unwrap_or_else(|| "-".to_string());

        println!("{:<22} {:<9} {:<8} {}", m.name, rank, reg, last_online);

        // Pending invitations are not real members and will never show up in
        // the in-game report, so keep them out of the absence lists.
        if matches!(m.guild_rank, GuildRank::Invited) {
            invited.push(m.name.clone());
            continue;
        }

        let joined_attack = matches!(
            m.battles_joined,
            Some(BattlesJoined::Attack | BattlesJoined::Both)
        );
        let joined_defense = matches!(
            m.battles_joined,
            Some(BattlesJoined::Defense | BattlesJoined::Both)
        );
        if !joined_attack {
            absent_attack.push(m.name.clone());
        }
        if !joined_defense {
            absent_defense.push(m.name.clone());
        }
    }

    println!();
    println!(
        "NOT REGISTERED FOR ATTACK  ({}): {}",
        absent_attack.len(),
        absent_attack.join(", ")
    );
    println!(
        "NOT REGISTERED FOR DEFENSE ({}): {}",
        absent_defense.len(),
        absent_defense.join(", ")
    );
    if !invited.is_empty() {
        println!(
            "excluded (pending invites, {}): {}",
            invited.len(),
            invited.join(", ")
        );
    }
}
