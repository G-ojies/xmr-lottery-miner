#!/usr/bin/env bash
#
# build_randomx.sh — build the real RandomX backend (pyrx) into ./.venv
#
# The miner runs in DEMO mode without this (BLAKE2b stand-in; pool rejects
# shares). Run this once to get real, pool-valid proof-of-work.
#
# Why it's not just `pip install pyrx`:
#   * The PyPI package named "pyrx" is an UNRELATED JSON-schema validator.
#   * The real RandomX bindings live at github.com/jtgrassie/pyrx and build
#     from C++ source. On Python 3.11+ that repo's vendored pybind11 (v2.4.3)
#     is too old to compile, and newer GCC needs an extra <cstdint> include.
#   * CMake 4.x rejects RandomX's old `cmake_minimum_required`, so we pin <4.
#
# This script applies those fixes against an isolated checkout and installs the
# resulting extension into the project venv. Needs: git + a C/C++ toolchain.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$ROOT/.venv"
PYBIND11_TAG="v2.13.6"   # first pybind11 line that compiles cleanly on Python 3.13
BUILD_DIR="${TMPDIR:-/tmp}/pyrx-build-$$"

echo "==> ensuring venv at $VENV"
[ -d "$VENV" ] || python3 -m venv "$VENV"

echo "==> installing build tooling (cmake<4, setuptools, wheel) + runtime deps"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet rich psutil setuptools wheel "cmake<4"

# The cmake pip package ships a real native binary under data/bin; the console
# shim in venv/bin breaks inside pip's isolated build env, so use the binary.
CMAKE_BIN_DIR="$("$VENV/bin/python" -c \
  'import cmake, os; print(os.path.join(os.path.dirname(cmake.__file__), "data", "bin"))')"
echo "==> using cmake: $("$CMAKE_BIN_DIR/cmake" --version | head -1)"

echo "==> cloning jtgrassie/pyrx (recursive) into $BUILD_DIR"
rm -rf "$BUILD_DIR"
git clone --quiet --recursive https://github.com/jtgrassie/pyrx "$BUILD_DIR"

echo "==> bumping vendored pybind11 to $PYBIND11_TAG"
git -C "$BUILD_DIR/external/pybind11" fetch --quiet --depth 1 origin tag "$PYBIND11_TAG"
git -C "$BUILD_DIR/external/pybind11" checkout --quiet "$PYBIND11_TAG"

echo "==> patching RandomX test header (add <cstdint>) and C++ standard"
UTIL="$BUILD_DIR/external/RandomX/src/tests/utility.hpp"
grep -q '#include <cstdint>' "$UTIL" || \
  sed -i 's@#include <cstdlib>@#include <cstdlib>\n#include <cstdint>@' "$UTIL"
sed -i 's/set(CMAKE_CXX_STANDARD 11)/set(CMAKE_CXX_STANDARD 17)/' "$BUILD_DIR/CMakeLists.txt"

echo "==> building + installing pyrx into the venv (this compiles RandomX; takes a few minutes)"
( cd "$BUILD_DIR" && PATH="$CMAKE_BIN_DIR:$PATH" "$VENV/bin/pip" install --no-build-isolation . )

echo "==> verifying"
"$VENV/bin/python" - <<'PY'
import xmr_miner_core as core
assert core.HAVE_RANDOMX, "pyrx imported but get_rx_hash missing"
print("OK — HAVE_RANDOMX =", core.HAVE_RANDOMX)
PY

rm -rf "$BUILD_DIR"
echo
echo "Done. Run the miner with real PoW:  $VENV/bin/python main.py"
