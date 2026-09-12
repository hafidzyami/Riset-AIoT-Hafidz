#!/usr/bin/env python3
"""
fitur_lanjutan.py — dua eksperimen Stage 1 dari berkas mentah yang sudah ada
------------------------------------------------------------------------------
Membentuk ulang window dari cuplikan mentah, dengan dua pilihan yang dapat
dikombinasikan. Tidak memerlukan koleksi data baru.

A. MODE PEMBENTUKAN WINDOW (--window-mode)

   media  : batas window dipetakan dari waktu media ke waktu dinding, sama
            seperti align_qos.py. Inilah yang dipakai pada evaluasi luring.
   wall   : batas window adalah interval waktu dinding tetap sejak pemutaran
            dimulai, sama seperti yang dilakukan loop Fase 2 karena di lapangan
            tidak ada telemetri klien untuk menyelaraskan.

   Membandingkan keduanya MENGISOLASI dugaan bahwa ketidakselarasan waktu
   dinding terhadap waktu media menyebabkan degradasi kelas tengah pada Fase 2.
   Bila kelas tengah ikut turun pada mode wall secara luring, mekanismenya
   terbukti. Bila tidak, penyebabnya ada pada hal lain, misalnya kesulitan
   intrinsik kelas tengah atau jumlah sampelnya yang kecil.

B. SET FITUR (--feature-set)

   dasar      : tujuh fitur turunan throughput, sama seperti sebelumnya.
   multiskala : fitur volumetrik dihitung pada EMPAT cakupan temporal sekaligus,
                yaitu cuplikan terakhir, jendela geser pendek, window penuh, dan
                kumulatif sejak sesi dimulai. Ini meniru pendekatan multi-cakupan
                ViCrypt (Wassermann dkk., IEEE TNSM 2020), yang menghitung fitur
                ringan pada beberapa skala waktu sekaligus.

   CATATAN KEJUJURAN: ini implementasi ULANG BERBASIS DESKRIPSI, bukan
   reproduksi persis daftar fitur ViCrypt. Yang diuji adalah apakah konteks
   temporal multi-cakupan menambah daya prediktif di atas agregat window
   tunggal, dan itulah yang harus dinyatakan di naskah.

Pakai:
  python fitur_lanjutan.py --dir hasil_v4 --window-mode wall --out-suffix _wall
  python fitur_lanjutan.py --dir hasil_v4 --feature-set multiskala --out-suffix _ms
  python fitur_lanjutan.py --self-test
"""
import argparse
import csv
import glob
import json
import math
import os
import sys

DASAR = ["throughput_mean", "throughput_std", "throughput_min", "throughput_max",
         "total_bytes", "total_packets", "active_flows"]


def media_to_wall(m, ps, stalls):
    """Waktu dinding saat posisi media m tercapai.

    Identik dengan align_qos.py agar mode media benar-benar mereproduksi
    pipeline luring. Stall pada posisi nol adalah buffering awal yang sudah
    tercakup di playback_start, sehingga tidak dijumlahkan lagi.
    """
    return ps + m + sum(d for pos, d in stalls if 0 < pos < m)


def load_stalls(meta):
    out = []
    for s in meta.get("stalls", []):
        out.append((float(s["position"]), float(s["duration"])))
    return sorted(out)


def ambil(r, *nama, default=0):
    """Ambil nilai kolom pertama yang ada, agar tahan variasi penamaan.

    Cuplikan mentah memakai delta_bytes dan delta_packets karena pencacah di
    kernel bersifat kumulatif sehingga agen menuliskan selisihnya. Penamaan itu
    sempat berbeda antar versi agen, jadi beberapa alias diterima.
    """
    for k in nama:
        if k in r and r[k] not in (None, ""):
            return r[k]
    return default


def stat(v):
    if not v:
        return 0.0, 0.0, 0.0, 0.0
    m = sum(v) / len(v)
    sd = math.sqrt(sum((x - m) ** 2 for x in v) / len(v))
    return m, sd, min(v), max(v)


