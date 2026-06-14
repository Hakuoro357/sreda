"""Карта возможностей Среды — автогенератор (внутренний тулинг, lean v1).

Назначение: один документ, отвечающий на вопрос «что Среда УЖЕ умеет?» —
чтобы перед планом/реализацией проверить «нет ли готового механизма?» (прецедент
#143: и я, и Codex предложили СТРОИТЬ resolver-слой, хотя finder `find_by_title`
и self-resolve чек-листов уже были в коде), и чтобы класть выжимку ревьюеру,
который кода не видит.

Источники (из КОДА → не гниёт):
  1. Реестр инструментов планировщика — обход `MIGRATED_TOOL_SPECS` (импорт безопасен,
     без БД/сети), статусы ответа через `composer.presenters.build_valid_status_map`.
  2. `lookup_mode` — ЯВНАЯ курируемая метка (НЕ угадывание по имени поля): как
     инструмент находит объект-цель. Ключи сверяются с реальными именами инструментов
     (ловит переименование). Незакурированные инструменты честно показаны отдельным
     списком (карта не даёт ложной уверенности).
  3. Object-finders (`find_*_by_title`) — через AST доменных сервисов (без импорта:
     side-effects/БД), отделены от infra-резолверов (provider/auth) по domain-allowlist.
  4. Drift-секция — сверка `ToolSpec.name` ↔ `TOOL_FAMILY_MANIFEST` + прогон
     `validate_tool_registry_quality`: карта не должна врать при registry↔manifest рассинхроне.

Ручной слой — `capabilities.manual.toml` рядом с этим скриптом: 5-10 сквозных
механизмов с symbol+snippet-якорями. Генератор РЕЗОЛВИТ каждый якорь по AST и ПАДАЕТ
на пропаже символа/сниппета (line-number — advisory, перегенерится сам). Формат TOML
(не YAML, как в плане): читается stdlib `tomllib`, БЕЗ новой зависимости.

Свежесть: `--check` падает, если документ разошёлся с кодом (гонять в CI/review-gate —
это и есть гарантия). pre-commit hook (scripts/dev/hooks/pre-commit) — лишь удобство.

Команды:
  python scripts/dev/gen_capabilities.py            # перегенерить документ
  python scripts/dev/gen_capabilities.py --check    # упасть, если документ устарел
  python scripts/dev/gen_capabilities.py --domain tasks   # выжимка по домену → stdout
"""

from __future__ import annotations

import argparse
import ast
import difflib
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# Пути (скрипт лежит в <repo>/scripts/dev/)
# --------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"
MANUAL_PATH = Path(__file__).resolve().parent / "capabilities.manual.toml"
DOC_PATH = REPO_ROOT / "docs" / "internal" / "dev" / "SREDA_CAPABILITIES.md"

# Импорт пакета sreda работает и без editable-install.
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# --------------------------------------------------------------------------
# Маркеры секций (рендер полностью перегенерим; маркеры — самопроверка структуры)
# --------------------------------------------------------------------------
GEN_BEGIN = "<!-- BEGIN:GENERATED (из кода — не править руками) -->"
GEN_END = "<!-- END:GENERATED -->"
MAN_BEGIN = "<!-- BEGIN:MANUAL (рендер из capabilities.manual.toml) -->"
MAN_END = "<!-- END:MANUAL -->"

# --------------------------------------------------------------------------
# lookup_mode — КУРИРУЕМАЯ метка. Значения и их смысл (легенда в шапке документа).
# Курируем ТОЛЬКО подтверждённое по коду; остальное честно «—» + список незакурированных.
# --------------------------------------------------------------------------
LOOKUP_ID_ONLY = "id_only"  # оперирует по заранее известному id; сам название НЕ резолвит
LOOKUP_SELF_TITLE = "self_resolves_title"  # принимает название/фрагмент, резолвит внутри (finder)
LOOKUP_FREE_TEXT = "free_text_search"  # свободный поисковый запрос
LOOKUP_PRIOR_LIST = "needs_prior_list"  # список-кандидатов: для операции по одному нужен prior-list → .only
LOOKUP_UNKNOWN = "unknown"  # AST/реестр не дал распознать (advisory, не молчим)

