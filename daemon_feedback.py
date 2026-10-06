"""The display feedback log surface.

Single concept: reading and recording operator ratings -- the durable JSONL
notes with their optional PNG frames, plus the summary. Storage itself is
feedback.py.
"""


class FeedbackMixin:
    """Feedback recording and read-back."""

    # ---- display feedback --------------------------------------------

    def record_feedback(self, view, rating, categories=None, notes="",
                          params=None, agent="anonymous", include_frame=True):
        """Record one judgement about what a view looks like on the panel.

        Captures a /snapshot at feedback time so a later reader sees exactly
        what was judged. Raises KeyError (unknown view) or ValueError (bad
        rating/categories). Deliberately does NOT touch the policy clock:
        annotating the panel is not display activity, and counting it would
        keep an idle panel awake while reviewers write notes about it."""
        entry = self.renderers.get(view)
        if not entry or "module" not in entry:
            raise KeyError("unknown renderer: %s" % view)
        png, size = None, None
        if include_frame:
            png = self.snapshot()
            if png is not None:
                size = (self.fb.width, self.fb.height)
        stored = self.feedback.record(
            view, rating, categories=categories, notes=notes,
            params=params, agent=agent, frame_png=png, frame_size=size)
        stored["snapshot_captured"] = png is not None
        return stored

    def list_feedback(self, view=None, limit=50):
        entries = self.feedback.list(view=view, limit=limit)
        return {"feedback": entries, "count": len(entries)}

    def get_feedback(self, entry_id):
        return self.feedback.get(entry_id)

    def feedback_frame(self, entry_id):
        """Raw PNG bytes of a note's frame. Raises KeyError/IOError."""
        with open(self.feedback.frame_path(entry_id), "rb") as fh:
            return fh.read()

    def feedback_summary(self):
        return self.feedback.summary()
