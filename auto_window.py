"""
Automatically find the best 5-second window in a longer audio file
by locating the highest-energy segment - no manual waveform inspection needed.

This mirrors the logic your ESP32 will eventually run in real time:
compute short-term energy, and only "activate" (transmit/classify)
when energy crosses a threshold / a peak is found.
"""

import librosa
import numpy as np
import soundfile as sf
import logging

logger = logging.getLogger(__name__)

SAMPLE_RATE = 32000
CLIP_SAMPLES = 160000  # 5 seconds at 32kHz


def find_best_window(y, sr, clip_samples=CLIP_SAMPLES, hop_seconds=0.5):
    """
    Slide a short analysis window across the whole recording, compute RMS
    energy for each position, and return the clip_samples-long segment
    centered on the highest-energy point.

    hop_seconds: how finely to step through the file when searching (smaller
    = more precise but slower). 0.5s is a reasonable default.
    """
    hop_samples = int(hop_seconds * sr)
    analysis_win = int(0.25 * sr)  # 250ms analysis window for energy calc

    best_energy = -1
    best_center = clip_samples // 2

    for start in range(0, len(y) - analysis_win, hop_samples):
        segment = y[start : start + analysis_win]
        energy = np.sqrt(np.mean(segment ** 2))  # RMS energy
        if energy > best_energy:
            best_energy = energy
            best_center = start + analysis_win // 2

    # Center the final 5s clip on the peak, clamped to valid range
    half = clip_samples // 2
    clip_start = max(0, best_center - half)
    clip_end = clip_start + clip_samples

    if clip_end > len(y):
        clip_end = len(y)
        clip_start = max(0, clip_end - clip_samples)

    clip = y[clip_start:clip_end]

    # Pad if the file itself is shorter than 5s
    if len(clip) < clip_samples:
        clip = np.pad(clip, (0, clip_samples - len(clip)))

    return clip, clip_start / sr, best_energy, best_center / sr


def auto_extract_best_clip(input_path: str, output_path : str = "auto_best_clip.wav"):
    y, sr = librosa.load(input_path, sr=SAMPLE_RATE, mono=True)
    logger.info(f"Loaded {input_path}: {len(y)/sr:.1f}s total")

    clip, start_time, peak_energy, peak_time = find_best_window(y, sr)

    sf.write(output_path, clip, sr)
    logger.info(f"Peak detected at {peak_time:.1f}s (RMS energy: {peak_energy:.4f})")
    logger.info(f"Clip window: {start_time:.1f}s to {start_time+5:.1f}s")
    logger.info(f"Saved {clip_samples_info(clip, sr)} to {output_path}")

    return output_path


def clip_samples_info(clip, sr):
    return f"{len(clip)} samples ({len(clip)/sr:.2f}s)"


if __name__ == "__main__":
    # Update this to your actual filename
    input_file = "XC442900 - Marquesan Imperial Pigeon - Ducula galeata.mp3"
    auto_extract_best_clip(input_file, "auto_best_clip.wav")
