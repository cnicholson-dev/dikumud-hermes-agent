# Third-Party Notices

This project bundles or builds the following third-party components. All notices
below were read from the pinned upstream revisions listed in
`public-documentation/DEPENDENCY_RECORD.md`, not from secondary sources.

The project's own code is a separate matter from everything below: it is
Copyright (C) 2026 Cody Nicholson under LGPL-2.1-only, and the root `LICENSE`
governs it. The election recorded in section 1 is the choice this project makes
within DikuMUD's own dual option, not the licence of the original code, though
the two are the same version.

## 1. DikuMUD (service: `dikumud`)

- Upstream: https://github.com/Seifert69/DikuMUD
- Pinned commit: `81b74dce0436b782d08b19064e32013c73525b45` (2020-12-07)
- License: dual option, LGPL-2.1 **or** the original DikuMUD license.

`dm-dist-alfa/doc/license.doc` records that as of February 3rd, 2020 the authors
Sebastian Hammer, Hans-Henrik Starfeldt, Katja Nyboe, and Michael Seifert agreed
to make their DikuMUD work available under the LGPL, and states: "This means you
can choose yourself if you want to use DikuMUD under the LGPL or the original
license." The same file records that the authors were unable to reach Tom Madsen,
the fifth original author, though they state they are "fairly confident that he
would likewise agree."

**Election for this project: LGPL-2.1**, using the repository's root `LICENSE`
(GNU Lesser General Public License, Version 2.1, February 1999).

Regardless of that election, this project preserves:

- The upstream `LICENSE` file (LGPL-2.1 full text).
- `dm-dist-alfa/doc/license.doc` and `dm-dist-alfa/doc/license_original.doc`.
- All in-source copyright notices.
- The stock login sequence and `credits` command content, unmodified.

The original license (retained at `dm-dist-alfa/doc/license_original.doc`)
additionally requires that copyright notices are not removed, that the authors
appear in the login sequence, that the `credits` command names them, that the
license travels with any copy, and that the authors be notified by mail or e-mail
before publishing or running a version. It also forbids making a profit on any
part of DikuMUD. This project does not strip the credits or login notices, and is
a non-commercial supervised portfolio demonstration, so it remains consistent
with the original terms as well as the LGPL election.

Any compatibility change required to build the 1990-era source on a modern
toolchain is kept as an explicit patch file under `services/dikumud/patches/` and
is itself covered by LGPL-2.1. Patches are never folded into the upstream source
tree.

## 2. TinTin++ (service: `mud-control`)

- Upstream: https://github.com/scandum/tintin
- Pinned tag `2.02.61`, commit `010881e7689c5328096f0b63002b898104cd72f1`
  (2026-01-29)
- License: GNU General Public License, Version 3, 29 June 2007, or (at your
  option) any later version. Verified in `COPYING` and in the per-file header of
  `src/tintin.h`.

TinTin++ is built from unmodified upstream source and executed as a **separate
process** under a pseudo-terminal. It is not linked into, imported by, or
statically combined with this project's Python code. The two communicate only
through the PTY, which is arm's-length inter-process communication; the Python
sources in this repository are therefore not derivative works of TinTin++, and
their presence in the same container image is mere aggregation.

The GPL-3.0 obligations that do attach on distribution of the `mud-control` image
are: preserve `COPYING` and the copyright notices, and make the corresponding
source of the TinTin++ binary available to recipients. Because the build is
pinned to the tag above, the corresponding source is the upstream tree at that
exact commit. **If the `mud-control` image is ever pushed to a public registry, a
written offer or a source copy must accompany it.** A locally built,
undistributed image does not trigger the distribution obligation.

## 3. Hermes Agent (service: `hermes-player`)

- Upstream: https://github.com/NousResearch/hermes-agent
- Pinned release tag: `v2026.8.18` ("Hermes Agent v0.20.4"), commit
  `e624e9fde561e1add9388384012b295fde669ade`
