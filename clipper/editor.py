import logging, subprocess, tempfile
from pathlib import Path
import cv2
import numpy as np
from . import db
from .config import env

log = logging.getLogger("editor")
W, H = 1080, 1920
CAM_H = 640
ENC = env("VIDEO_ENCODER", "libx264")  # macOS-Alternative: h264_videotoolbox
_face = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")


def source(rec_id, a, b, tmp):
    cs = db.q("SELECT path,start_ts,duration FROM chunks WHERE recording_id=? AND status!='deleted' "
              "AND start_ts<? AND start_ts+duration>? ORDER BY start_ts", rec_id, b, a)
    if not cs:
        raise RuntimeError("keine Chunks für Zeitraum")
    lst = Path(tmp) / "src.txt"
    lst.write_text("".join(f"file '{Path(c['path']).resolve()}'\n" for c in cs))
    return ["-f", "concat", "-safe", "0", "-i", str(lst)], cs[0]["start_ts"]


def frame_at(src, t, out, width=None):
    """src: ffmpeg-Input-Argumente (Liste) oder Dateipfad."""
    src = src if isinstance(src, list) else ["-i", str(src)]
    vf = ["-vf", f"scale={width}:-2"] if width else []
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", f"{max(t, 0):.2f}", *src, "-frames:v", "1",
                    *vf, "-q:v", "3", str(out)], check=True)
    return out


def ahash(img_path):
    g = cv2.cvtColor(cv2.imread(str(img_path)), cv2.COLOR_BGR2GRAY)
    s = cv2.resize(g, (16, 16), interpolation=cv2.INTER_AREA)
    return "".join("1" if v else "0" for v in (s > s.mean()).flatten())


def hamming(a, b):
    return sum(x != y for x, y in zip(a, b))


