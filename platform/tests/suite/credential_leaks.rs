//! Repository-wide credential leak checks (static tier, security set).
//!
//! What it guards. No committed file holds a private address (RFC 1918: 10/8, 172.16/12 and
//! 192.168/16), a literal password or API token, or a secret file (`.env`, a private key, a
//! `.secret`). `.gitignore` covers the secret shapes. Jinja2 templates draw on approved
//! namespaces. A secret-writing playbook takes its environment secret before an extra var, and
//! no template tells an operator to pass a secret with `-e`: Semaphore keeps every extra var it
//! is given. Every play that reaches OpenBao includes the transport guard, and the guard's
//! pattern accepts internal endpoints and refuses public ones, because the AppRole secret_id and
//! the client token cross that connection. No bytecode is tracked. The bao-merge-keys create is
//! CAS-guarded, and the seed's read-only access check ends the play before any write.
//! docs/MISTAKES.md 1.1, 2.1, 2.18 and 5.1 name these checks as their enforcement.
//!
//! Two BATS behaviours are kept as they were, and one is corrected:
//! - Kept: the Jinja scan reads the BATS bracket `[\s]`, which is a backslash or an `s`, not
//!   whitespace. So it matches `{{name` and not `{{ name`. The intended `\s*` reports 412 names
//!   in 58 templates on 2026-11-12, so the fix is a separate change. test_launch_gate_allowlist.py
//!   records the same gap.
//! - Kept: the BATS secret scans excluded comment lines with `^\s*#`, but grep -n puts `N:` in
//!   front of every line, so that exclusion never applied. Comment lines are scanned.
//! - Corrected: the BATS `["\x27]` is a bracket of `"`, `\`, `x`, `2` and `7`, not a quote. The
//!   port reads an optional quote, `["']`. It adds no hits on the tree (checked 2026-11-12).
//!
//! Replaces platform/tests/test_credential_leaks.bats (plan 24, Phase 4).
//!
//! Port map
//!
//! | BATS @test | Rust fn |
//! |---|---|
//! | credential leak: no hardcoded 192.168.x.x IPs in committed files | `committed_files_hold_no_private_address` (case `192.168`) |
//! | credential leak: no hardcoded 10.x.x.x IPs in committed files | `committed_files_hold_no_private_address` (case `10`) |
//! | credential leak: no hardcoded 172.16-31.x.x IPs in committed files | `committed_files_hold_no_private_address` (case `172.16-31`) |
//! | private address scan exempts only exact RFC1918 definition tokens | `scanner_exempts_only_exact_rfc1918_definition_tokens` |
//! | private address scan inspects symlink blobs without opening external targets | `scanner_reads_a_symlink_as_its_target_text` |
//! | private address scan propagates index read failures | `scanner_refuses_a_path_missing_from_the_index` |
//! | credential leak: no hardcoded passwords in committed files | `committed_files_hold_no_literal_password` |
//! | credential leak: no hardcoded API keys or tokens in committed files | `committed_files_hold_no_literal_api_key_or_token` |
//! | credential leak: Jinja2 templates use approved variable namespaces | `jinja_templates_draw_on_approved_namespaces` |
//! | credential leak: .gitignore covers secrets/ directory | `gitignore_covers_each_secret_shape` (case `secrets/`) (candidate: pins text) |
//! | credential leak: .gitignore covers *.secret files | `gitignore_covers_each_secret_shape` (case `*.secret`) (candidate: pins text) |
//! | credential leak: .gitignore covers *.key files | `gitignore_covers_each_secret_shape` (case `*.key`) (candidate: pins text) |
//! | credential leak: .gitignore covers *.pem files | `gitignore_covers_each_secret_shape` (case `*.pem`) (candidate: pins text) |
//! | credential leak: .gitignore covers runtime .env files | `gitignore_covers_each_secret_shape` (case `.env`) (candidate: pins text) |
//! | credential leak: no .env files tracked by git (only .env.example) | `no_secret_file_is_tracked` (case `.env`) |
//! | credential leak: no private key files tracked by git | `no_secret_file_is_tracked` (case `private key`) |
//! | credential leak: no .secret files tracked by git | `no_secret_file_is_tracked` (case `.secret`) |
//! | secret-writing playbooks prefer an environment secret over an extra var | `secret_playbooks_take_the_environment_before_an_extra_var` (candidate: pins text) |
//! | seed-openbao-key uses the resolved value everywhere, not the raw extra var | `seed_openbao_key_merges_the_resolved_value_only` (candidate: pins text) |
//! | no Semaphore template still instructs operators to pass a secret as -e | `no_template_asks_for_a_secret_as_an_extra_var` (candidate: pins text) |
//! | NetBox token bootstrap guards AppRole transport before login | `netbox_token_bootstrap_guards_transport_before_login` |
//! | every play that resolves an OpenBao URL includes the transport guard | `every_openbao_play_includes_the_transport_guard` |
//! | the transport pattern accepts internal endpoints and refuses public ones | `transport_pattern_accepts_internal_endpoints_and_refuses_public_ones` |
//! | repo: no generated Python bytecode is tracked | `no_python_bytecode_is_tracked` (its two .gitignore lines: candidate: pins text) |
//! | bao-merge-keys: creation is CAS-guarded, and losing the race falls back to a merge | `bao_merge_keys_creates_a_path_only_under_cas` |
//! | seed-openbao-key: the read-only access check ends the play before any write | `seed_access_check_ends_the_play_before_any_write` |

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fs;
use std::net::Ipv4Addr;
use std::os::unix::fs::symlink;
use std::path::{Component, Path, PathBuf};
use std::rc::Rc;

use agent_cloud_tests::process;
use agent_cloud_tests::repo;
use agent_cloud_tests::yaml::{self, Node};
use regex::{Regex, bytes};
use tempfile::TempDir;

const GUARD: &str = "platform/playbooks/tasks/assert-bao-transport.yml";
const GUARD_REL: &str = "tasks/assert-bao-transport.yml";
const PLAYBOOKS: &str = "platform/playbooks";
const NETBOX_TOKEN: &str = "platform/playbooks/provision-netbox-automation-token.yml";
const SEED_KEY: &str = "platform/playbooks/seed-openbao-key.yml";
const SEED_POSTIZ: &str = "platform/playbooks/seed-postiz-secrets.yml";
const SEED_ACCESS: &str = "platform/playbooks/tasks/assert-bao-seed-access.yml";
const BAO_MERGE: &str = "platform/playbooks/tasks/bao-merge-keys.yml";
const TEMPLATES: &str = "platform/semaphore/templates.yml";
const RATCHET: &str = "platform/tests/known_unguarded_bao_plays.txt";
const INCLUDE_KEYS: [&str; 4] = [
    "ansible.builtin.import_tasks",
    "ansible.builtin.include_tasks",
    "import_tasks",
    "include_tasks",
];
/// The include keys the BATS line matches named (`include_tasks:`), not `import_tasks:`.
const INCLUDE_TASKS: [&str; 2] = ["ansible.builtin.include_tasks", "include_tasks"];

// ── Helpers ───────────────────────────────────────────────────────────────────

fn re(pattern: &str) -> Regex {
    Regex::new(pattern).unwrap_or_else(|e| panic!("bad pattern {pattern:?}: {e}"))
}

fn read(rel: &str) -> String {
    fs::read_to_string(repo::path(rel)).unwrap_or_else(|e| panic!("{rel}: {e}"))
}

