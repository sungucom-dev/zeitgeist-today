"""
curate.py
Günün analizini okur, Gemini'den sanat eseri ve müzik önerisi ister.
Sanat eseri için Wikipedia'da kendi sayfası ve görseli olduğunu doğrular.
Bulamazsa Gemini'ye geri besleme yapıp tekrar önerisini ister (max 3 deneme).
Sonucu curation.json olarak kaydeder.
"""

import os
import json
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
MODEL_NAME = "gemini-2.5-flash"
USER_AGENT = "ZeitgeistToday/1.0 (https://sungu.com; personal project)"
MAX_ATTEMPTS = 3

HEADERS = {"User-Agent": USER_AGENT}


# ====================== Wikipedia Yardımcıları ======================

def wiki_search(query, lang="en", limit=5):
    url = f"https://{lang}.wikipedia.org/w/api.php"
    params = {
        "action": "query", "format": "json", "list": "search",
        "srsearch": query, "srlimit": limit,
    }
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
    params = {
        "action": "query", "format": "json", "titles": title,
        "prop": "categories", "cllimit": 50,
    }
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
    """Bu sayfa gerçekten BU eserin kendi sayfası mı? Sanatçı sayfası DEĞİL.
    Sayfanın kategorilerine bakarak karar verir."""
    
    # Sayfa başlığı sadece sanatçı adıysa (örn "Julie Mehretu") -> sanatçı sayfası, eser değil
    if title.lower().strip() == artist.lower().strip():
        return False
    
    cats_lower = " ".join(categories).lower()
    
    # Eser-spesifik kategori anahtar kelimeleri
    artwork_indicators = [
        "paintings by", "sculptures by", "works by",
        "artworks", "individual paintings", "individual sculptures",
        "20th-century paintings", "21st-century paintings",
        "19th-century paintings", "18th-century paintings",
        "performance art works", "installations",
        "lithographs", "engravings", "drawings by",
    ]
    
    has_artwork_cat = any(ind in cats_lower for ind in artwork_indicators)
    
    # Sanatçı kategorisi GÖSTERGESİ — varsa kişi sayfası demektir
    person_indicators = [
        "births", "deaths", "living people", "alumni",
        "people from", "graduates of",
    ]
    has_person_cat = any(ind in cats_lower for ind in person_indicators)
    
    # Eser kategorisi var ve kişi kategorisi yoksa -> bu eser sayfası
    return has_artwork_cat and not has_person_cat


