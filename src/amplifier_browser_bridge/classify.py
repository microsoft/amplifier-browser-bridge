"""Deterministic, page-immune-*advisory* action classification (design doc
`docs/designs/confirmation-gate.md`, Candidate A + the lemma in section 2).

This module is a **pure function over data** -- no I/O, no hub/policy/registry
imports, no network, no model calls. `classify(descriptor, profile)` scores an
`ActionDescriptor` against a `ClassifierProfile` and returns a `Classification`.

**This is advisory, not a security boundary** (design doc section 2(a)): every
field on `ActionDescriptor` except `url`/`origin`/`flow_elevated` is page-asserted,
which means an adversarial page can set it to anything. `Classification.advisory`
is always `True` and is not decoration -- it is the contract statement that this
module's output must never be treated as page-immune by any caller. Page-immune
protection is `scope.py`'s job; page-immune *detection* (post-hoc) is
`effects.py`'s job. This module raises the floor against ordinary, non-adversarial
pages, which is most of them -- it does not claim more than that.

`status="unknown"` is a first-class, distinct outcome from `status="clear"`
(design doc section 6): a descriptor with **no page semantics at all** (no label,
role, tag, or page context) must never silently classify as `clear`. This is the
single most important assertion this module's tests make.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

Provenance = Literal["page", "browser", "caller", "external"]
Status = Literal["clear", "elevated", "unknown"]
ReasonCode = Literal[
    "ref_not_observed",
    "hint_stale",
    "descriptor_unavailable",
    "device_capability_missing",
    "no_page_semantics",
]

DEFAULT_THRESHOLD: int = 3

DEFAULT_POLICY_FILE = Path("~/.config/amplifier-browser-bridge/policy.json")

# The family that fixes the measured case (docs/designs/confirmation-gate.md
# section 1: "Elevate bkrabach to Administrator" -- two terms from this family
# co-occurring scores 3, at threshold, with no phrase list and no URL
# corroboration required). Word-boundaried, case-insensitive matching (see
# `_count_family_terms`) -- "role" alone scores 1 and does not gate; "elevate"
# + "administrator" scores 3 and does.
DEFAULT_FAMILIES: dict[str, tuple[str, ...]] = {
    "privilege": (
        "elevate",
        "elevation",
        "escalate",
        "administrator",
        "admin",
        "owner",
        "maintainer",
        "privilege",
        "sudo",
        "root",
        "role",
        "permission",
        "permissions",
        "grant",
        "revoke",
        "collaborator",
        "member",
        "access",
        "just-in-time",
        "jit",
    ),
    # Deliberately NOT in DEFAULT_PHRASES (see below): each of these words is
    # individually far too common to be a high-confidence phrase on its own
    # (a cookie banner's "Allow", a form's "Accept") -- this is the actual
    # defect the pre-existing GATE_RULES.oauth_grant tried to patch over with
    # `combine="all"` (requiring a URL too). The family mechanism reproduces
    # that same requirement honestly: one term alone scores 1 (below
    # threshold on its own); combined with the oauth authorize URL pattern
    # (weight 2) it reaches threshold. Two terms co-occurring (e.g. "Allow"
    # and "Authorize" both present in a longer label) also reaches threshold
    # without any URL, which is the correct behavior for stronger evidence.
    "oauth_consent": ("allow", "authorize", "accept", "grant access"),
}

# Maps a family name to the canonical category name it contributes to on the
# `Classification.categories` output -- the seven canonical categories
# (docs/POLICY.md section 3: purchase/send/delete/oauth_grant/file_upload/
# account_creation/permission_change) are the caller-facing vocabulary;
# family names are this module's internal scoring taxonomy and need not
# match 1:1 (design doc section 7.2's worked example: the "privilege" family
# firing on "Elevate bkrabach to Administrator" reports
# `categories: ["permission_change"]`, not `["privilege"]`).
FAMILY_TO_CATEGORY: dict[str, str] = {
    "privilege": "permission_change",
    "oauth_consent": "oauth_grant",
}

# FIX 3 (product review panel, priority finding: "name and enforce an explicit
# deny-list of privilege/permission-escalating actions that no declared session
# scope can implicitly include"). Categories in this set get a structural
# guarantee `scope.py`'s `write` origin allowlist does NOT provide on its own:
# `PolicyEngine.evaluate` forces `redeem="unredeemable"` for any classification
# whose `categories` intersects this set, REGARDLESS of write scope and
# REGARDLESS of the session's own declared `redeem` mode -- unless the session
# was explicitly established with `allow_self_attested_escalation=True`
# (scope.py). This closes the gap the incident itself demonstrates: the
# incident happened while browsing github.com, and github.com will almost
# always be IN a broadly-scoped session's write set (the maintainer's own
# stated preference for broad access), so "the origin is in scope" cannot by
# itself be read as "and therefore this session may also grant itself
# Administrator." See policy.py's `evaluate()` and scope.py's module docstring
# for the enforcement and the honest limits of the signal that assigns this
# category (it is not purely browser-asserted -- see the note below).
#
# Deliberately a single category today, not the full canonical seven
# (docs/POLICY.md section 3): `permission_change` is the one the measured
# incident's own category belongs to (Administrator access), and the
# two-implementation rule (KERNEL_PHILOSOPHY.md) argues against generalizing
# to categories with no measured incident behind them yet. Extend this set
# only when a second, real case motivates it.
ESCALATION_CATEGORIES: frozenset[str] = frozenset({"permission_change"})

# High-confidence exact phrases -- kept from the pre-existing GATE_RULES lists
# (policy.py), now scored (weight 3) rather than gating outright on their own.
DEFAULT_PHRASES: dict[str, tuple[str, ...]] = {
    "purchase": (
        r"\bplace order\b",
        r"\bbuy now\b",
        r"\bconfirm purchase\b",
        r"\bcomplete (?:your )?purchase\b",
        r"\bpay now\b",
        r"\bconfirm (?:order|payment)\b",
        r"\bsubmit payment\b",
        r"\bcheckout\b",
    ),
    "send": (
        r"^send$",
        r"\bsend message\b",
        r"\bsend email\b",
        r"\bpublish\b",
        r"\btweet\b",
        r"\breply\b",
        r"\bshare\b",
        r"\bpost\b",
    ),
    "delete": (
        r"\bdelete\b",
        r"\bremove\b",
        r"\btrash\b",
        r"\bpermanently delete\b",
        r"\bdeactivate\b",
    ),
    # Deliberately EMPTY -- see DEFAULT_FAMILIES's "oauth_consent" family
    # comment: "allow"/"authorize"/"accept"/"grant access" were the ORIGINAL
    # bug (GATE_RULES.oauth_grant needed combine="all" specifically because
    # these words are too weak alone). Scored via the family mechanism
    # instead of a phrase list so a lone weak word does not gate.
    "oauth_grant": (),
    "file_upload": (
        r"\bupload\b",
        r"\bchoose file\b",
        r"\battach\b",
    ),
    "account_creation": (
        r"\bsign up\b",
        r"\bcreate account\b",
        r"\bregister\b",
        r"\bcreate (?:my|your|a) account\b",
    ),
    "permission_change": (
        r"\bgrant\b",
        r"\bchange permission\b",
        r"\bupdate permission\b",
        r"\bmake public\b",
        r"\bmake private\b",
    ),
}

DEFAULT_URL_PATTERNS: dict[str, tuple[str, ...]] = {
    "purchase": (r"/checkout(?:/|$)", r"checkout\.stripe\.com", r"paypal\.com/checkoutnow"),
    "oauth_grant": (r"oauth2?/authorize", r"/o/oauth2", r"/login/oauth/authorize"),
    "account_creation": (r"/sign[-_]?up(?:/|$)", r"/register(?:/|$)", r"/create-account"),
    "permission_change": (r"/settings/permissions", r"/security/permissions"),
}

# input_type-based file-upload detection -- an explicit, unambiguous hint
# (element channel), separate from fuzzy label/phrase matching.
FILE_UPLOAD_INPUT_TYPES: frozenset[str] = frozenset({"file"})


@dataclass(frozen=True)
class ActionDescriptor:
    """Everything known about a proposed action, before it executes.

    Fields sourced from the DOM (everything except `url`/`origin`/
    `flow_elevated`/`flow_elevated_by`) are page-asserted and therefore
    forgeable (design doc section 2). `url`/`origin` come from the hub's own
    observation of the tab (`policy._tab_hosts`) and are browser-asserted.
    `flow_elevated` is derived from prior browser-asserted effects (or,
    weakly, from page context) and is hub-supplied, never caller-supplied.
    """

    command: str
    # --- page-asserted ---
    label: str | None = None
    role: str | None = None
    tag: str | None = None
    input_type: str | None = None
    href: str | None = None
    href_cross_origin: bool | None = None
    form_method: str | None = None  # "get" | "post" | None
    form_action: str | None = None
    form_cross_origin: bool | None = None
    is_submit: bool | None = None
    page_title: str | None = None
    nearest_heading: str | None = None
    dialog_title: str | None = None
    # --- browser-asserted ---
    url: str | None = None
    origin: str | None = None
    # --- derived from prior browser-asserted effects (hub-supplied) ---
    flow_elevated: bool = False
    flow_elevated_by: str | None = None  # "observed_effect" | "page_context"

    @property
    def has_any_page_semantics(self) -> bool:
        """False only when there is truly nothing to classify from -- no
        label, role, tag, input type, page-context field, browser-observed
        URL, or flow elevation. This is what makes `classify()` return
        `unknown` rather than silently treating an absent descriptor as
        `clear` (design doc section 6).

        `url` counts here even though it is browser-asserted, not
        page-asserted (the property name refers to "is there anything to
        classify from," not "is every field page-authored") -- a bare
        `navigate` command has no DOM descriptor at all by definition (there
        is no element being acted on), so without counting `url` every
        `navigate` would be unconditionally `unknown`, which would silently
        disable the url-pattern channel (purchase-via-checkout,
        account_creation-via-signup) design doc section 3's Candidate A
        explicitly restores."""
        return any(
            [
                self.label,
                self.role,
                self.tag,
                self.input_type,
                self.href,
                self.form_method,
                self.page_title,
                self.nearest_heading,
                self.dialog_title,
                self.url,
                self.flow_elevated,
            ]
        )


@dataclass(frozen=True)
class Signal:
    """One scored channel contribution -- carried on `Classification.signals`
    so the audit log and the calling agent can see exactly *why* a score was
    reached, not just the total."""

    channel: str  # "label" | "page_context" | "url" | "element" | "flow" | "screen_hook"
    provenance: Provenance
    value: str | None
    matched: tuple[str, ...]
    weight: int


@dataclass(frozen=True)
class Classification:
    status: Status
    score: int | None
    threshold: int
    categories: tuple[str, ...]
    signals: tuple[Signal, ...]
    advisory: bool = True
    reason_code: ReasonCode | None = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "score": self.score,
            "threshold": self.threshold,
            "categories": list(self.categories),
            "advisory": self.advisory,
            "reason_code": self.reason_code,
            "signals": [
                {
                    "channel": s.channel,
                    "provenance": s.provenance,
                    "value": s.value,
                    "matched": list(s.matched),
                    "weight": s.weight,
                }
                for s in self.signals
            ],
        }


@dataclass(frozen=True)
class ClassifierProfile:
    """The replaceable default. Loadable from the same `policy.json` the
    denylist uses (key: `"classifier"`), so the taxonomy is configuration, not
    a fact baked into dispatch (design doc section 13)."""

    threshold: int = DEFAULT_THRESHOLD
    families: dict[str, tuple[str, ...]] = field(default_factory=lambda: dict(DEFAULT_FAMILIES))
    phrases: dict[str, tuple[str, ...]] = field(default_factory=lambda: dict(DEFAULT_PHRASES))
    url_patterns: dict[str, tuple[str, ...]] = field(default_factory=lambda: dict(DEFAULT_URL_PATTERNS))

    @staticmethod
    def load(path: str | Path | None = None) -> ClassifierProfile:
        """Resolution order mirrors `Denylist.load` (policy.py): explicit
        `path` -> `AMPLIFIER_BROWSER_BRIDGE_POLICY_FILE` env var -> conventional
        `~/.config/amplifier-browser-bridge/policy.json` -> built-in defaults.
        A user file's `"classifier"` key may override `threshold` only in
        this phase -- families/phrases/url_patterns stay code-defined until a
        real consumer needs them configurable too (two-implementation rule,
        KERNEL_PHILOSOPHY.md)."""
        import json
        import os

        file_path = Path(
            path or os.environ.get("AMPLIFIER_BROWSER_BRIDGE_POLICY_FILE") or DEFAULT_POLICY_FILE
        ).expanduser()
        if not file_path.is_file():
            return ClassifierProfile()
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return ClassifierProfile()
        classifier_section = data.get("classifier") if isinstance(data, dict) else None
        if not isinstance(classifier_section, dict):
            return ClassifierProfile()
        threshold = classifier_section.get("threshold")
        if isinstance(threshold, int) and threshold > 0:
            return ClassifierProfile(threshold=threshold)
        return ClassifierProfile()


def _compiled(patterns: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p, re.IGNORECASE) for p in patterns)


# B2 fix (security review finding, classifier extraction gap): a label like
# "Аdmin" (Cyrillic А, U+0410) is byte-different from "Admin" but visually
# indistinguishable, and every match in this module is a plain codepoint
# comparison -- so a page can defeat every family/phrase match just by
# substituting look-alike characters from another script into an otherwise
# ordinary-looking label. This is exactly the technique IDN-homograph phishing
# uses against domain names; the same substitution works against label text.
#
# The full, normative answer is Unicode's own confusables mapping
# (https://www.unicode.org/reports/tr39/, `confusables.txt`) via a "skeleton"
# transform. That table has thousands of entries covering combining marks,
# ligatures, and many scripts. Depending on it as a runtime dependency (a
# bundled data file, or a PyPI package like `confusable-homoglyphs`) is more
# than this specific, narrow gap warrants -- IMPLEMENTATION_PHILOSOPHY.md's
# library-vs-custom-code judgment: reach for a library when the COMPLEXITY IT
# HANDLES is worth the dependency, not merely because a standard exists.
#
# LOAD-BEARING SUBSET IMPLEMENTED HERE: single-character substitution of the
# specific Cyrillic and Greek letters that are visually identical (or
# near-identical) to a Latin letter used somewhere in this module's own
# family/phrase vocabulary (a/e/o/p/c/y/x/i/j/s/k/h/t/b + their uppercase
# forms) -- exactly the technique a "let's rename the Elevate button to look
# the same but score differently" adversary would reach for first. Applied
# BEFORE matching, to a NORMALIZED COPY of the text used only for scoring --
# `Signal.value`/audit records still carry the ORIGINAL text, so a human
# reviewing the audit log sees the real (possibly homoglyph-laden) label, not
# a silently-rewritten one.
#
# NOT IMPLEMENTED (say so plainly, not implied-complete): full Unicode
# confusables skeleton transform, multi-character ligatures/sequences,
# combining-mark stripping, scripts beyond Cyrillic/Greek (e.g. Cherokee,
# Armenian letters that are also confusable), and whole-word transliteration.
# A sufficiently exotic homoglyph from outside this table still evades
# scoring -- this raises the bar against the specific, cheap, already-observed
# technique, it does not close every possible one. See classify.py's module
# docstring: this whole module is advisory, never a security boundary.
_CONFUSABLES_MAP: dict[str, str] = {
    # Cyrillic -> visually-identical Latin lookalike (lowercase)
    "а": "a",  # U+0430 CYRILLIC SMALL LETTER A
    "е": "e",  # U+0435 CYRILLIC SMALL LETTER IE
    "о": "o",  # U+043E CYRILLIC SMALL LETTER O
    "р": "p",  # U+0440 CYRILLIC SMALL LETTER ER
    "с": "c",  # U+0441 CYRILLIC SMALL LETTER ES
    "у": "y",  # U+0443 CYRILLIC SMALL LETTER U
    "х": "x",  # U+0445 CYRILLIC SMALL LETTER HA
    "і": "i",  # U+0456 CYRILLIC SMALL LETTER BYELORUSSIAN-UKRAINIAN I
    "ј": "j",  # U+0458 CYRILLIC SMALL LETTER JE
    "ѕ": "s",  # U+0455 CYRILLIC SMALL LETTER DZE
    "к": "k",  # U+043A CYRILLIC SMALL LETTER KA (not identical, but common OCR/homoglyph pairing)
    "н": "h",  # U+043D CYRILLIC SMALL LETTER EN
    "т": "t",  # U+0442 CYRILLIC SMALL LETTER TE
    "в": "b",  # U+0432 CYRILLIC SMALL LETTER VE (loose but commonly used)
    # Cyrillic -> Latin lookalike (uppercase)
    "А": "A",  # U+0410
    "Е": "E",  # U+0415
    "О": "O",  # U+041E
    "Р": "P",  # U+0420
    "С": "C",  # U+0421
    "Х": "X",  # U+0425
    "К": "K",  # U+041A
    "Н": "H",  # U+041D
    "Т": "T",  # U+0422
    "В": "B",  # U+0412
    "М": "M",  # U+041C
    # Greek -> Latin lookalike
    "α": "a",  # U+03B1 GREEK SMALL LETTER ALPHA
    "ο": "o",  # U+03BF GREEK SMALL LETTER OMICRON
    "ρ": "p",  # U+03C1 GREEK SMALL LETTER RHO
    "υ": "y",  # U+03C5 GREEK SMALL LETTER UPSILON
    "Α": "A",  # U+0391 GREEK CAPITAL LETTER ALPHA
    "Β": "B",  # U+0392 GREEK CAPITAL LETTER BETA
    "Ε": "E",  # U+0395 GREEK CAPITAL LETTER EPSILON
    "Ζ": "Z",  # U+0396 GREEK CAPITAL LETTER ZETA
    "Η": "H",  # U+0397 GREEK CAPITAL LETTER ETA
    "Ι": "I",  # U+0399 GREEK CAPITAL LETTER IOTA
    "Κ": "K",  # U+039A GREEK CAPITAL LETTER KAPPA
    "Μ": "M",  # U+039C GREEK CAPITAL LETTER MU
    "Ν": "N",  # U+039D GREEK CAPITAL LETTER NU
    "Ο": "O",  # U+039F GREEK CAPITAL LETTER OMICRON
    "Ρ": "P",  # U+03A1 GREEK CAPITAL LETTER RHO
    "Τ": "T",  # U+03A4 GREEK CAPITAL LETTER TAU
    "Υ": "Y",  # U+03A5 GREEK CAPITAL LETTER UPSILON
    "Χ": "X",  # U+03A7 GREEK CAPITAL LETTER CHI
}

_CONFUSABLES_TABLE = str.maketrans(_CONFUSABLES_MAP)


def _normalize_confusables(text: str | None) -> str | None:
    """Map known look-alike Cyrillic/Greek characters to their Latin
    equivalent, for MATCHING purposes only -- see `_CONFUSABLES_MAP`'s
    comment for exactly what this does and does not cover. Callers should
    keep using the ORIGINAL (un-normalized) text for anything user- or
    audit-facing; only the comparison should see the normalized form."""
    if text is None:
        return None
    return text.translate(_CONFUSABLES_TABLE)


def _first_match(patterns: tuple[re.Pattern[str], ...], value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.translate(_CONFUSABLES_TABLE)
    for pattern in patterns:
        if pattern.search(normalized):
            return pattern.pattern
    return None


def _count_family_terms(family_terms: tuple[str, ...], *texts: str | None) -> list[str]:
    """Word-boundaried, case-insensitive, stem-tolerant count of DISTINCT
    family terms appearing anywhere across `texts` (`permission`/
    `permissions` count once, as one term -- see the module docstring's
    "Family lexicon" note). Returns the list of matched terms (for the
    signal's `matched` field), not just a count.

    Matches against a confusables-normalized copy of `texts` (see
    `_normalize_confusables`) -- a label using look-alike Cyrillic/Greek
    characters for an otherwise-ordinary word (e.g. "Аdmin" with a Cyrillic
    А) is not silently invisible to this scan."""
    joined = " ".join(t for t in texts if t)
    if not joined:
        return []
    normalized_joined = _normalize_confusables(joined) or ""
    matched: list[str] = []
    for term in family_terms:
        # "just-in-time" contains a literal hyphen; word-boundary matching
        # still works since \b anchors on the alnum/non-alnum transition.
        pattern = re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE)
        if pattern.search(normalized_joined):
            matched.append(term)
    return matched


def classify(
    descriptor: ActionDescriptor,
    profile: ClassifierProfile,
    *,
    extra_signals: tuple[Signal, ...] = (),
) -> Classification:
    """Pure sum-over-channels classifier. See the scoring table in
    `docs/designs/confirmation-gate.md` section 11.1 -- this function is that
    table, implemented literally, with one hard early-exit: an
    `ActionDescriptor` with `has_any_page_semantics is False` returns
    `status="unknown"` immediately and MUST NOT fall through to scoring
    (which would silently produce `score=0`, i.e. `clear` -- exactly the
    conflation design doc section 6 exists to end)."""
    if not descriptor.has_any_page_semantics:
        return Classification(
            status="unknown",
            score=None,
            threshold=profile.threshold,
            categories=(),
            signals=(),
            advisory=True,
            reason_code="descriptor_unavailable",
        )

    signals: list[Signal] = []
    categories: set[str] = set()

    # --- label channel: family terms + exact phrases ---
    for family_name, terms in profile.families.items():
        matched = _count_family_terms(terms, descriptor.label)
        if len(matched) >= 2:
            signals.append(Signal("label", "page", descriptor.label, tuple(matched), 3))
            categories.add(FAMILY_TO_CATEGORY.get(family_name, family_name))
        elif len(matched) == 1:
            signals.append(Signal("label", "page", descriptor.label, tuple(matched), 1))

    for category, phrase_patterns in profile.phrases.items():
        hit = _first_match(_compiled(phrase_patterns), descriptor.label)
        if hit is not None:
            signals.append(Signal("label", "page", descriptor.label, (hit,), 3))
            categories.add(category)

    # --- url channel: category url patterns, against the browser-asserted url ---
    for category, url_patterns in profile.url_patterns.items():
        hit = _first_match(_compiled(url_patterns), descriptor.url)
        if hit is not None:
            signals.append(Signal("url", "browser", descriptor.url, (hit,), 2))
            categories.add(category)

    # --- page_context channel: page_title + nearest_heading + dialog_title ---
    context_text = " ".join(
        t for t in (descriptor.page_title, descriptor.nearest_heading, descriptor.dialog_title) if t
    )
    if context_text:
        for family_name, terms in profile.families.items():
            matched = _count_family_terms(terms, context_text)
            if len(matched) >= 2:
                signals.append(Signal("page_context", "page", context_text, tuple(matched), 2))
                categories.add(FAMILY_TO_CATEGORY.get(family_name, family_name))
            # exactly 1 term scores 0 per the design doc's scoring table --
            # recorded as a zero-weight signal so it's still visible/auditable.
            elif len(matched) == 1:
                signals.append(Signal("page_context", "page", context_text, tuple(matched), 0))

    # --- element channel: file input, cross-origin submit ---
    if descriptor.input_type == "file":
        signals.append(Signal("element", "page", descriptor.input_type, ("input_type:file",), 3))
        categories.add("file_upload")
    if descriptor.is_submit and descriptor.form_method == "post":
        signals.append(Signal("element", "page", "form_method:post", ("is_submit", "form_method:post"), 2))
    if descriptor.form_cross_origin or descriptor.href_cross_origin:
        signals.append(Signal("element", "page", "cross_origin", ("cross_origin",), 1))

    # --- flow channel: prior browser-asserted (or weak page-context) elevation ---
    if descriptor.flow_elevated:
        signals.append(
            Signal(
                "flow", "browser", descriptor.flow_elevated_by, (descriptor.flow_elevated_by or "flow",), 3
            )
        )

    # --- external escalate-only hook (Candidate B, design doc section 3) ---
    signals.extend(extra_signals)

    score = sum(s.weight for s in signals)
    status: Status = "elevated" if score >= profile.threshold else "clear"
    return Classification(
        status=status,
        score=score,
        threshold=profile.threshold,
        categories=tuple(sorted(categories)),
        signals=tuple(signals),
        advisory=True,
        reason_code=None,
    )


__all__ = [
    "DEFAULT_FAMILIES",
    "DEFAULT_PHRASES",
    "DEFAULT_THRESHOLD",
    "DEFAULT_URL_PATTERNS",
    "FILE_UPLOAD_INPUT_TYPES",
    "ActionDescriptor",
    "Classification",
    "ClassifierProfile",
    "Provenance",
    "ReasonCode",
    "Signal",
    "Status",
    "classify",
]
