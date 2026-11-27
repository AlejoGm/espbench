// WiFi del banco en el dashboard (index.html): EB.wifiView, wifiChipHtml, wifiMenuHtml, wifiUntilText.
// node --test tests/js/test_wifi.js
const test = require('node:test');
const assert = require('node:assert/strict');
const EB = require('../../remote/dashboard/espbench.js');

const OFF = {available: true, configured: true, on: false, ssid: 'RedDelBanco', channel: 6, clients: [], error: null};
const ON = {available: true, configured: true, on: true, ssid: 'RedDelBanco', channel: 6, by: 'facu', until: '2026-11-27T18:40:00-03:00',
            clients: [{mac: 'AA:BB:CC:DD:EE:01', ip: '10.42.0.23', signal_dbm: -41, board: '13875803451784'},
                      {mac: '11:22:33:44:55:66', ip: null, signal_dbm: null, board: null}], error: null};

test('wifiView/wifiChipHtml: sin WiFi (una Pi) no hay chip', () => {
    for (const w of [null, undefined, {available: false, on: false}]) {
        assert.equal(EB.wifiView(w), null);
        assert.equal(EB.wifiChipHtml(w), '');
        assert.equal(EB.wifiMenuHtml(w), '');
    }
});

test('wifiChipHtml: apagada, prendida con placas y hora, y estado desconocido', () => {
    assert.match(EB.wifiChipHtml(OFF), /class="mchip wifi wifi-off"[^>]*>.*ti-wifi-off.*WiFi<\/b> apagada/);
    assert.match(EB.wifiChipHtml(ON), /class="mchip wifi wifi-on".*2 placas · hasta 18:40/);
    assert.match(EB.wifiChipHtml(ON), /prendida por facu/);
    assert.match(EB.wifiChipHtml({...ON, clients: [ON.clients[0]], until: null}), /1 placa</);
    assert.match(EB.wifiChipHtml({available: true, on: null, error: 'sudo: a password is required'}), /wifi warn.*title="sudo: a password is required"/);
});

test('wifiMenuHtml apagada: duraciones (8 h elegida por defecto) y Prender', () => {
    const now = new Date(2026, 10, 27, 10, 40).getTime();
    const html = EB.wifiMenuHtml(OFF, undefined, now);
    assert.match(html, /RedDelBanco · canal 6 · apagada/);
    assert.deepEqual([...html.matchAll(/data-pick="(\d+)"/g)].map(m => +m[1]), [1, 4, 8, 12]);
    assert.match(html, /data-pick="8" class="on" aria-pressed="true"/);
    assert.match(html, /data-act="on" data-hours="8".*Prender hasta las 18:40/);
    assert.match(EB.wifiMenuHtml(OFF, 1, now), /data-hours="1".*hasta las 11:40/);
});

test('wifiMenuHtml prendida: placas por nombre (o MAC), Extender y Apagar', () => {
    const html = EB.wifiMenuHtml(ON);
    assert.match(html, /prendida por facu · se apaga a las 18:40/);
    assert.match(html, /<span>13875803451784<\/span><span>10.42.0.23<\/span><span>-41 dBm<\/span>/);
    assert.match(html, /<span>11:22:33:44:55:66<\/span><span>–<\/span><span><\/span>/);
    assert.match(html, /data-act="on" data-hours="8">Extender 8 h/);
    assert.match(html, /data-act="off".*Apagar/);
    assert.match(EB.wifiMenuHtml({...ON, clients: []}), /Ninguna placa conectada/);
    assert.match(EB.wifiMenuHtml({...ON, ssid: '<b>x'}), /&lt;b&gt;x/);
    assert.match(EB.wifiMenuHtml({...OFF, error: 'el dongle no está enchufado'}), /wm-err">el dongle no está enchufado/);
});

test('sin configurar: chip "sin configurar" y el menú es el formulario', () => {
    const w = {available: true, configured: false, on: false, iface: 'wlxb0487a', ssid: null, channel: null, clients: []};
    assert.match(EB.wifiChipHtml(w), /wifi-off.*sin configurar/);
    assert.match(EB.wifiChipHtml(w), /Dongle enchufado \(wlxb0487a\)/);
    const html = EB.wifiMenuHtml(w);
    assert.match(html, /Configurar la red del banco/);
    assert.match(html, /name="ssid"[^>]*value=""/);
    assert.match(html, /name="password" type="password"[^>]*placeholder="8 a 63 caracteres"/);
    assert.match(html, /<option value="6" selected>6<\/option>/);
    assert.doesNotMatch(html, /data-cancel/);                        // sin configurar no hay a dónde volver
    assert.match(html, /data-save="1"/);
});

test('configurada: "Configurar la red" en el menú; el formulario trae el SSID pero nunca la clave', () => {
    assert.match(EB.wifiMenuHtml(OFF), /data-cfg="1"/);
    const html = EB.wifiMenuHtml({...OFF, channel: 11}, 8, null, 'config');
    assert.match(html, /name="ssid"[^>]*value="RedDelBanco"/);
    assert.match(html, /placeholder="sin cambios"/);
    assert.doesNotMatch(html, /name="password"[^>]*value=/);
    assert.match(html, /<option value="11" selected>/);
    assert.match(html, /data-cancel="1"/);
});

test('wifiConfigCheck: la misma validación que el server', () => {
    const ok = {ssid: 'RedDelBanco', password: 'una-clave-larga', channel: '6'};
    assert.equal(EB.wifiConfigCheck(ok, false), null);
    assert.equal(EB.wifiConfigCheck({...ok, password: ''}, true), null);           // configurada: vacía = la misma
    assert.match(EB.wifiConfigCheck({...ok, password: ''}, false), /Falta la clave/);
    assert.match(EB.wifiConfigCheck({...ok, ssid: ' '}, false), /Falta el nombre/);
    assert.match(EB.wifiConfigCheck({...ok, ssid: 'x'.repeat(33)}, false), /32/);
    assert.match(EB.wifiConfigCheck({...ok, password: 'corta'}, false), /entre 8 y 63/);
    assert.match(EB.wifiConfigCheck({...ok, password: 'contraseña-larga'}, false), /entre 8 y 63/);
    assert.match(EB.wifiConfigCheck({...ok, channel: '14'}, false), /Canal/);
});
