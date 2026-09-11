"""Cut both sprint recordings to the timing map and mux the narration over each.

    PYTHONPATH=src python -m e2e.demo_video.compose --out /tmp/nh-sprint-demo/out

End to end (skippable per stage, so a re-cut never forces a re-record):

    1. capture     sprint_gui / sprint_cli -> N_FRAMES raw frames each
    2. voiceover   one narration.m4a, every line at its timing-map offset
    3. bars        one caption strip per narration beat (captions.py's own
                   typography — the strips are the same chrome as the 26 s
                   clips', so the site can mix the two)
    4. compose     scale each capture into the 1600x900 app area, pad to
                   1600x960, overlay the current beat's strip
    5. encode      demo-sprint-gui.mp4 / demo-sprint-cli.mp4 — H.264 + AAC,
                   faded on narration.py's own fade times, audio muxed
    6. side-by-side  demo-sprint-sxs.mp4, the two staged frame sets hstacked
                   (3200x960) under the SAME single audio track — the literal
                   rendering of "both run side by side with the same audio"

Both clips carry N_FRAMES frames stepped on the narration's clock, so "cut to
the shared timing map" is enforced structurally: a frame count that is not
N_FRAMES refuses to encode, which is also what keeps the pair frame-locked to
each other and to the audio.

Then check the result rather than assuming it:

    PYTHONPATH=src python -m e2e.demo_video.verify_sync \
        --out /tmp/nh-sprint-demo/out --work /tmp/nh-sprint-demo

`verify_sync` reads the encoded files back and proves the narration lands
where the map says — everything above this line is intent, and intent is what
a muxer is free to ignore.

There is deliberately no spotlight here. The 26 s silent clips need one
because a silent full frame points at nothing; this cut has a narrator, and
the voice is the pointer. The caption bar stays: muted-autoplay embeds and
accessibility both want the beat named on screen.
"""

from __future__ import annotations

import argparse
import hashlib
import asyncio
import concurrent.futures
import shutil
import subprocess
import sys
from pathlib import Path

from . import narration as nr
from . import voiceover
from .camera import APP_H, APP_W, BAR_H, OUT_H, OUT_W
from .captions import CSS, PAGE, _serve, _stylesheet

FFMPEG = "/opt/homebrew/bin/ffmpeg"
WORK = Path("/tmp/nh-sprint-demo")

#: crf pairs per surface — same reasoning as record.py: the terminal clip is
#: wall-to-wall glyph edges and buys its quality with more bits.
MP4_CRF = {"gui": 24, "cli": 25}


def beat_index(t: float) -> int:
    idx = 0
    for i, beat in enumerate(nr.BEATS):
        if t >= beat.start:
            idx = i
    return idx


# --------------------------------------------------------------------------- #
# Caption strips — one per narration beat, captions.py's own renderer          #
# --------------------------------------------------------------------------- #

async def render_bars(out_dir: Path) -> list[Path]:
    """captions.main, parameterised by the narration's beats instead of the
    26 s cut's CAPTIONS. Same server, same stylesheet, same fonts-by-sample
    guard — the strip must be the product's own type or it reads as a
    third-party watermark."""
    from playwright.async_api import async_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    strips = [(f"{i + 1} / {len(nr.BEATS)}", beat.caption)
              for i, beat in enumerate(nr.BEATS)]
    srv = _serve()
    made: list[Path] = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(
                viewport={"width": APP_W, "height": BAR_H},
                device_scale_factor=1)
            await page.goto("http://127.0.0.1:4713/",
                            wait_until="domcontentloaded")
            await page.set_content(
                PAGE % {"css": _stylesheet(),
                        "inline": CSS % {"w": APP_W, "h": BAR_H},
                        "dots": "<i></i>" * len(strips)},
                wait_until="networkidle")
            sample = " ".join(s + t for s, t in strips)
            loaded = await page.evaluate(
                """async (sample) => {
                    const want = [['500 25px "DM Sans"', sample],
                                  ['500 22px "IBM Plex Mono"', sample]];
                    const got = [];
                    for (const [font, text] of want) {
                        const faces = await document.fonts.load(font, text);
                        got.push(faces.length > 0 && document.fonts.check(font, text));
                    }
                    await document.fonts.ready;
                    return got;
                }""", sample)
            for family, ok in zip(("DM Sans", "IBM Plex Mono"), loaded):
                if not ok:
                    raise SystemExit(f"{family} did not load for the bars")
            for i, (step, text) in enumerate(strips):
                await page.evaluate(
                    """([step, text, i]) => {
                        document.getElementById('step').textContent = step;
                        document.getElementById('text').textContent = text;
                        document.querySelectorAll('#dots i').forEach((d, n) =>
                            d.classList.toggle('on', n === i));
                    }""", [step, text, i])
                dst = out_dir / f"bar{i}.png"
                await page.locator("#bar").screenshot(path=str(dst))
                made.append(dst)
            await browser.close()
    finally:
        srv.shutdown()
    print(f"[bars] {len(made)} caption strips -> {out_dir}")
    return made


