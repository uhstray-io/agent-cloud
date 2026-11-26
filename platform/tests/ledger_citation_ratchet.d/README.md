# Ledger citation ratchet, one file per port group

Each file here is `<group>.txt`, one per port group, in the same line format as
`../ledger_citation_ratchet.txt`, whose header explains it:
`<entry> <citation> # <reason>`, one line per occurrence.

Both citation checkers (`../suite/ledger.rs` and `../test_ledger_test_citations.py`) read the
old file plus every `*.txt` here, sorted by name. Other files are ignored.

A port pull request adds its lines here, in its own file: a citation of a test the port
deletes is covered by a line whose reason names the replacing Rust test. In the same pull
request it appends an "Enforcement moved" note to each citing entry in `docs/MISTAKES.md`,
naming those tests (`docs/MISTAKES.md` 5.29). The ledger is append-only, so the note is added
and the entry's old text stays. The old file stays and only shrinks.

Each line's reason names the replacing Rust test(s) as `suite/<module>.rs::<fn>`; the test
`every_ratchet_directory_line_names_an_existing_replacing_test` in `../suite/ledger.rs` fails a
line that names none, or one that does not exist, and
`every_ratchet_line_for_a_deleted_test_has_an_enforcement_moved_note` fails a line whose entry
has no "Enforcement moved" note naming every test the line names. The ports of plan 24 that
landed before that test (v0.17.0) left the notes to one closing change, AC-1029, which wrote
them.
