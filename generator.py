import os
import json
import asyncio
import requests
import math
import re
import pickle
import edge_tts
from google import genai
from google.genai import types
from moviepy.editor import VideoFileClip, AudioFileClip, concatenate_videoclips, TextClip, CompositeVideoClip
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.http import MediaFileUpload

# --- 1. SETUP & SECRETS ---
# This pulls your secure keys from GitHub Actions (or your local computer)
os.environ["GEMINI_API_KEY"] = os.environ.get("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY")
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "YOUR_PEXELS_API_KEY")
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY", "56482907-f2ad06f4b528c159b29ad37c6")

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
    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.7
        )
    )
    return json.loads(response.text)

# --- 3. THE VOICE & SUBTITLES (EDGE-TTS) ---
async def generate_audio(text):
    print(f"🎙️ Generating voiceover and subtitle tracks ({VOICE})...")
    communicate = edge_tts.Communicate(text, VOICE)
    submaker = edge_tts.SubMaker()
    
    with open(OUTPUT_AUDIO, "wb") as fp:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                fp.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                submaker.feed(chunk)
                
    with open("subtitles.srt", "w", encoding="utf-8") as f:
        f.write(submaker.get_srt())
        
    print(f"✅ Success! Audio and subtitles.srt saved.")

# --- 4. THE VISUALS (PEXELS API) ---
def download_videos(prompts):
    print("\n🎬 Searching and downloading videos from Pexels...")
    headers = {"Authorization": PEXELS_API_KEY}
    
    if not os.path.exists("videos"):
        os.makedirs("videos")

    for index, prompt in enumerate(prompts):
        print(f"🔍 Searching for: {prompt}")
        url = f"https://api.pexels.com/videos/search?query={prompt}&orientation=portrait&per_page=1"
        response = requests.get(url, headers=headers)
        
        if response.status_code == 200:
            data = response.json()
            if len(data["videos"]) > 0:
                video_url = data["videos"][0]["video_files"][0]["link"]
                print(f"⬇️ Downloading video {index + 1}...")
                vid_data = requests.get(video_url).content
                with open(f"videos/clip_{index}.mp4", "wb") as f:
                    f.write(vid_data)
                print(f"✅ Saved clip_{index}.mp4")
            else:
                print(f"⚠️ No video found for '{prompt}'")
        else:
            print(f"❌ Error connecting to Pexels: {response.status_code}")

# --- Helper: Parse SRT Subtitles ---
def parse_srt(filename):
    with open(filename, "r", encoding="utf-8") as f:
        content = f.read()
    pattern = re.compile(r"(\d+)\n(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})\n(.*)")
    matches = pattern.findall(content)
    
    def time_to_sec(t_str):
        h, m, s = t_str.split(':')
        s, ms = s.split(',')
        return int(h)*3600 + int(m)*60 + int(s) + int(ms)/1000.0

    subtitles = []
    for match in matches:
        start = time_to_sec(match[1])
        end = time_to_sec(match[2])
        text = match[3].strip()
        subtitles.append((start, end, text))
    return subtitles

# --- 5. THE EDITOR (MOVIEPY) ---
def assemble_video():
    print("\n🎬 Assembling final video with animated captions and auto-music...")
    
    # 1. Load the Voiceover
    voice_audio = AudioFileClip(OUTPUT_AUDIO)
    audio_duration = voice_audio.duration
    
    # 2. Download and Load Background Music dynamically!
    bgm_file = download_music()
    
    if bgm_file:
        import moviepy.audio.fx.all as afx
        bgm = AudioFileClip(bgm_file)
        
        # Loop the music to match the voiceover length exactly
        bgm = bgm.fx(afx.audio_loop, duration=audio_duration)
        
        # Drop the volume to 10% so the AI voice remains clear
        bgm = bgm.fx(afx.volumex, 0.1)
        
        # Mix them together
        final_audio = CompositeAudioClip([voice_audio, bgm])
    else:
        # Fallback just in case Pixabay goes down
        final_audio = voice_audio
        
