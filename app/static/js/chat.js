/* Ledger assistant — one client for both surfaces.
 *
 * The conversation is never stored on the server, so this file owns it: the
 * message list lives in sessionStorage (survives navigating between pages,
 * gone when the tab closes) and is posted back with every turn. The server
 * signs it and refuses anything that has been edited, so the signature has to
 * travel with the history everywhere it goes.
 */
(function () {
  "use strict";

  /* ── SSE over fetch (EventSource cannot POST) ────────────────────────── */
  async function* sseEvents(response) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let split;
      while ((split = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, split);
        buffer = buffer.slice(split + 2);
        let name = null, data = "";
        for (const line of frame.split("\n")) {
          if (line.startsWith("event: ")) name = line.slice(7);
          else if (line.startsWith("data: ")) data += line.slice(6);
        }
        if (name) {
          try { yield [name, data ? JSON.parse(data) : {}]; }
          catch (e) { /* a partial frame is not worth killing the turn over */ }
        }
      }
    }
  }

  /* ── Minimal markdown ────────────────────────────────────────────────── */
  const esc = (s) => String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

  function inline(text) {
    return esc(text)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
               '<a href="$2" target="_blank" rel="noopener">$1</a>');
  }

  const charts = [];   // chart specs waiting to be drawn after insertion

  function markdown(src) {
    const lines = String(src || "").split("\n");
    let html = "", list = null, i = 0;

    const closeList = () => { if (list) { html += `</${list}>`; list = null; } };

    while (i < lines.length) {
      const line = lines[i];

      // Fenced block — ```chart gets rendered, anything else is code.
      const fence = line.match(/^```(\w*)/);
      if (fence) {
        const lang = fence[1];
        const body = [];
        i++;
        while (i < lines.length && !/^```/.test(lines[i])) body.push(lines[i++]);
        i++;
        closeList();
        if (lang === "chart") {
          const id = "chart_" + charts.length;
          try {
            charts.push({ id: id, spec: JSON.parse(body.join("\n")) });
            html += `<div class="chat-chart"><canvas id="${id}"></canvas></div>`;
          } catch (e) {
            html += `<pre><code>${esc(body.join("\n"))}</code></pre>`;
          }
        } else {
          html += `<pre><code>${esc(body.join("\n"))}</code></pre>`;
        }
        continue;
      }

      // Table: a header row followed by a |---|---| separator.
      if (/^\s*\|/.test(line) && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1] || "")) {
        closeList();
        const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
        const head = cells(line);
        i += 2;
        let rows = "";
        while (i < lines.length && /^\s*\|/.test(lines[i])) {
          rows += "<tr>" + cells(lines[i++]).map((c) => `<td>${inline(c)}</td>`).join("") + "</tr>";
        }
        html += '<div class="chat-table-wrap"><table><thead><tr>'
              + head.map((c) => `<th>${inline(c)}</th>`).join("")
              + `</tr></thead><tbody>${rows}</tbody></table></div>`;
        continue;
      }

      const heading = line.match(/^(#{1,6})\s+(.*)$/);
      if (heading) { closeList(); html += `<h3>${inline(heading[2])}</h3>`; i++; continue; }

      if (/^\s*([-*+])\s+/.test(line)) {
        if (list !== "ul") { closeList(); html += "<ul>"; list = "ul"; }
        html += `<li>${inline(line.replace(/^\s*[-*+]\s+/, ""))}</li>`; i++; continue;
      }
      if (/^\s*\d+[.)]\s+/.test(line)) {
        if (list !== "ol") { closeList(); html += "<ol>"; list = "ol"; }
        html += `<li>${inline(line.replace(/^\s*\d+[.)]\s+/, ""))}</li>`; i++; continue;
      }
      if (/^\s*>\s?/.test(line)) {
        closeList(); html += `<blockquote>${inline(line.replace(/^\s*>\s?/, ""))}</blockquote>`;
        i++; continue;
      }
      if (/^\s*(---+|\*\*\*+)\s*$/.test(line)) { closeList(); html += "<hr>"; i++; continue; }
      if (!line.trim()) { closeList(); i++; continue; }

      closeList();
      const para = [line];
      i++;
      while (i < lines.length && lines[i].trim() && !/^\s*([-*+>#]|\d+[.)]|\||```)/.test(lines[i])) {
        para.push(lines[i++]);
      }
      html += `<p>${inline(para.join(" "))}</p>`;
    }
    closeList();
    return html;
  }

  function drawCharts(scope) {
    if (typeof Chart === "undefined") { charts.length = 0; return; }
    const dark = document.body.classList.contains("dark");
    const palette = ["#6366F1", "#10B981", "#F59E0B", "#EF4444", "#3B82F6", "#8B5CF6"];
    charts.splice(0).forEach(function (c) {
      const el = (scope || document).querySelector("#" + c.id);
      if (!el) return;
      const spec = c.spec || {};
      const sets = (spec.datasets || []).map(function (d, n) {
        return Object.assign({
          backgroundColor: spec.type === "pie" ? palette : palette[n % palette.length],
          borderColor: palette[n % palette.length], borderWidth: 2, tension: 0.3,
        }, d);
      });
      try {
        new Chart(el, {
          type: spec.type || "bar",
          data: { labels: spec.labels || [], datasets: sets },
          options: {
            responsive: true, maintainAspectRatio: true, aspectRatio: 1.9,
            plugins: {
              legend: { display: sets.length > 1 || spec.type === "pie",
                        labels: { color: dark ? "#94A3B8" : "#64748B", boxWidth: 12 } },
              title: { display: !!spec.title, text: spec.title,
                       color: dark ? "#F1F5F9" : "#1E293B" },
            },
            scales: spec.type === "pie" ? {} : {
              x: { ticks: { color: dark ? "#64748B" : "#94A3B8" },
                   grid: { color: dark ? "#334155" : "#E2E8F0" } },
              y: { ticks: { color: dark ? "#64748B" : "#94A3B8" },
                   grid: { color: dark ? "#334155" : "#E2E8F0" } },
            },
          },
        });
      } catch (e) { /* a malformed spec should not break the message */ }
    });
  }

  /* ── Icons ───────────────────────────────────────────────────────────── */
  const ICON = {
    send: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>',
    save: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>',
    copy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
    warn: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
  };

  /* ── Panel ───────────────────────────────────────────────────────────── */
  function ChatPanel(root, options) {
    this.root = root;
    this.mode = options.mode;                       // "helper" | "chat"
    this.storeKey = "ledgerChat:" + this.mode;
    this.suggestions = options.suggestions || [];
    this.emptyTitle = options.emptyTitle || "Ask about your data";
    this.emptyBody = options.emptyBody || "";
    this.history = [];
    this.sig = null;
    this.pending = null;
    this.busy = false;
    this.restore();
    this.build();
    this.render();
  }

  ChatPanel.prototype.restore = function () {
    try {
      const raw = sessionStorage.getItem(this.storeKey);
      if (!raw) return;
      const saved = JSON.parse(raw);
      this.history = saved.history || [];
      this.sig = saved.sig || null;
      this.pending = saved.pending || null;
    } catch (e) { this.history = []; }
  };

  ChatPanel.prototype.persist = function () {
    try {
      sessionStorage.setItem(this.storeKey, JSON.stringify({
        history: this.history, sig: this.sig, pending: this.pending,
      }));
    } catch (e) { /* private mode, or full: the chat still works, it just won't survive */ }
  };

  ChatPanel.prototype.clear = function () {
    this.history = []; this.sig = null; this.pending = null;
    try { sessionStorage.removeItem(this.storeKey); } catch (e) {}
    this.render();
  };

  ChatPanel.prototype.build = function () {
    this.root.innerHTML =
      '<div class="chat-thread"></div>' +
      '<div class="chat-composer">' +
      '  <div class="chat-error" style="display:none"></div>' +
      '  <div class="chat-composer-row">' +
      '    <textarea class="chat-input" rows="1" placeholder="Ask about your data…"></textarea>' +
      '    <button class="chat-send" title="Send">' + ICON.send + "</button>" +
      "  </div>" +
      '  <div class="chat-foot"><span class="chat-context-pill"></span><span class="chat-usage"></span></div>' +
      "</div>";
    this.thread = this.root.querySelector(".chat-thread");
    this.input = this.root.querySelector(".chat-input");
    this.sendBtn = this.root.querySelector(".chat-send");
    this.errorBox = this.root.querySelector(".chat-error");
    this.usage = this.root.querySelector(".chat-usage");

    const self = this;
    this.sendBtn.addEventListener("click", function () { self.send(); });
    this.input.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); self.send(); }
    });
    this.input.addEventListener("input", function () {
      this.style.height = "auto";
      this.style.height = Math.min(this.scrollHeight, 140) + "px";
    });

    const ctx = pageContext();
    const pill = this.root.querySelector(".chat-context-pill");
    if (ctx && ctx.label && this.mode === "helper") {
      pill.textContent = "Viewing: " + ctx.label;
    }
  };

  ChatPanel.prototype.render = function () {
    this.thread.innerHTML = "";
    if (!this.history.length) { this.renderEmpty(); return; }

    const results = {};
    this.history.forEach(function (m) {
      if (m.role === "tool") results[m.tool_call_id] = m;
    });

    for (const msg of this.history) {
      if (msg.role === "user") this.thread.appendChild(this.userNode(msg.content));
      else if (msg.role === "assistant") this.thread.appendChild(this.assistantNode(msg, results));
    }
    if (this.pending) this.renderConfirm(this.pending);
    drawCharts(this.thread);
    this.scroll();
  };

  ChatPanel.prototype.renderEmpty = function () {
    const self = this;
    const wrap = document.createElement("div");
    wrap.className = "chat-empty";
    wrap.innerHTML = "<h4>" + esc(this.emptyTitle) + "</h4>" +
      (this.emptyBody ? "<p>" + esc(this.emptyBody) + "</p>" : "");
    if (this.suggestions.length) {
      const box = document.createElement("div");
      box.className = "chat-suggestions";
      this.suggestions.forEach(function (text) {
        const b = document.createElement("button");
        b.className = "chat-suggestion";
        b.textContent = text;
        b.addEventListener("click", function () { self.send(text); });
        box.appendChild(b);
      });
      wrap.appendChild(box);
    }
    this.thread.appendChild(wrap);
  };

  ChatPanel.prototype.userNode = function (text) {
    const node = document.createElement("div");
    node.className = "chat-msg chat-msg-user";
    const bubble = document.createElement("div");
    bubble.className = "chat-bubble";
    bubble.textContent = typeof text === "string" ? text : "";
    node.appendChild(bubble);
    return node;
  };

  ChatPanel.prototype.assistantNode = function (msg, results) {
    const self = this;
    const node = document.createElement("div");
    node.className = "chat-msg chat-msg-assistant";

    if (msg.reasoning_content && this.mode !== "helper") {
      node.appendChild(reasoningNode(msg.reasoning_content));
    }
    if (msg.tool_calls && msg.tool_calls.length) {
      const box = document.createElement("div");
      box.className = "chat-tools";
      msg.tool_calls.forEach(function (call) {
        const done = results[call.id];
        const failed = done && /^(Permission denied|Invalid arguments|SQL error|The tool failed|Arguments were not|This surface is read-only|The user declined)/.test(done.content || "");
        box.appendChild(chipNode(
          humanise(call.function.name, call.function.arguments),
          done ? (failed ? "err" : "ok") : "running",
          done ? done.content : null));
      });
      node.appendChild(box);
    }
    if (msg.content) {
      const bubble = document.createElement("div");
      bubble.className = "chat-bubble";
      bubble.innerHTML = markdown(msg.content);
      node.appendChild(bubble);

      const actions = document.createElement("div");
      actions.className = "chat-msg-actions";
      actions.appendChild(actionButton(ICON.save, "Save", function (btn) {
        self.saveToInbox(msg.content, btn);
      }));
      actions.appendChild(actionButton(ICON.copy, "Copy", function (btn) {
        navigator.clipboard.writeText(msg.content).then(function () {
          btn.classList.add("done");
          btn.lastChild.textContent = "Copied";
        });
      }));
      node.appendChild(actions);
    }
    return node;
  };

  function actionButton(icon, label, onClick) {
    const b = document.createElement("button");
    b.className = "chat-act";
    b.innerHTML = icon + "<span>" + label + "</span>";
    b.addEventListener("click", function () { onClick(b); });
    return b;
  }

  function reasoningNode(text) {
    const d = document.createElement("details");
    d.className = "chat-reasoning";
    d.innerHTML = "<summary>Reasoning</summary>" +
                  '<div class="chat-reasoning-body">' + esc(text) + "</div>";
    return d;
  }

  function chipNode(label, state, detail) {
    const chip = document.createElement("div");
    chip.className = "chat-chip " + state + (detail ? " expandable" : "");
    chip.innerHTML = '<span class="chat-chip-dot"></span>' +
                     '<span class="chat-chip-label">' + esc(label) + "</span>";
    if (detail) {
      let open = false, box = null;
      chip.addEventListener("click", function () {
        open = !open;
        if (open) {
          box = document.createElement("div");
          box.className = "chat-chip-detail";
          box.textContent = detail.length > 4000 ? detail.slice(0, 4000) + "…" : detail;
          chip.parentNode.insertBefore(box, chip.nextSibling);
        } else if (box) { box.remove(); box = null; }
      });
    }
    return chip;
  }

  function humanise(name, args) {
    let hint = "";
    try {
      const parsed = typeof args === "string" ? JSON.parse(args || "{}") : (args || {});
      for (const key of ["query", "sql", "name", "title", "invoice_number", "client_id", "product_id"]) {
        if (parsed[key] !== undefined && parsed[key] !== "") {
          hint = " · " + String(parsed[key]).slice(0, 42);
          break;
        }
      }
    } catch (e) {}
    const words = name.replace(/_/g, " ");
    return words.charAt(0).toUpperCase() + words.slice(1) + hint;
  }

  ChatPanel.prototype.scroll = function () {
    this.thread.scrollTop = this.thread.scrollHeight;
  };

  ChatPanel.prototype.showError = function (message, retry) {
    const self = this;
    this.errorBox.style.display = "flex";
    this.errorBox.innerHTML = "<span>" + esc(message) + "</span>";
    if (retry) {
      const b = document.createElement("button");
      b.className = "chat-act";
      b.textContent = "Retry";
      b.addEventListener("click", function () {
        self.errorBox.style.display = "none";
        self.send(retry);
      });
      this.errorBox.appendChild(b);
    }
  };

  ChatPanel.prototype.setBusy = function (busy) {
    this.busy = busy;
    this.sendBtn.disabled = busy;
    this.input.disabled = busy;
  };

  ChatPanel.prototype.send = function (text) {
    const message = (text !== undefined ? text : this.input.value).trim();
    if (!message || this.busy) return;
    this.input.value = "";
    this.input.style.height = "auto";
    this.errorBox.style.display = "none";
    if (!this.history.length) this.thread.innerHTML = "";
    this.thread.appendChild(this.userNode(message));
    this.scroll();
    this.run({ text: message, page_context: pageContext() }, message);
  };

  ChatPanel.prototype.confirm = function (decisions) {
    this.pending = null;
    this.persist();
    this.root.querySelectorAll(".chat-confirm").forEach(function (el) { el.remove(); });
    this.run({ decisions: decisions });
  };

  ChatPanel.prototype.run = function (payload, retryText) {
    const self = this;
    this.setBusy(true);

    const live = document.createElement("div");
    live.className = "chat-msg chat-msg-assistant";
    const tools = document.createElement("div");
    tools.className = "chat-tools";
    const bubble = document.createElement("div");
    bubble.className = "chat-bubble";
    bubble.innerHTML = '<div class="chat-typing"><span></span><span></span><span></span></div>';
    live.appendChild(tools);
    live.appendChild(bubble);
    this.thread.appendChild(live);
    this.scroll();

    const chips = {};
    let streamed = "";

    const body = Object.assign({
      mode: this.mode, history: this.history, history_sig: this.sig,
    }, payload);

    fetch("/chat/api/turn", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(async function (response) {
      if (!response.ok) {
        let detail = "The assistant is unavailable (" + response.status + ").";
        if (response.status === 503) {
          detail = "The assistant is not configured on the server yet.";
        } else if (response.status === 403) {
          detail = "You don't have access to this.";
        } else {
          try {
            const data = await response.json();
            if (data.error) detail = data.error;
            if (data.reset) self.clear();
          } catch (e) {}
        }
        live.remove();
        self.showError(detail, null);
        self.setBusy(false);
        return;
      }

      for await (const [event, data] of sseEvents(response)) {
        if (event === "text") {
          streamed += data.delta;
          bubble.textContent = streamed;
          self.scroll();
        } else if (event === "thinking") {
          if (self.mode !== "helper" && !live.querySelector(".chat-reasoning")) {
            live.insertBefore(reasoningNode(""), tools);
          }
          const box = live.querySelector(".chat-reasoning-body");
          if (box) box.textContent += data.delta;
        } else if (event === "tool_start") {
          const chip = chipNode(humanise(data.name, data.args), "running", null);
          chips[data.tool_call_id] = chip;
          tools.appendChild(chip);
          self.scroll();
        } else if (event === "tool_end") {
          const chip = chips[data.tool_call_id];
          if (chip) chip.className = "chat-chip " + (data.ok ? "ok" : "err");
        } else if (event === "usage") {
          self.usage.textContent = (data.prompt + data.completion).toLocaleString() + " tokens";
        } else if (event === "confirm_required") {
          self.history = data.history; self.sig = data.history_sig;
          self.pending = data.cards; self.persist();
          live.remove(); self.render();
        } else if (event === "done") {
          self.history = data.history; self.sig = data.history_sig;
          self.pending = null; self.persist();
          live.remove(); self.render();
        } else if (event === "error") {
          if (data.history) { self.history = data.history; self.sig = data.history_sig; self.persist(); }
          live.remove(); self.render();
          self.showError(data.message, data.retryable ? retryText : null);
        }
      }
      self.setBusy(false);
      self.input.focus();
    }).catch(function (err) {
      live.remove();
      self.showError("Lost connection to the server. Your message was not sent.", retryText);
      self.setBusy(false);
    });
  };

  ChatPanel.prototype.renderConfirm = function (cards) {
    const self = this;
    const decisions = {};
    cards.forEach(function (card) { decisions[card.tool_call_id] = false; });

    cards.forEach(function (card) {
      const box = document.createElement("div");
      box.className = "chat-confirm" + (card.danger ? " danger" : "");
      box.innerHTML =
        '<div class="chat-confirm-title">' + ICON.warn + esc(card.title) + "</div>" +
        '<div class="chat-confirm-lines">' +
        card.lines.map(function (line) {
          const at = line.indexOf(":");
          const key = at > 0 ? line.slice(0, at) : "";
          const val = at > 0 ? line.slice(at + 1).trim() : line;
          return '<div class="chat-confirm-line"><b>' + esc(key) + "</b><span>" + esc(val) + "</span></div>";
        }).join("") +
        "</div>";

      let typed = null;
      if (card.danger) {
        typed = document.createElement("input");
        typed.className = "chat-confirm-typed";
        typed.placeholder = 'Type DELETE to allow this';
        box.appendChild(typed);
      }

      const actions = document.createElement("div");
      actions.className = "chat-confirm-actions";
      const yes = document.createElement("button");
      yes.className = "btn btn-sm " + (card.danger ? "btn-danger" : "btn-primary");
      yes.textContent = card.danger ? "Delete" : "Confirm";
      const no = document.createElement("button");
      no.className = "btn btn-sm btn-secondary";
      no.textContent = "Cancel";

      yes.addEventListener("click", function () {
        if (typed && typed.value.trim().toUpperCase() !== "DELETE") {
          typed.focus();
          return;
        }
        decisions[card.tool_call_id] = true;
        finish();
      });
      no.addEventListener("click", function () { finish(); });

      actions.appendChild(yes);
      actions.appendChild(no);
      const note = document.createElement("span");
      note.className = "chat-confirm-note";
      note.textContent = "Nothing has changed yet.";
      actions.appendChild(note);
      box.appendChild(actions);
      self.thread.appendChild(box);
    });

    function finish() {
      self.confirm(decisions);
    }
    this.scroll();
  };

  ChatPanel.prototype.saveToInbox = function (content, button) {
    const title = (content.split("\n").find(function (l) { return l.trim(); }) || "Saved note")
      .replace(/^[#>*\-\s]+/, "").slice(0, 80);
    fetch("/chat/api/inbox", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: title, body_md: content }),
    }).then(function (r) { return r.json(); }).then(function (data) {
      if (data.error) return;
      button.classList.add("done");
      button.lastChild.textContent = "Saved";
      Bell.refresh();
    }).catch(function () {});
  };

  /* ── Inbox ───────────────────────────────────────────────────────────── */
  function Inbox(root) {
    this.root = root;
    this.root.classList.add("chat-inbox");   // add, don't replace: the pane's
    this.loaded = false;                      // own class is the tab's handle
  }

  Inbox.prototype.load = function () {
    const self = this;
    this.root.innerHTML = '<div class="chat-empty"><p>Loading…</p></div>';
    fetch("/chat/api/inbox").then(function (r) { return r.json(); }).then(function (data) {
      self.loaded = true;
      self.renderItems(data.items || []);
      Bell.set(data.unread || 0);
    }).catch(function () {
      self.root.innerHTML = '<div class="chat-empty"><p>Could not load the inbox.</p></div>';
    });
  };

  Inbox.prototype.renderItems = function (items) {
    const self = this;
    this.root.innerHTML = "";
    if (!items.length) {
      this.root.innerHTML =
        '<div class="chat-empty"><h4>Nothing saved yet</h4>' +
        "<p>Answers you save, reminders and scheduled reports arrive here.</p></div>";
      return;
    }
    const bar = document.createElement("div");
    bar.style.cssText = "display:flex;justify-content:flex-end;margin-bottom:8px";
    const readAll = document.createElement("button");
    readAll.className = "chat-act";
    readAll.textContent = "Mark all read";
    readAll.addEventListener("click", function () {
      fetch("/chat/api/inbox/read-all", { method: "POST" })
        .then(function () { self.load(); });
    });
    bar.appendChild(readAll);
    this.root.appendChild(bar);

    items.forEach(function (item) { self.root.appendChild(self.itemNode(item)); });
  };

  Inbox.prototype.itemNode = function (item) {
    const self = this;
    const node = document.createElement("div");
    node.className = "chat-inbox-item" + (item.read ? "" : " unread");
    node.innerHTML =
      '<div class="chat-inbox-head">' +
      '<span class="chat-inbox-kind kind-' + esc(item.kind) + '">' + esc(item.kind) + "</span>" +
      '<span class="chat-inbox-title">' + esc(item.title) + "</span>" +
      '<span class="chat-inbox-when">' + esc(when(item.created_at)) + "</span></div>";

    const body = document.createElement("div");
    body.className = "chat-inbox-body collapsed chat-bubble";
    body.style.background = "none";
    body.style.border = "none";
    body.style.padding = "0";
    body.innerHTML = markdown(item.body_md || "");
    node.appendChild(body);
    drawCharts(node);

    const actions = document.createElement("div");
    actions.className = "chat-inbox-actions";
    let expanded = false;
    actions.appendChild(actionButton("", "Expand", function (btn) {
      expanded = !expanded;
      body.classList.toggle("collapsed", !expanded);
      btn.lastChild.textContent = expanded ? "Collapse" : "Expand";
    }));
    if (!item.read) {
      actions.appendChild(actionButton("", "Mark read", function (btn) {
        fetch("/chat/api/inbox/" + item.id + "/read", { method: "POST" })
          .then(function (r) { return r.json(); })
          .then(function (d) { node.classList.remove("unread"); btn.remove(); Bell.set(d.unread); });
      }));
    }
    actions.appendChild(actionButton("", "Delete", function () {
      fetch("/chat/api/inbox/" + item.id, { method: "DELETE" })
        .then(function (r) { return r.json(); })
        .then(function (d) { node.remove(); Bell.set(d.unread); });
    }));
    (item.files || []).forEach(function (f) {
      const a = document.createElement("a");
      a.className = "chat-act";
      a.href = "/chat/files/" + f.token;
      a.textContent = "⭳ " + f.filename;
      actions.appendChild(a);
    });
    node.appendChild(actions);
    return node;
  };

  function when(iso) {
    if (!iso) return "";
    const then = new Date(iso.replace(" ", "T") + (/[Zz+]/.test(iso) ? "" : "Z"));
    if (isNaN(then)) return iso;
    const mins = Math.round((Date.now() - then.getTime()) / 60000);
    if (mins < 1) return "just now";
    if (mins < 60) return mins + "m ago";
    if (mins < 1440) return Math.round(mins / 60) + "h ago";
    return then.toLocaleDateString(undefined, { day: "numeric", month: "short" });
  }

  /* ── Unread badge ────────────────────────────────────────────────────── */
  const Bell = {
    count: 0,
    set: function (n) {
      this.count = n || 0;
      document.querySelectorAll(".chat-fab-badge").forEach(function (el) {
        el.style.display = n > 0 ? "" : "none";
        el.textContent = n > 9 ? "9+" : String(n);
      });
    },
    refresh: function () {
      const self = this;
      fetch("/chat/api/inbox/unread")
        .then(function (r) { return r.json(); })
        .then(function (d) { self.set(d.unread || 0); })
        .catch(function () {});
    },
  };

  /* ── Page context ────────────────────────────────────────────────────── */
  function pageContext() {
    try {
      const raw = document.body.getAttribute("data-chat-context");
      return raw ? JSON.parse(raw) : null;
    } catch (e) { return null; }
  }

  /* ── Drawer ──────────────────────────────────────────────────────────── */
  function initDrawer() {
    const drawer = document.getElementById("chatDrawer");
    if (!drawer) return;
    const askPane = drawer.querySelector(".chat-pane-ask");
    const inboxPane = drawer.querySelector(".chat-pane-inbox");
    const panel = new ChatPanel(askPane, {
      mode: "helper",
      emptyTitle: "Ask about this page",
      emptyBody: "I can look anything up, but I can't change data — that's the Chat tab.",
      suggestions: drawerSuggestions(),
    });
    const inbox = new Inbox(inboxPane);

    function open(tab) {
      document.body.classList.add("chat-drawer-open");
      if (tab) selectTab(tab);
      if (tab !== "inbox") setTimeout(function () { panel.input.focus(); }, 220);
    }
    function close() { document.body.classList.remove("chat-drawer-open"); }

    function selectTab(name) {
      drawer.querySelectorAll(".chat-tab").forEach(function (t) {
        t.classList.toggle("active", t.dataset.tab === name);
      });
      askPane.parentElement.classList.toggle("active", name === "ask");
      inboxPane.parentElement.classList.toggle("active", name === "inbox");
      if (name === "inbox") inbox.load();
    }

    drawer.querySelectorAll(".chat-tab").forEach(function (t) {
      t.addEventListener("click", function () { selectTab(t.dataset.tab); });
    });
    drawer.querySelector(".chat-close").addEventListener("click", close);
    drawer.querySelector(".chat-clear").addEventListener("click", function () {
      panel.clear();
    });
    document.querySelectorAll(".chat-fab, .chat-bell").forEach(function (el) {
      el.addEventListener("click", function () {
        if (document.body.classList.contains("chat-drawer-open")) { close(); return; }
        open(el.classList.contains("chat-bell") ? "inbox" : "ask");
      });
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && document.body.classList.contains("chat-drawer-open")) close();
    });

    selectTab("ask");
    Bell.refresh();

    // Show what's waiting once per tab, rather than on every page load.
    fetch("/chat/api/inbox/unread").then(function (r) { return r.json(); })
      .then(function (d) {
        Bell.set(d.unread || 0);
        if (d.unread > 0 && !sessionStorage.getItem("ledgerChat:inboxSeen")) {
          try { sessionStorage.setItem("ledgerChat:inboxSeen", "1"); } catch (e) {}
          open("inbox");
        }
      }).catch(function () {});
  }

  function drawerSuggestions() {
    const ctx = pageContext();
    if (ctx && ctx.entity === "client") {
      return ["What's their outstanding balance?",
              "When did they last pay, and how much?",
              "What do they buy most?"];
    }
    if (ctx && ctx.entity === "product") {
      return ["How much stock is left?", "How fast is this selling?"];
    }
    return ["How many clients do we have?",
            "What's overdue right now?",
            "Which products are low on stock?"];
  }

  window.LedgerChat = {
    ChatPanel: ChatPanel, Inbox: Inbox, Bell: Bell, markdown: markdown,
    drawCharts: drawCharts,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initDrawer);
  } else {
    initDrawer();
  }
})();
