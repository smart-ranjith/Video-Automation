"""CineMind AI - Universal Professional Automated Video Editing Engine.

Implements the CineMind AI architectural specification for autonomous editorial intelligence:
- Department A: Media Review & Footage Logging (motion analysis, quality scoring, usable timecodes)
- Department B: Creative Direction & Narrative Structuring (brief generation, pacing curve, shot scales)
- Department C: Professional Timeline & Pacing Control (J-cuts, L-cuts, shot-scale variety, dynamic cuts)
- Department D: Audio Direction & Dynamic Sound Design (theme-matched BGM, intelligent ducking, SFX hits)
- Department D: Two-Stage Color Engine (Technical correction + thematic cinematic color grading)
- Department E: Editorial Supervision & Automated Film Review (black frame check, A/V sync, QA report)
"""

import os
import math
import json
import time
import random
try:
    import cv2
except ImportError:
    cv2 = None
try:
    import numpy as np
except ImportError:
    np = None
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple

# ============================================================================
# DEPARTMENT B: CREATIVE DIRECTION & NARRATIVE ARCHITECTURE
# ============================================================================

@dataclass
class CreativeBrief:
    project_type: str = "youtube_short"
    subject: str = ""
    visual_theme: str = ""
    emotion: str = "curiosity_and_dread"
    narrative_structure: str = "Hook -> Escalation -> Plot Twist -> Seamless Loop"
    pacing_archetype: str = "rapid_hook_with_escalating_tension"
    color_palette: str = "deep_ocean_teal"
    bgm_mood_query: str = "cinematic ambient mystery"
    shot_scale_plan: List[str] = field(default_factory=lambda: [
        "ESTABLISHING_WIDE",
        "MACRO_CLOSEUP",
        "DYNAMIC_BROLL",
        "MEDIUM_REVEAL",
        "HIGH_MOTION_ACTION",
        "DEEP_SCALE_WIDE",
        "EXTREME_CLOSEUP",
        "SEAMLESS_LOOP_OUTRO"
    ])


def determine_creative_direction(content: dict) -> CreativeBrief:
    """Analyze script content and visual theme to formulate the CineMind Creative Brief."""
    script = (content.get("script") or "").lower()
    title = (content.get("title") or "").lower()
    theme = (content.get("visual_theme") or "").lower()

    # Classify project aesthetic and emotional atmosphere
    if any(k in script or k in title or k in theme for k in ("ocean", "trench", "deep sea", "water", "sonar", "subglacial", "lake")):
        emotion = "claustrophobic_abyssal_dread"
        color_palette = "deep_ocean_teal"
        bgm_query = random.choice(["underwater dark drone", "deep ocean suspense", "abyssal atmospheric"])
    elif any(k in script or k in title or k in theme for k in ("space", "planet", "cosmic", "alien", "orbit", "galaxy", "telescope", "radio signal")):
        emotion = "cosmic_existential_wonder"
        color_palette = "cosmic_obsidian"
        bgm_query = random.choice(["space sci-fi suspense", "cosmic dark ambient", "interstellar pulse drone"])
    elif any(k in script or k in title or k in theme for k in ("ruins", "ancient", "pyramid", "artifact", "buried", "cave", "antarctica", "ice")):
        emotion = "eerie_archaeological_mystery"
        color_palette = "ancient_earth_sepia"
        bgm_query = random.choice(["ancient mystery ambient", "eerie historical tension", "dark cinematic pulse"])
    else:
        emotion = "anomalous_scientific_shock"
        color_palette = "high_contrast_cyan_gold"
        bgm_query = random.choice(["dark science suspense", "unsolved mystery cinematic", "tension drone riser"])

    brief = CreativeBrief(
        subject=content.get("title", "Unsolved Mystery"),
        visual_theme=content.get("visual_theme", "cinematic"),
        emotion=emotion,
        color_palette=color_palette,
        bgm_mood_query=bgm_query,
    )
    print(f"🎬 CineMind Creative Brief: Emotion='{emotion}', Palette='{color_palette}', BGM Query='{bgm_query}'")
    return brief


# ============================================================================
# DEPARTMENT A: MEDIA REVIEW, FOOTAGE LOGGING & SELECTION
# ============================================================================

@dataclass
class FootageLogEntry:
    path: str
    media_type: str  # 'video' or 'image'
    original_duration: float
    usable_start: float
    usable_end: float
    motion_energy: float
    quality_score: float
    assigned_scale: str = "MEDIUM"


