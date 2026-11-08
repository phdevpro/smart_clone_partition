#!/usr/bin/env bash
# Builds dist/smartclone.iso: a Debian live system that boots straight into the
# SmartClone menu. Needs Docker (live-build runs in a privileged Debian container),
# internet access and ~4 GB of free disk. Downloaded packages are cached in .build/.
set -euo pipefail
cd "$(dirname "$0")/.."
docker run --rm --privileged -v "$PWD":/src debian:trixie \
    bash /src/live/lb-build.sh "$(id -u):$(id -g)"
ls -lh dist/smartclone.iso
