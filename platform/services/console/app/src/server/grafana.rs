//! Grafana deep links for the Phase 3 views. The console links out to Grafana and never
//! calls it. Dashboard uids and variable names are the ones the o11y dashboards declare, in
//! `platform/services/o11y/deployment/config/grafana/dashboards/*.json`:
//!
//! - `service-overview`: variables `service` and `log_service`.
//! - `service-conformance`: no variables.
//!
//! Query values are encoded by `reqwest::Url`, so a service name cannot add a parameter.

use reqwest::Url;

/// The per-service dashboard for one VM or service, as a ready-made link.
/// `None` only when `base` does not parse as a URL, which the config guard prevents.
pub fn service_overview(base: &str, service: &str) -> Option<String> {
    let mut url = Url::parse(&format!("{base}/d/service-overview/service-overview")).ok()?;
    url.query_pairs_mut()
        .append_pair("var-service", service)
        .append_pair("var-log_service", service);
    Some(url.as_str().to_string())
}

/// The service-deployment conformance dashboard. It takes no variables.
pub fn service_conformance(base: &str) -> Option<String> {
    let url = Url::parse(&format!(
        "{base}/d/service-conformance/service-deployment-conformance"
    ))
    .ok()?;
    Some(url.as_str().to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    const BASE: &str = "https://grafana.example.test";

    fn query_of(link: &str) -> Vec<(String, String)> {
        Url::parse(link)
            .expect("link parses")
            .query_pairs()
            .map(|(k, v)| (k.into_owned(), v.into_owned()))
            .collect()
    }

    #[test]
    fn service_overview_link_sets_both_variables_to_the_service() {
        assert_eq!(
            service_overview(BASE, "tududi").as_deref(),
            Some("https://grafana.example.test/d/service-overview/service-overview?var-service=tududi&var-log_service=tududi")
        );
    }

    #[test]
    fn a_service_name_cannot_add_a_parameter_and_round_trips() {
        let name = "odd name&var-x=1#frag";
        let link = service_overview(BASE, name).expect("link");
        assert!(!link.contains("var-x=1"), "{link}");
        assert!(!link.contains('#'), "{link}");
        assert_eq!(
            query_of(&link),
            [
                ("var-service".to_string(), name.to_string()),
                ("var-log_service".to_string(), name.to_string()),
            ]
        );
    }

    #[test]
    fn conformance_link_has_the_fixed_path_and_no_variables() {
        assert_eq!(
            service_conformance(BASE).as_deref(),
            Some(
                "https://grafana.example.test/d/service-conformance/service-deployment-conformance"
            )
        );
        assert!(query_of(&service_conformance(BASE).expect("link")).is_empty());
    }

    #[test]
    fn a_base_with_a_sub_path_keeps_it() {
        assert_eq!(
            service_conformance("https://o11y.example.test/grafana").as_deref(),
            Some("https://o11y.example.test/grafana/d/service-conformance/service-deployment-conformance")
        );
    }

    #[test]
    fn an_unparseable_base_gives_no_link() {
        assert_eq!(service_overview("not a url", "tududi"), None);
        assert_eq!(service_conformance("http://[::1"), None);
    }
}
