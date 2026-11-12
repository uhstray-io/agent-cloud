//! Shared support for the agent-cloud test suite (plan 24).
//!
//! Three things every area needs, and nothing else:
//! - [`repo`]: the repository root, `git ls-files`, and the playbook task walker;
//! - [`yaml`]: YAML loaded the way Ansible and podman-compose read it (YAML 1.1 scalars,
//!   anchors, aliases, `<<` merge keys);
//! - [`process`]: a bounded child process with a scrubbed environment (03 Part 1, rule 11).

pub mod yaml {
    //! YAML as PyYAML's `SafeLoader` reads it.
    //!
    //! `yaml-rust2` implements YAML 1.2, where a bare `no` is the string "no". Ansible and
    //! podman-compose parse with PyYAML (YAML 1.1), where it is the boolean false, and
    //! `.yamllint.yml` allows bare `yes`/`no`. A test that compared strings would accept
    //! what the deploy tooling reads differently (plan 24, D7). So scalars are resolved here,
    //! from the event stream, with PyYAML's implicit resolvers for bool and null
    //! (`yaml/resolver.py`, PyYAML 6). Every other plain scalar stays a string: no test
    //! compares numbers yet.

    use std::collections::HashMap;
    use std::path::Path;

    use yaml_rust2::parser::{Event, MarkedEventReceiver, Parser};
    use yaml_rust2::scanner::{Marker, TScalarStyle};

    #[derive(Clone, Debug, PartialEq)]
    pub enum Node {
        Null,
        Bool(bool),
        Str(String),
        Seq(Vec<Node>),
        /// Insertion order kept, merge keys already applied.
        Map(Vec<(Node, Node)>),
    }

    impl Node {
        /// The value under a string key of a mapping; `None` for a missing key or a non-mapping.
        pub fn get(&self, key: &str) -> Option<&Node> {
            match self {
                Node::Map(entries) => entries
                    .iter()
                    .find(|(k, _)| k.as_str() == Some(key))
                    .map(|(_, v)| v),
                _ => None,
            }
        }

        pub fn as_str(&self) -> Option<&str> {
            match self {
                Node::Str(s) => Some(s),
                _ => None,
            }
        }

        pub fn as_map(&self) -> Option<&[(Node, Node)]> {
            match self {
                Node::Map(entries) => Some(entries),
                _ => None,
            }
        }

        pub fn as_seq(&self) -> Option<&[Node]> {
            match self {
                Node::Seq(items) => Some(items),
                _ => None,
            }
        }
    }

    /// PyYAML's implicit resolution of a plain scalar (resolver.py: bool, null).
    pub fn resolve_plain(value: &str) -> Node {
        match value {
            "" | "~" | "null" | "Null" | "NULL" => Node::Null,
            "yes" | "Yes" | "YES" | "true" | "True" | "TRUE" | "on" | "On" | "ON" => {
                Node::Bool(true)
            }
            "no" | "No" | "NO" | "false" | "False" | "FALSE" | "off" | "Off" | "OFF" => {
                Node::Bool(false)
            }
            _ => Node::Str(value.to_owned()),
        }
    }

    enum Frame {
        Seq {
            items: Vec<Node>,
            anchor: usize,
        },
        Map {
            entries: Vec<(Node, Node)>,
            merges: Vec<Node>,
            key: Option<Key>,
            anchor: usize,
        },
    }

    enum Key {
        Merge,
        Node(Node),
    }

    #[derive(Default)]
    struct Builder {
        docs: Vec<Node>,
        stack: Vec<Frame>,
        anchors: HashMap<usize, Node>,
    }

    impl Builder {
        fn finish(&mut self, node: Node, anchor: usize) {
            if anchor > 0 {
                self.anchors.insert(anchor, node.clone());
            }
            self.push(node, false);
        }

        fn push(&mut self, node: Node, is_merge_key: bool) {
            match self.stack.last_mut() {
                None => self.docs.push(node),
                Some(Frame::Seq { items, .. }) => items.push(node),
                Some(Frame::Map {
                    entries,
                    merges,
                    key,
                    ..
                }) => match key.take() {
                    None if is_merge_key => *key = Some(Key::Merge),
                    None => *key = Some(Key::Node(node)),
                    Some(Key::Merge) => merges.push(node),
                    Some(Key::Node(k)) => entries.push((k, node)),
                },
            }
        }

        fn key_position(&self) -> bool {
            matches!(self.stack.last(), Some(Frame::Map { key: None, .. }))
        }
    }