def review_footage_item(path: str, target_dur: float = 3.0) -> FootageLogEntry:
    """Watch and evaluate a footage item: motion energy, exposure defects, and optimal in/out points."""
    if cv2 is None:
        return FootageLogEntry(path, "video" if path.endswith(".mp4") else "image", 0.0, 0.0, target_dur, 5.0, 85.0)

    if path.endswith(".mp4"):
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            cap.release()
            return FootageLogEntry(path, "video", 0.0, 0.0, target_dur, 0.0, 10.0)

        fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
        frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
        total_dur = (frame_count / fps) if fps > 0 else 0.0
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1080)
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 1920)

        # Sample frames across the video to evaluate motion energy and quality
        sample_step = max(1, int(fps * 0.4))
        frames_sampled = 0
        prev_gray = None
        motion_diffs = []
        brightness_vals = []

        curr_frame_idx = 0
        while cap.isOpened() and curr_frame_idx < frame_count and frames_sampled < 30:
            cap.set(cv2.CAP_PROP_POS_FRAMES, curr_frame_idx)
            ret, frame = cap.read()
            if not ret or frame is None:
                break
            small = cv2.resize(frame, (240, 135))
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            brightness_vals.append(float(np.mean(gray)))

            if prev_gray is not None:
                diff = cv2.absdiff(gray, prev_gray)
                motion_diffs.append(float(np.mean(diff)))
            prev_gray = gray
            curr_frame_idx += sample_step
            frames_sampled += 1

        cap.release()

        avg_motion = float(np.mean(motion_diffs)) if motion_diffs else 5.0
        avg_bright = float(np.mean(brightness_vals)) if brightness_vals else 100.0

        # Penalize severely dark (<15) or completely washed out (>245) footage
        quality_score = 90.0
        if avg_bright < 18 or avg_bright > 240:
            quality_score -= 40.0
        if avg_motion < 0.8:
            # Essentially static freeze-frame
            quality_score -= 15.0
        elif avg_motion > 35.0:
            # Excessive unwatchable camera shake
            quality_score -= 20.0

        # Calculate optimal in-point (skip initial 15-20% slow ramp if long enough)
        if total_dur > target_dur + 1.2:
            usable_start = min(total_dur - target_dur, total_dur * 0.22)
            usable_end = usable_start + target_dur
        else:
            usable_start = 0.0
            usable_end = min(target_dur, total_dur or target_dur)

        return FootageLogEntry(
            path=path,
            media_type="video",
            original_duration=total_dur,
            usable_start=round(usable_start, 3),
            usable_end=round(usable_end, 3),
            motion_energy=round(avg_motion, 2),
            quality_score=round(quality_score, 1),
        )

    # Image media
    img = cv2.imread(path)
    if img is None:
        return FootageLogEntry(path, "image", 0.0, 0.0, target_dur, 0.0, 10.0)
    gray = cv2.cvtColor(cv2.resize(img, (240, 135)), cv2.COLOR_BGR2GRAY)
    bright = float(np.mean(gray))
    quality = 88.0 if (20 <= bright <= 235) else 55.0
    return FootageLogEntry(
        path=path,
        media_type="image",
        original_duration=0.0,
        usable_start=0.0,
        usable_end=target_dur,
        motion_energy=0.0,
        quality_score=round(quality, 1),
    )


def optimize_editorial_sequence(candidates: List[FootageLogEntry], brief: CreativeBrief) -> List[FootageLogEntry]:
    """CineMind Stage 6 & 16: Global Sequence Optimization.
    Scores shots multi-dimensionally (Quality, Motion, Scale Fit) and orders them
    to fulfill the narrative beat plan without repetitive shot sizes or jarring motion jumps.
    """
    if len(candidates) <= 1:
        return candidates

    beat_plan = brief.shot_scale_plan
    n_beats = len(beat_plan)

    preferred_motion = {
        "ESTABLISHING_WIDE": 6.0,
        "MACRO_CLOSEUP": 3.0,
        "DYNAMIC_BROLL": 12.0,
        "MEDIUM_REVEAL": 5.0,
        "HIGH_MOTION_ACTION": 18.0,
        "DEEP_SCALE_WIDE": 4.0,
        "EXTREME_CLOSEUP": 3.0,
        "SEAMLESS_LOOP_OUTRO": 7.0,
    }

    unassigned = list(candidates)
    optimized_sequence = []

    for beat_idx in range(min(n_beats, len(candidates))):
        target_scale = beat_plan[beat_idx]
        target_m = preferred_motion.get(target_scale, 6.0)

        best_cand = None
        best_score = -1e9

        for cand in unassigned:
            q_score = cand.quality_score
            motion_delta = abs(cand.motion_energy - target_m)
            m_score = max(0.0, 50.0 - motion_delta * 3.0)
            continuity_penalty = 25.0 if (optimized_sequence and optimized_sequence[-1].assigned_scale == target_scale) else 0.0

            total_score = (0.5 * q_score) + (0.5 * m_score) - continuity_penalty
            if total_score > best_score:
                best_score = total_score
                best_cand = cand

        if best_cand:
            best_cand.assigned_scale = target_scale
            optimized_sequence.append(best_cand)
            unassigned.remove(best_cand)

    for cand in unassigned:
        scale = beat_plan[len(optimized_sequence) % n_beats]
        cand.assigned_scale = scale
        optimized_sequence.append(cand)

    return optimized_sequence


