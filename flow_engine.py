import os
import sys
import json
import time
import shutil
import subprocess
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("flow_engine")

def find_flow_auto_bin():
    """Locates the flow-auto binary across Windows and Linux environments."""
    bin_name = "flow-auto"
    found = shutil.which(bin_name)
    if found:
        return found
    
    # Common Windows npm global paths
    win_npm_candidates = [
        os.path.expandvars(r"%APPDATA%\npm\flow-auto.cmd"),
        os.path.expandvars(r"%APPDATA%\npm\flow-auto"),
        os.path.expandvars(r"%USERPROFILE%\AppData\Roaming\npm\flow-auto.cmd"),
        r"C:\Users\ranji\AppData\Roaming\npm\flow-auto.cmd",
    ]
    for c in win_npm_candidates:
        if os.path.exists(c):
            return c
    return "flow-auto"

FLOW_AUTO_BIN = find_flow_auto_bin()

def get_flow_status():
    """
    Returns (available: bool, status_dict: dict, error_msg: str)
    Checks if flow-auto bridge daemon and browser extension are connected.
    """
    try:
        proc = subprocess.run(
            [FLOW_AUTO_BIN, "status", "--site", "flow", "--json"],
            capture_output=True,
            text=True,
            timeout=15,
            shell=True if sys.platform == "win32" else False
        )
        stdout = proc.stdout.strip()
        if not stdout:
            return False, {}, "No output from flow-auto status"
        
        # flow-auto status may prepend notice lines before the JSON object
        json_start = stdout.find("{")
        json_end = stdout.rfind("}")
        if json_start != -1 and json_end != -1:
            json_str = stdout[json_start : json_end + 1]
            data = json.loads(json_str)
            is_connected = bool(data.get("extensionConnected") and data.get("loggedIn"))
            return is_connected, data, ""
        return False, {}, f"Unable to parse status JSON: {stdout[:200]}"
    except Exception as e:
        return False, {}, str(e)

def is_flow_available():
    """Quick boolean check if Flow is ready to generate."""
    avail, status, _ = get_flow_status()
    return avail

def get_downloads_folder():
    """Returns the user's Downloads/flow-auto directory."""
    if sys.platform == "win32":
        user_profile = os.environ.get("USERPROFILE", os.path.expanduser("~"))
        return os.path.join(user_profile, "Downloads", "flow-auto")
    return os.path.join(os.path.expanduser("~"), "Downloads", "flow-auto")

def _extract_downloaded_files(output_text, target_folder):
    """
    Finds downloaded files from stdout or by checking the target folder for recent files.
    """
    downloaded = []
    # Check lines in output for downloaded file indicators
    for line in output_text.splitlines():
        line = line.strip()
        if (line.endswith(".mp4") or line.endswith(".jpg") or line.endswith(".png")) and os.path.exists(line):
            downloaded.append(line)
        elif "downloaded:" in line.lower() or "saved:" in line.lower():
            parts = line.split()
            for p in parts:
                if os.path.exists(p) and p.endswith((".mp4", ".jpg", ".png")):
                    downloaded.append(p)
    
    if downloaded:
        return downloaded

    # Fallback: inspect target folder for files modified in the last 10 minutes
    if os.path.exists(target_folder):
        now = time.time()
        for f in os.listdir(target_folder):
            full_path = os.path.join(target_folder, f)
            if os.path.isfile(full_path) and full_path.endswith((".mp4", ".jpg", ".png")):
                if now - os.path.getmtime(full_path) < 600:
                    downloaded.append(full_path)
    
    downloaded.sort(key=lambda x: os.path.getmtime(x), reverse=True)
    return downloaded

