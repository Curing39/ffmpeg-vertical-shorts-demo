"""shorts.py: cut a long 16:9 video into vertical 1080x1920 clips with optional burned-in captions.

Independent demonstration tool (Python 3.10+, ffmpeg on PATH; no paid services, no cloud, no GPU).

    python shorts.py --input long.mp4 --clips clips.json [--srt captions.srt]
                     [--framing crop|fill] [--offset -1..1] [--font FILE] [--font-size 64]
                     [--caption-y 0.72] [--lufs -14] [--out out_dir]

For each clip: frame-accurate cut (re-encoded), H.264 + AAC at the source frame rate, crop or fill framing,
captions retimed to the clip (lines crossing the clip edges are trimmed, not dropped), two-pass loudness
normalisation, and a 6-frame JPG contact sheet. One bad clip is reported and the rest still render.
A run summary is written to <out>/report.json.
"""
from __future__ import annotations

import argparse
import json
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

W, H = 1080, 1920


class ClipError(Exception):
    pass


@dataclass
class Cue:
    start: float
    end: float
    text: str


def parse_time(s: str) -> float:
    m = re.fullmatch(r"(?:(\d+):)?(\d+):(\d+(?:[.,]\d+)?)", s.strip())
    if not m:
        raise ClipError(f"bad timestamp {s!r} (use HH:MM:SS.mmm)")
    h, mnt, sec = m.groups()
    return int(h or 0) * 3600 + int(mnt) * 60 + float(sec.replace(",", "."))


def srt_time(t: float) -> str:
    ms = round(max(t, 0) * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def read_srt(path: Path) -> list[Cue]:
    cues = []
    for block in re.split(r"\r?\n\s*\r?\n", path.read_text(encoding="utf-8-sig").strip()):
        lines = block.strip().splitlines()
        arrow = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if arrow is None:
            continue
        a, b = [x.strip().split(" ")[0] for x in lines[arrow].split("-->")]
        cues.append(Cue(parse_time(a), parse_time(b), "\n".join(lines[arrow + 1:]).strip()))
    return cues


def retime(cues: list[Cue], start: float, end: float) -> list[Cue]:
    """Cues overlapping [start, end], shifted to clip time and clamped to [0, end-start] (trimmed, not dropped)."""
    dur = end - start
    out = []
    for c in cues:
        if c.end <= start or c.start >= end:
            continue
        out.append(Cue(max(c.start - start, 0.0), min(c.end - start, dur), c.text))
    return out


def write_srt(cues: list[Cue], path: Path) -> None:
    path.write_text("\n".join(f"{i}\n{srt_time(c.start)} --> {srt_time(c.end)}\n{c.text}\n"
                              for i, c in enumerate(cues, 1)), encoding="utf-8")


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode:
        raise ClipError(f"ffmpeg failed: {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else r.returncode}")
    return r


def probe(path: Path) -> dict:
    r = run(["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,codec_name,width,height,r_frame_rate",
             "-of", "json", str(path)])
    return json.loads(r.stdout)


def has_filter(name: str) -> bool:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True)
    return re.search(rf"^\s*\S+\s+{name}\s", r.stdout, re.M) is not None


def default_font() -> str | None:
    for p in {"Windows": ["C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf"],
              "Darwin": ["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial.ttf",
                         "/System/Library/Fonts/Helvetica.ttc"],
              }.get(platform.system(), ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]):
        if Path(p).exists():
            return p
    return None


def font_family(path: str) -> str | None:
    """libass selects fonts by family name, not file name. Pillow is optional; without it libass picks a default."""
    try:
        from PIL import ImageFont
        return ImageFont.truetype(path, 12).getname()[0]
    except Exception:
        return None


def esc(path: str) -> str:
    """Escape a path for use inside an ffmpeg filter argument."""
    return path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def framing_filter(mode: str, offset: float) -> str:
    if mode == "crop":
        # scale to 1920 high, then crop 1080 wide; offset -1 = left edge, 0 = centre, +1 = right edge
        return (f"scale=-2:{H},crop={W}:{H}:x='(iw-{W})/2*(1+{offset:.4f})':y=0,setsar=1")
    # fill: blurred, enlarged copy behind the whole 16:9 frame
    return (f"split=2[bg][fg];[bg]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
            f"gblur=sigma=40[bgb];[fg]scale={W}:-2[fgs];[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1")


def ass_time(t: float) -> str:
    cs = round(max(t, 0) * 100)
    h, cs = divmod(cs, 360_000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def write_ass(cues: list[Cue], path: Path, family: str, size: int, ypos: float) -> None:
    """ASS with an explicit 1080x1920 canvas, so font size and position are in real output pixels.
    (A plain SRT is laid out on libass's default 384x288 canvas, which mis-scales size and margins.)"""
    margin_v = int(H * (1 - ypos))
    head = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 0\n"
            "ScaledBorderAndShadow: yes\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, "
            "SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, "
            "Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Cap,{family},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,5,0,2,"
            f"60,60,{margin_v},1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
            "Effect, Text\n")
    lines = [f"Dialogue: 0,{ass_time(c.start)},{ass_time(c.end)},Cap,,0,0,0,,"
             + c.text.replace("\n", r"\N").replace("{", "(").replace("}", ")") for c in cues]
    path.write_text(head + "\n".join(lines) + "\n", encoding="utf-8")


