from __future__ import annotations

import hashlib
import io
import json
import re
import sqlite3
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from itertools import zip_longest
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components

APP_TITLE = "IGI Academy — KOL Finder v3.6"
SOURCE_MODES = ["TikTok + Instagram", "TikTok saja", "Instagram saja"]
WORKERS = 4   # request paralel ke SerpApi (naikkan hati-hati, bisa kena rate limit)
BATCH = 8     # ukuran batch supaya bisa berhenti lebih awal kalau target sudah tercapai
HISTORY_PATH = Path(__file__).with_name("kol_history.csv")
CACHE_PATH = Path(__file__).with_name("serp_cache.sqlite")
CACHE_TTL = 14 * 24 * 3600   # cache SerpApi disimpan 14 hari
VERIFY_PER_ROUND = 6          # verifikasi profil IG per putaran pencarian (supaya hasil muncul bertahap)

# The discovery strategy is intentionally creator-first. The goal is to find PEOPLE
# whose public content is about Japan, then map them to their Instagram account.
QUERY_PACKS: dict[str, list[str]] = {
    "WNI / Japan Life": [
        '"orang Indonesia di Jepang"',
        '"WNI di Jepang"',
        '"tinggal di Jepang" Indonesia',
        '"hidup di Jepang" Indonesia',
        '"daily life" Jepang Indonesia',
        '"Indonesia in Japan"',
        '"Indonesian in Japan"',
    ],
    "Japan Vlog / Experience": [
        '"Japan vlog" Indonesia',
        '"vlog di Jepang" Indonesia',
        '"pengalaman di Jepang" Indonesia',
        '"pengalaman tinggal di Jepang"',
        '"pernah tinggal di Jepang"',
        '"cerita hidup di Jepang" Indonesia',
        '"kehidupan di Jepang" Indonesia',
        '"merantau ke Jepang" Indonesia',
    ],
    "Work-in-Japan Personal": [
        '"kerja di Jepang" Indonesia pengalaman',
        '"pekerja Indonesia di Jepang"',
        '"pengalaman kerja di Jepang"',
        '"gaji kerja di Jepang" pengalaman',
        '"kenshusei" Indonesia pengalaman',
        '"tokutei ginou" pengalaman Indonesia',
        '"SSW Jepang" pengalaman pribadi',
    ],
    "Japan Culture / Language Personal": [
        '"bahasa Jepang" creator Indonesia',
        '"belajar bahasa Jepang" creator',
        '"budaya Jepang" creator Indonesia',
        '"perbedaan Indonesia Jepang" creator',
        '"tips hidup di Jepang" Indonesia',
        '"Japanese culture" Indonesia creator',
    ],
}

# These are hard exclusions. They are applied to the TikTok search result AND the final
# Instagram profile. An institutional signal is enough to remove a candidate.
HARD_NEGATIVE = {
    "lpk", "lembaga pelatihan", "training center", "pusat pelatihan",
    "agency", "agensi", "recruitment", "rekrutmen", "job agency",
    "job center", "jobcentre", "jobcenter", "penyalur kerja", "penempatan kerja",
    "lowongan kerja", "loker", "lowongan", "sekolah bahasa", "language school",
    "kursus bahasa", "kursus", "academy", "akademi", "konsultan", "consultant",
    "konsultasi pendidikan", "study abroad", "studi ke jepang", "kampus",
    "universitas", "university", "college", "beasiswa center", "official account",
    "official", "recruiter", "rekruter", "headhunter", "talent agency", "manpower",
    "staffing", "sponsor kerja", "penyalur", "jasa visa", "travel agent",
    "tour & travel", "japan visa service", "working holiday agency",
}

SOFT_NEGATIVE = {
    "travel agency": -20,
    "tour & travel": -18,
    "travel": -5,  # a personal travel creator can still qualify
    "tour": -4,
    "school": -12,
    "education": -6,
    "study": -4,
    "beasiswa": -6,
    "info loker": -15,
    "kerja jepang": -1,
}

POSITIVE = {
    "orang indonesia di jepang": 30,
    "wni di jepang": 32,
    "indonesian in japan": 30,
    "tinggal di jepang": 27,
    "hidup di jepang": 24,
    "daily life": 16,
    "japan life": 18,
    "japan vlog": 17,
    "vlog di jepang": 18,
    "pengalaman tinggal di jepang": 28,
    "pengalaman di jepang": 24,
    "pengalaman kerja di jepang": 30,
    "pernah kerja di jepang": 28,
    "pernah tinggal di jepang": 26,
    "merantau ke jepang": 22,
    "pekerja indonesia di jepang": 29,
    "kerja di jepang": 18,
    "kenshusei": 22,
    "tokutei ginou": 30,
    "ssw jepang": 30,
    "bahasa jepang": 12,
    "budaya jepang": 11,
    "perbedaan indonesia jepang": 12,
    "gaji kerja di jepang": 14,
    "content creator": 16,
    "creator": 10,
    "vlog": 9,
    "daily": 6,
    "lifestyle": 6,
    "experience": 8,
    "pengalaman": 7,
}

PERSONAL_SIGNALS = {
    "aku": 10, "saya": 10, "gue": 8, "gw": 8,
    "my life": 9, "daily life": 9, "vlog": 7, "experience": 7,
    "pengalaman": 7, "merantau": 9, "tinggal": 6, "pekerja": 9,
    "karyawan": 6, "living in": 6, "life in": 6,
}

JAPAN_SIGNALS = {
    "jepang": 6, "japan": 6, "nihongo": 5, "nihon": 5,
    "tokyo": 5, "osaka": 5, "nagoya": 4, "saitama": 4,
    "kyoto": 4, "fukuoka": 4, "hiroshima": 3, "hokkaido": 3,
}

CITY_SIGNALS = {k for k in JAPAN_SIGNALS if k not in {"jepang", "japan", "nihongo", "nihon"}}

@dataclass
class Candidate:
    platform_source: str
    source_username: str
    source_url: str
    instagram_username: str = ""
    instagram_url: str = ""
    title: str = ""
    snippet: str = ""
    full_name: str = ""
    biography: str = ""
    followers: int | None = None
    following: int | None = None
    is_business: bool | None = None
    is_professional: bool | None = None
    is_private: bool | None = None
    is_verified: bool | None = None
    category: str = ""
    engagement_score: float = 0.0
    match_confidence: int = 0
    relevance: int = 0
    personal_score: int = 0
    closing_score: int = 0
    risk_score: int = 0
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source_queries: set[str] = field(default_factory=set)
    followers_hint: int | None = None
    tiktok_username: str = ""
    sources: set[str] = field(default_factory=set)


