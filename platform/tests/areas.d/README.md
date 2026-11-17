# Area files

One file per area, so a pull request that ports a test group adds its own file and edits no
shared one. `Config::load` in `src/lanes.rs` reads every `*.yml` here, in name order, after
`areas.yml`; other files are ignored.

- The file name is the area name: `areas.d/<area>.yml`, letters, digits and underscores.
- `paths` (required): the globs whose change selects the area. May be `[]`.
- `tiers` (required): tier name (`static`, `unit`, `cli`, `http`, `playbook`) to module list.
  May be `{}`.
- `always` (optional): a tier map whose modules join the security set every lane runs.
- No other key. An area already defined, here or in `areas.yml`, is refused.
- Each module (`suite/<module>.rs`) is listed exactly once across `areas.yml` and every file
  here; the `lanes` test `every_suite_module_is_listed_exactly_once_in_areas_yml` checks it.

A port whose tests belong to the paths of an existing area declares its own area with the same
globs: a changed path selects every area whose globs match it.

```yaml
# areas.d/netbox_prefix.yml
paths:
  - "platform/playbooks/netbox-allocate-ip.yml"
tiers:
  static: [netbox_prefix]
```
