#!/usr/bin/env python3
"""
justifikasi_desain.py — data pendukung untuk dua keputusan desain
-------------------------------------------------------------------
Menghasilkan bukti terukur untuk dua pertanyaan yang akan diajukan penelaah:

  A. Mengapa pelabelan bersifat video-only, bukan audiovisual?
  B. Mengapa ITU-T P.1203 Mode 0 yang dipilih, bukan mode yang lebih tinggi?

Keduanya sebelumnya hanya dinyatakan sebagai keterbatasan tanpa angka. Skrip ini
menghitung angkanya, sehingga dapat dikutip langsung di naskah.

Pakai:
  python justifikasi_desain.py --all
  python justifikasi_desain.py --audio --mpd-dir /path/ke/dash
  python justifikasi_desain.py --audio --mpd-base http://192.168.50.10:8080
  python justifikasi_desain.py --mode0
  python justifikasi_desain.py --self-test
"""
import argparse
import glob
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET

NS = "{urn:mpeg:dash:schema:mpd:2011}"

# Ladder representatif untuk membandingkan O22 dan O46 pada rentang yang sama.
# Nilainya mengikuti pola ladder DASHDataset2014 dari rung terendah ke tertinggi.
LADDER = [(45, "320x240"), (128, "320x240"), (256, "480x360"), (378, "480x360"),
          (578, "854x480"), (783, "1280x720"), (1474, "1280x720"), (3936, "1920x1080")]

KONTEKS = {"device": "mobile", "displaySize": "1280x720", "viewingDistance": "150cm"}


def kelas(m):
    return "Excellent" if m >= 4 else "Good" if m >= 3 else "Degraded" if m >= 2 else "Critical"


def bangun(bw, res, dengan_audio, frames=None):
    seg = {"start": 0, "duration": 30.0, "bitrate": bw, "resolution": res,
           "fps": 24.0, "codec": "h264"}
    if frames is not None:
        seg["frames"] = frames
    r = {"I11": {"streamId": 11, "segments": []},
         "I13": {"streamId": 42, "segments": [seg]},
         "I23": {"streamId": 42, "stalling": []},
         "IGen": dict(KONTEKS)}
    if dengan_audio:
        r["I11"]["segments"] = [{"start": 0, "duration": 30.0,
                                 "bitrate": 128, "codec": "aaclc"}]
    return r


# ------------------------------------------------------------------ A. audio
def cek_audio_mpd(teks):
    """Kembalikan (n_adaptationset_video, n_audio, daftar_codec_audio)."""
    root = ET.fromstring(teks)
    nv = na = 0
    codecs = []
    for a in root.iter(f"{NS}AdaptationSet"):
        mt = a.get("mimeType") or ""
        reps = list(a.iter(f"{NS}Representation"))
        if not mt and reps:
            mt = reps[0].get("mimeType") or ""
        cs = [r.get("codecs") or "" for r in reps]
        if mt.startswith("audio") or any(c.startswith(("mp4a", "aac")) for c in cs):
            na += 1
            codecs += [c for c in cs if c]
        elif mt.startswith("video"):
            # subtitle wvtt kadang ditandai video/mp4; jangan dihitung sbg video
            if all(c.startswith("wvtt") for c in cs if c):
                continue
            nv += 1
    return nv, na, sorted(set(codecs))


