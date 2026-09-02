#!/usr/bin/env bash
#
# deploy.sh -- push sources to the build host, compile, and copy the binaries
# to the camera node.
#
# The camera node (Raspberry Pi 4, Debian 13) has no cmake, so everything is
# compiled once on the build host (Raspberry Pi 5, Debian 12) and the binaries
# are copied over: both are aarch64, and the older glibc on the build host links
# forward-compatibly onto the newer one.  libcoap is statically linked, so the
# camera node needs no libcoap install at all.

set -euo pipefail

BUILD_HOST=${BUILD_HOST:-hafidz@smarthome}   # has cmake + libcoap
CAM_HOST=${CAM_HOST:-yb@serveryb}            # camera node
REMOTE_DIR=${REMOTE_DIR:-coap-video}

cd "$(dirname "$0")/.."

echo "==> syncing sources to $BUILD_HOST"
ssh "$BUILD_HOST" "mkdir -p ~/$REMOTE_DIR/src ~/$REMOTE_DIR/bench ~/$REMOTE_DIR/results"
scp -q src/cvid.h src/frame_source.h src/frame_source.c \
       src/coap_video_server.c src/coap_video_client.c \
       "$BUILD_HOST:~/$REMOTE_DIR/src/"
scp -q Makefile "$BUILD_HOST:~/$REMOTE_DIR/"

echo "==> building on $BUILD_HOST"
ssh "$BUILD_HOST" "cd ~/$REMOTE_DIR && make -s"

echo "==> copying binaries to $CAM_HOST"
ssh "$CAM_HOST" "mkdir -p ~/$REMOTE_DIR/bin"
tmp=$(mktemp -d)
scp -q "$BUILD_HOST:~/$REMOTE_DIR/bin/coap-video-server" "$tmp/"
scp -q "$BUILD_HOST:~/$REMOTE_DIR/bin/coap-video-client" "$tmp/"
scp -q "$tmp/coap-video-server" "$tmp/coap-video-client" "$CAM_HOST:~/$REMOTE_DIR/bin/"
ssh "$CAM_HOST" "chmod +x ~/$REMOTE_DIR/bin/coap-video-server ~/$REMOTE_DIR/bin/coap-video-client"
rm -rf "$tmp"

echo "==> done"
ssh "$CAM_HOST"   "~/$REMOTE_DIR/bin/coap-video-server --help 2>&1 | head -1"
ssh "$BUILD_HOST" "~/$REMOTE_DIR/bin/coap-video-client --help 2>&1 | head -1"
