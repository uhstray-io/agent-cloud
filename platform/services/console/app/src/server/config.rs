//! Environment configuration. Parsed from an injected lookup so tests never
//! touch the process environment. Secret values never reach an error message.

use std::fmt;
use std::path::PathBuf;

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

        Ok(Config {
            base_url,
            oidc_issuer,
            oidc_client_id,
            oidc_client_secret,
            extra_ca_file,
            data_dir,
            scope_file,
        })
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
}
