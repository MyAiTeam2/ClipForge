"""Audio dynamics and energy analysis engine.
Extracts volume curves, peak moments, and excitement/laughter indicators
to power intelligent clip candidate selection.
"""
from __future__ import annotations
import math
import wave
from pathlib import Path
from typing import Optional, List, Dict, Any
import numpy as np


class AudioEnergyProfile:
    """Stores precomputed rolling audio dynamics for fast range scoring."""

    def __init__(
        self,
        times: np.ndarray,
        rms: np.ndarray,
        peaks: np.ndarray,
        bursts: np.ndarray,
        duration: float,
    ):
        self.times = times
        self.rms = rms
        self.peaks = peaks
        self.bursts = bursts
        self.duration = duration
        
        # Precompute summary statistics
        self.mean_rms = float(np.mean(rms)) if len(rms) > 0 else 0.0
        self.p90_rms = float(np.percentile(rms, 90)) if len(rms) > 0 else 0.0
        self.max_peak = float(np.max(peaks)) if len(peaks) > 0 else 1.0

    def score_range(self, start: float, end: float) -> Dict[str, float]:
        """Score the audio dynamics of a specific [start, end] window.
        Returns a dict with avg_energy, peak_energy, laughter_burst, and composite_score (0.0-1.0).
        """
        if len(self.times) == 0 or end <= start:
            return {
                "avg_energy": 0.5,
                "peak_energy": 0.5,
                "laughter_burst": 0.0,
                "composite_score": 0.5,
            }

        mask = (self.times >= start) & (self.times <= end)
        if not np.any(mask):
            return {
                "avg_energy": 0.5,
                "peak_energy": 0.5,
                "laughter_burst": 0.0,
                "composite_score": 0.5,
            }

        window_rms = self.rms[mask]
        window_peaks = self.peaks[mask]
        window_bursts = self.bursts[mask]

        avg_rms = float(np.mean(window_rms))
        max_peak = float(np.max(window_peaks))
        max_burst = float(np.max(window_bursts))

        # Normalized against video's distribution
        avg_energy = min(1.0, avg_rms / (self.p90_rms + 1e-6))
        peak_energy = min(1.0, max_peak / (self.max_peak + 1e-6))
        laughter_burst = min(1.0, max_burst)

        # Composite score weighting: steady energy + peak punch + excitement bursts
        composite = 0.40 * avg_energy + 0.35 * peak_energy + 0.25 * laughter_burst

        return {
            "avg_energy": round(avg_energy, 3),
            "peak_energy": round(peak_energy, 3),
            "laughter_burst": round(laughter_burst, 3),
            "composite_score": round(composite, 3),
        }


def extract_audio_profile(wav_path: str | Path, window_sec: float = 0.5, step_sec: float = 0.25) -> AudioEnergyProfile:
    """Analyze a 16-bit PCM WAV file and compute rolling dynamics."""
    path = Path(wav_path)
    if not path.is_file():
        # Fallback empty profile
        return AudioEnergyProfile(np.array([]), np.array([]), np.array([]), np.array([]), 0.0)

    with wave.open(str(path), "rb") as wf:
        sample_rate = wf.getframerate()
        n_channels = wf.getnchannels()
        n_frames = wf.getnframes()
        duration = n_frames / float(sample_rate) if sample_rate > 0 else 0.0
        
        raw_bytes = wf.readframes(n_frames)

    if n_frames == 0 or sample_rate == 0:
        return AudioEnergyProfile(np.array([]), np.array([]), np.array([]), np.array([]), 0.0)

    # Convert to float numpy array normalized between -1.0 and 1.0
    audio_int = np.frombuffer(raw_bytes, dtype=np.int16)
    if n_channels > 1:
        audio_int = audio_int.reshape(-1, n_channels).mean(axis=1).astype(np.int16)

    samples = audio_int.astype(np.float32) / 32768.0

    win_samples = max(1, int(window_sec * sample_rate))
    step_samples = max(1, int(step_sec * sample_rate))
    n_steps = max(1, (len(samples) - win_samples) // step_samples + 1)

    times = np.zeros(n_steps, dtype=np.float32)
    rms_arr = np.zeros(n_steps, dtype=np.float32)
    peaks_arr = np.zeros(n_steps, dtype=np.float32)
    bursts_arr = np.zeros(n_steps, dtype=np.float32)

    # Rolling background envelope baseline
    baseline_env = 0.05

    for i in range(n_steps):
        s_idx = i * step_samples
        e_idx = s_idx + win_samples
        chunk = samples[s_idx:e_idx]
        
        chunk_time = s_idx / sample_rate
        chunk_rms = float(np.sqrt(np.mean(chunk**2)))
        chunk_peak = float(np.max(np.abs(chunk)))

        # Update running baseline envelope
        baseline_env = 0.95 * baseline_env + 0.05 * chunk_rms

        # Laughter / emphatic burst signature: sudden surge above running baseline with staccato peaks
        burst_ratio = max(0.0, (chunk_rms - baseline_env) / (baseline_env + 1e-4))
        burst_score = min(1.0, burst_ratio / 3.0)

        times[i] = chunk_time
        rms_arr[i] = chunk_rms
        peaks_arr[i] = chunk_peak
        bursts_arr[i] = burst_score

    return AudioEnergyProfile(times, rms_arr, peaks_arr, bursts_arr, duration)
