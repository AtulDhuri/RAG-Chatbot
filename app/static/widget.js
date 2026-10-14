/*!
 * RAG Website Chatbot — embeddable widget
 *
 * Usage: <script src="https://your-host/widget.js" defer></script>
 *
 * The widget reads the host page's domain straight from the address bar
 * (window.location), registers it with the backend (which crawls + indexes
 * the site on first use), and answers visitor questions from that site.
 *
 * Optional override for local testing: add data-domain="example.com" to the
 * <script> tag to point the widget at a different site.
 */
(function () {
  "use strict";

  if (window.__ragChatbotLoaded) return; // guard against double-inject
  window.__ragChatbotLoaded = true;

  // ---- Resolve the backend base URL from this script's own src ----
  var current =
    document.currentScript ||
    (function () {
      var s = document.getElementsByTagName("script");
      return s[s.length - 1];
    })();
  var API_BASE;
  try {
    API_BASE = new URL(current.src).origin;
  } catch (e) {
    API_BASE = "";
  }

  // ---- Read the domain from the address bar (host + port) ----
  // e.g. "localhost:3000", "www.example.com" -> "example.com"
  function detectDomain() {
    var override = (current.getAttribute("data-domain") || "").trim();
    if (override) return normalizeDomain(override);
    var host = (window.location.hostname || "").replace(/^www\./, "");
    var port = window.location.port;
    // Keep the port only for local/dev origins; public sites use default ports.
    if (port && (host === "localhost" || /^\d+\.\d+\.\d+\.\d+$/.test(host))) {
      return host + ":" + port;
    }
    return host;
  }

  function normalizeDomain(raw) {
    raw = (raw || "").trim().toLowerCase();
    raw = raw.replace(/^https?:\/\//, "").replace(/\/.*$/, "");
    return raw.replace(/^www\./, "");
  }

  var DOMAIN = detectDomain();
  var TITLE = (current.getAttribute("data-title") || "Ask about this site").trim();
  var ACCENT = (current.getAttribute("data-accent") || "#2563eb").trim();

  var registered = false;
  var greeted = false;

  // ------------------------- styles -------------------------
  var css = `
  .ragc-launcher{position:fixed;bottom:20px;right:20px;width:56px;height:56px;border-radius:50%;
    background:${ACCENT};color:#fff;border:none;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.25);
    display:flex;align-items:center;justify-content:center;z-index:2147483000;transition:transform .15s ease}
  .ragc-launcher:hover{transform:scale(1.06)}
  .ragc-launcher svg{width:26px;height:26px}
  .ragc-panel{position:fixed;bottom:88px;right:20px;width:360px;max-width:calc(100vw - 32px);height:520px;
    max-height:calc(100vh - 120px);background:#fff;border-radius:14px;box-shadow:0 12px 40px rgba(0,0,0,.22);
    display:none;flex-direction:column;overflow:hidden;z-index:2147483000;font-family:system-ui,-apple-system,
    Segoe UI,Roboto,Helvetica,Arial,sans-serif}
  .ragc-panel.ragc-open{display:flex}
  .ragc-header{background:${ACCENT};color:#fff;padding:14px 16px;font-size:15px;font-weight:600;
    display:flex;align-items:center;justify-content:space-between;gap:8px}
  .ragc-header-title{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .ragc-close{background:none;border:none;color:#fff;font-size:20px;cursor:pointer;line-height:1}
  .ragc-body{flex:1;padding:14px;overflow-y:auto;background:#f7f8fa;display:flex;flex-direction:column;gap:10px}
  .ragc-msg{max-width:85%;padding:9px 12px;border-radius:12px;font-size:14px;line-height:1.4;white-space:pre-wrap;
    word-wrap:break-word}
  .ragc-user{align-self:flex-end;background:${ACCENT};color:#fff;border-bottom-right-radius:4px}
  .ragc-bot{align-self:flex-start;background:#fff;color:#1f2937;border:1px solid #e5e7eb;border-bottom-left-radius:4px}
  .ragc-sources{font-size:11px;color:#6b7280;margin-top:4px}
  .ragc-sources a{color:${ACCENT};text-decoration:none;display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .ragc-typing{align-self:flex-start;color:#6b7280;font-size:13px;font-style:italic}
  .ragc-footer{display:flex;border-top:1px solid #e5e7eb;background:#fff;padding:10px}
  .ragc-input{flex:1;border:1px solid #d1d5db;border-radius:8px;padding:9px 11px;font-size:14px;outline:none}
  .ragc-input:focus{border-color:${ACCENT}}
  .ragc-send{margin-left:8px;background:${ACCENT};color:#fff;border:none;border-radius:8px;padding:0 14px;
    cursor:pointer;font-size:14px}
  .ragc-send:disabled{opacity:.5;cursor:not-allowed}
  `;
  var styleEl = document.createElement("style");
  styleEl.textContent = css;
  document.head.appendChild(styleEl);

  // ------------------------- DOM -------------------------
  var launcher = document.createElement("button");
  launcher.className = "ragc-launcher";
  launcher.setAttribute("aria-label", "Open chat");
  launcher.innerHTML =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>';

  var panel = document.createElement("div");
  panel.className = "ragc-panel";
  panel.innerHTML =
    '<div class="ragc-header">' +
    '<span class="ragc-header-title">' + escapeHtml(TITLE) + "</span>" +
    '<button class="ragc-close" aria-label="Close">\u00d7</button>' +
    "</div>" +
    '<div class="ragc-body" role="log" aria-live="polite"></div>' +
    '<div class="ragc-footer">' +
    '<input class="ragc-input" type="text" placeholder="Type your question..." aria-label="Message"/>' +
    '<button class="ragc-send">Send</button>' +
    "</div>";

  document.body.appendChild(launcher);
  document.body.appendChild(panel);

  var body = panel.querySelector(".ragc-body");
  var input = panel.querySelector(".ragc-input");
  var sendBtn = panel.querySelector(".ragc-send");
  var closeBtn = panel.querySelector(".ragc-close");

  // Kick off indexing as soon as the widget loads, so the site is being read
  // before the visitor even opens the chat.
  registerDomain();

  // ------------------------- behavior -------------------------
  launcher.addEventListener("click", togglePanel);
  closeBtn.addEventListener("click", togglePanel);
  sendBtn.addEventListener("click", send);
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter") send();
  });

  function togglePanel() {
    var open = panel.classList.toggle("ragc-open");
    if (open) {
      input.focus();
      if (!greeted) {
        greeted = true;
        addBot("Hi! Ask me anything about " + DOMAIN + ".");
      }
    }
  }

  function registerDomain() {
    if (registered || !API_BASE || !DOMAIN) return;
    registered = true;
    fetch(API_BASE + "/api/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ domain: DOMAIN }),
    }).catch(function () {
      /* non-fatal: chat will register on demand too */
    });
  }

  function send() {
    var text = input.value.trim();
    if (!text) return;
    input.value = "";
    addUser(text);
    setBusy(true);
    var typing = addTyping();

    fetch(API_BASE + "/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ domain: DOMAIN, message: text }),
    })
      .then(function (r) {
        if (r.status === 429) throw new Error("rate");
        return r.json();
      })
      .then(function (data) {
        typing.remove();
        addBot(data.answer || "Sorry, I couldn't come up with an answer.", data.sources);
      })
      .catch(function (err) {
        typing.remove();
        if (err && err.message === "rate") {
          addBot("You're sending messages too quickly. Please wait a moment.");
        } else {
          addBot("Something went wrong reaching the assistant. Please try again.");
        }
      })
      .finally(function () {
        setBusy(false);
        input.focus();
      });
  }

  function setBusy(b) {
    sendBtn.disabled = b;
    input.disabled = b;
  }

  function addUser(text) {
    var el = document.createElement("div");
    el.className = "ragc-msg ragc-user";
    el.textContent = text;
    body.appendChild(el);
    scroll();
  }

  function addBot(text, sources) {
    var el = document.createElement("div");
    el.className = "ragc-msg ragc-bot";
    el.textContent = text;
    if (sources && sources.length) {
      var box = document.createElement("div");
      box.className = "ragc-sources";
      box.appendChild(document.createTextNode("Sources:"));
      sources.slice(0, 3).forEach(function (u) {
        var a = document.createElement("a");
        a.href = u;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        a.textContent = u;
        box.appendChild(a);
      });
      el.appendChild(box);
    }
    body.appendChild(el);
    scroll();
  }

  function addTyping() {
    var el = document.createElement("div");
    el.className = "ragc-typing";
    el.textContent = "Assistant is typing...";
    body.appendChild(el);
    scroll();
    return el;
  }

  function scroll() {
    body.scrollTop = body.scrollHeight;
  }

  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
})();
