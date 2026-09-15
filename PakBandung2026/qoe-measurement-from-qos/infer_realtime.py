#!/usr/bin/env python3
"""
infer_realtime.py — FASE 2: inferensi QoE real-time di perangkat edge
-----------------------------------------------------------------------
Membaca map eBPF, menghitung fitur QoS, dan mengeluarkan kelas QoE setiap
window. TIDAK memakai telemetri klien, manifest, maupun P.1203. Hanya paket
yang lewat.

Yang diukur di sini BUKAN akurasi (itu sudah ditetapkan saat pelatihan),
melainkan kelayakan operasional:
  - latensi dari window ditutup sampai kelas dikeluarkan
  - apakah loop sanggup mengikuti irama window tanpa tertinggal
  - kestabilan dalam durasi panjang

Pakai (di Raspberry Pi 5, sensor XDP sudah terpasang):
  sudo python3 infer_realtime.py --model model_dasar/DecisionTree.joblib
  sudo python3 infer_realtime.py --model tree_standalone.py --pure
  python3 infer_realtime.py --self-test

Urutan fitur WAJIB sama dengan saat pelatihan.
"""
import argparse
import csv
import json
import math
import os
import signal
import socket
import struct
import subprocess
import sys
import time

# Urutan ini HARUS identik dengan kolom yang dipakai train_compare.py
FITUR = ["throughput_mean", "throughput_std", "throughput_min", "throughput_max",
         "total_bytes", "total_packets", "active_flows"]
MAP_NAME = "flows"
# Ambang trafik minimal agar sebuah window dianggap dapat diinferensi. Nilainya
# sama dengan penyaringan saat pelatihan.
MIN_TP_MBPS = 0.01
# Jumlah cuplikan per window pada koleksi data, yaitu acuan yang HARUS disamai
# Fase 2. Nilainya BERBEDA antar koleksi: v4 terukur sekitar 30 (3,0 Hz) karena
# map eBPF menumpuk entri, sedangkan v5 sekitar 98 (9,8 Hz) karena sensor dimuat
# ulang sebelum tiap run. Menyamakannya penting karena throughput_std, _min,
# _max, dan seluruh fitur jendela pendek bergantung pada laju cuplik.
LATIH_N_CUPLIK = 98
_stop = False


def _on_signal(sig, frame):
    global _stop
    _stop = True


# ---------------------------------------------------------------- baca map
def ip_str(v):
    return socket.inet_ntoa(struct.pack("<I", v & 0xFFFFFFFF))


def port_num(v):
    return struct.unpack(">H", struct.pack("<H", v & 0xFFFF))[0]


def _as_bytes(arr):
    return bytes(int(b, 16) if isinstance(b, str) else int(b) for b in arr)


def parse_dump(entries):
    """Tangani dua format bpftool: dict ber-BTF dan array byte mentah."""
    flows = {}
    for e in entries:
        k, v = e.get("key"), e.get("value")
        if isinstance(k, dict) and isinstance(v, dict):
            try:
                key = (ip_str(k["saddr"]), port_num(k["sport"]),
                       ip_str(k["daddr"]), port_num(k["dport"]))
                flows[key] = (int(v["bytes"]), int(v["packets"]),
                              int(v.get("reorder", v.get("retrans", 0))),
                              int(v.get("jitter_ns", 0)),
                              int(v.get("rtt_sum_ns", 0)), int(v.get("rtt_cnt", 0)))
            except (KeyError, TypeError, ValueError):
                continue
        elif isinstance(k, list) and isinstance(v, list):
            try:
                kb, vb = _as_bytes(k), _as_bytes(v)
                if len(kb) < 12 or len(vb) < 16:
                    continue
                sa, da, sp, dp = struct.unpack("<IIHH", kb[:12])
                nb, npk = struct.unpack_from("<QQ", vb, 0)
                # Offset mengikuti struct sensor lengkap (80 B). Struct lama
                # tanpa jitter dan RTT tetap didukung: medan yang tidak ada
                # dibaca sebagai nol, sehingga fitur turunannya bernilai nol.
                jit = struct.unpack_from("<Q", vb, 40)[0] if len(vb) >= 48 else 0
                rtx = struct.unpack_from("<Q", vb, 48)[0] if len(vb) >= 56 else 0
                rs = struct.unpack_from("<Q", vb, 64)[0] if len(vb) >= 72 else 0
                rc = struct.unpack_from("<Q", vb, 72)[0] if len(vb) >= 80 else 0
                flows[(ip_str(sa), port_num(sp), ip_str(da), port_num(dp))] = (
                    nb, npk, rtx, jit, rs, rc)
            except (struct.error, TypeError, ValueError):
                continue
    return flows


