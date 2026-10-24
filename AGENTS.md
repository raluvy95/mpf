# Commit and release conventions

- Use Conventional Commits for new commits: `type: short imperative description` (optionally `type(scope): ...`).
- Use `feat:` for features, `fix:` for bug fixes, and `docs:`, `test:`, `refactor:`, or `chore:` where appropriate.
- Mark breaking changes with `!` after the type/scope and describe them in a `BREAKING CHANGE:` footer.
- Follow Semantic Versioning for releases: breaking changes bump MAJOR, features bump MINOR, and fixes bump PATCH. Other commit types do not imply a version bump by themselves.
- Base the next version on the latest existing tag (check `git tag`), never on unreleased commits. Only the highest bump
  since that tag applies, so with `v1.0.3` as the latest tag, any number of pending `feat:` commits means `1.1.0`, not `1.2.0`.
- These are naming and versioning conventions; pushing a SemVer tag publishes a
  GitHub Release with generated notes through GitHub Actions.
- Do not commit local virtual environments such as `.venv/`.
- Always pull and resolve upstream changes before creating a commit.
- Never push to GitHub from this environment; tell the user when a manual push is needed.
