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

test('uptimeText y chip "Encendida" en la card (sin número grande)', () => {
    assert.equal(EB.uptimeText(['2', 'h', '13', 'min']), '2\u00a0h 13\u00a0min');
    assert.equal(EB.uptimeText(null), null);
    const html = EB.boardCardHtml(dev({}), {now: NOW});
    assert.match(html, /<i class="ti ti-clock" aria-label="Encendida"><\/i><b>2\u00a0h 13\u00a0min<\/b>/);
    assert.doesNotMatch(html, /class="uptime"|class="big"/);
    assert.doesNotMatch(EB.boardCardHtml(dev({state: 'flashing'}), {now: NOW}), /ti-clock/);
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
    assert.match(html, /aria-label="Encendida"><\/i><b>2\u00a0h 13\u00a0min<\/b>/);
    assert.match(html, /data-act="rename"/);
    assert.match(html, /data-act="copy"/);
    assert.match(html, /href="device.html\?tty=ttyUSB0"/);
    assert.match(html, /class="lock"[^>]*><i class="ti ti-lock"><\/i>ana</);
    assert.match(html, /Último log <b>2 s<\/b>/);
    const m = EB.boardCardHtml(dev({}), {now: NOW, bench: 'pi2', href: 'bench/pi2/device.html?tty=ttyUSB0', direct: 'http://x:8080/device.html'});
    assert.match(m, /<div class="meta">SFY1 · ttyUSB0<\/div>/);              // el bench no va mezclado con el modelo
    assert.match(m, /<div class="b-ctx"><span class="bench-tag"[^>]*><i class="ti ti-server-2"[^>]*><\/i><b>pi2<\/b><\/span>/);
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
    assert.match(html, /class="prop-chip pc-estado prop-warn" data-filter="estado:no-tocar"[^>]*><i class="ti ti-alert-triangle"[^>]*><\/i>no tocar</);
    assert.match(html, /class="prop-chip pc-spec pc-chip" data-filter="chip:esp32-s3"[^>]*><i class="ti ti-cpu"/);
    assert.match(html, /class="prop-chip pc-spec prop-unknown" data-filter="conectividad:lte"/);   // categoría fuera del catálogo
    assert.match(html, /data-act="props"[^>]*><i class="ti ti-adjustments-horizontal">/);
    assert.match(html, /class="note-edit" data-act="note"/);
    assert.doesNotMatch(html, /data-act="note"[^>]*><i class="ti ti-note">/);           // con nota, el lápiz va en la nota
    assert.match(html, /<div class="meta-editor"><\/div>/);
    // bench-master: solo lectura
    const ro = EB.boardCardHtml(d, {now: NOW, catalog: CAT, bench: 'pi2'});
    assert.match(ro, /class="card-note"/);
    assert.match(ro, /data-filter="chip:esp32-s3"/);
    assert.doesNotMatch(ro, /data-act="(note|props)"/);
    assert.doesNotMatch(ro, /meta-editor/);
    // sin nota ni props: en el bench, los botones para agregar; en el master, nada (sin huecos)
    const empty = EB.boardCardHtml(dev({}), {now: NOW, meta: true});
    assert.match(empty, /data-act="props"[^>]*><i class="ti ti-plus"><\/i>chip, conectividad…</);
    assert.match(empty, /data-act="note"[^>]*><i class="ti ti-note"><\/i>Nota</);
    assert.doesNotMatch(EB.boardCardHtml(dev({}), {now: NOW}), /card-meta|prop-row/);
    // sin MAC: ni nota ni botones
    assert.doesNotMatch(EB.boardCardHtml(dev({mac: null, note: 'x'}), {now: NOW, meta: true}), /card-meta/);
});

const CAT2 = [{id: 'estado', label: 'estado', multi: false, values: [
                  {id: 'no-tocar', label: 'no tocar', warn: true, exclude_pick: true}, {id: 'testeando', label: 'testeando'}]},
              {id: 'uso', label: 'uso', multi: true, values: [{id: 'agentes', label: 'agentes'}, {id: 'ci', label: 'CI'}]},
              {id: 'chip', label: 'chip', multi: false, values: [{id: 'esp32', label: 'ESP32'}, {id: 'esp32-s3', label: 'ESP32-S3'},
                                                                  {id: 'esp32-c3', label: 'ESP32-C3'}]},
              {id: 'conectividad', label: 'conectividad', multi: true, values: [{id: 'wifi', label: 'WiFi'}, {id: 'lte', label: 'LTE'},
                                                                                  {id: 'ble', label: 'BLE'}]}];

