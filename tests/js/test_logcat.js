// Filtro del log por categoría y nivel (monitor): EB.logCategorizer, logFilterPasses, logFilterSelection,
// logCatBarHtml, compactCount. La paridad con server/logcat.py está en tests/test_logcat_parity.py.
// node --test tests/js/test_logcat.js
const test = require('node:test');
const assert = require('node:assert/strict');
const EB = require('../../remote/dashboard/espbench.js');

const CATALOG = {categories: [
    {id: 'celular', label: 'Celular', tags: ['BG95'], lines: ['^Unregistered']},
    {id: 'controlador', label: 'Controlador', tags: ['MB_*', 'Controller*'], lines: []},
    {id: 'sistema', label: 'Sistema', tags: ['boot'], lines: ['^rst:0x', 'Guru Meditation Error']},
    {id: 'raro', label: 'Raro <b>', tags: [], lines: ['(']},          // regex inválida: se ignora
    {id: 'banco', label: 'Banco', tags: [], lines: [], builtin: true},
    {id: 'otros', label: 'Otros', tags: [], lines: [], builtin: true}
]};
const P = '2026-11-25 10:00:00.000 ';

function seq(lines) {
    const c = EB.logCategorizer(CATALOG);
    let prev = null;
    return lines.map(l => { prev = c.classify(l, prev); return [prev.cat, prev.lvl]; });
}

test('logCategorizer: tag, regex, panic/reset, herencia y líneas del server', () => {
    assert.deepEqual(seq([
        P + '> suelta',
        P + '> I (1) BG95 ./x.cpp:1 f(): estado',
        P + '> Unregistered',
        P + '↪ sigue',
        P + '> \x1b[0;31mE (2) MB_CONTROLLER_MASTER: falla\x1b[0m',
        P + '> I (3) Controller Config Danfoss: ok',
        P + '> Guru Meditation Error: Core  1 panic\'ed (LoadProhibited).',
        P + '> Backtrace: 0x1',
        P + '> rst:0xc (SW_CPU_RESET),boot:0x13',
        P + '| WARN  | protocol       | algo',
        P + '> I (4) Nuevo: x',
        'I (5) boot: sin prefijo'
    ]), [
        ['otros', null], ['celular', 'I'], ['celular', null], ['celular', null], ['controlador', 'E'],
        ['controlador', 'I'], ['sistema', 'E'], ['sistema', 'E'], ['sistema', 'W'], ['banco', 'W'],
        ['otros', 'I'], ['sistema', 'I']
    ]);
});

test('logCategorizer: el \\r se aplica como en el server (queda el último segmento)', () => {
    assert.deepEqual(seq([P + '> basura\rI (1) BG95: x', P + '> I (1) boot: y\r']), [['celular', 'I'], ['sistema', 'I']]);
});

test('logFilterPasses: categorías y niveles (I incluye D y V; sin nivel no pasa)', () => {
    assert.equal(EB.logFilterPasses(null, {celular: true}, null), true);       // sin catálogo: pasa
    assert.equal(EB.logFilterPasses({cat: 'celular', lvl: 'I'}, null, null), true);
    assert.equal(EB.logFilterPasses({cat: 'nube', lvl: 'E'}, {celular: true}, null), false);
    assert.equal(EB.logFilterPasses({cat: 'nube', lvl: 'D'}, null, {I: true}), true);
    assert.equal(EB.logFilterPasses({cat: 'nube', lvl: null}, null, {I: true}), false);
    assert.equal(EB.logFilterPasses({cat: 'nube', lvl: 'W'}, {nube: true}, {E: true, W: true}), true);
});

test('logFilterSelection: solo lo que existe en el catálogo; vacío = null', () => {
    const cats = EB.logCategorizer(CATALOG).categories;
    assert.deepEqual(EB.logFilterSelection({cats: ['celular', 'radio'], lvls: ['E', 'X']}, cats),
                     {cats: {celular: true}, lvls: {E: true}});
    assert.deepEqual(EB.logFilterSelection({cats: ['radio'], lvls: []}, cats), {cats: null, lvls: null});
    assert.deepEqual(EB.logFilterSelection(null, cats), {cats: null, lvls: null});
});

test('compactCount', () => {
    assert.equal(EB.compactCount(0), '0');
    assert.equal(EB.compactCount(999), '999');
    assert.equal(EB.compactCount(1234), '1,2k');
    assert.equal(EB.compactCount(9999), '9,9k');
    assert.equal(EB.compactCount(12345), '12k');
});

test('logCatBarHtml: un chip por categoría y por nivel, elegidos, vacíos y Limpiar', () => {
    const cats = CATALOG.categories;
    const counts = {cat: {celular: 1500, controlador: 3}, lvl: {E: 2}};
    const html = EB.logCatBarHtml(cats, counts, {celular: true}, null);
    assert.match(html, /data-cat="celular" aria-pressed="true"/);
    assert.match(html, /class="lc-chip on"/);
    assert.match(html, /data-cat="controlador" aria-pressed="false"/);
    assert.match(html, /data-n="cat:celular">1,5k</);
    assert.match(html, /class="lc-chip zero" data-cat="sistema"/);       // sin líneas: atenuado
    assert.match(html, /Raro &lt;b&gt;/);                                 // el label del bench se escapa
    assert.match(html, /data-lvl="E"[^>]*>Errores <span class="lc-n" data-n="lvl:E">2</);
    assert.match(html, /lc-lvl lc-lvl-w/);
    assert.match(html, /data-clear="1"/);
    assert.doesNotMatch(EB.logCatBarHtml(cats, counts, null, null), /data-clear/);
    assert.match(EB.logCatBarHtml(cats, null, null, {W: true}), /data-lvl="W" aria-pressed="true"/);
});
