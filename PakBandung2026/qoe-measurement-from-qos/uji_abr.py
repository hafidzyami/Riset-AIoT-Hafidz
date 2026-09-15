#!/usr/bin/env python3
"""
uji_abr.py — jalankan seluruh mode ABR pada kondisi jaringan identik, satu perintah
-------------------------------------------------------------------------------------
Menjawab satu pertanyaan sebelum koleksi besar dilepas: apakah tiap mode ABR
benar-benar menambah titik yang berbeda pada matriks, atau ada yang sekadar
menduplikasi mode lain.

Tiap mode dijalankan dengan seed tc yang SAMA, sehingga lintasan bandwidth yang
dialami identik dan satu-satunya yang berbeda adalah algoritma ABR-nya. Setelah
seluruh mode selesai, perbandingannya dijalankan otomatis.

Sensor eBPF dan RPi 5 TIDAK dilibatkan. Yang dibandingkan adalah representasi apa
yang benar-benar diputar, dan itu berasal dari telemetri klien.

Trafik latar dimatikan secara bawaan. Jumlah byte yang benar-benar terunduh
bervariasi antar-percobaan meski jadwalnya deterministik, dan variasi itu akan
mengaburkan perbedaan antar-algoritma yang justru sedang dicari.

Pakai:
  python uji_abr.py --seed 3 --title BigBuckBunny
  python uji_abr.py --seed 3 --modes throughput,bola --duration 180
  python uji_abr.py --self-test
"""
import argparse
import os
import subprocess
import sys
import time

MPD = {
    "BigBuckBunny": "BigBuckBunny_4s_simple_2014_05_09.mpd",
    "ElephantsDream": "ElephantsDream_4s_simple_2014_05_09.mpd",
    "OfForestAndMen": "OfForestAndMen_4s_simple_2014_05_09.mpd",
    "RedBullPlayStreets": "RedBull_4_simple_2014_05_09.mpd",
    "TearsOfSteel": "TearsOfSteel_4s_simple_2014_05_09.mpd",
    "TheSwissAccount": "TheSwissAccount_4s_simple_2014_05_09.mpd",
    "Valkaama": "Valkaama_4s_simple_2014_05_09.mpd",
}
MODE_SAH = ["throughput", "dynamic", "bola", "l2a", "lolp"]

JEDA_MUKA = 8          # detik; jadwal tc mulai lebih dulu
TAMBAHAN = 30          # detik; jadwal tc berjalan lebih lama dari pemutaran


def sh(cmd, timeout=120):
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


def baru_ditulis(ssh, path, maks=60):
    """True bila berkas ada DAN diubah dalam maks detik terakhir.

    Pemeriksaan proses saja pernah menyesatkan: pgrep melaporkan proses hidup
    padahal jadwal tc tidak pernah berjalan, sehingga satu run lima menit
    menghasilkan data tanpa pembatasan bandwidth sama sekali.
    """
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


def bangun(a, mode):
    rid = f"ABR_{mode}"
    opsi = ["-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=20"]
    ssh4 = ["ssh"] + opsi + [f"{a.pi4_user}@{a.pi4_host}"]
    tclog = f"tc_uji_abr_s{a.seed}.jsonl"
    meta = os.path.join(a.results, f"{rid}_client_metadata.json")
    return {
        "rid": rid, "ssh4": ssh4, "tclog": tclog, "meta": meta,
        "bersih4": ssh4 + [f"cd {a.pi4_dir} && rm -f {tclog} tc.log; "
                           f"sudo /usr/sbin/tc qdisc del dev {a.iface} root "
                           f"2>/dev/null; true"],
        "tc": ssh4 + [
            f"cd {a.pi4_dir} && setsid nohup /usr/bin/python3 tc_continuous.py "
            f"--seed {a.seed} --iface {a.iface} "
            f"--duration {a.duration + TAMBAHAN} --log {tclog} "
            f"< /dev/null > tc.log 2>&1 & echo mulai"],
        "stop4": ssh4 + ["/usr/bin/pkill -f '[t]c_continuous' ; true"],
        "bereskan4": ssh4 + [f"sudo /usr/sbin/tc qdisc del dev {a.iface} root "
                             f"2>/dev/null; true"],
        "bg": [sys.executable, "bg_traffic.py", "--server",
               f"http://{a.server_ip}:{a.port}", "--seed", str(a.seed),
               "--duration", str(a.duration), "--max-mbps", str(a.bg_max_mbps),
               "--log", os.path.join(a.results, f"bg_{rid}.jsonl")],
        "harness": ["node", "harness.js", "--mpd",
                    f"http://{a.server_ip}:{a.port}/{a.title}/{MPD[a.title]}",
                    "--duration", str(a.duration), "--run-id", rid,
                    "--abr", mode, "--display", a.display, "--out", meta],
    }


