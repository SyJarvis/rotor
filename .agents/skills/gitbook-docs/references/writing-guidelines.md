# Writing Guidelines

## Evidence

- Trace behavioral claims to source, tests, examples, or explicit project
  documentation.
- Prefer public interfaces over private implementation details.
- Distinguish verified behavior, recommendations, and known limitations.
- Do not convert a test fixture or internal helper into a promised public API.
- If evidence conflicts, investigate before writing. If it remains unresolved,
  state the uncertainty or omit the claim.

## Page Design

- Start with what the reader will accomplish or understand.
- Keep one primary question per page.
- Introduce prerequisites before commands.
- Use the shortest real example that demonstrates the supported path.
- Explain important parameters, outputs, failure modes, and cleanup immediately
  around the relevant step.
- Link to reference pages instead of repeating long option lists.
- End task pages with a concrete next step when one exists.

## Page Types

- **Tutorial**: a guided end-to-end learning path with a reliable outcome.
- **How-to guide**: steps for one concrete task; assume basic familiarity.
- **Concept**: explain boundaries, mental models, and design decisions.
- **Reference**: describe stable contracts precisely and make lookup easy.
- **Troubleshooting**: symptom, cause, diagnosis, fix, and verification.

Do not mix all page types into a single long page.

## Code Examples

- Reuse repository-native imports, names, and configuration.
- Run examples when practical; otherwise check every referenced symbol against
  the source.
- Include only setup required for the demonstrated task.
- Avoid hidden state and unexplained placeholders.
- Label shortened or non-runnable examples.

## Quality Check

Before finishing, verify:

- terminology matches the codebase;
- project scope and non-goals are accurate;
- installation and quick-start commands agree with package metadata;
- internal links resolve;
- navigation order supports the intended reading paths;
- no TODO text, generated filler, duplicated explanation, or speculative claim
  remains.
