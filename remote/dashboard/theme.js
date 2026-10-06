/*
 * theme.js — tema claro/oscuro de todas las páginas (bench, monitor y bench-master).
 * Va en el <head> antes del CSS: pone <html data-theme> antes de pintar (sin parpadeo).
 * Lo elegido se guarda en localStorage (eb.theme); si no hay nada, sigue al sistema.
 * Cualquier elemento con [data-theme-toggle] lo alterna (su <i> muestra sol o luna).
 */
(function () {
    'use strict';
    var KEY = 'eb.theme';
    var root = document.documentElement;

    function saved() {
        try { var t = localStorage.getItem(KEY); return t === 'light' || t === 'dark' ? t : null; } catch (e) { return null; }
    }
    function system() {
        return window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    }
    function sync() {
        var dark = root.dataset.theme === 'dark';
        document.querySelectorAll('[data-theme-toggle]').forEach(function (b) {
            var i = b.querySelector('i');
            if (i) i.className = dark ? 'ti ti-sun' : 'ti ti-moon';
            b.setAttribute('aria-label', dark ? 'Usar tema claro' : 'Usar tema oscuro');
        });
    }
    function toggle() {
        root.dataset.theme = root.dataset.theme === 'dark' ? 'light' : 'dark';
        try { localStorage.setItem(KEY, root.dataset.theme); } catch (e) {}
        sync();
    }

    root.dataset.theme = saved() || system();
    document.addEventListener('click', function (e) {
        if (e.target.closest && e.target.closest('[data-theme-toggle]')) toggle();
    });
    document.addEventListener('DOMContentLoaded', sync);
    window.EBTheme = {toggle: toggle, sync: sync};
})();
