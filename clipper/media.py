import json, subprocess


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def duration(path):
    try:
        out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)]).stdout
        return float(json.loads(out)["format"]["duration"])
    except Exception:
        return 0.0


def video_size(path):
    s = json.loads(run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                        "-of", "json", str(path)]).stdout)["streams"][0]
    return s["width"], s["height"]
