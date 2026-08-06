import os
import PIL.Image
import numpy as np
import io
import urllib.parse
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
from google import genai
from google.genai import types
import edge_tts
from moviepy.editor import *
import moviepy.video.fx.all as vfx
import moviepy.audio.fx.all as afx
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
import base64
from ai_render_engine import ProceduralAIVideoGenerator, AIVideoEngine

# --- 1. SETUP & SECRETS ---
try:
    from dotenv import load_dotenv
    load_dotenv() 
except ImportError:
    pass 

def require_env(key):
    val = os.environ.get(key)
    if not val:
        raise Exception(f"Missing required env var: {key}")
    return val

GEMINI_API_KEY = require_env("GEMINI_API_KEY")
PIXABAY_API_KEY = require_env("PIXABAY_API_KEY")

gemini_client = genai.Client(api_key=GEMINI_API_KEY)

_AUTH = base64.b64decode("aHR0cHM6Ly93d3cuZ29vZ2xlYXBpcy5jb20vYXV0aC8=").decode("utf-8")
SCOPES = [
    _AUTH + "youtube.upload",
    _AUTH + "yt-analytics.readonly",
    _AUTH + "youtube.force-ssl"
]

def restore_google_secrets():
    if not os.environ.get("GITHUB_ACTIONS"): return
    if os.path.exists("client_secrets.json") and os.path.exists("token.pickle"): return
    client_b64, token_b64 = os.environ.get("CLIENT_SECRETS_BASE64"), os.environ.get("TOKEN_PICKLE_BASE64")
    if client_b64:
        with open("client_secrets.json", "wb") as f: f.write(base64.b64decode(client_b64))
    if token_b64:
        with open("token.pickle", "wb") as f: f.write(base64.b64decode(token_b64))

def get_google_credentials():
    credentials = None
    if os.path.exists("token.pickle"):
        with open("token.pickle", "rb") as token: credentials = pickle.load(token)
    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token: credentials.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("client_secrets.json", SCOPES)
            credentials = flow.run_local_server(port=0)
        with open("token.pickle", "wb") as token: pickle.dump(credentials, token)
    return credentials

VOICE = random.choice([
    "en-US-ChristopherNeural", "en-US-GuyNeural", "en-GB-RyanNeural", 
    "en-US-JennyNeural", "en-US-AriaNeural", "en-GB-SoniaNeural"      
])
OUTPUT_AUDIO = "voiceover.mp3"

TOPIC_HISTORY_FILE = "topic_history.json"
CLIFFHANGER_FILE = "cliffhanger.json"

def load_topic_history():
    if os.path.exists(TOPIC_HISTORY_FILE):
        with open(TOPIC_HISTORY_FILE, "r") as f: return json.load(f)
    return []

def save_topic_history(entry):
    history = load_topic_history()
    history.append(entry)
    with open(TOPIC_HISTORY_FILE, "w") as f: json.dump(history[-100:], f, indent=2)

def fetch_top_performing_titles(credentials, max_results=5):
    try:
        analytics = build("youtubeAnalytics", "v2", credentials=credentials)
        youtube = build("youtube", "v3", credentials=credentials)
        end = time.strftime("%Y-%m-%d")
        start = time.strftime("%Y-%m-%d", time.localtime(time.time() - 30 * 86400))
        report = analytics.reports().query(ids="channel==MINE", startDate=start, endDate=end, metrics="views", dimensions="video", sort="-views", maxResults=max_results).execute()
        rows = report.get("rows", [])
        if not rows: return []
        vids = youtube.videos().list(part="snippet", id=",".join([r[0] for r in rows])).execute()
        return [item["snippet"]["title"] for item in vids.get("items", [])]
    except Exception as e:
        print(f"⚠️ Notice: YouTube Analytics API not enabled or accessible (skipping boost topics): {e}")
        return []

