# syntax=docker/dockerfile:1
#
# dikumud - the pinned original DikuMUD (Alfa) game server.
#
# Trust boundary: this service owns the world and player data and talks only to
# mud-control over net_mcp_game. It has no egress, no secrets, and no public
# port. See public-documentation/DESIGN.md sections 4.1 and 5.2.

ARG DEBIAN_BASE=debian:bookworm-slim@sha256:abd67ffcfa541b485a3dff59865ab629aa048a6c613e639d36e7456b0b229241

# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------
FROM ${DEBIAN_BASE} AS builder

# Pinned upstream revision. The commit hash is the content address, so the
# checkout is verified against it rather than against a regenerated tarball.
ARG DIKUMUD_COMMIT=81b74dce0436b782d08b19064e32013c73525b45
ARG DIKUMUD_REPO=https://github.com/Seifert69/DikuMUD.git

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential libcrypt-dev patch git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN git init -q . \
 && git remote add origin "${DIKUMUD_REPO}" \
 && git fetch -q --depth 1 origin "${DIKUMUD_COMMIT}" \
 && git checkout -q FETCH_HEAD \
 && test "$(git rev-parse HEAD)" = "${DIKUMUD_COMMIT}" \
 && echo "verified upstream commit ${DIKUMUD_COMMIT}"

# Compatibility patches only. Each is documented in patches/README.md; none
# alters game rules, world content, or the command set.
COPY patches/ /patches/
RUN cd /build/dm-dist-alfa \
 && for p in /patches/0*.patch; do \
      echo "applying $(basename "$p")"; \
      patch -p1 --batch --forward < "$p"; \
    done \
 && make \
 && test -x dmserver && test -x delplay

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
FROM ${DEBIAN_BASE}

RUN apt-get update \
 && apt-get install -y --no-install-recommends libcrypt1 \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10001 dikumud \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin dikumud

# Binaries, and a pristine copy of the stock data directory used to seed the
# volume on first start. The live data directory is left empty on purpose so
# seeding is explicit rather than relying on Docker's volume auto-populate.
COPY --from=builder /build/dm-dist-alfa/dmserver /build/dm-dist-alfa/delplay /opt/dikumud/bin/
COPY --from=builder /build/dm-dist-alfa/lib/ /opt/dikumud/lib-dist/

# Upstream license notices, preserved as required by the LGPL election and the
# original DikuMUD license. See THIRD_PARTY_NOTICES.md.
COPY --from=builder /build/LICENSE /opt/dikumud/licenses/LICENSE
COPY --from=builder /build/read.me /opt/dikumud/licenses/read.me
COPY --from=builder /build/dm-dist-alfa/doc/ /opt/dikumud/licenses/doc/

COPY entrypoint.sh /opt/dikumud/bin/entrypoint.sh

RUN chmod 0755 /opt/dikumud/bin/entrypoint.sh \
 && mkdir -p /var/lib/dikumud/data \
 && chown -R 10001:10001 /var/lib/dikumud

USER 10001:10001
WORKDIR /var/lib/dikumud
VOLUME ["/var/lib/dikumud/data"]
EXPOSE 4000

# Confirms the game is actually accepting connections, not merely that the
# process exists. Uses bash's /dev/tcp so no extra package is required.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
  CMD bash -c 'exec 3<>/dev/tcp/127.0.0.1/4000' || exit 1

ENTRYPOINT ["/opt/dikumud/bin/entrypoint.sh"]
