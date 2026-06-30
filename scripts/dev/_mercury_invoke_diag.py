import io,os,sys,time,base64,tempfile
from pathlib import Path
_REPO=Path(__file__).resolve().parents[2]
os.environ["SREDA_INCEPTION_API_KEY_FILE"]=str(_REPO/".secrets"/"inception.txt")
os.environ["SREDA_ENCRYPTION_KEY"]=base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode()
sys.stdout=io.TextIOWrapper(sys.stdout.buffer,encoding="utf-8",errors="replace")
from sreda.config.settings import get_settings; get_settings.cache_clear()
from sreda.services.llm import get_chat_llm
from langchain_core.messages import HumanMessage
prompt=(Path(tempfile.gettempdir())/"full_prompt.txt").read_text(encoding="utf-8")
print(f"промпт символов: {len(prompt)}")
llm=get_chat_llm(provider="inception-mercury2")
print("max_retries:", getattr(llm,"max_retries",None), "| timeout:", getattr(llm,"request_timeout",None), "| temp:", getattr(llm,"temperature",None))
t0=time.monotonic(); r=llm.invoke([HumanMessage(prompt)]); dt=time.monotonic()-t0
print(f"ОДИН invoke: {dt:.2f}с | ответ: {str(r.content)[:60]!r}")
