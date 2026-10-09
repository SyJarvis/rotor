import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('../src/rotor/frontend/charts.js', import.meta.url), 'utf8');
async function environment() {
  const instances = [];
  let color = '#09090b';
  class Chart {
    constructor(context, config) { this.context = context; this.config = config; this.destroyed = 0; instances.push(this); }
    destroy() { this.destroyed++; }
  }
  const context = vm.createContext({ window: { Chart }, document: { documentElement: {} }, getComputedStyle: () => ({ getPropertyValue: () => color }) });
  const module = new vm.SourceTextModule(source, { context });
  await module.link(() => {}); await module.evaluate();
  const canvas = { isConnected: true, pageActive: true, getContext() { return this; }, closest() { return { classList: { contains: () => this.pageActive } }; } };
  return { charts: module.namespace, instances, canvas, theme(value) { color = value; } };
}

test('usage curves use monotone interpolation, nonnegative axis, unstacked models and unconnected unknown values', async () => {
  const env = await environment();
  env.charts.renderLine('usage', env.canvas, ['a', 'b', 'c'], [{ label: 'Total', data: [0, null, 4] }], { fill: true, yTitle: 'USD' });
  const config = env.instances[0].config;
  assert.equal(config.type, 'line');
  assert.equal(config.plugins.length, 0); assert.equal(config.options.scales.x.min, undefined); assert.equal(config.options.scales.x.ticks.autoSkip, undefined);
  assert.equal(config.options.scales.y.stacked, false); assert.equal(config.options.scales.y.min, 0);
  assert.equal(config.options.scales.y.title.text, 'USD');
  const dataset = config.data.datasets[0];
  assert.equal(dataset.cubicInterpolationMode, 'monotone'); assert.equal(dataset.fill, true);
  assert.equal(dataset.spanGaps, false); assert.equal(dataset.data[1], null);
  env.charts.renderLine('usage', env.canvas, ['a'], [{ label: 'alpha', data: [1] }, { label: 'beta', data: [2] }], { fill: false });
  assert.equal(env.instances[0].destroyed, 1);
  assert.equal(env.instances[1].config.data.datasets.every((item) => item.fill === false), true);
});

test('every theme replacement remains registered and explicit destruction prevents chart resurrection', async () => {
  const env = await environment();
  env.charts.renderLine('usage', env.canvas, ['a'], [{ label: 'Total', data: [2] }]);
  env.theme('#fafafa'); env.charts.refreshTheme();
  assert.equal(env.instances[0].destroyed, 1);
  assert.equal(env.instances[1].config.data.datasets[0].borderColor, '#fafafa');
  env.charts.refreshTheme();
  assert.equal(env.instances[1].destroyed, 1);
  env.charts.destroyChart('usage');
  assert.equal(env.instances[2].destroyed, 1);
  env.charts.refreshTheme();
  assert.equal(env.instances.length, 3);
});

test('detached or inactive page canvases are not recreated on theme changes and destroyAll forgets factories', async () => {
  const env = await environment();
  env.charts.renderDonut('donut', env.canvas, ['alpha'], [2]);
  env.canvas.isConnected = false;
  env.charts.refreshTheme();
  assert.equal(env.instances.length, 1); assert.equal(env.instances[0].destroyed, 1);
  env.canvas.isConnected = true; env.canvas.pageActive = false;
  env.charts.renderHBar('channels', env.canvas, ['scoped'], [75]);
  env.charts.refreshTheme();
  assert.equal(env.instances.length, 2); assert.equal(env.instances[1].destroyed, 1);
  env.canvas.pageActive = true;
  env.charts.renderLine('usage', env.canvas, ['a'], [{ label: 'Total', data: [1] }]);
  env.charts.destroyAll(); env.charts.refreshTheme();
  assert.equal(env.instances.length, 3); assert.equal(env.instances[2].destroyed, 1);
});


