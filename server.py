#!/usr/bin/env python3
"""
Akor Sahnesi — yerel sunucu
----------------------------
akorlar.com (Akor_Data.py parser'ı), YouTube arama ve LRCLIB'i birleştirip
arayüze (app.html) tek bir şarkı JSON'u sunar. Şartnamedeki mimariyi
uygular: tarayıcı CORS yüzünden dış sitelere gidemediği için tüm dış
istekler burada, sunucuda yapılır.

Kurulum:
    pip install flask requests beautifulsoup4 playwright
    python3 -m playwright install chromium

Çalıştırma:
    python3 server.py
    -> http://127.0.0.1:5000 aç

Opsiyonel: YouTube Data API anahtarın varsa daha isabetli eşleşme için:
    export YT_API_KEY=...
"""

import atexit
import difflib
import json
import os
import re
from pathlib import Path
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, send_from_directory

import Akor_Data as parser  # kullanıcının akorlar.com parser'ı (Fetcher, scrape_url, BASE, ...)

APP_DIR = Path(__file__).parent
CACHE_DIR = APP_DIR / "cache"
CACHE_DIR.mkdir(exist_ok=True)

UA = {"User-Agent": parser.USER_AGENT}
YT_API_KEY = os.environ.get("YT_API_KEY", "").strip()
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()

# Bulut barındırmalarda (Render vb.) akorlar.com'un Cloudflare koruması IP'yi
# doğrudan engelliyor — bu yüzden orada canlı çekim hiç denenmez, sadece
# Mac'ten senkronlanmış önbellek sunulur. Yerelde varsayılan olarak açık.
LIVE_SCRAPE = os.environ.get("LIVE_SCRAPE", "1").strip() != "0"

app = Flask(__name__)

SLUG_RE = re.compile(r'^[a-z0-9][a-z0-9-]*$')


# --------------------------------------------------------------------------
# Fetcher (Cloudflare'i bir kez çözen paylaşılan Playwright oturumu)
# --------------------------------------------------------------------------

_fetcher = None


def get_fetcher():
    """Tembel başlatılan, tüm istekler arasında paylaşılan tek Fetcher.

    Playwright'ın senkron API'si tek bir thread'e bağlıdır; bu yüzden sunucu
    tek thread'li çalıştırılmalı (bkz. app.run(threaded=False) altta).
    """
    global _fetcher
    if _fetcher is None:
        _fetcher = parser.Fetcher()
    return _fetcher


@atexit.register
def _shutdown_fetcher():
    if _fetcher is not None:
        try:
            _fetcher.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# 4.2 YouTube otomatik eşleştirme
# --------------------------------------------------------------------------

# Arama sonucu sayfasında ilgisiz videoId'ler de geçer (öneri rafları, "bunu da
# izlediler" vb.). Sadece gerçek arama sonucu kartlarını (videoRenderer) ve
# başlığını eşleştiriyoruz ki hem doğru sıralama hem de doğrulama için başlık
# elimizde olsun.
_VIDEO_RENDERER_RE = re.compile(
    r'"videoRenderer":\{"videoId":"([\w-]{11})".*?"title":\{"runs":\[\{"text":"((?:[^"\\]|\\.)*)"',
    re.DOTALL,
)


def filter_embeddable(candidates):
    """YT_API_KEY varsa, videos.list ile gerçekten gömülebilir olanları eler.

    Çoğu resmi/parayla gelir getiren müzik yüklemesi Content ID politikası
    yüzünden JS API ile kontrollü gömmeyi (senkron için şart) engelliyor;
    bunu denemeden önce eleyip zaman kaybetmemek + kullanıcıya baştan
    çalışan bir video sunmak için kullanılır.
    """
    if not YT_API_KEY or not candidates:
        return candidates
    ids = [c["id"] for c in candidates]
    try:
        r = requests.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={"part": "status", "id": ",".join(ids), "key": YT_API_KEY},
            timeout=15,
        )
        status_map = {it["id"]: it.get("status", {}) for it in r.json().get("items", [])}
    except Exception:
        return candidates  # kontrol başarısız olursa filtrelemeden devam et

    filtered = [c for c in candidates if status_map.get(c["id"], {}).get("embeddable")]
    return filtered if filtered else candidates  # hepsi elenirse yine de bir şey döndür


