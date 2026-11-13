"""The single Jinja2Templates instance the web app renders from."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from krater.config import get_settings
from krater.services import funding_holds
from krater.services.actor import GROUP_ADMIN
from krater.web.csrf import register_csrf_template_global
from krater.web.estimator_form import summary_text_from_dict
from krater.web.flash import register_flash_template_global
from krater.web.money import format_cents, format_per_tb, format_rate
from krater.web.textfmt import format_size, markdown, minutes_ago, nl2br

TEMPLATES_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
register_csrf_template_global(templates)
register_flash_template_global(templates)
templates.env.filters["dollars"] = format_cents
templates.env.filters["rate"] = format_rate
templates.env.filters["per_tb"] = format_per_tb
templates.env.filters["filesize"] = format_size
templates.env.filters["nl2br"] = nl2br
templates.env.filters["minutes_ago"] = minutes_ago
templates.env.filters["markdown"] = markdown
templates.env.filters["estimate_summary"] = summary_text_from_dict


#: Session key remembering the last answer, for pages that never load the user (the gallery, pricing, error pages).
NAV_ADMIN_KEY = "nav_admin"


def nav_is_admin(request) -> bool:
    """Whether the navigation bar shows the Admin link. Display only, from the roles Weave last gave
    (`users.roles_cached`); `/admin` itself checks Weave fresh. False on a page with no signed-in user.

    A page that loaded the user (`krater.web.deps.current_user`) answers from it and remembers the answer in the
    signed session cookie, as sign-in does the name; a page that didn't uses the remembered answer."""
    session = request.session if "session" in request.scope else {}
    if not session.get("user_id"):
        return False
    user = getattr(request.state, "nav_user", None)
    if user is None:
        return bool(session.get(NAV_ADMIN_KEY))
    is_admin = GROUP_ADMIN in (user.roles_cached or [])
    if session.get(NAV_ADMIN_KEY) != is_admin:
        session[NAV_ADMIN_KEY] = is_admin
    return is_admin


templates.env.globals["nav_is_admin"] = nav_is_admin
# Read at render time, not import time, so a test can change the setting.
templates.env.globals["is_staging"] = lambda: get_settings().staging
templates.env.globals["funding_hold_label"] = lambda hold: funding_holds.LABELS[hold].lower()
templates.env.globals["submission_criteria_url"] = lambda: get_settings().submission_criteria_url
