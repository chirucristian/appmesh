/* Shared line-icon set for the AppMesh redesign mockups.
   Monochrome, 1.6px stroke, currentColor — drop-in replacements for the
   emoji used in the current console. Each is a 20x20 viewBox <svg> string. */
(function () {
  const w = (p, opts = '') =>
    `<svg class="ic" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" ${opts}>${p}</svg>`;

  window.IC = {
    // resources
    shield:   w('<path d="M10 2.5l5.5 2v4c0 3.6-2.3 6.4-5.5 7.5C6.8 14.9 4.5 12.1 4.5 8.5v-4z"/>'),
    key:      w('<circle cx="7" cy="9" r="3"/><path d="M9.5 10.5L16 17M14 15l1.5-1.5M12.5 13.5L14 12"/>'),
    idcard:   w('<rect x="2.5" y="4.5" width="15" height="11" rx="1.5"/><circle cx="7" cy="9.5" r="1.8"/><path d="M11 8.5h4M11 11.5h3"/>'),
    box:      w('<path d="M10 2.7l6 3.2v8.2l-6 3.2-6-3.2V5.9z"/><path d="M4.2 6L10 9.2 15.8 6M10 9.2V16.5"/>'),
    doc:      w('<path d="M5 2.5h6l4 4v11H5z"/><path d="M11 2.5v4h4M7.5 11h5M7.5 13.5h5"/>'),
    target:   w('<circle cx="10" cy="10" r="6.5"/><circle cx="10" cy="10" r="3"/><circle cx="10" cy="10" r="0.6" fill="currentColor"/>'),
    balance:  w('<path d="M10 3.5v13M4 7h12"/><path d="M4 7l-2 4.5h4zM16 7l-2 4.5h4z"/><path d="M2 11.5a2 2 0 004 0M14 11.5a2 2 0 004 0M7 16.5h6"/>'),
    waves:    w('<path d="M5 7v6M8 4.5v11M11 7v6M14 9v2M17 7v6"/>'),
    activity: w('<path d="M2.5 10.5h3l2-5 3 9 2-6 1.5 2h3.5"/>'),
    globe:    w('<circle cx="10" cy="10" r="7"/><path d="M3 10h14M10 3c2 2.2 2 11.8 0 14M10 3c-2 2.2-2 11.8 0 14"/>'),
    // ui
    server:   w('<rect x="3" y="3.5" width="14" height="5" rx="1"/><rect x="3" y="11.5" width="14" height="5" rx="1"/><path d="M6 6h0.01M6 14h0.01"/>'),
    cpu:      w('<rect x="5.5" y="5.5" width="9" height="9" rx="1.2"/><path d="M8 2.5v3M12 2.5v3M8 14.5v3M12 14.5v3M2.5 8h3M2.5 12h3M14.5 8h3M14.5 12h3"/>'),
    refresh:  w('<path d="M16 5.5A7 7 0 003.5 8M4 4v3.5h3.5M4 14.5A7 7 0 0016.5 12M16 16v-3.5h-3.5"/>'),
    external: w('<path d="M11 3h6v6M17 3l-8 8M14 11.5V16a1 1 0 01-1 1H4a1 1 0 01-1-1V7a1 1 0 011-1h4.5"/>'),
    copy:     w('<rect x="6.5" y="6.5" width="10" height="10" rx="1.5"/><path d="M13 6.5V4.5a1 1 0 00-1-1H4.5a1 1 0 00-1 1V12a1 1 0 001 1h2"/>'),
    chevron:  w('<path d="M5 7.5l5 5 5-5"/>'),
    chevronR: w('<path d="M7.5 5l5 5-5 5"/>'),
    arrowUp:  w('<path d="M10 16V4M5 9l5-5 5 5"/>'),
    check:    w('<path d="M4 10.5l3.5 3.5L16 6"/>'),
    dot:      w('<circle cx="10" cy="10" r="3.5" fill="currentColor" stroke="none"/>'),
    plus:     w('<path d="M10 4v12M4 10h12"/>'),
    arrowL:   w('<path d="M15 10H5M9 5l-5 5 5 5"/>'),
    trash:    w('<path d="M4 6h12M8 6V4.5h4V6M6 6l.7 10.5h6.6L14 6M8.5 9v5M11.5 9v5"/>'),
    upload:   w('<path d="M10 13V4M6 8l4-4 4 4M4 14v1.5a1 1 0 001 1h10a1 1 0 001-1V14"/>'),
    region:   w('<path d="M10 17s5.5-4.7 5.5-9A5.5 5.5 0 0010 2.5 5.5 5.5 0 004.5 8c0 4.3 5.5 9 5.5 9z"/><circle cx="10" cy="8" r="2"/>'),
    clock:    w('<circle cx="10" cy="10" r="7"/><path d="M10 6v4l2.5 2"/>'),
    layers:   w('<path d="M10 3l7 3.5-7 3.5-7-3.5z"/><path d="M3 10l7 3.5 7-3.5M3 13.5l7 3.5 7-3.5"/>'),
    heart:    w('<path d="M10 16.5C5 13 2.5 10.4 2.5 7.5A3.5 3.5 0 0110 5a3.5 3.5 0 017.5 2.5c0 2.9-2.5 5.5-7.5 9z"/>'),
  };
})();
