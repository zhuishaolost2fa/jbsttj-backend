"""剧本杀筛选维度与选项的**唯一数据源**。

建表 SQL 生成、数据灌入、离线兜底都从这里取数，避免多处维护导致漂移。

选项体系参考当前主流剧本杀社区（小黑探等）与线下门店的通行叫法整理：
  - 玩法：本格/变格这类「案件形态」与欢乐/情感/阵营这类「体验类型」混编，
          这是玩家实际选本时的心智模型，不做学院派拆分。
  - 人数 / 时长：除展示文案外额外携带 `min_value` / `max_value` 数值区间，
          后端可直接把选项翻译成真实范围查询，前端无需硬编码任何数字。
"""

from __future__ import annotations

from typing import Any, Dict, List

# 维度编码常量，供路由与业务层引用，避免手写字符串拼错
CATEGORY_PLAYSTYLE = "playstyle"
CATEGORY_THEME = "theme"
CATEGORY_RELEASE = "release"
CATEGORY_DIFFICULTY = "difficulty"
CATEGORY_PLAYER_COUNT = "player_count"
CATEGORY_DURATION = "duration"

UNIT_PERSON = "person"
UNIT_MINUTE = "minute"


CATEGORIES: List[Dict[str, Any]] = [
    {
        "code": CATEGORY_PLAYSTYLE,
        "name": "玩法",
        "description": "剧本的体验类型与案件形态，决定这个本「怎么玩」",
        "multi_select": True,
        "sort_order": 10,
    },
    {
        "code": CATEGORY_THEME,
        "name": "题材",
        "description": "故事的时代背景与世界观设定",
        "multi_select": True,
        "sort_order": 20,
    },
    {
        "code": CATEGORY_RELEASE,
        "name": "发行方式",
        "description": "剧本的授权发售形式，通常与门店定价、演绎规格正相关",
        "multi_select": True,
        "sort_order": 30,
    },
    {
        "code": CATEGORY_DIFFICULTY,
        "name": "难度",
        "description": "推理与信息处理的门槛，供玩家按经验匹配",
        "multi_select": True,
        "sort_order": 40,
    },
    {
        "code": CATEGORY_PLAYER_COUNT,
        "name": "人数",
        "description": "建议开本人数，携带数值区间可直接用于范围过滤",
        "multi_select": True,
        "sort_order": 50,
    },
    {
        "code": CATEGORY_DURATION,
        "name": "时长",
        "description": "预计游戏时长，数值区间单位为分钟",
        "multi_select": True,
        "sort_order": 60,
    },
]


