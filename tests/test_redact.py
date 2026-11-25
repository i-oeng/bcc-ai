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
