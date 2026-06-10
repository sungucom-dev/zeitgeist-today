"""
curate.py
Günün analizini okur, Gemini'den sanat eseri ve müzik önerisi ister.
Son 60 günün arşivini tarar, daha önce seçilmiş eser/parçaları engeller.
Sanat eseri için Wikipedia'da kendi sayfası ve görseli olduğunu doğrular.
Küratör metni bu dosyada üretilmez — run_daily.py'de Spotify sonrası üretilir.
Sonucu curation.json olarak kaydeder.
"""

import os
import json
import time
import requests
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

INPUT_FILE = "day_analysis.json"
OUTPUT_FILE = "curation.json"
ARCHIVE_DIR = Path("archive")
MODEL_NAME = "gemini-3.1-flash-lite"
USER_AGENT = "ZeitgeistToday/1.0 (https://sungu.com; personal project)"
MAX_ATTEMPTS = 3
HISTORY_DAYS = 60

MAX_RETRIES = 6
RETRY_DELAYS = [30, 60, 120, 240, 480, 600]

HEADERS = {"User-Agent": USER_AGENT}


# ====================== Arşiv ======================

def load_recent_history():
    if not ARCHIVE_DIR.exists():
        return [], []
    files = sorted(ARCHIVE_DIR.glob("*.json"), reverse=True)[:HISTORY_DAYS]
    artworks, musics = [], []
    for f in files:
        try:
            with open(f, encoding="utf-8") as file:
                data = json.load(file)
            artwork = data.get("artwork")
            if artwork and artwork.get("title"):
                artworks.append({"date": data.get("date"), "title": artwork.get("title"), "artist": artwork.get("artist")})
            music = data.get("music")
            if music and music.get("title"):
                musics.append({"date": data.get("date"), "title": music.get("title"), "artists": music.get("artists")})
        except Exception:
            continue
    return artworks, musics


def format_history_block(artworks, musics):
    if not artworks and not musics:
        return ""
    block = "\n\n=== SON GÜNLERDE SEÇİLEN ESERLER VE MÜZİKLER ===\n"
    block += "Aşağıdaki seçimler zaten yapıldı, BUNLARDAN HİÇBİRİNİ TEKRAR ÖNERME:\n\n"
    if artworks:
        block += "Eserler:\n"
        for a in artworks:
            block += f"  - \"{a['title']}\" / {a['artist']} ({a.get('date', '?')})\n"
    if musics:
        block += "\nMüzikler:\n"
        for m in musics:
            block += f"  - \"{m['title']}\" / {m['artists']} ({m.get('date', '?')})\n"
    block += "\nBu listede olan hiçbir esere veya parçaya geri dönme.\n"
    return block


# ====================== Wikipedia ======================

def wiki_search(query, lang="en", limit=5):
    url = f"https://{lang}.wikipedia.org/w/api.php"
    params = {"action": "query", "format": "json", "list": "search", "srsearch": query, "srlimit": limit}
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=15)
        r.raise_for_status()
        return r.json().get("query", {}).get("search", [])
    except Exception as e:
        print(f"    ⚠️  Arama hatası: {e}")
        return []


def wiki_page_summary(title, lang="en"):
    url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{quote(title)}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
        return {
            "title": data.get("title"),
            "extract": data.get("extract"),
            "page_url": data.get("content_urls", {}).get("desktop", {}).get("page"),
            "thumbnail": data.get("thumbnail", {}).get("source"),
            "originalimage": data.get("originalimage", {}).get("source"),
            "description": data.get("description"),
            "lang": lang,
        }
    except Exception:
        return None


def wiki_page_categories(title, lang="en"):
    url = f"https://{lang}.wikipedia.org/w/api.php"
    params = {"action": "query", "format": "json", "titles": title, "prop": "categories", "cllimit": 50}
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=15)
        r.raise_for_status()
        pages = r.json().get("query", {}).get("pages", {})
        for page in pages.values():
            return [c.get("title", "") for c in page.get("categories", [])]
    except Exception:
        pass
    return []


def is_specific_artwork_page(title, categories, artist):
    if title.lower().strip() == artist.lower().strip():
        return False
    cats_lower = " ".join(categories).lower()
    artwork_indicators = ["paintings by", "sculptures by", "works by", "artworks", "individual paintings",
                          "individual sculptures", "20th-century paintings", "21st-century paintings",
                          "19th-century paintings", "18th-century paintings", "performance art works",
                          "installations", "lithographs", "engravings", "drawings by"]
    has_artwork_cat = any(ind in cats_lower for ind in artwork_indicators)
    person_indicators = ["births", "deaths", "living people", "alumni", "people from", "graduates of"]
    has_person_cat = any(ind in cats_lower for ind in person_indicators)
    return has_artwork_cat and not has_person_cat


