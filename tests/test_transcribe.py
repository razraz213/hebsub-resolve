"""Tests for the transcribe module.

Only the parts that need no audio. Per D14 the engine-facing behaviour is
covered by running the real fixtures; what is unit-testable is the mapping
from engine output onto the contract.
"""

from __future__ import annotations

from hebsub.transcribe import _join_geresh_fragments, _join_number_fragments


def word(text, start, end, conf=0.9):
    return {"w": text, "start": start, "end": end, "conf": conf}


class TestNumberFragments:
    """faster-whisper splits `75,000` into `75` and `,000`.

    Left alone, "75,000 שקל" reaches the screen as "75 ,000 שקל" with a card
    boundary free to fall between them -- and every price, date and statistic
    is exposed to it.
    """

    def test_thousands_separator_is_rejoined(self):
        got = _join_number_fragments([
            word("75", 4.74, 5.02), word(",000", 5.02, 5.50),
            word("שקל", 5.50, 5.90),
        ])
        assert [w["w"] for w in got] == ["75,000", "שקל"]

    def test_the_joined_word_spans_both_halves(self):
        got = _join_number_fragments([
            word("75", 4.74, 5.02), word(",000", 5.02, 5.50),
        ])
        assert got[0]["start"] == 4.74
        assert got[0]["end"] == 5.50

    def test_confidence_takes_the_worse_half(self):
        got = _join_number_fragments([
            word("75", 4.74, 5.02, 0.99), word(",000", 5.02, 5.50, 0.42),
        ])
        assert got[0]["conf"] == 0.42

    def test_a_decimal_point_joins_too(self):
        got = _join_number_fragments([
            word("3", 0.0, 0.2), word(".5", 0.2, 0.4),
        ])
        assert [w["w"] for w in got] == ["3.5"]

    def test_a_plain_comma_after_a_number_is_left_alone(self):
        # "יש 5, אבל" -- the comma is punctuation, not a separator
        got = _join_number_fragments([
            word("5", 0.0, 0.2), word(",", 0.2, 0.3), word("אבל", 0.3, 0.6),
        ])
        assert [w["w"] for w in got] == ["5", ",", "אבל"]

    def test_a_comma_after_a_word_is_left_alone(self):
        got = _join_number_fragments([
            word("שקל", 0.0, 0.3), word(",000", 0.3, 0.6),
        ])
        assert len(got) == 2

    def test_words_without_numbers_pass_through_untouched(self):
        words = [word("שלום", 0.0, 0.3), word("עולם", 0.3, 0.6)]
        assert _join_number_fragments(words) == words

    def test_the_input_list_is_not_mutated(self):
        words = [word("75", 0.0, 0.2), word(",000", 0.2, 0.4)]
        before = [dict(w) for w in words]
        _join_number_fragments(words)
        assert words == before


