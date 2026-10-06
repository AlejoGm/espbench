// node --test tests/js/test_espbench.js  (también lo corre pytest: tests/test_dashboard_js.py)
const test = require('node:test');
const assert = require('node:assert/strict');
const EB = require('../../remote/dashboard/espbench.js');

test('relTime: local sin zona', () => {
    const now = new Date(2026, 9, 5, 16, 0, 0).getTime();
    assert.equal(EB.relTime('2026-10-05T15:59:50', now), 'recién');
    assert.equal(EB.relTime('2026-10-05T15:55:00', now), 'hace 5 min');
    assert.equal(EB.relTime('2026-10-05T13:00:00', now), 'hace 3 h');
    assert.equal(EB.relTime('2026-10-03T16:00:00', now), 'hace 2 d');
    assert.equal(EB.relTime('2026-01-01T00:00:00', now), '2026-01-01');
    assert.equal(EB.relTime(null, now), null);
});

test('fmtBytes', () => {
    assert.equal(EB.fmtBytes(512), '512 B');
    assert.equal(EB.fmtBytes(2048), '2.0 KB');
    assert.equal(EB.fmtBytes(3 * 1024 * 1024), '3.0 MB');
});

test('healthBadges: sano no muestra nada', () => {
    assert.deepEqual(EB.healthBadges({boots: 1, panics: 0, boot_loop: false, last_reset: {reason: 'POWERON_RESET', abnormal: false}}), []);
    assert.deepEqual(EB.healthBadges(null), []);
    assert.equal(EB.healthLevel({boots: 1, panics: 0}), 'ok');
});

test('healthBadges: panic + reinicios', () => {
    const b = EB.healthBadges({boots: 3, panics: 1, boot_loop: false,
        last_panic: {kind: 'guru', detail: 'LoadProhibited', ts: '2026-10-05T16:00:00'},
        last_reset: {reason: 'SW_CPU_RESET', abnormal: false}});
    assert.deepEqual(b.map(x => x.cls), ['hb-panic', 'hb-info']);
    assert.equal(b[0].text, '⚠ 1 panic');
    assert.match(b[0].title, /Guru Meditation \(LoadProhibited\)/);
    assert.equal(b[1].text, '↻ 2');
    assert.equal(EB.healthLevel({panics: 1}), 'bad');
});

test('healthBadges: boot loop tapa reset y contador', () => {
    const b = EB.healthBadges({boots: 5, panics: 0, boot_loop: true,
        last_reset: {reason: 'TG1WDT_SYS_RESET', abnormal: true}});
    assert.deepEqual(b.map(x => x.cls), ['hb-loop']);
});

test('healthBadges: reset anormal sin panic = warn', () => {
    const h = {boots: 1, panics: 0, last_reset: {reason: 'RTCWDT_BROWN_OUT_RESET', abnormal: true}};
    assert.deepEqual(EB.healthBadges(h).map(x => x.cls), ['hb-warn']);
    assert.equal(EB.healthLevel(h), 'warn');
});

test('lineClass', () => {
    assert.equal(EB.lineClass('2026-10-05 16:00:00 | WARN  | protocol       | algo'), 'ln-tl ln-tl-warn');
    assert.equal(EB.lineClass('2026-10-05 16:00:00 | ERROR | flash          | x'), 'ln-tl ln-tl-error');
    assert.equal(EB.lineClass("Guru Meditation Error: Core  0 panic'ed (LoadProhibited)."), 'ln-panic');
    assert.equal(EB.lineClass('Backtrace: 0x400d1234:0x3ffb0000'), 'ln-panic');
    assert.equal(EB.lineClass('rst:0x8 (TG1WDT_SYS_RESET),boot:0x13'), 'ln-panic');
    assert.equal(EB.lineClass('rst:0x1 (POWERON_RESET),boot:0x13'), 'ln-reset');
    assert.equal(EB.lineClass('I (123) app: hola'), '');
    assert.equal(EB.lineClass('E (123) wifi: fallo'), 'ln-esp-e');
    assert.equal(EB.lineClass('W (123) wifi: ojo'), 'ln-esp-w');
    assert.ok(EB.isProblem('ln-tl ln-tl-error') && EB.isProblem('ln-esp-w') && EB.isProblem('ln-panic'));
    assert.ok(!EB.isProblem('ln-tl ln-tl-info') && !EB.isProblem(''));
});

test('ansiLineToHtml: colores, escape y secuencias no-SGR', () => {
    assert.equal(EB.ansiLineToHtml('\x1b[0;32mI (1) x: <ok>\x1b[0m'),
                 '<span style="color:#3fb950;">I (1) x: &lt;ok&gt;</span>');
    assert.equal(EB.ansiLineToHtml('a\x1b[Kb'), 'ab');
});