def map_ids(nama=MAP_NAME):
    out = subprocess.run(["bpftool", "-j", "map", "show"],
                         capture_output=True, text=True, check=True).stdout
    m = json.loads(out)
    if isinstance(m, dict):
        m = [m]
    return [x["id"] for x in m if x.get("name") == nama]


def baca(ids):
    gab = {}
    for mid in ids:
        try:
            out = subprocess.run(["bpftool", "-j", "map", "dump", "id", str(mid)],
                                 capture_output=True, text=True, check=True).stdout
            for k, v in parse_dump(json.loads(out)).items():
                gab[(mid,) + k] = v
        except Exception:
            pass
    return gab


def deltas(prev, cur):
    """Selisih antar cuplikan, direplikasi sama persis dari qos_agent.py.

    Pembobotannya tidak sembarang. Jitter adalah LEVEL dari estimator EWMA
    sehingga dibobot PAKET. RTT dibobot BYTE karena kedua arah menghasilkan
    sampel untuk ruas jalur yang BERBEDA, dan aliran data server ke klien
    menguasai hampir seluruh byte sehingga pembobotan byte memilih ruas tempat
    NetEm dipasang. Salah membobot akan menghasilkan fitur yang berbeda dari
    yang dipakai saat pelatihan, tanpa satu pun pesan kesalahan.
    """
    d_bytes = d_pkts = d_rtx = n_active = 0
    jit_bobot = jit_total = 0
    rtt_bobot = rtt_total = 0
    for key, cv in cur.items():
        cv = tuple(cv) + (0,) * (6 - len(cv))
        b, p, rtx, jit, rs, rc = cv[:6]
        pv = tuple(prev.get(key, ())) + (0,) * 6
        pb, pp, prtx, _, prs, prc = pv[:6]
        db = (b - pb) if b >= pb else b
        dp = (p - pp) if p >= pp else p
        d_bytes += db
        d_pkts += dp
        d_rtx += (rtx - prtx) if rtx >= prtx else rtx
        drs = (rs - prs) if rs >= prs else rs
        drc = (rc - prc) if rc >= prc else rc
        if db > 0:
            n_active += 1
            jit_total += jit * dp
            jit_bobot += dp
            if drc > 0:
                rtt_total += (drs / drc) * db
                rtt_bobot += db
    return (d_bytes, d_pkts, d_rtx,
            (jit_total / jit_bobot) if jit_bobot else 0.0,
            (rtt_total / rtt_bobot) if rtt_bobot else 0.0,
            n_active)


# ---------------------------------------------------------------- fitur
DASAR = ["throughput_mean", "throughput_std", "throughput_min", "throughput_max",
         "total_bytes", "total_packets", "active_flows"]
JR_RTT = ["jitter_mean", "jitter_p95", "reorder_rate", "reorder_count",
          "rtt_mean", "rtt_p95", "rtt_std"]
MS = ["tp_slot_akhir", "tp_pendek_mean", "tp_pendek_std", "tp_pendek_max",
      "tp_delta_prev", "tp_rasio_prev", "pkt_delta_prev",
      "tp_kumulatif", "tp_rasio_kumulatif", "bytes_kumulatif", "rasio_diam"]
SET_FITUR = {"dasar": DASAR, "semua": DASAR + JR_RTT,
             "multiskala": DASAR + MS, "lengkap": DASAR + JR_RTT + MS}


def p95(v):
    if not v:
        return 0.0
    w = sorted(v)
    return w[min(len(w) - 1, int(round(0.95 * (len(w) - 1))))]


