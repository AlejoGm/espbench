// Lógica de la UI v2 (cards de placa, estado, actividad, chips del bench). node --test tests/js/test_v2.js
const test = require('node:test');
const assert = require('node:assert/strict');
const EB = require('../../remote/dashboard/espbench.js');

const NOW = new Date(2026, 9, 6, 12, 0, 0).getTime();
const base = {tty_name: 'ttyUSB0', status: 'RUNNING', state: 'monitoring', mac: 'AA:BB', device_key: 'medidor',
              hw_model: 'SFY1', fw_version: '56.1', last_log_epoch: NOW / 1000 - 2,
              health: {boots: 1, panics: 0, boot_loop: false, last_reset: {ts: '2026-10-06T09:47:00', reason: 'POWERON_RESET', abnormal: false}}};
const dev = (over) => Object.assign({}, base, over);

test('boardStatus: prioridad de estados', () => {
    assert.deepEqual(EB.boardStatus(dev({}), NOW), {cls: 'ok', text: 'En línea'});
    assert.equal(EB.boardStatus(dev({status: 'DOWN'}), NOW).text, 'Caída');
    assert.equal(EB.boardStatus(dev({status: 'DOWN', state: 'disconnected'}), NOW).text, 'Desconectada');
    assert.deepEqual(EB.boardStatus(dev({state: 'flashing'}), NOW), {cls: 'flash', text: 'Flasheando'});
    assert.equal(EB.boardStatus(dev({state: 'unknown'}), NOW).text, 'Sin MAC');
    assert.equal(EB.boardStatus(dev({health: {boot_loop: true, panics: 3}}), NOW).text, 'Boot loop');
    assert.equal(EB.boardStatus(dev({health: {panics: 1}}), NOW).text, 'Panic');
    assert.equal(EB.boardStatus(dev({health: {panics: 3}}), NOW).text, '3 panics');
    assert.deepEqual(EB.boardStatus(dev({last_log_epoch: NOW / 1000 - 600}), NOW), {cls: 'warn', text: 'Sin log'});
    assert.equal(EB.boardStatus(dev({health: {last_reset: {abnormal: true, reason: 'X'}}}), NOW).text, 'Reset anormal');
});

test('silentFor: solo monitoreando y pasado el umbral', () => {
    assert.equal(EB.silentFor(dev({last_log_epoch: NOW / 1000 - 299}), NOW), null);
    assert.equal(Math.round(EB.silentFor(dev({last_log_epoch: NOW / 1000 - 301}), NOW)), 301);
    assert.equal(EB.silentFor(dev({state: 'flashing', last_log_epoch: 1}), NOW), null);
    assert.equal(EB.silentFor(dev({last_log_epoch: null}), NOW), null);
});

test('uptimeParts: desde el último boot', () => {
    assert.deepEqual(EB.uptimeParts(dev({}), NOW), ['2', 'h', '13', 'min']);
    assert.deepEqual(EB.uptimeParts(dev({health: {last_reset: {ts: '2026-10-05T08:00:00'}}}), NOW), ['1', 'd', '4', 'h']);
    assert.deepEqual(EB.uptimeParts(dev({health: {last_reset: {ts: '2026-10-06T11:57:00'}}}), NOW), ['3', 'min']);
    assert.equal(EB.uptimeParts(dev({health: {}}), NOW), null);
    assert.equal(EB.uptimeParts(dev({state: 'flashing'}), NOW), null);
});

