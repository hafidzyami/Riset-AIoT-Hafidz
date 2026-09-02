#!/usr/bin/env bash
#
# run_matrix.sh -- sweep RFC 7959 vs RFC 9177 across resolution, block size and
# packet loss, and collect one CSV row per run.
#
# Runs from any machine that can ssh to both Pis (it does not require the two
# Pis to be able to ssh to each other).
#
#   CAM_HOST   ssh target of the camera / CoAP server   (Raspberry Pi 4)
#   CLI_HOST   ssh target of the CoAP client            (Raspberry Pi 5)
#   CAM_ADDR   IP the client dials the camera node on
#
# Loss is injected on the server's transmit path with libcoap's built-in
# emulator, so it drops block payloads on the way to the client -- the exact
# condition RFC 9177 recovery is meant to handle.  No root/tc required.

set -uo pipefail

CAM_HOST=${CAM_HOST:-yb@serveryb}
CLI_HOST=${CLI_HOST:-hafidz@smarthome}
CAM_ADDR=${CAM_ADDR:-192.168.18.233}

CAM_BIN=${CAM_BIN:-\$HOME/coap-video/bin/coap-video-server}
CLI_BIN=${CLI_BIN:-\$HOME/coap-video/bin/coap-video-client}

FRAMES=${FRAMES:-100}
WARMUP=${WARMUP:-5}
FRAMERATE=${FRAMERATE:-15}
QUALITY=${QUALITY:-80}
TIMEOUT=${TIMEOUT:-10}
REPEATS=${REPEATS:-1}

# Matrix axes (override from the environment).
MECHS=${MECHS:-"7959 9177"}
RESOLUTIONS=${RESOLUTIONS:-"640x480 1280x720 1920x1080"}
BLOCKS=${BLOCKS:-"1024"}
LOSSES=${LOSSES:-"0 5 10 20"}

OUT=${OUT:-results/matrix.csv}
RAW=${RAW:-results/raw}

mkdir -p "$(dirname "$OUT")" "$RAW"

if [ ! -s "$OUT" ]; then
  echo "run,mechanism,resolution,block_size,loss_pct,repeat,frames,ok,bad,timeout,secs,fps,goodput_mbps,lat_mean,lat_p50,lat_p95,lat_p99,lat_max,dg_in,dg_out,dg_per_frame,bytes,gaps,srv_cpu_s,srv_captured,srv_served" > "$OUT"
fi

stop_server() {
  # -x matches the (15-char truncated) process name, so it can never match the
  # ssh command line that carries this script.
  ssh "$CAM_HOST" 'pkill -x coap-video-serv 2>/dev/null; sleep 0.4; exit 0' >/dev/null 2>&1
}

# start_server <rfc> <w> <h> <block> <loss>
start_server() {
  local rfc=$1 w=$2 h=$3 blk=$4 loss=$5
  local lossopt=""
  [ "$loss" != "0" ] && lossopt="--loss ${loss}%"

  ssh "$CAM_HOST" "nohup setsid $CAM_BIN --rfc $rfc --width $w --height $h \
      --framerate $FRAMERATE --quality $QUALITY --block-size $blk $lossopt \
      > /tmp/cvs.log 2>&1 < /dev/null & sleep 4; pgrep -x coap-video-serv > /tmp/cvs.pid; exit 0" \
    >/dev/null 2>&1
  ssh "$CAM_HOST" 'test -s /tmp/cvs.pid' >/dev/null 2>&1
}

# Server CPU seconds so far (utime+stime from /proc/<pid>/stat).
server_cpu() {
  ssh "$CAM_HOST" 'p=$(cat /tmp/cvs.pid 2>/dev/null); [ -n "$p" ] && \
     awk "{print (\$14+\$15)/$(getconf CLK_TCK)}" /proc/$p/stat 2>/dev/null || echo 0' 2>/dev/null
}

server_counts() {
  ssh "$CAM_HOST" 'grep -E "frames_captured|frames_served" /tmp/cvs.log 2>/dev/null | tr "\n" " "' 2>/dev/null
}

run=0
total=$(( $(echo $MECHS|wc -w) * $(echo $RESOLUTIONS|wc -w) * $(echo $BLOCKS|wc -w) * $(echo $LOSSES|wc -w) * REPEATS ))

for res in $RESOLUTIONS; do
  W=${res%x*}; H=${res#*x}
  for blk in $BLOCKS; do
    for loss in $LOSSES; do
      for mech in $MECHS; do
        for rep in $(seq 1 "$REPEATS"); do
          run=$((run+1))
          tag="${mech}_${res}_b${blk}_l${loss}_r${rep}"
          printf '[%2d/%2d] %-6s %-9s block=%-4s loss=%-3s%% ... ' \
                 "$run" "$total" "rfc$mech" "$res" "$blk" "$loss"

          stop_server
          if ! start_server "$mech" "$W" "$H" "$blk" "$loss"; then
            echo "SERVER FAILED TO START"
            continue
          fi

          cpu0=$(server_cpu)

          out=$(ssh "$CLI_HOST" "$CLI_BIN --server $CAM_ADDR --rfc $mech \
                  --block-size $blk --frames $FRAMES --warmup $WARMUP \
                  --timeout $TIMEOUT --csv /tmp/perframe_${tag}.csv" 2>/dev/null)

          cpu1=$(server_cpu)
          summary=$(echo "$out" | grep '^SUMMARY' | head -1)

          if [ -z "$summary" ]; then
            echo "NO RESULT"
            continue
          fi

          # scrape key=value pairs out of the SUMMARY line
          get() { echo "$summary" | tr ' ' '\n' | grep "^$1=" | cut -d= -f2; }

          cpu=$(awk -v a="${cpu0:-0}" -v b="${cpu1:-0}" 'BEGIN{printf "%.3f", b-a}')

          stop_server
          counts=$(ssh "$CAM_HOST" 'tail -3 /tmp/cvs.log' 2>/dev/null)
          cap=$(echo "$counts" | grep -o 'captured=[0-9]*' | cut -d= -f2 | tail -1)
          srv=$(echo "$counts" | grep -o 'served=[0-9]*'   | cut -d= -f2 | tail -1)

          echo "$run,rfc$mech,$res,$blk,$loss,$rep,$(get frames),$(get ok),$(get bad),$(get timeout),$(get secs),$(get fps),$(get goodput_mbps),$(get lat_mean),$(get lat_p50),$(get lat_p95),$(get lat_p99),$(get lat_max),$(get dg_in),$(get dg_out),$(get dg_per_frame),$(get bytes),$(get gaps),$cpu,${cap:-0},${srv:-0}" >> "$OUT"

          scp -q "$CLI_HOST:/tmp/perframe_${tag}.csv" "$RAW/" 2>/dev/null

          printf 'fps=%-7s lat_p50=%-7s dg/frame=%-6s bad=%s\n' \
                 "$(get fps)" "$(get lat_p50)" "$(get dg_per_frame)" "$(get bad)"
        done
      done
    done
  done
done

stop_server
echo
echo "matrix written to $OUT"
echo "per-frame traces in $RAW/"
