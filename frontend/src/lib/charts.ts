// M4: 图表公共样式。集中处理两类「文字挤压」问题：
//   ① 长模型名重叠 —— 类目轴喂完整名称（tooltip 显全称），轴上截断 + 倾斜 + hideOverlap，
//      grid 用 containLabel 让 ECharts 按标签实际占位留白；
//   ② ECharts 6 把 legend 默认位置从 top 改成了 bottom（LegendModel.defaultOption.bottom），
//      于是图例压在 x 轴标签上 —— 这里统一显式 top: 0，并给 grid.top 留出图例行高。
export function clip(v: string, n = 12): string {
  return v.length > n ? v.slice(0, n) + '…' : v
}

/** 读一个主题 token 的当前值。ECharts 只吃具体色值（不认 CSS 变量），
 *  所以每次重算 option 时现取一次 —— 切主题后重新 computed 即可换色。 */
export function cssVar(name: string, fallback = ''): string {
  if (typeof document === 'undefined') return fallback
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return v || fallback
}

/** 图表用色板（随 A/B/C 主题变化）。 */
export function chartTheme() {
  return {
    a: cssVar('--chart-a', '#4F63F5'),
    b: cssVar('--chart-b', '#35B8C9'),
    ok: cssVar('--ok', '#15A34A'),
    warn: cssVar('--warn', '#D97706'),
    err: cssVar('--err', '#DC2626'),
    text: cssVar('--text', '#16203A'),
    muted: cssVar('--muted', '#6B7891'),
    muted2: cssVar('--muted-2', '#98A3B7'),
    line: cssVar('--line', '#E5EAF4'),
    primary: cssVar('--primary', '#4F63F5'),
  }
}

/** 类目轴配置：传入完整名称（tooltip 显全称），轴上按 maxLen 截断显示。 */
export function categoryAxis(labels: string[], maxLen = 12) {
  const c = chartTheme()
  return {
    type: 'category' as const,
    data: labels,
    axisTick: { alignWithLabel: true },
    axisLine: { lineStyle: { color: c.line } },
    axisLabel: {
      interval: 0,
      rotate: labels.length > 4 ? 30 : 0,
      hideOverlap: true,
      margin: 8,
      fontSize: 11,
      color: c.muted2,
      formatter: (v: string) => clip(v, maxLen),
    },
  }
}

/** 数值轴：统一网格线与轴色（深色主题下默认灰线会糊成一片）。 */
export function valueAxis(name = '', minInterval = 0) {
  const c = chartTheme()
  return {
    type: 'value' as const,
    name,
    minInterval,
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { fontSize: 11, color: c.muted2 },
    splitLine: { lineStyle: { color: c.line } },
  }
}

/** 图例：显式钉在顶部，避免 ECharts 6 默认落到底部压住 x 轴标签。 */
export function legend(data: string[]) {
  return {
    data,
    top: 0,
    left: 'center',
    itemWidth: 14,
    itemHeight: 10,
    itemGap: 14,
    textStyle: { fontSize: 12, color: chartTheme().muted },
  }
}

/** top 留出图例（约 22px）；containLabel 自动为轴标签留白，不用再手算 bottom。 */
export const grid = { left: 12, right: 18, top: 34, bottom: 10, containLabel: true }

/** 环形图：半径留足空间给外部标签，避免标签被容器裁掉。 */
export const pieRadius = ['34%', '56%']
export const pieCenter = ['50%', '52%']
/** 环形图标签：色值必须现取（ECharts 默认 #333，深色主题 A 下会糊在底色里看不见）。
 *  和 legend/valueAxis 一样做成函数，切主题后重算 option 就会跟着换色。 */
export function pieLabel() {
  return {
    overflow: 'truncate' as const,
    width: 110,
    formatter: '{b}: {c}',
    fontSize: 11,
    color: chartTheme().muted,
  }
}

/** 空数据的图表别只留一副空坐标轴：在画布中央给一行灰字提示。
 *  传进来的数组全空（或全 0）时才出现，有数据时返回空数组、不影响原图。 */
export function emptyTip(series: (number[] | undefined | null)[], text: string) {
  const allEmpty = series.every((s) => !s || !s.length || s.every((v) => !v))
  if (!allEmpty) return []
  return [
    {
      type: 'text',
      left: 'center',
      top: 'middle',
      silent: true,
      style: { text, fill: chartTheme().muted2, fontSize: 12 },
    },
  ]
}
export const pieLabelLine = { length: 8, length2: 10, smooth: true }

/** 环形图配色：主色系渐次铺开，供应商/模型数量不定也不至于撞色。 */
export function piePalette(n: number): string[] {
  const c = chartTheme()
  const base = [c.a, c.b, c.warn, c.ok, c.primary, c.muted2]
  return Array.from({ length: Math.max(n, 1) }, (_, i) => base[i % base.length])
}

/** 空数据时环形图也得画个灰圈，别留白块。
 *  空态那一项只报名字（「暂无数据」），别拼成「暂无数据: 1」这种看着像真数据的怪文案。 */
export function pieData(items: { name: string; value: number }[], emptyLabel = '暂无数据') {
  if (items.length) {
    const colors = piePalette(items.length)
    return items.map((it, i) => ({ ...it, itemStyle: { color: colors[i] } }))
  }
  const c = chartTheme()
  return [{
    name: emptyLabel,
    value: 1,
    itemStyle: { color: c.line },
    // 逐项覆盖：不显示数值、不画引导线，纯占位
    label: { show: true, formatter: emptyLabel, color: c.muted },
    labelLine: { show: false },
  }]
}
