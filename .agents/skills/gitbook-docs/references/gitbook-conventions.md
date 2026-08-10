# GitBook Conventions

## Repository Layout

Prefer a root `.gitbook.yaml` and a dedicated documentation directory:

```yaml
root: ./docs/

structure:
  readme: README.md
  summary: SUMMARY.md
```

Preserve an existing valid layout unless changing it solves a concrete problem.
Use paths relative to the configured GitBook root in `SUMMARY.md`.

## Navigation

- Keep `SUMMARY.md` synchronized with real pages.
- Use descriptive link labels rather than filenames.
- Order pages by reader journey, not alphabetically by default.
- Avoid linking the same page from multiple primary navigation entries.
- Exclude drafts and internal planning files from published navigation.

Example:

```markdown
# Summary

## Getting started

- [Overview](README.md)
- [Installation](getting-started/installation.md)
- [Quick start](getting-started/quickstart.md)
```

## Redirects

When moving published pages, add explicit old-to-new mappings in
`.gitbook.yaml` where the repository already uses GitBook redirects:

```yaml
redirects:
  old/path.md: new/path.md
```

Do not invent redirects for paths that were never published.

## Compatibility

GitBook behavior can evolve. When a task depends on a feature not already
demonstrated in the repository, verify the current official GitBook
documentation before relying on it. Keep standard Markdown portable and avoid
vendor-specific syntax unless it materially improves the documentation.
