#!/usr/bin/env bash
#
# E5: RFC 9177 recovery-timer tuning under loss.
#
# E3 shows that once packets start disappearing, both mechanisms become
# dominated by a *timer*, not by their transfer structure:
#
#   RFC 7959  a lost block stalls the transfer for ACK_TIMEOUT (2 s default)
#   RFC 9177  a receiver waits NON_RECEIVE_TIMEOUT (4 s default) before it asks
#             for the blocks it is missing
#
# So RFC 9177 ships with a recovery timer twice as long as the mechanism it is
# meant to improve on, which is a bad default for live video.  Its real
# advantage is that one recovery request repairs *all* the missing blocks at
# once, while RFC 7959 pays its 2 s per lost block, serially.  This sweep lowers
# the RFC 9177 timers to find where that structural advantage actually shows up.
#
# RFC 9177 section 6.2 constrains the pair:
#     NON_RECEIVE_TIMEOUT  >=  1.5 * NON_TIMEOUT + 1
# so NON_TIMEOUT has to come down with it.  The client prints the value libcoap
# actually adopted, which is recorded in the effective_nrt column.

set -u
cd "$(dirname "$0")/.."

CAM_HOST=${CAM_HOST:-yb@serveryb}
CLI_HOST=${CLI_HOST:-hafidz@smarthome}
CAM_ADDR=${CAM_ADDR:-192.168.18.233}
SRV='$HOME/coap-video/bin/coap-video-server'
CLI='$HOME/coap-video/bin/coap-video-client'

RES=${RES:-640x480}
W=${RES%x*}; H=${RES#*x}
BLOCK=${BLOCK:-1024}
LOSS=${LOSS:-10}
FRAMES=${FRAMES:-30}
TIMEOUT=${TIMEOUT:-8}

OUT=results/e5_timers.csv
echo "label,mechanism,loss_pct,non_timeout,non_receive_timeout,effective_nrt,frames,ok,bad,timeout,secs,fps,goodput_mbps,lat_mean,lat_p50,lat_p95,lat_p99,lat_max,dg_in,dg_out,dg_per_frame" > "$OUT"

# label            rfc   NON_TIMEOUT  NON_RECEIVE_TIMEOUT
ARMS="
7959-baseline      7959  -            -
9177-default       9177  2            -
9177-nrt2.0        9177  0.5          2.0
9177-nrt1.5        9177  0.3          1.5
9177-nrt1.2        9177  0.1          1.2
"

echo "E5: $RES block=${BLOCK}B loss=${LOSS}% frames=$FRAMES"
echo

printf '%s\n' "$ARMS" | while read -r label rfc nt nrt; do
  [ -z "${label:-}" ] && continue
  printf '%-18s ... ' "$label"

  srv_opts="--rfc $rfc --width $W --height $H --framerate 15 --block-size $BLOCK --loss ${LOSS}%"
  cli_opts="--rfc $rfc --block-size $BLOCK --frames $FRAMES --warmup 3 --timeout $TIMEOUT"
  if [ "$nt" != "-" ]; then
    srv_opts="$srv_opts --non-timeout $nt"
    cli_opts="$cli_opts --non-timeout $nt"
  fi
  if [ "$nrt" != "-" ]; then
    srv_opts="$srv_opts --non-receive-timeout $nrt"
    cli_opts="$cli_opts --non-receive-timeout $nrt"
  fi

  ssh -n "$CAM_HOST" 'pkill -x coap-video-serv 2>/dev/null; sleep 0.4; exit 0' >/dev/null 2>&1
  ssh -n "$CAM_HOST" "nohup setsid $SRV $srv_opts > /tmp/cvs.log 2>&1 < /dev/null & sleep 4; exit 0" >/dev/null 2>&1

  out=$(ssh -n "$CLI_HOST" "$CLI --server $CAM_ADDR $cli_opts" 2>&1)
  s=$(echo "$out" | grep '^SUMMARY' | head -1)
  eff=$(echo "$out" | grep -o 'NON_RECEIVE_TIMEOUT=[0-9.]*s' | head -1 | cut -d= -f2)
  get() { echo "$s" | tr ' ' '\n' | grep "^$1=" | cut -d= -f2; }

  if [ -z "$s" ]; then echo "NO RESULT"; continue; fi

  echo "$label,rfc$rfc,$LOSS,$nt,$nrt,${eff:-n/a},$(get frames),$(get ok),$(get bad),$(get timeout),$(get secs),$(get fps),$(get goodput_mbps),$(get lat_mean),$(get lat_p50),$(get lat_p95),$(get lat_p99),$(get lat_max),$(get dg_in),$(get dg_out),$(get dg_per_frame)" >> "$OUT"

  printf 'fps=%-8s lat_p50=%-8s lat_p99=%-9s timeouts=%-3s eff_nrt=%s\n' \
         "$(get fps)" "$(get lat_p50)" "$(get lat_p99)" "$(get timeout)" "${eff:-n/a}"
done

ssh "$CAM_HOST" 'pkill -x coap-video-serv 2>/dev/null; exit 0' >/dev/null 2>&1
echo
echo "-> $OUT"
