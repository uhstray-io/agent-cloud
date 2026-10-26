//! The scope bar: which org, site and environment are chosen, what each select
//! offers, and how a default scope is written to the preferences.
//!
//! A scope is one of `org`, `org/site` or `org/site/env`, written with ids so a
//! renamed org keeps its saved default. The tenant is not part of a scope. The
//! sidebar owns the tenant, and the saved default applies to the tenant that
//! opens first.

use dioxus::prelude::*;

use super::api::Tree;
use super::icons::icon;
use super::shell::{Ctx, Loaded};

/// The chosen org, and optionally one site and one env. `None` means "all".
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Scope {
    pub org: String,
    pub site: Option<String>,
    pub env: Option<String>,
}

/// One option in the default-scope select: the stored value and its label.
#[derive(Debug, Clone, PartialEq)]
pub struct Choice {
    pub value: String,
    pub label: String,
}

/// The scope to open with: the saved default when it resolves in this tree,
/// otherwise the first org with all sites and all envs.
pub fn initial(tree: &Tree, saved: Option<&str>) -> Scope {
    saved
        .and_then(|text| resolve(tree, text))
        .unwrap_or_else(|| first(tree))
}

fn first(tree: &Tree) -> Scope {
    Scope {
        org: tree.orgs.first().map(|o| o.id.clone()).unwrap_or_default(),
        site: None,
        env: None,
    }
}

/// Resolves a stored default against the tree. `None` when any part is not
/// found, or when the text has more than three parts.
pub fn resolve(tree: &Tree, text: &str) -> Option<Scope> {
    let parts: Vec<&str> = text.split('/').collect();
    let org = tree.orgs.iter().find(|o| o.id == parts[0])?;
    let mut scope = Scope {
        org: org.id.clone(),
        site: None,
        env: None,
    };
    match parts.as_slice() {
        [_] => Some(scope),
        [_, site_id] => {
            let site = org.sites.iter().find(|s| s.id == *site_id)?;
            scope.site = Some(site.id.clone());
            Some(scope)
        }
        [_, site_id, env] => {
            let site = org.sites.iter().find(|s| s.id == *site_id)?;
            if !site.envs.iter().any(|e| e == env) {
                return None;
            }
            scope.site = Some(site.id.clone());
            scope.env = Some((*env).to_string());
            Some(scope)
        }
        _ => None,
    }
}

/// The (id, name) of each site under an org.
pub fn sites(tree: &Tree, org_id: &str) -> Vec<(String, String)> {
    tree.orgs
        .iter()
        .find(|o| o.id == org_id)
        .map(|org| {
            org.sites
                .iter()
                .map(|s| (s.id.clone(), s.name.clone()))
                .collect()
        })
        .unwrap_or_default()
}

/// The envs the env select offers: the chosen site's envs, or, with all
/// sites chosen, every env the org's sites share, in first-seen order.
pub fn envs(tree: &Tree, scope: &Scope) -> Vec<String> {
    let Some(org) = tree.orgs.iter().find(|o| o.id == scope.org) else {
        return Vec::new();
    };
    match &scope.site {
        Some(site_id) => org
            .sites
            .iter()
            .find(|s| &s.id == site_id)
            .map(|s| s.envs.clone())
            .unwrap_or_default(),
        None => {
            let mut out: Vec<String> = Vec::new();
            for site in &org.sites {
                for env in &site.envs {
                    if !out.contains(env) {
                        out.push(env.clone());
                    }
                }
            }
            out
        }
    }
}

/// Picks an org. Site and env reset to all.
pub fn choose_org(org_id: &str) -> Scope {
    Scope {
        org: org_id.to_string(),
        site: None,
        env: None,
    }
}

/// Picks a site, or all sites with `None`. The env stays when the new site
/// still offers it and resets to all otherwise.
pub fn choose_site(tree: &Tree, scope: &Scope, site_id: Option<&str>) -> Scope {
    let mut next = Scope {
        org: scope.org.clone(),
        site: site_id.map(str::to_string),
        env: None,
    };
    if let Some(env) = &scope.env {
        if envs(tree, &next).contains(env) {
            next.env = Some(env.clone());
        }
    }
    next
}

/// Picks an env, or all envs with `None`.
pub fn choose_env(scope: &Scope, env: Option<&str>) -> Scope {
    Scope {
        org: scope.org.clone(),
        site: scope.site.clone(),
        env: env.map(str::to_string),
    }
}

/// The stored form of the default-scope select. An empty value is "None".
pub fn default_text(value: &str) -> Option<String> {
    if value.is_empty() {
        None
    } else {
        Some(value.to_string())
    }
}