def mbps_dari(rows):
    """Throughput per cuplikan dalam Mbps."""
    out = []
    for r in rows:
        dt = float(r["dt"])
        b = float(ambil(r, "delta_bytes", "bytes"))
        out.append(b * 8.0 / (dt * 1e6) if dt > 0 else 0.0)
    return out


def fitur_dasar(dalam):
    if not dalam:
        return {k: 0.0 for k in DASAR} | {"n_samples": 0}
    mbps = mbps_dari(dalam)
    m, sd, mn, mx = stat(mbps)
    return {"throughput_mean": round(m, 6), "throughput_std": round(sd, 6),
            "throughput_min": round(mn, 6), "throughput_max": round(mx, 6),
            "total_bytes": sum(int(ambil(r, "delta_bytes", "bytes")) for r in dalam),
            "total_packets": sum(int(ambil(r, "delta_packets", "packets")) for r in dalam),
            "active_flows": max((int(ambil(r, "active_flows")) for r in dalam), default=0),
            "n_samples": len(dalam)}


def fitur_multiskala(dalam, sebelumnya, kumulatif, pendek_detik=3.0):
    """Fitur volumetrik pada empat cakupan temporal.

    Cakupannya: cuplikan terakhir (paling responsif), jendela geser pendek,
    window penuh, dan kumulatif sejak sesi dimulai (paling stabil). Tujuannya
    memberi model konteks temporal yang tidak dimiliki agregat window tunggal.
    """
    f = fitur_dasar(dalam)
    out = dict(f)

    # cakupan 1: cuplikan terakhir dalam window
    akhir = dalam[-1:] if dalam else []
    out["tp_slot_akhir"] = round(mbps_dari(akhir)[0], 6) if akhir else 0.0

    # cakupan 2: jendela geser pendek di ujung window
    if dalam:
        t_akhir = float(dalam[-1]["t_epoch"])
        pendek = [r for r in dalam if float(r["t_epoch"]) > t_akhir - pendek_detik]
    else:
        pendek = []
    m2, sd2, _, mx2 = stat(mbps_dari(pendek))
    out["tp_pendek_mean"] = round(m2, 6)
    out["tp_pendek_std"] = round(sd2, 6)
    out["tp_pendek_max"] = round(mx2, 6)

    # cakupan 3: selisih terhadap window SEBELUMNYA (konteks temporal)
    tp_prev = sebelumnya.get("throughput_mean", 0.0) if sebelumnya else 0.0
    out["tp_delta_prev"] = round(f["throughput_mean"] - tp_prev, 6)
    out["tp_rasio_prev"] = round(f["throughput_mean"] / tp_prev, 6) if tp_prev > 1e-9 else 0.0
    out["pkt_delta_prev"] = f["total_packets"] - (
        sebelumnya.get("total_packets", 0) if sebelumnya else 0)

    # cakupan 4: kumulatif sejak sesi dimulai
    kb = kumulatif["bytes"] + f["total_bytes"]
    kd = kumulatif["durasi"] + sum(float(r["dt"]) for r in dalam)
    out["tp_kumulatif"] = round(kb * 8 / (kd * 1e6), 6) if kd > 0 else 0.0
    out["tp_rasio_kumulatif"] = round(f["throughput_mean"] / out["tp_kumulatif"], 6) \
        if out["tp_kumulatif"] > 1e-9 else 0.0
    out["bytes_kumulatif"] = kb
    kumulatif["bytes"], kumulatif["durasi"] = kb, kd

    # rasio diam: proporsi cuplikan tanpa trafik, penanda pola hidup-mati DASH
    n0 = sum(1 for x in mbps_dari(dalam) if x < 0.01)
    out["rasio_diam"] = round(n0 / len(dalam), 6) if dalam else 0.0
    return out


