"""Keep the three learning surfaces' conversations independent."""
from __future__ import annotations

from .practice import binding


def _surface(preferences):
    return preferences.get('workspace_mode') or preferences.get('session_kind') or preferences.get('capability') or ''


def is_reading_conversation(session_id, workspace_id, preferences):
    if binding(session_id):
        return False
    surface = _surface(preferences)
    if surface == 'mastery_path':
        return False
    owner = preferences.get('reading_workspace_id')
    return not owner or owner == workspace_id


def assert_session_surface(session_id, preferences, capability, workspace_mode, payload):
    """Reject a mode change before admission can attach or rewrite a session.

    A deep_question tool inside reading is allowed: the reading workspace
    remains its owner. Standalone practice has its own durable binding.
    """
    practice_book = binding(session_id)
    if practice_book:
        if (
            workspace_mode
            or capability != 'deep_question'
            or not (payload.get('config') or {}).get('bookmind_objective_id')
            or payload.get('reading_workspace_id')
        ):
            raise RuntimeError('该会话属于专项练习，请打开本书独立的自主学习或 AI 导学对话。')
        return
    surface = _surface(preferences)
    if surface == 'immersive_reading':
        if workspace_mode != 'immersive_reading':
            raise RuntimeError('该会话属于自主学习，不能改作 AI 导学或专项练习。')
        owner = preferences.get('reading_workspace_id')
        requested = payload.get('reading_workspace_id') or owner
        if owner and requested != owner:
            raise RuntimeError('该阅读对话属于另一份教材。')
    elif surface == 'mastery_path':
        if workspace_mode != 'mastery_path':
            raise RuntimeError('该会话属于 AI 导学，请打开独立的自主学习或专项练习。')
        owner = preferences.get('mastery_path_id')
        requested = payload.get('mastery_path_id') or owner
        if owner and requested != owner:
            raise RuntimeError('该导学对话属于另一条学习路径。')
