#!/bin/sh
set -eu
SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PYTHONPATH="$SOURCE_DIR"
exec /usr/bin/python3 -m mintbot_desktop.lifecycle uninstall
