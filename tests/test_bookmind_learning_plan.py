import asyncio
import json
import re

import pytest

from bookmind.learning.models import KnowledgeType
from bookmind.learning.storage import LearningStore, PathLeaseConflictError
from bookmind.textbook import planning, service


@pytest.fixture
def plan_book(native_book):
    return service.find_book(id=native_book["id"])


def fake_planner(monkeypatch, *, bad_type=False, bad_source=False):
    async def model(*, prompt, **kwargs):
        data = json.loads(prompt)
        locator = int(re.search(r'原文位置 (\d+)', data['原文窗口']).group(1))
        return json.dumps({'goals': [
            {'name': '解释中序遍历的访问顺序', 'type': 'unknown' if bad_type else 'concept',
             'mastery_requirement': '能用自己的话说明先左子树、再根、后右子树的访问顺序。',
             'source_locators': [999999 if bad_source else locator]},
            {'name': '独立推演一棵二叉树的中序遍历', 'type': 'procedure',
             'mastery_requirement': '给出一棵新的二叉树时，独立写出中序遍历结果并说明访问步骤。',
             'source_locators': [locator]},
        ]}, ensure_ascii=False)
    monkeypatch.setattr(planning, 'complete', model)


def focus(book):
    return next(p for p in service.objectives(book) if '中序' in p['name'])


def preview(book):
    return asyncio.run(planning.generate_plan(book, focus(book)['id'], goal='会解释原理并独立推演遍历'))


def test_preview_reads_text_and_does_not_change_learning_state(plan_book, monkeypatch):
    fake_planner(monkeypatch)
    before = LearningStore().load(plan_book['path_id']).model_dump()
    plan = preview(plan_book)
    assert [p['type'] for p in plan['goals']] == ['concept', 'procedure']
    assert plan['source_locators'] and plan['goal']
    assert LearningStore().load(plan_book['path_id']).model_dump() == before


def test_adoption_links_goals_to_same_textbook_and_preserves_section_id(plan_book, monkeypatch):
    fake_planner(monkeypatch)
    plan = preview(plan_book)
    result = planning.apply_plan(plan_book, plan['id'])
    points = [p for m in LearningStore().load(plan_book['path_id']).modules for p in m.knowledge_points]
    created = [p for p in points if p.id in result['objective_ids']]
    assert result['objective_ids'][0] == focus(plan_book)['id']
    assert [p.type for p in created] == [KnowledgeType.CONCEPT, KnowledgeType.PROCEDURE]
    assert all(p.mastery_requirement and p.planning_status == 'planned' for p in created)
    assert all(p.source_section_id == plan['section_id'] for p in created)
    for point in service.objectives(plan_book):
        if point['id'] in result['objective_ids']:
            assert point['start'] in plan['source_locators']
            assert service.verified_practice_objective(plan_book['material_id'], point['id'], point['start'])[1]['id'] == point['id']
    assert planning.apply_plan(plan_book, plan['id']) == result


def test_existing_mastery_and_evidence_are_not_reinterpreted(plan_book, monkeypatch):
    fake_planner(monkeypatch)
    store, point_id = LearningStore(), focus(plan_book)['id']
    progress = store.load(plan_book['path_id'])
    progress.qualitative_mastery[point_id] = True
    progress.mastery_levels[point_id] = 1.0
    progress.feynman_explanations[point_id] = '已经独立解释过的原始答案'
    store.save(progress)
    plan = preview(plan_book)
    result = planning.apply_plan(plan_book, plan['id'])
    after = store.load(plan_book['path_id'])
    assert result['preserved_objective_id'] == point_id
    assert point_id not in result['objective_ids']
    assert after.qualitative_mastery == progress.qualitative_mastery
    assert after.mastery_levels == progress.mastery_levels
    assert after.feynman_explanations == progress.feynman_explanations
    assert after.learning_evidence == progress.learning_evidence


def test_note_linked_objective_keeps_its_identity_and_name(plan_book, monkeypatch):
    fake_planner(monkeypatch)
    point = focus(plan_book)
    from bookmind.services.notebook import get_notebook_manager
    monkeypatch.setattr(get_notebook_manager(), 'get_notebook', lambda _id: {'records': [
        {'metadata': {'bookmind': {'knowledge_point_id': point['id']}}}
    ]})
    result = planning.apply_plan(plan_book, preview(plan_book)['id'])
    after = service.objectives(plan_book)
    assert result['preserved_objective_id'] == point['id']
    assert next(p for p in after if p['id'] == point['id'])['name'] == point['name']


