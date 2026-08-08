"""Tests for classify.py -- the B2 fix (Unicode confusables normalization).

No existing test_classify.py predated this pass; these focus on the specific
behavior change (confusable-character matching) plus enough baseline coverage
to prove the normalization doesn't change ordinary, non-adversarial scoring.
"""

from __future__ import annotations

from amplifier_browser_bridge.classify import (
    ActionDescriptor,
    ClassifierProfile,
    _normalize_confusables,
    classify,
)


def test_normalize_confusables_maps_cyrillic_lookalikes_to_latin() -> None:
    # "Аdmin" with a Cyrillic А (U+0410), the security review's own example.
    assert _normalize_confusables("\u0410dmin") == "Admin"


def test_normalize_confusables_maps_greek_lookalikes_to_latin() -> None:
    # "\u0391dmin" -- Greek capital alpha, visually identical to Latin "A".
    assert _normalize_confusables("\u0391dmin") == "Admin"


def test_normalize_confusables_leaves_ordinary_text_unchanged() -> None:
    assert _normalize_confusables("Elevate to Administrator") == "Elevate to Administrator"


def test_normalize_confusables_handles_none() -> None:
    assert _normalize_confusables(None) is None


def test_classify_matches_ordinary_ascii_label_as_before() -> None:
    """Baseline: the measured incident's own label still classifies elevated,
    confirming normalization didn't change behavior for non-adversarial text."""
    descriptor = ActionDescriptor(command="click", label="Elevate bkrabach to Administrator")
    result = classify(descriptor, ClassifierProfile())
    assert result.status == "elevated"
    assert "permission_change" in result.categories


def test_classify_matches_cyrillic_homoglyph_variant_of_the_same_label() -> None:
    """The B2 regression test: a Cyrillic А (U+0410) substituted into an
    otherwise byte-identical label must still classify as elevated -- before
    this fix, the differing codepoint made every family/phrase regex miss
    entirely, silently starving the gate of its strongest signal."""
    homoglyph_label = "\u0410dministrator elevate"  # Cyrillic А, not Latin A
    assert homoglyph_label != "Administrator elevate"  # sanity: genuinely different bytes

    descriptor = ActionDescriptor(command="click", label=homoglyph_label)
    result = classify(descriptor, ClassifierProfile())

    assert result.status == "elevated"
    assert "permission_change" in result.categories


def test_classify_original_unnormalized_label_is_preserved_in_signal_value() -> None:
    """The normalized form is used ONLY for matching -- the audit-facing
    `Signal.value` must still show the real (possibly homoglyph-laden) label
    a human reviewing the log actually saw on the page, not a silently
    rewritten one."""
    homoglyph_label = "\u0410dministrator elevate"
    descriptor = ActionDescriptor(command="click", label=homoglyph_label)
    result = classify(descriptor, ClassifierProfile())

    label_signals = [s for s in result.signals if s.channel == "label"]
    assert label_signals, "expected at least one label-channel signal"
    assert all(s.value == homoglyph_label for s in label_signals)


def test_classify_single_family_term_alone_still_scores_below_threshold() -> None:
    """Confirms the confusables fix didn't loosen the family mechanism's own
    two-terms-required-for-a-real-hit rule (classify.py's module docstring)."""
    descriptor = ActionDescriptor(command="click", label="View role")
    result = classify(descriptor, ClassifierProfile())
    assert result.status == "clear"


def test_classify_descriptor_with_no_page_semantics_is_unknown_not_clear() -> None:
    descriptor = ActionDescriptor(command="navigate")
    result = classify(descriptor, ClassifierProfile())
    assert result.status == "unknown"
    assert result.reason_code == "descriptor_unavailable"
