//! Shared support for the agent-cloud test suite (plan 24).
//!
//! - [`repo`]: the repository root, `git ls-files`, and the playbook task walker;
//! - [`yaml`]: YAML loaded the way Ansible and podman-compose read it (YAML 1.1 scalars,
//!   anchors, aliases, `<<` merge keys);
//! - [`process`]: bounded child processes with a scrubbed environment, and the confined
//!   `ansible-playbook` runner (03 Part 1, rule 11);
//! - [`lanes`]: which tests a lane runs, from `areas.yml` and the changed paths (the
//!   `testsel` binary and `lane.sh` use it).

pub mod lanes;

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
        origin: String,
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

    /// Insert, or replace the value of an existing key: a mapping's later key wins, as in a
    /// Python dict.
    fn upsert(entries: &mut Vec<(Node, Node)>, key: Node, value: Node) {
        match entries.iter_mut().find(|(k, _)| *k == key) {
            Some(slot) => slot.1 = value,
            None => entries.push((key, value)),
        }
    }

    /// PyYAML's `flatten_mapping` (constructor.py): each `<<` value, in the order the keys
    /// appear, is a mapping or a list of mappings whose list is reversed, so within one list the
    /// earlier mapping wins and across `<<` keys the later one does. Explicit keys come last and
    /// win over every merged key. A merge value that is not a mapping is an error, as in PyYAML.
    fn apply_merges(
        explicit: Vec<(Node, Node)>,
        merges: Vec<Node>,
        origin: &str,
    ) -> Vec<(Node, Node)> {
        let mut out = vec![];
        for merge in merges {
            let maps = match merge {
                Node::Map(_) => vec![merge],
                Node::Seq(items) => items.into_iter().rev().collect(),
                other => panic!("{origin}: a `<<` merge value must be a mapping, found {other:?}"),
            };
            for map in maps {
                let Node::Map(entries) = map else {
                    panic!("{origin}: a `<<` merge list may hold only mappings, found {map:?}")
                };
                for (k, v) in entries {
                    upsert(&mut out, k, v);
                }
            }
        }
        for (k, v) in explicit {
            upsert(&mut out, k, v);
        }
        out
    }

    /// The handle yaml-rust2 gives a `!!` core tag. No repository file uses one, so the
    /// loader refuses them all rather than construct some and misread others.
    const CORE_TAG: &str = "tag:yaml.org,2002:";

    // `!!bool` arrives as handle `tag:yaml.org,2002:` plus suffix `bool`; the verbatim
    // `!<tag:yaml.org,2002:bool>` as an empty handle plus the whole URI. Join them so both match.
    fn refuse_core_tag(tag: Option<&yaml_rust2::parser::Tag>, origin: &str) {
        if let Some(t) = tag {
            let full = format!("{}{}", t.handle, t.suffix);
            if let Some(name) = full.strip_prefix(CORE_TAG) {
                panic!("{origin}: core tag `!!{name}` is not supported by the test loader");
            }
        }
    }

    impl MarkedEventReceiver for Builder {
        fn on_event(&mut self, ev: Event, _mark: Marker) {
            if let Event::Scalar(_, _, _, tag)
            | Event::SequenceStart(_, tag)
            | Event::MappingStart(_, tag) = &ev
            {
                refuse_core_tag(tag.as_ref(), &self.origin);
            }
            match ev {
                Event::Scalar(value, style, anchor, tag) => {
                    let plain = style == TScalarStyle::Plain && tag.is_none();
                    let is_merge_key = plain && self.key_position() && value == "<<";
                    // An application tag (compose's `!reset`, `!override`) reads as its text, as
                    // the any-tag constructor of the Python tests did.
                    let node = if plain {
                        resolve_plain(&value)
                    } else {
                        Node::Str(value)
                    };
                    if anchor > 0 {
                        self.anchors.insert(anchor, node.clone());
                    }
                    self.push(node, is_merge_key);
                }
                Event::Alias(id) => {
                    // yaml-rust2 refuses an alias to an unknown anchor before this sees it.
                    let node = self
                        .anchors
                        .get(&id)
                        .cloned()
                        .expect("alias to a known anchor");
                    self.push(node, false);
                }
                Event::SequenceStart(anchor, _) => {
                    self.stack.push(Frame::Seq {
                        items: vec![],
                        anchor,
                    });
                }
                Event::MappingStart(anchor, _) => {
                    self.stack.push(Frame::Map {
                        entries: vec![],
                        merges: vec![],
                        key: None,
                        anchor,
                    });
                }
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
                        let merged = apply_merges(entries, merges, &self.origin);
                        self.finish(Node::Map(merged), anchor);
                    }
                }
                _ => {}
            }
        }
    }

    /// Every document in `text`. Panics with `origin` on a parse error: in a test, a file the
    /// suite cannot parse is a failure, never a skip.
    pub fn parse(text: &str, origin: &str) -> Vec<Node> {
        let mut builder = Builder {
            origin: origin.to_owned(),
            ..Builder::default()
        };
        Parser::new_from_str(text)
            .load(&mut builder, true)
            .unwrap_or_else(|e| panic!("{origin}: YAML parse error: {e}"));
        builder.docs
    }

    /// The one document in a file, `None` for an empty file. PyYAML's `yaml.load` refuses a
    /// stream of several documents, and so does this.
    pub fn load_one(path: &Path) -> Option<Node> {
        let text =
            std::fs::read_to_string(path).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
        let docs = parse(&text, &path.display().to_string());
        assert!(
            docs.len() <= 1,
            "{}: {} YAML documents, expected one",
            path.display(),
            docs.len()
        );
        docs.into_iter().next()
    }
}

