//! Placeholder UI. The real shell replaces this later; this slice only proves
//! that `/` renders on the server, hydrates on the client, and reads `/v1/me`
//! with the browser's own session cookie.

use dioxus::prelude::*;

#[derive(Clone, Debug, PartialEq, Routable)]
pub enum Route {
    #[route("/")]
    Home {},
}

pub fn app() -> Element {
    rsx! {
        document::Stylesheet { href: asset!("/assets/main.css") }
        Router::<Route> {}
    }
}

/// What the client learned from `GET /v1/me`. `Loading` is also what the
/// server renders, so hydration matches before the effect runs.
#[derive(Clone, Debug, PartialEq)]
enum Who {
    Loading,
    SignedOut,
    SignedIn(String),
    Failed,
}

// Same-origin fetch through Dioxus's own JS bridge: reqwest cannot take a
// relative URL and web-sys is not an approved dependency.
const ME_SCRIPT: &str = r#"
    const r = await fetch("/v1/me", { headers: { accept: "application/json" }, credentials: "same-origin" });
    let claims = null;
    if (r.status === 200) {
        claims = (await r.json()).claims;
    }
    dioxus.send({ status: r.status, claims: claims });
"#;

#[component]
fn Home() -> Element {
    let mut who = use_signal(|| Who::Loading);

    use_effect(move || {
        spawn(async move {
            let mut eval = document::eval(ME_SCRIPT);
            let next = match eval.recv::<serde_json::Value>().await {
                Ok(value) => classify(&value),
                Err(_) => Who::Failed,
            };
            who.set(next);
        });
    });

    rsx! {
        main { class: "shell",
            h1 { "Dispatch Console" }
            {body(who())}
        }
    }
}

fn body(who: Who) -> Element {
    match who {
        Who::Loading => rsx! { p { "Checking session\u{2026}" } },
        Who::SignedOut => rsx! {
            p { "You are not signed in." }
            a { href: "/auth/login", "Sign in" }
        },
        Who::SignedIn(name) => rsx! {
            p { "Signed in as {name}" }
            form { method: "post", action: "/auth/logout",
                button { "Sign out" }
            }
        },
        Who::Failed => rsx! { p { "Could not read session state." } },
    }
}

fn classify(value: &serde_json::Value) -> Who {
    match value["status"].as_u64() {
        Some(401) => Who::SignedOut,
        Some(200) => {
            let claims = &value["claims"];
            ["name", "username", "email", "sub"]
                .iter()
                .filter_map(|key| claims[*key].as_str())
                .find(|text| !text.is_empty())
                .map_or(Who::Failed, |text| Who::SignedIn(text.to_string()))
        }
        _ => Who::Failed,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn unauthenticated_status_means_signed_out() {
        assert_eq!(
            classify(&json!({"status": 401, "claims": null})),
            Who::SignedOut
        );
    }

    #[test]
    fn signed_in_prefers_name_then_falls_back() {
        let named = json!({"status": 200, "claims": {"name": "Ada", "username": "ada"}});
        assert_eq!(classify(&named), Who::SignedIn("Ada".into()));
        let no_name = json!({"status": 200, "claims": {"name": null, "username": "ada"}});
        assert_eq!(classify(&no_name), Who::SignedIn("ada".into()));
    }

    #[test]
    fn other_status_is_failed() {
        assert_eq!(
            classify(&json!({"status": 500, "claims": null})),
            Who::Failed
        );
        assert_eq!(classify(&json!({"status": 200, "claims": {}})), Who::Failed);
    }
}
