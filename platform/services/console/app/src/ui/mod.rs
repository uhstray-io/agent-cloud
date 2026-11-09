//! Dispatch Console UI. One Dioxus app: the server renders the first frame and
//! the browser hydrates it. Every page except the sign-in screen sits under
//! `Shell`, which owns the sidebar and the scope bar.

mod access;
mod access_api;
mod action;
mod api;
mod cicd;
pub(crate) mod cicd_api;
mod compute;
mod dashboard;
mod empty;
mod gateway;
mod gateway_api;
mod icons;
mod inventory;
mod inventory_api;
mod policy;
mod policy_api;
mod profile;
pub(crate) mod proposals_api;
mod request;
mod request_api;
mod roles;
mod roles_api;
mod scope;
mod secrets;
mod secrets_api;
mod services;
mod settings;
mod settings_api;
mod shell;
mod status;
mod telemetry;
pub(crate) mod telemetry_api;
mod topology;
mod work;
mod work_detail;

#[cfg(all(test, feature = "server"))]
mod render_tests;

use dioxus::prelude::*;

use access::Access;
use cicd::CiCd;
use compute::Compute;
use dashboard::Dashboard;
use empty::NotFound;
use gateway::{AiGateway, LlmAccess};
use policy::Policy;
use profile::Profile;
use request::RequestService;
use roles::RoleDetail;
use secrets::Secrets;
use services::{ServiceDetail, Services};
use settings::{OrgSettings, TenantSettings};
use shell::Shell;
use telemetry::Telemetry;
use topology::Topology;
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
    #[route("/services/:name")]
    ServiceDetail { name: String },
    #[route("/request")]
    RequestService {},
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
    #[route("/roles/:role")]
    RoleDetail { role: String },
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
