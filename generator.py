import os
import PIL.Image
import numpy as np
import io
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
# Upgraded to the new genai SDK to avoid future deprecation crashes
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
        raise Exception(f"Missing required env var: {key}. Set it in GitHub Actions secrets, "
                         f"or locally create a .env file with {key}=your_value and run: pip install python-dotenv")
    return val

GEMINI_API_KEY = require_env("GEMINI_API_KEY")
PEXELS_API_KEY = require_env("PEXELS_API_KEY")
PIXABAY_API_KEY = require_env("PIXABAY_API_KEY")

# Initialize new Client from the updated google-genai SDK
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/yt-analytics.readonly"]

def restore_google_secrets():
    """Decode base64 secrets from env vars into files GH Actions run needs.
    Skips gracefully if files already exist on disk (local development)."""
    if os.path.exists("client_secrets.json") and os.path.exists("token.pickle"):
        print("✅ Found local client_secrets.json and token.pickle. Skipping cloud extraction.")
        return

    client_b64 = os.environ.get("CLIENT_SECRETS_BASE64")
    token_b64 = os.environ.get("TOKEN_PICKLE_BASE64")

    if client_b64:
        with open("client_secrets.json", "wb") as f:
            f.write(base64.b64decode(client_b64))
    if token_b64:
        with open("token.pickle", "wb") as f:
            f.write(base64.b64decode(token_b64))

def get_google_credentials():
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
    return credentials

VOICE_POOL = ["en-US-ChristopherNeural", "en-US-GuyNeural", "en-GB-RyanNeural"]
VOICE = random.choice(VOICE_POOL)
OUTPUT_AUDIO = "voiceover.mp3"

# --- TOPIC MEMORY ---
TOPIC_HISTORY_FILE = "topic_history.json"

def load_topic_history():
    if os.path.exists(TOPIC_HISTORY_FILE):
        with open(TOPIC_HISTORY_FILE, "r") as f:
            return json.load(f)
    return []

def save_topic_history(entry):
    history = load_topic_history()
    history.append(entry)
    history = history[-100:]  
    with open(TOPIC_HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)

def fetch_top_performing_titles(credentials, max_results=5):
    try:
        from googleapiclient.discovery import build as build_api
        analytics = build_api("youtubeAnalytics", "v2", credentials=credentials)
        youtube = build_api("youtube", "v3", credentials=credentials)

        end = time.strftime("%Y-%m-%d")
        start = time.strftime("%Y-%m-%d", time.localtime(time.time() - 30 * 86400))

        report = analytics.reports().query(
            ids="channel==MINE", startDate=start, endDate=end,
            metrics="views", dimensions="video", sort="-views", maxResults=max_results
        ).execute()

        rows = report.get("rows", [])
        if not rows:
            return []

        video_ids = [row[0] for row in rows]
        vids = youtube.videos().list(part="snippet", id=",".join(video_ids)).execute()
        return [item["snippet"]["title"] for item in vids.get("items", [])]
    except Exception as e:
        print(f"⚠️ Analytics fetch skipped (no data yet or scope missing): {e}")
        return []

