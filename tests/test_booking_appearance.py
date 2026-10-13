"""Unit tests for app/garages/appearance.py - the contrast maths and pair rules.

The vector table is shared with MOT-frontend (src/lib/contrastVectors.json)
so the Python and TypeScript implementations cannot drift apart.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.garages.appearance import (
    MIN_CONTRAST_RATIO,
    BookingAppearanceError,
    contrast_ratio,
    is_hex_color,
    normalise_pair,
    public_colors,
)

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "contrast_vectors.json").read_text())


def test_the_shared_vectors_use_the_same_minimum():
    assert VECTORS["min_ratio"] == MIN_CONTRAST_RATIO


@pytest.mark.parametrize(
    "vector", VECTORS["ratios"], ids=lambda v: f"{v['background']}-{v['text']}"
)
def test_contrast_ratio_matches_the_shared_vectors(vector):
    ratio = contrast_ratio(vector["background"], vector["text"])

    assert ratio == pytest.approx(vector["ratio"], abs=0.005)
    assert (ratio >= MIN_CONTRAST_RATIO) is vector["passes"]


@pytest.mark.parametrize("value", VECTORS["valid_hex"])
def test_valid_hex_is_accepted(value):
    assert is_hex_color(value)


@pytest.mark.parametrize("value", VECTORS["invalid_hex"])
def test_invalid_hex_is_rejected(value):
    assert not is_hex_color(value)


def test_contrast_ratio_is_symmetric():
    assert contrast_ratio("#0b1f3a", "#f5f7fa") == contrast_ratio("#f5f7fa", "#0b1f3a")


def test_normalise_pair_lowercases_a_valid_pair():
    assert normalise_pair("#0B1F3A", "#F5F7FA") == ("#0b1f3a", "#f5f7fa")


def test_normalise_pair_accepts_both_null_as_the_default():
    assert normalise_pair(None, None) == (None, None)


@pytest.mark.parametrize("pair", [("#000000", None), (None, "#ffffff")])
def test_normalise_pair_rejects_only_one_colour(pair):
    with pytest.raises(BookingAppearanceError, match="both"):
        normalise_pair(*pair)


def test_normalise_pair_rejects_three_digit_hex():
    with pytest.raises(BookingAppearanceError, match="6-digit"):
        normalise_pair("#fff", "#000000")


def test_normalise_pair_states_the_ratio_and_minimum_when_contrast_is_too_low():
    with pytest.raises(BookingAppearanceError) as exc:
        normalise_pair("#ffffff", "#777777")

    assert "4.48:1" in str(exc.value)
    assert "4.5:1" in str(exc.value)


def test_public_colors_passes_a_valid_stored_pair_through():
    garage = SimpleNamespace(booking_background_color="#0b1f3a", booking_text_color="#f5f7fa")
    assert public_colors(garage) == ("#0b1f3a", "#f5f7fa")


@pytest.mark.parametrize(
    "stored",
    [
        ("#ffffff", "#eeeeee"),  # contrast too low
        ("red", "#000000"),  # not hex
        ("#000000", None),  # half a pair
        ("#000000;}body{x", "#ffffff"),  # anything that isn't a colour
    ],
)
def test_public_colors_emits_null_for_a_stored_pair_that_fails_validation(stored):
    garage = SimpleNamespace(booking_background_color=stored[0], booking_text_color=stored[1])
    assert public_colors(garage) == (None, None)
