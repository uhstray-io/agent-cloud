# Ledger citation ratchet, one file per port group

Each file here is `<group>.txt`, one per port group, in the same line format as
`../ledger_citation_ratchet.txt`, whose header explains it:
`<entry> <citation> # <reason>`, one line per occurrence.

Both citation checkers (`../suite/ledger.rs` and `../test_ledger_test_citations.py`) read the
old file plus every `*.txt` here, sorted by name. Other files are ignored.

A port pull request adds its lines here, in its own file, and never edits `docs/MISTAKES.md`:
a citation of a test the port deletes is covered by a line whose reason names the replacing
Rust test. The ledger is append-only: one later change adds each citing entry's "Enforcement
moved" note. The old file stays and only shrinks.

Each line's reason names the replacing Rust test(s) as `suite/<module>.rs::<fn>`; the test
`every_ratchet_directory_line_names_an_existing_replacing_test` in `../suite/ledger.rs` fails a
line that names none, or one that does not exist. The later change writes each citing entry's
"Enforcement moved" note (`docs/MISTAKES.md` 5.29) from these names. Provisional, pending the
operator's decision on 5.29 (plan 24, D10).
