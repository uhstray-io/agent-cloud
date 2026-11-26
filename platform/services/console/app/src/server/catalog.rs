//! The service catalog the Request a service page lists (plan 17, task 8.4, D38).
//!
//! `config/catalog.json` is rendered at deploy from the deploy templates in `platform/semaphore/templates.yml`
//! (`platform/services/console/deployment/templates/catalog.json.j2`) and read once at startup. It carries names,
//! template names and host counts only. `deny_unknown_fields` makes the server refuse a file that carries anything
//! else, so a bad render stops startup instead of reaching a response.
//!
//! `GET /v1/catalog` answers every signed-in member: which services the platform can deploy is not sensitive. The
//! host count is for platform admins. A member gets `running` (whether any host carries the service), never the
//! number, and the member body has no `deployed_hosts` key at all. A signed-out caller gets 401.

use std::path::Path;

use dioxus::server::axum::extract::State;
use dioxus::server::axum::response::{IntoResponse, Response};
use dioxus::server::axum::Json;
use serde::{Deserialize, Serialize};
use tower_sessions::Session;

use super::api::unauthenticated;
use super::auth;
use super::scope::is_platform_admin;
use super::SharedState;

/// The file as deploy-console.yml renders it. The field names are the wire contract with `catalog.json.j2`:
/// `test_rendered_catalog_matches_the_servers_struct` checks the template's keys against them.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CatalogFile {
    pub services: Vec<CatalogEntry>,
}

/// One deployable service: its name, its deploy template, and how many inventory hosts carry it.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CatalogEntry {
    pub service: String,
    pub name: String,
    pub template: String,
    pub deployed_hosts: u64,
}

/// Whether a catalog file was configured. Its own enum, as the gateway's is: the response is the wire contract with the
/// UI, and it carries no error word.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum CatalogState {
    Ok,
    Unconfigured,
}

/// The catalog the server holds. `unconfigured` with no services when no file is set.
#[derive(Debug, Clone, PartialEq)]
pub struct CatalogSet {
    pub state: CatalogState,
    pub services: Vec<CatalogEntry>,
}

impl CatalogSet {
    pub fn unconfigured() -> Self {
        CatalogSet {
            state: CatalogState::Unconfigured,
            services: Vec::new(),
        }
    }

    pub fn configured(file: CatalogFile) -> Self {
        CatalogSet {
            state: CatalogState::Ok,
            services: file.services,
        }
    }

    /// What a member is answered: each service's name and template, and whether any host runs it. No host count.
    pub fn member_answer(&self) -> MemberAnswer {
        MemberAnswer {
            state: self.state,
            services: self.services.iter().map(member_service).collect(),
        }
    }

    /// What a platform admin is answered: the member answer, with the host count added to each service.
    pub fn admin_answer(&self) -> AdminAnswer {
        AdminAnswer {
            state: self.state,
            services: self
                .services
                .iter()
                .map(|entry| AdminService {
                    member: member_service(entry),
                    deployed_hosts: entry.deployed_hosts,
                })
                .collect(),
        }
    }
}

fn member_service(entry: &CatalogEntry) -> MemberService {
    MemberService {
        service: entry.service.clone(),
        name: entry.name.clone(),
        template: entry.template.clone(),
        running: entry.deployed_hosts > 0,
    }
}

/// What `GET /v1/catalog` answers a member. Serialized as is, so the body has no `deployed_hosts` key.
#[derive(Debug, Serialize)]
pub struct MemberAnswer {
    pub state: CatalogState,
    pub services: Vec<MemberService>,
}

#[derive(Debug, Serialize)]
pub struct MemberService {
    pub service: String,
    pub name: String,
    pub template: String,
    pub running: bool,
}

/// What `GET /v1/catalog` answers a platform admin: the member shape, plus the host count per service.
#[derive(Debug, Serialize)]
pub struct AdminAnswer {
    pub state: CatalogState,
    pub services: Vec<AdminService>,
}

#[derive(Debug, Serialize)]
pub struct AdminService {
    #[serde(flatten)]
    pub member: MemberService,
    pub deployed_hosts: u64,
}

/// Reads `CONSOLE_CATALOG_FILE`. A configured file that cannot be read or parsed fails startup, as the gateway file does.
pub fn load_catalog(path: &Path) -> Result<CatalogFile, String> {
    let text = std::fs::read_to_string(path).map_err(|err| {
        format!(
            "CONSOLE_CATALOG_FILE: cannot read {}: {err}",
            path.display()
        )
    })?;
    serde_json::from_str(&text).map_err(|err| format!("CONSOLE_CATALOG_FILE: invalid JSON: {err}"))
}

