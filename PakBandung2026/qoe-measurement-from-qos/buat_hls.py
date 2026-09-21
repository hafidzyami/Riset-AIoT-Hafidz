#!/usr/bin/env python3
"""
buat_hls.py — bungkus ulang konten DASH menjadi HLS tanpa enkode ulang
=========================================================================
Konten DASH pada penelitian ini memakai segmen fMP4 dengan init segment
terpisah per representasi. HLS mendukung fMP4 lewat tag EXT-X-MAP sejak versi 6,
sehingga playlist dapat menunjuk ke BERKAS SEGMEN YANG SAMA PERSIS.

APA YANG SEBENARNYA DIUJI, DAN APA YANG TIDAK

Karena bytenya identik, yang berubah hanyalah format manifest dan PEMUTAR yang
memakainya. Segmen, bitrate, resolusi, dan ladder seluruhnya sama.

  Berubah        format manifest (.mpd menjadi .m3u8)
  Berubah        pemutar (dash.js menjadi hls.js)
  BERUBAH BESAR  algoritma ABR, karena hls.js memakai implementasinya sendiri
  Tidak berubah  berkas segmen, ladder bitrate, resolusi, codec

Jadi eksperimen ini sebaiknya TIDAK dinamai "DASH lawan HLS", karena itu
mengesankan perbedaan pengemasan yang sesungguhnya tidak ada di kabel. Nama yang
jujur: leave-one-player-out, atau pengaruh pemutar dan format manifest.

Penerapan HLS di lapangan sering memakai segmen MPEG-TS, bukan fMP4. Menguji itu
menuntut enkode ulang, dan perbedaan yang terlihat akan mencampur pengemasan
dengan enkode. Pilihan di sini mengorbankan realisme demi isolasi yang bersih,
dan itu harus dinyatakan.

Pakai (dijalankan DI RPi 4):
  python3 buat_hls.py --dir /home/yb/RisetPakBandung2026/dash
  python3 buat_hls.py --dir ... --judul BigBuckBunny --verbose
  python3 buat_hls.py --self-test
"""
import argparse
import glob
import os
import re
import sys
import xml.etree.ElementTree as ET

NS = {"m": "urn:mpeg:dash:schema:mpd:2011"}


def urai_fps(teks):
    """frameRate DASH boleh berbentuk pecahan seperti 120000/4004 (29,97 fps).

    Bentuk pecahan itu yang dipakai untuk laju frame NTSC, dan mengubahnya
    langsung dengan float() akan gagal.
    """
    if not teks:
        return None
    teks = teks.strip()
    if "/" in teks:
        a, b = teks.split("/", 1)
        try:
            pembagi = float(b)
            return float(a) / pembagi if pembagi else None
        except ValueError:
            return None
    try:
        return float(teks)
    except ValueError:
        return None


