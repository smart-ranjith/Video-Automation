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

VOICE_POOL = [
    "en-US-ChristopherNeural", "en-US-GuyNeural", "en-GB-RyanNeural", 
    "en-US-JennyNeural", "en-US-AriaNeural", "en-GB-SoniaNeural"      
]
VOICE = random.choice(VOICE_POOL)
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
        from googleapiclient.discovery import build as build_api
        analytics = build_api("youtubeAnalytics", "v2", credentials=credentials)
        youtube = build_api("youtube", "v3", credentials=credentials)
        end = time.strftime("%Y-%m-%d")
        start = time.strftime("%Y-%m-%d", time.localtime(time.time() - 30 * 86400))
        report = analytics.reports().query(ids="channel==MINE", startDate=start, endDate=end, metrics="views", dimensions="video", sort="-views", maxResults=max_results).execute()
        rows = report.get("rows", [])
        if not rows: return []
        vids = youtube.videos().list(part="snippet", id=",".join([r[0] for r in rows])).execute()
        return [item["snippet"]["title"] for item in vids.get("items", [])]
    except Exception: return []

# --- 2. THE AI SCRIPT ENGINE ---
def generate_script(avoid_topics=None, boost_topics=None):
    print("🧠 Asking Gemini to write the viral script...")
    avoid_block = f"\n    Do NOT repeat these already-covered topics: {'; '.join(avoid_topics[-20:])}\n" if avoid_topics else ""
    boost_block = f"\n    Reverse-engineer these high-performing topics: {'; '.join(boost_topics)}\n" if boost_topics else ""

    cliffhanger_block = ""
    if os.path.exists(CLIFFHANGER_FILE):
        with open(CLIFFHANGER_FILE, "r") as f: pending_mystery = f.read()
        cliffhanger_block = f"\n    URGENT: You left your audience on a cliffhanger yesterday. You MUST resolve this mystery in the first 2 sentences today: '{pending_mystery}'\n"
        os.remove(CLIFFHANGER_FILE)

    prompt = f"""
    You are an expert YouTube Shorts scriptwriter. Write a fast-paced 25-30 second script about a fascinating mystery. 
    Ensure the script is written entirely in English.
    {avoid_block}{boost_block}{cliffhanger_block}
    
    Structure Rules:
    1. HOOK: First 8 words must be shocking. No "did you know".
    2. BODY: Rapid-fire facts. 
    3. CTA: Second-to-last sentence must ask viewers what mystery to explore next.
    4. THE LOOP: The final sentence must be an incomplete thought that seamlessly loops into the hook.

    NEW CLIFFHANGER RULE:
    - Decide if today's video ends on a cliffhanger. If yes, set "is_cliffhanger" to true and write the unresolved question in "cliffhanger_setup".

    Visual Constraints:
    - "image_prompts" must contain exactly 10 visual descriptions. Write them as prompts for an AI Image Generator (e.g., "A glowing blue bioluminescent octopus in a dark trench, cinematic lighting, 8k resolution").

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
      "cliffhanger_setup": "..."
    }}
    """
    for attempt in range(3):
        try:
            response = gemini_client.models.generate_content(model='gemini-2.5-flash', contents=prompt, config=types.GenerateContentConfig(response_mime_type="application/json"))
            raw_text = response.text.strip()
            if raw_text.startswith("```"): raw_text = "\n".join(raw_text.split('\n')[1:-1]).strip()
            data = json.loads(raw_text)
            if data.get("is_cliffhanger") and data.get("cliffhanger_setup"):
                with open(CLIFFHANGER_FILE, "w") as f: f.write(data["cliffhanger_setup"])
            return data
        except Exception as e:
            if "429" in str(e) or "503" in str(e): time.sleep(45)
            else: time.sleep(5)
    raise Exception("Failed to get response from Gemini.")

# --- 3. AUDIO SYNC ---
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
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        r = requests.get(url, headers=headers, timeout=60)
        if r.status_code == 200 and len(r.content) > min_bytes:
            with open(out_path, "wb") as f: f.write(r.content)
            return True
    except Exception: pass
    return False

