# 前端重设计 v2 —— 页面改造规范

本文件是改造**页面**时必须遵守的约定。基础设施（主题、布局、图标、样式）已经完成，
不需要你动。

## 0. 铁律

1. **不动业务逻辑。** API 调用、轮询定时器、SSE 流式解析、上传与压缩、localStorage 键名、
   错误分类、二次确认、权限判断 —— 全部保持与原文件**语义等价**。你只改模板结构与样式。
2. **不写硬编码颜色。** 不许出现 `#fff` / `#909399` / `#3b5998` / `rgba(0,0,0,.06)` 这类
   具体色值（图表除外，图表用 `chartTheme()`）。一律用下面的 CSS 变量。
3. **不用 emoji 当图标。** 用 `<Icon name="..." />`。
4. **不改 `src/locales/*.ts`。** 文案优先复用已有 key；确实缺 key 时，把
   「key 路径 + 中文 + English」写进你的最终报告，由主流程统一补。
5. **完成后必须跑 `npm run typecheck`，要求 0 error。**

## 1. 主题 token（src/style.css 已定义，直接用）

| 变量 | 用途 |
| --- | --- |
| `--bg` / `--bg-2` | 页面底色（bg 是内容区，bg-2 是最外层） |
| `--surface` / `--surface-2` | 卡片底 / 次级底（输入框、只读控件） |
| `--line` / `--line-2` | 分隔线（line 明显，line-2 极淡） |
| `--text` / `--muted` / `--muted-2` | 主文字 / 次要 / 更淡 |
| `--primary` / `--primary-ink` / `--primary-soft` | 主色 / 主色上的字 / 主色淡底 |
| `--ok` `--warn` `--err` + `--ok-soft` `--warn-soft` `--err-soft` | 语义色与淡底 |
| `--chip` | 灰色小底（标签、胶囊） |
| `--radius` / `--radius-s` / `--radius-l` | 12 / 8 / 16 |
| `--gap` | 14px 间距 |
| `--chart-a` / `--chart-b` | 图表主/辅色（ECharts 用 `chartTheme()` 取） |

三套主题 A（深空蓝，深色）/ B（素白蓝紫，默认）/ C（暖砂墨绿）会自动切换这些变量，
**页面不需要为某个主题写特例**。

## 2. 页面外壳

```vue
<div class="page">…</div>        <!-- 普通滚动页：列表、表单、表格 -->
<div class="page-fill">…</div>   <!-- 撑满高度的页：Chat / Images / Videos -->
```

`.page` / `.page-fill` 自带 16/20 的内边距，窄屏自动收成 12/14。
`.page-fill` 内部需要自己安排「固定区 + 可滚区」。

**不要再使用** `.main-scroll`、`.page-card`（已删除）、`el-row` + `el-col` 栅格。

## 3. 现成类（都在 src/style.css）

- 卡片：`.card`（白底圆角边框）＋ `.card-h`（内放 `<b>标题</b>` 和 `<span class="sub">副标题</span>`
  以及右侧 `.pager`）
- KPI：`.kpis`（4 列网格）> `.kpi` > `.k` / `.v` / `.f`；`.delta.up/.down/.flat`；
  窄屏自动两列
- 栅格：`.grid2`（1.55fr 1fr）、`.grid3`（1.35fr 1fr）、`.grid-eq`（1fr 1fr），自带 `margin-top: var(--gap)`
- 竖排信息行：`.pv` > `.pv-i` > `.nm` / `.st`；进度条 `.bar` > `i`（`width` 控制）
- 状态胶囊：`.pill.ok/.warn/.err/.mute`；徽标 `.badge.g/.o/.r/.m`
- 可选中的胶囊：`.chip-sel`（选中加 `.on`）
- 表单：`.field`（`<label>` + 控件）、`.form-row`（横向一行）、`.ctl` / `.ctl.area`（只读展示态控件）
- 行式设置项：`.set-grid` > `.card` > `.set-row`（左 `.lb`+`.sub`，右 `.rt`）
- 主题卡：`.theme-card` / `.theme-prev`
- 工具类：`.card-h`、`.muted`、`.muted-2`、`.hint`、`.flex-1`、`.mb`、`.nowrap`、`.code`、`.about-line`、`.sep`
- 手机端：`.m-card`、`.seg`（分段控件）、`.m-kpis` / `.m-kpi`、`.m-grid`、`.m-thumb`

组件（el-*）已经被主题变量接管，**继续用 el-table / el-input / el-select / el-button /
el-dialog / el-tag / el-switch / el-form**，不要自己重写交互控件。表格里不要写死颜色。

## 4. 图标

```vue
import Icon from '../components/Icon.vue'
<Icon name="dl" :size="14" />
```

可用 name：`dash chat img vid model metric key users log set menu refresh dl bell plus up
theme play eye bolt send chev chevD clip x check copy trash search user link stop filter dots`

## 5. ECharts

```ts
import { chartTheme, categoryAxis, valueAxis, grid, legend, pieData, pieLabel,
         pieLabelLine, pieRadius, pieCenter } from '../lib/charts'
const { resolved } = useTheme()          // computed 里先 void resolved.value，切主题才会重算
```

## 6. 参考实现（先读这三个，照它们的写法来）

- `src/views/Dashboard.vue` —— 数据页：KPI + 卡片 + 图表 + 表格 + 底部信息条
- `src/views/Settings.vue` —— 表单/设置：`.set-grid` + `.set-row` + 行内控件
- `src/views/Login.vue` —— 独立页（不在 MainLayout 里，自带整屏布局）

## 7. 设计稿（视觉基准，按需查阅）

`design/v2/*.png`（`03-images.png` / `04-videos.png` / `06-keys-users.png` / `07-logs-settings.png`）
以及 `design/v2/screens.css`（类名与结构参考）。手机端设计在同一张画板的下半部分。
