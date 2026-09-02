#!/usr/bin/env bash
# Drives the three experiments in sequence; they share one camera so they
# cannot overlap.
set -u
cd "$(dirname "$0")/.."

echo "############ E1: resolution sweep (no loss) ############"
MECHS="7959 9177" RESOLUTIONS="640x480 1280x720 1920x1080" BLOCKS="1024" \
LOSSES="0" FRAMES=100 WARMUP=5 OUT=results/e1_resolution.csv RAW=results/raw \
  bash bench/run_matrix.sh

echo
echo "############ E2: block size sweep (640x480, no loss) ############"
MECHS="7959 9177" RESOLUTIONS="640x480" BLOCKS="128 256 512 1024" \
LOSSES="0" FRAMES=100 WARMUP=5 OUT=results/e2_blocksize.csv RAW=results/raw \
  bash bench/run_matrix.sh

echo
echo "############ E3: packet loss sweep (640x480) ############"
MECHS="7959 9177" RESOLUTIONS="640x480" BLOCKS="1024" \
LOSSES="0 2 5 10 20" FRAMES=30 WARMUP=3 TIMEOUT=6 \
OUT=results/e3_loss.csv RAW=results/raw \
  bash bench/run_matrix.sh
