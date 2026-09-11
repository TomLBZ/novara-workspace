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
  tb.innerHTML = data.map((row) => "<tr>" + cells(row).map((c) => "<td>" + c + "</td>").join("") + "</tr>").join("")
    || "<tr><td colspan='4' class='dim'>none</td></tr>";
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
      s.name, s.pid ?? "<span class='dim'>—</span>", s.ports.join(", "),
      s.healthy ? "<span class='ok'>up</span>" : "<span class='bad'>down</span>"
    ]);
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
