//! Lane selection (plan 24, D3): the nextest filterset a lane runs, from the paths a change touches.
//!
//! The rules are in `platform/tests/areas.yml`, read with this crate's YAML loader. Each changed
//! path is classified in this order: it matches `full` (the change selects everything); else it
//! matches the paths of one or more areas (those areas are selected); else it matches `ignore`
//! (nothing is selected); else it is unclassified (the push and CI lanes run everything).
//!
//! Selection is by module name: nextest's `test(/^<module>::/)` matches the tests of each
//! selected module, and `suite/main.rs` declares the modules.

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use globset::{GlobBuilder, GlobSet, GlobSetBuilder};

use crate::yaml::{self, Node};

/// A test tier. `static` tests parse repository files; the others start processes.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Tier {
    Static,
    Unit,
    Cli,
    Http,
    Playbook,
}

impl Tier {
    fn parse(name: &str) -> Option<Tier> {
        match name {
            "static" => Some(Tier::Static),
            "unit" => Some(Tier::Unit),
            "cli" => Some(Tier::Cli),
            "http" => Some(Tier::Http),
            "playbook" => Some(Tier::Playbook),
            _ => None,
        }
    }
}

/// The module names of each tier.
type Tiers = BTreeMap<Tier, BTreeSet<String>>;

struct Area {
    paths: GlobSet,
    tiers: Tiers,
}

/// The parsed `areas.yml`.
pub struct Config {
    full: GlobSet,
    ignore: GlobSet,
    always: Tiers,
    areas: BTreeMap<String, Area>,
}

impl Config {
    /// The configuration in the file at `path`. A file that cannot be read, or that is not
    /// one YAML document of the expected shape, is an error.
    pub fn load(path: &Path) -> Result<Config, String> {
        let text = std::fs::read_to_string(path).map_err(|e| format!("{}: {e}", path.display()))?;
        Config::parse(&text, &path.display().to_string())
    }

    /// The configuration in `text`, named `origin` in errors. A YAML syntax error panics, as
    /// every test helper's parser does.
    pub fn parse(text: &str, origin: &str) -> Result<Config, String> {
        let docs = yaml::parse(text, origin);
        let [doc] = docs.as_slice() else {
            return Err(format!(
                "{origin}: expected one YAML document, found {}",
                docs.len()
            ));
        };
        from_node(doc, origin)
    }

    /// Every module named in a tier list, once for each list that names it. The repository
    /// test reads this to check that each suite module is listed exactly once.
    pub fn listed_modules(&self) -> Vec<String> {
        std::iter::once(&self.always)
            .chain(self.areas.values().map(|area| &area.tiers))
            .flat_map(|tiers| tiers.values().flat_map(|names| names.iter().cloned()))
            .collect()
    }
}

/// The lanes (plan 24, D3). `Full` runs everything; the others select by the changed paths.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Lane {
    Commit,
    Push,
    Ci,
    Full,
}

impl Lane {
    pub fn parse(name: &str) -> Option<Lane> {
        match name {
            "commit" => Some(Lane::Commit),
            "push" => Some(Lane::Push),
            "ci" => Some(Lane::Ci),
            "full" => Some(Lane::Full),
            _ => None,
        }
    }
}

/// What a lane runs: every test, or the tests of the named modules (none when the set is empty).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Selection {
    All,
    Modules(BTreeSet<String>),
}

impl Selection {
    /// The nextest filterset for this selection.
    pub fn filterset(&self) -> String {
        match self {
            Selection::All => "all()".to_owned(),
            Selection::Modules(names) if names.is_empty() => "none()".to_owned(),
            Selection::Modules(names) => {
                let alternatives: Vec<&str> = names.iter().map(String::as_str).collect();
                format!("test(/^(?:{})::/)", alternatives.join("|"))
            }
        }
    }
}

/// The selection of `lane` for a change that touches `changed` (repository-relative paths).
///
/// - `full` runs everything.
/// - `commit` runs the static tier of the selected areas and of `always`. A `full` hit runs the
///   static tier of every area; an unclassified path adds nothing.
/// - `push` and `ci` run every tier of the selected areas and of `always`. A `full` hit or an
///   unclassified path runs everything.
pub fn select(config: &Config, lane: Lane, changed: &[String]) -> Selection {
    let mut full_hit = false;
    let mut unclassified = false;
    let mut selected: Vec<&Area> = Vec::new();
    for path in changed {
        if config.full.is_match(path) {
            full_hit = true;
            continue;
        }
        let mut hits = config
            .areas
            .values()
            .filter(|area| area.paths.is_match(path))
            .peekable();
        if hits.peek().is_some() {
            selected.extend(hits);
        } else if !config.ignore.is_match(path) {
            unclassified = true;
        }
    }

    match lane {
        Lane::Full => Selection::All,
        Lane::Commit => {
            let scope: Vec<&Area> = if full_hit {
                config.areas.values().collect()
            } else {
                selected
            };
            let mut modules = BTreeSet::new();
            for area in scope {
                add_tiers(&mut modules, &area.tiers, true);
            }
            add_tiers(&mut modules, &config.always, true);
            Selection::Modules(modules)
        }
        Lane::Push | Lane::Ci => {
            if full_hit || unclassified {
                return Selection::All;
            }
            let mut modules = BTreeSet::new();
            for area in selected {
                add_tiers(&mut modules, &area.tiers, false);
            }
            add_tiers(&mut modules, &config.always, false);
            Selection::Modules(modules)
        }
    }
}

