"""Use the native visualization tool within a textbook conversation."""
from __future__ import annotations

from deeptutor.capabilities.protocol import PromptBlock
from deeptutor.core.context import UnifiedContext
from deeptutor.visualizers.protocol import VISUALIZE_MODE_KEY
from deeptutor.visualizers.registry import get_visualizer_registry

from .policy import text_only_app


class BookMindStudyDiagrams:
    name = 'bookmind_study_diagrams'
    owned_tools = ('submit_visualization',)

    def is_active(self, context: UnifiedContext) -> bool:
        metadata = context.metadata or {}
        return bool(text_only_app() and not metadata.get(VISUALIZE_MODE_KEY)
                    and (metadata.get('immersive_reading_mode') or metadata.get('mastery_mode'))
                    and not metadata.get('selection_tutor_context'))

    def system_block(self, context, *, language, prompts):
        if not self.is_active(context):
            return None
        if language.startswith('zh'):
            rules = '''你是 BookMind 学习助手。学习对话中的图解直接使用 submit_visualization，
复用已安装的文字模型图解工具，无需图像生成、视觉模型、Python 或外部文件。
用户要求“画图/示意图/图解”时，默认选择 SVG，生成完整图形并提交；流程图也可选 Mermaid。
不要用 ASCII 字符树或代码块代替图形，除非用户明确要求字符画或源代码。
图解成功后用两三句话解释即可，不要重复 payload。普通概念问答不必强行画图。

教学图设计：主题集中、留白充分、节点大小一致、连线整齐、标签简短且不重叠。
图将显示在约 360–480px 的聊天栏中。简单图的 viewBox 宽度优先设为 420–480，
不要生成 860/1200 宽的大画布再等比例压小。文字用 inline style="font-size:18px"
一类的样式指定字号（节点可用 20px，说明不低于 16px），不要只写会被主题覆盖的
font-size 属性。颜色仍使用主题类。一个图聚焦一个例子，不堆叠未被要求的扩展图。
树图保持根在上、左右孩子分列、每层间距均匀；可用暖色表示根、蓝色表示内部结点、
绿色表示叶子，并提供简洁图例。使用 SVG 主题类以适配明暗主题。

先完成用户当前的需求。一次简单示意图只需核对相关定义，不要展开成整章讲义，
不要因上下文有笔记就另讲无关笔记。没有要求复刻教材原图时，基于文字定义绘制自己的
示意图，说明是示意即可；当前没有视觉能力，不要尝试读图片像素。
已有可核对原文就直接使用。需要定位时先用自动检索提示或目录选一个途径，读取少量相关页；
证据足够后停止检索。引用使用 [p.N] 可点击来源，不要为同一页反复 reader_goto。
'''
        else:
            rules = '''You are the BookMind study assistant. Use submit_visualization directly
inside the current study conversation. Text-model SVG diagrams need no image API, vision,
Python execution or external files. When asked to draw, default to SVG (Mermaid for flows).
Never replace a requested diagram with ASCII art or a code block unless explicitly requested.
After acceptance, explain briefly without repeating the payload. Ordinary answers need no diagram.
Use balanced spacing, equal node sizes, readable labels and a restrained theme-aware palette.
The host chat column is about 360–480 CSS pixels wide. Prefer a 420–480-wide viewBox for
simple diagrams rather than shrinking an 860/1200-wide canvas. Set label sizes using inline
style="font-size:18px" (nodes about 20px, captions at least 16px), not presentation attributes
that the theme overrides. Keep theme classes for colors. Focus on one requested example.
For trees, place the root above evenly spaced levels, distinguish root/internal/leaf nodes and
include a short legend. Draw an original schematic from verified text, not an invented copy of
unseen textbook pixels. Vision is unavailable here. Stay on the user's requested topic; do not
expand unrelated notes into a lecture. Read only the source needed to verify the explanation;
stop searching when sufficient. Prefer clickable [p.N] citations over repeated reader_goto calls.
'''
        return PromptBlock(self.name, rules.strip() + '\n\n' + get_visualizer_registry().prompt_catalog('auto'))

    def augment_kwargs(self, tool_name, kwargs, context):
        if tool_name != 'submit_visualization':
            return kwargs
        return {**kwargs, '_visualize_context': context,
                '_visualizer_registry': get_visualizer_registry(),
                '_requested_visualizer': 'auto', '_bookmind_inline': True}

    def pre_loop_seed(self, context):
        return ''