def analisis_audio(mpd_dir, mpd_base, judul):
    from itu_p1203 import P1203Standalone
    import logging
    logging.disable(logging.WARNING)     # implementasi memperingatkan audio kosong

    print("=" * 74)
    print("A. MENGAPA PELABELAN VIDEO-ONLY")
    print("=" * 74)

    # --- A.1 ketersediaan trek audio pada konten uji ---
    print("\nA.1 Ketersediaan trek audio pada konten uji\n")
    hasil = []
    for t in judul:
        teks = None
        if mpd_dir:
            f = sorted(glob.glob(os.path.join(mpd_dir, t, "*.mpd")))
            f = [x for x in f if "simple" in os.path.basename(x).lower()] or f
            if f:
                teks = open(f[0], encoding="utf-8", errors="replace").read()
        if teks is None and mpd_base:
            try:
                idx = urllib.request.urlopen(f"{mpd_base}/{t}/", timeout=15
                                             ).read().decode("utf-8", "replace")
                nama = [n for n in re.findall(r'href="([^"?/][^"]*)"', idx)
                        if n.lower().endswith(".mpd")]
                pilih = [n for n in nama if "simple" in n.lower()] or nama
                if pilih:
                    teks = urllib.request.urlopen(f"{mpd_base}/{t}/{pilih[0]}",
                                                  timeout=15).read().decode("utf-8", "replace")
            except Exception as e:
                print(f"  {t:<22} tidak terjangkau: {str(e)[:40]}")
                continue
        if teks is None:
            print(f"  {t:<22} manifest tidak ditemukan")
            continue
        nv, na, cs = cek_audio_mpd(teks)
        hasil.append((t, nv, na, cs))

    if hasil:
        print(f"  {'judul':<22}{'AS video':>10}{'AS audio':>10}  codec audio")
        print("  " + "-" * 66)
        for t, nv, na, cs in hasil:
            print(f"  {t:<22}{nv:>10}{na:>10}  {', '.join(cs) if cs else '(tidak ada)'}")
        n_tanpa = sum(1 for _, _, na, _ in hasil if na == 0)
        print(f"\n  -> {n_tanpa} dari {len(hasil)} judul TIDAK memiliki trek audio sama sekali.")
        if n_tanpa:
            print("     Menghitung O46 untuk judul tersebut menuntut trek audio yang")
            print("     tidak ada, sehingga nilainya harus direka. Itu fabrikasi data,")
            print("     bukan pengukuran.")

    # --- A.2 perilaku implementasi saat audio kosong ---
    print("\nA.2 Perilaku implementasi resmi bila audio kosong\n")
    out = P1203Standalone(bangun(3936, "1920x1080", False)).calculate_complete()
    print(f"  O46 tanpa trek audio  : {out['O46']:.3f}")
    print(f"  O23 (audio) yg dipakai: {out['O23']:.3f}")
    print("  -> Implementasi mengeluarkan peringatan bahwa O21 tidak punya skor")
    print("     lalu MENGASUMSIKAN audio berkualitas tinggi konstan. Jadi O46 di")
    print("     sini bukan mengukur audio, melainkan menambahkan nilai tetap.")

    # --- A.3 dampak pada rentang dinamis dan keterjangkauan kelas ---
    print("\nA.3 Dampak O46 terhadap rentang dinamis dan keterjangkauan kelas\n")
    print(f"  {'bitrate':>9} {'resolusi':>11} {'O22':>8} {'O46':>8}   {'kelas O22':<11} kelas O46")
    print("  " + "-" * 70)
    o22s, o46s = [], []
    for bw, res in LADDER:
        v = P1203Standalone(bangun(bw, res, False)).calculate_pv()["video"]["O22"]
        m22 = sum(v) / len(v)
        m46 = P1203Standalone(bangun(bw, res, True)).calculate_complete()["O46"]
        o22s.append(m22)
        o46s.append(m46)
        print(f"  {bw:>7}k {res:>11} {m22:>8.3f} {m46:>8.3f}   "
              f"{kelas(m22):<11} {kelas(m46)}")

    l22, l46 = max(o22s) - min(o22s), max(o46s) - min(o46s)
    k22 = sorted(set(kelas(x) for x in o22s), key=lambda k: -o22s[0])
    print(f"\n  rentang O22 : {min(o22s):.3f} sampai {max(o22s):.3f}  (lebar {l22:.3f})")
    print(f"  rentang O46 : {min(o46s):.3f} sampai {max(o46s):.3f}  (lebar {l46:.3f})")
    print(f"  penyusutan  : {(1 - l46 / l22) * 100:.1f} persen")
    kk22 = set(kelas(x) for x in o22s)
    kk46 = set(kelas(x) for x in o46s)
    print(f"\n  kelas terjangkau O22 : {len(kk22)} dari 4  {sorted(kk22)}")
    print(f"  kelas terjangkau O46 : {len(kk46)} dari 4  {sorted(kk46)}")
    hilang = kk22 - kk46
    if hilang:
        print(f"\n  -> Di bawah O46, kelas {sorted(hilang)} menjadi TIDAK TERJANGKAU")
        print("     pada seluruh ladder. Skema empat kelas akan runtuh menjadi tiga")
        print("     kelas karena alasan struktural, bukan karena data.")