def fetch_high_reach_tags(credentials):
    print("🔍 Fetching tags from your highest-reach videos...")
    fallback_tags = ["#ScienceMystery", "#SpaceMystery", "#Unexplained", "#ScienceFacts", "#Cosmos", "#EarthMystery", "#UnsolvedMysteries", "#AlienLife", "#Astrophysics", "#CosmicSecrets", "#NatureFacts", "#SpaceShorts", "#Shorts", "#DarkMatter", "#Universe", "#DeepEarth", "#Geology", "#PlanetEarth", "#SETI"]
    try:
        youtube = build("youtube", "v3", credentials=credentials)
        search_res = youtube.search().list(part="id", forMine=True, type="video", order="viewCount", maxResults=5).execute()
        video_ids = [item["id"]["videoId"] for item in search_res.get("items", [])]
        
        if not video_ids: return fallback_tags
        
        vid_res = youtube.videos().list(part="snippet", id=",".join(video_ids)).execute()
        dynamic_tags = []
        for item in vid_res.get("items", []):
            dynamic_tags.extend(item["snippet"].get("tags", []))
        
        if not dynamic_tags: return fallback_tags
        
        formatted_tags = list(set([f"#{t.replace(' ', '').replace('#', '')}" for t in dynamic_tags]))
        print(f"✅ Successfully extracted {len(formatted_tags)} dynamic tags from top videos!")
        return formatted_tags[:30]
    except Exception as e:
        print(f"⚠️ Could not fetch dynamic tags, using fallback. Error: {e}")
        return fallback_tags

def log_weekly_analytics(credentials):
    print("📊 Compiling Weekly Channel Performance Report...")
    try:
        analytics = build("youtubeAnalytics", "v2", credentials=credentials)
        youtube = build("youtube", "v3", credentials=credentials)
        end = time.strftime("%Y-%m-%d")
        start = time.strftime("%Y-%m-%d", time.localtime(time.time() - 7 * 86400))
        
        report = analytics.reports().query(
            ids="channel==MINE", startDate=start, endDate=end, 
            metrics="views,estimatedMinutesWatched,averageViewDuration", 
            dimensions="video", sort="-views", maxResults=10
        ).execute()
        
        rows = report.get("rows", [])
        if not rows: return

        vids_info = youtube.videos().list(part="snippet,statistics", id=",".join([r[0] for r in rows])).execute()
        weekly_data = [{"title": item["snippet"]["title"], "views": item["statistics"].get("viewCount", "0"), "likes": item["statistics"].get("likeCount", "0")} for item in vids_info.get("items", [])]
        
        with open("performance_report.json", "w", encoding="utf-8") as f:
            json.dump({"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "top_videos_this_week": weekly_data}, f, indent=2)
        print("✅ Weekly telemetry report saved to 'performance_report.json'")
    except Exception as e:
        print(f"⚠️ Analytics report skipped safely: {e}")

# --- 2. THE AI SCRIPT ENGINE (Trend-Jacked via Google Search) ---
def generate_script(avoid_topics=None, boost_topics=None, dynamic_tags_list=None):
    print("🧠 Asking Gemini to scan live web trends and write the script...")
    
    if not dynamic_tags_list:
        dynamic_tags_list = ["#Shorts", "#Mystery"]
        
    tags_string = ", ".join(dynamic_tags_list)

    avoid_block = f"\n    Do NOT repeat these already-covered topics: {'; '.join(avoid_topics[-20:])}\n" if avoid_topics else ""
    boost_block = f"\n    Reverse-engineer these high-performing topics: {'; '.join(boost_topics)}\n" if boost_topics else ""
    cliffhanger_block = ""
    if os.path.exists(CLIFFHANGER_FILE):
        with open(CLIFFHANGER_FILE, "r") as f: pending_mystery = f.read()
        cliffhanger_block = f"\n    URGENT: Resolve yesterday's cliffhanger in the first sentence: '{pending_mystery}'\n"
        os.remove(CLIFFHANGER_FILE)

    prompt = f"""
    Search the web for current viral mystery trends, unsolved phenomena, or fascinating historical oddities trending right now.
    Write an optimized 20-25 second YouTube Shorts script in English based on a fresh trending concept. No filler.
    {avoid_block}{boost_block}{cliffhanger_block}
    
    Structure Rules:
    1. HOOK (First 3 seconds): Visually and verbally shocking. No "Did you know".
    2. BODY: Fast, punchy fragments. 
    3. THE LOOP: The final sentence must seamlessly bleed back into the first word of the hook.
    
    Monetization & Engagement:
    - "affiliate_keyword": A generic 1-2 word product search term related to this mystery (e.g., "telescope", "metal detector").
    - "poll_question": A highly debatable question about this topic for a Community Tab Poll.
    - "poll_options": An array of 3 short possible answers.

    Cinematic Sound Design:
    - "sfx_cues": Assign 2 or 3 sound effects to specific image clips. Provide the "clip_index" (0 to 9) and a 1-2 word "query" to search on Pixabay (e.g., "heartbeat", "thunder", "metal clank"). 

    Format as valid JSON exactly like this:
    {{
      "script": "...",
      "visual_theme": "...",
      "image_prompts": ["...", "...", "..."],
      "title": "SEO Title #shorts",
      "thumbnail_text": "...",
      "tags": ["#tag1", "#tag2", "#tag3"],
      "description": "...",
      "is_cliffhanger": true/false,
      "cliffhanger_setup": "...",
      "affiliate_keyword": "...",
      "poll_question": "...",
      "poll_options": ["...", "...", "..."],
      "sfx_cues": [
        {{"clip_index": 2, "query": "thunder"}},
        {{"clip_index": 6, "query": "heartbeat"}}
      ]
    }}
    - "tags": YOU MUST select 5 to 8 highly relevant hashtags from this exact proven list of my top performers: {tags_string}.
    """
    for attempt in range(3):
        try:
            response = gemini_client.models.generate_content(
                model='gemini-2.5-flash', 
                contents=prompt, 
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())]
                )
            )
            raw_text = response.text.strip()
            if raw_text.startswith("```"): raw_text = "\n".join(raw_text.split('\n')[1:-1]).strip()
            data = json.loads(raw_text)
            if data.get("is_cliffhanger") and data.get("cliffhanger_setup"):
                with open(CLIFFHANGER_FILE, "w") as f: f.write(data["cliffhanger_setup"])
            return data
        except Exception as e:
            print(f"⚠️ Gemini Attempt {attempt + 1} Failed: {e}")
            if "429" in str(e) or "503" in str(e): time.sleep(45)
            else: time.sleep(5)
    raise Exception("Failed to get response from Gemini.")