    /// PyYAML's merge: explicit keys win, then the merged maps in order (earlier wins).
    fn apply_merges(mut entries: Vec<(Node, Node)>, merges: Vec<Node>) -> Vec<(Node, Node)> {
        let sources = merges.into_iter().flat_map(|m| match m {
            Node::Seq(maps) => maps,
            other => vec![other],
        });
        for source in sources {
            for (k, v) in source.as_map().map(<[_]>::to_vec).unwrap_or_default() {
                if !entries.iter().any(|(existing, _)| *existing == k) {
                    entries.push((k, v));
                }
            }
        }
        entries
    }

    impl MarkedEventReceiver for Builder {
        fn on_event(&mut self, ev: Event, _mark: Marker) {
            match ev {
                Event::Scalar(value, style, anchor, tag) => {
                    let is_merge_key = self.key_position()
                        && style == TScalarStyle::Plain
                        && tag.is_none()
                        && value == "<<";
                    let node = match (style, tag) {
                        // An application tag (compose's `!reset`, `!override`) reads as its text,
                        // as the any-tag constructor of the Python tests did.
                        (TScalarStyle::Plain, None) => resolve_plain(&value),
                        _ => Node::Str(value),
                    };
                    if anchor > 0 {
                        self.anchors.insert(anchor, node.clone());
                    }
                    self.push(node, is_merge_key);
                }
                Event::Alias(id) => {
                    let node = self.anchors.get(&id).cloned().unwrap_or(Node::Null);
                    self.push(node, false);
                }
                Event::SequenceStart(anchor, _) => self.stack.push(Frame::Seq {
                    items: vec![],
                    anchor,
                }),
                Event::MappingStart(anchor, _) => self.stack.push(Frame::Map {
                    entries: vec![],
                    merges: vec![],
                    key: None,
                    anchor,
                }),
                Event::SequenceEnd => {
                    if let Some(Frame::Seq { items, anchor }) = self.stack.pop() {
                        self.finish(Node::Seq(items), anchor);
                    }
                }
                Event::MappingEnd => {
                    if let Some(Frame::Map {
                        entries,
                        merges,
                        anchor,
                        ..
                    }) = self.stack.pop()
                    {
                        self.finish(Node::Map(apply_merges(entries, merges)), anchor);
                    }
                }
                _ => {}
            }
        }
    }

    /// Every document in `text`. Panics with `origin` on a parse error: in a test, a file the
    /// suite cannot parse is a failure, never a skip.
    pub fn parse(text: &str, origin: &str) -> Vec<Node> {
        let mut builder = Builder::default();
        Parser::new_from_str(text)
            .load(&mut builder, true)
            .unwrap_or_else(|e| panic!("{origin}: YAML parse error: {e}"));
        builder.docs
    }

    pub fn load(path: &Path) -> Vec<Node> {
        let text =
            std::fs::read_to_string(path).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
        parse(&text, &path.display().to_string())
    }
}

pub mod repo {
    use std::path::{Path, PathBuf};
    use std::process::Command;
    use std::sync::LazyLock;

    use crate::yaml::{self, Node};

    static ROOT: LazyLock<PathBuf> = LazyLock::new(|| {
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../..")
            .canonicalize()
            .expect("repository root")
    });

    /// The repository root (this crate lives at `platform/tests/`).
    pub fn root() -> &'static Path {
        &ROOT
    }

    pub fn path(rel: &str) -> PathBuf {
        root().join(rel)
    }

    /// Tracked files matching git pathspecs, relative to the root. Untracked files are not
    /// part of the repository and never decide a test.
    pub fn ls_files(pathspecs: &[&str]) -> Vec<String> {
        let out = Command::new("git")
            .arg("-C")
            .arg(root())
            .args(["ls-files", "--"])
            .args(pathspecs)
            .output()
            .expect("git ls-files");
        assert!(
            out.status.success(),
            "git ls-files failed: {}",
            String::from_utf8_lossy(&out.stderr)
        );
        String::from_utf8(out.stdout)
            .expect("utf-8 paths")
            .lines()
            .map(str::to_owned)
            .collect()
    }

    /// `git grep -n -E` over tracked files; the matching lines, empty when none match.
    pub fn grep(pattern: &str, pathspecs: &[&str]) -> Vec<String> {
        let out = Command::new("git")
            .arg("-C")
            .arg(root())
            .args(["grep", "-n", "-E", "-e", pattern, "--"])
            .args(pathspecs)
            .output()
            .expect("git grep");
        // git grep exits 1 for "no match"; anything above is an error.
        assert!(
            out.status.code().is_some_and(|c| c <= 1),
            "git grep failed: {}",
            String::from_utf8_lossy(&out.stderr)
        );
        String::from_utf8_lossy(&out.stdout)
            .lines()
            .map(str::to_owned)
            .collect()
    }

    /// Every task of a task file or play list, depth first, with `block`/`rescue`/`always`
    /// children included after their parent. Assertions select a task from here and test
    /// that task, never the whole file (03 Part 1, rule 8).
    pub fn tasks(rel: &str) -> Vec<Node> {
        fn walk(items: &[Node], out: &mut Vec<Node>) {
            for item in items {
                out.push(item.clone());
                for section in [
                    "block",
                    "rescue",
                    "always",
                    "tasks",
                    "pre_tasks",
                    "post_tasks",
                    "handlers",
                ] {
                    if let Some(children) = item.get(section).and_then(Node::as_seq) {
                        walk(children, out);
                    }
                }
            }
        }
        let mut out = vec![];
        for doc in yaml::load(&path(rel)) {
            if let Some(items) = doc.as_seq() {
                walk(items, &mut out);
            }
        }
        out
    }
}