test('LineBuffer: chunks partidos, CRLF partido y overwrite', () => {
    const lb = new EB.LineBuffer();
    assert.deepEqual(lb.push('hola mu'), []);
    assert.equal(lb.partialView(), 'hola mu');
    assert.deepEqual(lb.push('ndo\r'), []);
    assert.deepEqual(lb.push('\nsegunda\r\r\n'), ['hola mundo', 'segunda']);
    assert.deepEqual(lb.push('10%\r50%\r100%\n'), ['100%']);
    assert.deepEqual(lb.push('progreso 10%\r'), []);
    assert.equal(lb.partialView(), 'progreso 10%');
    assert.deepEqual(lb.push('progreso 90%\n'), ['progreso 90%']);
});

// ── Prefijo del DeviceLog ("YYYY-MM-DD HH:MM:SS.mmm <origen> ") ─────────

test('splitPrefix: serial, taglog, continuación y línea vieja', () => {
    assert.deepEqual(EB.splitPrefix('2026-10-05 16:02:03.123 > I (1) app: hola'),
        {date: '2026-10-05', time: '16:02:03.123', origin: '>', prefix: '2026-10-05 16:02:03.123 > ',
         body: 'I (1) app: hola'});
    assert.equal(EB.splitPrefix('2026-10-05 16:02:03.123 | INFO  | x | y').origin, '|');
    assert.equal(EB.splitPrefix('2026-10-05 16:02:03.123 ↪ resto').body, 'resto');
    const old = EB.splitPrefix('I (1) app: sin prefijo');
    assert.equal(old.time, null);
    assert.equal(old.body, 'I (1) app: sin prefijo');
    // taglog viejo: tiene hora pero no milisegundos ni origen → no es prefijo
    assert.equal(EB.splitPrefix('2026-10-05 16:00:00 | WARN  | protocol       | algo').origin, null);
});

test('lineClass con prefijo: las regex ancladas miran el cuerpo', () => {
    const P = '2026-10-05 16:02:03.123 ';
    assert.equal(EB.lineClass(P + '| WARN  | protocol       | algo'), 'ln-tl ln-tl-warn');
    assert.equal(EB.lineClass(P + '| ERROR | flash          | x'), 'ln-tl ln-tl-error');
    assert.equal(EB.lineClass(P + '| INFO  | device         | a -> b'), 'ln-tl ln-tl-info');
    assert.equal(EB.lineClass(P + '> rst:0x1 (POWERON_RESET),boot:0x13'), 'ln-reset');
    assert.equal(EB.lineClass(P + '> rst:0x8 (TG1WDT_SYS_RESET),boot:0x13'), 'ln-panic');
    assert.equal(EB.lineClass(P + '> Backtrace: 0x400d1234:0x3ffb0000'), 'ln-panic');
    assert.equal(EB.lineClass(P + '> E (123) wifi: fallo'), 'ln-esp-e');
    assert.equal(EB.lineClass(P + '↪ W (123) wifi: ojo'), 'ln-esp-w');
    assert.equal(EB.lineClass(P + '> I (123) app: hola'), '');
    // una línea serial que parece taglog no es taglog
    assert.equal(EB.lineClass(P + '> INFO  | x | y'), '');
});

test('overwrite conserva el prefijo', () => {
    assert.equal(EB.overwrite('2026-10-05 16:02:03.123 > 10%\r50%\r100%'), '2026-10-05 16:02:03.123 > 100%');
    assert.equal(EB.overwrite('10%\r100%'), '100%');
    assert.equal(EB.overwrite('2026-10-05 16:02:03.123 > sin cr'), '2026-10-05 16:02:03.123 > sin cr');
});

test('LineBuffer con líneas prefijadas', () => {
    const lb = new EB.LineBuffer();
    const out = lb.push('2026-10-05 16:02:03.123 > a 10%\r90%\n2026-10-05 16:02:03.200 | INFO  | x              | y\n');
    assert.deepEqual(out, ['2026-10-05 16:02:03.123 > 90%', '2026-10-05 16:02:03.200 | INFO  | x              | y']);
});

test('sesiones: el nombre es el inicio (con pid); los viejos no', () => {
    assert.equal(EB.sessionStart('output_20261005_160203_812.log'), '2026-10-05T16:02:03');
    assert.equal(EB.sessionStart('output_20261005_160203.log'), null);   // log viejo: hora de rotación
    assert.equal(EB.sessionStart('output.log'), null);
    const epoch = new Date(2026, 9, 5, 16, 2, 3).getTime() / 1000;
    assert.equal(EB.isoLocal(epoch), '2026-10-05T16:02:03');
});