def clean_text(*values: object) -> str:
    out = []
    for v in values:
        if v is None:
            continue
        try:
            if pd.isna(v):
                continue
        except Exception:
            pass
        text = str(v).strip()
        if text:
            out.append(text)
    return " ".join(out)


def normalize_handle(value: object) -> str | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    text = str(value).strip()
    if not text:
        return None
    patterns = [
        r"(?:instagram\.com)/(?:@)?([A-Za-z0-9._]{2,50})",
        r"(?:tiktok\.com)/@([A-Za-z0-9._]{2,50})",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.I)
        if m:
            text = m.group(1)
            break
    else:
        text = text.replace("instagram:", " ").replace("tiktok:", " ")
        text = text.split("/")[-1].strip().lstrip("@").split()[0]
    text = text.strip().lstrip("@").rstrip(".,;:")
    return text.lower() if re.fullmatch(r"[A-Za-z0-9._]{2,50}", text) else None


def extract_platform_handles(text: str) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for m in re.finditer(r"instagram\.com/(?:@)?([A-Za-z0-9._]{2,50})", text, re.I):
        u = m.group(1).lower()
        if u not in {"p", "reel", "reels", "stories", "explore", "accounts", "directory", "tv"}:
            found.add(("Instagram", u))
    for m in re.finditer(r"tiktok\.com/@([A-Za-z0-9._]{2,50})", text, re.I):
        found.add(("TikTok", m.group(1).lower()))
    return found


def load_blacklist(uploaded_file: object | None, cross_platform: bool = True) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    if uploaded_file is not None:
        data = uploaded_file.getvalue()
        name = str(getattr(uploaded_file, "name", "")).lower()
        try:
            if name.endswith(".csv"):
                frames = [pd.read_csv(io.BytesIO(data), dtype=object)]
            else:
                xls = pd.ExcelFile(io.BytesIO(data))
                frames = [pd.read_excel(io.BytesIO(data), sheet_name=s, header=None, dtype=object) for s in xls.sheet_names]
            for df in frames:
                for _, row in df.iterrows():
                    row_text = clean_text(*row.tolist())
                    found |= extract_platform_handles(row_text)
                    for value in row.tolist():
                        u = normalize_handle(value)
                        if u and not str(value).startswith("http"):
                            # Existing IGI monitoring sheet is primarily Instagram.
                            found.add(("Instagram", u))
        except Exception:
            pass
    if HISTORY_PATH.exists():
        try:
            hist = pd.read_csv(HISTORY_PATH, dtype=str)
            for _, row in hist.iterrows():
                p = str(row.get("platform", "Instagram")).strip() or "Instagram"
                u = normalize_handle(row.get("username", ""))
                if u:
                    found.add((p, u))
        except Exception:
            pass
    if cross_platform:
        handles = {u for _, u in found}
        found |= {(p, u) for u in handles for p in ("Instagram", "TikTok")}
    return found


def record_contacted(platform: str, username: str, status: str = "DM Sent") -> None:
    u = normalize_handle(username) or username.lower().lstrip("@")
    row = pd.DataFrame([{
        "platform": platform,
        "username": f"@{u}",
        "status": status,
        "timestamp": pd.Timestamp.now().isoformat(timespec="seconds"),
    }])
    try:
        if HISTORY_PATH.exists():
            old = pd.read_csv(HISTORY_PATH, dtype=str)
            old = pd.concat([old, row], ignore_index=True)
            old.drop_duplicates(subset=["platform", "username"], keep="last").to_csv(HISTORY_PATH, index=False)
            return
    except Exception:
        pass
    row.to_csv(HISTORY_PATH, index=False)


