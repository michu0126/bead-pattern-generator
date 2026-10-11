import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import { editGridCell, summarizeGrid } from '../app/static/editor-core.mjs';

const appSource = readFileSync(new URL('../app/static/app.js', import.meta.url), 'utf8');
const displaySource = appSource.slice(appSource.indexOf('function showDirectImage2Pattern('), appSource.indexOf('async function generatePattern('));
const materialSource = appSource.slice(appSource.indexOf('function refreshMaterials('), appSource.indexOf('function updatePixelPanel('));

function response() {
  return {
    board: { width: 2, height: 2 }, width: 2, height: 2,
    grid: [['H2', 'F5'], [null, 'H2']],
    colours: [{ code: 'H2', rgb: [255, 255, 255] }, { code: 'F5', rgb: [200, 20, 20] }],
    warnings: ['模型返回的白色区域请核对'],
  };
}

function harness() {
  let received;
  const context = vm.createContext({ initializeEditor: data => { received = data; } });
  vm.runInContext(displaySource, context);
  return { show: context.showDirectImage2Pattern, received: () => received };
}

test('Image2 editor consumes the authoritative grid, retaining edge whites and transparent cells', () => {
  const data = response();
  const display = harness();
  display.show(data);
  assert.equal(display.received(), data);
  assert.deepEqual(data.grid, [['H2', 'F5'], [null, 'H2']]);
  assert.equal(summarizeGrid(data.grid).total, 3);
  editGridCell(data.grid, 0, 1, 'H2');
  assert.deepEqual([...summarizeGrid(data.grid).counts], [['H2', 3]]);
});

test('Image2 editor rejects missing, malformed or unknown-code grids instead of raster resampling', () => {
  for (const alteration of [
    { grid: undefined }, { grid: [['H2']] }, { width: 52 },
    { grid: [['H2', 'INVALID'], [null, 'H2']] },
    { grid: [['H2', ''], [null, 'H2']] },
  ]) {
    const display = harness();
    assert.throws(() => display.show({ ...response(), ...alteration }), /逐格解析结果/);
    assert.equal(display.received(), undefined);
  }
  assert.throws(() => harness().show({ ...response(), colours: [] }), /色卡数据/);
  assert.doesNotMatch(appSource, /function (?:image2Grid|closestMardCode|clearExteriorWhite)\(/);
});

test('parser warnings are plain text and survive material refresh after an edit', () => {
  const data = response();
  const untrustedWarning = '<img src=x onerror="alert(1)">';
  let rendered;
  let nodes = [];
  const context = vm.createContext({
    patternGrid: data.grid,
    patternWarnings: [untrustedWarning],
    colourByCode: new Map(data.colours.map(item => [item.code, item])),
    summarizeGrid,
    renderMaterials: (items, total, board, empty) => {
      nodes = [];
      rendered = { items, total, board, empty };
    },
    document: { createElement: () => ({}) },
    $: () => ({ append: node => nodes.push(node) }),
  });
  vm.runInContext(materialSource, context);
  context.refreshMaterials();
  assert.equal(rendered.total, 3);
  assert.equal(nodes[0].textContent, untrustedWarning);
  assert.equal(nodes[0].innerHTML, undefined);
  editGridCell(data.grid, 0, 0, null);
  context.refreshMaterials();
  assert.equal(rendered.total, 2);
  assert.equal(rendered.empty, 2);
  assert.equal(nodes.length, 1);
  assert.equal(nodes[0].textContent, untrustedWarning);
});