def hitung_fitur(samples, set_fitur="dasar", riwayat=None, pendek_detik=3.0):
    """samples = [(d_bytes, d_pkts, d_rtx, jitter_ns, rtt_ns, dt, n_aktif), ...]

    Rumusnya HARUS identik dengan align_qos.py dan fitur_lanjutan.py yang dipakai
    saat membangun data latih. Perbedaan sekecil apa pun membuat model menerima
    masukan dengan distribusi berbeda dari yang dilatihkan, tanpa satu pun pesan
    kesalahan.

    riwayat = dict berisi keadaan antar-window untuk fitur multi-cakupan, yaitu
    nilai window sebelumnya dan akumulasi sejak sesi dimulai. Dipakai dan
    diperbarui di tempat.
    """
    mbps = [(s[0] * 8.0) / (s[5] * 1e6) for s in samples if s[5] > 0]
    if not mbps:
        return None
    n = len(mbps)
    mean = sum(mbps) / n
    var = sum((x - mean) ** 2 for x in mbps) / n          # populasi, bukan sampel
    tb = sum(s[0] for s in samples)
    tp = sum(s[1] for s in samples)
    f = {"throughput_mean": round(mean, 6), "throughput_std": round(math.sqrt(var), 6),
         "throughput_min": round(min(mbps), 6), "throughput_max": round(max(mbps), 6),
         "total_bytes": tb, "total_packets": tp,
         "active_flows": max(s[6] for s in samples)}

    if set_fitur in ("semua", "lengkap"):
        jit = [s[3] / 1e6 for s in samples]               # ns -> ms
        rtt = [s[4] / 1e6 for s in samples if s[4] > 0]
        tr = sum(s[2] for s in samples)
        m = sum(rtt) / len(rtt) if rtt else 0.0
        f.update({
            "jitter_mean": round(sum(jit) / len(jit), 6) if jit else 0.0,
            "jitter_p95": round(p95(jit), 6),
            "reorder_rate": round(tr / tp * 100.0, 6) if tp else 0.0,
            "reorder_count": tr,
            "rtt_mean": round(m, 6), "rtt_p95": round(p95(rtt), 6),
            "rtt_std": round(math.sqrt(sum((x - m) ** 2 for x in rtt) / len(rtt)), 6)
                       if rtt else 0.0,
        })

    if set_fitur in ("multiskala", "lengkap"):
        if riwayat is None:
            riwayat = {}
        f["tp_slot_akhir"] = round(mbps[-1], 6)
        t_akhir = sum(s[5] for s in samples)
        akum, pendek = 0.0, []
        for s in reversed(samples):
            akum += s[5]
            pendek.append((s[0] * 8.0) / (s[5] * 1e6) if s[5] > 0 else 0.0)
            if akum >= pendek_detik:
                break
        mp = sum(pendek) / len(pendek) if pendek else 0.0
        vp = (sum((x - mp) ** 2 for x in pendek) / len(pendek)) if pendek else 0.0
        f["tp_pendek_mean"] = round(mp, 6)
        f["tp_pendek_std"] = round(math.sqrt(vp), 6)
        f["tp_pendek_max"] = round(max(pendek), 6) if pendek else 0.0

        tp_prev = riwayat.get("tp_prev", 0.0)
        pkt_prev = riwayat.get("pkt_prev", 0)
        f["tp_delta_prev"] = round(f["throughput_mean"] - tp_prev, 6)
        f["tp_rasio_prev"] = round(f["throughput_mean"] / tp_prev, 6) \
            if tp_prev > 1e-9 else 0.0
        f["pkt_delta_prev"] = tp - pkt_prev

        kb = riwayat.get("kum_bytes", 0) + tb
        kd = riwayat.get("kum_durasi", 0.0) + sum(s[5] for s in samples)
        f["tp_kumulatif"] = round(kb * 8 / (kd * 1e6), 6) if kd > 0 else 0.0
        f["tp_rasio_kumulatif"] = round(f["throughput_mean"] / f["tp_kumulatif"], 6) \
            if f["tp_kumulatif"] > 1e-9 else 0.0
        f["bytes_kumulatif"] = kb
        f["rasio_diam"] = round(sum(1 for x in mbps if x < 0.01) / len(mbps), 6)

        riwayat.update({"tp_prev": f["throughput_mean"], "pkt_prev": tp,
                        "kum_bytes": kb, "kum_durasi": kd})

    return [f[k] for k in SET_FITUR[set_fitur]]


