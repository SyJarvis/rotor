---
name: gitbook-docs
description: Create, plan, migrate, or reorganize GitBook project documentation by inspecting the repository, deriving an evidence-backed information architecture, generating .gitbook.yaml, SUMMARY.md, and Markdown pages, and validating navigation and links. Use for requests such as creating GitBook docs for a codebase, redesigning a docs directory, documenting a project from source, filling documentation gaps, or reviewing an existing GitBook structure.
---

# GitBook Project Documentation

Build documentation from repository evidence rather than from a fixed page
template. Keep the information architecture task-oriented and adapt it to the
project's actual scope.

## Workflow

1. Determine the requested mode:
   - **Plan**: propose the documentation structure without writing pages.
   - **Create**: establish a new GitBook documentation set.
   - **Migrate**: reorganize existing documentation while preserving useful
     content and redirects.
   - **Extend**: add or revise a bounded section.
   - **Audit**: report structural, navigation, evidence, and link problems.

2. Inspect the repository before choosing a documentation structure.
   - Read repository instructions such as `AGENTS.md`.
   - Read the root README and package manifests.
   - Inspect source packages, public exports, examples, tests, configuration,
     formats, and existing documentation.
   - Run `python scripts/inspect_project.py <project-root>` for a compact
     inventory, then use targeted searches and source reads for important
     details.
   - Treat source, tests, examples, and explicit project notes as evidence.
     Do not infer supported behavior from filenames alone.

3. Build an internal evidence map before writing:

   ```text
   Page or claim -> source/API/test/example -> confidence or missing evidence
   ```

   Omit unsupported claims. Report material documentation gaps instead of
   inventing behavior.

4. Design the information architecture.
   - Read `references/information-architecture.md`.
   - Select sections because the project needs them, not because they appear in
     the baseline outline.
   - Give every proposed page a reader, question, evidence source, and place in
     the reading path.
   - Preserve stable existing URLs when migrating; add redirects for unavoidable
     moves.

5. Present a compact documentation plan before generating pages. Include:
   - the proposed directory tree;
   - the purpose and audience of each page;
   - the main evidence sources;
   - pages intentionally omitted because evidence is insufficient.

   Pause for confirmation only when the user requests a plan-first workflow or
   when competing structures would materially change the result. Otherwise,
   state the plan as a progress update and continue.

6. Generate or revise content.
   - Read `references/writing-guidelines.md`.
   - Write the homepage, installation, and shortest working path first.
   - Then write concepts and task guides, followed by reference, integrations,
     troubleshooting, and contributor material that the evidence supports.
   - Generate or update `SUMMARY.md` after the pages stabilize.
   - When creating or changing GitBook configuration, also read
     `references/gitbook-conventions.md`.

7. Validate the result.
   - Run examples or documentation snippets when practical.
   - Run `python scripts/validate_gitbook.py <project-root>`.
   - Review the final navigation order, relative links, orphan pages,
     placeholders, and working-tree diff.
   - Fix validation errors introduced by the task before finishing.

## Editing Rules

- Preserve accurate existing content; do not rewrite merely for stylistic
  uniformity.
- Prefer small, composable changes for extend and audit requests.
- Keep user guides organized around tasks, not source-directory mirrors.
- Separate tutorials, explanations, task guides, and API/reference material.
- Make project boundaries and non-goals explicit when they affect user
  expectations.
- Use repository-native examples and public APIs. Label pseudocode as
  pseudocode.
- Do not add empty sections, speculative roadmaps, unsupported compatibility
  claims, or copied implementation details presented as stable contracts.
- Do not introduce a documentation build system beyond GitBook unless the user
  requests it.

## Resources

- `scripts/inspect_project.py`: produce a bounded JSON inventory of a repository.
- `scripts/validate_gitbook.py`: validate GitBook structure, navigation, local
  Markdown links, orphan pages, and unresolved placeholders.
- `references/information-architecture.md`: choose pages and navigation.
- `references/writing-guidelines.md`: write evidence-backed technical content.
- `references/gitbook-conventions.md`: create and maintain GitBook files.