# --- 4. AI VISUALS & ASSET CACHING ---
def download_ai_visuals(prompts, visual_theme=""):
    print("🎨 Painting Custom AI Visuals (Full Frame)...")
    if not os.path.exists("media"): os.makedirs("media")
    pol_api = base64.b64decode("aHR0cHM6Ly9pbWFnZS5wb2xsaW5hdGlvbnMuYWkvcHJvbXB0Lw==").decode("utf-8")
    
    for index, prompt in enumerate(prompts):
        full_prompt = f"{prompt}, {visual_theme}, photorealistic, ultra detailed, cinematic lighting, full frame, edge-to-edge, no borders"
        for attempt in range(4):
            seed = random.randint(1, 999999)
            url = f"{pol_api}{urllib.parse.quote(full_prompt)}?width=1080&height=1920&nologo=true&seed={seed}"
            if safe_download(url, f"media/clip_{index}.jpg", min_bytes=20000): break
            else: time.sleep(2)

def generate_ai_thumbnail(title, visual_theme="", out_path="thumbnail.jpg"):
    print("🖼️ Painting Custom AI Thumbnail...")
    try:
        pol_api = base64.b64decode("aHR0cHM6Ly9pbWFnZS5wb2xsaW5hdGlvbnMuYWkvcHJvbXB0Lw==").decode("utf-8")
        prompt = f"A dramatic, high-contrast YouTube thumbnail for: {title}. Theme: {visual_theme}. Bold saturated colors, strong focal point, no text, no borders."
        url = f"{pol_api}{urllib.parse.quote(prompt)}?width=1080&height=1920&nologo=true&seed={random.randint(1, 999999)}"
        safe_download(url, out_path, min_bytes=20000)
    except Exception as e: print(f"⚠️ Thumbnail generation failed: {e}")

def download_music():
    if os.path.exists("background_music.mp3"): return "background_music.mp3"
    audio_api = base64.b64decode("aHR0cHM6Ly9waXhhYmF5LmNvbS9hcGkvYXVkaW8v").decode("utf-8")
    response = requests.get(audio_api, params={"key": PIXABAY_API_KEY, "q": "cinematic ambient"})
    if response.status_code == 200 and response.json().get("hits"):
        if safe_download(random.choice(response.json()["hits"])["audio"], "background_music.mp3", 20000): return "background_music.mp3"
    return None

def download_sfx():
    audio_api = base64.b64decode("aHR0cHM6Ly9waXhhYmF5LmNvbS9hcGkvYXVkaW8v").decode("utf-8")
    if not os.path.exists("whoosh.mp3"):
        try: safe_download(requests.get(audio_api, params={"key": PIXABAY_API_KEY, "q": "whoosh"}).json()["hits"][0]["audio"], "whoosh.mp3", 1000)
        except Exception: pass
    if not os.path.exists("pop.mp3"):
        try: safe_download(requests.get(audio_api, params={"key": PIXABAY_API_KEY, "q": "pop bubble"}).json()["hits"][0]["audio"], "pop.mp3", 500)
        except Exception: pass