def caption_filter(srt: Path, font: str | None, size: int, ypos: float, use_libass: bool,
                   cues: list[Cue]) -> str:
    if use_libass:
        ass = srt.with_suffix(".ass")
        write_ass(cues, ass, (font_family(font) if font else None) or "Arial", size, ypos)
        fontsdir = f":fontsdir='{esc(str(Path(font).parent))}'" if font else ""
        return f"ass='{esc(str(ass))}'{fontsdir}"
    # Fallback for ffmpeg builds without libass: one drawtext per cue, same look.
    if not font:
        raise ClipError("no libass in this ffmpeg and no --font given for the drawtext fallback")
    parts = []
    for c in cues:
        text = c.text.replace("\\", "\\\\").replace("'", "\u2019").replace(":", "\\:").replace("%", "\\%")
        parts.append(f"drawtext=fontfile='{esc(font)}':text='{text}':fontsize={size}:fontcolor=white:"
                     f"borderw=5:bordercolor=black:x=(w-text_w)/2:y=h*{ypos:.3f}-text_h:"
                     f"enable='between(t,{c.start:.3f},{c.end:.3f})'")
    return ",".join(parts) if parts else "null"


def loudnorm_measure(src: Path, start: float, dur: float, lufs: float) -> dict:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{dur:.3f}",
                        "-vn", "-af", f"loudnorm=I={lufs}:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr)
    if not m:
        raise ClipError("loudness measurement failed")
    return json.loads(m.group(0))


def contact_sheet(clip: Path, dur: float, out: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        frames = []
        for i in range(6):
            t = dur * (i + 0.5) / 6
            f = Path(tmp) / f"f{i}.png"
            run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.3f}", "-i", str(clip),
                 "-frames:v", "1", "-vf", "scale=360:640", str(f)])
            frames.append(f)
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-framerate", "1",
             "-i", str(Path(tmp) / "f%d.png"), "-vf", "tile=3x2:padding=8:margin=8:color=black",
             "-frames:v", "1", "-q:v", "3", str(out)])


def render_clip(args, src_info: dict, clip: dict, cues: list[Cue], use_libass: bool, font: str | None,
                tmp: Path) -> dict:
    name = clip.get("name") or "clip"
    if not re.fullmatch(r"[\w\-]+", name):
        raise ClipError(f"clip name {name!r} must use letters, digits, _ or -")
    start, end = parse_time(clip["start"]), parse_time(clip["end"])
    if end <= start:
        raise ClipError("end must be after start")
    if start < 0 or end > src_info["duration"] + 1e-3:
        raise ClipError(f"times {clip['start']}-{clip['end']} fall outside the video "
                        f"(length {src_info['duration']:.3f}s)")
    dur = end - start
    vf = framing_filter(args.framing, args.offset)
    retimed = retime(cues, start, end) if cues else []
    if args.srt:
        clip_srt = tmp / f"{name}.srt"
        write_srt(retimed, clip_srt)
        if retimed:
            vf += "," + caption_filter(clip_srt, font, args.font_size, args.caption_y, use_libass, retimed)
    m = loudnorm_measure(args.input, start, dur, args.lufs)
    af = (f"loudnorm=I={args.lufs}:TP=-1.5:LRA=11:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
          f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:"
          f"linear=true,aresample=48000")
    out = args.out / f"{name}.mp4"
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}", "-i", str(args.input),
         "-t", f"{dur:.3f}", "-filter_complex", f"[0:v]{vf}[v];[0:a]{af}[a]", "-map", "[v]", "-map", "[a]",
         "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
         "-fps_mode", "passthrough", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)])
    contact_sheet(out, dur, args.out / f"{name}_contact.jpg")
    return {"name": name, "status": "ok", "start": start, "end": end, "duration": round(dur, 3),
            "captions": len(retimed), "output": out.name}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--clips", type=Path, required=True)
    p.add_argument("--srt", type=Path)
    p.add_argument("--framing", choices=["crop", "fill"], default="fill")
    p.add_argument("--offset", type=float, default=0.0, help="crop only: -1 left edge .. 0 centre .. 1 right edge")
    p.add_argument("--font", help="font file for captions (default: a system sans font)")
    p.add_argument("--font-size", type=int, default=64)
    p.add_argument("--caption-y", type=float, default=0.78, help="caption baseline as a fraction of height")
    p.add_argument("--lufs", type=float, default=-14.0)
    p.add_argument("--out", type=Path, default=Path("out"))
    args = p.parse_args(argv)

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"error: {tool} not found on PATH")
    for f in [args.input, args.clips] + ([args.srt] if args.srt else []):
        if not f.exists():
            sys.exit(f"error: file not found: {f}")
    if not -1 <= args.offset <= 1:
        sys.exit("error: --offset must be between -1 and 1")
    args.out.mkdir(parents=True, exist_ok=True)

    info = probe(args.input)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    src = {"duration": float(info["format"]["duration"]), "fps": v["r_frame_rate"]}
    cues = read_srt(args.srt) if args.srt else []
    font = args.font or default_font()
    use_libass = has_filter("subtitles")
    clips = json.loads(args.clips.read_text(encoding="utf-8"))["clips"]

    results = []
    with tempfile.TemporaryDirectory() as tmp:
        for clip in clips:
            try:
                r = render_clip(args, src, clip, cues, use_libass, font, Path(tmp))
                print(f"[ok]   {r['name']}: {r['duration']}s, {r['captions']} captions -> {r['output']}")
            except ClipError as e:
                r = {"name": clip.get("name"), "status": "error", "error": str(e)}
                print(f"[skip] {clip.get('name')}: {e}")
            results.append(r)
    report = {"input": args.input.name, "source_fps": src["fps"], "framing": args.framing,
              "caption_renderer": "libass" if use_libass else "drawtext", "lufs_target": args.lufs,
              "platform": f"{platform.system()} {platform.machine()}", "clips": results}
    (args.out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    ok = sum(r["status"] == "ok" for r in results)
    print(f"done: {ok} rendered, {len(results) - ok} skipped -> {args.out / 'report.json'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
