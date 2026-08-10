# Information Architecture

## Baseline

Use this only as a menu:

```text
docs/
├── README.md
├── SUMMARY.md
├── getting-started/
├── guides/
├── concepts/
├── api/
├── integrations/
├── formats/
├── troubleshooting/
└── development/
```

Remove sections the project does not need. Add domain-specific sections when
they better match how readers approach the project.

## Selection Rules

For every page, identify:

- **Reader**: new user, practitioner, integrator, or contributor.
- **Question**: the single primary question the page answers.
- **Evidence**: source files, public symbols, tests, examples, or project notes.
- **Next step**: where the reader should go afterward.

Do not create a page when its question duplicates another page or its claims
cannot be supported.

## Reading Paths

Provide at least one short path for a new user:

```text
Homepage -> Installation -> Quick start -> First task guide
```

Keep reference pages reachable without forcing experienced readers through a
tutorial. Put troubleshooting close to the workflows where failures occur and
also expose it in navigation.

## Existing Documentation

When reorganizing existing docs:

1. Classify each page as keep, revise, merge, move, or remove.
2. Preserve useful prose and stable URLs.
3. Add redirects for moved pages when the GitBook setup supports them.
4. Avoid parallel pages that explain the same concept differently.
5. Keep migration notes only when users still need them.

## Plan Format

Use a compact plan:

```text
path
  Reader:
  Question:
  Evidence:
  Action: create | revise | keep | move | merge
```
