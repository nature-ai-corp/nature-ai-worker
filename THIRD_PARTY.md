# Third-party dependencies

Runtime dependencies installed by `pip install .`, pinned in `requirements.lock`, with the license metadata declared by each installed package. Re-verified on 2026-10-26 from the installed package metadata with `python scripts/repo_checks.py --print-licenses` (CI runs `--licenses`, which fails on an unknown or incompatible license and on a pinned package missing from this table).

All runtime dependencies are under permissive licenses compatible with distributing this project under Apache-2.0, except `certifi`, which is MPL-2.0. MPL-2.0 is a file-level copyleft: using certifi unmodified as a separate library, as this project does, is permitted. Modifying certifi's own files would oblige us to publish those modifications under MPL-2.0.

No third-party code or assets are copied into this repository; the web UI uses no external libraries, fonts or images.

## Distribution notes

- **Release bundles** (see [docs/RELEASES.md](docs/RELEASES.md)) contain the dependency wheels below, unmodified, each with its own license files. Publishing a bundle is a redistribution of those packages, which all of these licenses permit. The `cryptography` wheels include a statically linked OpenSSL (4.0.3 in cryptography 50.0.2); OpenSSL is Apache-2.0 since version 3.0.
- **Windows bundles** additionally contain `colorama` (BSD-3-Clause), which `click` requires on Windows only, so it is absent from the Linux-generated `requirements.lock`. Bundles are built on their target OS so that such platform-specific packages are included.
- **Docker images.** The `Dockerfile` builds on `python:3.14-slim` (Debian), whose system packages are under many licenses, including GPL. Building an image locally raises no issue. Publishing a pre-built image is a distribution of those packages and needs the base image's license and source-availability obligations to be met. **No image is published by this repository.**
- **Platform contract.** `src/nature_worker/contracts/client-interface-v1.openapi.json` is an interface description extracted from the NATURE AI Platform's own OpenAPI document. It is not third-party material, but it is NATURE AI Platform material. Including it here publishes it under this repository's license; see [docs/OPEN_SOURCE_PROTECTION.md](docs/OPEN_SOURCE_PROTECTION.md).

## Runtime dependencies

| Package | Version | License |
|---|---|---|
| annotated-doc | 0.0.5 | MIT |
| annotated-types | 0.8.0 | MIT |
| anthropic | 1.15.0 | MIT License |
| anyio | 4.15.1 | MIT |
| certifi | 2026.7.22 | Mozilla Public License 2.0 (MPL 2.0) |
| cffi | 2.1.1 | MIT-0 |
| charset-normalizer | 3.5.2 | MIT |
| click | 8.5.0 | BSD-3-Clause |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause |
| distro | 1.9.0 | Apache Software License |
| docstring_parser | 0.18.0 | MIT License |
| fastapi | 0.143.3 | MIT |
| google-auth | 2.62.0 | Apache Software License |
| google-genai | 2.31.0 | Apache-2.0 |
| h11 | 0.16.0 | MIT License |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpcore2 | 2.13.1 | BSD-3-Clause |
| httpx | 0.28.1 | BSD License |
| httpx2 | 2.13.1 | BSD-3-Clause |
| idna | 3.20 | BSD-3-Clause |
| jiter | 0.17.0 | MIT |
| openai | 3.33.0 | Apache-2.0 |
| opentelemetry-api | 1.45.1 | Apache-2.0 |
| pyasn1 | 0.6.4 | BSD-2-Clause |
| pyasn1_modules | 0.4.2 | BSD License |
| pycparser | 3.11 | BSD-3-Clause |
| pydantic | 2.14.1 | MIT |
| pydantic_core | 2.50.1 | MIT |
| requests | 2.34.2 | Apache Software License |
| sniffio | 1.3.1 | MIT License; Apache Software License |
| starlette | 1.8.0 | BSD-3-Clause |
| tenacity | 9.1.4 | Apache Software License |
| truststore | 0.10.5 | MIT |
| typing-inspection | 0.4.4 | MIT |
| typing_extensions | 4.16.0 | PSF-2.0 |
| urllib3 | 2.8.0 | MIT |
| uvicorn | 0.54.0 | BSD-3-Clause |
| websockets | 16.1.1 | BSD-3-Clause |

## Development and test dependencies (not distributed)

Pinned in `requirements-dev.lock`; used only to run the tests.

| Package | Version | License |
|---|---|---|
| iniconfig | 2.3.1 | MIT |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| pluggy | 1.7.0 | MIT License |
| Pygments | 2.21.0 | BSD-2-Clause |
| pytest | 9.1.1 | MIT |
