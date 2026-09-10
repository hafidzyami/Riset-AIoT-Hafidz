#!/usr/bin/env python3
"""
run_one_v4.py — jalankan SATU run koleksi v4 dari laptop
----------------------------------------------------------
Mengoordinasi ketiga perangkat sehingga tidak perlu menekan Enter di empat
terminal. Selisih beberapa detik antar-komponen tidak merusak data karena
penyelarasan dilakukan setelahnya lewat timestamp epoch, tetapi dua syarat
harus dipenuhi: agen dan jadwal tc sudah berjalan SEBELUM pemutaran dimulai,
dan masih berjalan SAMPAI pemutaran selesai. Skrip ini menjamin keduanya
dengan memberi jeda muka dan durasi lebih panjang pada keduanya.

Prasyarat yang sudah ada dari koleksi sebelumnya:
  - kunci SSH dari laptop ke kedua Raspberry Pi
  - sudoers NOPASSWD: /usr/sbin/tc di RPi 4; /usr/bin/python3 dan
    /usr/bin/pkill di RPi 5
  - tc_continuous.py di RPi 4, qos_agent.py di RPi 5

Pakai:
  python run_one_v4.py --seed 1 --title BigBuckBunny --abr dynamic
  python run_one_v4.py --seed 1 --title BigBuckBunny --dry-run
  python run_one_v4.py --self-test
"""
import argparse
import os
import re
import subprocess
import sys
import time

# Nama berkas MPD tidak seragam antar-judul pada DASHDataset2014.
MPD = {
    "BigBuckBunny": "BigBuckBunny_4s_simple_2014_05_09.mpd",
    "ElephantsDream": "ElephantsDream_4s_simple_2014_05_09.mpd",
    "OfForestAndMen": "OfForestAndMen_4s_simple_2014_05_09.mpd",
    "RedBullPlayStreets": "RedBull_4_simple_2014_05_09.mpd",
    "TearsOfSteel": "TearsOfSteel_4s_simple_2014_05_09.mpd",
    "TheSwissAccount": "TheSwissAccount_4s_simple_2014_05_09.mpd",
    "Valkaama": "Valkaama_4s_simple_2014_05_09.mpd",
}

JEDA_MUKA = 8          # detik; agen dan tc mulai sekian detik lebih dulu
TAMBAHAN = 40          # detik; agen dan tc berjalan sekian detik lebih lama
# Jeda sebelum agen dihentikan. Stalling menggeser waktu dinding terhadap waktu
# media, sehingga window terakhir berakhir SETELAH harness selesai. Tanpa jeda
# ini, agen dimatikan lebih dulu dan window penutup keluar tanpa cuplikan sama
# sekali. Pada uji seed 2 dengan 3 stall, window 29 kosong karena sebab itu.
JEDA_AKHIR = 25


def sh(cmd, cek=True, timeout=90):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if cek and r.returncode != 0:
        raise RuntimeError(f"gagal: {' '.join(cmd[:4])}...\n{r.stderr.strip()[:300]}")
    return r


def luncurkan(cmd, timeout=20):
    """Jalankan perintah peluncuran latar, dan anggap timeout sebagai normal.

    SSH tetap menunggu meski proses sudah dilatarbelakangi, karena proses anak
    masih memegang kanal sesi. Redirect stdout dan stderr saja tidak cukup;
    stdin juga harus dilepas. Bahkan dengan ketiganya, beberapa kombinasi sudo
    dan nohup masih menahan sesi. Karena itu timeout di sini TIDAK dianggap
    kegagalan, dan keberhasilan diperiksa lewat panggilan terpisah.
    """
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        pass


def berjalan(ssh_dasar, pola):
    """True bila ada proses yang cocok dengan pola di host tujuan."""
    r = subprocess.run(ssh_dasar + [f"pgrep -f '{pola}' > /dev/null && echo ADA || echo TIADA"],
                       capture_output=True, text=True, timeout=30)
    return "ADA" in (r.stdout or "")


