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

    Language Rules (critical for all-age appeal):
    - Simple, universal vocabulary understandable by both children and adults.
    - If a technical/scientific term is used, explain it in the SAME sentence with a simple analogy.
    - Universal wonder/surprise emotion, not niche humor, slang, or cultural references.

    Technical Constraints:
    - The "script" must be exactly 55 to 65 words (fits 25-30 seconds spoken).
    - The "image_prompts" array must contain exactly 10 visual search terms for stock footage, each 4-6 words, cinematic and specific (e.g. "astronaut floating dark space station" not "space"). Mix wide establishing shots, close-ups, and action/movement shots. Avoid abstract concepts - describe a visual proxy instead.
    - "thumbnail_text" = 3-4 word punchy bold overlay text for the first-frame thumbnail, separate from title.

    Format the output as strictly valid JSON exactly like this:
    {{
      "script": "...",
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
def download_sfx():
    """Downloads lightweight standard SFX if they don't exist yet."""
    if not os.path.exists("whoosh.mp3"):
        # Downloading a generic royalty-free whoosh from a reliable raw source
        with open("whoosh.mp3", "wb") as f:
            f.write(requests.get("https://raw.githubusercontent.com/Ranjith/dummy/main/whoosh.mp3").content) # Replace with actual direct MP3 links if needed, or place manually
    if not os.path.exists("pop.mp3"):
        with open("pop.mp3", "wb") as f:
            f.write(requests.get("https://raw.githubusercontent.com/Ranjith/dummy/main/pop.mp3").content)

def pick_best_video_file(video_files):
    """From Pexels' list of resolutions for one clip, pick the best portrait-fit one.
    Prefer HD (1080-2160 width range), avoid tiny previews."""
    portrait = [v for v in video_files if v.get("height", 0) > v.get("width", 0)]
    candidates = portrait if portrait else video_files
    candidates = sorted(candidates, key=lambda v: v.get("width", 0), reverse=True)
    for v in candidates:
        if 720 <= v.get("width", 0) <= 2160:
            return v
    return candidates[0] if candidates else None

# Fallback keyword pool - used when a specific prompt returns zero results,
# keeps the pipeline from breaking on an obscure search term.
FALLBACK_KEYWORDS = ["space stars", "ocean waves", "forest nature", "city lights night", "abstract particles"]

def download_videos(prompts):
    headers = {"Authorization": PEXELS_API_KEY}
    if not os.path.exists("media"): os.makedirs("media")

    for index, prompt in enumerate(prompts):
        got_media = False

        # 1. Try Video First - pull top 5 results, pick best match not just [0]
        vid_url = f"https://api.pexels.com/videos/search?query={prompt}&orientation=portrait&per_page=5"
        response = requests.get(vid_url, headers=headers)

        if response.status_code == 200 and response.json().get("videos"):
            videos = response.json()["videos"]
            best_video = max(videos, key=lambda v: v.get("duration", 0))
            video_file = pick_best_video_file(best_video["video_files"])
            if video_file:
                with open(f"media/clip_{index}.mp4", "wb") as f:
                    f.write(requests.get(video_file["link"]).content)
                got_media = True

        # 2. IMAGE FALLBACK if no video
        if not got_media:
            print(f"⚠️ No video for '{prompt}'. Falling back to Image...")
            img_url = f"https://api.pexels.com/v1/search?query={prompt}&orientation=portrait&per_page=1"
            img_response = requests.get(img_url, headers=headers)
            if img_response.status_code == 200 and img_response.json().get("photos"):
                photo_url = img_response.json()["photos"][0]["src"]["portrait"]
                with open(f"media/clip_{index}.jpg", "wb") as f:
                    f.write(requests.get(photo_url).content)
                got_media = True

        # 3. FALLBACK KEYWORD if the specific prompt returned nothing at all
        if not got_media:
            fallback = random.choice(FALLBACK_KEYWORDS)
            print(f"⚠️ Prompt '{prompt}' totally empty. Using fallback keyword '{fallback}'...")
            fb_url = f"https://api.pexels.com/v1/search?query={fallback}&orientation=portrait&per_page=1"
            fb_response = requests.get(fb_url, headers=headers)
            if fb_response.status_code == 200 and fb_response.json().get("photos"):
                photo_url = fb_response.json()["photos"][0]["src"]["portrait"]
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
def make_vignette_clip(duration, w=1080, h=1920):
    """Dark radial vignette overlay - pulls eye to center, hides rough edges."""
    import numpy as np
    yy, xx = np.mgrid[0:h, 0:w]
    cx, cy = w / 2, h / 2
    max_dist = ((cx) ** 2 + (cy) ** 2) ** 0.5
    dist = ((xx - cx) ** 2 + (yy - cy) ** 2) ** 0.5
    norm = np.clip(dist / max_dist, 0, 1)
    alpha = (norm ** 2 * 160).astype("uint8")  # darken more toward edges
    frame = np.zeros((h, w, 3), dtype="uint8")
    mask = alpha.astype(float) / 255.0
    vclip = ImageClip(frame).set_duration(duration)
    vclip = vclip.set_mask(ImageClip(alpha, ismask=True).set_duration(duration))
    return vclip.set_opacity(1.0)

def apply_random_motion(clip, clip_duration):
    """Alternate zoom-in / zoom-out / pan-left / pan-right per cut for variety."""
    motion = random.choice(['zoom_in', 'zoom_out', 'pan_l', 'pan_r'])
    if motion == 'zoom_in':
        return clip.resize(lambda t: 1 + 0.06 * (t / clip_duration))
    elif motion == 'zoom_out':
        return clip.resize(lambda t: 1.06 - 0.06 * (t / clip_duration))
    elif motion == 'pan_l':
        clip = clip.resize(1.15)
        return clip.set_position(lambda t: (-40 * (t / clip_duration), 'center'))
    else:
        clip = clip.resize(1.15)
        return clip.set_position(lambda t: (-40 + 40 * (t / clip_duration), 'center'))

def assemble_video(thumbnail_text=""):
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

        # Cinematic color grade: darker shadows + slight saturation push
        clip = clip.fx(vfx.lum_contrast, lum=0, contrast=0.15, contrast_thr=127)
        clip = clip.fx(vfx.colorx, 1.1)

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
    def scale_pop(t, dur):
        if t < 0.08:
            return 1.3 - (0.3 * t / 0.08)
        return 1.0

    text_clips = []
    for i, word_data in enumerate(words):
        start_t = word_data["start"]
        end_t = word_data["end"]
        word_dur = max(end_t - start_t, 0.01)
        
        # Main Active Word (Yellow, Large) - pop-in scale + shadow
        txt_active = TextClip(word_data["text"].upper(), fontsize=120, color='yellow', font='Arial-Bold', stroke_color='black', stroke_width=5)
        txt_active = txt_active.resize(lambda t, d=word_dur: scale_pop(t, d))
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

    # Vignette overlay on top of everything
    vignette = make_vignette_clip(total_dur)

    # Final Assembly
    final_audio = CompositeAudioClip(audio_tracks)
    bg_video = CompositeVideoClip(clips, size=(1080, 1920)).set_audio(final_audio)
    final = CompositeVideoClip([bg_video, vignette] + text_clips)
    
    final.write_videofile("final_short.mp4", fps=24, codec="libx264", audio_codec="aac", threads=4)

    # Grab a good mid-video frame + stamp bold thumbnail_text on it for a custom thumbnail
    generate_thumbnail(final, thumbnail_text=thumbnail_text)

    final.close()

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
        
    download_videos(content["image_prompts"])
    assemble_video(thumbnail_text=content.get("thumbnail_text", content.get("title", "")))
    video_id = upload_to_youtube("final_short.mp4", content, credentials)

    save_topic_history({"title": content["title"], "video_id": video_id, "date": time.strftime("%Y-%m-%d")})
    
if __name__ == "__main__":
    asyncio.run(main())
