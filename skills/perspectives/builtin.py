"""内置人物视角加载器 — 将所有官方人物注册到系统中"""

from skills.registry import Perspective, PerspectiveRegistry


def _load_builtins(registry: PerspectiveRegistry) -> None:
    """注册所有内置人物视角"""

    # ========== AI 与科技创业者 ==========
    registry.register(Perspective(
        id="altman", name="山姆·奥特曼", name_en="Sam Altman",
        description="以OpenAI CEO的视角，用AI时代创业、指数思维、技术乐观主义和规模化的思维模式回答问题",
        tags=["AI", "创业", "技术", "未来", "科技"],
        trigger_words=["奥特曼", "Altman", "OpenAI", "AI创业", "技术乐观"],
    ))
    registry.register(Perspective(
        id="steve-jobs", name="乔布斯", name_en="Steve Jobs",
        description="用第一性原理、极致产品直觉和现实扭曲力场分析产品与商业问题",
        tags=["产品", "设计", "科技", "创业", "品味"],
        trigger_words=["乔布斯", "Jobs", "苹果", "iPhone", "产品设计"],
    ))
    registry.register(Perspective(
        id="elon-musk", name="马斯克", name_en="Elon Musk",
        description="以第一性原理、五步工作法和物理学家式思维解构复杂问题",
        tags=["科技", "创业", "工程", "物理", "第一性原理"],
        trigger_words=["马斯克", "Musk", "特斯拉", "SpaceX", "第一性原理"],
    ))
    registry.register(Perspective(
        id="leijun", name="雷军", name_en="Lei Jun",
        description="以雷军的极致性价比、风口论和互联网思维来分析问题",
        tags=["创业", "互联网", "性价比", "风口"],
        trigger_words=["雷军", "小米", "风口", "互联网思维"],
    ))
    registry.register(Perspective(
        id="thiel", name="彼得·蒂尔", name_en="Peter Thiel",
        description="用从0到1的垄断思维和逆向思考分析商业与创业问题",
        tags=["创业", "商业", "垄断", "逆向思维"],
        trigger_words=["蒂尔", "Thiel", "从0到1", "垄断", "逆向"],
    ))

    # ========== 投资与商业思想家 ==========
    registry.register(Perspective(
        id="munger", name="查理·芒格", name_en="Charlie Munger",
        description="以多元思维模型、反向思考和人类误判心理学分析决策与投资问题",
        tags=["投资", "思维模型", "心理学", "决策", "商业"],
        trigger_words=["芒格", "Munger", "多元思维", "反向思考", "误判心理学"],
    ))
    registry.register(Perspective(
        id="bogle", name="约翰·博格", name_en="John Bogle",
        description="以指数基金哲学、低成本投资原则与长期持有信念视角分析投资问题",
        tags=["投资", "指数基金", "长期主义", "被动投资"],
        trigger_words=["博格", "Bogle", "指数基金", "被动投资", "先锋"],
    ))
    registry.register(Perspective(
        id="taleb", name="纳西姆·塔勒布", name_en="Nassim Taleb",
        description="用反脆弱、黑天鹅和杠铃策略审视风险与不确定性",
        tags=["风险", "概率", "反脆弱", "哲学"],
        trigger_words=["塔勒布", "Taleb", "黑天鹅", "反脆弱", "杠铃策略"],
    ))
    registry.register(Perspective(
        id="naval", name="Naval Ravikant", name_en="Naval Ravikant",
        description="用财富杠杆、复利思维和幸福哲学解构人生与商业",
        tags=["财富", "哲学", "创业", "幸福", "杠杆"],
        trigger_words=["Naval", "纳瓦尔", "财富", "杠杆", "复利"],
    ))
    registry.register(Perspective(
        id="bogle", name="约翰·博格", name_en="John Bogle",
        description="以指数基金哲学、低成本投资原则与长期持有信念视角分析投资问题",
        tags=["投资", "指数基金", "长期主义", "被动投资"],
        trigger_words=["博格", "Bogle", "指数基金", "被动投资", "先锋"],
    ))

    # ========== 物理学家与科学家 ==========
    registry.register(Perspective(
        id="feynman", name="理查德·费曼", name_en="Richard Feynman",
        description="用费曼学习法、第一性原理和孩童般的探索精神解释复杂问题",
        tags=["物理", "科学", "教学", "好奇心", "简化"],
        trigger_words=["费曼", "Feynman", "学习法", "物理", "科学思维"],
    ))
    registry.register(Perspective(
        id="einstein", name="爱因斯坦", name_en="Albert Einstein",
        description="以思维实验方法、相对论思维和想象力驱动的方式回答问题",
        tags=["物理", "科学", "想象力", "思维实验"],
        trigger_words=["爱因斯坦", "Einstein", "相对论", "思维实验"],
    ))
    registry.register(Perspective(
        id="turing", name="艾伦·图灵", name_en="Alan Turing",
        description="以计算思维、跨界思考和简洁优雅的问题分解方式回答问题",
        tags=["计算机", "数学", "逻辑", "AI"],
        trigger_words=["图灵", "Turing", "计算", "AI", "计算机"],
    ))
    registry.register(Perspective(
        id="shannon", name="克劳德·香农", name_en="Claude Shannon",
        description="用信息论思维、化繁为简的天赋与创造性玩耍精神分析和解决问题",
        tags=["信息论", "数学", "工程", "创新"],
        trigger_words=["香农", "Shannon", "信息论", "熵", "通信"],
    ))
    registry.register(Perspective(
        id="vonneumann", name="冯·诺依曼", name_en="John von Neumann",
        description="用博弈论思维、计算架构直觉与跨学科超速思考能力分析问题",
        tags=["数学", "计算机", "博弈论", "跨学科"],
        trigger_words=["诺依曼", "von Neumann", "博弈论", "计算机架构"],
    ))
    registry.register(Perspective(
        id="sagan", name="卡尔·萨根", name_en="Carl Sagan",
        description="以科学传播的热情、宇宙视角的谦卑、怀疑精神与淡蓝小点的诗意回答问题",
        tags=["天文", "科学", "宇宙", "科普"],
        trigger_words=["萨根", "Sagan", "宇宙", "淡蓝小点", "科学精神"],
    ))

    # ========== 思想家与哲学家 ==========
    registry.register(Perspective(
        id="franklin", name="本杰明·富兰克林", name_en="Benjamin Franklin",
        description="以十三种美德、实用主义和跨领域智慧的视角处世与解决问题",
        tags=["哲学", "实用主义", "自律", "自我完善"],
        trigger_words=["富兰克林", "Franklin", "十三美德", "穷理查"],
    ))
    registry.register(Perspective(
        id="churchill", name="温斯顿·丘吉尔", name_en="Winston Churchill",
        description="融合领导力、雄辩修辞与逆境哲学回应挑战性问题",
        tags=["领导力", "政治", "历史", "演讲"],
        trigger_words=["丘吉尔", "Churchill", "领导力", "逆境"],
    ))
    registry.register(Perspective(
        id="ferriss", name="蒂姆·费里斯", name_en="Tim Ferriss",
        description="以生活黑客与系统化最优解视角，用DEAL框架和80/20法则优化一切",
        tags=["效率", "生活黑客", "系统化", "优化"],
        trigger_words=["费里斯", "Ferriss", "黑客", "DEAL", "80/20"],
    ))
    registry.register(Perspective(
        id="ohmae", name="大前研一", name_en="Kenichi Ohmae",
        description="以战略思维、无国界经济、问题解决法探讨商业与低欲望社会",
        tags=["战略", "商业", "全球化", "日本", "咨询"],
        trigger_words=["大前研一", "Ohmae", "战略思维", "无国界"],
    ))
    registry.register(Perspective(
        id="fengtang", name="冯唐", name_en="Feng Tang",
        description="以成事心法、金线原理和跨界智慧分析管理与人生问题",
        tags=["管理", "写作", "成事", "人生智慧"],
        trigger_words=["冯唐", "成事", "金线", "麦肯锡"],
    ))

    # ========== 创作者与艺术家 ==========
    registry.register(Perspective(
        id="miyazaki", name="宫崎骏", name_en="Hayao Miyazaki",
        description="温柔而固执、关爱自然和儿童、手工匠人精神的视角回答问题",
        tags=["动画", "艺术", "自然", "创意", "匠人"],
        trigger_words=["宫崎骏", "Miyazaki", "吉卜力", "动画", "匠人"],
    ))
    registry.register(Perspective(
        id="kurosawa", name="黑泽明", name_en="Akira Kurosawa",
        description="用电影哲学与创作方法审视问题——完美主义、人性深度和视觉叙事的力量",
        tags=["电影", "艺术", "叙事", "人性"],
        trigger_words=["黑泽明", "Kurosawa", "电影", "导演"],
    ))
    registry.register(Perspective(
        id="wangxiaobo", name="王小波", name_en="Wang Xiaobo",
        description="以自由精神与黑色幽默进行写作和表达",
        tags=["文学", "幽默", "自由", "批判"],
        trigger_words=["王小波", "自由", "黑色幽默", "沉默的大多数"],
    ))
    registry.register(Perspective(
        id="linyutang", name="林语堂", name_en="Lin Yutang",
        description="以生活艺术哲学与中西贯通的视野进行写作和思考",
        tags=["文学", "哲学", "生活", "中西"],
        trigger_words=["林语堂", "生活艺术", "吾国吾民"],
    ))

    # ========== 中国历史人物 ==========
    registry.register(Perspective(
        id="zhugeliang", name="诸葛亮", name_en="Zhuge Liang",
        description="以隆中对策、空城计智谋和鞠躬尽瘁的视角分析战略与人生",
        tags=["历史", "谋略", "智慧", "三国"],
        trigger_words=["诸葛亮", "孔明", "三国", "卧龙", "隆中对"],
    ))
    registry.register(Perspective(
        id="zengguofan", name="曾国藩", name_en="Zeng Guofan",
        description="以拙诚、坚忍、克己修身与经世致用之道分析自我管理与领导力",
        tags=["历史", "修身", "领导力", "管理"],
        trigger_words=["曾国藩", "家书", "拙诚", "坚忍", "修身"],
    ))
    registry.register(Perspective(
        id="zhangjuzheng", name="张居正", name_en="Zhang Juzheng",
        description="融合改革智慧、超强执行力、制度设计与一条鞭法的治理思维",
        tags=["历史", "改革", "治理", "制度"],
        trigger_words=["张居正", "改革", "一条鞭法", "万历"],
    ))
    registry.register(Perspective(
        id="qianxuesen", name="钱学森", name_en="Qian Xuesen",
        description="以系统工程思维、跨学科融合、航天精神与工程哲学的统一视角看问题",
        tags=["科学", "工程", "航天", "系统", "爱国"],
        trigger_words=["钱学森", "系统工程", "航天", "两弹一星"],
    ))
    registry.register(Perspective(
        id="taoxingzhi", name="陶行知", name_en="Tao Xingzhi",
        description="以生活教育、教学做合一、扎根大地的视角回应问题",
        tags=["教育", "实践", "平民", "知行合一"],
        trigger_words=["陶行知", "行知", "生活教育", "教学做合一"],
    ))
    registry.register(Perspective(
        id="sudongpo", name="苏东坡", name_en="Su Dongpo",
        description="以豁达人生、诗词书画、美食与逆境中自得其乐的视角品味生活",
        tags=["文学", "诗词", "人生", "豁达", "美食"],
        trigger_words=["苏东坡", "苏轼", "东坡", "豁达", "诗词"],
    ))
    registry.register(Perspective(
        id="zhangxuefeng", name="张雪峰", name_en="Zhang Xuefeng",
        description="以实用主义教育观、职业规划和接地气的表达分析教育与人生选择",
        tags=["教育", "职业", "规划", "实用"],
        trigger_words=["张雪峰", "考研", "高考", "志愿", "职业规划"],
    ))
    registry.register(Perspective(
        id="guiguzi", name="鬼谷子", name_en="Gui Guzi",
        description="以纵横捭阖、揣情摩意和捭阖之术分析人际博弈与战略策略",
        tags=["谋略", "纵横", "兵法", "沟通"],
        trigger_words=["鬼谷子", "捭阖", "纵横", "谋略", "兵法"],
    ))

    # ========== 心灵与修行 ==========
    registry.register(Perspective(
        id="thichnhathanh", name="一行禅师", name_en="Thich Nhat Hanh",
        description="以温柔简单、正念修行和诗意语言照亮当下的视角回应人生",
        tags=["禅修", "正念", "安宁", "心灵"],
        trigger_words=["一行禅师", "Thich Nhat Hanh", "正念", "活在当下"],
    ))

    registry.mark_ready()