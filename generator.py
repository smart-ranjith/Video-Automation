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
import google.generativeai as genai
import edge_tts
from moviepy.editor import *
import moviepy.video.fx.all as vfx
import moviepy.audio.fx.all as afx
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

# --- 1. SETUP & SECRETS ---
import base64

try:
    from dotenv import load_dotenv
    load_dotenv()  # loads a local .env file if present - harmless no-op on GitHub Actions (uses real secrets there)
except ImportError:
    pass  # python-dotenv not installed - fine on GH Actions, but locally you'll need env vars set manually or run: pip install python-dotenv

def require_env(key):
    val = os.environ.get(key)
    if not val:
        raise Exception(f"Missing required env var: {key}. Set it in GitHub Actions secrets, "
                         f"or locally create a .env file with {key}=your_value and run: pip install python-dotenv")
    return val

GEMINI_API_KEY = require_env("GEMINI_API_KEY")
PEXELS_API_KEY = require_env("PEXELS_API_KEY")
PIXABAY_API_KEY = require_env("PIXABAY_API_KEY")

genai.configure(api_key=GEMINI_API_KEY)
SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/yt-analytics.readonly"]

def restore_google_secrets():
    """Decode base64 secrets from env vars into files GH Actions run needs.
    Run once at start of main() before any Google auth call."""
    client_b64 = require_env("CLIENT_SECRETS_BASE64")
    token_b64 = require_env("TOKEN_PICKLE_BASE64")
    with open("client_secrets.json", "wb") as f:
        f.write(base64.b64decode(client_b64))
    with open("token.pickle", "wb") as f:
        f.write(base64.b64decode(token_b64))

def get_google_credentials():
    """Shared credential loader - used by both analytics fetch and upload."""
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

# --- TOPIC MEMORY (avoid repeating past topics + track what performed well) ---
TOPIC_HISTORY_FILE = "topic_history.json"

def load_topic_history():
    if os.path.exists(TOPIC_HISTORY_FILE):
        with open(TOPIC_HISTORY_FILE, "r") as f:
            return json.load(f)
    return []

def save_topic_history(entry):
    history = load_topic_history()
    history.append(entry)
    history = history[-100:]  # keep last 100, avoid unbounded growth
    with open(TOPIC_HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)

