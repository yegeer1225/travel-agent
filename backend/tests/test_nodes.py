"""8 个节点各做什么 —— 逐个盯死。

这个文件盯的是**每个节点自己的契约**，不涉及图的连线（那是 `test_graph.py`）。
分成两个文件是因为它们坏的方式完全不同：

| 坏了的表现 | 该去哪个文件找 |
|---|---|
| "这一步算错了 / 该写 state 的没写" | 本文件 |
| "该走的路没走 / 绕圈绕不出去 / 该刹车没刹" | `test_graph.py` |

═══════════════════════════════════════════════════════════════
 三个最值钱的断言（删掉别的也要留这三条）
═══════════════════════════════════════════════════════════════

**① `tool_step` 必须回应每一条 `tool_calls`。**
   实测：漏一条 → 下一轮请求稳定 400。这是全项目最容易写出、
   又最难在本地发现的 bug（因为单次调用看上去完全正常）。

**② 候选池为空时 `generate_plan` 不许调模型。**
   池子空 = 模型一次都没搜过。这时让它"生成行程"，它必然从记忆里编，
   而且**会成功**。这条断言是"封闭世界"的最后一道闸。

**③ 超限时 `agent_step` 不许调模型。**
   上限的意义就是"不再花钱"。如果守卫生效了却还是调了一次，
   那 D25 三个上限就只是装饰。
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from app.graph.draft import DRAFT_SCHEMA_HINT, DraftDay, PlanDraft, assemble_trip
from app.graph.intent import Requirements
from app.graph.nodes import (
    FORCE_STOP_TEXT,
    LLM_FAIL_TEXT,
    MAX_AGENT_ROUNDS,
    MAX_TOOL_CALLS,
    TOOL_BUDGET_TEXT,
)
from app.providers.base import DistanceResult
from app.providers.mock import MockAmapProvider
from app.tools.poi_pool import PoiPool
from fakes import (
    POI_IDS,
    ScriptedChatModel,
    ai_multi_tool_calls,
    ai_text,
    ai_tool_call,
    make_state,
    pool_of,
    run,
    sub_finds,
)
from fakes import make_nodes as _make_nodes

TODAY = date(2026, 9, 16)


def make_nodes(**kwargs):
    """固定"今天"的 `make_nodes`。

    🔴 **测试必须固定今天。** 不固定的话，`parse_intent` 里
    "过去的日期要丢弃"和"锚点日期要写进 prompt"这两条断言会随真实日期漂移 ——
    那种测试比没有更糟：它会在某个不相干的日子里突然红掉，
    让人以为是新写的代码出了问题。
    """
    kwargs.setdefault("today", TODAY)
    return _make_nodes(**kwargs)


class BoomProvider:
    """一个"所有方法都炸"的数据源。用来验证**失败也要变成 ToolMessage**。"""

    name = "boom"

    def covers(self, city: str | None) -> bool:
        """⚠️ 必须 `True`（D67-B）。它的用途是"让流程走到工具那一步才炸"；
        若它自称覆盖不了，`agent_step` 的守卫会**提前**拦掉，
        "工具抛异常 → 变成 ToolMessage"这条路径就测不到了。"""
        return True

    async def search_poi(self, keyword, city=None, limit=10):
        raise RuntimeError("上游炸了")

    async def get_poi(self, poi_id):
        raise RuntimeError("上游炸了")

    async def get_weather(self, city, day):
        raise RuntimeError("上游炸了")

    async def calc_distance(self, origin, dest):
        raise RuntimeError("上游炸了")


def _empty_draft() -> PlanDraft:
    """一个"合法但什么都没排"的草稿 —— 用来单独测 `render` 的往返检查。"""
    return PlanDraft(title="x", days=[DraftDay(date=TODAY, theme="t", stops=[])])


# ══════════════════════════════════════════════════════════════
#  ⑦ parse_intent
# ══════════════════════════════════════════════════════════════


def test_parse_intent_extracts_and_reports_no_missing():
    nodes = make_nodes(
        extract_script=[
            ai_text('{"destination": "成都", "date": "2026-09-20", "days": 3}')
        ]
    )
    out = run(nodes.parse_intent(make_state()))

    assert out["missing_required"] == []
    assert out["requirements"]["destination"] == "成都"
    assert out["requirements"]["date"] == "2026-09-20"
    assert "error" not in out


def test_parse_intent_reports_missing_blocking_when_model_gives_nothing():
    nodes = make_nodes(extract_script=[ai_text('{"destination": null, "date": null}')])
    out = run(nodes.parse_intent(make_state()))

    assert out["missing_required"] == ["destination", "date"]
    assert out["requirements"]["destination"] is None


def test_parse_intent_survives_garbage_output():
    """模型输出不是 JSON 时**不能抛** —— 返回"缺两个必填项"+ 原因，让 `ask_more` 去问。"""
    nodes = make_nodes(extract_script=[ai_text("我觉得成都不错，玩个三四天吧")])
    out = run(nodes.parse_intent(make_state()))

    assert out["missing_required"] == ["destination", "date"]
    assert "error" in out and "无法解析" in out["error"]


def test_parse_intent_survives_llm_exception():
    class Exploding(ScriptedChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            raise RuntimeError("网络断了")

    nodes = make_nodes()
    nodes.llm_extract = Exploding(script=[])
    out = run(nodes.parse_intent(make_state()))

    assert out["missing_required"] == ["destination", "date"]
    assert "error" in out and "抽取调用失败" in out["error"]


def test_parse_intent_discards_past_date():
    """🔴 抽取节点关着思考，模型算相对日期会偏。

    过去的日期**一定错了**，而且下游没有任何环节能发现（一个合法日期字符串
    看起来完全正常，天气只会回"窗口外无法判定"）。所以在这里就丢掉、重新追问。
    """
    nodes = make_nodes(
        extract_script=[ai_text('{"destination": "成都", "date": "2026-01-01"}')]
    )
    out = run(nodes.parse_intent(make_state()))

    assert out["requirements"]["destination"] == "成都"
    assert out["requirements"]["date"] is None
    assert out["missing_required"] == ["date"]
    assert "早于今天" in out["error"]


def test_parse_intent_keeps_today_as_valid_date():
    """边界：**今天**不算过去，不该被丢。"""
    nodes = make_nodes(
        extract_script=[ai_text('{"destination": "成都", "date": "2026-09-16"}')]
    )
    out = run(nodes.parse_intent(make_state()))
    assert out["missing_required"] == []
    assert out["requirements"]["date"] == "2026-09-16"


def test_parse_intent_merges_with_previous_round():
    """第二轮只说"改成 4 天"，目的地不能被抹掉。"""
    nodes = make_nodes(extract_script=[ai_text('{"days": 4}')])
    out = run(
        nodes.parse_intent(
            make_state(
                user_message="改成4天",
                requirements={"destination": "成都", "date": "2026-09-20"},
            )
        )
    )
    assert out["requirements"]["destination"] == "成都"
    assert out["requirements"]["days"] == 4


def test_parse_intent_empty_message_does_not_call_model():
    nodes = make_nodes(extract_script=[ai_text("{}")])
    out = run(nodes.parse_intent(make_state(user_message="   ")))
    assert out["error"] == "收到空消息"
    assert nodes.llm_extract.call_count == 0


def test_parse_intent_prompt_is_json_mode_compatible():
    """`response_format=json_object` 的硬要求：prompt 里必须出现 "JSON" 字样
    （OpenAI 系和 DeepSeek 都有这条约束，不满足会直接 400）。"""
    nodes = make_nodes(extract_script=[ai_text("{}")])
    run(nodes.parse_intent(make_state()))
    joined = nodes.llm_extract.joined_prompts()
    assert "JSON" in joined
    assert "2026-09-16" in joined  # 锚点日期


# ══════════════════════════════════════════════════════════════
#  ⑧ ask_more
# ══════════════════════════════════════════════════════════════


def test_ask_more_produces_text_and_message():
    nodes = make_nodes()
    out = run(nodes.ask_more(make_state(missing_required=["destination", "date"])))

    assert out["ask"]
    assert "去哪座城市" in out["ask"]
    assert len(out["messages"]) == 1


def test_ask_more_falls_back_to_computed_missing():
    """`missing_required` 没写时自己算，不依赖上游一定填了。"""
    nodes = make_nodes()
    out = run(nodes.ask_more(make_state(requirements={"destination": "成都"})))
    assert "哪天出发" in out["ask"]


# ══════════════════════════════════════════════════════════════
#  ② agent_step
# ══════════════════════════════════════════════════════════════


def test_agent_step_returns_model_message_and_counts_round():
    nodes = make_nodes(tool_script=[ai_text("我打算这么安排")])
    out = run(nodes.agent_step(make_state()))

    assert out["agent_rounds"] == 1
    assert len(out["messages"]) == 1
    assert out["messages"][0].content == "我打算这么安排"


def test_agent_step_drains_pending_messages_into_context():
    """🔴 steering 的实战断言：用户插的话必须**在交给模型之前**进上下文，
    并且**被清空**（否则下一圈还会再插一遍，无限重复）。"""
    nodes = make_nodes(tool_script=[ai_text("好")])
    out = run(
        nodes.agent_step(make_state(pending_messages=["预算别超过人均800"]))
    )

    assert out["pending_messages"] == [], "吃完要清空"
    user_msgs = [m for m in out["messages"] if m.type == "human"]
    assert len(user_msgs) == 1
    assert "预算别超过人均800" in user_msgs[0].content

    # 而且要真的进了发给模型的消息里
    sent = nodes.llm_tool.calls[0]
    assert any("预算别超过人均800" in str(m.content) for m in sent)


def test_agent_step_no_pending_means_no_extra_message():
    nodes = make_nodes(tool_script=[ai_text("好")])
    out = run(nodes.agent_step(make_state(pending_messages=[])))
    assert "pending_messages" not in out
    assert all(m.type != "human" for m in out["messages"])


@pytest.mark.parametrize(
    "overrides",
    [
        {"tool_call_count": MAX_TOOL_CALLS},
        {"tool_call_count": MAX_TOOL_CALLS + 5},
        {"agent_rounds": MAX_AGENT_ROUNDS},
    ],
)
def test_agent_step_stops_calling_model_when_over_budget(overrides):
    """🔴 上限的意义是"不再花钱"。守卫生效了却还调一次，上限就只是装饰。"""
    nodes = make_nodes(tool_script=[ai_text("我要继续查")])
    out = run(nodes.agent_step(make_state(**overrides)))

    assert nodes.llm_tool.call_count == 0, "超限时不该调用模型"
    assert out["messages"][0].content == FORCE_STOP_TEXT


def test_agent_step_below_budget_still_calls_model():
    """反向断言：没超限时必须正常调用 —— 别把守卫写成"永远挡住"。"""
    nodes = make_nodes(tool_script=[ai_text("继续")])
    run(nodes.agent_step(make_state(tool_call_count=MAX_TOOL_CALLS - 1)))
    assert nodes.llm_tool.call_count == 1


def test_agent_step_survives_llm_exception():
    class Exploding(ScriptedChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            raise RuntimeError("500")

    nodes = make_nodes()
    nodes._agent_llm = Exploding(script=[])
    out = run(nodes.agent_step(make_state()))

    assert out["messages"][0].content == LLM_FAIL_TEXT
    assert "error" in out
    # 不能抛 —— 抛出去会让已经流出的 SSE token 变成孤儿


# ══════════════════════════════════════════════════════════════
#  ③ tool_step
# ══════════════════════════════════════════════════════════════


def test_tool_step_responds_to_every_tool_call():
    """🔴🔴 全项目最关键的一条断言。

    模型一次发 3 个 tool_calls 时，**必须回 3 条 ToolMessage**。
    实测：漏一条 → 下一次请求稳定 400
    （`An assistant message with 'tool_calls' must be followed by tool messages
    responding to each 'tool_call_id'`）。
    用 `calls[0]` 的写法就会挂在这条上。

    ⚠️ M4 后主 agent 没有直搜工具 —— 这里用 3 次天气调用当载体：
    这条测试盯的是"逐条回应"这个机制，工具是谁无关。
    """
    nodes = make_nodes()
    ai = ai_multi_tool_calls(
        ("get_weather", {"date_str": TODAY.isoformat()}),
        ("get_weather", {"date_str": TODAY.isoformat()}),
        ("get_weather", {"date_str": TODAY.isoformat()}),
    )
    out = run(nodes.tool_step(make_state(messages=[ai])))

    tool_msgs = out["messages"]
    assert len(tool_msgs) == 3, "每一个 tool_call 都要有一条回应"
    assert {m.tool_call_id for m in tool_msgs} == {"call_1", "call_2", "call_3"}
    assert out["tool_call_count"] == 3


def test_tool_step_responds_to_unknown_tool():
    """模型编了个不存在的工具 —— **也要回一条**，只是内容是"没有这个工具"。"""
    nodes = make_nodes()
    ai = ai_tool_call("book_hotel", {"city": "成都"})
    out = run(nodes.tool_step(make_state(messages=[ai])))

    assert len(out["messages"]) == 1
    assert "没有名为" in out["messages"][0].content
    assert out["messages"][0].tool_call_id == "call_1"


def test_tool_step_turns_exception_into_tool_message():
    """工具抛异常也必须变成回应 —— 抛出去 = 那条 tool_call 永远没有回应 = 下一轮必 400。"""
    nodes = make_nodes(provider=BoomProvider())
    ai = ai_tool_call("get_weather", {"date_str": TODAY.isoformat()})
    out = run(nodes.tool_step(make_state(messages=[ai])))

    assert len(out["messages"]) == 1
    assert "执行失败" in out["messages"][0].content
    assert "不要因此就凭记忆编数据" in out["messages"][0].content
    assert "error" in out


def test_tool_step_records_pois_into_state_snapshot():
    """工具返回的 POI 必须进快照 —— 否则校验层拿不到判据来源。

    🔴 M4 关键接线：主 agent 只能通过 `task` 搜索，子 agent 内部用的
    是**同一个** `search_poi` 实例，在 tool_step 的池子作用域内执行 ——
    所以"子 agent 搜到的"必须和"直搜的"一样进快照。这是"封闭世界不破"的实证。
    """
    nodes = make_nodes(
        sub_script=sub_finds("武侯祠", picks=[{"poi_id": POI_IDS[0], "reason": "测试"}])
    )
    ai = ai_tool_call("task", {"objective": "成都的历史古迹", "city": "成都"})
    out = run(nodes.tool_step(make_state(messages=[ai])))

    assert out["collected_pois"], "子 agent 搜到东西了，快照不能是空的"
    assert all("poi_id" in v for v in out["collected_pois"].values())
    assert POI_IDS[0] in out["collected_pois"], "终选清单里的 POI 必须真的在池子里"


def test_tool_step_snapshot_accumulates_across_rounds():
    """第二轮搜到的要和第一轮的**合并**（池子是只增不改的），不能覆盖。"""
    nodes = make_nodes(
        sub_script=[
            # 两次 task 各消费一段脚本（同一个 ScriptedChatModel 实例，游标连续）
            *sub_finds("武侯祠", picks=[{"poi_id": POI_IDS[0], "reason": "测试"}]),
            *sub_finds("人民公园", picks=[{"poi_id": POI_IDS[2], "reason": "测试"}]),
        ]
    )
    first = run(
        nodes.tool_step(
            make_state(messages=[ai_tool_call("task", {"objective": "古迹", "city": "成都"})])
        )
    )
    snapshot_1 = first["collected_pois"]

    second = run(
        nodes.tool_step(
            make_state(
                messages=[ai_tool_call("task", {"objective": "公园", "city": "成都"})],
                collected_pois=snapshot_1,
            )
        )
    )
    assert set(snapshot_1) <= set(second["collected_pois"]), "旧的 POI 不能丢"
    assert POI_IDS[2] in second["collected_pois"], "第二次 task 搜到的要合并进来"


def test_tool_step_is_noop_without_tool_calls():
    nodes = make_nodes()
    out = run(nodes.tool_step(make_state(messages=[ai_text("收工了")])))
    assert out == {}


def test_tool_step_refuses_calls_beyond_budget_but_still_answers():
    """🔴 2026-09-16 跑真实模型发现的缺口：**只有 `agent_step` 一处守卫时，
    工具调用总数会超到 23**（上限是 20）。

    原因：守卫只能在"下一轮开始前"判断，而模型**一轮能发 8 个调用** ——
    已用 19 次时依然放行（19 < 20），这一轮又发 4 个 → 23。

    所以执行预算必须在这里再拦一道。两条要求同时满足：
    · 超出的**不执行**（总数真的封顶，不是"大约 20"）
    · 超出的**仍然回一条 `ToolMessage`**（漏回应 = 下一轮 400）
    """
    nodes = make_nodes(max_tool_calls=2)
    ai = ai_multi_tool_calls(
        ("task", {"objective": "古迹", "city": "成都"}),
        ("task", {"objective": "美食", "city": "成都"}),
        ("task", {"objective": "公园", "city": "成都"}),
        ("task", {"objective": "购物", "city": "成都"}),
    )
    out = run(nodes.tool_step(make_state(messages=[ai])))

    assert len(out["messages"]) == 4, "四条都要回应"
    refused = [m for m in out["messages"] if TOOL_BUDGET_TEXT in str(m.content)]
    assert len(refused) == 2, "余额只有 2 次，后两条该被拒"
    assert {m.tool_call_id for m in refused} == {"call_3", "call_4"}
    # 被拒的两条**没有真的执行** —— 所以池子里不会有它们搜出来的东西
    executed = [m for m in out["messages"] if TOOL_BUDGET_TEXT not in str(m.content)]
    assert all("候选" in str(m.content) or "没有找到" in str(m.content) for m in executed)


def test_tool_step_budget_accounts_for_already_used_calls():
    """余额要按**已用次数**扣，不是每轮都重置成满额。"""
    nodes = make_nodes(max_tool_calls=3)
    ai = ai_multi_tool_calls(
        ("task", {"objective": "古迹", "city": "成都"}),
        ("task", {"objective": "美食", "city": "成都"}),
    )
    out = run(nodes.tool_step(make_state(messages=[ai], tool_call_count=2)))

    refused = [m for m in out["messages"] if TOOL_BUDGET_TEXT in str(m.content)]
    assert len(refused) == 1, "已用 2 次、上限 3 次 → 只剩 1 次余额"


def test_tool_step_is_noop_with_empty_history():
    nodes = make_nodes()
    assert run(nodes.tool_step(make_state(messages=[]))) == {}


# ══════════════════════════════════════════════════════════════
#  ④ generate_plan
# ══════════════════════════════════════════════════════════════


GOOD_DRAFT = (
    '{"title": "成都三日", "days": [{"date": "2026-09-16", "theme": "古迹", '
    '"stops": [{"poi_id": "' + POI_IDS[0] + '", "arrive": "09:00", '
    '"stay_min": 90, "match_reason": "适合带老人"}]}]}'
)


def _req_state(**overrides) -> dict:
    base = {
        "requirements": Requirements(destination="成都", date=TODAY)
        .with_defaults()
        .model_dump(mode="json"),
        "collected_pois": pool_of(5),
    }
    base.update(overrides)
    return make_state(**base)


def test_generate_plan_refuses_when_pool_is_empty():
    """🔴 封闭世界的最后一道闸：池子空 = 模型一次没搜过。

    这时让模型"生成行程"，它必然从记忆里编，**而且会成功**。
    所以必须在这里拦住，且**连模型都不要调**。
    """
    nodes = make_nodes(plan_script=[ai_text(GOOD_DRAFT)])
    out = run(nodes.generate_plan(_req_state(collected_pois={})))

    assert "error" in out
    assert "候选池是空的" in out["error"]
    assert nodes.llm_plan.call_count == 0, "既然注定要拦，就别花这次钱"


def _hangzhou_requirements() -> dict:
    """目的地**不在 mock 池覆盖范围内**的需求。D67 的两个测试都用它。"""
    return (
        Requirements(destination="杭州", date=TODAY).with_defaults().model_dump(mode="json")
    )


def test_generate_plan_blames_the_data_source_when_it_cannot_cover():
    """池空的两种成因要说清是哪一种（D67-A）。

    旧文案把『调了 9 次、每次数据源都返回空』也说成『模型没有成功调用过
    search_poi』，于是『目的地不在数据源覆盖范围内』被读成了『模型偷懒』——
    排查方向直接跑偏（作者本人被它误导过）。
    """
    nodes = make_nodes(plan_script=[ai_text(GOOD_DRAFT)])
    out = run(
        nodes.generate_plan(
            _req_state(collected_pois={}, requirements=_hangzhou_requirements())
        )
    )

    assert "覆盖不到" in out["error"]
    assert "杭州" in out["error"]
    assert "没有成功调用过" not in out["error"], "这条不是模型的问题，别这么说"
    assert nodes.llm_plan.call_count == 0, "既然注定要拦，就别花这次钱"


def test_agent_step_blocks_when_the_data_source_cannot_cover():
    """D67-B：数据源覆盖不到目的地时，连工具循环都不进。

    拦的理由是『可预知地浪费』：实测非成都目的地会白等 **159s**
    （其中 ≈100s 花在 7 次注定为空的搜索）才报错。
    判据由 provider 自述（`covers`）→ real 档下这道守卫**自动失效**，
    所以这里也顺带保证了它不会拦住真数据。
    """
    nodes = make_nodes(tool_script=[ai_tool_call("task", {"objective": "找杭州的景点"})])
    out = run(nodes.agent_step(_req_state(requirements=_hangzhou_requirements())))

    assert "覆盖不到" in out["error"]
    assert out["messages"][0].content == out["error"], "要和预算守卫同构：回一条纯文本"
    assert nodes.llm_tool.call_count == 0, "拦在这里就是为了不花这次钱"


def test_generate_plan_returns_draft():
    nodes = make_nodes(plan_script=[ai_text(GOOD_DRAFT)])
    out = run(nodes.generate_plan(_req_state()))

    assert "error" not in out
    assert out["draft"]["title"] == "成都三日"
    assert out["draft"]["days"][0]["stops"][0]["poi_id"] == POI_IDS[0]


def test_generate_plan_retries_once_with_pydantic_error():
    """字段错了是模型**自己能修**的那类错（跟"编地点"不同），所以回灌报错重试一次。"""
    nodes = make_nodes(plan_script=[ai_text('{"title": "x", "days": "不是数组"}'), ai_text(GOOD_DRAFT)])
    out = run(nodes.generate_plan(_req_state()))

    assert "draft" in out
    assert nodes.llm_plan.call_count == 2
    # 第二次请求里必须带上第一次的报错
    assert "不合格" in nodes.llm_plan.joined_prompts()


def test_generate_plan_gives_up_after_two_bad_outputs():
    nodes = make_nodes(plan_script=[ai_text("不是JSON"), ai_text("还是不是JSON")])
    out = run(nodes.generate_plan(_req_state()))

    assert "error" in out
    assert "连续两次" in out["error"]
    assert nodes.llm_plan.call_count == 2


def test_generate_plan_prompt_lists_candidates_and_requirements():
    """候选清单必须进 prompt —— 那把"事后校验"提前成"事前约束"。"""
    nodes = make_nodes(plan_script=[ai_text(GOOD_DRAFT)])
    run(nodes.generate_plan(_req_state()))

    prompt = nodes.llm_plan.joined_prompts()
    assert POI_IDS[0] in prompt, "候选清单里要有 id"
    assert "成都" in prompt
    assert "只能从这里面选" in prompt
    assert DRAFT_SCHEMA_HINT in prompt
    assert "JSON" in prompt


def test_generate_plan_prompt_includes_repair_hint():
    nodes = make_nodes(plan_script=[ai_text(GOOD_DRAFT)])
    run(nodes.generate_plan(_req_state(repair_hint="- 第 1 天第 2 站的 poi_id 是编的")))
    assert "编的" in nodes.llm_plan.joined_prompts()


def test_generate_plan_survives_llm_exception():
    class Exploding(ScriptedChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            raise RuntimeError("超时")

    nodes = make_nodes()
    nodes.llm_plan = Exploding(script=[])
    out = run(nodes.generate_plan(_req_state()))
    assert "error" in out and "生成节点" in out["error"]


# ══════════════════════════════════════════════════════════════
#  ⑤ check_plan
# ══════════════════════════════════════════════════════════════


def test_check_plan_passes_when_all_ids_are_real():
    nodes = make_nodes()
    draft = {
        "title": "x",
        "days": [
            {
                "date": TODAY.isoformat(),
                "theme": "t",
                "stops": [
                    {
                        "poi_id": POI_IDS[0],
                        "arrive": "09:00",
                        "stay_min": 90,
                        "match_reason": "r",
                    }
                ],
            }
        ],
    }
    out = run(nodes.check_plan(_req_state(draft=draft)))

    assert out["blocking"] == []
    assert "check_rounds" not in out, "重排计数由 `repair` 累加，不是这里"
    assert out["trip"]["days"][0]["stops"][0]["poi_id"] == POI_IDS[0]
    assert out["validation"]["remaining"] == []


def test_check_plan_blocks_invented_poi_and_counts_round():
    nodes = make_nodes()
    draft = {
        "title": "x",
        "days": [
            {
                "date": TODAY.isoformat(),
                "theme": "t",
                "stops": [
                    {
                        "poi_id": "B0000_FAKE",
                        "arrive": "09:00",
                        "stay_min": 90,
                        "match_reason": "r",
                    }
                ],
            }
        ],
    }
    out = run(nodes.check_plan(_req_state(draft=draft)))

    assert len(out["blocking"]) == 1
    assert len(out["validation"]["remaining"]) == 1
    assert out["validation"]["remaining"][0]["level"] == "hard"
    assert out["trip"]["summary"]["hard_errors"] == 1


def test_check_plan_queries_weather_itself():
    """🔴 天气由**代码**查，不靠模型"想起来调工具"。

    硬判据不能建立在"模型是否碰巧调了某个工具"上 ——
    工具是给模型的建议路径，不是代码的事实来源。
    """
    provider = MockAmapProvider(today=TODAY)
    nodes = make_nodes(provider=provider)
    draft = {
        "title": "x",
        "days": [
            {
                "date": TODAY.isoformat(),
                "theme": "t",
                "stops": [
                    {"poi_id": POI_IDS[0], "arrive": "09:00", "stay_min": 90, "match_reason": "r"}
                ],
            }
        ],
    }
    out = run(nodes.check_plan(_req_state(draft=draft)))

    assert out["trip"]["days"][0]["weather"]["status"] == "ok"
    assert out["collected_weather"][TODAY.isoformat()]["status"] == "ok"


def test_check_plan_weather_unavailable_outside_window():
    """行程在 4 天窗口外 → `unavailable` + 说明。**不编天气、也不算通过。**"""
    provider = MockAmapProvider(today=TODAY)
    nodes = make_nodes(provider=provider)
    far = date(2026, 12, 1)
    draft = {
        "title": "x",
        "days": [
            {
                "date": far.isoformat(),
                "theme": "t",
                "stops": [
                    {"poi_id": POI_IDS[0], "arrive": "09:00", "stay_min": 90, "match_reason": "r"}
                ],
            }
        ],
    }
    out = run(nodes.check_plan(_req_state(draft=draft)))
    weather = out["trip"]["days"][0]["weather"]
    assert weather["status"] == "unavailable"
    assert weather["note"]


def test_check_plan_without_draft_reports_error():
    nodes = make_nodes()
    out = run(nodes.check_plan(_req_state()))
    assert out["error"] == "没有草稿可校验"


def test_check_plan_does_not_override_existing_error():
    """上游已经失败了（比如生成节点报错），这里别再盖一层新错误 ——
    用户该看到的是**根因**，不是最后一步的"没有草稿"。"""
    nodes = make_nodes()
    out = run(nodes.check_plan(_req_state(error="模型连续两次输出的草稿都不合格")))
    assert out == {}


def test_check_plan_rejects_malformed_draft():
    """`{"title": "x"}` 本身是**合法**的（`days` 有默认空列表）——
    真正非法的形状要用一个类型错的字段来构造。"""
    nodes = make_nodes()
    out = run(nodes.check_plan(_req_state(draft={"title": "x", "days": "不是数组"})))
    assert "error" in out
    assert "草稿结构不合法" in out["error"]


def test_check_plan_forwards_rounds_to_trip():
    nodes = make_nodes()
    draft = {
        "title": "x",
        "days": [
            {
                "date": TODAY.isoformat(),
                "theme": "t",
                "stops": [
                    {"poi_id": POI_IDS[0], "arrive": "09:00", "stay_min": 90, "match_reason": "r"}
                ],
            }
        ],
    }
    out = run(nodes.check_plan(_req_state(draft=draft, check_rounds=1)))
    assert out["trip"]["validation"]["rounds"] == 1


# ── M3：三态在**节点层**的表现 ─────────────────────────────


def _one_stop_draft(poi_id: str, day: date = TODAY, arrive: str = "09:00") -> dict:
    return {
        "title": "x",
        "days": [
            {
                "date": day.isoformat(),
                "theme": "t",
                "stops": [
                    {"poi_id": poi_id, "arrive": arrive, "stay_min": 90, "match_reason": "r"}
                ],
            }
        ],
    }


def test_check_plan_unknown_is_visible_but_never_blocks():
    """🔴 **三态在节点层的表现** —— M3 最该守住的一条。

    构造：青城山实测**没有** `open_time` → `open_today` 只能落 `unknown`。

    断言三件事，每一件对应一个具体的坑：

    1. `trip.days[0].stops[0].checks` 里**确实有**那条 unknown
       （否则前端画不出灰色"无法判定" —— 而"如实标灰"是这个项目的核心视觉语言）
    2. `blocking` **是空的**（否则 agent 会去打回重排，而"高德没给营业时间"
       它**永远修不好** → 白白烧两轮，最后降级输出）
    3. `validation.remaining` 里也没有它
       （`ValidationIssue.level` 只有 hard/soft 两个值，装不下 unknown；
        硬塞进去会让"前端把它当成一种风险"变成既成事实）
    """
    nodes = make_nodes()
    # 青城山在池子第 9 位，所以这里要用全量池（默认的 `pool_of(5)` 不含它）
    out = run(
        nodes.check_plan(
            _req_state(collected_pois=pool_of(9), draft=_one_stop_draft(POI_IDS[8]))
        )
    )

    stop_checks = out["trip"]["days"][0]["stops"][0]["checks"]
    statuses = {c["code"]: c["status"] for c in stop_checks}

    assert statuses.get("open_today") == "unknown", (
        f"青城山没有 open_time，open_today 必须落 unknown 而不是 {statuses.get('open_today')}"
    )
    assert statuses.get("poi_exists") == "pass", "它是池子里的真 POI，这条该绿"

    assert out["blocking"] == [], f"unknown 不该打回：{out['blocking']}"
    assert out["trip"]["summary"]["hard_errors"] == 0
    assert out["validation"]["remaining"] == [], "unknown 不是一种'剩余风险'"


class _SlowProvider(MockAmapProvider):
    """距离永远超限的假 provider —— 用来在节点层构造**跨天段超限**。

    ⚠️ 为什么不直接改 `MockAmapProvider.calc_distance`：
       mock 的距离数值是 2026-09-15 **实测校准过**的（三段路网系数）。
       为了造一个失败场景去改它，等于让"校准"这件事失效 ——
       下次真数据变了，没人分得清那是模型偏差还是有人在测试里动过手。
       测试需要极端值时，**在测试里覆盖**，不污染被校准的实现。
    """

    async def calc_distance(self, origin, dest):  # type: ignore[override]
        return DistanceResult(km=200.0, drive_min=200, straight_km=150.0)


def test_check_plan_actually_checks_cross_day_hop():
    """🔴 `check_plan` 必须**真的把 `distance_fn` 传下去**，否则远郊首站没人判。

    这是 M3 之前一直漏掉的那个缺口（见 `validate.check_cross_day_hop` 的注释）：
    `Stop.from_prev_drive_min` 对当天第 1 站**恒为 0**，所以
    "第 2 天一早从市区开去青城山"这一大段车程，所有单跳判据都看不见。

    这条测试的价值在于它**能红**：把 `check_plan` 里那个
    `distance_fn=self.provider.calc_distance` 参数删掉，它立刻失败。
    """
    nodes = make_nodes(provider=_SlowProvider(today=TODAY))
    draft = {
        "title": "x",
        "days": [
            {
                "date": TODAY.isoformat(),
                "theme": "市区",
                "stops": [
                    {
                        "poi_id": POI_IDS[1],  # 锦里（紧挨武侯祠，算"市区那晚的落脚点"）
                        "arrive": "09:00",
                        "stay_min": 90,
                        "match_reason": "r",
                    }
                ],
            },
            {
                "date": TODAY.isoformat(),
                "theme": "远郊",
                "stops": [
                    {
                        "poi_id": POI_IDS[8],  # 青城山
                        "arrive": "09:00",
                        "stay_min": 90,
                        "match_reason": "r",
                    }
                ],
            },
        ],
    }
    out = run(nodes.check_plan(_req_state(collected_pois=pool_of(9), draft=draft)))

    assert out["blocking"], "第 2 天第一站要开 200 分钟，必须被打回"
    reason = out["blocking"][0]
    assert "第 2 天" in reason, f"要指出是哪一天：{reason}"
    assert "昨天的最后一站" in reason, f"要说清这是跨天段（不是当天某一跳）：{reason}"
    assert out["trip"]["summary"]["hard_errors"] == len(out["blocking"])
    assert out["validation"]["remaining"], "硬错要同时进 remaining（前端看得到风险清单）"


# ══════════════════════════════════════════════════════════════
#  ⑥ repair
# ══════════════════════════════════════════════════════════════


def test_repair_writes_error_back_and_clears_blocking():
    nodes = make_nodes()
    out = run(
        nodes.repair(
            make_state(blocking=["第 1 天第 2 站的 poi_id `X` 不在候选池里（不是工具搜出来的），已拒绝"])
        )
    )

    text = out["messages"][0].content
    assert "X" in text
    assert "search_poi" in text
    assert "不要凭记忆填任何地点" in text
    assert out["blocking"] == [], "错误已经写进消息，blocking 该清（会被 check_plan 重算）"
    assert "X" in out["repair_hint"]
    assert out["check_rounds"] == 1, "重排轮数由 repair 累加（不是 check_plan）"


def test_repair_hint_isolated_from_messages():
    """打回原因走专用通道 —— 因为 `generate_plan` **刻意不读对话历史**
    （避免同一份 POI 信息在历史与清单里出现两次、模型分不清哪份权威）。"""
    nodes = make_nodes()
    out = run(nodes.repair(make_state(blocking=["问题A"])))
    assert "问题A" in out["repair_hint"]


# ══════════════════════════════════════════════════════════════
#  ⑨ render
# ══════════════════════════════════════════════════════════════


def test_render_accepts_valid_trip_and_writes_nothing():
    """正常的 trip 应该静默通过（`render` 的返回值是"没有要改的"）。"""
    nodes = make_nodes()
    trip, _ = run(
        assemble_trip(_empty_draft(), PoiPool(), MockAmapProvider(today=TODAY), destination="成都")
    )
    out = run(nodes.render(make_state(trip=trip.model_dump(mode="json"))))
    assert out == {}


def test_render_flags_type_drift():
    """🔴 序列化往返检查 —— `state.py` 那条设计纪律的兑现。

    如果某个节点往 state 里塞了非契约形状的东西，在这里就炸出来，
    而不是等 M5 接了 checkpointer 之后表现为"恢复后行为不一致"。
    """
    nodes = make_nodes()
    out = run(nodes.render(make_state(trip={"trip_id": "x"})))
    assert "error" in out
    assert "序列化往返失败" in out["error"]


def test_render_noop_without_trip():
    nodes = make_nodes()
    assert run(nodes.render(make_state())) == {}


# ══════════════════════════════════════════════════════════════
#  构造期
# ══════════════════════════════════════════════════════════════


def test_nodes_indexes_tools_by_name():
    """M4 后主 agent 的工具集里**没有 search_poi**（搜索整体移交给子 agent，D5）。"""
    nodes = make_nodes()
    assert set(nodes._tools_by_name) == {"task", "get_weather", "calc_distance"}


def test_nodes_works_without_tools():
    """没有工具时也必须能构造（`bind_tools` 会退化成裸模型）——
    否则"删掉一个工具"会让整个图编译不过。"""
    nodes = make_nodes(tools=[])
    assert nodes._agent_llm is nodes.llm_tool


def test_boom_provider_satisfies_protocol():
    """顺手确认 `MockAmapProvider` 与 `BoomProvider` 都满足 `AmapProvider` 协议
    （`Protocol` 是结构化检查，不需要继承）。"""
    from app.providers.base import AmapProvider

    assert isinstance(MockAmapProvider(today=TODAY), AmapProvider)
    assert isinstance(BoomProvider(), AmapProvider)
