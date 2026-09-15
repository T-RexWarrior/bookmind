"""Task generation — ARCHITECTURE.md §3.5.

The Agent side of the task pipeline. The Diagnostician generates diagnostic
probes and the two changed tasks of different scenarios; the Tutor generates
ordinary quiz/review/prerequisite tasks. Each produces a
:class:`~bookmind.domain.models.TaskDraft` that must pass the shared
:mod:`~bookmind.engine.task.validator` before it becomes an immutable
TrustedTaskContext.

Offline (no live model) the generators deterministically derive drafts from the
BugEntry templates. Live mode may rephrase examples but must NOT change the
hypotheses a probe discriminates between (LEARNING_MODEL §9).
"""

from __future__ import annotations

import hashlib
import uuid

from ...agents.bug_library import BugEntry
from ...domain.enums import Level
from ...domain.models import TaskDraft
from ...llm.router import ModelRouter
from ..misconception.probe_classifier import _router_live


def _stage_fingerprint(bug_id: str, template: str, stage: int) -> str:
    raw = f"{bug_id}|{template.strip().lower()}|{stage}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _instance_id(template_id: str) -> str:
    """A per-assignment instance id (P0-02). The template id is stable
    (``quiz|c_reference``) but each *issued* task gets a unique instance suffix
    so two users (or two sessions) drawing the same template never share a row
    and never overwrite each other's task / hints / submission state."""
    return f"{template_id}|{uuid.uuid4().hex[:10]}"


def generate_probe(
    bug: BugEntry,
    *,
    target_concept_ids: list[str],
    level: Level = Level.L2,
    router: ModelRouter | None = None,
) -> TaskDraft:
    """Build a diagnostic-probe draft from ``bug.probe_templates``.

    Offline: uses the first template verbatim. Live: may ask the model to
    rephrase the *wording* while keeping the discriminated hypotheses fixed.
    """
    prompt = bug.probe_templates[0] if bug.probe_templates else ""
    if router is not None and _router_live(router):
        rephrased = _try_rephrase(router, bug, prompt, kind="probe")
        if rephrased:
            prompt = rephrased
    return TaskDraft(
        task_id=_instance_id(f"probe|{bug.bug_id}"),
        task_version=1,
        target_concept_ids=list(target_concept_ids),
        evidence_for_levels=[level],
        rubric=list(bug.rubric),
        prompt_text=prompt,
        is_probe=True,
        discriminated_bug_ids=[bug.bug_id],
    )


def generate_changed_task(
    bug: BugEntry,
    *,
    stage: int,  # 1 = near transfer, 2 = far transfer
    target_concept_ids: list[str],
    level: Level = Level.L3,
    router: ModelRouter | None = None,
) -> TaskDraft:
    """Build one changed-task draft for a remediation stage.

    Stage 1 is near transfer (``changed_task_templates[0]``), stage 2 is far
    transfer (``changed_task_templates[1]``). The scenario fingerprint embeds
    the stage so the two tasks can never be flagged as duplicates of each other.
    """
    idx = 0 if stage == 1 else 1
    template = bug.changed_task_templates[idx] if idx < len(bug.changed_task_templates) else ""
    prompt = template
    if router is not None and _router_live(router):
        rephrased = _try_rephrase(router, bug, template, kind="changed", stage=stage)
        if rephrased:
            prompt = rephrased
    fp = _stage_fingerprint(bug.bug_id, template, stage)
    return TaskDraft(
        task_id=_instance_id(f"changed|{bug.bug_id}|{stage}"),
        task_version=1,
        target_concept_ids=list(target_concept_ids),
        evidence_for_levels=[level],
        rubric=list(bug.rubric),
        prompt_text=prompt,
        is_changed_task=True,
        discriminated_bug_ids=[bug.bug_id],
        scenario_fingerprint=fp,
        remediation_stage=stage,
    )


