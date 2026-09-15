// M2/M5: Markdown 渲染 + 代码高亮 + XSS 消毒。
// 模型输出是不可信内容，必须经 DOMPurify 消毒后再 v-html 注入。
// M5: highlight.js 由「全量 import」改为 core + 按需注册语言，主包体积显著下降。

import { Marked } from 'marked'
import { markedHighlight } from 'marked-highlight'
import hljs from 'highlight.js/lib/core'
import DOMPurify from 'dompurify'
import python from 'highlight.js/lib/languages/python'
import javascript from 'highlight.js/lib/languages/javascript'
import typescript from 'highlight.js/lib/languages/typescript'
import bash from 'highlight.js/lib/languages/bash'
import json from 'highlight.js/lib/languages/json'
import xml from 'highlight.js/lib/languages/xml'
import css from 'highlight.js/lib/languages/css'
import markdown from 'highlight.js/lib/languages/markdown'
import sql from 'highlight.js/lib/languages/sql'
import yaml from 'highlight.js/lib/languages/yaml'
import rust from 'highlight.js/lib/languages/rust'
import go from 'highlight.js/lib/languages/go'
import java from 'highlight.js/lib/languages/java'
import cpp from 'highlight.js/lib/languages/cpp'
import c from 'highlight.js/lib/languages/c'

const langs: Record<string, any> = {
  python, javascript, typescript, bash, json, xml, css, markdown, sql, yaml, rust, go, java, cpp, c,
}
for (const [name, reg] of Object.entries(langs)) hljs.registerLanguage(name, reg)

const md = new Marked(
  markedHighlight({
    langPrefix: 'hljs language-',
    highlight(code: string, lang: string) {
      const l = lang && hljs.getLanguage(lang) ? lang : 'plaintext'
      return hljs.highlight(code, { language: l }).value
    },
  }),
  { gfm: true, breaks: true },
)

/** 把 markdown 渲染成已消毒的 HTML（安全地用于 v-html）。 */
export function renderMarkdown(src: string): string {
  return DOMPurify.sanitize(md.parse(src || '') as string)
}