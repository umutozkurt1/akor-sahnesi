#!/usr/bin/env python3
"""
akorlar.com scraper + parser
-----------------------------
akorlar.com sitesindeki şarkı akorlarını yapılandırılmış JSON'a çevirir ve
tüm siteyi (sitemap üzerinden ~15.770 şarkı) sırayla indirir.

Site Cloudflare JS challenge kullanıyor; düz `requests` ile 403 dönüyor.
Bu yüzden Playwright ile gerçek bir Chromium başlatıp challenge'ı bir kez
çözüyoruz, sonrasında aynı tarayıcı bağlamının (context) cf_clearance
çerezini kullanan hafif HTTP istekleriyle (sayfa render etmeden) devam
ediyoruz. Bu tekil sayfa indirimini çok hızlandırıyor.

{
  "artist": "...", "title": "...",
  "key": "G", "capo": 0, "rhythm": "A-K-Y-Y-K-Y",
  "base_chords": ["Gm","Cm","Ab","Bb"],
  "lines": [ {"lyric": "...", "chords": [{"chord":"Gm","col":0}, ...]}, ... ]
}

Kurulum:
    pip install playwright beautifulsoup4
    python3 -m playwright install chromium

Kullanım:
    # Tek şarkı (test için)
    python3 Akor_Data.py baris-manco-daglar-daglar
    python3 Akor_Data.py https://akorlar.com/duman-kufi

    # Sitedeki TÜM şarkıları sırayla indir (kaldığı yerden devam eder)
    python3 Akor_Data.py --all
    python3 Akor_Data.py --all --out akorlar_data --delay 1.2
    python3 Akor_Data.py --all --limit 50          # ilk 50 şarkıyla dene

Not: Toplu çekimde her şarkı arasında bir gecikme var (varsayılan 1.2s).
~15.770 şarkı için bu tempo ile toplam süre ~6-7 saat sürer. --delay ile
ayarlanabilir ama siteye nazik davranmak için çok düşürmemek daha güvenli.
"""

import argparse
import json
import random
import re
import sys
import time
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString
from playwright.sync_api import sync_playwright

BASE = "https://akorlar.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Bir akor token'ı: kök (A-G) + opsiyonel #/b + tür + ekler + bas nota
# (yalnızca <pre id="key"> bulunamadığı nadir durumlarda fallback olarak kullanılır)
CHORD = re.compile(
    r'^[A-G](#|b)?(m|min|maj|dim|aug|sus|add|M)?\d*(sus\d)?(add\d+)?(/[A-G](#|b)?)?$'
)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def is_chord_line(line: str) -> bool:
    """Satırdaki token'ların çoğu akorsa True. (fallback parser için)"""
    toks = line.split()
    if not toks:
        return False
    hits = sum(1 for t in toks if CHORD.match(t))
    return hits / len(toks) >= 0.6


def chord_positions(line: str):
    out = []
    for m in re.finditer(r'\S+', line):
        if CHORD.match(m.group()):
            out.append({"chord": m.group(), "col": m.start()})
    return out


def parse_chord_block_fallback(raw: str):
    """<pre id="key"> bulunamazsa devreye giren eski heuristik parser."""
    lines = raw.split("\n")
    result, i = [], 0
    while i < len(lines):
        line = lines[i]
        if is_chord_line(line):
            chords = chord_positions(line)
            lyric = ""
            if (i + 1 < len(lines)
                    and lines[i + 1].strip()
                    and not is_chord_line(lines[i + 1])):
                lyric = lines[i + 1].rstrip()
                i += 2
            else:
                i += 1
            result.append({"lyric": lyric, "chords": chords})
        elif line.strip():
            result.append({"lyric": line.rstrip(), "chords": []})
            i += 1
        else:
            i += 1
    return result


def parse_pre_rows(pre_tag):
    """<pre id="key"> içindeki her satır <span>'ını {chords, text} olarak çöz."""
    rows = []
    for line in pre_tag.find_all("span", recursive=False):
        col = 0
        chords = []
        text_parts = []
        for child in line.children:
            if isinstance(child, NavigableString):
                text = str(child)
                text_parts.append(text)
                col += len(text)
            elif child.name == "span" and "c" in (child.get("class") or []):
                chord = child.get_text()
                chords.append({"chord": chord, "col": col})
                col += len(chord)
            else:
                text = child.get_text()
                text_parts.append(text)
                col += len(text)
        rows.append({"chords": chords, "text": "".join(text_parts)})
    return rows