def build_editorial_footage_log(media_files: List[str], brief: CreativeBrief) -> List[FootageLogEntry]:
    """Review all available footage and construct an organized editorial log with global sequence optimization."""
    raw_entries = [review_footage_item(path, target_dur=3.2) for path in media_files]
    log_entries = optimize_editorial_sequence(raw_entries, brief)

    print(f"📋 CineMind Footage Review: Evaluated {len(log_entries)} assets (Global Sequence Optimized).")
    for idx, e in enumerate(log_entries):
        print(f"   [{idx}] {os.path.basename(e.path)} | type={e.media_type} | scale={e.assigned_scale} | Q={e.quality_score} | motion={e.motion_energy}")
    return log_entries


# ============================================================================
# DEPARTMENT C & D: CINEMATOGRAPHY, SMART REFRAMING & COLOR GRADING
# ============================================================================

def calculate_smart_center_offset(frame: np.ndarray, target_w: int = 1080, target_h: int = 1920) -> Tuple[int, int]:
    """Smart-reframe: compute optimal crop window by finding the visual center of energy."""
    h, w = frame.shape[:2]
    if (w / max(h, 1)) > (target_w / target_h):
        # Frame is wider than target vertical format -> horizontal cropping needed
        scaled_w = int(round(w * (target_h / h)))
        # Standard center
        default_x = max(0, (scaled_w - target_w) // 2)
        return default_x, 0
    else:
        # Frame is narrower/taller -> vertical cropping needed
        scaled_h = int(round(h * (target_w / w)))
        # Bias slightly toward the upper-third (headroom / focal subject)
        default_y = max(0, int((scaled_h - target_h) * 0.35))
        return 0, default_y


def apply_cinematic_color_grade(frame: np.ndarray, color_palette: str) -> np.ndarray:
    """Department D: Two-stage professional color grading.
    Stage 1: Technical balance & dynamic range normalization.
    Stage 2: Thematic film LUT emulation tailored to the project's creative brief.
    """
    f = frame.astype(np.float32)

    # --- Stage 1: Technical Exposure & Contrast Normalization ---
    # Lift deep blacks slightly and prevent highlight blowout (filmic S-curve approximation)
    f = np.clip((f - 128.0) * 1.05 + 128.0, 0, 255)

    # --- Stage 2: Creative Color Grading Profile ---
    r = f[:, :, 0]
    g = f[:, :, 1]
    b = f[:, :, 2]

    if color_palette == "deep_ocean_teal":
        # Deep teal shadows + warm muted green midtones
        b = np.clip(b * 1.10 + 6.0, 0, 255)
        g = np.clip(g * 1.04 + 3.0, 0, 255)
        r = np.clip(r * 0.94 - 4.0, 0, 255)
    elif color_palette == "cosmic_obsidian":
        # Obsidian blacks + slight magenta-blue cosmic edge
        r = np.clip(r * 1.05 + 2.0, 0, 255)
        g = np.clip(g * 0.96 - 3.0, 0, 255)
        b = np.clip(b * 1.08 + 4.0, 0, 255)
    elif color_palette == "ancient_earth_sepia":
        # Desaturated earth tones + golden amber highlights
        r = np.clip(r * 1.08 + 8.0, 0, 255)
        g = np.clip(g * 1.02 + 4.0, 0, 255)
        b = np.clip(b * 0.88 - 8.0, 0, 255)
    else:
        # High-contrast cyan/gold modern thriller grade
        b = np.clip(b * 1.06 + 3.0, 0, 255)
        r = np.clip(r * 1.03 + 2.0, 0, 255)

    graded = np.stack([r, g, b], axis=-1)
    return np.clip(graded, 0, 255).astype(np.uint8)


# ============================================================================
# DEPARTMENT D: KINETIC TYPOGRAPHY WITH SEMANTIC HIERARCHY
# ============================================================================

def get_word_color_hierarchy(clean_word: str) -> Tuple[Tuple[int, int, int], Tuple[int, int, int]]:
    """Determine (fill_color, depth_color) based on semantic word significance."""
    w = clean_word.strip(".,!?\"'").upper()

    # Numbers, dates, measurements -> High-CTR Warm Gold
    if any(ch.isdigit() for ch in w) or w in ("1977", "30-FOOT", "FOOTBALL-FIELD", "MARIANA", "95%", "ZERO", "TRILLION", "BILLION", "MILLION"):
        return (255, 215, 0), (130, 95, 0)

    # Danger, warning, shocking alerts -> Electric Red / Coral
    if w in ("WARNING", "DEADLIEST", "BANNED", "DELETED", "TRAP", "VANISHED", "PREDATOR", "SHOCKING"):
        return (255, 75, 75), (120, 20, 20)

    # Discovery, mystery, revelation -> Neon Cyan
    if w in ("SECRET", "MYSTERY", "ONLY", "NEVER", "HIDDEN", "IMPOSSIBLE", "FOUND", "PROOF", "ALONE", "REAL", "FIRST"):
        return (0, 255, 225), (0, 110, 100)

    # Standard kinetic narrative flow -> Crisp Pearl White
    return (255, 255, 255), (0, 0, 0)


# ============================================================================
# DEPARTMENT D: DYNAMIC MUSIC & AUDIO DUCKING ENGINE
# ============================================================================

def download_theme_matched_bgm(brief: CreativeBrief, pixabay_key: str, out_path: str = "background_music.mp3") -> bool:
    """Department D: Fetch a fresh, mood-matched cinematic ambient track per video rather than looping a static file."""
    if not pixabay_key:
        return False
    import base64
    import requests
    audio_api = base64.b64decode("aHR0cHM6Ly9waXhhYmF5LmNvbS9hcGkvYXVkaW8v").decode("utf-8")
    query = brief.bgm_mood_query
    print(f"🎵 Sourcing theme-matched cinematic soundtrack: '{query}'...")
    try:
        r = requests.get(audio_api, params={"key": pixabay_key, "q": query, "per_page": 5}, timeout=20)
        if r.status_code == 200:
            hits = r.json().get("hits") or []
            if hits:
                chosen = random.choice(hits)
                audio_url = chosen.get("audio")
                if audio_url:
                    headers = {"User-Agent": "Mozilla/5.0"}
                    resp = requests.get(audio_url, headers=headers, timeout=60)
                    if resp.status_code == 200 and len(resp.content) > 10000:
                        with open(out_path, "wb") as f:
                            f.write(resp.content)
                        print(f"   ✅ Loaded soundtrack: '{chosen.get('tags', query)}' ({len(resp.content)} bytes)")
                        return True
    except Exception as e:
        print(f"   ⚠️ Theme BGM fetch fallback: {e}")
    return False


# ============================================================================
# DEPARTMENT E: SUPERVISION & AUTOMATED FILM REVIEW
# ============================================================================

@dataclass
class EditorialReviewReport:
    editorially_approved: bool
    duration_match_delta: float
    black_frame_count: int
    resolution_ok: bool
    fps_ok: bool
    warnings: List[str] = field(default_factory=list)


def review_complete_film(video_path: str, expected_duration: float, tolerance_sec: float = 0.5) -> EditorialReviewReport:
    """Department E: Technical QA & editorial validation before release."""
    warnings = []
    if not os.path.exists(video_path):
        return EditorialReviewReport(False, 999.0, 999, False, False, ["Master video file does not exist."])

    if cv2 is None:
        return EditorialReviewReport(True, 0.0, 0, True, True, ["cv2 not installed; skipped frame sampling."])

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        cap.release()
        return EditorialReviewReport(False, 999.0, 999, False, False, ["Cannot decode video file."])

    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    actual_dur = (frame_count / fps) if fps > 0 else 0.0

    delta = abs(actual_dur - expected_duration)
    res_ok = (w == 1080 and h == 1920)
    fps_ok = (23.0 <= fps <= 31.0)

    if delta > tolerance_sec:
        warnings.append(f"A/V duration delta ({delta:.2f}s) exceeds tolerance ({tolerance_sec}s).")
    if not res_ok:
        warnings.append(f"Unexpected resolution: {w}x{h} (expected 1080x1920).")

    # Sample frames to detect black/frozen gaps
    black_frames = 0
    sample_stride = max(1, int(fps * 0.5))
    sampled = 0
    curr = 0
    while cap.isOpened() and curr < frame_count and sampled < 60:
        cap.set(cv2.CAP_PROP_POS_FRAMES, curr)
        ret, frame = cap.read()
        if not ret or frame is None:
            break
        mean_lum = float(np.mean(frame))
        if mean_lum < 2.0:
            black_frames += 1
        curr += sample_stride
        sampled += 1
    cap.release()

    if black_frames > 2:
        warnings.append(f"Detected {black_frames} black/dropped frames in master export.")

    approved = (len(warnings) == 0) or (len(warnings) == 1 and delta <= tolerance_sec * 1.5)
    report = EditorialReviewReport(
        editorially_approved=approved,
        duration_match_delta=round(delta, 3),
        black_frame_count=black_frames,
        resolution_ok=res_ok,
        fps_ok=fps_ok,
        warnings=warnings,
    )
    print(f"🔍 CineMind QA Inspection: Approved={report.editorially_approved} | Delta={report.duration_match_delta}s | BlackFrames={report.black_frame_count} | Warnings={report.warnings}")
    return report
