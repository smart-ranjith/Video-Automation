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
VIEWER_REQUESTS_FILE = "viewer_requests.json"

GEMINI_MODELS = [
    "gemini-3.8-flash",       # latest active generation recommended by API
    "gemini-3.6-flash",       # current generation
    "gemini-3.5-flash-lite",  # lite variant
    "gemini-3.1-pro-preview", # high reasoning fallback
]

GROQ_MODELS = [
    "openai/gpt-oss-120b",
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
]

EVERGREEN_DEMAND_TAGS = [
    "unsolved mysteries",
    "science shorts",
    "space mysteries",
    "unexplained phenomena",
    "weird history facts",
    "deep sea mysteries",
    "mind blowing facts",
    "mystery shorts",
]

def load_topic_history():
    if os.path.exists(TOPIC_HISTORY_FILE):
        with open(TOPIC_HISTORY_FILE, "r") as f: return json.load(f)
    return []

def save_topic_history(entry):
    history = load_topic_history()
    history.append(entry)
    with open(TOPIC_HISTORY_FILE, "w") as f: json.dump(history[-100:], f, indent=2)

def load_viewer_requests():
    if os.path.exists(VIEWER_REQUESTS_FILE):
        try:
            with open(VIEWER_REQUESTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [str(x).strip() for x in data if str(x).strip()]
        except Exception:
            pass
    return []

def extract_json_object(raw_text):
    """Parses JSON from an LLM response even when wrapped in markdown fences or extra prose."""
    text = (raw_text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise

def optimize_title(title):
    """Enforces a punchy, untruncated mobile-friendly title with #Shorts cleanly attached."""
    import re
    cleaned = re.sub(r"\s+", " ", (title or "").strip().strip('"\''))
    has_shorts = bool(re.search(r"#shorts?\b", cleaned, flags=re.IGNORECASE))
    body = re.sub(r"\s*#shorts?\b", "", cleaned, flags=re.IGNORECASE).strip(" -|:,")
    if not body:
        body = "The Unexplained Discovery Scientists Can't Ignore"
    # Keep title under ~65 chars so the curiosity hook isn't truncated in Shorts/Browse feeds
    max_body_len = 57
    if len(body) > max_body_len:
        body = body[:max_body_len].rsplit(" ", 1)[0].rstrip(" -|:,")
    suffix = " #Shorts" if has_shorts or len(body) <= 57 else ""
    return f"{body}{suffix}".strip()

def build_description_hashtags(hashtags_list, max_hashtags=4):
    """Selects 3-5 clean #Hashtags (YouTube displays top 3 above title; too many dilutes reach)."""
    import re
    seen = set()
    result = ["#Shorts"]
    seen.add("#shorts")
    for raw in (hashtags_list or []):
        clean = re.sub(r"[^A-Za-z0-9_]", "", str(raw).replace("#", ""))
        if not clean:
            continue
        tag = f"#{clean}"
        if tag.lower() not in seen:
            seen.add(tag.lower())
            result.append(tag)
        if len(result) >= max_hashtags:
            break
    if len(result) < 3:
        for fallback in ("#Mystery", "#Science", "#Unexplained"):
            if fallback.lower() not in seen:
                seen.add(fallback.lower())
                result.append(fallback)
            if len(result) >= max_hashtags:
                break
    return " ".join(result)

def build_youtube_seo_tags(seo_tags, hashtags_list=None, channel_tags=None, max_chars=480):
    """Builds plain keyword phrases (NO # symbols) packed cleanly inside YouTube's 500-char limit."""
    import re
    candidates = []
    for src in (seo_tags or [], channel_tags or [], hashtags_list or [], EVERGREEN_DEMAND_TAGS):
        for raw in src:
            phrase = re.sub(r"[#<>]", "", str(raw)).strip()
            phrase = re.sub(r"\s+", " ", phrase)
            if 2 <= len(phrase) <= 60:
                candidates.append(phrase)

    packed = []
    seen = set()
    used_chars = 0
    for tag in candidates:
        key = tag.lower()
        if key in seen:
            continue
        # YouTube counts quotes around multi-word tags + comma overhead toward the 500-char cap
        cost = len(tag) + (2 if " " in tag else 0) + (1 if packed else 0)
        if used_chars + cost > max_chars:
            continue
        seen.add(key)
        packed.append(tag)
        used_chars += cost
    return packed

def build_seo_description(data, hashtags_str, affiliate_block=""):
    """Constructs a high-CTR, search-indexed description with an open hook, debate CTA, and clean hashtags."""
    desc_hook = (data.get("description") or "").strip()
    cta_q = (data.get("cta_question") or data.get("poll_question") or "").strip()
    parts = [desc_hook] if desc_hook else []
    if cta_q:
        parts.append(f"💬 What do you think? {cta_q}")
    parts.append("🔔 Subscribe for daily 30-second mysteries & check the channel playlists for Part 2!")
    if affiliate_block:
        parts.append(affiliate_block.strip())
    if hashtags_str:
        parts.append(hashtags_str.strip())
    return "\n\n".join(parts)[:4900]

def fetch_top_performing_titles(credentials, max_results=5):
    try:
        analytics = build("youtubeAnalytics", "v2", credentials=credentials)
        youtube = build("youtube", "v3", credentials=credentials)
        end = time.strftime("%Y-%m-%d")
        start = time.strftime("%Y-%m-%d", time.localtime(time.time() - 30 * 86400))
        report = analytics.reports().query(ids="channel==MINE", startDate=start, endDate=end, metrics="views", dimensions="video", sort="-views", maxResults=max_results).execute()
        rows = report.get("rows", [])
        if not rows: return []
        view_map = {r[0]: int(r[1]) for r in rows}
        vids = youtube.videos().list(part="snippet", id=",".join([r[0] for r in rows])).execute()
        return [f"{item['snippet']['title']} ({view_map.get(item['id'], 0)} views)" for item in vids.get("items", [])]
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
        
        # Preserve multi-word search tags intact while deduplicating case-insensitively
        seen = set()
        cleaned_tags = []
        for t in dynamic_tags:
            val = str(t).strip()
            if val and val.lower() not in seen:
                seen.add(val.lower())
                cleaned_tags.append(val)
        print(f"✅ Successfully extracted {len(cleaned_tags)} dynamic tags from top videos!")
        return cleaned_tags[:30]
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


def generate_text_with_fallback(prompt, temperature=0.8):
    """Generates short plain text (e.g., pinned comment or reply) using Gemini model fallbacks then Groq."""
    for model_name in GEMINI_MODELS:
        try:
            res = gemini_client.models.generate_content(model=model_name, contents=prompt)
            text = (res.text or "").strip().strip('"')
            if text:
                return text
        except Exception:
            continue

    groq_key = os.environ.get("GROQ_API_KEY")
    if groq_key:
        for model_name in GROQ_MODELS:
            try:
                r = requests.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
                    json={
                        "model": model_name,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": temperature,
                    },
                    timeout=30,
                )
                if r.status_code == 200:
                    text = r.json()["choices"][0]["message"]["content"].strip().strip('"')
                    if text:
                        return text
            except Exception:
                continue
    return None

def generate_script_via_groq_fallback(prompt, tags_string):
    """Last-resort fallback if ALL Gemini models are exhausted/failing. Uses
    Groq (genuinely generous free tier, OpenAI-compatible API, no mysterious
    quota issues reported) for generation, and Tavily's free tier (built
    specifically for feeding search results into LLM prompts) to replace
    Gemini's built-in Google Search grounding tool, which Groq doesn't have
    natively."""
    groq_key = os.environ.get("GROQ_API_KEY")
    tavily_key = os.environ.get("TAVILY_API_KEY")
    if not groq_key:
        print("ℹ️ Groq fallback skipped - GROQ_API_KEY not set.")
        return None

    search_context = ""
    if tavily_key:
        try:
            print("🔎 Fetching live trend context via Tavily (Groq has no built-in search)...")
            r = requests.post("https://api.tavily.com/search", json={
                "api_key": tavily_key,
                "query": "viral mystery trends unsolved phenomena trending today",
                "max_results": 5,
            }, timeout=30)
            if r.status_code == 200:
                results = r.json().get("results", [])
                search_context = "\n".join(f"- {res.get('title', '')}: {res.get('content', '')[:200]}" for res in results)
        except Exception as e:
            print(f"⚠️ Tavily search fetch failed, proceeding without live context: {e}")

    full_prompt = prompt
    if search_context:
        full_prompt = f"Current trending context from live web search:\n{search_context}\n\n{prompt}"

    for model_name in GROQ_MODELS:
        try:
            print(f"🧠 Falling back to Groq ({model_name}) for script generation...")
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
                json={
                    "model": model_name,
                    "messages": [{"role": "user", "content": full_prompt}],
                    "temperature": 0.9,
                    "response_format": {"type": "json_object"},
                },
                timeout=60,
            )
            if r.status_code != 200:
                print(f"⚠️ Groq fallback ({model_name}) failed: {r.status_code} - {r.text[:300]}")
                continue
            raw_text = r.json()["choices"][0]["message"]["content"]
            data = extract_json_object(raw_text)
            print(f"✅ Groq fallback succeeded using {model_name}.")
            return data
        except Exception as e:
            print(f"⚠️ Groq fallback ({model_name}) error: {e}")
    return None