test('cardProps: specs, etiquetas y estado, con íconos', () => {
    const cp = EB.cardProps({estado: 'testeando', uso: ['ci'], chip: 'esp32-s3', conectividad: ['lte', 'wifi', 'nb-iot']}, CAT2);
    assert.deepEqual(cp.spec.map(p => [p.label, p.icon]), [['ESP32-S3', 'cpu'], ['WiFi', 'wifi'], ['LTE', 'antenna-bars-5'],
                                                           ['nb-iot', 'antenna']]);
    assert.deepEqual(cp.tag.map(p => p.label), ['CI']);
    assert.deepEqual(cp.estado.map(p => [p.label, p.warn, p.icon]), [['testeando', false, 'flag']]);
    assert.equal(EB.cardProps({estado: 'no-tocar'}, CAT2).estado[0].icon, 'alert-triangle');
    assert.deepEqual(EB.cardProps({otra: 'x'}, CAT2).tag.map(p => p.role), ['tag']);    // categoría nueva: etiqueta
});

test('boardMetaHtml: orden de la fila (estado que excluye, specs, etiquetas, estado común)', () => {
    const d = dev({props: {estado: 'testeando', uso: ['agentes'], chip: 'esp32', conectividad: ['wifi']}});
    const html = EB.boardMetaHtml(d, CAT2, false, NOW);
    assert.deepEqual([...html.matchAll(/data-filter="([^"]+)"/g)].map(m => m[1]),
                     ['chip:esp32', 'conectividad:wifi', 'uso:agentes', 'estado:testeando']);
    const warn = EB.boardMetaHtml(dev({props: {estado: 'no-tocar', chip: 'esp32'}}), CAT2, false, NOW);
    assert.deepEqual([...warn.matchAll(/data-filter="([^"]+)"/g)].map(m => m[1]), ['estado:no-tocar', 'chip:esp32']);
});

test('benchTagHtml y la card: bench aparte, con ubicación; benchTag:false la saca', () => {
    assert.match(EB.benchTagHtml('pi<2>', 'Santiago, CL'), /<b>pi&lt;2&gt;<\/b><span class="loc"><i class="ti ti-map-pin"[^>]*><\/i>Santiago, CL<\/span>/);
    assert.match(EB.benchTagHtml('pi2', null, false), /class="bench-tag off"[^>]*><i class="ti ti-server-off"/);
    const c = EB.boardCardHtml(dev({}), {now: NOW, bench: 'pi2', location: 'Santiago, CL'});
    assert.match(c, /Santiago, CL/);
    assert.match(c, /data-bench="pi2"/);
    const nt = EB.boardCardHtml(dev({}), {now: NOW, bench: 'pi2', location: 'Santiago, CL', benchTag: false});
    assert.doesNotMatch(nt, /bench-tag/);
    assert.match(nt, /data-bench="pi2"/);                                   // el click de copiar sigue sabiendo el bench
    assert.doesNotMatch(EB.boardCardHtml(dev({}), {now: NOW}), /bench-tag/);   // dashboard de un bench: sin etiqueta
});

test('groupOptions / pickGroup: la URL gana, lo que no se ofrece = sin agrupar', () => {
    assert.deepEqual(EB.groupOptions(true).map(o => o[0]), ['', 'bench', 'location', 'chip', 'conectividad', 'uso', 'estado']);
    assert.deepEqual(EB.groupOptions(false).map(o => o[0]), ['', 'chip', 'conectividad', 'uso', 'estado']);
    assert.equal(EB.pickGroup('chip', 'bench', true), 'chip');
    assert.equal(EB.pickGroup(null, 'bench', true), 'bench');
    assert.equal(EB.pickGroup(null, 'bench', false), '');                  // el bench no agrupa por bench
    assert.equal(EB.pickGroup('', 'chip', true), '');                       // ?group= vacío: sin agrupar
    assert.equal(EB.pickGroup('<x>', 'uso', true), 'uso');
    assert.equal(EB.pickGroup(undefined, null, true), '');
});

test('groupBoards: por propiedad (multi en cada grupo, orden del catálogo, sin valor al final) con conteos', () => {
    const a = dev({tty_name: 'a', props: {chip: 'esp32-s3', conectividad: ['wifi', 'lte']}});
    const b = dev({tty_name: 'b', props: {chip: 'esp32', conectividad: ['lte']}, health: {panics: 1}});
    const c = dev({tty_name: 'c', props: {chip: 'esp32-s3'}, lock_user: 'ana'});
    const d = dev({tty_name: 'd', props: {chip: 'esp32-h2', estado: 'no-tocar'}});
    const e = dev({tty_name: 'e'});
    const g = EB.groupBoards([a, b, c, d, e], 'conectividad', CAT2, NOW);
    assert.deepEqual(g.map(x => [x.key, x.label, x.items.map(i => i.tty_name)]),
                     [['wifi', 'WiFi', ['a']], ['lte', 'LTE', ['a', 'b']], ['', 'Sin conectividad', ['c', 'd', 'e']]]);
    assert.deepEqual(g.map(x => [x.count, x.free, x.problems]), [[1, 1, 0], [2, 1, 1], [3, 1, 0]]);   // c reservada, d no tocar
    assert.equal(g[2].empty, true);
    const chips = EB.groupBoards([a, b, c, d, e], 'chip', CAT2, NOW);
    assert.deepEqual(chips.map(x => x.label), ['ESP32', 'ESP32-S3', 'esp32-h2', 'Sin chip']);   // fuera del catálogo, después
    assert.deepEqual(EB.groupBoards([a, b], '', CAT2, NOW).map(x => x.items.length), [2]);
    assert.deepEqual(EB.groupBoards([], 'chip', CAT2, NOW), []);
    assert.deepEqual(EB.groupBoards([], '', CAT2, NOW), []);
});

test('groupBoards: por bench y por ubicación (sin distinguir mayúsculas), con la ubicación y los benches', () => {
    const x = (bench, loc, tty) => dev({tty_name: tty, bench: bench, bench_location: loc});
    const list = [x('pi2', 'Santiago, CL', '1'), x('pi1', 'Buenos Aires, AR', '2'), x('pi3', 'santiago, CL', '3'), x('pi4', null, '4')];
    const byBench = EB.groupBoards(list, 'bench', null, NOW);
    assert.deepEqual(byBench.map(g => [g.label, g.location]), [['pi1', 'Buenos Aires, AR'], ['pi2', 'Santiago, CL'],
                                                              ['pi3', 'santiago, CL'], ['pi4', null]]);
    const byLoc = EB.groupBoards(list, 'location', null, NOW);
    assert.deepEqual(byLoc.map(g => [g.label, g.benches]), [['Buenos Aires, AR', ['pi1']], ['Santiago, CL', ['pi2', 'pi3']],
                                                           ['Sin ubicación', ['pi4']]]);
});

test('groupHeaderHtml: ícono, nombre, cantidad, libres y con problemas (escapado)', () => {
    const h = EB.groupHeaderHtml({key: 'lte', label: 'LTE<', empty: false, count: 2, free: 1, problems: 1, benches: [], location: null}, 'conectividad');
    assert.match(h, /class="group-h" data-group="lte"/);
    assert.match(h, /ti-antenna-bars-5/);
    assert.match(h, /<h3>LTE&lt;<\/h3>/);
    assert.match(h, /2 placas<\/span><span class="gh-stat ok">1 libre<\/span><span class="gh-stat bad">1 con problemas/);
    const b = EB.groupHeaderHtml({key: 'pi1', label: 'pi1', count: 1, free: 0, problems: 0, benches: ['pi1'], location: 'Santiago, CL'}, 'bench');
    assert.match(b, /ti-server-2/);
    assert.match(b, /class="gh-sub"><i class="ti ti-map-pin"[^>]*><\/i>Santiago, CL/);
    assert.doesNotMatch(b, /libre|problemas/);
    const l = EB.groupHeaderHtml({key: '', label: 'Sin ubicación', empty: true, count: 1, free: 1, problems: 0, benches: ['a', 'b'], location: null}, 'location');
    assert.match(l, /class="group-h empty"/);
    assert.match(l, /class="gh-sub">a, b</);
});

test('locationCheck: la regla del server (60, sin control ni Cf)', () => {
    assert.deepEqual(EB.locationCheck('  Lab Chile '), {ok: true, text: 'Lab Chile', error: null});
    assert.equal(EB.locationCheck('x'.repeat(60)).ok, true);
    assert.equal(EB.locationCheck('x'.repeat(61)).ok, false);
    assert.equal(EB.locationCheck('a\nb').ok, false);
    assert.equal(EB.locationCheck('a‮b').ok, false);
    assert.equal(EB.locationCheck('a​b').ok, false);
    assert.equal(EB.locationCheck(null).text, '');
});

test('locationView: automática (con detalle y vieja) o manual; texto suelto de un bench 0.42', () => {
    const auto = EB.locationView({label: 'Santiago, CL', city: 'Santiago', region: 'Santiago Metropolitan', country: 'CL',
                                  tz: 'America/Santiago', source: 'auto', ts: '2026-10-06T11:00:00', stale: false}, NOW);
    assert.equal(auto.icon, 'current-location');
    assert.equal(auto.source, 'auto');
    assert.match(auto.title, /IP pública: Santiago, Santiago Metropolitan, CL \(America\/Santiago\), hace 1 h/);
    const stale = EB.locationView({label: 'Santiago, CL', source: 'auto', ts: '2026-10-04T12:00:00', stale: true}, NOW);
    assert.equal(stale.stale, true);
    assert.match(stale.title, /No se pudo actualizar: es de hace 2 d/);
    assert.deepEqual(EB.locationView('Lab Chile'), {label: 'Lab Chile', source: 'manual', stale: false, icon: 'map-pin',
                                                    title: 'Ubicación fijada a mano (pisa la automática)'});
    assert.equal(EB.locationView(null), null);
    assert.equal(EB.locationView({label: ''}), null);
});

test('areaChartSvg: puntos dentro del área y marcas', () => {
    const c = EB.areaChartSvg([0, 2, 1, 4], [3], 300, 100);
    assert.equal(c.points.length, 4);
    assert.ok(c.points.every(p => p[0] >= 0 && p[0] <= 300 && p[1] >= 0 && p[1] <= 100));
    assert.equal(c.points[0][0], 12);
    assert.match(c.svg, /<circle class="mark"/);
    assert.equal((c.svg.match(/<circle/g) || []).length, 1);
});
