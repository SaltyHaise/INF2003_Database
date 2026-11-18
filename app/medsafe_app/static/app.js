// MedSafe: autocomplete boxes and small page helpers (no libraries).
//
// Autocomplete markup:
//   <div class="ac" data-src="/api/drugs" data-target="drug_id" data-go="...optional url with {id}">
//     <input type="text"> <input type="hidden" name="drug_id">
//   </div>
(function () {
  function esc(s) { return String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
  function highlight(label, q) {
    const i = label.toLowerCase().indexOf(q.toLowerCase());
    if (i < 0 || !q) return esc(label);
    return esc(label.slice(0, i)) + "<mark>" + esc(label.slice(i, i + q.length)) + "</mark>" + esc(label.slice(i + q.length));
  }

  document.querySelectorAll(".ac").forEach(box => {
    const input = box.querySelector("input[type=text], input[type=search]");
    const hidden = box.querySelector("input[type=hidden]");
    const list = document.createElement("div");
    list.className = "ac-list"; list.hidden = true; list.setAttribute("role", "listbox");
    box.appendChild(list);
    let items = [], active = -1, timer = null, seq = 0;

    function choose(it) {
      input.value = it.label;
      if (hidden) hidden.value = it.id;
      list.hidden = true;
      if (box.dataset.go) { window.location = box.dataset.go.replace("{id}", encodeURIComponent(it.id)); return; }
      box.dispatchEvent(new CustomEvent("ac:chosen", { detail: it, bubbles: true }));
    }
    function render() {
      list.innerHTML = items.map((it, i) =>
        `<div class="ac-item${i === active ? " on" : ""}" role="option" data-i="${i}"><span>${highlight(it.label, input.value.trim())}</span><small>${esc(it.sub || "")}</small></div>`).join("")
        || `<div class="ac-item"><small>No matches</small></div>`;
      list.hidden = false;
    }
    input.setAttribute("autocomplete", "off");
    const listOnEmpty = box.dataset.min === "0";   // e.g. the patient's own conditions on click
    function lookup(q) {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const my = ++seq;
        try {
          const src = box.dataset.src;
          const r = await fetch(src + (src.includes("?") ? "&" : "?") + "q=" + encodeURIComponent(q));
          if (my !== seq) return;
          items = await r.json(); active = items.length ? 0 : -1;
          if (items.length || q) render(); else list.hidden = true;
        } catch (e) { list.hidden = true; }
      }, q ? 150 : 0);
    }
    input.addEventListener("input", () => {
      if (hidden) hidden.value = "";
      const q = input.value.trim();
      if (q.length < 2) { if (listOnEmpty && !q) lookup(""); else { clearTimeout(timer); list.hidden = true; } return; }
      lookup(q);
    });
    if (listOnEmpty) input.addEventListener("focus", () => { if (!input.value.trim()) lookup(""); });
    input.addEventListener("keydown", e => {
      if (list.hidden) return;
      if (e.key === "ArrowDown") { active = Math.min(items.length - 1, active + 1); render(); e.preventDefault(); }
      else if (e.key === "ArrowUp") { active = Math.max(0, active - 1); render(); e.preventDefault(); }
      else if (e.key === "Enter" && active >= 0 && items[active]) { choose(items[active]); e.preventDefault(); }
      else if (e.key === "Escape") { list.hidden = true; }
    });
    list.addEventListener("mousedown", e => {
      const el = e.target.closest(".ac-item[data-i]");
      if (el) { e.preventDefault(); choose(items[+el.dataset.i]); }
    });
    input.addEventListener("blur", () => setTimeout(() => { list.hidden = true; }, 120));
  });

  // Clickable table rows: <tr class="click" data-href="...">
  document.querySelectorAll("tr[data-href]").forEach(tr =>
    tr.addEventListener("click", e => { if (!e.target.closest("a, button, input, label")) window.location = tr.dataset.href; }));

  // Chip filters: <div class="filters" data-for="#list"><button data-k="all">...
  document.querySelectorAll(".filters[data-for]").forEach(f => {
    const rows = document.querySelectorAll(f.dataset.for + " [data-k]");
    f.addEventListener("click", e => {
      const b = e.target.closest("button"); if (!b) return;
      f.querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b));
      rows.forEach(r => { r.hidden = b.dataset.k !== "all" && r.dataset.k !== b.dataset.k; });
    });
  });

  // Prescription dose cards (one per medicine): end date preview + custom dose fields
  document.querySelectorAll(".rx-card").forEach(card => {
    const start = card.querySelector("[name^=start_]"), days = card.querySelector("[name^=days_]"),
          out = card.querySelector(".end-preview");
    const upd = () => {
      if (!start.value) { out.textContent = "—"; return; }
      const n = parseInt(days.value, 10);
      if (!n) { out.textContent = "Ongoing"; return; }
      const d = new Date(start.value + "T00:00"); d.setDate(d.getDate() + n - 1);
      out.textContent = d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
    };
    start.addEventListener("input", upd); days.addEventListener("input", upd); upd();
    const custom = card.querySelector(".custom-fields");
    const sync = () => { const c = card.querySelector("[name^=template_]:checked"); custom.hidden = !(c && c.value === "custom"); };
    card.querySelectorAll("[name^=template_]").forEach(r => r.addEventListener("change", sync)); sync();
  });

  // Review form: approve needs all checks
  const rv = document.getElementById("review-form");
  if (rv) {
    const boxes = rv.querySelectorAll("input[type=checkbox]"), ap = rv.querySelector("[value=approve]");
    const sync = () => { ap.disabled = ![...boxes].every(b => b.checked); };
    boxes.forEach(b => b.addEventListener("change", sync)); sync();
  }

  // Visit form: show findings only for the body parts ticked; "normal" and abnormal
  // findings for the same part exclude each other.
  const vf = document.getElementById("visit-form");
  if (vf) {
    const parts = [...vf.querySelectorAll("[name=exam_systems]")];
    const show = () => parts.forEach(t => { vf.querySelector(`[data-system="${t.value}"]`).hidden = !t.checked; });
    parts.forEach(t => t.addEventListener("change", show));
    vf.querySelectorAll(".exam-sys").forEach(box => box.addEventListener("change", e => {
      const cb = e.target;
      if (cb.type !== "checkbox" || !cb.checked) return;
      const isNormal = cb.value === "normal";
      box.querySelectorAll("input[type=checkbox]").forEach(o => {
        if (o !== cb && (o.value === "normal") !== isNormal) o.checked = false;
      });
    }));
    show();

    // Medicines follow the diagnosis: when the diagnosis or medicine changes, the page is
    // re-drawn by the server with everything typed so far (it never saves on refresh).
    const btn = document.getElementById("refresh-btn");
    const refresh = (anchor) => {
      btn.formAction = vf.getAttribute("action").split("#")[0] + anchor;
      btn.click();
    };
    const dxBox = document.getElementById("dx-box");
    if (dxBox) dxBox.addEventListener("ac:chosen", () => refresh("#medicines"));
    // Ticking a medicine adds its dose card; unticking (or Remove on the card) takes it away.
    vf.querySelectorAll("[name=drug_ids]").forEach(r => r.addEventListener("change", () =>
      refresh(r.checked ? "#rx-" + r.value : "#medicines")));
    vf.querySelectorAll("tr.pickrow").forEach(tr => tr.addEventListener("click", e => {
      const r = tr.querySelector("[name=drug_ids]");
      if (r && !e.target.closest("a, input, button, details")) { r.checked = !r.checked; r.dispatchEvent(new Event("change")); }
    }));
    vf.querySelectorAll("[data-untick]").forEach(b => b.addEventListener("click", () => {
      const r = vf.querySelector(`[name=drug_ids][value="${b.dataset.untick}"]`);
      if (r) { r.checked = false; r.dispatchEvent(new Event("change")); }
    }));
    const search = document.getElementById("drugsearch-box");
    if (search) search.addEventListener("ac:chosen", e => refresh("#rx-" + e.detail.id));

    // Advice: add how to take each ticked medicine and its common side effects
    const draft = document.getElementById("advice-draft");
    if (draft) draft.addEventListener("click", () => {
      const box = document.getElementById("v-advice");
      const lines = [...vf.querySelectorAll(".rx-card")].map(card => {
        const t = card.querySelector("[name^=template_]:checked");
        let how = "";
        if (t && t.value === "custom") {
          const v = n => card.querySelector(`[name^=${n}_]`).value;
          how = [v("dose_amount") && `${v("dose_amount")} ${v("dose_unit")}`, v("frequency")].filter(Boolean).join(", ");
        } else if (t) {
          const say = t.closest("label").querySelector("[data-say]");
          how = say ? say.dataset.say : "";
        }
        const days = card.querySelector("[name^=days_]");
        const drug = card.dataset.drug;
        let line = `${drug[0].toUpperCase()}${drug.slice(1)}: ${how || "as directed"}`;
        if (days && days.value) line += ` for ${days.value} days`;
        line += ".";
        if (card.dataset.se) line += ` Possible side effects: ${card.dataset.se}.`;
        return line;
      }).filter(l => !box.value.includes(l));
      if (!lines.length) return;
      box.value = [box.value.trim(), ...lines].filter(Boolean).join("\n");
      box.focus();
    });
  }

  // History form: show the fields for the chosen kind
  const hk = document.getElementById("hist-kind");
  if (hk) {
    const sync = () => document.querySelectorAll("[data-kind]").forEach(el => {
      el.hidden = !el.dataset.kind.split(" ").includes(hk.value);
    });
    hk.addEventListener("change", sync); sync();
  }
})();
