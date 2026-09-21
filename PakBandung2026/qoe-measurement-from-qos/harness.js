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
// Mode hls.js untuk lengan leave-one-player-out. Segmen fMP4 yang dipakai
// playlist HLS SAMA PERSIS dgn yang dipakai DASH, sehingga byte di kabel
// praktis identik; yang berubah hanya format manifest dan pemutarnya.
//
// Konsekuensi yang harus dinyatakan: hls.js memakai algoritma ABR-nya SENDIRI.
// Opsi --abr tidak berlaku di sini, dan itulah variabel yang sebenarnya diuji.
const PLAYER = String(arg("player", "dash")).toLowerCase();
if (!["dash", "hls"].includes(PLAYER)) {
  console.error("--player harus dash atau hls");
  process.exit(2);
}
const HLSJS = arg("hlsjs", "https://cdn.jsdelivr.net/npm/hls.js@1.5.17/dist/hls.min.js");

// Pemilih ABR. Ini menjawab kritik bahwa label nyaris merupakan fungsi
// deterministik dari throughput: aturan berbasis throughput memilih representasi
// dari bandwidth terukur, sehingga fitur throughput dan label QoE menjadi
// kolinear lewat ABR. BOLA memilih berdasarkan tingkat buffer, bukan throughput,
// sehingga pemetaan itu terputus.
//   throughput : murni berbasis throughput
//   dynamic    : bawaan dash.js, throughput saat buffer rendah dan BOLA saat tinggi
//   bola       : murni berbasis tingkat buffer
//
// Ketiganya membentuk rentang yang bermakna dari sepenuhnya digerakkan throughput
// sampai sepenuhnya digerakkan buffer, dan seluruhnya sah untuk konten VOD biasa.
//
//   l2a, lolp  : algoritma latensi rendah, DI LUAR PERUNTUKAN untuk konten ini.
//
// L2A-LL dan LoL+ dirancang untuk LL-DASH dengan CMAF berpotongan. Pada konten VOD
// bersegmen empat detik, keduanya mungkin tetap berjalan tetapi perilakunya bukan
// yang dijelaskan pada publikasi aslinya. Disediakan untuk eksplorasi, bukan untuk
// klaim. Periksa medan abr.effective pada metadata keluaran: bila berbunyi
// "default", versi dash.js yang dipakai tidak mengenali strategi itu.
// Chromium TIDAK memakai HTTP/3 secara otomatis untuk host baru: ia menunggu
// header Alt-Svc pada koneksi TCP lebih dulu, lalu baru mencoba QUIC pada
// permintaan BERIKUTNYA. Untuk sesi 5 menit itu berarti sebagian trafik awal
// tetap lewat TCP, dan perbandingan protokol menjadi tercampur. Bendera
// --origin-to-force-quic-on memaksa QUIC sejak permintaan pertama.
const QUIC = arg("quic", "") ? String(arg("quic", "")) : "";

// Chromium memverifikasi sertifikat pada jalur QUIC secara BERBEDA, dan
// --ignore-certificate-errors saja tidak selalu cukup. Bila origin dipaksa ke
// QUIC lalu koneksinya ditolak, Chromium TIDAK kembali ke TCP sehingga seluruh
// permintaan mati dan gejalanya terlihat seperti MPD tidak dapat diputar.
// --ignore-certificate-errors-spki-list menerima sidik jari SPKI dan bekerja
// pada kedua jalur. Ambil nilainya dengan:
//   openssl x509 -in dash.crt -pubkey -noout \
//     | openssl pkey -pubin -outform der \
//     | openssl dgst -sha256 -binary | openssl enc -base64
const SPKI = arg("spki", "") ? String(arg("spki", "")) : "";