@pytest.mark.parametrize('bad_type,bad_source', [(True, False), (False, True)])
def test_invalid_model_outputs_never_become_concept_fallback(plan_book, monkeypatch, bad_type, bad_source):
    fake_planner(monkeypatch, bad_type=bad_type, bad_source=bad_source)
    with pytest.raises(planning.PlanningError):
        preview(plan_book)
    assert not LearningStore().load(plan_book['path_id']).curriculum_plans


def test_stale_preview_cannot_overwrite_new_progress(plan_book, monkeypatch):
    fake_planner(monkeypatch)
    plan = preview(plan_book)
    store = LearningStore()
    store.mutate(plan_book['path_id'], lambda tx: tx.emit('learner.updated', {}))
    before = store.load(plan_book['path_id']).model_dump()
    with pytest.raises(planning.PlanningError, match='学习记录已更新'):
        planning.apply_plan(plan_book, plan['id'])
    assert store.load(plan_book['path_id']).model_dump() == before


def test_active_tutoring_lease_blocks_external_adoption(plan_book, monkeypatch):
    fake_planner(monkeypatch)
    plan, store = preview(plan_book), LearningStore()
    store.acquire_path_lease(plan_book['path_id'], 'test-session', 'test-turn', bind_session=False)
    with pytest.raises(PathLeaseConflictError):
        planning.apply_plan(plan_book, plan['id'])
    # A preparation tool can use its own live lease; it cannot forge another.
    with pytest.raises(planning.PlanningError, match='会话已经变化'):
        planning.apply_plan(plan_book, plan['id'], turn_id='different-turn')
    result = planning.apply_plan(plan_book, plan['id'], turn_id='test-turn')
    assert result['objective_ids']
    assert store.get_path_lease(plan_book['path_id']).turn_id == 'test-turn'


def test_outline_migration_does_not_change_scores_or_types(plan_book):
    store = LearningStore()
    before = store.load(plan_book['path_id'])
    for module in before.modules:
        for point in module.knowledge_points:
            point.planning_status = 'manual'
    store.save(before)
    planning.migrate_outline_markers(plan_book)
    after = store.load(plan_book['path_id'])
    assert [p.id for m in before.modules for p in m.knowledge_points] == [p.id for m in after.modules for p in m.knowledge_points]
    assert before.knowledge_types == after.knowledge_types
    assert before.mastery_levels == after.mastery_levels
    assert all(p.planning_status == 'outline' for m in after.modules for p in m.knowledge_points)
    version = after.version
    planning.migrate_outline_markers(plan_book)
    assert store.load(plan_book['path_id']).version == version


def test_source_change_invalidates_preview(plan_book, monkeypatch):
    fake_planner(monkeypatch)
    plan = preview(plan_book)
    original = planning._source_window
    def changed(book, section):
        rows, revision = original(book, section)
        return [(loc, text + '修改过的原文') for loc, text in rows], revision
    monkeypatch.setattr(planning, '_source_window', changed)
    with pytest.raises(planning.PlanningError, match='教材内容已经变化'):
        planning.apply_plan(plan_book, plan['id'])


def test_preparation_automatically_plans_only_the_selected_section(plan_book, monkeypatch):
    fake_planner(monkeypatch)
    point_id, result = asyncio.run(planning.prepare_objective(plan_book['path_id'], focus(plan_book)['id']))
    assert result and point_id == result['objective_ids'][0]
    assert len(LearningStore().load(plan_book['path_id']).curriculum_plans) == 1
    _, repeated = asyncio.run(planning.prepare_objective(plan_book['path_id'], point_id))
    assert repeated is None


def test_route_save_preserves_ai_requirements_and_source_group(plan_book, monkeypatch):
    from bookmind.learning.models import KnowledgePoint
    from bookmind.learning.service import LearningService
    fake_planner(monkeypatch)
    planning.apply_plan(plan_book, preview(plan_book)['id'])
    store = LearningStore()
    before = store.load(plan_book['path_id'])
    modules = [m.model_copy(deep=True) for m in before.modules]
    for module in modules:
        module.knowledge_points = [KnowledgePoint(id=p.id, name=p.name, type=p.type,
            module_id=p.module_id, topic_source_ids=p.topic_source_ids) for p in module.knowledge_points]
    LearningService(store).replace_modules_for_path(plan_book['path_id'], modules)
    after = store.load(plan_book['path_id'])
    assert [(p.id, p.planning_status, p.mastery_requirement, p.source_section_id)
            for m in after.modules for p in m.knowledge_points] == [
                (p.id, p.planning_status, p.mastery_requirement, p.source_section_id)
                for m in before.modules for p in m.knowledge_points]


def test_migration_skips_a_path_the_learner_deleted(plan_book):
    store = LearningStore()
    store.delete(plan_book['path_id'])
    planning.migrate_outline_markers(plan_book)
    assert not store.exists(plan_book['path_id'])
