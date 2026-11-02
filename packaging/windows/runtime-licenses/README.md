<!-- SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
# Licenses of the bundled Python runtime

The `install_only` runtime archive the installer ships contains only CPython's `LICENSE.txt`
(PSF, Microsoft Visual C++ runtime conditions, bzip2, Tcl/Tk/Tix). The libraries compiled into
it are licensed separately; python-build-standalone ships their license texts only in its "full"
archives. These files are copied unchanged from `python/licenses/` of

    cpython-3.12.15+20261009-x86_64-pc-windows-msvc-pgo-full.tar.zst
    sha256 9c2898a218c233d3de889d6aede2fdd859c07a51bfbb360f19bb660536f74672
    (equal to the digest GitHub publishes for that asset; checked 2026-10-28)

the same build as the pinned runtime (`../runtime.json`). Selected by that archive's
`PYTHON.json` (`license_paths` of the Windows extensions: bzip2, libffi, liblzma, libuuid,
OpenSSL 3, SQLite, Tcl, Tix), plus CPython itself and the libraries built in without a listed
path (zlib, expat, mpdecimal). Texts for libraries not in the Windows build (X11, ncurses,
libedit, Berkeley DB, OpenSSL 1.1) are left out.

Update these files whenever `runtime.json` changes.
