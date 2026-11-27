"""Схемы ответов модели (JSON) и внутренние структуры отчёта."""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from .grounding import Anchor

# ---------- ответы модели ----------


class ExtractedRequirement(BaseModel):
    doc_id: str = Field(description="Идентификатор в документе (Ф-1, п. 3, строка таблицы); пусто, если номера нет")
    section: str = Field(description="Раздел документа, где находится требование")
    quote: str = Field(description="Дословная цитата требования из документа")
    summary: str = Field(description="Краткая формулировка требования, до 15 слов")
    kind: str = Field(description="functional | nonfunctional | data | integration | interface | api | acceptance | business | constraint")


class SkippedFragment(BaseModel):
    section: str
    reason: str = Field(description="Почему это не требование (метаданные, шаблон, пояснение)")


class Segmentation(BaseModel):
    requirements: list[ExtractedRequirement]
    skipped: list[SkippedFragment]


class CriterionVerdict(BaseModel):
    criterion: str = Field(description="Код критерия")
    verdict: str = Field(description="ok | violation | na")
    comment: str = Field(description="Короткое обоснование со ссылкой на текст")


class RawFinding(BaseModel):
    criterion: str = Field(description="Код нарушенного критерия")
    severity: str = Field(description="critical | major | minor")
    quotes: list[str] = Field(description="Дословные фрагменты документа, на которые опирается нарушение")
    problem: str = Field(description="Что не так и почему: конкретно, с контрпримером или последствием")
    fix: str = Field(description="Как исправить: предлагаемая формулировка или что добавить")


class RequirementReview(BaseModel):
    req_id: str
    verdicts: list[CriterionVerdict]
    findings: list[RawFinding]


class RequirementBatch(BaseModel):
    reviews: list[RequirementReview]


class SetFinding(BaseModel):
    criterion: str
    severity: str
    req_ids: list[str] = Field(description="Затронутые требования (ТР-xx); пусто, если относится к документу целиком")
    quotes: list[str]
    problem: str
    fix: str


class SetVerdict(BaseModel):
    criterion: str
    verdict: str = Field(description="ok | partial | violation")
    comment: str


class AcceptanceCheck(BaseModel):
    acceptance_id: str = Field(description="ID примера приёмки ТР-xx")
    req_id: str = Field(description="ID проверяемого требования ТР-xx или пусто, если связь не установлена")
    status: str = Field(description="agrees | conflict | unclear")
    expected: str = Field(description="Ожидаемый результат по правилу для условий этого примера")
    stated: str = Field(description="Результат, указанный в примере")
    question: str = Field(description="Вопрос для разрешения расхождения или неопределённости, иначе пусто")


class SetReview(BaseModel):
    verdicts: list[SetVerdict]
    findings: list[SetFinding]


class ConsistencyReview(SetReview):
    acceptance_checks: list[AcceptanceCheck]


class CoverageRow(BaseModel):
    req_id: str = Field(description="ТР-xx функционального требования")
    acceptance_ids: list[str] = Field(description="ТР-xx критериев приёмки, которые его проверяют; пусто, если таких нет")
    missing_scenarios: list[str] = Field(description="Сценарии этого требования, которые не покрыты ни одним критерием приёмки")


class CompletenessReview(BaseModel):
    verdicts: list[SetVerdict]
    findings: list[SetFinding]
    coverage: list[CoverageRow] = Field(description="Покрытие каждого функционального требования критериями приёмки")


class VerifiedItem(BaseModel):
    finding_id: str
    keep: bool
    severity: str = Field(description="Итоговая серьёзность: critical | major | minor")
    duplicate_of: str = Field(description="ID находки, которую эта дублирует; пусто, если не дубль")
    reason: str = Field(description="Почему оставлена/отклонена/изменена серьёзность")


class Verification(BaseModel):
    items: list[VerifiedItem]


class DuplicateGroup(BaseModel):
    keep: str = Field(description="ID основной находки группы")
    duplicates: list[str] = Field(description="ID находок, которые описывают тот же дефект")
    reason: str


class Dedup(BaseModel):
    groups: list[DuplicateGroup]


class Question(BaseModel):
    priority: str = Field(description="blocking | important | nice")
    addressee: str = Field(description="Кому задать вопрос: Бизнес-заказчик | Аналитик | Архитектор | Разработка процессинга | ИБ | Тестирование | Юристы/комплаенс")
    req_ids: list[str]
    finding_ids: list[str]
    quote: str = Field(description="Дословный фрагмент документа, к которому относится вопрос")
    question: str
    why: str = Field(description="Что блокирует отсутствие ответа")
    options: list[str] = Field(description="2-3 варианта ответа, из которых заказчик может выбрать")


class Clarification(BaseModel):
    questions: list[Question]


class FixItem(BaseModel):
    title: str
    finding_ids: list[str]
    action: str


class ChecklistAnswer(BaseModel):
    item: str = Field(description="Пункт чек-листа дословно")
    answer: str = Field(description="Да | Нет | Не требуется")
    rationale: str = Field(description="Обоснование одним-двумя предложениями со ссылкой на находки или текст")
    finding_ids: list[str]


