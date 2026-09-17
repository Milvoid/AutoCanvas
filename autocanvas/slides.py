"""V1 slide comparison algorithms, independent of video, auth and persistence."""
from __future__ import annotations
import math
import re
import shutil
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Optional
from types import MappingProxyType
import cv2
import numpy as np


DEFAULT_THRESHOLDS = MappingProxyType({
    "sample_every": 5.0,
    "phash_threshold": 14,
    "dhash_threshold": 12,
    "absdiff_threshold": 9.0,
    "edge_threshold": 0.04,
    "ink_threshold": 0.35,
    "grid_max_threshold": 14.0,
    "grid_ratio_threshold": 0.08,
    "duplicate_phash_threshold": 12,
    "duplicate_dhash_threshold": 10,
    "duplicate_absdiff_threshold": 5.0,
    "duplicate_edge_threshold": 0.02,
    "duplicate_ink_threshold": 0.08,
    "duplicate_grid_max_threshold": 10.0,
    "duplicate_grid_ratio_threshold": 0.04,
})

@dataclass
class FrameSig:
    path: Path
    index: int
    seconds: float
    phash: int
    dhash: int
    small_gray: np.ndarray
    edge_gray: np.ndarray
    ink_mask: np.ndarray
    lap_var: float

def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "unknown"

def _phash(gray: np.ndarray) -> int:
    resized = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA)
    dct = cv2.dct(np.float32(resized))
    block = dct[:8, :8].copy()
    vals = block.flatten()[1:]
    med = float(np.median(vals))
    bits = block.flatten() > med
    value = 0
    for bit in bits:
        value = (value << 1) | int(bool(bit))
    return value

def _dhash(gray: np.ndarray) -> int:
    small = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    diff = small[:, 1:] > small[:, :-1]
    value = 0
    for bit in diff.flatten():
        value = (value << 1) | int(bool(bit))
    return value

def _hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()

def _find_slide_bbox(gray: np.ndarray) -> tuple[int, int, int, int]:
    mask = gray > 145
    mask = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    n_labels, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    if n_labels <= 1:
        return (0, 0, gray.shape[1], gray.shape[0])
    idx = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    x, y, w, h, _ = stats[idx]
    if w * h < gray.shape[0] * gray.shape[1] * 0.10:
        return (0, 0, gray.shape[1], gray.shape[0])
    return (int(x), int(y), int(x + w), int(y + h))

def _frame_signature(path: Path, index: int, sample_every: float) -> Optional[FrameSig]:
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        return None
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (320, 180), interpolation=cv2.INTER_AREA)
    x0, y0, x1, y1 = _find_slide_bbox(gray)
    slide = gray[y0:y1, x0:x1]
    h, w = slide.shape[:2]
    center = slide[int(h * 0.04) : int(h * 0.96), int(w * 0.04) : int(w * 0.96)]
    center_small = cv2.resize(center, (320, 180), interpolation=cv2.INTER_AREA)
    edge = cv2.Canny(center_small, 50, 150)
    ink_gray = cv2.resize(center, (480, 270), interpolation=cv2.INTER_AREA)
    ink = ink_gray < 120
    ink = cv2.dilate(ink.astype(np.uint8), np.ones((2, 2), np.uint8), iterations=1).astype(bool)
    lap_var = float(cv2.Laplacian(small, cv2.CV_64F).var())
    return FrameSig(
        path=path,
        index=index,
        seconds=(index - 1) * sample_every,
        phash=_phash(gray),
        dhash=_dhash(gray),
        small_gray=small,
        edge_gray=edge,
        ink_mask=ink,
        lap_var=lap_var,
    )

def _mean_absdiff(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.mean(cv2.absdiff(a, b)))

def _edge_diff(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.mean(cv2.absdiff(a, b)) / 255.0)

def _ink_diff(a: np.ndarray, b: np.ndarray) -> float:
    union = int(np.logical_or(a, b).sum())
    if union < 100:
        return 0.0
    xor = int(np.logical_xor(a, b).sum())
    return float(xor / union)

