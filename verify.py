"""Automated acceptance checks for shorts.py output (any OS with ffmpeg + numpy).

    python verify.py out/fill sample        # exits non-zero on any failure; writes <out>/verification.md

Checks per rendered clip: 1080x1920, H.264 + AAC, source frame rate, duration = requested length (within one
frame), integrated loudness -14 LUFS (+/-1 LU), captions visible exactly when a retimed cue is active
(including cues trimmed at the clip's start and end), the first frame matches the source frame at the
requested start better than its neighbours (frame-accurate cut), and the 6-frame contact sheet exists.
Batch checks: the out-of-range clip is reported, and the other clips still rendered.
"""
from __future__ import annotations

import json
import platform
import re
import subprocess
import sys
from pathlib import Path

import numpy as np

from shorts import H, W, framing_filter, parse_time, read_srt, retime

CAP_TOP, CAP_BOTTOM = int(H * 0.62), int(H * 0.86)   # lower-third band where captions are drawn


def ffprobe(path: Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration:stream=codec_type,codec_name,width,height,r_frame_rate",
                        "-of", "json", str(path)], capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def frame(path: Path, t: float, vf: str | None = None) -> np.ndarray:
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{t:.4f}", "-i", str(path), "-frames:v", "1"]
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(H, W, 3)


def caption_pixels(img: np.ndarray) -> int:
    band = img[CAP_TOP:CAP_BOTTOM]
    return int(np.all(band > 215, axis=2).sum())


def loudness(path: Path) -> float:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return float(re.findall(r"I:\s+(-?\d+\.\d+) LUFS", r.stderr)[-1])


def main(out_dir: str, sample_dir: str) -> int:
    out, sample = Path(out_dir), Path(sample_dir)
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    clips = {c["name"]: c for c in json.loads((sample / "clips.json").read_text(encoding="utf-8"))["clips"]}
    cues = read_srt(sample / "captions.srt")
    src = sample / "source_16x9.mp4"
    src_fps = report["source_fps"]
    num, den = map(int, src_fps.split("/"))
    frame_s = den / num
    results: list[tuple[str, bool, str]] = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok), detail))

    by_status = {r["name"]: r for r in report["clips"]}
    check("Out-of-range clip reported, not rendered", by_status["clip_bad"]["status"] == "error"
          and "outside the video" in by_status["clip_bad"]["error"], by_status["clip_bad"].get("error", ""))
    ok_clips = [r for r in report["clips"] if r["status"] == "ok"]
    check("Other clips still rendered", len(ok_clips) == 3, f"{len(ok_clips)} rendered")

    for r in ok_clips:
        name, path = r["name"], out / r["output"]
        start, end = parse_time(clips[name]["start"]), parse_time(clips[name]["end"])
        dur = end - start
        info = ffprobe(path)
        v = next(s for s in info["streams"] if s["codec_type"] == "video")
        a = next(s for s in info["streams"] if s["codec_type"] == "audio")
        check(f"{name}: 1080x1920 H.264 + AAC", (v["width"], v["height"], v["codec_name"], a["codec_name"])
              == (W, H, "h264", "aac"), f"{v['width']}x{v['height']} {v['codec_name']}/{a['codec_name']}")
        check(f"{name}: source frame rate kept", v["r_frame_rate"] == src_fps, f"{v['r_frame_rate']} vs {src_fps}")
        got = float(info["format"]["duration"])
        check(f"{name}: duration = requested length (+/- 1 frame)", abs(got - dur) <= frame_s + 0.025,
              f"{got:.3f}s vs {dur:.3f}s")
        lufs = loudness(path)
        check(f"{name}: loudness -14 LUFS (+/- 1 LU)", abs(lufs - report["lufs_target"]) <= 1.0, f"{lufs:.1f} LUFS")

        shown = retime(cues, start, end)
        probes = [((c.start + c.end) / 2, True, "mid-cue") for c in shown]
        if shown and shown[0].start == 0:
            probes.append((0.15, True, "trimmed first cue"))
        if shown and abs(shown[-1].end - dur) < 1e-6:
            probes.append((dur - 0.15, True, "trimmed last cue"))
        edges = sorted([0.0] + [x for c in shown for x in (c.start, c.end)] + [dur])
        gaps = [(edges[i] + edges[i + 1]) / 2 for i in range(0, len(edges) - 1, 2)
                if edges[i + 1] - edges[i] > 0.3]
        probes += [(g, False, "gap") for g in gaps]
        bad = []
        for t, expect, why in probes:
            px = caption_pixels(frame(path, t))
            if (px > 400) != expect:
                bad.append(f"{why}@{t:.2f}s px={px}")
        check(f"{name}: captions on exactly when a retimed cue is active ({len(probes)} probes)", not bad,
              "; ".join(bad) or f"{len(shown)} cues incl. trimmed edges")

        first = frame(path, 0).astype(np.int16)
        vf = framing_filter(report["framing"], 0.5 if "crop" in str(out) else 0.0)
        # -ss returns the first frame at or after t, so seek half a frame early to get frame n exactly
        n0 = round(start / frame_s)
        diffs = {k: float(np.abs(first - frame(src, (n0 + k - 0.5) * frame_s, vf).astype(np.int16))[:CAP_TOP].mean())
                 for k in (-1, 0, 1)}
        check(f"{name}: first frame = source frame at start (frame-accurate)", min(diffs, key=diffs.get) == 0,
              ", ".join(f"{k:+d}f:{d:.2f}" for k, d in diffs.items()))
        check(f"{name}: contact sheet exists", (out / f"{name}_contact.jpg").stat().st_size > 10_000)

    passed = all(ok for _, ok, _ in results)
    lines = [f"# Verification: {out.name}", "",
             f"Platform: {platform.system()} {platform.machine()}  ·  caption renderer: {report['caption_renderer']}",
             "", "| Check | Result | Detail |", "| --- | --- | --- |"]
    lines += [f"| {n} | {'PASS' if ok else 'FAIL'} | {d} |" for n, ok, d in results]
    lines += ["", f"**Overall: {'PASS' if passed else 'FAIL'}** ({sum(ok for _, ok, _ in results)}/{len(results)})", ""]
    (out / "verification.md").write_text("\n".join(lines), encoding="utf-8")
    for n, ok, d in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {n}  {d}")
    print("OVERALL:", "PASS" if passed else "FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "out/fill", sys.argv[2] if len(sys.argv) > 2 else "sample"))