pub mod repo {
    use std::path::{Path, PathBuf};
    use std::process::Command;
    use std::sync::LazyLock;

    use crate::yaml::{self, Node};

    /// Read at run time, never compiled in: two checkouts sharing one CARGO_TARGET_DIR reuse
    /// one build, and a compiled-in path would test the other checkout's files. Cargo and
    /// nextest set CARGO_MANIFEST_DIR; a binary run on its own (`testsel` from a test) finds
    /// the checkout it runs in through git.
    static ROOT: LazyLock<PathBuf> = LazyLock::new(|| {
        let root = match std::env::var_os("CARGO_MANIFEST_DIR") {
            Some(manifest) => Path::new(&manifest).join("../.."),
            None => {
                let out = Command::new("git")
                    .args(["rev-parse", "--show-toplevel"])
                    .output()
                    .expect("git");
                assert!(out.status.success(), "not inside a git checkout");
                PathBuf::from(String::from_utf8(out.stdout).expect("utf-8 path").trim())
            }
        };
        root.canonicalize().expect("repository root")
    });

    /// The repository root (this crate lives at `platform/tests/`).
    pub fn root() -> &'static Path {
        &ROOT
    }

    pub fn path(rel: &str) -> PathBuf {
        root().join(rel)
    }

    /// `git -C <root> <args>`; panics unless the exit status is at most `max_status`.
    fn git(args: &[&str], max_status: i32) -> String {
        let out = Command::new("git")
            .arg("-C")
            .arg(root())
            .args(args)
            .output()
            .expect("git");
        assert!(
            out.status.code().is_some_and(|c| c <= max_status),
            "git {args:?} failed: {}",
            String::from_utf8_lossy(&out.stderr)
        );
        String::from_utf8(out.stdout).expect("utf-8 git output")
    }

    /// Tracked files matching git pathspecs, relative to the root. Untracked files are not
    /// part of the repository and never decide a test.
    pub fn ls_files(pathspecs: &[&str]) -> Vec<String> {
        let args = [&["ls-files", "--"], pathspecs].concat();
        git(&args, 0).lines().map(str::to_owned).collect()
    }

    /// `git grep -n -E` over tracked files; the matching lines, empty when none match (git
    /// grep exits 1 for no match).
    pub fn grep(pattern: &str, pathspecs: &[&str]) -> Vec<String> {
        let args = [&["grep", "-n", "-E", "-e", pattern, "--"], pathspecs].concat();
        git(&args, 1).lines().map(str::to_owned).collect()
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
        if let Some(items) = yaml::load_one(&path(rel)).as_ref().and_then(Node::as_seq) {
            walk(items, &mut out);
        }
        out
    }
}

pub mod process {
    //! Child processes (03 Part 1, rule 11): every program a test starts goes through here.
    //! [`scrubbed`] and [`bash_with`] start a program with a scrubbed environment and a 30 s
    //! bound. [`ansible_playbook`] starts `ansible-playbook` confined to one directory, and
    //! [`run`] and [`run_for`] run any of these to completion, bounded and with no stdin.

    use std::ffi::OsString;
    use std::os::unix::process::ExitStatusExt;
    use std::path::{Path, PathBuf};
    use std::process::Output;
    use std::time::{Duration, Instant};

    use assert_cmd::Command;

    use crate::repo;

