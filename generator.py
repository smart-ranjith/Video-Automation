import os
import json
import asyncio
import requests
import math
import re
import pickle
import random
import time
import edge_tts
from google import genai
from google.genai import types
from moviepy.editor import VideoFileClip, AudioFileClip, concatenate_videoclips, TextClip, CompositeVideoClip, CompositeAudioClip
import moviepy.audio.fx.all as afx
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.http import MediaFileUpload

# --- 1. SETUP & SECRETS ---
os.environ["GEMINI_API_KEY"] = os.environ.get("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY")
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "YOUR_PEXELS_API_KEY")
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY", "YOUR_PIXABAY_API_KEY")

VOICE = "en-US-ChristopherNeural"
OUTPUT_AUDIO = "voiceover.mp3"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

# --- 2. THE BRAIN (GEMINI) ---
def generate_script():
    print("🧠 Asking Gemini to write the script...")
    client = genai.Client()
    prompt = """
    Write a 45-second YouTube Short script about a fascinating space phenomenon.
    Format your response as a JSON object with two keys:
    - "script": The spoken text of the video.
    - "image_prompts": A list of 5 short visual descriptions (max 5 words each) matching the script.
    """
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.7)
            )
            return json.loads(response.text)
        except Exception as e:
            print(f"⚠️ AI Busy: {e}")
            if attempt < max_retries - 1: time.sleep(15)
            else: raise e

# --- 3. THE VOICE & SUBTITLES ---
async def generate_audio(text):
    communicate = edge_tts.Communicate(text, VOICE)
    submaker = edge_tts.SubMaker()
    with open(OUTPUT_AUDIO, "wb") as fp:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio": fp.write(chunk["data"])
            elif chunk["type"] == "WordBoundary": submaker.feed(chunk)
    with open("subtitles.srt", "w", encoding="utf-8") as f: f.write(submaker.get_srt())

# --- 4. THE VISUALS & MUSIC ---
def download_videos(prompts):
    headers = {"Authorization": PEXELS_API_KEY}
    if not os.path.exists("videos"): os.makedirs("videos")
    for index, prompt in enumerate(prompts):
        url = f"https://api.pexels.com/videos/search?query={prompt}&orientation=portrait&per_page=1"
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            if len(data["videos"]) > 0:
                video_url = data["videos"][0]["video_files"][0]["link"]
                with open(f"videos/clip_{index}.mp4", "wb") as f: f.write(requests.get(video_url).content)

def download_music():
    url = f"https://pixabay.com/api/audio/?key={PIXABAY_API_KEY}&q=cinematic+ambient"
    response = requests.get(url)
    if response.status_code == 200:
        data = response.json()
        if len(data["hits"]) > 0:
            track = random.choice(data["hits"])
            with open("background_music.mp3", "wb") as f: f.write(requests.get(track["audio"]).content)
            return "background_music.mp3"
    return None

def parse_srt(filename):
    with open(filename, "r", encoding="utf-8") as f: content = f.read()
    pattern = re.compile(r"(\d+)\n(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})\n(.*)")
    subtitles = []
    for match in pattern.findall(content):
        start = sum(x * int(t) for x, t in zip([3600, 60, 1], match[1].split(':')[:2] + [match[1].split(':')[2].split(',')[0]])) + int(match[1].split(',')[1])/1000.0
        end = sum(x * int(t) for x, t in zip([3600, 60, 1], match[2].split(':')[:2] + [match[2].split(':')[2].split(',')[0]])) + int(match[2].split(',')[1])/1000.0
        subtitles.append((start, end, match[3].strip()))
    return subtitles

# --- 5. THE EDITOR ---
def assemble_video():
    print("\n🎬 Assembling final video...")
    voice_audio = AudioFileClip(OUTPUT_AUDIO)
    bgm_file = download_music()
    bgm = AudioFileClip(bgm_file).fx(afx.audio_loop, duration=voice_audio.duration).fx(afx.volumex, 0.1) if bgm_file else None
    
    video_files = [f for f in os.listdir("videos") if f.endswith(".mp4")]
    clip_length = math.ceil(voice_audio.duration / len(video_files))
    
    # Ensure every clip has a duration set
    clips = []
    for f in video_files:
        clip = VideoFileClip(os.path.join("videos", f)).subclip(0, min(clip_length, 60)).resize(height=1920, width=1080)
        clip = clip.set_duration(min(clip_length, 60)) # Explicitly setting duration
        clips.append(clip)
    
    bg_video = concatenate_videoclips(clips, method="compose")
    bg_video = bg_video.set_audio(CompositeAudioClip([voice_audio, bgm]) if bgm else voice_audio)
    bg_video = bg_video.set_duration(voice_audio.duration) # Ensure background duration matches audio
    
    # Subtitles
    text_clips = [TextClip(text, fontsize=70, color='white', font='Arial', stroke_color='black', stroke_width=3, method='caption', size=(900, None)).set_start(start).set_end(end) for start, end, text in parse_srt("subtitles.srt")]
    
    # CTA
    cta_clip = TextClip("Subscribe for more daily facts!", fontsize=60, color='white', font='Arial-Bold', stroke_color='black', stroke_width=2).set_duration(5).set_start(5).set_position(('center', 'bottom'))
    
    # Combine
    final = CompositeVideoClip([bg_video] + text_clips + [cta_clip])
    final = final.set_duration(voice_audio.duration) # Final safety check for duration
    
    final.write_videofile("final_short.mp4", fps=24, codec="libx264", audio_codec="aac")
    final.close()

# --- 6. THE DELIVERY ---
def upload_to_youtube(video_file, title, description):
    time.sleep(random.uniform(0, 900))
    with open("token.pickle", "rb") as token: credentials = pickle.load(token)
    youtube = build("youtube", "v3", credentials=credentials)
    
    media = MediaFileUpload(video_file, chunksize=-1, resumable=True)
    body = {"snippet": {"categoryId": "22", "title": title, "description": description, "tags": ["shorts", "space"]}, "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}}
    
    response = youtube.videos().insert(part="snippet,status", body=body, media_body=media).execute()
    with open("report.txt", "a") as f: f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - Uploaded: {title}\n")
    print(f"✅ Success! Link: https://youtu.be/{response['id']}")

async def main():
    content = generate_script()
    await generate_audio(content["script"])
    download_videos(content["image_prompts"])
    assemble_video()
    upload_to_youtube("final_short.mp4", "Mind-Blowing Space Facts 🌌 #shorts", "Subscribe for daily space facts!")

if __name__ == "__main__":
    asyncio.run(main())
