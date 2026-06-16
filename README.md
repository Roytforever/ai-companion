# AI-Companion 🐾

**智能伴侣** — 一个具备角色扮演、AI蒸馏和持久化记忆的智能 Agent 练手项目。

## ✨ 特性

- 🎭 **角色扮演模式** — 内置40+人物视角（乔布斯、费曼、芒格、诸葛亮等），切换后AI以该人物语气思考作答
- 🔮 **女娲造人** — 输入人名/主题，自动蒸馏出可运行的人物思维框架（心智模型+决策启发式+表达DNA）
- 🧠 **持久化记忆** — 记录对话历史和偏好，跨会话保持上下文
- 🔧 **工具调用** — Agent 可调用内置工具（查询时间、计算器等）
- 📚 **知识库** — 存储蒸馏素材和对话精华，支持持续优化
- 🌐 **Web 界面** — 基于 Streamlit 的清爽聊天 UI
- 💾 **本地存储** — 所有数据存在本地，隐私安全

## 快速开始

```bash
# 1. 克隆仓库
git clone https://github.com/Roytforever/ai-companion.git
cd ai-companion

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置环境变量
cp .env.example .env
# 编辑 .env，填入你的 API Key

# 4. 启动
streamlit run ui/app.py
```

## 项目结构

```
ai-companion/
├── core/               # Agent 核心逻辑
│   ├── agent.py        # Agent 主类（含角色切换）
│   └── llm.py          # 大模型调用封装
├── skills/             # 🆕 技能系统
│   ├── nuwa.py         # 女娲蒸馏引擎
│   ├── actor.py        # 角色扮演管理器
│   ├── registry.py     # 人物视角注册中心
│   ├── loader.py       # 加载器
│   └── perspectives/   # 内置人物目录
│       └── builtin.py  # 40+ 内置人物定义
├── knowledge_base/     # 🆕 知识库
│   ├── __init__.py     # 分布式知识库存储
│   └── README.md       # 方案说明
├── memory/             # 记忆系统
│   ├── store.py
│   └── manager.py
├── tools/              # 工具函数
│   ├── registry.py
│   └── builtin.py
├── config/
├── ui/
│   └── app.py          # Streamlit 应用（含新界面）
├── tests/
├── requirements.txt
├── .env.example
└── README.md
```

## 功能详情

### 🎭 角色扮演

内置40+知名人物的思维框架，涵盖：

| 领域 | 人物 |
|------|------|
| 🚀 AI与科技 | 奥特曼、乔布斯、马斯克、雷军、蒂尔 |
| 💰 投资与商业 | 芒格、博格、塔勒布、Naval |
| 🔬 科学家 | 费曼、爱因斯坦、图灵、香农、冯诺依曼、萨根、钱学森 |
| 🧠 思想家 | 富兰克林、丘吉尔、冯唐、费里斯、大前研一 |
| 🎨 艺术家 | 宫崎骏、黑泽明、王小波、林语堂 |
| 🏯 历史人物 | 诸葛亮、曾国藩、张居正、苏东坡、鬼谷子、陶行知、张雪峰 |
| 🧘 心灵修行 | 一行禅师 |

### 🔮 女娲造人

支持两种入口：
1. **明确人名** → 直接蒸馏（输入"巴菲特"，生成巴菲特的思维框架）
2. **模糊需求** → 诊断推荐（输入"我想提升决策质量"，推荐芒格/塔勒布等）

### 📚 知识库

所有蒸馏素材和对话精华持久化存储，支持后续迭代优化。

## License

MIT