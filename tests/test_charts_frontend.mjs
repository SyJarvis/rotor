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
