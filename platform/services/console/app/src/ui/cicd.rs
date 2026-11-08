//! The CI/CD page (plan 17, task 9.1, D42): required checks, the promotion path and open pull requests, read
//! from `GET /v1/delivery`, for platform admins.
//!
//! Scaffolding: the page shows its Phase 6 placeholder until the page lands.

use dioxus::prelude::*;

use super::empty::{EmptyPage, CICD};

#[component]
pub fn CiCd() -> Element {
    rsx! { EmptyPage { page: CICD } }
}
