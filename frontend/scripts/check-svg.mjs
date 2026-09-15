// 静态资源自检：把 src/assets 下的 SVG 逐个按 XML 解析一遍。
//
// 为什么需要：SVG 里只要出现一处 XML 级错误（最典型的是注释里写了 CSS 变量名，
// 也就是连续两个连字符 —— XML 注释不允许这个序列），浏览器就会整张图静默不画：
// 页面不报错、控制台没有明显线索，只表现为「改完看起来什么都没变」。
// 这个坑踩过一次，所以放进 build 前置步骤，坏了就构建失败，别再靠肉眼发现。
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

const dir = join(process.cwd(), 'src', 'assets')
let files = []
try {
  files = readdirSync(dir).filter((f) => f.toLowerCase().endsWith('.svg'))
} catch {
  console.log('[check-svg] 没有 src/assets 目录，跳过')
  process.exit(0)
}

const problems = []
for (const f of files) {
  const text = readFileSync(join(dir, f), 'utf8')
  // ① 注释里出现连续两个连字符 —— XML 规范禁止，浏览器直接放弃整张图
  for (const m of text.matchAll(/<!--([\s\S]*?)-->/g)) {
    if (m[1].includes('--')) problems.push(`${f}: 注释里出现了连续两个连字符（XML 注释不允许）`)
  }
  // ② 标签配平与属性引号这类硬伤，用一个极简栈式检查兜住
  const stripped = text.replace(/<!--[\s\S]*?-->/g, '').replace(/<\?[\s\S]*?\?>/g, '')
  const stack = []
  for (const m of stripped.matchAll(/<(\/?)([A-Za-z][\w:.-]*)((?:"[^"]*"|'[^']*'|[^>"'])*?)(\/?)>/g)) {
    const [, close, name, , selfClose] = m
    if (close) {
      const top = stack.pop()
      if (top !== name) problems.push(`${f}: 标签不匹配（</${name}> 对应的是 <${top || '空'}>）`)
    } else if (!selfClose) {
      stack.push(name)
    }
  }
  if (stack.length) problems.push(`${f}: 有未闭合的标签 ${stack.join(', ')}`)
  if (!/<svg[\s>]/.test(text)) problems.push(`${f}: 找不到 <svg> 根标签`)
}

if (problems.length) {
  console.error('[check-svg] 检查未通过：')
  for (const p of problems) console.error('  - ' + p)
  process.exit(1)
}
console.log(`[check-svg] ${files.length} 个 SVG 全部通过（${files.join(', ')}）`)
