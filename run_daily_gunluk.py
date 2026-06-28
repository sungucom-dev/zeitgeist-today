"""
run_daily.py
Pipeline:
1. RSS toplama
2. Haber analizi (Gemini)
3. Sanat eseri + müzik önerisi (Gemini + Wikipedia)
4. Spotify arama
5. Küratör metni (Gemini — gerçek Spotify parçasına göre)
6. Hava durumu
7. Final today.json
"""

import subprocess
import json
import sys
import os
import time
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

STEPS = [
    ("collect_news.py",      "RSS toplama"),
    ("analyze_news.py",      "Haber analizi (Gemini)"),
    ("curate.py",            "Sanat & müzik önerisi (Gemini + Wikipedia)"),
    ("find_spotify_track.py","Spotify parça arama"),
]

ARCHIVE_DIR = Path("archive")
TR_TZ = timezone(timedelta(hours=3))
IZMIR_LAT = 38.4192
IZMIR_LON = 27.1287

CURATOR_MODEL = "gemini-2.5-flash"
MAX_RETRIES = 4
RETRY_DELAYS = [30, 60, 120, 240]


def run_step(script_name, description):
    print(f"\n{'='*70}")
    print(f"▶  {description}")
    print(f"   ({script_name})")
    print(f"{'='*70}")
    result = subprocess.run([sys.executable, script_name], capture_output=False)
    return result.returncode == 0


def generate_curator_statement(curation_data, spotify_data, analysis_data):
    """Spotify'da bulunan gerçek parçaya göre küratör metni üret."""
    print(f"\n{'='*70}")
    print(f"▶  Küratör metni üretiliyor (Gemini)")
    print(f"{'='*70}")

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("   ❌ GEMINI_API_KEY bulunamadı")
        return None

    artwork = curation_data.get("curation", {}).get("artwork", {})
    artwork_page = curation_data.get("artwork_page", {})
    music_suggestion = spotify_data.get("music_suggestion", {})
    spotify_track = spotify_data.get("spotify_track", {})
    analysis = analysis_data.get("analysis", {})

    # Gerçekte çalacak parça
    real_title = spotify_track.get("name", music_suggestion.get("title", ""))
    real_artist = spotify_track.get("artists", music_suggestion.get("artist", ""))
    real_album = spotify_track.get("album", music_suggestion.get("album", ""))

    prompt = f"""Sen deneyimli bir sanat ve müzik küratörüsün. Aşağıdaki bilgilere dayanarak Türkçe, edebi ve düşünceli bir küratör yorumu yaz.

GÜNÜN RUHU: {analysis.get('day_mood', '')}
BASKIN DUYGU: {analysis.get('dominant_emotion', '')}
ANAHTAR TEMALAR: {', '.join(analysis.get('key_themes', []))}

SEÇİLEN SANAT ESERİ:
- Eser: {artwork.get('title', '')} ({artwork.get('year', '')})
- Sanatçı: {artwork.get('artist', '')}
- Teknik: {artwork.get('medium', '')}
- Açıklama: {artwork_page.get('extract', artwork.get('description', ''))[:300] if artwork_page else artwork.get('description', '')}

ÇALACAK MÜZİK PARÇASI:
- Parça: {real_title}
- Sanatçı: {real_artist}
- Albüm: {real_album}
- Atmosfer: {music_suggestion.get('mood', '')}

Küratör yorumu şunları yapmalı:
- Günün haberlerinin yarattığı atmosferi yansıtmalı
- Sanat eserini ve müzik parçasını birbirine bağlamalı
- Edebi ve düşünceli bir dil kullanmalı
- 4-6 cümle olmalı
- SADECE küratör metnini yaz, başka hiçbir şey ekleme
- Tırnak işareti (" veya ') kullanma, düz metin yaz
- Eser ve parça adlarını parantez olmadan yaz
"""

    client = genai.Client(api_key=api_key)
    last_error = None

    for attempt in range(MAX_RETRIES):
        try:
            response = client.models.generate_content(
                model=CURATOR_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.8,
                    max_output_tokens=2048,
                ),
            )
            text = response.text.strip()
            print(f"   ✓ Küratör metni üretildi ({len(text)} karakter)")
            print(f"\n📝 Küratör Metni:\n{text}\n")
            return text
        except Exception as e:
            error_str = str(e)
            last_error = e
            is_retryable = any(x in error_str for x in ["503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED"])
            if not is_retryable:
                break
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_DELAYS[attempt]
                print(f"   ⏳ Gemini hatası (deneme {attempt+1}): {error_str[:80]}, {delay}s bekleniyor...")
                time.sleep(delay)

    print(f"   ⚠️  Küratör metni üretilemedi: {last_error}")
    return None


def fetch_weather():
    print(f"\n{'='*70}")
    print(f"▶  Hava durumu (İzmir, 7 günlük)")
    print(f"{'='*70}")
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": IZMIR_LAT, "longitude": IZMIR_LON,
        "daily": "weather_code,temperature_2m_max,temperature_2m_min",
        "timezone": "Europe/Istanbul", "forecast_days": 7,
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json().get("daily", {})
        days = []
        for i, date in enumerate(data.get("time", [])):
            days.append({
                "date": date,
                "code": data.get("weather_code", [None])[i],
                "temp_max": data.get("temperature_2m_max", [None])[i],
                "temp_min": data.get("temperature_2m_min", [None])[i],
            })
        print(f"   ✓ {len(days)} günlük tahmin alındı")
        for d in days:
            print(f"     {d['date']}: kod={d['code']}, {d['temp_min']}°C - {d['temp_max']}°C")
        return {"location": "İzmir", "latitude": IZMIR_LAT, "longitude": IZMIR_LON, "days": days}
    except Exception as e:
        print(f"   ⚠️  Hava durumu hatası: {e}")
        return None


