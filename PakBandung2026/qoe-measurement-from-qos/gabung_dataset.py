#!/usr/bin/env python3
"""
gabung_dataset.py — gabungkan dua dataset pada kunci (run_id, window_index)
----------------------------------------------------------------------------
Dibuat untuk menguji apakah set fitur yang saling melengkapi memberi tambahan
ketika dipakai bersama. Misalnya set 25 fitur penelitian ini yang memuat jitter
dan RTT, digabung dengan 82 fitur gaya ViCrypt yang seluruhnya turunan cuplikan
throughput.

Penggabungan dilakukan sebagai inner join pada (run_id, window_index), sehingga
hasilnya hanya memuat window yang ADA DI KEDUANYA. Itu disengaja: window yang
hanya ada di satu berkas akan membuat perbandingan berhenti berpasangan.

Kolom yang bernama sama di kedua berkas diambil dari berkas PERTAMA, dan yang
kedua diabaikan. Kolom semacam itu dilaporkan supaya tidak ada yang tergabung
diam-diam.

Pakai:
  python gabung_dataset.py --a hasil_v5/dataset_ms.csv --b hasil_v5/dataset_vc.csv \\
      --out hasil_v5/dataset_gab.csv
  python gabung_dataset.py --self-test
"""
import argparse
import csv
import os
import sys

KUNCI = ("run_id", "window_index")


def baca(path):
    with open(path, encoding="utf-8") as f:
        r = csv.DictReader(f)
        return r.fieldnames or [], {(x["run_id"], x["window_index"]): x for x in r}


def gabung(kol_a, baris_a, kol_b, baris_b):
    """(kolom, baris, bersama) hasil inner join pada kunci."""
    bersama = [c for c in kol_b if c in kol_a and c not in KUNCI]
    tambah = [c for c in kol_b if c not in kol_a]
    kol = list(kol_a) + tambah
    kunci = [k for k in baris_a if k in baris_b]
    out = []
    for k in kunci:
        row = dict(baris_a[k])
        for c in tambah:
            row[c] = baris_b[k][c]
        out.append(row)
    return kol, out, bersama


def self_test():
    import tempfile
    d = tempfile.mkdtemp()

    def tulis(nama, kol, baris):
        p = os.path.join(d, nama)
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=kol)
            w.writeheader()
            w.writerows(baris)
        return p

    pa = tulis("a.csv", ["run_id", "window_index", "label", "tp", "x1"],
               [{"run_id": "R1", "window_index": "0", "label": "Good",
                 "tp": "1.0", "x1": "11"},
                {"run_id": "R1", "window_index": "1", "label": "Critical",
                 "tp": "0.2", "x1": "12"},
                {"run_id": "R2", "window_index": "0", "label": "Good",
                 "tp": "2.0", "x1": "13"}])
    pb = tulis("b.csv", ["run_id", "window_index", "label", "tp", "y1", "y2"],
               [{"run_id": "R1", "window_index": "0", "label": "Good",
                 "tp": "1.0", "y1": "21", "y2": "31"},
                {"run_id": "R1", "window_index": "1", "label": "Critical",
                 "tp": "0.2", "y1": "22", "y2": "32"},
                {"run_id": "R3", "window_index": "0", "label": "Good",
                 "tp": "9.0", "y1": "23", "y2": "33"}])

    ka, ba = baca(pa)
    kb, bb = baca(pb)
    kol, out, bersama = gabung(ka, ba, kb, bb)

    # inner join: R2 hanya di A dan R3 hanya di B, keduanya dibuang
    assert len(out) == 2, len(out)
    assert {(r["run_id"], r["window_index"]) for r in out} == {("R1", "0"), ("R1", "1")}
    print(f"  [OK] inner join menyisakan {len(out)} baris; yang hanya ada di satu "
          f"berkas dibuang")

    # kolom unik dari B ikut terbawa
    assert "y1" in kol and "y2" in kol
    assert out[0]["y1"] == "21" and out[0]["x1"] == "11"
    print("  [OK] kolom unik kedua berkas terbawa tanpa tertukar")

    # kolom bersama dilaporkan dan diambil dari berkas pertama
    assert set(bersama) == {"label", "tp"}, bersama
    assert out[0]["tp"] == "1.0"
    print(f"  [OK] kolom bersama {sorted(bersama)} dilaporkan, nilai dari berkas A")

    # tidak ada kolom yang terduplikasi di header
    assert len(kol) == len(set(kol)), kol
    print(f"  [OK] header {len(kol)} kolom tanpa duplikat")

    # urutan kunci mengikuti berkas A, supaya hasilnya reprodusibel
    assert [(r["run_id"], r["window_index"]) for r in out] == [("R1", "0"), ("R1", "1")]
    print("  [OK] urutan baris mengikuti berkas A")
    print("\nSEMUA UJI LULUS")
    return True


def main():
    ap = argparse.ArgumentParser(description="Gabungkan dua dataset pada kunci baris")
    ap.add_argument("--a", required=False)
    ap.add_argument("--b", required=False)
    ap.add_argument("--out", required=False)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        sys.exit(0 if self_test() else 1)
    if not (a.a and a.b and a.out):
        ap.error("perlu --a, --b, dan --out")
    for p in (a.a, a.b):
        if not os.path.exists(p):
            sys.exit(f"tidak ketemu: {p}")

    ka, ba = baca(a.a)
    kb, bb = baca(a.b)
    kol, out, bersama = gabung(ka, ba, kb, bb)
    if not out:
        sys.exit("tidak ada kunci (run_id, window_index) yang sama di kedua berkas")

    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=kol)
        w.writeheader()
        w.writerows(out)

    print(f"{os.path.basename(a.a)} ({len(ba):,} baris, {len(ka)} kolom)")
    print(f"{os.path.basename(a.b)} ({len(bb):,} baris, {len(kb)} kolom)")
    print(f"-> {a.out}  ({len(out):,} baris, {len(kol)} kolom)")
    hilang_a, hilang_b = len(ba) - len(out), len(bb) - len(out)
    if hilang_a or hilang_b:
        print(f"   dibuang: {hilang_a:,} baris hanya di A, {hilang_b:,} hanya di B")
    if bersama:
        tampil = ", ".join(sorted(bersama)[:6]) + (" ..." if len(bersama) > 6 else "")
        print(f"   {len(bersama)} kolom bernama sama diambil dari A: {tampil}")


if __name__ == "__main__":
    main()