# --- 5. THE PRO EDITOR ---
def apply_random_motion(clip, clip_duration, w=1080, h=1920):
    from PIL import Image, ImageFilter
    motion = random.choice(['zoom_in', 'zoom_out', 'pan_l', 'pan_r'])
    pan_scale = 1.10  
    def transform(get_frame, t):
        frame = get_frame(t)
        img = Image.fromarray(frame)
        progress = min(max(t / clip_duration, 0), 1)
        if motion in ('zoom_in', 'zoom_out'):
            cur_scale = (1.0 + 0.1 * progress) if motion == 'zoom_in' else (1.1 - 0.1 * progress)
            crop_w, crop_h = max(int(w / cur_scale), 2), max(int(h / cur_scale), 2)
            left, top = (w - crop_w) // 2, (h - crop_h) // 2
            img = img.crop((left, top, left + crop_w, top + crop_h)).resize((w, h), Image.LANCZOS)
        else:
            new_w, new_h = int(w * pan_scale), int(h * pan_scale)
            img = img.resize((new_w, new_h), Image.LANCZOS)
            max_shift = new_w - w
            left = int(max_shift * (1 - progress)) if motion == 'pan_l' else int(max_shift * progress)
            img = img.crop((left, (new_h - h) // 2, left + w, (new_h - h) // 2 + h))
        return np.array(img.filter(ImageFilter.GaussianBlur(radius=0.2)))
    return clip.fl(transform)

def safe_color_grade(get_frame, t):
    frame = get_frame(t).astype(np.float32)
    return np.clip((frame - 127.0) * 1.05 + 127.0 * 1.02, 0, 255).astype('uint8')

def get_caption_font(size):
    from PIL import ImageFont
    for path in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:\\Windows\\Fonts\\arialbd.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"]:
        try: return ImageFont.truetype(path, size)
        except Exception: continue
    return ImageFont.load_default()

def render_3d_word(text, fontsize=120, fill=(255, 255, 0), depth=10, depth_color=(120, 95, 0)):
    from PIL import Image, ImageDraw
    font = get_caption_font(fontsize)
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
    word = word.strip(".,!?\"'")
    if any(char.isdigit() for char in word): return True
    return word.upper() in ["MILLION", "BILLION", "DEADLY", "SECRET", "NEVER", "SHOCKING", "MYSTERY", "ONLY", "FIRST"]

def make_ducking_func(words):
    def filter_audio(get_frame, t):
        audio = get_frame(t)
        vol = np.full(t.shape, 0.20)
        for w in words:
            mask = (t >= w["start"] - 0.1) & (t <= w["end"] + 0.2)
            vol[mask] = 0.03
        return audio * vol[:, np.newaxis]
    return filter_audio

def assemble_video():
    print("\n🎬 Assembling cinematic video...")
    voice_audio = AudioFileClip(OUTPUT_AUDIO)
    audio_tracks = [voice_audio]
    
    with open("words.json", "r") as f: words = json.load(f)
    
    bgm_file = download_music()
    if bgm_file: 
        bgm = AudioFileClip(bgm_file).fx(afx.audio_loop, duration=voice_audio.duration)
        bgm = bgm.fl(make_ducking_func(words))
        audio_tracks.append(bgm)

    media_files = [os.path.join("media", f) for f in os.listdir("media") if f.endswith(".jpg")]
    media_files.sort(key=lambda x: int(os.path.basename(x).split('_')[1].split('.')[0]))
    if not media_files: raise Exception("CRITICAL ERROR: Zero images survived the download process.")

    clips = []
    current_time = 0
    media_index = 0
    has_sfx = os.path.exists("whoosh.mp3") and os.path.exists("pop.mp3")

    while current_time < voice_audio.duration:
        media_path = media_files[media_index % len(media_files)]
        clip_dur = min(3.0, voice_audio.duration - current_time)
        clip = ImageClip(media_path).set_duration(clip_dur).resize(height=1920).crop(x_center=1080/2, y_center=1920/2, width=1080, height=1920)
        clip = apply_random_motion(clip, clip_dur).fl(safe_color_grade).set_start(current_time)
        if current_time > 0: clip = clip.crossfadein(0.2)
        clips.append(clip)
        if has_sfx and current_time > 0: audio_tracks.append(AudioFileClip("whoosh.mp3").set_start(current_time).fx(afx.volumex, 0.3))
        current_time += clip_dur
        media_index += 1

    chunks = []
    curr_chunk = []
    for w in words:
        curr_chunk.append(w)
        if len(curr_chunk) >= 3 or w["text"][-1] in ".,!?":
            chunks.append(curr_chunk)
            curr_chunk = []
    if curr_chunk: chunks.append(curr_chunk)

    text_clips = []
    for i, chunk in enumerate(chunks):
        start_t = chunk[0]["start"]
        end_t = chunks[i+1][0]["start"] if i + 1 < len(chunks) else chunk[-1]["end"] + 0.3
        clean_text = " ".join([w["text"].strip(".,!?\"'").upper() for w in chunk])
        impact = any(is_high_impact(w["text"]) for w in chunk)
        
        size = 110 if len(chunk) > 1 else 130
        fill = (0, 255, 255) if impact else (255, 255, 255)
        depth_col = (0, 100, 100) if impact else (0, 0, 0)
        
        # FIX 1: Add has_mask=True to preserve transparency in MoviePy 1.0.3
        word_array = render_3d_word(clean_text, fontsize=size, fill=fill, depth=8, depth_color=depth_col)
        temp_img_path = f"media/temp_word_{i}.png"
        PIL.Image.fromarray(word_array).save(temp_img_path)
        
        txt_active = ImageClip(temp_img_path, has_mask=True).set_position(('center', 1150)).set_start(start_t).set_end(end_t)
        if impact and has_sfx: audio_tracks.append(AudioFileClip("pop.mp3").set_start(start_t).fx(afx.volumex, 0.2))
        text_clips.append(txt_active)

    retention_bar = ColorClip(size=(1080, 15), color=(255, 255, 0)).set_position(lambda t: (-1080 + int(1080 * (t / voice_audio.duration)), 0)).set_duration(voice_audio.duration)
    
    # FIX 2: Force the AI Thumbnail into the first 0.1 seconds of the timeline so YouTube naturally detects it.
    if os.path.exists("thumbnail.jpg"):
        print("📌 Injecting AI Thumbnail into timeline (Frame 0 Hack)...")
        thumb_clip = ImageClip("thumbnail.jpg").set_duration(0.1).resize(height=1920).crop(x_center=1080/2, y_center=1920/2, width=1080, height=1920)
        text_clips.insert(0, thumb_clip.set_start(0))

    final_audio = CompositeAudioClip(audio_tracks)
    final = CompositeVideoClip([CompositeVideoClip(clips, size=(1080, 1920)).set_audio(final_audio)] + text_clips + [retention_bar], size=(1080, 1920))
    final.write_videofile("final_short.mp4", fps=24, codec="libx264", audio_codec="aac", threads=4, bitrate="8000k", ffmpeg_params=["-maxrate", "8000k", "-bufsize", "16000k", "-crf", "20"])
    final.close()

# --- 6. UPLOAD & SYNDICATION ---
def upload_to_youtube(video_file, data, credentials):
    youtube = build("youtube", "v3", credentials=credentials)
    hashtags = " ".join(f"#{tag.replace(' ', '').replace('#', '')}" for tag in data.get("tags", [])[:8])
    body = {"snippet": {"categoryId": "28", "title": data["title"], "description": f"{data['description']}\n\n{hashtags}", "tags": data["tags"]}, "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}}
    video_id = youtube.videos().insert(part="snippet,status", body=body, media_body=MediaFileUpload(video_file, chunksize=-1, resumable=True)).execute()['id']
    print(f"✅ Success! YouTube Link: [https://youtu.be/](https://youtu.be/){video_id}")
    return video_id

def post_auto_comment(video_id, script_text, credentials):
    print("💬 Generating and posting auto-comment...")
    youtube = build("youtube", "v3", credentials=credentials)
    prompt = f"Read this short YouTube script and write ONE short, highly engaging question to pin in the comments. Keep under 15 words. Script: {script_text}"
    for attempt in range(3):
        try:
            comment_text = gemini_client.models.generate_content(model='gemini-2.5-flash', contents=prompt).text.strip().strip('"')
            youtube.commentThreads().insert(part="snippet", body={"snippet": {"videoId": video_id, "topLevelComment": {"snippet": {"textOriginal": comment_text}}}}).execute()
            print(f"✅ Auto-comment posted successfully: '{comment_text}'")
            return
        except Exception: time.sleep(5)

# FIX 3: Replaced Catbox with PixelDrain (Cloud-Runner Friendly & Direct MP4 Hotlinking)
def get_public_url(video_file):
    print("☁️ Uploading to PixelDrain for Zernio cross-posting...")
    try:
        with open(video_file, "rb") as f:
            # Base64 encoded: "https://pixeldrain.com/api/file"
            upload_api = base64.b64decode("aHR0cHM6Ly9waXhlbGRyYWluLmNvbS9hcGkvZmlsZQ==").decode("utf-8")
            
            r = requests.post(upload_api, files={"file": f}, timeout=180)
            
            if r.status_code in (200, 201):
                file_id = r.json().get("id")
                if file_id:
                    # PixelDrain returns the raw video stream at this exact endpoint
                    direct_link = f"{upload_api}/{file_id}"
                    print(f"   🔗 Direct Link generated: {direct_link}")
                    return direct_link
            else:
                print(f"⚠️ Host rejected upload (Status {r.status_code}): {r.text}")
    except Exception as e:
        print(f"⚠️ Cloud upload failed: {e}")
    return None

def repost_via_zernio(video_file, data):
    api_key = os.environ.get("ZERNIO_API_KEY")
    if not api_key:
        print("ℹ️ Zernio cross-posting skipped (ZERNIO_API_KEY not set).")
        return False
        
    video_public_url = get_public_url(video_file)
    if not video_public_url:
        print("ℹ️ Zernio cross-posting skipped (Failed to get direct public link).")
        return False
    
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        acc_r = requests.get(base64.b64decode("aHR0cHM6Ly96ZXJuaW8uY29tL2FwaS92MS9hY2NvdW50cw==").decode("utf-8"), headers=headers, timeout=30)
        accounts = acc_r.json().get("accounts", acc_r.json() if isinstance(acc_r.json(), list) else [])
        platform_entries = [{"platform": acc["platform"], "accountId": acc.get("_id") or acc.get("id") or acc.get("accountId")} for acc in accounts if acc.get("platform") in ("instagram", "facebook", "tiktok")]
        
        if not platform_entries: 
            print("⚠️ No Facebook/Instagram/TikTok accounts found in Zernio.")
            return False

        caption = f"{data.get('title', '')}\n\n{data.get('description', '')}"
        r = requests.post(base64.b64decode("aHR0cHM6Ly96ZXJuaW8uY29tL2FwaS92MS9wb3N0cw==").decode("utf-8"), headers=headers, json={"content": caption[:2200], "mediaItems": [{"type": "video", "url": video_public_url}], "platforms": platform_entries, "publishNow": True}, timeout=120)
        
        if r.status_code in (200, 201, 202, 207) and "error" not in r.json():
            print("🚀 Successfully triggered cross-posting to Instagram/Facebook via Zernio!")
            return True
        else:
            print(f"⚠️ Zernio Error: {r.json()}")
    except Exception as e: 
        print(f"⚠️ Zernio connection failed: {e}")
    return False

async def main():
    if not os.path.exists("media"): os.makedirs("media")
    restore_google_secrets()  # <--- THIS IS THE MISSING LINE
    credentials = get_google_credentials()
    
    boost_topics = fetch_top_performing_titles(credentials)
    content = generate_script(avoid_topics=[h["title"] for h in load_topic_history()], boost_topics=boost_topics)
    await generate_audio_and_timestamps(content["script"])
        
    download_ai_visuals(content["image_prompts"], content.get("visual_theme", ""))
    generate_ai_thumbnail(content.get("thumbnail_text", content.get("title", "")), content.get("visual_theme", ""))
    download_sfx()
    
    assemble_video()
    
    video_id = upload_to_youtube("final_short.mp4", content, credentials)
    post_auto_comment(video_id, content["script"], credentials)
    save_topic_history({"title": content["title"], "video_id": video_id, "date": time.strftime("%Y-%m-%d")})
    
    repost_via_zernio("final_short.mp4", content)

if __name__ == "__main__":
    asyncio.run(main())
