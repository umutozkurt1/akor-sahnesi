# Akor Sahnesi

akorlar.com'daki şarkı akorlarını; senkronize sözler ve YouTube video eşleşmesiyle
birleştirip tek bir sahne ekranında sunan web uygulaması. Gitar/piyano çalarken
akorları, sözleri ve videoyu aynı anda, zamanlamalı olarak takip etmek için.

## Özellikler

- **Akor + söz çıkarma** — akorlar.com'daki şarkı sayfalarını parse edip
  satır satır akor/söz yapısına çevirir (`Akor_Data.py`).
- **Senkronize sözler** — [LRCLIB](https://lrclib.net) üzerinden zaman damgalı
  söz bulunursa, akor sayfasındaki satırlarla otomatik hizalanır (`server.py:align`).
- **YouTube eşleştirme** — şarkı için en uygun, gömülebilir YouTube videosunu
  bulur; birden fazla aday arasından seçim yapılabilir (`/api/youtube-alts`).
- **Önbellek + bulut senkronu** — akorlar.com Cloudflare korumalı olduğundan
  canlı çekim yerelde (Mac) yapılır, sonuç `cache/`e yazılır ve
  `sync_to_cloud.py` ile bulut sunucuya (Render) senkronlanır.

## Mimari

```
Akor_Data.py     akorlar.com scraper + parser (Playwright ile Cloudflare çözümü)
server.py        Flask sunucusu: /api/song, /api/search, /api/youtube-alts, /api/admin/sync-song
app.html         Tek sayfalık arayüz (sahne ekranı)
sync_to_cloud.py Yerel önbelleği bulut sunucuya gönderen yardımcı script
cache/           Önceden çekilmiş şarkıların JSON önbelleği
Dockerfile       Render gibi bulut ortamları için deploy imajı
```

Tarayıcı CORS kısıtlaması yüzünden dış sitelere (akorlar.com, YouTube, LRCLIB)
doğrudan istek atamaz; bu yüzden tüm dış istekler sunucu tarafında (`server.py`)
yapılıp arayüze tek bir birleşik JSON olarak sunulur.

## Kurulum

```bash
pip install -r requirements.txt
python3 -m playwright install chromium
```

## Çalıştırma

```bash
python3 server.py
```

Sunucu varsayılan olarak `8765` portunda açılır → `http://127.0.0.1:8765`.

### Opsiyonel ortam değişkenleri

| Değişken       | Açıklama                                                                 |
|----------------|---------------------------------------------------------------------------|
| `YT_API_KEY`   | YouTube Data API anahtarı — verilirse video eşleştirme daha isabetli olur |
| `ADMIN_TOKEN`  | `/api/admin/sync-song` endpoint'ini korur, bulut senkronu için gerekir     |
| `LIVE_SCRAPE`  | `0` verilirse canlı çekim kapanır, sadece önbellekten sunulur (bulut modu) |
| `PORT`         | Dinlenecek port (Render gibi ortamlarda otomatik ayarlanır)               |

## Bulut senkronu

Cloudflare koruması yüzünden bulut sunucu akorlar.com'a doğrudan erişemez.
Akış:

1. Yerelde (Mac, ev IP'siyle) şarkı çekilip `cache/`e kaydedilir.
2. `sync_to_cloud.py` bu önbelleği `ADMIN_TOKEN` ile buluta gönderir:

```bash
export CLOUD_URL="https://<render-uygulaman>.onrender.com"
export ADMIN_TOKEN="..."   # server.py'daki ile aynı olmalı
python3 sync_to_cloud.py
```

3. Bulut sunucu `LIVE_SCRAPE=0` ile sadece kendisine senkronlanan önbellekten sunar.

## Toplu akor çekimi

Sitedeki tüm şarkıları (~15.770) kaldığı yerden devam ederek indirmek için:

```bash
python3 Akor_Data.py --all --out cache --delay 1.2
```

## Docker

```bash
docker build -t akor-sahnesi .
docker run -p 8765:8765 -e ADMIN_TOKEN=... akor-sahnesi
```
