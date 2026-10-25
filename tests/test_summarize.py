"""Tests for the faithfulness check and the summarizer fallback logic."""
import pytest

from src import summarize as sm

LOC = {
    "name": "Praha",
    "has_data": True,
    "totals": {"hours": 48, "open_meteo_mm": 3.2, "met_no_mm": 2.0, "diff_mm": 1.2, "max_prob_pct": 70},
    "daily": [
        {"date": "2026-10-26", "hours": 24, "open_meteo_mm": 2.0, "met_no_mm": 1.5, "diff_mm": 0.5},
        {"date": "2026-10-27", "hours": 24, "open_meteo_mm": 1.2, "met_no_mm": 0.5, "diff_mm": 0.7},
    ],
    "peaks": [{"t": "2026-10-26T12:00:00Z", "local_date": "2026-10-26", "local_time": "14:00",
               "open_meteo_mm": 1.2, "met_no_mm": 0.8}],
}


@pytest.fixture
def facts():
    from datetime import date
    return sm.build_facts(LOC, date(2026, 10, 26))


# ---- faithfulness check

def test_faithful_text_passes(facts):
    text = "Open-Meteo expects 3.2 mm over 48 hours, peaking tomorrow at 14:00."
    assert sm.check_faithfulness(text, facts) == []


def test_invented_number_is_reported(facts):
    assert sm.check_faithfulness("Expect 7.5 mm of rain.", facts) == [7.5]


def test_number_formatting_does_not_matter(facts):
    assert sm.check_faithfulness("About 3.20 mm.", facts) == []  # 3.20 == 3.2


def test_clock_time_must_match_exactly(facts):
    assert sm.check_faithfulness("Peak at 14:00.", facts) == []
    assert sm.check_faithfulness("Peak at 15:00.", facts) == ["15:00"]


def test_text_without_numbers_passes(facts):
    assert sm.check_faithfulness("It will be a wet day.", facts) == []


# ---- template summarizer

def test_template_is_deterministic_and_faithful(facts):
    text = sm.template_summary(facts)
    assert text == sm.template_summary(facts)
    assert sm.check_faithfulness(text, facts) == []
    assert "3.2 mm" in text and "14:00" in text


def test_template_dry_case():
    dry = {**LOC, "totals": {**LOC["totals"], "open_meteo_mm": 0.0, "met_no_mm": 0.0, "diff_mm": 0.0},
           "peaks": []}
    from datetime import date
    facts = sm.build_facts(dry, date(2026, 10, 26))
    text = sm.template_summary(facts)
    assert text.startswith("No rain is expected")
    assert sm.check_faithfulness(text, facts) == []


def test_template_with_one_source_missing():
    one = {**LOC, "totals": {**LOC["totals"], "met_no_mm": None}}
    from datetime import date
    facts = sm.build_facts(one, date(2026, 10, 26))
    text = sm.template_summary(facts)
    assert "only Open-Meteo data" in text
    assert sm.check_faithfulness(text, facts) == []


# ---- summarize(): LLM answer is accepted or replaced by the template

SETTINGS = {"ollama": {"url": "http://localhost:11434", "model": "test-model"}}


def test_llm_answer_is_used_when_faithful(facts, monkeypatch):
    monkeypatch.setattr(sm, "ollama_summary", lambda f, c: "Open-Meteo expects 3.2 mm. MET Norway expects 2.0 mm.")
    out = sm.summarize(facts, "ollama", SETTINGS)
    assert out["ai_generated"] is True and out["generated_by"] == "ollama:test-model"


def test_llm_answer_with_invented_number_falls_back(facts, monkeypatch):
    monkeypatch.setattr(sm, "ollama_summary", lambda f, c: "Heavy rain of 9.9 mm is on the way.")
    out = sm.summarize(facts, "ollama", SETTINGS)
    assert out["ai_generated"] is False and out["text"] == sm.template_summary(facts)


def test_llm_unreachable_falls_back(facts, monkeypatch):
    def boom(f, c):
        raise ConnectionError("no ollama here")
    monkeypatch.setattr(sm, "ollama_summary", boom)
    out = sm.summarize(facts, "ollama", SETTINGS)
    assert out["ai_generated"] is False and out["generated_by"] == "template"


def test_template_mode_never_calls_ollama(facts, monkeypatch):
    monkeypatch.setattr(sm, "ollama_summary", lambda f, c: pytest.fail("should not be called"))
    assert sm.summarize(facts, "template")["ai_generated"] is False


def test_unknown_mode_is_an_error(facts):
    with pytest.raises(ValueError):
        sm.summarize(facts, "gpt")