def parse_followers(text: str) -> int | None:
    if not text:
        return None
    patterns = [
        r"([0-9]+(?:[.,][0-9]+)?)\s*([KkMm])\s*(?:followers|pengikut)",
        r"(?:followers|pengikut)\D{0,15}([0-9]+(?:[.,][0-9]+)?)\s*([KkMm])?",
        r"([0-9]{1,3}(?:[.,][0-9]{3})+)\s*(?:followers|pengikut)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if not m:
            continue
        raw = m.group(1)
        suffix = (m.group(2) or "").lower()
        try:
            if suffix:
                return int(float(raw.replace(",", ".")) * (1_000 if suffix == "k" else 1_000_000))
            return int(raw.replace(".", "").replace(",", ""))
        except ValueError:
            continue
    return None


def extract_tiktok_profile(link: str) -> str | None:
    try:
        p = urlparse(link)
    except Exception:
        return None
    if "tiktok.com" not in p.netloc.lower():
        return None
    m = re.match(r"@([A-Za-z0-9._]{2,50})(?:/.*)?$", p.path.strip("/"))
    return m.group(1).lower() if m else None


def extract_instagram_profile(link: str) -> str | None:
    try:
        p = urlparse(link)
    except Exception:
        return None
    if "instagram.com" not in p.netloc.lower():
        return None
    path = p.path.strip("/")
    if not path:
        return None
    first = path.split("/")[0].lstrip("@")
    if first.lower() in {"p", "reel", "reels", "stories", "explore", "accounts", "directory", "tv"}:
        return None
    return normalize_handle(first)


def hard_excluded(text: str) -> tuple[bool, list[str]]:
    low = text.lower()
    hits = [term for term in HARD_NEGATIVE if term in low]
    return bool(hits), hits[:10]


def name_similarity(a: str, b: str) -> float:
    def norm(s: str) -> str:
        s = re.sub(r"[^a-z0-9 ]+", " ", s.lower()).replace("_", " ").replace(".", " ")
        return re.sub(r"\s+", " ", s).strip()
    aa, bb = norm(a), norm(b)
    if not aa or not bb:
        return 0.0
    return SequenceMatcher(None, aa, bb).ratio()


def score_match(tiktok_username: str, tiktok_title: str, instagram_username: str, instagram_name: str, instagram_bio: str) -> int:
    score = 0
    if instagram_username.lower() == tiktok_username.lower():
        score += 65
    elif instagram_username.lower().replace("_", "").replace(".", "") == tiktok_username.lower().replace("_", "").replace(".", ""):
        score += 50
    sim = name_similarity(tiktok_title, instagram_name)
    if sim >= 0.85:
        score += 25
    elif sim >= 0.70:
        score += 18
    elif sim >= 0.55:
        score += 10
    low_bio = instagram_bio.lower()
    if any(k in low_bio for k in ("tiktok", tiktok_username.lower().lstrip("@"))):
        score += 10
    return min(100, score)


def engagement_from_posts(posts: Any, followers: int | None) -> float:
    if not isinstance(posts, list) or not posts:
        return 0.0
    likes: list[float] = []
    comments: list[float] = []
    for post in posts[:12]:
        if not isinstance(post, dict):
            continue
        for key in ("likes", "like_count"):
            if isinstance(post.get(key), (int, float)):
                likes.append(float(post[key])); break
        for key in ("comments", "comment_count"):
            if isinstance(post.get(key), (int, float)):
                comments.append(float(post[key])); break
    if not likes or not followers:
        return 0.0
    avg_likes = sum(likes) / len(likes)
    like_rate = avg_likes / followers
    avg_comments = sum(comments) / len(comments) if comments else 0.0
    comment_rate = avg_comments / followers if followers else 0.0
    score = 0.0
    if like_rate >= 0.08: score += 50
    elif like_rate >= 0.04: score += 40
    elif like_rate >= 0.02: score += 30
    elif like_rate >= 0.01: score += 20
    elif like_rate >= 0.005: score += 10
    if comment_rate >= 0.01: score += 25
    elif comment_rate >= 0.004: score += 18
    elif comment_rate >= 0.001: score += 10
    return min(100.0, score)


def score_candidate(c: Candidate) -> None:
    hay = clean_text(c.title, c.snippet, c.full_name, c.biography, c.category, c.source_username, c.instagram_username).lower()
    relevance = 0
    personal = 0
    closing = 0
    risk = 0
    reasons: list[str] = []
    warnings: list[str] = []

    for term, weight in POSITIVE.items():
        if term in hay:
            relevance += weight
            closing += weight
            if len(reasons) < 10:
                reasons.append(term)
    for term, weight in PERSONAL_SIGNALS.items():
        if term in hay:
            personal += weight
            relevance += weight // 2
            closing += weight
    for term, weight in JAPAN_SIGNALS.items():
        if term in hay:
            relevance += weight
            closing += weight // 2
    for term, penalty in SOFT_NEGATIVE.items():
        if term in hay:
            risk += abs(penalty)
            relevance += penalty
            closing += penalty
            warnings.append(term)

    if c.match_confidence >= 90:
        closing += 12
    elif c.match_confidence >= 75:
        closing += 7
    elif c.match_confidence < 60:
        risk += 20
        warnings.append("match IG dari TikTok kurang kuat")

    if c.followers is not None:
        if 1_000 <= c.followers <= 3_000:
            relevance += 10; closing += 9
        elif 3_001 <= c.followers <= 7_500:
            relevance += 13; closing += 13
        else:
            relevance += 10; closing += 10
    else:
        risk += 40
        warnings.append("followers IG tidak terverifikasi")

    if c.is_business is True:
        # Do not hard reject: creators often use professional/business accounts.
        risk += 8
        warnings.append("professional/business account")
    if c.is_private is True:
        risk += 25
        warnings.append("akun private")
    if c.is_verified is True:
        closing += 2

    if c.engagement_score:
        closing += int(c.engagement_score * 0.20)

    # Personal creator gate: strong Japan + personal signal, not just a keyword.
    if not any(k in hay for k in ("jepang", "japan", "tokyo", "osaka", "nihon", "nihongo")):
        risk += 20
        warnings.append("sinyal Jepang lemah")
    if personal < 8:
        risk += 15
        warnings.append("personal signal lemah")

    c.relevance = max(0, min(100, relevance))
    c.personal_score = max(0, min(100, personal))
    c.risk_score = max(0, min(100, risk))
    c.closing_score = max(0, min(100, closing - max(0, risk - 15)))
    c.reasons = reasons[:10]
    c.warnings = warnings[:10]


class OutOfBudget(RuntimeError):
    """Batas kredit per run tercapai atau kredit SerpApi habis."""


class SerpBudget:
    def __init__(self, limit: int = 10**9) -> None:
        self.lock = threading.Lock()
        self.reset(limit)

    def reset(self, limit: int) -> None:
        self.limit = int(limit)
        self.used = 0
        self.cache_hits = 0
        self.reason = ""

    def spend(self) -> None:
        with self.lock:
            if self.reason:
                raise OutOfBudget(self.reason)
            if self.used >= self.limit:
                self.reason = f"batas {self.limit} kredit per run tercapai"
                raise OutOfBudget(self.reason)
            self.used += 1

    def hit(self) -> None:
        with self.lock:
            self.cache_hits += 1

    def mark_exhausted(self, msg: str) -> None:
        with self.lock:
            self.reason = msg


BUDGET = SerpBudget()
_cache_lock = threading.Lock()
_cache_db: sqlite3.Connection | None = None


def _db() -> sqlite3.Connection:
    global _cache_db
    if _cache_db is None:
        _cache_db = sqlite3.connect(str(CACHE_PATH), check_same_thread=False)
        _cache_db.execute("CREATE TABLE IF NOT EXISTS cache (k TEXT PRIMARY KEY, ts REAL, v TEXT)")
        _cache_db.commit()
    return _cache_db


def cache_key(params: dict[str, Any]) -> str:
    clean = {k: v for k, v in params.items() if k != "api_key"}
    return hashlib.sha1(json.dumps(clean, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def cache_get(key: str) -> dict | None:
    try:
        with _cache_lock:
            row = _db().execute("SELECT ts, v FROM cache WHERE k=?", (key,)).fetchone()
        if row and time.time() - row[0] <= CACHE_TTL:
            return json.loads(row[1])
    except Exception:
        pass
    return None


def cache_set(key: str, payload: dict) -> None:
    try:
        with _cache_lock:
            db = _db()
            db.execute("INSERT OR REPLACE INTO cache (k, ts, v) VALUES (?, ?, ?)", (key, time.time(), json.dumps(payload)))
            db.commit()
    except Exception:
        pass


def serpapi_request(session: requests.Session, params: dict[str, Any], timeout=(10, 60), retries=2) -> dict:
    # 1) Cache dulu: pencarian yang sama tidak dibayar dua kali
    key = cache_key(params)
    cached = cache_get(key)
    if cached is not None:
        BUDGET.hit()
        if "_error" in cached:
            raise RuntimeError(cached["_error"])
        return cached

    # 2) Cek batas kredit sebelum request
    BUDGET.spend()

    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = session.get("https://serpapi.com/search.json", params=params, timeout=timeout)
            try:
                payload = r.json()
            except ValueError:
                payload = {}
            err = payload.get("error") if isinstance(payload, dict) else None
            if err:
                low = str(err).lower()
                if "out of searches" in low or "run out" in low:
                    msg = "kredit SerpApi habis"
                    BUDGET.mark_exhausted(msg)
                    raise OutOfBudget(msg)
                if "hasn't returned any results" in low or "no results" in low:
                    cache_set(key, {"_error": str(err)})  # hasil kosong juga di-cache
                raise RuntimeError(str(err))
            r.raise_for_status()
            cache_set(key, payload)
            return payload
        except requests.HTTPError as exc:
            try:
                detail = exc.response.json().get("error", str(exc))
            except Exception:
                detail = str(exc)
            raise RuntimeError(detail) from exc
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(2 + 2 * attempt)
    raise RuntimeError("Timeout/koneksi ke SerpApi setelah beberapa percobaan.") from last_error


def serpapi_web_search(api_key: str, query: str, engine: str, page: int, location: str) -> dict:
    session = requests.Session()
    params = {
        "engine": engine,
        "q": query,
        "api_key": api_key,
        "cc": "id",
        "safeSearch": "Strict" if engine == "bing" else None,
    }
    if engine == "bing":
        params["first"] = 1 + page * 10
        params["count"] = 10
    else:
        params["start"] = page * 10
        params["gl"] = "id"
        params["hl"] = "id"
    if location:
        params["location"] = location
    params = {k: v for k, v in params.items() if v is not None}
    return serpapi_request(session, params)


def search_web(api_key: str, query: str, page: int, location: str, engine_mode: str) -> tuple[list[dict], list[str]]:
    engines = {
        "Bing": ["bing"],
        "Google": ["google"],
        "Auto (Bing → Google)": ["bing", "google"],
    }.get(engine_mode, ["bing"])
    errors = []
    for engine in engines:
        try:
            payload = serpapi_web_search(api_key, query, engine, page, location)
            return payload.get("organic_results", []), errors
        except OutOfBudget:
            raise
        except Exception as exc:
            errors.append(f"{engine}: {exc}")
    return [], errors


def instagram_profile_lookup(api_key: str, username: str) -> dict:
    session = requests.Session()
    params = {
        "engine": "instagram_profile",
        "profile_id": username,
        "api_key": api_key,
        "json_restrictor": "profile_results(biography,followers,following,full_name,username,is_business_account,is_professional_account,is_private,is_verified,category_name,business_category_name,posts)",
    }
    return serpapi_request(session, params, timeout=(10, 50), retries=1).get("profile_results", {}) or {}


def map_tiktok_to_instagram(api_key: str, tiktok: Candidate, location: str, engine_mode: str) -> tuple[str | None, int, list[str]]:
    # 1) If the TikTok result already exposes an Instagram URL, use it first.
    for text in (tiktok.title, tiktok.snippet, tiktok.source_url):
        m = re.search(r"instagram\.com/(?:@)?([A-Za-z0-9._]{2,50})", text, re.I)
        if m:
            ig = normalize_handle(m.group(1))
            if ig:
                return ig, 75, ["Instagram ditemukan dari hasil TikTok"]

    queries = []
    u = tiktok.source_username
    title = tiktok.title.strip()
    if u:
        queries.append(f'site:instagram.com "{u}"')
    if title:
        queries.append(f'site:instagram.com "{title}"')
    if u and title:
        queries.append(f'site:instagram.com "{u}" "{title}"')

    candidates: list[tuple[str, int, str]] = []
    for q in queries[:3]:
        items, _errors = search_web(api_key, q, 0, location, engine_mode)
        for item in items:
            link = str(item.get("link", ""))
            ig = extract_instagram_profile(link)
            if not ig:
                continue
            match = score_match(u, title, ig, str(item.get("title", "")), str(item.get("snippet", "")))
            candidates.append((ig, match, q))
    if not candidates:
        return None, 0, []
    candidates.sort(key=lambda x: -x[1])
    best_ig, conf, q = candidates[0]
    return best_ig, conf, [f"Match dari pencarian: {q}"]


IG_BIO_PATTERNS = [
    r"instagram\.com/(?:@)?([A-Za-z0-9._]{2,30})",
    r"\b(?:ig|insta(?:gram)?)\b\s*(?:[:：=|\-–]\s*@?|@)\s*([A-Za-z0-9._]{2,30})",
    r"@([A-Za-z0-9._]{2,30})\s*\(?\s*(?:ig|instagram)\b",
]
IG_HANDLE_BLOCK = {
    "p", "reel", "reels", "stories", "explore", "accounts", "directory", "tv", "tiktok", "instagram",
    "ig", "gmail", "yahoo", "whatsapp", "wa", "line", "youtube", "facebook", "linktr", "linktree", "com",
}
TIKTOK_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
             "Chrome/124.0 Safari/537.36")


def extract_ig_from_bio(text: str) -> str | None:
    """Ambil username Instagram yang tertulis di bio/snippet TikTok (IG: xxx, @xxx ig, instagram.com/xxx)."""
    if not text:
        return None
    for pat in IG_BIO_PATTERNS:
        for m in re.finditer(pat, text, re.I):
            ig = normalize_handle(m.group(1).rstrip("."))
            if ig and len(ig) >= 3 and ig not in IG_HANDLE_BLOCK:
                return ig
    return None


def fetch_tiktok_bio(username: str) -> str:
    """Baca bio langsung dari halaman profil TikTok (tanpa SerpApi). Gagal = string kosong."""
    try:
        r = requests.get(
            f"https://www.tiktok.com/@{username}",
            headers={"User-Agent": TIKTOK_UA, "Accept-Language": "id-ID,id;q=0.9,en;q=0.8"},
            timeout=(5, 8),
        )
        if r.status_code != 200:
            return ""
        m = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', r.text, re.S)
        if not m:
            return ""
        data = json.loads(m.group(1))
        user = (data.get("__DEFAULT_SCOPE__", {}).get("webapp.user-detail", {})
                .get("userInfo", {}).get("user", {}))
        bio_link = (user.get("bioLink") or {}).get("link")
        return clean_text(user.get("nickname"), user.get("signature"), bio_link)
    except Exception:
        return ""


def resolve_instagram(api_key: str, t: "Candidate", location: str, engine_mode: str,
                      fetch_bio: bool, search_fallback: bool) -> tuple[str | None, int, list[str]]:
    """Cari IG milik kreator TikTok. Urutan: snippet -> bio halaman TikTok (gratis) -> pencarian (opsional, boros)."""
    ig = extract_ig_from_bio(clean_text(t.title, t.snippet))
    if ig:
        return ig, 90, ["IG tertulis di snippet/bio TikTok"]
    if fetch_bio:
        bio = fetch_tiktok_bio(t.tiktok_username)
        if bio:
            excluded, _hits = hard_excluded(bio)
            if excluded:
                return None, 0, ["bio TikTok terdeteksi institusi"]
            t.snippet = clean_text(t.snippet, bio)  # bio ikut dipakai untuk scoring
            ig = extract_ig_from_bio(bio)
            if ig:
                return ig, 95, ["IG tertulis di bio TikTok"]
    if search_fallback:
        return map_tiktok_to_instagram(api_key, t, location, engine_mode)
    return None, 0, []


def ig_followers_hint(text: str) -> int | None:
    """Tebak followers IG dari snippet pencarian ('1,234 Followers, 500 Following'). Hanya untuk pre-filter hemat kredit."""
    m = re.search(r"([0-9][0-9.,]*)\s*(K|M|rb|jt)?\s*(?:Followers|Pengikut)\b", text, re.I)
    if not m:
        return None
    raw, suffix = m.group(1), (m.group(2) or "").lower()
    try:
        if suffix:
            mult = 1_000 if suffix in ("k", "rb") else 1_000_000
            return int(float(raw.replace(",", ".")) * mult)
        return int(re.sub(r"[.,]", "", raw))
    except ValueError:
        return None


def tiktok_round(api_key: str, query: str, page: int, blacklist: set[tuple[str, str]], location: str, engine_mode: str) -> tuple[list[Candidate], list[str]]:
    items, errs = search_web(api_key, f'site:tiktok.com/@ {query}', page, location, engine_mode)
    out: list[Candidate] = []
    seen: set[str] = set()
    for item in items:
        u = extract_tiktok_profile(str(item.get("link", "")))
        if not u or u in seen or ("TikTok", u) in blacklist:
            continue
        title = str(item.get("title", ""))
        snippet = str(item.get("snippet", ""))
        excluded, _hits = hard_excluded(clean_text(title, snippet, u, query))
        if excluded:
            continue
        seen.add(u)
        out.append(Candidate(
            platform_source="TikTok", source_username=u, source_url=f"https://www.tiktok.com/@{u}",
            title=title, snippet=snippet, source_queries={query}, tiktok_username=u, sources={"TikTok"},
        ))
    return out, errs


def instagram_round(api_key: str, query: str, page: int, blacklist: set[tuple[str, str]], location: str, engine_mode: str) -> tuple[list[Candidate], list[str]]:
    items, errs = search_web(api_key, f'site:instagram.com {query}', page, location, engine_mode)
    out: list[Candidate] = []
    seen: set[str] = set()
    for item in items:
        u = extract_instagram_profile(str(item.get("link", "")))
        if not u or u in seen or ("Instagram", u) in blacklist:
            continue
        title = str(item.get("title", ""))
        snippet = str(item.get("snippet", ""))
        excluded, _hits = hard_excluded(clean_text(title, snippet, u, query))
        if excluded:
            continue
        seen.add(u)
        out.append(Candidate(
            platform_source="Instagram", source_username=u, source_url=f"https://www.instagram.com/{u}/",
            instagram_username=u, instagram_url=f"https://www.instagram.com/{u}/",
            title=title, snippet=snippet, match_confidence=100, source_queries={query}, sources={"Instagram"},
            followers_hint=ig_followers_hint(clean_text(title, snippet)),
        ))
    return out, errs


def enrich_from_instagram(api_key: str, c: Candidate) -> Candidate:
    try:
        profile = instagram_profile_lookup(api_key, c.instagram_username)
        c.full_name = str(profile.get("full_name") or "")
        c.biography = str(profile.get("biography") or "")
        c.followers = int(profile["followers"]) if profile.get("followers") is not None else None
        c.following = int(profile["following"]) if profile.get("following") is not None else None
        c.is_business = profile.get("is_business_account")
        c.is_professional = profile.get("is_professional_account")
        c.is_private = profile.get("is_private")
        c.is_verified = profile.get("is_verified")
        c.category = clean_text(profile.get("category_name"), profile.get("business_category_name"))
        c.engagement_score = engagement_from_posts(profile.get("posts"), c.followers)
        c.instagram_url = f"https://www.instagram.com/{c.instagram_username}/"
        match = score_match(c.source_username, c.title, c.instagram_username, c.full_name, c.biography)
        c.match_confidence = max(c.match_confidence, match)
        combined = clean_text(c.full_name, c.biography, c.category)
        excluded, hits = hard_excluded(combined)
        if excluded:
            c.warnings.extend([f"terdeteksi: {h}" for h in hits])
        score_candidate(c)
    except OutOfBudget:
        raise
    except Exception as exc:
        c.warnings.append(f"verifikasi IG gagal: {exc}")
    return c


def build_queries(selected_packs: list[str], extra_keywords: list[str]) -> list[str]:
    seen: set[str] = set(); out: list[str] = []
    for pack in selected_packs:
        for q in QUERY_PACKS.get(pack, []):
            k = q.lower().strip()
            if k and k not in seen:
                seen.add(k); out.append(q)
    for q in extra_keywords:
        k = q.lower().strip()
        if k and k not in seen:
            seen.add(k); out.append(q.strip())
    return out


def run_parallel(fn, items: list, workers: int = WORKERS) -> list:
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, items))


