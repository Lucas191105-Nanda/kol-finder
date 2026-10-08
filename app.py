from __future__ import annotations

import hashlib
import io
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import requests
import streamlit as st

APP_TITLE = "IGI Academy — KOL Finder v5 (Apify · Study Jepang Batch 2)"
HERE = Path(__file__).parent if "__file__" in globals() else Path(".")
HISTORY_PATH = HERE / "kol_history.csv"
NICHE_PATH = HERE / "niches.json"
DATABASE_PATH = HERE / "kol_database.csv"
DM_PATH = HERE / "dm_templates.json"
CACHE_PATH = HERE / "igi_profile_cache.json"
CACHE_TTL = 30 * 24 * 3600
BRIEF_LINK = "https://drive.google.com/file/d/1yA09-zMqxLmrg7ZKBauN9uC6iCVvo94H/view"
APIFY_BASE = "https://api.apify.com/v2"
DEFAULT_ACTOR = "apify/instagram-scraper"
COST_PER_RESULT = 0.0027  # perkiraan kasar USD per hasil (cek harga terbaru di Apify)

# ---------------------------------------------------------------- niche (kata biasa = cari profil, diawali # = cari hashtag)
QUERY_PACKS: dict[str, list[str]] = {
    "Pelajar SMA": ["anak sma", "siswa sma", "pelajar sma", "kelas 12", "content creator pelajar",
                    "#siswasma", "#anaksma", "#pelajarsma", "#sman"],
    "Pelajar SMP": ["anak smp", "siswa smp", "pelajar smp", "kelas 9", "#anaksmp", "#siswasmp", "#pelajarsmp"],
    "Duta / Prestasi": ["duta bahasa", "duta pelajar", "duta genre", "duta sekolah", "siswa berprestasi",
                        "finalis duta", "#dutabahasa", "#dutapelajar", "#dutagenre", "#siswaberprestasi"],
    "Organisasi / Public Speaking": ["ketua osis", "osis sma", "mc pelajar", "public speaking pelajar",
                                     "#osis", "#publicspeaking", "#mcpelajar"],
    "Study / Edukasi": ["study vlog", "study with me indonesia", "tips belajar sma", "info kuliah",
                        "#studywithme", "#studyvlog", "#tipsbelajar", "#infokuliah", "#utbk"],
    "Jepang / Bahasa Asing": ["belajar bahasa jepang", "nihongo indonesia", "kuliah di jepang",
                              "#belajarbahasajepang", "#nihongo", "#kuliahdijepang", "#studyabroad"],
    "Kreatif (Foto / Video / Seni)": ["videografer pelajar", "fotografer sma", "editor video pelajar",
                                      "#videografipelajar", "#fotografisma"],
    "Lifestyle Remaja": ["school vlog", "daily vlog sma", "#schoolvlog", "#dailyvlog", "#grwmsekolah"],
}

HARD_NEGATIVE = {
    "lpk", "lembaga pelatihan", "agency", "agensi", "recruitment", "rekrutmen",
    "penyalur", "lowongan", "loker", "konsultan", "consultant", "jasa visa",
    "travel agent", "tour & travel", "bimbel", "bimbingan belajar", "les privat", "kursus",
    "sekolah bahasa", "language school", "official account", "info beasiswa", "info loker",
    "jasa joki", "olshop", "open order",
}
INSTITUTION_USERNAME = re.compile(r"^(sman|smkn|smpn|smas|mtsn|man)\d*[._]|^osis[._]|^(official|info)[._]", re.I)
SOFT_NEGATIVE = {"travel": -5, "tour": -4, "jual": -10, "promo": -6, "endorse": -4}

POSITIVE = {
    "duta bahasa": 30, "duta berprestasi": 30, "duta pelajar": 28, "duta sekolah": 28,
    "duta genre": 24, "finalis duta": 26, "duta": 18, "siswa berprestasi": 24,
    "pelajar berprestasi": 24, "juara": 12, "olimpiade": 14, "paskibra": 12, "mpk": 8, "osis": 12,
    "kuliah di jepang": 26, "studi di jepang": 26, "belajar bahasa jepang": 24, "bahasa jepang": 18,
    "nihongo": 16, "jepang": 10, "japan": 8, "kuliah": 8, "beasiswa": 8, "study abroad": 10,
    "persiapan kuliah": 14, "info kuliah": 12, "bingung kuliah": 14,
    "student creator": 22, "content creator": 16, "study vlog": 18, "study with me": 16,
    "public speaking": 14, "tips belajar": 12, "creator": 10, "vlog": 9, "mc": 6, "pengalaman": 6,
}
PERSONAL_SIGNALS = {
    "aku": 10, "saya": 10, "gue": 8, "gw": 8, "my life": 9, "vlog": 7,
    "siswa": 9, "pelajar": 9, "mahasiswa": 9, "student": 9, "sman": 8,
    "smk": 8, "smp": 8, "alumni": 7, "kelas": 5,
}
TOPIC_SIGNALS = ("duta", "pelajar", "siswa", "sma", "smk", "smp", "student", "osis", "kelas",
                 "prestasi", "mahasiswa", "jepang", "japan", "nihongo")
DUTA_TERMS = ("duta", "finalis", "paskibra", "osis", "mpk", "berprestasi", "juara", "olimpiade")
STOPWORDS = {"indonesia", "creator", "content", "sma", "smk", "smp", "pelajar", "siswa", "anak", "vlog", "kelas"}
DYNAMIC_TERMS: set[str] = set()
LEVELS = ["SMP", "SMA/SMK", "Mahasiswa/Alumni", "Tidak jelas"]
STATS: dict[str, int] = {}


@dataclass
class Candidate:
    username: str
    url: str
    snippet: str = ""
    full_name: str = ""
    biography: str = ""
    followers: int | None = None
    is_private: bool | None = None
    is_business: bool | None = None
    category: str = ""
    level: str = "Tidak jelas"
    contact: str = ""
    engagement: float = 0.0
    prospect: int = 0
    creator: int = 0
    risk: int = 0
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sources: set[str] = field(default_factory=set)
    status: str = "Terverifikasi"


