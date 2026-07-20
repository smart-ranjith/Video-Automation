import os
import time
import shutil
from google import genai
from gradio_client import Client
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
import json

client_genai = genai.Client()

HF_SPACE_ID = "OpenKing/wan2-video-generation"
HF_API_NAME = "/generate_video"  

def build_space_payload(video_prompt):
    return [video_prompt, None, 576, 1024, 73, 35, 5.0, -1]

def generate_daily_prompt(max_retries=5, base_delay=2):
    print("Generating today's unique video concept...")
    for attempt in range(max_retries):
        try:
            response = client_genai.models.generate_content(
                model='gemini-2.5-flash',
                contents="Write a highly engaging, mind-blowing fact prompt for a 3-second vertical AI video. Describe the visual action, subject, and cinematic style. No formatting."
            )
            return response.text.strip()
        except Exception as e:
            if "503" in str(e) or "429" in str(e):
                time.sleep(base_delay * (2 ** attempt))
            else:
                raise e
    raise Exception("Gemini API unreachable.")

def render_video_free(prompt_text, output_filename="daily_short.mp4"):
    print(f"Connecting to: {HF_SPACE_ID}...")
    hf_client = Client(HF_SPACE_ID)
    
    try:
        payload = build_space_payload(prompt_text)
        result = hf_client.predict(*payload, api_name=HF_API_NAME)
        
        if isinstance(result, (list, tuple)) and len(result) > 0:
            video_data = result[0]
            temp_video_path = video_data.get('video') if isinstance(video_data, dict) else video_data
        else:
            temp_video_path = result
        
        if not temp_video_path or not os.path.exists(temp_video_path):
            raise Exception("Invalid file path returned from cloud.")
            
        shutil.copy(temp_video_path, output_filename)
        print(f"Video saved: {output_filename}")
        return output_filename
    except Exception as e:
        print(f"Generation error: {e}")
        raise e

def upload_to_youtube(video_file, title, description):
    print(f"Uploading {video_file} to YouTube...")
    # Load token from environment variable secret injected by GitHub Actions
    token_data = json.loads(os.getenv("YOUTUBE_TOKEN_JSON"))
    creds = Credentials.from_authorized_user_info(token_data, ['https://www.googleapis.com/auth/youtube.upload'])
    youtube = build('youtube', 'v3', credentials=creds)

    body = {
        'snippet': {'title': title, 'description': description + "\n\n#Shorts #Facts #AIVideo", 'tags': ['shorts', 'facts', 'ai'], 'categoryId': '27'},
        'status': {'privacyStatus': 'private', 'selfDeclaredMadeForKids': False}
    }

    media = MediaFileUpload(video_file, mimetype='video/mp4', resumable=True)
    request = youtube.videos().insert(part=','.join(body.keys()), body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
    print(f"Upload Complete! Video ID: {response['id']}")

if __name__ == "__main__":
    # Setup GitHub runner environment token fallback for local testing
    if not os.getenv("YOUTUBE_TOKEN_JSON") and os.path.exists("token.json"):
        with open("token.json", "r") as f:
            os.environ["YOUTUBE_TOKEN_JSON"] = f.read()

    success = False
    # Outer loop: Try up to 3 times if the Hugging Face queue drops the request
    for run_attempt in range(3):
        try:
            print(f"\n--- Starting Generation Attempt {run_attempt + 1} ---")
            daily_prompt = generate_daily_prompt()
            video_path = render_video_free(daily_prompt)
            
            upload_to_youtube(
                video_file=video_path,
                title="Mind-Blowing Fact! 🤯 #Shorts",
                description=f"Generated via AI. Prompt: {daily_prompt}"
            )
            
            if os.path.exists(video_path):
                os.remove(video_path)
            print("Pipeline successfully finished.")
            success = True
            break
        except Exception as queue_error:
            print(f"Attempt failed due to cloud queue drop. Retrying pipeline...")
            time.sleep(15)
            
    if not success:
        print("\nAll 3 daily cloud queue attempts were dropped by the server.")
        exit(1)