def filter_reason(c: Candidate, min_followers: int, max_followers: int) -> str | None:
    """Alasan kandidat gugur. None = lolos."""
    if not c.instagram_username or c.followers is None:
        return "profil IG tidak terbaca / followers kosong"
    if not (min_followers <= c.followers <= max_followers):
        return "followers di luar range"
    if c.match_confidence < 60:
        return "match TikTok→IG lemah"
    excluded, _hits = hard_excluded(clean_text(c.full_name, c.biography, c.category, c.source_username))
    if excluded:
        return "terdeteksi institusi (LPK/agency/dll)"
    if c.is_private is True:
        return "akun private"
    if c.risk_score > 55:
        return "skor risiko tinggi"
    if c.personal_score < 8:
        return "sinyal personal lemah"
    return None


def prescore(c: Candidate) -> int:
    """Skor murah (tanpa kredit) untuk menentukan siapa yang diverifikasi duluan."""
    hay = clean_text(c.title, c.snippet, c.source_username).lower()
    sc = sum(w for t, w in POSITIVE.items() if t in hay)
    sc += sum(w for t, w in PERSONAL_SIGNALS.items() if t in hay)
    sc += sum(w for t, w in JAPAN_SIGNALS.items() if t in hay)
    sc -= sum(abs(p) for t, p in SOFT_NEGATIVE.items() if t in hay)
    if len(c.sources) > 1:
        sc += 40
    if c.match_confidence >= 90:
        sc += 10
    return sc


