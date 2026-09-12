#!/usr/bin/env python3
"""
run_eval_fase2.py — evaluasi Fase 2 pada beberapa sesi, satu perintah
-----------------------------------------------------------------------
Menjalankan loop inferensi real-time di RPi 5, jadwal bandwidth di RPi 4, serta
harness dan trafik latar di laptop, lalu membandingkan kelas yang dikeluarkan
dengan label P.1203 sebenarnya. Diulang untuk beberapa seed dan diagregasi.

Perlu beberapa sesi karena satu sesi tidak cukup: seed yang berbeda menjelajah
pita bandwidth yang berbeda, sehingga distribusi kelasnya bisa sangat miring.
Pada seed 1, sesi berisi 61 persen Degraded sementara data latih seimbang, dan
akurasi mentahnya tidak dapat ditafsirkan tanpa pembanding.

Prasyarat:
  - logreg_standalone.py sudah ada di RPi 5 (hasil export_linear.py)
  - tc_continuous.py sudah ada di RPi 4
  - label_from_metadata.py dan eval_fase2.py ada di direktori kerja laptop

Pakai:
  python run_eval_fase2.py --seeds 1,3,5,7 --title BigBuckBunny
  python run_eval_fase2.py --seeds 1,3 --dry-run
  python run_eval_fase2.py --self-test
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import time
from collections import Counter

MPD = {
    "BigBuckBunny": "BigBuckBunny_4s_simple_2014_05_09.mpd",
    "ElephantsDream": "ElephantsDream_4s_simple_2014_05_09.mpd",
    "OfForestAndMen": "OfForestAndMen_4s_simple_2014_05_09.mpd",
    "RedBullPlayStreets": "RedBull_4_simple_2014_05_09.mpd",
    "TearsOfSteel": "TearsOfSteel_4s_simple_2014_05_09.mpd",
    "TheSwissAccount": "TheSwissAccount_4s_simple_2014_05_09.mpd",
    "Valkaama": "Valkaama_4s_simple_2014_05_09.mpd",
}

JEDA_MUKA = 8          # detik; inferensi dan tc mulai lebih dulu
JEDA_AKHIR = 25        # detik; menutupi ekor window yang tergeser stall
TAMBAHAN = 40


def urai_angka(teks):
    out = []
    for b in teks.split(","):
        b = b.strip()
        if not b:
            continue
        if "-" in b:
            a, z = b.split("-", 1)
            out += list(range(int(a), int(z) + 1))
        else:
            out.append(int(b))
    return sorted(set(out))


def sh(cmd, cek=False, timeout=120):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 1, "", "timeout")


def luncurkan(cmd, timeout=20):
    """Peluncuran latar; timeout DIABAIKAN karena sesi SSH kerap menggantung."""
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        pass


def berjalan(ssh, pola):
    r = sh(ssh + [f"pgrep -f '{pola}' > /dev/null && echo ADA || echo TIADA"], timeout=30)
    return "ADA" in (r.stdout or "")


def baru_ditulis(ssh, path, maks=60):
    r = sh(ssh + [f"test -f {path} && echo $(( $(date +%s) - $(stat -c %Y {path}) )) "
                  f"|| echo TIDAKADA"], timeout=30)
    k = (r.stdout or "").strip()
    if not k or "TIDAKADA" in k:
        return False, "berkas tidak ada"
    try:
        umur = int(k.split()[-1])
    except ValueError:
        return False, f"keluaran tak terduga: {k[:30]}"
    return (umur <= maks), (f"segar ({umur}s)" if umur <= maks
                            else f"berumur {umur}s, sisa run lama")


def bangun(a, seed):
    rid = f"FASE2_s{seed}_{a.title}"
    total = a.duration + JEDA_MUKA + JEDA_AKHIR + TAMBAHAN
    opsi = ["-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=20"]
    ssh5 = ["ssh"] + opsi + [f"{a.pi5_user}@{a.pi5_host}"]
    ssh4 = ["ssh"] + opsi + [f"{a.pi4_user}@{a.pi4_host}"]
    tclog = f"tc_f2_s{seed}.jsonl"
    return {
        "rid": rid, "ssh5": ssh5, "ssh4": ssh4, "tclog": tclog,
        "mpd": f"http://{a.server_ip}:{a.port}/{a.title}/{MPD[a.title]}",
        # Berkas keluaran lama WAJIB dihapus: bila tersisa, verifikasi
        # kesegaran bisa lolos oleh berkas sesi sebelumnya.
        "bersih5": ssh5 + [f"cd {a.pi5_dir} && rm -f {a.infer_out} infer.log"],
        "bersih4": ssh4 + [f"cd {a.pi4_dir} && rm -f {tclog} tc.log; "
                           f"sudo /usr/sbin/tc qdisc del dev {a.iface} root "
                           f"2>/dev/null; true"],
        "infer": ssh5 + [
            f"cd {a.pi5_dir} && setsid nohup sudo /usr/bin/python3 infer_realtime.py "
            f"--model {a.model} --pure --window {a.window} --poll {a.poll} "
            f"--duration {total} --out {a.infer_out} "
            f"< /dev/null > infer.log 2>&1 & echo mulai"],
        "tc": ssh4 + [
            f"cd {a.pi4_dir} && setsid nohup /usr/bin/python3 tc_continuous.py "
            f"--seed {seed} --iface {a.iface} "
            f"--duration {a.duration + JEDA_AKHIR + TAMBAHAN} --log {tclog} "
            f"< /dev/null > tc.log 2>&1 & echo mulai"],
        "stop5": ssh5 + ["sudo /usr/bin/pkill -f '[i]nfer_realtime' ; true"],
        "stop4": ssh4 + ["/usr/bin/pkill -f '[t]c_continuous' ; true"],
        "bereskan4": ssh4 + [f"sudo /usr/sbin/tc qdisc del dev {a.iface} root "
                             f"2>/dev/null; true"],
        "bg": [sys.executable, "bg_traffic.py", "--server",
               f"http://{a.server_ip}:{a.port}", "--seed", str(seed),
               "--duration", str(a.duration), "--max-mbps", str(a.bg_max_mbps),
               "--log", os.path.join(a.results, f"bg_{rid}.jsonl")],
        "harness": ["node", "harness.js", "--mpd",
                    f"http://{a.server_ip}:{a.port}/{a.title}/{MPD[a.title]}",
                    "--duration", str(a.duration), "--run-id", rid,
                    "--abr", a.abr, "--display", a.display,
                    "--out", os.path.join(a.results, f"{rid}_client_metadata.json")],
        "tarik": ["scp", "-o", "BatchMode=yes",
                  f"{a.pi5_user}@{a.pi5_host}:{a.pi5_dir}/{a.infer_out}",
                  os.path.join(a.results, f"{rid}_inferensi.csv")],
        "label": [sys.executable, "label_from_metadata.py",
                  os.path.join(a.results, f"{rid}_client_metadata.json"),
                  "--device", "mobile", "--window", str(a.window),
                  "--out-prefix", os.path.join(a.results, rid)],
        "eval": [sys.executable, "eval_fase2.py",
                 "--infer", os.path.join(a.results, f"{rid}_inferensi.csv"),
                 "--meta", os.path.join(a.results, f"{rid}_client_metadata.json"),
                 "--labels", os.path.join(a.results, f"{rid}_labels.csv"),
                 "--window", str(a.window), "--quiet",
                 "--out-json", os.path.join(a.results, f"{rid}_eval.json")],
    }


def agregasi(results):
    berkas = sorted(glob.glob(os.path.join(results, "FASE2_*_eval.json")))
    if not berkas:
        return
    baris = []
    for p in berkas:
        try:
            baris.append((os.path.basename(p).replace("_eval.json", ""),
                          json.load(open(p, encoding="utf-8"))))
        except Exception:
            continue
    if not baris:
        return

    print(f"\n{'='*78}\nAGREGASI {len(baris)} SESI\n{'='*78}")
    print(f"{'sesi':<28}{'n':>5}{'akurasi':>10}{'mayoritas':>11}{'macro-F1':>9}"
          f"{'<=1 tingkat':>13}{'jarak':>8}")
    print("-" * 87)
    tot_n = tot_benar = tot_may = tot_d1 = 0
    tot_jarak = 0.0
    dist_a, dist_p = Counter(), Counter()
    conf_gab = {}          # confusion gabungan utk macro-F1 lintas sesi
    for nama, r in baris:
        n = r["n"]
        tot_n += n
        tot_benar += r["akurasi"] * n
        tot_may += r["akurasi_mayoritas"] * n
        tot_d1 += (r.get("dalam_1_tingkat") or 0) * n
        tot_jarak += (r.get("jarak_rata2") or 0) * n
        dist_a.update(r.get("dist_sebenarnya", {}))
        dist_p.update(r.get("dist_prediksi", {}))
        for akt, baris_c in (r.get("confusion") or {}).items():
            for pred, v in baris_c.items():
                conf_gab.setdefault(akt, {}).setdefault(pred, 0)
                conf_gab[akt][pred] += v
        tanda = " " if r["akurasi"] >= r["akurasi_mayoritas"] else "*"
        print(f"{nama:<28}{n:>5}{r['akurasi']*100:>9.1f}%"
              f"{r['akurasi_mayoritas']*100:>10.1f}%"
              f"{r.get('macro_f1') or 0:>9.3f}"
              f"{(r.get('dalam_1_tingkat') or 0)*100:>12.1f}%"
              f"{r.get('jarak_rata2') or 0:>8.2f}{tanda}")
    # macro-F1 gabungan dihitung dari confusion yang dikumpulkan, bukan
    # merata-rata macro-F1 per sesi (yang bias bila jumlah window berbeda)
    hadir = sorted({k for k in conf_gab} | {p for b in conf_gab.values() for p in b},
                   key=lambda k: ["Excellent", "Good", "Degraded", "Critical"].index(k)
                   if k in ["Excellent", "Good", "Degraded", "Critical"] else 9)
    f1s = []
    for k in hadir:
        tp = conf_gab.get(k, {}).get(k, 0)
        fp = sum(b.get(k, 0) for akt, b in conf_gab.items() if akt != k)
        fn = sum(v for p, v in conf_gab.get(k, {}).items() if p != k)
        pr = tp / (tp + fp) if tp + fp else 0.0
        rc = tp / (tp + fn) if tp + fn else 0.0
        f1s.append(2 * pr * rc / (pr + rc) if pr + rc else 0.0)
    mf1_gab = sum(f1s) / len(f1s) if f1s else 0.0
    print("-" * 87)
    print(f"{'GABUNGAN':<28}{tot_n:>5}{tot_benar/tot_n*100:>9.1f}%"
          f"{tot_may/tot_n*100:>10.1f}%{mf1_gab:>9.3f}"
          f"{tot_d1/tot_n*100:>12.1f}%{tot_jarak/tot_n:>8.2f}")
    print(f"\nmacro-F1 gabungan {mf1_gab:.3f} dihitung dari confusion seluruh sesi,")
    print("bukan rata-rata per sesi, sehingga sebanding dgn angka luring.")

    # Rincian per kelas menunjukkan kelas mana yang menyeret macro-F1 turun.
    # Kelas dgn sedikit window sebenarnya akan berderau tinggi, dan itu perlu
    # terlihat sebelum angkanya ditafsirkan.
    print(f"\n{'kelas':<12}{'n asli':>8}{'presisi':>10}{'recall':>9}{'F1':>8}")
    for i, k in enumerate(hadir):
        tp = conf_gab.get(k, {}).get(k, 0)
        fp = sum(b.get(k, 0) for akt, b in conf_gab.items() if akt != k)
        fn = sum(v for p, v in conf_gab.get(k, {}).items() if p != k)
        pr = tp / (tp + fp) if tp + fp else 0.0
        rc = tp / (tp + fn) if tp + fn else 0.0
        catat = "  <- sedikit sampel" if tp + fn < 15 else ""
        print(f"{k:<12}{tp+fn:>8}{pr:>10.3f}{rc:>9.3f}{f1s[i]:>8.3f}{catat}")

    print(f"\nconfusion gabungan (baris=sebenarnya, kolom=prediksi)")
    print(f"  {'':<11}" + "".join(f"{k[:9]:>11}" for k in hadir))
    for akt in hadir:
        print(f"  {akt:<11}" + "".join(f"{conf_gab.get(akt,{}).get(p,0):>11}"
                                       for p in hadir))
    kalah = [n for n, r in baris if r["akurasi"] < r["akurasi_mayoritas"]]
    if kalah:
        print(f"\n* {len(kalah)} sesi di bawah tebakan mayoritas: {', '.join(kalah)}")
        print("  Pada sesi berdistribusi miring, model yang dilatih dengan")
        print("  pembobotan kelas seimbang cenderung menyebarkan prediksi.")

    n = sum(dist_a.values())
    print(f"\n{'kelas':<12}{'sebenarnya':>18}{'prediksi':>18}")
    for k in ["Excellent", "Good", "Degraded", "Critical"]:
        print(f"{k:<12}{dist_a.get(k,0):>8} ({dist_a.get(k,0)/n*100:>4.0f}%)"
              f"{dist_p.get(k,0):>8} ({dist_p.get(k,0)/n*100:>4.0f}%)")


def self_test():
    class A:
        title = "RedBullPlayStreets"; abr = "dynamic"; duration = 300
        window = 10.0; poll = 0.33; bg_max_mbps = 1.5; display = "1280x720"
        server_ip = "192.168.50.10"; port = 8080; iface = "eth0"
        results = "hasil_f2"; model = "logreg_standalone.py"
        infer_out = "inferensi_realtime.csv"
        pi5_user = "hafidz"; pi5_host = "10.0.0.5"; pi5_dir = "~/x/ebpf"
        pi4_user = "yb"; pi4_host = "10.0.0.4"; pi4_dir = "~/x"

    assert urai_angka("1,3,5") == [1, 3, 5]
    assert urai_angka("1-4") == [1, 2, 3, 4]
    print("  [OK] penguraian daftar seed")

    c = bangun(A(), 3)
    assert c["rid"] == "FASE2_s3_RedBullPlayStreets"
    assert "RedBull_4_simple" in c["mpd"]
    print(f"  [OK] {c['rid']}, nama MPD tak seragam ditangani")

    total = 300 + JEDA_MUKA + JEDA_AKHIR + TAMBAHAN
    assert f"--duration {total}" in " ".join(c["infer"])
    assert f"--duration {300 + JEDA_AKHIR + TAMBAHAN}" in " ".join(c["tc"])
    print(f"  [OK] inferensi {total}s dan tc {300+JEDA_AKHIR+TAMBAHAN}s, "
          f"keduanya melampaui pemutaran 300s")

    for k in ("infer", "tc"):
        t = " ".join(c[k])
        assert "setsid" in t and "< /dev/null" in t, k
    assert "-n" in c["ssh5"] and "-n" in c["ssh4"]
    print("  [OK] stdin dilepas tiga lapis (ssh -n, setsid, < /dev/null)")

    assert "/usr/bin/python3 tc_continuous.py" in " ".join(c["tc"])
    assert "/usr/bin/python3 infer_realtime.py" in " ".join(c["infer"])
    print("  [OK] dipanggil lewat interpreter, tidak bergantung bit executable")

    assert f"rm -f {A.infer_out}" in " ".join(c["bersih5"])
    assert "rm -f tc_f2_s3.jsonl" in " ".join(c["bersih4"])
    print("  [OK] berkas keluaran lama dihapus sblm run")

    for k in ("stop5", "stop4"):
        t = " ".join(c[k])
        assert ("[i]nfer_realtime" in t or "[t]c_continuous" in t) and "nohup" not in t
    print("  [OK] pkill memakai pola bracket dan terpisah dari peluncuran")

    assert "--poll 0.33" in " ".join(c["infer"])
    print("  [OK] periode cuplik diteruskan agar cocok dgn data latih")

    # agregasi
    import tempfile
    d = tempfile.mkdtemp()
    for i, (n, ak, may, d1, jr) in enumerate(
            [(28, 0.429, 0.607, 1.0, 0.57), (30, 0.700, 0.400, 1.0, 0.30)], 1):
        json.dump({"n": n, "akurasi": ak, "akurasi_mayoritas": may,
                   "dalam_1_tingkat": d1, "jarak_rata2": jr,
                   "dist_sebenarnya": {"Degraded": n // 2, "Good": n - n // 2},
                   "dist_prediksi": {"Degraded": n // 3, "Good": n - n // 3}},
                  open(os.path.join(d, f"FASE2_s{i}_X_eval.json"), "w"))
    print("\n  --- contoh agregasi ---")
    agregasi(d)
    import shutil
    shutil.rmtree(d)
    print("\nSEMUA UJI LULUS")
    return True


def main():
    ap = argparse.ArgumentParser(description="Evaluasi Fase 2 pada beberapa sesi")
    ap.add_argument("--seeds", default="1,3,5,7")
    ap.add_argument("--title", default="BigBuckBunny", choices=sorted(MPD))
    ap.add_argument("--abr", default="dynamic", choices=["dynamic", "throughput", "bola"])
    ap.add_argument("--duration", type=float, default=300)
    ap.add_argument("--window", type=float, default=10.0)
    ap.add_argument("--poll", type=float, default=0.33)
    ap.add_argument("--model", default="logreg_standalone.py")
    ap.add_argument("--infer-out", default="inferensi_realtime.csv")
    ap.add_argument("--bg-max-mbps", type=float, default=1.5)
    ap.add_argument("--display", default="1280x720")
    ap.add_argument("--results", default="hasil_f2")
    ap.add_argument("--server-ip", default="192.168.50.10")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--iface", default="eth0")
    ap.add_argument("--pi5-user", default="hafidz")
    ap.add_argument("--pi5-host", default="192.168.18.234")
    ap.add_argument("--pi5-dir", default="~/RisetPakBandung2026/ebpf")
    ap.add_argument("--pi4-user", default="yb")
    ap.add_argument("--pi4-host", default="192.168.18.233")
    ap.add_argument("--pi4-dir", default="~/RisetPakBandung2026")
    ap.add_argument("--only-aggregate", action="store_true",
                    help="lewati eksekusi, agregasi berkas *_eval.json yang ada")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        sys.exit(0 if self_test() else 1)
    os.makedirs(a.results, exist_ok=True)
    if a.only_aggregate:
        agregasi(a.results)
        return

    seeds = urai_angka(a.seeds)
    per_run = a.duration + JEDA_MUKA + JEDA_AKHIR + 45
    print(f">> {len(seeds)} sesi pada {a.title}, abr={a.abr}")
    print(f">> perkiraan {len(seeds)*per_run/60:.0f} menit\n")

    if a.dry_run:
        for s in seeds:
            c = bangun(a, s)
            print(f"  [seed {s}] {c['rid']}")
            for k in ("bersih5", "bersih4", "infer", "tc", "bg", "harness",
                      "stop5", "stop4", "tarik", "label", "eval"):
                print(f"    {k:<10} {' '.join(c[k])}")
            print()
        return

    for i, s in enumerate(seeds, 1):
        c = bangun(a, s)
        print(f"[{i}/{len(seeds)}] {c['rid']}  "
              f"(sisa ~{(len(seeds)-i+1)*per_run/60:.0f} menit)")
        bg = None
        try:
            sh(c["bersih5"]); sh(c["bersih4"])
            luncurkan(c["infer"]); luncurkan(c["tc"])
            time.sleep(JEDA_MUKA)

            p5, p4 = berjalan(c["ssh5"], "[i]nfer_realtime"), berjalan(c["ssh4"], "[t]c_continuous")
            f5, k5 = baru_ditulis(c["ssh5"], f"{a.pi5_dir}/infer.log")
            f4, k4 = baru_ditulis(c["ssh4"], f"{a.pi4_dir}/{c['tclog']}")
            print(f"    inferensi: proses {'ada' if p5 else 'TIADA'}, log {k5}")
            print(f"    tc       : proses {'ada' if p4 else 'TIADA'}, catatan {k4}")
            if not (p5 and f5) or not (p4 and f4):
                host, path, nama = ((c["ssh5"], f"{a.pi5_dir}/infer.log", "infer.log")
                                    if not (p5 and f5)
                                    else (c["ssh4"], f"{a.pi4_dir}/tc.log", "tc.log"))
                r = sh(host + [f"tail -8 {path} 2>/dev/null || echo '(log tidak ada)'"])
                for ln in (r.stdout or "").strip().split("\n"):
                    print(f"      {ln}")
                print("    dilewati")
                continue

            bg = subprocess.Popen(c["bg"], stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
            r = sh(c["harness"], timeout=a.duration + 240)
            for ln in (r.stdout or "").strip().split("\n")[-2:]:
                if ln.strip():
                    print(f"    {ln.strip()}")
            time.sleep(JEDA_AKHIR)
        finally:
            if bg and bg.poll() is None:
                bg.terminate()
                try:
                    bg.wait(timeout=10)
                except Exception:
                    bg.kill()
            sh(c["stop5"]); sh(c["stop4"]); sh(c["bereskan4"])

        time.sleep(2)
        if sh(c["tarik"]).returncode != 0:
            print("    GAGAL menarik berkas inferensi, dilewati")
            continue
        for k in ("label", "eval"):
            rr = sh(c[k], timeout=300)
            ekor = [x for x in (rr.stdout or "").strip().split("\n") if x.strip()]
            if k == "eval":
                for ln in ekor:
                    if ln.startswith(("akurasi", "dalam 1", "rata-rata")):
                        print(f"    {ln}")
            elif rr.returncode != 0:
                print(f"    label GAGAL: {(rr.stderr or '')[:120]}")
        print()

    agregasi(a.results)


if __name__ == "__main__":
    main()