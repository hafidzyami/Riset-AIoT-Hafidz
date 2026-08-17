#!/usr/bin/env node
/**
 * harness.js — rekam telemetri klien dash.js menjadi client_metadata.json
 * -----------------------------------------------------------------------
 * Untuk fase PROTOTYPING (tanpa Raspberry Pi). Jalan di Windows/Linux/Mac.
 *
 * Menghasilkan client_metadata.json berisi:
 *   - quality_timeline : perpindahan resolusi/bitrate yang BENAR-BENAR diputar (media time)
 *   - stalls           : event stalling [posisi media-time, durasi detik] (termasuk initial delay di posisi 0)
 * File ini dikonsumsi oleh label_from_metadata.py untuk membangun input P.1203 + label.
 *
 * Prasyarat:  npm install puppeteer
 *
 * Contoh:
 *   node harness.js --mpd <URL_MPD> --duration 120 --run-id S2B_bbb_rep1 --fps 30
 *   node harness.js --mpd <URL_MPD> --duration 120 --throttle 1.5mbit --latency 40 --device mobile
 *
 * CATATAN throttling: page.emulateNetworkConditions (CDP) hanya mengatur BANDWIDTH + LATENCY
 * untuk trafik HTTP/DASH. Ia TIDAK bisa mensimulasikan jitter/packet-loss untuk DASH
 * (parameter packetLoss CDP khusus WebRTC). Skenario jitter/loss menunggu NetEm di Raspberry Pi.
 */
const puppeteer = require("puppeteer");
const http = require("http");
const fs = require("fs");

// ---------------- CLI ----------------
const argv = process.argv;
const arg = (name, def) => {
  const i = argv.indexOf("--" + name);
  return i >= 0 && i + 1 < argv.length ? argv[i + 1] : def;
};

const MPD = arg("mpd", "https://dash.akamaized.net/akamai/bbb_30fps/bbb_30fps.mpd");
const DURATION = parseInt(arg("duration", "120"), 10);        // detik pemutaran
const RUN_ID = arg("run-id", "run_" + Date.now());
const OUT = arg("out", "client_metadata.json");
const THROTTLE = arg("throttle", "none");
const LATENCY = parseInt(arg("latency", "0"), 10);            // ms
const FPS_ARG = arg("fps", "auto");   // "auto" = baca frameRate langsung dari MPD
const CODEC = arg("codec", "h264");
const DISPLAY = arg("display", "1920x1080");
// dash.js DIPIN ke v4: v5 mengganti getBitrateInfoListFor -> getRepresentationsByType.
const DASHJS = arg("dashjs", "https://cdn.dashjs.org/v4.7.4/dash.all.min.js");

// Throttle menerima nilai BEBAS: "400k", "1.5m", "800kbit", "1.5mbit", "250kbps", atau "none".
// Nilai adalah bitrate (bit/detik); CDP minta byte/detik, jadi dibagi 8.
function parseThrottle(s) {
  if (!s || s === "none") return -1;
  const m = String(s).trim().toLowerCase().match(/^([\d.]+)\s*([kmg])?/);
  if (!m) return -1;
  const mult = { k: 1e3, m: 1e6, g: 1e9 }[m[2]] || 1;
  return Math.round((parseFloat(m[1]) * mult) / 8);   // bit/s -> byte/s
}
const dlBps = parseThrottle(THROTTLE);
const [dispW, dispH] = DISPLAY.split("x").map(Number);