def save_community_post(data):
    print("📝 Generating Community Tab Poll asset...")
    try:
        with open("community_poll.txt", "w", encoding="utf-8") as f:
            f.write(f"POLL QUESTION:\n{data.get('poll_question', 'What do you think?')}\n\n")
            f.write("OPTIONS:\n")
            for opt in data.get('poll_options', []):
                f.write(f"- {opt}\n")
        print("✅ Poll saved to 'community_poll.txt'")
    except Exception: pass

# --- 3. AUDIO & SFX SYNC ---
async def generate_audio_and_timestamps(text):
    print(f"🎙️ Generating AI Voiceover ({VOICE})...")
    communicate = edge_tts.Communicate(text, VOICE)
    words = []
    with open(OUTPUT_AUDIO, "wb") as fp:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio": fp.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                words.append({"text": chunk["text"], "start": chunk["offset"] / 10000000.0, "end": (chunk["offset"] + chunk["duration"]) / 10000000.0})
    with open("words.json", "w") as f: json.dump(words, f)

def safe_download(url, out_path, min_bytes=2048):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        r = requests.get(url, headers=headers, timeout=60)
        if r.status_code == 200 and len(r.content) > min_bytes:
            with open(out_path, "wb") as f: f.write(r.content)
            return True
    except Exception: pass
    return False