def proses_run(meta, samples, window, mode, feature_set):
    ps = meta["playback_start_epoch"]
    stalls = load_stalls(meta)
    dur = float(meta.get("media_duration", 0.0))
    n_win = max(1, math.ceil(dur / window)) if dur > 0 else 1
    smp = sorted(samples, key=lambda r: float(r["t_epoch"]))
    rid = meta.get("run_id", "run")

    out, sebelumnya = [], None
    kumulatif = {"bytes": 0, "durasi": 0.0}
    for w in range(n_win):
        m0, m1 = w * window, (w + 1) * window
        if mode == "media":
            t0, t1 = media_to_wall(m0, ps, stalls), media_to_wall(m1, ps, stalls)
        else:
            # Waktu dinding tetap sejak pemutaran dimulai. Periode stall ikut
            # masuk ke window, persis seperti yang dialami loop Fase 2.
            t0, t1 = ps + m0, ps + m1
        dalam = [r for r in smp
                 if float(r["t_epoch"]) > t0 and
                 (float(r["t_epoch"]) - float(r["dt"])) < t1]
        f = (fitur_dasar(dalam) if feature_set == "dasar"
             else fitur_multiskala(dalam, sebelumnya, kumulatif))
        row = {"run_id": rid, "window_index": w, "t_media_start": round(m0, 1),
               "t_wall_start": round(t0, 3), "t_wall_end": round(t1, 3)}
        row.update(f)
        out.append(row)
        sebelumnya = f
    return out


def self_test():
    ps = 1000.0
    stalls = [(30.0, 5.0)]
    meta = {"run_id": "UJI", "playback_start_epoch": ps, "media_duration": 60.0,
            "stalls": [{"position": 30.0, "duration": 5.0}]}
    # cuplikan 1 Hz selama 70 detik waktu dinding
    smp = [{"t_epoch": ps + i + 1, "dt": 1.0, "delta_bytes": 500000,
            "delta_packets": 400, "active_flows": 2} for i in range(70)]

    a = proses_run(meta, smp, 10.0, "media", "dasar")
    b = proses_run(meta, smp, 10.0, "wall", "dasar")
    assert len(a) == len(b) == 6
    print(f"  [OK] kedua mode menghasilkan {len(a)} window")

    # Stall pada posisi media 30 belum terjadi ketika posisi 30 baru dicapai,
    # sehingga AWAL window 3 belum bergeser; yang bergeser adalah akhirnya dan
    # seluruh window sesudahnya.
    assert a[3]["t_wall_start"] == b[3]["t_wall_start"]
    assert abs(a[3]["t_wall_end"] - b[3]["t_wall_end"] - 5.0) < 1e-6
    print("  [OK] awal window yang memuat stall belum bergeser, akhirnya bergeser 5s")
    assert abs(a[4]["t_wall_start"] - b[4]["t_wall_start"] - 5.0) < 1e-6
    print(f"  [OK] window sesudahnya bergeser penuh 5,0s "
          f"({a[4]['t_wall_start']:.0f} vs {b[4]['t_wall_start']:.0f})")
    assert a[0]["t_wall_start"] == b[0]["t_wall_start"]
    print("  [OK] sebelum stall, kedua mode identik")

    # mode media WAJIB sepadan dengan align_qos.py, karena itulah acuan luring
    try:
        import align_qos
        for w in (0, 3, 4, 5):
            ref = align_qos.media_to_wall(w * 10.0, ps, stalls)
            assert abs(ref - a[w]["t_wall_start"]) < 1e-6, (w, ref, a[w]["t_wall_start"])
        print("  [OK] mode media sepadan persis dengan align_qos.media_to_wall")
    except ImportError:
        print("  [--] align_qos.py tidak ada di sini; uji kesepadanan dilewati")

    # multiskala harus menambah fitur, bukan mengubah yg dasar
    c = proses_run(meta, smp, 10.0, "media", "multiskala")
    tambahan = set(c[0]) - set(a[0])
    assert len(tambahan) >= 10, tambahan
    for k in DASAR:
        assert abs(c[2][k] - a[2][k]) < 1e-6, k
    print(f"  [OK] multiskala menambah {len(tambahan)} fitur tanpa mengubah yg dasar")

    # kumulatif harus monoton naik
    kum = [x["bytes_kumulatif"] for x in c]
    assert all(kum[i] <= kum[i + 1] for i in range(len(kum) - 1)), kum
    print(f"  [OK] fitur kumulatif monoton: {kum[0]:,} -> {kum[-1]:,} byte")

    # rasio diam terdeteksi pada cuplikan tanpa trafik
    smp2 = [dict(r, delta_bytes=0, delta_packets=0) if 5 <= i < 10 else r
            for i, r in enumerate(smp)]
    d = proses_run(meta, smp2, 10.0, "media", "multiskala")
    assert d[0]["rasio_diam"] > 0.3, d[0]["rasio_diam"]
    print(f"  [OK] rasio diam terdeteksi: {d[0]['rasio_diam']:.2f} pada window 0")

    # delta terhadap window sebelumnya
    assert "tp_delta_prev" in c[1] and c[0]["tp_delta_prev"] == c[0]["throughput_mean"]
    print("  [OK] delta window pertama sama dgn nilainya sendiri (tidak ada sebelumnya)")
    # penamaan alias harus memberi hasil identik
    smp3 = [{"t_epoch": r["t_epoch"], "dt": r["dt"], "bytes": r["delta_bytes"],
             "packets": r["delta_packets"], "active_flows": r["active_flows"]}
            for r in smp]
    e = proses_run(meta, smp3, 10.0, "media", "dasar")
    for w in range(len(a)):
        for k in DASAR:
            assert abs(a[w][k] - e[w][k]) < 1e-6, (w, k)
    print("  [OK] penamaan delta_bytes dan bytes memberi hasil identik")
    print("\nSEMUA UJI LULUS")
    return True