def baru_ditulis(ssh_dasar, path, maks_umur=45):
    """True bila berkas ada DAN diubah dalam maks_umur detik terakhir.

    Pemeriksaan pgrep saja tidak cukup dan pernah menyesatkan: ia melaporkan
    proses hidup padahal jadwal tc tidak pernah berjalan, sehingga satu run
    berdurasi lima menit menghasilkan data tanpa pembatasan bandwidth sama
    sekali. Berkas log yang segar adalah bukti bahwa prosesnya benar-benar
    bekerja, bukan sekadar ada.
    """
    r = subprocess.run(
        ssh_dasar + [f"test -f {path} && echo $(( $(date +%s) - $(stat -c %Y {path}) )) "
                     f"|| echo TIDAKADA"],
        capture_output=True, text=True, timeout=30)
    keluar = (r.stdout or "").strip()
    if not keluar or "TIDAKADA" in keluar:
        return False, "berkas tidak ada"
    try:
        umur = int(keluar.split()[-1])
    except ValueError:
        return False, f"keluaran tak terduga: {keluar[:40]}"
    if umur > maks_umur:
        return False, f"berkas berumur {umur}s, sisa run sebelumnya"
    return True, f"segar ({umur}s)"


def bangun_perintah(a):
    """Kembalikan dict berisi seluruh perintah yang akan dijalankan."""
    total = a.duration + JEDA_MUKA + JEDA_AKHIR + TAMBAHAN
    rid = f"CONT_s{a.seed}_{a.title}"
    proto = "https" if a.https else "http"
    port = a.https_port if a.https else a.port
    mpd = f"{proto}://{a.server_ip}:{port}/{a.title}/{MPD[a.title]}"

    # -n melepas stdin dari kanal SSH; tanpa ini sesi menggantung meski
    # prosesnya sudah dilatarbelakangi.
    opsi = ["-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=20"]
    ssh5 = ["ssh"] + opsi + [f"{a.pi5_user}@{a.pi5_host}"]
    ssh4 = ["ssh"] + opsi + [f"{a.pi4_user}@{a.pi4_host}"]

    return {
        "rid": rid, "mpd": mpd, "total": total,
        "ssh5": ssh5, "ssh4": ssh4,
        "tclog": f"tc_s{a.seed}_{a.title}.jsonl",
        # Berkas lama dihapus karena agen menulis dalam mode append; tanpa ini
        # cuplikan run sebelumnya akan ikut terbawa.
        "bersih5": ssh5 + [f"cd {a.pi5_dir} && rm -f qos_samples.csv qos_features.csv agen.log"],
        # Log tc lama WAJIB dihapus. Pada uji sebelumnya, berkas sisa run
        # terdahulu ikut tertarik dan tampak seolah jadwal berjalan normal.
        "bersih4": ssh4 + [
            f"cd {a.pi4_dir} && rm -f tc_s{a.seed}_{a.title}.jsonl tc.log; "
            f"sudo /usr/sbin/tc qdisc del dev {a.iface} root 2>/dev/null; true"],
        "agen": ssh5 + [
            f"cd {a.pi5_dir} && setsid nohup sudo /usr/bin/python3 qos_agent.py "
            f"--run-id {rid} --poll {a.poll} --window {a.window} "
            f"--duration {total} < /dev/null > agen.log 2>&1 & echo mulai"],
        "tc": ssh4 + [
            # Dipanggil lewat interpreter, BUKAN lewat shebang. Berkas yang
            # disalin dari Windows sering berakhiran CRLF sehingga baris shebang
            # menjadi "python3\r" dan exec gagal dengan "bad interpreter".
            f"cd {a.pi4_dir} && setsid nohup /usr/bin/python3 tc_continuous.py --seed {a.seed} "
            f"--iface {a.iface} --duration {a.duration + JEDA_AKHIR + TAMBAHAN} "
            f"--log tc_s{a.seed}_{a.title}.jsonl < /dev/null > tc.log 2>&1 & echo mulai"],
        # pkill DIPISAH dari perintah peluncuran: bila keduanya dalam satu
        # baris perintah, pola akan mencocokkan shell-nya sendiri.
        "stop_agen": ssh5 + ["sudo /usr/bin/pkill -f '[q]os_agent' ; true"],
        "stop_tc": ssh4 + ["/usr/bin/pkill -f '[t]c_continuous' ; true"],
        "bereskan4": ssh4 + [f"sudo /usr/sbin/tc qdisc del dev {a.iface} root 2>/dev/null; true"],
        # Durasi bg SAMA dengan harness, bukan lebih panjang. Bila lebih panjang,
        # trafik latar terus berjalan setelah pemutaran berakhir dan mencemari
        # window terakhir; pada uji pertama hal ini menghasilkan window bernilai
        # 895 Mbps yang sama sekali bukan trafik video.
        "bg": [sys.executable, "bg_traffic.py", "--server",
               f"http://{a.server_ip}:{a.port}", "--seed", str(a.seed),
               "--duration", str(a.duration), "--streams", str(a.streams),
               "--max-mbps", str(a.bg_max_mbps),
               "--log", os.path.join(a.results, f"bg_s{a.seed}_{a.title}.jsonl")],
        "harness": ["node", "harness.js", "--mpd", mpd, "--duration", str(a.duration),
                    "--run-id", rid, "--abr", a.abr, "--display", a.display,
                    "--out", os.path.join(a.results, f"{rid}_client_metadata.json")],
        "tarik_smp": ["scp", "-o", "BatchMode=yes",
                      f"{a.pi5_user}@{a.pi5_host}:{a.pi5_dir}/qos_samples.csv",
                      os.path.join(a.results, f"{rid}_qos_samples.csv")],
        "tarik_agenlog": ["scp", "-o", "BatchMode=yes",
                          f"{a.pi5_user}@{a.pi5_host}:{a.pi5_dir}/agen.log",
                          os.path.join(a.results, f"{rid}_agen.log")],
        "tarik_tclog": ["scp", "-o", "BatchMode=yes",
                        f"{a.pi4_user}@{a.pi4_host}:{a.pi4_dir}/tc_s{a.seed}_{a.title}.jsonl",
                        os.path.join(a.results, f"tc_s{a.seed}_{a.title}.jsonl")],
        "label": [sys.executable, "label_from_metadata.py",
                  os.path.join(a.results, f"{rid}_client_metadata.json"),
                  "--device", "mobile", "--window", str(a.window),
                  "--out-prefix", os.path.join(a.results, rid)],
        "align": [sys.executable, "align_qos.py",
                  os.path.join(a.results, f"{rid}_client_metadata.json"),
                  os.path.join(a.results, f"{rid}_qos_samples.csv"),
                  "--window", str(a.window),
                  "--out", os.path.join(a.results, f"{rid}_qos_aligned.csv")],
    }


