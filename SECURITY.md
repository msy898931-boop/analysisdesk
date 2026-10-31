# Security Policy

## Reporting a vulnerability

Please report security issues **privately**, not in a public issue.

- Open a private advisory: <https://github.com/msy898931-boop/analysisdesk/security/advisories/new>
- Or email the maintainer listed on the GitHub profile.

Include the affected endpoint or file, what you expected, what happened, and a
minimal way to reproduce it. Please do not include real market data, keys, or
another person's chat id in the report.

You can expect an acknowledgement within a few days. This is a small,
single-maintainer project with no bug-bounty budget; fixes are made on a
best-effort basis and the advisory is published once a fix ships.

## Scope

In scope:

- The deployed service at `https://analysisdesk.onrender.com` and the code in
  this repository.
- The HTTP API (`/api/*`), the served pages (`/`, `/pitch`), and the container
  image built from `Dockerfile`.

Out of scope:

- Denial of service by volume against the free-tier host.
- The availability or accuracy of upstream market-data providers (Yahoo
  Finance, Kraken, Coinbase, Swissquote) and the public RSS feeds.
- Findings that require a compromised host, browser, or network.

## What this service already does

The controls below exist in the code and are covered by tests; a report that one
of them is missing should say which and where.

- **Content-Security-Policy** `script-src 'self'` with no `'unsafe-inline'` for
  scripts, plus `frame-ancestors 'none'`, `object-src 'none'`, and
  `base-uri 'self'`. Pages ship no inline script; the language toggle is CSS.
- **No secrets in the repository or the image.** Keys are read from the
  environment; `.env` and the SQLite store are git-ignored and excluded from the
  image by `.dockerignore`.
- **Unprivileged container.** The image runs as uid 10001, never root, and CI
  fails the build if it does.
- **Hash-pinned dependencies.** The image installs `requirements.lock` with
  `--require-hashes`, so a moved transitive dependency fails the build.
- **Rate limiting and an optional API key** on the analysis endpoints.
- **Error handling that does not leak.** Unhandled exceptions are logged
  server-side with secrets redacted and answered to the caller as a bare
  `internal error` with a request id.
- **No `eval`, no shell from request data, no SSRF.** Symbols are validated
  against a strict pattern before they reach any upstream URL, and upstream
  hosts are fixed.

## Supported versions

Only the latest commit on `main` (the deployed revision) is supported; there
are no maintained release branches. The current version is the `version` in
`pyproject.toml`, reported by `/api/status`.
