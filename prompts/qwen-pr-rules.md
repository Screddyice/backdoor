# Git and pull-request rules

You are running in a minimal (`--bare`) session, which skips CLAUDE.md
auto-discovery and every hook. Nothing else is going to supply these rules and
no automation is going to do this for you, so follow them yourself.

## Branches

- Never commit to `main`. Create a branch first.
- Naming: `feat/fe-*` (frontend), `feat/be-*` (backend), `bug-fix/*`,
  `hot-fix/*`. A pre-push hook rejects anything else.
- Never put a Linear issue ID in the branch name — the same hook rejects it.

## Every branch gets a PR

- Open the PR as soon as the branch has its **first** commit. Do not wait until
  the work looks finished:

  ```bash
  git push -u origin HEAD
  gh pr create --draft --fill
  ```

- One branch, one PR. A branch that has commits and no PR is unfinished work.
- Before you stop, check yourself:

  ```bash
  git log --oneline origin/main..HEAD          # commits not on main?
  gh pr list --head "$(git branch --show-current)" --state open
  ```

  If the first prints anything and the second is empty, open the PR now.

## Check which repo `gh` is talking to

If the repo has both an `origin` and an `upstream` remote, `gh` may default to
`upstream` — someone else's repository. Run `git remote -v` first, and if there
are two, point `gh` at yours before creating anything:

```bash
gh repo set-default <owner>/<repo>     # the owner of `origin`
```

## Every PR updates the README

Every PR must include a real change to the repo's primary `README.md` covering
what the PR changes for a user or a developer. Do not mark a PR ready, hand it
off, or call the work complete until that change is committed. Timestamps and
"updated the README" entries do not count.

## Commits

- Conventional Commits: `feat:`, `fix:`, `docs:`, `chore:`, `refactor:`,
  `test:`, `ci:`, `perf:`, `style:`, `build:`.
- Subject 72 characters or fewer. The body explains what changed and why.
- **Never** use `--no-verify`.
- End the message with a `Co-Authored-By:` trailer naming the model that did the
  work.

## Linear

To link an issue, put `Closes ABC-123` on its own line in the PR **body** —
never in the branch name, and never as a bare `linear.app` URL. If you do not
have a real issue ID, leave it out. Do not invent one.

## RS21 is off limits

Any repository with `rs21` in its name gets no automation at all: no pushes, no
PRs, no merges. Stop and tell the user instead.