def build_df(cands: list[Candidate], limit: int | None = None) -> pd.DataFrame:
    ordered = sorted(cands, key=lambda c: (-c.closing_score, -c.personal_score, -c.match_confidence, c.instagram_username))
    if limit:
        ordered = ordered[:limit]
    rows = []
    for i, c in enumerate(ordered, 1):
        tt_url = f"https://www.tiktok.com/@{c.tiktok_username}" if c.tiktok_username else ""
        rows.append({
            "Rank": i,
            "Sumber": " + ".join(sorted(c.sources)),
            "Instagram": f"@{c.instagram_username}",
            "Followers IG": c.followers,
            "Nama IG": c.full_name or "Perlu cek",
            "TikTok": f"@{c.tiktok_username}" if c.tiktok_username else "-",
            "Prospek Closing": c.closing_score,
            "Personal Creator": c.personal_score,
            "Relevansi Jepang": c.relevance,
            "Match TikTok→IG": c.match_confidence,
            "Engagement": round(c.engagement_score, 1),
            "Risiko Institusi": c.risk_score,
            "Alasan": ", ".join(c.reasons[:8]),
            "Warning": ", ".join(dict.fromkeys(c.warnings[:8])),
            "Bio IG": c.biography,
            "TikTok Context": c.title,
            "Keyword": " | ".join(sorted(c.source_queries))[:450],
            "Instagram Profile": c.instagram_url,
            "TikTok Profile": tt_url,
            "DM": f"https://ig.me/m/{c.instagram_username}",
            "Status": "Belum DM",
        })
    return pd.DataFrame(rows)


