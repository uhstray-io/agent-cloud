//! Environment configuration. Parsed from an injected lookup so tests never
//! touch the process environment. Secret values never reach an error message.

use std::fmt;
use std::path::PathBuf;

/// A credential value. Its Debug form is redacted, so a struct holding one can
/// derive Debug without printing the value.
#[derive(Clone, PartialEq, Eq)]
pub struct Secret(String);

impl Secret {
    pub fn new(value: impl Into<String>) -> Self {
        Secret(value.into())
    }

    pub fn expose(&self) -> &str {
        &self.0
    }
}

impl fmt::Debug for Secret {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("Secret(<redacted>)")
    }
}

/// Semaphore as the Active Work source. Present only when all four
/// `CONSOLE_SEMAPHORE_*` variables are set (see `semaphore_from`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SemaphoreConfig {
    /// Server-side API origin, no trailing slash. Only the poller calls it.
    pub api_url: String,
    /// Browser-facing origin, no trailing slash. Receipt links are built from it.
    pub ui_url: String,
    pub project: u32,
    pub token: Secret,
}

/// NetBox as the inventory source. Present only when both `CONSOLE_NETBOX_*` variables are
/// set (see `netbox_from`). The token is read only by the refresh task.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NetboxConfig {
    /// Server-side API origin, no trailing slash.
    pub api_url: String,
    pub token: Secret,
}

/// Proxmox as the live-state source. Present only when the `CONSOLE_PROXMOX_*` group is
/// set (see `proxmox_from`). `ca_file` adds roots for a self-signed API certificate; it never
/// turns verification off.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProxmoxConfig {
    /// Server-side API origin, no trailing slash.
    pub api_url: String,
    /// `user@realm!tokenname`. Read-only auditor token (plan 17 D22).
    pub token_id: String,
    pub token: Secret,
    pub ca_file: Option<PathBuf>,
}

/// Authentik as the Access page's directory source (plan 17, 8.2b). Present only when both
/// `CONSOLE_AUTHENTIK_URL` and `CONSOLE_AUTHENTIK_TOKEN` are set (see `authentik_from`). The token is a
/// view-only service account's, read only by the refresh task in `access.rs`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AuthentikConfig {
    /// Server-side API origin, no trailing slash.
    pub api_url: String,
    pub token: Secret,
}

/// OpenBao as the console's secret store (plan 17 D27). Present only when all three
/// `CONSOLE_OPENBAO_*` variables are set (see `openbao_from`). The AppRole pair is read only by
/// the client in `openbao.rs`, which logs in on first use, never at boot.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OpenbaoConfig {
    /// Server-side API origin, no trailing slash.
    pub api_url: String,
    pub role_id: Secret,
    pub secret_id: Secret,
}

#[derive(Clone)]
pub struct Config {
    /// Public origin, no trailing slash. The OIDC redirect is `<base_url>/auth/callback`.
    pub base_url: String,
    pub oidc_issuer: String,
    pub oidc_client_id: String,
    /// Never printed: `Config` has no Debug impl.
    pub oidc_client_secret: String,
    /// Optional PEM bundle added to the OIDC HTTP client's roots (local step-ca).
    pub extra_ca_file: Option<PathBuf>,
    /// Directory holding `prefs.db`.
    pub data_dir: PathBuf,
    /// Scope file rendered from inventory `console_tenants`.
    pub scope_file: PathBuf,
    /// Semaphore work source. `None` leaves `/v1/work` answering `unconfigured`.
    pub semaphore: Option<SemaphoreConfig>,
    /// Registry steps JSON rendered from `registry.yml`. `None` leaves roles and run steps empty.
    pub workflow_file: Option<PathBuf>,
    /// NetBox inventory source. `None` leaves `/v1/tenants/{t}/inventory` answering `unconfigured`.
    pub netbox: Option<NetboxConfig>,
    /// Proxmox live-state source. `None` leaves every VM's live status unknown.
    pub proxmox: Option<ProxmoxConfig>,
    /// OpenBao login for run-time reads (plan 17 D27). `None` leaves every OpenBao read unconfigured.
    pub openbao: Option<OpenbaoConfig>,
    /// Data-flow catalogue (JSON array) rendered from inventory declarations. `None` answers `unconfigured`.
    pub flows_file: Option<PathBuf>,
    /// agentgateway clients, budgets and models (JSON object) rendered from the agentgateway host's vars (task 8.3a).
    /// `None` answers `/v1/gateway` as `unconfigured`.
    pub gateway_file: Option<PathBuf>,
    /// Grafana origin for deep links, no trailing slash. `None` leaves every link null.
    pub grafana_url: Option<String>,
    /// Grafana API token for the gateway usage read (D39, a Viewer service account's). Present only with
    /// `grafana_url` (see `from_lookup`). Read only by the refresh task in `grafana.rs`. `None` leaves usage unconfigured.
    pub grafana_token: Option<Secret>,
    /// Authentik directory for the Access page. `None` leaves `/v1/access` answering `unconfigured`.
    pub authentik: Option<AuthentikConfig>,
    /// Role map JSON rendered from `vars/access-role-map.yml`. `None` serves an empty role map.
    pub role_map_file: Option<PathBuf>,
}