def download_dynamic_sfx(sfx_cues):
    print("🎵 Directing Cinematic Sound Design...")
    audio_api = base64.b64decode("aHR0cHM6Ly9waXhhYmF5LmNvbS9hcGkvYXVkaW8v").decode("utf-8")
    sfx_map = {}
    for cue in sfx_cues:
        try:
            idx = cue.get("clip_index")
            query = cue.get("query", "").replace(" ", "+")
            if idx is None or not query: continue
            
            res = requests.get(audio_api, params={"key": PIXABAY_API_KEY, "q": query, "per_page": 3}).json()
            if res.get("hits"):
                out_path = f"sfx_dynamic_{idx}.mp3"
                safe_download(res["hits"][0]["audio"], out_path, 1000)
                sfx_map[idx] = out_path
                print(f"   🔉 Loaded SFX for clip {idx}: {query}")
        except Exception: pass
    return sfx_map

def download_ai_visuals(prompts, visual_theme=""):
    print("🎨 Painting Custom AI Visuals...")
    pol_api = base64.b64decode("aHR0cHM6Ly9pbWFnZS5wb2xsaW5hdGlvbnMuYWkvcHJvbXB0Lw==").decode("utf-8")
    for index, prompt in enumerate(prompts):
        full_prompt = f"{prompt}, {visual_theme}, photorealistic, cinematic lighting, full frame, no borders, 8k resolution, octane render"
        for attempt in range(4):
            if safe_download(f"{pol_api}{urllib.parse.quote(full_prompt)}?width=1080&height=1920&nologo=true&seed={random.randint(1, 999999)}", f"media/clip_{index}.jpg", 20000): break
            else: time.sleep(2)

def generate_ai_thumbnail(title, visual_theme="", out_path="thumbnail.jpg"):
    print("🖼️ Painting Custom AI Thumbnail...")
    try:
        pol_api = base64.b64decode("aHR0cHM6Ly9pbWFnZS5wb2xsaW5hdGlvbnMuYWkvcHJvbXB0Lw==").decode("utf-8")
        prompt = f"A dramatic, high-contrast YouTube thumbnail for: {title}. Theme: {visual_theme}. Bold saturated colors, strong focal point, no text, no borders."
        url = f"{pol_api}{urllib.parse.quote(prompt)}?width=1080&height=1920&nologo=true&seed={random.randint(1, 999999)}"
        safe_download(url, out_path, min_bytes=20000)
    except Exception as e: print(f"⚠️ Thumbnail generation failed: {e}")

def download_sfx():
    audio_api = base64.b64decode("aHR0cHM6Ly9waXhhYmF5LmNvbS9hcGkvYXVkaW8v").decode("utf-8")
    if not os.path.exists("background_music.mp3"):
        res = requests.get(audio_api, params={"key": PIXABAY_API_KEY, "q": "cinematic ambient"})
        if res.status_code == 200 and res.json().get("hits"): safe_download(random.choice(res.json()["hits"])["audio"], "background_music.mp3", 20000)
    if not os.path.exists("whoosh.mp3"):
        try: safe_download(requests.get(audio_api, params={"key": PIXABAY_API_KEY, "q": "whoosh"}).json()["hits"][0]["audio"], "whoosh.mp3", 1000)
        except Exception: pass
    if not os.path.exists("pop.mp3"):
        try: safe_download(requests.get(audio_api, params={"key": PIXABAY_API_KEY, "q": "pop bubble"}).json()["hits"][0]["audio"], "pop.mp3", 500)
        except Exception: pass

# --- 4. THE PRO EDITOR ---
def safe_color_grade(get_frame, t):
    frame = get_frame(t).astype(np.float32)
    return np.clip((frame - 127.0) * 1.05 + 127.0 * 1.02, 0, 255).astype('uint8')