def youtube_ids(query, n=1):
    """query için {"id","title"} sözlüklerinden ilk n benzersiz sonucu döndürür."""
    if YT_API_KEY:
        r = requests.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={
                "part": "snippet", "q": query, "type": "video",
                "maxResults": min(max(n * 3, n), 25), "key": YT_API_KEY,
            },
            timeout=15,
        )
        items = r.json().get("items", [])
        results = [
            {"id": it["id"]["videoId"], "title": it["snippet"]["title"]}
            for it in items if it.get("id", {}).get("videoId")
        ]
        results = filter_embeddable(results)
        if results:
            return results[:n]

    # anahtarsız yedek: arama sayfasındaki gerçek sonuç kartlarını regex ile çek
    r = requests.get(
        "https://www.youtube.com/results",
        params={"search_query": query}, headers=UA, timeout=15,
    )
    seen, results = set(), []
    for vid, raw_title in _VIDEO_RENDERER_RE.findall(r.text):
        if vid in seen:
            continue
        seen.add(vid)
        try:
            title = json.loads(f'"{raw_title}"')
        except Exception:
            title = raw_title
        results.append({"id": vid, "title": title})
        if len(results) >= n:
            break
    return results


def youtube_id(query):
    results = youtube_ids(query, n=1)
    return results[0]["id"] if results else None


# --------------------------------------------------------------------------
# 4.3 LRCLIB senkron
# --------------------------------------------------------------------------

def lrclib_synced(artist, title):
    r = requests.get(
        "https://lrclib.net/api/search",
        params={"track_name": title, "artist_name": artist},
        headers={"User-Agent": "akor-sahnesi/1.0"}, timeout=15,
    )
    for item in r.json():
        if item.get("syncedLyrics"):
            return item["syncedLyrics"], item.get("duration")
    return None, None


def parse_lrc(text):
    out = []
    for line in text.split("\n"):
        m = re.match(r'\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)', line)
        if m and m.group(3).strip():
            t = int(m.group(1)) * 60 + float(m.group(2))
            out.append({"t": round(t, 2), "text": m.group(3).strip()})
    return out


# --------------------------------------------------------------------------
# 4.4 Hizalama — DOĞRULANMIŞ (aynen korunuyor)
# --------------------------------------------------------------------------

def norm(s):
    s = s.lower()
    s = re.sub(r'\s*x\d+\s*$', '', s)                     # "x2" tekrar işaretini at
    s = s.translate(str.maketrans("çğıöşü", "cgiosu"))    # Türkçe sadeleştir
    s = re.sub(r'[^a-z0-9 ]', '', s)                      # noktalama at
    return re.sub(r'\s+', ' ', s).strip()


def align(lines, lrc):
    lrc_norm = [norm(l["text"]) for l in lrc]
    j = 0
    for cl in lines:
        if not cl.get("lyric", "").strip():
            cl["t"] = None
            continue
        target = norm(cl["lyric"])
        best = (0.0, None)
        for k in range(j, min(j + 5, len(lrc))):          # ileri pencere, monoton
            r = difflib.SequenceMatcher(None, target, lrc_norm[k]).ratio()
            if r > best[0]:
                best = (r, k)
        if best[1] is not None and best[0] >= 0.6:
            cl["t"] = lrc[best[1]]["t"]
            j = best[1] + 1
        else:
            cl["t"] = None
    # eşleşmeyen satırları komşulara göre yumuşakça doldur (nakarat tekrarları vb.)
    known = [(i, l["t"]) for i, l in enumerate(lines) if l.get("t") is not None]
    for i, l in enumerate(lines):
        if l.get("t") is None and known:
            prev = [t for idx, t in known if idx < i]
            nxt = [t for idx, t in known if idx > i]
            if prev and nxt:
                l["t"] = round((prev[-1] + nxt[0]) / 2, 2)
            elif prev:
                l["t"] = prev[-1]
            elif nxt:
                l["t"] = nxt[0]
    return lines


# --------------------------------------------------------------------------
# Bölüm başlığı (nakarat vb.) tespiti — sezgisel
# --------------------------------------------------------------------------

SECTION_RE = re.compile(
    r'^\(?\s*(\d\s*\.?\s*)?'
    r'(nakarat|koro|chorus|verse|bridge|köprü|solo|intro|outro|ara\s*müzik|dize|kıta)'
    r'\s*\)?\s*:?\s*$',
    re.IGNORECASE,
)


def is_section_line(lyric, chords):
    if chords:
        return False
    text = (lyric or "").strip()
    if not text:
        return False
    return bool(SECTION_RE.match(text))


# --------------------------------------------------------------------------
# Şarkı derleme akışı (4.1)
# --------------------------------------------------------------------------

