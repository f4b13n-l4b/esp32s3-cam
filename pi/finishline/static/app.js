// Interface bénévoles — JavaScript sans dépendance.
// Sécurité : tout le contenu dynamique est inséré via textContent (pas d'innerHTML),
// le code d'accès est gardé en sessionStorage (effacé à la fermeture de l'onglet).
"use strict";

const $ = (sel) => document.querySelector(sel);
const POLL_MS = 1000;
let token = "";
let state = null;
let pollTimer = null;
let activeTab = "saisie";

// ---------------------------------------------------------------- utilitaires

function storageGet(key) {
  try { return sessionStorage.getItem(key) || ""; } catch { return ""; }
}
function storageSet(key, value) {
  try { value ? sessionStorage.setItem(key, value) : sessionStorage.removeItem(key); } catch { /* navigation privée */ }
}

async function api(path, { method = "GET", body, raw = false } = {}) {
  const res = await fetch(path, {
    method,
    headers: { "X-Token": token, ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
    cache: "no-store",
  });
  if (res.status === 401) {
    logout("Code d'accès refusé");
    throw new Error("401");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* corps non JSON */ }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return raw ? res : res.json();
}

let toastTimer = null;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 3500);
}

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") node.className = v;
    else if (k === "on") for (const [ev, fn] of Object.entries(v)) node.addEventListener(ev, fn);
    else if (k in node) node[k] = v;
    else node.setAttribute(k, v);
  }
  for (const c of children) if (c != null) node.append(c);
  return node;
}

async function guarded(fn) {
  try { await fn(); await refresh(); } catch (e) { if (e.message !== "401") toast(e.message); }
}

// ---------------------------------------------------------------- connexion

function logout(msg) {
  token = "";
  storageSet("token", "");
  clearInterval(pollTimer);
  $("#app").hidden = true;
  $("#login").hidden = false;
  $("#login-error").textContent = msg || "";
}

async function login(t) {
  token = t;
  try {
    await api("/api/state");
  } catch (e) {
    if (e.message !== "401") $("#login-error").textContent = e.message;
    return;
  }
  storageSet("token", t);
  $("#login").hidden = true;
  $("#app").hidden = false;
  startPolling();
  $("#bib").focus();
}

// ---------------------------------------------------------------- rafraîchissement

function startPolling() {
  clearInterval(pollTimer);
  refresh();
  pollTimer = setInterval(() => { if (!document.hidden) refresh(); }, POLL_MS);
}

async function refresh() {
  try {
    state = await api("/api/state");
  } catch (e) {
    if (e.message !== "401") $("#cam-status").textContent = "Serveur injoignable";
    return;
  }
  render();
}

function render() {
  const s = state;
  $("#clock").textContent = (s.server_time || "").slice(0, 8);
  renderCamera(s.camera);

  if (!s.race) {
    $("#race-name").textContent = "Aucune course : onglet « Course »";
    return;
  }
  const r = s.race;
  $("#race-name").textContent = `${r.name}${r.started ? " — départ " + r.start.slice(0, 8) : " — non démarrée"}${r.closed ? " (clôturée)" : ""}`;
  $("#start-current").textContent = r.started ? r.start : "non donné";
  $("#close-race").textContent = r.closed ? "Rouvrir la course" : "Clôturer la course";

  const delta = s.counts.passages - s.counts.bibs;
  $("#n-passages").textContent = s.counts.passages;
  $("#n-bibs").textContent = s.counts.bibs;
  $("#n-delta").textContent = Math.abs(delta);
  $("#delta-label").textContent = delta > 0 ? "dossards à saisir" : delta < 0 ? "passages manquants" : "écart";
  $("#delta-box").classList.toggle("warn", delta !== 0);

  if (activeTab === "saisie") renderLastBibs(s);
  if (activeTab === "controle") renderControl(s);
}

function renderCamera(c) {
  const st = $("#cam-status");
  st.className = "status";
  if (!c.connected) {
    st.textContent = "Caméra déconnectée";
  } else if (!c.clock_ok) {
    st.textContent = `Horloge caméra décalée (${c.clock_skew_ms} ms)`;
    st.classList.add("warn");
  } else {
    st.textContent = `Caméra OK · ${c.fps} i/s`;
    st.classList.add("ok");
  }
  if (activeTab === "reglage") {
    const dl = $("#cam-details");
    const rows = [
      ["Connexion", c.connected ? "oui" : "non"],
      ["Images/s", c.fps],
      ["Décalage horloge", c.clock_skew_ms == null ? "—" : `${c.clock_skew_ms} ms`],
      ["Occupation de la ligne", c.occupancy == null ? "—" : `${Math.round(c.occupancy * 100)} %`],
      ["Coureurs détectés (session)", c.detector_total],
      ["Dernière erreur", c.last_error || "aucune"],
    ];
    dl.replaceChildren(...rows.flatMap(([k, v]) => [el("dt", { textContent: k }), el("dd", { textContent: String(v) })]));
  }
}