const ABR_SAH = ["throughput", "dynamic", "bola"];
const ABR_EKSPLORASI = ["l2a", "lolp"];
const ABR = String(arg("abr", "dynamic")).toLowerCase();
if (![...ABR_SAH, ...ABR_EKSPLORASI].includes(ABR)) {
  console.error(`--abr harus salah satu dari: ${[...ABR_SAH, ...ABR_EKSPLORASI].join(", ")} `
                + `(diberi: ${ABR})`);
  process.exit(2);
}
if (ABR_EKSPLORASI.includes(ABR)) {
  console.warn(`PERINGATAN: --abr ${ABR} adalah algoritma latensi rendah dan berada `
               + `di luar peruntukan untuk konten VOD bersegmen empat detik. `
               + `Periksa abr.effective pada metadata keluaran.`);
}

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
<script src="${PLAYER === "hls" ? HLSJS : DASHJS}"></script></head>
<body style="margin:0;background:#000">
<video id="v" muted playsinline style="width:${dispW}px;height:${dispH}px"></video>
<script>
window.__t = { quality_timeline: [], stalls: [], events: [], playback_start_epoch: null };
(function () {
  var v = document.getElementById('v');
  var MODE_HLS = ${JSON.stringify(PLAYER === "hls")};

  // Deteksi stall memakai event native <video>, sehingga sama persis pada kedua
  // pemutar. Yang berbeda hanya cara membaca representasi yang sedang diputar.
  var started = false, stallStartWall = null, stallPos = null, stallEpoch = null;

  function pasangStall() {
    v.addEventListener('waiting', function () {
      stallStartWall = performance.now();
      stallEpoch = Date.now() / 1000;
      stallPos = v.currentTime || 0;
      window.__t.events.push(['waiting', +(+stallPos).toFixed(3)]);
    });
    v.addEventListener('playing', function () {
      window.__t.events.push(['playing', +(+(v.currentTime || 0)).toFixed(3)]);
      if (!started) {
        started = true;
        window.__t.playback_start_epoch = Date.now() / 1000;
        if (window.__t.quality_timeline.length === 0) seedKualitas();
      }
      if (stallStartWall !== null) {
        var dur = (performance.now() - stallStartWall) / 1000;
        if (dur > 0.05) window.__t.stalls.push({ position: +(+stallPos).toFixed(3),
                                                duration: +dur.toFixed(3),
                                                t_epoch: stallEpoch });
        stallStartWall = null; stallPos = null; stallEpoch = null;
      }
    });
  }

  function catatKualitas(bitrate_bps, w, h, tMedia) {
    if (!bitrate_bps) return;
    var last = window.__t.quality_timeline[window.__t.quality_timeline.length - 1];
    var kbps = Math.round(bitrate_bps / 1000);
    if (last && last.bitrate_kbps === kbps && last.width === w &&
        last.height === h) return;
    window.__t.quality_timeline.push({
      t_media: +(+tMedia).toFixed(3), bitrate_kbps: kbps, width: w, height: h
    });
  }

  var seedKualitas = function () {};

  if (MODE_HLS) {
    if (!window.Hls || !Hls.isSupported()) {
      window.__t.fatal = 'hls.js tidak didukung di peramban ini';
      return;
    }
    var hls = new Hls({ enableWorker: true });
    window.__player = hls;
    window.__t.abr_mode = 'hlsjs-default';
    // hls.js tidak menyediakan pilihan strategi ABR seperti dash.js. Nilai ini
    // direkam apa adanya supaya analisis tidak salah mengira mode ABR yang
    // diminta benar-benar diterapkan.
    window.__t.abr_effective = 'hlsjs-internal';

    function levelSaatIni() {
      try {
        var i = hls.currentLevel >= 0 ? hls.currentLevel : hls.loadLevel;
        return (hls.levels && hls.levels[i]) || null;
      } catch (e) { return null; }
    }
    seedKualitas = function () {
      var L = levelSaatIni();
      if (L) catatKualitas(L.bitrate, L.width, L.height, 0);
    };
    hls.on(Hls.Events.LEVEL_SWITCHED, function (ev, data) {
      var L = (hls.levels && hls.levels[data.level]) || null;
      if (L) catatKualitas(L.bitrate, L.width, L.height, v.currentTime || 0);
    });
    hls.on(Hls.Events.ERROR, function (ev, data) {
      if (data && data.fatal) window.__t.fatal = String(data.type) + '/' +
                                                 String(data.details);
    });
    pasangStall();
    hls.loadSource(${JSON.stringify(MPD)});
    hls.attachMedia(v);
    hls.on(Hls.Events.MANIFEST_PARSED, function () {
      var p = v.play();
      if (p && p.catch) p.catch(function () {});
    });
    return;
  }

  var player = dashjs.MediaPlayer().create();
  // ABR digerakkan bandwidth, bukan ukuran viewport.
  // ABR_MODE menentukan aturan pemilihan representasi; lihat catatan di CLI.
  var abrCfg = { limitBitrateByPortal: false };
  var mode = ${JSON.stringify(ABR)};
  var PETA = { throughput: 'abrThroughput', bola: 'abrBola',
               l2a: 'abrL2A', lolp: 'abrLoLP' };
  if (PETA[mode]) {
    abrCfg.ABRStrategy = PETA[mode];
    abrCfg.useDefaultABRRules = true;
  }
  player.updateSettings({ streaming: { abr: abrCfg } });
  window.__t.abr_mode = mode;
  // Rekam strategi yang BENAR-BENAR aktif, bukan yang diminta, supaya bisa
  // diperiksa saat analisis bila dash.js mengabaikan pengaturan.
  try {
    window.__t.abr_effective =
      (player.getSettings().streaming.abr || {}).ABRStrategy || 'default';
  } catch (e) { window.__t.abr_effective = 'unknown'; }
  window.__player = player;

  function bitrateList() { try { return player.getBitrateInfoListFor('video') || []; } catch (e) { return []; } }
  function recordQuality(qi, tMedia) {
    var info = bitrateList().find(function (b) { return b.qualityIndex === qi; });
    if (!info) return;
    catatKualitas(info.bitrate, info.width, info.height, tMedia);
  }
  seedKualitas = function () { recordQuality(player.getQualityFor('video'), 0); };

  player.on(dashjs.MediaPlayer.events.QUALITY_CHANGE_RENDERED, function (e) {
    if (e && e.mediaType === 'video') recordQuality(e.newQuality, v.currentTime || 0);
  });

  // Stalling dipasang lewat pasangStall(), yang dipakai KEDUA pemutar, sehingga
  // definisi stall identik pada lengan DASH maupun HLS. Seed kualitas awal hanya
  // berjalan bila timeline masih kosong: event 'playing' dan event perpindahan
  // kualitas saling berlomba, dan bila seed yang memaksa t_media=0 menyala
  // belakangan, timeline jadi tidak urut sehingga setelah diurutkan kualitas
  // awal yang rendah terentang ke seluruh sesi.
  pasangStall();

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

// fetch() bawaan Node MENOLAK sertifikat self-signed, dan bendera
// --ignore-certificate-errors hanya berlaku untuk Chromium. Pengambilan MPD di
// sini berjalan di jalur Node yang terpisah, sehingga pada server HTTPS uji ia
// selalu gagal dan fps diam-diam jatuh ke nilai bawaan. Modul https bawaan
// dipakai supaya rejectUnauthorized dapat dimatikan secara eksplisit.
function ambilTeks(url) {
  return new Promise((resolve, reject) => {
    const aman = url.toLowerCase().startsWith("https:");
    const lib = aman ? require("https") : require("http");
    const opts = aman ? { rejectUnauthorized: false } : {};
    lib.get(url, opts, (res) => {
      if (res.statusCode !== 200) {
        res.resume();
        return reject(new Error(`HTTP ${res.statusCode}`));
      }
      let d = "";
      res.setEncoding("utf8");
      res.on("data", (c) => (d += c));
      res.on("end", () => resolve(d));
    }).on("error", reject);
  });
}

async function detectFps(mpdUrl) {
  const teks = await ambilTeks(mpdUrl);
  // MPD memakai atribut frameRate pada Representation. Master playlist HLS
  // memakai FRAME-RATE di dalam EXT-X-STREAM-INF, dan nilainya sudah desimal.
  // Tanpa cabang kedua ini, mode hls selalu gagal mendeteksi fps lalu berhenti.
  const mDash = teks.match(/frameRate="([^"]+)"/);
  if (mDash) return parseFrameRate(mDash[1]);
  const mHls = teks.match(/FRAME-RATE=([\d.]+)/);
  if (mHls) return parseFrameRate(mHls[1]);
  return null;
}

// ---------------- driver ----------------
(async () => {
  let FPS;
  if (FPS_ARG === "auto") {
    // Kegagalan deteksi TIDAK lagi jatuh diam-diam ke 24. Nilai fps masuk ke
    // perhitungan O22 P.1203, sehingga nilai yang keliru merusak label seluruh
    // run tanpa memunculkan tanda apa pun, dan baru terlihat sebagai akurasi
    // yang buruk berbulan-bulan kemudian.
    let d = null, galat = null;
    try { d = await detectFps(MPD); } catch (e) { galat = e.message; }
    if (d) { FPS = d; console.log(`fps: ${FPS.toFixed(3)} (terdeteksi dari MPD)`); }
    else {
      console.error(`fps: GAGAL dideteksi dari MPD` + (galat ? ` (${galat})` : ""));
      console.error("  Nilai fps masuk ke perhitungan skor P.1203, sehingga");
      console.error("  melanjutkan dengan tebakan akan merusak label run ini.");
      console.error("  Periksa MPD-nya, atau beri nilai eksplisit: --fps 24");
      process.exit(3);
    }
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
    // --ignore-certificate-errors diperlukan untuk MPD yang disajikan lewat HTTPS
    // dengan sertifikat mandiri. Tanpa ini Chromium menolak koneksi dan
    // quality_timeline keluar kosong tanpa pesan yang jelas.
    args: ["--no-sandbox", "--disable-dev-shm-usage",
           "--autoplay-policy=no-user-gesture-required",
           "--ignore-certificate-errors",
           // --quic-version SENGAJA tidak diberikan. Nilai yang tidak dikenal
           // versi Chromium yang dipakai justru mematikan QUIC sepenuhnya, dan
           // membiarkannya dinegosiasikan lebih aman.
           ...(QUIC ? ["--enable-quic", `--origin-to-force-quic-on=${QUIC}`] : []),
           ...(SPKI ? [`--ignore-certificate-errors-spki-list=${SPKI}`] : [])],
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
      player: PLAYER,
      quic_forced: QUIC || null,
      spki_pinned: SPKI ? true : false,
      codec: CODEC,
      display: { width: dispW, height: dispH },
      throttle: { preset: THROTTLE, downloadThroughput_Bps: dlBps, latency_ms: LATENCY },
      // Mode ABR dicatat agar dapat dipakai sebagai variabel analisis. Sesi
      // BOLA memutus kolinearitas throughput-ke-label yang muncul pada aturan
      // berbasis throughput.
      abr: { requested: tel.data.abr_mode || "dynamic",
             effective: tel.data.abr_effective || "unknown" },
      quality_timeline: tel.data.quality_timeline,
      stalls: tel.data.stalls,
      events_raw: tel.data.events,
    };
    fs.writeFileSync(OUT, JSON.stringify(meta, null, 2));
    console.log(`OK: ${meta.quality_timeline.length} perpindahan kualitas, ${meta.stalls.length} stall, media ${meta.media_duration}s, abr ${meta.abr.requested}`);
    console.log(`-> ${OUT}`);
  } finally {
    await browser.close();
    server.close();
  }
})().catch((e) => { console.error("ERROR:", e); process.exit(1); });