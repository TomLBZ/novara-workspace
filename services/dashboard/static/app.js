const $ = (id) => document.getElementById(id);
let refreshMs = 5000;

function fmtUptime(s) {
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return [d ? d + "d" : null, h ? h + "h" : null, m + "m"].filter(Boolean).join(" ");
}

async function pingPublic(url) {
  const t0 = performance.now();
  try {
    const r = await fetch(url + "/healthz", { cache: "no-store" });
    const ms = Math.round(performance.now() - t0);
    return r.ok ? { text: ms + " ms (200)", ok: true } : { text: ms + " ms (HTTP " + r.status + ")", ok: false };
  } catch (e) {
    return { text: "unreachable", ok: false };
  }
}

function rows(tableId, data, cells) {
  const tb = $(tableId).querySelector("tbody");
  const span = $(tableId).querySelectorAll("thead th").length || 4;
  tb.innerHTML = data.map((row) => "<tr>" + cells(row).map((c) => "<td>" + c + "</td>").join("") + "</tr>").join("")
    || "<tr><td colspan='" + span + "' class='dim'>none</td></tr>";
}

/* Links are only ever rendered from what a service itself declares
 * (`/api/status` → links/api_links/endpoints, sourced from that service's own
 * `GET <prefix>/api/routes`). Empty list → "" so the caller can show a dash. */
function linkList(items) {
  return (items || []).map((l) =>
    "<a href='" + esc(l.path) + "' title='" + esc(l.full_label || l.label) + "'>" + esc(l.label) + "</a>"
  ).join(" · ");
}

/* Collapsed by default: service/project link lists are detail, never the list's main visual. */
function fold(summary, items) {
  if (!(items || []).length) return "";
  return "<details><summary>" + esc(summary + " " + items.length) + "</summary>" +
    "<div class='links dim'>" + linkList(items) + "</div></details>";
}

function serviceDetail(s) {
  const apis = s.api_links || [];
  const out = [fold("接口", apis) || "<span class='dim'>—</span>"];
  // why the endpoint list is empty (no-prefix / routes-unreachable / none) — reported, not guessed.
  // Only for services that have their own entry prefix: without one there was nothing to read.
  if (!apis.length && s.url) out.push("<div class='dim'>" + esc(s.links_source || "none") + "</div>");
  return out.join("");
}

function badge(text, cls) {
  return "<span class='badge" + (cls ? " " + cls : "") + "'>" + esc(text) + "</span>";
}

/* Projects & routes: one entry per independently running webui app.
 * name/prefix/sub-pages all come from that app's own `/api/routes` self-declaration
 * (see collect_projects in dashboard.py) — nothing is hardcoded here. */
function projectCard(p) {
  const isApp = p.kind === "webui-app";
  const head = "<div class='proj-head'>" +
    "<a class='proj-entry' href='" + esc(p.entry) + "'>" + esc(p.name) + "</a>" +
    badge(isApp ? "独立 app" : "入口未自述", isApp ? "app" : "warn") +
    badge("入口 " + p.entry, "path") +
    (p.healthy ? "<span class='ok'>up</span>" : "<span class='bad'>down</span>") +
    "</div>";
  const meta = "<div class='proj-meta dim'>service " + esc(p.service) +
    " · port " + (p.ports || []).join(",") +
    (p.declared_by ? " · 自述者 " + esc(p.declared_by) : "") +
    " · 入口来源 " + esc(p.entry_source || "—") +
    " · 名称来源 " + esc(p.name_source || "—") +
    " · routes " + esc(p.routes_source || "—") +
    ((p.views || []).length ? " · 视角 " + esc(p.views.join("/")) : "") + "</div>";
  return "<div class='proj'>" + head + meta +
    (isApp ? "" : "<div class='dim'>入口数据拿不到（" + esc(p.routes_source || "none") + "）— 不猜不编</div>") +
    fold("子页面", p.entries) + fold("接口", p.apis) + "</div>";
}

function renderProjects(projects) {
  if (!Array.isArray(projects)) {
    $("projects").innerHTML = "<div class='dim'>this page expects <code>projects[]</code> in /api/status — the API returned none (stale build?)</div>";
    return;
  }
  $("projects").innerHTML = projects.map(projectCard).join("") ||
    "<div class='dim'>no independently running webui app found (a candidate needs its own gateway prefix and a route table it declares itself)</div>";
}

