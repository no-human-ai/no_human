# syntax=docker/dockerfile:1
#
# The review-gate GitHub Action's own image — deliberately separate from the
# root Dockerfile (the always-on dashboard/server image) and Dockerfile.mcp
# (the MCP stdio server image). This one runs `no_human.ci_action.run` and
# nothing else: no server, no dashboard, no persisted ~/.no_human.
#
# The image NEVER bakes in a credential. ANTHROPIC_API_KEY and
# CLAUDE_CODE_OAUTH_TOKEN are supplied at `docker run` time by the Action
# runtime via the `credential` input (see action.yml / src/no_human/ci_action).
FROM python:3.12-slim

# git: the Action reads the diff/tamper-guard state via read-only `git`
# subcommands (see ci_action/run.py's `_ALLOWED_GIT_SUBCOMMANDS`).
# ca-certificates/curl: TLS trust + fetching the Node.js setup script.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# The `claude` CLI: claude_agent_sdk (no_human's only LLM transport) shells
# out to it — see claude_agent_sdk/_internal/transport/subprocess_cli.py.
RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app
COPY pyproject.toml README.md hatch_build.py ./
COPY src/ ./src/
# `migrations` is a STATIC wheel force-include (pyproject.toml
# `[tool.hatch.build.targets.wheel.force-include]`) — core/db.py's schema
# lives here at the repo root, outside `packages = ["src/no_human"]`, so the
# wheel build fails outright without it even though this Action never opens
# a database (see ci_action/__init__.py's module docstring).
COPY migrations/ ./migrations/
# hatch_build.py's board hook fails a STANDARD (non-editable) wheel build if
# `web/dist` is missing outright — see hatch_build.py's module docstring. It
# treats `web/dist` present with `web/src` absent as an "sdist-to-wheel tree"
# (a release artifact, not a dev checkout) and requires nothing further: no
# npm, no board content. This Action ships no dashboard, so a placeholder is
# exactly that tree shape — never a real board, never built with npm.
RUN mkdir -p web/dist \
    && printf '<!doctype html><title>no_human CI review gate — no board in this image</title>' > web/dist/index.html
RUN pip install --no-cache-dir .

# A throwaway HOME inside the container — never the runner's real $HOME, and
# never ~/.no_human: this Action is one-shot and must not read or create a
# subscription/database directory that could carry state between runs.
ENV HOME=/tmp/nh-action-home
RUN mkdir -p "$HOME"

ENTRYPOINT ["python", "-m", "no_human.ci_action.run"]