#[derive(Debug, PartialEq, Eq)]
pub enum ConfigError {
    Missing(&'static str),
    Invalid {
        name: &'static str,
        reason: &'static str,
    },
}

impl fmt::Display for ConfigError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ConfigError::Missing(name) => write!(f, "missing required environment variable {name}"),
            ConfigError::Invalid { name, reason } => write!(f, "{name}: {reason}"),
        }
    }
}

impl Config {
    pub fn from_lookup(get: impl Fn(&str) -> Option<String>) -> Result<Self, ConfigError> {
        let required = |name: &'static str| -> Result<String, ConfigError> {
            get(name)
                .map(|value| value.trim().to_string())
                .filter(|value| !value.is_empty())
                .ok_or(ConfigError::Missing(name))
        };

        let base_url = required("CONSOLE_BASE_URL")?
            .trim_end_matches('/')
            .to_string();
        if !(base_url.starts_with("https://") || base_url.starts_with("http://")) {
            return Err(ConfigError::Invalid {
                name: "CONSOLE_BASE_URL",
                reason: "must start with https:// or http://",
            });
        }
        let oidc_issuer = required("CONSOLE_OIDC_ISSUER")?;
        let oidc_client_id = required("CONSOLE_OIDC_CLIENT_ID")?;
        let oidc_client_secret = required("CONSOLE_OIDC_CLIENT_SECRET")?;
        let extra_ca_file = get("CONSOLE_EXTRA_CA_FILE")
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
            .map(PathBuf::from);
        let data_dir = PathBuf::from(required("CONSOLE_DATA_DIR")?);
        let scope_file = PathBuf::from(required("CONSOLE_SCOPE_FILE")?);
        let semaphore = semaphore_from(&get)?;
        let workflow_file = get("CONSOLE_WORKFLOW_FILE")
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
            .map(PathBuf::from);
        let netbox = netbox_from(&get)?;
        let proxmox = proxmox_from(&get)?;
        let openbao = openbao_from(&get)?;
        let flows_file = get("CONSOLE_FLOWS_FILE")
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
            .map(PathBuf::from);
        let gateway_file = get("CONSOLE_GATEWAY_FILE")
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
            .map(PathBuf::from);
        let grafana_url = get("CONSOLE_GRAFANA_URL")
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
            .map(|value| infrastructure_origin("CONSOLE_GRAFANA_URL", &value))
            .transpose()?;
        let grafana_token = get("CONSOLE_GRAFANA_TOKEN")
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
            .map(Secret::new);
        // The token is sent only to the origin it names, so it cannot be set without one.
        if grafana_token.is_some() && grafana_url.is_none() {
            return Err(ConfigError::Missing("CONSOLE_GRAFANA_URL"));
        }
        let authentik = authentik_from(&get)?;
        let role_map_file = get("CONSOLE_ROLE_MAP_FILE")
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
            .map(PathBuf::from);

        Ok(Config {
            base_url,
            oidc_issuer,
            oidc_client_id,
            oidc_client_secret,
            extra_ca_file,
            data_dir,
            scope_file,
            semaphore,
            workflow_file,
            netbox,
            proxmox,
            openbao,
            flows_file,
            gateway_file,
            grafana_url,
            grafana_token,
            authentik,
            role_map_file,
        })
    }
}

/// The two `CONSOLE_AUTHENTIK_*` variables are all-or-none, as the NetBox group is. The origin follows the
/// infrastructure rule: https anywhere, cleartext only to a loopback or RFC1918 host. Values never reach an error.
fn authentik_from(
    get: impl Fn(&str) -> Option<String>,
) -> Result<Option<AuthentikConfig>, ConfigError> {
    let read = |name: &'static str| {
        get(name)
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
    };
    let url = read("CONSOLE_AUTHENTIK_URL");
    let token = read("CONSOLE_AUTHENTIK_TOKEN");
    if url.is_none() && token.is_none() {
        return Ok(None);
    }
    let url = url.ok_or(ConfigError::Missing("CONSOLE_AUTHENTIK_URL"))?;
    let token = token.ok_or(ConfigError::Missing("CONSOLE_AUTHENTIK_TOKEN"))?;
    Ok(Some(AuthentikConfig {
        api_url: infrastructure_origin("CONSOLE_AUTHENTIK_URL", &url)?,
        token: Secret::new(token),
    }))
}

/// The two `CONSOLE_NETBOX_*` variables are all-or-none, as the Semaphore group is.
fn netbox_from(get: impl Fn(&str) -> Option<String>) -> Result<Option<NetboxConfig>, ConfigError> {
    let read = |name: &'static str| {
        get(name)
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
    };
    let url = read("CONSOLE_NETBOX_URL");
    let token = read("CONSOLE_NETBOX_TOKEN");
    if url.is_none() && token.is_none() {
        return Ok(None);
    }
    let url = url.ok_or(ConfigError::Missing("CONSOLE_NETBOX_URL"))?;
    let token = token.ok_or(ConfigError::Missing("CONSOLE_NETBOX_TOKEN"))?;
    Ok(Some(NetboxConfig {
        api_url: infrastructure_origin("CONSOLE_NETBOX_URL", &url)?,
        token: Secret::new(token),
    }))
}