LOOKUP_MODE_LEGEND: dict[str, str] = {
    LOOKUP_ID_ONLY: "оперирует по заранее известному id; объект по названию НЕ резолвит "
                    "— название резолвится выше (prior-list → .only)",
    LOOKUP_SELF_TITLE: "принимает название/фрагмент и резолвит объект сам "
                       "(через find_*_by_title)",
    LOOKUP_FREE_TEXT: "свободный текстовый поиск/запрос",
    LOOKUP_PRIOR_LIST: "список-кандидатов: чтобы оперировать по одному, нужен "
                       "паттерн prior-list → .only",
    LOOKUP_UNKNOWN: "не удалось распознать (см. drift-секцию)",
}

# Только подтверждённое по коду (specs_tasks/specs_checklists/specs_recipes + Explore-разведка).
LOOKUP_MODES: dict[str, str] = {
    # tasks: операции — по task_id (см. specs_tasks.py complete/cancel/delete/update)
    "complete_task": LOOKUP_ID_ONLY,
    "uncomplete_task": LOOKUP_ID_ONLY,
    "cancel_task": LOOKUP_ID_ONLY,
    "delete_task": LOOKUP_ID_ONLY,
    "update_task": LOOKUP_ID_ONLY,
    # checklists: #143 Phase B — mark/delete теперь СТРОГО по item_id
    # (id из list_checklist_items → .only); название НЕ резолвят сами.
    "mark_checklist_item_done": LOOKUP_ID_ONLY,
    "delete_checklist_item": LOOKUP_ID_ONLY,
    # archive — по list_id_or_title (find_list_by_title внутри).
    "archive_checklist": LOOKUP_SELF_TITLE,
    # списки-продюсеры кандидатов (нужен .only после)
    "list_tasks": LOOKUP_PRIOR_LIST,
    # #143 Phase B: пункты «по описанию» во всех списках → .only → mark/delete.
    "list_checklist_items": LOOKUP_PRIOR_LIST,
    "list_checklists": LOOKUP_PRIOR_LIST,
    "list_shopping": LOOKUP_PRIOR_LIST,
    "list_reminders": LOOKUP_PRIOR_LIST,
    "list_family_members": LOOKUP_PRIOR_LIST,
    "list_menu": LOOKUP_PRIOR_LIST,
    # свободный поиск
    "search_recipes": LOOKUP_FREE_TEXT,
    "recall_memory": LOOKUP_FREE_TEXT,
    "web_search": LOOKUP_FREE_TEXT,
}

# Доменные сервисы, в которых ищем object-finders (allowlist; отделяет от infra-резолверов).
FINDER_FILES = (
    "src/sreda/services/tasks.py",
    "src/sreda/services/checklists.py",
    "src/sreda/services/housewife_reminders.py",
)
# object-finder = функция/метод, резолвящая доменный объект по названию.
_FINDER_NAME_PREFIXES = ("find_",)
_FINDER_NAME_MARKERS = ("by_title", "find_by_title")


# ==========================================================================
# Сбор: инструменты планировщика
# ==========================================================================
@dataclass
class ToolRow:
    name: str
    family: str
    effect: str
    durable_write: bool
    lookup_mode: str
    input_fields: list[str]
    statuses: list[str]
    source: str  # модуль-источник (stem)


def collect_tools() -> list[ToolRow]:
    from sreda.services.composer.presenters import build_valid_status_map
    from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS

    status_map = build_valid_status_map(MIGRATED_TOOL_SPECS)
    rows: list[ToolRow] = []
    for spec in MIGRATED_TOOL_SPECS:
        try:
            fields = list(spec.input_model.model_fields.keys())
        except Exception:  # noqa: BLE001
            fields = []
        source = getattr(spec.input_model, "__module__", "?").rsplit(".", 1)[-1]
        rows.append(
            ToolRow(
                name=spec.name,
                family=str(spec.family) if spec.family else "—",
                effect=spec.effect,
                durable_write=bool(spec.is_durable_write),
                lookup_mode=LOOKUP_MODES.get(spec.name, "—"),
                input_fields=fields,
                statuses=sorted(status_map.get(spec.name, frozenset())),
                source=source,
            )
        )
    return rows


# ==========================================================================
# Сбор: object-finders через AST (без импорта сервисов)
# ==========================================================================
@dataclass
class FinderRow:
    name: str
    owner: str  # класс-владелец или "—"
    domain: str  # stem файла
    signature: str
    returns: str
    doc_first_line: str
    ambiguity: str  # подсказка по политике неоднозначности, если видно в docstring
    lineno: int


_AMBIGUITY_HINTS = ("перв", "свеж", "earliest", "first", "most recent", "best match",
                    "ровно", "только один", "single")