# --- 2. THE AI SCRIPT ENGINE ---
def generate_script(avoid_topics=None, boost_topics=None):
    print("🧠 Asking Gemini to write the viral script...")

    avoid_block = ""
    if avoid_topics:
        avoid_list = "; ".join(avoid_topics[-20:])  
        avoid_block = f"\n    Do NOT repeat these already-covered topics/titles: {avoid_list}\n"

    boost_block = ""
    if boost_topics:
        boost_list = "; ".join(boost_topics)
        boost_block = f"\n    These recent topics performed best with viewers - lean toward similar style/subject (but do not repeat them exactly): {boost_list}\n"

    prompt = f"""
    You are an expert YouTube Shorts scriptwriter known for extreme viewer retention across ALL ages (kids to elderly). 
    Write a highly engaging, fast-paced 25-30 second script about a fascinating science, space, or nature mystery. 
    Ensure the script and all metadata are written entirely in English.
    {avoid_block}{boost_block}
    Strict Structure Rules:
    1. THE HOOK: First 8 words must be a shocking claim, surprising number, or contradiction. NO "did you know" / "have you ever" openers. Second person "you" early.
    2. THE BODY: Deliver rapid-fire, mind-blowing facts. No dead air, no filler.
    3. THE LOOP: The final sentence must be an incomplete thought that grammatically loops seamlessly right back into the first word of the hook.

    Psychological Engagement Rules (this is what turns viewers into subscribers, apply ALL of them):
    - CURIOSITY GAP: Never fully answer the hook's question immediately. Open a question in the first sentence, delay the payoff to the middle of the script - the brain craves closure and keeps watching to get it.
    - SPECIFICITY OVER VAGUENESS: Use exact numbers, distances, temperatures, ages, counts wherever possible ("4.6 billion years" beats "a very long time") - specific facts feel more credible and more shareable than vague claims.
    - ESCALATING STAKES: Each fact in the body should feel BIGGER or stranger than the last, building toward a peak right before the loop - a flat list of equally-weighted facts loses attention, an escalating one doesn't.
    - RELATABILITY ANCHOR: At least once, connect the cosmic/strange fact back to something the viewer can personally sense or imagine ("right now, above your head...") - this personalizes an abstract fact and increases retention.
    - AWE + FEAR BLEND: The best-performing science shorts blend genuine awe ("this is beautiful/incredible") with a small edge of unease or existential surprise ("...and it could happen again") - pure awe is pleasant but forgettable, awe+edge is memorable and shareable.
    - IMPLICIT SUBSCRIBE TRIGGER: Structure the second-to-last fact as the biggest "wait, WHAT?" moment of the whole script - viewers who are hit with the strongest surprise right before a loop are most likely to rewatch and subscribe, so don't save your best fact for a soft ending, save it for right before the loop.

    Language Rules (critical for all-age appeal):
    - Simple, universal vocabulary understandable by both children and adults.
    - If a technical/scientific term is used, explain it in the SAME sentence with a simple analogy.
    - Universal wonder/surprise emotion, not niche humor, slang, or cultural references.

    Technical Constraints:
    - The "script" must be exactly 55 to 65 words (fits 25-30 seconds spoken).
    - First decide ONE consistent visual world for this whole video (e.g. "deep space nebula", "ocean abyss", "volcanic planet surface", "arctic ice cave") - put it in "visual_theme".
    - The "image_prompts" array must contain exactly 10 visual search terms for stock footage, each 4-6 words, cinematic and specific, and ALL 10 must visually belong to the SAME "visual_theme" world - same setting, same color palette, same lighting mood. Do NOT mix unrelated settings.
    - Mix shot types within that one theme: wide establishing, close-up detail, slow-motion/action - variety of shots, not variety of subjects.
    - Avoid abstract concepts - describe a visual proxy instead, staying inside the chosen visual_theme.
    - "thumbnail_text" = 3-4 word punchy bold overlay text for the first-frame thumbnail, separate from title.
    - "description" should open with a curiosity-gap question, then a natural one-line subscribe nudge.

    Format the output as strictly valid JSON exactly like this:
    {{
      "script": "...",
      "visual_theme": "...",
      "image_prompts": ["...", "...", "..."],
      "title": "Catchy SEO Title #shorts",
      "thumbnail_text": "...",
      "tags": ["tag1", "tag2", "tag3"],
      "description": "SEO description."
    }}
    """
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = gemini_client.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json")
            )
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

# --- 4. MEDIA DOWNLOADER ---
def safe_download(url, out_path, min_bytes=2048, headers=None):
    try:
        r = requests.get(url, headers=headers, timeout=30)
    except requests.RequestException as e:
        print(f"⚠️ Download failed ({url[:60]}...): {e}")
        return False
    if r.status_code != 200:
        print(f"⚠️ Download got HTTP {r.status_code} ({url[:60]}...) - skipping.")
        return False
    content = r.content
    if len(content) < min_bytes:
        print(f"⚠️ Download suspiciously small ({len(content)} bytes) - skipping.")
        return False
    with open(out_path, "wb") as f:
        f.write(content)
    return True

def pick_best_video_file(video_files):
    portrait = [v for v in video_files if v.get("height", 0) > v.get("width", 0)]
    candidates = portrait if portrait else video_files
    candidates = sorted(candidates, key=lambda v: v.get("width", 0), reverse=True)
    for v in candidates:
        if v.get("width", 0) >= 1080:
            return v
    return candidates[0] if candidates else None

FALLBACK_KEYWORDS = ["space stars", "ocean waves", "forest nature", "city lights night", "abstract particles",
                     "aurora borealis sky", "desert dunes sunset", "clouds timelapse sky", "mountain fog morning",
                     "underwater coral reef", "milky way night sky", "waterfall slow motion"]

