---
name: build-skill
description: Build, test and save a reusable Kairos skill with checked supporting files.
---

# Build a skill

Use a skill for a repeatable procedure with a clear trigger, inputs and result.
A one-time request usually needs only a prompt. Check list_skills and read_skill
for an existing procedure before creating a duplicate. Ask for the desired
behavior, when it applies and an example; turn those answers into practical steps.

## Format and scope

A skill is a folder in Kairos's skills directory containing UTF-8 `SKILL.md`.
Kairos parses a frontmatter block between `---` lines followed by Markdown:

```markdown
---
name: weekly-review
description: Review open work and produce a weekly plan when a weekly review is requested.
---

# Weekly review

Read the specified open work. Group it by deadline and return a short plan.
If the input is unavailable, state which source is missing.
```

Use exactly `name` and `description` for a new skill. Keep the description on
one line and state when to use it. Kairos also reads indented description block
scalars and preserves other frontmatter keys on edits. The body is Markdown,
not executable configuration. Keep instructions concise, with expected inputs,
tool steps, output format and behavior for missing data. Avoid private app APIs.

Names become slugs: trim, lowercase, replace spaces with hyphens, remove
characters outside letters, digits, underscores and hyphens, and trim leading
hyphens. The final slug must match `[a-z0-9][a-z0-9_-]*` and cannot be empty.
Use `weekly_review` if sharing is planned: Store slugs require
`[a-z][a-z0-9_]{0,63}` and cannot be `routes`, `services` or `views`.

Supporting files are a map of relative paths to text, alongside SKILL.md.
Use forward slashes and ASCII path segments containing letters, digits,
underscores, dots or hyphens, at most 240 characters total. Absolute paths,
empty segments, traversal, backslashes, trailing dots/spaces, device names,
credential-file prefixes, bytecode and Git directories are refused. Supporting
files cannot supply SKILL.md or case variants of another path. Explain in the
body when to read a helper; a helper is not run merely because it exists.

## Scan and test

Kairos scans imports, including supporting text. The guard detects credential
access or transfer, instruction overrides, destructive actions, persistent
agent configuration changes, suspicious downloads and network behavior.
Critical findings produce `dangerous`; high findings produce `caution`;
medium and low findings can remain `safe`. Describe risks in plain language
instead of including attack examples or unnecessary command snippets. Review
actual findings and correct the procedure, rather than hiding its behavior.
The scan is a heuristic, not a correctness guarantee.

Test the procedure on the person's example and on missing or empty inputs.
Check that each named tool exists and its arguments are accurate. Keep secrets,
personal records and machine-specific paths out of reusable files. Computer
recordings are drafts: inspect their steps and retain the intended review points.

## Save and handoff

Finish by calling `save_skill(name, description, body, files)`; `body` contains
the Markdown after frontmatter and `files` is optional. This admin-only tool
uses the import scan. A caution report asks the person to review; a dangerous
report is refused outright by the existing import policy. A turn that
read untrusted content also asks, even with a safe scan. Replacement requires
`replace: true` and fresh approval; do not silently replace a skill. Report the
saved slug or the refusal and show where it appears in Tool Store > Skills.

## Share to the Kairos Store

Use the skill card's **Share to store** button. User-created, imported and
recorded skills can be shared; bundled skills cannot. Publishing requires
GitHub sign-in, a preview and confirmation, and produces a public MIT submission.
Use a semver version. Keep only text files: `.md .txt .py .sh .bash .js .ts .rb
.yaml .yml .json .toml .cfg .ini .conf .html .css .xml .tex .r .jl .pl .php`;
`.skillignore` and `.clawhubignore` are also allowed for skills. Limit the bundle
to 50 files, 5 MiB total, 256 KiB per file, and SKILL.md to 100,000 bytes.
No secrets or personal data; public payloads cannot contain insecure HTTP URLs.
