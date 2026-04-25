"""
collect_news.py
Tüm RSS beslemelerini paralel olarak çeker, son 24 saatlik haberleri filtreler,
deduplikasyon yapar ve sonucu JSON olarak kaydeder.
"""

import feedparser
import json
import re
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ----- Ayarlar -----
HOURS_BACK = 24          # Kaç saat geriye bakacağız
TIMEOUT_SECONDS = 15     # Bir beslemeyi en fazla kaç saniye bekleyeceğiz
MAX_WORKERS = 20         # Aynı anda kaç besleme çekilsin
FEEDS_FILE = "feeds.json"
OUTPUT_FILE = "collected_news.json"


def clean_text(text):
    """HTML etiketlerini ve fazla boşlukları temizler"""
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', '', text)         # HTML tag'leri
    text = re.sub(r'\s+', ' ', text)             # Çoklu boşluklar
    text = text.replace('&nbsp;', ' ').replace('&amp;', '&')
    text = text.replace('&quot;', '"').replace('&#039;', "'")
    return text.strip()


def normalize_title(title):
    """Başlığı deduplikasyon için normalize eder"""
    title = title.lower()
    title = re.sub(r'[^\w\s]', '', title)        # Noktalama
    title = re.sub(r'\s+', ' ', title).strip()
    return title


def fetch_feed(category, source):
    """Tek bir beslemeyi çeker, son 24 saatlik haberleri döner"""
    name = source["name"]
    url = source["url"]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_BACK)
    
    try:
        # feedparser'a timeout vermek için socket modülünü kullanıyoruz
        import socket
        socket.setdefaulttimeout(TIMEOUT_SECONDS)
        
        feed = feedparser.parse(url)
        
        if not feed.entries:
            return {"name": name, "url": url, "category": category,
                    "status": "empty", "items": []}
        
        items = []
        for entry in feed.entries:
            pub_parsed = entry.get('published_parsed') or entry.get('updated_parsed')
            
            # Tarih yoksa son 24 saatte var sayalım (varsayım)
            if pub_parsed:
                pub_date = datetime(*pub_parsed[:6], tzinfo=timezone.utc)
                if pub_date < cutoff:
                    continue
                pub_iso = pub_date.isoformat()
            else:
                pub_iso = None
            
            title = clean_text(entry.get('title', ''))
            summary = clean_text(entry.get('summary', ''))[:300]  # En fazla 300 karakter
            link = entry.get('link', '')
            
            if not title:
                continue
            
            items.append({
                "title": title,
                "summary": summary,
                "link": link,
                "published": pub_iso,
                "source": name,
                "category": category
            })
        
        return {"name": name, "url": url, "category": category,
                "status": "ok", "items": items}
    
    except Exception as e:
        return {"name": name, "url": url, "category": category,
                "status": "error", "error": str(e)[:100], "items": []}


def deduplicate(items):
    """Aynı başlığa sahip haberleri birleştirir, ilk gördüğünü tutar"""
    seen = {}
    for item in items:
        key = normalize_title(item["title"])
        if not key or len(key) < 10:  # Çok kısa başlıkları at
            continue
        if key not in seen:
            seen[key] = item
            seen[key]["also_in"] = []
        else:
            # Aynı haberin başka kaynaklarda olduğunu not et
            if item["source"] not in seen[key]["also_in"]:
                seen[key]["also_in"].append(item["source"])
    return list(seen.values())


def main():
    # Beslemeleri yükle
    feeds_path = Path(FEEDS_FILE)
    if not feeds_path.exists():
        print(f"❌ {FEEDS_FILE} bulunamadı.")
        return
    
    with open(feeds_path, encoding="utf-8") as f:
        feeds_data = json.load(f)
    
    # Tüm beslemeleri düz listeye çevir
    all_sources = []
    for category, sources in feeds_data.items():
        for source in sources:
            all_sources.append((category, source))
    
    total = len(all_sources)
    print(f"🔄 {total} besleme paralel olarak çekiliyor (timeout: {TIMEOUT_SECONDS}s)\n")
    
    results = []
    completed = 0
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(fetch_feed, cat, src): (cat, src)
                   for cat, src in all_sources}
        
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            completed += 1
            
            # İlerleme göster
            status_icon = {"ok": "✓", "empty": "○", "error": "✗"}[result["status"]]
            print(f"  [{completed}/{total}] {status_icon} {result['name']:<40} "
                  f"({len(result['items'])} haber)")
    
    # İstatistikler
    ok = sum(1 for r in results if r["status"] == "ok")
    empty = sum(1 for r in results if r["status"] == "empty")
    error = sum(1 for r in results if r["status"] == "error")
    
    print(f"\n📊 Besleme istatistikleri:")
    print(f"  Başarılı: {ok}")
    print(f"  Boş (24 saatte haber yok): {empty}")
    print(f"  Hatalı: {error}")
    
    # Hatalı olanları göster
    if error > 0:
        print(f"\n⚠️  Hatalı beslemeler:")
        for r in results:
            if r["status"] == "error":
                print(f"  - {r['name']}: {r.get('error', '?')}")
    
    # Tüm haberleri birleştir
    all_items = []
    for r in results:
        all_items.extend(r["items"])
    
    print(f"\n📰 Toplam haber: {len(all_items)}")
    
    # Deduplikasyon
    unique_items = deduplicate(all_items)
    duplicate_count = len(all_items) - len(unique_items)
    print(f"🔁 Tekrar eleme sonrası: {len(unique_items)} (kaldırılan: {duplicate_count})")
    
    # Kategori dağılımı
    cat_dist = {}
    for item in unique_items:
        cat_dist[item["category"]] = cat_dist.get(item["category"], 0) + 1
    print(f"\n📁 Kategori dağılımı:")
    for cat, count in sorted(cat_dist.items(), key=lambda x: -x[1]):
        print(f"  {cat}: {count}")
    
    # Kaydet
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stats": {
            "feeds_total": total,
            "feeds_ok": ok,
            "feeds_empty": empty,
            "feeds_error": error,
            "items_raw": len(all_items),
            "items_unique": len(unique_items),
        },
        "category_distribution": cat_dist,
        "items": unique_items
    }
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    
    print(f"\n💾 Kaydedildi: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
