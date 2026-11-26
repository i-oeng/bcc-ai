import pytest

from validator.redact import RedactionLeak, Redactor, iban_ok, iin_ok, luhn_ok


def test_checksums():
    assert luhn_ok("4111111111111111")
    assert not luhn_ok("4111111111111112")
    assert not iin_ok("12345678901")  # 11 цифр
    assert iban_ok("KZ86125KZT5004100100")
    assert not iban_ok("KZ86125KZT5004100101")


def _valid_iin() -> str:
    base = "90010130001"
    for d in range(10):
        if iin_ok(base + str(d)):
            return base + str(d)
    raise AssertionError("не нашли валидный ИИН")


def test_redact_and_restore():
    iin = _valid_iin()
    text = (
        "Клиент Иванов И.И. (ИИН " + iin + ") платит картой 4111 1111 1111 1111, "
        "счёт KZ86125KZT5004100100, тел. +7 701 123 45 67, почта ivanov@bank.kz. "
        "Порог 50 000 ₸, ответ 404, токен Bearer abcdefghijklmnopqrstuvwxyz123456."
    )
    r = Redactor(use_ner=False)
    masked = r.redact(text)
    for secret in ["4111 1111 1111 1111", iin, "KZ86125KZT5004100100", "701 123 45 67", "ivanov@bank.kz",
                   "Иванов И.И.", "abcdefghijklmnopqrstuvwxyz123456"]:
        assert secret not in masked
    # суммы и коды не трогаем
    assert "50 000 ₸" in masked and "404" in masked
    assert "[КАРТА_1]" in masked
    assert r.restore(masked) == text
    r.guard(masked)  # не должно падать


def test_same_value_same_placeholder():
    r = Redactor(use_ner=False)
    a = r.redact("карта 4111111111111111")
    b = r.redact("ещё раз 4111 1111 1111 1111")
    assert "[КАРТА_1]" in a and "[КАРТА_1]" in b


def test_guard_blocks_leak():
    r = Redactor(use_ner=False)
    with pytest.raises(RedactionLeak):
        r.guard("отправляю карту 4111111111111111 в модель")


def test_amounts_are_not_cards():
    r = Redactor(use_ner=False)
    text = "Порог от 1 000 до 1 000 000 ₸ включительно, расходы 48 500 ₸, лимит 2147483647."
    assert r.redact(text) == text


def test_custom_terms():
    r = Redactor(use_ner=False, extra_terms=["CardProc-Core"])
    assert "CardProc-Core" not in r.redact("Событие приходит из CardProc-Core.")


def test_query_verbs_are_not_names_but_names_still_are():
    """Вопрос помощнику: «Предложи…» не ФИО; имя и «Фамилия И.О.» маскируются; guard сравнивает слова целиком."""
    from validator.redact import RedactionLeak, Redactor

    r = Redactor()
    assert r.redact_query("Предложи исправленную формулировку требования.") == "Предложи исправленную формулировку требования."
    masked = r.redact_query("Что сказал Иван Петров? Согласовано: Ахметова А.К.")
    assert "Иван Петров" not in masked and "Ахметова" not in masked
    r.guard("Предложить исправление")  # «предложи» внутри другого слова не утечка
    try:
        r.guard("Ответ для Иван Петров")
        raise AssertionError("guard пропустил имя")
    except RedactionLeak:
        pass


def test_common_word_tagged_as_name_does_not_block_document():
    """NER иногда принимает термин за фамилию («Кешбэк по категориям»). Если то же слово встречается
    со строчной, это не имя: документ не маскируется по ошибке и guard не блокирует проверку."""
    text = ("# Кешбэк по выбранным категориям\n\n"
            "Кешбэк за месяц начисляется на бонусный счёт.\n"
            "Если клиент не выбрал категории, кешбэк начисляется по прошлому месяцу.\n")
    r = Redactor()
    masked = r.redact(text)
    assert "[ФИО" not in masked
    r.guard(masked)


def test_found_name_is_masked_everywhere_and_trimmed():
    """Имя маскируется во всех вхождениях, включая ячейки таблиц, где NER его не видит;
    разделители ячеек не попадают в значение. Одиночная фамилия не задевает то же слово со строчной."""
    text = ("Согласовал Иванов Пётр Сергеевич, начальник отдела.\n"
            "| Информационная безопасность | Иванов Пётр Сергеевич | |\n"
            "| ИВАНОВ ПЁТР СЕРГЕЕВИЧ |\n")
    r = Redactor()
    masked = r.redact(text)
    assert "Иванов" not in masked and "ИВАНОВ" not in masked
    assert set(r.backward.values()) <= {"Иванов Пётр Сергеевич", "ИВАНОВ ПЁТР СЕРГЕЕВИЧ"}
    r.guard(masked)

    r2 = Redactor(use_ner=False)
    r2.placeholder("person", "Роза")
    assert r2.redact("Цветок роза. Подписала Роза.") == "Цветок роза. Подписала [ФИО_1]."


def test_fake_documents_mask_cleanly():
    """Учебные документы samples/fakes: кешбэк-документ без ложных ФИО, в PDF с ПДн всё замаскировано."""
    from pathlib import Path

    from validator.loader import load

    root = Path(__file__).resolve().parent.parent / "samples" / "fakes"
    r = Redactor()
    masked = r.redact(load(str(root / "cashback.docx")).text)
    assert "[ФИО" not in masked
    r.guard(masked)

    r = Redactor()
    masked = r.redact(load(str(root / "phone_transfer.pdf")).text)
    r.guard(masked)
    for leak in ("Иванов", "Нурланов", "Ахметова", "4111 1111", "555 12 34", "example.kz"):
        assert leak not in masked, leak
    assert all("|" not in v for v in r.backward.values())
