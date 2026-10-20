"""Generate a fully SYNTHETIC test source: no third-party footage, nothing downloaded.

Writes to sample/:
  source_16x9.mp4  1920x1080, 30 fps, 90 s; moving shapes + a big timecode in the upper area (the lower third
                   stays dark so burned-in captions can be detected automatically); audio = tone at about -30 LUFS
                   so loudness normalisation has real work to do
  captions.srt     one caption every 3 s, some deliberately crossing clip boundaries
  clips.json       4 clips: three valid, one outside the video's length (must be reported, not crash the batch)
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from shorts import default_font, esc, has_filter

HERE = Path(__file__).parent
OUT = HERE / "sample"
DURATION = 90


def ts(sec: float) -> str:
    ms = round(sec * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def main() -> None:
    OUT.mkdir(exist_ok=True)
    bg = (f"color=c=0x101826:s=1920x1080:r=30:d={DURATION},format=yuv420p,"
          "drawbox=x=1660:y=420:w=220:h=220:color=0x3b82f6@0.9:t=fill,"
          "drawbox=x=300:y=380:w=160:h=160:color=0x14b8a6@0.9:t=fill")
    # testsrc: ffmpeg's built-in pattern with a moving element and a frame counter, drawn without freetype, so every
    # frame differs on every ffmpeg build. Kept in the upper area, away from the caption band.
    pattern = f"testsrc=size=480x270:rate=30:duration={DURATION}"
    graph = "[0:v][1:v]overlay=1060:60"
    if has_filter("drawtext") and default_font():  # Homebrew ffmpeg on macOS has no drawtext
        graph += (f",drawtext=fontfile='{esc(default_font())}':text='SYNTHETIC DEMO  %{{pts\\:hms}}':"
                  "x=80:y=60:fontsize=56:fontcolor=0xe5e7eb")
    graph += "[v]"
    audio = f"sine=frequency=330:sample_rate=48000:duration={DURATION},volume=0.035"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", bg, "-f", "lavfi", "-i", pattern, "-f", "lavfi", "-i", audio,
                    "-filter_complex", graph, "-map", "[v]", "-map", "2:a",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "128k",
                    "-shortest", str(OUT / "source_16x9.mp4")], check=True)

    cues = []
    for i, start in enumerate(range(1, DURATION - 2, 3), start=1):
        cues.append(f"{i}\n{ts(start)} --> {ts(start + 2.5)}\nCaption {i} at {start}s, a sample subtitle line\n")
    (OUT / "captions.srt").write_text("\n".join(cues), encoding="utf-8")

    clips = {"clips": [
        {"name": "clip01", "start": "00:00:05.200", "end": "00:00:20.000"},   # starts and ends mid-caption
        {"name": "clip02", "start": "00:00:31.000", "end": "00:00:48.500"},
        {"name": "clip03", "start": "00:01:10.000", "end": "00:01:29.000"},
        {"name": "clip_bad", "start": "00:01:25.000", "end": "00:02:10.000"},  # past the end: must be reported
    ]}
    (OUT / "clips.json").write_text(json.dumps(clips, indent=2), encoding="utf-8")
    print("wrote", *sorted(p.name for p in OUT.iterdir()))


if __name__ == "__main__":
    main()
