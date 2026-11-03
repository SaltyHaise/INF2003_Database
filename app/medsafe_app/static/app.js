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
    input.addEventListener("input", () => {
      if (hidden) hidden.value = "";
      clearTimeout(timer);
      const q = input.value.trim();
      if (q.length < 2) { list.hidden = true; return; }
      timer = setTimeout(async () => {
        const my = ++seq;
        try {
          const r = await fetch(box.dataset.src + "?q=" + encodeURIComponent(q));
          if (my !== seq) return;
          items = await r.json(); active = items.length ? 0 : -1; render();
        } catch (e) { list.hidden = true; }
      }, 150);
    });
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

  // Prescription form: end date preview + custom dose fields
  const rxForm = document.getElementById("rx-form");
  if (rxForm) {
    const start = rxForm.querySelector("[name=start]"), days = rxForm.querySelector("[name=days]"), out = document.getElementById("end-preview");
    const upd = () => {
      if (!start.value) { out.textContent = "—"; return; }
      const n = parseInt(days.value, 10);
      if (!n) { out.textContent = "Ongoing"; return; }
      const d = new Date(start.value + "T00:00"); d.setDate(d.getDate() + n - 1);
      out.textContent = d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
    };
    start.addEventListener("input", upd); days.addEventListener("input", upd); upd();
    const custom = document.getElementById("custom-fields");
    const sync = () => { const c = rxForm.querySelector("[name=template]:checked"); custom.hidden = !(c && c.value === "custom"); };
    rxForm.querySelectorAll("[name=template]").forEach(r => r.addEventListener("change", sync)); sync();
  }

  // Review form: approve needs all checks
  const rv = document.getElementById("review-form");
  if (rv) {
    const boxes = rv.querySelectorAll("input[type=checkbox]"), ap = rv.querySelector("[value=approve]");
    const sync = () => { ap.disabled = ![...boxes].every(b => b.checked); };
    boxes.forEach(b => b.addEventListener("change", sync)); sync();
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