- License: MIT

Used as a published container image; see the dependency record for the digest.
Preserve the upstream `LICENSE` when redistributing.

## 4. Model Context Protocol Python SDK (service: `mud-control`)

- Upstream: https://github.com/modelcontextprotocol/python-sdk
- Pinned version: `mcp==2.0.0` (PyPI, released 2026-07-28)
- License: MIT

## 5. Python dependencies

All transitive Python dependencies resolve to permissive licenses. No copyleft
license appears anywhere in either service's locked dependency set. Versions and
licenses below were read from the installed distribution metadata of the
hash-verified locks, not from package documentation.

### `mud-control` (from `services/mud-control/requirements.txt`)

| Package | Version | License |
| --- | --- | --- |
| `annotated-types` | 0.8.0 | MIT |
| `anyio` | 4.14.2 | MIT |
| `attrs` | 26.1.0 | MIT |
| `cffi` | 2.1.1 | MIT-0 |
| `click` | 8.4.2 | BSD-3-Clause |
| `cryptography` | 50.0.0 | Apache-2.0 OR BSD-3-Clause |
| `h11` | 0.16.0 | MIT |
| `httpcore2` | 2.12.0 | BSD-3-Clause |
| `httpx2` | 2.12.0 | BSD-3-Clause |
| `idna` | 3.19 | BSD-3-Clause |
| `jsonschema` | 4.26.0 | MIT |
| `jsonschema-specifications` | 2025.9.1 | MIT |
| `mcp` | 2.0.0 | MIT |
| `mcp-types` | 2.0.0 | MIT |
| `opentelemetry-api` | 1.44.0 | Apache-2.0 |
| `pycparser` | 3.0 | BSD-3-Clause |
| `pydantic` | 2.13.4 | MIT |
| `pydantic_core` | 2.46.4 | MIT |
| `PyJWT` | 2.13.0 | MIT |
| `python-multipart` | 0.0.32 | Apache-2.0 |
| `referencing` | 0.37.0 | MIT |
| `rpds-py` | 2026.6.3 | MIT |
| `sse-starlette` | 3.4.8 | BSD-3-Clause |
| `starlette` | 1.6.0 | BSD-3-Clause |
| `truststore` | 0.10.4 | MIT |
| `typing-inspection` | 0.4.4 | MIT |
| `typing_extensions` | 4.16.0 | PSF-2.0 |
| `uvicorn` | 0.52.4 | BSD-3-Clause |

### `openrouter-relay` (from `services/openrouter-relay/requirements.txt`)

| Package | Version | License |
| --- | --- | --- |
| `annotated-types` | 0.8.0 | MIT |
| `anyio` | 4.14.2 | MIT |
| `click` | 8.4.2 | BSD-3-Clause |
| `h11` | 0.16.0 | MIT |
| `httpcore2` | 2.12.0 | BSD-3-Clause |
| `httpx2` | 2.12.0 | BSD-3-Clause |
| `idna` | 3.19 | BSD-3-Clause |
| `pydantic` | 2.13.4 | MIT |
| `pydantic_core` | 2.46.4 | MIT |
| `starlette` | 1.6.0 | BSD-3-Clause |
| `truststore` | 0.10.4 | MIT |
| `typing-inspection` | 0.4.4 | MIT |
| `typing_extensions` | 4.16.0 | PSF-2.0 |
| `uvicorn` | 0.52.4 | BSD-3-Clause |

## 6. Container base images

See `public-documentation/DEPENDENCY_RECORD.md` for pinned digests. Debian base
images aggregate many separately licensed packages; their notices ship inside the
images at `/usr/share/doc/*/copyright` and are preserved by not stripping those
paths during the build.

## 7. Not bundled

NVIDIA Nemotron 3 Ultra is accessed as a hosted third-party service through
OpenRouter. No model weights are distributed with this project, and its use is
governed by the OpenRouter and NVIDIA terms in force at run time.
