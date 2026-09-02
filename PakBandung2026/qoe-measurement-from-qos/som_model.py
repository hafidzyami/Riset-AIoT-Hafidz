#!/usr/bin/env python3
"""
som_model.py — SOMClassifier sebagai modul terpisah
----------------------------------------------------
Diletakkan di modul sendiri (bukan di dalam skrip) agar model yang disimpan
dengan joblib bisa dimuat kembali oleh skrip lain. Kalau kelas didefinisikan di
dalam __main__, pickle akan menyimpan rujukan ke __main__ dan gagal dimuat di
proses lain (mis. saat benchmark di Raspberry Pi).

Modul ini harus ADA di samping berkas .joblib saat model dimuat.
"""
from collections import Counter

import numpy as np
from sklearn.base import BaseEstimator, ClassifierMixin


class SOMClassifier(ClassifierMixin, BaseEstimator):
    """Self-Organizing Map dipakai sebagai pengklasifikasi.

    SOM aslinya metode tak-terawasi; di sini setiap node diberi label lewat suara
    terbanyak dari sampel latih yang memetakan ke node tersebut. Dipertahankan
    sebagai titik pembanding "model paling ringan": bobotnya hanya
    grid x grid x n_fitur bilangan float, dan inferensinya sekadar mencari node
    terdekat.
    """

    def __init__(self, grid=10, sigma=1.0, lr=0.5, iterasi=5000, seed=0):
        self.grid = grid
        self.sigma = sigma
        self.lr = lr
        self.iterasi = iterasi
        self.seed = seed

    def fit(self, X, y):
        from minisom import MiniSom
        X = np.asarray(X, dtype=float)
        self.som_ = MiniSom(self.grid, self.grid, X.shape[1], sigma=self.sigma,
                            learning_rate=self.lr, random_seed=self.seed)
        self.som_.random_weights_init(X)
        self.som_.train_random(X, self.iterasi)
        isi = {}
        for xi, yi in zip(X, y):
            isi.setdefault(self.som_.winner(xi), Counter())[yi] += 1
        self.peta_ = {k: c.most_common(1)[0][0] for k, c in isi.items()}
        self.default_ = Counter(y).most_common(1)[0][0]   # utk node tanpa sampel
        self.classes_ = np.unique(y)
        return self

    def predict(self, X):
        X = np.asarray(X, dtype=float)
        return np.array([self.peta_.get(self.som_.winner(x), self.default_) for x in X])

    def __sklearn_is_fitted__(self):
        return hasattr(self, "som_")