function renderLastBibs(s) {
  const list = $("#last-bibs");
  const entries = s.bibs.filter((b) => !b.voided);
  const rankOf = new Map(entries.map((b, i) => [b.id, i + 1]));
  const last = s.bibs.slice(-8).reverse();
  list.replaceChildren(
    ...last.map((b) =>
      el("li", { class: b.voided ? "voided" : "" },
        el("span", { class: "rank", textContent: b.voided ? "—" : ordinal(rankOf.get(b.id)) }),
        el("span", { class: "bibnum", textContent: b.bib }),
        b.voided
          ? el("button", { type: "button", class: "secondary small", textContent: "Rétablir",
              on: { click: () => guarded(() => api(`/api/bibs/${b.id}/void`, { method: "POST", body: { voided: false } })) } })
          : el("button", { type: "button", class: "danger small", textContent: "Annuler",
              "aria-label": `Annuler le dossard ${b.bib}`,
              on: { click: () => guarded(() => api(`/api/bibs/${b.id}/void`, { method: "POST", body: { voided: true } })) } }),
      ),
    ),
  );
}

function ordinal(n) {
  return n === 1 ? "1er" : `${n}e`;
}

const FLAG_LABELS = {
  dossard_manquant: "dossard à saisir",
  temps_manquant: "sans temps",
  doublon: "doublon",
  dossard_inconnu: "inconnu",
};

function renderControl(s) {
  const w = $("#warnings");
  w.replaceChildren(
    ...(s.warnings.length ? s.warnings.map((m) => el("li", { textContent: m }))
      : [el("li", { class: "ok", textContent: "Aucune anomalie : passages et dossards concordent." })]),
  );

  const entries = s.bibs.filter((b) => !b.voided);
  const tbody = $("#ranking");
  tbody.replaceChildren(
    ...s.ranking.map((r) => {
      const i = entries.findIndex((b) => b.id === r.bib_entry_id);
      const up = i > 0 ? entries[i - 1].id : undefined;
      const downBefore = i >= 0 && i < entries.length - 1 ? (entries[i + 2] ? entries[i + 2].id : null) : undefined;
      const actions = el("td", { class: "actions" });
      if (up !== undefined) {
        actions.append(el("button", { type: "button", class: "secondary small", textContent: "▲", "aria-label": `Remonter ${r.bib}`,
          on: { click: () => guarded(() => api(`/api/bibs/${r.bib_entry_id}/move`, { method: "POST", body: { before_id: up } })) } }));
      }
      if (downBefore !== undefined) {
        actions.append(el("button", { type: "button", class: "secondary small", textContent: "▼", "aria-label": `Descendre ${r.bib}`,
          on: { click: () => guarded(() => api(`/api/bibs/${r.bib_entry_id}/move`, { method: "POST", body: { before_id: downBefore } })) } }));
      }
      if (r.passage_id) {
        actions.append(el("button", { type: "button", class: "secondary small", textContent: "Photo",
          on: { click: () => openPhotos(r.passage_id) } }));
      }
      const bibCell = el("td", { textContent: r.bib || "?" });
      for (const f of r.flags) bibCell.append(el("span", { class: "flag", textContent: `⚠ ${FLAG_LABELS[f] || f}` }));
      return el("tr", { class: r.flags.length ? "flagged" : "" },
        el("td", { textContent: r.rank }),
        bibCell,
        el("td", { textContent: [r.name, r.categorie].filter(Boolean).join(" · ") }),
        el("td", { textContent: r.elapsed || "—" }),
        actions,
      );
    }),
  );

  const pbody = $("#passages");
  pbody.replaceChildren(
    ...[...s.passages].reverse().map((p) =>
      el("tr", { class: p.voided ? "voided" : "" },
        el("td", { textContent: p.time }),
        el("td", { textContent: p.elapsed || "—" }),
        el("td", { textContent: p.count }),
        el("td", { textContent: p.source }),
        el("td", { class: "actions" },
          el("button", { type: "button", class: "secondary small", textContent: "−1", "aria-label": "Retirer un coureur", disabled: p.voided || p.count <= 1,
            on: { click: () => guarded(() => api(`/api/passages/${p.id}`, { method: "PATCH", body: { count: p.count - 1 } })) } }),
          el("button", { type: "button", class: "secondary small", textContent: "+1", "aria-label": "Ajouter un coureur", disabled: p.voided,
            on: { click: () => guarded(() => api(`/api/passages/${p.id}`, { method: "PATCH", body: { count: p.count + 1 } })) } }),
          el("button", { type: "button", class: p.voided ? "secondary small" : "danger small", textContent: p.voided ? "Rétablir" : "Faux positif",
            on: { click: () => guarded(() => api(`/api/passages/${p.id}`, { method: "PATCH", body: { voided: !p.voided } })) } }),
          p.photo ? el("button", { type: "button", class: "secondary small", textContent: "Photo", on: { click: () => openPhotos(p.id) } }) : null,
        ),
      ),
    ),
  );
}

// ---------------------------------------------------------------- photos

