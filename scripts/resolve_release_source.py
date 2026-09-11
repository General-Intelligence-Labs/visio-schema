"""Resolve and validate the immutable source used by the release workflow."""

import os
import re
import subprocess
import tomllib

tagged = os.environ["GITHUB_REF"].startswith("refs/tags/")
tag = os.environ["GITHUB_REF_NAME"] if tagged else os.environ["REQUESTED_TAG"]
publish = tagged or os.environ["REQUESTED_PUBLISH"] == "true"
if publish and not tag:
    raise SystemExit("Publishing requires an existing release tag")
if tag and not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag):
    raise SystemExit("Expected a vX.Y.Z release tag")
ref = f"refs/tags/{tag}" if tag else os.environ["GITHUB_SHA"]
sha = subprocess.check_output(
    ["git", "rev-parse", "--verify", ref + "^{commit}"], text=True
).strip()
config = subprocess.check_output(
    ["git", "show", f"{sha}:python/pyproject.toml"], text=True
)
version = tomllib.loads(config)["project"]["version"]
if tag and tag != f"v{version}":
    raise SystemExit(f"Tag {tag} does not match package version {version}")
with open(os.environ["GITHUB_OUTPUT"], "a") as out:
    out.write(f"sha={sha}\ntag={tag}\npublish={str(publish).lower()}\n")
print(f"Building {sha}; tag={tag}; publish={publish}")