/// `GET /v1/catalog`: any signed-in member. The host count is added only for a platform admin.
pub async fn get_catalog(State(app): State<SharedState>, session: Session) -> Response {
    let Some(user) = auth::current_user(&session).await else {
        return unauthenticated();
    };
    if is_platform_admin(&user.groups) {
        Json(app.catalog.admin_answer()).into_response()
    } else {
        Json(app.catalog.member_answer()).into_response()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn catalog_file(name: &str, text: &str) -> std::path::PathBuf {
        let path = std::env::temp_dir().join(format!(
            "console-catalog-{}-{name}.json",
            std::process::id()
        ));
        std::fs::write(&path, text).expect("write");
        path
    }

    const GOOD: &str = r#"{
        "services": [
            {"service": "netbox", "name": "NetBox", "template": "Deploy NetBox", "deployed_hosts": 2},
            {"service": "step-ca", "name": "step-ca", "template": "Deploy step-ca (Dev)", "deployed_hosts": 0}
        ]
    }"#;

    #[test]
    fn load_catalog_reads_the_declared_shape() {
        let file = load_catalog(&catalog_file("good", GOOD)).expect("valid");
        assert_eq!(file.services.len(), 2);
        assert_eq!(file.services[0].service, "netbox");
        assert_eq!(file.services[0].deployed_hosts, 2);
        assert_eq!(file.services[1].template, "Deploy step-ca (Dev)");
    }

    #[test]
    fn load_catalog_names_the_setting_on_every_error() {
        let err = load_catalog(&catalog_file("bad", "{")).expect_err("must fail");
        assert!(
            err.starts_with("CONSOLE_CATALOG_FILE: invalid JSON"),
            "{err}"
        );
        let missing = load_catalog(&std::env::temp_dir().join("console-catalog-absent.json"))
            .expect_err("must fail");
        assert!(
            missing.starts_with("CONSOLE_CATALOG_FILE: cannot read"),
            "{missing}"
        );
    }

    #[test]
    fn a_key_address_or_extra_field_is_refused_not_kept() {
        // Names and counts only: an unknown field is a startup failure, never a silently kept value.
        for injected in [
            r#""key": "sk-live-value""#,
            r#""upstream": "198.51.100.9:8000""#,
            r#""address": "198.51.100.5""#,
        ] {
            let text = GOOD.replacen(
                r#""service": "netbox","#,
                &format!(r#""service": "netbox", {injected},"#),
                1,
            );
            let err = load_catalog(&catalog_file("inject", &text)).expect_err("must fail");
            assert!(
                err.starts_with("CONSOLE_CATALOG_FILE: invalid JSON"),
                "{err}"
            );
            assert!(err.contains("unknown field"), "{err}");
        }
        let top = GOOD.replacen(r#""services""#, r#""extra": 1, "services""#, 1);
        assert!(load_catalog(&catalog_file("top", &top)).is_err());
    }

    #[test]
    fn a_member_answer_carries_no_host_count() {
        let set =
            CatalogSet::configured(load_catalog(&catalog_file("member", GOOD)).expect("valid"));
        let body = serde_json::to_string(&set.member_answer()).expect("json");
        assert_eq!(
            body,
            r#"{"state":"ok","services":[{"service":"netbox","name":"NetBox","template":"Deploy NetBox","running":true},{"service":"step-ca","name":"step-ca","template":"Deploy step-ca (Dev)","running":false}]}"#
        );
        assert!(!body.contains("deployed_hosts"), "{body}");
    }

    #[test]
    fn an_admin_answer_adds_the_host_count_to_each_service() {
        let set =
            CatalogSet::configured(load_catalog(&catalog_file("admin", GOOD)).expect("valid"));
        let body = serde_json::to_string(&set.admin_answer()).expect("json");
        assert_eq!(
            body,
            r#"{"state":"ok","services":[{"service":"netbox","name":"NetBox","template":"Deploy NetBox","running":true,"deployed_hosts":2},{"service":"step-ca","name":"step-ca","template":"Deploy step-ca (Dev)","running":false,"deployed_hosts":0}]}"#
        );
    }

    #[test]
    fn unconfigured_answers_empty_to_members_and_admins_alike() {
        let set = CatalogSet::unconfigured();
        assert_eq!(
            serde_json::to_string(&set.member_answer()).expect("json"),
            r#"{"state":"unconfigured","services":[]}"#
        );
        assert_eq!(
            serde_json::to_string(&set.admin_answer()).expect("json"),
            r#"{"state":"unconfigured","services":[]}"#
        );
    }

    #[test]
    fn catalog_state_serializes_to_the_two_wire_words() {
        assert_eq!(
            serde_json::to_string(&CatalogState::Ok).expect("json"),
            r#""ok""#
        );
        assert_eq!(
            serde_json::to_string(&CatalogState::Unconfigured).expect("json"),
            r#""unconfigured""#
        );
    }
}
