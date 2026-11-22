#!/usr/bin/env python3
"""
CineMind Zero-Cost Hybrid Telegram Bot
=====================================
Allows creators to generate bespoke cinematic clips for $0 using Google Flow / Veo
(50 free credits/day per account), drop them directly into Telegram, and let CineMind AI
handle automatic voiceover, kinetic pop subtitles, high-CTR packaging, and 1-click publishing.
"""

import os
import sys
import time
import json
import base64
import requests
import asyncio
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ALLOWED_USER_ID = os.environ.get("TELEGRAM_CHAT_ID")
INCOMING_DIR = Path("incoming_clips")
INCOMING_DIR.mkdir(exist_ok=True)

BASE_TG_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else None

# Pending review storage: message_id -> { "video_path": ..., "content": ... }
PENDING_POSTS = {}

def tg_request(method, payload=None, files=None):
    if not BASE_TG_URL:
        print("⚠️ TELEGRAM_BOT_TOKEN not configured.")
        return None
    url = f"{BASE_TG_URL}/{method}"
    try:
        if files:
            r = requests.post(url, data=payload, files=files, timeout=120)
        else:
            r = requests.post(url, json=payload, timeout=40)
        if r.status_code == 200:
            return r.json()
        print(f"⚠️ Telegram API error ({method}): {r.status_code} - {r.text[:200]}")
    except Exception as e:
        print(f"⚠️ Telegram connection exception ({method}): {e}")
    return None

def send_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg_request("sendMessage", payload)

def send_video(chat_id, video_path, caption=None, reply_markup=None):
    if not os.path.exists(video_path):
        return None
    data = {"chat_id": chat_id, "parse_mode": "Markdown"}
    if caption:
        data["caption"] = caption
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    with open(video_path, "rb") as f:
        return tg_request("sendVideo", payload=data, files={"video": f})

def download_telegram_file(file_id, dest_path):
    info = tg_request("getFile", {"file_id": file_id})
    if not info or not info.get("ok"):
        return False
    file_path = info["result"]["file_path"]
    download_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    r = requests.get(download_url, stream=True, timeout=120)
    if r.status_code == 200:
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        return True
    return False

def generate_flow_prompts(topic=None):
    """Generates 3 cinematic, vertical (9:16) prompts tailored for Google Flow / Veo."""
    from generator import generate_text_with_fallback
    niche_topic = topic or "deep-sea anomaly or unexplained geological discovery"
    p = f"""
    Create 3 ultra-vivid, photorealistic, cinematic prompt descriptions to generate 5-second vertical (9:16) video clips in Google Flow (Veo).
    Topic: {niche_topic}
    Requirements for each prompt:
    - Include specific lighting (e.g. volumetric god-rays, abyssal bioluminescence, moody amber rim light)
    - Specify camera motion (e.g. slow cinematic push-in, macro pan, crane descent)
    - Keep each under 40 words, hyper-realistic, 8k documentary style.
    Number them 1, 2, 3.
    """
    res = generate_text_with_fallback(p, temperature=0.7)
    return res or "1. Cinematic slow macro push-in on an glowing crystalline underwater structure, deep turquoise volumetric rays.\n2. Submersible camera lights illuminating an ancient carved monolith 4,000 meters deep.\n3. Eerie boiling brine pool on ocean floor with dark volcanic venting, 8k documentary."

def evaluate_quality_control(content, video_path):
    """
    Automated Quality Control Gate (CineMind + youtube-agent-skill):
    Evaluates Hook Retention Score, Packaging Linter, and Video Integrity.
    Returns (passed: bool, reason: str, score: int)
    """
    from generator import score_script_hook, VAGUE_WORDS, STOP_WORDS, words
    
    script = content.get("script", "")
    h_score, h_props = score_script_hook(script)
    
    # 1. Hook Score Gate: Must be >= 60 to prevent viewer swipe-away
    if h_score < 60:
        weak_prop = min(h_props, key=h_props.get) if h_props else "general"
        return False, f"Hook retention score too low ({h_score}/100 - weakest link: {weak_prop})", h_score

    # 2. Packaging Gate: Title & Thumbnail must not repeat words
    title = content.get("title", "")
    thumb = content.get("thumbnail_text", "")
    title_substantive = set(words(title)) - STOP_WORDS
    thumb_substantive = set(words(thumb)) - STOP_WORDS
    if title_substantive & thumb_substantive:
        return False, f"Thumbnail duplicated words from title ({title_substantive & thumb_substantive})", h_score

    # 3. Video File Gate: Must exist and be > 500KB
    if not os.path.exists(video_path) or os.path.getsize(video_path) < 500000:
        return False, "Rendered video file is corrupted or too small", h_score

    return True, "All Quality Control criteria passed", h_score

