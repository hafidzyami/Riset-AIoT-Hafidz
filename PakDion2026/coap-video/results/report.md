# CoAP live video: RFC 7959 vs RFC 9177 - measured results

Improvement rows are stated so that **>1x always means RFC 9177 did better**, whichever direction the metric runs in.

## e1_resolution.csv

### Sweep over resolution

| metric | 640x480 | 1280x720 | 1920x1080 |
|---|---|---|---|
| frame rate (fps) - rfc7959 | 29.74 | 7.03 | 2.62 |
| frame rate (fps) - rfc9177 | 65.84 | 18.46 | 5.76 |
| *frame rate (fps) - improvement* | **2.21x** | **2.63x** | **2.20x** |
| goodput (Mbit/s) - rfc7959 | 3.71 | 4.14 | 3.85 |
| goodput (Mbit/s) - rfc9177 | 8.37 | 10.82 | 8.46 |
| *goodput (Mbit/s) - improvement* | **2.26x** | **2.61x** | **2.20x** |
| latency p50 (ms) - rfc7959 | 31.42 | 132.06 | 312.30 |
| latency p50 (ms) - rfc9177 | 13.05 | 50.69 | 124.43 |
| *latency p50 (ms) - improvement* | **2.41x** | **2.61x** | **2.51x** |
| latency p95 (ms) - rfc7959 | 41.20 | 163.19 | 369.65 |
| latency p95 (ms) - rfc9177 | 22.16 | 60.66 | 147.17 |
| *latency p95 (ms) - improvement* | **1.86x** | **2.69x** | **2.51x** |
| UDP datagrams / frame - rfc7959 | 31.55 | 144.98 | 359.21 |
| UDP datagrams / frame - rfc9177 | 18.19 | 79.69 | 198.13 |
| *UDP datagrams / frame - improvement* | **1.73x** | **1.82x** | **1.81x** |
| client transmissions - rfc7959 | 1577.00 | 7248.00 | 17960.00 |
| client transmissions - rfc9177 | 211.00 | 766.00 | 1842.00 |
| *client transmissions - improvement* | **7.47x** | **9.46x** | **9.75x** |
| server CPU (s) - rfc7959 | 0.18 | 0.75 | 1.53 |
| server CPU (s) - rfc9177 | 0.05 | 0.36 | 0.64 |
| *server CPU (s) - improvement* | **3.60x** | **2.08x** | **2.39x** |
| corrupt frames - rfc7959 | 0.00 | 0.00 | 0.00 |
| corrupt frames - rfc9177 | 0.00 | 0.00 | 0.00 |
| *corrupt frames - improvement* | tie | tie | tie |
| timed-out frames - rfc7959 | 0.00 | 0.00 | 0.00 |
| timed-out frames - rfc9177 | 0.00 | 0.00 | 0.00 |
| *timed-out frames - improvement* | tie | tie | tie |

## e2_blocksize.csv

### Sweep over block size (B)

| metric | 128 | 256 | 512 | 1024 |
|---|---|---|---|---|
| frame rate (fps) - rfc7959 | 3.67 | 7.07 | 16.68 | 27.52 |
| frame rate (fps) - rfc9177 | 3.04 | 22.26 | 36.20 | 8.46 |
| *frame rate (fps) - improvement* | **0.83x** | **3.15x** | **2.17x** | **0.31x** |
| goodput (Mbit/s) - rfc7959 | 0.46 | 0.89 | 2.08 | 3.44 |
| goodput (Mbit/s) - rfc9177 | 0.38 | 2.80 | 4.49 | 1.05 |
| *goodput (Mbit/s) - improvement* | **0.83x** | **3.16x** | **2.16x** | **0.31x** |
| latency p50 (ms) - rfc7959 | 215.52 | 109.79 | 52.76 | 32.00 |
| latency p50 (ms) - rfc9177 | 79.92 | 41.12 | 21.93 | 13.49 |
| *latency p50 (ms) - improvement* | **2.70x** | **2.67x** | **2.41x** | **2.37x** |
| latency p95 (ms) - rfc7959 | 253.12 | 136.46 | 80.94 | 50.79 |
| latency p95 (ms) - rfc9177 | 339.17 | 52.47 | 50.35 | 51.41 |
| *latency p95 (ms) - improvement* | **0.75x** | **2.60x** | **1.61x** | **0.99x** |
| UDP datagrams / frame - rfc7959 | 245.76 | 123.30 | 62.01 | 31.62 |
| UDP datagrams / frame - rfc9177 | 135.92 | 68.67 | 34.48 | 17.77 |
| *UDP datagrams / frame - improvement* | **1.81x** | **1.80x** | **1.80x** | **1.78x** |
| client transmissions - rfc7959 | 12285.00 | 6164.00 | 3100.00 | 1581.00 |
| client transmissions - rfc9177 | 1282.00 | 671.00 | 351.00 | 212.00 |
| *client transmissions - improvement* | **9.58x** | **9.19x** | **8.83x** | **7.46x** |
| server CPU (s) - rfc7959 | 1.32 | 0.65 | 0.23 | 0.20 |
| server CPU (s) - rfc9177 | 0.59 | 0.25 | 0.13 | 0.10 |
| *server CPU (s) - improvement* | **2.24x** | **2.60x** | **1.77x** | **2.00x** |
| corrupt frames - rfc7959 | 0.00 | 0.00 | 0.00 | 0.00 |
| corrupt frames - rfc9177 | 0.00 | 0.00 | 0.00 | 0.00 |
| *corrupt frames - improvement* | tie | tie | tie | tie |
| timed-out frames - rfc7959 | 0.00 | 0.00 | 0.00 | 0.00 |
| timed-out frames - rfc9177 | 1.00 | 0.00 | 0.00 | 1.00 |
| *timed-out frames - improvement* | **0.00x** | tie | tie | **0.00x** |

