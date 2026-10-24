# Handoff: AppMesh Console v3 — "Cool Console" redesign

## Overview
A full visual + layout redesign of the AppMesh deploy console (the Flask app in `admin_console/`).
It replaces the current top-navbar, Inter-based UI with a **dark slate sidebar rail + white/cool-gray
content area**, steel-blue accent, Geist/Geist Mono typography, and a consistent panel/badge/table
component system. It covers all seven surfaces of the app: Login, Dashboard, Deployment Detail,
New Deployment, Regions, Accounts, and Scan — and reflects the current architecture
(shared regional ALB + per-region gateway, host-based listener rules, root/user roles).

## About the Design Files
The files in this bundle are **design references created in HTML/React** — a clickable prototype
showing intended look and behavior, **not production code to copy directly**. The task is to
**recreate this design in the target codebase**: a Flask app with server-rendered Jinja templates
(`admin_console/templates/*.html`) and one stylesheet (`admin_console/static/css/style.css`),
with vanilla-JS `fetch`/`EventSource` logic inline in the templates.

Do **not** introduce React. Port the design as: one rewritten `style.css` (the design system below),
a rewritten `base.html` (sidebar layout shell), and restyled markup in each template — **preserving
every element `id`, every `<script>` block, all Jinja variables/conditionals, and all API calls
exactly as they are.** The JS targets ids like `#status-badge`, `#live-resources`, `#op-log`,
`#desired-input`; those must keep working.

## Fidelity
**High-fidelity.** Colors, type, spacing, radii, and copy are final. Recreate pixel-perfectly.
The prototype's mock data (politia, ambulanta, gateways…) is illustrative — real data comes from
the Flask context exactly as today.

## How to view the prototype
Open `AppMesh Console v3.html` in a browser (needs internet for fonts/React CDN). Navigate via the
sidebar; click a dashboard row for the detail screen; "Log out" shows the login screen.
Ignore `tweaks-panel.jsx` and the floating Tweaks panel — it's a design-review tool, not part of the product.

## Screens / Views

### 1. App shell (`base.html`)
- Layout: `display:flex; min-height:100vh`. Fixed-width **218px sidebar** (sticky, `height:100vh`)
  + fluid main column. Content inside main: `max-width:1160px; margin:0 auto; padding:24px 32px 56px`.