async function load() {
  try {
    const r = await fetch("/api/status", { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const d = await r.json();
    $("error").hidden = true;
    refreshMs = (d.public.refresh_seconds || 5) * 1000;

    const healthy = d.services.every((s) => s.healthy);
    $("dot").className = "dot " + (healthy ? "ok" : "bad");

    $("public-link").textContent = d.public.url;
    $("public-link").href = d.public.url;
    $("pub-url").textContent = d.public.url;
    const st = $("pub-state");
    st.textContent = d.public.state || "unknown";
    st.className = /^UP/.test(d.public.state || "") ? "ok" : "bad";
    if (document.visibilityState === "visible") {
      const p = await pingPublic(location.origin);
      $("pub-latency").textContent = p.text;
      $("pub-latency").className = p.ok ? "ok" : "bad";
    }

    rows("services", d.services, (s) => [
      "<b>" + esc(s.name) + "</b>" + (s.description
        ? "<br><span class='dim'>" + esc(s.description) + "</span>"
        : ""),
      s.pid ?? "<span class='dim'>—</span>", s.ports.join(", "),
      s.url ? "<a href='" + esc(s.url) + "'>" + esc(s.url) + "</a>" : "<span class='dim'>—</span>",
      s.healthy ? "<span class='ok'>up</span>" : "<span class='bad'>down</span>",
      serviceDetail(s)
    ]);
    renderProjects(d.projects);
    rows("routes", d.routes, (r) => [
      "<a href='" + r.link + "'>" + r.prefix + "</a>", r.type, r.target
    ]);
    rows("watchers", d.watchers, (w) => [
      w.name, w.schedule, (w.last_run_at || "—") + "", w.last_status || "—"
    ]);

    const s = d.system;
    $("system").innerHTML = [
      ["container", s.container], ["python", s.python], ["host uptime", fmtUptime(s.uptime_h * 3600)],
      ["load", s.load.join(" ")], ["memory", (s.mem_avail_mb ?? "?") + " / " + (s.mem_total_mb ?? "?") + " MB free"],
      ["/workspace disk", s.workspace_gb_free + " / " + s.workspace_gb_total + " GB free"],
      ["git rev", s.git_rev || "—"], ["dashboard uptime", fmtUptime(d.uptime_s)]
    ].map(([k, v]) => "<div><span>" + k + "</span><b>" + v + "</b></div>").join("");

    $("requests").textContent = d.recent_requests.join("\n") || "no requests logged yet";
    $("updated").textContent = "updated " + new Date().toLocaleTimeString() + " · auto " + (refreshMs / 1000) + "s";
  } catch (e) {
    $("error").hidden = false;
    $("error").textContent = "dashboard API unreachable: " + e.message + " (retrying…)";
    $("dot").className = "dot bad";
  }
}

$("refresh").addEventListener("click", load);
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") load(); });
load();
setInterval(load, refreshMs);
setTimeout(() => { load(); }, 2500);

/* ---------------------------------------------------------------------------
 * Files: read-only browser (anonymous scope = files.root, admin = workspace)
 * ------------------------------------------------------------------------- */
const FILES = { path: "", token: localStorage.getItem("ws-files-token") || "", scope: "root", root: "projects" };
const IMG_EXT = [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico"];

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmtSize(n) {
  if (n === null || n === undefined) return "—";
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  return (n / 1048576).toFixed(1) + " MB";
}
function fmtTime(ts) { return ts ? new Date(ts * 1000).toLocaleString() : "—"; }

function setToken(t) {
  FILES.token = t || "";
  if (t) localStorage.setItem("ws-files-token", t); else localStorage.removeItem("ws-files-token");
  $("files-lock").hidden = !t;
}

async function filesFetch(url) {
  const opts = { cache: "no-store", headers: {} };
  if (FILES.token) opts.headers["X-WS-Files-Token"] = FILES.token;
  const r = await fetch(url, opts);
  if (r.status === 401) { setToken(""); throw new Error("token rejected — paste it again"); }
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || ("HTTP " + r.status));
  return d;
}

async function filesLoad(path) {
  const target = path === undefined ? FILES.path : path;
  try {
    const d = await filesFetch("/api/files/list?path=" + encodeURIComponent(target));
    FILES.path = d.path || "";
    FILES.scope = d.scope || "root";
    FILES.root = d.browse_root || "projects";
    $("files-path").textContent = "/" + FILES.path + (FILES.path ? "" : "  (" + FILES.root + ")");
    const chip = $("files-scope");
    chip.textContent = d.authenticated ? "admin · workspace" : "anonymous · " + FILES.root;
    chip.className = "scope-chip" + (d.authenticated ? " admin" : "");
    $("files-up").disabled = FILES.path === "";
    const body = d.entries.map((e) => (
      '<tr class="' + (e.denied ? "denied" : e.type) + '" data-name="' + esc(e.name) + '" data-type="' + e.type + '">' +
      "<td>" + (e.type === "dir" ? "▸ " : "") + esc(e.name) + (e.denied ? ' <span class="dim">denied</span>' : "") + "</td>" +
      "<td>" + (e.symlink ? "link → " : "") + e.type + "</td>" +
      "<td>" + (e.type === "dir" ? "—" : fmtSize(e.size)) + "</td>" +
      "<td>" + fmtTime(e.mtime) + "</td></tr>"
    )).join("") || '<tr><td colspan="4" class="dim">empty</td></tr>';
    $("files-list").querySelector("tbody").innerHTML = body +
      (d.truncated ? '<tr><td colspan="4" class="dim">… listing truncated</td></tr>' : "");
  } catch (e) {
    $("files-preview-name").textContent = "listing failed";
    $("files-preview-meta").textContent = "";
    $("files-preview").textContent = e.message;
  }
}

async function filesPreview(name) {
  const rel = FILES.path ? FILES.path + "/" + name : name;
  const ext = name.slice(name.lastIndexOf(".")).toLowerCase();
  $("files-preview-name").textContent = rel;
  $("files-preview-meta").textContent = "";
  $("files-preview-media").innerHTML = "";
  try {
    if (IMG_EXT.includes(ext)) {
      const headers = FILES.token ? { "X-WS-Files-Token": FILES.token } : {};
      const r = await fetch("/api/files/raw?path=" + encodeURIComponent(rel), { cache: "no-store", headers });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const blob = await r.blob();
      const img = document.createElement("img");
      img.src = URL.createObjectURL(blob);
      img.alt = rel;
      $("files-preview-media").appendChild(img);
      $("files-preview").textContent = "";
      $("files-preview-meta").textContent = fmtSize(blob.size) + " · inline";
      return;
    }
    const d = await filesFetch("/api/files/read?path=" + encodeURIComponent(rel));
    $("files-preview-meta").textContent = fmtSize(d.size) +
      (d.truncated ? " · first " + fmtSize(d.limit) + " shown" : "");
    $("files-preview").textContent = d.kind === "binary" ? (d.note || "binary file") : (d.content || "(empty file)");
  } catch (e) {
    $("files-preview").textContent = "cannot preview: " + e.message;
  }
}

$("files-list").addEventListener("click", (ev) => {
  const row = ev.target.closest("tr[data-name]");
  if (!row || row.classList.contains("denied")) return;
  const name = row.dataset.name;
  if (row.dataset.type === "dir") filesLoad(FILES.path ? FILES.path + "/" + name : name);
  else filesPreview(name);
});
$("files-up").addEventListener("click", () => filesLoad(FILES.path.includes("/") ? FILES.path.replace(/\/[^/]*$/, "") : ""));
$("files-reload").addEventListener("click", () => filesLoad());
$("files-unlock").addEventListener("click", async () => {
  const token = $("files-token").value.trim();
  if (!token) return;
  try {
    const r = await fetch("/api/files/unlock", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token })
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error((d.error || "HTTP " + r.status) +
      (d.attempts_left !== undefined ? " — " + d.attempts_left + " attempts left" : ""));
    setToken(token);
    $("files-token").value = "";
    filesLoad("");
  } catch (e) {
    $("files-preview-name").textContent = "unlock failed";
    $("files-preview").textContent = e.message;
  }
});
$("files-token").addEventListener("keydown", (ev) => { if (ev.key === "Enter") $("files-unlock").click(); });
$("files-lock").addEventListener("click", () => { setToken(""); filesLoad(""); });
setToken(FILES.token);
filesLoad("");
