#!/usr/bin/env python3
"""
baseline_degraded.py — baseline NAIF P.1203_degraded (pembanding untuk model ML)
---------------------------------------------------------------------------------
Menghasilkan label QoE hanya dari apa yang BISA dilihat perangkat edge:
  (a) manifest server (.mpd), yang memang melintasi bridge sebagai respons HTTP
  (b) fitur QoS hasil sensor eBPF

TIDAK memakai telemetri klien sama sekali. Resolusi/bitrate yang diputar dan
kejadian stalling DITEBAK dengan heuristik, lalu tebakan itu dimasukkan ke
implementasi resmi P.1203 persis seperti jalur label sebenarnya.

Inilah pembanding yang harus dikalahkan model ML. Perlu dicatat: baseline ini
justru mendapat informasi LEBIH BANYAK daripada model (ia memegang manifest),
sehingga mengalahkannya adalah bukti yang bermakna.

Heuristik yang dipakai:
  1. Bitrate/resolusi  : ambil representasi tertinggi yang muat di throughput
                         teramati (faktor ABR 0,80, terverifikasi empiris).
  2. Stalling          : simulasi buffer. Tiap detik buffer bertambah sebesar
                         (data terkirim / bitrate yang diputar) dan berkurang 1
                         detik karena pemutaran. Bila buffer habis -> stall.

Pakai:
  python baseline_degraded.py --dir hasil_v3 --mpd-base http://192.168.50.10:8080
  python baseline_degraded.py --dir hasil_v3 --mpd-dir /path/ke/dash
  python baseline_degraded.py --self-test
"""
import argparse
import csv
import glob
import json
import math
import os
import sys
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter

NS = {"m": "urn:mpeg:dash:schema:mpd:2011"}
# Faktor default 0,95. CATATAN PENTING: faktor 0,80 dikalibrasi terhadap LAJU
# THROTTLE, bukan throughput TERUKUR. Pada kondisi mapan throughput terukur sudah
# mendekati bitrate yang diputar (rasio 0,97-1,04 pada S2B/S2C/S2D), sehingga
# memakai 0,80 berarti mendiskon dua kali dan baseline jadi terlalu lemah.
FAKTOR_ABR = 0.95
BUFFER_AWAL = 2.0          # detik, asumsi buffer setelah pemutaran dimulai
KELAS = ["Excellent", "Good", "Degraded", "Critical"]


# ---------------------------------------------------------------- manifest
def ladder_dari_mpd(xml_text):
    """[(bitrate_kbps, 'WxH', fps)] terurut naik; audio & subtitle dibuang."""
    root = ET.fromstring(xml_text)
    out = []
    for rep in root.iter("{urn:mpeg:dash:schema:mpd:2011}Representation"):
        if rep.get("mimeType") != "video/mp4":
            continue
        if (rep.get("codecs") or "").startswith("wvtt"):
            continue
        w, h = rep.get("width"), rep.get("height")
        if not w or not h:
            continue
        fr = rep.get("frameRate") or "24"
        if "/" in fr:
            a, b = fr.split("/")
            fr = float(a) / float(b)
        out.append((int(rep.get("bandwidth")) / 1000.0, f"{w}x{h}", round(float(fr), 3)))
    return sorted(out)


def pilih_rep(ladder, mbps, faktor=None):
    """Representasi tertinggi yang muat di bandwidth teramati."""
    budget = mbps * 1000.0 * (FAKTOR_ABR if faktor is None else faktor)
    muat = [r for r in ladder if r[0] <= budget]
    return muat[-1] if muat else ladder[0]


# ---------------------------------------------------------------- heuristik
def tebak(windows, ladder, window=10.0, faktor=None):
    """windows = [throughput_mean Mbps per window] -> (segmen I13, stalling I23).

    Simulasi buffer memakai HANYA throughput; edge tidak bisa melihat buffer
    klien, jadi inilah batas terbaik pendekatan naif.
    """
    segmen, stalls = [], []
    buffer_s = BUFFER_AWAL
    for i, mbps in enumerate(windows):
        bw, res, fps = pilih_rep(ladder, mbps, faktor)
        segmen.append({"start": round(i * window, 3), "duration": window,
                       "bitrate": round(bw, 2), "resolution": res,
                       "fps": fps, "codec": "h264"})
        # isi buffer = (bit terkirim / bit per detik pemutaran); kuras = durasi window
        terisi = (mbps * 1e6 * window) / (bw * 1000.0) if bw > 0 else 0.0
        buffer_s += terisi - window
        if buffer_s < 0:
            durasi = min(-buffer_s, window)          # tidak bisa melebihi window
            stalls.append([round(i * window, 3), round(durasi, 3)])
            buffer_s = 0.0
        buffer_s = min(buffer_s, 30.0)               # batas buffer wajar dash.js
    return segmen, stalls