class TestGereshFragments:
    """faster-whisper splits `ג'יפ` into `ג` and `'יפ`.

    The geresh marks a sound Hebrew has no letter for -- ג' is *j*, צ' is
    *ch*, ז' is *zh* -- so it sits inside the word, not between two. The
    engine treats it as punctuation and breaks there, which turns every
    ג'יפ, ג'וב and צ'ק into two tokens with a card boundary free to fall
    between them. Measured on Raz's tools timeline: 8 of 81 word errors,
    the single largest cause in the file.
    """

    def test_a_split_geresh_word_is_rejoined(self):
        got = _join_geresh_fragments([
            word("ג", 1.0, 1.2), word("'יפ", 1.2, 1.5),
        ])
        assert [w["w"] for w in got] == ["ג'יפ"]

    def test_an_attached_prefix_still_joins(self):
        # "לג'יפ", "הג'יפ", "בג'יפ" -- Hebrew glues prepositions on, so the
        # fragment left behind is prefix + letter, not a bare letter.
        for prefix in ("ל", "ה", "ב", "מ", "ש", "כ", "ו"):
            got = _join_geresh_fragments([
                word(prefix + "ג", 1.0, 1.2), word("'יפ", 1.2, 1.5),
            ])
            assert [w["w"] for w in got] == [prefix + "ג'יפ"]

    def test_a_multi_letter_stem_joins(self):
        # "הבירצ" + "'ה" -- the real case from Raz's tools timeline. The stem
        # is a whole word, so a prefix-only rule would miss it.
        got = _join_geresh_fragments([
            word("הבירצ", 1.0, 1.4), word("'ה", 1.4, 1.6),
        ])
        assert [w["w"] for w in got] == ["הבירצ'ה"]

    def test_a_final_tsadi_stem_joins(self):
        got = _join_geresh_fragments([
            word("בירצ", 1.0, 1.4), word("'ה", 1.4, 1.6),
        ])
        assert [w["w"] for w in got] == ["בירצ'ה"]

    def test_a_quote_after_a_word_ending_in_taf_is_left_alone(self):
        # ת takes no geresh in modern Hebrew, so "אמרת 'שלום" stays two words.
        got = _join_geresh_fragments([
            word("אמרת", 1.0, 1.3), word("'שלומ", 1.3, 1.7),
        ])
        assert [w["w"] for w in got] == ["אמרת", "'שלומ"]

    def test_the_known_false_join_is_pinned(self):
        # Documenting a limit rather than hiding it: a quotation opening right
        # after a word that ends in a geresh-taking letter IS joined. "אז"
        # ends in ז. Never observed in 7,300 words of real engine output, but
        # if it ever starts happening this test is where it shows up.
        got = _join_geresh_fragments([
            word("אז", 1.0, 1.2), word("'שלומ", 1.2, 1.6),
        ])
        assert [w["w"] for w in got] == ["אז'שלומ"]

    def test_the_joined_word_spans_both_halves(self):
        got = _join_geresh_fragments([
            word("ג", 1.0, 1.2), word("'יפ", 1.2, 1.55),
        ])
        assert got[0]["start"] == 1.0
        assert got[0]["end"] == 1.55

    def test_confidence_takes_the_worse_half(self):
        got = _join_geresh_fragments([
            word("צ", 1.0, 1.2, conf=0.91), word("'ק", 1.2, 1.5, conf=0.42),
        ])
        assert got[0]["conf"] == 0.42

    def test_the_hebrew_geresh_and_curly_quote_join_too(self):
        for mark in ("׳", "’"):
            got = _join_geresh_fragments([
                word("ג", 1.0, 1.2), word(mark + "יפ", 1.2, 1.5),
            ])
            assert [w["w"] for w in got] == ["ג" + mark + "יפ"]

    def test_a_quoted_word_is_left_alone(self):
        # "אמר 'שלום" is a quotation, not a geresh: א is not an attached
        # prefix, so the previous token is a whole word rather than a stem.
        got = _join_geresh_fragments([
            word("אמר", 1.0, 1.3), word("'שלומ", 1.3, 1.7),
        ])
        assert [w["w"] for w in got] == ["אמר", "'שלומ"]

    def test_a_letter_that_never_takes_a_geresh_is_left_alone(self):
        got = _join_geresh_fragments([
            word("שלוש", 1.0, 1.3), word("'משהו", 1.3, 1.7),
        ])
        assert [w["w"] for w in got] == ["שלוש", "'משהו"]

    def test_a_bare_quote_token_is_left_alone(self):
        # Nothing Hebrew follows the mark, so it is punctuation.
        got = _join_geresh_fragments([
            word("ג", 1.0, 1.3), word("'", 1.3, 1.7),
        ])
        assert [w["w"] for w in got] == ["ג", "'"]

    def test_a_word_that_already_carries_its_geresh_is_untouched(self):
        words = [word("בגאז'", 1.0, 1.4), word("שלי", 1.4, 1.7)]
        assert [w["w"] for w in _join_geresh_fragments(words)] == [
            "בגאז'", "שלי",
        ]

    def test_words_without_a_geresh_pass_through_untouched(self):
        words = [word("שלומ", 1.0, 1.2), word("עולמ", 1.2, 1.4)]
        assert _join_geresh_fragments(words) == words

    def test_the_input_list_is_not_mutated(self):
        words = [word("ג", 1.0, 1.2), word("'יפ", 1.2, 1.5)]
        _join_geresh_fragments(words)
        assert words[0]["w"] == "ג"
        assert words[0]["end"] == 1.2


# --------------------------------------------------------------------------
# a present GPU is not a usable GPU
# --------------------------------------------------------------------------


