/* 极简 Markdown 渲染器（pc.html / h5.html 共用）。
 *
 * 设计取舍：全程用 createElement + textContent 构建节点，**任何地方都不碰 innerHTML**。
 * 模型输出可能被知识库内容污染（有人往 FAQ 里塞 <img onerror=...>），走 innerHTML
 * 就等于把 XSS 面交给一个正则去守。这里从结构上就不可能执行脚本，因此不需要
 * DOMPurify 之类的清洗库。
 *
 * 支持范围（够客服回答用，不追求完整 CommonMark）：
 *   块级：标题 #/##/###、无序列表 - * +、有序列表 1. 2.、引用 >、代码块 ```、分隔线 ---、表格、段落
 *   行内：**粗体**、*斜体*、`行内代码`、[文字](链接)
 * 额外：连续 `- **标签**：值` 会渲染成两列键值对齐（订单信息那四行）。
 * 额外：连续多行 `单号｜商品｜状态｜日期` 即使模型丢了 `- ` 也排成列表（pipeRow 兜底）；
 *       四列且首列带数字时进一步排成对齐记录行，状态词染成色块（只改颜色不改文字）。
 *
 * 不支持：嵌套列表、脚注、图片语法、HTML 透传。
 */
(function (global) {
  'use strict';

  // 只作模板用：inline() 是递归的，绝不能共享同一个带 g 标志的 RegExp 实例。
  // 内层一旦 exec，会把外层的 lastIndex 覆盖掉，外层循环就会从 0 重新开始 → 死循环。
  var INLINE = /(`[^`\n]+`)|(\*\*(?!\s)[^*\n]+?\*\*)|(__[^_\n]+?__)|(\[[^\]\n]+\]\((?:[^()\s]|\([^()\s]*\))+\))|(\*(?!\s)[^*\n]+?\*)/g;
  var LINK = /^\[([^\]]+)\]\(((?:[^()\s]|\([^()\s]*\))+)\)$/;
  var SAFE_HREF = /^(https?:|mailto:)/i;

  function el(tag, cls) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    return n;
  }

  /* 行内解析：把一段纯文本变成「文本节点 + 行内元素」的片段。
   * depth 限制递归层数，避免 **`**a**`** 这种畸形输入无限展开。 */
  function inline(parent, text, depth) {
    depth = depth || 0;
    if (depth > 3) {
      parent.appendChild(document.createTextNode(text));
      return;
    }
    var re = new RegExp(INLINE.source, 'g'); // 每次调用独立实例，递归安全
    var last = 0;
    var m;
    while ((m = re.exec(text))) {
      if (m.index > last) {
        parent.appendChild(document.createTextNode(text.slice(last, m.index)));
      }
      var tok = m[0];
      if (m[1]) {
        // 行内代码：内部不再解析，否则 `**a**` 会被误当粗体
        var code = el('code');
        code.textContent = tok.slice(1, -1);
        parent.appendChild(code);
      } else if (m[2] || m[3]) {
        var strong = el('strong');
        inline(strong, tok.slice(2, -2), depth + 1);
        parent.appendChild(strong);
      } else if (m[4]) {
        var lm = LINK.exec(tok);
        var href = lm[2];
        if (SAFE_HREF.test(href)) {
          var a = el('a');
          a.href = href;
          a.target = '_blank';
          a.rel = 'noopener noreferrer';
          inline(a, lm[1], depth + 1);
          parent.appendChild(a);
        } else {
          // javascript: / data: 一律降级成纯文字，只保留链接文字
          parent.appendChild(document.createTextNode(lm[1]));
        }
      } else if (m[5]) {
        var em = el('em');
        inline(em, tok.slice(1, -1), depth + 1);
        parent.appendChild(em);
      }
      last = re.lastIndex;
    }
    if (last < text.length) {
      parent.appendChild(document.createTextNode(text.slice(last)));
    }
  }

  /* 一组列表项如果都是「**标签**：值」的形状，就转成两列键值表。
   * 要求全部命中才转 —— 混合内容（其中一项是普通句子）按普通列表渲染，
   * 否则会出现"一半对齐一半不对齐"的错位。 */
  function keyValueRows(items) {
    if (items.length < 2 || items.length > 8) return null;
    var rows = [];
    for (var i = 0; i < items.length; i++) {
      var m = /^\s*\*\*(.+?)\*\*\s*[：:]\s*(\S.*)$/.exec(items[i]);
      if (!m) return null;
      rows.push([m[1], m[2]]);
    }
    return rows;
  }

  /* 订单状态词 → 视觉分组。**只影响颜色，不改一个字**：客户读到的仍是原文，
   * 只是"已发货"和"待发货"不再长得一样。未命中任何规则就按普通文字渲染。 */
  var STATE_KIND = [
    [/^(待付款|待支付|未付款|待确认|待发货|备货中|待出库)$/, 'wait'],
    [/^(已发货|运输中|派送中|待收货|已揽收|清关中|已出库)$/, 'ship'],
    [/^(已签收|已完成|交易成功|已收货|已送达)$/, 'done'],
    [/^(退货中|退款中|售后中|换货中|退货完成)$/, 'after'],
    [/^(已取消|已关闭|已退款|交易关闭)$/, 'void']
  ];

  function stateKind(cell) {
    var t = String(cell).replace(/[\s`*]/g, '');
    if (!t || t.length > 6) return '';
    for (var i = 0; i < STATE_KIND.length; i++) {
      if (STATE_KIND[i][0].test(t)) return STATE_KIND[i][1];
    }
    return '';
  }

  function splitPipe(line) {
    var parts = String(line).trim().split(/[｜|]/);
    for (var i = 0; i < parts.length; i++) parts[i] = parts[i].trim();
    while (parts.length > 1 && !parts[parts.length - 1]) parts.pop(); // 尾随竖线
    return parts;
  }

  /* 一条记录 = 四列 + 首列含数字（单号｜商品｜状态｜日期）。
   * 要求整组行同为四列，否则退回普通文本 —— 混排会让网格错位。 */
  function recordCells(items) {
    var rows = [];
    for (var i = 0; i < items.length; i++) {
      var parts = splitPipe(items[i]);
      if (parts.length !== 4 || !/\d/.test(parts[0])) return null;
      rows.push(parts);
    }
    return rows.length ? rows : null;
  }

  function recordCell(text, index) {
    var kind = stateKind(text);
    var cell = el('span', 'md-cell');
    if (kind) {
      cell.className += ' md-state';
      var chip = el('span', 'md-chip md-chip-' + kind);
      inline(chip, text);
      cell.appendChild(chip);
      return cell;
    }
    cell.className += index === 0 ? ' md-key' : (index === 1 ? ' md-main' : ' md-meta');
    inline(cell, text);
    return cell;
  }

  function buildRecords(rows) {
    var list = el('ul', 'md-rows');
    for (var i = 0; i < rows.length; i++) {
      var li = el('li', 'md-row md-row-record');
      for (var c = 0; c < rows[i].length; c++) li.appendChild(recordCell(rows[i][c], c));
      list.appendChild(li);
    }
    return list;
  }

  function buildList(items, ordered) {
    var records = ordered ? null : recordCells(items);
    if (records) return buildRecords(records);

    var rows = keyValueRows(items);
    if (rows) {
      var grid = el('div', 'md-kv');
      for (var i = 0; i < rows.length; i++) {
        var k = el('div', 'md-kv-k');
        k.textContent = rows[i][0];
        var v = el('div', 'md-kv-v');
        inline(v, rows[i][1]);
        grid.appendChild(k);
        grid.appendChild(v);
      }
      return grid;
    }
    var list = el(ordered ? 'ol' : 'ul');
    for (var j = 0; j < items.length; j++) {
      var li = el('li');
      inline(li, items[j]);
      list.appendChild(li);
    }
    return list;
  }

  var UL = /^\s*[-*+]\s+/;
  var OL = /^\s*\d+[.)]\s+/;
  var HEADING = /^(#{1,3})\s+(.+)$/;
  var HR = /^\s*([-*_])\1{2,}\s*$/;
  var FENCE = /^\s*```(\w*)\s*$/;
  var QUOTE = /^\s*>\s?/;
  var BLOCK_START = /^\s*(?:[-*+]\s|\d+[.)]\s|#{1,3}\s|>|```)/;

  function tableCells(line) {
    var s = line.trim();
    if (s.charAt(0) !== '|' || s.charAt(s.length - 1) !== '|') return null;
    var cells = [];
    var cell = '';
    for (var i = 1; i < s.length - 1; i++) {
      if (s.charAt(i) === '\\' && s.charAt(i + 1) === '|') { cell += '|'; i++; }
      else if (s.charAt(i) === '|') { cells.push(cell.trim()); cell = ''; }
      else cell += s.charAt(i);
    }
    cells.push(cell.trim());
    return cells;
  }

  function tableColumns(lines, i) {
    if (i + 1 >= lines.length) return null;
    var head = tableCells(lines[i]);
    var separator = tableCells(lines[i + 1]);
    if (!head || !separator || head.length !== separator.length || !head.length) return null;
    if (!separator.every(function (cell) { return /^:?-{3,}:?$/.test(cell); })) return null;
    return head;
  }

  function buildTable(lines, start, headers) {
    var wrap = el('div', 'md-table-wrap');
    var table = el('table');
    var thead = el('thead');
    var tr = el('tr');
    headers.forEach(function (cell) {
      var th = el('th');
      inline(th, cell);
      tr.appendChild(th);
    });
    thead.appendChild(tr);
    table.appendChild(thead);
    var tbody = el('tbody');
    var i = start + 2;
    while (i < lines.length) {
      var cells = tableCells(lines[i]);
      if (!cells || cells.length !== headers.length) break;
      tr = el('tr');
      cells.forEach(function (cell) {
        var td = el('td');
        inline(td, cell);
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
      i++;
    }
    table.appendChild(tbody);
    wrap.appendChild(table);
    return { node: wrap, next: i };
  }

  /* 「一行一条记录」的兜底识别：`单号｜商品｜状态｜日期` 这种用竖线分隔的连续行。
   *
   * 工具给 Agent 的原文是 `- ORD-…｜…｜…` 的列表，但模型改写时经常把 `- ` 吃掉。
   * 少了列表符号，Markdown 就没有任何可渲染的结构，客户看到的是"三行话糊在一起"。
   * 这里只认「连续 ≥2 行、每行 ≥2 个竖线、且不含行内标记」的块，达不到条件就当普通
   * 文本，避免把正文里的 `A | B` 误判成列表。 */
  var PIPE = /[｜|]/g;

  function pipeRow(line) {
    var t = line.trim();
    if (!t) return false;
    if (t.charAt(0) === '|') return false;                  // 表格分支已经处理过的形状
    if (/^[-*+]\s|\d+[.)]\s|^#{1,3}\s|^>|^```/.test(t)) return false; // 本身是块级标记
    if (t.indexOf('**') >= 0 || t.indexOf('`') >= 0 || t.indexOf('](') >= 0) return false;
    if (/^:?-{3,}:?$/.test(t)) return false;
    var hits = t.match(PIPE);
    return !!hits && hits.length >= 2;
  }

  function pipeRunStarts(lines, at) {
    return pipeRow(lines[at]) && at + 1 < lines.length && pipeRow(lines[at + 1]);
  }

  function buildPipeList(lines, start, end) {
    var items = [];
    for (var k = start; k < end; k++) items.push(lines[k].trim());

    var records = recordCells(items);
    if (records) return buildRecords(records);

    var list = el('ul', 'md-rows');
    for (var n = 0; n < items.length; n++) {
      var li = el('li', 'md-row');
      inline(li, items[n]);
      list.appendChild(li);
    }
    return list;
  }

  function render(el_, text) {
    el_.textContent = '';
    // U+2028/U+2029 是 JSON 里合法的换行字符，模型偶发输出会让整段挤成一行
    var lines = String(text == null ? '' : text)
      .replace(/\r\n?/g, '\n')
      .replace(/[\u2028\u2029]/g, '\n')
      .split('\n');
    var i = 0;

    while (i < lines.length) {
      var line = lines[i];

      if (!line.trim()) { i++; continue; }

      var headers = tableColumns(lines, i);
      if (headers) {
        var tableBlock = buildTable(lines, i, headers);
        el_.appendChild(tableBlock.node);
        i = tableBlock.next;
        continue;
      }

      // 代码块：内部原样保留，不解析任何标记
      var fence = FENCE.exec(line);
      if (fence) {
        var buf = [];
        i++;
        while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) { buf.push(lines[i]); i++; }
        i++;
        var pre = el('pre');
        var codeEl = el('code');
        if (fence[1]) codeEl.className = 'lang-' + fence[1];
        codeEl.textContent = buf.join('\n');
        pre.appendChild(codeEl);
        el_.appendChild(pre);
        continue;
      }

      var h = HEADING.exec(line);
      if (h) {
        // 用 div 而不是 h1/h2/h3：气泡里的标题不该参与页面大纲，也不该继承全局标题样式
        var hd = el('div', 'md-h' + h[1].length);
        inline(hd, h[2]);
        el_.appendChild(hd);
        i++;
        continue;
      }

      if (HR.test(line)) {
        el_.appendChild(el('div', 'md-hr'));
        i++;
        continue;
      }

      if (UL.test(line)) {
        var items = [];
        while (i < lines.length && UL.test(lines[i])) {
          items.push(lines[i].replace(UL, ''));
          i++;
        }
        el_.appendChild(buildList(items, false));
        continue;
      }

      if (OL.test(line)) {
        var oitems = [];
        while (i < lines.length && OL.test(lines[i])) {
          oitems.push(lines[i].replace(OL, ''));
          i++;
        }
        el_.appendChild(buildList(oitems, true));
        continue;
      }

      if (QUOTE.test(line)) {
        var qlines = [];
        while (i < lines.length && QUOTE.test(lines[i])) {
          qlines.push(lines[i].replace(QUOTE, ''));
          i++;
        }
        var bq = el('blockquote');
        render(bq, qlines.join('\n'));
        el_.appendChild(bq);
        continue;
      }

      // 段落：吃到空行或下一个块级起始为止
      var plines = [line];
      i++;
      while (i < lines.length && lines[i].trim() && !BLOCK_START.test(lines[i]) && !tableColumns(lines, i)) {
        plines.push(lines[i]);
        i++;
      }
      // 段内再按「连续记录行」切开：说明句成段，记录行成列表
      var at = 0;
      while (at < plines.length) {
        if (pipeRunStarts(plines, at)) {
          var end = at;
          while (end < plines.length && pipeRow(plines[end])) end++;
          el_.appendChild(buildPipeList(plines, at, end));
          at = end;
          continue;
        }
        var stop = at + 1;
        while (stop < plines.length && !pipeRunStarts(plines, stop)) stop++;
        var p = el('p');
        for (var k = at; k < stop; k++) {
          if (k > at) p.appendChild(document.createElement('br'));
          inline(p, plines[k]);
        }
        el_.appendChild(p);
        at = stop;
      }
    }
  }

  /* 对外入口。传进来的元素会被清空后重建，可反复调用（流式节流依赖这一点）。 */
  global.renderMarkdown = function (target, text) {
    if (!target) return;
    render(target, text);
  };
})(window);