OPTIONS: Dict[str, List[Dict[str, Any]]] = {
    # ---------------- 玩法 ----------------
    CATEGORY_PLAYSTYLE: [
        {
            "code": "happy",
            "label": "欢乐",
            "aliases": ["欢乐本", "爆笑本"],
            "description": "剧情与机制都围绕搞笑展开，气氛轻松，是新手入坑首选",
            "is_hot": True,
        },
        {
            "code": "emotional",
            "label": "情感",
            "aliases": ["情感本", "哭哭本"],
            "description": "以人物故事与共情沉浸为核心，部分本甚至没有凶案",
            "is_hot": True,
        },
        {
            "code": "healing",
            "label": "治愈",
            "aliases": ["治愈本", "温情本"],
            "description": "情感向的温暖分支，结局给予慰藉与和解，情绪落点柔和",
            "is_hot": True,
        },
        {
            "code": "conceptual",
            "label": "立意",
            "aliases": ["立意本", "深度本"],
            "description": "落点在人生哲理或社会议题，玩完会有较深的思考与回味",
            "is_hot": True,
        },
        {
            "code": "hardcore",
            "label": "硬核推理",
            "aliases": ["硬核本", "推理本", "烧脑本"],
            "description": "线索密集、逻辑链长，需推导作案手法与过程，解谜成就感强",
            "is_hot": True,
        },
        {
            "code": "whodunit",
            "label": "推凶",
            "aliases": ["推凶本", "找凶手"],
            "description": "真凶隐藏在玩家之中，需要狡辩抗推，对抗性强",
        },
        {
            "code": "restore",
            "label": "还原",
            "aliases": ["还原本"],
            "description": "重点不是抓凶，而是还原案件与角色之间故事的全貌，新手慎选",
        },
        {
            "code": "faction",
            "label": "阵营",
            "aliases": ["阵营本", "对抗本"],
            "description": "玩家分属多个阵营竞争，取胜条件各异，常设内奸让结局多变",
            "is_hot": True,
        },
        {
            "code": "mechanism",
            "label": "机制",
            "aliases": ["机制本"],
            "description": "内置卡牌、竞拍、大富翁等小游戏环节，靠机制博弈获取优势",
            "is_hot": True,
        },
        {
            "code": "horror",
            "label": "恐怖",
            "aliases": ["恐怖本", "惊悚本"],
            "description": "主打恐怖氛围，常含单人搜证、追逐与惊吓环节",
        },
        {
            "code": "light_horror",
            "label": "微恐",
            "aliases": ["微恐本"],
            "description": "有恐怖元素但强度可控，胆小玩家也能接受",
        },
        {
            "code": "immersive",
            "label": "沉浸演绎",
            "aliases": ["沉浸本", "演绎本", "实景本"],
            "description": "配套实景与服化道，玩家换装入戏，演绎比重远高于推理",
        },
        {
            "code": "quarrel",
            "label": "撕逼",
            "aliases": ["撕逼本", "对戏本"],
            "description": "强对抗互怼，有怨报怨有仇报仇，讲究嘴上功夫",
        },
        {
            "code": "social",
            "label": "社交",
            "aliases": ["社交本", "破冰本"],
            "description": "轻推理重互动，适合团建与陌生人破冰局",
        },
        {
            "code": "honkaku",
            "label": "本格",
            "aliases": ["本格推理"],
            "description": "不含超自然因素，作案手法均可用科学手段验证，逻辑至上",
        },
        {
            "code": "shin_honkaku",
            "label": "新本格",
            "aliases": ["新本格推理"],
            "description": "世界观含变格成分，但案件最终回归证据与动机，当下多数推理本属此类",
        },
        {
            "code": "henkaku",
            "label": "变格",
            "aliases": ["变格推理"],
            "description": "含诅咒、法术、附体等科学无法解释的超自然手法",
        },
        {
            "code": "trpg",
            "label": "跑团",
            "aliases": ["跑团本", "TRPG"],
            "description": "无固定剧本约束的开放式扮演，走向由玩家决策驱动",
        },
        {
            "code": "cp",
            "label": "CP",
            "aliases": ["CP本", "情侣本"],
            "description": "男女角色成对设定，多为对等人数配置，适合情侣或熟人局",
        },
        {
            "code": "kids",
            "label": "儿童",
            "aliases": ["儿童本", "亲子本"],
            "description": "面向青少年与亲子家庭，内容无恐怖血腥元素",
        },
        {
            "code": "empty_city",
            "label": "空城",
            "aliases": ["空城本"],
            "description": "允许缺角开本，人数不齐时由 DM 补位，凑车压力小",
        },
    ],
    # ---------------- 题材 ----------------
    CATEGORY_THEME: [
        {
            "code": "modern",
            "label": "现代",
            "aliases": ["现代都市", "当代"],
            "description": "以当代社会为背景，代入门槛最低",
            "is_hot": True,
        },
        {
            "code": "ancient",
            "label": "古风",
            "aliases": ["古代", "古装"],
            "description": "以中国古代为背景，常与情感、宫廷、江湖题材叠加",
            "is_hot": True,
        },
        {
            "code": "republic",
            "label": "民国",
            "aliases": ["民国风"],
            "description": "民国时期背景，谍战与家族恩怨的高频土壤",
            "is_hot": True,
        },
        {
            "code": "mythology",
            "label": "神话",
            "aliases": ["神话传说", "封神", "西游"],
            "description": "取材东方或世界神话体系，神佛妖魔同台",
            "is_hot": True,
        },
        {
            "code": "scifi",
            "label": "科幻",
            "aliases": ["科幻本", "未来"],
            "description": "时间穿越、人工智能、星际设定，解谜难度通常偏高",
            "is_hot": True,
        },
        {
            "code": "wuxia",
            "label": "武侠",
            "aliases": ["江湖", "武侠本"],
            "description": "刀光剑影的江湖恩怨，常与家国情、师徒情结合",
            "is_hot": True,
        },
        {
            "code": "xianxia",
            "label": "仙侠",
            "aliases": ["修仙", "修真"],
            "description": "宗门、功法、飞升体系，作案手法脑洞极大",
        },
        {
            "code": "fantasy",
            "label": "玄幻",
            "aliases": ["奇幻"],
            "description": "架空的超自然力量体系，规则由世界观自定义",
        },
        {
            "code": "suspense",
            "label": "悬疑",
            "aliases": ["悬疑本", "犯罪"],
            "description": "强调谜面与反转，是推理本最常见的底色",
            "is_hot": True,
        },
        {
            "code": "spy",
            "label": "谍战",
            "aliases": ["谍战本", "特工"],
            "description": "潜伏与身份博弈，多与民国、阵营机制搭配",
        },
        {
            "code": "campus",
            "label": "校园",
            "aliases": ["校园本", "青春"],
            "description": "校园社团、霸凌与青春回忆，多见于情感与恐怖本",
        },
        {
            "code": "palace",
            "label": "宫廷",
            "aliases": ["宫斗", "后宫"],
            "description": "深宫权谋与位分之争，撕逼与演绎浓度高",
        },
        {
            "code": "western",
            "label": "欧美",
            "aliases": ["西式", "西方"],
            "description": "欧美背景，庄园、侦探与蒸汽时代是经典场景",
        },
        {
            "code": "japanese",
            "label": "日式",
            "aliases": ["日系", "和风"],
            "description": "日式物哀美学与暗黑风格，常见于新本格推理",
        },
        {
            "code": "folklore",
            "label": "民俗",
            "aliases": ["中式民俗", "乡村", "志怪"],
            "description": "乡野秘闻与传统仪式，氛围感强，近年中式恐怖主力",
        },
        {
            "code": "history",
            "label": "历史",
            "aliases": ["历史本", "正史"],
            "description": "依托真实历史事件与人物改编",
        },
        {
            "code": "war",
            "label": "战争",
            "aliases": ["军事", "抗战"],
            "description": "战争年代的抉择与牺牲，家国情怀浓厚",
        },
        {
            "code": "apocalypse",
            "label": "末世",
            "aliases": ["废土", "丧尸"],
            "description": "文明崩坏后的生存博弈，资源与人性双重考验",
        },
        {
            "code": "cyberpunk",
            "label": "赛博朋克",
            "aliases": ["赛博", "未来都市"],
            "description": "高科技低生活的近未来，义体与数据是常见线索载体",
        },
        {
            "code": "business",
            "label": "商战",
            "aliases": ["职场", "商业"],
            "description": "商业博弈与职场权斗，机制本高频题材",
        },
        {
            "code": "showbiz",
            "label": "娱乐圈",
            "aliases": ["名利场", "演艺圈"],
            "description": "聚光灯下的秘密与塌房，话题性强",
        },
        {
            "code": "alternate",
            "label": "架空",
            "aliases": ["架空世界", "平行世界"],
            "description": "完全虚构的世界观，不依附任何真实时代",
            "is_hot": True,
        },
        {
            "code": "fairytale",
            "label": "童话",
            "aliases": ["童话风", "绘本"],
            "description": "童话外壳包裹的成人内核，反差感是主要看点",
        },
    ],
    # ---------------- 发行方式 ----------------
    CATEGORY_RELEASE: [
        {
            "code": "boxed",
            "label": "盒装",
            "aliases": ["盒装本"],
            "description": "不限量发售，各门店均可采购，玩家侧价格最亲民，好本上限并不低",
            "is_hot": True,
        },
        {
            "code": "city_limited",
            "label": "城限",
            "aliases": ["城限本", "城市限定"],
            "description": "城市限定，同城仅 3 至 8 家门店可授权，演绎环节多，收费高于盒装",
            "is_hot": True,
        },
        {
            "code": "exclusive",
            "label": "独家",
            "aliases": ["独家本"],
            "description": "同城仅 1 家门店可开，演绎规格与收费通常最高",
            "is_hot": True,
        },
        {
            "code": "limited",
            "label": "限定",
            "aliases": ["限量", "绝版"],
            "description": "限量发行或已绝版，多见于展会限定与联名款",
        },
        {
            "code": "real_scene",
            "label": "实景",
            "aliases": ["实景本", "沉浸实景"],
            "description": "配套独立实景空间与服化道，按场景规格授权",
        },
        {
            "code": "online",
            "label": "线上",
            "aliases": ["线上本", "语音本"],
            "description": "线上语音开本，不依赖线下门店",
        },
        {
            "code": "beta",
            "label": "网测",
            "aliases": ["网测本", "测试本"],
            "description": "尚未正式发行的测试版本，内容可能仍在迭代",
        },
    ],
    # ---------------- 难度 ----------------
    CATEGORY_DIFFICULTY: [
        {
            "code": "beginner",
            "label": "新手",
            "aliases": ["新手本", "入门"],
            "description": "人物关系简单、线索量小，第一次玩也不会坐牢",
            "is_hot": True,
        },
        {
            "code": "intermediate",
            "label": "进阶",
            "aliases": ["进阶本", "中级"],
            "description": "有一定推理量与信息差，适合打过几个本的玩家",
            "is_hot": True,
        },
        {
            "code": "advanced",
            "label": "高阶",
            "aliases": ["高阶本", "高级"],
            "description": "线索交叉复杂、时间线绕，需要较强的逻辑梳理能力",
        },
        {
            "code": "expert",
            "label": "骨灰",
            "aliases": ["骨灰级", "地狱难度"],
            "description": "面向资深玩家的极限难度，体量与烧脑程度拉满",
        },
    ],
    # ---------------- 人数 ----------------
    CATEGORY_PLAYER_COUNT: [
        {
            "code": "lte_4",
            "label": "4人及以下",
            "aliases": ["4人本", "小车本"],
            "description": "小车局，凑人压力最小",
            "min_value": 1,
            "max_value": 4,
            "unit": UNIT_PERSON,
        },
        {
            "code": "p5",
            "label": "5人",
            "aliases": ["5人本"],
            "description": "五人配置，常见于情感与还原本",
            "min_value": 5,
            "max_value": 5,
            "unit": UNIT_PERSON,
        },
        {
            "code": "p6",
            "label": "6人",
            "aliases": ["6人本"],
            "description": "市场主流配置，剧本供给量最大",
            "min_value": 6,
            "max_value": 6,
            "unit": UNIT_PERSON,
            "is_hot": True,
        },
        {
            "code": "p7",
            "label": "7人",
            "aliases": ["7人本"],
            "description": "七人配置，多为阵营与机制本",
            "min_value": 7,
            "max_value": 7,
            "unit": UNIT_PERSON,
        },
        {
            "code": "p8",
            "label": "8人",
            "aliases": ["8人本"],
            "description": "八人配置，CP 本的经典人数（4 男 4 女）",
            "min_value": 8,
            "max_value": 8,
            "unit": UNIT_PERSON,
            "is_hot": True,
        },
        {
            "code": "gte_9",
            "label": "9人及以上",
            "aliases": ["大车本", "多人本"],
            "description": "大车局，阵营对抗与大机制本常见",
            "min_value": 9,
            "max_value": 30,
            "unit": UNIT_PERSON,
        },
    ],
    # ---------------- 时长 ----------------
    CATEGORY_DURATION: [
        {
            "code": "lt_2h",
            "label": "2小时以下",
            "aliases": ["快本", "短本"],
            "description": "轻量快本，适合饭后或碎片时间",
            "min_value": 0,
            "max_value": 120,
            "unit": UNIT_MINUTE,
        },
        {
            "code": "h2_4",
            "label": "2-4小时",
            "aliases": ["4小时以内"],
            "description": "节奏紧凑，欢乐本与小体量情感本的主流时长",
            "min_value": 120,
            "max_value": 240,
            "unit": UNIT_MINUTE,
            "is_hot": True,
        },
        {
            "code": "h4_6",
            "label": "4-6小时",
            "aliases": ["半天本"],
            "description": "市场最主流的时长区间，体量与体验较平衡",
            "min_value": 240,
            "max_value": 360,
            "unit": UNIT_MINUTE,
            "is_hot": True,
        },
        {
            "code": "h6_8",
            "label": "6-8小时",
            "aliases": ["长本"],
            "description": "大体量本，硬核推理与大机制本常见",
            "min_value": 360,
            "max_value": 480,
            "unit": UNIT_MINUTE,
        },
        {
            "code": "gt_8h",
            "label": "8小时以上",
            "aliases": ["上班本", "超长本"],
            "description": "俗称「上班本」，体量巨大，建议留出整天时间",
            "min_value": 480,
            "max_value": 1440,
            "unit": UNIT_MINUTE,
        },
    ],
}


