# Names and logo: usage guidelines

This page explains how the names **NATURE AI** and **Nature AI Worker**, and the "N" logo mark, may be used together with this open-source software.

> **Legal status, stated plainly.** No trademark registration for these names or the logo is claimed in this repository. The maintainers use them to identify their software and official releases, and they ask you to follow these guidelines. Questions about rights in a specific country go to the maintainers (see [Contact](#contact)); this page is not legal advice.

## What the Apache License gives you, and what it does not

The source code is licensed under the [Apache License 2.0](LICENSE). That license gives you broad rights, and these guidelines do not take any of them away. You may:

- use the software for any purpose, including commercial use;
- study, modify and build on it;
- distribute it, modified or not, free or for a fee;
- fork it and run your own project.

Section 6 of the license says that it does **not** grant permission to use the licensor's trade names, trademarks, service marks or product names, *except as required for reasonable and customary use in describing the origin of the Work and reproducing the content of the NOTICE file*. These guidelines describe that boundary for this project.

## Uses that are always fine

- Saying truthfully that your software is **based on**, **derived from**, **compatible with** or **works with** Nature AI Worker. For example: "Acme Assistant, based on Nature AI Worker".
- Referring to the project by name in articles, talks, documentation, reviews, tutorials and comparisons.
- Distributing **unmodified** official releases under their own name, with the LICENSE and NOTICE files kept intact.
- Keeping the LICENSE, NOTICE and copyright/SPDX headers in modified copies, which the Apache License requires anyway (Section 4).

## Uses that need the maintainers' permission

- Naming a modified version, a fork, a product, a company, or a domain **Nature AI Worker**, **NATURE AI**, or something confusingly similar (for example "Nature AI Worker Pro" or "NatureAI Enterprise Worker").
- Using the "N" logo mark, or the NATURE AI wordmark styling in the user interface, for a modified version or another product.
- Presenting a build, a package, a container image or a download as **official**, **certified** or **endorsed** by the NATURE AI maintainers, or implying a partnership that does not exist.
- Using the names in a way that suggests that a modified version is the NATURE AI Platform or connects to it on the maintainers' behalf.

## Modified versions

If you distribute a modified version:

1. Give it a different name. "Based on Nature AI Worker" in its description is fine.
2. Replace the "N" logo mark and the "NATURE AI / Worker" wordmark in the user interface (`src/nature_worker/static/index.html`, `src/nature_worker/platform_client/static/index.html`) with your own branding.
3. Keep the LICENSE and NOTICE files, keep the copyright and SPDX headers in files you changed, and add a notice that you changed them (Apache License, Section 4(b)).
4. Do not sign your builds so that they look like official releases (see [docs/RELEASES.md](docs/RELEASES.md)). An installation trusts a release only if it is signed by a key that the installation trusts. Your own keys are always reported as "not an official NATURE AI release".

## Official releases

Only releases identified as described in [docs/RELEASES.md](docs/RELEASES.md) are official NATURE AI releases. A build that is not identified that way is not official, whatever it is called.

## Contact

Ask for permission or report misuse through the repository's issue tracker. For anything sensitive, use the private channel in [SECURITY.md](SECURITY.md).