function markerDrawing(plugin, value = 13.5) {
  const calls = [], chartArea = { left: 10, right: 250, top: 20, bottom: 120 };
  const ctx = { save() { calls.push(['save']); }, restore() { calls.push(['restore']); }, setLineDash(dash) { calls.push(['dash', ...dash]); }, beginPath() {}, moveTo(x, y) { calls.push(['start', x, y]); }, lineTo(x, y) { calls.push(['end', x, y]); }, stroke() { calls.push(['stroke', this.strokeStyle, this.lineWidth]); }, measureText() { return { width: 20 }; }, fillText(label, x, y) { calls.push(['label', label, x, y, this.fillStyle]); } };
  plugin.afterDatasetsDraw({ ctx, chartArea, scales: { x: { getPixelForValue(actual) { assert.equal(actual, value); return 10 + actual * 10; } } } });
  return calls;
}

test('Today axis includes the day-end boundary, sparse labels and a correctly positioned local dashed marker', async () => {
  const env = await environment(); const labels = Array.from({ length: 25 }, (_, hour) => `${String(hour).padStart(2, '0')}:00`);
  env.charts.renderLine('usage', env.canvas, labels, [{ label: 'Total', data: [...Array(14).fill(0), ...Array(11).fill(null)] }], { xAxis: { min: 0, max: 24, tickStep: 4 }, nowMarker: { value: 13.5, label: 'Now' } });
  const config = env.instances[0].config, axis = config.options.scales.x;
  assert.equal(axis.type, 'category'); assert.equal(axis.min, 0); assert.equal(axis.max, 24); assert.equal(axis.ticks.autoSkip, false);
  assert.deepEqual(labels.map((_, index) => axis.ticks.callback(index)).filter(Boolean), ['00:00', '04:00', '08:00', '12:00', '16:00', '20:00', '24:00']);
  assert.equal(config.plugins.length, 1); assert.equal(config.data.datasets[0].data.at(-1), null); assert.equal(config.data.datasets[0].spanGaps, false);
  const drawn = markerDrawing(config.plugins[0]);
  assert.deepEqual(drawn.find(([name]) => name === 'start'), ['start', 145, 20]); assert.deepEqual(drawn.find(([name]) => name === 'end'), ['end', 145, 120]);
  assert.deepEqual(drawn.find(([name]) => name === 'dash'), ['dash', 3, 4]); assert.deepEqual(drawn.find(([name]) => name === 'stroke'), ['stroke', 'rgba(9, 9, 11, 0.4)', 1]);
  assert.equal(drawn.filter(([name]) => name === 'label').length, 1); assert.equal(drawn.at(-1)[0], 'restore');
});

test('marker colors rebuild with theme without duplication or resurrection, and midnight labels stay within the chart', async () => {
  const env = await environment();
  for (const value of [0, 23 + 59 / 60]) {
    env.charts.renderLine('usage', env.canvas, ['00:00', '24:00'], [{ label: 'Total', data: [0, null] }], { nowMarker: { value, label: '现在' } });
    const last = env.instances.at(-1); assert.equal(last.config.plugins.length, 1);
    const drawn = markerDrawing(last.config.plugins[0], value), label = drawn.find(([name]) => name === 'label');
    assert.ok(label[2] >= 10 && label[2] + 20 <= 250);
  }
  const previous = env.instances.at(-1); env.theme('#fafafa'); env.charts.refreshTheme();
  assert.equal(previous.destroyed, 1); assert.equal(env.instances.at(-1).config.plugins.length, 1);
  const drawn = markerDrawing(env.instances.at(-1).config.plugins[0], 23 + 59 / 60);
  assert.equal(drawn.find(([name]) => name === 'stroke')[1], 'rgba(250, 250, 250, 0.4)');
  const count = env.instances.length; env.charts.destroyChart('usage'); env.charts.refreshTheme(); assert.equal(env.instances.length, count);
});

test('all registry consumers use the same versioned charts module URL', async () => {
  const files = ['app.js', 'pages/usage.js', 'pages/logs.js'];
  const versions = await Promise.all(files.map(async (file) => (await readFile(new URL(`../src/rotor/frontend/${file}`, import.meta.url), 'utf8')).match(/charts\.js\?v=(\d+)/)?.[1]));
  assert.equal(new Set(versions).size, 1); assert.ok(versions[0]);
});
