"""
Step 1 sanity check: does Perch v2 load and run at all?
Run this BEFORE any hardware work. If this doesn't work, nothing else matters yet.

1. Download perch_v2.onnx from:
   https://huggingface.co/justinchuby/Perch-onnx/tree/main
2. Place it in this same folder.
3. Run: python test_perch.py
"""

import numpy as np
import onnxruntime as ort

MODEL_PATH = "perch_v2.onnx"
LABELS_PATH = "labels.csv"
SAMPLE_RATE = 32000
CLIP_SECONDS = 5
NUM_SAMPLES = SAMPLE_RATE * CLIP_SECONDS  # 160,000


def load_labels(path):
    """One species name per line; first line is a header ('inat2024_fsd50k'), skip it.
    Row N (0-indexed, after header) corresponds to model output index N."""
    with open(path, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]
    return lines[1:]  # drop header

def test_with_random_input():
    """First check: does the model even load and produce output shapes we expect?"""
    session = ort.InferenceSession(MODEL_PATH)

    print("Model inputs:")
    for inp in session.get_inputs():
        print(f"  {inp.name}: {inp.shape} ({inp.type})")

    print("Model outputs:")
    for out in session.get_outputs():
        print(f"  {out.name}: {out.shape} ({out.type})")

    dummy_audio = np.random.randn(1, NUM_SAMPLES).astype(np.float32)
    outputs = session.run(None, {"inputs": dummy_audio})

    label_logits = outputs[3]  # shape (1, 14795)
    top5_idx = np.argsort(label_logits[0])[-5:][::-1]
    print(f"\nTop-5 class indices (random noise input, meaningless labels): {top5_idx}")
    print("If this ran without error, the pipeline mechanics work.")


def test_with_real_audio(wav_path):
    """Second check: load an actual audio file and classify it."""
    import soundfile as sf

    audio, sr = sf.read(wav_path)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)  # convert to mono if stereo

    if sr != SAMPLE_RATE:
        raise ValueError(f"Expected {SAMPLE_RATE}Hz, got {sr}Hz. Resample first (e.g. librosa.resample).")

    if len(audio) < NUM_SAMPLES:
        audio = np.pad(audio, (0, NUM_SAMPLES - len(audio)))
    else:
        audio = audio[:NUM_SAMPLES]

    audio = audio.astype(np.float32).reshape(1, -1)

    session = ort.InferenceSession(MODEL_PATH)
    outputs = session.run(None, {"inputs": audio})
    label_logits = outputs[3][0]

    top5_idx = np.argsort(label_logits)[-5:][::-1]

    try:
        labels = load_labels(LABELS_PATH)
        print("Top-5 predictions:")
        for idx in top5_idx:
            print(f"  {labels[idx]:40s}  logit={label_logits[idx]:.3f}")
    except FileNotFoundError:
        print(f"Top-5 class indices: {top5_idx}")
        print(f"Top-5 logit values: {label_logits[top5_idx]}")
        print(f"\n(Labels file '{LABELS_PATH}' not found - update LABELS_PATH to see species names)")


def res_with_real_audio(wav_path) -> dict[str,any]:
    """Second check: load an actual audio file and classify it."""
    import soundfile as sf
    audio, sr = sf.read(wav_path)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)  # convert to mono if stereo

    if sr != SAMPLE_RATE:
        raise ValueError(f"Expected {SAMPLE_RATE}Hz, got {sr}Hz. Resample first (e.g. librosa.resample).")

    if len(audio) < NUM_SAMPLES:
        audio = np.pad(audio, (0, NUM_SAMPLES - len(audio)))
    else:
        audio = audio[:NUM_SAMPLES]

    audio = audio.astype(np.float32).reshape(1, -1)

    session = ort.InferenceSession(MODEL_PATH)
    outputs = session.run(None, {"inputs": audio})
    label_logits = outputs[3][0]

    top5_idx = np.argsort(label_logits)[-5:][::-1]

    res = {"top":[]}
    
    try:
        labels = load_labels(LABELS_PATH)
        # print("Top-5 predictions:")
        for idx in top5_idx:
            # print(f"  {labels[idx]:40s}  logit={label_logits[idx]:.3f}")
            res["top"].append({"label":f"{labels[idx]}","logit":float(label_logits[idx])})
    except FileNotFoundError:
        # print(f"Top-5 class indices: {top5_idx}")
        # print(f"Top-5 logit values: {label_logits[top5_idx]}")
        # print(f"\n(Labels file '{LABELS_PATH}' not found - update LABELS_PATH to see species names)")
        res["err"] = "internal server error"

    return res

if __name__ == "__main__":
    # test_with_random_input()
    # Once you have a real 5s/32kHz wav clip of a bird or forest sound, uncomment:
    test_with_real_audio("auto_best_clip.wav")