def urai_durasi(teks):
    """Durasi ISO 8601 seperti PT0H9M56.46S menjadi detik."""
    m = re.match(r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?", teks or "")
    if not m:
        return 0.0
    d, h, mi, s = (float(x) if x else 0.0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def baca_mpd(path):
    """(durasi, dur_segmen, template_media, template_init, [representasi])."""
    root = ET.parse(path).getroot()
    dur = urai_durasi(root.get("mediaPresentationDuration"))
    st = root.find(".//m:SegmentTemplate", NS)
    if st is None:
        raise RuntimeError("MPD tanpa SegmentTemplate; format ini tidak didukung")
    ts = float(st.get("timescale", "1"))
    dseg = float(st.get("duration", "0")) / ts if ts else 0.0
    if dseg <= 0:
        raise RuntimeError("durasi segmen tidak terbaca dari SegmentTemplate")
    reps = []
    for r in root.findall(".//m:Representation", NS):
        reps.append({
            "bw": int(r.get("bandwidth")),
            "w": r.get("width"), "h": r.get("height"),
            "codecs": r.get("codecs"),
            "fps": r.get("frameRate"),
        })
    if not reps:
        raise RuntimeError("MPD tanpa Representation")
    return (dur, dseg, st.get("media"), st.get("initialization"),
            int(st.get("startNumber", "1")), reps)


def isi_template(tpl, bw, nomor=None):
    out = tpl.replace("$Bandwidth$", str(bw))
    if nomor is not None:
        out = re.sub(r"\$Number(?:%0(\d+)d)?\$",
                     lambda m: (f"{nomor:0{int(m.group(1))}d}" if m.group(1)
                                else str(nomor)), out)
    return out


def media_playlist(dasar, tpl_media, tpl_init, bw, mulai, dseg, dur_total):
    """Playlist satu representasi; jumlah segmen dibaca dari BERKAS NYATA.

    Menghitung jumlah segmen dari durasi saja berisiko: pembulatan dapat
    menghasilkan satu segmen lebih atau kurang, dan playlist yang menunjuk ke
    berkas tidak ada membuat pemutar berhenti di tengah tanpa pesan jelas.
    """
    baris = []
    n = 0
    while True:
        rel = isi_template(tpl_media, bw, mulai + n)
        if not os.path.isfile(os.path.join(dasar, rel)):
            break
        baris.append(rel)
        n += 1
    if not baris:
        return None, 0
    sisa = dur_total - (n - 1) * dseg
    akhir = max(min(sisa, dseg), 0.001) if dur_total > 0 else dseg

    out = ["#EXTM3U", "#EXT-X-VERSION:7",
           f"#EXT-X-TARGETDURATION:{int(round(dseg))}",
           f"#EXT-X-MEDIA-SEQUENCE:{mulai}",
           "#EXT-X-PLAYLIST-TYPE:VOD",
           f'#EXT-X-MAP:URI="{isi_template(tpl_init, bw)}"']
    for i, rel in enumerate(baris):
        d = akhir if i == len(baris) - 1 else dseg
        out.append(f"#EXTINF:{d:.3f},")
        out.append(rel)
    out.append("#EXT-X-ENDLIST")
    return "\n".join(out) + "\n", n


def master_playlist(reps, nama):
    out = ["#EXTM3U", "#EXT-X-VERSION:7"]
    for r in sorted(reps, key=lambda x: x["bw"]):
        atr = [f'BANDWIDTH={r["bw"]}']
        if r.get("w") and r.get("h"):
            atr.append(f'RESOLUTION={r["w"]}x{r["h"]}')
        if r.get("codecs"):
            atr.append(f'CODECS="{r["codecs"]}"')
        fps = urai_fps(r.get("fps"))
        if fps:
            atr.append(f"FRAME-RATE={fps:.3f}")
        out.append("#EXT-X-STREAM-INF:" + ",".join(atr))
        out.append(f'{nama}_{r["bw"]}.m3u8')
    return "\n".join(out) + "\n"


def self_test():
    import tempfile
    assert abs(urai_fps("120000/4004") - 29.97002997) < 1e-6
    assert urai_fps("24") == 24.0
    assert urai_fps("30000/1001") is not None
    assert urai_fps(None) is None and urai_fps("x/y") is None
    assert urai_fps("24/0") is None
    print("  [OK] frameRate pecahan terurai (120000/4004 = 29,970), "
          "bentuk rusak mengembalikan None")

    assert abs(urai_durasi("PT0H9M56.46S") - 596.46) < 1e-6
    assert abs(urai_durasi("PT5M") - 300.0) < 1e-9
    print("  [OK] durasi ISO 8601 terurai (596,46 detik)")

    tpl = "bunny_$Bandwidth$bps/BigBuckBunny_4s$Number$.m4s"
    assert isi_template(tpl, 45226, 7) == "bunny_45226bps/BigBuckBunny_4s7.m4s"
    tpl2 = "r$Bandwidth$/seg$Number%05d$.m4s"
    assert isi_template(tpl2, 100, 7) == "r100/seg00007.m4s"
    print("  [OK] template $Bandwidth$ dan $Number$ terisi, termasuk berpadding")

    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "bunny_45226bps"))
    open(os.path.join(d, "bunny_45226bps", "BigBuckBunny_4s_init.mp4"), "w").close()
    for i in range(1, 6):
        open(os.path.join(d, "bunny_45226bps", f"BigBuckBunny_4s{i}.m4s"), "w").close()

    # durasi MPD yang JAUH melebihi segmen nyata: segmen terakhir tetap penuh,
    # bukan menjadi durasi raksasa
    plx, nx = media_playlist(d, tpl,
                             "bunny_$Bandwidth$bps/BigBuckBunny_4s_init.mp4",
                             45226, 1, 4.0, 596.46)
    ex = [x for x in plx.split("\n") if x.startswith("#EXTINF")]
    assert nx == 5 and ex[-1] == "#EXTINF:4.000,", ex[-1]
    assert abs(sum(float(x[8:-1]) for x in ex) - 20.0) < 1e-6
    print("  [OK] MPD 596s dgn 5 segmen nyata tetap menghasilkan total 20s")

    pl, n = media_playlist(d, tpl,
                           "bunny_$Bandwidth$bps/BigBuckBunny_4s_init.mp4",
                           45226, 1, 4.0, 18.5)
    assert n == 5, n
    print(f"  [OK] jumlah segmen dibaca dari berkas NYATA: {n}")

    # segmen keenam tidak ada, jadi TIDAK boleh muncul di playlist
    assert "BigBuckBunny_4s6.m4s" not in pl
    print("  [OK] segmen yang tidak ada tidak dicantumkan")

    # EXT-X-MAP wajib, karena fMP4 tanpa init segment tidak dapat diputar
    assert '#EXT-X-MAP:URI="bunny_45226bps/BigBuckBunny_4s_init.mp4"' in pl
    assert "#EXT-X-VERSION:7" in pl
    print("  [OK] EXT-X-MAP menunjuk init segment, versi 7 untuk fMP4")

    # durasi segmen terakhir harus PARSIAL, bukan penuh
    ext = [x for x in pl.split("\n") if x.startswith("#EXTINF")]
    assert len(ext) == 5
    assert ext[0] == "#EXTINF:4.000," and ext[-1] == "#EXTINF:2.500,", ext[-1]
    assert abs(sum(float(x[8:-1]) for x in ext) - 18.5) < 1e-6
    print(f"  [OK] segmen terakhir {ext[-1][8:-1]}s, total durasi cocok 18,5s")

    # durasi total yang PAS membagi habis tidak boleh menghasilkan segmen nol
    pl2, _ = media_playlist(d, tpl,
                            "bunny_$Bandwidth$bps/BigBuckBunny_4s_init.mp4",
                            45226, 1, 4.0, 20.0)
    e2 = [x for x in pl2.split("\n") if x.startswith("#EXTINF")]
    assert e2[-1] == "#EXTINF:4.000,", e2[-1]
    print("  [OK] durasi yang membagi habis menghasilkan segmen terakhir penuh")

    # frameRate pecahan tidak boleh menggagalkan master playlist
    mpf = master_playlist([{"bw": 1, "w": "8", "h": "6", "codecs": "c",
                            "fps": "120000/4004"}], "X")
    assert "FRAME-RATE=29.970" in mpf, mpf
    print("  [OK] master playlist menerima frameRate pecahan")

    mp = master_playlist([{"bw": 45226, "w": "320", "h": "240",
                           "codecs": "avc1.42c00d", "fps": "24"},
                          {"bw": 3936261, "w": "1920", "h": "1080",
                           "codecs": "avc1.42c032", "fps": "24"}], "BBB")
    # varian harus terurut menaik, supaya pemutar memulai dari rung terendah
    i1, i2 = mp.index("BANDWIDTH=45226"), mp.index("BANDWIDTH=3936261")
    assert i1 < i2
    assert "RESOLUTION=1920x1080" in mp and 'CODECS="avc1.42c032"' in mp
    assert mp.count("#EXT-X-STREAM-INF") == 2
    print("  [OK] master playlist terurut menaik dgn resolusi dan codec")
    print("\nSEMUA UJI LULUS")
    return True


