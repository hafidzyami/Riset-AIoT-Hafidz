#!/usr/bin/env python3
"""
audit_hasil.py — periksa semua run di folder hasil dan tandai kejanggalan
-------------------------------------------------------------------------
Memeriksa tiap run secara silang: client_metadata, input P.1203, cuplikan QoS,
fitur selaras, dan label. Lalu meringkas per skenario dan menandai anomali.

Pakai (dari folder kerja laptop):
  python audit_hasil.py --dir hasil
  python audit_hasil.py --dir hasil --detail        # tampilkan tabel per run
"""
import argparse
import csv
import glob
import json
import math
import os
from collections import Counter, defaultdict

# Label yang WAJAR untuk tiap skenario (dari kalibrasi terverifikasi).
# Bukan aturan keras; hanya untuk menandai yang perlu dilihat lagi.
HARAPAN = {
    "S1":  {"Excellent", "Good"},
    "S2A": {"Excellent", "Good"},
    "S2B": {"Good", "Excellent", "Degraded", "Critical"},
    "S2C": {"Degraded", "Good", "Critical"},
    "S2D": {"Critical", "Degraded"},
    "S3A": {"Degraded", "Critical", "Good", "Excellent"},
    "S3B": {"Degraded", "Critical", "Good", "Excellent"},
    "S4A": {"Excellent", "Good", "Degraded"},
    "S4B": {"Excellent", "Good", "Degraded"},
    "S4C": {"Excellent", "Good", "Degraded"},
    "S5F": {"Degraded", "Critical", "Good", "Excellent"},
    "S5A": {"Degraded", "Critical", "Good", "Excellent"},
    "S5B": {"Degraded", "Critical", "Good", "Excellent"},
    "S5C": {"Degraded", "Critical", "Good", "Excellent"},
}
# Batas atas throughput per skenario (Mbps); None = tanpa batas.
# Diberi kelonggaran 1.6x untuk burst token bucket dan overhead.
PLAFON = {"S2A": 3.0, "S2B": 0.7, "S2C": 0.4, "S2D": 0.15}


