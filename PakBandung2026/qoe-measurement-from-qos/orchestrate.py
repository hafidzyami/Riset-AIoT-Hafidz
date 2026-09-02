#!/usr/bin/env python3
"""
orchestrate.py — jalankan seluruh matriks skenario secara otomatis (dari LAPTOP)
--------------------------------------------------------------------------------
Mengoordinasikan tiga mesin per run:
  RPi 4 (server) : pasang/lepas aturan tc NetEm
  RPi 5 (bridge) : jalankan/hentikan qos_agent.py (sensor eBPF sudah terpasang)
  Laptop (klien) : jalankan harness.js, lalu labeler + penyelaras

Satu run menghasilkan, di folder hasil:
  <run_id>_client_metadata.json   telemetri klien
  <run_id>_labels.csv             label QoE per window   (y)
  <run_id>_qos_samples.csv        cuplikan QoS mentah
  <run_id>_qos_aligned.csv        fitur QoS per window   (X)

PRASYARAT (sekali saja):
  1. Kunci SSH dari laptop ke kedua Pi (tanpa kata sandi):
       ssh-keygen -t ed25519
       ssh-copy-id yb@<ip-rpi4>          # atau salin manual isi .pub
       ssh-copy-id hafidz@<ip-rpi5>
     Uji: ssh -o BatchMode=yes yb@<ip-rpi4> echo ok
  2. sudo tanpa kata sandi untuk perintah yang dipakai.
     Di RPi 4:  sudo visudo -f /etc/sudoers.d/riset
       yb ALL=(ALL) NOPASSWD: /usr/sbin/tc
     Di RPi 5:  sudo visudo -f /etc/sudoers.d/riset
       hafidz ALL=(ALL) NOPASSWD: /usr/bin/python3, /usr/bin/pkill, /usr/bin/pgrep
     (Catatan keamanan: NOPASSWD untuk python3 setara akses root. Dapat diterima
      untuk mesin lab terisolasi; jangan dipakai di mesin produksi.)
  3. Sensor XDP sudah terpasang di RPi 5 dan konten DASH tersaji di RPi 4.

Pakai:
  python orchestrate.py --preflight              # periksa kesiapan saja
  python orchestrate.py --dry-run                # tampilkan rencana
  python orchestrate.py                          # jalankan semua (bisa dilanjutkan)
  python orchestrate.py --scenarios S2C,S5F --titles BigBuckBunny
  python orchestrate.py --merge-only             # hanya gabungkan hasil
  python orchestrate.py --self-test              # uji logika tanpa perangkat
"""
import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

# ----------------------------------------------------------------------------
# KONFIGURASI — sesuaikan di sini
# ----------------------------------------------------------------------------
RPI4 = "yb@192.168.18.233"          # server (alamat Wi-Fi, untuk SSH)
RPI5 = "hafidz@192.168.18.234"      # bridge (alamat Wi-Fi, untuk SSH)
SERVER_IP = "192.168.50.10"         # alamat server di jalur KABEL (dilewati bridge)
SERVER_PORT = 8080
SRV_IFACE = "eth0"                  # antarmuka RPi 4 menuju RPi 5
# Path ABSOLUT: shell SSH non-interaktif tidak memuat /usr/sbin ke PATH,
# sedangkan sudo memakai secure_path sendiri. Tanpa ini "tc"/"ip" tidak ketemu.
TC_BIN = "/usr/sbin/tc"
IP_BIN = "/usr/sbin/ip"
DIR5 = "~/RisetPakBandung2026/ebpf"  # folder qos_agent.py di RPi 5
DISPLAY = "1280x720"
DEVICE = "mobile"