# ---------------------------------------------------------------- helpers
def clean_text(*values: object) -> str:
    return " ".join(str(v).strip() for v in values if v is not None and str(v).strip() and str(v) != "nan")


def has(term: str, hay: str) -> bool:
    tail = r"(?![a-z0-9])" if len(term) <= 3 else ""
    return bool(re.search(r"(?<![a-z0-9])" + re.escape(term) + tail, hay))


def normalize_handle(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    m = re.search(r"instagram\.com/(?:@)?([A-Za-z0-9._]{2,30})", text, re.I)
    if m:
        text = m.group(1)
    text = text.lstrip("@").split("/")[-1].split()[0].rstrip(".,;:") if text else ""
    return text.lower() if re.fullmatch(r"[A-Za-z0-9._]{2,30}", text) else None


def load_blacklist(uploaded: object | None) -> set[str]:
    found: set[str] = set()
    if uploaded is not None:
        data = uploaded.getvalue()
        name = str(getattr(uploaded, "name", "")).lower()
        try:
            if name.endswith(".csv"):
                frames = [pd.read_csv(io.BytesIO(data), dtype=object, header=None)]
            else:
                xls = pd.ExcelFile(io.BytesIO(data))
                frames = [pd.read_excel(xls, sheet_name=s, header=None, dtype=object) for s in xls.sheet_names]
            for df in frames:
                for value in df.to_numpy().ravel():
                    u = normalize_handle(value)
                    if u:
                        found.add(u)
        except Exception:
            pass
    if HISTORY_PATH.exists():
        try:
            for v in pd.read_csv(HISTORY_PATH, dtype=str).get("username", []):
                u = normalize_handle(v)
                if u:
                    found.add(u)
        except Exception:
            pass
    return found


def record_contacted(username: str, status: str = "Sudah dihubungi") -> None:
    row = pd.DataFrame([{"username": f"@{username}", "status": status,
                         "timestamp": pd.Timestamp.now().isoformat(timespec="seconds")}])
    try:
        if HISTORY_PATH.exists():
            row = pd.concat([pd.read_csv(HISTORY_PATH, dtype=str), row], ignore_index=True)
        row.drop_duplicates(subset=["username"], keep="last").to_csv(HISTORY_PATH, index=False)
    except Exception:
        row.to_csv(HISTORY_PATH, index=False)


def hard_excluded(text: str, username: str = "") -> bool:
    low = text.lower()
    return any(t in low for t in HARD_NEGATIVE) or bool(INSTITUTION_USERNAME.search(username))


def detect_level(text: str) -> str:
    low = text.lower()
    if re.search(r"\b(smp|mts|smpn)\b|kelas\s*(7|8|9)\b", low):
        return "SMP"
    if re.search(r"\b(sma|sman|smk|smkn|smas|man)\b|kelas\s*(10|11|12|x|xi|xii)\b", low):
        return "SMA/SMK"
    if re.search(r"\b(mahasiswa|mahasiswi|alumni|s1|ui|ugm|its|ub|unair|undip|uin)\b", low):
        return "Mahasiswa/Alumni"
    return "Tidak jelas"


def extract_contact(text: str) -> str:
    emails = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    wa = re.findall(r"(?:\+62|62|0)8[0-9][0-9\s-]{7,12}", text)
    return " | ".join(dict.fromkeys(emails + [w.strip() for w in wa]))[:200]


def needs_guardian(level: str) -> str:
    if level in ("SMP", "SMA/SMK"):
        return "YA, izin orang tua/wali"
    if level == "Tidak jelas":
        return "Cek usia dulu"
    return "Tidak"


def engagement_from_posts(posts: Any, followers: int | None) -> float:
    if not isinstance(posts, list) or not posts or not followers:
        return 0.0
    likes, cmts = [], []
    for p in posts[:12]:
        if not isinstance(p, dict):
            continue
        lk = p.get("likesCount", p.get("likes"))
        cm = p.get("commentsCount", p.get("comments"))
        if isinstance(lk, (int, float)) and lk >= 0:  # Instagram kadang menyembunyikan likes (-1)
            likes.append(float(lk))
        if isinstance(cm, (int, float)) and cm >= 0:
            cmts.append(float(cm))
    if not likes:
        return 0.0
    lr = (sum(likes) / len(likes)) / followers
    cr = (sum(cmts) / len(cmts)) / followers if cmts else 0.0
    s = 50 if lr >= 0.08 else 40 if lr >= 0.04 else 30 if lr >= 0.02 else 20 if lr >= 0.01 else 10 if lr >= 0.005 else 0
    s += 25 if cr >= 0.01 else 18 if cr >= 0.004 else 10 if cr >= 0.001 else 0
    return float(min(100, s))


def hay_of(c: Candidate) -> str:
    return clean_text(c.snippet, c.full_name, c.biography, c.category, c.username).lower()


def signals(hay: str) -> tuple[int, int, list[str]]:
    rel = per = 0
    reasons: list[str] = []
    for term, w in POSITIVE.items():
        if has(term, hay) or (" " in term and term.replace(" ", "") in hay):  # cocokkan juga bentuk #hashtag
            rel += w
            if len(reasons) < 8:
                reasons.append(term)
    for term, w in PERSONAL_SIGNALS.items():
        if has(term, hay):
            per += w
    for term in DYNAMIC_TERMS:
        if term in hay or term.replace(" ", "") in hay:
            rel += 15
            if len(reasons) < 8:
                reasons.append(term)
    return rel, per, reasons


def duta_tags(hay: str) -> str:
    return ", ".join(t for t in DUTA_TERMS if has(t, hay))


def score_candidate(c: Candidate) -> None:
    hay = hay_of(c)
    rel, per, c.reasons = signals(hay)
    risk = 0
    for term, pen in SOFT_NEGATIVE.items():
        if has(term, hay):
            risk += abs(pen)
            rel += pen
            c.warnings.append(term)
    if not any(has(k, hay) for k in TOPIC_SIGNALS) and not any(k in hay for k in DYNAMIC_TERMS):
        risk += 20
        c.warnings.append("sinyal pelajar/duta lemah")
    if per < 8:
        risk += 15
        c.warnings.append("sinyal personal lemah")
    if c.is_business:
        risk += 8
        c.warnings.append("akun professional/business")
    if c.followers is None:
        risk += 40
        c.warnings.append("followers tidak terbaca")
    if not c.contact:
        c.warnings.append("tidak ada kontak di bio")
    if c.level in ("SMP", "SMA/SMK"):
        rel += 6
    c.creator = max(0, min(100, per))
    c.risk = max(0, min(100, risk))
    prospect = rel + per // 2 + int(c.engagement * 0.2) + (6 if c.contact else 0)
    c.prospect = max(0, min(100, prospect - max(0, risk - 15)))


# ---------------------------------------------------------------- Apify client
class ApifyError(Exception):
    def __init__(self, msg: str, fatal: bool = False):
        super().__init__(msg)
        self.fatal = fatal


TERMINAL = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}