# --------------------------------------------------------------------------- #
# Frame composition and encoding — pure arg builders, thin runners             #
# --------------------------------------------------------------------------- #

def composite_args(src: Path, dst: Path, bar: Path) -> list[str]:
    """Scale a capture into the app area, pad the caption band, overlay the
    beat's strip. No spotlight — see the module docstring."""
    graph = (f"[0:v]scale={APP_W}:{APP_H}:flags=lanczos,"
             f"pad={OUT_W}:{OUT_H}:0:0:color=black[app];"
             f"[app][1:v]overlay=0:{APP_H}[out]")
    return [FFMPEG, "-y", "-loglevel", "error", "-i", str(src),
            "-i", str(bar), "-filter_complex", graph, "-map", "[out]",
            "-frames:v", "1", str(dst)]


def compose(frames_dir: Path, stage_dir: Path, bars: list[Path]) -> None:
    n = len(list(frames_dir.glob("f*.png")))
    if n != nr.N_FRAMES:
        raise SystemExit(f"{frames_dir} holds {n} frames, "
                         f"expected {nr.N_FRAMES}")
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True)
    jobs = []
    for i in range(nr.N_FRAMES):
        bar = bars[beat_index(nr.t_of(i))]
        jobs.append(composite_args(frames_dir / f"f{i:04d}.png",
                                   stage_dir / f"f{i:04d}.png", bar))
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(lambda a: subprocess.run(a, check=True), jobs))


FADE = (f"fade=t=in:st=0:d={nr.FADE_IN_END}:color=black,"
        f"fade=t=out:st={nr.FADE_OUT_START}:"
        f"d={nr.FADE_OUT_END - nr.FADE_OUT_START}:color=black")

#: The narration fades with the picture over the same window — an audio track
#: that keeps talking over a black frame reads as a glitch.
AFADE = (f"afade=t=out:st={nr.FADE_OUT_START}:"
         f"d={nr.FADE_OUT_END - nr.FADE_OUT_START}")


def encode_args(stage_dir: Path, audio: Path, out_mp4: Path,
                crf: int) -> list[str]:
    return [FFMPEG, "-y", "-loglevel", "error",
            "-framerate", str(nr.FPS), "-i", str(stage_dir / "f%04d.png"),
            "-i", str(audio),
            "-vf", FADE, "-af", AFADE,
            "-c:v", "libx264", "-crf", str(crf), "-preset", "slow",
            "-profile:v", "high", "-level", "4.0", "-pix_fmt", "yuv420p",
            "-g", str(nr.FPS), "-movflags", "+faststart",
            "-c:a", "aac", "-b:a", "96k", "-shortest", str(out_mp4)]


def sxs_args(gui_stage: Path, cli_stage: Path, audio: Path,
             out_mp4: Path, crf: int = 26) -> list[str]:
    """The combined cut: both staged frame sets, hstacked to 3200x960, under
    the one audio track. hstack refuses mismatched heights, which is one more
    structural guard that the two clips were cut to the same map."""
    graph = f"[0:v][1:v]hstack=inputs=2,{FADE}[v]"
    return [FFMPEG, "-y", "-loglevel", "error",
            "-framerate", str(nr.FPS), "-i", str(gui_stage / "f%04d.png"),
            "-framerate", str(nr.FPS), "-i", str(cli_stage / "f%04d.png"),
            "-i", str(audio),
            "-filter_complex", graph, "-map", "[v]", "-map", "2:a",
            "-af", AFADE,
            "-c:v", "libx264", "-crf", str(crf), "-preset", "slow",
            "-profile:v", "high", "-level", "4.2", "-pix_fmt", "yuv420p",
            "-g", str(nr.FPS), "-movflags", "+faststart",
            "-c:a", "aac", "-b:a", "96k", "-shortest", str(out_mp4)]