// ---------------- halaman player (disajikan via server lokal) ----------------
const PAGE_HTML = `<!doctype html><html><head><meta charset="utf-8">
<script src="${DASHJS}"></script></head>
<body style="margin:0;background:#000">
<video id="v" muted playsinline style="width:${dispW}px;height:${dispH}px"></video>
<script>
window.__t = { quality_timeline: [], stalls: [], events: [], playback_start_epoch: null };
(function () {
  var v = document.getElementById('v');
  var player = dashjs.MediaPlayer().create();
  // ABR digerakkan bandwidth, bukan ukuran viewport
  player.updateSettings({ streaming: { abr: { limitBitrateByPortal: false } } });
  window.__player = player;

  function bitrateList() { try { return player.getBitrateInfoListFor('video') || []; } catch (e) { return []; } }
  function recordQuality(qi, tMedia) {
    var info = bitrateList().find(function (b) { return b.qualityIndex === qi; });
    if (!info) return;
    var last = window.__t.quality_timeline[window.__t.quality_timeline.length - 1];
    if (last && last.bitrate_kbps === Math.round(info.bitrate / 1000) &&
        last.width === info.width && last.height === info.height) return; // dedupe kualitas sama
    window.__t.quality_timeline.push({
      t_media: +(+tMedia).toFixed(3),
      bitrate_kbps: Math.round(info.bitrate / 1000),   // bitrate dash.js dalam bit/s -> kbps
      width: info.width, height: info.height
    });
  }

  var started = false, stallStartWall = null, stallPos = null, stallEpoch = null;

  player.on(dashjs.MediaPlayer.events.QUALITY_CHANGE_RENDERED, function (e) {
    if (e && e.mediaType === 'video') recordQuality(e.newQuality, v.currentTime || 0);
  });

  // Stalling via event native <video> (stabil lintas-versi dash.js).
  v.addEventListener('waiting', function () {
    stallStartWall = performance.now();
    stallEpoch = Date.now() / 1000;          // epoch: untuk penyelarasan dgn agen QoS
    stallPos = v.currentTime || 0;
    window.__t.events.push(['waiting', +(+stallPos).toFixed(3)]);
  });
  v.addEventListener('playing', function () {
    window.__t.events.push(['playing', +(+(v.currentTime || 0)).toFixed(3)]);
    if (!started) {
      started = true;
      window.__t.playback_start_epoch = Date.now() / 1000;   // media t=0 terjadi di sini
      // Seed kualitas awal HANYA bila belum ada entri. Event 'playing' dan
      // QUALITY_CHANGE_RENDERED saling berlomba; bila seed (yang memaksa
      // t_media=0) menyala setelah event kualitas pertama, timeline jadi tidak
      // urut dan setelah diurutkan kualitas awal yang rendah akan terentang
      // ke seluruh sesi -> label sistematis terlalu rendah.
      if (window.__t.quality_timeline.length === 0) {
        recordQuality(player.getQualityFor('video'), 0);
      }
    }
    if (stallStartWall !== null) {                    // tutup satu event stall
      var dur = (performance.now() - stallStartWall) / 1000;
      if (dur > 0.05) window.__t.stalls.push({ position: +(+stallPos).toFixed(3),
                                              duration: +dur.toFixed(3),
                                              t_epoch: stallEpoch });
      stallStartWall = null; stallPos = null; stallEpoch = null;
    }
  });

  player.initialize(v, ${JSON.stringify(MPD)}, true);   // autoplay = true
})();
</script></body></html>`;

// ---------------- deteksi fps dari MPD ----------------
// Dataset AAU memakai dua format: "24"/"25" dan pecahan "120000/4004" (=29.97).
// Salah fps -> skor O22 P.1203 melenceng, jadi lebih aman dibaca otomatis.
function parseFrameRate(v) {
  if (!v) return null;
  if (v.includes("/")) {
    const [a, b] = v.split("/").map(Number);
    return b ? a / b : null;
  }
  const n = parseFloat(v);
  return isFinite(n) ? n : null;
}

async function detectFps(mpdUrl) {
  try {
    const res = await fetch(mpdUrl);
    if (!res.ok) return null;
    const xml = await res.text();
    const m = xml.match(/frameRate="([^"]+)"/);
    return m ? parseFrameRate(m[1]) : null;
  } catch (e) {
    return null;
  }
}