def generate_quiz(
    *,
    concept,
    level: Level = Level.L1,
    prompt_text: str = "",
    rubric: list[str] | None = None,
    target_concept_ids: list[str] | None = None,
    router: ModelRouter | None = None,
    source_context: str = "",
    previous_prompts: list[str] | None = None,
) -> TaskDraft:
    """Build an ordinary grounded quiz/review draft (Tutor side).

    Live mode asks the model for an application/contrast/reasoning question
    grounded in the selected source excerpts.  Offline mode still produces a
    useful multi-step question instead of the old definition-only template.
    The draft always goes through the shared validator before persistence.
    """
    targets = target_concept_ids or [concept.concept_id]
    # Curated demo concepts are deliberately immediate and deterministic. For
    # all other concepts the live model is used first, with offline fallback.
    generated = _curated_quiz(concept.concept_id) if not prompt_text else None
    generation_mode = "curated" if generated is not None else "template" if prompt_text else "offline_fallback"
    generation_notice = "内置示例题：无需调用模型，也可离线稳定运行。" if generated is not None else ""
    if generated is None and not prompt_text and router is not None and _router_live(router):
        generated, generation_notice = _try_generate_quiz(
            router,
            concept_name=concept.name,
            concept_description=getattr(concept, "description", "") or "",
            source_context=source_context,
            level=level,
            previous_prompts=previous_prompts or [],
        )
        if generated is not None:
            generation_mode = "llm"
            generation_notice = "已使用在线模型根据当前资料生成题目；提交后会结合该题的标准答案与判分要点进行判定。"
    if generated is not None:
        generated_prompt, generated_answer, generated_rubric = generated
    else:
        generated_prompt, generated_answer, generated_rubric = _offline_quiz(
            concept.name,
            getattr(concept, "description", "") or "",
            source_context,
        )
        if not generation_notice:
            generation_notice = (
                "当前没有可用的在线模型配置，已使用离线兜底题目；"
                "它仍可判分，但题干会更通用。"
            )
        elif generation_mode == "offline_fallback":
            generation_notice = f"在线模型未返回可用题干，已切换为离线兜底题目。{generation_notice}"
    return TaskDraft(
        task_id=_instance_id(f"quiz|{concept.concept_id}"),
        task_version=1,
        target_concept_ids=targets,
        evidence_for_levels=[level],
        rubric=rubric or generated_rubric,
        prompt_text=prompt_text or generated_prompt,
        expected_answer=generated_answer,
        generation_mode=generation_mode,
        generation_notice=generation_notice,
    )