let photoUrls = [];
let photoFrames = [];
let photoIndex = 0;
let photoPassage = null;

async function openPhotos(pid) {
  try {
    const info = await api(`/api/passages/${pid}/frames`);
    if (!info.frames.length) { toast("Photos pas encore enregistrées (patienter 2 s)"); return; }
    photoPassage = pid;
    photoFrames = info.frames;
    photoIndex = Math.max(0, photoFrames.findIndex((f) => f.name === info.photo));
    await showPhoto();
    $("#photo-dialog").showModal();
  } catch (e) { if (e.message !== "401") toast(e.message); }
}

async function showPhoto() {
  const f = photoFrames[photoIndex];
  const res = await api(`/api/passages/${photoPassage}/frames/${f.name}`, { raw: true });
  const url = URL.createObjectURL(await res.blob());
  photoUrls.push(url);
  $("#photo-main").src = url;
  const sign = f.delta_ms >= 0 ? "+" : "";
  $("#photo-info").textContent = `${photoIndex + 1}/${photoFrames.length} · ${f.time} (${sign}${f.delta_ms} ms)`;
  $("#photo-prev").disabled = photoIndex === 0;
  $("#photo-next").disabled = photoIndex === photoFrames.length - 1;
}

// ---------------------------------------------------------------- aperçu caméra

let previewTimer = null;
let previewUrl = null;
async function refreshPreview() {
  try {
    const res = await api("/api/preview.jpg", { raw: true });
    const url = URL.createObjectURL(await res.blob());
    $("#preview").src = url;
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = url;
  } catch { /* pas encore d'image */ }
}

// ---------------------------------------------------------------- onglets

function selectTab(name) {
  activeTab = name;
  for (const b of document.querySelectorAll(".tabs button")) b.setAttribute("aria-selected", String(b.dataset.tab === name));
  for (const p of document.querySelectorAll(".panel")) p.hidden = p.id !== `tab-${name}`;
  clearInterval(previewTimer);
  if (name === "reglage") { refreshPreview(); previewTimer = setInterval(refreshPreview, 1000); }
  if (name === "saisie") $("#bib").focus();
  if (state) render();
}

// ---------------------------------------------------------------- événements

function init() {
  $("#login-form").addEventListener("submit", (e) => { e.preventDefault(); login($("#token").value); });
  for (const b of document.querySelectorAll(".tabs button")) b.addEventListener("click", () => selectTab(b.dataset.tab));

  const bib = $("#bib");
  document.querySelector(".keypad").addEventListener("click", (e) => {
    const key = e.target.closest("button")?.dataset.key;
    if (!key) return;
    bib.value = key === "back" ? bib.value.slice(0, -1) : (bib.value + key).slice(0, 8);
    bib.focus();
  });
  $("#bib-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const v = bib.value.trim();
    if (!v) return;
    bib.value = "";
    guarded(() => api("/api/bibs", { method: "POST", body: { bib: v } }));
    bib.focus();
  });

  $("#manual-passage").addEventListener("click", () =>
    guarded(async () => { await api("/api/passages", { method: "POST", body: { count: 1 } }); toast("Passage manuel ajouté"); }));

  $("#race-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const name = $("#race-input").value.trim();
    if (state?.race && !state.race.closed && !confirm(`Créer « ${name} » clôturera « ${state.race.name} ». Continuer ?`)) return;
    guarded(async () => { await api("/api/race", { method: "POST", body: { name } }); $("#race-input").value = ""; toast("Course créée"); });
  });
  $("#start-now").addEventListener("click", () => {
    if (state?.race?.started && !confirm("Le départ est déjà donné. Le remplacer par maintenant ?")) return;
    guarded(async () => { await api("/api/race/start", { method: "POST", body: {} }); toast("Départ enregistré"); });
  });
  $("#start-form").addEventListener("submit", (e) => {
    e.preventDefault();
    guarded(async () => { await api("/api/race/start", { method: "POST", body: { time: $("#start-time").value } }); toast("Heure de départ corrigée"); });
  });
  $("#close-race").addEventListener("click", () => {
    const closing = !state?.race?.closed;
    guarded(() => api("/api/race/close", { method: "POST", body: { closed: closing } }));
  });
  $("#export").addEventListener("click", async () => {
    try {
      const res = await api("/api/export.csv", { raw: true });
      const url = URL.createObjectURL(await res.blob());
      const name = /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") || "")?.[1] || "classement.csv";
      el("a", { href: url, download: name }).click();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
    } catch (e) { if (e.message !== "401") toast(e.message); }
  });

  $("#photo-prev").addEventListener("click", () => { photoIndex--; showPhoto(); });
  $("#photo-next").addEventListener("click", () => { photoIndex++; showPhoto(); });
  $("#photo-dialog").addEventListener("close", () => { photoUrls.forEach(URL.revokeObjectURL); photoUrls = []; });

  // Reconnexion automatique avec le code mémorisé pour cet onglet
  const saved = storageGet("token");
  if (saved) login(saved); else logout();
}

init();
