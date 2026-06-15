"""Smoke: реально ли отвечает NVIDIA NIM через get_chat_llm (endpoint+ключ+модель+проводка)."""
import io, os, sys
from pathlib import Path
_REPO = Path(__file__).resolve().parents[2]
os.environ["SREDA_NVIDIA_API_KEY_FILE"] = str(_REPO / ".secrets" / "nvidia.txt")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from sreda.config.settings import get_settings
get_settings.cache_clear()
from sreda.services.llm import get_chat_llm

for prov in ["nvidia-nemotron-nano", "nvidia-llama-nemotron-49b", "nvidia-nemotron-super"]:
    print(f"\n=== {prov} ===")
    llm = get_chat_llm(provider=prov)
    print("llm built:", llm is not None, "model:", getattr(llm, "model_name", None) or getattr(llm, "model", None))
    if llm is None:
        continue
    try:
        r = llm.invoke("Ответь одним словом: работает?")
        txt = r.content if isinstance(r.content, str) else str(r.content)
        print("resp:", txt[:120])
    except Exception as e:
        print("ERROR:", str(e)[:200])