def _layout(src, off, dur, tmp):
    faces, diffs, size = [], [], None
    for k, t in enumerate(np.linspace(off + 1, off + dur - 1, 8)):
        f1 = cv2.imread(str(frame_at(src, t, tmp / f"a{k}.jpg")))
        f2 = cv2.imread(str(frame_at(src, t + 0.4, tmp / f"b{k}.jpg")))
        size = f1.shape[1], f1.shape[0]
        g = cv2.cvtColor(f1, cv2.COLOR_BGR2GRAY)
        det = _face.detectMultiScale(g, 1.1, 6, minSize=(int(size[1] * 0.04),) * 2)
        faces.append([tuple(map(int, d)) for d in det])
        d = cv2.absdiff(cv2.cvtColor(f2, cv2.COLOR_BGR2GRAY), g)
        diffs.append(d)
    fw, fh = size
    flat = [f for fl in faces for f in fl]
    if flat:
        cx = np.median([x + w / 2 for x, y, w, h in flat])
        cy = np.median([y + h / 2 for x, y, w, h in flat])
        fs = np.median([h for *_, h in flat])
        stable = sum(any(abs(x + w / 2 - cx) < fw * .05 and abs(y + h / 2 - cy) < fh * .05 for x, y, w, h in fl)
                     for fl in faces)
        if stable >= 4 and fs < fh * 0.25:
            ch = min(fs * 3.2, fh)
            cw = min(ch * W / CAM_H, fw)
            ch = cw * CAM_H / W
            x = int(np.clip(cx - cw / 2, 0, fw - cw))
            y = int(np.clip(cy - ch / 2, 0, fh - ch))
            cam = (int(cw) // 2 * 2, int(ch) // 2 * 2, x, y)
            bw = int(fh * W / (H - CAM_H)) // 2 * 2
            gx = (fw - bw) // 2
            if cx > fw * 0.6 or cx < fw * 0.4:  # Gameplay leicht weg von der Facecam zentrieren
                gx = int(np.clip(gx + (-1 if cx > fw / 2 else 1) * fw * 0.05, 0, fw - bw))
            return {"mode": "split", "cam": cam, "game": (bw, fh, gx, 0)}
        center = cx
    else:
        m = np.mean(diffs, axis=0)
        col = m.sum(axis=0)
        center = float((col * np.arange(len(col))).sum() / max(col.sum(), 1)) if col.sum() > 0 else fw / 2
    cw = int(fh * 9 / 16) // 2 * 2
    x = int(np.clip(center - cw / 2, 0, fw - cw))
    return {"mode": "single", "crop": (cw, fh, x, 0)}


def _ass_time(t):
    t = max(t, 0)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def _esc(s):
    return s.replace("{", "(").replace("}", ")").replace("\\", "")


def subtitles(words, t0, dur, mode, hook, credit, path):
    y = CAM_H + 120 if mode == "split" else 1300
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,Arial Black,96,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,3,5,60,60,0,1
Style: Hook,Arial Black,64,&H00000000,&H00FFFFFF,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,18,0,8,80,80,0,1
Style: Credit,Arial,34,&H90FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,2,1,2,40,40,40,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    ws = [dict(w, s=w["s"] - t0, e=w["e"] - t0) for w in words if 0 <= w["s"] - t0 < dur]
    groups, cur = [], []
    for w in ws:
        if cur and (len(cur) >= 3 or w["s"] - cur[-1]["e"] > 0.6 or cur[-1]["w"][-1:] in ".?!,"):
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    for g in groups:
        for i, w in enumerate(g):
            end = g[i + 1]["s"] if i + 1 < len(g) else w["e"] + 0.25
            txt = " ".join(("{\\c&H00E5FF&\\fscx112\\fscy112}" + _esc(x["w"].upper()) + "{\\r}") if j == i
                           else _esc(x["w"].upper()) for j, x in enumerate(g))
            ev.append(f"Dialogue: 0,{_ass_time(w['s'])},{_ass_time(end)},Sub,,0,0,0,,{{\\pos(540,{y})}}{txt}")
    if hook:
        hy = 90 if mode == "split" else 260
        ev.append(f"Dialogue: 1,{_ass_time(0)},{_ass_time(2.8)},Hook,,0,0,0,,{{\\pos(540,{hy})\\fad(0,250)}}"
                  f"{_esc(hook.upper())}")
    ev.append(f"Dialogue: 0,{_ass_time(0)},{_ass_time(dur)},Credit,,0,0,0,,{_esc(credit)}")
    Path(path).write_text(head + "\n".join(ev) + "\n", encoding="utf-8")


def render(rec_id, start, end, peak, words, hook, credit, out):
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        src, base = source(rec_id, start, end, tmp)
        off, dur = start - base, end - start
        lay = _layout(src, off, dur, tmp)
        subtitles(words, start, dur, lay["mode"], hook, credit, tmp / "subs.ass")
        p = max(peak - start, 0)
        if lay["mode"] == "split":
            cw, ch, cx, cy = lay["cam"]
            gw, gh, gx, gy = lay["game"]
            v = (f"[0:v]fps=30,split=2[a][b];[a]crop={cw}:{ch}:{cx}:{cy},scale={W}:{CAM_H}[top];"
                 f"[b]crop={gw}:{gh}:{gx}:{gy},scale={W}:{H - CAM_H}[bot];[top][bot]vstack[v0];")
        else:
            cw, ch, cx, cy = lay["crop"]
            v = f"[0:v]fps=30,crop={cw}:{ch}:{cx}:{cy},scale={W}:{H}[v0];"
        zoom = f"if(between(it\\,{p - 0.2:.2f}\\,{p + 1.6:.2f})\\,1.12\\,1)"
        v += (f"[v0]zoompan=z='{zoom}':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps=30,"
              f"ass=subs.ass,format=yuv420p[v];[0:a]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[a]")
        venc = ["-c:v", "h264_videotoolbox", "-b:v", "9M"] if ENC == "h264_videotoolbox" else \
            ["-c:v", "libx264", "-preset", "medium", "-crf", "20"]
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", f"{off:.2f}", "-t", f"{dur:.2f}", *src,
                        "-filter_complex", v, "-map", "[v]", "-map", "[a]", *venc, "-c:a", "aac", "-b:a", "160k",
                        "-movflags", "+faststart", str(Path(out).resolve())], check=True, cwd=td)
        thumb = Path(out).with_suffix(".jpg")
        frame_at(str(out), min(p + 0.5, dur - 0.5), thumb, 540)
    log.info("Gerendert: %s (%s, %.0fs)", out, lay["mode"], dur)
    return lay["mode"]
