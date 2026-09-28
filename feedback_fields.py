"""feedback_fields - taxonomy and validation for display-feedback entries.

Single concept: what a valid feedback entry looks like. The durable log
itself lives in feedback.py; this module only defines the fixed category
taxonomy, the rating bounds, and the field validator shared by writers.
"""

# Fixed taxonomy: categories aggregate, prose does not. "other" is the
# escape hatch so a note is never forced into a wrong bucket.
CATEGORIES = (
    "readability",   # legible at viewing distance / size
    "layout",        # arrangement, spacing, alignment
    "color",         # palette, contrast choices
    "content",       # the information itself: right thing shown?
    "timing",        # how long it stayed, animation speed, staleness
    "size",          # too much / too little on screen at once
    "other",
)

RATING_MIN, RATING_MAX = 1, 5


def validate_entry_fields(view, rating, categories, notes, params, agent):
    """Raise ValueError on any bad field. Unknown *views* are checked by the
    daemon (which knows the renderer list); here view must just be present."""
    if not isinstance(view, str) or not view.strip():
        raise ValueError("view is required (which renderer the note concerns)")
    if isinstance(rating, bool) or not isinstance(rating, int):
        raise ValueError("rating must be an integer %d-%d"
                         % (RATING_MIN, RATING_MAX))
    if not (RATING_MIN <= rating <= RATING_MAX):
        raise ValueError("rating must be within [%d, %d]"
                         % (RATING_MIN, RATING_MAX))
    categories = categories or []
    if not isinstance(categories, (list, tuple)):
        raise ValueError("categories must be a list")
    for cat in categories:
        if cat not in CATEGORIES:
            raise ValueError("unknown category %r (one of: %s)"
                             % (cat, ", ".join(CATEGORIES)))
    if notes is not None and not isinstance(notes, str):
        raise ValueError("notes must be a string")
    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be an object")
    if agent is not None and not isinstance(agent, str):
        raise ValueError("agent must be a string")
    return list(categories)
