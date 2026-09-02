#!/usr/bin/env python3
"""
relabel_all.py — labeli ulang SEMUA run dari data mentah, tanpa mengambil data lagi
------------------------------------------------------------------------------------
Label dan fitur selaras dihitung ulang dari berkas yang sudah ada:
  <run>_client_metadata.json  +  <run>_qos_samples.csv

Berguna untuk:
  - menerapkan perbaikan pada labeler tanpa mengulang 10 jam pengambilan data
  - ANALISIS SENSITIVITAS: coba konteks tampilan / perangkat berbeda, lalu
    bandingkan apakah kesimpulan klasifikasi berubah

Pakai:
  python relabel_all.py --dir hasil
  python relabel_all.py --dir hasil --device pc --display 1920x1080 --out-dir hasil_pc
  python relabel_all.py --dir hasil --out-dir sens_720 --display 1280x720 --merge

Berkas mentah (client_metadata, qos_samples) TIDAK pernah diubah.
"""
import argparse
import glob
import os
import shutil
import subprocess
import sys


def main():
    ap = argparse.ArgumentParser(description="Labeli ulang semua run dari data mentah")
    ap.add_argument("--dir", default="hasil", help="folder berisi hasil run")
    ap.add_argument("--out-dir", default=None,
                    help="folder keluaran (default: sama dgn --dir, menimpa label lama)")
    ap.add_argument("--device", default="mobile", choices=["pc", "mobile", "handheld"])
    ap.add_argument("--display", default=None, help="mis. 1920x1080 (default: dari metadata)")
    ap.add_argument("--window", type=float, default=10.0)
    ap.add_argument("--stall-threshold", type=float, default=None,
                    help="ambang stall (detik) utk analisis sensitivitas, mis. 0.5 atau 2.0")
    ap.add_argument("--merge", action="store_true", help="jalankan merge_dataset.py di akhir")
    ap.add_argument("--min-samples", type=int, default=0)
    a = ap.parse_args()

    out = a.out_dir or a.dir
    os.makedirs(out, exist_ok=True)

    metas = sorted(glob.glob(os.path.join(a.dir, "*_client_metadata.json")))
    if not metas:
        sys.exit(f"tidak ada *_client_metadata.json di {a.dir}")

    print(f">> {len(metas)} run | device={a.device} "
          f"display={a.display or '(dari metadata)'} window={a.window}s"
          + (f" ambang_stall={a.stall_threshold}s" if a.stall_threshold is not None else "")
          + f" -> {out}")

    ok = gagal = 0
    for i, mp in enumerate(metas, 1):
        rid = os.path.basename(mp).replace("_client_metadata.json", "")
        smp = os.path.join(a.dir, f"{rid}_qos_samples.csv")
        if not os.path.exists(smp):
            print(f"  [{i}/{len(metas)}] {rid}: LEWATI, qos_samples.csv tidak ada")
            gagal += 1
            continue

        if out != a.dir:                       # salin data mentah agar folder mandiri
            for suf in ("_client_metadata.json", "_qos_samples.csv"):
                src = os.path.join(a.dir, rid + suf)
                if os.path.exists(src):
                    shutil.copy(src, os.path.join(out, rid + suf))

        cmd = [sys.executable, "label_from_metadata.py", mp, "--device", a.device,
               "--window", str(a.window), "--out-prefix", os.path.join(out, rid)]
        if a.display:
            cmd += ["--display", a.display]
        if a.stall_threshold is not None:
            cmd += ["--stall-threshold", str(a.stall_threshold)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  [{i}/{len(metas)}] {rid}: labeler GAGAL {r.stderr.strip()[:120]}")
            gagal += 1
            continue

        r2 = subprocess.run(
            [sys.executable, "align_qos.py", mp, smp, "--window", str(a.window),
             "--out", os.path.join(out, f"{rid}_qos_aligned.csv")],
            capture_output=True, text=True)
        if r2.returncode != 0:
            print(f"  [{i}/{len(metas)}] {rid}: penyelaras GAGAL {r2.stderr.strip()[:120]}")
            gagal += 1
            continue

        ok += 1
        if i % 20 == 0 or i == len(metas):
            print(f"  ... {i}/{len(metas)} (ok {ok}, gagal {gagal})")

    print(f">> selesai: {ok} berhasil, {gagal} gagal")

    if a.merge and ok:
        mc = [sys.executable, "merge_dataset.py", "--dir", out,
              "--out", os.path.join(out, "dataset.csv")]
        if a.min_samples:
            mc += ["--min-samples", str(a.min_samples)]
        print()
        subprocess.run(mc)


if __name__ == "__main__":
    main()