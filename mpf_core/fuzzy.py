"""In-process fast fuzzy matching algorithm with scoring and word-boundary bonuses."""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional, Tuple

if TYPE_CHECKING:
    from mpf_core.models import Track


_WORD_BOUNDARIES = frozenset(" -_./([{:|")


def fuzzy_score(pattern: str, text: str) -> Optional[int]:
    """Calculate fuzzy match score between pattern and text.

    Returns an integer score if all pattern characters match sequentially in text,
    or None if pattern is not a subsequence of text.
    """
    pattern = pattern.lower()
    text_lower = text.lower()
    p_len = len(pattern)

    if not pattern:
        return 0
    if p_len > len(text_lower):
        return None
    if len(text_lower) != len(text):
        # A few characters change length when lowercased ("İ" becomes two), so the positions found in
        # text_lower no longer line up with text. Compare case-insensitively rather than index past it.
        text = text_lower

    # Leftmost greedy match of each pattern character; str.find runs in C, so texts that do not
    # match are rejected quickly and matching ones are only scored at the p_len matched positions.
    positions: List[int] = []
    position = -1
    for char in pattern:
        position = text_lower.find(char, position + 1)
        if position < 0:
            return None
        positions.append(position)

    score = 0
    prev_match_idx = -2
    for pattern_char, text_idx in zip(pattern, positions):
        score += 10  # base match points
        if text_idx == prev_match_idx + 1:
            score += 15  # consecutive character bonus
        if text_idx == 0 or text[text_idx - 1] in _WORD_BOUNDARIES:
            score += 20  # start of a word / token
        if text[text_idx] == pattern_char:
            score += 5  # exact case match
        prev_match_idx = text_idx

    # Penalty for unmatched text length and start offset
    span_len = prev_match_idx - positions[0] + 1
    score -= (span_len - p_len) * 2
    score -= positions[0] * 2

    return score


def fuzzy_filter_tracks(tracks: List[Track], query: str) -> List[Tuple[int, Track]]:
    """Filter and rank tracks using fuzzy matching over title and uploader.

    Returns list of (original_index, track) sorted by match score descending.
    """
    tokens = [tok for tok in query.strip().split() if tok]
    if not tokens:
        return list(enumerate(tracks))

    scored_items: List[Tuple[int, int, Track]] = []

    for orig_idx, track in enumerate(tracks):
        target_text = f"{track.title} {track.uploader}"
        total_score = 0
        all_matched = True

        for token in tokens:
            token_score = fuzzy_score(token, target_text)
            if token_score is None:
                all_matched = False
                break
            total_score += token_score

        if all_matched:
            scored_items.append((total_score, orig_idx, track))

    # Sort by score descending, then by original index ascending
    scored_items.sort(key=lambda x: (-x[0], x[1]))
    return [(orig_idx, track) for _, orig_idx, track in scored_items]