def baca(p):
    with open(p, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def periksa_run(d, rid):
    """Kembalikan (info, daftar_masalah)."""
    m, s = {}, []
    f_meta = os.path.join(d, f"{rid}_client_metadata.json")
    f_ali = os.path.join(d, f"{rid}_qos_aligned.csv")
    f_lab = os.path.join(d, f"{rid}_labels.csv")
    f_smp = os.path.join(d, f"{rid}_qos_samples.csv")
    f_p12 = os.path.join(d, f"{rid}_p1203_input.json")

    for nama, p in (("metadata", f_meta), ("aligned", f_ali), ("labels", f_lab),
                    ("samples", f_smp), ("p1203", f_p12)):
        if not os.path.exists(p) or os.path.getsize(p) == 0:
            s.append(f"berkas {nama} hilang/kosong")
    if s:
        return m, s

    meta = json.load(open(f_meta, encoding="utf-8"))
    ali, lab, smp = baca(f_ali), baca(f_lab), baca(f_smp)
    p12 = json.load(open(f_p12, encoding="utf-8"))

    dur = float(meta.get("media_duration", 0))
    ps = meta.get("playback_start_epoch")
    stalls = meta.get("stalls", [])
    m["media"] = dur
    m["n_stall"] = len(stalls)
    m["stall_total"] = round(sum(x["duration"] for x in stalls), 3)
    m["stall_tengah"] = round(sum(x["duration"] for x in stalls if x["position"] > 0), 3)
    qt = meta.get("quality_timeline", [])
    m["n_switch"] = len(qt)

    # BUG RACE: seed 'playing' memaksa t_media=0 dan bisa tercatat SETELAH event
    # kualitas pertama. Setelah diurutkan, kualitas awal yang rendah terentang ke
    # seluruh sesi sehingga label sistematis terlalu rendah.
    tak_urut = sum(1 for i in range(1, len(qt)) if qt[i]["t_media"] < qt[i-1]["t_media"])
    m["tak_urut"] = tak_urut
    if tak_urut:
        s.append(f"quality_timeline TIDAK URUT ({tak_urut}x) -> label kemungkinan SALAH, "
                 f"run perlu diulang")

    # segmen terpanjang menempati porsi berapa, dan bitrate-nya berapa
    if qt and dur > 0:
        qs = sorted(qt, key=lambda e: e["t_media"])
        terpanjang, bw_pjg = 0.0, None
        for i, e in enumerate(qs):
            b = qs[i+1]["t_media"] if i+1 < len(qs) else dur
            if b - e["t_media"] > terpanjang:
                terpanjang, bw_pjg = b - e["t_media"], e["bitrate_kbps"]
        m["porsi_dominan"] = round(terpanjang / dur, 2)
        m["bw_dominan"] = bw_pjg

    if not ps:
        s.append("playback_start_epoch tidak ada")

    # kontinuitas I13
    segs = p12["I13"]["segments"]
    t, celah = 0.0, 0
    for sg in segs:
        if abs(sg["start"] - t) > 1e-3:
            celah += 1
        t = sg["start"] + sg["duration"]
    if celah:
        s.append(f"I13 ada {celah} celah/tumpang tindih")
    if abs(t - dur) > 0.05:
        s.append(f"cakupan I13 {t:.2f}s != media {dur:.2f}s")

    # cuplikan QoS
    te = [float(r["t_epoch"]) for r in smp]
    dts = [float(r["dt"]) for r in smp]
    m["n_smp"] = len(smp)
    m["dt_maks"] = round(max(dts), 3) if dts else 0
    if dts and max(dts) > 1.6:
        s.append(f"ada jeda pencuplikan {max(dts):.2f}s")
    if ps and te:
        m["mulai_sblm"] = round(ps - min(te), 1)
        m["akhir_stlh"] = round(max(te) - (ps + dur), 1)
        if m["mulai_sblm"] < 0:
            s.append("agen mulai SETELAH playback (window awal bisa kosong)")
        if m["akhir_stlh"] < -2:
            s.append(f"agen berhenti {-m['akhir_stlh']:.0f}s SEBELUM playback selesai")

    # window
    m["n_win_ali"], m["n_win_lab"] = len(ali), len(lab)
    if len(ali) - len(lab) not in (0, 1):
        s.append(f"jumlah window beda: aligned {len(ali)} vs labels {len(lab)}")
    # beda tepat 1 (aligned lebih banyak) itu wajar: penyelaras membentuk window
    # ekor yang tidak punya detik O22, dan inner join di merge membuangnya.
    kosong = [r["window_index"] for r in ali if int(r["n_samples"]) == 0]
    if kosong:
        s.append(f"{len(kosong)} window tanpa cuplikan: {kosong[:6]}")
    m["n_parsial"] = sum(1 for r in ali if 0 < int(r["n_samples"]) < 9)

    tp = [float(r["throughput_mean"]) for r in ali]
    m["tp_min"], m["tp_maks"] = (round(min(tp), 3), round(max(tp), 3)) if tp else (0, 0)
    m["tp_rata"] = round(sum(tp) / len(tp), 3) if tp else 0
    # window sepi di EKOR (konten habis sebelum pemutaran selesai)
    ekor = 0
    for v in reversed(tp):
        if v <= 0.05:
            ekor += 1
        else:
            break
    m["ekor_sepi"] = ekor

    sc = rid.split("_")[0]
    plafon = PLAFON.get(sc)
    if plafon and tp and max(tp) > plafon * 1.6:
        s.append(f"throughput {max(tp):.2f} Mbps >> plafon {sc} {plafon} Mbps")

    dist = Counter(r["label"] for r in lab)
    m["dist"] = dict(dist)
    # Window 0 SELALU dipengaruhi buffering awal, bukan skenario. Kalau tidak
    # dikecualikan, tiap run akan tertandai dan peringatan jadi diabaikan.
    dist1 = Counter(r["label"] for r in lab if int(r["window_index"]) > 0)
    m["dist1"] = dict(dist1)
    tak_wajar = set(dist1) - HARAPAN.get(sc, set(dist1))
    if tak_wajar:
        s.append(f"label di luar harapan {sc} (window>0): {sorted(tak_wajar)}")

    return m, s


def main():
    ap = argparse.ArgumentParser(description="Audit hasil pengambilan data")
    ap.add_argument("--dir", default="hasil")
    ap.add_argument("--detail", action="store_true", help="tabel per run")
    a = ap.parse_args()

    rids = sorted({os.path.basename(p).replace("_labels.csv", "")
                   for p in glob.glob(os.path.join(a.dir, "*_labels.csv"))})
    if not rids:
        raise SystemExit(f"tidak ada *_labels.csv di {a.dir}")

    hasil, semua_masalah = {}, {}
    for rid in rids:
        m, s = periksa_run(a.dir, rid)
        hasil[rid] = m
        if s:
            semua_masalah[rid] = s

    print(f"AUDIT {len(rids)} run di '{a.dir}'\n")

    if a.detail:
        print(f"{'run':<34}{'media':>7}{'win':>4}{'tp rata':>8}{'bw dom':>8}{'porsi':>6}"
              f"{'ekor':>5}{'stall':>6}{'?':>2}  distribusi")
        print("-" * 118)
        for rid, m in hasil.items():
            if not m:
                print(f"{rid:<34}  << berkas tidak lengkap")
                continue
            print(f"{rid:<34}{m['media']:>7.1f}{m['n_win_ali']:>4}"
                  f"{m['tp_rata']:>8.3f}{str(m.get('bw_dominan','-')):>8}"
                  f"{m.get('porsi_dominan',0):>6.2f}{m['ekor_sepi']:>5}"
                  f"{m['n_stall']:>6}{'!' if m.get('tak_urut') else ' ':>2}  {m['dist']}")
        print()

    # ringkasan per skenario
    per_sc = defaultdict(lambda: {"run": 0, "lab": Counter(), "tp": [], "ekor": 0, "stall": 0})
    for rid, m in hasil.items():
        if not m:
            continue
        sc = rid.split("_")[0]
        g = per_sc[sc]
        g["run"] += 1
        g["lab"].update(m.get("dist1", m["dist"]))
        g["tp"].append(m["tp_rata"])
        g["ekor"] += m["ekor_sepi"]
        g["stall"] += m["n_stall"]
    print("RINGKASAN PER SKENARIO")
    print(f"{'sc':<5}{'run':>4}{'tp rata':>9}{'stall/run':>10}{'ekor sepi':>10}  distribusi label (tanpa window 0)")
    print("-" * 100)
    for sc in sorted(per_sc, key=lambda x: (x[:2], x)):
        g = per_sc[sc]
        tp = sum(g["tp"]) / len(g["tp"])
        print(f"{sc:<5}{g['run']:>4}{tp:>9.3f}{g['stall']/g['run']:>10.1f}{g['ekor']:>10}"
              f"  {dict(g['lab'])}")

    tot, tot0 = Counter(), Counter()
    for m in hasil.values():
        if m:
            tot.update(m.get("dist1", {}))
            tot0.update(m["dist"])
    print(f"\nTOTAL LABEL termasuk window 0 : {dict(tot0)}")
    print(f"TOTAL LABEL tanpa window 0    : {dict(tot)}   <- ini yang masuk dataset")
    n = sum(tot.values())
    if n:
        print("  proporsi: " + ", ".join(f"{k} {v/n*100:.1f}%" for k, v in tot.most_common()))

    rusak = [r for r, m in hasil.items() if m and m.get("tak_urut")]
    if rusak:
        print(f"\n!! {len(rusak)} run dgn quality_timeline TIDAK URUT (label salah, perlu diulang):")
        for r in rusak:
            print(f"   {r}")
        print("   Perbaiki harness.js, hapus berkas run tsb, lalu jalankan orchestrate.py lagi.")

    print(f"\nMASALAH: {len(semua_masalah)} run bermasalah dari {len(rids)}")
    for rid, s in semua_masalah.items():
        print(f"  {rid}")
        for x in s:
            print(f"    - {x}")
    if not semua_masalah:
        print("  (tidak ada)")


if __name__ == "__main__":
    main()