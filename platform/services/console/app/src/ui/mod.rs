//! Dispatch Console UI. One Dioxus app: the server renders the first frame and
//! the browser hydrates it. Every page except the sign-in screen sits under
//! `Shell`, which owns the sidebar and the scope bar.

mod api;
mod empty;
mod icons;
mod profile;
mod scope;
mod shell;
mod work;
mod work_detail;

use dioxus::prelude::*;

use empty::{
    Access, AiGateway, CiCd, Compute, Dashboard, LlmAccess, NotFound, OrgSettings, Policy, Secrets,
    Services, Telemetry, TenantSettings, Topology,
};
use profile::Profile;
use shell::Shell;
use work::Home;

#[derive(Clone, Debug, PartialEq, Routable)]
pub enum Route {
    #[layout(Shell)]
    #[route("/")]
    Home {},
    #[route("/dashboard")]
    Dashboard {},
    #[route("/topology")]
    Topology {},
    #[route("/services")]
    Services {},
    #[route("/compute")]
    Compute {},
    #[route("/telemetry")]
    Telemetry {},
    #[route("/access")]
    Access {},
    #[route("/secrets")]
    Secrets {},
    #[route("/policy")]
    Policy {},
    #[route("/ai-gateway")]
    AiGateway {},
    #[route("/llm-access")]
    LlmAccess {},
    #[route("/cicd")]
    CiCd {},
    #[route("/settings/tenant")]
    TenantSettings {},
    #[route("/settings/org")]
    OrgSettings {},
    #[route("/profile")]
    Profile {},
    #[end_layout]
    // Outside the shell on purpose: an unknown path shows no account data.
    #[route("/:..segments")]
    NotFound { segments: Vec<String> },
}

pub fn app() -> Element {
    rsx! {
        document::Title { "Dispatch Console" }
        document::Stylesheet { href: asset!("/assets/main.css") }
        Router::<Route> {}
    }
}
