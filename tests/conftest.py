import pytest

from bookmind.reading import ReadingStore
from bookmind.services.path_service import PathService
from bookmind.textbook import service


@pytest.fixture
def native_book(tmp_path, monkeypatch):
    monkeypatch.setenv('BOOKMIND_HOME', str(tmp_path))
    PathService.reset_instance()
    file = tmp_path / 'tree.md'
    file.write_text('# 二叉树\n\n二叉树的每个结点至多有两个孩子。\n\n## 中序遍历\n\n中序遍历先左子树，再根节点，最后右子树。\n', encoding='utf-8')
    manifest = ReadingStore().ingest(file)
    yield service.bind_material(manifest.material_id)
    PathService.reset_instance()
