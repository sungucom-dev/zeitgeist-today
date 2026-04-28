"""
run_daily.py
Günün tüm pipeline'ını sırayla çalıştırır:
1. RSS toplama (collect_news.py)
2. Haber analizi (analyze_news.py)
3. Küratöryel öneri + Wikipedia doğrulama (curate.py)
4. Spotify arama (find_spotify_track.py)
5. Final today.json üretimi + arşivleme

Tek komutla: python run_daily.py
"""

import subprocess
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Pipeline adımları sırasıyla
STEPS = [
    ("collect_news.py", "RSS toplama"),
    ("analyze_news.py", "Haber analizi (Gemini)"),
    ("curate.py", "Sanat & müzik küratörlüğü (Gemini + Wikipedia)"),
    ("find_spotify_track.py", "Spotify parça arama"),
]

ARCHIVE_DIR = Path("archive")


def run_step(script_name, description):
    """Bir Python scriptini çalıştırır, başarısızsa False döner."""
    print(f"\n{'='*70}")
    print(f"▶  {description}")
    print(f"   ({script_name})")
    print(f"{'='*70}")
    
    result = subprocess.run(
        [sys.executable, script_name],
        capture_output=False,
    )
    return result.returncode == 0


def build_today_json():
    """Tüm ara JSON'ları birleştirip tek 'today.json' üretir.
    Bu dosya WordPress sayfasının okuyacağı asıl ürün."""
    
    print(f"\n{'='*70}")
    print(f"▶  Final today.json üretiliyor")
    print(f"{'='*70}")
    
    today = datetime.now(timezone.utc) + timedelta(hours=3)  # TSİ
    today_iso = today.strftime("%Y-%m-%d")
    today_human = today.strftime("%d %B %Y")
    
    # JSON'ları yükle
    files_to_load = {
        "collected": "collected_news.json",
        "analysis": "day_analysis.json",
        "curation": "curation.json",
        "spotify": "spotify_result.json",
    }
    
    data = {}
    for key, filename in files_to_load.items():
        if not Path(filename).exists():
            print(f"   ⚠️  {filename} bulunamadı, atlanıyor")
            data[key] = None
            continue
        with open(filename, encoding="utf-8") as f:
            data[key] = json.load(f)
    
    # Final yapılandırılmış çıktı
    today_json = {
        "date": today_iso,
        "date_human": today_human,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        
        # Üst düzey sayısal istatistikler
        "stats": {
            "feeds_collected": data["collected"]["stats"]["feeds_ok"] if data["collected"] else 0,
            "news_total": data["collected"]["stats"]["items_unique"] if data["collected"] else 0,
            "news_analyzed": data["analysis"]["sampled_items"] if data["analysis"] else 0,
            "clusters": len(data["analysis"]["analysis"]["clusters"]) if data["analysis"] else 0,
        } if data["collected"] and data["analysis"] else {},
        
        # Günün ruhu
        "mood": {
            "description": data["analysis"]["analysis"].get("day_mood") if data["analysis"] else None,
            "dominant_emotion": data["analysis"]["analysis"].get("dominant_emotion") if data["analysis"] else None,
            "key_themes": data["analysis"]["analysis"].get("key_themes", []) if data["analysis"] else [],
        },
        
        # Kümeler (sayfada gösterilecek konu listesi)
        "clusters": [],
        
        # Sanat eseri
        "artwork": None,
        
        # Müzik
        "music": None,
        
        # Küratör notu
        "curator_statement": data["curation"]["curation"].get("curator_statement") if data["curation"] else None,
    }
    
    # Kümeleri zenginleştir
    if data["analysis"]:
        for cluster in data["analysis"]["analysis"].get("clusters", []):
            today_json["clusters"].append({
                "title": cluster.get("title"),
                "meta_category": cluster.get("meta_category"),
                "summary": cluster.get("summary"),
                "importance": cluster.get("importance"),
                "story_count": cluster.get("story_count"),
                "stories": cluster.get("stories", [])[:5],  # En fazla 5 örnek haber
            })
    
    # Sanat eseri
    if data["curation"]:
        artwork_sug = data["curation"]["curation"].get("artwork", {})
        artwork_page = data["curation"].get("artwork_page")
        artist_page = data["curation"].get("artist_page")
        
        today_json["artwork"] = {
            "title": artwork_sug.get("title"),
            "artist": artwork_sug.get("artist"),
            "year": artwork_sug.get("year"),
            "form": artwork_sug.get("form"),
            "medium": artwork_sug.get("medium"),
            "location": artwork_sug.get("location"),
            "description": artwork_sug.get("description"),
            "verified": data["curation"].get("verified", False),
            "wikipedia_url": artwork_page.get("page_url") if artwork_page else None,
            "wikipedia_extract": artwork_page.get("extract") if artwork_page else None,
            "image_url": artwork_page.get("originalimage") if artwork_page else None,
            "artist_wikipedia_url": artist_page.get("page_url") if artist_page else None,
        }
    
    # Müzik
    if data["spotify"] and data["spotify"].get("success"):
        track = data["spotify"].get("spotify_track", {})
        today_json["music"] = {
            "title": track.get("name"),
            "artists": track.get("artists"),
            "album": track.get("album"),
            "duration": track.get("duration"),
            "spotify_url": track.get("url"),
            "spotify_id": track.get("id"),
            "album_image": track.get("album_image"),
            "embed_html": track.get("embed_html"),
            "preview_url": track.get("preview_url"),
            "suggested_genre": data["spotify"]["music_suggestion"].get("genre"),
            "suggested_mood": data["spotify"]["music_suggestion"].get("mood"),
        }
    
    # Final JSON kaydet
    with open("today.json", "w", encoding="utf-8") as f:
        json.dump(today_json, f, ensure_ascii=False, indent=2)
    print(f"   ✓ today.json üretildi")
    
    # Arşivle
    ARCHIVE_DIR.mkdir(exist_ok=True)
    archive_path = ARCHIVE_DIR / f"{today_iso}.json"
    with open(archive_path, "w", encoding="utf-8") as f:
        json.dump(today_json, f, ensure_ascii=False, indent=2)
    print(f"   ✓ Arşive kopyalandı: {archive_path}")
    
    return today_json