def download_videos(prompts, visual_theme=""):
    headers = {"Authorization": PEXELS_API_KEY}
    if not os.path.exists("media"): os.makedirs("media")

    for index, prompt in enumerate(prompts):
        got_media = False
        queries_to_try = [f"{prompt} {visual_theme}".strip()] if visual_theme else []
        queries_to_try.append(prompt)

        videos = []
        for q in queries_to_try:
            vid_url = f"https://api.pexels.com/videos/search?query={q}&orientation=portrait&per_page=5"
            response = requests.get(vid_url, headers=headers)
            if response.status_code == 200 and response.json().get("videos"):
                videos = response.json()["videos"]
                break

        if videos:
            sorted_videos = sorted(videos, key=lambda v: v.get("duration", 0), reverse=True)
            top_candidates = sorted_videos[:3] if len(sorted_videos) >= 3 else sorted_videos
            best_video = random.choice(top_candidates)
            video_file = pick_best_video_file(best_video["video_files"])
            if video_file:
                got_media = safe_download(video_file["link"], f"media/clip_{index}.mp4", min_bytes=50_000)

        if not got_media:
            print(f"⚠️ No usable video for '{prompt}'. Falling back to Image...")
            img_url = f"https://api.pexels.com/v1/search?query={prompt}&orientation=portrait&per_page=1"
            img_response = requests.get(img_url, headers=headers)
            if img_response.status_code == 200 and img_response.json().get("photos"):
                photo_url = img_response.json()["photos"][0]["src"]["portrait"]
                got_media = safe_download(photo_url, f"media/clip_{index}.jpg", min_bytes=5_000)

        if not got_media:
            fallback = random.choice(FALLBACK_KEYWORDS)
            print(f"⚠️ Prompt '{prompt}' failed. Using fallback keyword '{fallback}'...")
            fb_url = f"https://api.pexels.com/v1/search?query={fallback}&orientation=portrait&per_page=1"
            fb_response = requests.get(fb_url, headers=headers)
            if fb_response.status_code == 200 and fb_response.json().get("photos"):
                photo_url = fb_response.json()["photos"][0]["src"]["portrait"]
                safe_download(photo_url, f"media/clip_{index}.jpg", min_bytes=5_000)

def download_music():
    url = f"https://pixabay.com/api/audio/?key={PIXABAY_API_KEY}&q=cinematic+ambient"
    response = requests.get(url)
    if response.status_code == 200 and response.json().get("hits"):
        track = random.choice(response.json()["hits"])
        if safe_download(track["audio"], "background_music.mp3", min_bytes=20_000):
            return "background_music.mp3"
    return None

# --- 5. THE PRO EDITOR ---
def make_vignette_clip(duration, w=1080, h=1920):
    yy, xx = np.mgrid[0:h, 0:w]
    cx, cy = w / 2, h / 2
    max_dist = ((cx) ** 2 + (cy) ** 2) ** 0.5
    dist = ((xx - cx) ** 2 + (yy - cy) ** 2) ** 0.5
    norm = np.clip(dist / max_dist, 0, 1)
    inner_safe = 0.65  
    falloff = np.clip((norm - inner_safe) / (1 - inner_safe), 0, 1)
    alpha = (falloff ** 3 * 90).astype("uint8")  
    frame = np.zeros((h, w, 3), dtype="uint8")
    vclip = ImageClip(frame).set_duration(duration)
    vclip = vclip.set_mask(ImageClip(alpha, ismask=True).set_duration(duration))
    return vclip.set_opacity(1.0)

def apply_random_motion(clip, clip_duration, w=1080, h=1920):
    from PIL import Image, ImageFilter
    motion = random.choice(['zoom_in', 'zoom_out', 'pan_l', 'pan_r'])
    pan_scale = 1.08  

    def transform(get_frame, t):
        frame = get_frame(t)
        img = Image.fromarray(frame)
        progress = min(max(t / clip_duration, 0), 1)

        if motion in ('zoom_in', 'zoom_out'):
            cur_scale = (1.0 + 0.06 * progress) if motion == 'zoom_in' else (1.06 - 0.06 * progress)
            crop_w = max(int(w / cur_scale), 2)
            crop_h = max(int(h / cur_scale), 2)
            left = (w - crop_w) // 2
            top = (h - crop_h) // 2
            img = img.crop((left, top, left + crop_w, top + crop_h))
            img = img.resize((w, h), Image.LANCZOS)
        else:
            new_w, new_h = int(w * pan_scale), int(h * pan_scale)
            img = img.resize((new_w, new_h), Image.LANCZOS)
            max_shift = new_w - w
            left = int(max_shift * (1 - progress)) if motion == 'pan_l' else int(max_shift * progress)
            top = (new_h - h) // 2
            img = img.crop((left, top, left + w, top + h))

        img = img.filter(ImageFilter.GaussianBlur(radius=0.35))
        return np.array(img)

    return clip.fl(transform)