def merge_rows(rows):
    """Akor-satırı + söz-satırı çiftlerini {"lyric","chords"} kayıtlarına birleştir."""
    result = []
    i, n = 0, len(rows)
    while i < n:
        row = rows[i]
        stripped = row["text"].strip()
        if row["chords"]:
            lyric = ""
            j = i + 1
            if j < n and not rows[j]["chords"] and rows[j]["text"].strip():
                lyric = rows[j]["text"].rstrip()
                i = j + 1
            else:
                i += 1
            result.append({"lyric": lyric, "chords": row["chords"]})
        elif stripped:
            result.append({"lyric": row["text"].rstrip(), "chords": []})
            i += 1
        else:
            result.append({"lyric": "", "chords": []})  # ayraç boş satır
            i += 1
    return result


def extract_meta(soup: BeautifulSoup):
    meta = {"key": None, "capo": None, "rhythm": None, "base_chords": []}

    key_div = soup.select_one("#default-key")
    if key_div and key_div.get("data-key"):
        meta["key"] = key_div["data-key"]
    else:
        pre = soup.select_one("pre#key")
        if pre and pre.get("data-key"):
            meta["key"] = pre["data-key"]

    capo_span = soup.select_one(".menu-capo span")
    if capo_span:
        m = re.search(r'(\d+)', capo_span.get_text())
        if m:
            meta["capo"] = int(m.group(1))

    rhythm_span = soup.select_one(".menu-rhythm-section span")
    if rhythm_span:
        # Site farklı şarkılarda farklı biçimler kullanıyor: "A-K-Y-Y-K-Y",
        # "AA YYY AYAY", "A-A-YYAAYAY" gibi. Tire/boşluğu at, sadece A/K/Y
        # harflerini vuruş olarak al, tekdüze "A-K-Y..." biçimine çevir.
        letters = re.sub(r'[^AKYaky]', '', rhythm_span.get_text(strip=True)).upper()
        if letters:
            meta["rhythm"] = "-".join(letters)

    text = soup.get_text("\n")
    m = re.search(r'Temel Akorlar[:\s]*([A-G][^\n<]+)', text)
    if m:
        parts = re.split(r'\s*-\s*', m.group(1).strip())
        meta["base_chords"] = [p.strip() for p in parts if CHORD.match(p.strip())]

    return meta


def parse_song_html(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    artist, title = None, None
    h1 = soup.find("h1")
    if h1:
        h1_text = h1.get_text(" ", strip=True)
        h1_text = re.sub(r'\s*Akor\s*$', '', h1_text)
        if "-" in h1_text:
            artist, title = [s.strip() for s in h1_text.split("-", 1)]
        else:
            title = h1_text

    pre = soup.select_one("pre#key") or soup.find("pre")
    if pre and pre.find("span", class_="c"):
        rows = parse_pre_rows(pre)
        lines = merge_rows(rows)
    elif pre:
        lines = parse_chord_block_fallback(pre.get_text())
    else:
        candidates = []
        for tag in soup.find_all(["div", "code", "section"]):
            t = tag.get_text("\n")
            score = sum(1 for ln in t.split("\n") if is_chord_line(ln))
            if score >= 2:
                candidates.append((score, t))
        raw_block = max(candidates, key=lambda x: x[0])[1] if candidates else ""
        lines = parse_chord_block_fallback(raw_block) if raw_block else []

    meta = extract_meta(soup)

    return {
        "url": url,
        "artist": artist,
        "title": title,
        "key": meta["key"],
        "capo": meta["capo"],
        "rhythm": meta["rhythm"],
        "base_chords": meta["base_chords"],
        "lines": lines,
    }


# --------------------------------------------------------------------------
# Fetching (Cloudflare-aware)
# --------------------------------------------------------------------------

class Fetcher:
    """Cloudflare challenge'ı bir kez çözüp sonraki istekleri hafif HTTP ile yapar."""

    def __init__(self):
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=True)
        self.context = self.browser.new_context(
            user_agent=USER_AGENT, locale="tr-TR"
        )
        self.page = self.context.new_page()
        self._warm_up()

    def _warm_up(self):
        self.page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=30000)
        self.page.wait_for_timeout(1500)

    def get(self, url: str, retries: int = 3) -> str:
        for attempt in range(retries):
            resp = self.context.request.get(url, timeout=30000)
            if resp.status == 200:
                body = resp.text()
                if "Just a moment" not in body and "cf_chl_opt" not in body:
                    return body
            # challenge/expired cookie ya da geçici hata -> tarayıcıda yeniden çöz
            self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            self.page.wait_for_timeout(2000 + attempt * 1000)
            html = self.page.content()
            if "Just a moment" not in html and "cf_chl_opt" not in html:
                return html
        raise RuntimeError(f"Sayfa alınamadı (challenge çözülemedi): {url}")

    def close(self):
        self.browser.close()
        self._pw.stop()