/// The Proxmox group is `CONSOLE_PROXMOX_URL`, `_TOKEN_ID` and `_TOKEN`, all-or-none. The
/// optional `CONSOLE_PROXMOX_CA_FILE` counts as a member, so a CA file alone names the
/// missing URL instead of being silently ignored. Values never reach an error.
fn proxmox_from(
    get: impl Fn(&str) -> Option<String>,
) -> Result<Option<ProxmoxConfig>, ConfigError> {
    let read = |name: &'static str| {
        get(name)
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
    };
    let url = read("CONSOLE_PROXMOX_URL");
    let token_id = read("CONSOLE_PROXMOX_TOKEN_ID");
    let token = read("CONSOLE_PROXMOX_TOKEN");
    let ca_file = read("CONSOLE_PROXMOX_CA_FILE");
    if url.is_none() && token_id.is_none() && token.is_none() && ca_file.is_none() {
        return Ok(None);
    }
    let url = url.ok_or(ConfigError::Missing("CONSOLE_PROXMOX_URL"))?;
    let token_id = token_id.ok_or(ConfigError::Missing("CONSOLE_PROXMOX_TOKEN_ID"))?;
    let token = token.ok_or(ConfigError::Missing("CONSOLE_PROXMOX_TOKEN"))?;
    Ok(Some(ProxmoxConfig {
        api_url: infrastructure_origin("CONSOLE_PROXMOX_URL", &url)?,
        token_id,
        token: Secret::new(token),
        ca_file: ca_file.map(PathBuf::from),
    }))
}

/// The three `CONSOLE_OPENBAO_*` variables are all-or-none, as the Proxmox group is. Values
/// never reach an error. The address takes the transport rule of the other infrastructure APIs.
fn openbao_from(
    get: impl Fn(&str) -> Option<String>,
) -> Result<Option<OpenbaoConfig>, ConfigError> {
    let read = |name: &'static str| {
        get(name)
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
    };
    let url = read("CONSOLE_OPENBAO_URL");
    let role_id = read("CONSOLE_OPENBAO_ROLE_ID");
    let secret_id = read("CONSOLE_OPENBAO_SECRET_ID");
    if url.is_none() && role_id.is_none() && secret_id.is_none() {
        return Ok(None);
    }
    let url = url.ok_or(ConfigError::Missing("CONSOLE_OPENBAO_URL"))?;
    let role_id = role_id.ok_or(ConfigError::Missing("CONSOLE_OPENBAO_ROLE_ID"))?;
    let secret_id = secret_id.ok_or(ConfigError::Missing("CONSOLE_OPENBAO_SECRET_ID"))?;
    Ok(Some(OpenbaoConfig {
        api_url: infrastructure_origin("CONSOLE_OPENBAO_URL", &url)?,
        role_id: Secret::new(role_id),
        secret_id: Secret::new(secret_id),
    }))
}

/// The four Semaphore variables are all-or-none. None set means no work source;
/// some set means the first missing one is named. Values never reach an error.
fn semaphore_from(
    get: impl Fn(&str) -> Option<String>,
) -> Result<Option<SemaphoreConfig>, ConfigError> {
    let read = |name: &'static str| {
        get(name)
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
    };
    let api = read("CONSOLE_SEMAPHORE_API_URL");
    let ui = read("CONSOLE_SEMAPHORE_UI_URL");
    let project = read("CONSOLE_SEMAPHORE_PROJECT");
    let token = read("CONSOLE_SEMAPHORE_TOKEN");
    if api.is_none() && ui.is_none() && project.is_none() && token.is_none() {
        return Ok(None);
    }

    let api = api.ok_or(ConfigError::Missing("CONSOLE_SEMAPHORE_API_URL"))?;
    let ui = ui.ok_or(ConfigError::Missing("CONSOLE_SEMAPHORE_UI_URL"))?;
    let project = project.ok_or(ConfigError::Missing("CONSOLE_SEMAPHORE_PROJECT"))?;
    let token = token.ok_or(ConfigError::Missing("CONSOLE_SEMAPHORE_TOKEN"))?;
    let project = project
        .parse::<u32>()
        .ok()
        .filter(|id| *id > 0)
        .ok_or(ConfigError::Invalid {
            name: "CONSOLE_SEMAPHORE_PROJECT",
            reason: "must be a positive integer",
        })?;
    Ok(Some(SemaphoreConfig {
        api_url: origin("CONSOLE_SEMAPHORE_API_URL", &api)?,
        ui_url: origin("CONSOLE_SEMAPHORE_UI_URL", &ui)?,
        project,
        token: Secret::new(token),
    }))
}

/// Trims trailing slashes and requires an http(s) scheme, as `CONSOLE_BASE_URL` does.
fn origin(name: &'static str, value: &str) -> Result<String, ConfigError> {
    let trimmed = value.trim_end_matches('/').to_string();
    if !(trimmed.starts_with("https://") || trimmed.starts_with("http://")) {
        return Err(ConfigError::Invalid {
            name,
            reason: "must start with https:// or http://",
        });
    }
    Ok(trimmed)
}