# 3. Prepare the Video Clips (Proceed with video stitching as normal)
# ... rest of your assemble_video code here ...
    
    video_folder = "videos"
    video_files = [f for f in os.listdir(video_folder) if f.endswith(".mp4")]
    
    clip_length = math.ceil(audio_duration / len(video_files))
    video_clips = []
    
    for i, file in enumerate(video_files):
        filepath = os.path.join(video_folder, file)
        clip = VideoFileClip(filepath)
        end_time = min(clip_length, clip.duration)
        trimmed_clip = clip.subclip(0, end_time)
        resized_clip = trimmed_clip.resize(height=1920, width=1080)
        video_clips.append(resized_clip)

    background_video = concatenate_videoclips(video_clips, method="compose")
    background_video = background_video.set_audio(audio)
    
    # Add subtitles overlay
    subtitle_data = parse_srt("subtitles.srt")
    text_clips = []
    
    for start, end, text in subtitle_data:
        # Note: If GitHub Actions throws a font error, change 'Arial' to 'Ubuntu' or remove the font parameter completely.
        txt_clip = TextClip(
            text, 
            fontsize=70, 
            color='white', 
            font='Arial', 
            stroke_color='black', 
            stroke_width=3,
            method='caption',
            size=(900, None)
        )
        txt_clip = txt_clip.set_start(start).set_end(end).set_position(('center', 'center'))
        text_clips.append(txt_clip)
    
    final_video = CompositeVideoClip([background_video] + text_clips)
    
    print("⏳ Rendering final_short.mp4 with subtitles...")
    final_video.write_videofile("final_short.mp4", fps=24, codec="libx264", audio_codec="aac", logger=None)
    
    final_video.close()
    audio.close()
    print("✅ Complete video with centered subtitles rendered successfully!")

# --- 6. THE DELIVERY (YOUTUBE API) ---
def upload_to_youtube(video_file, title, description):
    print("\n🚀 Starting YouTube Upload Process...")
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

    request_body = {
        "snippet": {
            "categoryId": "22",
            "title": title,
            "description": description,
            "tags": ["shorts", "space", "facts", "automation"]
        },
        "status": {
            "privacyStatus": "public",  # Set to public for instant publishing
            "selfDeclaredMadeForKids": False
        }
    }

    media_file = MediaFileUpload(video_file, chunksize=-1, resumable=True)
    
    print("⏳ Uploading to YouTube...")
    response = youtube.videos().insert(
        part="snippet,status",
        body=request_body,
        media_body=media_file
    ).execute()

    print(f"✅ Success! Video uploaded to your channel.")
    print(f"🔗 Link: https://youtu.be/{response['id']}")

# --- THE MUSIC (PIXABAY API) ---
def download_music():
    print("\n🎵 Fetching copyright-free background music from Pixabay...")
    
    # We search for cinematic or ambient music suitable for voiceovers
    url = f"https://pixabay.com/api/audio/?key={PIXABAY_API_KEY}&q=cinematic+ambient"
    response = requests.get(url)
    
    if response.status_code == 200:
        data = response.json()
        if len(data["hits"]) > 0:
            # Pick a random track from the search results
            import random
            track = random.choice(data["hits"])
            audio_url = track["audio"]
            
            print(f"⬇️ Downloading music: {track['name']}...")
            audio_data = requests.get(audio_url).content
            
            with open("background_music.mp3", "wb") as f:
                f.write(audio_data)
            print("✅ Background music saved!")
            return "background_music.mp3"
        else:
            print("⚠️ No music found for that query.")
    else:
        print(f"❌ Error connecting to Pixabay: {response.status_code}")
    
    return None

# --- MASTER FUNCTION ---
async def main():
    content = generate_script()
    
    print("\n--- GENERATED SCRIPT ---")
    print(content["script"])
    
    await generate_audio(content["script"])
    download_videos(content["image_prompts"])
    assemble_video()
    
    # Upload the video (we can pass a dynamic title based on the niche!)
    upload_to_youtube("final_short.mp4", "Mind-Blowing Space Facts 🌌 #shorts #space", "Generated entirely by AI. Subscribe for daily videos!")
    
    print("\n🎉 AUTOMATION COMPLETE! See you tomorrow.")

if __name__ == "__main__":
    asyncio.run(main())