def safe_color_grade(get_frame, t):
    frame = get_frame(t).astype(np.float32)
    frame = (frame - 127.0) * 1.08 + 127.0   
    frame = frame * 1.04                     
    frame = np.clip(frame, 0, 255).astype('uint8')
    return frame

def get_caption_font(size):
    from PIL import ImageFont
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  
        "C:\\Windows\\Fonts\\arialbd.ttf",                         
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",       
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()

def render_3d_word(text, fontsize=120, fill=(255, 255, 0), depth=10, depth_color=(120, 95, 0)):
    from PIL import Image, ImageDraw
    font = get_caption_font(fontsize)
    tmp = Image.new("RGBA", (10, 10))
    tmp_draw = ImageDraw.Draw(tmp)
    bbox = tmp_draw.textbbox((0, 0), text, font=font, stroke_width=6)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad = depth + 24
    W, H = tw + pad * 2, th + pad * 2
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    ox, oy = pad - bbox[0], pad - bbox[1]

    for d in range(depth, 0, -1):
        draw.text((ox + d, oy + d), text, font=font, fill=depth_color + (255,))
    draw.text((ox, oy), text, font=font, fill=fill + (255,), stroke_width=6, stroke_fill=(0, 0, 0, 255))
    return np.array(img)

def scale_pop(t, dur):
    if t < 0.08:
        return 1.3 - (0.3 * t / 0.08)
    return 1.0

def assemble_video(thumbnail_text="", visual_theme=""):
    print("\n🎬 Assembling cinematic video...")
    voice_audio = AudioFileClip(OUTPUT_AUDIO)
    
    audio_tracks = [voice_audio]
    bgm_file = download_music()
    if bgm_file:
        bgm = AudioFileClip(bgm_file).fx(afx.audio_loop, duration=voice_audio.duration).fx(afx.volumex, 0.05)
        audio_tracks.append(bgm)

    with open("words.json", "r") as f:
        words = json.load(f)

    media_files = [os.path.join("media", f) for f in os.listdir("media") if f.endswith(".mp4") or f.endswith(".jpg")]
    if len(media_files) == 0:
        raise Exception("CRITICAL ERROR: No media files downloaded. Check PEXELS_API_KEY.")
    
    clips = []
    current_time = 0
    media_index = 0
    cut_duration = 3.0
    fade_dur = 0.2
    has_sfx = os.path.exists("whoosh.mp3") and os.path.exists("pop.mp3")

    while current_time < voice_audio.duration:
        media_path = media_files[media_index % len(media_files)]
        
        if media_path.endswith(".mp4"):
            raw_clip = VideoFileClip(media_path)
            clip_duration = min(cut_duration, raw_clip.duration, voice_audio.duration - current_time)
            raw_clip = raw_clip.subclip(0, clip_duration)
        else:
            clip_duration = min(cut_duration, voice_audio.duration - current_time)
            raw_clip = ImageClip(media_path).set_duration(clip_duration)
        
        clip = raw_clip.resize(height=1920)
        clip = clip.crop(x_center=clip.w/2, y_center=clip.h/2, width=1080, height=1920)
        clip = apply_random_motion(clip, clip_duration)
        clip = clip.fl(safe_color_grade)
        clip = clip.set_start(current_time)

        if current_time > 0:
            clip = clip.crossfadein(fade_dur)
        clips.append(clip)

        if has_sfx and current_time > 0:
            whoosh = AudioFileClip("whoosh.mp3").set_start(current_time).fx(afx.volumex, 0.3)
            audio_tracks.append(whoosh)
        
        current_time += clip_duration
        media_index += 1

    text_clips = []
    for i, word_data in enumerate(words):
        start_t = word_data["start"]
        end_t = word_data["end"]
        word_dur = max(end_t - start_t, 0.01)
        
        clean_word = word_data["text"].strip(".,!?\"'").upper()
        
        word_img = render_3d_word(clean_word, fontsize=120, fill=(255, 255, 0), depth=10, depth_color=(120, 95, 0))
        txt_active = ImageClip(word_img)
        txt_active = txt_active.resize(lambda t, d=word_dur: scale_pop(t, d))
        txt_active = txt_active.set_position(('center', 1150)).set_start(start_t).set_end(end_t)
        
        if i + 1 < len(words):
            next_clean_word = words[i+1]["text"].strip(".,!?\"'").upper()
            peek_img = render_3d_word(next_clean_word, fontsize=70, fill=(255, 255, 255), depth=0)
            txt_next = ImageClip(peek_img)
            txt_next = txt_next.set_position(('center', 1300)).set_start(start_t).set_end(end_t)
            text_clips.append(txt_next)

        text_clips.append(txt_active)

        if has_sfx:
            pop = AudioFileClip("pop.mp3").set_start(start_t).fx(afx.volumex, 0.2)
            audio_tracks.append(pop)

    retention_bar = ColorClip(size=(1080, 15), color=(255, 255, 0))
    total_dur = voice_audio.duration
    retention_bar = retention_bar.set_position(lambda t: (-1080 + int(1080 * (t / total_dur)), 0)).set_duration(total_dur)
    text_clips.append(retention_bar)

    vignette = make_vignette_clip(total_dur)

    final_audio = CompositeAudioClip(audio_tracks)
    bg_video = CompositeVideoClip(clips, size=(1080, 1920)).set_audio(final_audio)
    final = CompositeVideoClip([bg_video, vignette] + text_clips)
    
    final.write_videofile("final_short.mp4", fps=24, codec="libx264", audio_codec="aac", threads=4,
                           bitrate="8000k", ffmpeg_params=["-maxrate", "8000k", "-bufsize", "16000k", "-crf", "20"])

    if not generate_ai_thumbnail(title=thumbnail_text, visual_theme=visual_theme):
        generate_thumbnail(final, thumbnail_text=thumbnail_text)

    final.close()

