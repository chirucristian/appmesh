/* AppMesh Console v2 — screens: Regions, Accounts, New Deployment, Scan, Login */

function RegionsScreen() {
  const [opOpen, setOpOpen] = React.useState(true);
  return (
    <div className="page" data-screen-label="Regions">
      <div className="phead">
        <div>
          <h1>Regions</h1>
          <p className="sub">Each onboarded region runs one shared ALB and a gateway service. Onboarding provisions real AWS resources.</p>
        </div>
      </div>

      <div className="regiongrid">
        {AM_REGIONS.map((r) =>
          r.active ? (
            <div className="regioncard" key={r.id}>
              <div className="rhead">
                <span className="rname"><Ic k="region" />{r.id}</span>
                <span className="badge"><span className="dot"></span>active</span>
              </div>
              <div className="rfacts">
                <div className="rfact"><Ic k="balance" />Shared ALB<span className="ok"><span className="dot"></span>healthy</span></div>
                <div className="rfact"><Ic k="term" />Gateway <b>{r.gateway}</b><span className="ok"><span className="dot"></span>1/1</span></div>
                <div className="rfact"><Ic k="box" /><span><b>{r.services}</b> service{r.services === 1 ? '' : 's'} deployed</span></div>
                <div className="rfact"><Ic k="clock" />Onboarded {r.onboarded}</div>
              </div>
              <div className="rfoot">
                {r.console
                  ? <span className="badge badge-neutral">console region</span>
                  : <button className="btn btn-sm btn-danger"><Ic k="trash" />Remove region</button>}
              </div>
            </div>
          ) : (
            <div className="regioncard off" key={r.id}>
              <div className="rhead">
                <span className="rname"><Ic k="region" />{r.id}</span>
                <span className="badge badge-neutral">not onboarded</span>
              </div>
              <div className="rfacts">
                <p className="offnote">Onboarding creates the shared ALB, deploys the gateway JAR, and registers DNS. Takes ~4 minutes.</p>
              </div>
              <div className="rfoot">
                <button className="btn btn-sm btn-primary" onClick={() => setOpOpen(true)}><Ic k="plus" />Onboard region</button>
              </div>
            </div>
          )
        )}
      </div>

      {opOpen && (
        <div className="term">
          <div className="term-head">
            <span className="term-title"><Ic k="term" />Onboarding eu-west-1</span>
            <span className="term-status"><span className="dot"></span>running · step 5 / 7</span>
          </div>
          <div className="term-body">
            <div className="tl"><span className="ts">14:02:11</span><span className="ok">✓ Ensuring gateway IAM role… ok</span></div>
            <div className="tl"><span className="ts">14:02:14</span><span className="ok">✓ Ensuring shared ALB in eu-west-1… created appmesh-alb-euw1</span></div>
            <div className="tl"><span className="ts">14:03:02</span><span className="ok">✓ Waiting for ALB active… ok (47s)</span></div>
            <div className="tl"><span className="ts">14:03:05</span><span className="ok">✓ Uploading gateway JAR… gateway-lb-3e91.jar (18.2 MB)</span></div>
            <div className="tl"><span className="ts">14:03:18</span><span className="run">▸ Provisioning gateway stack in eu-west-1… ASG launching 1 instance</span></div>
          </div>
        </div>
      )}
    </div>
  );
}

