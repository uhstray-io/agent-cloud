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
        })
    }
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
}