def apply_procedural_compositing(clip):
    """Simulates a human editor adding a cinematic vignette and subtle film grain."""
    def add_texture(get_frame, t):
        frame = get_frame(t).astype(np.float32)
        h, w = frame.shape[:2]
        
        # 1. Generate Vignette (Darken edges to guide the eye)
        X, Y = np.meshgrid(np.linspace(-1, 1, w), np.linspace(-1, 1, h))
        radius = np.sqrt(X**2 + Y**2)
        vignette = np.clip(1 - (radius * 0.6), 0, 1)
        
        # 2. Add subtle moving grain
        noise = np.random.normal(loc=0, scale=8, size=frame.shape).astype(np.float32)
        
        # Blend layout
        composited = (frame * vignette[:, :, np.newaxis]) + noise
        return np.clip(composited, 0, 255).astype(np.uint8)
        
    return clip.fl(add_texture)

def render_3d_word(text, fontsize=120, fill=(255, 255, 0), depth=10, depth_color=(120, 95, 0)):
    from PIL import Image, ImageDraw, ImageFont
    try: font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", fontsize)
    except Exception: font = ImageFont.load_default()
    tmp = Image.new("RGBA", (10, 10))
    bbox = ImageDraw.Draw(tmp).textbbox((0, 0), text, font=font, stroke_width=6)
    W, H = (bbox[2] - bbox[0]) + depth + 48, (bbox[3] - bbox[1]) + depth + 48
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    ox, oy = (depth + 24) - bbox[0], (depth + 24) - bbox[1]
    for d in range(depth, 0, -1): draw.text((ox + d, oy + d), text, font=font, fill=depth_color + (255,))
    draw.text((ox, oy), text, font=font, fill=fill + (255,), stroke_width=6, stroke_fill=(0, 0, 0, 255))
    return np.array(img)

def is_high_impact(word):
    return any(char.isdigit() for char in word) or word.strip(".,!?\"'").upper() in ["MILLION", "SECRET", "SHOCKING", "MYSTERY", "ONLY"]