def generate_ai_thumbnail(title, visual_theme="", out_path="thumbnail.jpg"):
    from PIL import Image
    try:
        prompt = (
            f"A dramatic, high-contrast YouTube thumbnail image for a Shorts video about: {title}. "
            f"Visual theme: {visual_theme or 'space and science mystery'}. "
            "Bold, vivid, saturated colors, strong single focal subject, cinematic lighting, "
            "eye-catching and click-worthy composition, no text or letters in the image, "
            "portrait orientation."
        )
        
        response = gemini_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt
        )
        
        # Currently, the google-genai image generation is handled via generate_images or standard flash model 
        # For safety/fallback, if the model cannot generate raw bytes, we fallback to our reliable frame grab
        print("⚠️ Note: Using standard thumbnail stamp instead of AI due to image generation pipeline requirements.")
        return False
        
    except Exception as e:
        print(f"⚠️ AI thumbnail generation failed ({e}) - using frame-stamp fallback instead.")
        return False

def generate_thumbnail(final_clip, thumbnail_text, out_path="thumbnail.jpg"):
    from PIL import Image, ImageDraw, ImageFont
    frame_time = min(final_clip.duration * 0.15, 2.0)
    frame = final_clip.get_frame(frame_time)
    img = Image.fromarray(frame).convert("RGB")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 110)
    except Exception:
        font = ImageFont.load_default()

    text = (thumbnail_text or "").upper()
    W, H = img.size
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x, y = (W - tw) / 2, H * 0.28

    for dx in range(-6, 7, 3):
        for dy in range(-6, 7, 3):
            draw.text((x + dx, y + dy), text, font=font, fill="black")
    draw.text((x, y), text, font=font, fill="yellow")
    img.save(out_path, quality=92)

# --- 6. THE DELIVERY ---
def upload_to_youtube(video_file, data, credentials):
    youtube = build("youtube", "v3", credentials=credentials)
    media = MediaFileUpload(video_file, chunksize=-1, resumable=True)
    hashtags = " ".join(f"#{tag.replace(' ', '')}" for tag in data.get("tags", [])[:5])
    description = f"{data['description']}\n\n{hashtags}".strip()

    body = {"snippet": {"categoryId": "28", "title": data["title"], "description": description, "tags": data["tags"]}, 
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False, "containsSyntheticMedia": True}}
            
    response = youtube.videos().insert(part="snippet,status", body=body, media_body=media).execute()
    video_id = response['id']
    print(f"✅ Success! Link: https://youtu.be/{video_id}")

    if os.path.exists("thumbnail.jpg"):
        try:
            youtube.thumbnails().set(videoId=video_id, media_body=MediaFileUpload("thumbnail.jpg")).execute()
            print("🖼️ Custom thumbnail set.")
        except Exception as e:
            print(f"⚠️ Thumbnail upload failed (needs phone-verified channel): {e}")

    return video_id

