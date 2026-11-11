//! The profile page. Identity claims come from Authentik and are shown locked.
//! Preferences are the one thing the console stores, and they save through
//! `PUT /v1/me`.

use dioxus::prelude::*;
use serde_json::json;

use super::api::{self, Account, Me, Preferences};
use super::icons::icon;
use super::scope;
use super::shell::{Ctx, Loaded};

/// Zones offered as suggestions. The server accepts any name that fits its
/// pattern, so this list only saves typing.
const TIMEZONES: [&str; 5] = [
    "UTC",
    "America/New_York",
    "America/Chicago",
    "America/Los_Angeles",
    "Europe/London",
];

const GENERIC_SAVE_ERROR: &str = "Could not save. Try again.";

/// Where the last save stands.
#[derive(Debug, Clone, PartialEq)]
enum SaveState {
    Idle,
    Saving,
    Saved,
    Failed(String),
}

#[component]
pub fn Profile() -> Element {
    let ctx = use_context::<Ctx>();
    let me = ctx.me.read().clone();
    match me {
        Me::SignedIn(account) => rsx! { ProfileView { account } },
        _ => rsx! { p { class: "faint", "Checking session\u{2026}" } },
    }
}

#[component]
fn ProfileView(account: Account) -> Element {
    let claims = account.claims.clone();
    let name = claims.name.clone().filter(|v| !v.is_empty());
    let username = claims.username.clone().filter(|v| !v.is_empty());
    let email = claims.email.clone().filter(|v| !v.is_empty());

    rsx! {
        div { class: "view",
            header { class: "view-h",
                h1 { {icon("user-round")} "Your profile" }
                span { class: "authority",
                    {icon("fingerprint")}
                    "Source of truth: Authentik (OIDC claims)"
                }
                p { "Your account comes from Authentik over OIDC. What Authentik owns is locked here; change it in Authentik. Everything else is yours to set." }
            }
            section { class: "card", aria_labelledby: "pf-account-h",
                div { class: "card-h", h2 { id: "pf-account-h", "Account" } }
                div { class: "card-b",
                    LockRow { label: "Name", value: name.unwrap_or_default() }
                    LockRow { label: "Username", value: username.unwrap_or_default() }
                    LockRow { label: "Email", value: email.unwrap_or_default() }
                    LockRow { label: "Subject", value: claims.sub.clone(), mono: true }
                    div { class: "pf-lock",
                        span { class: "label", "Groups" }
                        span { class: "row wrap grow",
                            if claims.groups.is_empty() {
                                span { class: "faint", "None" }
                            }
                            for group in claims.groups.iter() {
                                span { class: "tag mono", "{group}" }
                            }
                        }
                        LockTag {}
                    }
                }
            }
            section { class: "card", aria_labelledby: "pf-prefs-h",
                div { class: "card-h", h2 { id: "pf-prefs-h", "Preferences" } }
                div { class: "card-b",
                    PreferencesForm { saved: account.preferences.clone() }
                }
            }
        }
    }
}

/// One identity field, locked. An empty value reads "Not set".
#[component]
fn LockRow(label: &'static str, value: String, #[props(default)] mono: bool) -> Element {
    let missing = value.is_empty();
    let shown = if missing {
        "Not set".to_string()
    } else {
        value
    };
    let class = match (missing, mono) {
        (true, _) => "grow faint",
        (false, true) => "grow mono",
        (false, false) => "grow",
    };
    rsx! {
        div { class: "pf-lock",
            span { class: "label", "{label}" }
            span { class: "{class}", "{shown}" }
            LockTag {}
        }
    }
}

#[component]
fn LockTag() -> Element {
    rsx! {
        span { class: "tag", title: "Provided by Authentik over OIDC",
            {icon("lock")}
            "Managed in Authentik"
        }
    }
}