def main():
    ap = argparse.ArgumentParser(description="Bungkus ulang DASH menjadi HLS")
    ap.add_argument("--dir", default="/home/yb/RisetPakBandung2026/dash")
    ap.add_argument("--judul", default=None, help="hanya satu judul; bawaan semua")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        sys.exit(0 if self_test() else 1)
    if not os.path.isdir(a.dir):
        sys.exit(f"direktori tidak ada: {a.dir}")

    mpds = sorted(glob.glob(os.path.join(a.dir, "*", "*.mpd")))
    if a.judul:
        mpds = [p for p in mpds if os.path.basename(os.path.dirname(p)) == a.judul]
    if not mpds:
        sys.exit(f"tidak ada *.mpd di {a.dir}/*/")

    total_var = 0
    peringatan = []
    for pm in mpds:
        judul = os.path.basename(os.path.dirname(pm))
        dasar = os.path.dirname(pm)
        nama = os.path.splitext(os.path.basename(pm))[0]
        try:
            dur, dseg, tmedia, tinit, mulai, reps = baca_mpd(pm)
        except RuntimeError as e:
            print(f"  {judul:<22} DILEWATI: {e}")
            continue

        jadi, n_seg = [], set()
        for r in reps:
            pl, n = media_playlist(dasar, tmedia, tinit, r["bw"], mulai, dseg, dur)
            if pl is None:
                if a.verbose:
                    print(f"    {r['bw']} bps: tidak ada segmen, dilewati")
                continue
            with open(os.path.join(dasar, f"{nama}_{r['bw']}.m3u8"), "w",
                      encoding="utf-8") as f:
                f.write(pl)
            jadi.append(r)
            n_seg.add(n)

        if not jadi:
            print(f"  {judul:<22} DILEWATI: tidak ada representasi bersegmen")
            continue
        with open(os.path.join(dasar, f"{nama}.m3u8"), "w", encoding="utf-8") as f:
            f.write(master_playlist(jadi, nama))
        total_var += len(jadi)
        n1 = sorted(n_seg)[0]
        dur_nyata = n1 * dseg
        tanda = "" if len(n_seg) == 1 else f"  <- SEGMEN TIDAK SERAGAM: {sorted(n_seg)}"
        print(f"  {judul:<22}{len(jadi):>3} varian, {n1:>3} segmen, "
              f"konten {dur_nyata:.1f}s, MPD {dur:.1f}s, segmen {dseg:.1f}s{tanda}")
        # Durasi MPD yang jauh melebihi jumlah segmen berarti hanya sebagian
        # konten yang ada di disk. Pada DASH hal itu tidak terlihat selama sesi
        # berhenti sebelum segmen yang hilang dibutuhkan, tetapi playlist HLS
        # menyatakan durasi SEBENARNYA sehingga pemutar tahu videonya berakhir.
        if dur > 0 and abs(dur - dur_nyata) > dseg:
            peringatan.append((judul, dur_nyata, dur))

    print(f"\n{len(mpds)} judul, {total_var} playlist varian ditulis")
    if peringatan:
        print(f"\nPERHATIAN: {len(peringatan)} judul memiliki konten LEBIH PENDEK "
              f"daripada yang dinyatakan MPD-nya:")
        for j, nyata, mpd in peringatan:
            print(f"  {j:<22}konten {nyata:.0f}s, MPD menyatakan {mpd:.0f}s")
        print("\n  Playlist HLS menyatakan durasi sebenarnya, sedangkan MPD tidak.")
        print("  Akibatnya hls.js mengetahui video berakhir sementara dash.js")
        print("  mengira masih ada sisa. Bila durasi sesi mendekati panjang konten,")
        print("  perilaku menjelang akhir dapat BERBEDA antar kedua lengan, dan")
        print("  perbedaan itu bukan berasal dari pemutar melainkan dari konten.")
        print("  Pakai durasi sesi yang lebih pendek, mis. 280 detik untuk konten")
        print("  300 detik, agar kedua lengan sama-sama jauh dari akhir konten.")
    print("\nnginx perlu tipe MIME untuk m3u8. Tambahkan ke blok types:")
    print("    application/vnd.apple.mpegurl  m3u8;")
    print("\nVerifikasi dari laptop:")
    print("  curl -s http://192.168.50.10:8080/BigBuckBunny/"
          "BigBuckBunny_4s_simple_2014_05_09.m3u8 | head -5")


if __name__ == "__main__":
    main()