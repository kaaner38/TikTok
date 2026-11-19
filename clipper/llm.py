import base64, json, logging
import anthropic
from .config import MODEL, learned

log = logging.getLogger("llm")
client = anthropic.Anthropic()

JUDGE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["what_happens", "game_or_topic", "situation", "people", "context", "virality", "reason",
                 "start_offset", "peak_offset", "end_offset"],
    "properties": {
        "what_happens": {"type": "string"}, "game_or_topic": {"type": "string"}, "situation": {"type": "string"},
        "people": {"type": "string"}, "context": {"type": "string"},
        "virality": {"type": "integer"}, "reason": {"type": "string"},
        "start_offset": {"type": "number"}, "peak_offset": {"type": "number"}, "end_offset": {"type": "number"},
    },
}

CAPTION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["variants", "best_index", "on_screen_hook", "hashtags"],
    "properties": {
        "variants": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["style", "hook", "score"],
            "properties": {"style": {"type": "string",
                                     "enum": ["curiosity", "contrast", "open_question", "shock", "story"]},
                           "hook": {"type": "string"}, "score": {"type": "integer"}}}},
        "best_index": {"type": "integer"},
        "on_screen_hook": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
}

JUDGE_SYS = """Du bist ein erfahrener TikTok-Clip-Editor für Livestream-Highlights (Gaming, Just Chatting, IRL, Reaction, Sport).
Du bekommst Keyframes und ein Transkript mit Zeitstempeln (Sekunden relativ zum Fensterstart) sowie Signal-Scores.
Bewerte nüchtern, wie viral der Moment als eigenständiger 45-180-s-Clip für Leute ist, die den Streamer nicht kennen.
virality 1-10: 10 = Millionen-Views-Potenzial, 7 = klar überdurchschnittlich, <=5 = langweilig/kontextabhängig/zu leise.
Schnitt-Regeln: start_offset höchstens 2 s vor dem stärksten Reiz (Schrei, Fail, Pointe, Schockmoment) - kein Anlauf, kein Intro.
Falls der Höhepunkt ohne Vorgeschichte unverständlich ist, beginne am Höhepunkt und plane die Erklärung danach ein.
end_offset nach Auflösung und Reaktion, bevor es abflacht. Dauer end-start zwischen 45 und 180 s.
Alle Offsets in Sekunden relativ zum Fensterstart."""

CAPTION_SYS = """Du schreibst TikTok-Captions für Stream-Highlights mit maximaler Watchtime und Kommentaren.
Erzeuge genau 5 Hook-Varianten in verschiedenen Stilen (curiosity, contrast, open_question, shock, story),
bewerte jede (score 1-10) und wähle die stärkste (best_index). Hooks: max. 110 Zeichen, konkret, kein Clickbait, das
nicht eingelöst wird, keine Spoiler der Pointe, max. 1 Emoji. on_screen_hook: max. 6 Wörter, Großbuchstaben-tauglich.
3-5 Hashtags (ohne Credit), Mischung aus Nische (Spiel/Streamer/Thema) und Reichweite. Sprache = Sprache des Streams."""


def _call(system, content, schema, effort="medium"):
    r = client.beta.messages.create(
        model=MODEL, max_tokens=16000, system=system,
        messages=[{"role": "user", "content": content}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        betas=["server-side-fallback-2026-07-01"], fallbacks="default",
    )
    if r.stop_reason == "refusal":
        log.warning("refusal: %s", getattr(r, "stop_details", None))
        return None
    if r.stop_reason == "max_tokens":
        log.warning("max_tokens erreicht")
        return None
    return json.loads(next(b.text for b in r.content if b.type == "text"))


def judge(streamer, frames, transcript, features, win_len):
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                            "data": base64.standard_b64encode(open(f, "rb").read()).decode()}}
               for _, f in frames]
    content.append({"type": "text", "text": json.dumps({
        "streamer": streamer["name"], "plattform": streamer["platform"], "kategorie": streamer["category"],
        "sprache": streamer["language"], "fensterlaenge_s": win_len,
        "keyframe_zeitpunkte_s": [round(t, 1) for t, _ in frames],
        "signal_scores_0_1": features, "transkript": transcript or "(kein Sprachinhalt)",
    }, ensure_ascii=False)})
    return _call(JUDGE_SYS, content, JUDGE_SCHEMA)


def caption(streamer, analysis, transcript):
    styles = learned()["hook_styles"]
    pref = sorted(styles, key=lambda k: -styles[k])
    txt = json.dumps({
        "sprache": streamer["language"], "streamer": streamer["name"], "kategorie": streamer["category"],
        "analyse": analysis, "transkript_ausschnitt": transcript[:3000],
        "stil_performance_ranking": pref,
        "hinweis": "Bei gleichwertigen Varianten den Stil mit besserem Performance-Ranking wählen.",
    }, ensure_ascii=False)
    r = _call(CAPTION_SYS, [{"type": "text", "text": txt}], CAPTION_SCHEMA)
    if not r or not r["variants"]:
        return None
    best = r["variants"][max(0, min(r["best_index"], len(r["variants"]) - 1))]
    tags = ["#" + t.lstrip("#").replace(" ", "") for t in r["hashtags"][:5]]
    return {"hook": best["hook"], "style": best["style"], "on_screen": r["on_screen_hook"],
            "hashtags": tags, "variants": r["variants"],
            "text": f"{best['hook']}\n\n{streamer['credit']}\n{' '.join(tags)}"}