#[component]
fn PreferencesForm(saved: Preferences) -> Element {
    let mut ctx = use_context::<Ctx>();
    let mut theme = use_signal(|| saved.theme.clone());
    let mut timezone = use_signal(|| saved.timezone.clone());
    let mut default_scope = use_signal(|| saved.default_scope.clone().unwrap_or_default());
    let mut save = use_signal(|| SaveState::Idle);

    let tree = match &*ctx.tree.read() {
        Loaded::Ready(tree) => Some(tree.clone()),
        _ => None,
    };
    let choices = scope::default_choices(tree.as_ref(), &default_scope());
    let saving = save() == SaveState::Saving;

    let onsubmit = move |evt: FormEvent| {
        evt.prevent_default();
        let prefs = Preferences {
            theme: theme(),
            timezone: timezone(),
            default_scope: scope::default_text(&default_scope()),
        };
        save.set(SaveState::Saving);
        spawn(async move {
            let reply = api::request("PUT", "/v1/me", Some(json!({ "preferences": prefs }))).await;
            match api::classify_me(&reply) {
                Me::SignedIn(updated) => {
                    api::apply_theme(&updated.preferences.theme);
                    ctx.me.set(Me::SignedIn(updated));
                    save.set(SaveState::Saved);
                }
                Me::SignedOut => ctx.me.set(Me::SignedOut),
                _ => {
                    let message = api::invalid_detail(&reply)
                        .unwrap_or_else(|| GENERIC_SAVE_ERROR.to_string());
                    save.set(SaveState::Failed(message));
                }
            }
        });
    };

    rsx! {
        form { class: "stack", onsubmit,
            fieldset { class: "pf-theme",
                legend { class: "label", "Theme" }
                div { class: "seg-row",
                    for (value, text) in [("system", "System"), ("light", "Light"), ("dark", "Dark")] {
                        label { class: "choice",
                            input {
                                r#type: "radio",
                                name: "pf-theme",
                                value: "{value}",
                                checked: theme() == value,
                                onchange: move |_| theme.set(value.to_string()),
                            }
                            span { "{text}" }
                        }
                    }
                }
            }
            label { class: "field",
                span { class: "label", "Timezone" }
                input {
                    class: "input",
                    r#type: "text",
                    name: "timezone",
                    list: "pf-timezones",
                    autocomplete: "off",
                    spellcheck: "false",
                    value: "{timezone}",
                    oninput: move |evt: FormEvent| timezone.set(evt.value()),
                }
            }
            datalist { id: "pf-timezones",
                for zone in TIMEZONES {
                    option { value: zone }
                }
            }
            label { class: "field",
                span { class: "label", "Default scope" }
                select {
                    class: "input",
                    name: "default_scope",
                    aria_describedby: "pf-default-scope-hint",
                    value: "{default_scope}",
                    onchange: move |evt: FormEvent| default_scope.set(evt.value()),
                    for choice in choices.iter() {
                        option {
                            value: "{choice.value}",
                            selected: choice.value == default_scope(),
                            "{choice.label}"
                        }
                    }
                }
            }
            p { id: "pf-default-scope-hint", class: "hint",
                "Applied when the console opens, if the scope exists in the first tenant."
            }
            div { class: "row wrap",
                button { r#type: "submit", class: "btn primary", disabled: saving,
                    if saving {
                        span { class: "spin", {icon("loader-circle")} }
                        "Saving\u{2026}"
                    } else {
                        {icon("check")}
                        "Save preferences"
                    }
                }
                SaveStatus { state: save() }
            }
        }
    }
}

#[component]
fn SaveStatus(state: SaveState) -> Element {
    match state {
        SaveState::Idle | SaveState::Saving => rsx! {},
        SaveState::Saved => rsx! {
            span { class: "st ok", role: "status",
                {icon("circle-check")}
                "Preferences saved"
            }
        },
        SaveState::Failed(message) => rsx! {
            span { class: "st crit", role: "alert",
                {icon("octagon-x")}
                "{message}"
            }
        },
    }
}