# ------------------------------------------------------------------ B. mode 0
def analisis_mode0():
    from itu_p1203 import P1203Standalone
    import logging
    logging.disable(logging.ERROR)

    print("\n" + "=" * 74)
    print("B. MENGAPA MODE 0")
    print("=" * 74)

    print("\nB.1 Masukan yang dituntut tiap mode, dan ketersediaannya di sisi jaringan\n")
    tabel = [
        ("Mode 0", "bitrate, resolusi, framerate, codec",
         "manifest DASH", "TERSEDIA"),
        ("Mode 1", "Mode 0 + tipe dan ukuran tiap frame",
         "parsing bitstream", "tidak tersedia"),
        ("Mode 2", "Mode 1 + nilai QP pada 2 persen frame",
         "dekode sebagian", "tidak tersedia"),
        ("Mode 3", "Mode 1 + nilai QP seluruh frame",
         "dekode penuh", "tidak tersedia"),
    ]
    print(f"  {'mode':<8}{'masukan tambahan':<40}{'sumber':<20}status")
    print("  " + "-" * 84)
    for m, inp, src, st in tabel:
        print(f"  {m:<8}{inp:<40}{src:<20}{st}")

    print("\nB.2 Verifikasi pada implementasi resmi\n")
    seg_ok = None
    try:
        v = P1203Standalone(bangun(1000, "1280x720", False)).calculate_pv()["video"]["O22"]
        seg_ok = sum(v) / len(v)
        print(f"  Mode 0, hanya metadata           : BERHASIL, O22 rata-rata {seg_ok:.3f}")
    except Exception as e:
        print(f"  Mode 0 gagal: {e}")

    # frames tanpa medan wajib -> ditolak, membuktikan mode >0 menuntut data frame
    frames = [{"type": "I", "size": 9000, "dts": 0.0, "duration": 1 / 24}]
    try:
        P1203Standalone(bangun(1000, "1280x720", False, frames)).calculate_pv()
        print("  Mode 1, tanpa frameType/frameSize : tak terduga BERHASIL")
    except Exception as e:
        print(f"  Mode 1, tanpa frameType/frameSize : DITOLAK")
        print(f"    pesan: {str(e)[:70]}")
    print("\n  -> Mode di atas 0 menuntut medan tingkat frame yang hanya diperoleh")
    print("     dengan mem-parse bitstream video. Perangkat bridge hanya melihat")
    print("     ukuran dan waktu kedatangan paket, sehingga medan itu mustahil")
    print("     dipenuhi tanpa merakit ulang dan mendekode aliran video.")

    print("\nB.3 Alasan kedua: konsistensi dengan konteks penerapan\n")
    print("  Sistem yang dibangun menyimpulkan QoE dari sisi jaringan. Memakai")
    print("  ground truth yang sendirinya menuntut akses bitstream akan menuntut")
    print("  perangkat merakit ulang dan mendekode video, yang meniadakan premis")
    print("  sensor ringan: biaya per paket sensor ini 124 nanodetik, sedangkan")
    print("  dekode video berada pada orde milidetik per frame.")

    print("\nB.4 Alasan ketiga: cakupan yang memang diperuntukkan\n")
    print("  Mode 0 dirancang justru untuk situasi tanpa akses bitstream, yaitu")
    print("  pemantauan sisi jaringan. Mode 1 sampai 3 diperuntukkan bagi pengukuran")
    print("  di sisi klien atau di laboratorium yang memegang berkas video.")
    print("  Memilih Mode 0 karena itu bukan kompromi, melainkan mode yang memang")
    print("  sesuai dengan titik pengamatan penelitian ini.")