fn load(rel: &str) -> Node {
    yaml::load_one(&repo::path(rel)).unwrap_or_else(|| panic!("{rel}: empty YAML document"))
}

fn items_of(node: Option<&Node>) -> Vec<Node> {
    node.and_then(Node::as_seq)
        .map(<[Node]>::to_vec)
        .unwrap_or_default()
}

/// `item` and then its `block`, `rescue` and `always` children, depth first.
fn flatten(items: &[Node], out: &mut Vec<Node>) {
    for item in items {
        out.push(item.clone());
        for section in ["block", "rescue", "always"] {
            if let Some(children) = item.get(section).and_then(Node::as_seq) {
                flatten(children, out);
            }
        }
    }
}

/// Every task of every play in a playbook, in file order (a play's `tasks`, flattened).
fn play_tasks(rel: &str) -> Vec<Node> {
    let mut out = Vec::new();
    for play in items_of(Some(&load(rel))) {
        flatten(&items_of(play.get("tasks")), &mut out);
    }
    out
}

/// The first task whose `name` starts with `prefix`, as `block_task` in the BATS helper did.
fn task_named<'a>(tasks: &'a [Node], prefix: &str) -> &'a Node {
    tasks
        .iter()
        .find(|t| {
            t.get("name")
                .and_then(Node::as_str)
                .is_some_and(|n| n.starts_with(prefix))
        })
        .unwrap_or_else(|| panic!("no task named {prefix:?}"))
}

/// Every mapping in `node`, parents before children and keys in file order: the document order
/// `grep -n` gave the BATS line matches.
fn maps_in_order(node: &Node) -> Vec<&Node> {
    fn collect<'a>(node: &'a Node, out: &mut Vec<&'a Node>) {
        match node {
            Node::Map(entries) => {
                out.push(node);
                for (_, value) in entries {
                    collect(value, out);
                }
            }
            Node::Seq(items) => {
                for item in items {
                    collect(item, out);
                }
            }
            _ => {}
        }
    }
    let mut out = Vec::new();
    collect(node, &mut out);
    out
}

/// Every string held under `key` anywhere in `file`, as a line `key: value` would show.
fn values_for_key(file: &str, key: &str) -> Vec<String> {
    let doc = load(file);
    maps_in_order(&doc)
        .into_iter()
        .filter_map(|m| m.get(key).and_then(Node::as_str).map(str::to_owned))
        .collect()
}

/// Every item of every `that:` list in `file`.
fn that_items(file: &str) -> Vec<String> {
    let doc = load(file);
    maps_in_order(&doc)
        .into_iter()
        .flat_map(|m| match m.get("that") {
            Some(Node::Seq(items)) => items
                .iter()
                .filter_map(Node::as_str)
                .map(str::to_owned)
                .collect::<Vec<String>>(),
            Some(Node::Str(s)) => vec![s.clone()],
            _ => Vec::new(),
        })
        .collect()
}

/// The `when:` of every task in `file`, each item of a list on its own.
fn when_items(file: &str) -> Vec<String> {
    let doc = load(file);
    maps_in_order(&doc)
        .into_iter()
        .flat_map(|m| match m.get("when") {
            Some(Node::Seq(items)) => items
                .iter()
                .filter_map(Node::as_str)
                .map(str::to_owned)
                .collect::<Vec<String>>(),
            Some(Node::Str(s)) => vec![s.clone()],
            _ => Vec::new(),
        })
        .collect()
}

/// The file a task includes, for `include_tasks: x` and the mapping form `{file: x, ...}`.
fn include_target<'a>(map: &'a Node, keys: &[&str]) -> Option<&'a str> {
    keys.iter().find_map(|key| {
        let value = map.get(key)?;
        match value {
            Node::Map(_) => value.get("file").and_then(Node::as_str),
            other => other.as_str(),
        }
    })
}

fn position(maps: &[&Node], pred: impl Fn(&Node) -> bool) -> Option<usize> {
    maps.iter().position(|m| pred(m))
}

fn set_key(node: &mut Node, key: &str, value: Node) {
    let Node::Map(entries) = node else {
        panic!("set_key on a node that is not a mapping");
    };
    match entries.iter_mut().find(|(k, _)| k.as_str() == Some(key)) {
        Some(slot) => slot.1 = value,
        None => entries.push((Node::Str(key.to_owned()), value)),
    }
}

fn mapping(pairs: &[(&str, Node)]) -> Node {
    Node::Map(
        pairs
            .iter()
            .map(|(k, v)| (Node::Str((*k).to_owned()), v.clone()))
            .collect(),
    )
}

/// `codecs.decode(s, 'unicode_escape')` for the escapes these patterns can hold. An unknown
/// escape, such as `\.` or `\[`, stays as written, as Python keeps it.
fn unicode_escape(s: &str) -> String {
    let mut out = String::new();
    let mut chars = s.chars().peekable();
    while let Some(c) = chars.next() {
        if c != '\\' {
            out.push(c);
            continue;
        }
        match chars.peek().copied() {
            Some('\\') => out.push(chars.next().unwrap_or('\\')),
            Some('\'') => out.push(chars.next().unwrap_or('\'')),
            Some('"') => out.push(chars.next().unwrap_or('"')),
            Some('n') => {
                chars.next();
                out.push('\n');
            }
            Some('t') => {
                chars.next();
                out.push('\t');
            }
            Some('r') => {
                chars.next();
                out.push('\r');
            }
            Some('x') => {
                chars.next();
                let hex: String = chars.by_ref().take(2).collect();
                let byte = u8::from_str_radix(&hex, 16)
                    .unwrap_or_else(|_| panic!("bad \\x escape in {s:?}"));
                out.push(char::from(byte));
            }
            _ => out.push('\\'),
        }
    }
    out
}

/// The path, with `..` and `.` folded and symbolic links followed where the path exists, as
/// Python's `Path.resolve()` gives it.
fn resolve(path: &Path) -> PathBuf {
    fs::canonicalize(path).unwrap_or_else(|_| {
        let mut out = PathBuf::new();
        for component in path.components() {
            match component {
                Component::ParentDir => {
                    out.pop();
                }
                Component::CurDir => {}
                other => out.push(other.as_os_str()),
            }
        }
        out
    })
}

/// Tracked files minus vendored, documentation, test and example paths: `_committed_files`.
/// This module is left out. It spells out the addresses and credential shapes it looks for.
fn committed_files() -> Vec<String> {
    let excluded = [
        re(r"^platform/services/netbox/deployment/netbox-docker/"),
        re(r"\.gitkeep$"),
        re(r"\.png$"),
        re(r"\.svg$"),
        re(r"\.ico$"),
        re(r"^platform/tests/suite/credential_leaks\.rs$"),
        re(r"\.md$"),
        re(r"\.MD$"),
        re(r"^plan/"),
        re(r"^docs/"),
        re(r"test_.*\.py$"),
        re(r"test_.*\.bats$"),
        re(r"conftest\.py$"),
        re(r"\.example$"),
        re(r"\.j2$"),
    ];
    repo::ls_files(&[])
        .into_iter()
        .filter(|f| !excluded.iter().any(|x| x.is_match(f)))
        .collect()
}

