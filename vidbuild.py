#!/usr/bin/env python3
"""Single-keyframe camera-move video.

Measured 2026-10-29: generating one still per shot produced a montage of
DIFFERENT people in different rooms, and shot prompts that pushed the subject
"small in frame" made the model clothe her — at small scale it stops honouring
"unclothed". So the honest construction is: get ONE approved keyframe, then move
a virtual camera over it. Same subject, same room, continuous motion.
"""
import os
import subprocess


def _seg(i, dur, fps, w, h):
    """A camera move as continuous zoom+pan over the still.

    `on` is the output frame index inside this segment, so every expression is
    linear in time and the move never jumps.
    """
    n = max(2, int(round(dur * fps)))
    moves = [
        # (zoom start, zoom end, pan x from, pan x to, pan y from, pan y to)
        (1.00, 1.22, 0.5, 0.5, 0.45, 0.40),   # push in, drift up
        (1.22, 1.02, 0.5, 0.5, 0.42, 0.50),   # pull back
        (1.06, 1.26, 0.35, 0.65, 0.50, 0.50),  # pan left -> right
        (1.24, 1.24, 0.65, 0.38, 0.50, 0.46),  # lateral truck
        (1.00, 1.18, 0.50, 0.42, 0.35, 0.42),  # tilt down from face
        (1.16, 1.16, 0.42, 0.58, 0.62, 0.55),  # waist-level drift
        (1.28, 1.04, 0.50, 0.50, 0.55, 0.42),  # retreat from close
        (1.02, 1.14, 0.60, 0.40, 0.48, 0.48),  # slow arc
    ]
    z0, z1, x0, x1, y0, y1 = moves[i % len(moves)]
    z = "%f+(%f-%f)*on/%d" % (z0, z1, z0, n - 1)
    cx = "%f+(%f-%f)*on/%d" % (x0, x1, x0, n - 1)
    cy = "%f+(%f-%f)*on/%d" % (y0, y1, y0, n - 1)
    # zoompan crops a window of size iw/zoom around (x,y) in source pixels.
    return (
        "[0:v]scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,"
        "setsar=1,split=1[s%d];"
        "[s%d]zoompan=z='%s':x='%s*iw-iw/zoom/2':y='%s*ih-ih/zoom/2':"
        "d=%d:s=%dx%d:fps=%d[v%d]"
        % (w * 2, h * 2, w * 2, h * 2, i,
           i, z, cx, cy, n, w, h, fps, i)
    )


def build_camera_video(image, out_path, duration=8.0, fps=25,
                       segments=3, w=1280, h=720, crossfade=0.5):
    """Concatenate several camera moves over one still, with soft dissolves."""
    # Each dissolve consumes `crossfade` seconds of overlap, so the per-segment
    # length has to grow to still land on the requested total duration.
    seg_dur = (duration + (segments - 1) * crossfade) / float(segments)
    parts, chains = [], []
    for i in range(segments):
        chains.append(_seg(i, seg_dur, fps, w, h))
    if segments > 1:
        prev = "v0"
        for i in range(1, segments):
            off = i * (seg_dur - crossfade)
            chains.append(
                "[%s][v%d]xfade=transition=fade:duration=%.2f:offset=%.2f[x%d]"
                % (prev, i, crossfade, off, i))
            prev = "x%d" % i
        last = prev
    else:
        last = "v0"
    cmd = (["ffmpeg", "-y", "-loglevel", "error", "-loop", "1",
            "-framerate", "1", "-t", "1", "-i", image,
            "-filter_complex", ";".join(chains), "-map", "[%s]" % last,
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
            "-movflags", "+faststart", out_path])
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or "")[-500:])
    return out_path


def build_montage(frames, out_path, per_frame=2.2, fps=25, xfade=0.6,
                  w=1280, h=720):
    """Fallback: several stills, slow push on each, dissolved together.

    Each input is a SINGLE frame (-framerate 1 -t 1). Measured: with
    `-loop 1 -t 2.2` the input already held 55 frames at 25 fps and zoompan's
    `d` multiplied per input frame — 4 stills became a 104 s file.
    """
    n = len(frames)
    if n == 0:
        return None
    if n == 1:
        return build_camera_video(frames[0], out_path, duration=6.0, fps=fps,
                                  segments=2, w=w, h=h, crossfade=xfade)
    frames_per = max(2, int(round(per_frame * fps)))
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    for f in frames:
        cmd += ["-loop", "1", "-framerate", "1", "-t", "1", "-i", f]
    chains = []
    for i in range(n):
        step = 0.18 / frames_per
        if i % 2 == 0:
            z = "min(zoom+%.6f,1.18)" % step
        else:
            z = "if(lte(zoom,1.0),1.18,max(1.001,zoom-%.6f))" % step
        chains.append(
            "[%d:v]scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,"
            "setsar=1,zoompan=z='%s':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            "d=%d:s=%dx%d:fps=%d[v%d]"
            % (i, w * 2, h * 2, w * 2, h * 2, z, frames_per, w, h, fps, i))
    prev = "v0"
    for i in range(1, n):
        off = i * (per_frame - xfade)
        chains.append(
            "[%s][v%d]xfade=transition=fade:duration=%.2f:offset=%.2f[x%d]"
            % (prev, i, xfade, off, i))
        prev = "x%d" % i
    cmd += ["-filter_complex", ";".join(chains), "-map", "[%s]" % prev,
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
            "-movflags", "+faststart", out_path]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or "")[-500:])
    return out_path