/// The modules of `tiers`; with `static_only`, those of the static tier alone.
fn add_tiers(modules: &mut BTreeSet<String>, tiers: &Tiers, static_only: bool) {
    for (tier, names) in tiers {
        if !static_only || *tier == Tier::Static {
            modules.extend(names.iter().cloned());
        }
    }
}

fn from_node(doc: &Node, origin: &str) -> Result<Config, String> {
    let [full, ignore, always, areas] = section(
        doc,
        "the file",
        ["full", "ignore", "always", "areas"],
        origin,
    )?;

    let area_entries = areas
        .as_map()
        .ok_or_else(|| format!("{origin}: `areas` must map area names to their bodies"))?;
    let mut parsed = BTreeMap::new();
    for (key, body) in area_entries {
        let name = key
            .as_str()
            .ok_or_else(|| format!("{origin}: area name {key:?} is not a string"))?;
        let name = module_name(name, "area name", origin)?;
        let [paths, tiers] = section(body, &format!("area `{name}`"), ["paths", "tiers"], origin)?;
        parsed.insert(
            name.clone(),
            Area {
                paths: glob_set(&string_list(paths, &name, origin)?, &name, origin)?,
                tiers: tier_map(tiers, &name, origin)?,
            },
        );
    }

    Ok(Config {
        full: glob_set(&string_list(full, "full", origin)?, "full", origin)?,
        ignore: glob_set(&string_list(ignore, "ignore", origin)?, "ignore", origin)?,
        always: tier_map(always, "always", origin)?,
        areas: parsed,
    })
}

/// The value of each key of `node`, in the order of `keys`. Every key is required and no
/// other is allowed: a misspelt key would otherwise select nothing, without a word.
fn section<'a, const N: usize>(
    node: &'a Node,
    what: &str,
    keys: [&str; N],
    origin: &str,
) -> Result<[&'a Node; N], String> {
    let entries = node
        .as_map()
        .ok_or_else(|| format!("{origin}: {what} must be a mapping"))?;
    for (key, _) in entries {
        if !key.as_str().is_some_and(|k| keys.contains(&k)) {
            return Err(format!(
                "{origin}: {what} has the unknown key {key:?} (expected {keys:?})"
            ));
        }
    }
    let mut values = [node; N];
    for (value, key) in values.iter_mut().zip(keys) {
        *value = node
            .get(key)
            .ok_or_else(|| format!("{origin}: {what} is missing `{key}`"))?;
    }
    Ok(values)
}

/// A module or area name: letters, digits and underscores, never empty. The name becomes a
/// regular expression in the filterset, so nothing else is accepted.
fn module_name(name: &str, what: &str, origin: &str) -> Result<String, String> {
    if !name.is_empty() && name.chars().all(|c| c.is_ascii_alphanumeric() || c == '_') {
        Ok(name.to_owned())
    } else {
        Err(format!(
            "{origin}: {what} `{name}` is not a module name (letters, digits and underscores)"
        ))
    }
}

fn string_list(node: &Node, what: &str, origin: &str) -> Result<Vec<String>, String> {
    let items = node
        .as_seq()
        .ok_or_else(|| format!("{origin}: `{what}` must be a list"))?;
    items
        .iter()
        .map(|item| {
            item.as_str()
                .map(str::to_owned)
                .ok_or_else(|| format!("{origin}: `{what}` holds the non-string {item:?}"))
        })
        .collect()
}

/// Each tier name maps to a list of module names.
fn tier_map(node: &Node, what: &str, origin: &str) -> Result<Tiers, String> {
    let entries = node
        .as_map()
        .ok_or_else(|| format!("{origin}: `{what}` must map tier names to module lists"))?;
    let mut tiers = Tiers::new();
    for (key, modules) in entries {
        let tier = key
            .as_str()
            .and_then(Tier::parse)
            .ok_or_else(|| format!("{origin}: `{what}` names the unknown tier {key:?}"))?;
        let names = string_list(modules, what, origin)?
            .iter()
            .map(|name| module_name(name, what, origin))
            .collect::<Result<BTreeSet<String>, String>>()?;
        tiers.entry(tier).or_default().extend(names);
    }
    Ok(tiers)
}

/// Compiled globs. A `*` does not cross a `/` and `**` does: `literal_separator` is what
/// makes `platform/lib/*.sh` select `platform/lib/x.sh` and not `platform/lib/nested/x.sh`.
fn glob_set(patterns: &[String], what: &str, origin: &str) -> Result<GlobSet, String> {
    let mut builder = GlobSetBuilder::new();
    for pattern in patterns {
        let glob = GlobBuilder::new(pattern)
            .literal_separator(true)
            .build()
            .map_err(|e| format!("{origin}: `{what}` glob `{pattern}`: {e}"))?;
        builder.add(glob);
    }
    builder
        .build()
        .map_err(|e| format!("{origin}: `{what}`: {e}"))
}