/// Each line of each file that matches `pattern` and holds none of `excluded`, as
/// `file:line:text`. A tracked path that is not a regular file is skipped, as `[ -f ]` skipped it.
fn line_hits(files: &[String], pattern: &Regex, excluded: &[&str]) -> Vec<String> {
    let mut hits = Vec::new();
    for file in files {
        let path = repo::path(file);
        if !path.is_file() {
            continue;
        }
        let bytes = fs::read(&path).unwrap_or_else(|e| panic!("{file}: {e}"));
        let text = String::from_utf8_lossy(&bytes);
        for (index, line) in text.split('\n').enumerate() {
            if pattern.is_match(line) && !excluded.iter().any(|x| line.contains(*x)) {
                hits.push(format!("{file}:{}:{line}", index + 1));
            }
        }
    }
    hits
}

/// `git` in `dir` with a scrubbed environment, its standard input `input`. Ok(stdout) on a
/// zero exit status.
fn run_git(dir: &Path, home: &Path, args: &[&str], input: &[u8]) -> Result<Vec<u8>, ()> {
    let out = process::scrubbed("git", home)
        .current_dir(dir)
        .args(args)
        .write_stdin(input.to_vec())
        .output()
        .map_err(|_| ())?;
    if out.status.success() {
        Ok(out.stdout)
    } else {
        Err(())
    }
}

fn init_repo(dir: &Path, home: &Path, files: &[&str]) {
    assert!(
        run_git(dir, home, &["init", "-q"], b"").is_ok(),
        "git init failed"
    );
    if files.is_empty() {
        return;
    }
    let mut args = vec!["add", "--"];
    args.extend_from_slice(files);
    assert!(run_git(dir, home, &args, b"").is_ok(), "git add failed");
}

// ── The private address scan ─────────────────────────────────────────────────

/// One RFC 1918 range: its network, prefix length, the leading octets an address must start
/// with, and for 172 the second-octet range the BATS scan required (16 to 31).
struct Range {
    network: Ipv4Addr,
    bits: u8,
    prefix: &'static [u32],
    second_octet: Option<std::ops::RangeInclusive<u32>>,
}

impl Range {
    fn of(family: &str) -> Range {
        match family {
            "10" => Range {
                network: Ipv4Addr::new(10, 0, 0, 0),
                bits: 8,
                prefix: &[10],
                second_octet: None,
            },
            "172" => Range {
                network: Ipv4Addr::new(172, 16, 0, 0),
                bits: 12,
                prefix: &[172],
                second_octet: Some(16..=31),
            },
            "192.168" => Range {
                network: Ipv4Addr::new(192, 168, 0, 0),
                bits: 16,
                prefix: &[192, 168],
                second_octet: None,
            },
            other => panic!("no RFC 1918 range named {other:?}"),
        }
    }

    fn cidr(&self) -> String {
        format!("{}/{}", self.network, self.bits)
    }
}

#[derive(Debug, PartialEq, Eq)]
struct Finding {
    path: String,
    line: usize,
    address: String,
}

fn render(findings: &[Finding]) -> String {
    findings
        .iter()
        .map(|f| format!("{}:{}:{}", f.path, f.line, f.address))
        .collect::<Vec<_>>()
        .join("\n")
}

/// `splitlines` for bytes: `\n`, `\r` and `\r\n` each end a line.
fn splitlines(content: &[u8]) -> Vec<&[u8]> {
    let mut lines = Vec::new();
    let mut start = 0;
    let mut i = 0;
    while i < content.len() {
        match content[i] {
            b'\n' => {
                lines.push(&content[start..i]);
                i += 1;
                start = i;
            }
            b'\r' => {
                lines.push(&content[start..i]);
                i += if content.get(i + 1) == Some(&b'\n') {
                    2
                } else {
                    1
                };
                start = i;
            }
            _ => i += 1,
        }
    }
    if start < content.len() {
        lines.push(&content[start..]);
    }
    lines
}

/// The index entries of `paths` under `root`, read as `git ls-files --stage` reads them: the
/// blobs as stored, so a symbolic link is its target text and is never followed.
fn index_blobs(
    root: &Path,
    home: &Path,
    paths: &[String],
) -> Result<Vec<(String, Vec<u8>)>, String> {
    const MALFORMED: &str = "private-address scan found malformed or ambiguous Git index entries";
    let listing = run_git(root, home, &["ls-files", "--stage", "-z"], b"")
        .map_err(|()| "private-address scan could not read Git index".to_owned())?;
    let selected: BTreeSet<&str> = paths.iter().map(String::as_str).collect();
    let mut entries: BTreeMap<String, (String, String)> = BTreeMap::new();
    for record in listing.split(|b| *b == 0).filter(|r| !r.is_empty()) {
        let tab = record.iter().position(|b| *b == b'\t').ok_or(MALFORMED)?;
        let metadata = std::str::from_utf8(&record[..tab]).map_err(|_| MALFORMED)?;
        let fields: Vec<&str> = metadata.split_ascii_whitespace().collect();
        let [mode, object_id, stage] = fields.as_slice() else {
            return Err(MALFORMED.to_owned());
        };
        let path = String::from_utf8_lossy(&record[tab + 1..]).into_owned();
        if !selected.contains(path.as_str()) {
            continue;
        }
        if entries.contains_key(&path)
            || *stage != "0"
            || !matches!(*mode, "100644" | "100755" | "120000")
        {
            return Err(MALFORMED.to_owned());
        }
        entries.insert(path, ((*mode).to_owned(), (*object_id).to_owned()));
    }
    if entries.keys().map(String::as_str).collect::<BTreeSet<_>>() != selected {
        return Err("private-address scan found missing Git index entries".to_owned());
    }
    let ordered: Vec<(&str, &str)> = paths
        .iter()
        .map(|p| (p.as_str(), entries[p].1.as_str()))
        .collect();
    let input: Vec<u8> = ordered
        .iter()
        .flat_map(|(_, oid)| format!("{oid}\n").into_bytes())
        .collect();
    let blobs = run_git(root, home, &["cat-file", "--batch"], &input)
        .map_err(|()| "private-address scan could not read Git blobs".to_owned())?;
    let mut out = Vec::new();
    let mut offset = 0;
    for (path, oid) in &ordered {
        let header_end = blobs[offset..]
            .iter()
            .position(|b| *b == b'\n')
            .map(|p| p + offset)
            .ok_or("private-address scan received a truncated Git blob header")?;
        let header = std::str::from_utf8(&blobs[offset..header_end]).unwrap_or("");
        let fields: Vec<&str> = header.split_ascii_whitespace().collect();
        let [got_oid, kind, size] = fields.as_slice() else {
            return Err("private-address scan received an invalid Git blob header".to_owned());
        };
        if *got_oid != *oid || *kind != "blob" {
            return Err("private-address scan received an invalid Git blob header".to_owned());
        }
        let size: usize = size
            .parse()
            .map_err(|_| "private-address scan received an invalid Git blob size".to_owned())?;
        let body_start = header_end + 1;
        let body_end = body_start + size;
        if body_end >= blobs.len() || blobs[body_end] != b'\n' {
            return Err("private-address scan received a truncated Git blob".to_owned());
        }
        out.push(((*path).to_owned(), blobs[body_start..body_end].to_vec()));
        offset = body_end + 1;
    }
    if offset != blobs.len() {
        return Err("private-address scan received trailing Git blob data".to_owned());
    }
    Ok(out)
}

