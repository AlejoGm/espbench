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

test('cardState: caído, ocupado, sin MAC, salud', () => {
    assert.equal(EB.cardState({status: 'DOWN', state: 'monitoring'}), 'st-down');
    assert.equal(EB.cardState({status: 'RUNNING', state: 'flashing'}), 'st-busy');
    assert.equal(EB.cardState({status: 'RUNNING', state: 'discovering'}), 'st-busy');
    assert.equal(EB.cardState({status: 'RUNNING', state: 'unknown'}), 'st-unknown');
    assert.equal(EB.cardState({status: 'RUNNING', state: 'monitoring', health: {boots: 1, panics: 0}}), 'st-ok');
    assert.equal(EB.cardState({status: 'RUNNING', state: 'monitoring', health: {boots: 3, panics: 0, boot_loop: true}}), 'st-bad');
});

test('summarize: sin MAC solo cuenta en el total', () => {
    const c = EB.summarize([
        {status: 'RUNNING', state: 'monitoring', lock_user: 'ana'},
        {status: 'RUNNING', state: 'flashing'},
        {status: 'DOWN'},
        {status: 'RUNNING', state: 'unknown', lock_user: 'x'},
    ]);
    assert.deepEqual(c, {total: 4, ok: 1, busy: 1, bad: 0, down: 1, locked: 1});
});

test('stateBadgeHtml / firmwareHtml / lastFlashHtml', () => {
    assert.equal(EB.stateBadgeHtml({state: 'monitoring'}), '');
    assert.match(EB.stateBadgeHtml({state: 'erasing'}), /BORRANDO/);
    assert.equal(EB.firmwareHtml({}), '');
    assert.match(EB.firmwareHtml({fw_project: 'a<b', fw_version: 'v1', fw_idf: 'v5.3'}), /a&lt;b <span[^>]*>v1<\/span>.*IDF v5\.3/);
    assert.match(EB.lastFlashHtml({}), /nunca/);
    const now = new Date(2026, 9, 5, 16, 0, 0).getTime();
    assert.match(EB.lastFlashHtml({last_flash_ts: '2026-10-05T15:55:00', last_flash_ok: false, last_flash_user: 'ana'}, now),
                 /✗.*hace 5 min.*ana/);
});

test('basePath / wsUrl: directo y a través de bench-master', () => {
    assert.equal(EB.basePath('/'), '/');
    assert.equal(EB.basePath('/device.html'), '/');
    assert.equal(EB.basePath('/bench/sensipi02/device.html'), '/bench/sensipi02/');
    assert.equal(EB.basePath('/bench/sensipi02/'), '/bench/sensipi02/');
    assert.equal(EB.wsUrl({protocol: 'http:', host: 'pi:8080', pathname: '/device.html'}, 'ws/device/esp-slot1'),
                 'ws://pi:8080/ws/device/esp-slot1');
    assert.equal(EB.wsUrl({protocol: 'https:', host: 'localhost:8090', pathname: '/bench/b1/device.html'}, 'ws/device/x'),
                 'wss://localhost:8090/bench/b1/ws/device/x');
});
