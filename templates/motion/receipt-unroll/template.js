/* receipt-unroll: the paper grows as each line prints and scrolls once it
   outgrows the frame; amounts tick up; the total lands in red with a punch. */
(function () {
  var H = window.YTPH, Y = H.Y, c = H.cues, d = H.data, tl = H.tl, E = Y.el;
  H.fill(["label", "title", "source"]);
  var vp = H.el("viewport"), paper = H.el("paper"), items = H.el("items");
  var pad = d.pad, w = Y.stage.w;
  paper.style.height = d.content_h + "px";
  function put(cls, el, x, y, right) {
    var n = document.createElement("div");
    n.className = cls;
    if (right) { n.style.right = pad + "px"; } else { n.style.left = x + "px"; }
    n.style.top = y + "px";
    items.appendChild(n);
    H.text(n, el);
    return n;
  }
  var format = function (v) { return H.fmt(v, d.decimals); };
  H.inUp("#label", c.label, 0.3, 10);
  H.inUp("#title", c.title, 0.6, 24);
  H.inUp("#source", c.source, 0.4, 0);
  tl.fromTo(vp, { height: 0 }, { height: pad * 2, duration: 0.25, ease: "power2.out" }, c.paper);
  d.items.forEach(function (item, i) {
    var cue = c.items[i];
    var lab = put("rc-item", E["item" + i], pad, item.y, false);
    var amt = put("rc-amount", E["amount" + i], 0, item.y, true);
    tl.to(vp, { height: cue.paper_h, duration: 0.25, ease: "power2.out" }, cue.t);
    tl.to(paper, { y: -cue.scroll, duration: 0.25, ease: "power2.out" }, cue.t);
    tl.fromTo([lab, amt], { opacity: 0, y: -8 }, { opacity: 1, y: 0, duration: 0.2, ease: "power2.out" }, cue.t + 0.05);
    H.count(amt, 0, item.amount, cue.t + 0.05, cue.t + 0.35, format, "power1.out");
  });
  var rule = document.createElement("div");
  rule.className = "rc-rule";
  rule.style.left = pad + "px";
  rule.style.right = pad + "px";
  rule.style.top = d.rule_y + "px";
  items.appendChild(rule);
  var tlab = put("rc-total", E.total_label, pad, d.total_y, false);
  var tamt = put("rc-total-amount", E.total_amount, 0, d.amount_y, true);
  tl.to(vp, { height: c.total_paper_h, duration: 0.3, ease: "power2.out" }, c.rule);
  tl.to(paper, { y: -c.total_scroll, duration: 0.3, ease: "power2.out" }, c.rule);
  tl.fromTo(rule, { opacity: 0 }, { opacity: 1, duration: 0.15 }, c.rule);
  tl.fromTo([tlab, tamt], { opacity: 0, scale: 1.18, transformOrigin: "50% 50%" },
    { opacity: 1, scale: 1, duration: 0.35, ease: "back.out(2.2)" }, c.total);
  H.finish();
})();
