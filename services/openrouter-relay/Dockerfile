# syntax=docker/dockerfile:1
#
# openrouter-relay - the only service with Internet egress.
#
# Its whole purpose is to be narrow: one upstream URL, one model, no
# forwarding. See public-documentation/DESIGN.md section 4.6. The credential
# lives here and nowhere else, delivered as a file so it never appears in the
# environment.

ARG PYTHON_BASE=python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a

FROM ${PYTHON_BASE}

RUN groupadd --gid 10003 relay \
 && useradd --uid 10003 --gid 10003 --no-create-home --shell /usr/sbin/nologin relay

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt

COPY src/ /opt/relay/src/

ENV PYTHONPATH=/opt/relay/src \
    PYTHONUNBUFFERED=1 \
    RELAY_PORT=8080 \
    RELAY_API_KEY_FILE=/run/secrets/openrouter_api_key

USER 10003:10003
WORKDIR /opt/relay
EXPOSE 8080

# Probes whatever RELAY_BIND names, because Phase 7 binds this listener to the
# agent-facing network address rather than 0.0.0.0; a check hardcoded to
# loopback would then fail against a healthy relay.
HEALTHCHECK --interval=20s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request,sys; h=os.environ.get('RELAY_BIND','127.0.0.1'); h='127.0.0.1' if h=='0.0.0.0' else h; p=os.environ.get('RELAY_PORT','8080'); sys.exit(0 if urllib.request.urlopen(f'http://{h}:{p}/healthz',timeout=3).status==200 else 1)"

CMD ["python", "-m", "openrouter_relay.app"]