def _is_finder_name(name: str) -> bool:
    if not name.startswith(_FINDER_NAME_PREFIXES):
        return False
    return any(m in name for m in _FINDER_NAME_MARKERS) or name == "find_by_title"


def _doc_first_line(node: ast.AST) -> str:
    doc = ast.get_docstring(node) or ""
    for line in doc.splitlines():
        s = line.strip()
        if s:
            return s
    return ""


def _ambiguity_hint(node: ast.AST) -> str:
    doc = (ast.get_docstring(node) or "").lower()
    for h in _AMBIGUITY_HINTS:
        if h in doc:
            # вернуть короткую фразу из docstring, где встретился маркер
            for line in (ast.get_docstring(node) or "").splitlines():
                if h in line.lower():
                    return line.strip()
    return ""


def collect_finders() -> list[FinderRow]:
    rows: list[FinderRow] = []
    for rel in FINDER_FILES:
        path = REPO_ROOT / rel
        if not path.exists():
            continue
        domain = path.stem
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_finder_name(child.name):
                    rows.append(_finder_row(child, owner=node.name, domain=domain))
        # модуль-уровневые finder'ы (на случай, если появятся вне классов)
        for child in tree.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_finder_name(child.name):
                rows.append(_finder_row(child, owner="—", domain=domain))
    rows.sort(key=lambda r: (r.domain, r.owner, r.name))
    return rows


def _finder_row(node: ast.FunctionDef | ast.AsyncFunctionDef, *, owner: str, domain: str) -> FinderRow:
    try:
        sig = ast.unparse(node.args)
    except Exception:  # noqa: BLE001
        sig = "(?)"
    returns = ast.unparse(node.returns) if node.returns is not None else "—"
    return FinderRow(
        name=node.name,
        owner=owner,
        domain=domain,
        signature=sig,
        returns=returns,
        doc_first_line=_doc_first_line(node),
        ambiguity=_ambiguity_hint(node),
        lineno=node.lineno,
    )


# ==========================================================================
# Сбор: drift / рассинхрон (warnings)
# ==========================================================================
@dataclass
class DriftReport:
    spec_not_in_manifest: list[str] = field(default_factory=list)
    manifest_not_in_specs: list[str] = field(default_factory=list)
    quality_violations: list[str] = field(default_factory=list)
    lookup_mode_key_unknown: list[str] = field(default_factory=list)  # ключ LOOKUP_MODES без живого инструмента
    lookup_uncurated: list[str] = field(default_factory=list)  # инструмент без курированного lookup_mode

    @property
    def clean(self) -> bool:
        return not (self.spec_not_in_manifest or self.manifest_not_in_specs
                    or self.quality_violations or self.lookup_mode_key_unknown)


def compute_drift() -> DriftReport:
    from sreda.services.tool_schemas.families import TOOL_FAMILY_MANIFEST
    from sreda.services.tool_schemas.registry_quality import validate_tool_registry_quality
    from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS

    spec_names = {s.name for s in MIGRATED_TOOL_SPECS}
    manifest_names = set(TOOL_FAMILY_MANIFEST.keys())
    rep = DriftReport()
    rep.spec_not_in_manifest = sorted(spec_names - manifest_names)
    rep.manifest_not_in_specs = sorted(manifest_names - spec_names)

    for v in validate_tool_registry_quality(MIGRATED_TOOL_SPECS, strict=True):
        loc = f"{v.tool_name}{(':' + v.field_path) if v.field_path else ''}"
        rep.quality_violations.append(f"{loc} — {v.code}: {v.message}")
    rep.quality_violations.sort()

    # ключ LOOKUP_MODES, не соответствующий живому инструменту = drift (переименование/опечатка)
    rep.lookup_mode_key_unknown = sorted(set(LOOKUP_MODES) - spec_names)
    # инструменты без курированного lookup_mode — честно показать (не молчать)
    rep.lookup_uncurated = sorted(spec_names - set(LOOKUP_MODES))
    return rep


# ==========================================================================
# Ручной слой: разбор TOML + резолв symbol+snippet якорей (fail-loud)
# ==========================================================================
@dataclass
class Anchor:
    path: str
    symbol: str
    required_snippet: str
    resolved_line: int = 0


@dataclass
class Mechanism:
    id: str
    summary: str
    why_use: str
    known_traps: str
    anchors: list[Anchor]


class AnchorError(RuntimeError):
    """Якорь ручного слоя не резолвится (символ или сниппет отсутствует) — fail-loud."""


