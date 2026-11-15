//! Every tracked YAML file loads with the suite's loader (YAML 1.2, read the way PyYAML reads
//! it). yamllint and PyYAML read YAML 1.1 and accept files this loader refuses, so this is the only
//! check for them. Its own module, listed under `always` in `areas.yml`, so every lane runs it:
//! a YAML file under `plan/**` or `docs/**` selects nothing on its own (#1146 review).

use agent_cloud_tests::repo;
use agent_cloud_tests::yaml::parse;

#[test]
fn every_tracked_yaml_file_loads_with_the_suite_loader() {
    // Every file is read even after one fails, so the message names all of them. A parse
    // error panics in the loader; a panic here is the loader refusing the file.
    let files = repo::ls_files(&["*.yml", "*.yaml"]);
    assert!(
        !files.is_empty(),
        "git ls-files found no tracked YAML files"
    );

    let mut failures = Vec::new();
    for rel in &files {
        let text = match std::fs::read_to_string(repo::path(rel)) {
            Ok(text) => text,
            Err(e) => {
                failures.push(format!("{rel}: {e}"));
                continue;
            }
        };
        if let Err(payload) = std::panic::catch_unwind(|| {
            parse(&text, rel);
        }) {
            let reason = payload
                .downcast_ref::<String>()
                .map(String::as_str)
                .or_else(|| payload.downcast_ref::<&str>().copied())
                .unwrap_or("panicked");
            // The loader's parse error already names the file it was given.
            if reason.starts_with(rel.as_str()) {
                failures.push(reason.to_owned());
            } else {
                failures.push(format!("{rel}: {reason}"));
            }
        }
    }
    assert!(
        failures.is_empty(),
        "{} of {} tracked YAML files do not load with the suite loader:\n{}",
        failures.len(),
        files.len(),
        failures.join("\n")
    );
}
