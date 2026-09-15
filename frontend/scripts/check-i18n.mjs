// 文案体检（build 前置校验，第二十三轮补）
//
// 为什么需要它：vue-i18n 把文案里的 {...} 当占位符解析。踩过的坑 —— 一条提示文案写了
// `{"status":"ok"}`，编译器把它当占位符、报 "Invalid token in placeholder"，而这条错误
// 是"渲染时才编译"时抛的：用到这条文案的整块界面直接不渲染，控制台里也只是一条 Vue 错误，
// 表现看起来像"这个页签/卡片挂了"（当时误判成 Element Plus 的 pane 机制问题，查了两轮）。
// 现在 build 前先把两份文案逐条过一遍，类似的写法在构建阶段就被拦下。
//
// 另外顺手核三件小事（都是会出现"界面里冒出 nav.xxx 原始键名"的原因）：
//   1. 中英两份键完全对称；2. 同一个对象里没有重复键；3. 没有空文案。
//
// 只依赖 Node 内置模块：不引第三方解析器，规则显式写在下面，谁都能看懂。

import fs from 'node:fs'
import path from 'node:path'

const ROOT = path.resolve(import.meta.dirname, '..')
const FILES = { zh: 'src/locales/zh.ts', en: 'src/locales/en.ts' }

// 占位符第一个字符的合法集合：{name} {0} {n} {@.upper:name} {'字面量'} {-1}
const OK_FIRST = /[A-Za-z0-9_$@'\u4e00-\u9fa5-]/

/** 逐行解析 locale 文件（2 空格缩进 + `key: '...'`）→ { `a.b.c`: [值, 行号] } */
function parseLocale(file) {
  const out = new Map()
  const dup = []
  const stack = [] // [{ indent, key }]
  const abs = path.join(ROOT, file)
  const lines = fs.readFileSync(abs, 'utf8').split('\n')

  lines.forEach((raw, i) => {
    const line = raw.replace(/\s+$/, '')
    const m = line.match(/^(\s*)([A-Za-z0-9_$]+):\s*(.*)$/)
    if (!m) return
    const indent = m[1].length
    const key = m[2]
    const rest = m[3]
    while (stack.length && stack[stack.length - 1].indent >= indent) stack.pop()
    if (rest === '{') {              // 开一个子对象
      stack.push({ indent, key })
      return
    }
    // 字符串值（单/双引号都认；允许行尾逗号）。注意要认得"这条文案里带 {'{'} 字面量花括号时
    // 只能用双引号写"的情况，否则这条文案会被跳过、体检等于没查。
    const sm = rest.match(/^(['"])(.*)\1(,?)$/)
    if (!sm) return                          // 其它形态（注释、非字符串）不检查
    const full = [...stack.map((s) => s.key), key].join('.')
    if (out.has(full)) dup.push(`${full}（第 ${out.get(full)[1]} 行 与 第 ${i + 1} 行）`)
    else out.set(full, [unescape(sm[2]), i + 1])
  })
  return { map: out, dup }
}

function unescape(s) {
  return s.replace(/\\(.)/g, (_, c) => (c === 'n' ? '\n' : c))
}

/** 扫一条文案里的花括号，返回问题列表 */
function checkBraces(value) {
  const bad = []
  for (let i = 0; i < value.length; i++) {
    const ch = value[i]
    if (ch === '}') { bad.push('有一个多余的 }'); continue }
    if (ch !== '{') continue
    const next = value[i + 1]
    if (next === undefined) { bad.push('{ 没有收尾的 }'); continue }
    if (!OK_FIRST.test(next)) {
      bad.push(`{ 后面跟着 ${JSON.stringify(value.slice(i, i + 20))} —— vue-i18n 会把 {...} 当占位符，` +
        `花括号要写成 {'{'} / {'}'} 才能当普通字符用`)
      continue
    }
    // 找到配对的 }，期间不允许再出现 {（vue-i18n 不支持嵌套占位符）
    let j = i + 1
    let quoted = false
    for (; j < value.length; j++) {
      const c = value[j]
      if (c === "'") quoted = !quoted
      else if (!quoted && c === '{') { bad.push(`占位符里又出现了 {（不支持嵌套）：${JSON.stringify(value.slice(i, j + 10))}`); break }
      else if (!quoted && c === '}') break
    }
    if (j >= value.length) { bad.push(`{ 没有收尾的 }：${JSON.stringify(value.slice(i, i + 20))}`); continue }
    if (j === i + 1) bad.push('{} 是空占位符')
    i = j
  }
  return bad
}

const problems = []
const parsed = {}

for (const [lang, file] of Object.entries(FILES)) {
  const { map, dup } = parseLocale(file)
  parsed[lang] = map
  for (const d of dup) problems.push(`${file} 重复键：${d}`)
  for (const [k, [v, line]] of map) {
    if (!v.trim()) problems.push(`${file}:${line} ${k} 是空文案`)
    for (const b of checkBraces(v)) problems.push(`${file}:${line} ${k} —— ${b}`)
  }
}

// 中英对称
const zhKeys = new Set(parsed.zh.keys())
const enKeys = new Set(parsed.en.keys())
for (const k of zhKeys) if (!enKeys.has(k)) problems.push(`en 少了键：${k}（中文写好了英文没写 → 英文界面会显示这个原始键名）`)
for (const k of enKeys) if (!zhKeys.has(k)) problems.push(`zh 少了键：${k}（英文写好了中文没写 → 中文界面会显示这个原始键名）`)

if (problems.length) {
  console.error(`\n✗ 文案体检不通过（${problems.length} 个问题）：`)
  for (const p of problems) console.error('  - ' + p)
  console.error('\n改完再 build。\n')
  process.exit(1)
}

console.log(`✓ 文案体检通过：中 ${zhKeys.size} 条 / 英 ${enKeys.size} 条，键对称、无重复、占位符语法正常`)