def get_searches_left(api_key: str) -> int | None:
    try:
        acc = requests.get("https://serpapi.com/account.json", params={"api_key": api_key}, timeout=(10, 30)).json()
        v = acc.get("total_searches_left", acc.get("plan_searches_left"))
        return int(v) if v is not None else None
    except Exception:
        return None


def run_pipeline(api_key: str, queries: list[str], blacklist: set[tuple[str, str]], target: int,
                 min_followers: int, max_followers: int, pages: int, location: str, engine_mode: str,
                 source_mode: str = "TikTok + Instagram", bio_only: bool = True, fetch_bio: bool = True,
                 on_update=None) -> tuple[pd.DataFrame, list[str], str]:
    """Pipeline bertahap: tiap putaran = 1 keyword -> cari -> verifikasi yang terbaik -> tampilkan.
    Hasil yang sudah lolos tidak hilang walaupun kredit habis / terjadi error."""
    use_tt = source_mode in ("TikTok + Instagram", "TikTok saja")
    use_ig = source_mode in ("TikTok + Instagram", "Instagram saja")
    errors: list[str] = []
    stats: Counter = Counter()
    drop: Counter = Counter()
    results: dict[str, Candidate] = {}
    pending: dict[str, Candidate] = {}
    done_ig: set[str] = set()
    stop_reason = ""
    rounds = [(p, q) for p in range(pages) for q in queries]

    def emit(msg: str) -> None:
        if on_update:
            frac = max(len(results) / max(1, target), BUDGET.used / max(1, BUDGET.limit))
            on_update(build_df(list(results.values()), target), msg, min(1.0, frac))

    def status_line(prefix: str) -> str:
        return f"{prefix} | hasil {len(results)}/{target} | kredit {BUDGET.used}/{BUDGET.limit} | antre verifikasi {len(pending)}"

    def add_pending(c: Candidate) -> None:
        ig = c.instagram_username
        if not ig or ("Instagram", ig) in blacklist:
            return
        existing = results.get(ig) or pending.get(ig)
        if existing is not None:
            existing.sources |= c.sources
            existing.tiktok_username = existing.tiktok_username or c.tiktok_username
            existing.source_queries |= c.source_queries
            return
        if ig not in done_ig:
            pending[ig] = c

    def verify_some(n: int) -> None:
        batch: list[Candidate] = []
        for c in sorted(pending.values(), key=lambda x: -prescore(x)):
            ig = c.instagram_username
            h = c.followers_hint
            if h is not None and not (min_followers * 0.75 <= h <= max_followers * 1.25):
                pending.pop(ig, None)
                done_ig.add(ig)
                drop["followers dari snippet di luar range (hemat 1 kredit)"] += 1
                continue
            batch.append(c)
            if len(batch) >= n:
                break
        if not batch:
            return
        for c in batch:
            pending.pop(c.instagram_username, None)
            done_ig.add(c.instagram_username)

        def _verify(c: Candidate) -> bool:
            try:
                enrich_from_instagram(api_key, c)
                return True
            except OutOfBudget:
                return False

        flags = run_parallel(_verify, batch)
        for c, ok in zip(batch, flags):
            if not ok:
                continue
            stats["verified"] += 1
            reason = filter_reason(c, min_followers, max_followers)
            if reason:
                drop[reason] += 1
                continue
            if len(c.sources) > 1:
                c.closing_score = min(100, c.closing_score + 5)
                c.reasons.insert(0, "ketemu di TikTok & Instagram")
            results[c.instagram_username] = c
        if not all(flags):
            raise OutOfBudget(BUDGET.reason or "batas kredit tercapai")

    def _resolve(t: Candidate):
        try:
            return resolve_instagram(api_key, t, location, engine_mode, fetch_bio, search_fallback=not bio_only)
        except OutOfBudget:
            raise
        except Exception as exc:
            return None, 0, [f"resolve IG gagal: {exc}"]

    kinds = (["tt"] if use_tt else []) + (["ig"] if use_ig else [])

    def _search(args):
        kind, query, page = args
        try:
            fn = tiktok_round if kind == "tt" else instagram_round
            return fn(api_key, query, page, blacklist, location, engine_mode)
        except OutOfBudget as exc:
            return exc

    try:
        emit(status_line("Mulai"))
        for ri, (page, query) in enumerate(rounds):
            if len(results) >= target:
                break
            if BUDGET.reason or BUDGET.used >= BUDGET.limit:
                raise OutOfBudget(BUDGET.reason or f"batas {BUDGET.limit} kredit per run tercapai")
            emit(status_line(f"Keyword {ri + 1}/{len(rounds)}: {query}"))

            outs = run_parallel(_search, [(k, query, page) for k in kinds], workers=2)
            budget_exc = None
            tts: list[Candidate] = []
            for kind, out in zip(kinds, outs):
                if isinstance(out, OutOfBudget):
                    budget_exc = out
                    continue
                cands, errs = out
                errors.extend(errs)
                if kind == "tt":
                    tts.extend(cands)
                else:
                    stats["ig_found"] += len(cands)
                    for c in cands:
                        add_pending(c)
            stats["tt_found"] += len(tts)

            for t, (ig, conf, notes) in zip(tts, run_parallel(_resolve, tts)):
                stats["tt_tried"] += 1
                if not ig:
                    continue
                stats["tt_with_ig"] += 1
                t.instagram_username = ig
                t.match_confidence = conf
                t.warnings.extend(notes)
                add_pending(t)

            verify_some(VERIFY_PER_ROUND)
            emit(status_line(f"Keyword {ri + 1}/{len(rounds)} selesai"))
            if budget_exc:
                raise budget_exc

        # Sisa kredit: habiskan antrean verifikasi
        while pending and len(results) < target:
            before = len(pending)
            verify_some(VERIFY_PER_ROUND)
            emit(status_line("Memverifikasi antrean"))
            if len(pending) == before:
                break
    except OutOfBudget as exc:
        stop_reason = str(exc)
    except Exception as exc:
        stop_reason = f"error tak terduga: {exc!r}"
        errors.append(stop_reason)

    log = [f"{msg} (x{n})" if n > 1 else msg for msg, n in Counter(errors).items()]
    log.append("--- Ringkasan ---")
    if stop_reason:
        log.append(f"Berhenti: {stop_reason}")
    log.append(f"Kredit terpakai: {BUDGET.used}/{BUDGET.limit} | pencarian dari cache (gratis): {BUDGET.cache_hits}")
    log.append(f"[1] Discovery: TikTok {stats['tt_found']} creator | Instagram langsung {stats['ig_found']} akun")
    if use_tt:
        log.append(f"[2] IG dari TikTok: ketemu {stats['tt_with_ig']} dari {stats['tt_tried']} creator")
    log.append(f"[3] Verifikasi profil IG: {stats['verified']} akun | lolos filter: {len(results)}")
    for reason, n in drop.most_common():
        log.append(f"    gugur: {reason} = {n}")
    return build_df(list(results.values()), target), log, stop_reason


