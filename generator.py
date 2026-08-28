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

def parse_iso8601_duration(duration_str):
    """Parses YouTube's ISO8601 video duration format (e.g. 'PT27S', 'PT1M5S') into seconds."""
    import re
    m = re.match(r"PT(?:(\d+)M)?(?:(\d+)S)?", duration_str or "")
    if not m:
        return 0
    minutes = int(m.group(1) or 0)
    seconds = int(m.group(2) or 0)
    return minutes * 60 + seconds

def fetch_retention_dropoff(credentials):
    """Finds the EXACT second viewers bail on your most recent video, using
    audienceWatchRatio (second-by-second retention curve) rather than just
    aggregate view counts. Far more actionable than 'this topic performed well' -
    tells the script generator exactly where pacing needs to tighten."""
    try:
        youtube = build("youtube", "v3", credentials=credentials)
        analytics = build("youtubeAnalytics", "v2", credentials=credentials)

        search_res = youtube.search().list(part="id", forMine=True, type="video", order="date", maxResults=1).execute()
        items = search_res.get("items", [])
        if not items:
            return None
        video_id = items[0]["id"]["videoId"]

        vid_res = youtube.videos().list(part="contentDetails", id=video_id).execute()
        vid_items = vid_res.get("items", [])
        if not vid_items:
            return None
        duration_sec = parse_iso8601_duration(vid_items[0]["contentDetails"]["duration"])
        if duration_sec <= 0:
            return None

        end = time.strftime("%Y-%m-%d")
        start = time.strftime("%Y-%m-%d", time.localtime(time.time() - 30 * 86400))
        report = analytics.reports().query(
            ids="channel==MINE", startDate=start, endDate=end,
            metrics="audienceWatchRatio", dimensions="elapsedVideoTimeRatio",
            filters=f"video=={video_id}", sort="elapsedVideoTimeRatio"
        ).execute()
        rows = report.get("rows", [])
        if not rows:
            return None

        # Find the first point where retention drops below 50% - that's the real bail-out moment
        for elapsed_ratio, watch_ratio in rows:
            if float(watch_ratio) < 0.5:
                dropoff_second = round(float(elapsed_ratio) * duration_sec)
                print(f"📉 Retention analysis: previous video lost half its audience by ~{dropoff_second}s.")
                return dropoff_second
        return None  # never dropped below 50% - good retention, nothing to flag
    except Exception as e:
        print(f"⚠️ Retention drop-off analysis skipped: {e}")
        return None


