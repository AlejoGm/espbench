// Monitor (device.html): chips de la nota y las propiedades en el header. node --test tests/js/test_monitor.js
const test = require('node:test');
const assert = require('node:assert/strict');
const EB = require('../../remote/dashboard/espbench.js');

test('metaSupported: 404 del catálogo o device sin props/note = bench viejo', () => {
    const d = {mac: 'AA', props: {}, note: null};
    assert.equal(EB.metaSupported(d, 404), false);
    assert.equal(EB.metaSupported({mac: 'AA', device_key: 'x'}, 200), false);
    assert.equal(EB.metaSupported(d, 200), true);
    assert.equal(EB.metaSupported(d, null), null);           // catálogo todavía no cargó
    assert.equal(EB.metaSupported(d, 500), true);            // error transitorio: no se esconde
});

const CAT = [{id: 'estado', values: [{id: 'roto', label: 'roto', warn: true, exclude_pick: true}, {id: 'testeando', label: 'testeando'}]},
             {id: 'chip', values: [{id: 'esp32-s3', label: 'ESP32-S3'}]},
             {id: 'conectividad', multi: true, values: [{id: 'wifi', label: 'WiFi'}, {id: 'lte', label: 'LTE'}]}];

test('monitorMetaHtml: sin nada, un único botón para agregar', () => {
    const h = EB.monitorMetaHtml({mac: 'AA', props: {}, note: null}, CAT, true);
    assert.equal((h.match(/<button/g) || []).length, 1);
    assert.match(h, /data-act="add"/);
    assert.equal(EB.monitorMetaHtml({tty_name: 'x'}, CAT, true), '');      // sin MAC no hay nota ni props
});

test('monitorMetaHtml: chips, nota resumida y botones', () => {
    const d = {mac: 'AA', props: {estado: 'roto', chip: 'esp32-s3', conectividad: ['lte', 'wifi']},
               note: 'no tocar <b>', note_by: 'ana', note_at: '2026-10-06T11:00:00'};
    const h = EB.monitorMetaHtml(d, CAT, true, new Date(2026, 9, 6, 12, 0, 0).getTime());
    // el estado que excluye primero, después las specs en el orden del catálogo
    const order = [...h.matchAll(/data-filter="([^"]+)"/g)].map(m => m[1]);
    assert.deepEqual(order, ['estado:roto', 'chip:esp32-s3', 'conectividad:wifi', 'conectividad:lte']);
    assert.match(h, /class="note-chip" data-act="note"/);
    assert.match(h, /no tocar &lt;b&gt;/);
    assert.ok(!h.includes('<b>'));
    assert.match(h, /title="Nota de ana[^"]*no tocar &lt;b&gt;/);          // texto completo en el tooltip
    assert.match(h, /data-act="props"/);
    assert.equal((h.match(/data-act="note"/g) || []).length, 1);           // con nota, la edita el chip
});

test('monitorMetaHtml: props sin nota → botón para agregar la nota', () => {
    const h = EB.monitorMetaHtml({mac: 'AA', props: {chip: 'esp32-s3'}, note: null}, CAT, true);
    assert.match(h, /data-act="props"/);
    assert.match(h, /<button class="meta-btn icon" data-act="note"/);
    assert.ok(!h.includes('note-chip'));
});

test('monitorMetaHtml: bench sin soporte → ícono deshabilitado, sin botones', () => {
    const h = EB.monitorMetaHtml({mac: 'AA'}, null, false);
    assert.ok(!h.includes('<button'));
    assert.match(h, /meta-off/);
    assert.ok(h.includes(EB.META_OFF_TITLE));
});