def find_artwork_page_strict(title, artist):
    """Eserin KENDİ Wikipedia sayfasını bulmaya çalışır. Sanatçı sayfasını kabul etmez."""
    
    queries = [
        f'"{title}"',
        f'"{title}" {artist}',
        f'{title} painting',
        f'{title} artwork',
    ]
    
    seen_titles = set()
    
    for query in queries:
        results = wiki_search(query, lang="en", limit=5)
        for r in results:
            r_title = r.get("title", "")
            
            if r_title in seen_titles:
                continue
            seen_titles.add(r_title)
            
            # Hızlı reddetmeler
            r_lower = r_title.lower()
            if any(bad in r_lower for bad in [
                "list of", "video game", "google stadia", "discography",
                "filmography", "bibliography",
            ]):
                continue
            
            # Sanatçı tam sayfası olmasın
            if r_title.lower().strip() == artist.lower().strip():
                continue
            
            # Kategorilere bak
            cats = wiki_page_categories(r_title, lang="en")
            if not is_specific_artwork_page(r_title, cats, artist):
                continue
            
            # Eser başlığı sayfada anlamlı şekilde geçiyor mu?
            title_words = [w for w in title.lower().split() if len(w) > 2]
            title_match = sum(1 for w in title_words if w in r_lower)
            if title_match < max(1, len(title_words) // 2):
                continue
            
            # Tüm filtrelerden geçtiyse özet al
            summary = wiki_page_summary(r_title, lang="en")
            if summary and summary.get("extract") and summary.get("originalimage"):
                # Görseli olan + extract'i olan eser sayfası bulundu
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
                if any(k in cats_lower for k in ["artist", "painter", "sculptor",
                                                   "sanatçı", "ressam", "heykeltıraş"]):
                    summary = wiki_page_summary(r_title, lang=lang)
                    if summary and summary.get("extract"):
                        return summary
    return None


# ====================== Gemini Yardımcıları ======================

def build_initial_prompt(analysis):
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

KÜRATÖR NOTU: {analysis.get('curator_note', '')}

ÖNEMLİ KISITLAR:

Sanat eseri seçiminde:
- Eser Wikipedia'da KENDİ AYRI SAYFASI olan kanonik bir iş olmalı (sadece sanatçı sayfasında geçen değil)
- Yani gerçekten ünlü, üzerinde yazılmış, müzelerde olan eserler
- Klasikten çağdaşa serbest, ama her durumda Wikipedia'da sayfası olan
- Örnek olabilirler: Yıldızlı Gece (Van Gogh), Guernica (Picasso), Çığlık (Munch), Olympia (Manet), Comedian (Cattelan'ın muzu), Girl with Balloon (Banksy), The Physical Impossibility of Death (Hirst), Cloud Gate (Kapoor), Fountain (Duchamp), 100 Years of Solitude vs.
- Klişeden kaçın ama "Wikipedia'da sayfası vardır" diye emin olduklarını seç
- Resim, heykel, enstalasyon, fotoğraf, video, performans — her form serbest

Müzik seçiminde:
- Spotify'da bulunabilen gerçek bir parça olmalı
- Tür sınırlaması yok: klasik, caz, elektronik, halk, rock, deneysel, ambient, dünya müziği serbest
- Sanatçı ve parça adı kesin olmalı

CEVABINI MUTLAKA AŞAĞIDAKİ JSON FORMATINDA VER, başka hiçbir metin ekleme:

{{
  "artwork": {{
    "title": "Eserin tam adı (Wikipedia'daki haliyle, İngilizce tercih)",
    "artist": "Sanatçı tam adı",
    "year": "yapım yılı",
    "medium": "tekniği",
    "form": "resim/heykel/fotoğraf/enstalasyon/video/performans",
    "location": "şu an nerede sergileniyor",
    "description": "2-3 cümlelik tarif",
    "wikipedia_title_guess": "Wikipedia'da hangi başlıkla geçiyor olabilir (en yakın tahminin)"
  }},
  "music": {{
    "title": "Parçanın tam adı",
    "artist": "Sanatçı/grup tam adı",
    "album": "Albüm adı",
    "year": "çıkış yılı",
    "genre": "tür",
    "mood": "atmosfer (3-5 sıfat)",
    "spotify_search": "Spotify aramak için query"
  }},
  "curator_statement": "Türkçe küratör yorumu, edebi ve düşünceli, 4-6 cümle"
}}

ÖNEMLİ:
- Eser Wikipedia'da SAYFASI OLAN bir iş olmalı, emin değilsen tahmin etme — alternatif düşün
- Klişe değil ama doğrulanabilir seçim
"""


def build_retry_prompt(analysis, previous_attempts):
    """Önceki başarısız denemeler sonrası daha hedefli istek."""
    
    failed_list = ""
    for i, attempt in enumerate(previous_attempts, 1):
        failed_list += f"{i}. '{attempt['title']}' - {attempt['artist']}\n"
    
    return f"""Daha önce şu sanat eserini önerdin ama Wikipedia'da KENDİ AYRI SAYFASI BULUNAMADI:

{failed_list}

Yeni bir öneri yap. ÇOK KATI KISIT: Eserin Wikipedia'da kendi ayrı sayfası olmalı (sanatçı sayfasında geçmesi YETMEZ). 

Bunu garanti etmenin en iyi yolu: çok ünlü, kanonik eserlerden seç. Şu kriterlerden en az birini sağlayanlar genelde Wikipedia'da kendi sayfasına sahiptir:
- Müze koleksiyonlarının "imza" eserleri (Yıldızlı Gece, Mona Lisa, Las Meninas tarzı)
- Sanat tarihinde dönüm noktası eserler (Avignon Kızları, Pisuvar/Fountain, Çeşme, Black Square)  
- Çağdaş sanatın ikonik işleri (Girl with Balloon, Comedian, The Physical Impossibility of Death, Cloud Gate, Maman, For the Love of God)
- Açık alanda büyük heykeller (Cloud Gate, Spoonbridge and Cherry, Maman)

Atmosfer: {analysis.get('day_mood', '')}
Baskın duygu: {analysis.get('dominant_emotion', '')}
Temalar: {', '.join(analysis.get('key_themes', []))}

Aynı JSON formatında cevap ver, müzik önerisini de yenile (önceki denemelerle uyumlu olabilir):

{{
  "artwork": {{
    "title": "...",
    "artist": "...",
    "year": "...",
    "medium": "...",
    "form": "...",
    "location": "...",
    "description": "...",
    "wikipedia_title_guess": "..."
  }},
  "music": {{
    "title": "...",
    "artist": "...",
    "album": "...",
    "year": "...",
    "genre": "...",
    "mood": "...",
    "spotify_search": "..."
  }},
  "curator_statement": "..."
}}
"""


def call_gemini(client, prompt):
    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.9,
        ),
    )
    return json.loads(response.text)


# ====================== Ana Akış ======================

def main():
    if not Path(INPUT_FILE).exists():
        print(f"❌ {INPUT_FILE} bulunamadı.")
        return
    
    with open(INPUT_FILE, encoding="utf-8") as f:
        data = json.load(f)
    
    analysis = data["analysis"]
    
    print(f"🎭 Günün Ruhu: {analysis.get('day_mood', '')[:120]}...")
    print(f"💫 Baskın Duygu: {analysis.get('dominant_emotion', '?')}\n")
    
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("❌ GEMINI_API_KEY bulunamadı")
        return
    
    client = genai.Client(api_key=api_key)
    
    # Doğrulama döngüsü
    failed_attempts = []
    final_curation = None
    artwork_page = None
    
    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(f"\n{'='*60}")
        print(f"🔄 DENEME {attempt}/{MAX_ATTEMPTS}")
        print(f"{'='*60}")
        
        if attempt == 1:
            prompt = build_initial_prompt(analysis)
        else:
            prompt = build_retry_prompt(analysis, failed_attempts)
        
        try:
            curation = call_gemini(client, prompt)
        except Exception as e:
            print(f"❌ Gemini hatası: {e}")
            return
        
        artwork = curation.get("artwork", {})
        title = artwork.get("title", "")
        artist = artwork.get("artist", "")
        
        print(f"\n🎨 Önerilen: '{title}' - {artist}")
        print(f"   {artwork.get('year', '?')} | {artwork.get('form', '?')} | {artwork.get('location', '?')}")
        
        # Doğrula
        print(f"\n🔍 Wikipedia'da eser sayfası aranıyor (sıkı doğrulama)...")
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
            
            if attempt < MAX_ATTEMPTS:
                print(f"   → Gemini'ye yeni öneri istetiliyor...")
    
    # Sonuç
    if not final_curation:
        print(f"\n⚠️  {MAX_ATTEMPTS} denemede de doğrulanmış eser bulunamadı.")
        print("Son denemeyi yine de kaydediyoruz, ama görsel olmayabilir.")
        final_curation = curation  # Son denemeyi kullan
    
    # Sanatçı sayfasını da arayalım (ek bilgi için)
    artist_name = final_curation.get("artwork", {}).get("artist", "")
    print(f"\n🔍 Sanatçı sayfası aranıyor: {artist_name}")
    artist_page = find_artist_page(artist_name)
    if artist_page:
        print(f"   ✓ {artist_page['page_url']}")
    
    # Özet
    artwork = final_curation.get("artwork", {})
    music = final_curation.get("music", {})
    
    print("\n" + "=" * 60)
    print("🖼️  SANAT ESERİ (DOĞRULANMIŞ)" if artwork_page else "🖼️  SANAT ESERİ (DOĞRULANAMADI)")
    print("=" * 60)
    print(f"  Başlık: {artwork.get('title')}")
    print(f"  Sanatçı: {artwork.get('artist')}")
    print(f"  Yıl: {artwork.get('year')}")
    print(f"  Form: {artwork.get('form')}")
    print(f"  Konum: {artwork.get('location')}")
    if artwork_page:
        print(f"  ✓ Wikipedia: {artwork_page['page_url']}")
        print(f"  ✓ Görsel: {artwork_page['originalimage']}")
    
    print()
    print("=" * 60)
    print("🎵 MÜZİK")
    print("=" * 60)
    print(f"  Parça: {music.get('title')} - {music.get('artist')}")
    print(f"  Albüm: {music.get('album')} ({music.get('year')})")
    print(f"  Tür: {music.get('genre')}")
    print(f"  Atmosfer: {music.get('mood')}")
    
    print()
    print("=" * 60)
    print("📝 KÜRATÖR YORUMU")
    print("=" * 60)
    print(final_curation.get("curator_statement", ""))
    
    # Kaydet
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model_used": MODEL_NAME,
        "attempts": len(failed_attempts) + (1 if artwork_page else 0),
        "failed_attempts": failed_attempts,
        "verified": bool(artwork_page),
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
