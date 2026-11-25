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


class SetReview(BaseModel):
    verdicts: list[SetVerdict]
    findings: list[SetFinding]


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


class Final(BaseModel):
    questions: list[Question]
    development_rationale: str
    testing_rationale: str
    fix_first: list[FixItem]
    overall: str = Field(description="2-4 предложения: общий вывод о документе")


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
