/* AppMesh Console v2 — screens: Dashboard, Deployment Detail */

function DashboardScreen({ go, role }) {
  const isRoot = role === 'root';
  const deps = isRoot ? AM_DEPLOYMENTS : AM_DEPLOYMENTS.filter((d) => d.owner === 'andrei');
  const active = AM_REGIONS.filter((r) => r.active);
  return (
    <div className="page" data-screen-label="Dashboard">
      <div className="phead">
        <div>
          <h1>Deployments</h1>
          <p className="sub">{deps.length} services across {active.length} regions</p>
        </div>
        <div className="phead-actions">
          <button className="btn" onClick={() => go('scan')}><Ic k="radar" />Scan AWS</button>
          <button className="btn btn-primary" onClick={() => go('new')}><Ic k="plus" />New deployment</button>
        </div>
      </div>

      <div className="rstrip">
        {AM_REGIONS.map((r) => (
          <div className="rchip" key={r.id} onClick={() => go(isRoot ? 'regions' : 'dashboard')}>
            <span className={'rdot' + (r.active ? '' : ' off')}></span>
            <span className="rname">{r.id}</span>
            <span className="rmeta">{r.active ? `gateway ok · ${r.services} svc` : 'not onboarded'}</span>
          </div>
        ))}
      </div>

      <div className="tcard">
        <table className="dt">
          <thead>
            <tr>
              <th>Service</th>
              {isRoot && <th>Owner</th>}
              <th>Region</th>
              <th>Status</th>
              <th>Hostname</th>
              <th>Instances</th>
              <th>Created</th>
              <th className="r"></th>
            </tr>
          </thead>
          <tbody>
            {deps.map((d) => (
              <tr className="rowlink" key={d.id} onClick={() => go('detail', d.id)}>
                <td>
                  <span className="svc">
                    {d.name}
                    {d.kind === 'gateway' && <span className="kindtag">gateway</span>}
                  </span>
                </td>
                {isRoot && <td className="muted">{d.owner}</td>}
                <td><RegionPill region={d.region} /></td>
                <td><StatusBadge status={d.status} /></td>
                <td><a className="hostlink" onClick={(e) => e.stopPropagation()}>{d.host}</a></td>
                <td className="muted mono" style={{ fontSize: '12px' }}>{d.instances[0]} / {d.instances[1]}</td>
                <td className="muted">{d.created}</td>
                <td className="r"><span className="rowarrow"><Ic k="chevronR" /></span></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function DetailScreen({ go, depId }) {
  const d = AM_DEPLOYMENTS.find((x) => x.id === depId) || AM_DEPLOYMENTS[0];
  const resources = [
    ['shield', 'Security Group', 'sg-0a2b3c4d · port 8080', 'active'],
    ['key', 'IAM Role', `appmesh-${d.name}-role`, 'active'],
    ['idcard', 'Instance Profile', `appmesh-${d.name}-ip`, 'active'],
    ['box', 'S3 JAR', `${d.jar} · 24.7 MB`, 'active'],
    ['doc', 'Launch Template', 'lt-09c4f1 · v3', 'active'],
    ['target', 'Target Group', `${d.instances[0]}/${d.instances[1]} healthy`, 'active'],
    ['rule', 'Listener Rule', `host ${d.host} → tg`, 'active'],
    ['balance', 'Regional ALB', `${d.region} · shared`, 'shared'],
    ['activity', 'Auto Scaling Group', `desired ${d.instances[1]}`, 'active'],
    ['globe', 'DNS Record', 'CNAME · proxied', 'active'],
  ];
  return (
    <div className="page" data-screen-label="Deployment Detail">
      <button className="backlink" onClick={() => go('dashboard')}><Ic k="arrowL" />Deployments</button>
      <div className="phead">
        <div>
          <h1>
            {d.name}
            {d.kind === 'gateway' && <span className="kindtag">gateway</span>}
            <StatusBadge status={d.status} />
          </h1>
          <div className="meta">
            <span className="mi"><Ic k="region" />{d.region}</span>
            <span className="mi"><Ic k="box" /><code>{d.jar}</code></span>
            <span className="mi"><Ic k="users" />{d.owner}</span>
            <span className="mi"><Ic k="clock" />Deployed {d.created} · 14:31</span>
          </div>
        </div>
        <div className="phead-actions">
          <button className="btn"><Ic k="upload" />Update JAR</button>
          <button className="btn btn-danger"><Ic k="trash" />Teardown</button>
        </div>
      </div>

      <div className="urlstrip">
        <span className="lt"><span className="dot"></span>Live</span>
        <a>https://{d.host}</a>
        <button className="icon-btn" title="Copy URL"><Ic k="copy" /></button>
        <button className="icon-btn" title="Open"><Ic k="external" /></button>
      </div>

      <div className="kpis">
        <div className="kpi">
          <div className="kl"><Ic k="heart" />Status</div>
          <div className="kv">Healthy</div>
          <div className="ks ok">All checks passing</div>
        </div>
        <div className="kpi">
          <div className="kl"><Ic k="server" />Instances</div>
          <div className="kv">{d.instances[0]}<span className="u"> / {d.instances[1]}</span></div>
          <div className="ks ok">100% healthy</div>
        </div>
        <div className="kpi">
          <div className="kl"><Ic k="target" />Targets</div>
          <div className="kv">{d.instances[0]}<span className="u"> / {d.instances[1]}</span></div>
          <div className="ks ok">Passing health check</div>
        </div>
        <div className="kpi">
          <div className="kl"><Ic k="activity" />Auto-scaling</div>
          <div className="kv">{d.range[0]}<span className="u"> – </span>{d.range[1]}</div>
          <div className="ks">desired {d.instances[1]}</div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <span className="panel-title"><Ic k="layers" />Request path</span>
          <span className="panel-ts">live · refreshed 12s ago</span>
        </div>
        <div className="panel-body" style={{ paddingTop: '24px' }}>
          <div className="pipe">
            <div className="stage">
              <div className="sbox">
                <div className="si"><Ic k="globe" /></div>
                <div className="sname">Cloudflare DNS</div>
                <div className="smeta">CNAME · proxied</div>
                <span className="schk"><Ic k="check" /></span>
              </div>
              <div className="scap">Edge</div>
            </div>
            <div className="conn"><span className="ccap">:443</span><span className="cl"></span></div>
            <div className="stage">
              <div className="sbox">
                <span className="stag">shared · {d.region}</span>
                <div className="si"><Ic k="balance" /></div>
                <div className="sname">Regional ALB</div>
                <div className="smeta">host rule → tg</div>
                <span className="schk"><Ic k="check" /></span>
              </div>
              <div className="scap">One per region</div>
            </div>
            <div className="conn"><span className="ccap">:8080</span><span className="cl"></span></div>
            <div className="stage">
              <div className="sbox">
                <div className="si"><Ic k="target" /></div>
                <div className="sname">Target Group</div>
                <div className="smeta">HTTP · {d.instances[0]}/{d.instances[1]}</div>
                <span className="schk"><Ic k="check" /></span>
              </div>
              <div className="scap">Targets</div>
            </div>
            <div className="conn"><span className="ccap">reg</span><span className="cl"></span></div>
            <div className="stage">
              <div className="sbox">
                <div className="si"><Ic k="activity" /></div>
                <div className="sname">Auto Scaling</div>
                <div className="smeta">desired {d.instances[1]}</div>
                <div className="pods">
                  {Array.from({ length: d.instances[0] }).map((_, i) => <span className="pod" key={i}></span>)}
                </div>
                <span className="schk"><Ic k="check" /></span>
              </div>
              <div className="scap">EC2 · 2 AZ</div>
            </div>
          </div>
        </div>
      </div>

      <div className="cols">
        <div className="panel">
          <div className="panel-head"><span className="panel-title"><Ic k="idcard" />Deployment</span></div>
          <div className="panel-body" style={{ paddingTop: '4px', paddingBottom: '4px' }}>
            <dl className="dl">
              <div className="row"><dt>ID</dt><dd><code>{d.id}</code></dd></div>
              <div className="row"><dt>JAR</dt><dd><code>{d.jar}</code></dd></div>
              <div className="row"><dt>Owner</dt><dd>{d.owner}</dd></div>
              <div className="row"><dt>Region</dt><dd>{d.region}</dd></div>
              <div className="row"><dt>Created</dt><dd>2026-06-02 14:31:08</dd></div>
              <div className="row"><dt>Hostname</dt><dd><code>{d.host}</code></dd></div>
            </dl>
          </div>
        </div>
        <div className="panel">
          <div className="panel-head">
            <span className="panel-title"><Ic k="layers" />AWS Resources</span>
            <span className="panel-ts">10 active</span>
          </div>
          <div className="panel-body" style={{ paddingTop: '4px', paddingBottom: '4px' }}>
            <div className="rtable">
              {resources.map(([ic, n, m, s]) => (
                <div className="rrow" key={n}>
                  <span className="ri"><Ic k={ic} /></span>
                  <span className="rn">{n}</span>
                  <span className="rm">{m}</span>
                  {s === 'shared'
                    ? <span className="rs neutral"><span className="dot"></span>shared</span>
                    : <span className="rs"><span className="dot"></span>active</span>}
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head"><span className="panel-title"><Ic k="activity" />Scaling</span></div>
        <div className="panel-body">
          <div className="scaler">
            <div className="grp">
              <div className="m"><div className="mv">{d.instances[1]}</div><div className="ml">Desired</div></div>
              <div className="m"><div className="mv">{d.instances[0]}</div><div className="ml">Running</div></div>
              <div className="m"><div className="mv">{d.instances[0]}</div><div className="ml">Healthy</div></div>
              <div className="m"><div className="mv">{d.range[0]}–{d.range[1]}</div><div className="ml">Min–Max</div></div>
            </div>
            <div className="ctrl">
              <span className="lab">Set desired</span>
              <div className="stepper">
                <button><Ic k="minus" /></button>
                <span className="n">{d.instances[1]}</span>
                <button><Ic k="plus" /></button>
              </div>
              <button className="btn btn-primary">Apply</button>
            </div>
          </div>
        </div>
      </div>

      <div className="cols">
        <div className="panel">
          <div className="panel-head"><span className="panel-title"><Ic k="upload" />Update JAR</span></div>
          <div className="panel-body">
            <div className="upd">
              <div className="drop"><Ic k="box" /><span>Drop a <b>.jar</b> to roll out via instance refresh</span></div>
              <button className="btn btn-primary">Deploy</button>
            </div>
          </div>
        </div>
        <div className="panel">
          <div className="panel-head">
            <span className="panel-title"><Ic k="check" />Deployment steps</span>
            <span className="panel-ts">12 / 12</span>
          </div>
          <div className="panel-body" style={{ paddingTop: '8px', paddingBottom: '8px' }}>
            <div className="steplist">
              {['Security group + IAM', 'JAR upload + launch template', 'Target group', 'Shared regional ALB',
                'Listener rule (host-based)', 'Auto Scaling Group', 'Cloudflare DNS', 'Health check'].map((s) => (
                <div className="step" key={s}>
                  <span className="sc"><Ic k="check" /></span>
                  <span className="st">{s}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

Object.assign(window, { DashboardScreen, DetailScreen });
