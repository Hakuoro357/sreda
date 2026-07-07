"""#285 M1 (канарейка 2026-07-07): read-кюс на обзорные/множественные формы own-data чтений.

Канарейка показала (MINOR субагента): «какие у меня дела»/«какие списки»/«план на неделю» роутились
в checklists, но read_cue_domains требовал притяжательности/«список дел» → allowed_read=[web] → own-data
тул не биндился → модель уточняла, а не показывала. M1 расширяет обзорные формы.

КРИТИЧНО: анти-идиома «как дела?» ОСТАЁТСЯ пустой (иначе вернётся баг канарейки: смолток → чеклисты).
Триггеры новых форм — «какие/покажи/мои/наши/все» (НЕ «как»), поэтому «как дела?» не матчит.
"""

from __future__ import annotations

from sreda.runtime.react_signals import read_cue_domains


# ─────────── обзорные «дела» → checklists ───────────
def test_overview_dela_forms_open_checklists():
    for t in ("какие у меня дела", "какие дела", "покажи мои дела", "покажи дела",
              "какие там дела", "покажи какие у меня дела"):
        assert "checklists" in read_cue_domains(t), t


# ─────────── обзорные «списки» → checklists+shopping ───────────
def test_overview_spisok_forms_open_lists():
    for t in ("какие списки", "покажи все списки", "мои списки", "какие у меня списки",
              "покажи мои списки"):
        d = read_cue_domains(t)
        assert "checklists" in d and "shopping" in d, (t, d)


# ─────────── «план на <время>» → checklists ───────────
def test_plan_na_time_opens_checklists():
    for t in ("план на неделю", "какой план на сегодня", "планы на завтра", "план на месяц"):
        assert "checklists" in read_cue_domains(t), t


# ─────────── АНТИ-РЕГРЕСС: идиома «как дела?» остаётся ПУСТОЙ (не own-data) ───────────
def test_kak_dela_stays_idiom_empty():
    for t in ("как дела?", "как дела", "как у тебя дела", "как твои дела", "ну как дела"):
        assert read_cue_domains(t) == frozenset(), (t, read_cue_domains(t))


# ─────────── «как <притяж.> дела» = ПРОСЬБА показать задачи (Борис 2026-07-07), НЕ смолток ───────────
def test_kak_possessive_dela_opens_checklists():
    # Владелец: «как мои дела»/«как там мои дела» — просьба показать ЗАДАЧИ (притяж. «мои/наши»),
    # не приветствие. Ошибочный `(?<!как\s)` снят: НЕ подавлять эти формы (приняли просьбу за смолток).
    for t in ("как мои дела", "как там мои дела", "как наши дела", "как вообще мои дела"):
        assert "checklists" in read_cue_domains(t), (t, read_cue_domains(t))


# ─────────── прочие негативы не заглатываются обзорными формами ───────────
def test_other_negatives_stay_clean():
    # «как жизнь» / «что нового» — смолток, не own-data
    for t in ("как жизнь", "что нового", "как настроение"):
        assert read_cue_domains(t) == frozenset(), (t, read_cue_domains(t))
