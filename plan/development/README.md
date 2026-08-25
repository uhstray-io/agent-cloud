# plan/development

Implementation plans — one numbered document per initiative, read in dependency
order (`00-` first). A plan is written **before** coding begins; see the root
`AGENTS.md` ("Adding a New Service") and `plan/architecture/02-service-onboarding.md`.

This directory is also the **OpenSpec store root**. `openspec/` lives here,
registered as store `agent-cloud`, so active changes are
`openspec/changes/<slug>/` and ratified capability specs are `openspec/specs/`.

From the repository root, every OpenSpec command needs the store flag — the
root is nested, so ancestor-only resolution will not find it:

```bash
openspec list --store agent-cloud
openspec validate --all --store agent-cloud
```

**Change status is not tracked in a file here.** It comes from the store
(`openspec list`). A hand-maintained index of the same facts is a second source
that drifts — so there deliberately isn't one.