def build_today_json(weather, curator_statement):
    print(f"\n{'='*70}")
    print(f"▶  Final today.json üretiliyor")
    print(f"{'='*70}")

    import argparse

parser = argparse.ArgumentParser()
parser.add_argument('--date', type=str, default=None)
args = parser.parse_args()

if args.date:
    today = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=TR_TZ)
else:
    today = datetime.now(TR_TZ)
    today_iso = today.strftime("%Y-%m-%d")
    today_human = today.strftime("%d %B %Y")

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

    today_json = {
        "date": today_iso,
        "date_human": today_human,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stats": {
            "feeds_collected": data["collected"]["stats"]["feeds_ok"] if data["collected"] else 0,
            "news_total": data["collected"]["stats"]["items_unique"] if data["collected"] else 0,
            "news_analyzed": data["analysis"]["sampled_items"] if data["analysis"] else 0,
            "clusters": len(data["analysis"]["analysis"]["clusters"]) if data["analysis"] else 0,
        } if data["collected"] and data["analysis"] else {},
        "mood": {
            "description": data["analysis"]["analysis"].get("day_mood") if data["analysis"] else None,
            "dominant_emotion": data["analysis"]["analysis"].get("dominant_emotion") if data["analysis"] else None,
            "key_themes": data["analysis"]["analysis"].get("key_themes", []) if data["analysis"] else [],
        },
        "weather": weather,
        "clusters": [],
        "artwork": None,
        "music": None,
        "curator_statement": curator_statement,
    }

    if data["analysis"]:
        for cluster in data["analysis"]["analysis"].get("clusters", []):
            today_json["clusters"].append({
                "title": cluster.get("title"),
                "meta_category": cluster.get("meta_category"),
                "summary": cluster.get("summary"),
                "importance": cluster.get("importance"),
                "story_count": cluster.get("story_count"),
                "stories": cluster.get("stories", [])[:5],
            })

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
        }

    with open("today.json", "w", encoding="utf-8") as f:
        json.dump(today_json, f, ensure_ascii=False, indent=2)
    print(f"   ✓ today.json üretildi")

    ARCHIVE_DIR.mkdir(exist_ok=True)
    archive_path = ARCHIVE_DIR / f"{today_iso}.json"
    with open(archive_path, "w", encoding="utf-8") as f:
        json.dump(today_json, f, ensure_ascii=False, indent=2)
    print(f"   ✓ Arşive kopyalandı: {archive_path}")

    return today_json


def print_summary(today_json):
    print(f"\n{'='*70}")
    print(f"🎉 GÜNÜN ÖZETİ — {today_json['date_human']}")
    print(f"{'='*70}\n")
    stats = today_json.get("stats", {})
    print(f"📊 {stats.get('feeds_collected', 0)} kaynaktan {stats.get('news_total', 0)} haber, {stats.get('clusters', 0)} kümeye indirgendi\n")
    mood = today_json.get("mood", {})
    print(f"🎭 Günün Ruhu:\n   {mood.get('description', '?')}\n")
    weather = today_json.get("weather")
    if weather and weather.get("days"):
        first = weather["days"][0]
        print(f"🌤  Bugün İzmir: {first.get('temp_min')}°C - {first.get('temp_max')}°C\n")
    artwork = today_json.get("artwork", {})
    if artwork:
        verified = "✓" if artwork.get("verified") else "✗"
        print(f"🖼️  Eser ({verified} doğrulandı):\n   {artwork.get('title')} — {artwork.get('artist')} ({artwork.get('year')})")
    music = today_json.get("music", {})
    if music:
        print(f"\n🎵 Müzik:\n   {music.get('title')} — {music.get('artists')}")
    print(f"\n{'='*70}\n")


def main():
    start_time = datetime.now()

    for f in ["day_analysis.json", "curation.json", "spotify_result.json"]:
        Path(f).unlink(missing_ok=True)

    print(f"🚀 ZeitgeistToday günlük pipeline başlıyor")
    print(f"   {datetime.now(TR_TZ).strftime('%d %B %Y, %H:%M:%S')} TSİ")

    expected_outputs = {
        "collect_news.py":       "collected_news.json",
        "analyze_news.py":       "day_analysis.json",
        "curate.py":             "curation.json",
        "find_spotify_track.py": "spotify_result.json",
    }

    for script_name, description in STEPS:
        if not Path(script_name).exists():
            print(f"❌ {script_name} bulunamadı, durduruluyor.")
            sys.exit(1)
        success = run_step(script_name, description)
        expected = expected_outputs.get(script_name)
        if expected and not Path(expected).exists():
            print(f"\n❌ {description} başarısız: {expected} oluşmadı.")
            sys.exit(1)
        if not success:
            print(f"\n❌ {description} başarısız.")
            sys.exit(1)

    # Küratör metnini Spotify sonrası üret
    curator_statement = None
    try:
        with open("curation.json", encoding="utf-8") as f:
            curation_data = json.load(f)
        with open("spotify_result.json", encoding="utf-8") as f:
            spotify_data = json.load(f)
        with open("day_analysis.json", encoding="utf-8") as f:
            analysis_data = json.load(f)
        curator_statement = generate_curator_statement(curation_data, spotify_data, analysis_data)
    except Exception as e:
        print(f"   ⚠️  Küratör metni üretilirken hata: {e}")

    weather = fetch_weather()
    today_json = build_today_json(weather, curator_statement)
    print_summary(today_json)

    elapsed = datetime.now() - start_time
    print(f"⏱  Toplam süre: {elapsed.total_seconds():.1f} saniye")


if __name__ == "__main__":
    main()