def _grid_diff(a: np.ndarray, b: np.ndarray, rows: int = 6, cols: int = 8) -> tuple[float, float]:
    diff = cv2.absdiff(a, b)
    h, w = diff.shape[:2]
    cell_scores = []
    for r in range(rows):
        y0 = int(h * r / rows)
        y1 = int(h * (r + 1) / rows)
        for c in range(cols):
            x0 = int(w * c / cols)
            x1 = int(w * (c + 1) / cols)
            cell_scores.append(float(np.mean(diff[y0:y1, x0:x1])))
    max_cell = max(cell_scores) if cell_scores else 0.0
    changed_ratio = sum(score >= 8.0 for score in cell_scores) / max(len(cell_scores), 1)
    return max_cell, changed_ratio

def _is_new_slide(sig: FrameSig, accepted: list[FrameSig], config) -> tuple[bool, dict[str, Any]]:
    if not accepted:
        return True, {"reason": "first_frame"}

    last = accepted[-1]
    metrics = {
        "phash_distance": _hamming(sig.phash, last.phash),
        "dhash_distance": _hamming(sig.dhash, last.dhash),
        "mean_absdiff": _mean_absdiff(sig.small_gray, last.small_gray),
        "edge_diff": _edge_diff(sig.edge_gray, last.edge_gray),
        "ink_diff": _ink_diff(sig.ink_mask, last.ink_mask),
    }
    grid_max, grid_ratio = _grid_diff(sig.small_gray, last.small_gray)
    metrics["grid_max_absdiff"] = grid_max
    metrics["grid_changed_ratio"] = grid_ratio

    changed_from_last = (
        metrics["mean_absdiff"] >= config["absdiff_threshold"]
        or metrics["edge_diff"] >= config["edge_threshold"]
        or metrics["ink_diff"] >= config["ink_threshold"]
        or metrics["grid_max_absdiff"] >= config["grid_max_threshold"]
        or metrics["grid_changed_ratio"] >= config["grid_ratio_threshold"]
        or (
            metrics["phash_distance"] >= config["phash_threshold"]
            and metrics["dhash_distance"] >= config["dhash_threshold"]
        )
    )
    if not changed_from_last:
        return False, {"reason": "similar_to_previous", **metrics}

    nearest = None
    nearest_score = math.inf
    for old in accepted:
        ph = _hamming(sig.phash, old.phash)
        dh = _hamming(sig.dhash, old.dhash)
        ad = _mean_absdiff(sig.small_gray, old.small_gray)
        ed = _edge_diff(sig.edge_gray, old.edge_gray)
        ink = _ink_diff(sig.ink_mask, old.ink_mask)
        gm, gr = _grid_diff(sig.small_gray, old.small_gray)
        score = ph + dh + ad / 2.0 + ed * 10.0 + ink * 20.0 + gm / 2.0 + gr * 10.0
        if score < nearest_score:
            nearest_score = score
            nearest = {
                "slide_index": old.index,
                "phash_distance": ph,
                "dhash_distance": dh,
                "mean_absdiff": ad,
                "edge_diff": ed,
                "ink_diff": ink,
                "grid_max_absdiff": gm,
                "grid_changed_ratio": gr,
            }

    duplicate = bool(
        nearest
        and nearest["mean_absdiff"] <= config["duplicate_absdiff_threshold"]
        and nearest["edge_diff"] <= config["duplicate_edge_threshold"]
        and nearest["ink_diff"] <= config["duplicate_ink_threshold"]
        and nearest["grid_max_absdiff"] <= config["duplicate_grid_max_threshold"]
        and nearest["grid_changed_ratio"] <= config["duplicate_grid_ratio_threshold"]
        and (
            nearest["phash_distance"] <= config["duplicate_phash_threshold"]
            or nearest["dhash_distance"] <= config["duplicate_dhash_threshold"]
        )
    )
    if duplicate:
        return False, {"reason": "duplicate_existing_slide", "nearest": nearest, **metrics}
    return True, {"reason": "changed", "nearest": nearest, **metrics}