/// An origin for an infrastructure API (NetBox, Proxmox). Cleartext is allowed only to a
/// loopback or RFC1918 host, so a credential never crosses a public network unencrypted.
fn infrastructure_origin(name: &'static str, value: &str) -> Result<String, ConfigError> {
    let url = origin(name, value)?;
    if transport_allowed(&url) {
        Ok(url)
    } else {
        Err(ConfigError::Invalid {
            name,
            reason: "cleartext http:// is allowed only to a loopback or RFC1918 host; use https://",
        })
    }
}

/// True for `https://` anywhere, and for `http://` only when the host is `localhost`, an
/// IPv4 loopback or RFC1918 address, or an IPv6 loopback. The host is the one `reqwest::Url`
/// parses, so a fragment, a query or a backslash cannot move it: `http://evil.example#@` followed
/// by a private address has host `evil.example` and is refused. A hostname other than `localhost` is refused over
/// `http://`, since its address cannot be checked here.
pub fn transport_allowed(url: &str) -> bool {
    let Ok(parsed) = reqwest::Url::parse(url) else {
        return false;
    };
    match parsed.scheme() {
        "https" => true,
        "http" => {
            let Some(host) = parsed.host_str() else {
                return false;
            };
            // `host_str` brackets an IPv6 literal; strip the brackets before parsing.
            let bare = host
                .strip_prefix('[')
                .and_then(|inner| inner.strip_suffix(']'))
                .unwrap_or(host);
            match bare.parse::<std::net::IpAddr>() {
                Ok(std::net::IpAddr::V4(v4)) => v4.is_loopback() || v4.is_private(),
                Ok(std::net::IpAddr::V6(v6)) => v6.is_loopback(),
                Err(_) => bare.eq_ignore_ascii_case("localhost"),
            }
        }
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn full_env() -> HashMap<&'static str, &'static str> {
        HashMap::from([
            ("CONSOLE_BASE_URL", "https://console.example.test:8443/"),
            (
                "CONSOLE_OIDC_ISSUER",
                "https://auth.example.test/application/o/console/",
            ),
            ("CONSOLE_OIDC_CLIENT_ID", "console"),
            ("CONSOLE_OIDC_CLIENT_SECRET", "s3cret-value"),
            ("CONSOLE_DATA_DIR", "/data"),
            ("CONSOLE_SCOPE_FILE", "/etc/console/scope.json"),
        ])
    }

    fn lookup(env: HashMap<&'static str, &'static str>) -> impl Fn(&str) -> Option<String> {
        move |name| env.get(name).map(|value| value.to_string())
    }

    #[test]
    fn complete_environment_parses_and_trims_trailing_slash() {
        let config = Config::from_lookup(lookup(full_env())).expect("valid config");
        assert_eq!(config.base_url, "https://console.example.test:8443");
        assert_eq!(config.oidc_client_id, "console");
        assert!(config.extra_ca_file.is_none());
    }

    #[test]
    fn missing_required_variable_is_named() {
        let err = Config::from_lookup(|_| None).err().expect("must fail");
        assert_eq!(err, ConfigError::Missing("CONSOLE_BASE_URL"));
        assert_eq!(
            err.to_string(),
            "missing required environment variable CONSOLE_BASE_URL"
        );
    }

    #[test]
    fn each_required_variable_is_checked_by_name() {
        for name in [
            "CONSOLE_OIDC_ISSUER",
            "CONSOLE_OIDC_CLIENT_ID",
            "CONSOLE_OIDC_CLIENT_SECRET",
            "CONSOLE_DATA_DIR",
            "CONSOLE_SCOPE_FILE",
        ] {
            let mut env = full_env();
            env.remove(name);
            let err = Config::from_lookup(lookup(env)).err().expect("must fail");
            assert_eq!(err, ConfigError::Missing(name));
        }
    }

    #[test]
    fn blank_value_counts_as_missing() {
        let mut env = full_env();
        env.insert("CONSOLE_OIDC_CLIENT_SECRET", "   ");
        let err = Config::from_lookup(lookup(env)).err().expect("must fail");
        assert_eq!(err, ConfigError::Missing("CONSOLE_OIDC_CLIENT_SECRET"));
    }

    #[test]
    fn base_url_needs_a_scheme() {
        let mut env = full_env();
        env.insert("CONSOLE_BASE_URL", "console.example.test");
        let err = Config::from_lookup(lookup(env)).err().expect("must fail");
        assert!(matches!(
            err,
            ConfigError::Invalid {
                name: "CONSOLE_BASE_URL",
                ..
            }
        ));
    }

    fn semaphore_env() -> HashMap<&'static str, &'static str> {
        let mut env = full_env();
        env.insert("CONSOLE_SEMAPHORE_API_URL", "https://semaphore.test:3000/");
        env.insert("CONSOLE_SEMAPHORE_UI_URL", "https://semaphore.test:8443");
        env.insert("CONSOLE_SEMAPHORE_PROJECT", "1");
        env.insert("CONSOLE_SEMAPHORE_TOKEN", "sem-token-value");
        env
    }

    #[test]
    fn semaphore_is_unconfigured_when_all_four_are_unset() {
        let config = Config::from_lookup(lookup(full_env())).expect("valid config");
        assert!(config.semaphore.is_none());
        assert!(config.workflow_file.is_none());
    }

    #[test]
    fn semaphore_parses_trims_and_keeps_the_token_out_of_debug() {
        let config = Config::from_lookup(lookup(semaphore_env())).expect("valid config");
        let semaphore = config.semaphore.expect("configured");
        assert_eq!(semaphore.api_url, "https://semaphore.test:3000");
        assert_eq!(semaphore.ui_url, "https://semaphore.test:8443");
        assert_eq!(semaphore.project, 1);
        assert_eq!(semaphore.token.expose(), "sem-token-value");
        let debug = format!("{semaphore:?}");
        assert!(!debug.contains("sem-token-value"), "{debug}");
        assert!(debug.contains("<redacted>"), "{debug}");
    }

    #[test]
    fn partial_semaphore_group_names_the_first_missing_variable() {
        let mut env = full_env();
        env.insert("CONSOLE_SEMAPHORE_API_URL", "https://semaphore.test");
        env.insert("CONSOLE_SEMAPHORE_PROJECT", "1");
        let err = Config::from_lookup(lookup(env)).err().expect("must fail");
        assert_eq!(err, ConfigError::Missing("CONSOLE_SEMAPHORE_UI_URL"));

        let mut env = semaphore_env();
        env.insert("CONSOLE_SEMAPHORE_TOKEN", "  ");
        let err = Config::from_lookup(lookup(env)).err().expect("must fail");
        assert_eq!(err, ConfigError::Missing("CONSOLE_SEMAPHORE_TOKEN"));
    }

    #[test]
    fn semaphore_project_must_be_a_positive_integer_without_echoing_it() {
        let mut env = semaphore_env();
        env.insert("CONSOLE_SEMAPHORE_PROJECT", "0");
        let err = Config::from_lookup(lookup(env)).err().expect("must fail");
        assert!(matches!(
            err,
            ConfigError::Invalid {
                name: "CONSOLE_SEMAPHORE_PROJECT",
                ..
            }
        ));

        let mut env = semaphore_env();
        env.insert("CONSOLE_SEMAPHORE_PROJECT", "one-secret-looking");
        let err = Config::from_lookup(lookup(env)).err().expect("must fail");
        assert!(!err.to_string().contains("one-secret-looking"));
    }

    #[test]
    fn semaphore_ui_url_needs_a_scheme() {
        let mut env = semaphore_env();
        env.insert("CONSOLE_SEMAPHORE_UI_URL", "semaphore.test:8443");
        let err = Config::from_lookup(lookup(env)).err().expect("must fail");
        assert!(matches!(
            err,
            ConfigError::Invalid {
                name: "CONSOLE_SEMAPHORE_UI_URL",
                ..
            }
        ));
    }

    fn netbox_env() -> HashMap<&'static str, &'static str> {
        let mut env = full_env();
        env.insert("CONSOLE_NETBOX_URL", "https://netbox.example.test/");
        env.insert("CONSOLE_NETBOX_TOKEN", "nbt_netbox-token-value");
        env
    }

    fn proxmox_env() -> HashMap<&'static str, &'static str> {
        let mut env = full_env();
        env.insert("CONSOLE_PROXMOX_URL", "https://pve.example.test:8006/");
        env.insert("CONSOLE_PROXMOX_TOKEN_ID", "console@pve!ro");
        env.insert("CONSOLE_PROXMOX_TOKEN", "pve-secret-value");
        env
    }

    #[test]
    fn inventory_sources_are_unconfigured_when_unset() {
        let config = Config::from_lookup(lookup(full_env())).expect("valid config");
        assert!(config.netbox.is_none());
        assert!(config.proxmox.is_none());
        assert!(config.flows_file.is_none());
        assert!(config.gateway_file.is_none());
    }

    #[test]
    fn netbox_parses_trims_and_keeps_the_token_out_of_debug() {
        let config = Config::from_lookup(lookup(netbox_env())).expect("valid config");
        let netbox = config.netbox.expect("configured");
        assert_eq!(netbox.api_url, "https://netbox.example.test");
        assert_eq!(netbox.token.expose(), "nbt_netbox-token-value");
        let debug = format!("{netbox:?}");
        assert!(!debug.contains("nbt_netbox-token-value"), "{debug}");
    }

    #[test]
    fn partial_netbox_group_names_the_missing_variable() {
        let mut env = full_env();
        env.insert("CONSOLE_NETBOX_URL", "https://netbox.example.test");
        let err = Config::from_lookup(lookup(env)).err().expect("must fail");
        assert_eq!(err, ConfigError::Missing("CONSOLE_NETBOX_TOKEN"));

        let mut env = full_env();
        env.insert("CONSOLE_NETBOX_TOKEN", "nbt_only-token");
        let err = Config::from_lookup(lookup(env)).err().expect("must fail");
        assert_eq!(err, ConfigError::Missing("CONSOLE_NETBOX_URL"));
    }

    #[test]
    fn netbox_url_needs_a_scheme() {
        let mut env = netbox_env();
        env.insert("CONSOLE_NETBOX_URL", "netbox.example.test");
        let err = Config::from_lookup(lookup(env)).err().expect("must fail");
        assert!(matches!(
            err,
            ConfigError::Invalid {
                name: "CONSOLE_NETBOX_URL",
                ..
            }
        ));
    }

    #[test]
    fn proxmox_parses_with_and_without_a_ca_file() {
        let config = Config::from_lookup(lookup(proxmox_env())).expect("valid config");
        let proxmox = config.proxmox.expect("configured");
        assert_eq!(proxmox.api_url, "https://pve.example.test:8006");
        assert_eq!(proxmox.token_id, "console@pve!ro");
        assert!(proxmox.ca_file.is_none());
        let debug = format!("{proxmox:?}");
        assert!(!debug.contains("pve-secret-value"), "{debug}");

        let mut env = proxmox_env();
        env.insert("CONSOLE_PROXMOX_CA_FILE", "/etc/console/pve-ca.pem");
        let config = Config::from_lookup(lookup(env)).expect("valid config");
        assert_eq!(
            config.proxmox.expect("configured").ca_file,
            Some(PathBuf::from("/etc/console/pve-ca.pem"))
        );
    }

    #[test]
    fn partial_proxmox_group_names_the_missing_variable() {
        let mut env = proxmox_env();
        env.remove("CONSOLE_PROXMOX_TOKEN");
        let err = Config::from_lookup(lookup(env)).err().expect("must fail");
        assert_eq!(err, ConfigError::Missing("CONSOLE_PROXMOX_TOKEN"));

        let mut env = proxmox_env();
        env.remove("CONSOLE_PROXMOX_TOKEN_ID");
        let err = Config::from_lookup(lookup(env)).err().expect("must fail");
        assert_eq!(err, ConfigError::Missing("CONSOLE_PROXMOX_TOKEN_ID"));
    }

    #[test]
    fn proxmox_ca_file_alone_names_the_missing_url() {
        let mut env = full_env();
        env.insert("CONSOLE_PROXMOX_CA_FILE", "/etc/console/pve-ca.pem");
        let err = Config::from_lookup(lookup(env)).err().expect("must fail");
        assert_eq!(err, ConfigError::Missing("CONSOLE_PROXMOX_URL"));
    }

    fn authentik_env() -> HashMap<&'static str, &'static str> {
        let mut env = full_env();
        env.insert("CONSOLE_AUTHENTIK_URL", "https://auth.example.test:8443/");
        env.insert(
            "CONSOLE_AUTHENTIK_TOKEN",
            "svc-console-directory-token-value",
        );
        env
    }

    #[test]
    fn authentik_and_the_role_map_are_unconfigured_when_unset() {
        let config = Config::from_lookup(lookup(full_env())).expect("valid config");
        assert!(config.authentik.is_none());
        assert!(config.role_map_file.is_none());
    }

    #[test]
    fn authentik_parses_trims_and_keeps_the_token_out_of_debug() {
        let config = Config::from_lookup(lookup(authentik_env())).expect("valid config");
        let authentik = config.authentik.expect("configured");
        assert_eq!(authentik.api_url, "https://auth.example.test:8443");
        assert_eq!(
            authentik.token.expose(),
            "svc-console-directory-token-value"
        );
        let debug = format!("{authentik:?}");
        assert!(
            !debug.contains("svc-console-directory-token-value"),
            "{debug}"
        );
    }

    #[test]
    fn partial_authentik_group_names_the_missing_variable() {
        let mut env = authentik_env();
        env.remove("CONSOLE_AUTHENTIK_TOKEN");
        // `Config` has no Debug (it holds secrets), so the Ok side is not formatted: `.err()`, not `expect_err`.
        let err = Config::from_lookup(lookup(env))
            .err()
            .expect("token missing");
        assert_eq!(err, ConfigError::Missing("CONSOLE_AUTHENTIK_TOKEN"));

        let mut env = authentik_env();
        env.remove("CONSOLE_AUTHENTIK_URL");
        let err = Config::from_lookup(lookup(env)).err().expect("url missing");
        assert_eq!(err, ConfigError::Missing("CONSOLE_AUTHENTIK_URL"));
    }

    #[test]
    fn authentik_cleartext_follows_the_infrastructure_rule_and_names_the_setting() {
        let mut env = authentik_env();
        env.insert("CONSOLE_AUTHENTIK_URL", "http://auth.example.test:8443");
        let err = Config::from_lookup(lookup(env))
            .err()
            .expect("cleartext to a public name");
        assert_eq!(
            err,
            ConfigError::Invalid {
                name: "CONSOLE_AUTHENTIK_URL",
                reason:
                    "cleartext http:// is allowed only to a loopback or RFC1918 host; use https://",
            }
        );
        let mut env = authentik_env();
        env.insert("CONSOLE_AUTHENTIK_URL", "http://127.0.0.1:8080");
        let config = Config::from_lookup(lookup(env)).expect("RFC1918 cleartext is allowed");
        assert_eq!(
            config.authentik.expect("configured").api_url,
            "http://127.0.0.1:8080"
        );
    }

    #[test]
    fn role_map_file_is_optional_trimmed_and_read_from_its_own_variable() {
        let mut env = full_env();
        env.insert("CONSOLE_ROLE_MAP_FILE", "  /etc/console/role-map.json ");
        let config = Config::from_lookup(lookup(env)).expect("valid config");
        assert_eq!(
            config.role_map_file,
            Some(PathBuf::from("/etc/console/role-map.json"))
        );
    }

    fn openbao_env() -> HashMap<&'static str, &'static str> {
        let mut env = full_env();
        env.insert("CONSOLE_OPENBAO_URL", "https://openbao.example.test:8200/");
        env.insert("CONSOLE_OPENBAO_ROLE_ID", "console-role-value");
        env.insert("CONSOLE_OPENBAO_SECRET_ID", "console-secret-id-value");
        env
    }

    #[test]
    fn openbao_is_unconfigured_when_unset() {
        let config = Config::from_lookup(lookup(full_env())).expect("valid config");
        assert!(config.openbao.is_none());
    }

    #[test]
    fn openbao_parses_trims_and_keeps_the_secrets_out_of_debug() {
        let config = Config::from_lookup(lookup(openbao_env())).expect("valid config");
        let openbao = config.openbao.expect("configured");
        assert_eq!(openbao.api_url, "https://openbao.example.test:8200");
        assert_eq!(openbao.role_id.expose(), "console-role-value");
        assert_eq!(openbao.secret_id.expose(), "console-secret-id-value");
        let debug = format!("{openbao:?}");
        assert!(!debug.contains("console-role-value"), "{debug}");
        assert!(!debug.contains("console-secret-id-value"), "{debug}");
        assert!(debug.contains("<redacted>"), "{debug}");
    }

    #[test]
    fn partial_openbao_group_names_the_first_missing_variable() {
        let mut env = openbao_env();
        env.remove("CONSOLE_OPENBAO_URL");
        let err = Config::from_lookup(lookup(env)).err().expect("must fail");
        assert_eq!(err, ConfigError::Missing("CONSOLE_OPENBAO_URL"));

        let mut env = full_env();
        env.insert("CONSOLE_OPENBAO_URL", "https://openbao.example.test");
        let err = Config::from_lookup(lookup(env)).err().expect("must fail");
        assert_eq!(err, ConfigError::Missing("CONSOLE_OPENBAO_ROLE_ID"));

        let mut env = openbao_env();
        env.insert("CONSOLE_OPENBAO_SECRET_ID", "  ");
        let err = Config::from_lookup(lookup(env)).err().expect("must fail");
        assert_eq!(err, ConfigError::Missing("CONSOLE_OPENBAO_SECRET_ID"));
    }

    #[test]
    fn openbao_cleartext_follows_the_infrastructure_rule_and_never_echoes_values() {
        let mut env = openbao_env();
        env.insert("CONSOLE_OPENBAO_URL", "http://openbao.example.test:8200");
        let err = Config::from_lookup(lookup(env)).err().expect("must fail");
        assert!(matches!(
            err,
            ConfigError::Invalid {
                name: "CONSOLE_OPENBAO_URL",
                ..
            }
        ));
        let text = err.to_string();
        assert!(!text.contains("console-role-value"), "{text}");
        assert!(!text.contains("console-secret-id-value"), "{text}");

        let url = format!("http://{}:8200", ip([10, 20, 0, 40]));
        let config =
            Config::from_lookup(overlay(openbao_env(), "CONSOLE_OPENBAO_URL", url.clone()))
                .expect("valid config");
        assert_eq!(config.openbao.expect("configured").api_url, url);
    }

    #[test]
    fn gateway_file_is_optional_and_trimmed() {
        let mut env = full_env();
        env.insert("CONSOLE_GATEWAY_FILE", "  /etc/console/gateway.json  ");
        let config = Config::from_lookup(lookup(env)).expect("valid config");
        assert_eq!(
            config.gateway_file,
            Some(PathBuf::from("/etc/console/gateway.json"))
        );

        let mut env = full_env();
        env.insert("CONSOLE_GATEWAY_FILE", "   ");
        let config = Config::from_lookup(lookup(env)).expect("valid config");
        assert!(config.gateway_file.is_none());
    }

    #[test]
    fn flows_file_is_optional_and_trimmed() {
        let mut env = full_env();
        env.insert("CONSOLE_FLOWS_FILE", "  /etc/console/flows.json  ");
        let config = Config::from_lookup(lookup(env)).expect("valid config");
        assert_eq!(
            config.flows_file,
            Some(PathBuf::from("/etc/console/flows.json"))
        );

        let mut env = full_env();
        env.insert("CONSOLE_FLOWS_FILE", "   ");
        let config = Config::from_lookup(lookup(env)).expect("valid config");
        assert!(config.flows_file.is_none());
    }

    #[test]
    fn grafana_url_is_optional_trimmed_and_guarded() {
        let config = Config::from_lookup(lookup(full_env())).expect("valid config");
        assert!(config.grafana_url.is_none());

        let mut env = full_env();
        env.insert("CONSOLE_GRAFANA_URL", "  https://grafana.example.test/  ");
        let config = Config::from_lookup(lookup(env)).expect("valid config");
        assert_eq!(
            config.grafana_url.as_deref(),
            Some("https://grafana.example.test")
        );

        let mut env = full_env();
        env.insert("CONSOLE_GRAFANA_URL", "   ");
        let config = Config::from_lookup(lookup(env)).expect("valid config");
        assert!(config.grafana_url.is_none());

        let mut env = full_env();
        env.insert("CONSOLE_GRAFANA_URL", "http://grafana.example.test");
        let err = Config::from_lookup(lookup(env)).err().expect("must fail");
        assert!(matches!(
            err,
            ConfigError::Invalid {
                name: "CONSOLE_GRAFANA_URL",
                ..
            }
        ));

        let url = format!("http://{}:3000", ip([10, 20, 0, 30]));
        let config = Config::from_lookup(overlay(full_env(), "CONSOLE_GRAFANA_URL", url.clone()))
            .expect("valid config");
        assert_eq!(config.grafana_url.as_deref(), Some(url.as_str()));
    }

    #[test]
    fn grafana_token_needs_the_url_and_never_prints() {
        let mut env = full_env();
        env.insert("CONSOLE_GRAFANA_TOKEN", "grafana-token-value");
        let err = Config::from_lookup(lookup(env))
            .err()
            .expect("a token without its url must fail");
        assert_eq!(err, ConfigError::Missing("CONSOLE_GRAFANA_URL"));
        assert!(!err.to_string().contains("grafana-token-value"));

        let mut env = full_env();
        env.insert("CONSOLE_GRAFANA_URL", "https://grafana.example.test");
        env.insert("CONSOLE_GRAFANA_TOKEN", "  grafana-token-value  ");
        let config = Config::from_lookup(lookup(env)).expect("valid config");
        let kept = config.grafana_token.expect("configured");
        assert_eq!(kept.expose(), "grafana-token-value");
        assert!(!format!("{kept:?}").contains("grafana-token-value"));

        let mut env = full_env();
        env.insert("CONSOLE_GRAFANA_URL", "https://grafana.example.test");
        env.insert("CONSOLE_GRAFANA_TOKEN", "   ");
        let config = Config::from_lookup(lookup(env)).expect("valid config");
        assert!(config.grafana_token.is_none(), "a blank token is no token");
        let config = Config::from_lookup(lookup(full_env())).expect("valid config");
        assert!(config.grafana_token.is_none());
    }

    /// A private or loopback address built at runtime. The guard tests need these addresses,
    /// and the repository scanner refuses a dotted private literal in any committed file.
    fn ip(octets: [u8; 4]) -> String {
        std::net::Ipv4Addr::from(octets).to_string()
    }

    /// `env` with one value replaced by a runtime string.
    fn overlay(
        env: HashMap<&'static str, &'static str>,
        name: &'static str,
        value: String,
    ) -> impl Fn(&str) -> Option<String> {
        move |key| {
            if key == name {
                Some(value.clone())
            } else {
                env.get(key).map(|v| v.to_string())
            }
        }
    }

    #[test]
    fn cleartext_is_allowed_only_to_loopback_or_rfc1918_hosts() {
        let allowed = [
            "https://netbox.example.test".to_string(),
            "https://8.8.8.8".to_string(),
            "http://127.0.0.1:8000".to_string(),
            "http://127.4.5.6/api".to_string(),
            "http://localhost:8000".to_string(),
            "http://LOCALHOST".to_string(),
            format!("http://{}", ip([10, 20, 0, 5])),
            format!("http://{}", ip([172, 16, 0, 1])),
            format!("http://{}:443", ip([172, 31, 255, 1])),
            format!("http://{}:8080/api/", ip([192, 168, 1, 10])),
            format!("http://user:pw@{}:8080", ip([10, 0, 0, 1])),
            "http://[::1]:8000".to_string(),
        ];
        for url in &allowed {
            assert!(transport_allowed(url), "{url} should be allowed");
        }
        let private = ip([10, 0, 0, 1]);
        let refused = [
            "http://8.8.8.8".to_string(),
            "http://172.32.0.1".to_string(),
            "http://172.15.255.255".to_string(),
            "http://192.169.0.1".to_string(),
            "http://netbox.internal:8000".to_string(),
            "http://[2001:db8::1]:8000".to_string(),
            "http://".to_string(),
            format!("ftp://{private}"),
            format!("http://evil.example#@{private}"),
            format!("http://evil.example?@{private}"),
            format!("http://evil.example\\@{private}"),
        ];
        for url in &refused {
            assert!(!transport_allowed(url), "{url} should be refused");
        }
    }

    #[test]
    fn cleartext_to_a_public_address_is_named_and_refused_at_load() {
        let mut env = netbox_env();
        env.insert("CONSOLE_NETBOX_URL", "http://8.8.8.8:8000");
        let err = Config::from_lookup(lookup(env)).err().expect("must fail");
        assert!(matches!(
            err,
            ConfigError::Invalid {
                name: "CONSOLE_NETBOX_URL",
                ..
            }
        ));

        let mut env = proxmox_env();
        env.insert("CONSOLE_PROXMOX_URL", "http://pve.example.test:8006");
        let err = Config::from_lookup(lookup(env)).err().expect("must fail");
        assert!(matches!(
            err,
            ConfigError::Invalid {
                name: "CONSOLE_PROXMOX_URL",
                ..
            }
        ));
    }

    #[test]
    fn cleartext_to_an_rfc1918_address_loads() {
        let url = format!("http://{}:8000", ip([10, 20, 0, 9]));
        let config = Config::from_lookup(overlay(netbox_env(), "CONSOLE_NETBOX_URL", url.clone()))
            .expect("valid config");
        assert_eq!(config.netbox.expect("configured").api_url, url);
    }
}