def apify_call(method: str, path: str, token: str, **kw: Any) -> Any:
    try:
        r = requests.request(method, APIFY_BASE + path, headers={"Authorization": f"Bearer {token}"},
                             timeout=(10, 90), **kw)
    except (requests.Timeout, requests.ConnectionError) as exc:
        raise ApifyError(f"Koneksi ke Apify gagal: {exc}") from exc
    if r.status_code in (401, 403):
        raise ApifyError("Token Apify tidak valid atau tidak punya akses.", fatal=True)
    if r.status_code in (402,):
        raise ApifyError("Kredit / batas pemakaian Apify habis.", fatal=True)
    if r.status_code >= 400:
        raise ApifyError(f"Apify HTTP {r.status_code}: {r.text[:200]}")
    try:
        return r.json()
    except Exception as exc:
        raise ApifyError("Balasan Apify bukan JSON.") from exc


def run_actor(token: str, actor: str, payload: dict, max_wait: int = 240, max_cost: float = 0.0,
              items_limit: int = 1000) -> list[dict]:
    """Jalankan actor, tunggu selesai (maks max_wait detik), ambil hasil. Kalau timeout: abort & ambil hasil sebagian."""
    actor_id = actor.strip().replace("/", "~")
    q = f"?maxTotalChargeUsd={max_cost}" if max_cost and max_cost > 0 else ""
    try:
        run = apify_call("POST", f"/acts/{actor_id}/runs{q}", token, json=payload)
    except ApifyError as exc:
        if q and "HTTP 400" in str(exc):  # actor tanpa dukungan batas biaya
            run = apify_call("POST", f"/acts/{actor_id}/runs", token, json=payload)
        else:
            raise
    data = run.get("data", {})
    rid, ds, status = data.get("id"), data.get("defaultDatasetId"), data.get("status", "READY")
    if not rid:
        raise ApifyError("Apify tidak mengembalikan ID run.")
    deadline = time.time() + max_wait
    info: dict = data
    while status not in TERMINAL and time.time() < deadline:
        info = apify_call("GET", f"/actor-runs/{rid}?waitForFinish=20", token).get("data", {})
        status = info.get("status", status)
        ds = info.get("defaultDatasetId", ds)
    if status not in TERMINAL:
        try:
            apify_call("POST", f"/actor-runs/{rid}/abort", token)
        except ApifyError:
            pass
    items = apify_call("GET", f"/datasets/{ds}/items?clean=true&format=json&limit={items_limit}", token)
    items = items if isinstance(items, list) else []
    if status in ("FAILED", "ABORTED") and not items:
        raise ApifyError(f"Run Apify {status}: {info.get('statusMessage', '')}")
    return [i for i in items if isinstance(i, dict)]


