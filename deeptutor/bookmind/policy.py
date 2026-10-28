"""BookMind's optional services are explicit, separate from upstream defaults."""
from __future__ import annotations

import os

TEXT_TOOLS = frozenset({'brainstorm', 'reason'})
TEXT_VISUALIZERS = frozenset({'svg', 'chartjs', 'mermaid', 'mindmap', 'html'})


def text_only_app() -> bool:
    return os.environ.get('BOOKMIND_APP') == '1'


def permitted_optional_tools(names):
    """Extra image/video/search integrations are not configured by a text API."""
    if not text_only_app():
        return names
    return [name for name in (names or []) if name in TEXT_TOOLS]
