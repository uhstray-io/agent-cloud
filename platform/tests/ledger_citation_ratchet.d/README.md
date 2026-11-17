# Ledger citation ratchet, one file per port group

Each file here is `<group>.txt`, one per port group, in the same line format as
`../ledger_citation_ratchet.txt`, whose header explains it:
`<entry> <citation> # <reason>`, one line per occurrence.

Both citation checkers (`../suite/ledger.rs` and `../test_ledger_test_citations.py`) read the
old file plus every `*.txt` here, sorted by name. Other files are ignored.

A port pull request adds its lines here, in its own file, and never edits `docs/MISTAKES.md`:
a citation of a test the port deletes is covered by a line whose reason names the replacing
Rust test. One later change rewrites the citations. The old file stays and only shrinks.