# Matriks skenario — nilai sudah diverifikasi pada NetEm (lihat catatan kalibrasi)
SCENARIOS = {
    "S1":  None,                                          # baseline ideal
    "S2A": "tbf rate 3mbit burst 32k latency 50ms",        # -> Excellent
    "S2B": "tbf rate 700kbit burst 15k latency 50ms",      # -> Good      (7/7 konsisten)
    "S2C": "tbf rate 400kbit burst 15k latency 50ms",      # -> Degraded  (7/7 konsisten)
    "S2D": "tbf rate 150kbit burst 15k latency 50ms",      # -> Critical  (6/7)
    "S3A": "netem delay 250ms 50ms distribution normal",   # jitter
    "S3B": "netem delay 100ms 30ms 25%",                   # jitter berkorelasi
    "S4A": "netem delay 50ms",                             # validasi RTT (RQ1)
    "S4B": "netem delay 150ms",
    "S4C": "netem delay 300ms",
    "S5F": "netem delay 100ms 20ms loss 5%",               # multi-faktor
    "S5A": "netem delay 100ms loss 5%",                    # ablation
    "S5B": "netem delay 1ms 20ms loss 5%",
    "S5C": "netem delay 100ms 20ms",
}

# Nama berkas MPD berbeda antar judul (RedBull memakai pola lain)
TITLES = {
    "BigBuckBunny":       "BigBuckBunny_4s_simple_2014_05_09.mpd",
    "ElephantsDream":     "ElephantsDream_4s_simple_2014_05_09.mpd",
    "OfForestAndMen":     "OfForestAndMen_4s_simple_2014_05_09.mpd",
    "RedBullPlayStreets": "RedBull_4_simple_2014_05_09.mpd",
    "TearsOfSteel":       "TearsOfSteel_4s_simple_2014_05_09.mpd",
    "Valkaama":           "Valkaama_4s_simple_2014_05_09.mpd",
    "TheSwissAccount":    "TheSwissAccount_4s_simple_2014_05_09.mpd",
}

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=10",
            # keepalive: putuskan cepat bila sesi menggantung, jangan tunggu timeout penuh
            "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3"]