def _find_named_node(tree: ast.Module, symbol: str) -> ast.AST | None:
    """Найти def/class/присваивание с именем symbol (на любом уровне вложенности)."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == symbol:
                return node
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == symbol:
                    return node
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == symbol:
                return node
    return None


def resolve_anchor(anchor: Anchor) -> None:
    """Резолвит symbol+snippet; кидает AnchorError на пропаже. Заполняет resolved_line."""
    path = REPO_ROOT / anchor.path
    if not path.exists():
        raise AnchorError(f"якорь: файл не найден: {anchor.path}")
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:  # pragma: no cover
        raise AnchorError(f"якорь: не разобрать {anchor.path}: {exc}") from exc
    node = _find_named_node(tree, anchor.symbol)
    if node is None:
        raise AnchorError(
            f"якорь: символ '{anchor.symbol}' не найден в {anchor.path} "
            f"(переименован/удалён?)"
        )
    segment = ast.get_source_segment(source, node) or ""
    if anchor.required_snippet not in segment:
        raise AnchorError(
            f"якорь: сниппет {anchor.required_snippet!r} не найден в теле "
            f"'{anchor.symbol}' ({anchor.path}) — механизм мог быть выпотрошен"
        )
    anchor.resolved_line = getattr(node, "lineno", 0)


def load_manual() -> list[Mechanism]:
    if not MANUAL_PATH.exists():
        raise AnchorError(f"нет ручного слоя: {MANUAL_PATH}")
    data = tomllib.loads(MANUAL_PATH.read_text(encoding="utf-8"))
    mechs: list[Mechanism] = []
    seen_ids: set[str] = set()
    for raw in data.get("mechanism", []):
        mid = str(raw.get("id", "")).strip()
        if not mid:
            raise AnchorError("механизм без id в capabilities.manual.toml")
        if mid in seen_ids:
            raise AnchorError(f"дублирующийся id механизма: {mid}")
        seen_ids.add(mid)
        anchors = [
            Anchor(
                path=str(a["path"]),
                symbol=str(a["symbol"]),
                required_snippet=str(a["required_snippet"]),
            )
            for a in raw.get("anchors", [])
        ]
        if not anchors:
            raise AnchorError(f"механизм '{mid}' без якорей")
        mechs.append(
            Mechanism(
                id=mid,
                summary=str(raw.get("summary", "")).strip(),
                why_use=str(raw.get("why_use", "")).strip(),
                known_traps=str(raw.get("known_traps", "")).strip(),
                anchors=anchors,
            )
        )
    if not mechs:
        raise AnchorError("ручной слой пуст (нет ни одного [[mechanism]])")
    for m in mechs:
        for a in m.anchors:
            resolve_anchor(a)  # fail-loud
    return mechs


# ==========================================================================
# Рендер
# ==========================================================================
def _md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render(tools: list[ToolRow], finders: list[FinderRow],
           drift: DriftReport, mechanisms: list[Mechanism]) -> str:
    from sreda.services.tool_schemas.families import FAMILIES

    out: list[str] = []
    out.append("# Карта возможностей Среды")
    out.append("")
    out.append("> **АВТОГЕНЕРАЦИЯ — не править руками.** Источник: `scripts/dev/gen_capabilities.py`")
    out.append("> (из кода) + `scripts/dev/capabilities.manual.toml` (ручной слой). Перегенерация:")
    out.append("> `python scripts/dev/gen_capabilities.py`. Свежесть в CI/ревью: `--check`.")
    out.append("> Внутренний тулинг — НЕ часть продукта.")
    out.append("")
    out.append("Зачем: перед планом проверить «нет ли в Среде готового механизма?» (прецедент #143 —")
    out.append("предлагали строить resolver, хотя `find_by_title` уже был) и класть выжимку ревьюеру.")
    out.append("")

    out.append(GEN_BEGIN)
    out.append("")
    # --- легенда lookup_mode ---
    out.append("## Легенда `lookup_mode`")
    out.append("")
    for key in (LOOKUP_ID_ONLY, LOOKUP_SELF_TITLE, LOOKUP_FREE_TEXT, LOOKUP_PRIOR_LIST, LOOKUP_UNKNOWN):
        out.append(f"- **`{key}`** — {LOOKUP_MODE_LEGEND[key]}")
    out.append("")

    # --- инструменты по family ---
    out.append(f"## Инструменты планировщика ({len(tools)})")
    out.append("")
    by_family: dict[str, list[ToolRow]] = {}
    for t in tools:
        by_family.setdefault(t.family, []).append(t)
    family_order = list(FAMILIES) + [f for f in by_family if f not in FAMILIES]
    for fam in family_order:
        fam_rows = by_family.get(fam)
        if not fam_rows:
            continue
        out.append(f"### {fam} ({len(fam_rows)})")
        out.append("")
        out.append("| инструмент | effect | lookup_mode | входные поля | статусы ответа | источник |")
        out.append("|---|---|---|---|---|---|")
        for t in fam_rows:
            eff = t.effect + (" · durable" if t.durable_write else "")
            inp = ", ".join(t.input_fields) if t.input_fields else "—"
            sts = ", ".join(t.statuses) if t.statuses else "—"
            out.append(
                f"| `{t.name}` | {eff} | `{t.lookup_mode}` | {_md_escape(inp)} "
                f"| {_md_escape(sts)} | {t.source} |"
            )
        out.append("")

    # --- object-finders ---
    out.append(f"## Object-finders (резолв доменного объекта по названию) — {len(finders)}")
    out.append("")
    out.append("Готовые механизмы «найти объект по названию/фрагменту». Перед тем как строить")
    out.append("новый resolver — проверь, нет ли подходящего здесь (урок #143).")
    out.append("")
    out.append("| finder | владелец | домен | сигнатура | возвращает | неоднозначность | назначение |")
    out.append("|---|---|---|---|---|---|---|")
    for f in finders:
        amb = _md_escape(f.ambiguity) if f.ambiguity else "—"
        out.append(
            f"| `{f.name}` | {f.owner} | {f.domain}:{f.lineno} | `{_md_escape(f.signature)}` "
            f"| `{_md_escape(f.returns)}` | {amb} | {_md_escape(f.doc_first_line)} |"
        )
    out.append("")
    out.append("> Infra-резолверы (provider/auth/config: `resolve_mimo_api_key`, `resolve_outbox_routings`,")
    out.append("> `resolve_tenant_from_*` и т.п.) НЕ входят в карту object-возможностей — они вне scope lean v1.")
    out.append("")

    # --- drift ---
    out.append("## Drift / рассинхрон")
    out.append("")
    if drift.clean and not drift.lookup_uncurated:
        out.append("Рассинхрон не обнаружен.")
        out.append("")
    else:
        if drift.spec_not_in_manifest:
            out.append("**ToolSpec без записи в `TOOL_FAMILY_MANIFEST`:**")
            for n in drift.spec_not_in_manifest:
                out.append(f"- `{n}`")
            out.append("")
        if drift.manifest_not_in_specs:
            out.append("**В манифесте, но не мигрировано в реестр:**")
            for n in drift.manifest_not_in_specs:
                out.append(f"- `{n}`")
            out.append("")
        if drift.lookup_mode_key_unknown:
            out.append("**Ключ `LOOKUP_MODES` без живого инструмента (переименование/опечатка):**")
            for n in drift.lookup_mode_key_unknown:
                out.append(f"- `{n}`")
            out.append("")
        if drift.quality_violations:
            out.append("**`validate_tool_registry_quality` (strict):**")
            for v in drift.quality_violations:
                out.append(f"- {v}")
            out.append("")
        if drift.lookup_uncurated:
            out.append(f"**Без курированного `lookup_mode` ({len(drift.lookup_uncurated)})** "
                       f"— карта не выдаёт по ним ложной уверенности (`lookup_mode = —`):")
            out.append("")
            out.append("> " + ", ".join(f"`{n}`" for n in drift.lookup_uncurated))
            out.append("")

    out.append(GEN_END)
    out.append("")

    # --- ручной слой ---
    out.append(MAN_BEGIN)
    out.append("")
    out.append("## Сквозные механизмы (ручной слой)")
    out.append("")
    out.append("Якоря резолвятся по AST при генерации; `--check` падает, если символ/сниппет ушёл.")
    out.append("")
    for m in mechanisms:
        out.append(f"### {m.id}")
        out.append("")
        if m.summary:
            out.append(m.summary)
            out.append("")
        if m.why_use:
            out.append(f"**Зачем:** {m.why_use}")
            out.append("")
        if m.known_traps:
            out.append(f"**Грабли:** {m.known_traps}")
            out.append("")
        out.append("Якоря:")
        for a in m.anchors:
            out.append(f"- `{a.path}:{a.resolved_line}` → `{a.symbol}` "
                       f"(инвариант: `{a.required_snippet}`)")
        out.append("")
    out.append(MAN_END)
    out.append("")

    text = "\n".join(out)
    _assert_markers(text)
    return text


def _assert_markers(text: str) -> None:
    """Самопроверка структуры: ровно по одному маркеру каждого вида."""
    for marker in (GEN_BEGIN, GEN_END, MAN_BEGIN, MAN_END):
        n = text.count(marker)
        if n != 1:
            raise AnchorError(f"маркер встречается {n} раз (ожидалось 1): {marker}")


# ==========================================================================
# --domain extractor (выжимка для ревьюера)
# ==========================================================================
def extract_domain(needle: str, tools: list[ToolRow], finders: list[FinderRow],
                   mechanisms: list[Mechanism]) -> str:
    low = needle.lower()
    out: list[str] = [f"# Выжимка карты возможностей по запросу: «{needle}»", ""]

    tool_hits = [t for t in tools
                 if low in t.name.lower() or low == t.family.lower()
                 or any(low in f.lower() for f in t.input_fields)]
    out.append(f"## Инструменты ({len(tool_hits)})")
    for t in tool_hits:
        out.append(f"- `{t.name}` [{t.family}] lookup_mode=`{t.lookup_mode}` "
                   f"вход: {', '.join(t.input_fields) or '—'}")
    out.append("")

    finder_hits = [f for f in finders if low in f.name.lower() or low in f.domain.lower()
                   or low in f.doc_first_line.lower()]
    out.append(f"## Object-finders ({len(finder_hits)})")
    for f in finder_hits:
        out.append(f"- `{f.name}` ({f.domain}:{f.lineno}) → {f.returns}: {f.doc_first_line}")
    out.append("")

    mech_hits = [m for m in mechanisms
                 if low in m.id.lower() or low in m.summary.lower()
                 or any(low in a.path.lower() or low in a.symbol.lower() for a in m.anchors)]
    out.append(f"## Механизмы ({len(mech_hits)})")
    for m in mech_hits:
        out.append(f"- {m.id}: {m.summary}")
        for a in m.anchors:
            out.append(f"    - {a.path}:{a.resolved_line} `{a.symbol}`")
    return "\n".join(out)


# ==========================================================================
# main
# ==========================================================================
def build_document() -> str:
    tools = collect_tools()
    finders = collect_finders()
    drift = compute_drift()
    mechanisms = load_manual()  # fail-loud на якорях
    return render(tools, finders, drift, mechanisms)


def main(argv: list[str] | None = None) -> int:
    # Windows-консоль по умолчанию cp1251 — print() Cyrillic/«→»/«—» падает с
    # UnicodeEncodeError. Файл пишется utf-8 отдельно; здесь — только вывод в консоль.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 — старый поток без reconfigure
            pass

    parser = argparse.ArgumentParser(description="Генератор карты возможностей Среды (lean v1).")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true",
                       help="не писать; упасть (rc=1), если документ разошёлся с кодом")
    group.add_argument("--domain", metavar="NEEDLE",
                       help="выжимка по домену/инструменту/полю → stdout (для ревьюера)")
    args = parser.parse_args(argv)

    if args.domain:
        tools = collect_tools()
        finders = collect_finders()
        mechanisms = load_manual()
        print(extract_domain(args.domain, tools, finders, mechanisms))
        return 0

    try:
        doc = build_document()
    except AnchorError as exc:
        print(f"[gen_capabilities] ОШИБКА: {exc}", file=sys.stderr)
        return 2

    if args.check:
        current = DOC_PATH.read_text(encoding="utf-8") if DOC_PATH.exists() else ""
        if current == doc:
            print("[gen_capabilities] --check: документ актуален.")
            return 0
        diff = difflib.unified_diff(
            current.splitlines(keepends=True), doc.splitlines(keepends=True),
            fromfile="SREDA_CAPABILITIES.md (на диске)", tofile="ожидаемый (из кода)",
        )
        sys.stderr.writelines(diff)
        print("\n[gen_capabilities] --check: документ УСТАРЕЛ. "
              "Перегенери: python scripts/dev/gen_capabilities.py", file=sys.stderr)
        return 1

    DOC_PATH.parent.mkdir(parents=True, exist_ok=True)
    DOC_PATH.write_text(doc, encoding="utf-8")
    print(f"[gen_capabilities] записано: {DOC_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