def to_excel(df: pd.DataFrame) -> bytes:
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="KOL Candidates")
    return out.getvalue()


def copy_box(text_value: str) -> None:
    payload = json.dumps(text_value)
    components.html(
        f"""
        <div style='font-family:Arial,sans-serif'>
          <button onclick='copyMsg()' style='padding:8px 12px;border:1px solid #ccc;border-radius:8px;background:#fff;cursor:pointer;'>📋 Copy DM</button>
          <span id='status' style='margin-left:8px;font-size:13px;color:#555'></span>
        </div>
        <script>
        const msg = {payload};
        function copyMsg() {{
          navigator.clipboard.writeText(msg).then(() => {{
            document.getElementById('status').textContent = 'Copied';
          }}).catch(() => {{
            document.getElementById('status').textContent = 'Copy manual dari kotak teks';
          }});
        }}
        </script>
        """,
        height=48,
    )


st.set_page_config(page_title=APP_TITLE, page_icon="🌏", layout="wide")
st.title("🌏 IGI Academy — KOL Finder v3.6")
st.caption("TikTok + Instagram discovery (paralel) → gabung & dedupe → verifikasi followers IG 1K–15K → buang agency/LPK → ranking personal creator")

with st.sidebar:
    st.header("1. Sumber")
    api_key = st.text_input("SerpAPI API Key", type="password")
    uploaded = st.file_uploader("Upload database KOL lama (XLSX/CSV)", type=["xlsx", "csv"])
    cross_platform = st.checkbox("Username yang sama di IG/TikTok ikut diblokir", value=True)
    blacklist = load_blacklist(uploaded, cross_platform=cross_platform)
    st.info(f"Blacklist: **{len(blacklist):,}** akun/platform")

    st.header("2. Discovery")
    source_mode = st.radio("Sumber discovery", SOURCE_MODES, index=0,
                           help="Mode gabungan mencari di TikTok dan Instagram bersamaan, lalu hasilnya digabung. Pemakaian kredit SerpApi lebih besar.")
    min_followers = st.number_input("Min followers Instagram", min_value=1_000, value=1_000, step=500)
    max_followers = st.number_input("Max followers Instagram", min_value=1_000, value=15_000, step=500)
    target = st.number_input("Target hasil Instagram", min_value=5, max_value=100, value=50, step=5)
    pages = st.selectbox("Halaman per keyword", [1, 2, 3], index=1)
    max_queries = st.slider("Maksimal keyword per run", 4, 30, 18)
    credit_limit = st.number_input("Batas kredit SerpApi per run", min_value=0, max_value=2000, value=40, step=10,
                                   help="Aplikasi berhenti otomatis di batas ini dan tetap menampilkan hasil yang sudah didapat. Pencarian yang sama dari cache tidak dihitung.")
    location = st.text_input("Lokasi pencarian", value="Indonesia")
    engine_mode = st.selectbox("Mesin pencarian", ["Bing", "Auto (Bing → Google)", "Google"], index=1)

    if source_mode != "Instagram saja":
        tt_bio_only = st.checkbox("TikTok: hanya ambil yang IG-nya tertulis di bio", value=True,
                                  help="Hemat kredit: tidak ada pencarian mapping TikTok→IG. Kalau dimatikan, creator tanpa IG di bio dicari manual lewat SerpApi (boros).")
        tt_fetch_bio = st.checkbox("Baca bio langsung dari halaman TikTok (gratis)", value=True,
                                   help="Tidak memakai kredit SerpApi. TikTok kadang memblokir, jadi tidak selalu berhasil.")
    else:
        tt_bio_only, tt_fetch_bio = True, False

    st.header("3. Personal Japan Creator")
    selected_packs = st.multiselect(
        "Paket pencarian",
        list(QUERY_PACKS.keys()),
        default=["WNI / Japan Life", "Japan Vlog / Experience", "Work-in-Japan Personal"],
    )
    extra_text = st.text_area("Keyword tambahan (1/baris)", value="", height=90)
    extra_keywords = [x.strip() for x in extra_text.splitlines() if x.strip()]

    st.header("4. DM")
    default_dm = (
        "Halo Kak {nama}! 👋🏻✨\n\n"
        "Perkenalkan, saya Nanda dari IGI Academy. Sebelumnya saya sudah menghubungi Kakak terkait kesempatan kolaborasi.\n\n"
        "Saat ini kami sedang membuka campaign Kelas Bahasa Jepang untuk persiapan bekerja ke Jepang melalui jalur Visa SSW 🇯🇵. Kami tertarik dengan konten Kakak yang relevan dengan Jepang dan ingin mengajak Kakak berkolaborasi.\n\n"
        "Jika Kakak tertarik, saya siap kirimkan brief lengkapnya yaa 🙏🏻\n\n"
        "{brief_link}"
    )
    dm_template = st.text_area("Template DM", value=default_dm, height=220)
    brief_link = st.text_input("Link brief", value="https://drive.google.com/file/d/1mEJ8Ji9bnFoq79A_cFj6Ju_1eCtt8ZDG/view?usp=drive_link")

if "results" not in st.session_state:
    st.session_state.results = pd.DataFrame()

