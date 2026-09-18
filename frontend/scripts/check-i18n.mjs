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
// 第三十轮追加第四件：**源码里的硬编码中文**。
//   体检的由头是 Videos.vue 的素材摘要（`首帧:xx.png`）和 lib/upload.ts 的一串报错，
//   全都是写死的中文 —— 切到英文后这些地方照旧中文，肉眼很难在一个大页面里发现。
//   规则：src 下（locales 除外）**字符串字面量**里出现中日韩统一表意文字就报错；
//   注释里的中文不算（本项目注释大量中文，且注释本来就不该翻）、模板里的纯文字节点不算
//   （那是语言切换器自己要显示的「中文」，按惯例各语言写给自己的名字）。
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

// ── 硬编码中文扫描（第三十轮）──────────────────────────────
const CJK = /[\u4e00-\u9fa5]/

/** 允许出现的硬编码中文（逐条写上理由，别用"通配"把整类放过） */
const CJK_ALLOW = new Map([
  // 登录页右上角的语言切换：写的是**中文这门语言自己的名字**，切到英文界面也该显示「中文」
  ['views/Login.vue::中', '语言切换按钮上的语言自称（各语言写自己的名字）'],
])

function walk(dir) {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const p = path.join(dir, e.name)
    return e.isDirectory() ? walk(p) : [p]
  })
}

/** 去掉注释（块注释 / HTML 注释 / 行注释 —— 行注释要避开 `https://` 里的两个斜杠） */
function stripComments(code) {
  return code
    .replace(/\/\*[\s\S]*?\*\//g, ' ')
    .replace(/<!--[\s\S]*?-->/g, ' ')
    .split('\n')
    .map((l) => l.replace(/(^|[^:])\/\/.*$/, '$1'))
    .join('\n')
}

function scanHardcodedChinese() {
  const found = []
  const files = walk(path.join(ROOT, 'src')).filter(
    (f) => /\.(vue|ts|mts|js|mjs)$/.test(f) && !f.includes(`${path.sep}locales${path.sep}`),
  )
  for (const abs of files) {
    const rel = path.relative(path.join(ROOT, 'src'), abs).split(path.sep).join('/')
    const lines = stripComments(fs.readFileSync(abs, 'utf8')).split('\n')
    lines.forEach((line, i) => {
      const lits = line.match(/'[^'\n]*'|"[^"\n]*"|`[^`\n]*`/g) || []
      for (const lit of lits) {
        const text = lit.slice(1, -1)
        if (!CJK.test(text)) continue
        if (CJK_ALLOW.has(`${rel}::${text}`)) continue
        found.push(`src/${rel}:${i + 1} 硬编码中文：${lit}（走 i18n：中文进 zh.ts、英文进 en.ts）`)
      }
    })
  }
  return found
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

// 硬编码中文
problems.push(...scanHardcodedChinese())

if (problems.length) {
  console.error(`\n✗ 文案体检不通过（${problems.length} 个问题）：`)
  for (const p of problems) console.error('  - ' + p)
  console.error('\n改完再 build。\n')
  process.exit(1)
}

console.log(`✓ 文案体检通过：中 ${zhKeys.size} 条 / 英 ${enKeys.size} 条，键对称、无重复、占位符语法正常`)
