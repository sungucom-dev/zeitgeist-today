"""
analyze_news.py
collected_news.json'u okur, akıllı örnekleme yapar, Gemini ile haberleri 
kümeler ve günün ruhunu çıkartır. Sonucu day_analysis.json olarak kaydeder.
"""

import os
import json
from datetime import datetime, timezone
from collections import defaultdict
from pathlib import Path
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

# ----- Ayarlar -----
INPUT_FILE = "collected_news.json"
OUTPUT_FILE = "day_analysis.json"
MODEL_NAME = "gemini-2.5-flash"

# Her kategoriden Gemini'ye en fazla kaç haber gönderelim
# Gündem fazla, ama dengelemek için kategori başına sınır koyuyoruz
CATEGORY_LIMITS = {
    "Gündem": 250,
    "Teknoloji": 80,
    "Spor": 60,
    "Eğlence": 50,
    "Ekonomi ve Finans": 60,
    "Kültür ve Sanat": 50,   # Az ama hepsini al
    "Yaşam": 30,
    "Savunma ve Sanayi": 30,
    "Bilim": 50,             # Az ama hepsini al
    "İş Dünyası": 20,
}


def smart_sample(items):
    """Kategori bazında akıllı örnekleme yapar.
    Çok haberli kategorilerde sınır uygular, az olanlarda hepsini alır."""
    by_category = defaultdict(list)
    for item in items:
        by_category[item["category"]].append(item)
    
    sampled = []
    for category, cat_items in by_category.items():
        limit = CATEGORY_LIMITS.get(category, 50)
        if len(cat_items) <= limit:
            sampled.extend(cat_items)
        else:
            # En çok kaynakta görüleni öne al (also_in dolu olanlar)
            cat_items.sort(key=lambda x: -len(x.get("also_in", [])))
            sampled.extend(cat_items[:limit])
    
    return sampled


def build_prompt(items):
    """Gemini'ye gönderilecek prompt'u oluşturur."""
    
    # Haberleri kategorilere göre grupla, kompakt format
    by_category = defaultdict(list)
    for i, item in enumerate(items):
        by_category[item["category"]].append({
            "id": i,
            "title": item["title"],
            "source_count": 1 + len(item.get("also_in", []))
        })
    
    news_text = ""
    for category, cat_items in by_category.items():
        news_text += f"\n=== {category} ({len(cat_items)} haber) ===\n"
        for item in cat_items:
            sc = item["source_count"]
            sc_marker = f" [{sc} kaynakta]" if sc > 1 else ""
            news_text += f"{item['id']}: {item['title']}{sc_marker}\n"
    
    today = datetime.now().strftime("%d %B %Y")
    
    prompt = f"""Sen bir küratör ve haber analistisin. Aşağıda {today} tarihli son 24 saatlik Türk basınından toplanmış haberler var. Görevin:

1. **KÜMELEME**: Aynı olayı/konuyu farklı kelimelerle anlatan haberleri tek küme yap. Örneğin "Erdoğan'ın açıklaması", "Cumhurbaşkanı bugün konuştu", "TBMM'de tarihi konuşma" aynı olaysa tek kümeye al. Her küme için en az 3-4 haberin birleşmesini bekliyorum (önemli olaylar için).

2. **KATEGORİZE ETME**: Her kümeyi şu meta-kategorilerden birine yerleştir:
   - "Politika ve Diplomasi"
   - "Ekonomi ve Piyasalar"  
   - "Toplum ve Yaşam"
   - "Bilim ve Teknoloji"
   - "Kültür, Sanat ve Düşünce"
   - "Spor"
   - "Doğa, Çevre ve İklim"
   - "Sağlık"
   - "Eğitim"
   - "Magazin ve Eğlence"
   - "Diğer"

3. **GÜNÜN RUHU**: Tüm haberlere bakarak günün genel atmosferini tarif et. Hangi duygular baskın? Bu gün nasıl bir gün? (Örnek: "gergin ve siyasi", "hüzünlü ama umutlu", "olağan ve sakin", "kaotik")

4. **ANAHTAR TEMALAR**: Günün 3-5 anahtar temasını çıkar.

CEVABINI MUTLAKA AŞAĞIDAKİ JSON FORMATINDA VER, başka hiçbir metin ekleme:

{{
  "day_mood": "günün genel ruhunu anlatan 1-2 cümle, atmosferik",
  "key_themes": ["tema 1", "tema 2", "tema 3"],
  "dominant_emotion": "tek kelimeyle baskın duygu (gergin, umutlu, hüzünlü, kaotik, dingin, vb.)",
  "clusters": [
    {{
      "title": "kümeyi en iyi anlatan başlık (kısa)",
      "meta_category": "yukarıdaki listeden",
      "summary": "2-3 cümle özet",
      "importance": 1-10 arası önem skoru,
      "story_ids": [haber id'leri]
    }}
  ],
  "curator_note": "Günü tek paragrafta, küratör bakışıyla özetleyen 3-4 cümle. Sanat eseri ve müzik seçimi için ipucu veren atmosferik bir not."
}}

ÖNEMLİ: Çok haberli olaylar için büyük kümeler oluştur, tekil ya da önemsiz haberleri kümelemeden geç. En fazla 25 küme yeter, daha azı daha iyi (önem sırasına göre).

İŞTE HABERLER:
{news_text}
"""
    return prompt