def iter_option_rows() -> List[Dict[str, Any]]:
    """把 OPTIONS 拍平成可直接入库的行，自动按声明顺序生成 sort_order。"""
    rows: List[Dict[str, Any]] = []
    for category_code, items in OPTIONS.items():
        for index, item in enumerate(items, start=1):
            rows.append(
                {
                    "category_code": category_code,
                    "code": item["code"],
                    "label": item["label"],
                    "aliases": item.get("aliases", []),
                    "description": item.get("description"),
                    "min_value": item.get("min_value"),
                    "max_value": item.get("max_value"),
                    "unit": item.get("unit"),
                    # 留出间隔，方便后续人工插入新选项而不必整体重排
                    "sort_order": index * 10,
                    "is_hot": bool(item.get("is_hot", False)),
                    "is_active": True,
                    "extra": item.get("extra", {}),
                }
            )
    return rows


def iter_category_rows() -> List[Dict[str, Any]]:
    """维度行，字段与表结构一一对应。"""
    return [
        {
            "code": c["code"],
            "name": c["name"],
            "description": c.get("description"),
            "multi_select": bool(c.get("multi_select", True)),
            "sort_order": c["sort_order"],
            "is_active": True,
        }
        for c in CATEGORIES
    ]


CATEGORY_CODES = [c["code"] for c in CATEGORIES]
