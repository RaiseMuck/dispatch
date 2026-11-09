"""Qwen field guesses against a fake Ollama (no network beyond 127.0.0.1, no real model)."""
import json
import time

from dispatch import qwen
from dispatch.areas import UNSORTED

DEAD_URL = "http://127.0.0.1:9"
GOOD = '{"area": "Coreflow/CLARA", "payoff": 4, "likelihood": 0.8, "estimate_minutes": 45}'


def test_valid_json_gives_all_four_fields(fake_ollama):
    url, seen = fake_ollama(lambda m, r: GOOD)
    g = qwen.guess_fields("call Sam at Acme Plumbing", "pilot", base_url=url)
    assert (g.area, g.payoff, g.likelihood, g.estimate_minutes) == ("Coreflow/CLARA", 4, 0.8, 45)
    assert g.guessed == ["area", "payoff", "likelihood", "estimate_minutes"]
    first = qwen.QWEN_PREFERENCE[0]
    assert g.model == first and seen[0]["model"] == first
    req = seen[0]
    assert req["format"] == "json" and req["think"] is False and req["stream"] is False
    assert req["keep_alive"] == qwen.KEEP_ALIVE and req["options"]["temperature"] == 0
    assert 'To-do: "call Sam at Acme Plumbing"' in req["prompt"] and "Coreflow/CLARA" in req["prompt"]
    assert g.info()["guess"] == {"area": "Coreflow/CLARA", "payoff": 4, "likelihood": 0.8,
                                 "estimate_minutes": 45}


def test_partial_and_garbage_fields_fall_back_individually(fake_ollama):
    url, _ = fake_ollama(lambda m, r: '{"area": "banana", "payoff": 11, "likelihood": "high", '
                                      '"estimate_minutes": 0}')
    g = qwen.guess_fields("x", base_url=url)
    assert (g.area, g.payoff, g.likelihood, g.estimate_minutes) == (UNSORTED, 5, 0.7, 5)
    assert g.guessed == ["payoff", "estimate_minutes"]       # only the ones that parsed


def test_percent_likelihood_and_think_wrapper(fake_ollama):
    url, _ = fake_ollama(lambda m, r: '<think>hmm {"no": 1</think> sure: {"area": "school", '
                                      '"payoff": "2", "likelihood": 70, "estimate_minutes": 90.4}')
    g = qwen.guess_fields("quiz", base_url=url)
    assert (g.area, g.payoff, g.likelihood, g.estimate_minutes) == ("School", 2, 0.7, 90)


def test_slow_first_model_falls_back_to_the_second(fake_ollama):
    first, second = qwen.QWEN_PREFERENCE

    def respond(model, req):
        if model == first:
            time.sleep(1.5)
        return GOOD

    url, seen = fake_ollama(respond)
    g = qwen.guess_fields("x", base_url=url, timeouts=(0.5, 3.0))
    assert [r["model"] for r in seen] == [first, second]
    assert g.model == second and g.area == "Coreflow/CLARA"


def test_invalid_json_from_both_models_gives_defaults(fake_ollama):
    url, seen = fake_ollama(lambda m, r: "I think it is School")
    g = qwen.guess_fields("x", base_url=url)
    assert len(seen) == 2 and g.guessed == [] and g.model is None
    assert (g.area, g.payoff, g.likelihood, g.estimate_minutes) == (UNSORTED, 3, 0.7, 30)


def test_both_down_is_fast_and_safe():
    t0 = time.monotonic()
    g = qwen.guess_fields("x", base_url=DEAD_URL, timeouts=(1.0, 1.0), connect_timeout=1.0)
    assert time.monotonic() - t0 < 3
    assert g.guessed == [] and g.model is None and g.area == UNSORTED


def test_server_error_is_survived(fake_ollama):
    def boom(m, r):
        raise RuntimeError
    url, _ = fake_ollama(boom)
    assert qwen.guess_fields("x", base_url=url).guessed == []