class Final(BaseModel):
    questions: list[Question]
    checklist: list[ChecklistAnswer] = Field(description="Ответы на пункты чек-листа ревью из документа; пусто, если чек-листа нет")
    development_rationale: str
    testing_rationale: str
    fix_first: list[FixItem]
    overall: str = Field(description="2-4 предложения: общий вывод о документе")


class Resolution(BaseModel):
    finding_id: str
    status: str = Field(description="closed (ответ устраняет дефект) | open (не устраняет) | changed (дефект остаётся, но меняется суть или серьёзность)")
    question_ids: list[str] = Field(description="В-n, ответы на которые повлияли")
    severity: str = Field(description="Серьёзность после ответа: critical | major | minor")
    note: str = Field(description="Что решил ответ и что осталось сделать в документе")


class AnswerReview(BaseModel):
    resolutions: list[Resolution]


class DocChange(BaseModel):
    finding_ids: list[str]
    change: str = Field(description="Что изменено в документе")


class FixedDocument(BaseModel):
    markdown: str = Field(description="Полный исправленный документ в Markdown")
    changes: list[DocChange]
    open_points: list[str] = Field(description="Что не удалось исправить без решения заказчика (помечено [УТОЧНИТЬ: ...])")


class DocEdit(BaseModel):
    line: int = Field(description="Номер строки документа, где начинается заменяемый фрагмент")
    old_text: str = Field(description="Фрагмент документа дословно, как в тексте (без номера строки), "
                                      "достаточно длинный, чтобы встречаться один раз; пусто, если текст вставляется после строки line")
    new_text: str = Field(description="Чем заменить фрагмент (или что вставить после строки line)")
    reason: str = Field(description="Коротко: что исправляет правка, со ссылкой на ID находки")


class AnswerDraft(BaseModel):
    question_id: str = Field(description="ID вопроса заказчику: В-1, В-2…")
    answer: str = Field(description="Предлагаемый ответ: «Вариант N: …» из списка вариантов или свой текст")
    basis: str = Field(description="На чём основан ответ: строка документа или «допущение, нужно подтверждение заказчика»")


class AssistantReply(BaseModel):
    answer: str = Field(description="Ответ пользователю по-русски: коротко, по делу, со ссылками на строки и ID находок")
    suggested_text: str = Field(description="Текст для показа (тест-кейсы, пример формулировки); пусто, если не нужен "
                                            "или если всё уже есть в edits")
    edits: list[DocEdit] = Field(default_factory=list,
                                 description="Правки рабочей редакции документа; только если пользователь просит исправить, "
                                             "переписать, разбить или дополнить документ")
    answers: list[AnswerDraft] = Field(default_factory=list,
                                       description="Черновики ответов на вопросы заказчику; только если пользователь просит "
                                                   "помочь с ответами")


class StandardCriterionDraft(BaseModel):
    code: str = Field(description="Код латиницей snake_case; для критериев набора начинается с set_")
    scope: str = Field(description="requirement (проверяется каждое требование) или set (весь набор)")
    name: str = Field(description="Название критерия по-русски, 1-4 слова")
    short: str = Field(description="Короткое имя для колонки таблицы, до 8 символов")
    source: str = Field(description="Пункт или раздел стандарта, на который ссылается критерий")
    question: str = Field(description="Что проверяет агент: 1-3 предложения, когда нарушение и когда ставить na")


class StandardDraft(BaseModel):
    id: str = Field(description="Идентификатор латиницей snake_case, 2-40 символов")
    title: str = Field(description="Краткое название стандарта")
    cite: str = Field(description="Полная ссылка для отчёта: название, год, разделы")
    description: str = Field(description="Одно предложение: что добавляет стандарт к проверке")
    criteria: list[StandardCriterionDraft]


class Conclusion(BaseModel):
    development_rationale: str
    testing_rationale: str
    fix_first: list[FixItem]
    overall: str = Field(description="2-4 предложения: общий вывод о документе")


# ---------- внутренние структуры ----------


@dataclass
class Requirement:
    rid: str  # ТР-01
    doc_id: str
    section: str
    summary: str
    kind: str
    anchor: Anchor
    quote: str  # точный фрагмент документа (маскированный)


@dataclass
class Finding:
    fid: str  # Н-01
    scope: str  # requirement | set
    criterion: str
    severity: str
    req_ids: list[str]
    anchors: list[Anchor]
    problem: str
    fix: str
    source: str  # llm | rule
    kept: bool = True
    verifier_note: str = ""
    duplicate_of: str = ""
    dropped_quotes: list[str] = field(default_factory=list)
    orig_id: str = ""  # ID до перенумерации (как в журнале запросов)
    resolution: str = ""  # closed | changed (после ответов на вопросы)
    resolution_note: str = ""
    verification_status: str = ""  # confirmed | rejected | unreviewed; пусто в старых отчётах