- Sidebar (`--rail: #131c27`):
  - Brand row: 24px blue (accent) rounded-6px mark with a "layers" line icon, "AppMesh" white 600 +
    "Console" in `#6e7c8c`, 18px padding, 1px bottom border `rgba(255,255,255,.07)`.
  - Nav groups with mono uppercase labels (10px, letter-spacing .09em, `#6e7c8c`):
    **OPERATE** → Deployments (with right-aligned mono count chip), Scan AWS.
    **ADMINISTER** (render only when `session.role == 'root'`) → Regions, Accounts.
  - Nav item: 13px/500, color `#aeb9c6`, icon 15px `#6e7c8c`, padding 7px 10px, radius 6px.
    Hover + active: background `#1b2735`, text white. Active also: icon `#7da7d4` and a 2px
    accent bar at the far left edge of the rail (positioned at the item's vertical extent).
  - Footer (pinned bottom, top border): 28px square avatar chip (mono initials, `#7da7d4` on
    `#1b2735`), username white 12.5px/600, role uppercase 10px `#6e7c8c`, and an icon-only
    logout button on the right (links to `/logout`).
  - Active nav state comes from the current route (e.g. `request.path`); "Deployments" is active
    for `/`, `/new`, and `/deployment/<id>`.
- No top navbar, no footer bar except a small centered mono line `appmesh deploy console`
  (10.5px, `#8b97a5`) at the bottom of the content column.
- Login page renders **without** the sidebar (full-viewport centered).

### 2. Login (`login.html`)
- Full-viewport centered, background `#f3f5f7` plus a faint top radial glow
  `radial-gradient(ellipse 60% 45% at 50% -10%, rgba(58,110,168,.08), transparent)`.
- 360px column: centered brand (30px accent mark + "AppMesh Console"), subtitle
  "Sign in to manage your deployments" (12.5px `#8b97a5`), then a white card (radius 10px,
  border `#e3e8ed`, shadow `0 1px 2px rgba(22,32,43,.04), 0 8px 24px -12px rgba(22,32,43,.10)`,
  padding 24px) containing Username (mono input) + Password fields and a full-width primary
  button "Sign in" with a lock icon. Mono footer line: `appmesh console · us-east-1`.

### 3. Dashboard (`index.html`)
- Page head: H1 "Deployments" (20px/600, -.015em) + subtitle "N services across M regions"
  (13px `#5b6776`). Right-aligned actions: secondary "Scan AWS" (radar icon) and primary
  "+ New deployment".
- **Regions strip** (new element): one chip per region — 7px status dot (green if active,
  `#d2dae1` if not), mono region id 11.5px/600, and meta text 11px `#8b97a5`
  ("gateway ok · 2 svc" / "not onboarded"). White, border `#e3e8ed`, radius 7px, padding 7px 12px.
  Links to `/regions` for root.
- **Table** in a white card (radius 8px, border, subtle shadow). Header row: `#f8fafb` background,
  10.5px/600 uppercase mono-ish letters `#8b97a5`, letter-spacing .07em.
  Columns: Service · Owner (root only) · Region · Status · Hostname · Instances · Created · chevron.
  - Service: 13px/600; gateway deployments get a `GATEWAY` tag (9.5px mono uppercase, bordered chip).
  - Region: pin icon + mono region id.
  - Status: badge (see tokens) — `deployed` green, `deploying/updating/tearing_down` amber with
    pulsing dot, `failed/teardown_partial` red.
  - Hostname: mono 11.5px `#5b6776`, hover accent. Instances: mono "2 / 3" `#8b97a5`.
  - Rows: padding 10px 16px, bottom hairline, whole row clickable → detail, hover `#f8fafb`.
- Empty state (keep existing behavior): dashed-border card, line icon (no emoji), "No deployments
  yet", primary button.

### 4. Deployment Detail (`deployment_detail.html`) — biggest file, be careful
Order of sections, top to bottom:
1. Back link "← Deployments" (12px, gray → dark on hover).
2. Page head: name (20px/600) + GATEWAY tag if applicable + status badge (`id="status-badge"`).
   Meta row beneath: region · jar filename (mono) · owner · "Deployed <date> · <time>", each with
   a 14px line icon. Right actions: "Update JAR" secondary, "Teardown" danger-secondary
   (red text, red border + `#fbecec` bg on hover).
3. **URL strip** (`id="app-url-banner"`): white bar, mono uppercase green "● LIVE" label,
   mono URL link, copy + open icon-buttons (28px square). Keep the copy-to-clipboard JS.
4. **KPI strip**: one white card split into 4 equal cells by hairlines:
   Status ("Healthy" / "All checks passing" in green 11.5px), Instances ("3 / 3"),
   Targets ("3 / 3"), Auto-scaling ("1 – 6", "desired 3"). Value 22px/600 tabular-nums;
   label 10.5px uppercase `#8b97a5` with icon.
5. **Request path** panel (visualizes live-status): 4 stages connected by 2px arrows with mono
   port captions (`:443`, `:8080`, `reg`):
   Cloudflare DNS (CNAME · proxied) → **Regional ALB** with a floating mono chip
   "SHARED · <region>" in accent color on its top edge (host rule → tg) → Target Group
   (HTTP · 3/3) → Auto Scaling (desired N, green 8px square "pods" per running instance).
   Each stage: raised box `#f8fafb`, border `#d2dae1`, radius 8px, 28px icon tile, name 12px/600,
   mono meta 10px, green check top-right, mono uppercase caption underneath (Edge / One per
   region / Targets / EC2 · 2 AZ). Populate health checks from `/api/deployments/<id>/live-status`.
6. Two-column grid (1fr 1fr, 14px gap):
   - **Deployment** panel: key/value hairline rows (ID, JAR, Owner, Region, Created, Hostname),
     values right-aligned, mono for machine values.
   - **AWS Resources** panel (`id="live-resources"` region): one row per resource — 26px icon tile,
     name 12.5px/600 (fixed 148px), mono detail in gray, right status `● active` (green mono 10.5px)
     or `● shared` (gray) for the Regional ALB row. Resources now are: Security Group, IAM Role,
     Instance Profile, S3 JAR, Launch Template, Target Group, **Listener Rule**, **Regional ALB
     (shared)**, Auto Scaling Group, DNS Record. Keep refresh button + `#live-ts` timestamp
     (mono 11px) in the panel header.
7. **Scaling** panel: left group of four stats (Desired / Running / Healthy / Min–Max; 20px/600
   values, 10px mono uppercase labels), right-aligned control: "Set desired" label + stepper
   (− / mono number / +, 30px tall, hairline borders) + primary "Apply". Keep ids
   (`current-desired`, `current-running`, `current-healthy`, `current-range`, `desired-input`,
   `scale-apply-btn`) wired to the existing scale API JS; the stepper can wrap a hidden/number input.
8. Two-column grid: **Update JAR** panel (dashed drop row "Drop a .jar to roll out via instance
   refresh" + primary Deploy; keep the existing file input + update stream JS) and **Deployment
   steps** panel (list of steps with 16px round green check chips; running step uses accent-soft
   chip; pending steps use bordered idle chip; right-aligned mono durations optional). This list is
   fed by the SSE deploy/update/teardown streams — keep that logic, just restyle the rows.
   Step labels now include "Shared regional ALB" and "Listener rule (host-based)".

### 5. New Deployment (`new_deployment.html`)
- Narrower page (`max-width: 980px`), back link + head "New deployment" with subtitle
  "Deploy a .jar as a scaled service behind the region's shared load balancer."
- Two columns (1.5fr 1fr):
  - Form panel: Service name (mono input, hint "Hostname will be `<name>-<region>.appmesh.io`"),
    Region as a **segmented control** of onboarded regions only (mono labels, accent-soft active
    segment; hint "Only onboarded regions are available."), Initial instances stepper,
    big dashed dropzone ("Drop your .jar here or **browse**" / mono hint "Spring Boot fat JAR ·
    serves HTTP on :8080"), full-width primary "Deploy".
  - **What gets created** panel: 7 idle-chip steps (Security group + IAM role; JAR uploaded to S3;
    EC2 launch template; Target group on :8080; Host rule on the shared regional ALB; Auto Scaling
    Group across 2 AZs; Cloudflare CNAME + health check) + hairline-separated footnote:
    "No new load balancer — your service attaches to the region's shared ALB with a host-based
    rule. Live progress streams here during deploy." On submit, this panel becomes the live SSE
    step timeline (existing behavior).

### 6. Regions (`regions.html`) — root only
- Head: "Regions" + subtitle "Each onboarded region runs one shared ALB and a gateway service.
  Onboarding provisions real AWS resources."
- **3-column card grid** (one card per allowed region):
  - Active card: mono region name + green `● active` badge; fact rows (13px icons):
    "Shared ALB — ● healthy", "Gateway **gateway-xxx** — ● 1/1", "**N** services deployed",
    "Onboarded <date>". Footer: neutral chip `console region` for the console region, else a
    small danger button "Remove region".
  - Not-onboarded card: dashed border, transparent bg, neutral badge, note "Onboarding creates
    the shared ALB, deploys the gateway JAR, and registers DNS. Takes ~4 minutes.", small primary
    "Onboard region".
- **Operation terminal** (replaces the bare `<pre id="op-log">`): dark panel `#131c27`, radius 8px.
  Header row: title "Onboarding eu-west-1" (12px/600 light) + right mono status
  "● running · step 5 / 7" with pulsing blue dot. Body: mono 11.5px/1.8 log lines —
  timestamp `#5b6a80`, completed lines prefixed `✓` in `#79c298`, current line prefixed `▸` in
  `#8cb4e0`. Keep the existing EventSource code writing into the body; max-height 300px, scroll.

### 7. Accounts (`accounts.html`) — root only
- Head: "Accounts" + subtitle "N users · root accounts manage regions, accounts and all deployments".
- Two columns (1.6fr 1fr):
  - Users table card: Username (avatar chip + name), Role (`root` accent badge / `user` neutral
    badge), Deployments count, Created, right-aligned small danger "Delete" (non-root rows only).
  - **Create account** panel: Username (mono), Password, Role as 2-segment control (user/root)
    with hint "Users only see and manage their own deployments.", full-width primary
    "Create account". Keep the existing fetch JS + `#acct-msg` feedback line.

### 8. Scan (`scan.html`)
- Head: "Scan AWS" + subtitle "Discovers appmesh-* resources across regions, groups them by
  deployment, and flags drift." Right: mono last-scan timestamp + primary "Run scan" (radar icon).
- Results grouped under mono uppercase section labels, each group a panel with a **3px left
  border**: green = healthy, amber = broken, red = orphans.
  - Group header: service name + region pill; right side either a green "complete · imported"
    badge or action buttons (secondary "Import", danger "Teardown"; orphans get "Clean up all").
  - Group body: hairline rows — icon, mono resource id, right-aligned mono `found` (green) /
    `missing` / `orphan DNS` / `orphan config` (red).
- Keep the SSE scan progress logic; while scanning you can reuse the dark terminal component
  from Regions.

## Interactions & Behavior
- All transitions: `.14s` ease on background/border/color. No entrance animations.
- Buttons hover: secondary → bg `#f8fafb`, border `#8b97a5`; primary → `#2f5c8e`;
  danger-secondary → bg `#fbecec`, border `#c03b3b`.
- Table rows: pointer cursor, hover `#f8fafb`, whole row navigates (keep `event.stopPropagation()`
  on inner links/buttons).
- In-flight statuses (`deploying`, `updating`, `tearing_down`) and terminal status dots pulse:
  `@keyframes pulse { 0%,100% {opacity:1} 50% {opacity:.3} }`, 1.4s infinite.
- Inputs focus: accent border + `0 0 0 3px #e8eff7` ring.
- Confirms/alerts: keep existing `confirm()`/`alert()` calls as-is (no modal system in scope).
- Role gating: sidebar ADMINISTER group, Owner column, and Regions/Accounts routes render only
  for `session.role == 'root'` — same conditionals as today, new markup.

## State Management
No new state. All data flows stay exactly as in the current app: Jinja context on render,
`fetch` for actions, `EventSource` (SSE) for deploy/update/teardown/scan/region-op streams,
periodic live-status polling on the detail page. The redesign is purely presentational.

## Design Tokens
Copy these as CSS custom properties (see `console-v3.css` `:root`-equivalent on `.am`):

**Colors**
| Token | Value | Use |
|---|---|---|
| `--bg` | `#f3f5f7` | app background |
| `--surface` | `#ffffff` | cards, panels, tables |
| `--raised` | `#f8fafb` | table headers, hovers, icon tiles |
| `--line` | `#e3e8ed` | hairlines, card borders |
| `--line-2` | `#d2dae1` | input/button borders, dashed borders |
| `--text` | `#16202b` | primary text |
| `--text-2` | `#5b6776` | secondary text |
| `--text-3` | `#8b97a5` | tertiary/labels |
| `--accent` | `#3a6ea8` | primary buttons, active states |
| `--accent-d` | `#2f5c8e` | primary hover |
| `--accent-soft` | `#e8eff7` | focus rings, active segments |
| `--green` / `--green-soft` | `#1f8a5b` / `#e6f3ec` | healthy/deployed |
| `--amber` / `--amber-soft` | `#9a7416` / `#f6f0dc` | in-progress |
| `--red` / `--red-soft` | `#c03b3b` / `#fbecec` | failed/danger |
| `--rail` / `--rail-2` | `#131c27` / `#1b2735` | sidebar bg / hover-active |
| `--rail-fg` / `--rail-fg-2` | `#aeb9c6` / `#6e7c8c` | sidebar text / muted |
| rail hairline | `rgba(255,255,255,.07)` | sidebar borders |
| sidebar active icon | `#7da7d4` | icon + avatar accent on dark |
| terminal bg/fg | `#131c27` / `#c7d1dc` | op logs (ok `#79c298`, run `#8cb4e0`, ts `#5b6a80`) |

**Typography** — Google Fonts: `Geist` (400/500/600/700) UI, `Geist Mono` (400/500/600) for all
machine data: ids, hostnames, region names, badges, counts, timestamps, logs, uppercase labels.
Base 14px/1.5. H1 20px/600/-.015em. Panel titles 13px/600. Table cells 13px. Badges 11px mono.
Uppercase micro-labels 10–10.5px, letter-spacing .06–.09em.

**Spacing & shape** — radii: panels/cards 8px, buttons/inputs 6px, small buttons/icon tiles 5–6px,
badges 4px. Buttons 32px tall (26px small). Inputs 34px. Panel padding 16px; head 11px 16px.
Card shadow: `0 1px 2px rgba(22,32,43,.03)`. Grid gaps 14px.

## Assets
- **Icons**: no icon font, no emoji. ~30 inline SVG line icons, 20×20 viewBox, `stroke-width:1.6`,
  `currentColor` — full set in `icons.js` (base) and the IIFE at the top of `console-v3-shell.jsx`
  (extras: users, radar, logout, lock, terminal, alert, listener-rule, import). Inline them as a
  Jinja macro/include or an SVG sprite. They replace ☁️ 🚀 🌐 etc.
- **Fonts**: Google Fonts link for Geist + Geist Mono (replaces Inter + JetBrains Mono).
- Favicon: replace the ☁️ emoji favicon with a simple accent-blue rounded square + white
  layers glyph (SVG data URI).

## Files
| File | What it is |
|---|---|
| `AppMesh Console v3.html` | Entry point — open in a browser to click through all screens |
| `console-v3.css` | **The design system.** Closest thing to the target `style.css`; scoped under `.am` (strip the prefix when porting) |
| `console-v3-shell.jsx` | Sidebar nav, status badge, region pill, extra icons, mock data |
| `console-v2-screens-a.jsx` | Dashboard + Deployment Detail screen markup |
| `console-v2-screens-b.jsx` | Regions, Accounts, New Deployment, Scan, Login screen markup |
| `icons.js` | Base icon set (SVG strings) |
| `tweaks-panel.jsx` | Design-review tool only — **do not port** |

## Suggested implementation order
1. `static/css/style.css` — port `console-v3.css` (strip `.am` scoping), add Geist fonts.
2. `base.html` — sidebar shell + role-gated nav; verify every page still renders.
3. `index.html` (dashboard + regions strip) and `login.html` — quick wins.
4. `regions.html`, `accounts.html`, `new_deployment.html`.
5. `deployment_detail.html` — largest; preserve all ids/SSE/polling JS, restyle around it.
6. `scan.html`.
Test each step against a running console; the SSE flows (deploy, region onboard, scan) are the
regression hot spots.