def test_extract_json_edge_cases():
    assert qwen._extract_json('x {"a": "}"} y') == {"a": "}"}
    assert qwen._extract_json("[1,2]") is None
    assert qwen._extract_json("{broken") is None
    assert qwen._extract_json(None) is None
    assert json.dumps(qwen._extract_json('{"a": {"b": 1}}')) == '{"a": {"b": 1}}'


def test_qwen_model_env_goes_first(fake_ollama, monkeypatch):
    url, seen = fake_ollama(lambda m, r: GOOD)
    monkeypatch.setenv("QWEN_MODEL", "qwen3.5:4b")
    assert qwen.choose_models(url) == ["qwen3.5:4b", "qwen3.5:27b"]
    monkeypatch.setenv("QWEN_MODEL", "not-installed")
    assert qwen.choose_models(url)[0] == qwen.QWEN_PREFERENCE[0]


# ---- Phase 2.1: place / energy, backfill, split proposals --------------------------------
GOOD6 = ('{"area": "Personal admin", "payoff": 3, "likelihood": 0.9, "estimate_minutes": 45, '
         '"place": "Out", "energy": "light"}')


def test_place_and_energy_are_guessed_with_the_other_fields(fake_ollama):
    url, seen = fake_ollama(lambda m, r: GOOD6)
    g = qwen.guess_fields("return the Strimer at the store", base_url=url)
    assert (g.place, g.energy) == ("out", "light")
    assert g.guessed == ["area", "payoff", "likelihood", "estimate_minutes", "place", "energy"]
    assert qwen.needs_kwargs(g) == {"place": "out", "energy": "light"}
    assert '"out" = he must physically go somewhere' in seen[0]["prompt"]
    assert g.info()["guess"]["place"] == "out"


def test_bad_place_or_energy_defaults_and_is_not_guessed(fake_ollama):
    url, _ = fake_ollama(lambda m, r: '{"place": "car", "energy": 3}')
    g = qwen.guess_fields("x", base_url=url)
    assert (g.place, g.energy) == ("home", "light") and g.guessed == []
    assert qwen.needs_kwargs(g) == {}


def test_backfill_is_one_call_and_drops_bad_rows(fake_ollama):
    answer = json.dumps({"items": [{"id": 1, "place": "out", "energy": "light"},
                                   {"id": "#2", "place": "phone", "energy": "nope"},
                                   {"id": 99, "place": "home", "energy": "light"},
                                   {"id": 3, "place": "moon", "energy": "fuzzy"}]})
    url, seen = fake_ollama(lambda m, r: answer)
    got = qwen.guess_needs([(1, "Strimer return"), (2, "Call Sam"), (3, "x")], base_url=url)
    assert got == {1: ("out", "light"), 2: ("phone", None)}
    assert len(seen) == 1 and seen[0]["options"]["num_predict"] == 20 + 30 * 3
    assert "#2 Call Sam" in seen[0]["prompt"]


def test_backfill_down_is_empty():
    assert qwen.guess_needs([(1, "x")], base_url=DEAD_URL, timeouts=(1, 1),
                            connect_timeout=1) == {}
    assert qwen.guess_needs([], base_url=DEAD_URL) == {}


def test_propose_step_validates_and_clamps(fake_ollama):
    url, seen = fake_ollama(lambda m, r: '{"first_step": " Outline the three sections. ", '
                                         '"minutes": 45}')
    assert qwen.propose_step("PC workbench", None, 180, base_url=url) == \
        ("Outline the three sections", 20)
    assert "FIRST concrete step" in seen[0]["prompt"] and seen[0]["options"]["num_predict"] == 80
    url, _ = fake_ollama(lambda m, r: '{"first_step": ""}')
    assert qwen.propose_step("x", base_url=url) is None
    assert qwen.propose_step("x", base_url=DEAD_URL, timeouts=(1, 1), connect_timeout=1) is None
