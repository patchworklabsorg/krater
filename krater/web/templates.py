"""The single Jinja2Templates instance the web app renders from."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from krater.config import get_settings
from krater.web.csrf import register_csrf_template_global
from krater.web.estimator_form import summary_text_from_dict
from krater.web.flash import register_flash_template_global
from krater.web.money import format_cents, format_per_tb, format_rate
from krater.web.textfmt import format_size, markdown, nl2br

TEMPLATES_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
register_csrf_template_global(templates)
register_flash_template_global(templates)
templates.env.filters["dollars"] = format_cents
templates.env.filters["rate"] = format_rate
templates.env.filters["per_tb"] = format_per_tb
templates.env.filters["filesize"] = format_size
templates.env.filters["nl2br"] = nl2br
templates.env.filters["markdown"] = markdown
templates.env.filters["estimate_summary"] = summary_text_from_dict
# Read at render time, not import time, so a test can change the setting.
templates.env.globals["is_staging"] = lambda: get_settings().staging
