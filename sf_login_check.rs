// ============================================================================
// sf_login_check.rs
// ----------------------------------------------------------------------------
// A minimal login verification script for Shakes & Fidget, using the real
// `sf-api` crate (https://github.com/the-marenga/sf-api) by the-marenga.
//
// IMPORTANT — READ BEFORE RUNNING:
// This is a Rust binary, not Python. `sf-api` is a Rust crate (installed via
// `cargo add sf-api`) with no Python bindings — see the note at the bottom
// of this file for why a Python version isn't provided here.
//
// One field path in this file is flagged as UNVERIFIED — see the big comment
// block above `print_guild_name()` below. Everything else (the login flow,
// `SimpleSession`, `game_state()`, `character.description`) is taken
// directly from the crate's own README example, not guessed.
// ============================================================================

use sf_api::session::SimpleSession;
use std::io::{self, Write};

#[tokio::main]
async fn main() {
    println!("=== Shakes & Fidget — Login Verification ===\n");

    // ------------------------------------------------------------------
    // STEP 1: Collect login details from the user
    // ------------------------------------------------------------------
    let server = prompt("Server (e.g. s20.sfgame.eu): ");
    let username = prompt("Username: ");
    let password = prompt_password("Password: ");

    // ------------------------------------------------------------------
    // STEP 2: Authenticate
    // SimpleSession::login() handles the S&F login protocol (encryption,
    // request signing, response parsing) internally — this is the exact
    // call shown in the crate's own README example.
    // ------------------------------------------------------------------
    println!("\nConnecting to {server}...");

    let mut session = match SimpleSession::login(&username, &password, &server).await {
        Ok(s) => s,
        Err(e) => {
            // Covers wrong password, wrong server, unreachable server,
            // account-locked responses, etc. — sf-api surfaces these as
            // a single error type, so we can't distinguish the exact cause
            // here without inspecting the error's Debug output.
            eprintln!("❌ Login failed: {e:?}");
            eprintln!("   Check that the server address and credentials are correct,");
            eprintln!("   and that the server is reachable from this machine.");
            std::process::exit(1);
        }
    };

    // ------------------------------------------------------------------
    // STEP 3: Fetch character/guild data
    // ------------------------------------------------------------------
    let game_state = match session.game_state() {
        Some(gs) => gs,
        None => {
            eprintln!("❌ Login appeared to succeed, but no game state was returned.");
            eprintln!("   This can happen if the account needs to complete initial");
            eprintln!("   character creation in-browser first.");
            std::process::exit(1);
        }
    };

    // ------------------------------------------------------------------
    // GUILD NAME — ⚠️ UNVERIFIED FIELD PATH, PLEASE CONFIRM BEFORE RELYING ON THIS
    //
    // The README's only shown example field is `character.description`.
    // I could not confirm the exact path to guild membership data (docs.rs's
    // generated page for this crate didn't surface in search results), so
    // the line below is my best inference based on how S&F structures guild
    // membership (a character may or may not be in a guild, so this is
    // almost certainly an Option<...> of some guild-info type) — NOT a
    // confirmed field from the actual source.
    //
    // TO CONFIRM THE REAL FIELD NAME before trusting this script:
    //   1. Run `cargo doc --open` in a project that depends on sf-api, and
    //      browse to the `character` (or `guild`) module, OR
    //   2. Clone https://github.com/the-marenga/sf-api and grep the `src/`
    //      folder for "guild" (e.g. `grep -ri guild src/`), OR
    //   3. Add `dbg!(&game_state.character);` right here temporarily and run
    //      the script once to print the full real struct to your terminal.
    //
    // Once you know the real path, replace the line below accordingly.
    // ------------------------------------------------------------------
    // ------------------------------------------------------------------
    // TEMPORARY DEBUG STEP — discovering the real guild field.
    // The previous guess (character.guild) was confirmed WRONG by the
    // compiler: Character has no `guild` field, so guild info likely lives
    // directly on GameState instead (as a sibling of `character`).
    //
    // This next line is DELIBERATELY WRONG on purpose: assigning a struct
    // to `()` cannot compile, and rustc's error message will list every
    // real field on GameState the same way it just listed Character's 27
    // fields for us. This works regardless of whether GameState implements
    // Debug, so it's more reliable than a println!("{:#?}", ...) attempt.
    //
    // Run `cargo build`, read the field list in the error, find the one
    // that looks like guild data, then delete this block and use it below.
    // ------------------------------------------------------------------
    let _: () = game_state;

    let character = &game_state.character;
    println!("\n✅ Login succeeded for {username} (character: {}).", character.name);
}

// ----------------------------------------------------------------------
// Small helpers for reading input from the terminal
// ----------------------------------------------------------------------

fn prompt(label: &str) -> String {
    print!("{label}");
    io::stdout().flush().unwrap();
    let mut input = String::new();
    io::stdin().read_line(&mut input).unwrap();
    input.trim().to_string()
}

fn prompt_password(label: &str) -> String {
    // rpassword masks input the same way Python's getpass.getpass() does.
    // Add to Cargo.toml: rpassword = "7"
    rpassword::prompt_password(label).unwrap_or_default()
}

// ============================================================================
// WHY THIS ISN'T A PYTHON SCRIPT
// ----------------------------------------------------------------------------
// `sf-api` (the-marenga/sf-api) is a Rust-only crate — installed via
// `cargo add sf-api`, with no PyPI package and no PyO3/Python bindings found
// anywhere in the project. A `pip install sf-api` script pretending to wrap
// this library would either fail to install, or worse, silently install an
// unrelated package if one happens to exist under that name on PyPI.
//
// A genuine Python alternative would mean reimplementing S&F's login
// encryption/request-signing protocol from scratch — which isn't publicly
// documented by Playa Games and has changed before (this crate's changelog
// includes "Switch to the new cmd.php endpoint"). Guessing at that protocol
// would produce a script that looks plausible but fails in ways that are
// hard to diagnose, or risks the account being flagged for malformed
// traffic. If you do want a Python version, the safer route is porting the
// verified logic straight from sf-api's real Rust source, not writing it
// from general knowledge.
// ============================================================================