def g(d: dict, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if d.get(k) is not None:
            return d[k]
    return default


def profile_dict(item: dict) -> dict | None:
    """Normalisasi satu item profil dari Apify (nama field bisa sedikit berbeda antar actor)."""
    u = normalize_handle(g(item, "username", "ownerUsername"))
    if not u or item.get("error"):
        return None
    f = g(item, "followersCount", "followers", "followerCount")
    try:
        followers = int(f) if f is not None else None
    except (TypeError, ValueError):
        followers = None
    posts = g(item, "latestPosts", "posts", default=[])
    posts = posts if isinstance(posts, list) else []
    caps = " ".join(str(p.get("caption") or "")[:200] for p in posts[:6] if isinstance(p, dict))
    bio = str(g(item, "biography", "bio", default="") or "")
    contact = extract_contact(clean_text(bio, g(item, "businessEmail", "publicEmail", "email"),
                                         g(item, "businessPhoneNumber", "publicPhoneNumber")))
    return {
        "username": u,
        "full_name": str(g(item, "fullName", "ownerFullName", "full_name", default="") or ""),
        "biography": bio,
        "followers": followers,
        "is_private": g(item, "private", "isPrivate"),
        "is_business": g(item, "isBusinessAccount", "is_business_account"),
        "category": clean_text(item.get("businessCategoryName"), item.get("categoryName")),
        "engagement": engagement_from_posts(posts, followers),
        "contact": contact,
        "captions": caps[:900],
    }


# ---------------------------------------------------------------- cache
def load_cache() -> dict[str, dict]:
    try:
        if CACHE_PATH.exists():
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_cache(cache: dict[str, dict]) -> None:
    try:
        now = time.time()
        CACHE_PATH.write_text(json.dumps({k: v for k, v in cache.items() if now - v.get("ts", 0) < CACHE_TTL},
                                         ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------- niche custom
def load_custom() -> dict[str, list[str]]:
    try:
        if NICHE_PATH.exists():
            data = json.loads(NICHE_PATH.read_text(encoding="utf-8"))
            return {str(k): [str(x) for x in v] for k, v in data.items()}
    except Exception:
        pass
    return {}


def save_custom(data: dict[str, list[str]]) -> None:
    try:
        NICHE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def parse_terms(text: str) -> list[str]:
    out = []
    for raw in re.split(r"[\n,]+", text or ""):
        t = raw.strip().strip('"').strip()
        if t:
            out.append(t)
    return list(dict.fromkeys(out))


def split_terms(terms: list[str]) -> tuple[list[str], list[str]]:
    kws, tags = [], []
    for t in terms:
        t = t.strip().strip('"').strip()
        if not t:
            continue
        if t.startswith("#"):
            tag = re.sub(r"[^A-Za-z0-9_]", "", t.lstrip("#").lower())
            if tag and tag not in tags:
                tags.append(tag)
        elif t.lower() not in [k.lower() for k in kws]:
            kws.append(t)
    return kws, tags


def interleave(lists: list[list[str]]) -> list[str]:
    out, seen = [], set()
    for i in range(max((len(x) for x in lists), default=0)):
        for lst in lists:
            if i < len(lst) and lst[i].lower() not in seen:
                seen.add(lst[i].lower())
                out.append(lst[i])
    return out


def dynamic_terms(terms: list[str]) -> set[str]:
    out = set()
    for t in terms:
        k = t.strip().strip('"').lstrip("#").lower()
        if len(k) >= 3 and k not in STOPWORDS:
            out.add(k)
    return out


# ---------------------------------------------------------------- discovery
def reject_reason(c: Candidate, min_f, max_f, levels, require_contact) -> str | None:
    if not (min_f <= (c.followers or 0) <= max_f):
        return "Ditolak: followers di luar range"
    if c.is_private:
        return "Ditolak: akun private"
    if hard_excluded(clean_text(c.full_name, c.biography, c.category), c.username):
        return "Ditolak: akun institusi"
    if c.level not in levels:
        return f"Ditolak: jenjang {c.level}"
    if require_contact and not c.contact:
        return "Ditolak: tanpa kontak"
    if c.risk > 55 or c.creator < 8:
        return "Ditolak: sinyal creator lemah"
    return None


def row_of(c: Candidate) -> dict:
    hay = hay_of(c)
    return {
        "Username": c.username,
        "Instagram": f"@{c.username}",
        "Followers": c.followers,
        "Nama": c.full_name or "Perlu cek",
        "Jenjang": c.level,
        "Perlu Izin Ortu": needs_guardian(c.level),
        "Duta/Prestasi": duta_tags(hay) or "-",
        "Kontak di Bio": c.contact or "-",
        "Skor Prospek": c.prospect, "Skor Kreator": c.creator,
        "Engagement": round(c.engagement, 1), "Risiko": c.risk,
        "Status": c.status,
        "Alasan": ", ".join(c.reasons), "Warning": ", ".join(dict.fromkeys(c.warnings[:8])),
        "Bio": c.biography, "Sumber": " | ".join(sorted(c.sources))[:300],
        "Profil": c.url, "DM": f"https://ig.me/m/{c.username}",
    }


def load_database() -> pd.DataFrame:
    try:
        if DATABASE_PATH.exists():
            return pd.read_csv(DATABASE_PATH)
    except Exception:
        pass
    return pd.DataFrame()


def save_database(db: pd.DataFrame) -> None:
    if db.empty:
        return
    try:
        old = load_database()
        merged = pd.concat([old, db], ignore_index=True) if not old.empty else db
        merged.drop_duplicates(subset=["Instagram"], keep="last").to_csv(DATABASE_PATH, index=False)
    except Exception:
        pass


def discover(token, actor, keywords, hashtags, blacklist, min_f, max_f, levels, require_contact, show_weak,
             per_search, per_tag, max_details, max_cost, workers, progress: Callable[[str], None]):
    STATS.clear()
    STATS.update(keyword_runs=0, hashtag_posts=0, dari_search=0, dari_hashtag=0, detail_diminta=0,
                 detail_dapat=0, cache=0, blacklist=0, ditolak=0)
    errors: list[str] = []
    cache = load_cache()
    found: dict[str, dict] = {}
    sources: dict[str, set[str]] = {}

    def add(p: dict, src: str) -> None:
        u = p["username"]
        if u in blacklist:
            STATS["blacklist"] += 1
            return
        sources.setdefault(u, set()).add(src)
        old = found.get(u)
        if old is None or (old.get("followers") is None and p.get("followers") is not None):
            found[u] = {**(old or {}), **{k: v for k, v in p.items() if v not in (None, "")}}
        elif p.get("captions") and not old.get("captions"):
            old["captions"] = p["captions"]

    fatal = False
    # 1) Cari profil lewat kata kunci (hasil sudah berisi followers + bio)
    if keywords:
        progress(f"🔎 Mencari profil untuk {len(keywords)} kata kunci…")

        def kw_job(kw: str) -> tuple[str, list[dict]]:
            payload = {"search": kw, "searchType": "user", "searchLimit": per_search,
                       "resultsType": "details", "resultsLimit": 1}
            return kw, run_actor(token, actor, payload, max_cost=max_cost)

        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futs = [ex.submit(kw_job, kw) for kw in keywords]
            for fut in as_completed(futs):
                try:
                    kw, items = fut.result()
                    STATS["keyword_runs"] += 1
                    n = 0
                    for it in items:
                        p = profile_dict(it)
                        if p:
                            add(p, kw)
                            n += 1
                    STATS["dari_search"] += n
                    progress(f"  • “{kw}” → {n} profil")
                except ApifyError as exc:
                    errors.append(str(exc))
                    fatal = fatal or exc.fatal
                except Exception as exc:
                    errors.append(f"Kata kunci gagal: {exc}")

    # 2) Cari lewat hashtag: ambil postingan → ambil pemilik postingan
    if hashtags and not fatal:
        progress(f"#️⃣ Mengambil postingan dari {len(hashtags)} hashtag…")
        try:
            payload = {"directUrls": [f"https://www.instagram.com/explore/tags/{t}/" for t in hashtags],
                       "resultsType": "posts", "resultsLimit": per_tag}
            items = run_actor(token, actor, payload, max_cost=max_cost)
            STATS["hashtag_posts"] = len(items)
            for it in items:
                u = normalize_handle(g(it, "ownerUsername"))
                if not u:
                    continue
                m = re.search(r"/tags/([^/]+)", str(it.get("inputUrl") or it.get("url") or ""))
                tag = f"#{m.group(1)}" if m else "#hashtag"
                add({"username": u, "full_name": str(g(it, "ownerFullName", default="") or ""),
                     "captions": str(it.get("caption") or "")[:300]}, tag)
                STATS["dari_hashtag"] += 1
            progress(f"  • {len(items)} postingan → {sum(1 for v in sources.values() if any(s.startswith('#') for s in v))} akun")
        except ApifyError as exc:
            errors.append(str(exc))
            fatal = fatal or exc.fatal

    # 3) Lengkapi detail (followers/bio) untuk akun yang belum punya
    need = [u for u, p in found.items() if p.get("followers") is None]
    for u in list(need):
        hit = cache.get(u)
        if hit and time.time() - hit.get("ts", 0) < CACHE_TTL:
            found[u] = {**found[u], **hit["d"]}
            STATS["cache"] += 1
            need.remove(u)
    need = need[:max_details]
    STATS["detail_diminta"] = len(need)
    if need and not fatal:
        progress(f"👤 Mengambil detail {len(need)} profil (followers & bio)…")
        try:
            payload = {"directUrls": [f"https://www.instagram.com/{u}/" for u in need],
                       "resultsType": "details", "resultsLimit": 1}
            for it in run_actor(token, actor, payload, max_cost=max_cost):
                p = profile_dict(it)
                if p and p["username"] in found:
                    found[p["username"]] = {**found[p["username"]], **{k: v for k, v in p.items() if v not in (None, "")}}
                    STATS["detail_dapat"] += 1
        except ApifyError as exc:
            errors.append(str(exc))
    for u, p in found.items():  # simpan ke cache yang sudah lengkap
        if p.get("followers") is not None:
            cache[u] = {"ts": time.time(), "d": {k: p.get(k) for k in
                        ("full_name", "biography", "followers", "is_private", "is_business", "category",
                         "engagement", "contact", "captions")}}
    save_cache(cache)

    # 4) Nilai & saring
    kept: list[Candidate] = []
    weak: list[Candidate] = []
    allc: list[Candidate] = []
    for u, p in found.items():
        c = Candidate(u, f"https://www.instagram.com/{u}/", snippet=p.get("captions", ""),
                      full_name=p.get("full_name", ""), biography=p.get("biography", ""),
                      followers=p.get("followers"), is_private=p.get("is_private"),
                      is_business=p.get("is_business"), category=p.get("category", ""),
                      engagement=float(p.get("engagement") or 0), contact=p.get("contact", ""),
                      sources=sources.get(u, set()))
        c.level = detect_level(clean_text(c.full_name, c.biography))
        if c.level == "Tidak jelas":  # bio tidak menyebut jenjang -> lihat caption postingan
            c.level = detect_level(c.snippet)
        score_candidate(c)
        allc.append(c)
        if c.followers is None:
            c.status = "Detail tidak terbaca (cek manual)"
            continue
        reason = reject_reason(c, min_f, max_f, set(levels), require_contact)
        if reason is None:
            c.status = "Terverifikasi"
            kept.append(c)
        elif reason == "Ditolak: sinyal creator lemah" and show_weak:
            c.status = "Sinyal lemah (cek manual)"
            weak.append(c)
        else:
            c.status = reason
            STATS["ditolak"] += 1
    kept.sort(key=lambda c: (-c.prospect, -c.creator, c.username))
    weak.sort(key=lambda c: (-c.prospect, c.username))
    main_df = pd.DataFrame([row_of(c) for c in kept + weak])
    db = pd.DataFrame([row_of(c) for c in allc])
    if not db.empty:
        save_database(db)
    if not main_df.empty:
        main_df.insert(0, "Rank", range(1, len(main_df) + 1))
    return main_df, errors, db


# ---------------------------------------------------------------- DM templates (sesuai brief Batch 2)
DM_ADULT = (
    "Halo Kak {nama}! 👋🏻\n\n"
    "Saya Nanda dari IGI Academy. Kami sedang membuka kolaborasi KOL untuk campaign "
    "Persiapan Studi & Karir ke Jepang (Batch 2), dan kami tertarik dengan konten Kakak yang "
    "relevan dengan teman-teman pelajar.\n\n"
    "Benefit untuk Kakak:\n"
    "• Kelas bahasa Jepang gratis (4 pertemuan, online/offline)\n"
    "• Slot konsultasi karir, studi, & beasiswa ke Jepang gratis\n"
    "• Program afiliasi: reward Rp500.000 untuk setiap pendaftar yang terverifikasi memakai kode Kakak\n"
    "• Sertifikat kolaborasi + networking sesama KOL\n\n"
    "Tugasnya 1 konten Reels/Carousel (20–45 detik) dengan gaya Kakak sendiri, collab dengan @igi.academy.\n\n"
    "Kalau berkenan, saya kirim brief lengkapnya ya 🙏🏻\n{brief_link}"
)
DM_MINOR = (
    "Halo {nama}! 👋🏻\n\n"
    "Saya Nanda dari IGI Academy. Kami suka konten kamu dan ingin mengajak kolaborasi untuk "
    "campaign persiapan studi ke Jepang bagi teman-teman pelajar.\n\n"
    "Kamu akan dapat kelas bahasa Jepang gratis, sesi konsultasi studi & beasiswa gratis, serta "
    "sertifikat kolaborasi. Karena kolaborasi ini memakai perjanjian (MoU) dan program afiliasi "
    "berhadiah, kami perlu persetujuan orang tua/wali. Boleh kami kirim brief ke orang tua/wali, "
    "atau kamu teruskan brief ini ke beliau dulu?\n\n{brief_link}\n\n"
    "Terima kasih ya 🙏🏻"
)
T_ADULT, T_MINOR, T_BLANK, T_AUTO = "Dewasa (18+)", "Pelajar (di bawah 18)", "✏️ Tulis sendiri (kosong)", "Otomatis (sesuai jenjang)"
DEFAULT_TEMPLATES = {T_ADULT: DM_ADULT, T_MINOR: DM_MINOR}


def load_dm_templates() -> dict[str, str]:
    data = dict(DEFAULT_TEMPLATES)
    try:
        if DM_PATH.exists():
            data.update({str(k): str(v) for k, v in json.loads(DM_PATH.read_text(encoding="utf-8")).items()})
    except Exception:
        pass
    return data


def save_dm_templates(data: dict[str, str]) -> None:
    try:
        DM_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def fill_template(template: str, nama: str, username: str, brief_link: str) -> str:
    return (template.replace("{nama}", nama).replace("{username}", f"@{username}")
            .replace("{brief_link}", brief_link))


# ---------------------------------------------------------------- UI
st.set_page_config(page_title=APP_TITLE, page_icon="🎓", layout="wide")
st.title("🎓 IGI Academy — KOL Finder v5 (Apify)")
st.caption("Instagram langsung via Apify · 1K–10K followers · pelajar SMP/SMA · kata kunci profil + hashtag · "
           "arah niche bisa kamu ubah sendiri")

with st.sidebar:
    st.header("1. Sumber")
    token = st.text_input("Apify API Token", type="password",
                          help="Apify Console → Settings → API & Integrations → Personal API tokens")
    with st.expander("Pengaturan lanjutan Apify"):
        actor = st.text_input("Actor", value=DEFAULT_ACTOR,
                              help="Bisa diganti actor Instagram lain yang inputnya sama (search, searchType, directUrls, resultsType).")
        max_cost = st.number_input("Batas biaya per run (USD, 0 = tanpa batas)", 0.0, 50.0, 1.0, 0.5)
        workers = st.slider("Run paralel", 1, 5, 3)
    uploaded = st.file_uploader("Upload database KOL lama (XLSX/CSV)", type=["xlsx", "csv"])
    blacklist = load_blacklist(uploaded)
    st.info(f"Blacklist: **{len(blacklist):,}** akun")

    st.header("2. Kriteria")
    min_followers = st.number_input("Min followers", min_value=100, value=1_000, step=500)
    max_followers = st.number_input("Max followers", min_value=1_000, value=10_000, step=500)
    levels = st.multiselect("Jenjang yang diterima", LEVELS, default=["SMP", "SMA/SMK", "Tidak jelas"])
    require_contact = st.checkbox("Hanya yang punya kontak di bio (email/WA)", value=False)
    show_weak = st.checkbox("Tampilkan juga yang sinyal pelajarnya lemah (cek manual)", value=True)

    st.header("3. Seberapa banyak (mengatur biaya)")
    max_kw = st.slider("Maks kata kunci profil", 0, 20, 8)
    per_search = st.slider("Profil per kata kunci", 5, 50, 15, 5)
    max_tags = st.slider("Maks hashtag", 0, 15, 6)
    per_tag = st.slider("Postingan per hashtag", 10, 100, 30, 10)
    max_details = st.slider("Maks profil dilengkapi detailnya", 10, 200, 60, 10,
                            help="Akun dari hashtag perlu 1 pengambilan detail agar followers & bio terbaca.")

    st.header("4. Arah niche / profil")
    custom = load_custom()
    ALL_PACKS = {**QUERY_PACKS, **custom}
    packs = st.multiselect("Pilih arah niche (bisa lebih dari satu)", list(ALL_PACKS),
                           default=[p for p in ["Pelajar SMA", "Duta / Prestasi", "Study / Edukasi",
                                                "Jepang / Bahasa Asing"] if p in ALL_PACKS])
    extra_text = st.text_area("Keyword/hashtag bebas (1 per baris)", height=90,
                              placeholder="ketua osis\nanak sma malang\n#dutabahasa")
    with st.expander("✏️ Ubah / buat niche sendiri"):
        st.caption("Kata biasa = cari profil (mis. `duta bahasa`). Diawali # = cari hashtag (mis. `#dutabahasa`). "
                   "1 baris 1 istilah.")
        ed_pick = st.selectbox("Niche yang diedit", ["➕ Niche baru"] + list(ALL_PACKS), key="ed_pick")
        new_mode = ed_pick.startswith("➕")
        ed_name = st.text_input("Nama niche", value="" if new_mode else ed_pick, key=f"ed_name_{ed_pick}")
        ed_terms = st.text_area("Istilah (1 per baris)", value="" if new_mode else "\n".join(ALL_PACKS[ed_pick]),
                                height=200, key=f"ed_terms_{ed_pick}")
        e1, e2 = st.columns(2)
        if e1.button("💾 Simpan", use_container_width=True, key="niche_save"):
            terms = parse_terms(ed_terms)
            if ed_name.strip() and terms:
                custom[ed_name.strip()] = terms
                save_custom(custom)
                st.rerun()
            else:
                st.warning("Isi nama niche dan minimal satu istilah.")
        if e2.button("🗑️ Hapus / reset", use_container_width=True, key="niche_del"):
            if ed_name.strip() in custom:
                custom.pop(ed_name.strip())
                save_custom(custom)
                st.rerun()
            else:
                st.warning("Niche bawaan tidak bisa dihapus; edit lalu simpan untuk menimpa.")
        if custom:
            st.download_button("⬇️ Backup niche (JSON)", json.dumps(custom, ensure_ascii=False, indent=2),
                               "niches.json", "application/json", use_container_width=True)
        up = st.file_uploader("Impor niche (JSON)", type=["json"], key="niche_up")
        if up is not None and st.button("📥 Impor", use_container_width=True, key="niche_import"):
            try:
                custom.update({str(k): [str(x) for x in v] for k, v in json.loads(up.getvalue()).items()})
                save_custom(custom)
                st.rerun()
            except Exception:
                st.error("File JSON tidak valid.")

    st.header("5. DM")
    brief_link = st.text_input("Link brief", value=BRIEF_LINK)
    dm_templates = load_dm_templates()
    dm_choice = st.selectbox("Template DM", [T_AUTO, T_BLANK] + list(dm_templates))
    with st.expander("✍️ Edit / buat template DM"):
        st.caption("Placeholder: {nama} {username} {brief_link}")
        edit_key = dm_choice if dm_choice in dm_templates else "baru"
        t_name = st.text_input("Nama template", value=edit_key if edit_key != "baru" else "", key=f"t_name_{edit_key}")
        t_body = st.text_area("Isi template", value=dm_templates.get(dm_choice, ""), height=240, key=f"t_body_{edit_key}")
        s1, s2 = st.columns(2)
        if s1.button("💾 Simpan", use_container_width=True, key="dm_save"):
            if t_name.strip() and t_body.strip() and t_name.strip() not in (T_AUTO, T_BLANK):
                dm_templates[t_name.strip()] = t_body
                save_dm_templates(dm_templates)
                st.rerun()
            else:
                st.warning("Isi nama dan isi template.")
        if s2.button("↩️ Hapus / reset", use_container_width=True, key="dm_reset"):
            if t_name.strip() in DEFAULT_TEMPLATES:
                dm_templates[t_name.strip()] = DEFAULT_TEMPLATES[t_name.strip()]
            else:
                dm_templates.pop(t_name.strip(), None)
            save_dm_templates(dm_templates)
            st.rerun()

for key in ("results", "all_found"):
    if key not in st.session_state:
        st.session_state[key] = pd.DataFrame()

# gabungkan istilah dari niche terpilih + tambahan, lalu pisah: kata kunci profil vs hashtag
pool_terms = interleave([list(ALL_PACKS.get(p, [])) for p in packs] + [parse_terms(extra_text)])
all_kw, all_tags = split_terms(pool_terms)
keywords, hashtags = all_kw[:max_kw], all_tags[:max_tags]
DYNAMIC_TERMS.clear()
DYNAMIC_TERMS.update(dynamic_terms([t for p in packs if p in custom for t in custom[p]] + parse_terms(extra_text)))

est_results = len(keywords) * per_search + len(hashtags) * per_tag + max_details
st.write(f"**{len(keywords)} kata kunci profil** + **{len(hashtags)} hashtag** → perkiraan maks "
         f"**{est_results:,} hasil** ≈ **US$ {est_results * COST_PER_RESULT:.2f}** "
         f"(perkiraan kasar; harga sebenarnya cek di Apify).")
if keywords:
    st.caption("Kata kunci: " + ", ".join(keywords))
if hashtags:
    st.caption("Hashtag: " + ", ".join("#" + t for t in hashtags))

a, b = st.columns([1, 3])
if a.button("🔌 Cek Token", use_container_width=True, key="btn_api"):
    if not token:
        st.error("Masukkan Apify token dulu.")
    else:
        try:
            me = apify_call("GET", "/users/me", token).get("data", {})
            st.success(f"Token valid: {me.get('username', '?')}")
            try:
                lim = apify_call("GET", "/users/me/limits", token).get("data", {})
                used = lim.get("current", {}).get("monthlyUsageUsd")
                cap = lim.get("limits", {}).get("maxMonthlyUsageUsd")
                if used is not None:
                    st.metric("Pemakaian bulan ini (USD)", f"{used:.2f}", f"batas {cap}" if cap else None)
            except ApifyError:
                pass
        except ApifyError as exc:
            st.error(str(exc))

if b.button("🔎 Cari Creator Pelajar / Duta", type="primary", use_container_width=True, key="btn_search"):
    if not token:
        st.error("Masukkan Apify token dulu.")
    elif not (keywords or hashtags):
        st.error("Pilih minimal satu arah niche (atau isi keyword/hashtag).")
    elif not levels:
        st.error("Pilih minimal satu jenjang.")
    elif min_followers > max_followers:
        st.error("Min followers tidak boleh lebih besar dari Max.")
    else:
        status = st.status("Menjalankan pencarian di Apify… (bisa 1–5 menit)", expanded=True)
        try:
            df, errors, db = discover(token, actor, keywords, hashtags, blacklist, min_followers, max_followers,
                                      levels, require_contact, show_weak, per_search, per_tag, max_details,
                                      max_cost, workers, progress=status.write)
            st.session_state.results = df
            st.session_state.all_found = db
            status.update(label=f"Selesai — {len(df)} creator", state="complete", expanded=False)
            st.info("**Ringkasan:** {dari_search} profil dari kata kunci · {hashtag_posts} postingan hashtag "
                    "→ {dari_hashtag} akun · detail diminta {detail_diminta}, didapat {detail_dapat}, dari cache {cache} · "
                    "blacklist {blacklist} · ditolak {ditolak}".format(**STATS))
            for e in list(dict.fromkeys(errors))[:5]:
                st.warning(e)
            if df.empty:
                st.warning("Belum ada yang lolos. Longgarkan kriteria (jenjang/kontak), naikkan jumlah kata kunci "
                           "atau hashtag, atau buka 'Tes Apify' untuk melihat data mentah.")
        except Exception as exc:
            status.update(label="Gagal", state="error")
            st.error(f"Pencarian gagal: {exc}")

with st.expander("🧪 Tes Apify (lihat data mentah)"):
    t1, t2 = st.columns(2)
    t_type = t1.selectbox("Jenis", ["Cari profil (user)", "Postingan hashtag"], key="t_type")
    t_q = t2.text_input("Kata kunci / hashtag", value="duta bahasa", key="t_q")
    if st.button("Jalankan tes kecil", key="btn_test"):
        if not token:
            st.error("Masukkan Apify token dulu.")
        else:
            try:
                if t_type.startswith("Cari"):
                    pl = {"search": t_q, "searchType": "user", "searchLimit": 3, "resultsType": "details", "resultsLimit": 1}
                else:
                    tag = re.sub(r"[^A-Za-z0-9_]", "", t_q.lstrip("#"))
                    pl = {"directUrls": [f"https://www.instagram.com/explore/tags/{tag}/"],
                          "resultsType": "posts", "resultsLimit": 5}
                with st.spinner("Menjalankan…"):
                    its = run_actor(token, actor, pl, max_wait=180, max_cost=0.3)
                st.write(f"{len(its)} item mentah")
                if its:
                    st.json(its[0], expanded=False)
                    st.dataframe(pd.DataFrame([{"username": g(i, "username", "ownerUsername"),
                                                "followers": g(i, "followersCount"), "bio": str(i.get("biography", ""))[:80],
                                                "caption": str(i.get("caption", ""))[:80]} for i in its]),
                                 use_container_width=True, hide_index=True)
            except ApifyError as exc:
                st.error(str(exc))

results = st.session_state.results.copy()
if not results.empty:
    f1, f2, f3 = st.columns(3)
    sort_by = f1.selectbox("Urutkan berdasarkan", [c for c in ["Skor Prospek", "Followers", "Engagement", "Skor Kreator", "Risiko"]
                                                   if c in results.columns])
    sort_desc = f2.selectbox("Urutan", ["Besar → kecil", "Kecil → besar"]) == "Besar → kecil"
    only_ok = f3.checkbox("Hanya yang terverifikasi", value=False)
    if only_ok:
        results = results[results["Status"] == "Terverifikasi"]
    results = results.sort_values(sort_by, ascending=not sort_desc, na_position="last").reset_index(drop=True)
    results["Rank"] = range(1, len(results) + 1)
    st.caption("Klik judul kolom di tabel untuk menyortir lagi.")
if not results.empty:
    n_ok = int((results["Status"] == "Terverifikasi").sum())
    st.subheader(f"Hasil — {len(results)} creator")
    st.caption(f"{n_ok} sesuai kriteria · {len(results) - n_ok} sinyal lemah / perlu cek manual sebelum dihubungi")
    st.dataframe(
        results.drop(columns=["Bio", "Sumber"], errors="ignore"), use_container_width=True, hide_index=True,
        column_config={"Profil": st.column_config.LinkColumn("Instagram", display_text="Open IG"),
                       "DM": st.column_config.LinkColumn("DM", display_text="Open DM")},
    )
    buf = io.BytesIO()
    results.to_excel(buf, index=False, engine="openpyxl")
    d1, d2, d3 = st.columns(3)
    d1.download_button("⬇️ Download XLSX", buf.getvalue(), "igi_kol_study_v5.xlsx",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)
    d2.download_button("⬇️ Download CSV", results.to_csv(index=False).encode("utf-8-sig"),
                       "igi_kol_study_v5.csv", "text/csv", use_container_width=True)
    d3.download_button("⬇️ Daftar username (TXT)", "\n".join(f"@{u}" for u in results["Username"]),
                       "username_kol.txt", "text/plain", use_container_width=True)

    st.divider()
    st.subheader("Outreach satu per satu")
    selected = st.selectbox("Pilih kandidat", [f"{r['Instagram']} · {r['Nama']}" for _, r in results.iterrows()])
    handle = selected.split(" · ", 1)[0]
    row = results[results["Instagram"] == handle].iloc[0]
    uname = handle.lstrip("@")
    nama = row["Nama"] if row["Nama"] != "Perlu cek" else uname.replace(".", " ").replace("_", " ").title()
    minor = row["Perlu Izin Ortu"] != "Tidak"
    if dm_choice == T_AUTO:
        template = dm_templates[T_MINOR if minor else T_ADULT]
    elif dm_choice == T_BLANK:
        template = ""
    else:
        template = dm_templates.get(dm_choice, "")
    dm_text = fill_template(template, nama, uname, brief_link)
    dm_key = hashlib.md5(f"{dm_choice}|{template}|{brief_link}".encode()).hexdigest()[:8]

    left, right = st.columns([2, 1])
    with left:
        if minor:
            st.warning("Kemungkinan masih di bawah 18 tahun. Utamakan kontak resmi di bio atau orang tua/wali, "
                       "dan pastikan MoU & afiliasi disetujui wali.")
        edited = st.text_area("Pesan (boleh diubah)", value=dm_text, height=320, key=f"dm_{uname}_{dm_key}")
        st.caption("Salin dengan ikon copy di pojok kanan kotak, lalu tempel dan kirim sendiri di Instagram.")
        st.code(edited or " ", language=None)
    with right:
        st.markdown(f"### {handle}")
        fol = row["Followers"]
        fol_txt = f"{int(fol):,}" if pd.notna(fol) else "tidak terbaca"
        st.write(f"Status: **{row['Status']}**")
        st.write(f"Followers: **{fol_txt}** · Jenjang: **{row['Jenjang']}**")
        st.write(f"Duta/Prestasi: **{row.get('Duta/Prestasi', '-')}**")
        st.write(f"Skor prospek: **{row['Skor Prospek']}** · Skor kreator: **{row['Skor Kreator']}**")
        st.write(f"Kontak bio: {row['Kontak di Bio']}")
        if row.get("Bio"):
            st.caption(str(row["Bio"]))
        st.link_button("💬 Buka DM Instagram", str(row["DM"]), use_container_width=True)
        st.link_button("👤 Buka Profil", str(row["Profil"]), use_container_width=True)
        if st.button("✅ Tandai Sudah Dihubungi", use_container_width=True, key="btn_contacted"):
            record_contacted(uname)
            st.success("Masuk riwayat, akan diblokir di pencarian berikutnya.")
            st.rerun()
else:
    st.info("Isi Apify token, pilih arah niche, lalu klik Cari Creator.")

all_found = st.session_state.all_found
if not all_found.empty:
    st.divider()
    st.subheader(f"Database username — {len(all_found)} akun ditemukan")
    st.caption("Semua akun yang ditemukan di run ini (termasuk yang ditolak). Otomatis disimpan ke kol_database.csv.")
    cols = [c for c in ["Username", "Followers", "Jenjang", "Duta/Prestasi", "Status", "Profil"] if c in all_found.columns]
    st.dataframe(all_found[cols], use_container_width=True, hide_index=True,
                 column_config={"Profil": st.column_config.LinkColumn("Profil", display_text="Open")})
    c1, c2 = st.columns(2)
    c1.download_button("⬇️ Database (CSV)", all_found.to_csv(index=False).encode("utf-8-sig"),
                       "igi_kol_database.csv", "text/csv", use_container_width=True)
    xb = io.BytesIO()
    all_found.to_excel(xb, index=False, engine="openpyxl")
    c2.download_button("⬇️ Database (XLSX)", xb.getvalue(), "igi_kol_database.xlsx",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)

st.info("Tidak ada auto-send DM. Program hanya mencari dan menyiapkan pesan; tombol kirim tetap kamu yang tekan.")