test('activityBarsHtml / activityTotals', () => {
    const z = {boot: 0, panic: 0, flash: 0, boot_loop: 0, reserve: 0};
    const b = [Object.assign({}, z, {boot: 2}), Object.assign({}, z, {panic: 1, boot: 1}), Object.assign({}, z, {flash: 1, reserve: 1}), z];
    const html = EB.activityBarsHtml(b);
    assert.equal((html.match(/<span/g) || []).length, 4);
    assert.match(html, /class="boot" style="height:38%" title="Hace 4 h: 2 reinicios"/);
    assert.match(html, /class="panic" style="height:100%" title="Hace 3 h: 1 reinicio, 1 panic"/);
    assert.match(html, /class="flash resv"/);
    assert.match(html, /title="Hace 1 h: sin novedades"/);
    assert.deepEqual(EB.activityTotals(b), {boot: 3, panic: 1, flash: 1, boot_loop: 0, reserve: 1});
});

test('benchChips: valores, RAM y niveles', () => {
    const c = EB.benchChips({temp_c: 71.2, ram: {total_mb: 3793, used_pct: 25}, disk: {used_pct: 41}, load: {'1m': 0.4, cpus: 4}, uptime_s: 533040});
    assert.deepEqual(c.map(x => x.value), ['71,2 °C', '25 %', '41 %', '0,4', '6 d 4 h']);
    assert.equal(c[0].level, 'warn');
    assert.equal(c[1].label, 'RAM de 4 GB');
    assert.deepEqual(EB.benchChips({temp_c: null, ram: null, disk: null, load: null, uptime_s: null}), []);
    assert.equal(EB.benchChips({temp_c: 82}).length, 1);
    assert.equal(EB.benchChips({temp_c: 82})[0].level, 'bad');
});

test('boardCardHtml: card completa, escapada, con acciones', () => {
    const html = EB.boardCardHtml(dev({device_key: '<b>x</b>', lock_user: 'ana'}), {now: NOW, rename: true,
        buckets: Array(24).fill(0).map(() => ({boot: 0, panic: 0, flash: 0, boot_loop: 0, reserve: 0}))});
    assert.match(html, /&lt;b&gt;x&lt;\/b&gt;/);
    assert.doesNotMatch(html, /<b>x<\/b>/);
    assert.match(html, /class="status ok">En línea/);
    assert.match(html, /<span class="big">2<em>h<\/em>13<em>min<\/em><\/span>/);
    assert.match(html, /data-act="rename"/);
    assert.match(html, /data-act="copy"/);
    assert.match(html, /href="device.html\?tty=ttyUSB0"/);
    assert.match(html, /class="lock"[^>]*><i class="ti ti-lock"><\/i>ana</);
    assert.match(html, /Último log <b>2 s<\/b>/);
    const m = EB.boardCardHtml(dev({}), {now: NOW, bench: 'pi2', href: 'bench/pi2/device.html?tty=ttyUSB0', direct: 'http://x:8080/device.html'});
    assert.match(m, /SFY1, en pi2, ttyUSB0/);
    assert.match(m, /title="Abrir directo en el bench"/);
    assert.doesNotMatch(m, /data-act="rename"/);
    assert.doesNotMatch(m, /class="bars"/);           // sin actividad cargada, sin barras
});

const CAT = [{id: 'estado', label: 'estado', multi: false, values: [
                 {id: 'no-tocar', label: 'no tocar', warn: true, exclude_pick: true},
                 {id: 'prestada', label: 'prestada', warn: true, exclude_pick: true},
                 {id: 'testeando', label: 'testeando'}]},
             {id: 'chip', label: 'chip', multi: false, values: [{id: 'esp32-s3', label: 'esp32-s3'}]}];