function AccountsScreen() {
  return (
    <div className="page" data-screen-label="Accounts">
      <div className="phead">
        <div>
          <h1>Accounts</h1>
          <p className="sub">{AM_USERS.length} users · root accounts manage regions, accounts and all deployments</p>
        </div>
      </div>

      <div className="cols" style={{ gridTemplateColumns: '1.6fr 1fr' }}>
        <div className="tcard">
          <table className="dt">
            <thead>
              <tr><th>Username</th><th>Role</th><th>Deployments</th><th>Created</th><th className="r"></th></tr>
            </thead>
            <tbody>
              {AM_USERS.map((u) => (
                <tr key={u.username}>
                  <td>
                    <span className="svc">
                      <span className="userchip" style={{ padding: 0, cursor: 'default' }}>
                        <span className="avatar">{u.username.slice(0, 2).toUpperCase()}</span>
                      </span>
                      {u.username}
                    </span>
                  </td>
                  <td>{u.role === 'root'
                    ? <span className="badge badge-accent">root</span>
                    : <span className="badge badge-neutral">user</span>}</td>
                  <td className="muted">{AM_DEPLOYMENTS.filter((d) => d.owner === u.username && d.kind === 'service').length || '—'}</td>
                  <td className="muted">{u.created}</td>
                  <td className="r">{u.role !== 'root' && <button className="btn btn-sm btn-danger">Delete</button>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="panel" style={{ marginBottom: 0 }}>
          <div className="panel-head"><span className="panel-title"><Ic k="plus" />Create account</span></div>
          <div className="panel-body">
            <div className="field">
              <label>Username</label>
              <input className="input mono" placeholder="username" />
            </div>
            <div className="field">
              <label>Password</label>
              <input className="input" type="password" placeholder="••••••••" />
            </div>
            <div className="field">
              <label>Role</label>
              <div className="seg">
                <button className="on">user</button>
                <button>root</button>
              </div>
              <span className="hint">Users only see and manage their own deployments.</span>
            </div>
            <button className="btn btn-primary" style={{ width: '100%', justifyContent: 'center' }}>Create account</button>
          </div>
        </div>
      </div>
    </div>
  );
}

function NewDeploymentScreen({ go }) {
  const active = AM_REGIONS.filter((r) => r.active);
  return (
    <div className="page" data-screen-label="New Deployment" style={{ maxWidth: '980px' }}>
      <button className="backlink" onClick={() => go('dashboard')}><Ic k="arrowL" />Deployments</button>
      <div className="phead">
        <div>
          <h1>New deployment</h1>
          <p className="sub">Deploy a .jar as a scaled service behind the region's shared load balancer.</p>
        </div>
      </div>

      <div className="cols" style={{ gridTemplateColumns: '1.5fr 1fr' }}>
        <div className="panel" style={{ marginBottom: 0 }}>
          <div className="panel-body" style={{ padding: '22px' }}>
            <div className="field">
              <label>Service name</label>
              <input className="input mono" placeholder="my-service" />
              <span className="hint">Hostname will be <code>&lt;name&gt;-&lt;region&gt;.appmesh.io</code></span>
            </div>
            <div className="field">
              <label>Region</label>
              <div className="seg">
                {active.map((r, i) => (
                  <button key={r.id} className={i === 0 ? 'on' : ''}>{r.id}</button>
                ))}
              </div>
              <span className="hint">Only onboarded regions are available.</span>
            </div>
            <div className="field">
              <label>Initial instances</label>
              <div className="stepper" style={{ width: 'fit-content' }}>
                <button><Ic k="minus" /></button>
                <span className="n">2</span>
                <button><Ic k="plus" /></button>
              </div>
            </div>
            <div className="field">
              <label>Application JAR</label>
              <div className="bigdrop">
                <Ic k="upload" />
                <span className="t1">Drop your .jar here or <b>browse</b></span>
                <span className="t2">Spring Boot fat JAR · serves HTTP on :8080</span>
              </div>
            </div>
            <button className="btn btn-primary" style={{ width: '100%', justifyContent: 'center', height: '38px' }}>
              <Ic k="upload" />Deploy
            </button>
          </div>
        </div>

        <div className="panel" style={{ marginBottom: 0 }}>
          <div className="panel-head"><span className="panel-title"><Ic k="layers" />What gets created</span></div>
          <div className="panel-body" style={{ paddingTop: '8px', paddingBottom: '10px' }}>
            <div className="steplist">
              {[['shield', 'Security group + IAM role'],
                ['box', 'JAR uploaded to S3'],
                ['doc', 'EC2 launch template'],
                ['target', 'Target group on :8080'],
                ['rule', 'Host rule on the shared regional ALB'],
                ['activity', 'Auto Scaling Group across 2 AZs'],
                ['globe', 'Cloudflare CNAME + health check']].map(([ic, s]) => (
                <div className="step" key={s}>
                  <span className="sc idle"><Ic k={ic} /></span>
                  <span className="st">{s}</span>
                </div>
              ))}
            </div>
            <p style={{ fontSize: '11.5px', color: 'var(--text-3)', marginTop: '10px', borderTop: '1px solid var(--line)', paddingTop: '12px' }}>
              No new load balancer — your service attaches to the region's shared ALB with a host-based rule. Live progress streams here during deploy.
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}

function ScanScreen() {
  return (
    <div className="page" data-screen-label="Scan">
      <div className="phead">
        <div>
          <h1>Scan AWS</h1>
          <p className="sub">Discovers appmesh-* resources across regions, groups them by deployment, and flags drift.</p>
        </div>
        <div className="phead-actions">
          <span className="panel-ts">last scan · 14:08, all 3 regions</span>
          <button className="btn btn-primary"><Ic k="radar" />Run scan</button>
        </div>
      </div>

      <div className="sectionlabel"><Ic k="check" />Healthy — 4 groups, tracked in console</div>
      <div className="panel scangroup">
        <div className="panel-head">
          <span className="panel-title">politia <span className="regionpill" style={{ marginLeft: '4px' }}><Ic k="region" />eu-central-1</span></span>
          <span className="badge"><span className="dot"></span>complete · imported</span>
        </div>
        <div className="panel-body" style={{ paddingTop: '6px', paddingBottom: '6px' }}>
          <div className="scanrows">
            <div className="scanrow"><Ic k="target" /><code>appmesh-politia-tg-7f3a9c</code><span className="have">found</span></div>
            <div className="scanrow"><Ic k="rule" /><code>rule: politia-eu-central-1.appmesh.io</code><span className="have">found</span></div>
            <div className="scanrow"><Ic k="activity" /><code>appmesh-politia-asg-7f3a9c</code><span className="have">3 instances</span></div>
            <div className="scanrow"><Ic k="globe" /><code>CNAME politia-eu-central-1</code><span className="have">found</span></div>
          </div>
        </div>
      </div>

      <div className="sectionlabel"><Ic k="alert" />Broken — partial resource set</div>
      <div className="panel scangroup broken">
        <div className="panel-head">
          <span className="panel-title">demo-old <span className="regionpill" style={{ marginLeft: '4px' }}><Ic k="region" />us-east-1</span></span>
          <div style={{ display: 'flex', gap: '8px' }}>
            <button className="btn btn-sm"><Ic k="import" />Import</button>
            <button className="btn btn-sm btn-danger"><Ic k="trash" />Teardown</button>
          </div>
        </div>
        <div className="panel-body" style={{ paddingTop: '6px', paddingBottom: '6px' }}>
          <div className="scanrows">
            <div className="scanrow"><Ic k="shield" /><code>appmesh-demo-old-sg-2b91xx</code><span className="have">found</span></div>
            <div className="scanrow"><Ic k="doc" /><code>appmesh-demo-old-lt-2b91xx</code><span className="have">found</span></div>
            <div className="scanrow"><Ic k="activity" /><code>appmesh-demo-old-asg-2b91xx</code><span className="miss">missing</span></div>
            <div className="scanrow"><Ic k="target" /><code>appmesh-demo-old-tg-2b91xx</code><span className="miss">missing</span></div>
          </div>
        </div>
      </div>

      <div className="sectionlabel"><Ic k="alert" />Orphans — no matching AWS deployment</div>
      <div className="panel scangroup orphan" style={{ marginBottom: 0 }}>
        <div className="panel-head">
          <span className="panel-title">Cloudflare + S3 leftovers</span>
          <button className="btn btn-sm btn-danger"><Ic k="trash" />Clean up all</button>
        </div>
        <div className="panel-body" style={{ paddingTop: '6px', paddingBottom: '6px' }}>
          <div className="scanrows">
            <div className="scanrow"><Ic k="globe" /><code>CNAME test-svc-us-east-1.appmesh.io</code><span className="miss">orphan DNS</span></div>
            <div className="scanrow"><Ic k="box" /><code>conf/dashboard/test-svc-1a2b3c.json</code><span className="miss">orphan config</span></div>
          </div>
        </div>
      </div>
    </div>
  );
}

function LoginScreen({ onLogin }) {
  return (
    <div className="loginwrap" data-screen-label="Login">
      <div className="logincard">
        <div className="lbrand">
          <span className="mark"><Ic k="layers" /></span>
          <span>AppMesh <span style={{ color: 'var(--text-3)', fontWeight: 500 }}>Console</span></span>
        </div>
        <p className="lsub">Sign in to manage your deployments</p>
        <div className="lpanel">
          <div className="field">
            <label>Username</label>
            <input className="input mono" placeholder="username" defaultValue="cchiru" />
          </div>
          <div className="field">
            <label>Password</label>
            <input className="input" type="password" placeholder="••••••••" defaultValue="password" />
          </div>
          <button className="btn btn-primary" onClick={onLogin}><Ic k="lock" />Sign in</button>
        </div>
        <p className="lfoot">appmesh console · us-east-1</p>
      </div>
    </div>
  );
}

Object.assign(window, { RegionsScreen, AccountsScreen, NewDeploymentScreen, ScanScreen, LoginScreen });
