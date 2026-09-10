#!/usr/bin/env python3
"""
bg_traffic.py — trafik latar yang bersaing (jalankan di LAPTOP klien)
-----------------------------------------------------------------------
Pada testbed dengan satu aliran, throughput yang teramati sensor praktis sama
dengan bandwidth yang tersedia bagi pemutar, sehingga ABR memilih representasi
secara hampir deterministik dari throughput. Akibatnya fitur dan label menjadi
kolinear lewat ABR, dan skor klasifikasi mencerminkan pemetaan itu, bukan
inferensi QoE.

Aliran pesaing memutus kesamaan tersebut: sebagian bandwidth dipakai trafik lain,
sehingga sensor melihat total sementara pemutar hanya mendapat sisanya.

Berjalan sebagai proses terpisah yang mengunduh berkas dari server yang sama,
dengan jeda dan ukuran acak namun reprodusibel lewat --seed. Catatan aktivitas
disimpan agar dapat diperhitungkan saat analisis.

Pakai:
  python bg_traffic.py --server http://192.168.50.10:8080 --seed 1 --duration 300
  python bg_traffic.py --server http://192.168.50.10:8080 --seed 1 --dry-run
  python bg_traffic.py --self-test
"""
import argparse
import json
import random
import signal
import sys
import threading
import time
import urllib.request

_stop = threading.Event()

# TARGET_AKTIF adalah porsi WAKTU saat trafik latar aktif. Terlalu tinggi akan
# menenggelamkan sinyal video; terlalu rendah tidak mengubah apa pun. Sekitar
# sepertiga cukup untuk memutus determinisme tanpa mendominasi.
TARGET_AKTIF = 0.35
JEDA_MIN_S, JEDA_MAKS_S = 4, 25
BURST_MIN_S, BURST_MAKS_S = 3, 12

# Peluang per SELANG berbeda dari porsi waktu, karena burst rata-rata lebih
# pendek daripada jeda. Turunkan peluangnya dari target waktu agar keduanya
# konsisten: p*E[burst] / (p*E[burst] + (1-p)*E[jeda]) = TARGET_AKTIF
_EB = (BURST_MIN_S + BURST_MAKS_S) / 2
_EJ = (JEDA_MIN_S + JEDA_MAKS_S) / 2
P_AKTIF = (TARGET_AKTIF * _EJ) / (_EB * (1 - TARGET_AKTIF) + TARGET_AKTIF * _EJ)


def _sig(a, b):
    _stop.set()


def buat_jadwal(seed, total_s):
    """[(t_mulai, durasi, aktif)] menutupi seluruh rentang tanpa celah."""
    rng = random.Random(seed * 7919 + 13)     # offset agar tak sefase dgn tc
    out, t = [], 0.0
    while t < total_s:
        if rng.random() < P_AKTIF:
            d = min(rng.uniform(BURST_MIN_S, BURST_MAKS_S), total_s - t)
            out.append((round(t, 2), round(d, 2), True))
        else:
            d = min(rng.uniform(JEDA_MIN_S, JEDA_MAKS_S), total_s - t)
            out.append((round(t, 2), round(d, 2), False))
        t += d
    return out


def unduh(url, sampai, catat, max_bps):
    """Unduh berulang sampai batas waktu, dengan laju dibatasi.

    Pembatasan laju WAJIB. Tanpa itu, pada tahap tanpa pembatasan tc, dua aliran
    paralel menyaturasi kabel gigabit hingga sekitar 890 Mbps sehingga video
    justru kelaparan dan kelas Excellent tidak pernah tercapai. Yang diinginkan
    adalah pesaing yang memutus determinisme, bukan yang mendominasi.
    """
    total = 0
    t_mulai = time.time()
    while not _stop.is_set() and time.time() < sampai:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "bg-traffic/1.0"})
            with urllib.request.urlopen(req, timeout=5) as r:
                while not _stop.is_set() and time.time() < sampai:
                    blok = r.read(16384)
                    if not blok:
                        break
                    total += len(blok)
                    if max_bps > 0:
                        # tunggu sampai laju rata-rata turun ke batas
                        target = t_mulai + total * 8 / max_bps
                        jeda = target - time.time()
                        if jeda > 0:
                            _stop.wait(min(jeda, 0.5))
        except Exception:
            time.sleep(0.4)               # server sibuk atau koneksi putus, coba lagi
    catat["bytes"] = catat.get("bytes", 0) + total