# ------------------------------------------------------------------ uji
def self_test():
    mpd = f'''<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><Period>
 <AdaptationSet mimeType="video/mp4"><Representation id="v1" codecs="avc1.42c00d"
   width="320" height="240" bandwidth="45226"/></AdaptationSet>
 <AdaptationSet mimeType="audio/mp4"><Representation id="a1" codecs="mp4a.40.2"
   bandwidth="128000"/></AdaptationSet>
 <AdaptationSet mimeType="video/mp4"><Representation id="s1" codecs="wvtt"
   bandwidth="101"/></AdaptationSet>
</Period></MPD>'''
    nv, na, cs = cek_audio_mpd(mpd)
    assert (nv, na) == (1, 1), (nv, na)
    assert cs == ["mp4a.40.2"], cs
    print(f"  [OK] parsing MPD: {nv} AS video, {na} AS audio, codec {cs}")
    print("  [OK] subtitle wvtt bertanda video/mp4 tidak salah dihitung sbg video")

    mpd2 = mpd.replace('<AdaptationSet mimeType="audio/mp4"><Representation id="a1" '
                       'codecs="mp4a.40.2"\n   bandwidth="128000"/></AdaptationSet>', '')
    nv2, na2, _ = cek_audio_mpd(mpd2)
    assert na2 == 0, na2
    print(f"  [OK] konten tanpa audio terdeteksi benar ({na2} AS audio)")

    from itu_p1203 import P1203Standalone
    import logging
    logging.disable(logging.WARNING)
    a = P1203Standalone(bangun(256, "480x360", False)).calculate_pv()["video"]["O22"]
    b = P1203Standalone(bangun(256, "480x360", True)).calculate_complete()["O46"]
    m = sum(a) / len(a)
    assert b > m, (m, b)
    print(f"  [OK] O46 ({b:.3f}) lebih tinggi dari O22 ({m:.3f}) pd rung yang sama")
    assert kelas(m) != kelas(b), (kelas(m), kelas(b))
    print(f"  [OK] pergeseran kelas terbukti: {kelas(m)} menjadi {kelas(b)}")
    print("\nSEMUA UJI LULUS")
    return True


def main():
    ap = argparse.ArgumentParser(description="Data justifikasi video-only dan Mode 0")
    ap.add_argument("--audio", action="store_true")
    ap.add_argument("--mode0", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--mpd-dir", default=None)
    ap.add_argument("--mpd-base", default=None, help="mis. http://192.168.50.10:8080")
    ap.add_argument("--titles", default="BigBuckBunny,ElephantsDream,OfForestAndMen,"
                                        "RedBullPlayStreets,TearsOfSteel,TheSwissAccount,Valkaama")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        sys.exit(0 if self_test() else 1)
    if not (a.audio or a.mode0 or a.all):
        ap.error("pilih --audio, --mode0, atau --all")

    judul = [s.strip() for s in a.titles.split(",") if s.strip()]
    if a.audio or a.all:
        if not a.mpd_dir and not a.mpd_base:
            print("CATATAN: tanpa --mpd-dir atau --mpd-base, bagian A.1 dilewati.\n")
        analisis_audio(a.mpd_dir, a.mpd_base, judul)
    if a.mode0 or a.all:
        analisis_mode0()


if __name__ == "__main__":
    main()