def klasifikasi(o22, stall_s):
    """Ambang IDENTIK dengan label sebenarnya, agar perbandingan adil."""
    if stall_s > 1.0:
        return "Critical"
    if stall_s > 0.0:
        return "Degraded"
    if o22 >= 4.0:
        return "Excellent"
    if o22 >= 3.0:
        return "Good"
    if o22 >= 2.0:
        return "Degraded"
    return "Critical"


def label_naif(windows, ladder, device="mobile", display="1280x720", window=10.0, faktor=None):
    from itu_p1203 import P1203Standalone
    segmen, stalls = tebak(windows, ladder, window, faktor)
    rep = {"I11": {"streamId": 42, "segments": []},
           "I13": {"streamId": 42, "segments": segmen},
           "I23": {"streamId": 42, "stalling": stalls},
           "IGen": {"device": device, "displaySize": display,
                    "viewingDistance": "150cm"}}
    o22 = P1203Standalone(rep).calculate_pv()["video"]["O22"]
    hasil = []
    for i in range(len(windows)):
        t0, t1 = i * window, (i + 1) * window
        detik = [o22[s] for s in range(int(t0), int(math.ceil(t1))) if 0 <= s < len(o22)]
        m = sum(detik) / len(detik) if detik else 0.0
        st = sum(d for (p, d) in stalls if t0 <= p < t1)
        hasil.append((round(m, 4), round(st, 3), klasifikasi(m, st)))
    return hasil


# ---------------------------------------------------------------- uji mandiri
def self_test():
    ladder = [(45.2, "320x240", 24), (128.5, "320x240", 24), (255.9, "480x360", 24),
              (378.4, "480x360", 24), (577.8, "854x480", 24), (782.6, "1280x720", 24),
              (1473.8, "1280x720", 24), (3936.3, "1920x1080", 24)]
    assert pilih_rep(ladder, 5.0, 0.8)[0] == 3936.3
    assert pilih_rep(ladder, 0.4, 0.8)[0] == 255.9   # 400 kbps * 0.8 = 320
    assert pilih_rep(ladder, 0.001, 0.8)[0] == 45.2  # terlalu kecil -> rep terendah
    # faktor lebih tinggi -> memilih representasi lebih tinggi pd throughput sama
    assert pilih_rep(ladder, 0.7, 0.80)[0] == 378.4
    assert pilih_rep(ladder, 0.7, 0.95)[0] == 577.8
    print("  [OK] pemilihan representasi mengikuti faktor ABR (dapat disetel)")

    # throughput cukup -> tidak ada stall
    _, s1 = tebak([3.0] * 10, ladder, faktor=0.8)
    assert not s1, s1
    # throughput di bawah REPRESENTASI TERENDAH -> buffer terkuras -> stall
    _, s2 = tebak([0.02] * 10, ladder, faktor=0.8)   # 20 kbps < rep terendah
    assert len(s2) > 0, s2
    print(f"  [OK] simulasi buffer: cukup -> 0 stall, kekurangan -> {len(s2)} stall")

    # KETERBATASAN yg disengaja: selama throughput masih menampung rep terendah,
    # heuristik menyimpulkan klien turun kualitas dan TIDAK stall. Padahal ABR
    # nyata bereaksi terlambat sehingga stall tetap terjadi. Inilah kebutaan
    # pendekatan naif yang harus dikalahkan model ML.
    _, s3 = tebak([0.05] * 10, ladder, faktor=0.8)   # masih muat rep terendah
    assert not s3, s3
    print("  [OK] keterbatasan terdokumentasi: stall TAK terdeteksi bila throughput")
    print("       masih menampung representasi terendah (kebutaan thd buffer klien)")

    assert klasifikasi(4.5, 0) == "Excellent" and klasifikasi(3.5, 0) == "Good"
    assert klasifikasi(2.5, 0) == "Degraded" and klasifikasi(1.5, 0) == "Critical"
    assert klasifikasi(4.9, 1.5) == "Critical" and klasifikasi(4.9, 0.5) == "Degraded"
    print("  [OK] ambang klasifikasi identik dgn label sebenarnya")

    try:
        lab = label_naif([3.0] * 6, ladder)
        assert len(lab) == 6 and lab[0][2] in KELAS
        print(f"  [OK] jalur P.1203 utuh: 6 window -> {[l[2] for l in lab]}")
    except ImportError:
        print("  [--] itu_p1203 tidak terpasang; jalur P.1203 dilewati")

    print("\nSEMUA UJI LULUS")
    return True


