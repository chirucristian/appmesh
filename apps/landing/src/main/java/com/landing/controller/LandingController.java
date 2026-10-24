package com.landing.controller;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.ResponseBody;
import org.springframework.web.bind.annotation.RestController;

/**
 * Serves the public service directory.
 * <p>
 * {@code GET /} returns a self-contained page whose JavaScript fetches the live
 * routing table from {@code /gateway/status} (served by the gateway on the same
 * origin), renders one card per service, and links each to {@code /<service>}.
 * A <b>Refresh</b> button re-scans on demand. {@code GET /status} is the health probe.
 */
@RestController
public class LandingController {

    @GetMapping(value = "/", produces = "text/html; charset=utf-8")
    @ResponseBody
    public String index() {
        return PAGE;
    }

    @GetMapping(value = "/status", produces = "application/json")
    @ResponseBody
    public String status() {
        return "{\"status\":\"UP\",\"service\":\"landing\"}";
    }

    private static final String PAGE = """
            <!DOCTYPE html>
            <html lang="en"><head>
            <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>AppMesh — Services</title>
            <style>
            *{margin:0;padding:0;box-sizing:border-box}
            body{min-height:100vh;font-family:'Segoe UI',system-ui,sans-serif;
             background:radial-gradient(ellipse at top,#1a2238,#0a0e1a);color:#e6e9f0;padding:48px 24px}
            .wrap{max-width:980px;margin:0 auto}
            header{text-align:center;margin-bottom:36px}
            .logo{font-size:13px;letter-spacing:4px;text-transform:uppercase;color:#4f8cff;margin-bottom:12px}
            h1{font-size:40px;margin-bottom:10px}
            header p{color:#9aa3b2;font-size:16px;margin-bottom:20px}
            .controls{display:flex;align-items:center;justify-content:center;gap:14px}
            button.refresh{display:inline-flex;align-items:center;gap:8px;cursor:pointer;
             background:#4f8cff;color:#0a0e1a;border:none;border-radius:10px;
             padding:10px 20px;font-size:14px;font-weight:700;transition:transform .15s,box-shadow .15s}
            button.refresh:hover{transform:translateY(-1px);box-shadow:0 6px 20px rgba(79,140,255,0.35)}
            button.refresh:disabled{opacity:.6;cursor:default;transform:none;box-shadow:none}
            button.refresh .spin{display:inline-block}
            button.refresh.loading .spin{animation:spin .8s linear infinite}
            @keyframes spin{to{transform:rotate(360deg)}}
            .updated{color:#6b7382;font-size:12px}
            .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:18px;margin-top:28px}
            a.svc{display:block;text-decoration:none;color:inherit;
             background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);
             border-radius:16px;padding:22px 24px;transition:transform .15s,border-color .15s,background .15s}
            a.svc:hover{transform:translateY(-3px);border-color:#4f8cff;background:rgba(79,140,255,0.08)}
            .svc-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}
            .svc-name{font-size:19px;font-weight:700}
            .go{color:#4f8cff;font-size:13px;font-weight:600}
            .regions{display:flex;flex-wrap:wrap;gap:8px}
            .region{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:#c2c8d4;
             background:rgba(0,0,0,0.25);border-radius:999px;padding:4px 10px}
            .region i{width:8px;height:8px;border-radius:50%;display:inline-block}
            .region.ok i{background:#2ecc71;box-shadow:0 0 6px #2ecc71}
            .region.warn i{background:#f1c40f;box-shadow:0 0 6px #f1c40f}
            .region.down i{background:#e74c3c;box-shadow:0 0 6px #e74c3c}
            .empty{grid-column:1/-1;text-align:center;color:#6b7382;padding:60px 20px;
             border:1px dashed rgba(255,255,255,0.12);border-radius:16px}
            footer{text-align:center;margin-top:48px;color:#5a6270;font-size:13px}
            footer a{color:#4f8cff;text-decoration:none}
            </style></head><body>
            <div class="wrap">
             <header>
              <div class="logo">AppMesh</div>
              <h1>Service Directory</h1>
              <p>Live services routed across regions with automatic failover.</p>
              <div class="controls">
               <button id="refresh" class="refresh"><span class="spin">&#8635;</span> Refresh</button>
               <span id="updated" class="updated"></span>
              </div>
             </header>
             <div id="grid" class="grid"><div class="empty">Loading services…</div></div>
             <footer>Run your own services at <a href="https://console.appmesh.org">console.appmesh.org</a></footer>
            </div>
            <script>
            const grid = document.getElementById('grid');
            const updated = document.getElementById('updated');
            const refreshBtn = document.getElementById('refresh');

            function regionDot(r) {
              const cls = r.healthy ? 'ok' : (r.circuitBreaker === 'OPEN' ? 'down' : 'warn');
              return `<span class="region ${cls}"><i></i>${r.name}</span>`;
            }

            async function load() {
              refreshBtn.disabled = true;
              refreshBtn.classList.add('loading');
              try {
                const resp = await fetch('/gateway/status', { cache: 'no-store' });
                if (!resp.ok) throw new Error('HTTP ' + resp.status);
                const data = await resp.json();
                const names = Object.keys(data).filter(n => n !== 'landing').sort();
                if (!names.length) {
                  grid.innerHTML = '<div class="empty">No services are published yet. '
                    + 'Institutions can publish one from the AppMesh console.</div>';
                } else {
                  grid.innerHTML = names.map(name => {
                    const svc = data[name] || {};
                    const dots = (svc.regions || []).map(regionDot).join('');
                    return `<a class="svc" href="/${name}/">
                        <div class="svc-head"><span class="svc-name">${name}</span><span class="go">Open &rarr;</span></div>
                        <div class="regions">${dots}</div></a>`;
                  }).join('');
                }
                updated.textContent = 'updated ' + new Date().toLocaleTimeString();
              } catch (e) {
                grid.innerHTML = `<div class="empty">Couldn't load services (${e.message}).</div>`;
              } finally {
                refreshBtn.disabled = false;
                refreshBtn.classList.remove('loading');
              }
            }

            refreshBtn.addEventListener('click', load);
            load();
            </script>
            </body></html>
            """;
}
