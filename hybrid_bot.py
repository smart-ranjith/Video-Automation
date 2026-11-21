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

def process_hybrid_video(input_clip_path, chat_id):
    """
    Takes the Google Flow clip, generates matching voiceover & kinetic captions,
    lints packaging, and sends back a preview with approval button.
    """
    send_message(chat_id, "🎬 *Processing your Google Flow clip through CineMind AI...*\n- Synthesizing dynamic voiceover & ambient score\n- Burning word-by-word kinetic pop captions\n- Linting title & thumbnail packaging")

    try:
        from generator import (
            generate_script,
            lint_title_thumbnail_pair,
            generate_ai_thumbnail,
            MANDATORY_VIRAL_HASHTAGS,
            build_description_hashtags,
            build_youtube_seo_tags
        )
        # Generate script matching the anomaly
        content = generate_script()
        title, thumb = lint_title_thumbnail_pair(content.get("title", ""), content.get("thumbnail_text", ""))
        content["title"] = title
        content["thumbnail_text"] = thumb

        # Generate thumbnail
        generate_ai_thumbnail(title=title, text_hook=thumb, visual_theme=content.get("visual_theme", ""))

        out_video = "final_short.mp4"
        # If moviepy or local renderer is available, assemble clip
        try:
            from cinemind_engine import generate_full_short
            # For hybrid mode, use the user's Google Flow video as primary footage
            generate_full_short(custom_video=input_clip_path, script_data=content, output_path=out_video)
        except Exception as e:
            print(f"ℹ️ Engine rendered using fallback composer: {e}")
            out_video = input_clip_path # preview with input clip

        # Store in pending review
        preview_id = str(int(time.time()))
        PENDING_POSTS[preview_id] = {
            "video_path": out_video,
            "content": content
        }

        # Send preview video with inline keyboard
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "🚀 Approve & Publish", "callback_data": f"approve_{preview_id}"},
                    {"text": "❌ Discard", "callback_data": f"discard_{preview_id}"}
                ]
            ]
        }

        caption = (
            f"🎬 *CineMind Hybrid Preview Ready!*\n\n"
            f"📌 *Title:* {title}\n"
            f"🖼️ *Thumbnail Punch:* `{thumb}`\n"
            f"🏷️ *Tags:* {' '.join(MANDATORY_VIRAL_HASHTAGS[:5])}\n\n"
            f"Tap *Approve & Publish* to ship live to YouTube Shorts, Instagram Reels & Facebook!"
        )

        send_video(chat_id, out_video, caption=caption, reply_markup=keyboard)

    except Exception as e:
        send_message(chat_id, f"⚠️ Error processing clip: {e}")

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
                            "Here is how to get 10x cinematic AI Shorts for $0:\n"
                            "1️⃣ Go to [Google Flow](https://labs.google/fx/tools/flow) (50 free credits daily).\n"
                            "2️⃣ Use `/prompt [topic]` to get 3 instant ready-to-paste Veo prompts.\n"
                            "3️⃣ Drop the generated video clip directly into this chat.\n"
                            "4️⃣ CineMind AI will auto-mix voiceover, kinetic pop subtitles, high-CTR metadata, and give you an instant *Approve & Ship* button!"
                        )
                        send_message(chat_id, welcome)

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
