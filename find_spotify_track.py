"""
find_spotify_track.py
curation.json'daki müzik önerisini Spotify'da arar, track URL ve embed kodu üretir.
Playlist'e ekleme yapmaz (manuel olarak yapılacak). Sonucu spotify_result.json
olarak kaydeder.
"""

import os
import json
import base64
import requests
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

INPUT_FILE = "curation.json"
OUTPUT_FILE = "spotify_result.json"

CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")


def get_client_credentials_token():
    """Refresh token gerekmez — bu uygulamanın kendi credentialları yeterli (search için).
    Bu yöntemle 403 olmaz çünkü kullanıcı yetkisi gerekmiyor."""
    url = "https://accounts.spotify.com/api/token"
    
    auth_str = f"{CLIENT_ID}:{CLIENT_SECRET}"
    auth_b64 = base64.b64encode(auth_str.encode("ascii")).decode("ascii")
    
    headers = {
        "Authorization": f"Basic {auth_b64}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {"grant_type": "client_credentials"}
    
    r = requests.post(url, headers=headers, data=data, timeout=15)
    r.raise_for_status()
    return r.json()["access_token"]


def search_track(token, music_info):
    """Spotify'da parça ara."""
    url = "https://api.spotify.com/v1/search"
    headers = {"Authorization": f"Bearer {token}"}
    
    title = music_info.get("title", "")
    artist = music_info.get("artist", "")
    
    queries = [
        f'track:"{title}" artist:"{artist}"',
        f'{title} {artist}',
        music_info.get("spotify_search", ""),
    ]
    
    for q in queries:
        if not q:
            continue
        params = {"q": q, "type": "track", "limit": 5}
        r = requests.get(url, headers=headers, params=params, timeout=15)
        if r.status_code != 200:
            continue
        
        items = r.json().get("tracks", {}).get("items", [])
        if not items:
            continue
        
        # En iyi eşleşmeyi seç
        for item in items:
            track_artists = [a["name"].lower() for a in item.get("artists", [])]
            track_title = item.get("name", "").lower()
            
            artist_match = artist.lower() in " ".join(track_artists)
            title_match = (
                title.lower() in track_title or
                track_title in title.lower()
            )
            
            if artist_match and title_match:
                return item
        
        return items[0]
    
    return None


def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        print("❌ SPOTIFY_CLIENT_ID veya SPOTIFY_CLIENT_SECRET eksik")
        return
    
    if not Path(INPUT_FILE).exists():
        print(f"❌ {INPUT_FILE} bulunamadı.")
        return
    
    with open(INPUT_FILE, encoding="utf-8") as f:
        curation_data = json.load(f)
    
    music = curation_data.get("curation", {}).get("music", {})
    if not music:
        print("❌ curation.json'da music bilgisi yok")
        return
    
    print(f"🎵 Aranıyor: {music.get('title')} - {music.get('artist')}")
    
    # Token al (client credentials flow — 403 olmaz)
    print(f"\n🔑 Access token alınıyor...")
    try:
        token = get_client_credentials_token()
        print(f"   ✓ Token alındı")
    except Exception as e:
        print(f"   ❌ Token hatası: {e}")
        return
    
    # Parçayı ara
    print(f"\n🔍 Spotify aranıyor...")
    track = search_track(token, music)
    
    if not track:
        print(f"   ✗ Parça bulunamadı")
        output = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "success": False,
            "music_suggestion": music,
        }
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        return
    
    track_id = track["id"]
    track_uri = track["uri"]
    track_name = track["name"]
    track_artists = ", ".join(a["name"] for a in track["artists"])
    track_album = track["album"]["name"]
    track_url = track["external_urls"]["spotify"]
    track_duration_ms = track["duration_ms"]
    track_duration = f"{track_duration_ms // 60000}:{(track_duration_ms % 60000) // 1000:02d}"
    
    album_image = None
    if track["album"].get("images"):
        album_image = track["album"]["images"][0]["url"]
    
    # Spotify embed iframe kodu
    embed_html = (
        f'<iframe src="https://open.spotify.com/embed/track/{track_id}" '
        f'width="100%" height="152" frameborder="0" allowfullscreen '
        f'allow="autoplay; clipboard-write; encrypted-media; fullscreen; picture-in-picture" '
        f'loading="lazy"></iframe>'
    )
    
    print(f"\n   ✓ Bulundu: '{track_name}' - {track_artists}")
    print(f"     Albüm: {track_album}")
    print(f"     Süre: {track_duration}")
    print(f"     URL: {track_url}")
    
    print("\n" + "=" * 60)
    print("📊 SONUÇ")
    print("=" * 60)
    print(f"  Önerilen: {music.get('title')} - {music.get('artist')}")
    print(f"  Bulunan:  {track_name} - {track_artists}")
    print(f"  Spotify:  {track_url}")
    print(f"\n  Embed kodu spotify_result.json'a kaydedildi.")
    
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "success": True,
        "music_suggestion": music,
        "spotify_track": {
            "id": track_id,
            "uri": track_uri,
            "name": track_name,
            "artists": track_artists,
            "album": track_album,
            "duration": track_duration,
            "url": track_url,
            "album_image": album_image,
            "preview_url": track.get("preview_url"),
            "embed_html": embed_html,
        },
    }
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    
    print(f"\n💾 Kaydedildi: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