/// Options for the default-scope select: "None", then every org, site and env
/// of the tree. `current` is the draft value. When it is not in the tree
/// (saved for another tenant, say), it is still listed and marked, so the
/// select never shows a value it does not hold.
pub fn default_choices(tree: Option<&Tree>, current: &str) -> Vec<Choice> {
    let mut out = vec![Choice {
        value: String::new(),
        label: "None".to_string(),
    }];
    if let Some(tree) = tree {
        for org in &tree.orgs {
            out.push(Choice {
                value: org.id.clone(),
                label: org.name.clone(),
            });
            for site in &org.sites {
                out.push(Choice {
                    value: format!("{}/{}", org.id, site.id),
                    label: format!("{} \u{203A} {}", org.name, site.name),
                });
                for env in &site.envs {
                    out.push(Choice {
                        value: format!("{}/{}/{}", org.id, site.id, env),
                        label: format!("{} \u{203A} {} \u{203A} {}", org.name, site.name, env),
                    });
                }
            }
        }
    }
    if !current.is_empty() && !out.iter().any(|c| c.value == current) {
        out.push(Choice {
            value: current.to_string(),
            label: format!("{current} (not in this tenant)"),
        });
    }
    out
}

/// The scope bar. It reads the loaded tenant list and tree, and shows the
/// example badge whenever either carries the example flag. `Workspace` does not
/// render it for a caller with no tenant, so it never sees an empty list.
#[component]
pub fn ScopeBar() -> Element {
    let ctx = use_context::<Ctx>();
    let tenants = ctx.tenants.read().clone();
    let tree = ctx.tree.read().clone();
    let example = matches!(&tenants, Loaded::Ready(list) if list.example)
        || matches!(&tree, Loaded::Ready(t) if t.example);

    let fields = match (&tenants, &tree) {
        (_, Loaded::Ready(tree)) => rsx! { ScopeSelects { tree: tree.clone() } },
        (Loaded::Failed, _) | (_, Loaded::Failed) => {
            rsx! { p { class: "muted", "Scope is unavailable." } }
        }
        _ => rsx! { p { class: "faint", "Loading scope\u{2026}" } },
    };

    rsx! {
        header { class: "top",
            {fields}
            if example {
                span {
                    class: "synthetic",
                    title: "Every tenant, org, site and environment here is example data",
                    {icon("flask-conical")}
                    "Example data"
                }
            }
        }
    }
}