def build_song(slug):
    cache_file = CACHE_DIR / f"{slug}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))

    if not LIVE_SCRAPE:
        raise RuntimeError(
            "Bu şarkı henüz önbellekte yok. Bu sunucudan akorlar.com'a "
            "doğrudan erişim yok (Cloudflare engeli) — önce Mac'teki "
            "uygulamadan bu şarkıyı aç, sonra buluta senkronla."
        )

    url = f"{parser.BASE}/{slug}"
    raw = parser.scrape_url(get_fetcher(), url)
    if raw.get("title") and re.search(r'blocked|attention required|just a moment', raw["title"], re.I):
        raise RuntimeError("akorlar.com bu sunucudan erişilemiyor (Cloudflare engeli).")

    lines = []
    for ln in raw.get("lines", []):
        lyric = ln.get("lyric", "")
        chords = ln.get("chords", [])
        lines.append({
            "lyric": lyric,
            "chords": chords,
            "section": is_section_line(lyric, chords),
            "t": None,
        })

    song = {
        "slug": slug,
        "artist": raw.get("artist"),
        "title": raw.get("title"),
        "key": raw.get("key"),
        "capo": raw.get("capo") if raw.get("capo") is not None else 0,
        "rhythm": raw.get("rhythm"),
        "youtubeId": None,
        "hasSyncedLyrics": False,
        "lines": lines,
    }

    query = " ".join(p for p in [song["artist"], song["title"]] if p) or slug
    try:
        song["youtubeId"] = youtube_id(query)
    except Exception:
        song["youtubeId"] = None

    try:
        lrc, duration = lrclib_synced(song["artist"] or "", song["title"] or "")
    except Exception:
        lrc, duration = None, None

    if lrc:
        song["hasSyncedLyrics"] = True
        song["lines"] = align(song["lines"], parse_lrc(lrc))
        song["duration"] = duration

    cache_file.write_text(
        json.dumps(song, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return song


# --------------------------------------------------------------------------
# akorlar.com içi arama (arama ekranı için)
# --------------------------------------------------------------------------

def search_cache(query, limit=30):
    """Bulut modunda (canlı erişim yokken) yalnızca senkronlanmış önbellekte ara."""
    q = query.strip().lower()
    out = []
    for f in sorted(CACHE_DIR.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        label = f"{d.get('artist') or ''} - {d.get('title') or ''}".strip(" -")
        if q in label.lower():
            out.append({"slug": d.get("slug", f.stem), "label": label})
        if len(out) >= limit:
            break
    return out


def search_songs(query, limit=30):
    if not LIVE_SCRAPE:
        return search_cache(query, limit)

    slug_query = quote(re.sub(r'\s+', '-', query.strip()))
    url = f"{parser.BASE}/ara/{slug_query}"
    html = get_fetcher().get(url)
    soup = BeautifulSoup(html, "html.parser")

    container = soup.select_one("div.page-search")
    if not container:
        return []

    out, seen = [], set()
    for a in container.select("li a[href]"):
        href = a.get("href", "")
        slug = href.rstrip("/").split("/")[-1]
        if not slug or slug in seen or not SLUG_RE.match(slug):
            continue
        title_div = a.select_one(".title")
        label = title_div.get_text(strip=True) if title_div else a.get_text(" ", strip=True)
        if not label:
            continue
        seen.add(slug)
        out.append({"slug": slug, "label": label})
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(APP_DIR, "app.html")


@app.route("/api/song")
def api_song():
    raw_slug = request.args.get("slug", "").strip()
    if not raw_slug:
        return jsonify({"error": "slug gerekli"}), 400

    slug = parser.slug_of(raw_slug) if raw_slug.startswith("http") else raw_slug
    if not SLUG_RE.match(slug):
        return jsonify({"error": "geçersiz slug"}), 400

    try:
        song = build_song(slug)
    except Exception as e:
        return jsonify({"error": f"şarkı alınamadı: {e}"}), 502
    return jsonify(song)


@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    try:
        results = search_songs(q)
    except Exception as e:
        return jsonify({"error": f"arama başarısız: {e}"}), 502
    return jsonify(results)


@app.route("/api/admin/sync-song", methods=["POST"])
def api_sync_song():
    """Mac'te önbelleğe alınmış bir şarkıyı buluta yükler (senkron scripti kullanır)."""
    if not ADMIN_TOKEN or request.headers.get("Authorization") != f"Bearer {ADMIN_TOKEN}":
        return jsonify({"error": "yetkisiz"}), 403

    data = request.get_json(silent=True)
    if not data or not data.get("slug"):
        return jsonify({"error": "geçersiz gövde, 'slug' gerekli"}), 400

    slug = data["slug"]
    if not SLUG_RE.match(slug):
        return jsonify({"error": "geçersiz slug"}), 400

    cache_file = CACHE_DIR / f"{slug}.json"
    cache_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return jsonify({"ok": True, "slug": slug})


@app.route("/api/youtube-alts")
def api_youtube_alts():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    try:
        ids = youtube_ids(q, n=5)
    except Exception:
        ids = []
    return jsonify(ids)


if __name__ == "__main__":
    # Render (ve çoğu bulut barındırma) hangi portu dinlememiz gerektiğini
    # PORT ortam değişkeniyle söyler; yerelde yoksa eski portu kullan.
    # host=0.0.0.0: konteynerin dışından (Render'ın proxy'sinden / aynı Wi-Fi
    # ağındaki diğer cihazlardan) erişilebilsin diye tüm arayüzlerde dinliyor.
    # threaded=False: Playwright'ın senkron API'si tek thread ister.
    port = int(os.environ.get("PORT", 8765))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=False)