def generate_script(avoid_topics=None, boost_topics=None, dynamic_tags_list=None, retention_dropoff_sec=None, viewer_requests=None):
    print("🧠 Asking Gemini to scan live web trends and write the script...")
    
    if not dynamic_tags_list:
        dynamic_tags_list = ["Shorts", "Science Mystery", "Unexplained Phenomena", "Space Mysteries"]
        
    tags_string = ", ".join(dynamic_tags_list[:20])

    avoid_block = f"\n    Do NOT repeat these already-covered topics: {'; '.join(avoid_topics[-50:])}\n" if avoid_topics else ""
    boost_block = f"\n    Top performing channel videos (reverse-engineer why viewers clicked & watched): {'; '.join(boost_topics)}\n" if boost_topics else ""
    demand_block = ""
    if viewer_requests:
        demand_block = f"\n    HIGH-DEMAND VIEWER REQUEST (prioritize or weave this topic in if not already covered): {viewer_requests[0]}\n"
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
    Search the web for high-demand, trending unsolved mysteries, bizarre scientific discoveries, deep-sea/space anomalies, or shocking historical oddities with massive mainstream curiosity right now.
    Write an optimized 22-28 second YouTube Shorts script (55-75 spoken words) in English. Zero filler, maximum curiosity and retention.
    {avoid_block}{boost_block}{demand_block}{retention_block}{cliffhanger_block}
    
    Script Structure Rules (Algorithm Optimization):
    1. HOOK (First 3 seconds): Immediate pattern interrupt with concrete stakes, numbers, dates, or a shocking contradiction. NEVER start with "Did you know" or "Have you ever wondered".
    2. ESCALATION BODY: Short, visual, high-tension sentences. Raise a second surprise halfway through so viewers don't swipe away.
    3. PAYOFF + SEAMLESS LOOP: Save the craziest twist for the final 3 seconds, and end mid-thought/phrase so it bleeds seamlessly back into the first word of the hook (driving >100% rewatch retention).
    
    High-CTR Packaging Rules (CRITICAL):
    - "title": 42-60 characters MAX (including " #Shorts" at the end) so it NEVER gets truncated on mobile. Front-load the curiosity gap in the first 35 chars. Use specific numbers, years, or high-stakes tension (e.g., "Scientists Found THIS Under Antarctica #Shorts", "The 1977 Radio Signal That Never Repeated #Shorts", "Why 95% Of Earth's Ocean Is Unmapped #Shorts"). NEVER use generic "X Mystery #shorts" templates or overuse the word "Mystery".
    - "thumbnail_text": 2 to 4 ultra-short, shocking UPPERCASE words ONLY (e.g., "IT WASN'T EMPTY", "95% UNMAPPED", "ZERO SURVIVORS", "4 MINS TOO LATE"). Never write a full sentence.
    - "description": 2 tight, keyword-rich sentences (120-160 chars) written for YouTube Search & Suggested discovery. Open with primary search keywords + an open curiosity loop that makes viewers watch instead of spoiling the twist.
    - "tags": 3 to 4 focused #Hashtags relevant to the video (always include #Shorts, e.g. ["#Shorts", "#DeepSea", "#ScienceFacts"]).
    - "seo_tags": 10 to 15 high-demand YouTube Search keyword phrases (2-4 words each, NO # symbol, mixing specific topic queries + broad niche demand like "unsolved ocean mysteries", "weird science discoveries"). Incorporate relevant winning terms from: {tags_string}.
    - "image_prompts": Provide 8 distinct, vivid vertical-scene image prompts matching the beat-by-beat progression of the script.

    Monetization, Demand & Engagement:
    - "affiliate_keyword": A generic 1-2 word product search term related to this topic (e.g., "telescope", "night vision").
    - "cta_question": A polarizing, debate-sparking question for viewers to answer in the comments (drives comment velocity & algorithm push).
    - "pinned_comment": A short 1-sentence pinned comment asking that debate question + teasing the next video.
    - "poll_question": A highly debatable question about this topic for a Community Tab Poll.
    - "poll_options": An array of 3 short possible answers.

    Cinematic Sound Design:
    - "sfx_cues": Assign 2 or 3 sound effects to specific image clips. Provide the "clip_index" (0 to 7) and a 1-2 word "query" to search on Pixabay (e.g., "heartbeat", "thunder", "metal clank"). 

    Format as valid JSON only (no extra commentary) exactly like this schema:
    {{
      "script": "...",
      "visual_theme": "...",
      "image_prompts": ["...", "...", "...", "...", "...", "...", "...", "..."],
      "title": "...",
      "thumbnail_text": "...",
      "tags": ["#Shorts", "#Tag2", "#Tag3"],
      "seo_tags": ["keyword phrase 1", "keyword phrase 2", "..."],
      "description": "...",
      "cta_question": "...",
      "pinned_comment": "...",
      "is_cliffhanger": true/false,
      "cliffhanger_setup": "...",
      "affiliate_keyword": "...",
      "poll_question": "...",
      "poll_options": ["...", "...", "..."],
      "sfx_cues": [
        {{"clip_index": 2, "query": "thunder"}},
        {{"clip_index": 5, "query": "heartbeat"}}
      ]
    }}
    """

    for model_name in GEMINI_MODELS:
        for attempt in range(3):
            try:
                response = gemini_client.models.generate_content(
                    model=model_name,
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

                data = extract_json_object(raw_text)
                data["title"] = optimize_title(data.get("title", ""))
                if data.get("is_cliffhanger") and data.get("cliffhanger_setup"):
                    with open(CLIFFHANGER_FILE, "w") as f: f.write(data["cliffhanger_setup"])
                return data
            except Exception as e:
                err_str = str(e)
                print(f"⚠️ Gemini ({model_name}) Attempt {attempt + 1} Failed: {e}")
                if "404" in err_str or "NOT_FOUND" in err_str:
                    break  # Don't burn retries on a retired/non-existent model
                if "429" in err_str or "503" in err_str:
                    # Exponential backoff per Google's own troubleshooting guidance -
                    # 15s, 45s, 90s instead of a flat 45s every time.
                    time.sleep([15, 45, 90][attempt])
                else:
                    time.sleep(5)
        print(f"ℹ️ Giving up on {model_name}, trying next model if available...")

    print("ℹ️ All Gemini models exhausted - trying Groq fallback before giving up entirely...")
    fallback_data = generate_script_via_groq_fallback(prompt, tags_string)
    if fallback_data:
        fallback_data["title"] = optimize_title(fallback_data.get("title", ""))
        if fallback_data.get("is_cliffhanger") and fallback_data.get("cliffhanger_setup"):
            with open(CLIFFHANGER_FILE, "w") as f: f.write(fallback_data["cliffhanger_setup"])
        return fallback_data

    raise Exception("Failed to get response from Gemini (all models exhausted) and Groq fallback also failed or wasn't configured.")

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
def build_estimated_word_timestamps(text, total_duration):
    """Build proportional word-level timestamps when a TTS engine omits WordBoundary events."""
    raw_tokens = [tok.strip() for tok in (text or "").split() if tok.strip()]
    if not raw_tokens or total_duration <= 0:
        return []
    weights = []
    for tok in raw_tokens:
        base = max(2, len("".join(ch for ch in tok if ch.isalnum())))
        pause = 2.5 if tok[-1] in ".!?" else (1.2 if tok[-1] in ",;:" else 0.0)
        weights.append(base + pause)
    total_weight = sum(weights) or float(len(raw_tokens))
    words = []
    cursor = 0.0
    for tok, weight in zip(raw_tokens, weights):
        dur = total_duration * (weight / total_weight)
        end = min(total_duration, cursor + dur)
        words.append({"text": tok, "start": round(cursor, 4), "end": round(end, 4)})
        cursor = end
    return words


def _read_audio_duration(audio_path):
    clip = AudioFileClip(audio_path)
    try:
        return float(clip.duration or 0.0)
    finally:
        clip.close()


async def generate_audio_and_timestamps(text):
    words = []
    edge_voices = [VOICE] + [v for v in ("en-US-JennyNeural", "en-US-GuyNeural") if v != VOICE]
    for voice_name in edge_voices[:2]:
        try:
            print(f"🎙️ Generating AI Voiceover ({voice_name})...")
            communicate = edge_tts.Communicate(text, voice_name)
            attempt_words = []
            with open(OUTPUT_AUDIO, "wb") as fp:
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        fp.write(chunk["data"])
                    elif chunk["type"] == "WordBoundary":
                        attempt_words.append({
                            "text": chunk["text"],
                            "start": chunk["offset"] / 10000000.0,
                            "end": (chunk["offset"] + chunk["duration"]) / 10000000.0,
                        })
            if os.path.exists(OUTPUT_AUDIO) and os.path.getsize(OUTPUT_AUDIO) > 1024:
                words = attempt_words
                if not words:
                    dur = _read_audio_duration(OUTPUT_AUDIO)
                    words = build_estimated_word_timestamps(text, dur)
                with open("words.json", "w") as f:
                    json.dump(words, f)
                return
        except Exception as e:
            print(f"⚠️ edge-tts ({voice_name}) failed: {e}")

    # Fallback 1: gTTS (resilient when Bing WSS returns 403 on cloud runner IPs)
    try:
        from gtts import gTTS
        print("🎙️ Falling back to gTTS voiceover...")
        await asyncio.to_thread(lambda: gTTS(text=text, lang="en", slow=False).save(OUTPUT_AUDIO))
        if os.path.exists(OUTPUT_AUDIO) and os.path.getsize(OUTPUT_AUDIO) > 1024:
            dur = _read_audio_duration(OUTPUT_AUDIO)
            words = build_estimated_word_timestamps(text, dur)
            with open("words.json", "w") as f:
                json.dump(words, f)
            print(f"✅ gTTS fallback generated {dur:.2f}s audio with {len(words)} synced words.")
            return
    except Exception as e:
        print(f"⚠️ gTTS fallback failed: {e}")

    # Fallback 2: offline espeak-ng + ffmpeg if available on runner
    import shutil
    import subprocess
    espeak_bin = shutil.which("espeak-ng") or shutil.which("espeak")
    if espeak_bin:
        tmp_wav = "voiceover_tmp.wav"
        try:
            print(f"🎙️ Falling back to offline TTS ({os.path.basename(espeak_bin)})...")
            subprocess.run([espeak_bin, "-s", "165", "-w", tmp_wav, text], check=True)
            ffmpeg_bin = shutil.which("ffmpeg")
            if not ffmpeg_bin:
                import imageio_ffmpeg
                ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
            subprocess.run([ffmpeg_bin, "-y", "-i", tmp_wav, OUTPUT_AUDIO], check=True)
            if os.path.exists(OUTPUT_AUDIO) and os.path.getsize(OUTPUT_AUDIO) > 1024:
                dur = _read_audio_duration(OUTPUT_AUDIO)
                words = build_estimated_word_timestamps(text, dur)
                with open("words.json", "w") as f:
                    json.dump(words, f)
                print(f"✅ Offline TTS generated {dur:.2f}s audio with {len(words)} synced words.")
                return
        finally:
            if os.path.exists(tmp_wav):
                os.remove(tmp_wav)

    raise RuntimeError("All TTS providers (edge-tts, gTTS, espeak) failed to generate voiceover audio.")

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

def _load_bold_font(fontsize):
    from PIL import ImageFont
    font_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/impact.ttf",
    ]
    for path in font_candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, fontsize)
            except Exception:
                continue
    return ImageFont.load_default()

def _wrap_thumbnail_words(text_hook):
    """Wraps thumbnail text cleanly into 1-2 balanced lines without mid-word truncation."""
    import re
    words = re.sub(r"\s+", " ", (text_hook or "").strip()).upper().split()
    words = words[:4]  # Max 4 words keeps thumbnails instantly readable on mobile
    if not words:
        return []
    if len(words) <= 2 and len(" ".join(words)) <= 12:
        return [" ".join(words)]
    mid = (len(words) + 1) // 2
    return [" ".join(words[:mid]), " ".join(words[mid:])]

def _render_thumbnail_banner(lines, max_width=920):
    """Renders 1-2 lines of auto-sized 3D yellow/white text over a dark contrast pill."""
    from PIL import Image, ImageDraw
    fontsize = 128
    font = _load_bold_font(fontsize)
    tmp = Image.new("RGBA", (10, 10))
    tmp_draw = ImageDraw.Draw(tmp)

    while fontsize > 52:
        font = _load_bold_font(fontsize)
        widths = [tmp_draw.textbbox((0, 0), line, font=font, stroke_width=8)[2] for line in lines]
        if max(widths) <= max_width:
            break
        fontsize -= 6

    line_boxes = [tmp_draw.textbbox((0, 0), line, font=font, stroke_width=8) for line in lines]
    line_heights = [b[3] - b[1] for b in line_boxes]
    line_widths = [b[2] - b[0] for b in line_boxes]
    line_gap = 18
    depth = 10
    pad_x, pad_y = 44, 32

    total_w = max(line_widths) + depth + pad_x * 2
    total_h = sum(line_heights) + line_gap * (len(lines) - 1) + depth + pad_y * 2

    img = Image.new("RGBA", (total_w, total_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Dark translucent backdrop guarantees text pops even against bright AI imagery
    draw.rounded_rectangle([(0, 0), (total_w - 1, total_h - 1)], radius=28, fill=(0, 0, 0, 175))

    y_cursor = pad_y
    for idx, line in enumerate(lines):
        bbox = line_boxes[idx]
        lw, lh = line_widths[idx], line_heights[idx]
        ox = (total_w - lw) // 2 - bbox[0]
        oy = y_cursor - bbox[1]
        # Highlight line 1 in high-CTR yellow, line 2 in crisp white
        fill = (255, 220, 0) if idx == 0 else (255, 255, 255)
        for d in range(depth, 0, -1):
            draw.text((ox + d, oy + d), line, font=font, fill=(0, 0, 0, 255))
        draw.text((ox, oy), line, font=font, fill=fill + (255,), stroke_width=8, stroke_fill=(0, 0, 0, 255))
        y_cursor += lh + line_gap

    return img

def generate_ai_thumbnail(title, text_hook="", visual_theme="", out_path="thumbnail.jpg"):
    print("🖼️ Painting Custom High-CTR AI Thumbnail...")
    try:
        pol_api = base64.b64decode("aHR0cHM6Ly9pbWFnZS5wb2xsaW5hdGlvbnMuYWkvcHJvbXB0Lw==").decode("utf-8")
        prompt = (
            f"Ultra-dramatic high-contrast focal close-up for {title}, {visual_theme}, "
            "cinematic rim lighting, glowing mysterious subject centered in lower two-thirds, "
            "clean dark negative space at top third, hyper-detailed, 8k, no text, no words, no watermark"
        )
        url = f"{pol_api}{urllib.parse.quote(prompt)}?width=1080&height=1920&nologo=true&seed={random.randint(1, 999999)}"
        
        if safe_download(url, out_path, min_bytes=20000):
            lines = _wrap_thumbnail_words(text_hook)
            if lines:
                base_img = PIL.Image.open(out_path).convert("RGBA")
                txt_img = _render_thumbnail_banner(lines, max_width=base_img.width - 140)
                pos_x = max(0, (base_img.width - txt_img.width) // 2)
                # Place in upper-third safe zone so YouTube Shorts bottom/right UI never obscures it
                pos_y = max(80, (base_img.height // 4) - (txt_img.height // 2))
                base_img.paste(txt_img, (pos_x, pos_y), txt_img)
                base_img.convert("RGB").save(out_path, quality=95)
                print("✅ High-CTR thumbnail banner composited cleanly!")
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

def render_3d_word(text, fontsize=120, fill=(255, 255, 0), depth=10, depth_color=(120, 95, 0), max_width=960):
    from PIL import Image, ImageDraw
    tmp = Image.new("RGBA", (10, 10))
    draw_tmp = ImageDraw.Draw(tmp)
    current_size = fontsize
    font = _load_bold_font(current_size)
    bbox = draw_tmp.textbbox((0, 0), text, font=font, stroke_width=6)
    while (bbox[2] - bbox[0]) + depth + 48 > max_width and current_size > 48:
        current_size -= 8
        font = _load_bold_font(current_size)
        bbox = draw_tmp.textbbox((0, 0), text, font=font, stroke_width=6)
    W, H = (bbox[2] - bbox[0]) + depth + 48, (bbox[3] - bbox[1]) + depth + 48
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    ox, oy = (depth + 24) - bbox[0], (depth + 24) - bbox[1]
    for d in range(depth, 0, -1): draw.text((ox + d, oy + d), text, font=font, fill=depth_color + (255,))
    draw.text((ox, oy), text, font=font, fill=fill + (255,), stroke_width=6, stroke_fill=(0, 0, 0, 255))
    return np.array(img)

def is_high_impact(word):
    impact_words = {
        "MILLION", "BILLION", "TRILLION", "SECRET", "SHOCKING", "MYSTERY", "ONLY",
        "NEVER", "HIDDEN", "IMPOSSIBLE", "VANISHED", "FOUND", "WARNING", "DEADLIEST",
        "BANNED", "DELETED", "PROOF", "ALONE", "ZERO", "FIRST", "LAST", "REAL", "TRAP",
    }
    return any(char.isdigit() for char in word) or word.strip(".,!?\"'").upper() in impact_words

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
        txt_clip = ImageClip(temp_img_path, transparent=True).set_position(('center', 1150)).set_start(start_t).set_end(end_t)
        
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
    
    # Export with ultra-high quality CRF 18 and 12,000k bitrate.
    # Explicit pix_fmt/movflags/audio settings below match Meta's documented
    # encoding recommendations for Instagram/Facebook - without forcing these,
    # ffmpeg's default choices can produce a technically-valid H.264/AAC file
    # that Instagram's stricter parser still rejects with a generic
    # "couldn't process this video" error (most commonly caused by a pixel
    # format other than yuv420p, or the moov atom not being at the front of
    # the file for streaming).
    final.write_videofile(
        "final_short.mp4", 
        fps=24, 
        codec="libx264", 
        audio_codec="aac", 
        audio_fps=48000,
        threads=4, 
        bitrate="12000k", 
        ffmpeg_params=[
            "-crf", "18", "-preset", "slow",
            "-pix_fmt", "yuv420p",
            "-profile:v", "high",
            "-level", "4.2",
            "-movflags", "+faststart",
            "-ar", "48000",
        ]
    )
    final.close()

# --- 5. UPLOAD & SYNDICATION ---
def get_public_url(video_file):
    print("☁️ Uploading heavy-duty media for Zernio syndication...")

    # GitHub Releases - PRIMARY method. Gives a permanent, stable public URL.
    # Filebin (previously used here) redirects its download URL to a presigned
    # S3 link that expires after only 60 seconds (confirmed via Filebin's own
    # API spec) - fine for a human clicking immediately, but async third-party
    # fetchers (like Instagram's Reels ingestion pipeline) can take longer than
    # that to actually pull the file, so the link is already dead by the time
    # they try. That's the most likely explanation for Instagram consistently
    # failing to process the video while Facebook (faster to fetch) succeeded.
    try:
        print("   -> Trying GitHub Release (stable, non-expiring URL)...")
        token = os.environ.get("GITHUB_TOKEN")
        repo = os.environ.get("GITHUB_REPOSITORY")
        if token and repo:
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
            tag = f"short-{int(time.time())}"
            r = requests.post(f"https://api.github.com/repos/{repo}/releases", headers=headers, json={
                "tag_name": tag, "name": tag, "body": "Auto-generated video asset for cross-posting.",
                "draft": False, "prerelease": False,
            }, timeout=30)
            if r.status_code in (200, 201):
                release = r.json()
                upload_url = release["upload_url"].split("{")[0]
                with open(video_file, "rb") as f:
                    video_bytes = f.read()
                r2 = requests.post(f"{upload_url}?name={os.path.basename(video_file)}",
                                    headers={**headers, "Content-Type": "video/mp4"},
                                    data=video_bytes, timeout=180)
                if r2.status_code in (200, 201):
                    print("   ✅ GitHub Release upload successful (stable URL)!")
                    return r2.json().get("browser_download_url")
                else:
                    print(f"   ⚠️ GitHub Release asset upload failed: {r2.text[:300]}")
            else:
                print(f"   ⚠️ GitHub Release creation failed: {r.text[:300]}")
        else:
            print("   ⚠️ GITHUB_TOKEN/GITHUB_REPOSITORY not available in this environment - skipping GitHub Release method.")
    except Exception as e:
        print(f"   ⚠️ GitHub Release error: {e}")

    # Fallback 1: Filebin (fine for Facebook, which fetches fast - but NOT
    # reliable for Instagram per the expiring-URL issue above). Only used if
    # GitHub Release hosting isn't available.
    try:
        print("   -> Trying Filebin (fallback - may still fail for Instagram specifically)...")
        import string, random
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

    # Fallback 2: Uguu.se (128MB limit fallback)
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

    return None

def upload_to_youtube(video_file, data, credentials):
    youtube = build("youtube", "v3", credentials=credentials)
    final_title = optimize_title(data.get("title", ""))
    data["title"] = final_title
    hashtags = build_description_hashtags(data.get("tags", []))
    youtube_tags = build_youtube_seo_tags(
        seo_tags=data.get("seo_tags", []),
        hashtags_list=data.get("tags", []),
        channel_tags=data.get("_channel_tags", []),
    )
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
        affiliate_block = f"👇 Gear up:\n{affiliate_link}"

    full_description = build_seo_description(data, hashtags, affiliate_block)
    body = {
        "snippet": {
            "categoryId": "28",
            "title": final_title,
            "description": full_description,
            "tags": youtube_tags,
        },
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }
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

def post_auto_comment(video_id, script_text, affiliate_link, credentials, preset_comment=None):
    print("💬 Posting monetized auto-comment...")
    youtube = build("youtube", "v3", credentials=credentials)
    support_line = f"\n\n🔍 Support the channel: {affiliate_link}" if affiliate_link else ""
    comment_text = (preset_comment or "").strip().strip('"')
    if not comment_text:
        comment_text = generate_text_with_fallback(
            f"Write ONE short, debate-sparking question (under 20 words) to pin in the YouTube comments for this script: {script_text}"
        ) or "What do you think actually happened here? Drop your theory below!"
    try:
        youtube.commentThreads().insert(
            part="snippet",
            body={"snippet": {"videoId": video_id, "topLevelComment": {"snippet": {"textOriginal": f"{comment_text}{support_line}"}}}},
        ).execute()
        print("✅ Monetized auto-comment posted!")
    except Exception as e:
        print(f"⚠️ Auto-comment skipped: {e}")

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
        hashtags_str = build_description_hashtags(data.get("tags", []))
        social_caption = f"{data.get('title', '')}\n\n{data.get('description', '')}\n\n{hashtags_str}".strip()[:2200]
        payload = {
            "content": social_caption,
            "mediaItems": [{"type": "video", "url": url}],
            "platforms": platform_entries,
            "publishNow": True
        }

        try:
            # Zernio processes posts asynchronously - status moves through
            # scheduled -> publishing -> published (or failed/partial). The
            # CREATION response below only confirms Zernio accepted the request,
            # NOT that it actually went live - that was the bug: this used to be
            # treated as final success, but the real status arrives later. Must
            # poll GET /v1/posts/{postId} until it reaches a terminal state.
            r = requests.post(post_url, headers=headers, json=payload, timeout=300)
            if r.status_code not in (200, 201, 202, 207):
                print(f"⚠️ ZERNIO Post Creation Error ({r.status_code}): {r.text}")
                return False

            post_id = r.json().get("post", {}).get("_id")
            if not post_id:
                print(f"⚠️ Zernio response had no post ID to track: {r.text[:300]}")
                return False

            print(f"ℹ️ Post created (id: {post_id}), polling for actual publish status...")
            status_url = f"{post_url}/{post_id}"
            terminal_states = ("published", "failed", "partial")
            final_post = None

            for attempt in range(24):  # up to 24 * 10s = 4 minutes
                time.sleep(10)
                try:
                    s = requests.get(status_url, headers=headers, timeout=30)
                    if s.status_code != 200:
                        continue
                    post_obj = s.json().get("post", s.json())
                    if post_obj.get("status") in terminal_states:
                        final_post = post_obj
                        break
                except requests.exceptions.RequestException as poll_err:
                    # A single slow/failed poll shouldn't kill the whole loop or fall
                    # through to the initial-POST timeout handler below (which prints
                    # a misleading "media upload already succeeded" message meant for
                    # a completely different failure point). Just log and keep polling.
                    print(f"  (poll attempt {attempt + 1} had a network hiccup, continuing: {poll_err})")
                    continue

            if final_post is None:
                print("⚠️ Zernio post never reached a final status within 4 minutes - check "
                      "zernio.com/dashboard directly to confirm what actually happened.")
                return False

            # Report the REAL per-platform outcome, with the live URL as actual proof
            any_success = False
            for entry in final_post.get("platforms", []):
                plat = entry.get("platform", "?")
                plat_status = entry.get("status", "unknown")
                plat_url = entry.get("platformPostUrl")
                if plat_status == "published" and plat_url:
                    print(f"  ✅ {plat}: published - {plat_url}")
                    any_success = True
                else:
                    # Print the FULL raw entry rather than guessing a specific field
                    # name for the error - last time this guessed wrong ("error")
                    # and printed "no further detail", hiding the real reason.
                    print(f"  ❌ {plat}: {plat_status} - full detail: {entry}")

            if any_success:
                print("✅ Zernio cross-post confirmed live (see links above).")
            else:
                print("❌ Zernio post finished processing but did NOT actually go live on any platform.")
            return any_success

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
                reply_text = generate_text_with_fallback(
                    f"Write ONE short, warm, engaging reply (under 15 words) to this YouTube comment: \"{top_comment}\""
                )
                if not reply_text:
                    continue
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

    # Fetch tags from your highest reach videos and queued viewer requests
    viral_tags = fetch_high_reach_tags(credentials)
    viewer_requests = load_viewer_requests()
    
    # Pass the tags and viewer demand into the AI generator
    content = generate_script(
        avoid_topics=[h["title"] for h in load_topic_history()], 
        boost_topics=boost_topics,
        dynamic_tags_list=viral_tags,
        retention_dropoff_sec=retention_dropoff_sec,
        viewer_requests=viewer_requests,
    )
    content["_channel_tags"] = viral_tags
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
        post_auto_comment(vid_id, content["script"], aff_link, credentials, preset_comment=content.get("pinned_comment"))
        save_topic_history({"title": content["title"], "video_id": vid_id, "date": time.strftime("%Y-%m-%d")})
        if viewer_requests:
            try:
                with open(VIEWER_REQUESTS_FILE, "w", encoding="utf-8") as f:
                    json.dump(viewer_requests[1:], f, indent=2)
            except Exception:
                pass
        youtube_client = build("youtube", "v3", credentials=credentials)
        add_video_to_themed_playlist(youtube_client, vid_id, content.get("visual_theme", ""))
    repost_via_zernio("final_short.mp4", content)

if __name__ == "__main__":
    asyncio.run(main())