def get_all_song_urls(fetcher: Fetcher) -> list:
    """sitemap.php altındaki akorlar_sitemap1..N.xml dosyalarından tüm şarkı URL'lerini çıkar."""
    index_xml = fetcher.get(f"{BASE}/sitemap.php")
    sitemap_files = sorted(set(re.findall(r'akorlar_sitemap\d+\.xml', index_xml)))

    urls = []
    for fname in sitemap_files:
        xml = fetcher.get(f"{BASE}/{fname}")
        urls.extend(re.findall(r'<loc>([^<]+)</loc>', xml))
        time.sleep(0.3)

    # sitemap bazen ana sayfa gibi şarkı-olmayan girişler içerebiliyor; ayıkla
    urls = [u for u in urls if u.rstrip("/") != BASE and "-" in slug_of(u)]
    return sorted(set(urls))


# --------------------------------------------------------------------------
# Scraping
# --------------------------------------------------------------------------

def scrape_url(fetcher: Fetcher, url: str) -> dict:
    html = fetcher.get(url)
    return parse_song_html(html, url)


def slug_of(url: str) -> str:
    return url.rstrip("/").split("/")[-1]


def scrape_one(slug_or_url: str):
    url = slug_or_url if slug_or_url.startswith("http") else f"{BASE}/{slug_or_url}"
    fetcher = Fetcher()
    try:
        data = scrape_url(fetcher, url)
    finally:
        fetcher.close()

    out_name = slug_of(url) + ".json"
    with open(out_name, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"\n✓ Kaydedildi: {out_name}  ({len(data['lines'])} satır)")


def scrape_all(out_dir: str, delay: float, limit: int | None):
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    error_log = out_path / "_errors.log"

    fetcher = Fetcher()
    try:
        print("Sitemap taranıyor, şarkı listesi çıkarılıyor...")
        urls = get_all_song_urls(fetcher)
        print(f"Toplam {len(urls)} şarkı bulundu.")
        if limit:
            urls = urls[:limit]

        done, skipped, failed = 0, 0, 0
        for i, url in enumerate(urls, 1):
            slug = slug_of(url)
            out_file = out_path / f"{slug}.json"
            if out_file.exists():
                skipped += 1
                continue

            try:
                data = scrape_url(fetcher, url)
                with open(out_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                done += 1
                print(f"[{i}/{len(urls)}] ✓ {slug} ({len(data['lines'])} satır)")
            except Exception as e:
                failed += 1
                print(f"[{i}/{len(urls)}] ✗ {slug}: {e}")
                with open(error_log, "a", encoding="utf-8") as f:
                    f.write(f"{url}\t{e}\n")

            time.sleep(delay + random.uniform(0, 0.4))

        print(f"\nBitti. yeni={done} atlanan(zaten var)={skipped} hata={failed}")
        if failed:
            print(f"Hatalar için: {error_log}")
    finally:
        fetcher.close()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="akorlar.com scraper")
    parser.add_argument("slug", nargs="?", help="tek şarkı: slug ya da url")
    parser.add_argument("--all", action="store_true", help="sitedeki tüm şarkıları indir")
    parser.add_argument("--out", default="akorlar_data", help="toplu indirme çıktı klasörü")
    parser.add_argument("--delay", type=float, default=1.2, help="şarkı başına gecikme (sn)")
    parser.add_argument("--limit", type=int, default=None, help="toplu indirmede ilk N şarkıyla sınırla")
    args = parser.parse_args()

    if args.all:
        scrape_all(args.out, args.delay, args.limit)
    elif args.slug:
        scrape_one(args.slug)
    else:
        parser.print_help()
        sys.exit(1)
