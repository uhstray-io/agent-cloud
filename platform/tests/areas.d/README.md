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

A module outside `always` runs only when a change selects its area, or when a change selects
every area (a `full` path, or in the push and CI lanes an unclassified one). Any file it reads must
therefore select it in the push lane: by its area's `paths`, by `full`, or by being unclassified
(a path no area claims, which runs everything). Otherwise a change that adds an offender to that
file, where another area claims it, skips the test.

- A static module that scans beyond its area's paths (a `repo::grep` or `repo::ls_files` over
  `platform`, say) goes under `always: static:`. Static tests are cheap, and that is what the set
  is for.
- Any other module gets its area's `paths` widened to what it reads.
- A file in `ignore` (`**/*.md`, `plan/**`, `docs/**`) selects nothing only when neither `full`
  nor any area's `paths` match it, so the two rules above apply to it as to any other file.

The `lanes` test `every_repo_wide_scan_runs_when_a_file_it_reads_changes` checks this for every
`repo::grep` and `repo::ls_files` call outside `always`. It reads the pathspecs from the module's
source: string literals and constants of the same file. A pathspec built at run time is refused.
It sees those two calls written out, nothing else: not `repo::grep_in` or `repo::ls_files_in`
given the repository root, a call through a function value, or a directory walk. Reads of named
files (`text::read`, `repo::tasks`, `yaml::load_one`) and the configuration a playbook run loads
(the root `ansible.cfg` loads `platform/playbooks/filter_plugins/`) are not checked mechanically
either: list those files in the area's `paths`.

```yaml
# areas.d/netbox_prefix.yml
paths:
  - "platform/playbooks/netbox-allocate-ip.yml"
tiers:
  static: [netbox_prefix]
```