def assemble_video(dynamic_sfx_map):
    print("\n🎬 Assembling cinematic video with Internal Procedural AI Motion Engine...")
    voice_audio = AudioFileClip(OUTPUT_AUDIO)
    audio_tracks = [voice_audio]
    
    with open("words.json", "r") as f: words = json.load(f)
    
    if os.path.exists("background_music.mp3"):
        def filter_audio(get_frame, t):
            vol = np.full(t.shape, 0.20)
            for w in words:
                if (t >= w["start"] - 0.1).any() and (t <= w["end"] + 0.2).any(): vol[(t >= w["start"] - 0.1) & (t <= w["end"] + 0.2)] = 0.03
            return get_frame(t) * vol[:, np.newaxis]
        bgm = AudioFileClip("background_music.mp3").fx(afx.audio_loop, duration=voice_audio.duration).fl(filter_audio)
        audio_tracks.append(bgm)

    media_files = [os.path.join("media", f) for f in os.listdir("media") if f.endswith(".jpg")]
    media_files.sort(key=lambda x: int(os.path.basename(x).split('_')[1].split('.')[0]))
    
    clips = []
    current_time, media_index = 0, 0
    has_sfx = os.path.exists("whoosh.mp3") and os.path.exists("pop.mp3")

    while current_time < voice_audio.duration:
        media_path = media_files[media_index % len(media_files)]
        clip_dur = min(3.0, voice_audio.duration - current_time)
        
        # 1. Synthesize dynamic procedural 3D motion & vector warp frame-by-frame
        warped_clip = ProceduralAIVideoGenerator(media_path, duration=clip_dur, fps=24).to_clip()
        
        # 2. Apply AI frame flickering & color grading
        clip = AIVideoEngine.apply_ai_flicker(warped_clip).fl(safe_color_grade).set_start(current_time)
        
        if current_time > 0: 
            clip = clip.crossfadein(0.2)
        clips.append(clip)
        
        # 3. Add procedural glitch transitions & sound cues
        if has_sfx and current_time > 0: 
            audio_tracks.append(AudioFileClip("whoosh.mp3").set_start(current_time).fx(afx.volumex, 0.3))
            glitch_transition = AIVideoEngine.generate_procedural_glitch(duration=0.15, fps=24).set_start(current_time - 0.075)
            clips.append(glitch_transition)

        if media_index in dynamic_sfx_map:
            audio_tracks.append(AudioFileClip(dynamic_sfx_map[media_index]).set_start(current_time).fx(afx.volumex, 0.5))
            
        current_time += clip_dur
        media_index += 1

    # --- KINETIC TYPOGRAPHY ENGINE ---
    text_clips = []
    
    for i, w in enumerate(words):
        start_t = w["start"]
        # Extend end time seamlessly to the next word so the screen never flickers blank
        end_t = words[i+1]["start"] if i + 1 < len(words) else w["end"] + 0.2
        
        clean_text = w["text"].strip(".,!?\"'").upper()
        if not clean_text: continue
            
        impact = is_high_impact(clean_text)
        
        # Word-level visual hierarchy (Bigger and brighter for high-impact words)
        size = 145 if impact else 120
        fill = (0, 255, 255) if impact else (255, 255, 255) # Cyan for impact, White for normal
        depth_col = (0, 100, 100) if impact else (0, 0, 0)
        
        word_array = render_3d_word(clean_text, fontsize=size, fill=fill, depth=8, depth_color=depth_col)
        
        temp_img_path = f"media/temp_word_{i}.png"
        PIL.Image.fromarray(word_array).save(temp_img_path)
        
        # Center the text directly in the viewer's focal path
        txt_clip = ImageClip(temp_img_path, has_mask=True).set_position(('center', 1150)).set_start(start_t).set_end(end_t)
        
        # Apply the "Editor's Pop": A mathematically calculated zoom-in effect that snaps into place
        txt_clip = txt_clip.resize(lambda t: max(1.0, 1.2 - (t * 4))) 
        
        if impact and has_sfx: 
            audio_tracks.append(AudioFileClip("pop.mp3").set_start(start_t).fx(afx.volumex, 0.2))
            
        text_clips.append(txt_clip)

    retention_bar = ColorClip(size=(1080, 15), color=(255, 255, 0)).set_position(lambda t: (-1080 + int(1080 * (t / voice_audio.duration)), 0)).set_duration(voice_audio.duration)
    
    if os.path.exists("thumbnail.jpg"):
        thumb_clip = ImageClip("thumbnail.jpg").set_duration(0.1).resize(height=1920).crop(x_center=1080/2, y_center=1920/2, width=1080, height=1920)
        text_clips.insert(0, thumb_clip.set_start(0))

    final_audio = CompositeAudioClip(audio_tracks)
    final = CompositeVideoClip([CompositeVideoClip(clips, size=(1080, 1920)).set_audio(final_audio)] + text_clips + [retention_bar], size=(1080, 1920))
    final = apply_procedural_compositing(final)
    # Export with ultra-high quality CRF 18 and 12,000k bitrate
    final.write_videofile(
        "final_short.mp4", 
        fps=24, 
        codec="libx264", 
        audio_codec="aac", 
        threads=4, 
        bitrate="12000k", 
        ffmpeg_params=["-crf", "18", "-preset", "slow"]
    )
    final.close()

# --- 5. UPLOAD & SYNDICATION ---
def get_public_url(video_file):
    print("☁️ Uploading to PixelDrain for Zernio...")
    try:
        with open(video_file, "rb") as f:
            upload_api = base64.b64decode("aHR0cHM6Ly9waXhlbGRyYWluLmNvbS9hcGkvZmlsZQ==").decode("utf-8")
            r = requests.post(upload_api, files={"file": f}, timeout=180)
            if r.status_code in (200, 201) and r.json().get("id"): return f"{upload_api}/{r.json().get('id')}"
    except Exception: pass
    return None

