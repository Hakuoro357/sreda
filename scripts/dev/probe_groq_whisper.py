"""Live probe: generate a Russian TTS sample via edge-tts (no API key
required) and transcribe it through Groq Whisper. Measures latency
end-to-end and prints the reference-vs-transcript diff so we can
eyeball Russian quality on synthetic-but-realistic speech.

Run from repo root:

    python scripts/dev/probe_groq_whisper.py

Skips cleanly if either edge-tts or the Groq key is missing.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


SAMPLES = [
    "Привет! Добавь молоко и хлеб в список покупок.",
    "Сохрани рецепт борща: свёкла, капуста, мясо, сметана. Варить на медленном огне сорок минут.",
    "Напомни мне завтра в девять утра забрать заказ.",
    "Что у меня сейчас запланировано на среду?",
    "Перехотел хлеб, убери из списка. И добавь яйца десять штук.",
]


async def _synthesise(text: str, out_path: Path) -> None:
    import edge_tts

    # Russian female voice — natural prosody, close to Telegram mobile
    # user timbre for a fair STT stress test.
    communicate = edge_tts.Communicate(text, "ru-RU-SvetlanaNeural", rate="+0%")
    await communicate.save(str(out_path))


async def main() -> int:
    key_path = Path(".secrets/groq_api_key.txt")
    if not key_path.exists():
        print("[!] .secrets/groq_api_key.txt not found — aborting")
        return 2

    try:
        import edge_tts  # noqa: F401 — availability check
    except ImportError:
        print("[!] edge-tts not installed — run: pip install edge-tts")
        return 3

    from sreda.services.speech.groq import GroqWhisperRecognizer

    api_key = key_path.read_text(encoding="utf-8").strip()
    rec = GroqWhisperRecognizer(api_key=api_key)

    tmp_dir = Path(".runtime/groq_probe")
    tmp_dir.mkdir(parents=True, exist_ok=True)

    for idx, ref in enumerate(SAMPLES, start=1):
        mp3_path = tmp_dir / f"sample_{idx}.mp3"
        print(f"\n=== Sample {idx} ===")
        print(f"ref: {ref}")

        # Synthesise
        t0 = time.monotonic()
        await _synthesise(ref, mp3_path)
        tts_dt = time.monotonic() - t0
        size_kb = mp3_path.stat().st_size / 1024
        print(f"[TTS] {tts_dt:.2f}s → {size_kb:.1f} KB ({mp3_path.suffix})")

        audio = mp3_path.read_bytes()

        # Transcribe
        t0 = time.monotonic()
        try:
            got = await rec.recognize(audio, lang="ru-RU")
            stt_dt = time.monotonic() - t0
            print(f"[STT] {stt_dt:.2f}s")
            print(f"got: {got}")
            # naive diff hint
            if got.strip().lower().rstrip(".!?") == ref.strip().lower().rstrip(".!?"):
                print("match: EXACT ✓")
            else:
                print("match: close — spot-check manually")
        except Exception as exc:  # noqa: BLE001
            stt_dt = time.monotonic() - t0
            print(f"[STT] {stt_dt:.2f}s ERR: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
