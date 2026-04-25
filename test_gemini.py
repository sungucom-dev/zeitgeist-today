import os
from dotenv import load_dotenv
from google import genai

load_dotenv()

api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    print("❌ GEMINI_API_KEY bulunamadı, .env dosyasını kontrol et")
    exit(1)

# Yeni SDK ile client oluştur
client = genai.Client(api_key=api_key)

# Basit bir test
print("🤖 Test sorgusu gönderiliyor...")
response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents="Merhaba, çalışıyor musun? Tek cümleyle cevap ver."
)
print(f"Cevap: {response.text}")
