# Eventide Web 工作台 · 设计系统 MASTER

状态：Accepted（2026-09-11，用户三选一锁定）。来源：ui-ux-pro-max skill 数据库 + 用户决策。
本文件是视觉令牌与风格的唯一事实；改样式先改这里。

## 风格：Bento Box Grid（Apple 式模块卡片）

- 关键词：模块卡片、圆角 16-24px、柔和阴影、清晰层级、留白
- 顶栏胶囊按钮 + 悬浮详情卡（任务计划/工具活动/详情/用量按需浮层，同一时间只开一张）；Composer 为大卡片；回复卡为轻卡片
- 不做：仪表盘磁贴化正文、渐变、玻璃拟态、发光

## 字体

- 拉丁/数字：Inter（Google Fonts，`display=swap`；离线时回退系统栈，不阻塞）
- 中文：始终回退 `"Microsoft YaHei UI"/"Microsoft YaHei"` 等系统字体
- 代码/路径/时间戳：系统等宽栈（ui-monospace/Consolas）
- 数字密集处（用量、耗时、计数）保持 `font-variant-numeric: tabular-nums`

## 配色（沿用墨绿体系，仅新增令牌）

| 令牌 | 浅色 | 深色 |
|---|---|---|
| --paper（卡片面） | #ffffff | #14181d |
| --canvas（容器面） | #f7f8fa | #0f1216 |
| --ink / --muted | #24292f / #5a6370 | #e6e9ec / #9aa4b0 |
| --accent | #426958 | #7fb8a0 |
| --primary（主按钮） | #345947 | #3f6d59 |
| --warning / --danger | #856334 / #9c4945 | #d4a96a / #e08a84 |
| --shadow-soft（卡片阴影） | rgba(24,37,26,.06) | rgba(0,0,0,.4) |
| --shadow-overlay（浮层阴影） | rgba(24,37,26,.18) | rgba(0,0,0,.55) |

对比度纪律：正文 4.5:1（浅色 --muted 已加深达标）；语义色在两主题下均需通过。

## 形状与动效

- 圆角阶梯：卡片 16px · 结果块/输入区 12px · 按钮 8px · chip/badge 999px
- 阴影阶梯：卡片 `0 1px 2px var(--shadow-soft)`；悬浮详情卡/模式菜单等浮层 `0 12px 40px var(--shadow-overlay)`；仅交互卡片 hover 时轻微抬升，禁止大面积投影
- 动效：150-250ms ease；reduced-motion 全量关闭
- 间距：4px 基数；卡片内边距 14-16px；卡片间距 10-12px

## 组件映射

| 区域 | 处理 |
|---|---|
| #floating-panel 悬浮卡 | paper 面 + 圆角 16 + 遮罩级阴影（`0 12px 40px var(--shadow-overlay)`），自带滚动，窄屏近全屏 |
| 顶栏胶囊按钮 | pill 造型（圆角 999），12px，hover 与 aria-expanded 态有区分 |
| 模式菜单 | 浮层圆角 10，选项行含 12px muted 说明，Agent 选项带警示标注 |
| 圆形发送键 | 36-40px 圆形，primary 底、↑ 图标；运行中原位变 ■ 停止，停止态 danger 描边 |
| Composer | 大卡片（圆角 12），操作区底改 canvas |
| 回复卡 .chapter-result | 轻卡片（圆角 12） |
| 用户气泡 | 保持（已是气泡语义），圆角 10→12 对齐 |
| 按钮 | primary/secondary 圆角 8 |
| 弹窗 | 圆角 14 |

## 明确不做

- 正文叙事区不做卡片拼盘（卡片只属于悬浮详情卡与操作区，正文保持文档流）
- 不引入图标库/字体库依赖（Inter 为唯一外部资源，swap + 回退）
- 不为"高级感"加渐变、发光、玻璃拟态、装饰性动画