def generate_script(avoid_topics=None, boost_topics=None, dynamic_tags_list=None, retention_dropoff_sec=None):
    print("🧠 Asking Gemini to scan live web trends and write the script...")
    
    if not dynamic_tags_list:
        dynamic_tags_list = ["#Shorts", "#Mystery"]
        
    tags_string = ", ".join(dynamic_tags_list)

    avoid_block = f"\n    Do NOT repeat these already-covered topics: {'; '.join(avoid_topics[-20:])}\n" if avoid_topics else ""
    boost_block = f"\n    Reverse-engineer these high-performing topics: {'; '.join(boost_topics)}\n" if boost_topics else ""
    retention_block = ""
    if retention_dropoff_sec:
        retention_block = (f"\n    CRITICAL PACING FIX: your last video lost half its audience by second "
                            f"{retention_dropoff_sec}. Make sure THIS script's pacing stays punchy and escalating "
                            f"through at least that point - no slow/explanatory sentences before then.\n")
    cliffhanger_block = ""
    if os.path.exists(CLIFFHANGER_FILE):
        with open(CLIFFHANGER_FILE, "r") as f: pending_mystery = f.read()
        cliffhanger_block = f"\n    URGENT: Resolve yesterday's cliffhanger in the first sentence: '{pending_mystery}'\n"
        os.remove(CLIFFHANGER_FILE)

    prompt = f"""
    Search the web for current viral mystery trends, unsolved phenomena, or fascinating historical oddities trending right now.
    Write an optimized 20-25 second YouTube Shorts script in English based on a fresh trending concept. No filler.
    {avoid_block}{boost_block}{retention_block}{cliffhanger_block}
    
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
            # response.text is a convenience shortcut that can return None when
            # Google Search grounding produces a multi-part response (grounding
            # metadata mixed with text) rather than one simple text block - that
            # was crashing here with a cryptic 'NoneType has no attribute strip'.
            # Fall back to manually walking the response parts if the shortcut fails.
            raw_text = response.text
            if raw_text is None:
                parts = getattr(response.candidates[0].content, "parts", []) if response.candidates else []
                raw_text = "".join(p.text for p in parts if getattr(p, "text", None))
            if not raw_text:
                raise ValueError("Gemini returned an empty response (no text in any part) - likely a grounding-only or safety-filtered response.")

            raw_text = raw_text.strip()
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

def generate_ai_thumbnail(title, text_hook="", visual_theme="", out_path="thumbnail.jpg"):
    print("🖼️ Painting Custom High-CTR AI Thumbnail...")
    try:
        pol_api = base64.b64decode("aHR0cHM6Ly9pbWFnZS5wb2xsaW5hdGlvbnMuYWkvcHJvbXB0Lw==").decode("utf-8")
        prompt = f"Dramatic high-contrast cinema shot for {title}, {visual_theme}, vibrant saturated colors, central composition, 8k"
        url = f"{pol_api}{urllib.parse.quote(prompt)}?width=1080&height=1920&nologo=true&seed={random.randint(1, 999999)}"
        
        if safe_download(url, out_path, min_bytes=20000):
            # Overlay bold 3D text onto the thumbnail image
            if text_hook:
                base_img = PIL.Image.open(out_path).convert("RGBA")
                
                # Render bold 3D text clip
                clean_hook = text_hook.upper()[:25] # Keep thumbnail hook punchy
                txt_array = render_3d_word(clean_hook, fontsize=110, fill=(255, 220, 0), depth=12, depth_color=(0, 0, 0))
                txt_img = PIL.Image.fromarray(txt_array)
                
                # Center text on top 1/3 of thumbnail layout
                pos_x = (base_img.width - txt_img.width) // 2
                pos_y = (base_img.height // 3) - (txt_img.height // 2)
                
                base_img.paste(txt_img, (max(0, pos_x), max(0, pos_y)), txt_img)
                base_img.convert("RGB").save(out_path, quality=95)
                print("✅ Bold text hook composited onto thumbnail!")
    except Exception as e: 
        print(f"⚠️ Thumbnail generation failed: {e}")

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

def get_watermark_clip(duration, w=1080, h=1920):
    """Small persistent channel watermark, bottom-right corner - gives a
    recognizable cross-video branding thread. Skipped entirely if
    CHANNEL_WATERMARK_TEXT isn't set, rather than showing a generic placeholder."""
    text = os.environ.get("CHANNEL_WATERMARK_TEXT")
    if not text:
        return None
    from PIL import Image, ImageDraw, ImageFont
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 34)
    except Exception:
        font = ImageFont.load_default()
    tmp = Image.new("RGBA", (10, 10))
    bbox = ImageDraw.Draw(tmp).textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad = 16
    img = Image.new("RGBA", (tw + pad * 2, th + pad * 2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.text((pad, pad), text, font=font, fill=(255, 255, 255, 160))  # semi-transparent, unobtrusive
    arr = np.array(img)
    clip = ImageClip(arr).set_duration(duration)
    return clip.set_position((w - arr.shape[1] - 24, h - arr.shape[0] - 140))  # above the retention bar area

def assemble_video(dynamic_sfx_map):
    print("\n🎬 Assembling cinematic video with Internal Procedural AI Motion Engine...")
    voice_audio = AudioFileClip(OUTPUT_AUDIO)
    audio_tracks = [voice_audio]
    
    with open("words.json", "r") as f: words = json.load(f)
    
    # Map the exact seconds where a high-impact word is spoken
    impact_timestamps = [w["start"] for w in words if is_high_impact(w["text"].strip(".,!?\"'").upper())]
    
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
    
    # 1. Parse the data structure to find natural sentence boundaries
    sentence_boundaries = []
    for w in words:
        if w["text"][-1] in ".,!?":
            # Add a 0.3s psychological overlap (The J-Cut offset)
            sentence_boundaries.append(w["end"] + 0.3)
    
    # Ensure the final boundary matches the total audio length
    if not sentence_boundaries or sentence_boundaries[-1] < voice_audio.duration:
        sentence_boundaries.append(voice_audio.duration)

    clips = []
    current_time = 0
    media_index = 0
    boundary_index = 0
    has_sfx = os.path.exists("whoosh.mp3") and os.path.exists("pop.mp3")

    # 2. Loop through the calculated boundaries instead of rigid 3-second blocks
    while current_time < voice_audio.duration and boundary_index < len(sentence_boundaries):
        media_path = media_files[media_index % len(media_files)]
        
        # Calculate dynamic duration based on the next sentence end
        target_end = min(sentence_boundaries[boundary_index], voice_audio.duration)
        clip_dur = target_end - current_time
        
        # Safety fallback: If a sentence is incredibly long, break it up max every 4 seconds
        if clip_dur > 4.0:
            clip_dur = 4.0
        else:
            boundary_index += 1

        # 3. Synthesize dynamic procedural 3D motion with Audio-Reactive Micro-Zooms
        warped_clip = ProceduralAIVideoGenerator(
            media_path, 
            duration=clip_dur, 
            fps=24, 
            start_time=current_time, 
            impact_times=impact_timestamps
        ).to_clip()
        
        # 4. Apply AI frame flickering & color grading
        clip = AIVideoEngine.apply_ai_flicker(warped_clip).fl(safe_color_grade).set_start(current_time)
        
        if current_time > 0: 
            clip = clip.crossfadein(0.25) # Smooth visual blend to complement the audio overlap
        clips.append(clip)
        
        # 5. Add procedural glitch transitions & sound cues on the cuts
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
    extra_layers = [retention_bar]
    watermark = get_watermark_clip(voice_audio.duration)
    if watermark:
        extra_layers.append(watermark)
    final = CompositeVideoClip([CompositeVideoClip(clips, size=(1080, 1920)).set_audio(final_audio)] + text_clips + extra_layers, size=(1080, 1920))
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
    print("☁️ Uploading heavy-duty media for Zernio syndication...")
    
    # Attempt 1: Filebin (Highly reliable, up to 250MB, direct links)
    try:
        print("   -> Trying Filebin...")
        import string, random
        # Generate a random temporary bin
        bin_id = ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))
        filename = os.path.basename(video_file)
        url = f"https://filebin.net/{bin_id}/{filename}"
        
        with open(video_file, "rb") as f:
            headers = {"Content-Type": "video/mp4", "accept": "application/json"}
            r = requests.post(url, data=f, headers=headers, timeout=180)
            if r.status_code in (200, 201):
                print("   ✅ Filebin upload successful!")
                return url
            else:
                print(f"   ⚠️ Filebin rejected the file: Status {r.status_code}")
    except Exception as e:
        print(f"   ⚠️ Filebin connection error: {e}")

    # Attempt 2: Uguu.se (128MB limit fallback)
    try:
        print("   -> Trying Uguu.se...")
        with open(video_file, "rb") as f:
            r = requests.post("https://uguu.se/upload", files={"files[]": f}, timeout=180)
            if r.status_code == 200 and r.json().get("success"):
                direct_url = r.json()["files"][0]["url"]
                print("   ✅ Uguu upload successful!")
                return direct_url
            else:
                print(f"   ⚠️ Uguu rejected the file: {r.text}")
    except Exception as e:
        print(f"   ⚠️ Uguu connection error: {e}")

    # The typo has been fixed! (Changed 'Nones' to 'None')
    return None

