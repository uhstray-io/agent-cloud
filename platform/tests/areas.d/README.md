# Area files

One file per area, so a pull request that ports a test group adds its own file and edits no
shared one. `Config::load` in `src/lanes.rs` reads every `*.yml` here, in name order, after
`areas.yml`; other files are ignored.

- The file name is the area name: `areas.d/<area>.yml`, letters, digits and underscores.
- `paths` (required): the globs whose change selects the area. May be `[]`.
- `tiers` (required): tier name (`static`, `unit`, `cli`, `http`, `playbook`) to module list.
  May be `{}`.
- `always` (optional): a tier map whose modules join the security set: every lane runs its
  static tier, and the push and CI lanes run every tier.
- No other key. An area already defined, here or in `areas.yml`, is refused.
- Each module (`suite/<module>.rs`) is listed exactly once across `areas.yml` and every file
  here; the `lanes` test `every_suite_module_is_listed_exactly_once_in_areas_yml` checks it.

A port whose tests belong to the paths of an existing area declares its own area with the same
globs: a changed path selects every area whose globs match it.

## A test that reads beyond its area

In the commit, push and CI lanes, a module outside `always` runs only when a change selects its
area, or when a change selects every area (a `full` path, or in the push and CI lanes an
unclassified one). Any file it reads must therefore select it in the push lane: by its area's
`paths`, by `full`, or by being unclassified (a path that no area claims and `ignore` does not
match, which runs everything). Otherwise a change that adds an offender to that file, where
another area claims it, skips the test.

- A static module that scans beyond its area's paths (a `repo::grep` or `repo::ls_files` over
  `platform`, say) goes under `always: static:`. Static tests are cheap, and that is what the set
  is for.
- Any other module gets its area's `paths` widened to what it reads.
- A file in `ignore` (`**/*.md`, `plan/**`, `docs/**`) is never unclassified: it selects nothing
  unless `full` or an area's `paths` match it. The two rules above apply to it, without the
  unclassified fallback.

The `lanes` test `every_repo_wide_scan_runs_when_a_file_it_reads_changes` checks this for every
`repo::grep` and `repo::ls_files` call outside `always`. It reads the pathspecs from the module's
source: string literals and constants of the same file. A pathspec built at run time is refused.
It sees those two calls written out, nothing else: not `repo::grep_in` or `repo::ls_files_in`
given the repository root, a call through a function value, or a directory walk. Three more
`lanes` tests check named files:

- `every_fixed_path_read_runs_when_its_file_changes`: a string literal, or a constant of the same
  file, that is a whole argument of a call and names a tracked file. A method argument (`.name(`)
  and a macro argument are needles, not reads.
- `every_file_a_playbook_chain_loads_selects_its_module`: for each playbook-tier module, the files
  each playbook it reads loads: `import_playbook` targets followed recursively,
  `include_tasks`/`import_tasks` targets (each action under its short, `ansible.builtin.` and
  `ansible.legacy.` spellings), and `{{ playbook_dir }}/<path>` strings (a directory
  stands for every tracked file under it). A target it cannot resolve fails the test unless its
  exception table lists it. It reads the real file, so it also asks for the extra-var guard import
  that the test harness drops; that over-selection is accepted.
- `every_playbook_module_runs_when_ansible_cfg_or_a_filter_plugin_changes`: `ansible.cfg` and every
  tracked file in the directories it loads (the filter and callback plugins) select every
  playbook-tier module. The config is pinned, not parsed: `CFG_SETTINGS` holds its setting lines
  whole (an indented line counts as a change), so any change to a setting line other than trailing
  whitespace fails the test; blank lines and lines starting with `#` are ignored. `CFG_KEYS` declares, for every key the pin may hold, whether
  its value is a plugin directory; a key it does not list, or a directory value other than one
  `./` path, fails the test too.

Still not checked: a path built at run time, a read through a variable or a function value, a
constant defined twice, `repo::playbook_files()`, a directory walk other than `repo::grep` and
`repo::ls_files`, a playbook's `vars_files` and `include_vars` targets, roles (`roles:`,
`include_role`, `import_role`), and plugin directories beside a playbook. List those files in the area's `paths`.

```yaml
# areas.d/netbox_prefix.yml
paths:
  - "platform/playbooks/netbox-allocate-ip.yml"
tiers:
  static: [netbox_prefix]
```
