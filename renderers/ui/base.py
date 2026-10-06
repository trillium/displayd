"""Composition primitives for the component layer: no pixels of its own.

``chain`` is the load-bearing one, and the only thing this module needs.
The daemon composites several components onto every presented frame (the
playlist progress bar, the home badge, the sleep badge), and the panel
must never go blank because one of them raised: a skipped component is a
missing affordance, a blank frame is a dead panel.

The guarantee used to live in the home-chrome module -- i.e. one
component owned the safety of *all* components, and could only protect
the layers it happened to be handed. It belongs here instead, where the
composition happens, so any component added later inherits it.

A component used on its own gets the same guarantee by returning its
frame unchanged on any failure; that obligation is stated in
``renderers/ui/__init__`` and every component in this package keeps it.
"""


def chain(*layers):
    """Compose overlay functions into one ``fn(img) -> img``.

    Applies every non-None layer in order and returns the composed frame.
    Never raises: a layer that errors (or returns None) is skipped, so one
    broken component can never blank the panel. Pure factory.

    Order is the caller's: a component drawing later wins the pixels it
    covers, so the caller states the z-order by argument order.
    """
    fns = [fn for fn in layers if fn is not None]

    def apply(img):
        for fn in fns:
            try:
                out = fn(img)
            except Exception:
                continue
            if out is not None:
                img = out
        return img

    return apply
