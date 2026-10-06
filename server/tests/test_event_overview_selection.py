"""Market-data rows must not consume event-overview paid attempts."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "run_event_overviews.py"
)
SPEC = importlib.util.spec_from_file_location("run_event_overviews", SCRIPT_PATH)
overviews_script = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = overviews_script
SPEC.loader.exec_module(overviews_script)


def test_price_benchmark_rows_are_market_data() -> None:
    assert overviews_script.is_market_data_title("9月21日生意社石油焦基准价为3474.00元/吨")
    assert overviews_script.is_market_data_title("09月23日荣盛石化涤纶POY为9550元")
    assert overviews_script.is_market_data_title("9月23日中质含硫原油交割仓库数量持平")
    assert overviews_script.is_market_data_title("生意社：9月23日中质含硫原油交割仓库数量持平")
    assert overviews_script.is_market_data_title("EIA：9月23日国际原油期货收涨")
    assert overviews_script.is_market_data_title("9月24日生意社石脑油基准价为9283.33元/吨")


def test_real_event_titles_are_not_filtered() -> None:
    assert not overviews_script.is_market_data_title(
        "Oil settles up around 4% as Iran's president vows to never surrender - Reuters"
    )
    # 月份开头但无具体日期的标题是新闻叙述，不是行情行。
    assert not overviews_script.is_market_data_title("8月马来西亚棕榈油库存连续第五个月上升")
    assert not overviews_script.is_market_data_title(
        "9月全球炼厂检修集中，亚洲石脑油供应收紧带动下游补库——机构观点"
    )
    # 日期开头但长且含事件性表述的内容不是市场数据行。
    assert not overviews_script.is_market_data_title(
        "9月24日讯：据市场消息，某大型炼化一体化装置今日突发故障停车，涉及产能"
        "较大，具体恢复时间待确认，后续将持续跟踪对 PX 与石脑油供需的影响"
    )