def self_test():
    class A:
        seed = 3; title = "RedBullPlayStreets"; abr = "bola"; duration = 300
        poll = 0.1; window = 10.0; streams = 1; display = "1280x720"
        bg_max_mbps = 1.5
        server_ip = "192.168.50.10"; port = 8080; https = False; https_port = 8443
        iface = "eth0"; results = "hasil_v4"
        pi5_user = "hafidz"; pi5_host = "192.168.18.234"; pi5_dir = "~/x/ebpf"
        pi4_user = "yb"; pi4_host = "192.168.18.233"; pi4_dir = "~/x"
    c = bangun_perintah(A())

    assert c["rid"] == "CONT_s3_RedBullPlayStreets"
    print(f"  [OK] run_id: {c['rid']}")
    assert "RedBull_4_simple" in c["mpd"], c["mpd"]
    print("  [OK] nama MPD tak seragam ditangani (RedBull_4_simple, bukan RedBull_4s)")

    assert c["total"] == 300 + JEDA_MUKA + JEDA_AKHIR + TAMBAHAN
    assert f"--duration {c['total']}" in " ".join(c["agen"])
    assert f"--duration {300 + JEDA_AKHIR + TAMBAHAN}" in " ".join(c["tc"])
    print(f"  [OK] agen {c['total']}s dan tc {300+JEDA_AKHIR+TAMBAHAN}s, "
          f"keduanya melampaui pemutaran 300s + jeda akhir {JEDA_AKHIR}s")

    assert "rm -f qos_samples.csv" in " ".join(c["bersih5"])
    print("  [OK] berkas cuplikan lama dihapus (agen memakai mode append)")

    for kunci in ("stop_agen", "stop_tc"):
        t = " ".join(c[kunci])
        assert "[q]os_agent" in t or "[t]c_continuous" in t, t
        assert "nohup" not in t, "pkill tidak boleh sekalimat dgn peluncuran"
    print("  [OK] pkill memakai pola bracket dan terpisah dari peluncuran")

    assert "/usr/sbin/tc" in " ".join(c["bersih4"])
    assert "/usr/bin/python3" in " ".join(c["agen"])
    print("  [OK] biner dipanggil dgn path absolut (PATH SSH tanpa /usr/sbin)")

    assert "/usr/bin/python3 tc_continuous.py" in " ".join(c["tc"]), c["tc"]
    print("  [OK] tc dipanggil lewat interpreter, imun thd shebang berakhiran CRLF")
    assert "rm -f tc_s3_RedBullPlayStreets.jsonl" in " ".join(c["bersih4"])
    print("  [OK] log tc lama dihapus agar sisa run terdahulu tidak menyesatkan")
    for k in ("agen", "tc"):
        tt = " ".join(c[k])
        assert "< /dev/null" in tt, f"{k}: stdin belum dilepas"
        assert "setsid" in tt, f"{k}: belum memakai setsid"
    assert "-n" in c["ssh5"] and "-n" in c["ssh4"]
    print("  [OK] stdin dilepas tiga lapis: ssh -n, setsid, dan < /dev/null")

    assert "--abr bola" in " ".join(c["harness"])
    print("  [OK] mode ABR diteruskan ke harness")

    A.https = True
    c2 = bangun_perintah(A())
    assert c2["mpd"].startswith("https://") and ":8443/" in c2["mpd"], c2["mpd"]
    print(f"  [OK] varian HTTPS: {c2['mpd'][:46]}...")
    # trafik latar tetap lewat HTTP polos karena hanya berperan sbg pesaing
    assert "http://" in " ".join(c2["bg"]) and "https://" not in " ".join(c2["bg"])
    print("  [OK] trafik latar tetap HTTP polos (perannya hanya sbg pesaing)")

    bgt = " ".join(c["bg"])
    assert "--max-mbps 1.5" in bgt, bgt
    assert "--duration 300" in bgt, "durasi bg harus sama dgn harness"
    print("  [OK] trafik latar dibatasi 1.5 Mbps dan berhenti bersama harness")

    assert set(MPD) >= {"BigBuckBunny", "TearsOfSteel", "Valkaama"}
    print(f"  [OK] {len(MPD)} judul terdaftar")
    print("\nSEMUA UJI LULUS")
    return True