_CURATED_QUIZZES: dict[str, tuple[str, str, list[str]]] = {
    "demo_complexity": (
        "算法 A 对 n 个元素执行两重循环，每层各运行 n 次，但只使用常数个变量；算法 B 只扫描一遍，却额外申请长度为 n 的数组。请分别给出两者的时间、空间复杂度，并说明为什么“运行更快”和“占用更少空间”不能混为同一个结论。",
        "A 的时间复杂度为 O(n²)、额外空间为 O(1)；B 的时间复杂度为 O(n)、额外空间为 O(n)。时间复杂度描述操作次数增长，空间复杂度描述额外存储增长，二者是不同维度。",
        ["正确分析算法 A 的时间和空间复杂度", "正确分析算法 B 的时间和空间复杂度", "说明时间与空间复杂度衡量对象不同"],
    ),
    "demo_vector_expand": (
        "一个动态向量初始容量为 1。方案甲每次满时只增加 1 个容量，方案乙每次满时将容量翻倍。连续插入 n 个元素时，请比较两种方案累计搬移元素的数量级，并解释为什么方案乙的单次插入虽然偶尔很慢，平均仍可视为 O(1)。",
        "方案甲累计搬移约 1+2+…+(n-1)，为 O(n²)；方案乙搬移量形成 1+2+4+…，总计小于 2n，为 O(n)。因此翻倍方案的昂贵扩容成本可分摊到多次普通插入上，均摊为 O(1)。",
        ["写出两种扩容策略的累计搬移序列", "得到 O(n²) 与 O(n) 的累计量级", "用分摊思想解释翻倍方案的 O(1) 均摊插入"],
    ),
    "demo_binary_search": (
        "有序数组 [1, 2, 2, 2, 5, 8] 中需要找到数字 2 的第一次出现位置。普通二分在命中 2 后立即返回为什么不够？请给出修改后的区间更新规则，并手工说明搜索何时结束。",
        "命中任意 2 不能保证是第一个。命中后记录当前位置并继续把右边界移到 mid-1；小于目标时移动左边界，大于目标时移动右边界。区间为空时结束，最后记录的位置即第一次出现位置。",
        ["指出立即返回不能保证第一次出现", "给出命中后继续搜索左半区的规则", "说明其他比较分支和终止条件"],
    ),
    "demo_list": (
        "某系统维护 10 万条记录：场景一频繁按下标读取，极少插入；场景二总是在已知节点之后插入或删除，很少随机访问。请分别选择向量或链表，并从定位成本、移动元素和空间局部性三个方面解释选择。",
        "场景一适合向量：下标访问 O(1) 且局部性好；场景二在已知节点时适合链表：插删只改链接、不移动大量元素。链表定位第 k 项仍为 O(k)，且额外存储指针、局部性较差。",
        ["为两个场景作出合理选择", "比较随机定位与插删成本", "讨论元素移动、指针开销或空间局部性"],
    ),
    "demo_stack": (
        "函数 f(3) 调用 f(2)，f(2) 再调用 f(1)，每层在递归返回后还要执行一次加法。请画出调用栈从入栈到出栈的顺序，并说明每个栈帧至少要保存哪些信息，为什么不能只保存参数。",
        "栈帧按 f(3)、f(2)、f(1) 入栈，再按 f(1)、f(2)、f(3) 出栈。除参数外还需保存返回地址、局部变量以及必要的执行状态，才能在返回后恢复调用点并继续加法。",
        ["正确给出入栈和出栈顺序", "列出参数、返回地址和局部状态", "解释恢复调用现场与继续执行的原因"],
    ),
    "demo_queue": (
        "长度为 5 的数组实现循环队列，并约定牺牲一个单元区分队空与队满。初始 front=rear=0，依次入队 A、B、C、D，出队两次，再入队 E、F。请写出每一步 front、rear 的变化，并说明最终为何判满。",
        "入队四次后 rear=4；出队两次后 front=2；入队 E 后 rear=0，入队 F 后 rear=1。此时 (rear+1) mod 5 = 2 = front，所以队满；队空条件是 front=rear。",
        ["正确使用模 5 更新下标", "给出最终 front=2、rear=1", "正确说明牺牲单元时的队满与队空条件"],
    ),
    "demo_tree_traversal": (
        "一棵二叉树的先序序列为 A B D E C F，中序序列为 D B E A C F。请重建其结构，写出后序和层次遍历序列，并说明生成这两种序列分别更自然地使用什么机制。",
        "根为 A；左子树根 B，孩子为 D、E；右子树根 C，右孩子 F。后序为 D E B F C A，层次遍历为 A B C D E F。后序可用递归或显式栈，层次遍历使用队列。",
        ["由先序和中序正确重建树", "写出正确后序序列", "写出正确层次序列并说明栈/递归与队列的作用"],
    ),
    "demo_bfs": (
        "无权图边集为 A-B、A-C、B-D、C-D、D-E。从 A 开始执行 BFS，邻接点按字母序处理。请列出队列每轮变化、各顶点首次被发现时的距离，并解释为什么第一次发现 E 的路径一定具有最少边数。",
        "访问层次为 A；B、C；D；E，距离分别为 0、1、1、2、3。队列使距离为 k 的顶点都在距离为 k+1 的顶点之前展开，因此 E 第一次被发现时不可能还有边数更少而尚未处理的路径。",
        ["正确给出 BFS 队列或访问顺序", "正确标出各顶点距离", "用分层和先进先出解释最少边数性质"],
    ),
    "demo_dfs": (
        "有向图包含边 A→B、B→C、C→A、B→D。从 A 开始按字母序 DFS。请给出递归深入与回溯顺序，指出哪条边暴露了环，并说明仅靠“是否访问过”为什么不足以在有向图中准确识别回边。",
        "DFS 依次深入 A、B、C；C→A 指向当前递归路径中的祖先，是回边并表明存在环；回溯后再访问 D。识别有向图回边需要区分正在递归栈中的灰色顶点与已经完成的黑色顶点，单一 visited 无法区分二者。",
        ["正确描述 DFS 深入和回溯顺序", "识别 C→A 为回边", "说明需要区分正在访问与已经完成状态"],
    ),
    "demo_avl": (
        "依次向空 AVL 树插入 30、20、10、25、28。请指出每次首次失衡的节点和失衡类型，写出所需旋转，并给出最终树形。不要只写旋转名称，要说明旋转后高度为何恢复。",
        "插入 10 后节点 30 为 LL，右旋 30，得到 20 为根；插入 25 后仍平衡；插入 28 后节点 30 的左子树向右增长，形成 LR，需要先左旋 25 再右旋 30。最终根 20，左 10，右 28，28 的左右孩子为 25、30。",
        ["识别插入 10 后的 LL 与右旋", "识别插入 28 后的 LR 双旋", "给出最终树形并用子树高度解释恢复平衡"],
    ),
    "demo_hash": (
        "散列表长度为 11，散列函数 h(k)=k mod 11，使用线性探测。依次插入 22、1、13、24、35。请写出每个键最终位置和探测序列；若删除 13 时直接把槽位置空，会对后续查找造成什么错误？应如何处理？",
        "22→0；1→1；13 从 2 开始落在 2；24 从 2 探测到 3；35 从 2 探测到 4。若把 13 的槽直接置空，查找 24 或 35 会在 2 提前停止并误判不存在；应使用删除标记或重新整理探测簇。",
        ["正确计算最终位置和冲突探测过程", "指出直接置空会截断探测链", "给出删除标记或重建等正确处理"],
    ),
    "demo_huffman": (
        "字符 A、B、C、D 的频率分别为 2、3、7、9。请展示 Huffman 树每轮合并的权值，给出一组合法前缀编码，计算加权编码长度，并解释为什么交换同一父节点的 0/1 不影响最优性。",
        "合并 2+3=5，再合并 5+7=12，最后 9+12=21。可令 D=0、C=11、A=100、B=101，加权长度为 9×1+7×2+2×3+3×3=38。交换兄弟的 0/1 不改变码长，因此不改变加权路径长度。",
        ["正确给出三轮最小权值合并", "给出满足前缀性质的编码", "正确计算加权长度并解释兄弟换码不影响码长"],
    ),
    "demo_kmp": (
        "模式串 P=ABABAC 在主串匹配到前 5 个字符 ABABA 后，第 6 个字符发生失配。请说明 KMP 如何利用已匹配前缀与后缀决定下一次比较位置，并解释主串指针为什么无需回退。",
        "已匹配串 ABABA 的最长相等真前后缀为 ABA，长度 3，因此模式串可把该前缀对齐到已匹配后缀处，从模式下标 3 附近继续比较；主串已匹配部分的信息被复用，所以主串指针不回退。",
        ["找出 ABABA 的最长相等真前后缀 ABA", "说明模式串如何移动并继续比较", "解释复用模式自身信息使主串指针不回退"],
    ),
    "demo_quicksort": (
        "对已经升序的数组 [1,2,3,4,5,6] 做快速排序，若每次固定选第一个元素为轴点，会形成怎样的子问题？请写出递归深度和比较次数的数量级，并给出两种能降低这种最坏情况风险的轴点策略。",
        "每次划分得到规模 0 与 n-1 的子问题，递归深度为 O(n)，比较总数为 (n-1)+…+1=O(n²)。可随机选择轴点，或采用三数取中等更稳健策略。",
        ["指出划分极度不平衡", "得到 O(n) 深度与 O(n²) 比较量级", "给出两种合理轴点改进策略"],
    ),
    "demo_recursion": (
        "递归算法把规模 n 的问题分成两个规模 n/2 的子问题，并在每层额外做 n 次操作。请画出递归树前两层，推导时间复杂度；若两个递归调用顺序执行，额外栈空间为什么不是 O(n)？",
        "第 k 层有 2^k 个规模 n/2^k 的子问题，该层总工作量仍为 n，共 log n 层，所以时间为 O(n log n)。顺序递归时同时存在的活动调用只沿一条根到叶路径，深度 O(log n)，每帧常数空间，因此额外栈空间 O(log n)。",
        ["正确描述递归树每层总工作量", "推导 O(n log n) 时间复杂度", "区分调用总数与同时活动栈帧并得到 O(log n) 空间"],
    ),
}