    /// A child bounded by a timeout, with an environment that holds only `PATH`, a C locale,
    /// and a `HOME` and `TMPDIR` inside the directory the caller owns. Nothing from the
    /// developer's shell (an `ANSIBLE_CONFIG`, a `VAULT_TOKEN`) reaches the code under test.
    pub fn scrubbed(program: &str, home: &std::path::Path) -> Command {
        let mut cmd = Command::new(program);
        cmd.env_clear()
            .env("PATH", std::env::var_os("PATH").unwrap_or_default())
            .env("LANG", "C")
            .env("HOME", home)
            .env("TMPDIR", home)
            .timeout(Duration::from_secs(30));
        cmd
    }

    /// `bash -c` with `lib` (a repository-relative shell library) sourced first. `script`
    /// runs in the caller's working directory; arguments added with `.arg()` are `$1..`.
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

    /// The bound on one playbook run: the Python harness's `DEFAULT_TIMEOUT_SECONDS`.
    pub const PLAYBOOK_TIMEOUT: Duration = Duration::from_secs(120);

    /// SIGKILL, the signal `assert_cmd` sends a child at its timeout (the same on Linux and macOS).
    const SIGKILL: i32 = 9;

    /// The kernel sandbox a playbook run is confined by: `sandbox-exec` on macOS and `bwrap` on
    /// Linux, each when it is on PATH; `None` otherwise. Without one no write is refused by the
    /// kernel, so a confinement proof must check for it and say so (`support_playbook.rs`).
    pub fn sandbox() -> Option<&'static str> {
        if cfg!(target_os = "macos") && on_path("sandbox-exec") {
            Some("sandbox-exec")
        } else if cfg!(target_os = "linux") && on_path("bwrap") {
            Some("bwrap")
        } else {
            None
        }
    }

    fn on_path(program: &str) -> bool {
        std::env::var_os("PATH")
            .is_some_and(|path| std::env::split_paths(&path).any(|dir| dir.join(program).is_file()))
    }

    /// The environment of a playbook run whose writable state is `dir`. `caller` reads the
    /// caller's variables; the child gets exactly the list returned, never an inheritance. Ansible's
    /// own state moves under `dir`. ANSIBLE_CONFIG is never set, so the repository's `ansible.cfg`
    /// applies from the working directory. Collections stay where the caller's Ansible finds them:
    /// moving ANSIBLE_HOME would hide the installed `ansible.posix` (`harness_sandbox.py`'s
    /// `env_for`).
    pub fn playbook_env(
        dir: &Path,
        caller: impl Fn(&str) -> Option<OsString>,
    ) -> Vec<(&'static str, OsString)> {
        let state = dir.join(".ansible-state");
        let collections = caller("ANSIBLE_COLLECTIONS_PATH").unwrap_or_else(|| {
            let mut home = caller("ANSIBLE_HOME")
                .filter(|home| !home.is_empty())
                .unwrap_or_else(|| {
                    let mut home = caller("HOME").expect("HOME is set");
                    home.push("/.ansible");
                    home
                });
            home.push("/collections:/usr/share/ansible/collections");
            home
        });
        vec![
            ("PATH", caller("PATH").unwrap_or_default()),
            ("LANG", "C".into()),
            ("HOME", dir.into()),
            ("TMPDIR", dir.into()),
            ("ANSIBLE_HOME", state.join("home").into()),
            ("ANSIBLE_LOCAL_TEMP", state.join("local").into()),
            ("ANSIBLE_REMOTE_TEMP", state.join("remote").into()),
            ("ANSIBLE_NOCOLOR", "1".into()),
            ("ANSIBLE_COLLECTIONS_PATH", collections),
        ]
    }

    /// The directories a sandboxed run may write: `dir`, the caller's temp root (TMPDIR, else
    /// /tmp) and the process's temp directory, each resolved, without repeats.
    fn writable_roots(dir: &Path) -> Vec<PathBuf> {
        let caller_tmp = std::env::var_os("TMPDIR")
            .filter(|tmp| !tmp.is_empty())
            .map_or_else(|| PathBuf::from("/tmp"), PathBuf::from);
        let mut roots: Vec<PathBuf> = Vec::new();
        for path in [dir.to_path_buf(), caller_tmp, std::env::temp_dir()] {
            let resolved = path
                .canonicalize()
                .unwrap_or_else(|e| panic!("{}: {e}", path.display()));
            if !roots.contains(&resolved) {
                roots.push(resolved);
            }
        }
        roots
    }

    /// A sandbox-exec SBPL subpath for `root`. A quote or backslash would end the profile's
    /// string, so such a path is refused rather than escaped.
    fn subpath(root: &Path) -> String {
        let text = root.display().to_string();
        assert!(
            !text.contains(['"', '\\']),
            "{text}: a sandbox path cannot hold a quote or a backslash"
        );
        format!("(subpath \"{text}\")")
    }

    /// The `sandbox-exec` profile of harness_sandbox.py's `_profile`: default allow, every file
    /// write denied, then the writable roots and /dev allowed.
    fn sandbox_profile(roots: &[PathBuf]) -> String {
        let mut profile =
            String::from("(version 1)(allow default)(deny file-write*)(allow file-write*");
        for root in roots {
            profile.push(' ');
            profile.push_str(&subpath(root));
        }
        profile.push_str(" (subpath \"/dev\"))");
        profile
    }

    /// `ansible-playbook -i INVENTORY PLAYBOOK`, run from the repository root (so its
    /// `ansible.cfg` applies), in the environment of [`playbook_env`] and bounded by
    /// [`PLAYBOOK_TIMEOUT`]. Under a kernel sandbox (see [`sandbox`]) writes are allowed only in
    /// `dir`, the temp roots and /dev. Run it with [`run`]; add arguments with `.arg`.
    ///
    /// Panics when `ansible-playbook` is not on PATH: a test that cannot run a playbook must fail,
    /// never skip.
    pub fn ansible_playbook(dir: &Path, inventory: &str, playbook: &str) -> Command {
        assert!(
            on_path("ansible-playbook"),
            "ansible-playbook is not on PATH: run `uv sync --locked` and put .venv/bin first"
        );
        for sub in ["home", "local", "remote"] {
            let path = dir.join(".ansible-state").join(sub);
            std::fs::create_dir_all(&path).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
        }
        let roots = writable_roots(dir);
        let mut cmd = match sandbox() {
            None => Command::new("ansible-playbook"),
            Some("sandbox-exec") => {
                let mut cmd = Command::new("sandbox-exec");
                cmd.arg("-p")
                    .arg(sandbox_profile(&roots))
                    .arg("ansible-playbook");
                cmd
            }
            Some("bwrap") => {
                let mut cmd = Command::new("bwrap");
                // --die-with-parent: the sandboxed tree is SIGKILLed when bwrap's parent dies, so
                // a killed or crashed runner leaves no ansible behind (bwrap.xml).
                cmd.args([
                    "--ro-bind",
                    "/",
                    "/",
                    "--dev",
                    "/dev",
                    "--proc",
                    "/proc",
                    "--die-with-parent",
                ]);
                for root in &roots {
                    cmd.arg("--bind").arg(root).arg(root);
                }
                cmd.args(["--", "ansible-playbook"]);
                cmd
            }
            Some(other) => panic!("unknown sandbox `{other}`"),
        };
        cmd.args(["-i", inventory, playbook])
            .current_dir(repo::root())
            .env_clear()
            .envs(playbook_env(dir, |name| std::env::var_os(name)))
            .write_stdin(Vec::new())
            .timeout(PLAYBOOK_TIMEOUT);
        cmd
    }

    /// [`run_for`] with [`PLAYBOOK_TIMEOUT`]: the bound a playbook run is held to.
    pub fn run(cmd: &mut Command) -> Output {
        run_for(cmd, PLAYBOOK_TIMEOUT)
    }

    /// Runs `cmd` to completion with no stdin, killed once `timeout` has passed. A timeout panics
    /// with its bound and nothing the child printed: captured output can hold fixture credentials
    /// (`harness_sandbox.run`). `assert_cmd` returns a killed child as `Ok`, not as an error, so a
    /// timeout is a SIGKILL that came at or after the bound. The bound covers the direct child
    /// only: `assert_cmd` waits for the output pipes, so a grandchild (an Ansible worker) that
    /// keeps them open holds the return until it exits. The Python harness does not wait for that.
    pub fn run_for(cmd: &mut Command, timeout: Duration) -> Output {
        let started = Instant::now();
        let output = cmd
            .write_stdin(Vec::new())
            .timeout(timeout)
            .output()
            .unwrap_or_else(|e| panic!("the test command did not start: {e}"));
        let killed = output.status.signal() == Some(SIGKILL);
        if killed && started.elapsed() >= timeout {
            panic!(
                "test command timed out after {}s; captured output suppressed",
                timeout.as_secs_f64()
            );
        }
        output
    }

    fn shell_quote(s: &str) -> String {
        format!("'{}'", s.replace('\'', r"'\''"))
    }
}