def main():
    # Veriyi yükle
    if not Path(INPUT_FILE).exists():
        print(f"❌ {INPUT_FILE} bulunamadı. Önce collect_news.py'yi çalıştır.")
        return
    
    with open(INPUT_FILE, encoding="utf-8") as f:
        data = json.load(f)
    
    items = data["items"]
    print(f"📰 Toplam {len(items)} haber yüklendi")
    
    # Akıllı örnekleme
    sampled = smart_sample(items)
    print(f"🎯 Gemini'ye gönderilecek: {len(sampled)} haber")
    
    cat_dist = defaultdict(int)
    for item in sampled:
        cat_dist[item["category"]] += 1
    print(f"📁 Örneklem dağılımı:")
    for cat, count in sorted(cat_dist.items(), key=lambda x: -x[1]):
        print(f"  {cat}: {count}")
    
    # Gemini'ye gönder
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("❌ GEMINI_API_KEY bulunamadı")
        return
    
    client = genai.Client(api_key=api_key)
    prompt = build_prompt(sampled)
    
    print(f"\n🤖 Gemini'ye gönderiliyor (model: {MODEL_NAME})...")
    print(f"📏 Prompt uzunluğu: {len(prompt):,} karakter")
    
    try:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.7,
            ),
        )
    except Exception as e:
        print(f"❌ Gemini hatası: {e}")
        return
    
    print(f"✓ Cevap alındı ({len(response.text):,} karakter)\n")
    
    # JSON parse
    try:
        analysis = json.loads(response.text)
    except json.JSONDecodeError as e:
        print(f"❌ JSON parse hatası: {e}")
        print("Ham cevap:")
        print(response.text[:2000])
        return
    
    # Cluster'lara orijinal haber detaylarını ekle
    for cluster in analysis.get("clusters", []):
        cluster["stories"] = []
        for sid in cluster.get("story_ids", []):
            if 0 <= sid < len(sampled):
                story = sampled[sid]
                cluster["stories"].append({
                    "title": story["title"],
                    "source": story["source"],
                    "link": story["link"],
                    "also_in": story.get("also_in", []),
                })
        # Önem sıralaması için story sayısını ekle
        cluster["story_count"] = len(cluster["stories"])
    
    # Önem skoruna göre sırala
    analysis["clusters"].sort(
        key=lambda c: (c.get("importance", 0), c.get("story_count", 0)),
        reverse=True
    )
    
    # Özet yazdır
    print(f"🎭 Günün Ruhu: {analysis.get('day_mood', '?')}")
    print(f"💫 Baskın Duygu: {analysis.get('dominant_emotion', '?')}")
    print(f"🔑 Anahtar Temalar: {', '.join(analysis.get('key_themes', []))}")
    print(f"\n📰 {len(analysis.get('clusters', []))} küme oluşturuldu:")
    for i, cluster in enumerate(analysis["clusters"][:10], 1):
        print(f"  {i}. [{cluster.get('importance', '?')}/10] "
              f"{cluster.get('title', '?')} "
              f"({cluster.get('story_count', 0)} haber)")
    
    print(f"\n📝 Küratör Notu:\n{analysis.get('curator_note', '?')}")
    
    # Kaydet
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model_used": MODEL_NAME,
        "input_items": len(items),
        "sampled_items": len(sampled),
        "analysis": analysis,
    }
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    
    print(f"\n💾 Kaydedildi: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