// ---------------- driver ----------------
(async () => {
  let FPS;
  if (FPS_ARG === "auto") {
    const d = await detectFps(MPD);
    if (d) { FPS = d; console.log(`fps: ${FPS.toFixed(3)} (terdeteksi dari MPD)`); }
    else { FPS = 24; console.warn("fps: GAGAL deteksi dari MPD -> pakai 24. Set --fps manual bila perlu."); }
  } else {
    FPS = parseFloat(FPS_ARG);
    console.log(`fps: ${FPS} (manual)`);
  }

  const server = http.createServer((req, res) => {
    res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
    res.end(PAGE_HTML);
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const url = `http://127.0.0.1:${server.address().port}/`;

  const browser = await puppeteer.launch({
    headless: "new",
    // Bawaan Puppeteer 180 detik -> run 5 menit akan gagal. Beri kelonggaran.
    protocolTimeout: Math.max(300000, (DURATION + 180) * 1000),
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--autoplay-policy=no-user-gesture-required"],
  });
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: dispW, height: dispH });

    if (dlBps > 0 || LATENCY > 0) {
      const client = await page.target().createCDPSession();
      await client.send("Network.enable");
      await client.send("Network.emulateNetworkConditions", {
        offline: false,
        latency: LATENCY,
        downloadThroughput: dlBps,
        uploadThroughput: dlBps > 0 ? dlBps : -1,
      });
      console.log(`throttle: ${THROTTLE} (${dlBps} B/s), latency ${LATENCY} ms`);
    }

    console.log(`memuat MPD: ${MPD}`);
    await page.goto(url, { waitUntil: "domcontentloaded" });

    // Tunggu dari sisi NODE, bukan di dalam page.evaluate.
    // Menunggu di dalam browser membuat satu panggilan CDP berjalan selama DURATION
    // dan menabrak protocolTimeout. Polling pendek jauh lebih aman.
    const t0 = Date.now();
    let ended = false, lastLog = 0;
    while (!ended) {
      const elapsed = (Date.now() - t0) / 1000;
      if (elapsed >= DURATION) break;
      await new Promise((r) => setTimeout(r, 2000));
      try {
        ended = await page.evaluate(() => {
          const v = document.getElementById("v");
          return !!(v && v.ended);
        });
      } catch (e) {
        console.warn("  (poll gagal sekali, lanjut)");
      }
      if (elapsed - lastLog >= 30) {           // laporan kemajuan tiap ~30 detik
        lastLog = elapsed;
        const st = await page.evaluate(() => {
          const v = document.getElementById("v");
          return { t: v ? v.currentTime : 0, q: window.__t.quality_timeline.length, s: window.__t.stalls.length };
        }).catch(() => null);
        if (st) console.log(`  ${Math.round(elapsed)}s: media ${st.t.toFixed(1)}s, ${st.q} switch, ${st.s} stall`);
      }
    }
    if (ended) console.log("  video selesai sebelum durasi tercapai");

    const tel = await page.evaluate(() => {
      const v = document.getElementById("v");
      return { data: window.__t, mediaDuration: v.currentTime || 0 };
    });

    if (!tel.data.quality_timeline.length) {
      console.warn("PERINGATAN: quality_timeline kosong. MPD mungkin tak bisa diputar / URL mati.");
      console.warn("Coba MPD lain dari reference player: https://reference.dashif.org/dash.js/latest/samples/");
    }

    const meta = {
      run_id: RUN_ID,
      mpd: MPD,
      captured_at: new Date().toISOString(),
      media_duration: +tel.mediaDuration.toFixed(3),
      playback_start_epoch: tel.data.playback_start_epoch,   // untuk penyelarasan X<->y
      fps_assumed: FPS,
      codec: CODEC,
      display: { width: dispW, height: dispH },
      throttle: { preset: THROTTLE, downloadThroughput_Bps: dlBps, latency_ms: LATENCY },
      quality_timeline: tel.data.quality_timeline,
      stalls: tel.data.stalls,
      events_raw: tel.data.events,
    };
    fs.writeFileSync(OUT, JSON.stringify(meta, null, 2));
    console.log(`OK: ${meta.quality_timeline.length} perpindahan kualitas, ${meta.stalls.length} stall, media ${meta.media_duration}s`);
    console.log(`-> ${OUT}`);
  } finally {
    await browser.close();
    server.close();
  }
})().catch((e) => { console.error("ERROR:", e); process.exit(1); });