class TestCudaFallback:
    """The frozen Windows build ships no CUDA runtime, on purpose.

    `ctranslate2.get_cuda_device_count()` asks the DRIVER, so it reports a
    device on any machine with an NVIDIA card whether or not the runtime
    libraries exist. The installed app therefore chose cuda and died with
    "Library cublas64_12.dll is not found or cannot be loaded" on the first
    real transcription. A slower transcript beats no transcript.
    """

    def test_missing_cuda_libraries_are_recognised(self):
        from hebsub.engines.ivrit_local import _is_missing_cuda_library

        for message in (
            "Library cublas64_12.dll is not found or cannot be loaded",
            "Library cudnn_ops64_9.dll is not found or cannot be loaded",
            "cudart64_12.dll missing",
            "CUDA runtime error",
        ):
            assert _is_missing_cuda_library(RuntimeError(message)), message

    def test_unrelated_failures_are_not_mistaken_for_it(self):
        # The symlink-privilege failure (D-series, WinError 1314) must keep
        # its own hint rather than being swallowed by a CPU retry.
        from hebsub.engines.ivrit_local import _is_missing_cuda_library

        for message in (
            "[WinError 1314] A required privilege is not held by the client",
            "No such file or directory",
            "Connection reset by peer",
        ):
            assert not _is_missing_cuda_library(RuntimeError(message)), message

    def test_it_falls_back_to_cpu_and_says_so(self, monkeypatch, capsys):
        from hebsub.engines import ivrit_local

        attempts = []

        class FakeModel:
            def __init__(self, name, device=None, compute_type=None):
                attempts.append((device, compute_type))
                if device == "cuda":
                    raise RuntimeError(
                        "Library cublas64_12.dll is not found or cannot be loaded"
                    )

        fake_ct2 = type("ct2", (), {"get_cuda_device_count": staticmethod(lambda: 1)})
        monkeypatch.setitem(
            __import__("sys").modules, "ctranslate2", fake_ct2)
        monkeypatch.setitem(
            __import__("sys").modules, "faster_whisper",
            type("fw", (), {"WhisperModel": FakeModel}))

        engine = ivrit_local.IvritLocalEngine(device="auto")
        model = engine._load()

        assert attempts == [("cuda", "float16"), ("cpu", "int8")]
        assert engine.device == "cpu"
        assert model is not None
        assert "falling back to CPU" in capsys.readouterr().out

    def test_the_failure_is_lazy_and_the_retry_still_catches_it(self, monkeypatch, capsys):
        """The real bug: CUDA dies during INFERENCE, not at construction.

        `WhisperModel(device="cuda")` succeeds on any machine with an NVIDIA
        driver, because CTranslate2 loads cuBLAS lazily. The missing library
        surfaces only when the encoder first runs -- inside the segment
        generator. A fallback around the constructor catches nothing, and that
        is precisely what shipped a broken installer.
        """
        from hebsub.engines import ivrit_local

        built = []

        class LazyModel:
            def __init__(self, name, device=None, compute_type=None):
                built.append(device)
                self.device = device

            def transcribe(self, wav, **kw):
                def gen():
                    if self.device == "cuda":
                        raise RuntimeError(
                            "Library cublas64_12.dll is not found or "
                            "cannot be loaded"
                        )
                    yield type("Seg", (), {
                        "start": 0.0, "end": 1.0, "text": "שלום",
                        "words": [type("W", (), {
                            "start": 0.0, "end": 1.0,
                            "word": "שלום", "probability": 0.9})()],
                    })()
                return gen(), type("Info", (), {"language": "he", "language_probability": 0.99, "duration": 1.0})()

        monkeypatch.setitem(
            __import__("sys").modules, "ctranslate2",
            type("ct2", (), {"get_cuda_device_count": staticmethod(lambda: 1)}))
        monkeypatch.setitem(
            __import__("sys").modules, "faster_whisper",
            type("fw", (), {"WhisperModel": LazyModel}))

        engine = ivrit_local.IvritLocalEngine(device="auto")
        out = engine.transcribe("audio.wav")

        # It tried cuda, then rebuilt on cpu and produced a real transcript.
        assert built == ["cuda", "cpu"]
        assert engine.device == "cpu"
        assert out["segments"], "the CPU retry produced no segments"
        assert "retrying on CPU" in capsys.readouterr().out