def find_artwork_page_strict(title, artist):
    queries = [f'"{title}"', f'"{title}" {artist}', f'{title} painting', f'{title} artwork']
    seen_titles = set()
    for query in queries:
        results = wiki_search(query, lang="en", limit=5)
        for r in results:
            r_title = r.get("title", "")
            if r_title in seen_titles:
                continue
            seen_titles.add(r_title)
            r_lower = r_title.lower()
            if any(bad in r_lower for bad in ["list of", "video game", "discography", "filmography", "bibliography"]):
                continue
            if r_title.lower().strip() == artist.lower().strip():
                continue
            cats = wiki_page_categories(r_title, lang="en")
            if not is_specific_artwork_page(r_title, cats, artist):
                continue
            title_words = [w for w in title.lower().split() if len(w) > 2]
            title_match = sum(1 for w in title_words if w in r_lower)
            if title_match < max(1, len(title_words) // 2):
                continue
            # Başlık uyuşuyor mu?
            found_lower = r_title.lower()
            proposed_lower = title.lower()
            title_words_check = [w for w in proposed_lower.split() if len(w) > 3]
            match = any(w in found_lower for w in title_words_check)
            if not match:
                print(f"   ⚠️  Başlık uyuşmuyor: '{r_title}' ≠ '{title}', atlanıyor")
                continue
            summary = wiki_page_summary(r_title, lang="en")
            if summary and summary.get("extract") and summary.get("originalimage"):
                return summary
    return None


def find_artist_page(artist):
    for query in [f"{artist} artist", f"{artist} painter", artist]:
        for lang in ["en", "tr"]:
            results = wiki_search(query, lang=lang, limit=3)
            for r in results:
                r_title = r.get("title", "")
                if artist.lower() not in r_title.lower():
                    continue
                cats = wiki_page_categories(r_title, lang=lang)
                cats_lower = " ".join(cats).lower()
                if any(k in cats_lower for k in ["artist", "painter", "sculptor", "sanatçı", "ressam"]):
                    summary = wiki_page_summary(r_title, lang=lang)
                    if summary and summary.get("extract"):
                        return summary
    return None


# ====================== Gemini ======================

def build_prompt(analysis, history_block):
    clusters_summary = ""
    for c in analysis.get("clusters", [])[:15]:
        clusters_summary += f"- [{c.get('importance', '?')}/10] {c.get('title', '')}: {c.get('summary', '')}\n"
    today = datetime.now().strftime("%d %B %Y")

    return f"""Sen deneyimli bir küratörsün. Görevin: bugünün haberlerinin yarattığı atmosfere uygun BİR sanat eseri ve BİR müzik parçası seçmek.

BUGÜN: {today}
GÜNÜN RUHU: {analysis.get('day_mood', '')}
BASKIN DUYGU: {analysis.get('dominant_emotion', '')}
ANAHTAR TEMALAR: {', '.join(analysis.get('key_themes', []))}

ÖNE ÇIKAN HABER KÜMELERİ:
{clusters_summary}
{history_block}
ÖNEMLİ KISITLAR:
- Sanat eseri: Wikipedia'da KENDİ AYRI SAYFASI olan kanonik bir eser olmalı. Film, kitap, müzik albümü ÖNERME — sadece görsel sanat (resim, heykel, fotoğraf, enstalasyon, video art).
- Müzik: Spotify'da bulunabilen gerçek bir parça olmalı. Sanatçı ve parça adı kesin olmalı.
- Geçmiş seçimleri TEKRARLAMA.

CEVABINI MUTLAKA AŞAĞIDAKİ JSON FORMATINDA VER, başka hiçbir metin ekleme:

{{
  "artwork": {{
    "title": "Eserin tam adı (Wikipedia'daki haliyle, İngilizce tercih)",
    "artist": "Sanatçı tam adı",
    "year": "yapım yılı",
    "medium": "tekniği",
    "form": "resim/heykel/fotoğraf/enstalasyon/video",
    "location": "şu an nerede sergileniyor",
    "description": "Türkçe 2-3 cümlelik tarif"
  }},
  "music": {{
    "title": "Parçanın tam adı",
    "artist": "Sanatçı/grup tam adı",
    "album": "Albüm adı",
    "year": "çıkış yılı",
    "genre": "tür",
    "mood": "atmosfer (3-5 sıfat)",
    "spotify_search": "Spotify aramak için query"
  }}
}}
"""


def build_retry_prompt(analysis, previous_attempts, history_block):
    failed_list = "\n".join([f"{i+1}. '{a['title']}' - {a['artist']}" for i, a in enumerate(previous_attempts)])
    return f"""Daha önce şu sanat eserlerini önerdin ama kabul edilmedi:

{failed_list}

Yeni bir öneri yap. Wikipedia'da kendi ayrı sayfası olan, görsel sanat eseri (resim/heykel/enstalasyon) seç. Film önerme.

Atmosfer: {analysis.get('day_mood', '')}
Baskın duygu: {analysis.get('dominant_emotion', '')}
{history_block}
Aynı JSON formatında cevap ver (curator_statement olmadan).
"""


def call_gemini_with_retry(client, prompt):
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.9,
                    max_output_tokens=8192,
                ),
            )
            result = json.loads(response.text)
            if isinstance(result, list):
                result = result[0] if result else {}
            return result
        except Exception as e:
            error_str = str(e)
            last_error = e
            is_retryable = any(x in error_str for x in ["503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "timeout", "deadline"])
            if isinstance(e, json.JSONDecodeError):
                raise
            if not is_retryable:
                raise
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_DELAYS[attempt]
                print(f"   ⏳ Gemini hatası (deneme {attempt+1}/{MAX_RETRIES}): {error_str[:100]}")
                print(f"      {delay} saniye bekleniyor...")
                time.sleep(delay)
            else:
                print(f"   ❌ Son denemede de başarısız.")
    raise last_error


def is_in_history(title, artist, history_artworks):
    title_lower = title.lower().strip()
    artist_lower = artist.lower().strip()
    for h in history_artworks:
        h_title = h.get("title", "").lower().strip()
        h_artist = h.get("artist", "").lower().strip()
        if title_lower == h_title and artist_lower == h_artist:
            return True
        if h_title and title_lower == h_title:
            return True
    return False


# ====================== Ana Akış ======================

def main():
    if not Path(INPUT_FILE).exists():
        print(f"❌ {INPUT_FILE} bulunamadı.")
        return

    with open(INPUT_FILE, encoding="utf-8") as f:
        data = json.load(f)
    analysis = data["analysis"]

    print(f"🎭 Günün Ruhu: {analysis.get('day_mood', '')[:120]}...")
    print(f"💫 Baskın Duygu: {analysis.get('dominant_emotion', '?')}")

    history_artworks, history_musics = load_recent_history()
    print(f"\n📚 Arşivden {len(history_artworks)} eser, {len(history_musics)} müzik bulundu (son {HISTORY_DAYS} gün)")
    history_block = format_history_block(history_artworks, history_musics)

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("❌ GEMINI_API_KEY bulunamadı")
        return

    client = genai.Client(api_key=api_key)

    failed_attempts = []
    final_curation = None
    artwork_page = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(f"\n{'='*60}")
        print(f"🔄 DENEME {attempt}/{MAX_ATTEMPTS}")
        print(f"{'='*60}")

        prompt = build_prompt(analysis, history_block) if attempt == 1 else build_retry_prompt(analysis, failed_attempts, history_block)

        try:
            curation = call_gemini_with_retry(client, prompt)
        except Exception as e:
            print(f"❌ Gemini hatası: {e}")
            return

        artwork = curation.get("artwork", {})
        if not artwork:
            print(f"   ⚠️  Gemini boş artwork döndürdü, tekrar deneniyor...")
            failed_attempts.append({"title": "?", "artist": "?"})
            continue

        title = artwork.get("title", "")
        artist = artwork.get("artist", "")

        if not title or not artist:
            print(f"   ⚠️  Başlık veya sanatçı boş, tekrar deneniyor...")
            failed_attempts.append({"title": title or "?", "artist": artist or "?"})
            continue

        print(f"\n🎨 Önerilen: '{title}' - {artist}")
        print(f"   {artwork.get('year', '?')} | {artwork.get('form', '?')} | {artwork.get('location', '?')}")

        if is_in_history(title, artist, history_artworks):
            print(f"   ⚠️  Bu eser arşivde zaten var, yeni öneri istetiliyor...")
            failed_attempts.append({"title": title, "artist": artist})
            continue

        print(f"\n🔍 Wikipedia'da eser sayfası aranıyor...")
        artwork_page = find_artwork_page_strict(title, artist)

        if artwork_page:
            print(f"   ✓ EŞSİZ ESER SAYFASI BULUNDU!")
            print(f"     Başlık: {artwork_page['title']}")
            print(f"     URL: {artwork_page['page_url']}")
            print(f"     Görsel: {artwork_page['originalimage']}")
            final_curation = curation
            break
        else:
            print(f"   ✗ Eserin kendi sayfası bulunamadı")
            failed_attempts.append({"title": title, "artist": artist})

    if not final_curation:
        print(f"\n⚠️  {MAX_ATTEMPTS} denemede de doğrulanmış yeni eser bulunamadı.")
        final_curation = curation

    artist_name = final_curation.get("artwork", {}).get("artist", "")
    print(f"\n🔍 Sanatçı sayfası aranıyor: {artist_name}")
    artist_page = find_artist_page(artist_name)
    if artist_page:
        print(f"   ✓ {artist_page['page_url']}")

    music = final_curation.get("music", {})
    print("\n" + "="*60)
    print("🎵 MÜZİK")
    print("="*60)
    print(f"  Parça: {music.get('title')} - {music.get('artist')}")
    print(f"  Albüm: {music.get('album')} ({music.get('year')})")
    print(f"  Spotify arama: {music.get('spotify_search')}")

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model_used": MODEL_NAME,
        "attempts": len(failed_attempts) + 1,
        "failed_attempts": failed_attempts,
        "verified": bool(artwork_page),
        "history_count": len(history_artworks),
        "day_mood": analysis.get("day_mood"),
        "dominant_emotion": analysis.get("dominant_emotion"),
        "curation": final_curation,
        "artwork_page": artwork_page,
        "artist_page": artist_page,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n💾 Kaydedildi: {OUTPUT_FILE}")
    print(f"📊 Toplam deneme: {len(failed_attempts) + 1}")


if __name__ == "__main__":
    main()
