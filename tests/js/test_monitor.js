// Monitor (device.html): a dónde vuelve, chips de la nota y las propiedades en el header. node --test tests/js/test_monitor.js
const test = require('node:test');
const assert = require('node:assert/strict');
const EB = require('../../remote/dashboard/espbench.js');

const O = 'http://localhost:8090';

test('monitorNav: abierto directo en el bench vuelve al home del bench', () => {
    const n = EB.monitorNav('/device.html', '', 'http://pi:8080');
    assert.deepEqual(n, {home: '/', href: '/', master: null});
    // desde el home con búsqueda: la conserva
    assert.equal(EB.monitorNav('/device.html', 'http://pi:8080/?q=chip%3Aesp32', 'http://pi:8080').href, '/?q=chip%3Aesp32');
    assert.equal(EB.monitorNav('/device.html', 'http://pi:8080/index.html?group=chip', 'http://pi:8080').href,
                 '/index.html?group=chip');
});

test('monitorNav: por el proxy de bench-master vuelve al master, no al bench', () => {
    const n = EB.monitorNav('/bench/bench-chile/device.html', '', O);
    assert.deepEqual(n, {home: '/', href: '/', master: 'bench-chile'});
    // desde el master con agrupación y búsqueda: las conserva
    assert.equal(EB.monitorNav('/bench/bench-chile/device.html', O + '/?group=bench&q=lte', O).href, '/?group=bench&q=lte');
    // nombre con caracteres escapados
    assert.equal(EB.monitorNav('/bench/lab%20ba/device.html', '', O).master, 'lab ba');
});

test('monitorNav: el referrer solo cuenta si es el home y del mismo origen', () => {
    // de otro origen (el bench directo): no
    assert.equal(EB.monitorNav('/bench/b/device.html', 'http://pi:8080/?q=x', O).href, '/');
    // de otra página del mismo origen (otro monitor): no
    assert.equal(EB.monitorNav('/device.html', O + '/device.html?tty=x', O).href, '/');
    // referrer roto
    assert.equal(EB.monitorNav('/device.html', 'no es url', O).href, '/');
});

test('monitorNav: por el master, desde el dashboard del bench (proxy) vuelve a ese dashboard', () => {
    const n = EB.monitorNav('/bench/b/device.html', O + '/bench/b/?q=x', O);
    assert.equal(n.href, '/bench/b/?q=x');
    assert.equal(n.home, '/');                       // los chips igual filtran en el master
    assert.equal(EB.monitorNav('/bench/b/device.html', O + '/bench/otro/', O).href, '/');
});

test('navFilterHref: los chips filtran en el master o en el bench', () => {
    assert.equal(EB.navFilterHref(EB.monitorNav('/bench/b/device.html', '', O), 'chip:esp32-s3'), '/?q=chip%3Aesp32-s3');
    assert.equal(EB.navFilterHref(EB.monitorNav('/sub/device.html', '', O), 'uso:ci'), '/sub/?q=uso%3Aci');
});

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

test('monitorState: el estado de la placa, no el de la conexión, y qué se puede hacer', () => {
    const now = new Date(2026, 9, 27, 12, 0, 0).getTime();
    const base = {tty_name: 'ttyUSB0', status: 'RUNNING', state: 'monitoring', mac: 'AA', last_log_epoch: now / 1000 - 1,
                  health: {panics: 0}};
    const ok = EB.monitorState(base, 'ttyUSB0', now);
    assert.deepEqual([ok.status.text, ok.write, ok.session, ok.reason], ['En línea', true, true, null]);
    assert.match(ok.empty, /Esperando datos del serial de ttyUSB0/);
    // El mismo estado que la card: un panic sin ver es Panic aunque el log esté conectado
    assert.equal(EB.monitorState(Object.assign({}, base, {health: {panics: 1}}), 'ttyUSB0', now).status.text, 'Panic');
    // No existe (404): nada se puede hacer y el log lo dice
    const nf = EB.monitorState(null, 'ttyUSB9', now);
    assert.deepEqual([nf.status.text, nf.write, nf.session], ['No existe', false, false]);
    assert.match(nf.empty, /No hay ninguna placa en ttyUSB9/);
    // Flasheando: no se escribe, la sesión sí se puede reiniciar
    const fl = EB.monitorState(Object.assign({}, base, {state: 'flashing'}), 'ttyUSB0', now);
    assert.deepEqual([fl.status.text, fl.write, fl.session], ['Flasheando', false, true]);
    // Proceso caído: reiniciar la sesión es justo lo que hace falta
    const down = EB.monitorState(Object.assign({}, base, {status: 'DOWN', state: null}), 'ttyUSB0', now);
    assert.deepEqual([down.status.text, down.write, down.session], ['Caída', false, true]);
    assert.match(down.reason, /reiniciá la sesión/);
    // El bench no contestó
    assert.deepEqual([EB.monitorState(undefined, 'ttyUSB0', now).status.text, EB.monitorState(undefined, 'ttyUSB0', now).write],
                     ['Sin datos', false]);
});