def upload_to_youtube(video_file, data, credentials):
    youtube = build("youtube", "v3", credentials=credentials)
    hashtags = " ".join(f"#{tag.replace(' ', '').replace('#', '')}" for tag in data.get("tags", [])[:8])
    keyword = urllib.parse.quote(data.get("affiliate_keyword", "mystery books"))

    # Real Amazon Associates tag required - set AMAZON_AFFILIATE_TAG env var once you
    # have one. Without it, the link is skipped entirely rather than shipping a
    # broken placeholder ("YOUR_AMAZON_TAG_HERE") to every viewer, and the previous
    # markdown-bracket format was never going to render as a link on YouTube anyway
    # (YouTube descriptions are plain text, not markdown).
    amazon_tag = os.environ.get("AMAZON_AFFILIATE_TAG")
    affiliate_block = ""
    affiliate_link = ""
    if amazon_tag:
        affiliate_link = f"https://www.amazon.com/s?k={keyword}&tag={amazon_tag}"
        affiliate_block = f"\n\n👇 Gear up:\n{affiliate_link}"

    body = {"snippet": {"categoryId": "28", "title": data["title"], "description": f"{data['description']}{affiliate_block}\n\n{hashtags}", "tags": data["tags"]}, "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}}
    video_id = youtube.videos().insert(part="snippet,status", body=body, media_body=MediaFileUpload(video_file, chunksize=-1, resumable=True)).execute()['id']
    print(f"✅ Success! YouTube Link: https://youtu.be/{video_id}")
    
    # Force direct thumbnail upload via API
    if os.path.exists("thumbnail.jpg"):
        try:
            youtube.thumbnails().set(videoId=video_id, media_body=MediaFileUpload("thumbnail.jpg")).execute()
            print("🖼️ Custom high-CTR thumbnail successfully pushed to YouTube!")
        except Exception as e:
            print(f"⚠️ Notice: Direct thumbnail API upload skipped: {e}")
    return video_id, affiliate_link

def post_auto_comment(video_id, script_text, affiliate_link, credentials):
    print("💬 Posting monetized auto-comment...")
    youtube = build("youtube", "v3", credentials=credentials)
    support_line = f"\n\n🔍 Support the channel: {affiliate_link}" if affiliate_link else ""
    for attempt in range(3):
        try:
            comment_text = gemini_client.models.generate_content(model='gemini-2.5-flash', contents=f"Write ONE short, engaging question to pin in the comments for this script: {script_text}").text.strip().strip('"')
            youtube.commentThreads().insert(part="snippet", body={"snippet": {"videoId": video_id, "topLevelComment": {"snippet": {"textOriginal": f"{comment_text}{support_line}"}}}}).execute()
            print(f"✅ Monetized auto-comment posted!")
            return
        except Exception: time.sleep(5)

def repost_via_zernio(video_file, data):
    print("\n🚀 Preparing Zernio Multi-Platform Syndication...")
    api_key = os.environ.get("ZERNIO_API_KEY")
    if not api_key:
        print("⚠️ ZERNIO_API_KEY environment variable is missing. Skipping cross-posting.")
        return False
        
    url = get_public_url(video_file)
    if not url:
        print("⚠️ Could not generate public media URL via Filebin/Uguu. Skipping Zernio.")
        return False
    
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        acc_url = base64.b64decode("aHR0cHM6Ly96ZXJuaW8uY29tL2FwaS92MS9hY2NvdW50cw==").decode("utf-8")
        acc_r = requests.get(acc_url, headers=headers, timeout=30)
        
        if acc_r.status_code != 200:
            print(f"⚠️ ZERNIO API Error ({acc_r.status_code}): {acc_r.text}")
            return False

        accounts = acc_r.json().get("accounts", [])
        platform_entries = [{"platform": acc["platform"], "accountId": acc.get("_id") or acc.get("id")} 
                            for acc in accounts if acc.get("platform") in ("instagram", "facebook", "tiktok")]
        
        if not platform_entries:
            print("⚠️ No connected Instagram/TikTok/Facebook accounts found in Zernio dashboard.")
            return False

        post_url = base64.b64decode("aHR0cHM6Ly96ZXJuaW8uY29tL2FwaS92MS9wb3N0cw==").decode("utf-8")
        payload = {
            "content": f"{data.get('title', '')}\n\n{data.get('description', '')}"[:2200],
            "mediaItems": [{"type": "video", "url": url}],
            "platforms": platform_entries,
            "publishNow": True
        }

        try:
            # 300s (was 120s) - Zernio may be transcoding/processing the video
            # server-side before it can respond, which can genuinely take a while.
            # NOTE: deliberately not auto-retrying on timeout - if the first request
            # actually succeeded server-side and we just didn't get the response in
            # time, blindly resubmitting could create a duplicate live post on
            # Instagram/Facebook. Safer to report it and let you check manually.
            r = requests.post(post_url, headers=headers, json=payload, timeout=300)
            if r.status_code in (200, 201, 202, 207):
                print("✅ Zernio cross-posting triggered successfully for Instagram/TikTok!")
                return True
            else:
                print(f"⚠️ ZERNIO Post Creation Error ({r.status_code}): {r.text}")
        except requests.exceptions.Timeout:
            print("⚠️ Zernio post timed out after 300s with no response. The media upload already "
                  "succeeded, so this request likely reached Zernio's servers even though we never "
                  "got a reply - check zernio.com/dashboard to see if it actually posted before "
                  "manually retrying, to avoid double-posting.")
    except Exception as e:
        print(f"⚠️ Zernio Syndication Exception: {e}")
    return False

def sanitize_theme_label(visual_theme, max_words=3, max_chars=40):
    """Gemini sometimes returns visual_theme as a full descriptive sentence
    instead of a short label (e.g. 'Dark, Unsettling Historical Reenactment
    With Abstract Elements...') - that breaks playlist titles. Take just the
    first few words and hard-cap length so it stays usable as a title."""
    import re
    cleaned = re.sub(r"[^\w\s]", "", visual_theme or "").strip()
    words = cleaned.split()[:max_words]
    label = " ".join(words)[:max_chars].strip()
    return label

def add_video_to_themed_playlist(youtube, video_id, visual_theme, max_retries=3):
    """Groups videos into themed playlists (e.g. 'Space Mysteries'). Playlists
    drive session watch time via autoplay-into-next-video - a heavy YouTube
    ranking signal we weren't using before."""
    theme_label = sanitize_theme_label(visual_theme)
    if not theme_label:
        return
    playlist_title = f"{theme_label.title()} Mysteries"

    for attempt in range(max_retries):
        try:
            existing = youtube.playlists().list(part="snippet", mine=True, maxResults=50).execute()
            match = next((p for p in existing.get("items", [])
                          if p["snippet"]["title"].lower() == playlist_title.lower()), None)

            if match:
                playlist_id = match["id"]
            else:
                created = youtube.playlists().insert(part="snippet,status", body={
                    "snippet": {"title": playlist_title, "description": f"Auto-generated collection of {theme_label} content."},
                    "status": {"privacyStatus": "public"},
                }).execute()
                playlist_id = created["id"]
                print(f"📁 Created new playlist: {playlist_title}")

            youtube.playlistItems().insert(part="snippet", body={
                "snippet": {"playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}
            }).execute()
            print(f"✅ Added video to playlist: {playlist_title}")
            return
        except Exception as e:
            # YouTube's API occasionally throws transient 409/503 errors - worth a retry
            # before giving up, rather than failing on the first hiccup.
            if attempt < max_retries - 1:
                print(f"⚠️ Playlist attempt {attempt + 1} failed, retrying: {e}")
                time.sleep(5 * (attempt + 1))
            else:
                print(f"⚠️ Playlist grouping skipped after {max_retries} attempts: {e}")

def reply_to_top_comments(credentials, max_replies=3):
    """Auto-replies to top comments on your MOST RECENT PAST video (not the one
    just uploaded, which has zero comments yet) - light engagement signal, and
    the algorithm does notice reply rate on a channel."""
    try:
        youtube = build("youtube", "v3", credentials=credentials)
        search_res = youtube.search().list(part="id", forMine=True, type="video", order="date", maxResults=1).execute()
        items = search_res.get("items", [])
        if not items:
            return
        video_id = items[0]["id"]["videoId"]

        comments_res = youtube.commentThreads().list(
            part="snippet", videoId=video_id, order="relevance", maxResults=max_replies, textFormat="plainText"
        ).execute()

        for thread in comments_res.get("items", []):
            top_comment = thread["snippet"]["topLevelComment"]["snippet"]["textDisplay"]
            thread_id = thread["id"]
            # Skip if it already has replies (avoid double-replying on repeat runs)
            if thread["snippet"].get("totalReplyCount", 0) > 0:
                continue
            try:
                reply_text = gemini_client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=f"Write ONE short, warm, engaging reply (under 15 words) to this YouTube comment: \"{top_comment}\""
                ).text.strip().strip('"')
                youtube.comments().insert(part="snippet", body={
                    "snippet": {"parentId": thread_id, "textOriginal": reply_text}
                }).execute()
                print(f"💬 Replied to a comment: {reply_text}")
            except Exception as e:
                print(f"⚠️ Reply skipped for one comment: {e}")
    except Exception as e:
        print(f"⚠️ Comment engagement skipped: {e}")

async def main():
    if not os.path.exists("media"): os.makedirs("media")
    restore_google_secrets()
    credentials = get_google_credentials()
    
    # Run the Weekly Analytics Telemetry Logger
    log_weekly_analytics(credentials)

    # Engage with existing audience on the previous video before making a new one
    reply_to_top_comments(credentials)

    boost_topics = fetch_top_performing_titles(credentials)
    retention_dropoff_sec = fetch_retention_dropoff(credentials)

    # Fetch tags from your highest reach videos
    viral_tags = fetch_high_reach_tags(credentials)
    
    # Pass the tags into the AI generator
    content = generate_script(
        avoid_topics=[h["title"] for h in load_topic_history()], 
        boost_topics=boost_topics,
        dynamic_tags_list=viral_tags,
        retention_dropoff_sec=retention_dropoff_sec
    )
    await generate_audio_and_timestamps(content["script"])
    download_ai_visuals(content["image_prompts"], content.get("visual_theme", ""))
    
    # Corrected High-CTR Thumbnail generation (runs BEFORE video assembly and YouTube upload)
    generate_ai_thumbnail(
        title=content.get("title", ""), 
        text_hook=content.get("thumbnail_text", content.get("title", "")), 
        visual_theme=content.get("visual_theme", "")
    )
    
    download_sfx()
    dynamic_sfx_map = download_dynamic_sfx(content.get("sfx_cues", []))
    save_community_post(content)
    
    assemble_video(dynamic_sfx_map)
    
    vid_id, aff_link = upload_to_youtube("final_short.mp4", content, credentials)
    if vid_id:
        post_auto_comment(vid_id, content["script"], aff_link, credentials)
        save_topic_history({"title": content["title"], "video_id": vid_id, "date": time.strftime("%Y-%m-%d")})
        youtube_client = build("youtube", "v3", credentials=credentials)
        add_video_to_themed_playlist(youtube_client, vid_id, content.get("visual_theme", ""))
    repost_via_zernio("final_short.mp4", content)

if __name__ == "__main__":
    asyncio.run(main())