def self_test():
    class A:
        seed = 3; title = "RedBullPlayStreets"; duration = 300
        display = "1280x720"; results = "hasil_abr"; iface = "eth0"
        server_ip = "192.168.50.10"; port = 8080; bg_max_mbps = 1.5
        pi4_user = "yb"; pi4_host = "10.0.0.4"; pi4_dir = "~/x"

    c = bangun(A(), "bola")
    assert c["rid"] == "ABR_bola"
    assert "RedBull_4_simple" in " ".join(c["harness"])
    print(f"  [OK] {c['rid']}, nama MPD tak seragam ditangani")

    assert "--abr bola" in " ".join(c["harness"])
    assert c["meta"].endswith("ABR_bola_client_metadata.json")
    print("  [OK] mode diteruskan ke harness, metadata bernama per mode")

    # seed tc HARUS sama untuk seluruh mode, karena itulah inti pengujiannya
    seeds = set()
    for m in ("throughput", "dynamic", "bola"):
        cc = bangun(A(), m)
        seeds.add([x for x in " ".join(cc["tc"]).split() if x.isdigit()][0])
    assert len(seeds) == 1, seeds
    print(f"  [OK] seluruh mode memakai seed tc yang sama ({seeds.pop()}), "
          f"sehingga lintasan bandwidth identik")

    t = " ".join(c["tc"])
    assert "setsid" in t and "< /dev/null" in t and "-n" in c["ssh4"]
    print("  [OK] stdin dilepas tiga lapis (ssh -n, setsid, < /dev/null)")
    assert "/usr/bin/python3 tc_continuous.py" in t
    print("  [OK] dipanggil lewat interpreter, tidak bergantung bit executable")
    assert f"--duration {300 + TAMBAHAN}" in t
    print(f"  [OK] jadwal tc {300+TAMBAHAN}s, melampaui pemutaran 300s")
    assert "rm -f tc_uji_abr_s3.jsonl" in " ".join(c["bersih4"])
    print("  [OK] catatan tc lama dihapus sebelum tiap mode")
    assert "[t]c_continuous" in " ".join(c["stop4"]) and "nohup" not in " ".join(c["stop4"])
    print("  [OK] pkill memakai pola bracket dan terpisah dari peluncuran")
    print("\nSEMUA UJI LULUS")
    return True


def main():
    ap = argparse.ArgumentParser(
        description="Uji seluruh mode ABR pada kondisi jaringan identik")
    ap.add_argument("--seed", type=int, default=3,
                    help="seed lintasan bandwidth; pilih yang menengah agar ABR "
                         "benar-benar harus beradaptasi")
    ap.add_argument("--title", default="BigBuckBunny", choices=sorted(MPD))
    ap.add_argument("--modes", default="throughput,dynamic,bola,l2a")
    ap.add_argument("--duration", type=float, default=300)
    ap.add_argument("--results", default="hasil_abr")
    ap.add_argument("--display", default="1280x720")
    ap.add_argument("--server-ip", default="192.168.50.10")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--iface", default="eth0")
    ap.add_argument("--bg", action="store_true",
                    help="sertakan trafik latar. Bawaannya MATI, karena jumlah byte "
                         "yang terunduh bervariasi antar-percobaan dan akan "
                         "mengaburkan perbedaan antar-algoritma")
    ap.add_argument("--bg-max-mbps", type=float, default=1.5)
    ap.add_argument("--pi4-user", default="yb")
    ap.add_argument("--pi4-host", default="192.168.18.233")
    ap.add_argument("--pi4-dir", default="~/RisetPakBandung2026")
    ap.add_argument("--skip-banding", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        sys.exit(0 if self_test() else 1)

    modes = [m.strip().lower() for m in a.modes.split(",") if m.strip()]
    salah = [m for m in modes if m not in MODE_SAH]
    if salah:
        ap.error(f"mode tidak dikenal: {salah}; pilihan: {MODE_SAH}")
    os.makedirs(a.results, exist_ok=True)

    per_mode = a.duration + JEDA_MUKA + 25
    print(f">> {len(modes)} mode pada seed {a.seed}, judul {a.title}")
    print(f">> mode     : {', '.join(modes)}")
    print(f">> trafik latar: {'AKTIF' if a.bg else 'mati'}")
    print(f">> perkiraan {len(modes)*per_mode/60:.0f} menit\n")

    if a.dry_run:
        for m in modes:
            c = bangun(a, m)
            print(f"  [{m}]")
            for k in ("bersih4", "tc", "harness", "stop4", "bereskan4"):
                print(f"    {k:<11} {' '.join(c[k])}")
            print()
        return

    berhasil = []
    for i, m in enumerate(modes, 1):
        c = bangun(a, m)
        print(f"[{i}/{len(modes)}] {c['rid']}  "
              f"(sisa ~{(len(modes)-i+1)*per_mode/60:.0f} menit)")
        bg = None
        try:
            sh(c["bersih4"])
            luncurkan(c["tc"])
            time.sleep(JEDA_MUKA)

            segar, ket = baru_ditulis(c["ssh4"], f"{a.pi4_dir}/{c['tclog']}")
            print(f"    jadwal tc: catatan {ket}")
            if not segar:
                r = sh(c["ssh4"] + [f"tail -8 {a.pi4_dir}/tc.log 2>/dev/null "
                                    f"|| echo '(log tidak ada)'"])
                for ln in (r.stdout or "").strip().split("\n"):
                    print(f"      {ln}")
                print("    dilewati")
                continue

            if a.bg:
                bg = subprocess.Popen(c["bg"], stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
            r = sh(c["harness"], timeout=a.duration + 240)
            for ln in (r.stdout or "").strip().split("\n")[-2:]:
                if ln.strip():
                    print(f"    {ln.strip()}")
            if os.path.exists(c["meta"]):
                berhasil.append(c["meta"])
            else:
                print(f"    GAGAL: {c['meta']} tidak terbentuk")
        finally:
            if bg and bg.poll() is None:
                bg.terminate()
                try:
                    bg.wait(timeout=10)
                except Exception:
                    bg.kill()
            sh(c["stop4"])
            sh(c["bereskan4"])
        print()

    print(f">> {len(berhasil)} dari {len(modes)} mode selesai")
    if a.skip_banding or len(berhasil) < 2:
        if len(berhasil) < 2:
            print("   perbandingan butuh minimal dua mode yang berhasil")
        return

    print(f"\n{'='*70}\nPERBANDINGAN\n{'='*70}")
    subprocess.run([sys.executable, "banding_abr.py"] + berhasil)


if __name__ == "__main__":
    main()