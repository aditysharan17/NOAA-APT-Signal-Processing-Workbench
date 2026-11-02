#!/usr/bin/env python3
"""NOAA APT Signal Processing Workbench.

Processes existing recordings; it does not transmit or tune an SDR device.
Audio mode expects demodulated APT audio in WAV format.
IQ mode expects complex baseband samples stored in a NumPy .npy file.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from PIL import Image
from scipy.io import wavfile
from scipy.signal import firwin, fftconvolve, resample_poly, welch, spectrogram

APT_SAMPLES_PER_LINE = 2080
APT_LINES_PER_SECOND = 2.0

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("noaa-apt")


def normalize_float(x: np.ndarray) -> np.ndarray:
    """Convert numeric samples to finite float64 and peak-normalize safely."""
    x = np.asarray(x)
    if x.size == 0:
        raise ValueError("Input signal is empty.")
    if not np.all(np.isfinite(x)):
        raise ValueError("Input contains NaN or infinite values.")
    x = x.astype(np.float64, copy=False)
    peak = float(np.max(np.abs(x)))
    return x / peak if peak > 0 else x


def load_wav(path: Path) -> tuple[int, np.ndarray]:
    """Load WAV audio and mix stereo to mono."""
    sample_rate, raw = wavfile.read(path)
    if raw.ndim == 2:
        raw = raw.astype(np.float64).mean(axis=1)
    elif raw.ndim != 1:
        raise ValueError("Expected mono or stereo WAV audio.")
    if np.issubdtype(raw.dtype, np.integer):
        info = np.iinfo(raw.dtype)
        scale = max(abs(info.min), abs(info.max))
        audio = raw.astype(np.float64) / scale
    else:
        audio = raw.astype(np.float64)
    if sample_rate <= 0:
        raise ValueError("Invalid WAV sample rate.")
    return int(sample_rate), normalize_float(audio)


def load_iq(path: Path, sample_rate: int) -> tuple[int, np.ndarray]:
    """Load complex IQ samples from a NumPy .npy file."""
    iq = np.load(path, allow_pickle=False)
    if not np.iscomplexobj(iq):
        raise ValueError("IQ .npy input must contain complex-valued samples.")
    if iq.ndim != 1:
        raise ValueError("Expected a one-dimensional complex IQ array.")
    if sample_rate <= 0:
        raise ValueError("Sample rate must be positive.")
    if iq.size == 0:
        raise ValueError("IQ recording is empty.")
    if not np.all(np.isfinite(iq.real)) or not np.all(np.isfinite(iq.imag)):
        raise ValueError("IQ recording contains non-finite values.")
    return int(sample_rate), iq.astype(np.complex128, copy=False)


def fm_demodulate(iq: np.ndarray) -> np.ndarray:
    """Basic phase-difference FM discriminator for complex baseband IQ."""
    if len(iq) < 2:
        raise ValueError("At least two IQ samples are required for FM demodulation.")
    product = iq[1:] * np.conj(iq[:-1])
    return np.angle(product).astype(np.float64)


def fir_lowpass(signal: np.ndarray, sample_rate: int, cutoff_hz: float,
                taps: int = 129) -> np.ndarray:
    """Apply a windowed-sinc FIR low-pass filter."""
    if not 0 < cutoff_hz < sample_rate / 2:
        raise ValueError("Cutoff must be between 0 Hz and the Nyquist frequency.")
    if taps < 3 or taps % 2 == 0:
        raise ValueError("FIR tap count must be an odd integer >= 3.")
    coeffs = firwin(taps, cutoff_hz, fs=sample_rate, window="hamming")
    # fftconvolve handles long recordings efficiently. 'same' preserves length.
    return fftconvolve(signal, coeffs, mode="same")


def save_wav(path: Path, sample_rate: int, signal: np.ndarray) -> None:
    """Save normalized mono audio as 16-bit PCM WAV."""
    signal = np.clip(signal, -1.0, 1.0)
    wavfile.write(path, sample_rate, (signal * 32767).astype(np.int16))


def plot_spectrum(path: Path, signal: np.ndarray, sample_rate: int) -> None:
    freqs, power = welch(signal, fs=sample_rate, nperseg=min(8192, len(signal)))
    power_db = 10 * np.log10(np.maximum(power, 1e-15))
    fig, ax = plt.subplots(figsize=(11, 4.5))
    ax.plot(freqs, power_db, linewidth=0.8)
    ax.set(title="NOAA APT — Average Power Spectrum",
           xlabel="Frequency (Hz)", ylabel="Power (dB/Hz)")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def plot_spectrogram(path: Path, signal: np.ndarray, sample_rate: int) -> None:
    freqs, times, power = spectrogram(
        signal, fs=sample_rate, nperseg=min(2048, len(signal)),
        noverlap=min(1536, max(0, min(2048, len(signal)) - 1))
    )
    fig, ax = plt.subplots(figsize=(12, 5))
    mesh = ax.pcolormesh(
        times, freqs, 10 * np.log10(np.maximum(power, 1e-15)),
        shading="auto", cmap="viridis"
    )
    ax.set(title="NOAA APT — Spectrogram", xlabel="Time (s)",
           ylabel="Frequency (Hz)")
    fig.colorbar(mesh, ax=ax, label="Power (dB/Hz)")
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def reconstruct_strip(signal: np.ndarray, sample_rate: int, output: Path) -> dict:
    """Resample audio to nominal APT image sample rate and save a strip.

    This does not implement sync-pulse locking. The result can be slanted or
    misaligned when timing differs from nominal values or the recording has
    not been properly FM-demodulated.
    """
    target_rate = APT_SAMPLES_PER_LINE * APT_LINES_PER_SECOND
    # Resample to 4160 samples/sec, i.e. 2 lines/sec * 2080 samples/line.
    from fractions import Fraction
    ratio = Fraction(target_rate, sample_rate).limit_denominator(10000)
    resampled = resample_poly(signal, ratio.numerator, ratio.denominator)
    line_count = len(resampled) // APT_SAMPLES_PER_LINE
    if line_count < 1:
        raise ValueError("Recording is too short to contain one complete APT line.")
    strip = resampled[:line_count * APT_SAMPLES_PER_LINE]
    strip = strip.reshape(line_count, APT_SAMPLES_PER_LINE)
    # Robust percentile contrast normalization for visualization.
    lo, hi = np.percentile(strip, [1, 99])
    if hi <= lo:
        pixels = np.zeros(strip.shape, dtype=np.uint8)
    else:
        pixels = (np.clip((strip - lo) / (hi - lo), 0, 1) * 255).astype(np.uint8)
    Image.fromarray(pixels, mode="L").save(output)
    return {
        "image_lines": int(line_count),
        "samples_per_line": APT_SAMPLES_PER_LINE,
        "nominal_line_rate_per_second": APT_LINES_PER_SECOND,
        "image_width": APT_SAMPLES_PER_LINE,
        "image_height": int(line_count),
        "sync_lock_implemented": False,
    }


def process(input_path: Path, mode: str, sample_rate: int | None,
            output_dir: Path, cutoff_hz: float, taps: int) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    if mode == "audio":
        fs, raw = load_wav(input_path)
        baseband_audio = raw
        demod_note = "Input assumed to be already FM-demodulated APT audio."
    else:
        if sample_rate is None:
            raise ValueError("--sample-rate is required for IQ mode.")
        fs, iq = load_iq(input_path, sample_rate)
        baseband_audio = fm_demodulate(iq)
        demod_note = "Complex IQ processed using phase-difference FM discriminator."
        # IQ phase discriminator output is unitless radians/sample. Scale and
        # normalize for plotting and downstream visualization.
        baseband_audio = normalize_float(baseband_audio)

    filtered = fir_lowpass(baseband_audio, fs, cutoff_hz, taps)
    filtered = normalize_float(filtered)

    save_wav(output_dir / "filtered_audio.wav", fs, filtered)
    plot_spectrum(output_dir / "spectrum.png", filtered, fs)
    plot_spectrogram(output_dir / "spectrogram.png", filtered, fs)
    image_info = reconstruct_strip(
        filtered, fs, output_dir / "apt_image_strip.png"
    )

    rms = float(np.sqrt(np.mean(filtered ** 2)))
    peak = float(np.max(np.abs(filtered)))
    report = {
        "input_file": str(input_path),
        "mode": mode,
        "sample_rate_hz": fs,
        "duration_seconds": round(len(filtered) / fs, 3),
        "fir_cutoff_hz": cutoff_hz,
        "fir_taps": taps,
        "rms_level": rms,
        "peak_level": peak,
        "demodulation_note": demod_note,
        "reconstruction": image_info,
        "limitations": [
            "Nominal line-rate reconstruction only; no sync-pulse detection or clock recovery.",
            "Audio mode expects demodulated APT audio, not raw RF or IQ.",
            "Image quality depends on recording quality and correct input format."
        ],
    }
    (output_dir / "report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    log.info("Processing complete. Outputs saved to %s", output_dir.resolve())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyze and reconstruct images from recorded NOAA APT signals."
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    audio_parser = subparsers.add_parser("audio", help="Process a demodulated WAV recording.")
    audio_parser.add_argument("input", type=Path)

    iq_parser = subparsers.add_parser("iq", help="Process complex IQ samples in .npy format.")
    iq_parser.add_argument("input", type=Path)
    iq_parser.add_argument("--sample-rate", type=int, required=True,
                           help="IQ sample rate in samples/second.")

    for sub in (audio_parser, iq_parser):
        sub.add_argument("--out", type=Path, default=Path("results"))
        sub.add_argument("--cutoff", type=float, default=6000.0,
                         help="FIR low-pass cutoff in Hz (default: 6000).")
        sub.add_argument("--taps", type=int, default=129,
                         help="Odd FIR filter length (default: 129).")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        process(
            input_path=args.input,
            mode=args.mode,
            sample_rate=getattr(args, "sample_rate", None),
            output_dir=args.out,
            cutoff_hz=args.cutoff,
            taps=args.taps,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