def upload_to_github_release(video_file):
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")  
    if not token or not repo:
        print("ℹ️ No GITHUB_TOKEN/GITHUB_REPOSITORY in this environment - skipping release hosting.")
        return None

    try:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        tag = f"short-{int(time.time())}"
        r = requests.post(f"https://api.github.com/repos/{repo}/releases", headers=headers, json={
            "tag_name": tag, "name": tag, "body": "Auto-generated video asset for cross-posting.", "draft": False, "prerelease": False,
        }, timeout=30)
        if r.status_code not in (200, 201):
            return None
        release = r.json()
        upload_url = release["upload_url"].split("{")[0]

        with open(video_file, "rb") as f:
            video_bytes = f.read()
        r2 = requests.post(f"{upload_url}?name=final_short.mp4", headers={**headers, "Content-Type": "video/mp4"},
                            data=video_bytes, timeout=180)
        if r2.status_code not in (200, 201):
            return None
        return r2.json().get("browser_download_url")
    except Exception as e:
        print(f"⚠️ GitHub release hosting error: {e}")
        return None

def repost_via_zernio(video_file, data):
    api_key = os.environ.get("ZERNIO_API_KEY")
    if not api_key:
        print("ℹ️ Zernio repost skipped - ZERNIO_API_KEY not set.")
        return False

    video_public_url = upload_to_github_release(video_file)
    if not video_public_url:
        print("ℹ️ Zernio repost skipped - couldn't get a public URL for the video.")
        return False

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        acc_r = requests.get("https://zernio.com/api/v1/accounts", headers=headers, timeout=30)
        if acc_r.status_code != 200:
            return False
        accounts = acc_r.json().get("accounts", acc_r.json() if isinstance(acc_r.json(), list) else [])
        platform_entries = []
        for acc in accounts:
            if acc.get("platform") in ("instagram", "facebook"):
                acc_id = acc.get("_id") or acc.get("id") or acc.get("accountId")
                if acc_id:
                    platform_entries.append({"platform": acc["platform"], "accountId": acc_id})
        if not platform_entries:
            return False
    except Exception as e:
        return False

    try:
        caption = f"{data.get('title', '')}\n\n{data.get('description', '')}"
        r = requests.post(
            "https://zernio.com/api/v1/posts",
            headers=headers,
            json={
                "content": caption[:2200],
                "mediaItems": [{"type": "video", "url": video_public_url}],
                "platforms": platform_entries,
                "publishNow": True,
            },
            timeout=120,
        )
        try:
            body = r.json()
        except ValueError:
            body = {}
        
        looks_like_error = "error" in body
        looks_like_success = r.status_code in (200, 201, 202, 207) and not looks_like_error and ("_id" in body or "post" in body)
        if not looks_like_success:
            return False

        post_obj = body.get("post", body)
        platform_entries_result = post_obj.get("platforms", [])
        any_success = False
        any_failure = False
        for entry in platform_entries_result:
            plat_name = entry.get("platform", "?")
            status = entry.get("status") or entry.get("publishStatus") or "unknown"
            error_msg = entry.get("error") or entry.get("errorMessage")
            if error_msg or status in ("failed", "error"):
                any_failure = True
                print(f"  ❌ {plat_name}: FAILED - {error_msg or status}")
            else:
                any_success = True
                print(f"  ✅ {plat_name}: {status}")

        return any_success
    except Exception as e:
        print(f"⚠️ Zernio repost error: {e}")
        return False

async def main():
    restore_google_secrets()
    credentials = get_google_credentials()

    history = load_topic_history()
    avoid_topics = [h["title"] for h in history]
    boost_topics = fetch_top_performing_titles(credentials)

    content = generate_script(avoid_topics=avoid_topics, boost_topics=boost_topics)
    await generate_audio_and_timestamps(content["script"])
        
    download_videos(content["image_prompts"], visual_theme=content.get("visual_theme", ""))
    assemble_video(thumbnail_text=content.get("thumbnail_text", content.get("title", "")),
                   visual_theme=content.get("visual_theme", ""))
    video_id = upload_to_youtube("final_short.mp4", content, credentials)

    save_topic_history({"title": content["title"], "video_id": video_id, "date": time.strftime("%Y-%m-%d")})
    repost_via_zernio("final_short.mp4", content)

if __name__ == "__main__":
    asyncio.run(main())
