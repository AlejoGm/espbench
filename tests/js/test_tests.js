// Corridas de `espbench test` en el dashboard: card, barra, eventos `test`, estado. node --test tests/js/test_tests.js
const test = require('node:test');
const assert = require('node:assert/strict');
const EB = require('../../remote/dashboard/espbench.js');

const NOW = new Date(2026, 9, 26, 12, 0, 0).getTime();
const S = NOW / 1000;
const run = (over) => Object.assign({run_id: 'r1', status: 'running', total: 16, done: 7, passed: 6, failed: 1,
                                     counts: {pass: 6, fail: 1}, current: {index: 8, number: 9, name: 'corte de luz'},
                                     started: S - 120, updated: S - 2, ended: null, user: 'alejo', project: 'nvc3_test'}, over);
const dev = (over) => Object.assign({tty_name: 'ttyUSB0', status: 'RUNNING', state: 'monitoring', mac: 'AA:BB',
                                     health: {panics: 2, boot_loop: true, last_panic: {ts: '2026-10-26T11:59:00'}}}, over);

test('testView: corriendo, con el test en curso y lo que falló', () => {
    const v = EB.testView(run(), NOW);
    assert.equal(v.cls, 'run');
    assert.equal(v.text, 'Tests 7/16 · 1 ✗');
    assert.equal(v.sub, '▶ (9) corte de luz');
    assert.deepEqual(v.segs, [{cls: 'pass', n: 6}, {cls: 'fail', n: 1}, {cls: 'cur', n: 1}]);
    assert.match(v.title, /6 pasaron, 1 fallaron/);
});

test('testView: terminada (ok, con fallas), cortada y sin noticias; vieja no se muestra', () => {
    const ok = EB.testView(run({status: 'done', done: 16, passed: 16, failed: 0, counts: {pass: 16}, ended: S - 300}), NOW);
    assert.deepEqual([ok.cls, ok.text, ok.sub], ['ok', '16/16 pasaron', 'hace 5 min']);
    const bad = EB.testView(run({status: 'done', done: 16, passed: 14, failed: 2, ended: S - 60}), NOW);
    assert.deepEqual([bad.cls, bad.text], ['bad', '2 de 16 fallaron']);
    assert.equal(EB.testView(run({status: 'aborted', ended: S - 60}), NOW).text, 'Corrida cortada 7/16');
    assert.equal(EB.testView(run({status: 'interrupted'}), NOW).cls, 'warn');
    const old = run({status: 'done', ended: S - 25 * 3600});
    assert.equal(EB.testView(old, NOW), null);
    assert.ok(EB.testView(old, NOW, true));                 // la pestaña Tests la muestra igual
    assert.equal(EB.testView(null, NOW), null);
});

test('testBarHtml: un segmento por resultado, en proporción al total', () => {
    const html = EB.testBarHtml(EB.testView(run(), NOW));
    assert.match(html, /aria-valuenow="7"/);
    assert.match(html, /tb-pass" style="width:37.50%"/);
    assert.match(html, /tb-fail" style="width:6.25%"/);
    assert.match(html, /tb-cur/);
    assert.equal(EB.testBarHtml(null), '');
});

test('card: el bloque de tests va con la corrida y escapa el nombre', () => {
    const html = EB.boardCardHtml(dev({test: run({current: {index: 1, number: 1, name: '<b>x</b>'}})}), {now: NOW});
    assert.match(html, /class="tests t-run"/);
    assert.match(html, /&lt;b&gt;x&lt;\/b&gt;/);
    assert.doesNotMatch(EB.boardCardHtml(dev({}), {now: NOW}), /class="tests/);
});

test('boardStatus: corriendo tests le gana al boot loop y a los panics; terminada, el ACK de la corrida', () => {
    assert.deepEqual(EB.boardStatus(dev({test: run()}), NOW), {cls: 'test', text: 'Testeando'});
    // Terminó hace poco: el registry cubre el boot loop y el panic de la corrida
    const after = dev({test: run({status: 'done'}), acked: {panic: true, reset: true, boot_loop: true}});
    assert.deepEqual(EB.boardStatus(after, NOW), {cls: 'ok', text: 'En línea'});
    assert.equal(EB.boardStatus(dev({acked: {panic: true, boot_loop: false}}), NOW).text, 'Boot loop');
});

test('eventos test: ícono, detalle y bad por fase', () => {
    const v = (d) => EB.eventView({type: 'test', ts: '2026-10-26T12:00:00.000', cursor: 'c:20261026_120000_1:10', detail: d});
    assert.deepEqual([v({phase: 'start', total: 5, project: 'p'}).icon, v({phase: 'start', total: 5, project: 'p'}).detail], ['⚗', '5 tests · p']);
    assert.equal(v({phase: 'case', case: {number: 3, name: 'a'}}).detail, '▶ (3) a');
    const fail = v({phase: 'result', case: {number: 3, name: 'a', result: 'fail', duration_s: 1.25, message: 'Expected 1'}});
    assert.deepEqual([fail.icon, fail.detail, fail.bad], ['✗', 'falló (3) a · 1,3 s · Expected 1', true]);
    assert.equal(v({phase: 'result', case: {number: 3, name: 'a', result: 'pass', duration_s: 2}}).bad, false);
    assert.deepEqual([v({phase: 'end', status: 'done', total: 5, passed: 5, failed: 0}).detail,
                      v({phase: 'end', status: 'done', total: 5, failed: 2}).bad], ['5/5 pasaron', true]);
    assert.equal(v({phase: 'end', status: 'aborted', done: 2, total: 5, message: 'KeyboardInterrupt'}).detail,
                 'cortada · KeyboardInterrupt · 2/5');
});

test('barras de actividad: una hora con tests es test (no reinicio), y el tooltip lo cuenta', () => {
    const b = {boot: 0, panic: 0, flash: 0, boot_loop: 0, reserve: 0, test: 5, test_fail: 1, test_boot: 3, test_panic: 1};
    const html = EB.activityBarsHtml([b]);
    assert.match(html, /class="test tfail"/);
    assert.match(html, /5 tests \(1 fallaron\), en los tests: 3 reinicios, 1 panics/);
    // Un panic de verdad en la misma hora gana
    assert.match(EB.activityBarsHtml([Object.assign({}, b, {panic: 1})]), /class="panic/);
});

test('EventMarks: el inicio de un test va en la primera línea desde el aviso; el resultado, en su línea', () => {
    const m = new EB.EventMarks();
    m.add([{type: 'test', ts: '2026-10-26T12:00:00.050', cursor: 'c:S:1', detail: {phase: 'case', case: {number: 5, name: 'x'}}},
           {type: 'test', ts: '2026-10-26T12:00:00.040', cursor: 'c:S:1', detail: {phase: 'result', case: {result: 'pass'}}}]);
    const prev = m.feed('prev', '2026-10-26T12:00:00.000', 'test_x.cpp:42:anterior:PASS');
    // el resultado mantiene el margen de 150 ms: va en la línea de Unity que lo imprimió
    assert.deepEqual(prev.map(x => x.ev.detail.phase), ['result']);
    assert.deepEqual(m.feed('next', '2026-10-26T12:00:00.060', 'here is the menu').map(x => [x.ev.type, x.handle]),
                     [['test', 'next']]);
});
