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
- **Rate limiting and an optional API key** on the analysis endpoints. When
  `DESK_API_KEY` is set it is required from *every* caller. There is no
  same-origin exemption: a page-script check such as `Sec-Fetch-Site` is set by
  the browser but is equally settable by any script, so trusting it was a bypass
  rather than a credential. The gate fails closed.
- **The rate-limit identity is the socket peer, not a header.** `X-Forwarded-For`
  is caller-controlled, so it is read only when `DESK_TRUST_PROXY` says a proxy
  we control is in front (Render's router appends the real client as the last
  hop; the blueprint sets it). Off — the default, and any deployment with
  nothing in front — the header is ignored and the peer is used. The run command
  passes `--no-proxy-headers` so uvicorn cannot rewrite that peer from the same
  header. Reading the header untrusted let one caller mint a fresh identity per
  request and step around the limit entirely.
- **The alerts API is gated by a service token, not by the chat id.** A chat id
  is an identifier, not a secret, so `/api/alerts*` requires `x-alerts-token`
  matching `ALERTS_TOKEN`. With no token set the endpoints answer `503` (closed);
  they never serve an unauthenticated caller. The chat id still scopes the rows,
  so a token-holder can only reach the alerts it names.
- **Error handling that does not leak.** Unhandled exceptions are logged
  server-side with secrets redacted and answered to the caller as a bare
  `internal error` with a request id.
- **No `eval`, no shell from request data, no server-side request forgery.**
  A symbol becomes a path segment of an upstream URL, so it is validated against
  a strict anchored pattern (`^[A-Za-z0-9.^=_-]{1,20}$`, refusing any `..`) and
  percent-encoded at the point the URL is built — in `market.py`, where every
  caller (the API, `alerts.py`, the scripts) is covered, not only at the HTTP
  edge. Upstream hosts are fixed constants, never derived from input. This was a
  real CodeQL finding ("partial SSRF") on the first version of this pass: the
  check lived only in the API layer, so `fetch_series` had callers that could
  route a `..` into the path. It is fixed and pinned by tests.

## Supported versions

Only the latest commit on `main` (the deployed revision) is supported; there
are no maintained release branches. The current version is the `version` in
`pyproject.toml`, reported by `/api/status`.
