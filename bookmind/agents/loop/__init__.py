"""The agent loop and its host.

One turn = one loop over one growing conversation (see
:mod:`bookmind.agents.loop.agent_loop`). :class:`AgenticLoopPipeline` is the
host that assembles a turn for it — tools, prompt, budgets, dispatch — and is
subclassed by each loop that has its own protocol:

* :class:`bookmind.agents.chat.agentic_pipeline.AgenticChatPipeline` — chat,
  and the deep modes that run on chat's own protocol;
* :class:`bookmind.capabilities.mastery.pipeline.MasteryLoopPipeline` —
  mastery tutoring, whose protocol (a posed question ends the turn) is not
  chat's.
"""

from bookmind.agents.loop.pipeline import AgenticLoopPipeline
from bookmind.agents.loop.prompt_blocks import LoopPromptAssembler, PromptBlock

__all__ = ["AgenticLoopPipeline", "LoopPromptAssembler", "PromptBlock"]
