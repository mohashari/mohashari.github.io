---
layout: post
title: "Git Workflow Best Practices for Engineering Teams"
tags: [git, devops, engineering, best-practices]
description: "Practical Git workflows, branching strategies, and commit hygiene that make collaboration smoother and history readable."
---

Git is the most powerful tool most engineers use poorly. Good Git hygiene makes code reviews better, debugging easier, and rollbacks safer. Here's how to level up your Git game.

## Commit Messages: The Standard

Follow the Conventional Commits specification:

```
<type>(<scope>): <description>

[optional body]

[optional footer]
```

Types:
- `feat` — New feature
- `fix` — Bug fix
- `refactor` — Code restructuring without feature change
- `perf` — Performance improvement
- `test` — Adding/updating tests
- `docs` — Documentation only
- `ci` — CI/CD changes
- `chore` — Maintenance (deps, build)

```bash
# Good commit messages
feat(auth): add refresh token rotation
fix(orders): prevent duplicate order creation on retry
perf(db): add index on orders.user_id for faster queries
refactor(user): extract email validation to separate function

# Bad commit messages
wip
fix bug
update code
asdfasdf
```

A good commit message tells a story. The subject is the headline; the body explains why:

```
fix(payments): handle race condition in concurrent charge requests

Two simultaneous charge requests for the same order could both
succeed if they ran concurrently. Added an idempotency key check
using a Redis distributed lock before processing payment.

Fixes: #482
```

## Branching Strategy: GitHub Flow

For most teams, GitHub Flow is the right choice:

```
main (always deployable)
  └── feature/add-oauth-login
  └── fix/user-not-found-on-edge-case
  └── chore/upgrade-go-1-22
```

Rules:
1. `main` is always deployable
2. Create a branch for every change
3. Open a PR early — use draft PRs for work in progress
4. Merge to main via PR after review
5. Deploy immediately after merging

### Branch Naming

```bash
feature/oauth-google-login
fix/order-status-not-updating
chore/upgrade-postgres-16
experiment/grpc-migration-poc
```

## Git Flow for Complex Release Cycles

If you have scheduled releases or need separate environments:

```
main (production)
develop (integration)
  └── feature/new-checkout
  └── feature/user-profiles
release/2.0 (stabilization)
hotfix/critical-payment-bug → main + develop
```

Most startups don't need Git Flow — it adds overhead. Use it only if you truly need release branches.

## Interactive Rebase: Clean Up Before PR

Before opening a PR, clean up your commit history:

```bash
# Squash WIP commits before review
git rebase -i origin/main

# In the editor, squash/fixup noisy commits:
pick abc1234 feat(auth): add JWT token generation
squash def5678 WIP
squash ghi9012 fix typo
squash jkl3456 actually fix it this time

# → becomes one clean commit
pick abc1234 feat(auth): add JWT token generation
```

## Useful Git Commands You Should Know

```bash
# See a visual branch graph
git log --oneline --graph --all

# Find which commit introduced a bug
git bisect start
git bisect bad HEAD
git bisect good v1.5.0
# Git will checkout commits; mark each as good/bad until found
git bisect good  # or git bisect bad

# Find who last changed a line
git blame -L 42,55 src/auth/jwt.go

# Search commit messages
git log --grep="rate limiting" --oneline

# Find commits that changed a specific string
git log -S "calculateTax" --oneline

# Undo last commit but keep changes staged
git reset --soft HEAD~1

# Temporarily stash changes
git stash push -m "WIP: half-done oauth"
git stash pop

# Show changes from specific commit
git show abc1234

# Restore a deleted file from history
git checkout HEAD~3 -- src/utils/helper.go
```

## .gitignore Essentials

```
# Dependencies
node_modules/
vendor/
.venv/

# Build output
dist/
build/
*.o
*.a
*.so

# Secrets (NEVER commit these)
.env
.env.local
*.pem
*.key
credentials.json
secrets.yml

# IDE
.idea/
.vscode/
*.swp

# OS
.DS_Store
Thumbs.db
```

Consider using [gitignore.io](https://gitignore.io) to generate language/framework-specific entries.

## Git Hooks for Quality Gates

```bash
# .git/hooks/pre-commit (make executable with chmod +x)
#!/bin/bash
set -e

echo "Running pre-commit checks..."
go fmt ./...
go vet ./...
golangci-lint run
go test ./... -short

echo "All checks passed!"
```

Use [pre-commit](https://pre-commit.com) framework for team-wide hooks:

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/dnephin/pre-commit-golang
    rev: v0.5.1
    hooks:
      - id: go-fmt
      - id: go-vet
      - id: golangci-lint
```

## Code Review Best Practices

**As author:**
- Keep PRs small (< 400 lines changed)
- Write a clear PR description explaining what and why
- Self-review before requesting review
- Respond to all comments

**As reviewer:**
- Review within 24 hours (respect others' flow)
- Distinguish blocking (`must fix`) from non-blocking (`nit:`, `suggestion:`)
- Ask questions; don't assume bad intent
- Approve when good enough, not perfect

```markdown
# PR Template
## What
Brief description of the change.

## Why
Why is this change needed?

## How to test
Steps for reviewers to verify the change.

## Screenshots (if UI change)

## Checklist
- [ ] Tests added/updated
- [ ] Documentation updated
- [ ] No breaking changes (or noted in breaking changes section)
```

Good Git hygiene is a team sport. Establish conventions early, automate enforcement, and invest in commit message quality — your future self (and teammates) will thank you.
