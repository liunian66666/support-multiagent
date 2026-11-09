/* 前端 Markdown 渲染的回归检查：node scripts/test_markdown_render.js
 *
 * 起因：客服回答里的订单列表反复出现"看起来没渲染"——工具给的是 `- 单号｜商品｜状态｜日期`，
 * 但模型改写时会把 `- ` 吃掉，于是正文里没有任何 Markdown 结构，气泡就退化成一坨文字。
 * 这里钉住两件事：有没有标记都要排成逐行列表；正文里的普通竖线不能被误判成列表。
 */
'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');

function makeEl(tag) {
  return {
    tag,
    className: '',
    children: [],
    _text: '',
    appendChild(child) { this.children.push(child); return child; },
    set textContent(value) { this.children = []; this._text = String(value); },
    get textContent() { return this._text; },
  };
}

global.document = {
  createElement: makeEl,
  createTextNode: (text) => ({ tag: '#text', text: String(text) }),
};
global.window = global;

const source = fs.readFileSync(path.join(__dirname, '..', 'web', 'chat', 'markdown.js'), 'utf8');
// eslint-disable-next-line no-eval
eval(source);

function render(text) {
  const root = makeEl('div');
  global.renderMarkdown(root, text);
  return root;
}

function flatten(node, out) {
  out = out || [];
  if (node.tag === '#text') {
    if (node.text) out.push({ tag: '#text', text: node.text });
    return out;
  }
  out.push({ tag: node.tag, className: node.className, text: node._text });
  for (const child of node.children) flatten(child, out);
  return out;
}

const ORDER_ROWS = [
  'TB202410180001｜电商前缀格式测试订单｜已发货｜2024-10-18',
  'ORD-2024-003｜数据分析课程｜待发货｜2024-10-18',
  'ORD-2024-001｜VIP会员套餐｜已发货｜2024-01-15',
];

let checks = 0;
function check(name, fn) {
  fn();
  checks += 1;
  console.log('ok - ' + name);
}

check('模型丢掉 - 前缀时，订单行仍要排成列表', () => {
  const text = ['您好，为您查到以下订单：', ...ORDER_ROWS,
    '如需查看某笔订单的详情，请告诉我对应的订单号。'].join('\n');
  const nodes = flatten(render(text));
  const lis = nodes.filter((n) => n.tag === 'li');
  assert.strictEqual(lis.length, 3, '三笔订单应该是三个列表项');
  assert.strictEqual(nodes.filter((n) => n.tag === 'ul').length, 1);
  // 说明句仍然是段落，不能被一起塞进列表
  const firstP = nodes.find((n) => n.tag === 'p');
  assert.ok(firstP, '说明句要单独成段');
});

check('- 前缀的原始列表不受影响', () => {
  const text = ['以下是您最近的订单：', ...ORDER_ROWS.map((row) => '- ' + row)].join('\n');
  const nodes = flatten(render(text));
  assert.strictEqual(nodes.filter((n) => n.tag === 'li').length, 3);
});

check('U+2028 分隔的整段文本要能断开', () => {
  const text = ['您好：', ...ORDER_ROWS, '如需查看详情请告诉我订单号。'].join('\u2028');
  const nodes = flatten(render(text));
  assert.strictEqual(nodes.filter((n) => n.tag === 'li').length, 3);
  // 修复前这里会是 1 个 li（整段一行），修复后是 3 个
});

check('正文里的单个竖线不触发列表', () => {
  const text = '客服中心 | 工作时间 9:00-18:00\n如有疑问请随时联系我们。';
  const nodes = flatten(render(text));
  assert.strictEqual(nodes.filter((n) => n.tag === 'ul').length, 0);
  assert.strictEqual(nodes.filter((n) => n.tag === 'br').length, 1, '两行普通文本之间用 br');
});

check('表格仍然走表格分支', () => {
  const text = ['| 订单号 | 状态 |', '| --- | --- |', '| ORD-1 | 已发货 |'].join('\n');
  const nodes = flatten(render(text));
  assert.strictEqual(nodes.filter((n) => n.tag === 'table').length, 1);
  assert.strictEqual(nodes.filter((n) => n.tag === 'td').length, 2);
});

check('键值对仍然排成两列', () => {
  const text = ['**订单号**：ORD-1', '**状态**：已发货'].map((row) => '- ' + row).join('\n');
  const nodes = flatten(render(text));
  assert.ok(nodes.some((n) => n.className === 'md-kv'), '两条带标签的列表应转成键值表');
});

check('订单行排成四列记录结构（单号/商品/状态/日期）', () => {
  const text = ['您好，已为您查到名下共 3 笔订单：', ...ORDER_ROWS,
    '如需查看某笔订单的详情，请回复对应的订单号。'].join('\n');
  const rows = flatten(render(text)).filter((n) => /md-row-record/.test(n.className || ''));
  assert.strictEqual(rows.length, 3, '三笔订单应是三条记录');
  const li = flatten(render(text)).find((n) => /md-row-record/.test(n.className || ''));
  assert.ok(li, '记录行要有 md-row-record 类');
  const all = flatten(render(text));
  assert.strictEqual(all.filter((n) => n.className === 'md-cell md-key').length, 3, '每行首列是单号');
  assert.strictEqual(all.filter((n) => /md-meta/.test(n.className || '')).length, 3, '每行末列是日期');
});

check('- 前缀的原始订单列表同样排成记录结构', () => {
  const text = ORDER_ROWS.map((row) => '- ' + row).join('\n');
  const nodes = flatten(render(text));
  assert.strictEqual(nodes.filter((n) => /md-row-record/.test(n.className || '')).length, 3);
  assert.strictEqual(nodes.filter((n) => n.tag === 'ul').length, 1);
});

check('订单状态染成色块，文字本身不变', () => {
  const text = ORDER_ROWS.join('\n');
  const nodes = flatten(render(text));
  const chips = nodes.filter((n) => /^md-chip md-chip-/.test(n.className || ''));
  assert.strictEqual(chips.length, 3, '三个状态各一个色块');
  assert.ok(chips.some((c) => c.className === 'md-chip md-chip-ship'), '已发货 → ship');
  assert.ok(chips.some((c) => c.className === 'md-chip md-chip-wait'), '待发货 → wait');
  const texts = nodes.filter((n) => n.tag === '#text').map((n) => n.text);
  assert.ok(texts.includes('已发货') && texts.includes('待发货'), '色块里仍是原文');
});

check('非订单的四列内容不套记录结构', () => {
  const text = ['方案 A｜成本低｜上线快｜适合小团队', '方案 B｜成本高｜周期长｜适合大团队'].join('\n');
  const nodes = flatten(render(text));
  assert.strictEqual(nodes.filter((n) => /md-row-record/.test(n.className || '')).length, 0);
  assert.strictEqual(nodes.filter((n) => n.tag === 'li').length, 2, '仍按普通列表逐行显示');
});

check('三列订单行不误判成四列记录', () => {
  const text = ['ORD-1｜耳机｜已发货', 'ORD-2｜键盘｜待发货'].join('\n');
  const nodes = flatten(render(text));
  assert.strictEqual(nodes.filter((n) => /md-row-record/.test(n.className || '')).length, 0);
  assert.strictEqual(nodes.filter((n) => n.tag === 'li').length, 2);
});

console.log(`\n${checks} checks passed`);
