"""The phone-first control page served at GET /.

Single concept: the page itself, assembled from the five single-concept parts
next door -- the shell (control_page_shell), the body markup
(control_page_body), and the client script's read half, layout half and
control half (control_page_script_read, control_page_script_layout,
control_page_script_controls). Presentation only: every control drives an
existing API endpoint, and the page owns no routes. The phone-first contract
is pinned by tests/test_control.py.
"""

from control_page_body import _BODY
from control_page_script_controls import _SCRIPT_CONTROLS
from control_page_script_layout import _SCRIPT_LAYOUT
from control_page_script_read import _SCRIPT_READ
from control_page_shell import _SHELL

CONTROL_PAGE = (_SHELL + _BODY + _SCRIPT_READ + _SCRIPT_LAYOUT
                + _SCRIPT_CONTROLS)