/// The addresses of one RFC 1918 range in `paths`, outside the exact network token of a
/// `ipaddress.ip_network("<cidr>")` definition. Each is reported with its line.
fn private_address_findings(
    family: &str,
    root: &Path,
    paths: &[String],
) -> Result<Vec<Finding>, String> {
    let home = TempDir::new().expect("temporary home");
    if paths.is_empty()
        || paths.iter().any(String::is_empty)
        || paths.iter().collect::<BTreeSet<_>>().len() != paths.len()
    {
        return Err("private-address scan received an empty or ambiguous path list".to_owned());
    }
    let range = Range::of(family);
    let definition = bytes::Regex::new(&format!(
        r#"ipaddress\.ip_network\([[:space:]]*['"](?P<network>{})['"][[:space:]]*\)"#,
        regex::escape(&range.cidr())
    ))
    .expect("definition pattern");
    let address = bytes::Regex::new(r"(?-u:\b)(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?-u:\b)")
        .expect("address pattern");
    let network_len = range.network.to_string().len();

    let mut findings = Vec::new();
    for (path, content) in index_blobs(root, home.path(), paths)? {
        for (index, line) in splitlines(&content).into_iter().enumerate() {
            let spans: Vec<(usize, usize)> = definition
                .captures_iter(line)
                .filter_map(|c| c.name("network").map(|m| (m.start(), m.end())))
                .collect();
            for found in address.find_iter(line) {
                let text = std::str::from_utf8(found.as_bytes()).unwrap_or("");
                let octets: Vec<u32> = text.split('.').filter_map(|p| p.parse().ok()).collect();
                if octets.len() != 4 || octets[..range.prefix.len()] != *range.prefix {
                    continue;
                }
                if let Some(second) = &range.second_octet
                    && !second.contains(&octets[1])
                {
                    continue;
                }
                let exempt = spans.iter().any(|&(start, _)| {
                    start == found.start() && found.end() == start + network_len
                });
                if !exempt {
                    findings.push(Finding {
                        path: path.clone(),
                        line: index + 1,
                        address: text.to_owned(),
                    });
                }
            }
        }
    }
    Ok(findings)
}

// ── Secrets in the committed text ────────────────────────────────────────────

#[test]
fn committed_files_hold_no_private_address() {
    // Arrange
    let paths = committed_files();
    assert!(!paths.is_empty(), "the committed-file list is empty");

    for (case, family) in [("192.168", "192.168"), ("10", "10"), ("172.16-31", "172")] {
        // Act
        let result = private_address_findings(family, repo::root(), &paths);

        // Assert
        let findings = result.unwrap_or_else(|e| panic!("{case}: {e}"));
        assert!(
            findings.is_empty(),
            "{case}: found hardcoded private addresses:\n{}",
            render(&findings)
        );
    }
}