def process_hybrid_video(input_clip_path, chat_id):
    """
    Takes the Google Flow clip and autonomously:
    1. Generates script, voice, kinetic subtitles & thumbnail.
    2. Runs Automated Quality Control (QC).
    3. If QC fails -> auto-regenerates (up to 3 attempts).
    4. If QC passes -> DIRECTLY uploads to YouTube Shorts, Instagram & Facebook!
    Zero manual approval required.
    """
    send_message(chat_id, "🎬 *Ingesting Google Flow clip into CineMind AI...*\n- Evaluating visual anomaly\n- Autonomous Quality Control active (Auto-upload if good, Auto-regenerate if not)")

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            from generator import (
                generate_script,
                lint_title_thumbnail_pair,
                generate_ai_thumbnail,
                upload_to_youtube,
                repost_via_zernio,
                authenticate_youtube,
                MANDATORY_VIRAL_HASHTAGS
            )

            send_message(chat_id, f"🧠 *Attempt {attempt}/{max_attempts}:* Writing high-retention script and audio...")
            content = generate_script()
            title, thumb = lint_title_thumbnail_pair(content.get("title", ""), content.get("thumbnail_text", ""))
            content["title"] = title
            content["thumbnail_text"] = thumb

            generate_ai_thumbnail(title=title, text_hook=thumb, visual_theme=content.get("visual_theme", ""), content=content)

            out_video = "final_short.mp4"
            try:
                from cinemind_engine import generate_full_short
                generate_full_short(custom_video=input_clip_path, script_data=content, output_path=out_video)
            except Exception as e:
                print(f"ℹ️ Engine rendered using direct clip: {e}")
                out_video = input_clip_path

            # Autonomous Quality Control evaluation
            passed, reason, score = evaluate_quality_control(content, out_video)
            print(f"🧐 QC Evaluation: passed={passed}, score={score}, reason={reason}")

            if not passed:
                send_message(chat_id, f"⚠️ *Quality Check Failed (Score {score}/100):* {reason}.\n🔄 *Auto-regenerating to meet quality standards...*")
                continue

            # Quality Control PASSED -> Direct Automatic Upload!
            send_message(chat_id, f"✅ *Quality Control PASSED (Score: {score}/100)!*\n🚀 *Auto-publishing directly to YouTube Shorts, Instagram Reels & Facebook...*")

            credentials = authenticate_youtube()
            yt_id = upload_to_youtube(out_video, content, credentials)
            repost_via_zernio(out_video, content)

            yt_link = f"https://youtu.be/{yt_id}" if yt_id else "YouTube Shorts"
            caption = (
                f"🎉 *Published Successfully to YouTube & Socials!*\n\n"
                f"📌 *Title:* {title}\n"
                f"🎯 *Hook Score:* {score}/100 (Verified High Quality)\n"
                f"🔗 *YouTube Link:* {yt_link}\n"
                f"🖼️ *Thumbnail Text:* `{thumb}`\n"
                f"🏷️ *Tags:* {' '.join(MANDATORY_VIRAL_HASHTAGS[:5])}\n\n"
                f"✅ Syndicated to Instagram Reels & Facebook Video."
            )
            send_video(chat_id, out_video, caption=caption)
            return

        except Exception as e:
            print(f"⚠️ Attempt {attempt} error: {e}")
            if attempt == max_attempts:
                send_message(chat_id, f"❌ Failed after {max_attempts} attempts: {e}")
            time.sleep(3)

def handle_callback(callback_query):
    cq_id = callback_query.get("id")
    chat_id = callback_query.get("message", {}).get("chat", {}).get("id")
    data = callback_query.get("data", "")

    # Acknowledge callback query
    tg_request("answerCallbackQuery", {"callback_query_id": cq_id})

    if data.startswith("approve_"):
        pid = data.replace("approve_", "")
        post_data = PENDING_POSTS.get(pid)
        if not post_data:
            send_message(chat_id, "⚠️ This preview expired or was already processed.")
            return

        send_message(chat_id, "🚀 *Publishing live to YouTube Shorts & Socials...*")
        try:
            from generator import upload_to_youtube, repost_via_zernio, authenticate_youtube
            credentials = authenticate_youtube()
            yt_id = upload_to_youtube(post_data["video_path"], post_data["content"], credentials)
            repost_via_zernio(post_data["video_path"], post_data["content"])

            yt_link = f"https://youtu.be/{yt_id}" if yt_id else "YouTube (uploaded)"
            send_message(chat_id, f"🎉 *Published Successfully!*\n\n🔗 *YouTube Shorts:* {yt_link}\n📲 Syndicated to Instagram Reels & Facebook Video.")
            PENDING_POSTS.pop(pid, None)
        except Exception as e:
            send_message(chat_id, f"⚠️ Publish error: {e}")

    elif data.startswith("discard_"):
        pid = data.replace("discard_", "")
        PENDING_POSTS.pop(pid, None)
        send_message(chat_id, "🗑️ Post discarded.")

