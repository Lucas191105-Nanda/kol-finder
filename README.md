# IGI Academy — KOL Finder v3.6

Tool untuk mencari kreator (KOL) personal bertema Jepang dari **TikTok dan Instagram secara bersamaan**,
memverifikasi akun Instagram-nya, membuang akun institusi (LPK, agency, dll.), lalu meranking prospek
dan menyiapkan DM. Pencarian memakai [SerpApi](https://serpapi.com).

> Tool ini **tidak mengirim DM otomatis**. Kamu tetap menekan tombol Send sendiri.

## Isi folder

| File | Fungsi |
|---|---|
| `app.py` | Aplikasi Streamlit utama |
| `run_app.py` / `run_app.bat` | Launcher (cek/install dependency, lalu jalankan app) |
| `requirements.txt` | Daftar library Python |
| `kol_history.csv` | Dibuat otomatis saat klik "Tandai Sudah DM" |
| `serp_cache.sqlite` | Cache hasil SerpApi, dibuat otomatis (aman dihapus kapan saja) |

## Cara menjalankan

Syarat: Python 3.9+ dan API key SerpApi.

- **Windows:** double-click `run_app.bat`
- **Terminal:** `python run_app.py`
- **Manual:** `pip install -r requirements.txt` lalu `streamlit run app.py`

Browser terbuka di `http://localhost:8501`.

## Cara kerja v3.6 (hemat kredit, hasil tidak hilang)

Pencarian berjalan **bertahap per keyword**, bukan sekaligus di akhir:

```
untuk tiap keyword:
   cari di TikTok + Instagram (paralel)
   -> ambil IG dari bio TikTok (gratis)
   -> verifikasi profil IG yang paling menjanjikan (1 kredit/akun)
   -> hasil langsung tampil di layar
berhenti kalau target tercapai, batas kredit habis, atau kredit SerpApi habis
```

- **Hasil muncul langsung** di tabel selagi pencarian berjalan, dan **tetap tersimpan** kalau kredit habis,
  terjadi error, atau kamu menekan Stop. Tidak ada lagi hasil yang hilang di akhir.
- **Batas kredit per run** (sidebar, default 40): aplikasi berhenti otomatis di angka ini. Sisa kredit
  akun dicek dulu di awal.
- **Cache:** pencarian yang sama (keyword, halaman, profil IG) tidak dibayar dua kali selama 14 hari.
  Menjalankan ulang berarti bagian yang sudah pernah dicari gratis, lalu lanjut ke yang baru.
- **Hemat kredit sebelum verifikasi:** akun IG yang followers-nya terbaca di snippet dan jelas di luar range
  tidak diverifikasi. Yang diverifikasi duluan adalah yang sinyalnya paling kuat (ketemu di TikTok dan IG,
  atau snippet-nya paling relevan).

## Cara pakai

1. Isi **SerpAPI API Key**, klik **Cek API** untuk melihat sisa kredit.
2. (Opsional) Upload database KOL lama (XLSX/CSV) agar akun yang sudah pernah dihubungi tidak muncul lagi.
3. Pilih **Sumber discovery**: `TikTok + Instagram`, `TikTok saja`, atau `Instagram saja`.
4. Untuk TikTok: biarkan **hanya yang IG-nya tertulis di bio** dan **baca bio dari halaman TikTok** aktif.
   Mematikan opsi pertama membuat creator tanpa IG di bio dicari manual lewat SerpApi (boros kredit).
5. Atur range followers, target hasil, **batas kredit per run**, dan paket keyword.
6. Klik **Cari Creator**, lalu lihat hasil, download XLSX, atau siapkan DM.
7. Klik **Tandai Sudah DM** agar akun itu otomatis diblokir di pencarian berikutnya.

## Tips kalau kredit sedikit

- Mulai dengan batas 20–40 kredit dan **Halaman per keyword = 1**.
- Pakai `TikTok saja` atau `Instagram saja` kalau ingin lebih hemat.
- Jalankan ulang beberapa kali: yang sudah dicari diambil dari cache, jadi tiap run hanya membayar bagian baru.
- Di log, lihat baris `gugur:` untuk tahu kenapa kandidat tidak lolos.

## Troubleshooting

- **Log penuh "hasn't returned any results":** itu hasil pencarian kosong dari Bing. Pakai mesin `Auto (Bing → Google)`
  dan keyword yang lebih sederhana.
- **Berhenti dengan "kredit SerpApi habis":** hasil yang sudah ada tetap tampil dan bisa di-download.
- **Rate limit:** turunkan `WORKERS` di bagian atas `app.py` (default 4).
- **`python` tidak dikenali di Windows:** install Python dari python.org dan centang *Add to PATH*.