def main():
    ap = argparse.ArgumentParser(description="Jalankan satu run koleksi v4")
    # Tidak ditandai required agar --self-test dapat dijalankan tanpa keduanya;
    # validasi dilakukan setelah pengecekan --self-test.
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--title", default=None, choices=sorted(MPD))
    ap.add_argument("--abr", default="dynamic", choices=["dynamic", "throughput", "bola"])
    ap.add_argument("--duration", type=float, default=300)
    ap.add_argument("--poll", type=float, default=0.1)
    ap.add_argument("--window", type=float, default=10.0)
    ap.add_argument("--streams", type=int, default=1)
    ap.add_argument("--bg-max-mbps", type=float, default=1.5,
                    help="batas laju trafik latar (Mbps); 0 = tanpa batas")
    ap.add_argument("--display", default="1280x720")
    ap.add_argument("--results", default="hasil_v4")
    ap.add_argument("--server-ip", default="192.168.50.10")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--https", action="store_true")
    ap.add_argument("--https-port", type=int, default=8443)
    ap.add_argument("--iface", default="eth0")
    ap.add_argument("--pi5-user", default="hafidz")
    ap.add_argument("--pi5-host", default="192.168.18.234")
    ap.add_argument("--pi5-dir", default="~/RisetPakBandung2026/ebpf")
    ap.add_argument("--pi4-user", default="yb")
    ap.add_argument("--pi4-host", default="192.168.18.233")
    ap.add_argument("--pi4-dir", default="~/RisetPakBandung2026")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        sys.exit(0 if self_test() else 1)
    if a.seed is None or a.title is None:
        ap.error("--seed dan --title wajib diberikan (kecuali dengan --self-test)")

    os.makedirs(a.results, exist_ok=True)
    c = bangun_perintah(a)

    if a.dry_run:
        print(f">> run_id {c['rid']}")
        print(f">> mpd    {c['mpd']}\n")
        for k in ("bersih5", "bersih4", "agen", "tc", "bg", "harness",
                  "stop_agen", "stop_tc", "bereskan4",
                  "tarik_smp", "tarik_agenlog", "tarik_tclog", "label", "align"):
            print(f"  [{k}]")
            print(f"    {' '.join(c[k])}\n")
        return

    t0 = time.time()
    print(f">> {c['rid']} | abr={a.abr} | {a.duration:.0f}s pemutaran")
    bg = None
    try:
        print("   bersih-bersih ...", end="", flush=True)
        sh(c["bersih5"], cek=False)
        sh(c["bersih4"], cek=False)
        print(" selesai")

        print("   agen di RPi 5 ...", end="", flush=True)
        luncurkan(c["agen"])
        print(" diluncurkan")
        print("   jadwal tc di RPi 4 ...", end="", flush=True)
        luncurkan(c["tc"])
        print(" diluncurkan")

        print(f"   jeda muka {JEDA_MUKA}s ...", end="", flush=True)
        time.sleep(JEDA_MUKA)
        print(" selesai")

        # Verifikasi keduanya benar-benar hidup. Tanpa ini, run bisa berjalan
        # lima menit lalu menghasilkan berkas kosong tanpa peringatan apa pun.
        proses5 = berjalan(c["ssh5"], "[q]os_agent")
        proses4 = berjalan(c["ssh4"], "[t]c_continuous")
        segar5, ket5 = baru_ditulis(c["ssh5"], f"{a.pi5_dir}/qos_samples.csv")
        segar4, ket4 = baru_ditulis(c["ssh4"], f"{a.pi4_dir}/{c['tclog']}")
        print(f"   verifikasi agen : proses {'ada' if proses5 else 'TIADA'}, "
              f"cuplikan {ket5}")
        print(f"   verifikasi tc   : proses {'ada' if proses4 else 'TIADA'}, "
              f"catatan {ket4}")

        def gagal(host_ssh, logpath, nama):
            r = sh(host_ssh + [f"tail -8 {logpath} 2>/dev/null || echo '(log tidak ada)'"],
                   cek=False)
            print(f"     isi {nama}:")
            for ln in (r.stdout or "(kosong)").strip().split("\n"):
                print(f"       {ln}")

        if not (proses5 and segar5):
            gagal(c["ssh5"], f"{a.pi5_dir}/agen.log", "agen.log")
            raise RuntimeError("agen tidak bekerja; run dibatalkan")
        if not (proses4 and segar4):
            gagal(c["ssh4"], f"{a.pi4_dir}/tc.log", "tc.log")
            raise RuntimeError("jadwal tc tidak bekerja; run dibatalkan")

        print("   trafik latar ...", end="", flush=True)
        bg = subprocess.Popen(c["bg"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
        print(" jalan")

        print(f"   harness {a.duration:.0f}s ...", flush=True)
        r = subprocess.run(c["harness"], capture_output=True, text=True,
                           timeout=a.duration + 240)
        for ln in (r.stdout or "").strip().split("\n")[-3:]:
            if ln.strip():
                print(f"     {ln.strip()}")
        if r.returncode != 0:
            print(f"     harness GAGAL: {(r.stderr or '')[:200]}")
    finally:
        # Beri waktu agen menutupi ekor window yang tergeser oleh stalling.
        print(f"   jeda akhir {JEDA_AKHIR}s (menutupi ekor akibat stall) ...",
              end="", flush=True)
        time.sleep(JEDA_AKHIR)
        print(" selesai")
        if bg and bg.poll() is None:
            bg.terminate()
            try:
                bg.wait(timeout=10)
            except Exception:
                bg.kill()
        print("   hentikan agen & tc ...", end="", flush=True)
        sh(c["stop_agen"], cek=False)
        sh(c["stop_tc"], cek=False)
        sh(c["bereskan4"], cek=False)
        print(" selesai")

    time.sleep(2)                       # beri waktu agen menutup berkas
    print("   tarik berkas ...", end="", flush=True)
    for k in ("tarik_smp", "tarik_agenlog", "tarik_tclog"):
        rr = sh(c[k], cek=False)
        if rr.returncode != 0:
            print(f"\n     PERINGATAN {k}: {rr.stderr.strip()[:120]}")
    print(" selesai")

    for k, nama in (("label", "label"), ("align", "selaras")):
        rr = sh(c[k], cek=False)
        tag = "OK" if rr.returncode == 0 else "GAGAL"
        ekor = (rr.stdout or rr.stderr or "").strip().split("\n")
        print(f"   {nama}: {tag}  {ekor[-1][:90] if ekor and ekor[-1] else ''}")

    print(f">> selesai dalam {time.time()-t0:.0f}s -> {a.results}/")


if __name__ == "__main__":
    main()