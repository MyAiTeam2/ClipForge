import math
import numpy as np
import pytest
from server.audio_engine import AudioEnergyProfile
from server.media import keep_ranges, PURE_FILLERS
from server.ai import suggest, HOOK_REGEX

def test_audio_energy_profile_scoring():
    # Construct a synthetic audio profile: 10 seconds, calm at first, peak at 6-8s
    times = np.linspace(0, 10, 40, dtype=np.float32)
    rms = np.array([0.05]*24 + [0.45]*8 + [0.08]*8, dtype=np.float32)
    peaks = np.array([0.1]*24 + [0.95]*8 + [0.15]*8, dtype=np.float32)
    bursts = np.array([0.0]*24 + [0.8]*8 + [0.0]*8, dtype=np.float32)
    profile = AudioEnergyProfile(times, rms, peaks, bursts, duration=10.0)

    # Calm range (0-4s)
    calm_score = profile.score_range(0.0, 4.0)
    assert calm_score["avg_energy"] < 0.5
    assert calm_score["laughter_burst"] < 0.2

    # High energy peak / excitement range (5-8s)
    peak_score = profile.score_range(5.0, 8.0)
    assert peak_score["avg_energy"] > 0.65
    assert peak_score["peak_energy"] > 0.8
    assert peak_score["laughter_burst"] > 0.7
    assert peak_score["composite_score"] > calm_score["composite_score"]

def test_filler_word_removal():
    words = [
        {"start": 1.0, "end": 1.5, "word": "Here"},
        {"start": 1.6, "end": 1.9, "word": "is"},
        {"start": 2.0, "end": 2.4, "word": "the"},
        {"start": 2.5, "end": 2.8, "word": "um"},     # Filler word
        {"start": 3.4, "end": 3.9, "word": "secret."},
    ]

    # Without filler removal
    ranges_raw = keep_ranges(1.0, 4.0, words, remove=True, remove_fillers=False)
    # With filler removal
    ranges_clean = keep_ranges(1.0, 4.0, words, remove=True, remove_fillers=True, max_gap=0.30)

    # When fillers are removed and gap is > max_gap, it splits the ranges to excise the filler
    assert len(ranges_clean) >= 2
    # Verify the filler word interval (around 2.5-2.8s) is excised
    assert not any(r[0] <= 2.6 <= r[1] for r in ranges_clean)

def test_pure_filler_set():
    assert "um" in PURE_FILLERS
    assert "uh" in PURE_FILLERS
    assert "er" in PURE_FILLERS
    assert "hmm" in PURE_FILLERS

def test_viral_hook_regex():
    sample_hook = "The secret truth nobody knows about video editing"
    matches = HOOK_REGEX.findall(sample_hook)
    assert len(matches) >= 2
    assert "secret" in [m.lower() for m in matches]
    assert "truth" in [m.lower() for m in matches]

def test_offline_heuristic_suggest_ranking():
    # Transcript with a dull sentence and an exciting hook sentence
    transcript = {
        "segments": [
            {
                "start": 0.0,
                "end": 15.0,
                "text": "Hello everyone welcome to this video today we are going to talk about stuff and things."
            },
            {
                "start": 20.0,
                "end": 35.0,
                "text": "Why do 99% of people fail? Here is the secret truth that changed my entire career."
            },
        ]
    }

    results = suggest(
        transcript=transcript,
        duration=40.0,
        count=1,
        length=15,
        engine="local",
        brief="",
        progress=lambda m, f: None
    )

    assert len(results) == 1
    best_clip = results[0]
    # The clip with the question and secret hook words should rank #1
    assert best_clip["start"] >= 18.0
    assert "secret" in best_clip["title"].lower() or "why" in best_clip["title"].lower()
    assert best_clip["score"] > 60

def test_llm_viral_hook_multidimensional_scoring(monkeypatch):
    import server.ai as ai_module
    
    mock_llm_response = {
        "choices": [{
            "message": {
                "content": """
                {
                    "clips": [
                        {
                            "start": 5.0,
                            "end": 35.0,
                            "title": "Why You Must Stop Doing This",
                            "hook_sentence": "Here is the biggest mistake everyone is making.",
                            "hook_score": 95,
                            "retention_score": 90,
                            "payoff_score": 85,
                            "standalone_score": 90,
                            "reason": "Dramatic open loop with instant value payoff."
                        },
                        {
                            "start": 40.0,
                            "end": 70.0,
                            "title": "Rambling Intro",
                            "hook_sentence": "Hey guys welcome back today we might discuss things.",
                            "hook_score": 30,
                            "retention_score": 60,
                            "payoff_score": 50,
                            "standalone_score": 40,
                            "reason": "Slow intro with low retention."
                        }
                    ]
                }
                """
            }
        }]
    }

    monkeypatch.setattr(ai_module, "request", lambda endpoint, body: mock_llm_response)
    monkeypatch.setattr(ai_module, "KEY", "test_key")

    transcript = {
        "segments": [
            {"start": 0.0, "end": 40.0, "text": "Segment one with content"},
            {"start": 40.0, "end": 80.0, "text": "Segment two with content"}
        ]
    }

    clips = ai_module.suggest(
        transcript=transcript,
        duration=80.0,
        count=2,
        length=30,
        engine="openrouter",
        brief="",
        progress=lambda m, f: None
    )

    assert len(clips) == 2
    # The clip with hook_score 95 and standalone_score 90 should drastically outscore the rambling one
    assert clips[0]["title"] == "Why You Must Stop Doing This"
    assert clips[0]["score"] >= 85
    # The rambling clip has hook_score 30 and standalone_score 40, which triggers heavy penalties
    assert clips[1]["score"] < 45