#[component]
fn ScopeSelects(tree: Tree) -> Element {
    let mut ctx = use_context::<Ctx>();
    let scope = ctx.scope.read().clone();
    let org_options: Vec<(String, String)> = tree
        .orgs
        .iter()
        .map(|o| (o.id.clone(), o.name.clone()))
        .collect();
    let site_options = sites(&tree, &scope.org);
    let env_options = envs(&tree, &scope);
    let site_value = scope.site.clone().unwrap_or_default();
    let env_value = scope.env.clone().unwrap_or_default();

    let site_tree = tree.clone();
    let site_scope = scope.clone();
    let env_scope = scope.clone();

    rsx! {
        div { class: "scope", role: "group", aria_label: "Scope",
            label { class: "scope-field",
                span { class: "label", "Org" }
                select {
                    class: "scope-select",
                    value: "{scope.org}",
                    onchange: move |evt: FormEvent| {
                        ctx.scope.set(choose_org(&evt.value()));
                    },
                    for (id, name) in org_options.iter() {
                        option { value: "{id}", selected: *id == scope.org, "{name}" }
                    }
                }
            }
            label { class: "scope-field",
                span { class: "label", "Site" }
                select {
                    class: "scope-select",
                    value: "{site_value}",
                    onchange: move |evt: FormEvent| {
                        let value = evt.value();
                        let picked = if value.is_empty() { None } else { Some(value.as_str()) };
                        ctx.scope.set(choose_site(&site_tree, &site_scope, picked));
                    },
                    option { value: "", selected: site_value.is_empty(), "All sites" }
                    for (id, name) in site_options.iter() {
                        option { value: "{id}", selected: *id == site_value, "{name}" }
                    }
                }
            }
            label { class: "scope-field",
                span { class: "label", "Env" }
                select {
                    class: "scope-select",
                    value: "{env_value}",
                    onchange: move |evt: FormEvent| {
                        let value = evt.value();
                        let picked = if value.is_empty() { None } else { Some(value.as_str()) };
                        ctx.scope.set(choose_env(&env_scope, picked));
                    },
                    option { value: "", selected: env_value.is_empty(), "All envs" }
                    for env in env_options.iter() {
                        option { value: "{env}", selected: *env == env_value, "{env}" }
                    }
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ui::api::{Org, Site};

    fn site(id: &str, name: &str, envs: &[&str]) -> Site {
        Site {
            id: id.into(),
            name: name.into(),
            envs: envs.iter().map(|e| e.to_string()).collect(),
        }
    }

    fn tree() -> Tree {
        Tree {
            example: true,
            orgs: vec![
                Org {
                    id: "platform".into(),
                    name: "Platform".into(),
                    sites: vec![
                        site("hq", "site-hq", &["prod", "dev"]),
                        site("lab", "lab", &["dev", "local"]),
                    ],
                },
                Org {
                    id: "services".into(),
                    name: "Services".into(),
                    sites: vec![],
                },
            ],
        }
    }

    #[test]
    fn resolves_one_two_and_three_part_defaults() {
        let t = tree();
        assert_eq!(
            resolve(&t, "platform"),
            Some(Scope {
                org: "platform".into(),
                site: None,
                env: None
            })
        );
        assert_eq!(
            resolve(&t, "platform/hq"),
            Some(Scope {
                org: "platform".into(),
                site: Some("hq".into()),
                env: None
            })
        );
        assert_eq!(
            resolve(&t, "platform/hq/prod"),
            Some(Scope {
                org: "platform".into(),
                site: Some("hq".into()),
                env: Some("prod".into())
            })
        );
    }

    #[test]
    fn rejects_unknown_parts_and_extra_segments() {
        let t = tree();
        assert_eq!(resolve(&t, "nope"), None);
        assert_eq!(resolve(&t, "platform/nope"), None);
        assert_eq!(
            resolve(&t, "platform/hq/local"),
            None,
            "lab's env, not hq's"
        );
        assert_eq!(resolve(&t, "platform/hq/prod/extra"), None);
    }

    #[test]
    fn initial_uses_a_resolving_default_else_first_org_all() {
        let t = tree();
        let first = Scope {
            org: "platform".into(),
            site: None,
            env: None,
        };
        assert_eq!(
            initial(&t, Some("platform/lab")).site.as_deref(),
            Some("lab")
        );
        assert_eq!(initial(&t, Some("gone/away")), first);
        assert_eq!(initial(&t, None), first);
        assert_eq!(
            initial(
                &Tree {
                    example: false,
                    orgs: vec![]
                },
                None
            ),
            Scope::default()
        );
    }

    #[test]
    fn all_sites_offers_the_union_of_envs_in_first_seen_order() {
        let t = tree();
        let all = Scope {
            org: "platform".into(),
            site: None,
            env: None,
        };
        assert_eq!(envs(&t, &all), vec!["prod", "dev", "local"]);
        let hq = Scope {
            org: "platform".into(),
            site: Some("hq".into()),
            env: None,
        };
        assert_eq!(envs(&t, &hq), vec!["prod", "dev"]);
    }

    #[test]
    fn changing_site_keeps_an_env_only_when_the_new_site_offers_it() {
        let t = tree();
        let picked = Scope {
            org: "platform".into(),
            site: Some("hq".into()),
            env: Some("dev".into()),
        };
        let to_lab = choose_site(&t, &picked, Some("lab"));
        assert_eq!(to_lab.env.as_deref(), Some("dev"), "lab also runs dev");
        let to_all = choose_site(
            &t,
            &Scope {
                env: Some("prod".into()),
                ..picked.clone()
            },
            None,
        );
        assert_eq!(to_all.env.as_deref(), Some("prod"));
        let dropped = choose_site(
            &t,
            &Scope {
                env: Some("prod".into()),
                ..picked
            },
            Some("lab"),
        );
        assert_eq!(dropped.env, None, "lab does not run prod");
    }

    #[test]
    fn choosing_an_org_resets_site_and_env() {
        let next = choose_org("services");
        assert_eq!(
            next,
            Scope {
                org: "services".into(),
                site: None,
                env: None
            }
        );
    }

    #[test]
    fn sites_lists_the_selected_org_only() {
        let t = tree();
        assert_eq!(
            sites(&t, "platform"),
            vec![
                ("hq".into(), "site-hq".into()),
                ("lab".into(), "lab".into())
            ]
        );
        assert!(sites(&t, "services").is_empty());
    }

    #[test]
    fn default_text_maps_none_to_null() {
        assert_eq!(default_text(""), None);
        assert_eq!(default_text("platform/hq"), Some("platform/hq".into()));
    }

    #[test]
    fn default_choices_lists_every_level_after_none() {
        let t = tree();
        let values: Vec<String> = default_choices(Some(&t), "")
            .into_iter()
            .map(|c| c.value)
            .collect();
        assert_eq!(
            values,
            vec![
                "",
                "platform",
                "platform/hq",
                "platform/hq/prod",
                "platform/hq/dev",
                "platform/lab",
                "platform/lab/dev",
                "platform/lab/local",
                "services",
            ]
        );
    }

    #[test]
    fn default_choices_marks_a_value_missing_from_the_tree() {
        let t = tree();
        let choices = default_choices(Some(&t), "other/site");
        let last = choices.last().expect("at least None");
        assert_eq!(last.value, "other/site");
        assert_eq!(last.label, "other/site (not in this tenant)");
        let known = default_choices(Some(&t), "platform/hq");
        assert_eq!(known.iter().filter(|c| c.value == "platform/hq").count(), 1);
    }

    #[test]
    fn default_choices_without_a_tree_is_none_plus_the_draft() {
        assert_eq!(default_choices(None, "").len(), 1);
        assert_eq!(default_choices(None, "platform").len(), 2);
    }
}
