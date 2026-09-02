# CoAP live video on Raspberry Pi — RFC 7959 vs RFC 9177

A working live MJPEG video stream over CoAP, built on
[libcoap](https://github.com/obgm/libcoap), instrumented so that the two CoAP
block-wise transfer mechanisms can be compared on real hardware:

- **RFC 7959** — *Block-Wise Transfers in CoAP* (`Block1`/`Block2`), the
  lock-step mechanism every CoAP stack implements.
- **RFC 9177** — *Block-Wise Transfer Options Supporting Robust Transmission*
  (`Q-Block1`/`Q-Block2`), which bursts a body as non-confirmable payloads and
  repairs only what was lost.

The protocol analysis lives in **[RFC7959-vs-RFC9177.md](RFC7959-vs-RFC9177.md)**.
Measured numbers are below and in `results/`.

---

## Testbed

| role | host | hardware | camera |
|---|---|---|---|
| camera node / CoAP **server** | `serveryb` (192.168.18.233) | Raspberry Pi 4 B, 2 GB, Debian 13 | IMX219 |
| consumer / CoAP **client** | `smarthome` (192.168.18.234) | Raspberry Pi 5, 16 GB, Debian 12 | IMX708 |

Link: shared WLAN, ~9–13 ms RTT. The weaker Pi 4 is deliberately the sender,
which is the realistic constrained-device arrangement — and it is the side that
pays for RFC 7959's extra packets.

---

## Design

```
   Raspberry Pi 4  (camera node)                    Raspberry Pi 5  (consumer)
 ┌──────────────────────────────────┐            ┌───────────────────────────────┐
 │ rpicam-vid --codec mjpeg -o -    │            │  coap-video-client            │
 │            │ pipe                │            │    --rfc 7959 | 9177          │
 │            ▼                     │            │                               │
 │ frame_source.c  SOI/EOI split    │            │  libcoap reassembles body     │
 │            │                     │            │            │                  │
 │            ▼                     │            │            ▼                  │
 │ coap-video-server                │  Block2    │  cvid_body_check()            │
 │   /cam/frame   (pull)            │◄──── or ──►│  metrics → CSV                │
 │   /cam/stream  (observe)         │  Q-Block2  │  JPEG → --save-dir            │
 │   /cam/info /cam/stats           │            │                               │
 └──────────────────────────────────┘            └───────────────────────────────┘
```

### Representation

Each frame is served as a 28-byte `cvid_hdr_t` followed by the JPEG:

```
+------------------------+-----------------------------+
|  cvid_hdr_t (28 bytes) |  JPEG image (jpeg_len bytes) |
+------------------------+-----------------------------+
   magic 'CVID', version, w, h, seq, capture_us, jpeg_len
```

The header travels **in the payload rather than in CoAP options**, so the
representation is byte-identical under both mechanisms. Any measured difference
is therefore attributable to the block-wise machinery alone, which is what makes
the comparison fair. The frame sequence is *also* exposed as ETag and as an
elective option (65004) for correlation without parsing the body.

### Two implementation details worth knowing

**Body snapshots.** A block-wise transfer spans many datagrams. If the "latest
frame" changed mid-transfer, the client would reassemble blocks from two
different images into one corrupt frame. Each request therefore gets a private
`malloc`'d snapshot, released by libcoap via `release_func` when the last block
lands (`serve_frame()` in
[src/coap_video_server.c](src/coap_video_server.c)).

**Single-threaded, lock-free.** The camera pipe is handed to
`coap_io_process_with_fds()` so it shares libcoap's `select()`. Frames and CoAP
traffic are serviced by one thread, so no locking is needed anywhere.

---

## Build

libcoap must be built with Q-Block enabled (it is ON by default; the script is
explicit about it). No root required — everything installs under `~/.local`.

```bash
# on the build host (needs cmake + a C compiler)
bash bench/build_libcoap.sh

# compile the video tools, then copy binaries to the camera node
bash bench/deploy.sh
```

`deploy.sh` builds once on the Pi 5 and copies the binaries to the Pi 4, which
has no cmake. libcoap is linked **statically**, so the camera node needs no
libcoap install; both hosts are aarch64 and the older glibc on the build host
links forward-compatibly.

To build locally instead:

```bash
make                      # honours COAP_PREFIX, default ~/.local
```

---

## Run

On the camera node:

```bash
# RFC 7959 (Block2, lock-step, confirmable)
./bin/coap-video-server --rfc 7959 --width 640 --height 480 --framerate 15

# RFC 9177 (Q-Block2, bursting, non-confirmable)
./bin/coap-video-server --rfc 9177 --width 640 --height 480 --framerate 15 \
    --max-payloads 10 --non-timeout 2
```

On the consumer:

```bash
./bin/coap-video-client --server 192.168.18.233 --rfc 9177 \
    --frames 100 --csv run.csv --save-dir /tmp/frames
```

Interoperates with libcoap's reference client, which is a useful sanity check:

```bash
coap-client -m get -b 1024 -L 3     -o f.jpg coap://192.168.18.233/cam/frame  # RFC 7959
coap-client -m get -b 1024 -L 7 -N  -o f.jpg coap://192.168.18.233/cam/frame  # RFC 9177
```

### Key options

| option | applies to | meaning |
|---|---|---|
| `--rfc 7959\|9177` | both | block-wise mechanism |
| `--block-size N` | both | 16…1024, power of two |
| `--max-payloads N` | both | RFC 9177 `MAX_PAYLOADS` (congestion window) |
| `--non-timeout S` | both | RFC 9177 `NON_TIMEOUT`, seconds (fractional ok) |
| `--non-receive-timeout S` | both | RFC 9177 `NON_RECEIVE_TIMEOUT` — how long a receiver waits before asking for missing blocks. **Dominates recovery latency under loss** (see E5) |
| `--force-q-block` | both | skip Q-Block capability probing |
| `--mode pull\|observe` | client | request-per-frame, or RFC 7641 Observe |
| `--loss 5%` | both | emulate **transmit** loss (testing) |
| `--drop 5%` | client | emulate **receive** loss (testing) |
| `--save-dir DIR` | client | write received JPEGs |
| `--mjpeg-out FILE` | client | write an MJPEG stream (`-` = stdout) |

### Watching the stream

The client can emit a plain concatenation of JPEGs, which is a valid MJPEG
stream, so it pipes straight into a player:

```bash
./bin/coap-video-client --server 192.168.18.233 --rfc 9177 \
    --frames 100000 --warmup 0 --mjpeg-out - | ffplay -f mjpeg -
```

When the video goes to stdout the machine-readable `SUMMARY` line is redirected
to stderr so it cannot corrupt the stream. Writing to a file instead
(`--mjpeg-out out.mjpg`) produces a file `ffplay`/`ffprobe`/`mpv` read directly.

---

## Benchmarks

```bash
bash bench/run_all.sh                  # E1 + E2 + E3
bash bench/run_e4.sh                   # MAX_PAYLOADS sweep
bash bench/run_e5.sh                   # recovery-timer tuning under loss
python bench/validate.py results/*.csv # validity checks (run this first)
python bench/analyze.py  results/*.csv # tables + plots -> results/report.md
```

`run_all.sh` drives both Pis over SSH from a third machine and sweeps three
axes:

| experiment | axis | output |
|---|---|---|
| E1 | resolution — 640×480, 720p, 1080p | `results/e1_resolution.csv` |
| E2 | block size — 128, 256, 512, 1024 B | `results/e2_blocksize.csv` |
| E3 | packet loss — 0, 2, 5, 10, 20 % | `results/e3_loss.csv` |
| E4 | RFC 9177 `MAX_PAYLOADS` — 2…100 | `results/e4_maxpayloads.csv` |
| E5 | RFC 9177 recovery timers under loss | `results/e5_timers.csv` |

### Validity checking

Both arms of a comparison run back-to-back against a **live** camera, so the
scene can drift between them — and a noisier scene simply makes bigger JPEGs,
which need more blocks. That would look like a protocol difference but is a
lighting difference. `bench/validate.py` guards against this and three related
traps, and exits non-zero if a result set is not trustworthy:

1. **body-size parity** — both arms must have carried comparable frames;
2. **no corrupt bodies** — reassembly must never produce a torn JPEG;
3. **block arithmetic** — datagrams/frame must match each mechanism's model
   (≈2N for RFC 7959, ≈N+1 for RFC 9177), which independently confirms the
   protocols behaved as specified;
4. **stall-dominated frame rates** — one timed-out frame contributes its whole
   deadline to the wall clock, so a single stall can halve the reported `fps`
   while median latency stays healthy. Those rows are flagged, and latency
   should be read instead of `fps`.

Loss is injected on the **server's transmit path** using libcoap's built-in
emulator, so block payloads are lost on the way to the client — the exact
condition RFC 9177's recovery targets. This needs no root, which matters
because `tc netem` was unavailable on the test Pis.

Metrics per run: frame rate, goodput, latency (mean/p50/p95/p99/max), UDP
datagrams in/out from `/proc/net/snmp`, datagrams per frame, corrupt and
timed-out frames, and server CPU seconds from `/proc/<pid>/stat`.

---

## Results

See **[results/report.md](results/report.md)** for the full tables and
`results/*.png` for plots.

### E1 — the structural win (100 frames, 1024 B blocks, no emulated loss)

| metric | 640×480 | 1280×720 | 1920×1080 |
|---|---|---|---|
| **frame rate** 7959 → 9177 | 29.7 → **65.8** fps | 7.0 → **18.5** fps | 2.6 → **5.8** fps |
| *improvement* | **2.21×** | **2.63×** | **2.20×** |
| **latency p50** 7959 → 9177 | 31.4 → **13.1** ms | 132.1 → **50.7** ms | 312.3 → **124.4** ms |
| *improvement* | **2.41×** | **2.61×** | **2.51×** |
| **datagrams/frame** 7959 → 9177 | 31.6 → **18.2** | 145.0 → **79.7** | 359.2 → **198.1** |
| *improvement* | **1.73×** | **1.82×** | **1.81×** |
| **client transmissions** 7959 → 9177 | 1577 → **211** | 7248 → **766** | 17960 → **1842** |
| *improvement* | **7.5×** | **9.5×** | **9.8×** |
| **server CPU** 7959 → 9177 | 0.18 → **0.05** s | 0.75 → **0.36** s | 1.53 → **0.64** s |
| *improvement* | **3.6×** | **2.1×** | **2.4×** |

The datagram counts land almost exactly on the theory — ≈2N for RFC 7959 and
≈N+1 for RFC 9177 — which `bench/validate.py` checks mechanically. The
~9.5× drop in *client transmissions* is the mechanism in one number: one
request replaces N.

### E2 — block size matters far more for RFC 7959

| block size | 128 B | 256 B | 512 B | 1024 B |
|---|---|---|---|---|
| latency p50, RFC 7959 | 215.5 ms | 109.8 ms | 52.8 ms | 32.0 ms |
| latency p50, RFC 9177 | **79.9 ms** | **41.1 ms** | **21.9 ms** | **13.5 ms** |
| *improvement* | 2.70× | 2.67× | 2.41× | 2.37× |

Halving the block size doubles RFC 7959's round trips; for RFC 9177 it only
adds datagrams to an existing burst. At 128 B both mechanisms also start
suffering real stalls (`lat_p99` 2.5 s and 4.2 s), because ~120 datagrams per
frame is enough to provoke genuine loss on the WLAN.

### E3 — under loss, both are timer-bound (the surprising result)

30 frames, 640×480, 1024 B, loss emulated on the server's transmit path:

| loss | RFC 7959 | RFC 9177 | 7959 frames delivered | 9177 frames delivered |
|---|---|---|---|---|
| 0 % | 42.4 fps | **93.0 fps** | 33 | 33 |
| 2 % | 1.09 fps | **4.39 fps** | 32 | **33** |
| 5 % | 0.80 fps | **0.98 fps** | 33 | 32 |
| 10 % | 0.27 fps | **0.43 fps** | 27 | 27 |
| 20 % | 0.16 fps | **0.29 fps** | **1** | **25** |

This did **not** match the prediction. Both mechanisms collapse by ~40× at only
2 % loss, because a single lost block costs a *multi-second timer* against a
33 ms frame budget:

- RFC 7959 stalls for `ACK_TIMEOUT` (2 s) and pays it **per lost block,
  serially**;
- RFC 9177 waits `NON_RECEIVE_TIMEOUT` (**4 s** by default — twice as long)
  but then repairs **every** missing block in one request.

So RFC 9177 ships with a recovery timer twice as long as the mechanism it
improves on, which is a poor default for live video. Its batching is why it
stays ahead throughout (1.2×–4×), and why at 20 % loss it still delivers 25 of
30 frames where RFC 7959 delivers **one**.

Absolute frame rates in this sweep are noisy — real WLAN loss compounds with
the emulated loss, and each level is a separate ~2-minute run. The robust
findings are the direction (RFC 9177 ahead at every loss level), the ~40×
cliff between 0 % and 2 %, and the 20 % result where one mechanism keeps
working and the other stops.

That finding is what experiment **E5** exists to act on.

### E4 — `MAX_PAYLOADS` is a congestion window, and its default is small

40 frames at 1920×1080 (~150 blocks per frame), 1024 B blocks, no emulated
loss. Only RFC 9177's `MAX_PAYLOADS` changes:

| `MAX_PAYLOADS` | 2 | 5 | **10 (default)** | 20 | 50 | 100 |
|---|---|---|---|---|---|---|
| frame rate (fps) | 5.53 | 4.88 | **8.66** | 13.87 | 25.69 | **31.76** |
| latency mean (ms) | 167 | 191 | **108** | 67 | 36 | **28** |

`MAX_PAYLOADS` bounds how many non-confirmable payloads may go out back to back
before the sender must pause for `NON_TIMEOUT`. When a body needs ~150 blocks
and the window is 10, the transfer is paced by those pauses rather than by the
link — so raising it from the default 10 to 100 is worth **3.7×**.

The practical rule: **`MAX_PAYLOADS` should scale with `body_size / block_size`**,
not stay at a fixed default. It is genuine congestion control, though (RFC 9177
§3), so it should be raised deliberately on a provisioned network rather than
maximised blindly — see the crash below for what over-large bursts can do.

### E5 — tuning the recovery timer is worth more than the mechanism

25 frames, 640×480, 1024 B, **5 % loss**. Only `NON_RECEIVE_TIMEOUT` (and
`NON_TIMEOUT`, which RFC 9177 §6.2 couples to it via
`NON_RECEIVE_TIMEOUT ≥ 1.5×NON_TIMEOUT + 1`) changes between the RFC 9177 rows:

| arm | effective `NON_RECEIVE_TIMEOUT` | timed-out frames | latency p95 | fps |
|---|---|---|---|---|
| RFC 7959 baseline | — (`ACK_TIMEOUT` 2 s) | 4 | 7467 ms | 0.200 |
| RFC 9177, **stock defaults** | 4.0 s | 3 | 4106 ms | 0.438 |
| RFC 9177, tuned | 2.0 s | **0** | 4037 ms | 0.709 |
| RFC 9177, tuned | 1.5 s | **0** | 1533 ms | 1.145 |
| RFC 9177, tuned | **1.2 s** | **0** | **1240 ms** | **1.627** |

The p95 column is the cleanest view of the mechanism: tail latency tracks the
recovery timer almost directly, falling 6× as the timer comes down, because the
tail *is* frames that had to wait out one repair cycle.

Dropping the recovery timer from 4.0 s to 1.2 s is worth **3.7× against RFC
9177's own defaults** and **8.1× against RFC 7959** — a larger effect than
choosing the mechanism in the first place. Frame timeouts go to zero.

**This is the headline practical result:** adopting RFC 9177 for live video and
leaving `NON_RECEIVE_TIMEOUT` at its default captures only a fraction of the
available benefit. The defaults are sized for bulk telemetry, where a 4-second
repair delay is irrelevant; for video it is ~120 frame times.

---

## A double-free this experiment shook out

Large Q-Block2 bursts (`--max-payloads 100` at 1080p, ~170 blocks per frame)
overran the sender's socket buffer, and the resulting `EAGAIN` + peer `RST`
crashed the server with `double free or corruption (!prev)`.

The cause is an easy-to-miss ownership rule in libcoap:

```c
/* WRONG -- this is a double free */
if (!coap_add_data_large_response(..., snap_len, snap, release_snapshot, snap)) {
    free(snap);
    ...
}
```

`coap_add_data_large_response()` **already releases `app_ptr` on every failure
path** before returning 0 — via the `error:` label, which calls `release_func`
itself, or via `error_released:`, where the inner call released it. So the
caller must *not* free on failure:

```c
/* right -- on failure the buffer is already gone */
if (!coap_add_data_large_response(..., snap_len, snap, release_snapshot, snap))
    return;
```

It stays latent under RFC 7959 because the failure path is nearly unreachable
when only one block is in flight; RFC 9177's bursting makes send failures
routine and turns it into a crash. Fixed in `serve_frame()`, and verified by
re-running the exact configuration that crashed: the server now survives it
with 40/40 frames and no corruption.

---

## Verification

The pipeline is checked end-to-end, not just timed:

- every received body is validated by `cvid_body_check()` — magic, version,
  declared length, JPEG `SOI`…`EOI` framing — and counted as `corrupt` if it
  fails;
- saved frames decode: a 640×480 capture decodes to exactly 921 600 raw bytes
  (`ffmpeg -pix_fmt rgb24`);
- payloads are byte-identical between the two mechanisms for the same body;
- the server interoperates with the stock `coap-client` under both mechanisms.

---

## Limitations and future work

- **Loss model.** Emulated loss is Bernoulli on the transmit path. Real WiFi
  loss is bursty; `tc netem` with a Gilbert–Elliott model would be more faithful
  and needs root on the camera node.
- **No DTLS.** libcoap is built without DTLS to isolate block-wise behaviour.
  Adding DTLS/OSCORE would add per-datagram cost and should *widen* the gap,
  since RFC 9177 sends fewer datagrams.
- **Single client.** `MAX_PAYLOADS` is a congestion window, and its interesting
  behaviour appears when several senders share a bottleneck. A multi-client
  fairness experiment is the natural next step.
- **Observe mode** is implemented (`--mode observe`) but the reported benchmarks
  use pull mode, where one frame maps to exactly one transfer and attribution is
  unambiguous.
- **MJPEG only.** Deliberate: independent frames keep the loss experiment
  measuring the recovery mechanism rather than codec error propagation. H.264
  would be far more bandwidth-efficient but a lost block would corrupt a whole
  GOP.

---

## Files

| path | what |
|---|---|
| [src/cvid.h](src/cvid.h) | wire format, timing helpers, body validation |
| [src/frame_source.c](src/frame_source.c) | rpicam-vid MJPEG reader, SOI/EOI framing |
| [src/coap_video_server.c](src/coap_video_server.c) | CoAP server, resources, block-mode selection |
| [src/coap_video_client.c](src/coap_video_client.c) | CoAP client, metrics, CSV |
| [bench/build_libcoap.sh](bench/build_libcoap.sh) | libcoap build with Q-Block |
| [bench/deploy.sh](bench/deploy.sh) | build + push to the camera node |
| [bench/run_matrix.sh](bench/run_matrix.sh) | one sweep of the experiment matrix |
| [bench/run_all.sh](bench/run_all.sh) | E1 + E2 + E3 |
| [bench/run_e4.sh](bench/run_e4.sh) | MAX_PAYLOADS sweep |
| [bench/run_e5.sh](bench/run_e5.sh) | recovery-timer tuning under loss |
| [bench/validate.py](bench/validate.py) | experimental-validity checks |
| [bench/analyze.py](bench/analyze.py) | tables and plots |
| [RFC7959-vs-RFC9177.md](RFC7959-vs-RFC9177.md) | protocol analysis |
