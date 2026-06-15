"""Найти верный способ ВЫКЛЮЧИТЬ reasoning у NVIDIA Nemotron (NIM) — эмпирически.
Меряем латентность + смотрим, не сыплет ли модель reasoning. Провайдер nano.
"""
import io, os, sys, time
from pathlib import Path
_REPO = Path(__file__).resolve().parents[2]
os.environ["SREDA_NVIDIA_API_KEY_FILE"] = str(_REPO / ".secrets" / "nvidia.txt")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from sreda.config.settings import get_settings
get_settings.cache_clear()
from sreda.services.llm import get_chat_llm
from langchain_core.messages import SystemMessage, HumanMessage

PROMPT = ("Пользователь просит: «сохрани рецепт борща: свёкла, капуста, картошка, "
          "варить 40 минут». Верни КОРОТКО JSON-план из одного шага save_recipe.")
PROV = "nvidia-nemotron-nano"

def run(label, build_kwargs=None, messages=None):
    llm = get_chat_llm(provider=PROV, **(build_kwargs or {}))
    if llm is None:
        print(f"[{label}] llm=None"); return
    msgs = messages or [HumanMessage(PROMPT)]
    t0 = time.monotonic()
    try:
        r = llm.invoke(msgs)
        dt = time.monotonic() - t0
        txt = r.content if isinstance(r.content, str) else str(r.content)
        rc = r.additional_kwargs.get("reasoning_content") if hasattr(r, "additional_kwargs") else None
        usage = getattr(r, "usage_metadata", None) or getattr(r, "response_metadata", {}).get("token_usage", {})
        out_tok = (usage or {}).get("output_tokens") or (usage or {}).get("completion_tokens")
        tps = (out_tok / dt) if (out_tok and dt) else None
        print(f"[{label}] {dt:.1f}s out_tok={out_tok} tps={tps and round(tps,1)} "
              f"reasoning={'ДА(' + str(len(str(rc))) + ')' if rc else 'нет'} | resp: {txt[:90]!r}")
    except Exception as e:
        print(f"[{label}] ERROR {time.monotonic()-t0:.1f}s: {str(e)[:160]}")

print(f"### reasoning-off smoke на {PROV}")
run("A baseline (reasoning ON)")
run("B chat_template_kwargs.thinking=false",
    build_kwargs={"extra_body": {"chat_template_kwargs": {"thinking": False}}})
run("C system 'detailed thinking off'",
    messages=[SystemMessage("detailed thinking off"), HumanMessage(PROMPT)])
run("D system '/no_think'",
    messages=[SystemMessage("/no_think"), HumanMessage(PROMPT)])