# ---------------------------------------------------------------- model
def muat_model(path, pure):
    """Kembalikan fungsi predict(list_fitur) -> str."""
    if pure:
        ruang = {}
        exec(compile(open(path, encoding="utf-8").read(), path, "exec"), ruang)
        f = ruang.get("predict")
        if f is None:
            raise RuntimeError(f"{path} tidak punya fungsi predict()")
        return f, "python murni"
    import joblib
    est = joblib.load(path)
    return (lambda v: str(est.predict([v])[0])), "joblib/sklearn"


# ---------------------------------------------------------------- uji mandiri
def self_test():
    # bentuk cuplikan: (d_bytes, d_pkts, d_rtx, jitter_ns, rtt_ns, dt, n_aktif)
    s = [(1_250_000, 830, 0, 2_000_000, 40_000_000, 1.0, 1) for _ in range(10)]
    f = hitung_fitur(s)
    assert abs(f[0] - 10.0) < 1e-6 and f[1] < 1e-9, f
    assert f[4] == 12_500_000 and f[5] == 8300 and f[6] == 1
    print(f"  [OK] fitur konstan: mean {f[0]} Mbps, std {f[1]}")

    s2 = [(1_250_000, 830, 0, 2_000_000, 40_000_000, 1.0, 1),
          (0, 0, 0, 0, 0, 1.0, 0)] * 5
    f2 = hitung_fitur(s2)
    assert abs(f2[0] - 5.0) < 1e-6 and f2[2] == 0.0 and abs(f2[3] - 10.0) < 1e-6
    assert f2[1] > 4.9, f2[1]
    print(f"  [OK] pola hidup mati: mean {f2[0]}, min {f2[2]}, maks {f2[3]}, std {f2[1]:.2f}")

    assert len(f) == len(FITUR) == len(SET_FITUR["dasar"])
    print(f"  [OK] jumlah fitur {len(f)} cocok dgn urutan pelatihan")

    # set fitur lanjutan: jumlah dan nilai jitter/RTT harus benar
    f14 = hitung_fitur(s, "semua")
    assert len(f14) == 14, len(f14)
    i_jit = SET_FITUR["semua"].index("jitter_mean")
    i_rtt = SET_FITUR["semua"].index("rtt_mean")
    assert abs(f14[i_jit] - 2.0) < 1e-6, f14[i_jit]      # 2e6 ns -> 2 ms
    assert abs(f14[i_rtt] - 40.0) < 1e-6, f14[i_rtt]     # 4e7 ns -> 40 ms
    print(f"  [OK] set 'semua': 14 fitur, jitter {f14[i_jit]} ms, "
          f"rtt {f14[i_rtt]} ms (ns diubah ke ms)")

    riw = {}
    f18a = hitung_fitur(s, "multiskala", riw)
    f18b = hitung_fitur(s, "multiskala", riw)
    assert len(f18a) == 18
    i_dp = SET_FITUR["multiskala"].index("tp_delta_prev")
    i_kb = SET_FITUR["multiskala"].index("bytes_kumulatif")
    assert abs(f18a[i_dp] - 10.0) < 1e-6, f18a[i_dp]     # window pertama: delta = nilai
    assert abs(f18b[i_dp]) < 1e-6, f18b[i_dp]            # window kedua: tidak berubah
    assert f18b[i_kb] == 2 * f18a[i_kb], (f18a[i_kb], f18b[i_kb])
    print(f"  [OK] set 'multiskala': 18 fitur, keadaan antar-window bertahan "
          f"(kumulatif {f18a[i_kb]:,} -> {f18b[i_kb]:,} byte)")

    assert len(hitung_fitur(s, "lengkap", {})) == 25
    print("  [OK] set 'lengkap': 25 fitur")

    kb = struct.pack("<IIHH", 171092160, 338864320, 36895, 11577)
    vb = struct.pack("<QQ", 500, 3)
    got = parse_dump([{"key": [hex(b) for b in kb], "value": [hex(b) for b in vb]}])
    assert got[("192.168.50.10", 8080, "192.168.50.20", 14637)][:2] == (500, 3)
    print("  [OK] parsing map (byte order & format mentah)")

    # struct lengkap 80 B: jitter di offset 40, reorder 48, rtt_sum 64, rtt_cnt 72
    vb80 = bytearray(80)
    struct.pack_into("<QQ", vb80, 0, 500, 3)
    struct.pack_into("<Q", vb80, 40, 2_000_000)
    struct.pack_into("<Q", vb80, 48, 7)
    struct.pack_into("<Q", vb80, 64, 80_000_000)
    struct.pack_into("<Q", vb80, 72, 2)
    g80 = parse_dump([{"key": [hex(b) for b in kb],
                       "value": [hex(b) for b in bytes(vb80)]}])
    v = g80[("192.168.50.10", 8080, "192.168.50.20", 14637)]
    assert v == (500, 3, 7, 2_000_000, 80_000_000, 2), v
    print("  [OK] struct lengkap 80 B: jitter, reorder, dan RTT terbaca di offset benar")

    assert deltas({("a",): (100, 10)}, {("a",): (150, 15)})[:3] == (50, 5, 0)
    assert deltas({("a",): (100, 10)}, {("a",): (100, 10)})[:3] == (0, 0, 0)
    print("  [OK] perhitungan selisih & aliran aktif")

    # jitter dibobot PAKET, RTT dibobot BYTE; salah bobot memberi angka berbeda
    pv = {("a",): (0, 0, 0, 0, 0, 0), ("b",): (0, 0, 0, 0, 0, 0)}
    cv = {("a",): (1000, 10, 0, 10_000_000, 100_000_000, 10),   # rtt 10 ms
          ("b",): (9000, 90, 0, 20_000_000, 900_000_000, 10)}   # rtt 90 ms
    _, _, _, jit, rtt, akt = deltas(pv, cv)
    assert abs(jit - 19_000_000) < 1, jit        # (10*10 + 20*90)/100 juta ns
    assert abs(rtt - 82_000_000) < 1, rtt        # (10*1000 + 90*9000)/10000 juta ns
    assert akt == 2
    print(f"  [OK] jitter dibobot paket ({jit/1e6:.1f} ms), "
          f"RTT dibobot byte ({rtt/1e6:.1f} ms)")

    kosong = hitung_fitur([(0, 0, 0, 0, 0, 1.0, 0) for _ in range(10)])
    assert kosong is not None and kosong[0] < MIN_TP_MBPS, kosong
    print(f"  [OK] window tanpa trafik terdeteksi (tp {kosong[0]} < {MIN_TP_MBPS}) "
          f"dan akan dilaporkan TANPA-DATA, bukan diklasifikasi")

    # kolom CSV harus mengikuti set fitur, bukan daftar tujuh fitur baku
    for nama in ("dasar", "semua", "multiskala", "lengkap"):
        kol = ["window", "t_epoch", "kelas"] + SET_FITUR[nama]
        nilai = hitung_fitur(s, nama, {})
        assert len(nilai) == len(SET_FITUR[nama]), (nama, len(nilai))
        assert len(set(kol)) == len(kol), f"kolom duplikat pada set {nama}"
    print("  [OK] kolom CSV mengikuti set fitur (7, 14, 18, dan 25) tanpa duplikat")
    # window harus ditutup oleh waktu, bukan jumlah cuplikan
    W = 10.0
    t0 = 1000.0
    tw = t0
    tutup = []
    now = t0
    for i in range(40):
        now += 0.5                      # periode cuplik 0,5s, bukan 0,1s
        if now - tw >= W:
            tutup.append(round(now - tw, 1))
            tw = now
    assert tutup and all(abs(x - W) < 0.6 for x in tutup), tutup
    print(f"  [OK] window ditutup tiap ~{tutup[0]}s meski periode cuplik 0,5s "
          f"({len(tutup)} window dari 20s)")
    # kompensasi periode: laju harus tetap meski biaya baca berubah
    P = 0.33
    for biaya in (0.01, 0.20):
        t = 0.0
        sasaran = t + P
        n = 0
        while t < 10.0:
            tidur = sasaran - t
            if tidur > 0:
                t += tidur
            sasaran += P
            t += biaya                  # biaya pembacaan map
            n += 1
        assert abs(n - 10.0 / P) <= 2, (biaya, n)
        print(f"  [OK] biaya baca {biaya*1000:.0f} ms -> {n} cuplikan per 10s "
              f"(sasaran {10.0/P:.0f}), periode tetap")
    print("\nSEMUA UJI LULUS")
    return True


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Fase 2: inferensi QoE real-time")
    ap.add_argument("--model", default="model_dasar/DecisionTree.joblib")
    ap.add_argument("--pure", action="store_true",
                    help="model berupa berkas .py mandiri (tanpa sklearn)")
    ap.add_argument("--iface", default="eth0")
    ap.add_argument("--map", default=MAP_NAME)
    # PERIODE cuplik sasaran, bukan waktu tidur. Waktu tidur dikompensasi
    # terhadap biaya pembacaan map, sehingga laju menjadi parameter terkendali
    # dan tidak lagi bergantung pada jumlah entri map.
    #
    # Bawaan 0,33 detik dipilih agar laju mendekati 3 Hz, yaitu laju yang
    # terukur pada koleksi data v4 (median 30 cuplikan per window 10 detik).
    # Kecocokan ini WAJIB: throughput_std, _min, dan _max dihitung atas
    # cuplikan di dalam window, sehingga laju yang berbeda mengubah distribusi
    # fitur tanpa memunculkan satu pun pesan kesalahan. Pada map bersih,
    # pengaturan lama menghasilkan 9,5 Hz, yaitu tiga kali laju pelatihan.
    ap.add_argument("--poll", type=float, default=0.10,
                    help="periode cuplik sasaran dalam detik; 0.10 menyamai "
                         "laju sekitar 10 Hz pada koleksi data v5. Untuk model "
                         "yang dilatih pada v4, pakai 0.33")
    ap.add_argument("--window", type=float, default=10.0)
    ap.add_argument("--duration", type=float, default=0, help="detik; 0 = sampai dihentikan")
    ap.add_argument("--latih-n-cuplik", type=int, default=LATIH_N_CUPLIK,
                    help="jumlah cuplikan per window pada data latih model yang "
                         "dipakai; v5 sekitar 98, v4 sekitar 30. Dipakai untuk "
                         "memperingatkan bila laju Fase 2 menyimpang")
    ap.add_argument("--features", default="dasar", choices=sorted(SET_FITUR),
                    help="set fitur yang dihitung; HARUS cocok dgn model. "
                         "'semua' dan 'lengkap' menuntut sensor versi lengkap, "
                         "karena jitter dan RTT tidak ada pada sensor ringan")
    ap.add_argument("--out", default="inferensi_realtime.csv")
    ap.add_argument("--append", action="store_true",
                    help="tambahkan ke berkas lama alih-alih menimpanya. Bawaannya "
                         "menimpa, karena mode tambah membuat beberapa sesi "
                         "tercampur dalam satu berkas dan indeks window berulang "
                         "dari nol sehingga sulit dipisahkan saat analisis")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        sys.exit(0 if self_test() else 1)
    if not os.path.exists(a.model):
        sys.exit(f"model tidak ketemu: {a.model}")

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    t0 = time.perf_counter()
    predict, jenis = muat_model(a.model, a.pure)
    t_muat = (time.perf_counter() - t0) * 1000

    try:
        link = json.loads(subprocess.run(["ip", "-d", "-j", "link", "show", a.iface],
                                         capture_output=True, text=True, check=True).stdout)
        prog = ((link[0] or {}).get("xdp") or {}).get("prog") or {}
        if not prog.get("id"):
            print(f">> PERINGATAN: tidak ada program XDP di {a.iface}; map tidak akan terisi")
    except Exception:
        pass

    ids = map_ids(a.map)
    print(f">> model  : {os.path.basename(a.model)} ({jenis}), dimuat {t_muat:.1f} ms")
    per_win = max(1, int(round(a.window / a.poll)))
    print(f">> map    : {ids} | window {a.window}s | periode cuplik {a.poll}s "
          f"({1/a.poll:.1f} Hz) -> ~{per_win} cuplikan per window")
    acuan = a.latih_n_cuplik
    print(f">> data latih memakai ~{acuan} cuplikan per window "
          f"({acuan/a.window:.1f} Hz)")
    if not (0.5 <= per_win / acuan <= 2.0):
        print(f">> PERINGATAN: ~{per_win} cuplikan per window menyimpang jauh dari "
              f"{acuan} saat pelatihan.")
        print(f"   throughput_std bergantung pada jumlah ini, jadi model akan "
              f"menerima masukan berdistribusi berbeda.")
    print(f">> fitur  : {a.features} ({len(SET_FITUR[a.features])} fitur)")
    print()

    mode = "a" if a.append else "w"
    baru = mode == "w" or not os.path.exists(a.out)
    fh = open(a.out, mode, newline="", encoding="utf-8")
    # Kolom mengikuti SET FITUR yang dipakai, bukan daftar tujuh fitur baku.
    # Tanpa ini, fitur jitter, RTT, dan multi-cakupan dihitung dan diumpankan ke
    # model tetapi TIDAK tercatat, sehingga tidak ada cara memverifikasi apakah
    # sensor benar-benar mengisinya.
    w = csv.DictWriter(fh, fieldnames=["window", "t_epoch", "kelas"]
                       + SET_FITUR[a.features] +
                                      ["ms_baca", "ms_fitur", "ms_infer", "ms_total", "n_cuplik"])
    if baru:
        w.writeheader()

    prev = baca(ids)
    t_mulai = time.time()
    t_prev = t_mulai
    t_window = t_mulai          # awal window yang sedang berjalan
    samples, widx = [], 0
    n_cuplik_total = 0
    per_window = max(1, int(round(a.window / a.poll)))
    ms_baca = []
    lat = []

    # Keadaan antar-window untuk fitur multi-cakupan. Harus bertahan sepanjang
    # sesi, karena fitur kumulatif dan selisih terhadap window sebelumnya tidak
    # dapat dihitung dari satu window saja.
    riwayat = {}
    # Diperiksa sekali pada window bertrafik pertama, bukan diperingatkan di muka.
    # Peringatan yang selalu tercetak melatih orang mengabaikannya; yang berguna
    # adalah memeriksa apakah jitter dan RTT benar-benar terisi.
    perlu_periksa_sensor = a.features in ("semua", "lengkap")
    t_sasaran = time.time() + a.poll
    try:
        while not _stop:
            # Tidur hanya sebanyak sisa periode. Menidurkan a.poll penuh lalu
            # membaca map membuat periode = tidur + biaya baca, sehingga laju
            # ikut berubah bila jumlah entri map berubah.
            tidur = t_sasaran - time.time()
            if tidur > 0:
                time.sleep(tidur)
            t_sasaran += a.poll
            now = time.time()
            tb = time.perf_counter()
            cur = baca(ids)
            ms_baca.append((time.perf_counter() - tb) * 1000)
            db, dp, drtx, jit, rtt, akt = deltas(prev, cur)
            samples.append((db, dp, drtx, jit, rtt, now - t_prev, akt))
            n_cuplik_total += 1
            prev, t_prev = cur, now

            # Window ditutup berdasarkan WAKTU BERLALU, bukan jumlah cuplikan.
            # Menutup setelah N cuplikan hanya benar bila periode cuplik persis
            # sama dengan --poll. Pembacaan map lewat subproses jauh lebih mahal
            # dari itu, sehingga "window 10 detik" pernah membentang 49 detik dan
            # fitur dihitung atas rentang yang melintasi beberapa kondisi
            # jaringan sekaligus. Pada koleksi data hal ini tak terlihat karena
            # align_qos.py membentuk ulang window dari timestamp.
            if now - t_window >= a.window and samples:
                # ---- latensi yang diukur: dari window ditutup sampai kelas keluar
                t1 = time.perf_counter()
                f = hitung_fitur(samples, a.features, riwayat)
                t2 = time.perf_counter()
                # Window tanpa trafik teramati TIDAK diklasifikasi. Saat
                # pelatihan, window semacam itu dibuang (throughput < 0,01),
                # sehingga model tidak pernah melihatnya dan keluarannya tidak
                # bermakna. Melaporkannya sebagai kelas QoE akan menyesatkan:
                # nol paket berarti tidak ada data untuk diinferensi, bukan
                # pengalaman yang buruk.
                if f is None:
                    kelas = "-"
                elif f[0] < MIN_TP_MBPS:
                    kelas = "TANPA-DATA"
                else:
                    kelas = predict(f)
                t3 = time.perf_counter()

                if perlu_periksa_sensor and f is not None and f[0] >= MIN_TP_MBPS:
                    perlu_periksa_sensor = False
                    kol = SET_FITUR[a.features]
                    jm = f[kol.index("jitter_mean")]
                    rm = f[kol.index("rtt_mean")]
                    if jm == 0 and rm == 0:
                        print(">> PERINGATAN: jitter dan RTT keduanya NOL pada window")
                        print("   bertrafik. Kemungkinan sensor yang terpasang versi")
                        print("   ringan, atau TCP timestamp nonaktif di klien.")
                        print("   Model menerima tujuh fitur bernilai nol dan")
                        print("   prediksinya TIDAK bermakna.")
                    else:
                        print(f">> sensor lengkap terkonfirmasi: jitter {jm:.3f} ms, "
                              f"RTT {rm:.3f} ms")

                m_fit = (t2 - t1) * 1000
                m_inf = (t3 - t2) * 1000
                m_tot = m_fit + m_inf
                lat.append(m_tot)
                m_baca = sum(ms_baca) / len(ms_baca) if ms_baca else 0

                print(f"  w{widx:<3} {kelas:<10} tp {f[0]:>7.3f} Mbps  std {f[1]:>6.3f}  "
                      f"{f[5]:>6,} pkt  n={len(samples):<3}|  fitur {m_fit:.3f} ms + infer {m_inf:.3f} ms "
                      f"= {m_tot:.3f} ms")
                w.writerow({"window": widx, "t_epoch": round(now, 3), "kelas": kelas,
                            **dict(zip(SET_FITUR[a.features], f)),
                            "ms_baca": round(m_baca, 3), "ms_fitur": round(m_fit, 4),
                            "ms_infer": round(m_inf, 4), "ms_total": round(m_tot, 4),
                            "n_cuplik": len(samples)})
                fh.flush()
                samples, ms_baca, widx = [], [], widx + 1
                t_window = now

            if a.duration and (now - t_mulai) >= a.duration:
                break
    except KeyboardInterrupt:
        pass
    finally:
        fh.close()
        if lat:
            lat_urut = sorted(lat)
            p95 = lat_urut[max(0, int(0.95 * len(lat_urut)) - 1)]
            lama_total = time.time() - t_mulai
            hz = (n_cuplik_total / lama_total) if lama_total > 0 else 0
            print(f"\n>> {widx} window diproses dalam {lama_total:.0f}s -> {a.out}")
            print(f">> laju cuplik nyata {hz:.1f} Hz "
                  f"(~{hz*a.window:.0f} cuplikan per window {a.window:.0f}s)")
            n_win = hz * a.window
            if not (0.5 <= n_win / a.latih_n_cuplik <= 2.0):
                print(f">> PERINGATAN: ~{n_win:.0f} cuplikan per window vs "
                      f"{a.latih_n_cuplik} saat pelatihan.")
                if n_win < a.latih_n_cuplik:
                    print("   Pembacaan map terlalu mahal. Muat ulang sensor agar map "
                          "bersih (systemctl restart qos-sensor).")
                else:
                    print(f"   Naikkan --poll agar mendekati "
                          f"{a.window/a.latih_n_cuplik:.2f}s.")
            print(f">> latensi window-ke-kelas: median {lat_urut[len(lat_urut)//2]:.3f} ms, "
                  f"p95 {p95:.3f} ms, maks {max(lat):.3f} ms")
            print(f">> anggaran per window {a.window*1000:.0f} ms, "
                  f"terpakai {max(lat)/(a.window*1000)*100:.4f}%")
        else:
            print("\n>> tidak ada window yang selesai diproses")


if __name__ == "__main__":
    main()