queries = build_queries(selected_packs, extra_keywords)[:max_queries]
st.write(f"**{len(queries)} keyword** × **{pages} halaman**. Hasil muncul bertahap dan **tetap tersimpan walaupun kredit habis**. Maksimal **{credit_limit} kredit** per run; pencarian yang sama dari cache tidak memakai kredit lagi.")

check_col, run_col = st.columns([1, 3])
with check_col:
    test_api = st.button("🔌 Cek API", use_container_width=True)
with run_col:
    run = st.button("🔎 Cari Creator (TikTok + Instagram)", type="primary", use_container_width=True)

if test_api:
    if not api_key:
        st.error("Masukkan SerpAPI API Key dulu.")
    else:
        try:
            acc = requests.get("https://serpapi.com/account.json", params={"api_key": api_key}, timeout=(10, 30)).json()
            a, b = st.columns(2)
            a.metric("API", "OK")
            b.metric("Sisa pencarian", acc.get("total_searches_left", acc.get("plan_searches_left", "-")))
        except Exception as exc:
            st.error(f"API tidak bisa diakses: {exc}")

if run:
    if not api_key:
        st.error("Masukkan SerpAPI API Key dulu.")
    elif not queries:
        st.error("Pilih minimal satu paket keyword atau isi keyword tambahan.")
    elif min_followers > max_followers:
        st.error("Min followers tidak boleh lebih besar dari Max followers.")
    else:
        left = get_searches_left(api_key)
        limit = int(credit_limit) if left is None else max(0, min(int(credit_limit), left))
        if left is not None:
            st.caption(f"Sisa kredit akun SerpApi: {left} | batas run ini: {limit}")
        BUDGET.reset(limit)
        st.session_state.results = pd.DataFrame()
        progress = st.progress(0.0)
        status_box = st.empty()
        live_box = st.empty()
        live_cols = ["Rank", "Sumber", "Instagram", "Followers IG", "Nama IG", "Prospek Closing", "Personal Creator"]

        def on_update(df_live: pd.DataFrame, msg: str, frac: float) -> None:
            progress.progress(frac)
            status_box.info(msg)
            st.session_state.results = df_live
            if not df_live.empty:
                live_box.dataframe(df_live[live_cols], use_container_width=True, hide_index=True)

        df, log, stop_reason = st.session_state.results, [], ""
        try:
            df, log, stop_reason = run_pipeline(
                api_key, queries, blacklist, int(target),
                int(min_followers), int(max_followers), int(pages), location, engine_mode,
                source_mode=source_mode, bio_only=tt_bio_only, fetch_bio=tt_fetch_bio,
                on_update=on_update,
            )
            st.session_state.results = df
        except Exception as exc:
            st.error(f"Pencarian terhenti: {exc}. Hasil sementara tetap ditampilkan di bawah.")
        progress.empty()
        status_box.empty()
        live_box.empty()
        if stop_reason:
            st.warning(f"Pencarian berhenti: {stop_reason}. {len(st.session_state.results)} akun yang sudah lolos tetap ditampilkan di bawah.")
        elif st.session_state.results.empty:
            st.warning("Belum ada kandidat yang lolos. Buka log di bawah untuk melihat di tahap mana kandidat gugur.")
        if log:
            with st.expander(f"Log pencarian & ringkasan ({len(log)} baris)", expanded=st.session_state.results.empty):
                st.code("\n".join(log))

results = st.session_state.results
if not results.empty:
    st.subheader(f"Hasil — {len(results)} Instagram creator")
    st.caption("Sumber: " + ", ".join(f"{k}: {v}" for k, v in results["Sumber"].value_counts().items()))
    st.dataframe(
        results[[
            "Rank", "Sumber", "Instagram", "Followers IG", "Nama IG", "TikTok", "Prospek Closing",
            "Personal Creator", "Relevansi Jepang", "Match TikTok→IG", "Engagement",
            "Risiko Institusi", "Alasan", "Warning", "Instagram Profile", "TikTok Profile", "DM"
        ]],
        use_container_width=True,
        hide_index=True,
        column_config={
            "Instagram Profile": st.column_config.LinkColumn("Instagram", display_text="Open IG"),
            "TikTok Profile": st.column_config.LinkColumn("TikTok", display_text="Open TikTok"),
            "DM": st.column_config.LinkColumn("DM", display_text="Open DM"),
        },
    )
    st.download_button(
        "⬇️ Download kandidat XLSX",
        data=to_excel(results),
        file_name="igi_kol_candidates_v3.6.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    st.divider()
    st.subheader("Outreach satu per satu")
    options = [f"{r['Instagram']} · {r['Nama IG']}" for _, r in results.iterrows()]
    selected = st.selectbox("Pilih kandidat", options)
    sel_username, sel_name = selected.split(" · ", 1)
    row = results[results["Instagram"] == sel_username].iloc[0]
    uname = sel_username.lstrip("@")
    suggested_name = str(row["Nama IG"]) if str(row["Nama IG"]) not in ("", "nan", "Perlu cek") else uname.replace(".", " ").replace("_", " ").title()
    dm_text = dm_template.replace("{username}", f"@{uname}").replace("{nama}", suggested_name).replace("{brief_link}", brief_link)

    left, right = st.columns([2, 1])
    with left:
        st.text_area("Pesan siap kirim", value=dm_text, height=250, key=f"dm_ig_{uname}")
        copy_box(dm_text)
    with right:
        st.markdown(f"### {sel_username}")
        st.write(f"Followers IG: **{row['Followers IG']:,}**")
        st.write(f"Prospek closing: **{row['Prospek Closing']} / 100**")
        st.write(f"Personal creator: **{row['Personal Creator']} / 100**")
        st.write(f"Match TikTok → IG: **{row['Match TikTok→IG']} / 100**")
        if row.get("Bio IG"):
            st.caption(str(row["Bio IG"]))
        st.link_button("💬 Buka DM Instagram", str(row["DM"]), use_container_width=True)
        st.link_button("👤 Buka Profil Instagram", str(row["Instagram Profile"]), use_container_width=True)
        if row.get("TikTok Profile"):
            st.link_button("🎵 Buka TikTok", str(row["TikTok Profile"]), use_container_width=True)
        if st.button("✅ Tandai Sudah DM", use_container_width=True):
            record_contacted("Instagram", uname)
            st.success("Masuk riwayat dan akan diblokir pada pencarian berikutnya.")
            st.rerun()
else:
    st.info("Upload database lama + API key + pilih paket Personal Japan Creator, lalu klik tombol cari.")

st.info("Auto-send cold DM tidak digunakan. Program hanya mencari, memetakan, memverifikasi, menyiapkan DM, dan membuka profil/DM; tombol Send tetap kamu yang tekan.")