test('boardStatus: no-tocar / roto (avoid) solo si está sana; con catálogo usa sus exclude_pick', () => {
    assert.deepEqual(EB.boardStatus(dev({props: {estado: 'no-tocar'}}), NOW), {cls: 'avoid', text: 'No tocar'});
    assert.deepEqual(EB.boardStatus(dev({props: {estado: 'no-tocar'}}), NOW, CAT), {cls: 'avoid', text: 'No tocar'});
    assert.deepEqual(EB.boardStatus(dev({props: {estado: 'roto'}}), NOW), {cls: 'avoid', text: 'Roto'});
    assert.equal(EB.boardStatus(dev({props: {estado: 'prestada'}}), NOW).cls, 'ok');            // sin catálogo: los de siempre
    assert.equal(EB.boardStatus(dev({props: {estado: 'prestada'}}), NOW, CAT).cls, 'avoid');
    assert.equal(EB.boardStatus(dev({props: {estado: 'testeando'}}), NOW, CAT).cls, 'ok');
    // un problema gana: se ve el problema, no el estado
    assert.equal(EB.boardStatus(dev({props: {estado: 'roto'}, health: {panics: 1}}), NOW).text, 'Panic');
    assert.equal(EB.boardStatus(dev({props: {estado: 'roto'}, status: 'DOWN'}), NOW).cls, 'off');
    assert.equal(EB.boardStatus(dev({props: {estado: 'roto'}, last_log_epoch: NOW / 1000 - 600}), NOW).text, 'Sin log');
    assert.deepEqual(EB.avoidedBy(dev({props: {estado: 'no-tocar'}}), CAT), {cat: 'estado', value: 'no-tocar', label: 'no tocar'});
    assert.equal(EB.avoidedBy(dev({}), CAT), null);
});

test('boardCardHtml: nota y propiedades (escapadas), botones de edición solo con meta', () => {
    const d = dev({note: 'no <tocar>', note_by: 'ana', note_at: '2026-10-06T11:55:00',
                   props: {estado: 'no-tocar', chip: 'esp32-s3', conectividad: ['lte']}});
    const html = EB.boardCardHtml(d, {now: NOW, catalog: CAT, meta: true});
    assert.match(html, /<article class="board st-avoid"/);
    assert.match(html, /class="status avoid">No tocar</);
    assert.match(html, /class="card-note"[^>]*>.*no &lt;tocar&gt;.*— ana · hace 5 min/);
    assert.match(html, /class="prop-chip prop-warn" data-filter="estado:no-tocar"[^>]*>estado: no tocar</);
    assert.match(html, /class="prop-chip" data-filter="chip:esp32-s3"/);
    assert.match(html, /class="prop-chip prop-unknown" data-filter="conectividad:lte"/);   // categoría fuera del catálogo
    assert.match(html, /data-act="props"[^>]*>◇</);
    assert.match(html, /data-act="note"[^>]*>✎</);
    assert.match(html, /<div class="meta-editor"><\/div>/);
    // bench-master: solo lectura
    const ro = EB.boardCardHtml(d, {now: NOW, catalog: CAT, bench: 'pi2'});
    assert.match(ro, /class="card-note"/);
    assert.match(ro, /data-filter="chip:esp32-s3"/);
    assert.doesNotMatch(ro, /data-act="(note|props)"/);
    assert.doesNotMatch(ro, /meta-editor/);
    // sin nota ni props: en el bench, solo los botones con texto; en el master, nada
    const empty = EB.boardCardHtml(dev({}), {now: NOW, meta: true});
    assert.match(empty, /data-act="props"[^>]*>◇ props</);
    assert.match(empty, /data-act="note"[^>]*>✎ nota</);
    assert.doesNotMatch(EB.boardCardHtml(dev({}), {now: NOW}), /card-meta/);
    // sin MAC: ni nota ni botones
    assert.doesNotMatch(EB.boardCardHtml(dev({mac: null, note: 'x'}), {now: NOW, meta: true}), /card-meta/);
});

test('areaChartSvg: puntos dentro del área y marcas', () => {
    const c = EB.areaChartSvg([0, 2, 1, 4], [3], 300, 100);
    assert.equal(c.points.length, 4);
    assert.ok(c.points.every(p => p[0] >= 0 && p[0] <= 300 && p[1] >= 0 && p[1] <= 100));
    assert.equal(c.points[0][0], 12);
    assert.match(c.svg, /<circle class="mark"/);
    assert.equal((c.svg.match(/<circle/g) || []).length, 1);
});