def fetch_top_performing_titles(credentials, max_results=5):
    """Weekly-ish signal: pull best-performing recent video titles from YouTube Analytics
    to bias future topics toward what's working. Fails gracefully if scope/data missing."""
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
    model = genai.GenerativeModel('gemini-2.5-flash')

    avoid_block = ""
    if avoid_topics:
        avoid_list = "; ".join(avoid_topics[-20:])  # last 20 to keep prompt lean
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
    - The "image_prompts" array must contain exactly 10 visual search terms for stock footage, each 4-6 words, cinematic and specific, and ALL 10 must visually belong to the SAME "visual_theme" world - same setting, same color palette, same lighting mood. Do NOT mix unrelated settings (e.g. don't mix space shots with ocean shots in one video) - this consistency is what makes the final video feel like one cinematic piece instead of a random stock clip collage.
    - Mix shot types within that one theme: wide establishing, close-up detail, slow-motion/action - variety of shots, not variety of subjects.
    - Avoid abstract concepts - describe a visual proxy instead, staying inside the chosen visual_theme.
    - "thumbnail_text" = 3-4 word punchy bold overlay text for the first-frame thumbnail, separate from title.
    - "description" should open with a curiosity-gap question (not a summary that gives away the answer), then a natural one-line subscribe nudge tied to the content ("Subscribe if you want your mind blown daily" style, not generic "please subscribe").

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
def safe_download(url, out_path, min_bytes=2048, headers=None):
    """Download a file and validate it before writing to disk. Every previous
    download call wrote requests.get(...).content straight to disk with ZERO
    validation - if the request failed, timed out, or got rate-limited, we'd
    silently write an error page/empty response into a .mp4/.jpg file. FFmpeg
    then partially decodes that corrupted file, producing exactly the kind of
    glitchy/rainbow macroblock corruption reported. Returns True only if a
    plausible media file was actually written."""
    try:
        r = requests.get(url, headers=headers, timeout=30)
    except requests.RequestException as e:
        print(f"⚠️ Download failed ({url[:60]}...): {e}")
        return False
    if r.status_code != 200:
        print(f"⚠️ Download got HTTP {r.status_code} ({url[:60]}...) - skipping, not writing corrupt data")
        return False
    content = r.content
    if len(content) < min_bytes:
        print(f"⚠️ Download suspiciously small ({len(content)} bytes) - skipping, likely an error page not real media")
        return False
    with open(out_path, "wb") as f:
        f.write(content)
    return True

def download_sfx():
    """SFX disabled - the previous URLs were literal placeholder dummy links
    (https://.../Ranjith/dummy/...) that 404 every single run, silently writing
    a 404 error page as 'whoosh.mp3'/'pop.mp3'. Trying to play that broken file
    as audio downstream is a real bug. Leaving this off until real royalty-free
    SFX file URLs are supplied - safer than shipping guaranteed-broken audio."""
    print("ℹ️ SFX skipped - no valid SFX source configured yet (previous links were dead placeholders).")

def pick_best_video_file(video_files):
    """From Pexels' list of resolutions for one clip, pick the best portrait-fit one.
    Must be >= our 1080px output width or it gets upscaled and looks blurry/soft."""
    portrait = [v for v in video_files if v.get("height", 0) > v.get("width", 0)]
    candidates = portrait if portrait else video_files
    candidates = sorted(candidates, key=lambda v: v.get("width", 0), reverse=True)
    # Prefer clips at or above our output width (1080) - never upscale if avoidable
    for v in candidates:
        if v.get("width", 0) >= 1080:
            return v
    # Nothing HD enough - take the largest available rather than a tiny preview
    return candidates[0] if candidates else None

# Fallback keyword pool - used when a specific prompt returns zero results,
# keeps the pipeline from breaking on an obscure search term.
FALLBACK_KEYWORDS = ["space stars", "ocean waves", "forest nature", "city lights night", "abstract particles",
                     "aurora borealis sky", "desert dunes sunset", "clouds timelapse sky", "mountain fog morning",
                     "underwater coral reef", "milky way night sky", "waterfall slow motion"]

def download_videos(prompts, visual_theme=""):
    headers = {"Authorization": PEXELS_API_KEY}
    if not os.path.exists("media"): os.makedirs("media")

    for index, prompt in enumerate(prompts):
        got_media = False

        # 1. Try Video First - bias query toward the shared visual_theme for consistency,
        # fall back to the plain prompt if the themed search comes up empty.
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
            # Randomize among the longer/better candidates instead of always picking the
            # exact same max-duration result - deterministic picking meant repeated video
            # generations with overlapping prompts kept fetching the identical clip,
            # making the channel's content feel repetitive over time.
            sorted_videos = sorted(videos, key=lambda v: v.get("duration", 0), reverse=True)
            top_candidates = sorted_videos[:3] if len(sorted_videos) >= 3 else sorted_videos
            best_video = random.choice(top_candidates)
            video_file = pick_best_video_file(best_video["video_files"])
            if video_file:
                got_media = safe_download(video_file["link"], f"media/clip_{index}.mp4", min_bytes=50_000)

        # 2. IMAGE FALLBACK if no video (or video download failed/was corrupt)
        if not got_media:
            print(f"⚠️ No usable video for '{prompt}'. Falling back to Image...")
            img_url = f"https://api.pexels.com/v1/search?query={prompt}&orientation=portrait&per_page=1"
            img_response = requests.get(img_url, headers=headers)
            if img_response.status_code == 200 and img_response.json().get("photos"):
                photo_url = img_response.json()["photos"][0]["src"]["portrait"]
                got_media = safe_download(photo_url, f"media/clip_{index}.jpg", min_bytes=5_000)

        # 3. FALLBACK KEYWORD if the specific prompt returned nothing at all
        if not got_media:
            fallback = random.choice(FALLBACK_KEYWORDS)
            print(f"⚠️ Prompt '{prompt}' totally empty/failed. Using fallback keyword '{fallback}'...")
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
    """Dark radial vignette overlay - true edge-only darkening. Previous version
    darkened 30%+ starting well inside the frame (not just corners), reading as
    an overall muddy/washed-out look. This version stays fully clear through the
    central ~65% and only darkens the outer rim, capped lower."""
    yy, xx = np.mgrid[0:h, 0:w]
    cx, cy = w / 2, h / 2
    max_dist = ((cx) ** 2 + (cy) ** 2) ** 0.5
    dist = ((xx - cx) ** 2 + (yy - cy) ** 2) ** 0.5
    norm = np.clip(dist / max_dist, 0, 1)
    inner_safe = 0.65  # no darkening at all until this fraction of the way out
    falloff = np.clip((norm - inner_safe) / (1 - inner_safe), 0, 1)
    alpha = (falloff ** 3 * 90).astype("uint8")  # steeper curve, lower max (was 160)
    frame = np.zeros((h, w, 3), dtype="uint8")
    vclip = ImageClip(frame).set_duration(duration)
    vclip = vclip.set_mask(ImageClip(alpha, ismask=True).set_duration(duration))
    return vclip.set_opacity(1.0)

def apply_random_motion(clip, clip_duration, w=1080, h=1920):
    """Alternate zoom-in / zoom-out / pan-left / pan-right per cut for variety.
    Exactly ONE resize pass per frame (previous version chained up to 3 resizes,
    and each pass's faint ringing/aliasing got amplified by the color grade into
    visible grain/noise). Zoom crops a shrinking/growing window from the native
    frame and resizes once; pan pre-upsamples once and crops an exact w x h window
    (no second resize needed since the crop box is always exactly w x h)."""
    from PIL import Image, ImageFilter
    motion = random.choice(['zoom_in', 'zoom_out', 'pan_l', 'pan_r'])
    pan_scale = 1.08  # headroom for pan only

    def transform(get_frame, t):
        frame = get_frame(t)
        img = Image.fromarray(frame)
        progress = min(max(t / clip_duration, 0), 1)

        if motion in ('zoom_in', 'zoom_out'):
            # Crop a smaller-than-native window directly, then resize up ONCE.
            cur_scale = (1.0 + 0.06 * progress) if motion == 'zoom_in' else (1.06 - 0.06 * progress)
            crop_w = max(int(w / cur_scale), 2)
            crop_h = max(int(h / cur_scale), 2)
            left = (w - crop_w) // 2
            top = (h - crop_h) // 2
            img = img.crop((left, top, left + crop_w, top + crop_h))
            img = img.resize((w, h), Image.LANCZOS)
        else:
            # Pan: upsample once for headroom, crop an exact w x h window - no 2nd resize.
            new_w, new_h = int(w * pan_scale), int(h * pan_scale)
            img = img.resize((new_w, new_h), Image.LANCZOS)
            max_shift = new_w - w
            left = int(max_shift * (1 - progress)) if motion == 'pan_l' else int(max_shift * progress)
            top = (new_h - h) // 2
            img = img.crop((left, top, left + w, top + h))

        # Tiny blur kills LANCZOS ringing/aliasing before contrast amplifies it into grain.
        img = img.filter(ImageFilter.GaussianBlur(radius=0.35))
        return np.array(img)

    return clip.fl(transform)

def safe_color_grade(get_frame, t):
    """Contrast + saturation push with explicit clipping - prevents uint8 overflow
    wraparound. Kept mild - stronger values amplify any residual resize/compression
    artifacts into visible grain."""
    frame = get_frame(t).astype(np.float32)
    frame = (frame - 127.0) * 1.08 + 127.0   # contrast (reduced from 1.15)
    frame = frame * 1.04                     # slight brightness/saturation push (reduced from 1.08)
    frame = np.clip(frame, 0, 255).astype('uint8')
    return frame

def get_caption_font(size):
    """Cross-platform bold font lookup - avoids relying on ImageMagick finding
    'Arial-Bold' (a Windows font that doesn't exist on GH Actions' Ubuntu runner,
    which would silently break captions there even though it works locally)."""
    from PIL import ImageFont
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # Linux/GH Actions
        "C:\\Windows\\Fonts\\arialbd.ttf",                         # Windows
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",       # macOS
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()

def render_3d_word(text, fontsize=120, fill=(255, 255, 0), depth=10, depth_color=(120, 95, 0)):
    """Renders a word with a pseudo-3D extruded look (stacked offset layers going
    darker/deeper, like blocky 3D lettering) plus a black outline for pop. Pure
    PIL, no extra dependencies, no heavy 3D renderer needed - the diagonal stack
    of solid-color layers reads as depth/extrusion at video resolution."""
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

    # Extrusion: draw deepest layer first, working up to the front face
    for d in range(depth, 0, -1):
        draw.text((ox + d, oy + d), text, font=font, fill=depth_color + (255,))
    # Front face with black stroke for contrast/pop (matches existing caption style)
    draw.text((ox, oy), text, font=font, fill=fill + (255,), stroke_width=6, stroke_fill=(0, 0, 0, 255))

    return np.array(img)


def scale_pop(t, dur):
    if t < 0.08:
        return 1.3 - (0.3 * t / 0.08)
    return 1.0

def assemble_video(thumbnail_text="", visual_theme=""):
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
    fade_dur = 0.2

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
        
        # Crop to portrait frame first
        clip = raw_clip.resize(height=1920)
        clip = clip.crop(x_center=clip.w/2, y_center=clip.h/2, width=1080, height=1920)

        # Random zoom/pan motion (variety instead of same zoom every clip)
        clip = apply_random_motion(clip, clip_duration)

        # Cinematic color grade: darker shadows + slight saturation push (overflow-safe, clipped)
        clip = clip.fl(safe_color_grade)

        clip = clip.subclip(0, clip_duration).set_start(current_time)

        # Crossfade transition instead of hard cut
        if current_time > 0:
            clip = clip.crossfadein(fade_dur)

        clips.append(clip)

        # Add Whoosh SFX at every cut
        if has_sfx and current_time > 0:
            whoosh = AudioFileClip("whoosh.mp3").set_start(current_time).fx(afx.volumex, 0.3)
            audio_tracks.append(whoosh)
        
        current_time += clip_duration
        media_index += 1

# Text Engine (The "Stack" Method) with pop-in animation

    text_clips = []
    for i, word_data in enumerate(words):
        start_t = word_data["start"]
        end_t = word_data["end"]
        word_dur = max(end_t - start_t, 0.01)
        
        # Main Active Word (Yellow, pseudo-3D extruded) - pop-in scale + depth
        word_img = render_3d_word(word_data["text"].upper(), fontsize=120, fill=(255, 255, 0), depth=10, depth_color=(120, 95, 0))
        txt_active = ImageClip(word_img)
        txt_active = txt_active.resize(lambda t, d=word_dur: scale_pop(t, d))
        txt_active = txt_active.set_position(('center', 1150)).set_start(start_t).set_end(end_t)
        
        # Next Word Peek (White, Small, Underneath) - flat (depth=0), same safe font path
        if i + 1 < len(words):
            peek_img = render_3d_word(words[i+1]["text"].upper(), fontsize=70, fill=(255, 255, 255), depth=0)
            txt_next = ImageClip(peek_img)
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

    # Vignette overlay on top of everything
    vignette = make_vignette_clip(total_dur)

    # Final Assembly
    final_audio = CompositeAudioClip(audio_tracks)
    bg_video = CompositeVideoClip(clips, size=(1080, 1920)).set_audio(final_audio)
    final = CompositeVideoClip([bg_video, vignette] + text_clips)
    
    final.write_videofile("final_short.mp4", fps=24, codec="libx264", audio_codec="aac", threads=4,
                           bitrate="8000k", ffmpeg_params=["-maxrate", "8000k", "-bufsize", "16000k", "-crf", "20"])

    # Thumbnail: try a real AI-generated image first (more eye-catching than a
    # stamped video frame), fall back to the frame-stamp method if the API call
    # fails, isn't available on this key, or hits a quota limit.
    if not generate_ai_thumbnail(title=thumbnail_text, visual_theme=visual_theme):
        generate_thumbnail(final, thumbnail_text=thumbnail_text)

    final.close()

def generate_ai_thumbnail(title, visual_theme="", out_path="thumbnail.jpg"):
    """Real AI-generated thumbnail via Gemini's image model - a purpose-built
    dramatic composition beats a stamped video frame for CTR. Costs a few cents
    per image (or free depending on your key's tier) - trivial either way at
    1 thumbnail/day. Returns False on ANY failure so the caller can fall back
    to the free frame-stamp method instead of breaking the whole run."""
    from PIL import Image
    try:
        prompt = (
            f"A dramatic, high-contrast YouTube thumbnail image for a Shorts video about: {title}. "
            f"Visual theme: {visual_theme or 'space and science mystery'}. "
            "Bold, vivid, saturated colors, strong single focal subject, cinematic lighting, "
            "eye-catching and click-worthy composition, no text or letters in the image, "
            "portrait orientation."
        )
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-image:generateContent?key={GEMINI_API_KEY}"
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        r = requests.post(url, json=payload, timeout=60)
        if r.status_code != 200:
            print(f"⚠️ AI thumbnail unavailable (HTTP {r.status_code}) - using frame-stamp fallback instead.")
            return False

        data = r.json()
        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        for part in parts:
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                img_bytes = base64.b64decode(inline["data"])
                img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
                # Normalize to our 1080x1920 thumbnail canvas regardless of what the model returned
                img = img.resize((1080, 1920)) if img.size != (1080, 1920) else img
                img.save(out_path, quality=92)
                print("🎨 AI-generated thumbnail created.")
                return True

        print("⚠️ AI thumbnail response had no image data - using frame-stamp fallback instead.")
        return False
    except Exception as e:
        print(f"⚠️ AI thumbnail generation failed ({e}) - using frame-stamp fallback instead.")
        return False

def generate_thumbnail(final_clip, thumbnail_text, out_path="thumbnail.jpg"):
    """Pull a frame from ~15% into the video (past the very first flash-cut) and
    stamp bold punchy text on it - proven to lift CTR vs YouTube's auto-picked frame."""
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

    # Black stroke outline for contrast + yellow fill, matches caption style
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

    # Set custom thumbnail if we generated one
    if os.path.exists("thumbnail.jpg"):
        try:
            youtube.thumbnails().set(videoId=video_id, media_body=MediaFileUpload("thumbnail.jpg")).execute()
            print("🖼️ Custom thumbnail set.")
        except Exception as e:
            print(f"⚠️ Thumbnail upload failed (needs phone-verified channel): {e}")

    return video_id

async def main():
    restore_google_secrets()
    credentials = get_google_credentials()

    # Topic memory: avoid repeats, bias toward what performed well
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

    # Multi-platform repost via Zernio (unified API) - free for up to 2 accounts,
    # no Meta developer portal / app review needed at all.
    repost_via_zernio("final_short.mp4", content)

def upload_to_github_release(video_file):
    """Instagram's Graph API requires a public URL for the video (no direct file
    upload for Reels) - this uses a GitHub Release on your own repo as free public
    hosting, since you already have one. Needs GITHUB_TOKEN and GITHUB_REPOSITORY,
    which GitHub Actions provides automatically - nothing extra to configure there.
    Returns None (and the caller skips gracefully) if not running in that context."""
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")  # auto-set by GH Actions, e.g. "user/repo"
    if not token or not repo:
        print("ℹ️ No GITHUB_TOKEN/GITHUB_REPOSITORY in this environment - can't auto-host for Instagram here (this works automatically inside GH Actions).")
        return None

    try:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        tag = f"short-{int(time.time())}"
        r = requests.post(f"https://api.github.com/repos/{repo}/releases", headers=headers, json={
            "tag_name": tag, "name": tag, "body": "Auto-generated video asset for cross-posting.", "draft": False, "prerelease": False,
        }, timeout=30)
        if r.status_code not in (200, 201):
            print(f"⚠️ GitHub release creation failed: {r.text[:300]}")
            return None
        release = r.json()
        upload_url = release["upload_url"].split("{")[0]

        with open(video_file, "rb") as f:
            video_bytes = f.read()
        r2 = requests.post(f"{upload_url}?name=final_short.mp4", headers={**headers, "Content-Type": "video/mp4"},
                            data=video_bytes, timeout=180)
        if r2.status_code not in (200, 201):
            print(f"⚠️ GitHub release asset upload failed: {r2.text[:300]}")
            return None
        return r2.json().get("browser_download_url")
    except Exception as e:
        print(f"⚠️ GitHub release hosting error: {e}")
        return None

def repost_via_zernio(video_file, data):
    """Posts to Instagram + Facebook (and any other platform you connect in the
    Zernio dashboard) through one unified API call - no Meta developer portal,
    no App Review, no token wrangling. Free for up to 2 connected accounts.
    Still needs a public URL for the video file (Zernio pulls from a URL rather
    than accepting direct upload), handled via a GitHub Release."""
    api_key = os.environ.get("ZERNIO_API_KEY")
    if not api_key:
        print("ℹ️ Zernio repost skipped - ZERNIO_API_KEY not set yet.")
        return False

    video_public_url = upload_to_github_release(video_file)
    if not video_public_url:
        print("ℹ️ Zernio repost skipped - couldn't get a public URL for the video.")
        return False

    try:
        caption = f"{data.get('title', '')}\n\n{data.get('description', '')}"
        r = requests.post(
            "https://zernio.com/api/v1/posts",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "platforms": ["instagram", "facebook"],
                "content": caption[:2200],
                "mediaUrls": [video_public_url],
            },
            timeout=120,
        )
        if r.status_code not in (200, 201):
            print(f"⚠️ Zernio post failed: {r.text[:300]}")
            return False
        print("✅ Reposted to Instagram + Facebook via Zernio.")
        return True
    except Exception as e:
        print(f"⚠️ Zernio repost error: {e}")
        return False

if __name__ == "__main__":
    asyncio.run(main())