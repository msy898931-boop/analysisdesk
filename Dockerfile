# Pinned by digest, not just tag: `python:3.12-slim` is a moving target, so a
# rebuild months from now would pull a different base than the one CI tested.
# The digest is the multi-arch index, so the amd64 build on Render and the CI
# runner resolve to the same bytes. Bump it deliberately (see CONTRIBUTING).
FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so the layer is cached across code-only rebuilds.
#
# requirements.lock is the resolved, hash-pinned form of requirements.txt, so a
# rebuild months from now installs the same bytes it installs today. Without it
# a transitive dependency can move under us and the image that passes CI is not
# the image that ships. --require-hashes makes pip refuse anything that does not
# match, which is also what makes the lock a supply-chain check and not a note.
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY app ./app
COPY web ./web

# The version lives in pyproject.toml and nowhere else, so the image carries that
# one file for /api/status to read. It is the same source the metadata is built
# from, so there is no second copy to drift.
COPY pyproject.toml .

# Run as an unprivileged user. The service only reads its own code and serves
# HTTP, so it has no reason to be root; if the process is ever compromised, the
# blast radius stays inside the container instead of the container being the
# attacker's to keep.
RUN useradd --create-home --uid 10001 desk \
    && chown -R desk:desk /app
USER desk

# Render and most PaaS hosts inject $PORT at runtime; 12000 is only a local default.
ENV PORT=12000
EXPOSE 12000

# Render probes healthCheckPath; this makes the image self-describing elsewhere.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','12000')+'/api/health').read()" || exit 1

# Bind to 0.0.0.0 so the platform's router can reach it.
#
# --workers 1 is deliberate. The rate limiter counts in process memory, so with
# N workers the effective limit is N x RATE_LIMIT_PER_MIN and a caller could get
# N times the intended budget by spreading requests across workers. One worker
# makes the in-process counter the instance counter -- the number the operator
# set is the number that applies. The desk is I/O-bound (it waits on four
# upstreams, not on CPU), so a single worker is not a throughput problem; if
# that ever changes, move the counter to a shared store (Redis) before raising
# this, not after.
#
# --no-proxy-headers is deliberate too. Uvicorn otherwise rewrites the client
# address from X-Forwarded-For whenever the peer is a trusted proxy, so the
# socket peer we fall back to would itself be a header the caller set. With it
# off, the peer is the real peer and the app decides for itself whether to read
# the header (DESK_TRUST_PROXY), in one place instead of two.
CMD ["sh", "-c", "python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --no-proxy-headers"]