## e3_loss.csv

### Sweep over packet loss (%)

| metric | 0 | 2 | 5 | 10 | 20 |
|---|---|---|---|---|---|
| frame rate (fps) - rfc7959 | 42.41 | 1.09 | 0.80 | 0.27 | 0.16 |
| frame rate (fps) - rfc9177 | 93.03 | 4.39 | 0.98 | 0.43 | 0.29 |
| *frame rate (fps) - improvement* | **2.19x** | **4.04x** | **1.22x** | **1.60x** | **1.75x** |
| goodput (Mbit/s) - rfc7959 | 4.06 | 0.10 | 0.08 | 0.02 | 0.00 |
| goodput (Mbit/s) - rfc9177 | 9.20 | 0.40 | 0.09 | 0.03 | 0.02 |
| *goodput (Mbit/s) - improvement* | **2.27x** | **3.99x** | **1.21x** | **1.64x** | **52.00x** |
| latency p50 (ms) - rfc7959 | 20.04 | 22.22 | 23.26 | 2554.37 | 0.00 |
| latency p50 (ms) - rfc9177 | 9.23 | 7.37 | 10.09 | 10.76 | 4015.77 |
| *latency p50 (ms) - improvement* | **2.17x** | **3.01x** | **2.31x** | **237.39x** | **0.00x** |
| latency p95 (ms) - rfc7959 | 30.14 | 2888.51 | 2986.42 | 5614.22 | 0.00 |
| latency p95 (ms) - rfc9177 | 12.40 | 9.56 | 4066.48 | 4046.84 | 4088.44 |
| *latency p95 (ms) - improvement* | **2.43x** | **302.15x** | **0.73x** | **1.39x** | **0.00x** |
| UDP datagrams / frame - rfc7959 | 24.40 | 24.43 | 25.20 | 25.93 | 15.63 |
| UDP datagrams / frame - rfc9177 | 14.97 | 13.53 | 14.97 | 16.73 | 17.83 |
| *UDP datagrams / frame - improvement* | **1.63x** | **1.81x** | **1.68x** | **1.55x** | **0.88x** |
| client transmissions - rfc7959 | 366.00 | 371.00 | 385.00 | 404.00 | 255.00 |
| client transmissions - rfc9177 | 67.00 | 51.00 | 74.00 | 102.00 | 116.00 |
| *client transmissions - improvement* | **5.46x** | **7.27x** | **5.20x** | **3.96x** | **2.20x** |
| server CPU (s) - rfc7959 | 0.05 | 0.14 | 0.16 | 0.42 | 0.66 |
| server CPU (s) - rfc9177 | 0.01 | 0.03 | 0.11 | 0.26 | 0.40 |
| *server CPU (s) - improvement* | **5.00x** | **4.67x** | **1.45x** | **1.62x** | **1.65x** |
| corrupt frames - rfc7959 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| corrupt frames - rfc9177 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| *corrupt frames - improvement* | tie | tie | tie | tie | tie |
| timed-out frames - rfc7959 | 0.00 | 1.00 | 0.00 | 6.00 | 30.00 |
| timed-out frames - rfc9177 | 0.00 | 0.00 | 1.00 | 7.00 | 10.00 |
| *timed-out frames - improvement* | tie | **9177 only** | **0.00x** | **0.86x** | **3.00x** |

## e4_maxpayloads.csv

### Sweep over MAX_PAYLOADS

| metric | 2 | 5 | 10 | 20 | 50 | 100 |
|---|---|---|---|---|---|---|
| *mechanism* | rfc9177 | rfc9177 | rfc9177 | rfc9177 | rfc9177 | rfc9177 |
| frame rate (fps) | 5.53 | 4.88 | 8.66 | 13.87 | 25.68 | 31.76 |
| goodput (Mbit/s) | 7.15 | 6.30 | 11.18 | 17.95 | 33.36 | 40.92 |
| latency p50 (ms) | 162.86 | 191.59 | 104.43 | 67.47 | 35.12 | 27.92 |
| latency p95 (ms) | 191.61 | 209.23 | 126.98 | 75.46 | 45.53 | 33.20 |
| UDP datagrams / frame | 237.57 | 190.07 | 174.47 | 167.03 | 162.90 | 159.95 |
| client transmissions | 3175.00 | 1283.00 | 658.00 | 343.00 | 149.00 | 87.00 |
| corrupt frames | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| timed-out frames | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |

## e5_timers.csv

### Sweep over configuration

| metric | 7959-baseline | 9177-default | 9177-nrt2.0 | 9177-nrt1.5 | 9177-nrt1.2 |
|---|---|---|---|---|---|
| *mechanism* | rfc7959 | rfc9177 | rfc9177 | rfc9177 | rfc9177 |
| frame rate (fps) | 0.20 | 0.44 | 0.71 | 1.15 | 1.63 |
| goodput (Mbit/s) | 0.05 | 0.10 | 0.19 | 0.31 | 0.44 |
| latency p50 (ms) | 4302.28 | 24.95 | 2027.44 | 35.28 | 31.43 |
| latency p95 (ms) | 7467.04 | 4105.84 | 4036.57 | 1532.60 | 1240.20 |
| UDP datagrams / frame | 70.36 | 39.16 | 40.20 | 38.52 | 38.56 |
| client transmissions | 891.00 | 126.00 | 140.00 | 118.00 | 118.00 |
| corrupt frames | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| timed-out frames | 4.00 | 3.00 | 0.00 | 0.00 | 0.00 |