pub mod process {
    use std::time::Duration;

    use assert_cmd::Command;

    /// A child bounded by a timeout, with an environment that holds only `PATH`, a C locale
    /// and a `HOME` the caller owns. Nothing from the developer's shell (an `ANSIBLE_CONFIG`,
    /// a `VAULT_TOKEN`) reaches the code under test.
    pub fn scrubbed(program: &str, home: &std::path::Path) -> Command {
        let mut cmd = Command::new(program);
        cmd.env_clear()
            .env("PATH", std::env::var_os("PATH").unwrap_or_default())
            .env("LANG", "C")
            .env("HOME", home)
            .timeout(Duration::from_secs(30));
        cmd
    }

    /// `bash -c` with `lib` (a repository-relative shell library) sourced first. `script`
    /// runs in the caller's working directory, with `$1..` from `args`.
    pub fn bash_with(lib: &str, script: &str, home: &std::path::Path) -> Command {
        let mut cmd = scrubbed("bash", home);
        cmd.arg("-c")
            .arg(format!(
                "source {}\n{script}",
                shell_quote(&crate::repo::path(lib).display().to_string())
            ))
            .arg("bash");
        cmd
    }

    fn shell_quote(s: &str) -> String {
        format!("'{}'", s.replace('\'', r"'\''"))
    }
}

#[cfg(test)]
mod tests {
    use super::yaml::{Node, parse, resolve_plain};

    #[test]
    fn plain_scalars_resolve_as_pyyaml_does() {
        let cases = [
            ("no", Node::Bool(false)),
            ("Off", Node::Bool(false)),
            ("YES", Node::Bool(true)),
            ("on", Node::Bool(true)),
            ("~", Node::Null),
            ("", Node::Null),
            ("NULL", Node::Null),
            ("always", Node::Str("always".into())),
            ("n", Node::Str("n".into())),
            ("nO", Node::Str("nO".into())),
        ];
        for (input, expected) in cases {
            assert_eq!(resolve_plain(input), expected, "plain scalar {input:?}");
        }
    }

    #[test]
    fn quoted_and_tagged_scalars_stay_strings() {
        let doc = &parse("a: 'no'\nb: \"yes\"\nc: !reset no\nd: no\n", "inline")[0];
        assert_eq!(doc.get("a"), Some(&Node::Str("no".into())));
        assert_eq!(doc.get("b"), Some(&Node::Str("yes".into())));
        assert_eq!(doc.get("c"), Some(&Node::Str("no".into())));
        assert_eq!(doc.get("d"), Some(&Node::Bool(false)));
    }

    #[test]
    fn aliases_and_merge_keys_resolve_with_explicit_keys_winning() {
        let text = "base: &b {restart: always, image: x}\n\
                    other: &o {image: y, user: z}\n\
                    svc:\n  <<: [*b, *o]\n  image: mine\n\
                    copy: *b\n";
        let doc = &parse(text, "inline")[0];
        let svc = doc.get("svc").unwrap();
        assert_eq!(svc.get("image").and_then(Node::as_str), Some("mine"));
        assert_eq!(svc.get("restart").and_then(Node::as_str), Some("always"));
        assert_eq!(svc.get("user").and_then(Node::as_str), Some("z"));
        assert!(svc.get("<<").is_none(), "the merge key itself is not a key");
        assert_eq!(doc.get("copy"), doc.get("base"));
    }

    #[test]
    fn a_quoted_merge_key_is_an_ordinary_key() {
        let doc = &parse("b: &b {x: 1}\nm:\n  '<<': *b\n", "inline")[0];
        let m = doc.get("m").unwrap();
        assert!(m.get("<<").is_some());
        assert!(m.get("x").is_none());
    }
}
