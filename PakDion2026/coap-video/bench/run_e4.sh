#!/usr/bin/env bash
# E4: RFC 9177 MAX_PAYLOADS sweep.
#
# MAX_PAYLOADS is RFC 9177's congestion window: the sender may emit that many
# non-confirmable payloads back to back, then must pause for NON_TIMEOUT unless
# the receiver gives it something to go on.  When a body needs many more blocks
# than MAX_PAYLOADS allows, the window itself becomes the bottleneck -- so sweep
# it against a body large enough to matter (1080p, ~64 blocks at 1024 B).
set -u
cd "$(dirname "$0")/.."

CAM_HOST=${CAM_HOST:-yb@serveryb}
CLI_HOST=${CLI_HOST:-hafidz@smarthome}
CAM_ADDR=${CAM_ADDR:-192.168.18.233}
SRV=\$HOME/coap-video/bin/coap-video-server
CLI=\$HOME/coap-video/bin/coap-video-client

RES=${RES:-1920x1080}
W=${RES%x*}; H=${RES#*x}
FRAMES=${FRAMES:-40}
OUT=results/e4_maxpayloads.csv

echo "mechanism,resolution,block_size,max_payloads,frames,ok,bad,timeout,secs,fps,goodput_mbps,lat_mean,lat_p50,lat_p95,lat_p99,lat_max,dg_in,dg_out,dg_per_frame" > "$OUT"

for mp in 2 5 10 20 50 100; do
  printf 'MAX_PAYLOADS=%-4s ... ' "$mp"
  ssh "$CAM_HOST" 'pkill -x coap-video-serv 2>/dev/null; sleep 0.4; exit 0' >/dev/null 2>&1
  ssh "$CAM_HOST" "nohup setsid $SRV --rfc 9177 --width $W --height $H \
      --framerate 15 --block-size 1024 --max-payloads $mp \
      > /tmp/cvs.log 2>&1 < /dev/null & sleep 4; exit 0" >/dev/null 2>&1

  out=$(ssh "$CLI_HOST" "$CLI --server $CAM_ADDR --rfc 9177 --block-size 1024 \
          --max-payloads $mp --frames $FRAMES --warmup 3 --timeout 15" 2>/dev/null)
  s=$(echo "$out" | grep '^SUMMARY' | head -1)
  get() { echo "$s" | tr ' ' '\n' | grep "^$1=" | cut -d= -f2; }
  if [ -z "$s" ]; then echo "NO RESULT"; continue; fi
  echo "rfc9177,$RES,1024,$mp,$(get frames),$(get ok),$(get bad),$(get timeout),$(get secs),$(get fps),$(get goodput_mbps),$(get lat_mean),$(get lat_p50),$(get lat_p95),$(get lat_p99),$(get lat_max),$(get dg_in),$(get dg_out),$(get dg_per_frame)" >> "$OUT"
  printf 'fps=%-8s lat_p50=%-8s lat_p99=%-9s dg_out=%s\n' "$(get fps)" "$(get lat_p50)" "$(get lat_p99)" "$(get dg_out)"
done

ssh "$CAM_HOST" 'pkill -x coap-video-serv 2>/dev/null; exit 0' >/dev/null 2>&1
echo "-> $OUT"
