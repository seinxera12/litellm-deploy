"""
stt-fastwhisper — dedicated FastAPI server calling faster-whisper directly.

OpenAI-compatible transcription endpoint (POST /v1/audio/transcriptions) so
LiteLLM only needs an api_base change to route here. Built for long-form
(60min+) audio with a BatchedInferencePipeline and hallucination-suppression
defaults. The model is loaded once at startup (lifespan), never per request.

Startup-fixed:  model / device / compute_type  (env-configurable)
Per-request overridable via the multipart form body:
    batch_size, language, condition_on_previous_text,
    vad_filter, vad min_silence_duration_ms, vad speech_pad_ms, temperature
"""

import inspect
import logging
import os
import tempfile
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("stt-fastwhisper")

# ─── Startup-fixed configuration (env-overridable, model loaded once) ─────────
MODEL_NAME = os.getenv("FW_MODEL", "large-v3")
DEVICE = os.getenv("FW_DEVICE", "cuda")
COMPUTE_TYPE = os.getenv("FW_COMPUTE_TYPE", "float16")

# ─── Per-request defaults (overridable via the request body) ──────────────────
DEFAULT_BATCH_SIZE = int(os.getenv("FW_BATCH_SIZE", "16"))
DEFAULT_CONDITION_ON_PREVIOUS_TEXT = False
DEFAULT_VAD_FILTER = True
DEFAULT_VAD_MIN_SILENCE_MS = 500
DEFAULT_VAD_SPEECH_PAD_MS = 300

# Populated at startup.
STATE: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    from faster_whisper import BatchedInferencePipeline, WhisperModel

    log.info(
        "Loading faster-whisper model=%s device=%s compute_type=%s",
        MODEL_NAME, DEVICE, COMPUTE_TYPE,
    )
    model = WhisperModel(MODEL_NAME, device=DEVICE, compute_type=COMPUTE_TYPE)
    batched = BatchedInferencePipeline(model=model)
    # Cache the accepted kwargs so per-request overrides stay version-safe
    # across faster-whisper releases (e.g. condition_on_previous_text support
    # differs between batched/sequential paths).
    accepted = set(inspect.signature(batched.transcribe).parameters)
    STATE.update(model=model, batched=batched, accepted=accepted)
    log.info("Model loaded. Transcribe accepts: %s", sorted(accepted))
    yield
    STATE.clear()


app = FastAPI(title="stt-fastwhisper", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL_NAME, "loaded": "batched" in STATE}


def _format_timestamp(seconds: float, comma: bool = False) -> str:
    ms = int(round(seconds * 1000.0))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    sep = "," if comma else "."
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def _render(segments: list, info, response_format: str):
    text = "".join(s["text"] for s in segments).strip()

    if response_format == "text":
        return PlainTextResponse(text)

    if response_format == "srt":
        lines = []
        for i, s in enumerate(segments, 1):
            lines.append(str(i))
            lines.append(
                f"{_format_timestamp(s['start'], comma=True)} --> "
                f"{_format_timestamp(s['end'], comma=True)}"
            )
            lines.append(s["text"].strip())
            lines.append("")
        return PlainTextResponse("\n".join(lines))

    if response_format == "vtt":
        lines = ["WEBVTT", ""]
        for s in segments:
            lines.append(
                f"{_format_timestamp(s['start'])} --> {_format_timestamp(s['end'])}"
            )
            lines.append(s["text"].strip())
            lines.append("")
        return PlainTextResponse("\n".join(lines))

    if response_format == "verbose_json":
        return JSONResponse({
            "task": "transcribe",
            "language": info.language,
            "duration": info.duration,
            "text": text,
            "segments": [
                {
                    "id": i,
                    "seek": 0,
                    "start": s["start"],
                    "end": s["end"],
                    "text": s["text"],
                    "avg_logprob": s.get("avg_logprob"),
                    "compression_ratio": s.get("compression_ratio"),
                    "no_speech_prob": s.get("no_speech_prob"),
                }
                for i, s in enumerate(segments)
            ],
        })

    # Default: json
    return JSONResponse({"text": text})


@app.post("/v1/audio/transcriptions")
async def transcribe(
    file: UploadFile = File(...),
    model: Optional[str] = Form(None),          # accepted for OpenAI compat; model is fixed at startup
    response_format: str = Form("json"),
    language: Optional[str] = Form(None),
    temperature: Optional[float] = Form(None),
    prompt: Optional[str] = Form(None),
    # ── faster-whisper overridable knobs ──
    batch_size: int = Form(DEFAULT_BATCH_SIZE),
    condition_on_previous_text: bool = Form(DEFAULT_CONDITION_ON_PREVIOUS_TEXT),
    vad_filter: bool = Form(DEFAULT_VAD_FILTER),
    vad_min_silence_duration_ms: int = Form(DEFAULT_VAD_MIN_SILENCE_MS),
    vad_speech_pad_ms: int = Form(DEFAULT_VAD_SPEECH_PAD_MS),
):
    if "batched" not in STATE:
        raise HTTPException(status_code=503, detail="Model not loaded yet")

    if model and model not in (MODEL_NAME, f"Systran/faster-whisper-{MODEL_NAME}"):
        log.info("Request asked for model=%r; serving fixed model=%r", model, MODEL_NAME)

    # Persist upload to a temp file — PyAV decoding wants a seekable path.
    suffix = os.path.splitext(file.filename or "")[1] or ".bin"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        candidate = {
            "batch_size": batch_size,
            "language": language,
            "task": "transcribe",
            "initial_prompt": prompt,
            "condition_on_previous_text": condition_on_previous_text,
            "vad_filter": vad_filter,
            "vad_parameters": {
                "min_silence_duration_ms": vad_min_silence_duration_ms,
                "speech_pad_ms": vad_speech_pad_ms,
            },
        }
        if temperature is not None:
            candidate["temperature"] = temperature

        # Keep only kwargs this faster-whisper build actually accepts.
        accepted = STATE["accepted"]
        kwargs = {k: v for k, v in candidate.items() if k in accepted and v is not None}

        log.info("Transcribing %s (%s) with %s", file.filename, response_format, kwargs)
        segments_iter, info = STATE["batched"].transcribe(tmp_path, **kwargs)

        segments = [
            {
                "start": s.start,
                "end": s.end,
                "text": s.text,
                "avg_logprob": getattr(s, "avg_logprob", None),
                "compression_ratio": getattr(s, "compression_ratio", None),
                "no_speech_prob": getattr(s, "no_speech_prob", None),
            }
            for s in segments_iter
        ]
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    return _render(segments, info, response_format)