def generate_flow_video(prompt, duration=5, model="veo-3.1-lite", out_path=None, timeout_min=12):
    """
    Generates a 9:16 vertical video using Google Flow / Veo.
    Returns: file path to generated .mp4, or None on failure.
    """
    logger.info(f"🎬 Initiating Google Flow Video (Veo) generation: '{prompt[:50]}...'")
    if not is_flow_available():
        logger.warning("⚠️ Google Flow extension is not connected or logged in.")
        return None

    folder = "flow-auto"
    target_dir = get_downloads_folder()
    os.makedirs(target_dir, exist_ok=True)
    
    # Snapshot existing files in target folder before run
    existing_files = set(os.listdir(target_dir)) if os.path.exists(target_dir) else set()

    cmd = [
        FLOW_AUTO_BIN,
        "generate", "create", "flow-text-to-video",
        "--prompt", prompt,
        "--aspect-ratio", "9:16",
        "--duration", str(duration),
        "--model", model,
        "--folder", folder,
        "--wait",
        "--wait-timeout", str(timeout_min)
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_min * 60,
            shell=True if sys.platform == "win32" else False
        )
        output = proc.stdout + "\n" + proc.stderr
        logger.info(f"Flow CLI Output: {output.strip()}")

        # Check for new file in target_dir
        new_files = []
        if os.path.exists(target_dir):
            for f in os.listdir(target_dir):
                if f not in existing_files and f.endswith(".mp4"):
                    new_files.append(os.path.join(target_dir, f))
        
        chosen_file = None
        if new_files:
            new_files.sort(key=lambda x: os.path.getmtime(x), reverse=True)
            chosen_file = new_files[0]
        else:
            extracted = _extract_downloaded_files(output, target_dir)
            mp4s = [f for f in extracted if f.endswith(".mp4")]
            if mp4s:
                chosen_file = mp4s[0]

        if chosen_file and os.path.exists(chosen_file) and os.path.getsize(chosen_file) > 50000:
            logger.info(f"✅ Successfully generated Flow video: {chosen_file}")
            if out_path:
                os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
                shutil.copy2(chosen_file, out_path)
                return out_path
            return chosen_file
        else:
            logger.warning("⚠️ Flow video generation finished but no valid .mp4 was retrieved.")
            return None
    except Exception as e:
        logger.error(f"❌ Error during Flow video generation: {e}")
        return None

def generate_flow_image(prompt, model="nano-banana-2", out_path=None, timeout_min=5):
    """
    Generates a 9:16 vertical image using Google Flow / Nano Banana.
    Returns: file path to generated .jpg, or None on failure.
    """
    logger.info(f"🎨 Initiating Google Flow Image (Nano Banana) generation: '{prompt[:50]}...'")
    if not is_flow_available():
        logger.warning("⚠️ Google Flow extension is not connected or logged in.")
        return None

    folder = "flow-auto"
    target_dir = get_downloads_folder()
    os.makedirs(target_dir, exist_ok=True)
    
    existing_files = set(os.listdir(target_dir)) if os.path.exists(target_dir) else set()

    cmd = [
        FLOW_AUTO_BIN,
        "generate", "create", "flow-text-to-image",
        "--prompt", prompt,
        "--aspect-ratio", "9:16",
        "--model", model,
        "--folder", folder,
        "--wait",
        "--wait-timeout", str(timeout_min)
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_min * 60,
            shell=True if sys.platform == "win32" else False
        )
        output = proc.stdout + "\n" + proc.stderr
        logger.info(f"Flow CLI Output: {output.strip()}")

        new_files = []
        if os.path.exists(target_dir):
            for f in os.listdir(target_dir):
                if f not in existing_files and f.endswith((".jpg", ".png", ".webp")):
                    new_files.append(os.path.join(target_dir, f))
        
        chosen_file = None
        if new_files:
            new_files.sort(key=lambda x: os.path.getmtime(x), reverse=True)
            chosen_file = new_files[0]
        else:
            extracted = _extract_downloaded_files(output, target_dir)
            imgs = [f for f in extracted if f.endswith((".jpg", ".png", ".webp"))]
            if imgs:
                chosen_file = imgs[0]

        if chosen_file and os.path.exists(chosen_file) and os.path.getsize(chosen_file) > 10000:
            logger.info(f"✅ Successfully generated Flow image: {chosen_file}")
            if out_path:
                os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
                shutil.copy2(chosen_file, out_path)
                return out_path
            return chosen_file
        else:
            logger.warning("⚠️ Flow image generation finished but no valid image was retrieved.")
            return None
    except Exception as e:
        logger.error(f"❌ Error during Flow image generation: {e}")
        return None

if __name__ == "__main__":
    avail, st, err = get_flow_status()
    print(f"Flow Available: {avail}")
    print(f"Status: {st}")
    if err:
        print(f"Error: {err}")
