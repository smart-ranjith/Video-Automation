import os
import PIL.Image
if not hasattr(PIL.Image, 'ANTIALIAS'):
    PIL.Image.ANTIALIAS = PIL.Image.LANCZOS
import json
import time
import random
import requests
import asyncio
import pickle
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import google.generativeai as genai
import edge_tts
from moviepy.editor import *
import moviepy.video.fx.all as vfx
import moviepy.audio.fx.all as afx
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

# --- 1. SETUP & SECRETS ---
import base64

def require_env(key):
    val = os.environ.get(key)
    if not val:
        raise Exception(f"Missing required env var: {key}. Set it in GitHub Actions secrets.")
    return val

GEMINI_API_KEY = require_env("GEMINI_API_KEY")
PEXELS_API_KEY = require_env("PEXELS_API_KEY")
PIXABAY_API_KEY = require_env("PIXABAY_API_KEY")

genai.configure(api_key=GEMINI_API_KEY)
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

def restore_google_secrets():
    """Decode base64 secrets from env vars into files GH Actions run needs.
    Run once at start of main() before any Google auth call."""
    client_b64 = require_env("CLIENT_SECRETS_BASE64")
    token_b64 = require_env("TOKEN_PICKLE_BASE64")
    with open("client_secrets.json", "wb") as f:
        f.write(base64.b64decode(client_b64))
    with open("token.pickle", "wb") as f:
        f.write(base64.b64decode(token_b64))

VOICE = "en-US-ChristopherNeural"
OUTPUT_AUDIO = "voiceover.mp3"

# --- 2. THE AI SCRIPT ENGINE ---
def generate_script():
    print("🧠 Asking Gemini to write the viral script...")
    model = genai.GenerativeModel('gemini-2.5-flash')
    
    prompt = """
    You are an expert YouTube Shorts scriptwriter known for extreme viewer retention. 
    Write a highly engaging, fast-paced 45-second script about a fascinating science, tech, or space mystery. 
    Ensure the script and all metadata are written entirely in English.

    Strict Structure Rules:
    1. THE HOOK: Start with a bizarre, counter-intuitive question.
    2. THE BODY: Deliver rapid-fire, mind-blowing facts.
    3. THE LOOP: The final sentence must be an incomplete thought that grammatically loops seamlessly right back into the first word of the hook.

    Technical Constraints:
    - The "script" must be exactly 70 to 80 words.
    - The "image_prompts" array must contain exactly 15 short visual search terms for Pexels.

    Format the output as strictly valid JSON exactly like this:
    {
      "script": "...",
      "image_prompts": ["...", "...", "..."],
      "title": "Catchy SEO Title #shorts",
      "tags": ["tag1", "tag2", "tag3"],
      "description": "SEO description."
    }
    """
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
            return json.loads(response.text)
        except Exception as e:
            if "429" in str(e):
                time.sleep((attempt + 1) * 60)
            else:
                raise e
    raise Exception("Failed to get response from Gemini.")

# --- 3. WORD-BY-WORD AUDIO SYNC ---
async def generate_audio_and_timestamps(text):
    print("🎙️ Generating AI Voiceover and extracting timestamps...")
    communicate = edge_tts.Communicate(text, VOICE)
    words = []
    
    with open(OUTPUT_AUDIO, "wb") as fp:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                fp.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start_sec = chunk["offset"] / 10000000.0
                end_sec = start_sec + (chunk["duration"] / 10000000.0)
                words.append({"text": chunk["text"], "start": start_sec, "end": end_sec})
                
    with open("words.json", "w") as f:
        json.dump(words, f)

# --- 4. MEDIA DOWNLOADER (WITH IMAGE FALLBACK & SFX) ---
def download_sfx():
    """Downloads lightweight standard SFX if they don't exist yet."""
    if not os.path.exists("whoosh.mp3"):
        # Downloading a generic royalty-free whoosh from a reliable raw source
        with open("whoosh.mp3", "wb") as f:
            f.write(requests.get("https://raw.githubusercontent.com/Ranjith/dummy/main/whoosh.mp3").content) # Replace with actual direct MP3 links if needed, or place manually
    if not os.path.exists("pop.mp3"):
        with open("pop.mp3", "wb") as f:
            f.write(requests.get("https://raw.githubusercontent.com/Ranjith/dummy/main/pop.mp3").content)

def download_videos(prompts):
    headers = {"Authorization": PEXELS_API_KEY}
    if not os.path.exists("media"): os.makedirs("media")
    
    for index, prompt in enumerate(prompts):
        # 1. Try Video First
        vid_url = f"https://api.pexels.com/videos/search?query={prompt}&orientation=portrait&per_page=1"
        response = requests.get(vid_url, headers=headers)
        
        if response.status_code == 200 and response.json().get("videos"):
            video_url = response.json()["videos"][0]["video_files"][0]["link"]
            with open(f"media/clip_{index}.mp4", "wb") as f: 
                f.write(requests.get(video_url).content)
        else:
            # 2. IMAGE FALLBACK if Video fails
            print(f"⚠️ No video for '{prompt}'. Falling back to Image...")
            img_url = f"https://api.pexels.com/v1/search?query={prompt}&orientation=portrait&per_page=1"
            img_response = requests.get(img_url, headers=headers)
            if img_response.status_code == 200 and img_response.json().get("photos"):
                photo_url = img_response.json()["photos"][0]["src"]["portrait"]
                with open(f"media/clip_{index}.jpg", "wb") as f: 
                    f.write(requests.get(photo_url).content)

