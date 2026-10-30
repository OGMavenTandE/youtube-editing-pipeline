/* receipt-unroll: the header (label, title) is fixed from frame 1 and the paper hangs
   below it, growing down one printed line at a time. It never scrolls, so every line
   is on screen when the total lands in red. Holds; hard cut. */
(function () {
  var H = window.YTPH, Y = H.Y, c = H.cues, d = H.data, tl = H.tl, E = Y.el;
  H.fill(["label", "title", "source"]);
  var vp = H.el("viewport"), items = H.el("items");
  var pad = d.pad;
  H.el("rc-top").style.height = d.top + "px";
  ["label", "title"].forEach(function (k) {
    var n = H.el(k);
    if (n) n.style.marginBottom = d.gap + "px";
  });
  H.el("paper").style.height = d.content_h + "px";
  vp.style.height = d.paper0 + "px";
  function put(cls, el, y, side) {
    var n = document.createElement("div");
    n.className = cls;
    if (side === "right") { n.style.right = pad + "px"; }
    else if (side === "both") { n.style.left = pad + "px"; n.style.right = pad + "px"; }
    else { n.style.left = pad + "px"; }
    n.style.top = y + "px";
    items.appendChild(n);
    H.text(n, el);
    return n;
  }
  function rule(y) {
    var r = document.createElement("div");
    r.className = "rc-rule";
    r.style.left = pad + "px";
    r.style.right = pad + "px";
    r.style.top = y + "px";
    items.appendChild(r);
    return r;
  }
  var format = function (v) { return H.fmt(v, d.decimals); };
  H.show("#label");
  if (E.header) {
    put("rc-header", E.header, pad, "both");
    rule(d.header_rule_y);
  }
  H.inUp("#title", c.title, 0.6, 24);
  H.inUp("#source", c.source, 0.4, 0);
  d.items.forEach(function (item, i) {
    var cue = c.items[i];
    var nodes = [put("rc-item", E["item" + i], item.y, "left")];
    tl.to(vp, { height: cue.paper_h, duration: 0.25, ease: "power2.out" }, cue.t);
    if (item.amount !== null) {
      var amt = put("rc-amount", E["amount" + i], item.y, "right");
      nodes.push(amt);
      H.count(amt, 0, item.amount, cue.t + 0.05, cue.t + 0.35, format, "power1.out");
    }
    tl.fromTo(nodes, { opacity: 0, y: -8 }, { opacity: 1, y: 0, duration: 0.2, ease: "power2.out" }, cue.t + 0.05);
  });
  var total = rule(d.rule_y);
  var tlab = put("rc-total", E.total_label, d.total_y, "left");
  var tamt = put("rc-total-amount", E.total_amount, d.amount_y, "right");
  tl.to(vp, { height: c.total_paper_h, duration: 0.3, ease: "power2.out" }, c.rule);
  tl.fromTo(total, { opacity: 0 }, { opacity: 1, duration: 0.15 }, c.rule);
  tl.fromTo([tlab, tamt], { opacity: 0, scale: 1.18, transformOrigin: "50% 50%" },
    { opacity: 1, scale: 1, duration: 0.35, ease: "back.out(2.2)" }, c.total);
  H.finish();
})();
