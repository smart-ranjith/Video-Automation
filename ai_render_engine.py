import cv2
import numpy as np
from moviepy.editor import ImageClip, VideoClip, ImageSequenceClip

class ProceduralAIVideoGenerator:
    """Advanced Procedural AI Motion & Spatial Warping Engine"""

    def __init__(self, image_path, duration, fps=30):
        self.image = cv2.imread(image_path)
        if self.image is None:
            raise FileNotFoundError(f"Image at {image_path} could not be loaded.")

        self.image = cv2.resize(self.image, (1080, 1920), interpolation=cv2.INTER_CUBIC) 
        self.h, self.w, _ = self.image.shape
        self.duration = duration
        self.fps = fps

        # Pre-compute spatial meshgrid for vector displacement
        self.grid_x, self.grid_y = np.meshgrid(np.arange(self.w), np.arange(self.h))
        self.grid_x = self.grid_x.astype(np.float32)
        self.grid_y = self.grid_y.astype(np.float32)

    def render_frame(self, t):
        """Generates a single dynamically synthesized frame at time t."""
        progress = t / self.duration
        
        # 1. Calculate 3D Perspective Rotation Matrix
        angle_x = np.sin(progress * np.pi) * 4.0  # Pitch oscillation
        angle_y = np.cos(progress * np.pi) * 3.0  # Yaw oscillation
        scale = 1.0 + (0.08 * progress)          # Continuous zoom
        
        cx, cy = self.w / 2.0, self.h / 2.0
        
        # 2D Affine Approximator for 3D Camera Rotation
        M = cv2.getRotationMatrix2D((cx, cy), angle_x, scale)
        M[0, 2] += angle_y * 5  # X translation
        M[1, 2] += angle_x * 3  # Y translation
        
        transformed = cv2.warpAffine(
            self.image, M, (self.w, self.h), 
            flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT
        )

        # 2. Apply Fluid Vector-Field Warp (Organic Motion)
        warp_amplitude = 6.0 * np.sin(progress * np.pi)
        wavelength = 150.0
        
        map_x = self.grid_x + warp_amplitude * np.sin(self.grid_y / wavelength + progress * 2.0 * np.pi)
        map_y = self.grid_y + warp_amplitude * np.cos(self.grid_x / wavelength + progress * 2.0 * np.pi)
        
        warped = cv2.remap(
            transformed, map_x.astype(np.float32), map_y.astype(np.float32), 
            interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT
        )

        # 3. Apply Radial Chromatic Aberration
        shift = int(2 + 4 * np.sin(progress * np.pi))
        b, g, r = cv2.split(warped)
        
        r_shifted = np.roll(r, shift, axis=1)
        b_shifted = np.roll(b, -shift, axis=1)
        
        output = cv2.merge([b_shifted, g, r_shifted])
        
        # Convert BGR (OpenCV) to RGB (MoviePy)
        return cv2.cvtColor(output, cv2.COLOR_BGR2RGB)

    def to_clip(self):
        """Wraps the renderer into a MoviePy VideoClip object."""
        return VideoClip(self.render_frame, duration=self.duration)


class AIVideoEngine:
    """Internal Procedural Video Synthesizer & Effect Suite"""
    
    @staticmethod
    def generate_procedural_glitch(duration=0.2, fps=30, resolution=(1080, 1920)):
        """Mathematically generates a digital transition using raw NumPy arrays."""
        num_frames = int(duration * fps)
        # Synthesizes pure RGB static noise frames from thin air
        frames = [
            np.random.randint(0, 255, (resolution[1], resolution[0], 3), dtype=np.uint8) 
            for _ in range(num_frames)
        ]
        return ImageSequenceClip(frames, fps=fps)

    @staticmethod
    def cinematic_camera_push(img_path, duration):
        """Simulates an AI-generated 3D camera tracking shot."""
        clip = ImageClip(img_path).set_duration(duration)
        return clip.resize(lambda t: 1 + (0.04 * t)).set_position(('center', 'center'))

    @staticmethod
    def apply_ai_flicker(clip):
        """Simulates AI generation artifacts by procedurally dimming random frames."""
        def filter_flicker(get_frame, t):
            frame = get_frame(t)
            if np.random.random() > 0.90:
                return (frame * 0.8).astype(np.uint8)
            return frame
        return clip.fl(filter_flicker)
