import os
import json
import time
import random
import re
import requests
import asyncio
import pickle
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
# import google.generativeai as genai
# from google.generativeai import types
import edge_tts
from moviepy.editor import *
import moviepy.video.fx.all as vfx
import moviepy.audio.fx.all as afx
from google import genai  # <--- THIS IS THE CORRECT IMPORT FOR THE NEW LIBRARY
from google.genai import types

# --- 1. SETUP & SECRETS ---
os.environ["GEMINI_API_KEY"] = os.environ.get("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY")
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "YOUR_PEXELS_API_KEY")
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY", "YOUR_PIXABAY_API_KEY")

# --- CONFIG ---
VOICE = "en-US-ChristopherNeural"
OUTPUT_AUDIO = "voiceover.mp3"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

# --- 2. THE BRAIN (GEMINI) ---
def generate_script():
    print("🧠 Asking Gemini to write the script...")
    client = genai.Client()
    prompt = """
    Write a 45-second YouTube Short script about a space phenomenon.
    Format as valid JSON with the following keys:
    {
      "script": "The full script text...",
      "image_prompts": ["prompt 1", "prompt 2", ...],
      "title": "A high-CTR title (max 60 chars) with keywords...",
      "tags": ["tag1", "tag2", "tag3", ...],
      "description": "A 2-line optimized description..."
    }
    """
    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.7)
    )
    return json.loads(response.text)

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
            if len(data.get("videos", [])) > 0:
                video_url = data["videos"][0]["video_files"][0]["link"]
                with open(f"videos/clip_{index}.mp4", "wb") as f: f.write(requests.get(video_url).content)

def download_music():
    url = f"https://pixabay.com/api/audio/?key={PIXABAY_API_KEY}&q=cinematic+ambient"
    response = requests.get(url)
    if response.status_code == 200:
        data = response.json()
        if len(data.get("hits", [])) > 0:
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
    print("\n🎬 Assembling cinematic video...")
    voice_audio = AudioFileClip(OUTPUT_AUDIO)
    bgm_file = download_music()
    bgm = AudioFileClip(bgm_file).fx(afx.audio_loop, duration=voice_audio.duration).fx(afx.volumex, 0.1) if bgm_file else None
    
    subtitles = parse_srt("subtitles.srt")
    video_files = [os.path.join("videos", f) for f in os.listdir("videos") if f.endswith(".mp4")]
    
    # SAFETY CHECK: If no videos, stop to prevent IndexError
    if not video_files:
        raise Exception("CRITICAL ERROR: No video files found in /videos folder. Check API download step.")
    
    clips = []
    for i, (start, end, text) in enumerate(subtitles):
        video_path = video_files[i % len(video_files)]
        clip = VideoFileClip(video_path).resize(height=1920, width=1080)
        clip = clip.subclip(0, min(end - start, clip.duration))
        clip = clip.fx(vfx.colorx, 1.1).fx(vfx.lum_contrast, 0, 10, 1.1).fadein(0.2).fadeout(0.2)
        clip = clip.set_start(start).set_duration(end - start)
        clips.append(clip)
    
    bg_video = CompositeVideoClip(clips).set_audio(CompositeAudioClip([voice_audio, bgm]) if bgm else voice_audio)
    
    text_clips = []
    for start, end, text in subtitles:
        txt = TextClip(text, fontsize=80, color='yellow', font='Arial-Bold', 
                       stroke_color='black', stroke_width=4, method='caption', size=(900, None))
        txt = txt.set_start(start).set_duration(end - start).set_position(('center', 'center'))
        text_clips.append(txt)
    
    final = CompositeVideoClip([bg_video] + text_clips)
    final.write_videofile("final_short.mp4", fps=24, codec="libx264", audio_codec="aac")
    final.close()

# --- 6. THE DELIVERY ---
def upload_to_youtube(video_file, data):
    with open("token.pickle", "rb") as token: credentials = pickle.load(token)
    youtube = build("youtube", "v3", credentials=credentials)
    media = MediaFileUpload(video_file, chunksize=-1, resumable=True)
    body = {"snippet": {"categoryId": "28", "title": data["title"], "description": data["description"], "tags": data["tags"]}, 
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}}
    response = youtube.videos().insert(part="snippet,status", body=body, media_body=media).execute()
    print(f"✅ Success! Link: https://youtu.be/{response['id']}")

async def main():
    content = generate_script() 
    await generate_audio(content["script"])
    download_videos(content["image_prompts"])
    assemble_video()
    upload_to_youtube("final_short.mp4", content)
    
if __name__ == "__main__":
    asyncio.run(main())
