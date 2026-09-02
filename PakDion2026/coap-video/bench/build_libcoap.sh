#!/usr/bin/env bash
#
# build_libcoap.sh -- build libcoap with RFC 9177 (Q-Block) support enabled.
#
# Run this on the machine that will compile the video tools.  It installs into
# ~/.local, so no root is needed.  Q-Block is ON by default in libcoap but is
# passed explicitly here so the build is self-documenting.
#
# Only cmake, make and a C compiler are required -- DTLS/OSCORE/WebSockets are
# switched off to keep the dependency set to libc alone, which also means the
# resulting binaries can be copied to another aarch64 Pi that has no toolchain.

set -euo pipefail

PREFIX=${PREFIX:-$HOME/.local}
SRC=${SRC:-$HOME/src/libcoap}
REPO=${REPO:-https://github.com/obgm/libcoap.git}
JOBS=${JOBS:-$(nproc)}

if [ ! -d "$SRC/.git" ]; then
  mkdir -p "$(dirname "$SRC")"
  git clone --depth 50 "$REPO" "$SRC"
fi

cd "$SRC"
echo "libcoap at $(git log --oneline -1)"

rm -rf build
mkdir -p build
cd build

cmake .. \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX="$PREFIX" \
  -DENABLE_Q_BLOCK=ON \
  -DENABLE_DTLS=OFF \
  -DENABLE_OSCORE=OFF \
  -DENABLE_WS=OFF \
  -DENABLE_EXAMPLES=ON \
  -DENABLE_DOCS=OFF \
  -DENABLE_TESTS=OFF \
  -DBUILD_SHARED_LIBS=OFF \
  -DENABLE_SERVER_MODE=ON \
  -DENABLE_CLIENT_MODE=ON

make -j"$JOBS"
make install

echo
echo "installed into $PREFIX"
grep -q '#define COAP_Q_BLOCK_SUPPORT 1' "$PREFIX/include/coap3/coap_defines.h" 2>/dev/null \
  && echo "Q-Block (RFC 9177): ENABLED" \
  || echo "Q-Block (RFC 9177): check $PREFIX/include/coap3/coap_defines.h"
