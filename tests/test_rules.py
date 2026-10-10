from datetime import date

from sunuchat import rules


def test_red_flags_detected():
    flags = rules.detect_red_flags("Mon enfant a de la fièvre et des convulsions depuis ce matin")
    assert "convulsions" in flags


def test_no_red_flag_on_simple_question():
    assert rules.detect_red_flags("Comment entretenir la moustiquaire ?") == []


def test_parse_date_formats():
    assert rules.parse_date("2026-03-01") == date(2026, 3, 1)
    assert rules.parse_date("01/03/2026") == date(2026, 3, 1)


def test_vaccine_schedule_next_dose():
    s = rules.vaccine_schedule(date(2026, 1, 1), today=date(2026, 2, 1))
    assert s["prochain"]["age"] == "6 semaines"
    assert "Hexavalent 1" in s["prochain"]["vaccins"]
    assert len(s["deja_passes_a_verifier_sur_le_carnet"]) == 1
