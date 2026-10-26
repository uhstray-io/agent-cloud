//! Dispatch Console server core.
//!
//! One crate, two builds. `dx bundle --web` compiles the `web` feature (wasm
//! client that hydrates the UI) and the `server` feature (the axum host). The
//! server build also answers the `probe` subcommand the container healthcheck
//! runs; it is handled before any configuration or socket work.

mod ui;

#[cfg(feature = "server")]
mod probe;
#[cfg(feature = "server")]
mod server;

fn main() {
    #[cfg(not(feature = "server"))]
    dioxus::launch(ui::app);

    #[cfg(feature = "server")]
    run_server();
}

#[cfg(feature = "server")]
fn run_server() -> ! {
    let args: Vec<String> = std::env::args().collect();
    if args.get(1).map(String::as_str) == Some("probe") {
        std::process::exit(probe::run_cli(&args[2..]));
    }

    let config = match server::config::Config::from_lookup(|name| std::env::var(name).ok()) {
        Ok(config) => config,
        Err(err) => {
            eprintln!("console: configuration error: {err}");
            std::process::exit(1);
        }
    };
    let state = match server::load(config) {
        Ok(state) => state,
        Err(err) => {
            eprintln!("console: startup error: {err}");
            std::process::exit(1);
        }
    };

    // The listener binds before OIDC discovery finishes (healthz is up at once;
    // /auth/* answers 503 until discovery succeeds). See server::spawn_discovery.
    dioxus::serve(move || {
        let state = state.clone();
        async move {
            server::spawn_discovery(state.clone());
            Ok(server::router(state))
        }
    })
}