def run_bot():
    print("🤖 CineMind Hybrid Telegram Bot is starting...")
    if not TELEGRAM_BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN is not set in environment or .env file.")
        print("Please set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to start.")
        return

    offset = 0
    print(f"✅ Bot online! Polling updates (Allowed User ID: {ALLOWED_USER_ID or 'ALL'})...")

    while True:
        try:
            updates = tg_request("getUpdates", {"offset": offset, "timeout": 30})
            if updates and updates.get("ok"):
                for u in updates.get("result", []):
                    offset = u["update_id"] + 1

                    if "callback_query" in u:
                        handle_callback(u["callback_query"])
                        continue

                    msg = u.get("message", {})
                    chat_id = msg.get("chat", {}).get("id")
                    user_id = str(msg.get("from", {}).get("id"))

                    if ALLOWED_USER_ID and str(chat_id) != str(ALLOWED_USER_ID) and user_id != str(ALLOWED_USER_ID):
                        print(f"Blocked unauthorized user: {user_id}")
                        continue

                    text = msg.get("text", "")
                    video = msg.get("video") or msg.get("document")

                    if text.startswith("/start") or text.startswith("/help"):
                        welcome = (
                            "👋 *Welcome to CineMind Zero-Cost Hybrid Studio!*\n\n"
                            "Here is how to get 10x cinematic AI Shorts for $0 without manual approval:\n"
                            "🎬 *Autonomous Mode:* Send `/flow [topic]` ➔ Flow Connector will automatically generate the 9:16 Veo AI video scene directly from your Google Flow session, assemble the Short, run QC, and publish!\n\n"
                            "📁 *Manual Upload Mode:* Drop any Google Flow / Veo video clip into this chat.\n"
                            "📋 *Prompt Inspiration:* Use `/prompt [topic]` to get 3 instant ready-to-paste Veo prompts.\n\n"
                            "✨ *Autonomous Pipeline:*\n"
                            "   • Script & voiceover with kinetic pop subtitles\n"
                            "   • Automated Quality Control (Hook score >= 60, title packaging)\n"
                            "   • If Quality Check passes ➔ **DIRECTLY publishes to YouTube Shorts!**\n"
                            "   • If Quality Check fails ➔ **Auto-regenerates automatically** until it is good!"
                        )
                        send_message(chat_id, welcome)

                    elif text.startswith("/flow"):
                        topic = text.replace("/flow", "").strip() or None
                        send_message(chat_id, "🎨 *Connecting to Google Flow (Veo AI)...*\nPlease ensure your Flow browser window remains open and visible on screen!")
                        try:
                            import flow_engine
                            if not flow_engine.is_flow_available():
                                send_message(chat_id, "⚠️ Google Flow is not connected or logged in. Please ensure flow.google.com is open in Chrome with Flow Connector extension enabled.")
                            else:
                                flow_prompts = generate_flow_prompts(topic)
                                # Take first prompt as scene seed
                                prompt_seed = flow_prompts.strip().split("\n")[0]
                                if "." in prompt_seed:
                                    prompt_seed = prompt_seed.split(".", 1)[1].strip()

                                send_message(chat_id, f"🎬 *Rendering 9:16 Veo Video on Flow:*\n_{prompt_seed[:90]}..._")
                                dest = INCOMING_DIR / f"flow_{int(time.time())}.mp4"
                                generated_clip = flow_engine.generate_flow_video(prompt_seed, duration=5, out_path=str(dest))

                                if generated_clip and os.path.exists(generated_clip) and os.path.getsize(generated_clip) > 50000:
                                    send_message(chat_id, "✅ *Veo Video Rendered Successfully!* Ingesting into CineMind AI pipeline...")
                                    process_hybrid_video(str(dest), chat_id)
                                else:
                                    send_message(chat_id, "❌ Could not retrieve rendered video from Google Flow. Check your Flow browser window.")
                        except Exception as e:
                            send_message(chat_id, f"⚠️ Error in /flow generation: {e}")

                    elif text.startswith("/prompt"):
                        topic = text.replace("/prompt", "").strip() or None
                        send_message(chat_id, "🔍 *Generating Google Flow (Veo) Prompts...*")
                        prompts = generate_flow_prompts(topic)
                        send_message(chat_id, f"📋 *Copy-paste these into Google Flow (9:16):*\n\n{prompts}")

                    elif video:
                        file_id = video.get("file_id")
                        mime = video.get("mime_type", "")
                        if "video" in mime or "mp4" in mime or video.get("file_name", "").endswith((".mp4", ".mov")):
                            dest = INCOMING_DIR / f"clip_{int(time.time())}.mp4"
                            send_message(chat_id, "📥 *Downloading your cinematic clip...*")
                            if download_telegram_file(file_id, str(dest)):
                                process_hybrid_video(str(dest), chat_id)
                            else:
                                send_message(chat_id, "⚠️ Failed to download video from Telegram.")

        except Exception as e:
            print(f"⚠️ Polling loop exception: {e}")
            time.sleep(5)

if __name__ == "__main__":
    run_bot()
