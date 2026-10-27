from mannerism import stylometrics as sm


def test_features_basics():
    f = sm.features("lol ok, that's fine... see you tomorrow 😂 — maybe!")
    assert f["words"] >= 8 and f["starts_lower"] and f["all_lower"] and f["em_dash"] == 1
    assert f["emoji"] == 1 and f["laugh"] >= 2 and f["ellipsis"] == 1 and f["exclaim"] == 1 and f["hedge"] == 1
    assert f["contraction_per100w"] > 0 and f["ends_with_punct"]


def test_script_mix_is_generic():
    mix = sm.script_mix("hello привет мир こんにちは")
    assert mix["latin"] > 0 and mix["cyrillic"] > 0 and mix["hiragana"] > 0
    assert abs(sum(mix.values()) - 1) < 0.02
    assert sm.script_mix("123 !!!") == {}


def test_vector_and_distance():
    short, long_ = sm.vector(sm.features("ok")), sm.vector(sm.features("This is a much longer sentence, with commas, "
                                                                        "and a question? Really! " * 4))
    assert len(short) == len(long_) and sm.distance(short, short) == 0 and sm.distance(short, long_) > 0.5
    assert all(0 <= x <= 1.5 for x in long_)
