"""候选池质量实测 —— 回答「①typecode 过滤 / ②只标注类型 / ③types 定向搜」。

═══════════════════════════════════════════════════════════════
 为什么必须实测，不能推理
═══════════════════════════════════════════════════════════════

"池子里有噪音"是已确认的事实（D36），但**选哪个解法**推理不出来 ——
三个方案的差异全在"噪音的分布形状"上，而形状只能量出来。

第一轮实测（2026-09-15）就**推翻了两条我原本的直觉**：

1. 噪音不是均匀的，是**双峰**的：泛词（"古迹"/"博物馆"）有用率 85~100%，
   而**具体地名**（"武侯祠"/"宽窄巷子"）只有 20~40%。原因也不难懂：
   搜"武侯祠"时高德会把**地铁站 / 公交站 / 售票处 / 道路名 / 写字楼**全带出来。
2. `type` / `typecode` **可以是 `|` 分隔的多值**（"宽窄巷子景区" =
   `购物服务;特色商业街;特色商业街` **`|`** `风景名胜;风景名胜相关;旅游景点`）。
   只看第一段会把**真景点判成噪音** → 按一级大类做的白名单会误杀宽窄巷子。
═══════════════════════════════════════════════════════════════
 测量口径（写死在这里，否则三个月后没人能复现）
═══════════════════════════════════════════════════════════════

**majors** = `type` 按 `|` 拆段、每段取 `;` 第一段，去重后的集合。

**rank（可游玩性分层）** —— 取所有段里**最靠前**的那一档：

| rank | 大类 | 含义 |
|---|---|---|
| 0 | 风景名胜 / 公园广场 | 景点本体 |
| 1 | 科教文化服务 | 博物馆 / 美术馆 |
| 2 | 体育休闲服务 | 游乐场 / 运动场馆 |
| 3 | 餐饮服务 / 购物服务 | 吃 / 逛 |
| 4 | 住宿服务 | 睡觉（行程不排，但不该丢） |
| 5 | 其余（交通 / 地名地址 / 生活 / 政企 / 医疗 / 商务 …） | 对旅游规划无用 |

**有用率**按**场景**算，不按全局 —— 这是最容易做错的地方：
「餐饮服务」搜"古迹"时是噪音、搜"火锅"时是目标。所以场景定义里带 `want_slots`。

**top-N 口径跟线上一致**（`MAX_CANDIDATES = 8`）—— 这才是模型真正看到的窗口。

⚠️ 另一个容易做错的：**"有用率低"不等于"对模型有害"**。真正决定模型选错的，
是**期望目标掉出 top-N**。所以报告里 `期望目标排位` 这一列比"有用率"更重要。

用法：
    python scripts/probe_pool_quality.py              # 采集 + 分析
    python scripts/probe_pool_quality.py --analyse    # 只分析（读 fixture，不联网）
    python scripts/probe_pool_quality.py --types      # 额外采 types 定向搜样本
    python scripts/probe_pool_quality.py --geo --skip-collect   # 额外采地理约束样本

四节能回答不同问题的实测（前三节支撑 D39/D40/D41，第四节支撑第六章那条悬而未决）：

| 节 | 回答什么 | 结论落点 |
|---|---|---|
| 一、逐关键词 | 噪音长什么样、挤占了窗口多少席 | D39 |
| 二、★ 策略对比 | 四种方案谁的窗口最干净 | D40 |
| 三、types 定向搜 | 类别码能不能替代关键词搜 | D41（否） |
| 四、地理约束 | `location`+`radius` 能不能纠正返回范围 | 第六章悬而未决（能，但中心点取不到） |
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.providers.amap import parse_poi  # noqa: E402
from app.tools.poi_rank import majors_of, rank_of  # noqa: E402

# ⚠️ `majors_of` / `rank_of` **从 app 里 import，不在这里重写一遍**。
#    脚本和产品各留一份规则的结果是"实测数据对不上线上行为"，
#    而且漂移时**不会报错** —— 那种错最难发现。
#    这个脚本里自己写的只有"测量口径"（slot / 硬噪音 / 命中判定），
#    那些是**评测概念**，不属于产品逻辑。

BASE = "https://restapi.amap.com"
FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "amap" / "pool_quality"

#: 与 `probe_amap.py` 同口径。实测 3 QPS 是滑动窗口，留 4 倍余量。
THROTTLE_S = 1.3

#: 一次取回多少条。**比线上的 8 大** —— 要看分布就必须拿到足够长的尾巴，
#: 否则"噪音挤占前排"这种结论量不出来（尾巴被截掉了）。
BATCH = 20

#: 线上给模型的窗口。`top-N` 指标全部按这个口径算。
TOP_N = 8


# ══════════════════════════════════════════════════════════════
#  分类：majors / rank / slot
# ══════════════════════════════════════════════════════════════

CORE = "core"
MEAL = "meal"
LODGING = "lodging"
NOISE = "noise"

#: 「硬噪音」—— 对**任何**旅游规划场景都不可能是站点的类别。
#: 判定用 `all`（所有段都在黑名单里才剔）：这样 `购物服务|地名地址信息`
#: 这种多值条目会被保住，不会因为"顺带带了个道路名"就被丢掉。
HARD_NOISE_MAJORS = frozenset(
    {
        "交通设施服务",
        "地名地址信息",
        "生活服务",
        "政府机构及社会团体",
        "公司企业",
        "医疗保健服务",
        "金融保险服务",
        "汽车服务",
        "商务住宅",
        "道路附属设施",
    }
)


def slot_of(type_str: str | None) -> str:
    """**统计口径**：这条在"能当行程一站"的意义上算什么。

    ⚠️ 与 `rank_of` 的关系：rank 是**排序**用的连续档位，slot 是**统计**用的类别。
    同一条件目不能在两处得出不同结论（那会让"实测有用率"和"线上窗口质量"对不上），
    所以这里**直接从 `rank_of` 推导**，不自己再查一遍表。

    唯一的补充：rank 3 把"吃"和"逛"混在一起了 —— 统计时分开才看得出场景差异
    （搜"火锅"时餐饮是目标，搜"古迹"时是噪音）。
    """
    r = rank_of(type_str)
    if r <= 2:
        return CORE
    if r == 3:
        return MEAL if "餐饮服务" in majors_of(type_str) else NOISE
    if r == 4:
        return LODGING
    return NOISE


def is_hard_noise(type_str: str | None) -> bool:
    majors = majors_of(type_str)
    return bool(majors) and all(m in HARD_NOISE_MAJORS for m in majors)


#: 「附属设施」后缀。命中判定的关键 —— 名字里含目标词但其实是附属设施的条目很多：
#: `成都武侯祠博物馆东侧门售票处` / `都江堰景区站(公交站)` / `宽窄巷子景区西门地下停车场`。
#: 它们**包含**目标名，但**不是**目标本身。
_SUFFIX_NOISE = (
    "售票处", "游客中心", "服务中心", "咨询中心", "停车场", "公交站", "地铁站",
    "出口", "站", "大门", "园林区", "文物区", "东区", "西区", "南区", "北区",
    "大道", "大街", "横街", "东街", "西街", "南街", "北街",
)


def hit(text: str, want: str, *, tail_len: int = 10) -> bool:
    """判定 `want` 是否真的**指代**了这条目。

    ⚠️ 不能只做包含匹配：「武侯祠地铁站」包含「武侯祠」。做法是把 `want` 之后
    的一小段尾巴截出来，只要尾巴里出现附属设施词就判**不命中**。

    粗糙但方向是**故意**的：宁可漏算命中，不可多算 —— 多算会让"误杀"那一列
    出现假阳性，从而把"过滤"这个方案冤枉成危险的。
    """
    idx = text.find(want)
    if idx < 0:
        return False
    tail = text[idx + len(want) : idx + len(want) + tail_len]
    return not any(word in tail for word in _SUFFIX_NOISE)


def matches_any(text: str, wants: tuple[str, ...]) -> list[str]:
    return [w for w in wants if hit(text, w)]



# ══════════════════════════════════════════════════════════════
#  场景样本
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Case:
    """一个"模型可能会搜的关键词" + 它的期望口径。"""

    keyword: str
    scene: str
    want_slots: frozenset[str]
    expect_names: tuple[str, ...] = ()
    """该关键词下**人眼认为该出现**的地点。

    ⚠️ 写成**足够长的片段**（"武侯祠博物馆" 而不是 "武侯祠"）——
    包含匹配太松会把「武侯祠(地铁站)」也算命中，那样"召回"这一列就没意义了。
    ⚠️ 列表必然不全（成都景点我列不完）→ 只能当**下界**读，不能当分母。
    """


def _cases() -> list[Case]:
    sight = frozenset({CORE})
    eat = frozenset({MEAL})
    return [
        # ── 泛词（模型在"想去看看古迹"这类需求下最可能输出的形态）────────
        Case(
            "古迹", "历史文化", sight,
            ("武侯祠博物馆", "杜甫草堂博物馆", "青羊宫", "文殊院", "锦里", "宽窄巷子景区", "望江楼"),
        ),
        Case("成都景点", "泛需求", sight, ()),
        Case("公园", "休憩", sight, ("人民公园", "浣花溪", "望江楼", "塔子山", "青龙湖", "桂溪")),
        Case(
            "博物馆", "文化", sight,
            ("成都博物馆", "四川博物院", "自然博物馆", "武侯祠博物馆", "杜甫草堂博物馆", "金沙遗址"),
        ),
        Case("寺庙", "宗教", sight, ("文殊院", "大慈寺", "青羊宫", "昭觉寺", "宝光寺")),
        Case("打卡", "网红/观光", sight, ()),
        # ── 具体名（对照组：证明"噪音是关键词形态带来的，不是接口本身的毛病"）──
        Case("武侯祠", "具体名", sight, ("武侯祠博物馆",)),
        Case("宽窄巷子", "具体名", sight, ("宽窄巷子景区",)),
        Case("熊猫基地", "具体名", sight, ("大熊猫繁育研究基地",)),
        # ── 特殊场景 ──────────────────────────────────────────────
        Case(
            "亲子", "带小孩", sight,
            ("大熊猫", "欢乐谷", "海洋公园", "国色天乡", "动物园", "融创"),
        ),
        Case("都江堰", "郊区目的地", sight, ("都江堰景区", "青城山", "街子", "灌县")),
        # ── 生活场景（口径不同：这里"吃"才是目标）──────────────────
        Case("火锅", "吃饭", eat),
        Case("小吃", "吃饭", eat),
    ]


# ══════════════════════════════════════════════════════════════
#  采集
# ══════════════════════════════════════════════════════════════

_last_call = 0.0


def _get(path: str, **params: Any) -> dict[str, Any]:
    """打一发高德 GET。只拿 JSON，不解释错误码（那是 provider 的职责）。"""
    global _last_call
    wait = THROTTLE_S - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()

    params["key"] = settings.amap_webservice_key
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"status": "0", "info": f"HTTPError {exc.code}", "infocode": "HTTP"}


def _slug(text: str) -> str:
    return "".join(ch for ch in text if ch.isalnum()) or "x"


def _save(name: str, payload: dict[str, Any]) -> Path:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURE_DIR / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def collect(cases: list[Case], *, city: str) -> None:
    """按关键词采集原始响应。**原样落盘** —— 裁剪过的样本复现不了"噪音"这个现象。"""
    print(f"\n采集 {len(cases)} 个关键词（offset={BATCH}，city={city}）…")
    for i, case in enumerate(cases, 1):
        payload = _get(
            "/v3/place/text",
            keywords=case.keyword,
            city=city,
            offset=BATCH,
            extensions="all",
        )
        path = _save(f"kw_{_slug(case.keyword)}.json", payload)
        n = len(payload.get("pois") or [])
        flag = "✅" if payload.get("status") == "1" else "❌"
        print(f"  [{i:>2}/{len(cases)}] {flag} 「{case.keyword}」 {n} 条  → {path.name}")


def collect_types(pairs: list[tuple[str, str]], *, city: str) -> None:
    """按**类别码**定向搜。`pairs` 是 (类别码, 中文名)。"""
    print(f"\n定向搜 {len(pairs)} 个类别码…")
    for code, label in pairs:
        payload = _get(
            "/v3/place/text",
            types=code,
            city=city,
            offset=BATCH,
            extensions="all",
        )
        path = _save(f"types_{code}.json", payload)
        n = len(payload.get("pois") or [])
        flag = "✅" if payload.get("status") == "1" else "❌"
        print(f"  {flag} types={code}（{label}） {n} 条  → {path.name}")


#: 天府广场（成都市中心）。用它当「用户实际想逛的范围」的中心。
CITY_CENTER = "104.0657,30.6595"

#: 地理约束探针的样本：(关键词, location+radius 还是 None)。
#: 加不加各跑一次，**成对**才能证明 `location` 这个参数到底起不起作用。
#: 🔴 `都江堰` 那两条是**副作用探针**：都江堰距市中心 57km，如果加了 `location`
#:    就再也搜不到它，那"无脑加地理约束"就是**错的** —— 必须做成让模型自己选。
GEO_CASES: tuple[tuple[str, str | None, int], ...] = (
    ("公园", None, 0),
    ("公园", CITY_CENTER, 15_000),
    ("公园", CITY_CENTER, 25_000),
    ("博物馆", CITY_CENTER, 15_000),
    ("古迹", CITY_CENTER, 15_000),
    ("都江堰", CITY_CENTER, 15_000),
    ("都江堰", CITY_CENTER, 25_000),
    ("都江堰", None, 0),
)


def collect_geo(*, city: str) -> None:
    """验证 `location` + `radius` 能不能把"返回范围"收进用户真正要逛的城区。

    动机：实测发现「公园」返回的 20 条**全在高新区/天府新区**，
    而市中心的人民公园压根没出现 —— 猜测是排序按距离，给了 `location` 就能纠正。
    """
    print(f"\n地理约束探针（center={CITY_CENTER}）…")
    for keyword, location, radius_m in GEO_CASES:
        if location:
            tag = f"loc{radius_m // 1000}"
        else:
            tag = "noloc"
        params: dict[str, Any] = {"keywords": keyword, "city": city, "offset": BATCH, "extensions": "all"}
        if location:
            params["location"] = location
            params["radius"] = radius_m
        payload = _get("/v3/place/text", **params)
        path = _save(f"geo_{_slug(keyword)}_{tag}.json", payload)
        n = len(payload.get("pois") or [])
        flag = "✅" if payload.get("status") == "1" else "❌"
        print(f"  {flag} 「{keyword}」{tag} {n} 条  → {path.name}")


# ══════════════════════════════════════════════════════════════
#  分析
# ══════════════════════════════════════════════════════════════


@dataclass
class Row:
    """一个关键词的测量结果。"""

    case: Case
    parsed: list[Any]

    @property
    def n(self) -> int:
        return len(self.parsed)

    def slots(self) -> list[str]:
        return [slot_of(p.type) for p in self.parsed]

    def majors(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for p in self.parsed:
            for m in majors_of(p.type) or ["(无)"]:
                out[m] = out.get(m, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    def window(self, n: int = TOP_N) -> list[Any]:
        return self.parsed[:n]

    def useful(self, pois: list[Any]) -> int:
        return sum(1 for p in pois if slot_of(p.type) in self.case.want_slots)

    def filtered(self) -> list[Any]:
        """模拟「硬黑名单过滤」：剔掉对任何旅游场景都不可能是站点的类别。"""
        return [p for p in self.parsed if not is_hard_noise(p.type)]

    def reranked(self) -> list[Any]:
        """模拟「按可游玩性重排」：只改顺序，不删任何条目（`sorted` 稳定）。"""
        return sorted(self.parsed, key=lambda p: rank_of(p.type))

    def expect_ranks(self, pois: list[Any] | None = None) -> dict[str, int | None]:
        """每个期望目标在给定序列里的名次（1 起）。**这才是真正重要的指标** ——
        "池子里有噪音"和"模型会把噪音当成目标"是两回事，后者取决于目标排第几。"""
        seq = pois if pois is not None else self.parsed
        out: dict[str, int | None] = {}
        for want in self.case.expect_names:
            found = None
            for i, p in enumerate(seq, 1):
                text = (p.name or "") + " " + " ".join(p.alias or [])
                if hit(text, want):
                    found = i
                    break
            out[want] = found
        return out

    def dead_hard_noise(self) -> list[str]:
        """被硬黑名单剔掉、但**本来命中了期望目标**的条目 —— 误杀清单（应为空）。"""
        killed = []
        for p in self.parsed:
            if not is_hard_noise(p.type):
                continue
            text = (p.name or "") + " " + " ".join(p.alias or [])
            if matches_any(text, self.case.expect_names):
                killed.append(f"{p.name}[{'|'.join(majors_of(p.type))}]")
        return killed


def load_rows(cases: list[Case]) -> list[Row]:
    rows: list[Row] = []
    for case in cases:
        path = FIXTURE_DIR / f"kw_{_slug(case.keyword)}.json"
        if not path.exists():
            print(f"  ⚠️ 缺样本，跳过：「{case.keyword}」（先跑一次不带 --analyse 的）")
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw = [p for p in (payload.get("pois") or []) if isinstance(p, dict)]
        parsed = [x for x in (parse_poi(r) for r in raw) if x is not None]
        rows.append(Row(case=case, parsed=parsed))
    return rows


def _pct(part: int, whole: int) -> str:
    return f"{part / whole * 100:5.1f}%" if whole else "  n/a"


def report(rows: list[Row]) -> None:
    print("\n" + "═" * 96)
    print(f"  逐关键词：噪音分布 + top-{TOP_N} 挤占 + 期望目标排位")
    print("═" * 96)

    for row in rows:
        c = row.case
        print(f"\n「{c.keyword}」（{c.scene}，目标 slot={sorted(c.want_slots)}）  取回 {row.n} 条")
        if not row.n:
            print("   ⚠️ 空结果")
            continue

        print("   大类分布: " + " / ".join(f"{k} {v}" for k, v in row.majors().items()))

        slots = row.slots()
        counts = {s: slots.count(s) for s in (CORE, MEAL, LODGING, NOISE)}
        print(
            f"   全体    : core {counts[CORE]}  meal {counts[MEAL]}  "
            f"lodging {counts[LODGING]}  noise {counts[NOISE]}"
            f"   → 有用 {row.useful(row.parsed)}/{row.n}（{_pct(row.useful(row.parsed), row.n)}）"
        )

        win = row.window()
        print(
            f"   top-{TOP_N}   : 原序有用 {row.useful(win)}/{len(win)}"
            f"   |  过滤后 {row.useful(row.filtered()[:TOP_N])}/{len(row.filtered()[:TOP_N])}"
            f"   |  重排后 {row.useful(row.reranked()[:TOP_N])}/{len(row.reranked()[:TOP_N])}"
        )

        kept = row.filtered()
        print(f"   硬黑名单: 池子 {row.n} → {len(kept)} 条（丢掉 {row.n - len(kept)}）")

        if c.expect_names:
            base = row.expect_ranks()
            rer = row.expect_ranks(row.reranked())
            parts = []
            for want in c.expect_names:
                b, r = base[want], rer[want]
                if b is None and r is None:
                    continue
                mark = ""
                if b is not None and b > TOP_N and r is not None and r <= TOP_N:
                    mark = " 👍排进窗口"
                parts.append(f"{want}#{b if b else '✗'}→{r if r else '✗'}{mark}")
            hit = sum(1 for v in base.values() if v is not None)
            in_win = sum(1 for v in base.values() if v is not None and v <= TOP_N)
            print(f"   期望命中: {hit}/{len(c.expect_names)}（其中 {in_win} 个在原序 top-{TOP_N}）")
            if parts:
                print("   排位(原序→重排): " + "  ".join(parts))
            missed = [w for w, v in base.items() if v is None]
            if missed:
                print(f"   完全没出现: {missed}")

        killed = row.dead_hard_noise()
        if killed:
            print(f"   🔴 硬黑名单误杀期望目标: {killed}")

    _summary(rows)
    _strategy_compare(rows)
    _types_report()
    _geo_report()
    _typecode_frequency(rows)


def _summary(rows: list[Row]) -> None:
    print("\n" + "═" * 96)
    print("  汇总")
    print("═" * 96)

    n = sum(r.n for r in rows)
    useful = sum(r.useful(r.parsed) for r in rows)
    print(f"\n  全体条目 {n}，有用 {useful}（{_pct(useful, n)}），噪音 {n - useful}")

    generic = [r for r in rows if r.case.scene not in ("具体名", "吃饭")]
    concrete = [r for r in rows if r.case.scene == "具体名"]
    for label, group in (("泛词/场景词", generic), ("具体地名", concrete)):
        if not group:
            continue
        g_n = sum(r.n for r in group)
        g_u = sum(r.useful(r.parsed) for r in group)
        g_win = sum(len(r.window()) for r in group)
        g_wu = sum(r.useful(r.window()) for r in group)
        print(
            f"  {label:10} 有用率 {_pct(g_u, g_n)}   top-{TOP_N} 有用率 {_pct(g_wu, g_win)}"
            f"   （{len(group)} 个关键词）"
        )

    all_killed = [(r.case.keyword, k) for r in rows for k in r.dead_hard_noise()]
    print(f"\n  硬黑名单误杀期望目标：{len(all_killed)} 处" + (f" → {all_killed}" if all_killed else " ✅"))


def _strategy_compare(rows: list[Row]) -> None:
    """★ 决策表：四种策略在「模型看到的窗口」里各有多少有用条目。"""
    print("\n" + "═" * 96)
    print(f"  ★ 策略对比（口径：top-{TOP_N} 窗口里「有用」的条数 + 期望目标是否落进窗口）")
    print("═" * 96)

    def s0(r: Row) -> list[Any]:
        return r.window()

    def s1(r: Row) -> list[Any]:
        return r.window()  # 顺序不变，只是在文本里多带一个"类型"字段

    def s2(r: Row) -> list[Any]:
        return r.filtered()[:TOP_N]

    def s3(r: Row) -> list[Any]:
        return r.reranked()[:TOP_N]

    strategies = [
        ("S0 现状（keywords 原序，不标注）", s0),
        ("S1 + 标注类型（顺序不变）", s1),
        ("S2 硬黑名单过滤（剔 9 类硬噪音）", s2),
        ("S3 按可游玩性重排（不删条目）", s3),
    ]
    #: 每个策略下"用来算期望目标排位"的序列（窗口截断前）
    seqs = [lambda r: r.parsed, lambda r: r.parsed, lambda r: r.filtered(), lambda r: r.reranked()]

    hdr = f"  {'策略':<34} {'窗口有用率':>10} {'窗口 core 数':>12} {'目标落进窗口':>12}"
    print(hdr)
    print("  " + "-" * (len(hdr) + 12))
    for idx, (label, fn) in enumerate(strategies):
        seq_fn = seqs[idx]
        tot = core = 0
        tgt_in = tgt_all = 0
        for r in rows:
            window = fn(r)
            tot += len(window)
            core += sum(1 for p in window if slot_of(p.type) == CORE)
            if r.case.expect_names:
                for v in r.expect_ranks(seq_fn(r)).values():
                    tgt_all += 1
                    if v is not None and v <= TOP_N:
                        tgt_in += 1
        rate = f"{sum(r.useful(fn(r)) for r in rows) / tot * 100:.1f}%" if tot else "n/a"
        tgt = f"{tgt_in}/{tgt_all}" if tgt_all else "n/a"
        print(f"  {label:<34} {rate:>10} {core:>12} {tgt:>12}")

    print("\n  说明：")
    print("   · S1 与 S0 数值相同是**预期的** —— 标注不改变条目，它改变的是**模型手上的信息**")
    print("     （模型知道第 3 条是地铁站之后，就不会选它）。所以 S1 的收益量不出来，")
    print("     只能靠「减少误选」这个方向论证，属于**零成本保险**，不是主要收益来源。")
    print("   · S2 与 S3 才是真正改变窗口的手段。S3 不删条目 → 池子（校验用）不受影响，")
    print("     这是它相对 S2 的结构性优势：**不会因为分类错就丢掉真景点**。")


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """直线距离。自己写而不是 import provider 的私有函数 —— 脚本不该绑内部实现。"""
    radius = 6371.0088
    lng1, lat1 = a
    lng2, lat2 = b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(h))


def _geo_report() -> None:
    """对比「加 location+radius」与「不加」—— 验证能不能纠正返回范围。"""
    files = sorted(FIXTURE_DIR.glob("geo_*.json"))
    if not files:
        return

    center = tuple(float(x) for x in CITY_CENTER.split(","))
    groups: dict[str, dict[str, list[Any]]] = {}
    for path in files:
        stem = path.stem.removeprefix("geo_")
        keyword, _, tag = stem.rpartition("_")
        payload = json.loads(path.read_text(encoding="utf-8"))
        parsed = [
            x for x in (parse_poi(p) for p in (payload.get("pois") or []) if isinstance(p, dict)) if x
        ]
        groups.setdefault(keyword, {})[tag] = parsed

    print("\n" + "═" * 96)
    print("  ④ 地理约束（location + radius）实测")
    print("═" * 96)
    print("  指标：返回条目的「距市中心直线距离中位数」——越小说明返回得越靠城区")

    for keyword, variants in groups.items():
        print(f"\n「{keyword}」")
        for tag in sorted(variants):
            pois = variants[tag]
            if not pois:
                continue
            dists = sorted(_haversine_km(center, (p.lng, p.lat)) for p in pois)
            median = dists[len(dists) // 2]
            print(f"  {tag:>6}  {len(pois)} 条  中位距市中心 {median:6.1f} km")
            print(f"         前 8: {[p.name for p in pois[:TOP_N]]}")

        if "noloc" in variants:
            base = {p.poi_id for p in variants["noloc"]}
            for tag in sorted(variants):
                if tag == "noloc":
                    continue
                other = {p.poi_id for p in variants[tag]}
                print(f"  与 noloc 重合 {len(base & other)}/{len(base | other)} 条")


def _types_report() -> None:
    """对比 `types=类别码` 定向搜（如果采过）。"""
    files = sorted(FIXTURE_DIR.glob("types_*.json"))
    if not files:
        return

    print("\n" + "═" * 96)
    print("  ③ types 定向搜 实测")
    print("═" * 96)
    for path in files:
        code = path.stem.removeprefix("types_")
        payload = json.loads(path.read_text(encoding="utf-8"))
        parsed = [
            x for x in (parse_poi(p) for p in (payload.get("pois") or []) if isinstance(p, dict)) if x
        ]
        majors: dict[str, int] = {}
        for p in parsed:
            for m in majors_of(p.type) or ["(无)"]:
                majors[m] = majors.get(m, 0) + 1
        top_major = max(majors.values()) if majors else 0
        purity = top_major / len(parsed) * 100 if parsed else 0
        print(f"\n  types={code}  取回 {len(parsed)} 条  最大类占比 {purity:.0f}%")
        print(f"   大类: {majors}")
        print(f"   前 8: {[p.name for p in parsed[:TOP_N]]}")


def _typecode_frequency(rows: list[Row]) -> None:
    """数出 core / 噪音 typecode 的出现频次 —— ①的规则草案就来自这里。"""
    print("\n" + "═" * 96)
    print("  typecode 频次（按 rank 分层）")
    print("═" * 96)

    freq: dict[int, dict[tuple[str, str], int]] = {}
    for row in rows:
        for p in row.parsed:
            r = rank_of(p.type)
            key = (p.typecode or "?", p.type or "?")
            freq.setdefault(r, {})
            freq[r][key] = freq[r].get(key, 0) + 1

    for r in sorted(freq):
        label = {0: "景点本体", 1: "文化场馆", 2: "体育休闲", 3: "吃/逛", 4: "住宿", 5: "硬噪音"}[r]
        print(f"\n  ── rank {r}（{label}）")
        for (code, type_str), count in sorted(freq[r].items(), key=lambda kv: -kv[1])[:12]:
            print(f"    {count:>3}×  {code:<18} {type_str}")


# ══════════════════════════════════════════════════════════════


def main() -> None:
    ap = argparse.ArgumentParser(description="候选池质量实测")
    ap.add_argument("--city", default="成都")
    ap.add_argument("--analyse", action="store_true", help="只读 fixture 分析，不联网")
    ap.add_argument("--types", action="store_true", help="额外采 types 定向搜样本")
    ap.add_argument("--geo", action="store_true", help="额外采 location+radius 地理约束样本")
    ap.add_argument("--skip-collect", action="store_true", help="跳过 13 个关键词，只跑附加探针")
    args = ap.parse_args()

    cases = _cases()

    if not args.analyse:
        if not settings.amap_webservice_key:
            print("❌ AMAP_WEBSERVICE_KEY 为空")
            raise SystemExit(1)
        if not args.skip_collect:
            collect(cases, city=args.city)
        if args.types:
            collect_types(
                [
                    ("110000", "风景名胜（全域）"),
                    ("110200", "风景名胜"),
                    ("140100", "博物馆"),
                    ("050100", "中餐厅"),
                ],
                city=args.city,
            )
        if args.geo:
            collect_geo(city=args.city)

    report(load_rows(cases))


if __name__ == "__main__":
    main()