def main():
    ap = argparse.ArgumentParser(description="Ablasi window dan fitur multi-skala")
    ap.add_argument("--dir", default="hasil_v4")
    ap.add_argument("--window", type=float, default=10.0)
    ap.add_argument("--window-mode", default="media", choices=["media", "wall"])
    ap.add_argument("--feature-set", default="dasar", choices=["dasar", "multiskala"])
    ap.add_argument("--out-suffix", default=None,
                    help="akhiran berkas keluaran; default dari pilihan di atas")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        sys.exit(0 if self_test() else 1)
    if not os.path.isdir(a.dir):
        sys.exit(f"folder tidak ketemu: {a.dir}")

    suf = a.out_suffix or f"_{a.window_mode}_{a.feature_set}"
    metas = sorted(glob.glob(os.path.join(a.dir, "*_client_metadata.json")))
    if not metas:
        sys.exit(f"tidak ada *_client_metadata.json di {a.dir}")

    n_ok = n_gagal = 0
    for mp in metas:
        rid = os.path.basename(mp).replace("_client_metadata.json", "")
        sp = os.path.join(a.dir, f"{rid}_qos_samples.csv")
        if not os.path.exists(sp):
            print(f"  LEWATI {rid}: cuplikan mentah tidak ada")
            n_gagal += 1
            continue
        try:
            meta = json.load(open(mp, encoding="utf-8"))
            smp = list(csv.DictReader(open(sp, encoding="utf-8")))
            if smp:
                wajib = {"t_epoch", "dt", "active_flows"}
                kurang = wajib - set(smp[0])
                if kurang:
                    raise KeyError(f"kolom wajib hilang: {sorted(kurang)}")
                if not ({"delta_bytes", "bytes"} & set(smp[0])):
                    raise KeyError(f"tidak ada kolom byte; kolom yang ada: "
                                   f"{sorted(smp[0])[:8]}")
            rows = proses_run(meta, smp, a.window, a.window_mode, a.feature_set)
        except Exception as e:
            print(f"  LEWATI {rid}: {type(e).__name__} {str(e)[:70]}")
            n_gagal += 1
            continue
        out = os.path.join(a.dir, f"{rid}_qos_aligned{suf}.csv")
        with open(out, "w", newline="", encoding="utf-8") as f:
            wr = csv.DictWriter(f, fieldnames=list(rows[0]))
            wr.writeheader()
            wr.writerows(rows)
        n_ok += 1

    print(f"\n{n_ok} run diproses, {n_gagal} dilewati")
    print(f"mode window : {a.window_mode}")
    print(f"set fitur   : {a.feature_set}")
    print(f"akhiran     : {suf}")
    print(f"\nLangkah berikutnya, gabungkan dan latih:")
    print(f"  python merge_dataset.py --dir {a.dir} --aligned-suffix {suf} "
          f"--out {a.dir}/dataset{suf}.csv")


if __name__ == "__main__":
    main()