#[test]
fn scanner_exempts_only_exact_rfc1918_definition_tokens() {
    // Arrange: a fixture repository whose lines hold each address form the scan must tell apart.
    // Hosts are built from the range's network, so no literal address sits in this file.
    let dir = TempDir::new().expect("fixture dir");
    let home = TempDir::new().expect("fixture home");
    let host = |base: Ipv4Addr, offset: u32| Ipv4Addr::from(u32::from(base) + offset).to_string();
    let n10 = Ipv4Addr::new(10, 0, 0, 0);
    let n172 = Ipv4Addr::new(172, 16, 0, 0);
    let n192 = Ipv4Addr::new(192, 168, 0, 0);
    let c10 = format!("{n10}/8");
    let c172 = format!("{n172}/12");
    let c192 = format!("{n192}/16");
    let padded: Vec<String> = host(n10, 23)
        .split('.')
        .skip(1)
        .map(|p| format!("{:03}", p.parse::<u32>().unwrap_or(0)))
        .collect();
    let lines = [
        format!(r#"ipaddress.ip_network("{c10}")  # host {}"#, host(n10, 23)),
        format!(
            r#"ipaddress.ip_network("{c172}")  # host {}"#,
            host(n172, 23)
        ),
        format!(
            r#"ipaddress.ip_network("{c192}")  # host {}"#,
            host(n192, 23)
        ),
        format!(r#"host = "{}""#, host(n10, 99)),
        format!(r#"host = "{}""#, host(n172, 31)),
        format!(r#"host = "{}""#, host(n192, 2)),
        format!(r#"ipaddress.ip_network("{}/8")"#, host(n10, 1)),
        format!(r#"ipaddress.ip_network("{}/12")"#, host(n172, 17)),
        format!(r#"ipaddress.ip_network("{}/16")"#, host(n192, 1)),
        format!(r#"url = "https://{n10}/8""#),
        format!(r#"url = "https://{n172}/12""#),
        format!(r#"url = "https://{n192}/16""#),
        format!(r#"ipaddress.ip_network("{c10}")"#),
        format!(r#"ipaddress.ip_network("{c172}")"#),
        format!(r#"ipaddress.ip_network("{c192}")"#),
        format!(r#"host = "10.{}""#, padded.join(".")),
    ];
    fs::write(
        dir.path().join("rfc1918-definitions.txt"),
        lines.join("\n") + "\n",
    )
    .expect("fixture file");
    init_repo(dir.path(), home.path(), &["rfc1918-definitions.txt"]);
    let paths = vec!["rfc1918-definitions.txt".to_owned()];

    // Act and Assert, one case per range: the flagged lines are exact.
    let cases: [(&str, &[usize]); 3] = [
        ("10", &[1, 4, 7, 10, 16]),
        ("172", &[2, 5, 8, 11]),
        ("192.168", &[3, 6, 9, 12]),
    ];
    for (family, expected) in cases {
        let findings = private_address_findings(family, dir.path(), &paths)
            .unwrap_or_else(|e| panic!("{family}: {e}"));
        let lines: BTreeSet<usize> = findings.iter().map(|f| f.line).collect();
        let want: BTreeSet<usize> = expected.iter().copied().collect();
        assert_eq!(
            lines,
            want,
            "{family}: flagged lines\n{}",
            render(&findings)
        );
    }
}

#[test]
fn scanner_reads_a_symlink_as_its_target_text() {
    // Arrange: one link whose target is outside the repository, one whose target text holds an address.
    let dir = TempDir::new().expect("fixture dir");
    let home = TempDir::new().expect("fixture home");
    let external = dir.path().join("outside");
    fs::create_dir(&external).expect("external dir");
    let host = Ipv4Addr::from(u32::from(Ipv4Addr::new(10, 0, 0, 0)) + 211).to_string();
    fs::write(external.join("private-value.txt"), &host).expect("external value");
    let repo_dir = dir.path().join("repo");
    fs::create_dir(&repo_dir).expect("repo dir");
    symlink(
        external.join("private-value.txt"),
        repo_dir.join("external-link.txt"),
    )
    .expect("external link");
    symlink(
        format!("../unresolved/{host}"),
        repo_dir.join("link-text.txt"),
    )
    .expect("text link");
    init_repo(
        &repo_dir,
        home.path(),
        &["external-link.txt", "link-text.txt"],
    );
    let paths = vec!["external-link.txt".to_owned(), "link-text.txt".to_owned()];

    // Act
    let findings = private_address_findings("10", &repo_dir, &paths).expect("scan");

    // Assert: the link text is scanned, the external target is never opened.
    assert_eq!(
        findings,
        vec![Finding {
            path: "link-text.txt".to_owned(),
            line: 1,
            address: host,
        }]
    );
}

#[test]
fn scanner_refuses_a_path_missing_from_the_index() {
    // Arrange: an empty repository and a path it does not hold.
    let dir = TempDir::new().expect("fixture dir");
    let home = TempDir::new().expect("fixture home");
    init_repo(dir.path(), home.path(), &[]);
    let paths = vec!["missing.txt".to_owned()];

    // Act
    let result = private_address_findings("10", dir.path(), &paths);

    // Assert
    let error = result.expect_err("a path missing from the index must fail the scan");
    assert!(
        error.contains("private-address scan found missing Git index entries"),
        "{error}"
    );
}

#[test]
fn committed_files_hold_no_literal_password() {
    // Arrange: a password assignment with a literal value of eight or more characters.
    let pattern = re(r#"(?i)(password|passwd)\s*[:=]\s*["']?[A-Za-z0-9!@#$%^&*]{8,}"#);
    let excluded = [
        "{{",
        "$",
        "example",
        "REPLACE_ME",
        "changeme",
        "placeholder",
    ];

    // Act
    let hits = line_hits(&committed_files(), &pattern, &excluded);

    // Assert
    assert!(
        hits.is_empty(),
        "Found hardcoded passwords:\n{}",
        hits.join("\n")
    );
}

#[test]
fn committed_files_hold_no_literal_api_key_or_token() {
    // Arrange: a key, token or secret identifier assigned a literal value of twenty or more characters.
    let pattern =
        re(r#"(?i)(api_key|apikey|secret_key|secret_id|token)\s*[:=]\s*["']?[A-Za-z0-9_-]{20,}"#);
    let excluded = [
        "{{",
        "$",
        "example",
        "REPLACE_ME",
        "placeholder",
        "dummyKey",
        "dummy_key",
        "LOCAL_FAKE",
    ];

    // Act
    let hits = line_hits(&committed_files(), &pattern, &excluded);

    // Assert
    assert!(
        hits.is_empty(),
        "Found hardcoded API keys or tokens:\n{}",
        hits.join("\n")
    );
}

#[test]
fn jinja_templates_draw_on_approved_namespaces() {
    // Arrange
    let templates = repo::ls_files(&["platform/services/*.j2"]);
    assert!(
        !templates.is_empty(),
        "no Jinja2 templates found under platform/services"
    );
    let approved = re(
        r"^(secrets|_|ansible_|inventory_hostname|service_|monorepo_|container_|hostvars|groups|item|lookup|netbox_|diode_|openbao_|bao_|discovery_|orb_)",
    );
    // The BATS bracket `[\s]`: a backslash or an `s`. Kept as written (see the module note).
    let bare_name = re(r"\{\{[\\s]*[a-zA-Z][a-zA-Z0-9_.]*");

    // Act
    let mut violations = Vec::new();
    for template in &templates {
        let text = read(template);
        let bare: Vec<&str> = bare_name
            .find_iter(&text)
            .map(|m| m.as_str().trim_start_matches("{{").trim_start())
            .filter(|name| !approved.is_match(name))
            .collect();
        if !bare.is_empty() {
            violations.push(format!("{template}: {}", bare.join(" ")));
        }
    }

    // Assert
    assert!(
        violations.is_empty(),
        "Jinja2 templates with variables outside approved namespaces:\n{}",
        violations.join("\n")
    );
}

// ── Ignore rules and tracked secret files ────────────────────────────────────

#[test]
fn gitignore_covers_each_secret_shape() {
    // Arrange
    let gitignore = read(".gitignore");
    let cases = [
        ("secrets/", r"^[[:space:]]*secrets/"),
        ("*.secret", r"^[[:space:]]*\*\.secret"),
        ("*.key", r"^[[:space:]]*\*\.key"),
        ("*.pem", r"^[[:space:]]*\*\.pem"),
        (".env", r"(\.env|\*\*/config/\*\.env|\*\.env)"),
    ];

    for (case, pattern) in cases {
        // Act
        let covered = re(&format!("(?m){pattern}")).is_match(&gitignore);

        // Assert
        assert!(covered, "{case}: .gitignore does not cover it");
    }
}

#[test]
fn no_secret_file_is_tracked() {
    // Arrange
    let tracked = repo::ls_files(&[]);
    let cases: [(&str, &str, &[&str]); 3] = [
        (".env", r"\.env$", &[r"\.example"]),
        (
            "private key",
            r"\.(key|pem|p12|pfx)$",
            &[r"\.example", r"\.pub$"],
        ),
        (".secret", r"\.secret$", &[]),
    ];

    for (case, shape, except) in cases {
        let shape = re(shape);
        let except: Vec<Regex> = except.iter().map(|e| re(e)).collect();

        // Act
        let found: Vec<&String> = tracked
            .iter()
            .filter(|f| shape.is_match(f) && !except.iter().any(|e| e.is_match(f)))
            .collect();

        // Assert
        assert!(found.is_empty(), "{case} files tracked by git: {found:?}");
    }
}

// ── Secret handling in the playbooks and the Semaphore catalog ───────────────

#[test]
fn secret_playbooks_take_the_environment_before_an_extra_var() {
    // Arrange: the order of the operands is the assertion. The environment lookup comes first.
    let bao_value = values_for_key(SEED_KEY, "_bao_value");
    let env_name = values_for_key(SEED_POSTIZ, "_env_name");
    let supplied = values_for_key(SEED_POSTIZ, "_supplied");
    let seed_env = values_for_key(SEED_POSTIZ, "_seed_env");
    let postiz = read(SEED_POSTIZ);

    // Act and Assert
    assert!(
        bao_value
            .iter()
            .any(|v| re(r"lookup\('env', *'BAO_VALUE'\) *\| *default\(bao_value").is_match(v)),
        "seed-openbao-key: _bao_value does not read BAO_VALUE before the extra var"
    );
    assert!(
        !bao_value
            .iter()
            .any(|v| re(r"bao_value *\| *default\(lookup").is_match(v)),
        "seed-openbao-key: _bao_value reads the extra var before the environment"
    );
    assert!(
        env_name.iter().any(|v| v == "{{ _seed_env[item] }}"),
        "seed-postiz-secrets: _env_name is not the mapped SEED_ name"
    );
    assert!(
        supplied
            .iter()
            .any(|v| v.starts_with("{{ lookup('env', _env_name)")),
        "seed-postiz-secrets: _supplied does not read the environment first"
    );
    assert!(
        seed_env
            .iter()
            .any(|v| v.contains("map('regex_replace', '^', 'SEED_')")),
        "seed-postiz-secrets: _seed_env does not derive the SEED_ names"
    );
    assert!(
        !re(r"lookup\('vars', 'seed_' ~ item[^)]*\) *\| *default\(lookup\('env'").is_match(&postiz),
        "seed-postiz-secrets: the extra var is read before the environment"
    );
}

#[test]
fn seed_openbao_key_merges_the_resolved_value_only() {
    // Arrange: the write, the change check and the validation all use the resolved value.
    let merge_data = values_for_key(SEED_KEY, "_bm_data");
    let stored = values_for_key(SEED_KEY, "_bao_value_stored");
    let shared_set = values_for_key(BAO_MERGE, "_bm_set");
    let when = when_items(BAO_MERGE);
    let that = that_items(SEED_KEY);

    // Act and Assert
    assert!(
        merge_data
            .iter()
            .any(|v| v == "{{ {bao_key: _bao_value_stored} }}"),
        "_bm_data does not merge the resolved value"
    );
    assert!(
        !merge_data.iter().any(|v| v == "{{ {bao_key: bao_value} }}"),
        "_bm_data merges the raw extra var"
    );
    assert!(
        stored
            .iter()
            .any(|v| v.starts_with("{{ _bao_value | b64decode ")),
        "_bao_value_stored is not derived from the resolved _bao_value"
    );
    assert!(
        !stored.iter().any(|v| re(r"[^_]bao_value").is_match(v)),
        "_bao_value_stored reads the raw extra var"
    );
    assert!(
        shared_set
            .iter()
            .any(|v| v == "{{ _bm_data | default({}) }}"),
        "bao-merge-keys: _bm_set is not the caller's _bm_data"
    );
    assert!(
        when.iter()
            .any(|w| w.contains("combine(_bm_set)) != _bm_current")),
        "bao-merge-keys: the change check does not compare the merged dict"
    );
    assert!(
        that.iter().any(|t| t == "_bao_value | length > 0"),
        "the validation gate does not test the resolved value"
    );
    assert!(
        !that.iter().any(|t| t.starts_with("bao_value is defined")),
        "the validation gate tests the raw extra var"
    );
}

#[test]
fn no_template_asks_for_a_secret_as_an_extra_var() {
    // Arrange
    let text = read(TEMPLATES);

    // Act and Assert: the deny-list. A template must not instruct `-e` with a secret.
    for (case, pattern) in [
        (
            "-e bao_value=<secret>",
            r"(?m)^\s*#\s*-e bao_value=<secret>",
        ),
        ("-e seed_<name>=...", r"(?m)^\s*#\s*-e seed_[a-z_]+=\.\.\."),
    ] {
        assert!(
            !re(pattern).is_match(&text),
            "a template still instructs {case}"
        );
    }
    // The replacement channels are named.
    assert!(text.contains("BAO_VALUE"), "BAO_VALUE is not named");
    assert!(
        text.contains("SEED_X_API_KEY"),
        "SEED_X_API_KEY is not named"
    );
}

// ── The OpenBao transport guard ──────────────────────────────────────────────

#[test]
fn netbox_token_bootstrap_guards_transport_before_login() {
    // Arrange
    let doc = load(NETBOX_TOKEN);
    let maps = maps_in_order(&doc);

    // Act
    let guard = position(&maps, |m| {
        include_target(m, &INCLUDE_TASKS) == Some(GUARD_REL)
    });
    let first_uri = position(&maps, |m| m.get("ansible.builtin.uri").is_some());

    // Assert
    let guard = guard.expect("the transport guard is not included");
    let first_uri = first_uri.expect("no uri task");
    assert!(
        guard < first_uri,
        "the transport guard (map {guard}) comes after the first uri task (map {first_uri})"
    );
}

/// The `_pat_*` values the transport guard task declares, by name.
fn guard_patterns() -> HashMap<String, String> {
    let doc = load(GUARD);
    let mut patterns = HashMap::new();
    for map in maps_in_order(&doc) {
        if let Some(Node::Map(vars)) = map.get("vars") {
            for (key, value) in vars {
                if let (Some(k), Some(v)) = (key.as_str(), value.as_str())
                    && k.starts_with("_pat_")
                {
                    patterns.insert(k.to_owned(), v.to_owned());
                }
            }
        }
    }
    patterns
}

/// A `_pat_*` pattern with its `{{ _pat_x }}` references replaced, as Jinja renders it.
fn expand(name: &str, patterns: &HashMap<String, String>) -> String {
    let reference = re(r"\{\{\s*(_pat_[a-z0-9_]+)\s*\}\}");
    let lookup = |n: &str| -> String {
        patterns
            .get(n)
            .cloned()
            .unwrap_or_else(|| panic!("pattern {n} is not declared"))
    };
    let mut pattern = lookup(name);
    for _ in 0..5 {
        let expanded = reference
            .replace_all(&pattern, |caps: &regex::Captures| lookup(&caps[1]))
            .into_owned();
        if expanded == pattern {
            break;
        }
        pattern = expanded;
    }
    unicode_escape(&pattern)
}

/// Every play that resolves an OpenBao URL, with the guard's count, the ratchet and the
/// discovery's own self-checks, as the BATS test ran them.
#[test]
fn every_openbao_play_includes_the_transport_guard() {
    // Arrange: the refusal is written once, in the shared task.
    let copies = repo::grep(
        "Refusing to send secret material",
        &[":(glob)platform/playbooks/*.yml"],
    );
    assert!(
        copies.is_empty(),
        "the refusal is copied into a playbook:\n{}",
        copies.join("\n")
    );
    let guard_doc = load(GUARD);
    let refusal = maps_in_order(&guard_doc).into_iter().any(|m| {
        m.get("ansible.builtin.assert")
            .and_then(|a| a.get("fail_msg"))
            .and_then(Node::as_str)
            .is_some_and(|msg| msg.contains("Refusing to send secret material"))
    });
    assert!(
        refusal,
        "the shared guard does not refuse with the rule's message"
    );
    let patterns = guard_patterns();
    assert!(
        patterns
            .get("_pat_strict")
            .is_some_and(|v| v.contains("{{ _pat_loopback }}")),
        "the loopback fragment is not composed in _pat_strict"
    );
    assert_eq!(
        patterns.get("_pat_loopback").map(String::as_str),
        Some(r"127(?:\.{{ _pat_ipv4_octet }}){3}"),
        "_pat_loopback is not the bounded IPv4 fragment"
    );

    let ratchet: BTreeSet<String> = read(RATCHET)
        .lines()
        .map(str::trim)
        .filter(|l| !l.is_empty() && !l.starts_with('#'))
        .map(str::to_owned)
        .collect();

    // Act: every playbook, every play that resolves a URL.
    let mut playbook_names: Vec<String> = fs::read_dir(repo::path(PLAYBOOKS))
        .expect("playbooks directory")
        .map(|e| {
            e.expect("directory entry")
                .file_name()
                .to_string_lossy()
                .into_owned()
        })
        .filter(|n| n.ends_with(".yml") && repo::path(PLAYBOOKS).join(n).is_file())
        .collect();
    playbook_names.sort();

    let mut guard = Guard::new(HashMap::new());
    let mut seen = BTreeSet::new();
    let mut unguarded = BTreeSet::new();
    for name in &playbook_names {
        let source = resolve(&repo::path(&format!("{PLAYBOOKS}/{name}")));
        let plays = items_of(Some(&load(&format!("{PLAYBOOKS}/{name}"))));
        for play in plays {
            let resolves = play.get("vars").and_then(|v| v.get("_bao_url")).is_some();
            if !resolves {
                continue;
            }
            seen.insert(name.clone());
            let tasks = items_of(play.get("tasks"));
            if guard.count(&tasks, &source, std::slice::from_ref(&source)) < 1 {
                unguarded.insert(name.clone());
            }
        }
    }

    // The discovery must catch the exact failure it exists for: removing the nested guard from
    // the extracted survey makes that play unguarded.
    let survey = resolve(&repo::path(
        "platform/playbooks/survey-o11y-pbs-physical-storage.yml",
    ));
    let survey_plays = items_of(Some(&load(
        "platform/playbooks/survey-o11y-pbs-physical-storage.yml",
    )));
    let survey_play = survey_plays
        .iter()
        .find(|p| p.get("vars").and_then(|v| v.get("_bao_url")).is_some())
        .expect("the survey declares a play that resolves a URL");
    let survey_tasks_path = resolve(&repo::path(
        "platform/playbooks/tasks/o11y-pbs-physical-storage-survey.yml",
    ));
    let mut mutated = items_of(Some(&load(
        "platform/playbooks/tasks/o11y-pbs-physical-storage-survey.yml",
    )));
    let guard_task = mutated
        .iter_mut()
        .find(|t| {
            t.get("name").and_then(Node::as_str)
                == Some("Refuse public cleartext OpenBao transport")
        })
        .expect("the survey names its transport guard task");
    set_key(
        guard_task,
        "ansible.builtin.include_tasks",
        Node::Str("missing-guard.yml".to_owned()),
    );
    let mut removed = Guard::new(HashMap::from([(survey_tasks_path, mutated)]));
    let removed_count = removed.count(
        &items_of(survey_play.get("tasks")),
        &survey,
        std::slice::from_ref(&survey),
    );

    // Discovery counts a mapping-form include by its file, and ignores a mapping with none.
    let mut discovery = Guard::new(HashMap::new());
    let mapping_form = mapping(&[(
        "ansible.builtin.include_tasks",
        mapping(&[
            ("file", Node::Str(GUARD_REL.to_owned())),
            (
                "apply",
                mapping(&[("tags", Node::Seq(vec![Node::Str("verify".to_owned())]))]),
            ),
        ]),
    )]);
    let mapping_count = discovery.count(&[mapping_form], &survey, std::slice::from_ref(&survey));
    let malformed = mapping(&[(
        "ansible.builtin.include_tasks",
        mapping(&[(
            "apply",
            mapping(&[("tags", Node::Seq(vec![Node::Str("verify".to_owned())]))]),
        )]),
    )]);
    let malformed_count = discovery.count(&[malformed], &survey, std::slice::from_ref(&survey));

    // Assert
    let mut errors = Vec::new();
    let survey_name = "survey-o11y-pbs-physical-storage.yml";
    if removed_count != 0 || ratchet.contains(survey_name) {
        errors.push(
            "transport guard check did not detect removal of the extracted survey's nested guard"
                .to_owned(),
        );
    }
    if mapping_count != 1 || malformed_count != 0 {
        errors.push(
            "guard discovery must count a mapping-form include file and ignore a malformed mapping"
                .to_owned(),
        );
    }
    errors.extend(
        unguarded
            .difference(&ratchet)
            .map(|n| format!("NEW unguarded play not in the ratchet: {n}")),
    );
    errors.extend(
        ratchet
            .difference(&unguarded)
            .map(|n| format!("{n} is now guarded — remove it from the ratchet")),
    );
    errors.extend(
        ratchet
            .iter()
            .filter(|n| !repo::path(PLAYBOOKS).join(n).is_file())
            .map(|n| format!("ratchet names a playbook that no longer exists: {n}")),
    );
    if seen.len() < 8 {
        errors.push(format!(
            "transport guard coverage unexpectedly found only {} playbooks",
            seen.len()
        ));
    }
    assert!(errors.is_empty(), "{}", errors.join("\n"));
}

/// Counts the transport-guard includes a play runs. A task file's includes resolve from that
/// file's directory, a file already on the stack is not followed again, and an include whose
/// path holds a template is not counted. Task files are read once.
struct Guard {
    shared: PathBuf,
    overrides: HashMap<PathBuf, Vec<Node>>,
    loaded: HashMap<PathBuf, Rc<Vec<Node>>>,
}

impl Guard {
    fn new(overrides: HashMap<PathBuf, Vec<Node>>) -> Guard {
        Guard {
            shared: resolve(&repo::path(GUARD)),
            overrides,
            loaded: HashMap::new(),
        }
    }

    fn tasks_of(&mut self, path: &Path) -> Rc<Vec<Node>> {
        if let Some(tasks) = self.loaded.get(path) {
            return Rc::clone(tasks);
        }
        let tasks = match self.overrides.get(path) {
            Some(tasks) => tasks.clone(),
            None => items_of(yaml::load_one(path).as_ref()),
        };
        let tasks = Rc::new(tasks);
        self.loaded.insert(path.to_path_buf(), Rc::clone(&tasks));
        tasks
    }

    fn count(&mut self, tasks: &[Node], source: &Path, stack: &[PathBuf]) -> usize {
        let mut total = 0;
        for task in tasks {
            if task.as_map().is_none() {
                continue;
            }
            for key in INCLUDE_KEYS {
                let Some(target) = include_target(task, &[key]) else {
                    continue;
                };
                if target.contains("{{") {
                    continue;
                }
                let base = source.parent().unwrap_or(Path::new("."));
                let resolved = resolve(&base.join(target));
                if resolved == self.shared {
                    total += 1;
                } else if resolved.is_file() && !stack.contains(&resolved) {
                    let nested = self.tasks_of(&resolved);
                    let mut next = stack.to_vec();
                    next.push(resolved.clone());
                    total += self.count(nested.as_slice(), &resolved, &next);
                }
            }
            for section in ["block", "rescue", "always"] {
                if let Some(children) = task.get(section).and_then(Node::as_seq) {
                    total += self.count(children, source, stack);
                }
            }
        }
        total
    }
}

#[test]
fn transport_pattern_accepts_internal_endpoints_and_refuses_public_ones() {
    // Arrange: the patterns as Jinja renders them, and the gate that uses the local-dev branch.
    let patterns = guard_patterns();
    for name in [
        "_pat_strict",
        "_pat_https",
        "_pat_single_label",
        "_pat_ipv4_octet",
    ] {
        assert!(
            patterns.contains_key(name),
            "pattern {name} not found in the shared task"
        );
    }
    let strict = re(&expand("_pat_strict", &patterns));
    let https = re(&expand("_pat_https", &patterns));
    let single = re(&expand("_pat_single_label", &patterns));

    let gate = that_items(GUARD).join(" ");
    let gate = gate.split_whitespace().collect::<Vec<_>>().join(" ");
    assert!(
        re(r"or \(\(groups\['all'\] \| local_dev_inventory\) and _assert_bao_url is match\(_pat_single_label\)")
            .is_match(&gate),
        "the single-label branch is not gated on a local-dev inventory"
    );

    // Act: the accept and refuse tables. Generic addresses only: the platform's real endpoint is site data.
    let url = |a: u32, b: u32, c: u32, d: u32| format!("http://{a}.{b}.{c}.{d}:8200");
    let mut accept: Vec<String> = [
        "https://bao.example.com:8200",
        "https://bao.example.com.:8200",
        "http://localhost:8200",
        "http://127.0.0.1:8200",
        "http://10.1.2.3:8200",       // example: RFC 1918 10/8
        "http://10.1.2.3",            // example: no port at all
        "http://192.168.0.1:8200/v1", // example: RFC 1918 192.168/16 with path
        "http://172.16.0.1:8200",     // example: RFC 1918 lower bound
        "http://172.31.255.254:8200", // example: RFC 1918 upper bound
        "https://[::1]:8200",
    ]
    .map(str::to_owned)
    .to_vec();
    accept.extend([url(10, 0, 0, 0), url(10, 255, 255, 255)]);

    let mut refuse: Vec<String> = [
        "https://",
        "https://bad..example",
        "https://public.example:bad",
        concat!("https://", "user:FAKE@public.example"),
        "http://bao.example.com:8200",
        "http://10.evil.example/",
        "http://192.168.0.1.evil.example/",
        "http://172.32.0.1:8200",
        "http://172.15.0.1:8200",
        "http://1270.0.0.1:8200",
        "http://127.0.0.1:80@bao.evil.example/v1/auth/approle/login", // trufflehog:ignore
        "http://127.0.0.1@bao.evil.example/v1",                       // trufflehog:ignore
        "http://10.1.2.3:8200@bao.evil.example/v1",                   // trufflehog:ignore
        "http://192.168.0.1:8200@bao.evil.example/",                  // trufflehog:ignore
        "http://local-openbao@bao.evil.example/",                     // trufflehog:ignore
        "http://local-openbao.evil.example:8200/",
        "http://local-openbao:8200",
    ]
    .map(str::to_owned)
    .to_vec();
    refuse.extend([
        url(10, 999, 2, 3),
        url(10, 1, 2, 256),
        url(192, 168, 256, 1),
        url(172, 16, 999, 3),
        url(127, 256, 0, 1),
    ]);

    let accepted =
        |u: &str| strict.is_match(u) && (!u.starts_with("https://") || https.is_match(u));
    let mut bad: Vec<String> = Vec::new();
    for u in &accept {
        if !accepted(u) {
            bad.push(format!("should ACCEPT: {u}"));
        }
    }
    for u in &refuse {
        if accepted(u) {
            bad.push(format!("should REFUSE: {u}"));
        }
    }
    // The local-dev branch: single-label names only, the same trailing delimiter.
    let single_accept = [
        "http://local-openbao:8200",
        "http://local-openbao:8200/v1",
        "http://host.containers.internal:8000",
    ];
    let single_refuse = [
        "http://host.containers.internal.evil.example/",
        "http://host.containers.internal@evil.example/", // trufflehog:ignore
        "http://evil.containers.internal:8000",
        "http://local-openbao@bao.evil.example/", // trufflehog:ignore
        "http://local-openbao.evil.example:8200/",
        "https://anything",
    ];
    for u in single_accept {
        if !single.is_match(u) {
            bad.push(format!("single-label should ACCEPT: {u}"));
        }
    }
    for u in single_refuse {
        if single.is_match(u) {
            bad.push(format!("single-label should REFUSE: {u}"));
        }
    }

    // Assert
    assert!(bad.is_empty(), "{}", bad.join("\n"));
    assert_eq!(
        accept.len() + refuse.len() + single_accept.len() + single_refuse.len(),
        43,
        "the transport table lost or gained a case"
    );
}

// ── Bytecode, the CAS create and the seed's access check ─────────────────────

#[test]
fn no_python_bytecode_is_tracked() {
    // Arrange
    let tracked = repo::ls_files(&[]);
    let bytecode = re(r"\.py[cod]$|(^|/)__pycache__/");

    // Act
    let found: Vec<&String> = tracked.iter().filter(|f| bytecode.is_match(f)).collect();

    // Assert: nothing tracked, and the ignore rules are general (not per service).
    assert!(found.is_empty(), "tracked bytecode: {found:?}");
    let gitignore = read(".gitignore");
    assert!(
        gitignore.lines().any(|l| l == "__pycache__/"),
        ".gitignore has no general __pycache__/ rule"
    );
    assert!(
        gitignore.lines().any(|l| l == "*.py[cod]"),
        ".gitignore has no *.py[cod] rule"
    );
}

#[test]
fn bao_merge_keys_creates_a_path_only_under_cas() {
    // Arrange: the create task and the task for a lost race, as the play sees them.
    let tasks = repo::tasks(BAO_MERGE);

    // Act
    let create = task_named(&tasks, "Create the new path");
    let create_uri = create
        .get("ansible.builtin.uri")
        .expect("the create task is a uri request");
    let race = task_named(&tasks, "Losing the create race");
    let race_uri = race
        .get("ansible.builtin.uri")
        .expect("the lost-race task is a uri request");

    // Assert: a create is refused on an existing path (cas 0), and a refusal of that is merged.
    assert_eq!(
        create_uri
            .get("body")
            .and_then(|b| b.get("options"))
            .and_then(|o| o.get("cas"))
            .and_then(Node::as_str),
        Some("0"),
        "the create is not CAS-guarded with cas 0"
    );
    assert_eq!(
        create_uri.get("status_code"),
        Some(&Node::Seq(vec![
            Node::Str("200".to_owned()),
            Node::Str("400".to_owned())
        ])),
        "the create does not answer 400 for a lost race"
    );
    assert_eq!(
        race_uri.get("method").and_then(Node::as_str),
        Some("PATCH"),
        "the lost race does not merge with PATCH"
    );
    assert!(
        race_uri
            .get("headers")
            .and_then(|h| h.get("Content-Type"))
            .and_then(Node::as_str)
            .is_some_and(|v| v.contains("merge-patch")),
        "the lost race does not send a merge-patch body"
    );
}

#[test]
fn seed_access_check_ends_the_play_before_any_write() {
    // Arrange
    let seed = load(SEED_KEY);
    let seed_maps = maps_in_order(&seed);

    // Act
    let access = position(&seed_maps, |m| {
        include_target(m, &INCLUDE_TASKS) == Some("tasks/assert-bao-seed-access.yml")
    });
    let merge = position(&seed_maps, |m| {
        include_target(m, &INCLUDE_TASKS) == Some("tasks/bao-merge-keys.yml")
    });
    let end = position(&seed_maps, |m| {
        m.get("ansible.builtin.meta").and_then(Node::as_str) == Some("end_play")
    });

    // Assert: the access check and the end of play both come before the merge.
    let (access, merge, end) = (
        access.expect("the access check is not included"),
        merge.expect("the merge is not included"),
        end.expect("no end_play"),
    );
    assert!(access < merge, "the access check runs after the merge");
    assert!(end < merge, "the play can write before it ends");

    let seed_tasks = play_tasks(SEED_KEY);
    let end_block = task_named(
        &seed_tasks,
        "End read-only verification before every secret-store write",
    );
    assert!(
        end_block
            .get("when")
            .and_then(Node::as_str)
            .is_some_and(|w| w.contains("bao_verify_access_only | default(false) | bool")),
        "the end_play is not gated on the access-check flag"
    );
    let value_check = task_named(&seed_tasks, "Validate the secret value is present");
    assert!(
        value_check
            .get("when")
            .and_then(Node::as_str)
            .is_some_and(|w| w.contains("not (bao_verify_access_only | default(false) | bool)")),
        "the value check still runs in access-check mode"
    );

    // The access check's own task file.
    let access_doc = load(SEED_ACCESS);
    let access_maps = maps_in_order(&access_doc);
    assert!(
        access_maps.iter().any(|m| m
            .get("ansible.builtin.uri")
            .and_then(|u| u.get("url"))
            .and_then(Node::as_str)
            .is_some_and(|url| url.contains("/v1/sys/capabilities-self"))),
        "the access check does not read the token's capabilities"
    );
    assert!(
        values_for_key(SEED_ACCESS, "_missing")
            .iter()
            .any(|v| v == "{{ _caps | seed_access_missing(_exists | bool) }}"),
        "the missing-capability rule is not the seed_access_missing filter"
    );
    let no_log = access_maps
        .iter()
        .filter(|m| m.get("no_log") == Some(&Node::Bool(true)))
        .count();
    assert_eq!(
        no_log, 2,
        "the access check does not mark its two token calls no_log"
    );
}
