// M4: ECharts 按需注册（只引入用到的图表/组件，减小体积）。
import * as echarts from 'echarts/core'
import { BarChart, LineChart, PieChart } from 'echarts/charts'
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
  TitleComponent,
  DataZoomComponent,
  GraphicComponent, // 空数据的「暂无数据」提示靠它画（不注册的话 graphic 会被静默丢掉）
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([
  BarChart,
  LineChart,
  PieChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  TitleComponent,
  DataZoomComponent,
  GraphicComponent,
  CanvasRenderer,
])

export { echarts }
export type { EChartsCoreOption } from 'echarts/core'