def _hhmmss(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def _extract_representatives(
    video_id: str,
    frames_dir: Path,
    out_dir: Path,
    sample_every: float,
    config,
) -> list[dict[str, Any]]:
    accepted: list[FrameSig] = []
    metadata: list[dict[str, Any]] = []
    meta_by_frame_index: dict[int, dict[str, Any]] = {}
    safe_video_id = _safe_id(video_id)
    for old_image in out_dir.glob(f"{safe_video_id}_slide_*.jpg"):
        old_image.unlink()

    for index, frame_path in enumerate(sorted(frames_dir.glob("*.jpg")), start=1):
        sig = _frame_signature(frame_path, index, sample_every)
        if sig is None:
            continue
        is_new, metrics = _is_new_slide(sig, accepted, config)
        if not is_new:
            matched_meta = None
            if metrics.get("reason") == "similar_to_previous" and accepted:
                matched_meta = meta_by_frame_index.get(accepted[-1].index)
            elif metrics.get("reason") == "duplicate_existing_slide":
                nearest = metrics.get("nearest") or {}
                matched_meta = meta_by_frame_index.get(nearest.get("slide_index"))
            if matched_meta is not None:
                matched_meta["matched_sample_frames"].append(index)
                matched_meta["last_seen_seconds"] = sig.seconds
                matched_meta["last_seen_hhmmss"] = _hhmmss(sig.seconds)
                matched_meta["match_count"] = len(matched_meta["matched_sample_frames"])
            continue

        accepted.append(sig)
        slide_number = len(accepted)
        image_name = f"{safe_video_id}_slide_{slide_number:03d}_{int(sig.seconds):05d}s.jpg"
        slide_path = out_dir / image_name
        shutil.copy2(sig.path, slide_path)
        item = {
            "video_id": video_id,
            "slide_number": slide_number,
            "time_seconds": sig.seconds,
            "time_hhmmss": _hhmmss(sig.seconds),
            "last_seen_seconds": sig.seconds,
            "last_seen_hhmmss": _hhmmss(sig.seconds),
            "frame": sig.path,
            "image": slide_path,
            "matched_sample_frames": [index],
            "match_count": 1,
            "laplacian_variance": sig.lap_var,
            "phash": f"{sig.phash:016x}",
            "dhash": f"{sig.dhash:016x}",
            "decision": metrics,
        }
        metadata.append(item)
        meta_by_frame_index[sig.index] = item
    return metadata

def _contact_sheet(slides: list[dict[str, Any]], out_path: Path, thumb_width: int = 360) -> None:
    if not slides:
        return
    thumbs = []
    for slide in slides:
        img = cv2.imread(str(slide["image"]), cv2.IMREAD_COLOR)
        if img is None:
            continue
        h, w = img.shape[:2]
        scale = thumb_width / max(w, 1)
        thumb = cv2.resize(img, (thumb_width, max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
        label_h = 34
        canvas = np.full((thumb.shape[0] + label_h, thumb.shape[1], 3), 255, dtype=np.uint8)
        canvas[label_h:, :] = thumb
        cv2.putText(
            canvas,
            f"{slide['slide_number']:03d} {slide['time_hhmmss']}",
            (10, 23),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (0, 0, 0),
            1,
            cv2.LINE_AA,
        )
        thumbs.append(canvas)

    cols = min(4, len(thumbs))
    rows = math.ceil(len(thumbs) / cols)
    cell_h = max(t.shape[0] for t in thumbs)
    sheet = np.full((rows * cell_h, cols * thumb_width, 3), 245, dtype=np.uint8)
    for i, thumb in enumerate(thumbs):
        r, c = divmod(i, cols)
        y = r * cell_h
        x = c * thumb_width
        sheet[y : y + thumb.shape[0], x : x + thumb.shape[1]] = thumb
    cv2.imwrite(str(out_path), sheet)


def extract(frames_dir: Path, output_dir: Path, *, sample_every=5.0, thresholds=None):
    """Process evenly sampled JPG frames; return slide metadata, no course knowledge."""
    output_dir.mkdir(parents=True, exist_ok=True)
    config = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        config.update(thresholds)
    slides = _extract_representatives("frame", frames_dir, output_dir, sample_every, config)
    _contact_sheet(slides, output_dir / "contact_sheet.jpg")
    return slides
