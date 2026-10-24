/* AppMesh Console v3 — shell: icons, shared components, mock data, sidebar nav. */

/* extra icons on top of icons.js */
(function () {
  const w = (p) =>
    `<svg class="ic" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${p}</svg>`;
  Object.assign(window.IC, {
    users:  w('<circle cx="7.5" cy="7" r="2.8"/><path d="M2.5 16.5c0-2.8 2.2-4.5 5-4.5s5 1.7 5 4.5"/><path d="M13 4.6a2.8 2.8 0 010 4.9M14.5 12.3c1.8.6 3 2 3 4.2"/>'),
    radar:  w('<circle cx="10" cy="10" r="7"/><circle cx="10" cy="10" r="3.6" opacity=".45"/><path d="M10 10l4.8-4.8M10 3v2"/><circle cx="10" cy="10" r="0.7" fill="currentColor" stroke="none"/>'),
    logout: w('<path d="M8 3.5H4.5v13H8M13 6.5l3.5 3.5-3.5 3.5M16.5 10H8"/>'),
    lock:   w('<rect x="4.5" y="8.5" width="11" height="8.5" rx="1.5"/><path d="M7 8.5V6.5a3 3 0 016 0v2"/>'),
    term:   w('<rect x="2.5" y="3.5" width="15" height="13" rx="1.5"/><path d="M5.5 8l2.5 2-2.5 2M10.5 12.5h4"/>'),
    alert:  w('<path d="M10 3L1.8 16.5h16.4z"/><path d="M10 8.5v3.5M10 14.5h.01"/>'),
    minus:  w('<path d="M4 10h12"/>'),
    rule:   w('<path d="M3 6h14M3 10h9M3 14h14"/><circle cx="15" cy="10" r="1.8"/>'),
    import: w('<path d="M10 3v9M6.5 8.5L10 12l3.5-3.5M4 14.5v1a1.5 1.5 0 001.5 1.5h9a1.5 1.5 0 001.5-1.5v-1"/>'),
  });
})();

/* tiny helper to render an IC string */
function Ic({ k }) {
  return <span className="ic-wrap" style={{ display: 'contents' }} dangerouslySetInnerHTML={{ __html: IC[k] }}></span>;
}

/* ---------------- mock data ---------------- */
const AM_REGIONS = [
  { id: 'us-east-1',    abbrev: 'use1', active: true,  console: true,  gateway: 'gateway-use1', services: 2, onboarded: 'May 18, 2026' },
  { id: 'eu-central-1', abbrev: 'euc1', active: true,  console: false, gateway: 'gateway-euc1', services: 1, onboarded: 'May 21, 2026' },
  { id: 'eu-west-1',    abbrev: 'euw1', active: false, console: false, gateway: null,           services: 0, onboarded: null },
];

const AM_DEPLOYMENTS = [
  { id: 'politia-7f3a9c', name: 'politia', kind: 'service', owner: 'cchiru', jar: 'politia-0.3.1.jar', region: 'eu-central-1',
    status: 'deployed', host: 'politia-eu-central-1.appmesh.io', created: 'Jun 2, 2026', instances: [3, 3], range: [1, 6] },
  { id: 'ambulanta-2c81de', name: 'ambulanta', kind: 'service', owner: 'cchiru', jar: 'ambulanta-1.1.0.jar', region: 'us-east-1',
    status: 'deployed', host: 'ambulanta-us-east-1.appmesh.io', created: 'May 29, 2026', instances: [2, 2], range: [1, 4] },
  { id: 'game-service-9b3f21', name: 'game-service', kind: 'service', owner: 'andrei', jar: 'game-service-0.9.4.jar', region: 'us-east-1',
    status: 'updating', host: 'game-service-us-east-1.appmesh.io', created: 'Jun 7, 2026', instances: [2, 3], range: [1, 4] },
  { id: 'gateway-use1-infra', name: 'gateway-use1', kind: 'gateway', owner: 'root', jar: 'gateway-lb-0.0.1.jar', region: 'us-east-1',
    status: 'deployed', host: 'gateway-us-east-1.appmesh.io', created: 'May 18, 2026', instances: [1, 1], range: [1, 2] },
  { id: 'gateway-euc1-infra', name: 'gateway-euc1', kind: 'gateway', owner: 'root', jar: 'gateway-lb-0.0.1.jar', region: 'eu-central-1',
    status: 'deployed', host: 'gateway-eu-central-1.appmesh.io', created: 'May 21, 2026', instances: [1, 1], range: [1, 2] },
];

const AM_USERS = [
  { username: 'cchiru', role: 'root', created: '2026-05-18' },
  { username: 'andrei', role: 'user', created: '2026-05-30' },
  { username: 'deploy-bot', role: 'user', created: '2026-06-04' },
];

/* ---------------- shared bits ---------------- */
function StatusBadge({ status }) {
  if (status === 'deployed') {
    return <span className="badge"><span className="dot"></span>deployed</span>;
  }
  if (status === 'updating' || status === 'deploying' || status === 'tearing_down') {
    return <span className="badge badge-amber"><span className="dot pulse"></span>{status.replace('_', ' ')}</span>;
  }
  if (status === 'failed' || status === 'teardown_partial') {
    return <span className="badge badge-red"><span className="dot"></span>{status.replace('_', ' ')}</span>;
  }
  return <span className="badge badge-neutral">{status}</span>;
}

function RegionPill({ region }) {
  return <span className="regionpill"><Ic k="region" />{region}</span>;
}

function SideNav({ route, go, role, user, deployCount }) {
  const isRoot = role === 'root';
  const depActive = route === 'dashboard' || route === 'detail' || route === 'new';
  return (
    <aside className="sidenav">
      <a className="brand" onClick={() => go('dashboard')}>
        <span className="mark"><Ic k="layers" /></span>
        <span>AppMesh</span>
        <span className="dim">Console</span>
      </a>
      <div className="navgroup">
        <div className="navlabel">Operate</div>
        <button className={'navitem' + (depActive ? ' active' : '')} onClick={() => go('dashboard')}>
          <Ic k="box" />Deployments<span className="count">{deployCount}</span>
        </button>
        <button className={'navitem' + (route === 'scan' ? ' active' : '')} onClick={() => go('scan')}>
          <Ic k="radar" />Scan AWS
        </button>
      </div>
      {isRoot && (
        <div className="navgroup">
          <div className="navlabel">Administer</div>
          <button className={'navitem' + (route === 'regions' ? ' active' : '')} onClick={() => go('regions')}>
            <Ic k="globe" />Regions
          </button>
          <button className={'navitem' + (route === 'accounts' ? ' active' : '')} onClick={() => go('accounts')}>
            <Ic k="users" />Accounts
          </button>
        </div>
      )}
      <span className="spacer"></span>
      <div className="railfoot">
        <span className="avatar">{user.slice(0, 2).toUpperCase()}</span>
        <span>
          <div className="uname">{user}</div>
          <div className="urole">{role}</div>
        </span>
        <button className="out" title="Log out" onClick={() => go('login')}><Ic k="logout" /></button>
      </div>
    </aside>
  );
}

Object.assign(window, { Ic, AM_REGIONS, AM_DEPLOYMENTS, AM_USERS, StatusBadge, RegionPill, SideNav });
