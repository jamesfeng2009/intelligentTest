#!/usr/bin/env node
// 多语言 AST 函数提取脚本（JS/TS）
// 用法: node functions.js <file>
// 输出: JSON [{name, start_line, end_line}]
const fs = require('fs');
const path = require('path');

let parser;
try {
  parser = require('@babel/parser');
} catch (e) {
  console.error(JSON.stringify({ error: 'babel-parser 未安装: ' + e.message }));
  process.exit(2);
}

const file = process.argv[2];
if (!file) {
  console.error(JSON.stringify({ error: '缺少文件参数' }));
  process.exit(1);
}
const source = fs.readFileSync(file, 'utf-8');
const isTs = /\.(ts|tsx)$/i.test(file);

let ast;
try {
  ast = parser.parse(source, {
    sourceType: 'unambiguous',
    plugins: isTs
      ? ['typescript', 'jsx']
      : ['jsx'],
    errorRecovery: true,
  });
} catch (e) {
  // 解析失败（可能语法错误）：尽力用 recovery 模式
  try {
    ast = parser.parse(source, {
      sourceType: 'unambiguous',
      plugins: isTs ? ['typescript', 'jsx'] : ['jsx'],
      errorRecovery: true,
    });
  } catch (e2) {
    console.error(JSON.stringify({ error: 'AST 解析失败: ' + e2.message }));
    process.exit(3);
  }
}

function loc(node) {
  if (!node.loc) return null;
  return { start_line: node.loc.start.line, end_line: node.loc.end.line };
}

const funcs = [];
function walk(node) {
  if (!node || typeof node !== 'object') return;
  const type = node.type || '';
  if (
    type === 'FunctionDeclaration' ||
    type === 'FunctionExpression' ||
    type === 'ArrowFunctionExpression' ||
    type === 'ObjectMethod' ||
    type === 'ClassMethod'
  ) {
    // 只统计命名函数（匿名箭头函数跳过，避免噪声）
    let name = '';
    if (node.id && node.id.name) name = node.id.name;
    else if (node.key && (node.key.name || node.key.value)) name = String(node.key.name || node.key.value);
    if (!name) return;
    const l = loc(node);
    if (l) funcs.push({ name, ...l });
  }
  for (const key in node) {
    if (key === 'loc' || key === 'start' || key === 'end' || key === 'tokens' || key === 'comments') continue;
    const val = node[key];
    if (Array.isArray(val)) val.forEach(walk);
    else if (val && typeof val === 'object') walk(val);
  }
}
walk(ast.program);
console.log(JSON.stringify(funcs));
