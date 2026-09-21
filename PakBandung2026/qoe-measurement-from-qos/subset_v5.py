import csv
J = {"BigBuckBunny", "Valkaama", "TearsOfSteel", "RedBullPlayStreets"}
M = {"bola", "dynamic"}
src = "hasil_v5/dataset_ms.csv"
out = "hasil_v5/dataset_ms_subset.csv"
d = [r for r in csv.DictReader(open(src, encoding="utf-8"))
     if r["run_id"].split("_", 3)[3] in J and r["run_id"].split("_")[2] in M]
with open(out, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(d[0].keys()))
    w.writeheader()
    w.writerows(d)
runs = {r["run_id"] for r in d}
print(len(d), "window dari", len(runs), "run")
print("judul:", sorted({r.split("_", 3)[3] for r in runs}))
print("mode :", sorted({r.split("_")[2] for r in runs}))