# ----------------------------------------------------------------------------
# Utilitas
# ----------------------------------------------------------------------------
class _Hasil:
    """Pengganti CompletedProcess saat perintah gagal/menggantung."""
    def __init__(self, rc, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _jalankan(argv, timeout, tries, jeda=4):
    """Jalankan perintah dengan retry. TIDAK PERNAH melempar exception.

    Gangguan SSH sesaat (Wi-Fi, beban Pi) tidak boleh mematikan orkestrator
    di tengah pekerjaan 10 jam; kegagalan dikembalikan sebagai return code.
    """
    last = None
    for k in range(tries):
        try:
            return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            last = _Hasil(124, "", f"timeout setelah {timeout}s")
        except Exception as e:
            last = _Hasil(125, "", f"{type(e).__name__}: {e}")
        if k < tries - 1:
            time.sleep(jeda)
    return last


def ssh(host, cmd, timeout=60, tries=3):
    return _jalankan(["ssh", *SSH_OPTS, host, cmd], timeout, tries)


def scp_from(host, remote, local, timeout=120, tries=3):
    return _jalankan(["scp", *SSH_OPTS, f"{host}:{remote}", local], timeout, tries)


def mpd_url(title):
    return f"http://{SERVER_IP}:{SERVER_PORT}/{title}/{TITLES[title]}"


def build_plan(scenarios, titles, reps=1):
    """Daftar (run_id, skenario, judul). Judul mengisi slot ulangan."""
    plan = []
    for sc in scenarios:
        for t in titles:
            for r in range(1, reps + 1):
                plan.append((f"{sc}_{t}_rep{r}", sc, t))
    return plan


def already_done(results, run_id):
    """Sudah selesai bila label DAN fitur selaras ada dan tidak kosong."""
    for suf in ("_labels.csv", "_qos_aligned.csv"):
        p = os.path.join(results, run_id + suf)
        if not (os.path.exists(p) and os.path.getsize(p) > 0):
            return False
    return True


# ----------------------------------------------------------------------------
# Kendali perangkat jarak jauh
# ----------------------------------------------------------------------------
def tc_apply(spec):
    """Pasang aturan tc di RPi 4. spec None = bersih (baseline)."""
    base = f"sudo -n {TC_BIN} qdisc del dev {SRV_IFACE} root 2>/dev/null || true"
    if spec is None:
        return ssh(RPI4, f"{base}; sudo -n {TC_BIN} qdisc show dev {SRV_IFACE}")
    return ssh(RPI4, f"{base}; sudo -n {TC_BIN} qdisc add dev {SRV_IFACE} root {spec} "
                     f"&& sudo -n {TC_BIN} qdisc show dev {SRV_IFACE}")


def agent_start(run_id):
    """Jalankan agen di RPi 5. Dipisah DUA panggilan SSH, dan itu wajib.

    pkill -f mencocokkan pola terhadap SELURUH baris perintah tiap proses.
    Perintah yang menyalakan agen mau tidak mau memuat teks "qos_agent.py",
    sehingga bila pkill berada di perintah yang sama, shell SSH itu akan
    membunuh dirinya sendiri sebelum sempat menjalankan apa pun (gejalanya
    return code -1 dengan keluaran kosong, sangat menyesatkan).

    Panggilan 1 hanya memuat pola bracket, jadi aman memakai pkill.
    Panggilan 2 memuat "qos_agent.py" tetapi tidak memakai pkill/pgrep sama
    sekali; keberhasilan diverifikasi lewat isi agen.log, bukan daftar proses.
    """
    ssh(RPI5, "sudo -n pkill -f '[q]os_agent' >/dev/null 2>&1; sleep 1; echo ok")

    cmd = (f"cd {DIR5} && rm -f qos_samples.csv qos_features.csv agen.log; "
           f"nohup sudo -n python3 -u qos_agent.py --run-id {run_id} "
           f"> agen.log 2>&1 < /dev/null & "
           f"sleep 3; head -c 400 agen.log")
    r = ssh(RPI5, cmd)

    banner = r.stdout.strip()
    if f"run_id={run_id}" not in banner:
        return "", r
    peta = next((l.strip() for l in banner.splitlines() if "map" in l and "membaca" in l), "")
    return (peta or "berjalan"), r


def agent_stop():
    """Hentikan agen dengan SIGTERM (agen menyimpan CSV & window parsial)."""
    return ssh(RPI5, "sudo -n pkill -f '[q]os_agent' >/dev/null 2>&1 || true; "
                     "sleep 3; pgrep -f '[q]os_agent' >/dev/null && echo MASIH_JALAN || echo berhenti")


# ----------------------------------------------------------------------------
# Pemeriksaan pra-terbang
# ----------------------------------------------------------------------------
def preflight(titles):
    ok = True

    for label, host in (("RPi 4 (server)", RPI4), ("RPi 5 (bridge)", RPI5)):
        r = ssh(host, "echo ok")
        good = r.returncode == 0 and "ok" in r.stdout
        print(f"  [{'OK' if good else 'GAGAL'}] SSH ke {label} ({host})")
        if not good:
            print(f"        rc={r.returncode} {r.stderr.strip()[:160]}")
            if r.returncode == 124:
                print("        -> sesi menggantung. Cek beban RPi (uptime), dan matikan")
                print("           power-save Wi-Fi: sudo iw dev wlan0 set power_save off")
            print(f"        -> siapkan kunci SSH; uji: ssh -o BatchMode=yes {host} echo ok")
            ok = False

    r = ssh(RPI4, f"test -x {TC_BIN} && echo ada")
    good = "ada" in r.stdout
    print(f"  [{'OK' if good else 'GAGAL'}] biner tc ada di {TC_BIN} (RPi 4)")
    if not good:
        print("        -> cek lokasinya: ssh <rpi4> 'command -v tc', lalu sesuaikan TC_BIN")
        ok = False

    r = ssh(RPI4, f"sudo -n {TC_BIN} qdisc show dev {SRV_IFACE}")
    good = r.returncode == 0
    print(f"  [{'OK' if good else 'GAGAL'}] sudo tc tanpa kata sandi di RPi 4")
    if not good:
        print(f"        -> tambahkan di /etc/sudoers.d/riset: "
              f"<user> ALL=(ALL) NOPASSWD: {TC_BIN}")
        ok = False

    r = ssh(RPI5, "sudo -n python3 -c 'print(1)'")
    good = r.returncode == 0 and "1" in r.stdout
    print(f"  [{'OK' if good else 'GAGAL'}] sudo python3 tanpa kata sandi di RPi 5")
    if not good:
        print("        -> tambahkan: <user> ALL=(ALL) NOPASSWD: /usr/bin/python3, "
              "/usr/bin/pkill, /usr/bin/pgrep")
        ok = False

    r = ssh(RPI5, f"test -f {DIR5}/qos_agent.py && echo ada")
    good = "ada" in r.stdout
    print(f"  [{'OK' if good else 'GAGAL'}] qos_agent.py ada di {DIR5} (RPi 5)")
    ok = ok and good

    r = ssh(RPI5, f"{IP_BIN} -d link show eth0 | grep -q 'prog/xdp' && echo terpasang")
    good = "terpasang" in r.stdout
    print(f"  [{'OK' if good else 'GAGAL'}] sensor XDP terpasang di eth0 (RPi 5)")
    if not good:
        print(f"        -> di RPi 5: sudo {IP_BIN} link set dev eth0 xdpgeneric "
              f"obj qos_sensor.bpf.o sec xdp")
        ok = False

    for f in ("harness.js", "label_from_metadata.py", "align_qos.py"):
        good = os.path.exists(f)
        print(f"  [{'OK' if good else 'GAGAL'}] {f} ada di folder ini (laptop)")
        ok = ok and good

    for t in titles:
        url = mpd_url(t)
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                good = resp.status == 200
        except Exception as e:
            good = False
        print(f"  [{'OK' if good else 'GAGAL'}] MPD terjangkau: {t}")
        if not good:
            print(f"        {url}")
            ok = False

    return ok


# ----------------------------------------------------------------------------
# Satu run
# ----------------------------------------------------------------------------
def do_run(run_id, sc, title, dur, results, log):
    def catat(m):
        print(f"    {m}")
        log.write(f"{time.strftime('%H:%M:%S')} {run_id} {m}\n"); log.flush()

    spec = SCENARIOS[sc]
    r = tc_apply(spec)
    if r.returncode != 0:
        catat(f"GAGAL pasang tc: {r.stderr.strip()[:120]}")
        return False
    catat(f"tc: {spec or '(bersih)'}")

    status, r = agent_start(run_id)
    if not status:
        catat(f"GAGAL jalankan agen (rc={r.returncode}) "
              f"out={r.stdout.strip()[:100]!r} err={r.stderr.strip()[:150]!r}")
        rr = ssh(RPI5, f"tail -5 {DIR5}/agen.log 2>/dev/null")
        if rr.stdout.strip():
            catat(f"  agen.log: {rr.stdout.strip()[:250]}")
        return False
    catat(f"agen jalan | {status}")

    try:
        h = subprocess.run(
            ["node", "harness.js", "--mpd", mpd_url(title), "--duration", str(dur),
             "--run-id", run_id, "--display", DISPLAY],
            capture_output=True, text=True, timeout=dur + 180)
        ringkas = [l for l in h.stdout.splitlines() if l.startswith("OK:")]
        catat(f"harness: {ringkas[0] if ringkas else 'tidak ada baris OK'}")
        if h.returncode != 0:
            catat(f"harness GAGAL: {h.stderr.strip()[:160]}")
            return False
    except subprocess.TimeoutExpired:
        catat("harness TIMEOUT")
        return False
    finally:
        agent_stop()

    if not os.path.exists("client_metadata.json"):
        catat("client_metadata.json tidak dibuat")
        return False

    smp = os.path.join(results, f"{run_id}_qos_samples.csv")
    r = scp_from(RPI5, f"{DIR5}/qos_samples.csv", smp)
    if r.returncode != 0 or not os.path.exists(smp):
        catat(f"GAGAL scp cuplikan: {r.stderr.strip()[:120]}")
        return False

    meta = os.path.join(results, f"{run_id}_client_metadata.json")
    shutil.copy("client_metadata.json", meta)

    p = subprocess.run([sys.executable, "label_from_metadata.py", meta,
                        "--device", DEVICE, "--out-prefix",
                        os.path.join(results, run_id)],
                       capture_output=True, text=True)
    if p.returncode != 0:
        catat(f"labeler GAGAL: {p.stderr.strip()[:160]}")
        return False
    dist = [l for l in p.stdout.splitlines() if "distribusi" in l]
    catat(f"label: {dist[0].split(':',1)[1].strip() if dist else 'ok'}")

    p = subprocess.run([sys.executable, "align_qos.py", meta, smp,
                        "--out", os.path.join(results, f"{run_id}_qos_aligned.csv")],
                       capture_output=True, text=True)
    if p.returncode != 0:
        catat(f"penyelaras GAGAL: {p.stderr.strip()[:160]}")
        return False
    gs = [l for l in p.stdout.splitlines() if "pergeseran" in l]
    catat(f"selaras: {gs[0].strip() if gs else 'ok'}")
    return True


# ----------------------------------------------------------------------------
# Uji mandiri (logika murni)
# ----------------------------------------------------------------------------
def self_test(tmp="_ortest"):
    plan = build_plan(["S1", "S2C"], ["BigBuckBunny", "Valkaama"])
    assert len(plan) == 4, plan
    assert plan[0][0] == "S1_BigBuckBunny_rep1", plan[0]
    print(f"  [OK] rencana: {len(plan)} run untuk 2 skenario x 2 judul")

    penuh = build_plan(list(SCENARIOS), list(TITLES))
    assert len(penuh) == len(SCENARIOS) * len(TITLES) == 98, len(penuh)
    print(f"  [OK] matriks penuh: {len(SCENARIOS)} skenario x {len(TITLES)} judul "
          f"= {len(penuh)} run")

    ids = [p[0] for p in penuh]
    assert len(ids) == len(set(ids)), "run_id harus unik (mencegah berkas tertimpa)"
    print("  [OK] semua run_id unik")

    assert mpd_url("RedBullPlayStreets").endswith("RedBull_4_simple_2014_05_09.mpd")
    assert "/RedBullPlayStreets/" in mpd_url("RedBullPlayStreets")
    print("  [OK] URL MPD benar termasuk penamaan khusus RedBull")

    os.makedirs(tmp, exist_ok=True)
    rid = "S1_BigBuckBunny_rep1"
    assert not already_done(tmp, rid)
    open(os.path.join(tmp, rid + "_labels.csv"), "w").write("x\n")
    assert not already_done(tmp, rid), "butuh KEDUA berkas"
    open(os.path.join(tmp, rid + "_qos_aligned.csv"), "w").write("x\n")
    assert already_done(tmp, rid)
    open(os.path.join(tmp, rid + "_qos_aligned.csv"), "w").close()   # kosong
    assert not already_done(tmp, rid), "berkas kosong tidak dianggap selesai"
    shutil.rmtree(tmp)
    print("  [OK] logika lanjut-otomatis (resume), termasuk tolak berkas kosong")

    est = len(penuh) * 300 / 3600
    print(f"  [OK] estimasi waktu: {est:.1f} jam eksekusi murni "
          f"(+overhead ~30% = {est*1.3:.1f} jam)")

    print("\nSEMUA UJI LULUS")
    return True


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Orkestrator pengambilan data QoS/QoE")
    ap.add_argument("--results", default="hasil", help="folder keluaran (default: hasil)")
    ap.add_argument("--duration", type=int, default=300, help="detik per run (default 300)")
    ap.add_argument("--reps", type=int, default=1, help="ulangan per (skenario,judul)")
    ap.add_argument("--scenarios", default=None, help="mis. S2C,S5F (default: semua)")
    ap.add_argument("--titles", default=None, help="mis. BigBuckBunny (default: semua)")
    ap.add_argument("--preflight", action="store_true", help="periksa kesiapan lalu keluar")
    ap.add_argument("--dry-run", action="store_true", help="tampilkan rencana lalu keluar")
    ap.add_argument("--merge-only", action="store_true", help="hanya gabungkan hasil")
    ap.add_argument("--min-samples", type=int, default=0,
                    help="saring window dgn cuplikan < N saat menggabungkan (default 0 = "
                         "kumpulkan semua; penyaringan sebaiknya dilakukan saat analisis, "
                         "bukan dikunci di tahap pengambilan data)")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        sys.exit(0 if self_test() else 1)

    scs = [s.strip() for s in a.scenarios.split(",")] if a.scenarios else list(SCENARIOS)
    tts = [t.strip() for t in a.titles.split(",")] if a.titles else list(TITLES)
    for s in scs:
        if s not in SCENARIOS:
            sys.exit(f"skenario tidak dikenal: {s} (pilihan: {', '.join(SCENARIOS)})")
    for t in tts:
        if t not in TITLES:
            sys.exit(f"judul tidak dikenal: {t} (pilihan: {', '.join(TITLES)})")

    os.makedirs(a.results, exist_ok=True)

    merge_args = [sys.executable, "merge_dataset.py", "--dir", a.results,
                  "--out", os.path.join(a.results, "dataset.csv")]
    if a.min_samples:
        merge_args += ["--min-samples", str(a.min_samples)]

    if a.merge_only:
        subprocess.run(merge_args)
        return

    if a.preflight:
        print("== Pra-terbang ==")
        ok = preflight(tts)
        print("\nPra-terbang " + ("lolos. Siap dijalankan." if ok else "GAGAL. Perbaiki dulu."))
        sys.exit(0 if ok else 1)

    plan = build_plan(scs, tts, a.reps)
    sisa = [p for p in plan if not already_done(a.results, p[0])]
    jam = len(sisa) * a.duration / 3600

    print(f"Rencana: {len(plan)} run ({len(scs)} skenario x {len(tts)} judul x {a.reps})")
    print(f"  sudah selesai : {len(plan) - len(sisa)}")
    print(f"  akan dijalankan: {len(sisa)}  (~{jam:.1f} jam murni, ~{jam*1.3:.1f} jam total)")

    if a.dry_run:
        for rid, sc, t in sisa[:15]:
            print(f"    {rid:<38} tc: {SCENARIOS[sc] or '(bersih)'}")
        if len(sisa) > 15:
            print(f"    ... dan {len(sisa)-15} lagi")
        return

    print("\n== Pra-terbang ==")
    if not preflight(tts):
        sys.exit("\nPra-terbang GAGAL. Perbaiki dulu; tidak ada run yang dijalankan.")
    print("Pra-terbang lolos.\n")

    if not sisa:
        print("Semua run sudah selesai. Menggabungkan...")
    berhasil = gagal = 0
    t0 = time.time()
    with open(os.path.join(a.results, "orchestrate.log"), "a", encoding="utf-8") as log:
        log.write(f"\n=== mulai {time.strftime('%Y-%m-%d %H:%M:%S')} "
                  f"({len(sisa)} run) ===\n")
        for i, (rid, sc, t) in enumerate(sisa, 1):
            lewat = time.time() - t0
            sisa_jam = (lewat / max(i - 1, 1)) * (len(sisa) - i + 1) / 3600 if i > 1 else jam
            print(f"[{i}/{len(sisa)}] {rid}   (perkiraan sisa {sisa_jam:.1f} jam)")
            try:
                if do_run(rid, sc, t, a.duration, a.results, log):
                    berhasil += 1
                else:
                    gagal += 1
                    print("    -> DILEWATI (lanjut ke run berikutnya)")
            except KeyboardInterrupt:
                print("\nDihentikan pengguna. Jalankan ulang untuk melanjutkan.")
                break
            except Exception as e:
                gagal += 1
                print(f"    -> ERROR: {type(e).__name__}: {e}")

    tc_apply(None)
    print(f"\nSelesai: {berhasil} berhasil, {gagal} gagal, "
          f"{(time.time()-t0)/3600:.1f} jam. tc di server dibersihkan.")

    if berhasil:
        print("\n== Menggabungkan dataset ==")
        subprocess.run(merge_args)


if __name__ == "__main__":
    main()