# ---------------------------------------------------------------- main
def ambil_mpd(title, mpd_base, mpd_dir, cache):
    if title in cache:
        return cache[title]
    teks = None
    if mpd_dir:
        pola = os.path.join(mpd_dir, title, "*.mpd")
        f = sorted(glob.glob(pola))
        if f:
            teks = open(f[0], encoding="utf-8").read()
    if teks is None and mpd_base:
        idx = urllib.request.urlopen(f"{mpd_base}/{title}/", timeout=15).read().decode("utf-8", "replace")
        import re
        nama = [n for n in re.findall(r'href="([^"?/][^"]*)"', idx) if n.lower().endswith(".mpd")]
        pilih = [n for n in nama if "simple" in n.lower()] or nama
        teks = urllib.request.urlopen(f"{mpd_base}/{title}/{pilih[0]}", timeout=15).read().decode("utf-8", "replace")
    if teks is None:
        raise RuntimeError(f"manifest untuk {title} tidak ditemukan")
    cache[title] = ladder_dari_mpd(teks)
    return cache[title]


def main():
    ap = argparse.ArgumentParser(description="Baseline naif P.1203_degraded")
    ap.add_argument("--dir", default="hasil_v3")
    ap.add_argument("--mpd-base", default=None, help="mis. http://192.168.50.10:8080")
    ap.add_argument("--mpd-dir", default=None, help="folder lokal berisi <Judul>/*.mpd")
    ap.add_argument("--device", default="mobile")
    ap.add_argument("--display", default="1280x720")
    ap.add_argument("--window", type=float, default=10.0)
    ap.add_argument("--faktor", type=float, default=None,
                    help="faktor ABR (default 0.95); coba 0.8/0.9/1.0 utk uji sensitivitas")
    ap.add_argument("--out", default=None, help="default: <dir>/baseline_degraded.csv")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        sys.exit(0 if self_test() else 1)
    if not a.mpd_base and not a.mpd_dir:
        ap.error("butuh --mpd-base atau --mpd-dir (baseline HANYA boleh pakai manifest + QoS)")

    berkas = sorted(glob.glob(os.path.join(a.dir, "*_qos_aligned.csv")))
    if not berkas:
        sys.exit(f"tidak ada *_qos_aligned.csv di {a.dir}")

    cache, baris, lewat = {}, [], 0
    for p in berkas:
        rid = os.path.basename(p).replace("_qos_aligned.csv", "")
        title = rid.split("_")[1]
        try:
            ladder = ambil_mpd(title, a.mpd_base, a.mpd_dir, cache)
        except Exception as e:
            print(f"  LEWATI {rid}: {e}")
            lewat += 1
            continue
        qos = list(csv.DictReader(open(p, encoding="utf-8")))
        qos.sort(key=lambda r: int(r["window_index"]))
        tp = [float(r["throughput_mean"]) for r in qos]
        lab = label_naif(tp, ladder, a.device, a.display, a.window, a.faktor)
        for r, (o, st, k) in zip(qos, lab):
            baris.append({"run_id": rid, "window_index": int(r["window_index"]),
                          "o22_degraded": o, "stall_degraded": st, "label_degraded": k})

    out = a.out or os.path.join(a.dir, "baseline_degraded.csv")
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["run_id", "window_index", "o22_degraded",
                                          "stall_degraded", "label_degraded"])
        w.writeheader()
        w.writerows(baris)
    print(f"{len(berkas) - lewat} run -> {out}  ({len(baris)} window)")
    print(f"  distribusi baseline: {dict(Counter(r['label_degraded'] for r in baris))}")


if __name__ == "__main__":
    main()