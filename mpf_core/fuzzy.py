"""In-process fast fuzzy matching algorithm with scoring and word-boundary bonuses."""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional, Tuple

if TYPE_CHECKING:
    from mpf_core.models import Track


def fuzzy_score(pattern: str, text: str) -> Optional[int]:
    """Calculate fuzzy match score between pattern and text.

    Returns an integer score if all pattern characters match sequentially in text,
    or None if pattern is not a subsequence of text.
    """
    pattern = pattern.lower()
    text_lower = text.lower()
    p_len = len(pattern)
    t_len = len(text_lower)

    if not pattern:
        return 0
    if p_len > t_len:
        return None

    score = 0
    pattern_idx = 0
    prev_match_idx = -2
    first_match_idx = -1

    word_boundaries = {" ", "-", "_", ".", "/", "(", "[", "{", ":", "|"}

    for text_idx, char in enumerate(text_lower):
        if pattern_idx < p_len and char == pattern[pattern_idx]:
            if first_match_idx == -1:
                first_match_idx = text_idx

            # Base match points
            score += 10

            # Consecutive character bonus
            if text_idx == prev_match_idx + 1:
                score += 15

            # Word boundary bonus (start of word / token)
            if text_idx == 0 or text[text_idx - 1] in word_boundaries:
                score += 20

            # Exact case match bonus
            if text[text_idx] == pattern[pattern_idx]:
                score += 5

            prev_match_idx = text_idx
            pattern_idx += 1

    # All pattern chars must be matched
    if pattern_idx < p_len:
        return None

    # Penalty for unmatched text length and start offset
    span_len = prev_match_idx - first_match_idx + 1
    score -= (span_len - p_len) * 2
    score -= first_match_idx * 2

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