def print_summary(today_json):
    """Sonu güzel bir özet yazdır."""
    print(f"\n{'='*70}")
    print(f"🎉 GÜNÜN ÖZETİ — {today_json['date_human']}")
    print(f"{'='*70}\n")
    
    stats = today_json.get("stats", {})
    print(f"📊 {stats.get('feeds_collected', 0)} kaynaktan "
          f"{stats.get('news_total', 0)} haber, "
          f"{stats.get('clusters', 0)} kümeye indirgendi\n")
    
    mood = today_json.get("mood", {})
    print(f"🎭 Günün Ruhu:")
    print(f"   {mood.get('description', '?')}\n")
    print(f"💫 Baskın Duygu: {mood.get('dominant_emotion', '?')}")
    print(f"🔑 Anahtar Temalar: {', '.join(mood.get('key_themes', []))}\n")
    
    artwork = today_json.get("artwork", {})
    if artwork:
        verified = "✓" if artwork.get("verified") else "✗"
        print(f"🖼️  Eser ({verified} doğrulandı):")
        print(f"   {artwork.get('title')} — {artwork.get('artist')} ({artwork.get('year')})")
        if artwork.get("wikipedia_url"):
            print(f"   {artwork.get('wikipedia_url')}")
    
    music = today_json.get("music", {})
    if music:
        print(f"\n🎵 Müzik:")
        print(f"   {music.get('title')} — {music.get('artists')}")
        print(f"   {music.get('spotify_url')}")
    
    print(f"\n{'='*70}\n")


def main():
    start_time = datetime.now()
    print(f"🚀 ZeitgeistToday günlük pipeline başlıyor")
    print(f"   {start_time.strftime('%d %B %Y, %H:%M:%S')}")
    
    # Adımları sırayla çalıştır
    for script_name, description in STEPS:
        if not Path(script_name).exists():
            print(f"❌ {script_name} bulunamadı, durduruluyor.")
            return
        
        success = run_step(script_name, description)
        if not success:
            print(f"\n❌ Adım başarısız: {description}")
            print(f"   Pipeline durduruldu.")
            return
    
    # Final JSON
    today_json = build_today_json()
    
    # Özet
    print_summary(today_json)
    
    elapsed = datetime.now() - start_time
    print(f"⏱  Toplam süre: {elapsed.total_seconds():.1f} saniye")


if __name__ == "__main__":
    main()
