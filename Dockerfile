# Agentic Platform Engineering Extravaganza
#
# Two stages: fetch the pinned upstream binaries, then a slim runtime that has
# Python, PyYAML and nothing else. The binaries are the real upstream release
# artefacts — the image does not reimplement any of them.

FROM debian:bookworm-slim AS tools
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl tar \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY bin/setup.sh bin/setup.sh
RUN chmod +x bin/setup.sh && ./bin/setup.sh --all && ./bin/setup.sh --check


FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="agentic-platform-engineering-extravaganza"
LABEL org.opencontainers.image.description="Agentic platform engineering on entirely free and open-source tooling: golden path, Score, Crossplane-shaped platform API, OPA policy gates, and an authz-gated MCP server."
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.source="https://github.com/adventurewave-labs/agentic-platform-engineering-extravaganza"

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir PyYAML==6.0.2

WORKDIR /app
COPY --from=tools /src/bin/ /app/bin/
COPY . /app
RUN chmod +x /app/run.sh /app/bin/setup.sh /app/scripts/verify.sh \
 && rm -rf /app/workspace/* \
 && useradd -u 10001 -m northwind \
 && chown -R northwind:northwind /app
USER 10001

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PATH="/app/bin:${PATH}" \
    NORTHWIND_IDENTITY=platform-agent

EXPOSE 8080 8099

# `demo` runs the eight acts. Override with: site | mcp | verify | tools | drift
ENTRYPOINT ["/app/run.sh"]
CMD ["demo"]
