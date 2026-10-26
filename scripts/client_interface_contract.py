# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Extract or compare the Platform's Client Interface OpenAPI contract.

The Worker never authors this contract. ``extract`` copies, unchanged, the
operations the Platform marks with ``x-nature-client-interface`` together with
every schema and security scheme they reference. ``compare`` reports whether
two documents (for example the live Platform OpenAPI and the Platform
repository's committed ``docs/client-interface-v1.openapi.json``) define the
same Client Interface.

    python -I scripts/client_interface_contract.py extract LIVE.json OUT.json
    python -I scripts/client_interface_contract.py compare A.json B.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

MARKER = "x-nature-client-interface"
METHODS = ("get", "post", "put", "patch", "delete")


def _refs(node, found: set[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            found.add(ref.rsplit("/", 1)[1])
        for value in node.values():
            _refs(value, found)
    elif isinstance(node, list):
        for value in node:
            _refs(value, found)


def client_interface(document: dict) -> dict:
    paths: dict = {}
    for path, item in document.get("paths", {}).items():
        for method in METHODS:
            operation = item.get(method)
            if isinstance(operation, dict) and MARKER in operation:
                paths.setdefault(path, {})[method] = operation
    schemas = document.get("components", {}).get("schemas", {})
    wanted: set[str] = set()
    _refs(paths, wanted)
    done: set[str] = set()
    while wanted - done:
        name = sorted(wanted - done)[0]
        done.add(name)
        _refs(schemas[name], wanted)
    return {
        "openapi": document["openapi"],
        "info": document["info"],
        "paths": paths,
        "components": {
            "schemas": {name: schemas[name] for name in sorted(done)},
            "securitySchemes": document.get("components", {}).get("securitySchemes", {}),
        },
    }


def canonical(document: dict) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    ext = sub.add_parser("extract")
    ext.add_argument("source", type=Path)
    ext.add_argument("output", type=Path)
    cmp_ = sub.add_parser("compare")
    cmp_.add_argument("a", type=Path)
    cmp_.add_argument("b", type=Path)
    args = parser.parse_args(argv)

    if args.command == "extract":
        raw = args.source.read_bytes()
        subset = client_interface(json.loads(raw))
        args.output.write_text(json.dumps(subset, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        count = sum(len(ops) for ops in subset["paths"].values())
        print(json.dumps({
            "operations": count,
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "canonical_sha256": hashlib.sha256(canonical(subset)).hexdigest(),
        }, indent=2))
        return 0

    a = client_interface(json.loads(args.a.read_bytes()))
    b = client_interface(json.loads(args.b.read_bytes()))
    ops_a = {(m.upper(), p, o.get("operationId")) for p, ops in a["paths"].items() for m, o in ops.items()}
    ops_b = {(m.upper(), p, o.get("operationId")) for p, ops in b["paths"].items() for m, o in ops.items()}
    same = canonical(a) == canonical(b)
    print(json.dumps({
        "identical": same,
        "operations": [len(ops_a), len(ops_b)],
        "only_in_a": sorted(map(list, ops_a - ops_b)),
        "only_in_b": sorted(map(list, ops_b - ops_a)),
        "schemas_differing": sorted(
            name for name in set(a["components"]["schemas"]) | set(b["components"]["schemas"])
            if a["components"]["schemas"].get(name) != b["components"]["schemas"].get(name)
        ),
        "security_schemes_equal": a["components"]["securitySchemes"] == b["components"]["securitySchemes"],
    }, indent=2))
    return 0 if same else 1


if __name__ == "__main__":
    sys.exit(main())