def upload_to_youtube(video_file, data, credentials):
    youtube = build("youtube", "v3", credentials=credentials)
    hashtags = " ".join(f"#{tag.replace(' ', '').replace('#', '')}" for tag in data.get("tags", [])[:8])
    keyword = urllib.parse.quote(data.get("affiliate_keyword", "mystery books"))
    affiliate_link = f"[https://www.amazon.com/s?k=](https://www.amazon.com/s?k=){keyword}&tag=YOUR_AMAZON_TAG_HERE"
    
    body = {"snippet": {"categoryId": "28", "title": data["title"], "description": f"{data['description']}\n\n👇 Gear up:\n{affiliate_link}\n\n{hashtags}", "tags": data["tags"]}, "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}}
    video_id = youtube.videos().insert(part="snippet,status", body=body, media_body=MediaFileUpload(video_file, chunksize=-1, resumable=True)).execute()['id']
    print(f"✅ Success! YouTube Link: [https://youtu.be/](https://youtu.be/){video_id}")
    return video_id, affiliate_link

def post_auto_comment(video_id, script_text, affiliate_link, credentials):
    print("💬 Posting monetized auto-comment...")
    youtube = build("youtube", "v3", credentials=credentials)
    for attempt in range(3):
        try:
            comment_text = gemini_client.models.generate_content(model='gemini-2.5-flash', contents=f"Write ONE short, engaging question to pin in the comments for this script: {script_text}").text.strip().strip('"')
            youtube.commentThreads().insert(part="snippet", body={"snippet": {"videoId": video_id, "topLevelComment": {"snippet": {"textOriginal": f"{comment_text}\n\n🔍 Support the channel: {affiliate_link}"}}}}).execute()
            print(f"✅ Monetized auto-comment posted!")
            return
        except Exception: time.sleep(5)

def repost_via_zernio(video_file, data):
    api_key = os.environ.get("ZERNIO_API_KEY")
    if not api_key: return False
    url = get_public_url(video_file)
    if not url: return False
    
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        acc_r = requests.get(base64.b64decode("aHR0cHM6Ly96ZXJuaW8uY29tL2FwaS92MS9hY2NvdW50cw==").decode("utf-8"), headers=headers, timeout=30)
        platform_entries = [{"platform": acc["platform"], "accountId": acc.get("_id") or acc.get("id")} for acc in acc_r.json().get("accounts", []) if acc.get("platform") in ("instagram", "facebook", "tiktok")]
        if not platform_entries: return False

        r = requests.post(base64.b64decode("aHR0cHM6Ly96ZXJuaW8uY29tL2FwaS92MS9wb3N0cw==").decode("utf-8"), headers=headers, json={"content": f"{data.get('title', '')}\n\n{data.get('description', '')}"[:2200], "mediaItems": [{"type": "video", "url": url}], "platforms": platform_entries, "publishNow": True}, timeout=120)
        if r.status_code in (200, 201, 202, 207): print("🚀 Zernio cross-posting triggered!")
    except Exception: pass

async def main():
    if not os.path.exists("media"): os.makedirs("media")
    restore_google_secrets()
    credentials = get_google_credentials()
    
    # Run the Weekly Analytics Telemetry Logger
    log_weekly_analytics(credentials)
    
    boost_topics = fetch_top_performing_titles(credentials)
    
    # Fetch tags from your highest reach videos
    viral_tags = fetch_high_reach_tags(credentials)
    
    # Pass the tags into the AI generator
    content = generate_script(
        avoid_topics=[h["title"] for h in load_topic_history()], 
        boost_topics=boost_topics,
        dynamic_tags_list=viral_tags
    )
    await generate_audio_and_timestamps(content["script"])
    download_ai_visuals(content["image_prompts"], content.get("visual_theme", ""))
    generate_ai_thumbnail(content.get("thumbnail_text", content.get("title", "")), content.get("visual_theme", ""))
    download_sfx()
    dynamic_sfx_map = download_dynamic_sfx(content.get("sfx_cues", []))
    save_community_post(content)
    
    assemble_video(dynamic_sfx_map)
    
    vid_id, aff_link = upload_to_youtube("final_short.mp4", content, credentials)
    if vid_id:
        post_auto_comment(vid_id, content["script"], aff_link, credentials)
        save_topic_history({"title": content["title"], "video_id": vid_id, "date": time.strftime("%Y-%m-%d")})
    repost_via_zernio("final_short.mp4", content)

if __name__ == "__main__":
    asyncio.run(main())
