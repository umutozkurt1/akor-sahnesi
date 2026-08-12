#!/usr/bin/env python3
"""
Mac'te önbelleğe (cache/) alınmış şarkıları bulut sunucuya (Render) gönderir.

akorlar.com Cloudflare korumalı olduğu için bulut sunucu siteye doğrudan
erişemiyor; bu yüzden şarkılar önce burada (Mac'te, ev IP'siyle) çekiliyor,
sonra bu script ile buluta kopyalanıyor. Bulut sunucu artık sadece elindeki
önbellekten sunuyor.

Kullanım:
    export CLOUD_URL="https://akor-sahnesi.onrender.com"
    export ADMIN_TOKEN="..."   # server.py'daki ile aynı olmalı
    python3 sync_to_cloud.py
"""

import json
import os
import sys
from pathlib import Path

import requests

CACHE_DIR = Path(os.environ.get("LOCAL_CACHE_DIR", Path(__file__).parent / "cache"))
CLOUD_URL = os.environ.get("CLOUD_URL", "").rstrip("/")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()


def main():
    if not CLOUD_URL or not ADMIN_TOKEN:
        print("CLOUD_URL ve ADMIN_TOKEN ortam değişkenleri gerekli.")
        sys.exit(1)

    files = sorted(CACHE_DIR.glob("*.json"))
    if not files:
        print("cache/ klasöründe gönderilecek şarkı yok.")
        return

    ok, failed = 0, 0
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"✗ {f.name}: okunamadı ({e})")
            failed += 1
            continue

        try:
            r = requests.post(
                f"{CLOUD_URL}/api/admin/sync-song",
                json=data,
                headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
                timeout=20,
            )
            if r.status_code == 200:
                print(f"✓ {f.stem}")
                ok += 1
            else:
                print(f"✗ {f.stem}: {r.status_code} {r.text[:200]}")
                failed += 1
        except Exception as e:
            print(f"✗ {f.stem}: {e}")
            failed += 1

    print(f"\nBitti. gönderildi={ok} hata={failed}")


if __name__ == "__main__":
    main()