def _curated_quiz(concept_id: str) -> tuple[str, str, list[str]] | None:
    item = _CURATED_QUIZZES.get(concept_id)
    if item is None:
        return None
    prompt, answer, rubric = item
    return prompt, answer, list(rubric)


def _try_generate_quiz(
    router: ModelRouter,
    *,
    concept_name: str,
    concept_description: str,
    source_context: str,
    level: Level,
    previous_prompts: list[str],
) -> tuple[tuple[str, str, list[str]] | None, str]:
    """Generate one self-contained question *and its matching grading spec*.

    A question-only generation path used a generic local rubric for arbitrary
    model questions.  The resulting prompt and answer key could describe
    different skills, making a genuine learner answer impossible to judge.
    The model now returns a compact, closed question/answer/rubric contract;
    invalid contracts fall back to the deterministic local task.
    """
    system = (
        "你是一名严谨的数据结构课程教师。只依据材料出一道有区分度的中文练习题。"
        "必须要求应用、比较、推导、纠错或分析具体情境；禁止只问定义或复述概念。"
        "不得引入材料中未出现的定理、公式、算法或数字条件。"
        "题干必须自包含且可直接作答。输出严格 JSON："
        '{"prompt_text":"题干","expected_answer":"简洁标准答案","rubric":["评分点1","评分点2"]}。'
        "rubric 必须有 2 到 4 条、可逐项判定，且与题干和标准答案完全对应。"
    )
    prior = "\n".join(f"- {item}" for item in previous_prompts[-3:]) or "无"
    user = (
        f"知识点：{concept_name}\n"
        f"目标等级：{level.value}\n"
        f"知识点说明：{concept_description or '以资料片段为准'}\n"
        f"资料片段：\n{(source_context or concept_description)[:3500]}\n\n"
        f"近期已经出过的题（新题不得重复）：\n{prior}"
    )
    result = router.complete(
        "grounded_quiz_generation",
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        output_schema={"type": "object", "required": ["prompt_text", "expected_answer", "rubric"]},
        temperature=0.55,
        max_tokens=1200,
    )
    parsed = result.parsed_json if result.ok else None
    if not isinstance(parsed, dict):
        return None, (result.error or "模型请求失败或返回为空")[:180]
    prompt = str(parsed.get("prompt_text") or "").strip()
    answer = str(parsed.get("expected_answer") or "").strip()
    criteria = [str(item).strip() for item in (parsed.get("rubric") or []) if str(item).strip()]
    banned = ("请解释", "什么是", "定义是什么")
    if len(prompt) < 24 or len(prompt) > 800:
        return None, "模型返回的题干长度不符合要求"
    if len(answer) < 12 or not 2 <= len(criteria) <= 4:
        return None, "模型未返回可判定的标准答案或评分点"
    if any(prompt.startswith(prefix) for prefix in banned):
        return None, "模型返回了定义式题目，不符合应用型练习要求"
    return (prompt, answer, criteria), ""