def download_music():
    url = f"https://pixabay.com/api/audio/?key={PIXABAY_API_KEY}&q=cinematic+ambient"
    response = requests.get(url)
    if response.status_code == 200 and response.json().get("hits"):
        track = random.choice(response.json()["hits"])
        with open("background_music.mp3", "wb") as f: 
            f.write(requests.get(track["audio"]).content)
        return "background_music.mp3"
    return None

# --- 5. THE PRO EDITOR ---
def assemble_video():
    print("\n🎬 Assembling cinematic video...")
    voice_audio = AudioFileClip(OUTPUT_AUDIO)
    
    # Audio Setup
    audio_tracks = [voice_audio]
    bgm_file = download_music()
    if bgm_file:
        bgm = AudioFileClip(bgm_file).fx(afx.audio_loop, duration=voice_audio.duration).fx(afx.volumex, 0.05)
        audio_tracks.append(bgm)

    with open("words.json", "r") as f:
        words = json.load(f)

    media_files = [os.path.join("media", f) for f in os.listdir("media") if f.endswith(".mp4") or f.endswith(".jpg")]
    if len(media_files) == 0:
        raise Exception("CRITICAL ERROR: No media files were downloaded. Check your PEXELS_API_KEY and internet connection!")
    
    clips = []
    current_time = 0
    media_index = 0
    cut_duration = 3.0 
    
    # Try to load SFX
    has_sfx = os.path.exists("whoosh.mp3") and os.path.exists("pop.mp3")

    while current_time < voice_audio.duration:
        media_path = media_files[media_index % len(media_files)]
        
        # Determine if it's a Video or the Fallback Image
        if media_path.endswith(".mp4"):
            raw_clip = VideoFileClip(media_path)
            clip_duration = min(cut_duration, raw_clip.duration, voice_audio.duration - current_time)
        else:
            clip_duration = min(cut_duration, voice_audio.duration - current_time)
            raw_clip = ImageClip(media_path).set_duration(clip_duration)
        
        # Ken Burns & Crop
        clip = raw_clip.resize(height=1920)
        clip = clip.resize(lambda t: 1 + 0.05 * (t / clip_duration)) 
        clip = clip.crop(x_center=clip.w/2, y_center=clip.h/2, width=1080, height=1920)
        clip = clip.subclip(0, clip_duration).set_start(current_time).fx(vfx.colorx, 0.8)
        
        clips.append(clip)

        # Add Whoosh SFX at every cut
        if has_sfx and current_time > 0:
            whoosh = AudioFileClip("whoosh.mp3").set_start(current_time).fx(afx.volumex, 0.3)
            audio_tracks.append(whoosh)
        
        current_time += clip_duration
        media_index += 1

    # Text Engine (The "Stack" Method)
    text_clips = []
    for i, word_data in enumerate(words):
        start_t = word_data["start"]
        end_t = word_data["end"]
        
        # Main Active Word (Yellow, Large)
        txt_active = TextClip(word_data["text"].upper(), fontsize=120, color='yellow', font='Arial-Bold', stroke_color='black', stroke_width=5)
        txt_active = txt_active.set_position(('center', 1150)).set_start(start_t).set_end(end_t)
        
        # Next Word Peek (White, Small, Underneath)
        if i + 1 < len(words):
            txt_next = TextClip(words[i+1]["text"].upper(), fontsize=70, color='white', font='Arial-Bold', stroke_color='black', stroke_width=3)
            txt_next = txt_next.set_position(('center', 1300)).set_start(start_t).set_end(end_t)
            text_clips.append(txt_next)

        text_clips.append(txt_active)

        # Add Pop SFX for every word
        if has_sfx:
            pop = AudioFileClip("pop.mp3").set_start(start_t).fx(afx.volumex, 0.2)
            audio_tracks.append(pop)

    # Retention Bar
    retention_bar = ColorClip(size=(1080, 15), color=(255, 255, 0))
    total_dur = voice_audio.duration
    retention_bar = retention_bar.set_position(lambda t: (-1080 + int(1080 * (t / total_dur)), 0)).set_duration(total_dur)
    text_clips.append(retention_bar)

    # Final Assembly
    final_audio = CompositeAudioClip(audio_tracks)
    bg_video = CompositeVideoClip(clips).set_audio(final_audio)
    final = CompositeVideoClip([bg_video] + text_clips)
    
    final.write_videofile("final_short.mp4", fps=24, codec="libx264", audio_codec="aac", threads=4)
    final.close()

# --- 6. THE DELIVERY ---
def upload_to_youtube(video_file, data):
    credentials = None
    if os.path.exists("token.pickle"):
        with open("token.pickle", "rb") as token: 
            credentials = pickle.load(token)
            
    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("client_secrets.json", SCOPES)
            credentials = flow.run_local_server(port=0)
        with open("token.pickle", "wb") as token:
            pickle.dump(credentials, token)
            
    youtube = build("youtube", "v3", credentials=credentials)
    media = MediaFileUpload(video_file, chunksize=-1, resumable=True)
    body = {"snippet": {"categoryId": "28", "title": data["title"], "description": data["description"], "tags": data["tags"]}, 
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}}
            
    response = youtube.videos().insert(part="snippet,status", body=body, media_body=media).execute()
    print(f"✅ Success! Link: https://youtu.be/{response['id']}")

async def main():
    restore_google_secrets()
    content = generate_script() 
    await generate_audio_and_timestamps(content["script"])
        
    download_videos(content["image_prompts"])
    assemble_video()
    upload_to_youtube("final_short.mp4", content)
    
if __name__ == "__main__":
    asyncio.run(main())