def self_test():
    j = buat_jadwal(1, 300)
    assert abs(sum(d for _, d, _ in j) - 300) < 0.5, sum(d for _, d, _ in j)
    print(f"  [OK] jadwal menutupi 300 detik penuh dalam {len(j)} selang")

    assert buat_jadwal(5, 300) == buat_jadwal(5, 300)
    assert buat_jadwal(5, 300) != buat_jadwal(6, 300)
    print("  [OK] reprodusibel per seed, berbeda antar seed")

    porsi = []
    for s in range(40):
        jj = buat_jadwal(s, 300)
        porsi.append(sum(d for _, d, a in jj if a) / sum(d for _, d, _ in jj))
    rata = sum(porsi) / len(porsi)
    print(f"  [OK] porsi WAKTU aktif rata-rata {rata*100:.0f}% "
          f"(target {TARGET_AKTIF*100:.0f}%, peluang per selang {P_AKTIF*100:.0f}%)")
    assert abs(rata - TARGET_AKTIF) < 0.08, (rata, TARGET_AKTIF)

    # tidak boleh sefase dengan jadwal tc, agar kondisi jaringan dan trafik latar
    # tidak selalu berubah bersamaan
    # t=0 selalu awal selang pertama, jadi dikecualikan dari uji sebaran fase
    from collections import Counter
    awal = Counter(round(t) for s in range(30) for t, _, a in buat_jadwal(s, 300)
                   if a and t > 1)
    assert max(awal.values()) < 8, awal.most_common(3)
    print(f"  [OK] waktu mulai burst tersebar ({len(awal)} titik berbeda), "
          f"tidak sefase antar seed")
    # pembatas laju harus benar-benar menahan
    import http.server, socketserver, threading as th2
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            data = b"x" * 200000
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def do_HEAD(self):
            self.send_response(200); self.send_header("Content-Length","200000"); self.end_headers()
        def log_message(self, *a): pass
    srv = socketserver.TCPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    th2.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/x"
    for batas_mbps in (2.0, 8.0):
        catat = {}
        t0 = time.time()
        unduh(url, t0 + 3.0, catat, batas_mbps * 1e6)
        laju = catat["bytes"] * 8 / (time.time() - t0) / 1e6
        assert laju <= batas_mbps * 1.35, (laju, batas_mbps)
        print(f"  [OK] batas {batas_mbps:.0f} Mbps -> terukur {laju:.2f} Mbps")
    srv.shutdown()
    print("\nSEMUA UJI LULUS")
    return True


def main():
    ap = argparse.ArgumentParser(description="Trafik latar yang bersaing")
    ap.add_argument("--server", default="http://192.168.50.10:8080")
    ap.add_argument("--path", default=None,
                    help="berkas yang diunduh; default segmen besar BigBuckBunny")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--duration", type=float, default=300)
    ap.add_argument("--streams", type=int, default=1,
                    help="aliran paralel saat burst")
    ap.add_argument("--max-mbps", type=float, default=1.5,
                    help="batas laju TOTAL trafik latar (Mbps); 0 = tanpa batas. "
                         "Tanpa batas, kabel gigabit tersaturasi dan video kelaparan")
    ap.add_argument("--log", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        sys.exit(0 if self_test() else 1)

    path = a.path or "/BigBuckBunny/bunny_3936261bps/BigBuckBunny_4s10.m4s"
    url = a.server.rstrip("/") + path
    jadwal = buat_jadwal(a.seed, a.duration)

    batas = f"{a.max_mbps:.1f} Mbps" if a.max_mbps > 0 else "TANPA BATAS"
    print(f">> seed {a.seed} | {len(jadwal)} selang | {a.streams} aliran | batas {batas}")
    print(f">> url {url}")
    if a.dry_run:
        for t, d, aktif in jadwal:
            print(f"   t={t:>6.1f}s ({d:>5.1f}s)  {'BURST' if aktif else 'diam'}")
        aktif_s = sum(d for _, d, x in jadwal if x)
        print(f"\n   total aktif {aktif_s:.0f}s dari {a.duration:.0f}s "
              f"({aktif_s/a.duration*100:.0f}%)")
        return

    # Pastikan berkasnya benar-benar ada sebelum run dimulai, supaya tidak
    # menghabiskan lima menit menghasilkan nol trafik.
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=5) as r:
            print(f">> berkas terjangkau ({r.headers.get('Content-Length','?')} byte)")
    except Exception as e:
        sys.exit(f"berkas tidak terjangkau: {e}\ncoba --path lain")

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    log = a.log or f"bg_seed{a.seed}.jsonl"
    fh = open(log, "w", encoding="utf-8")
    t0 = time.time()
    total_bytes = 0
    print(f">> catatan -> {log}\n")

    for t, d, aktif in jadwal:
        if _stop.is_set():
            break
        # tunggu sampai giliran selang ini
        tunggu = t0 + t - time.time()
        if tunggu > 0:
            _stop.wait(tunggu)
        if _stop.is_set():
            break

        rec = {"t_epoch": round(time.time(), 3), "durasi": d, "aktif": aktif}
        if aktif:
            sampai = time.time() + d
            catat = {}
            per_aliran = (a.max_mbps * 1e6 / a.streams) if a.max_mbps > 0 else 0
            th = [threading.Thread(target=unduh,
                                   args=(url, sampai, catat, per_aliran), daemon=True)
                  for _ in range(a.streams)]
            [x.start() for x in th]
            [x.join(timeout=d + 3) for x in th]
            rec["bytes"] = catat.get("bytes", 0)
            total_bytes += rec["bytes"]
            print(f"[{time.strftime('%H:%M:%S')}] burst {d:>5.1f}s -> "
                  f"{rec['bytes']/1e6:>6.2f} MB")
        else:
            _stop.wait(d)
        fh.write(json.dumps(rec) + "\n")
        fh.flush()

    fh.close()
    lama = time.time() - t0
    aktif_s = sum(d for _, d, x in jadwal if x)
    print(f"\n>> selesai {lama:.0f}s | total {total_bytes/1e6:.1f} MB")
    print(f">> rata-rata sepanjang run   : {total_bytes*8/lama/1e6:.2f} Mbps")
    if aktif_s:
        # Angka inilah yang menentukan seberapa nyata kompetisinya. Rata-rata
        # sepanjang run selalu tampak kecil karena sebagian besar waktu diam.
        print(f">> rata-rata SELAMA burst    : {total_bytes*8/aktif_s/1e6:.2f} Mbps "
              f"({aktif_s:.0f}s aktif dari {lama:.0f}s)")
    print(f">> catatan tersimpan di {log}")


if __name__ == "__main__":
    main()