def _offline_quiz(concept_name: str, description: str, source_context: str) -> tuple[str, str, list[str]]:
    """A reasoning-oriented fallback that remains useful without a model."""
    basis = (description or source_context or f"准确说明{concept_name}的核心机制").strip()
    if len(basis) > 500:
        basis = basis[:500].rsplit("。", 1)[0] or basis[:500]
    prompt = (
        f"某位同学只记住了「{concept_name}」的结论，却不会判断它在具体问题中是否适用。"
        "请你构造一个最小示例，按“输入或初始状态—关键步骤—结果”的顺序完成分析，"
        "并指出一个容易忽略的适用条件或边界情况。只复述定义不算完整。"
    )
    answer = f"回答应以资料中的核心说明为依据：{basis}；同时给出可检查的具体示例、推理过程和边界条件。"
    rubric = [
        f"准确使用「{concept_name}」的核心机制，而不是只复述名称",
        "给出具体且可逐步检查的示例或输入",
        "说明关键推理步骤与最终结果之间的关系",
        "指出至少一个适用条件、限制或边界情况",
    ]
    return prompt, answer, rubric


def _try_rephrase(router: ModelRouter, bug: BugEntry, template: str, *, kind: str, stage: int = 0) -> str | None:
    """Ask the model to rephrase a task template without changing the skill.

    Returns the rephrased prompt, or None on any failure (caller keeps the
    template). LEARNING_MODEL §9: the hypotheses a probe discriminates must not
    change — so we only ask for surface rewording.
    """
    stage_hint = f" (stage {stage}: {'near' if stage == 1 else 'far'} transfer)" if kind == "changed" else ""
    system = (
        "你是出题助手。请改写下面的题目措辞，保持要考察的技能和假设不变，"
        "只换一个不同的情境或表述。直接输出改写后的题目文本，不要输出解释。"
    )
    user = f"原题：{template}{stage_hint}"
    res = router.complete(
        f"task_rephrase_{kind}",
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.3,
        max_tokens=2048,
    )
    if not res.ok or not res.content:
        return None
    text = res.content.strip()
    # Guard against the model returning nothing useful.
    if len(text) < 5:
        return None
    return text
