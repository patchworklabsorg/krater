"""The single Jinja2Templates instance the web app renders from."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from krater.web.csrf import register_csrf_template_global
from krater.web.estimator_form import summary_text_from_dict
from krater.web.flash import register_flash_template_global
from krater.web.money import format_cents
from krater.web.textfmt import nl2br

TEMPLATES_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
register_csrf_template_global(templates)
register_flash_template_global(templates)
templates.env.filters["dollars"] = format_cents
templates.env.filters["nl2br"] = nl2br
templates.env.filters["estimate_summary"] = summary_text_from_dict