def cut(work: Path, out: Path, which: str = "both",
        allow_robot_voice: bool = False) -> None:
    out.mkdir(parents=True, exist_ok=True)
    # `say` is a legitimate fallback for RUNNING the recorder on a bare checkout
    # — that is why voiceover.py detects rather than requires. It is not a
    # legitimate thing to PUBLISH: on a machine with only compact voices it
    # reads as a robot. voiceover.build already prints a NOTE when it degrades,
    # and that is exactly how the robot read reached the public site — a NOTE
    # scrolls past in a render that takes minutes and produces a file that is
    # the right length, in sync, and passes every other check. So the shipping
    # path refuses instead of warning. The usual cause is node < 22, which makes
    # `npx hyperframes` fail while npx itself reports fine.
    engine = voiceover.engine_id()
    if engine == "say" and not allow_robot_voice:
        raise SystemExit(
            "[compose] REFUSING to cut: the neural voice is not reachable, so "
            "this render would ship the `say` formant read (the robot voice).\n"
            "  Fix: use node >= 22 (`nvm use 22`) and re-run — `npx` succeeds "
            "on older node while hyperframes underneath refuses.\n"
            "  Override with --allow-robot-voice only for a throwaway cut.")
    # The audio is cached so a re-cut never forces a re-record — but the cache
    # has to know what it is a cache OF. Keyed on the script's own text plus the
    # engine, because otherwise editing a line and re-cutting silently ships the
    # PREVIOUS narration over the new picture, and nothing fails: the file is
    # the right length, the captions still match, verify_sync still passes. That
    # happened once (a neural voice replaced `say` and the render came out
    # byte-identical to the old one), which is what this stamp is for.
    audio_dir = work / "audio"
    stamp = audio_dir / "script.stamp"
    key = hashlib.sha256(
        ("\x00".join(l.tts_text for l in nr.LINES)
         + "\x00" + engine).encode()).hexdigest()
    audio = audio_dir / "narration.m4a"
    if audio.is_file() and stamp.is_file() and stamp.read_text(encoding="utf-8").strip() == key:
        print(f"[compose] audio cache HIT ({key[:12]})")
    else:
        if audio.is_file():
            print(f"[compose] audio cache STALE — script or engine changed; "
                  f"re-rendering the narration")
        audio = voiceover.build(audio_dir)
        stamp.write_text(key)
    bars = asyncio.run(render_bars(work / "bars"))
    plan = [name for name in ("gui", "cli") if which in (name, "both")]
    for name in plan:
        stage = work / f"cut-{name}"
        compose(work / f"frames-{name}", stage, bars)
        dst = out / f"demo-sprint-{name}.mp4"
        subprocess.run(encode_args(stage, audio, dst, MP4_CRF[name]),
                       check=True)
        print(f"[{name}] {dst}  {dst.stat().st_size:,} B")
    if which == "both":
        dst = out / "demo-sprint-sxs.mp4"
        subprocess.run(sxs_args(work / "cut-gui", work / "cut-cli",
                                audio, dst), check=True)
        print(f"[sxs] {dst}  {dst.stat().st_size:,} B")


async def capture(work: Path, which: str) -> None:
    from . import sprint_cli, sprint_gui
    if which in ("gui", "both"):
        await sprint_gui.main(work / "frames-gui")
    if which in ("cli", "both"):
        await sprint_cli.main(work / "frames-cli")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--work", default=WORK, type=Path)
    ap.add_argument("--only", choices=["gui", "cli", "both"], default="both")
    ap.add_argument("--skip-capture", action="store_true")
    ap.add_argument("--allow-robot-voice", action="store_true",
                    help="cut even when the neural voice is unreachable; the "
                         "result is a `say` read and must not be published")
    args = ap.parse_args(argv)
    if not args.skip_capture:
        asyncio.run(capture(args.work, args.only))
    cut(args.work, args.out, args.only, args.allow_robot_voice)


if __name__ == "__main__":
    main(sys.argv[1:])
