/* Shared playback helpers. Layout, sizes and cue times come from window.YTP,
   written by ytp/motion.py; nothing here carries copy or defaults. */
(function () {
  var Y = window.YTP;
  var tl = gsap.timeline({ paused: true });
  var H = { Y: Y, tl: tl, cues: Y.cues, data: Y.data, el: null };

  H.el = function (id) { return document.getElementById(id); };

  function esc(t) {
    return String(t).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function rgba(hex, alpha) {
    var n = parseInt(hex.slice(1), 16);
    return "rgba(" + (n >> 16) + "," + ((n >> 8) & 255) + "," + (n & 255) + "," + alpha + ")";
  }
  H.rgba = rgba;

  /* Fill a node with a fitted element: font, size, tracking and the exact lines. */
  H.text = function (node, f) {
    if (!node || !f) return node;
    node.classList.add("ytp-text", "ytp-" + f.role);
    var s = node.style;
    s.fontFamily = '"' + f.family + '"';
    s.fontSize = f.size + "px";
    s.fontWeight = f.weight;
    s.letterSpacing = f.tracking + "px";
    s.lineHeight = String(f.lh);
    if (f.opacity < 1) s.color = rgba("#ECE7DD", f.opacity);
    node.innerHTML = f.lines.map(function (line) {
      return '<span class="l">' + line.map(function (run, i) {
        var w = esc(run[0]);
        var gap = i === 0 || run[2] ? "" : " ";
        return gap + (run[1] ? '<em class="w">' + w + "</em>" : '<span class="w">' + w + "</span>");
      }).join("") + "</span>";
    }).join("");
    return node;
  };

  /* Fill every named element, and drop the nodes for optional ones left out. */
  H.fill = function (names) {
    names.forEach(function (k) {
      var node = H.el(k);
      if (!node) return;
      if (Y.el[k]) H.text(node, Y.el[k]);
      else node.parentNode.removeChild(node);
    });
  };

  H.place = function () {
    var st = H.el("stage"), s = Y.stage;
    st.style.left = s.x + "px";
    st.style.top = s.y + "px";
    st.style.width = s.w + "px";
    st.style.height = s.h + "px";
  };

  /* On screen from frame 1: no fade, no slide. Labels use this so a hard cut never
     lands on empty ink. */
  H.show = function (target) {
    if (typeof target === "string" && !document.querySelector(target)) return;
    tl.set(target, { opacity: 1 }, 0);
  };

  H.inUp = function (target, at, dur, dy) {
    if (at === null || at === undefined) return;
    if (typeof target === "string" && !document.querySelector(target)) return;
    tl.fromTo(target, { opacity: 0, y: (dy || 0) * Y.stage.scale },
      { opacity: 1, y: 0, duration: dur || 0.35, ease: "power2.out" }, at);
  };

  H.fmt = function (v, decimals) {
    var d = decimals || 0;
    var fixed = Number(v).toFixed(d);
    var parts = fixed.split(".");
    parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return parts.join(".");
  };

  /* A number that counts from a to b between t0 and t1. It stays hidden until t0,
     so a row never shows its starting value ("0 tokens") before the count runs. */
  H.count = function (node, a, b, t0, t1, format, ease) {
    var o = { v: a };
    node.textContent = format(a);
    tl.set(node, { opacity: 0 }, 0);
    tl.set(node, { opacity: 1 }, t0);
    tl.to(o, {
      v: b, duration: t1 - t0, ease: ease || "power2.out",
      onUpdate: function () { node.textContent = format(o.v); }
    }, t0);
  };

  /* Pad to the clip length and hand the timeline to HyperFrames. No end fade. */
  H.finish = function () {
    tl.set({}, {}, Y.duration);
    window.__timelines = window.__timelines || {};
    window.__timelines["ytp-motion"] = tl;
  };

  H.place();
  window.YTPH = H;
})();
